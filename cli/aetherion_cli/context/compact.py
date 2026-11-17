"""Deterministic conversation compaction primitives.

The context manager owns provider-facing budgeting.  This module only knows
how to identify atomic tool exchanges, retain a recent tail, and produce a
bounded local summary when older messages are removed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Message = dict[str, Any]


@dataclass(slots=True)
class CompactionResult:
    """The result of a loss-bearing conversation compaction operation."""

    mode: str
    retained_messages: list[Message]
    evicted_messages: list[Message]
    summary: str

    @property
    def messages(self) -> list[Message]:
        """Compatibility alias for callers that treat the result as a list."""

        return self.retained_messages


def normalize_message(message: Mapping[str, Any]) -> Message:
    """Copy a message into the provider-neutral message shape."""

    return {str(key): value for key, value in message.items()}


def _content_blocks(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = message.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def tool_call_ids(message: Mapping[str, Any]) -> set[str]:
    """Return tool-call IDs contained in an assistant message."""

    if str(message.get("role", "")) != "assistant":
        return set()
    ids = {
        str(block.get("id"))
        for block in _content_blocks(message)
        if block.get("type") in {"tool_use", "tool_call"} and block.get("id")
    }
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, Sequence) and not isinstance(raw_calls, (str, bytes, bytearray)):
        for call in raw_calls:
            if isinstance(call, Mapping):
                call_id = call.get("id") or call.get("call_id")
                if call_id:
                    ids.add(str(call_id))
    return ids


def tool_result_ids(message: Mapping[str, Any]) -> set[str]:
    """Return tool-call IDs contained in a tool-result message."""

    if str(message.get("role", "")) not in {"tool", "user"}:
        return set()
    ids = {
        str(block.get("tool_use_id") or block.get("call_id"))
        for block in _content_blocks(message)
        if block.get("type") in {"tool_result", "tool_response"}
        and (block.get("tool_use_id") or block.get("call_id"))
    }
    call_id = message.get("tool_call_id") or message.get("call_id")
    if call_id:
        ids.add(str(call_id))
    return ids


def group_messages(messages: Sequence[Mapping[str, Any]]) -> list[list[Message]]:
    """Group messages so assistant tool calls and their results are atomic.

    A group is the smallest unit that can be evicted.  Tool results are paired
    only with the immediately following result message and matching call IDs;
    malformed or incomplete exchanges remain standalone rather than being
    silently re-associated with unrelated messages.
    """

    normalized = [normalize_message(message) for message in messages]
    groups: list[list[Message]] = []
    index = 0
    while index < len(normalized):
        current = normalized[index]
        calls = tool_call_ids(current)
        group = [current]
        if calls:
            matched: set[str] = set()
            cursor = index + 1
            # Providers may emit one result message per call (OpenAI) or one
            # aggregate result message (Anthropic/Hermes).  Consume a
            # contiguous result run until every call is paired.  We never scan
            # past a non-result message, so unrelated user turns stay separate.
            while cursor < len(normalized) and matched != calls:
                following = normalized[cursor]
                results = tool_result_ids(following)
                if not results or not calls.intersection(results):
                    break
                group.append(following)
                matched.update(calls.intersection(results))
                cursor += 1
            index = cursor - 1
        groups.append(group)
        index += 1
    return groups


def flatten_groups(groups: Sequence[Sequence[Mapping[str, Any]]]) -> list[Message]:
    """Flatten grouped messages while copying their outer mappings."""

    return [normalize_message(message) for group in groups for message in group]


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                parts.append(str(block))
                continue
            block_type = str(block.get("type", ""))
            if block_type in {"text", "thinking"}:
                parts.append(str(block.get("text", block.get("thinking", ""))))
            elif block_type in {"tool_use", "tool_call"}:
                arguments = block.get("input", block.get("arguments", {}))
                parts.append(f"tool call {block.get('name', '')} {arguments}")
            elif block_type in {"tool_result", "tool_response"}:
                parts.append(f"tool result {block.get('content', '')}")
        return " ".join(part for part in parts if part)
    if isinstance(content, Mapping):
        return json.dumps(dict(content), ensure_ascii=False, sort_keys=True)
    return str(content)


def summarize_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    pinned_goal: str = "",
    previous_summary: str = "",
    mode: str = "full",
    max_chars: int = 8_000,
) -> str:
    """Create a bounded, deterministic summary without external model calls."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    lines: list[str] = [f"Context {mode} compaction summary."]
    if pinned_goal.strip():
        lines.append(f"Pinned goal: {pinned_goal.strip()}")
    if previous_summary.strip():
        lines.append(f"Previous summary: {previous_summary.strip()}")
    if messages:
        lines.append("Earlier conversation:")
    for message in messages:
        role = str(message.get("role", "unknown"))
        text = " ".join(_message_text(message).split())
        if text:
            lines.append(f"- {role}: {text}")
    summary = "\n".join(lines)
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 1].rstrip() + "…"


def _compact(
    messages: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    keep_groups: int,
    pinned_goal: str = "",
    previous_summary: str = "",
) -> CompactionResult:
    if keep_groups < 0:
        raise ValueError("keep_groups must be non-negative")
    groups = group_messages(messages)
    protected_indices = {
        index
        for index, group in enumerate(groups)
        if tool_call_ids(group[0])
        and not tool_call_ids(group[0]).issubset(
            set().union(*(tool_result_ids(message) for message in group[1:]))
        )
    }
    normal_indices = [index for index in range(len(groups)) if index not in protected_indices]
    retained_indices = set(normal_indices[-keep_groups:] if keep_groups else ())
    retained_indices.update(protected_indices)
    retained_groups = [groups[index] for index in sorted(retained_indices)]
    evicted_groups = [groups[index] for index in normal_indices if index not in retained_indices]
    retained = flatten_groups(retained_groups)
    evicted = flatten_groups(evicted_groups)
    summary = summarize_messages(
        evicted,
        pinned_goal=pinned_goal,
        previous_summary=previous_summary,
        mode=mode,
    )
    return CompactionResult(mode, retained, evicted, summary)


def micro_compact(
    messages: Sequence[Mapping[str, Any]],
    *,
    keep_groups: int = 12,
    pinned_goal: str = "",
) -> CompactionResult:
    """Tier 1 compaction: evict only the oldest complete message groups."""

    result = _compact(messages, mode="micro", keep_groups=keep_groups, pinned_goal=pinned_goal)
    result.retained_messages = _trim_tool_outputs(result.retained_messages)
    return result


def full_compact(
    messages: Sequence[Mapping[str, Any]],
    *,
    keep_groups: int = 12,
    pinned_goal: str = "",
    previous_summary: str = "",
) -> CompactionResult:
    """Tier 2 compaction: summarize evicted history while retaining a tail."""

    return _compact(
        messages,
        mode="full",
        keep_groups=keep_groups,
        pinned_goal=pinned_goal,
        previous_summary=previous_summary,
    )


def reactive_compact(
    messages: Sequence[Mapping[str, Any]],
    *,
    keep_groups: int = 4,
    pinned_goal: str = "",
    previous_summary: str = "",
) -> CompactionResult:
    """Tier 3 compaction: retain a smaller tail after provider overflow."""

    return _compact(
        messages,
        mode="reactive",
        keep_groups=keep_groups,
        pinned_goal=pinned_goal,
        previous_summary=previous_summary,
    )


def _trim_tool_outputs(messages: Sequence[Mapping[str, Any]], *, max_chars: int = 600) -> list[Message]:
    """Replace retained verbose tool payloads with bounded execution summaries."""

    trimmed: list[Message] = []
    for raw_message in messages:
        message = normalize_message(raw_message)
        content = message.get("content")
        if isinstance(content, str) and str(message.get("role", "")) == "tool":
            message["content"] = _execution_summary(content, max_chars)
        elif isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            blocks: list[Any] = []
            for raw_block in content:
                if not isinstance(raw_block, Mapping):
                    blocks.append(raw_block)
                    continue
                block = dict(raw_block)
                if block.get("type") in {"tool_result", "tool_response"}:
                    block["content"] = _execution_summary(block.get("content", ""), max_chars)
                blocks.append(block)
            message["content"] = blocks
        trimmed.append(message)
    return trimmed


def _execution_summary(value: Any, max_chars: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return text
    return f"execution summary: {text[: max_chars - 32].rstrip()}… ({len(text)} chars)"


__all__ = [
    "CompactionResult",
    "Message",
    "flatten_groups",
    "full_compact",
    "group_messages",
    "micro_compact",
    "normalize_message",
    "reactive_compact",
    "summarize_messages",
    "tool_call_ids",
    "tool_result_ids",
]
