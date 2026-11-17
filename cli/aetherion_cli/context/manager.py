"""Provider-neutral request assembly and bounded conversation context."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..engine.budget import TokenBudget, estimate_tokens
from ..engine.loop import Capabilities, ModelRequest, ToolCall, ToolResult
from .compact import (
    CompactionResult,
    flatten_groups,
    full_compact,
    group_messages,
    micro_compact,
    normalize_message,
    reactive_compact,
)

ContextValue = str | Mapping[str, Any] | Sequence[str] | Sequence[Mapping[str, Any]]
Message = dict[str, Any]


class TranscriptSink(Protocol):
    """The subset of :class:`Transcript` used by context management."""

    @property
    def branch(self) -> list[dict[str, Any]]: ...

    def append_boundary(
        self,
        summary: str,
        *,
        mode: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(slots=True, frozen=True)
class ContextSnapshot:
    """Inspectable context state useful for diagnostics and tests."""

    raw_tokens: int
    request_tokens: int
    effective_input_budget: int
    output_reserve: int
    compaction_mode: str


def _render_value(value: ContextValue | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2)
    if isinstance(value, Sequence):
        rendered: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                rendered.append(json.dumps(dict(item), ensure_ascii=False, sort_keys=True))
            else:
                rendered.append(str(item))
        return "\n".join(rendered)
    return str(value)


def _fit_text(
    text: str,
    max_tokens: int,
    tokenizer: Callable[[str], int] | None,
) -> str:
    """Keep the longest prefix whose deterministic estimate fits exactly."""

    if max_tokens <= 0 or not text:
        return ""
    if estimate_tokens([], [], text, tokenizer) <= max_tokens:
        return text
    low = 0
    high = len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = text[:midpoint]
        if estimate_tokens([], [], candidate, tokenizer) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1
    return text[:low]


def _fit_message(
    message: Mapping[str, Any],
    max_tokens: int,
    tokenizer: Callable[[str], int] | None,
) -> Message | None:
    candidate = normalize_message(message)
    if estimate_tokens([candidate], [], "", tokenizer) <= max_tokens:
        return candidate
    content = candidate.get("content")
    if not isinstance(content, str):
        return None
    low = 0
    high = len(content)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate_content = content[:midpoint]
        trial = {**candidate, "content": candidate_content}
        if estimate_tokens([trial], [], "", tokenizer) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1
    if low == 0:
        return None
    return {**candidate, "content": content[:low]}


class ContextManager:
    """Concrete implementation of the engine's ``ContextManager`` protocol.

    The manager keeps durable conversation records separate from the engine's
    transcript writes.  It consumes the transcript branch at startup and uses
    ``append_boundary`` only for durable compaction boundaries.
    """

    OUTPUT_RESERVE = 16_384
    _VALID_MODES = {"micro", "microcompact", "full", "compact", "reactive"}

    def __init__(
        self,
        transcript: TranscriptSink | None = None,
        *,
        system: str = "",
        system_prompt: str | None = None,
        environment: ContextValue = "",
        env: ContextValue | None = None,
        repo_map: ContextValue = "",
        memory: ContextValue = "",
        summary: str = "",
        pinned_goal: str = "",
        goal: str | None = None,
        tools: Sequence[Mapping[str, Any]] = (),
        token_budget: TokenBudget | None = None,
        context_window: int = 200_000,
        safety_margin: int = 0,
        max_output_tokens: int = OUTPUT_RESERVE,
        temperature: float = 0.2,
        tokenizer: Callable[[str], int] | None = None,
        recent_message_count: int = 12,
    ) -> None:
        if recent_message_count < 1:
            raise ValueError("recent_message_count must be positive")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if token_budget is not None and token_budget.reserved_output != self.OUTPUT_RESERVE:
            raise ValueError("ContextManager requires a 16,384-token output reserve")
        self.transcript = transcript
        self.system = system_prompt if system_prompt is not None else system
        self.environment = _render_value(env if env is not None else environment)
        self.repo_map = _render_value(repo_map)
        self.memory = _render_value(memory)
        self.summary = summary
        self._pinned_goal = pinned_goal or (goal or "")
        self.tools = tuple(dict(tool) for tool in tools)
        self.budget = token_budget or TokenBudget(
            context_window=context_window,
            reserved_output=self.OUTPUT_RESERVE,
            safety_margin=safety_margin,
        )
        self.max_output_tokens = min(max_output_tokens, self.OUTPUT_RESERVE)
        self.temperature = temperature
        self.tokenizer = tokenizer
        self.recent_message_count = recent_message_count
        self._history: list[Message] = []
        self._current_turn: list[Message] = []
        self._mutation_generation = 0
        self._last_micro_generation = -1
        self.last_compaction: CompactionResult | None = None
        self.last_request: ModelRequest | None = None
        self.last_request_tokens = 0
        self.last_raw_tokens = 0
        self.last_mode = "normal"
        self._load_transcript_branch()

    @property
    def pinned_goal(self) -> str:
        """The goal retained across full and reactive compaction."""

        return self._pinned_goal

    @property
    def messages(self) -> list[Message]:
        """Return the un-compacted internal conversation in transcript order."""

        return [*self._history, *self._current_turn]

    @property
    def current_turn(self) -> list[Message]:
        """Return a copy of the active turn."""

        return [normalize_message(message) for message in self._current_turn]

    @property
    def snapshot(self) -> ContextSnapshot:
        """Return the latest token accounting snapshot."""

        return ContextSnapshot(
            self.last_raw_tokens,
            self.last_request_tokens,
            self.budget.effective_input_budget,
            self.OUTPUT_RESERVE,
            self.last_mode,
        )

    def _load_transcript_branch(self) -> None:
        if self.transcript is None:
            return
        loaded: list[Message] = []
        for record in self.transcript.branch:
            if record.get("type") == "system" and record.get("subtype") in {
                "compact_boundary",
                "microcompact_boundary",
            }:
                metadata = record.get("compactMetadata", {})
                retained = metadata.get("retained_messages") if isinstance(metadata, Mapping) else None
                if isinstance(retained, Sequence) and not isinstance(retained, (str, bytes, bytearray)):
                    loaded = [
                        normalize_message(message)
                        for message in retained
                        if isinstance(message, Mapping) and message.get("role")
                    ]
                boundary = str(record.get("content", ""))
                if boundary:
                    self.summary = boundary
                continue
            message = record.get("message")
            if isinstance(message, Mapping) and message.get("role"):
                loaded.append(normalize_message(message))
        self._history.extend(loaded)
        if not self._pinned_goal:
            for message in self._history:
                if message.get("role") == "user" and isinstance(message.get("content"), str):
                    self._pinned_goal = str(message["content"])
                    break

    def _touch(self) -> None:
        self._mutation_generation += 1

    def add_user(self, prompt: str) -> None:
        """Start a new turn, committing the previous active turn to history."""

        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        if self._current_turn:
            self._history.extend(self._current_turn)
            self._current_turn = []
        if not self._pinned_goal:
            self._pinned_goal = prompt
        self._current_turn.append({"role": "user", "content": prompt})
        self._touch()

    def add_assistant(self, text: str, calls: Sequence[ToolCall]) -> None:
        """Append one assistant response and any native tool-use blocks."""

        if not isinstance(text, str):
            raise TypeError("assistant text must be a string")
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for call in calls:
            if isinstance(call, ToolCall):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": dict(call.arguments),
                        "metadata": dict(call.metadata),
                    }
                )
            elif isinstance(call, Mapping):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("call_id", call.get("id", ""))),
                        "name": str(call.get("name", "")),
                        "input": dict(call.get("arguments", call.get("input", {}))),
                    }
                )
            else:
                raise TypeError("calls must contain ToolCall or mapping values")
        content: str | list[dict[str, Any]] = blocks if blocks else text
        self._current_turn.append({"role": "assistant", "content": content})
        self._touch()

    def add_tool_results(self, results: Sequence[ToolResult]) -> None:
        """Append tool results as one provider-compatible result message."""

        blocks: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, ToolResult):
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result.call_id,
                        "content": result.output if result.ok else result.error,
                        "is_error": not result.ok,
                    }
                )
            elif isinstance(result, Mapping):
                call_id = str(result.get("call_id", result.get("tool_use_id", "")))
                ok = bool(result.get("ok", False))
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": result.get("output") if ok else result.get("error"),
                        "is_error": not ok,
                    }
                )
            else:
                raise TypeError("results must contain ToolResult or mapping values")
        if blocks:
            self._current_turn.append({"role": "user", "content": blocks})
            self._touch()

    def set_environment(self, value: ContextValue) -> None:
        self.environment = _render_value(value)
        self._touch()

    def set_repo_map(self, value: ContextValue) -> None:
        self.repo_map = _render_value(value)
        self._touch()

    def set_memory(self, value: ContextValue) -> None:
        self.memory = _render_value(value)
        self._touch()

    def set_summary(self, value: str) -> None:
        self.summary = value
        self._touch()

    def set_pinned_goal(self, value: str) -> None:
        self._pinned_goal = value
        self._touch()

    def record_usage(self, usage: Any) -> None:
        self.budget.record_usage(usage)

    def _budget_for(self, capabilities: Capabilities) -> TokenBudget:
        context_window = self.budget.context_window
        if capabilities.max_context > 0:
            context_window = min(context_window, capabilities.max_context)
        if context_window == self.budget.context_window:
            return self.budget
        return TokenBudget(
            context_window=context_window,
            reserved_output=self.OUTPUT_RESERVE,
            safety_margin=self.budget.safety_margin,
            micro_threshold=self.budget.micro_threshold,
            full_threshold=self.budget.full_threshold,
            system_environment_ratio=self.budget.system_environment_ratio,
            repo_map_ratio=self.budget.repo_map_ratio,
            memory_ratio=self.budget.memory_ratio,
            summary_ratio=self.budget.summary_ratio,
        )

    def _system_sections(self) -> list[tuple[str, str]]:
        return [
            ("system", self.system),
            ("environment", self.environment),
            ("repo_map", self.repo_map),
            ("memory", self.memory),
            ("summary", self.summary),
        ]

    def _render_system(self, budget: TokenBudget) -> str:
        slices = budget.slices()
        sections = self._system_sections()
        rendered = ""
        shared_limit = slices.system_environment
        for name, content in sections[:2]:
            if not content:
                continue
            section = f"[{name}]\n{content}"
            remaining = max(0, shared_limit - estimate_tokens([], [], rendered, self.tokenizer))
            fitted = _fit_text(section, remaining, self.tokenizer)
            if fitted:
                rendered = f"{rendered}\n\n{fitted}" if rendered else fitted
        for (name, content), limit in zip(
            sections[2:], (slices.repo_map, slices.memory, slices.summary)
        ):
            if not content:
                continue
            section = _fit_text(f"[{name}]\n{content}", limit, self.tokenizer)
            if section:
                rendered = f"{rendered}\n\n{section}" if rendered else section
        return rendered

    def _raw_system(self) -> str:
        return "\n\n".join(
            f"[{name}]\n{content}" for name, content in self._system_sections() if content
        )

    def _raw_estimate(self) -> int:
        return estimate_tokens(
            self.messages,
            self.tools,
            self._raw_system(),
            self.tokenizer,
        )

    def needs_compaction(self) -> bool:
        """Apply Tier 1 automatically, then signal Tier 2 when still needed."""

        raw = self._raw_estimate()
        self.last_raw_tokens = self.budget.record_estimate(raw)
        if self.budget.needs_micro_compaction(raw) and self._last_micro_generation != self._mutation_generation:
            self._apply_compaction("micro")
            self._last_micro_generation = self._mutation_generation
            raw = self._raw_estimate()
            self.last_raw_tokens = self.budget.record_estimate(raw)
        return self.budget.needs_full_compaction(raw)

    def _apply_compaction(self, mode: str) -> None:
        normalized = {"microcompact": "micro", "compact": "full"}.get(mode, mode)
        if normalized not in {"micro", "full", "reactive"}:
            raise ValueError(f"unsupported compaction mode: {mode}")
        history = self._history
        if normalized == "micro":
            result = micro_compact(
                history,
                keep_groups=self.recent_message_count,
                pinned_goal=self._pinned_goal,
            )
        elif normalized == "full":
            result = full_compact(
                history,
                keep_groups=self.recent_message_count,
                pinned_goal=self._pinned_goal,
                previous_summary=self.summary,
            )
        else:
            result = reactive_compact(
                history,
                keep_groups=max(1, self.recent_message_count // 2),
                pinned_goal=self._pinned_goal,
                previous_summary=self.summary,
            )
        self._history = result.retained_messages
        if normalized in {"full", "reactive"} and result.summary:
            self.summary = result.summary
        self.last_compaction = result
        self.last_mode = normalized
        self.budget.record_compaction(normalized)
        self._touch()
        if self.transcript is not None:
            transcript_mode = "microcompact" if normalized == "micro" else "compact"
            if normalized == "reactive":
                transcript_mode = "reactive"
            self.transcript.append_boundary(
                result.summary or self.summary or f"{normalized} compaction completed",
                mode=transcript_mode,
                metadata={
                    "tier": {"micro": 1, "full": 2, "reactive": 3}[normalized],
                    "evicted_messages": len(result.evicted_messages),
                    "pinned_goal": bool(self._pinned_goal),
                    # Store the retained semantic spine so a resumed process
                    # does not resurrect history that was intentionally
                    # compacted.  This is JSONL-safe and keeps the transcript
                    # append-only: recovery never rewrites old rows.
                    "retained_messages": result.retained_messages,
                },
            )

    async def compact(self, *, mode: str = "full") -> None:
        """Run one of the three compaction tiers required by the engine."""

        if mode not in self._VALID_MODES:
            raise ValueError(f"unsupported compaction mode: {mode}")
        self._apply_compaction(mode)

    def _select_tools(self, max_tokens: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for tool in self.tools:
            candidate = [*selected, dict(tool)]
            if estimate_tokens([], candidate, "", self.tokenizer) <= max_tokens:
                selected.append(dict(tool))
        return selected

    def _select_groups(
        self,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int,
        *,
        max_groups: int | None = None,
    ) -> list[Message]:
        groups = group_messages(messages)
        selected: list[list[Message]] = []
        considered = 0
        used = 0
        for group in reversed(groups):
            if max_groups is not None and considered >= max_groups:
                break
            considered += 1
            group_cost = estimate_tokens(group, [], "", self.tokenizer)
            if used + group_cost <= max_tokens:
                selected.insert(0, group)
                used += group_cost
                continue
            # The newest group is the semantic tail.  Skipping it and then
            # selecting older groups would silently drop the latest user/tool
            # exchange and make the request appear to contain stale context.
            # Tool call/result groups are never split.
            break
        return flatten_groups(selected)

    def _select_current(self, max_tokens: int) -> list[Message]:
        groups = group_messages(self._current_turn)
        selected: list[list[Message]] = []
        used = 0
        for group in reversed(groups):
            group_cost = estimate_tokens(group, [], "", self.tokenizer)
            if used + group_cost <= max_tokens:
                selected.insert(0, group)
                used += group_cost
                continue
            if len(group) == 1:
                remaining = max_tokens - used
                fitted = _fit_message(group[0], remaining, self.tokenizer)
                if fitted is not None:
                    selected.insert(0, [fitted])
                    used += estimate_tokens([fitted], [], "", self.tokenizer)
        return flatten_groups(selected)

    def build_request(self, capabilities: Capabilities) -> ModelRequest:
        """Assemble sections in fixed order and enforce the input budget."""

        budget = self._budget_for(capabilities)
        system = self._render_system(budget)
        system_tokens = estimate_tokens([], [], system, self.tokenizer)
        tools = self._select_tools(max(0, budget.effective_input_budget - system_tokens))
        tool_tokens = estimate_tokens([], tools, "", self.tokenizer)
        conversation_budget = max(0, budget.effective_input_budget - system_tokens - tool_tokens)

        current = self._select_current(conversation_budget)
        current_tokens = estimate_tokens(current, [], "", self.tokenizer)
        recent = self._select_groups(
            self._history,
            max(0, conversation_budget - current_tokens),
            max_groups=self.recent_message_count,
        )
        messages = [*recent, *current]

        request_tokens = estimate_tokens(messages, tools, system, self.tokenizer)
        if request_tokens > budget.effective_input_budget:
            # The normal path already accounts exactly. This final guard also
            # protects custom tokenizers whose rounding differs per sequence.
            messages = self._select_current(max(0, conversation_budget - 1))
            request_tokens = estimate_tokens(messages, tools, system, self.tokenizer)
            if request_tokens > budget.effective_input_budget:
                tools = []
                request_tokens = estimate_tokens(messages, tools, system, self.tokenizer)
            if request_tokens > budget.effective_input_budget:
                system = _fit_text(
                    system,
                    max(0, budget.effective_input_budget - estimate_tokens(messages, tools, "", self.tokenizer)),
                    self.tokenizer,
                )
                request_tokens = estimate_tokens(messages, tools, system, self.tokenizer)

        self.last_request_tokens = request_tokens
        self.last_raw_tokens = self._raw_estimate()
        self.budget.record_estimate(self.last_raw_tokens)
        request = ModelRequest(
            messages=messages,
            tools=tools,
            system=system,
            max_tokens=self.max_output_tokens,
            temperature=self.temperature,
            stream=True,
        )
        self.last_request = request
        return request


__all__ = ["ContextManager", "ContextSnapshot", "ContextValue", "TranscriptSink"]
