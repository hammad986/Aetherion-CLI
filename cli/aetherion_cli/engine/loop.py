"""Dependency-injected asynchronous Aetherion turn loop.

Phase 0/1 intentionally defines protocol boundaries rather than importing
provider, tool, permission, planner, or context implementations that belong to
later phases. The engine is therefore testable with a fake model and remains
independent of any vendor SDK.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import uuid4

from .events import Event, EventBus, EventType
from .state import EngineState, transition


@dataclass(slots=True, frozen=True)
class ModelRequest:
    messages: Sequence[dict[str, Any]]
    tools: Sequence[dict[str, Any]]
    system: str
    max_tokens: int
    temperature: float
    stream: bool = True
    turn_id: str = ""


@dataclass(slots=True, frozen=True)
class Capabilities:
    native_tools: bool = False
    strict_schemas: bool = False
    parallel_calls: bool = False
    prompt_caching: bool = False
    max_context: int = 0


@dataclass(slots=True, frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]
    kind: str = "function"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ToolResult:
    call_id: str
    ok: bool
    output: Any = None
    error: str | None = None


@dataclass(slots=True, frozen=True)
class TextDelta:
    text: str


@dataclass(slots=True, frozen=True)
class ReasoningDelta:
    text: str


@dataclass(slots=True, frozen=True)
class ToolCallEvent:
    call: ToolCall


@dataclass(slots=True, frozen=True)
class UsageEvent:
    usage: Any


@dataclass(slots=True, frozen=True)
class DoneEvent:
    reason: str | None = None


ModelEvent = TextDelta | ReasoningDelta | ToolCallEvent | UsageEvent | DoneEvent


class Provider(Protocol):
    capabilities: Capabilities

    def stream(self, request: ModelRequest, *, cancel: asyncio.Event) -> AsyncIterator[ModelEvent]: ...


class ToolScheduler(Protocol):
    async def run(self, calls: Sequence[ToolCall], engine: Engine, step: int) -> Sequence[ToolResult]: ...


class ContextManager(Protocol):
    def add_user(self, prompt: str) -> None: ...

    def add_assistant(self, text: str, calls: Sequence[ToolCall]) -> None: ...

    def add_tool_results(self, results: Sequence[ToolResult]) -> None: ...

    def needs_compaction(self) -> bool: ...

    async def compact(self, *, mode: str = "full") -> None: ...

    def build_request(self, capabilities: Capabilities) -> ModelRequest: ...


class PermissionGate(Protocol):
    async def check(self, call: ToolCall, engine: Engine) -> Any: ...


class PlanRunner(Protocol):
    async def propose(self, prompt: str) -> Any: ...

    async def approve(self, plan: Any) -> bool: ...


@dataclass(slots=True)
class AetherionConfig:
    max_steps: int = 24
    plan_mode: bool = False
    temperature: float = 0.2
    max_output_tokens: int = 16_384
    max_context_retries: int = 2

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.max_context_retries < 1:
            raise ValueError("max_context_retries must be positive")


Config = AetherionConfig


class EngineError(RuntimeError):
    """Base class for recoverable engine lifecycle failures."""


class TurnBusyError(EngineError):
    """Raised when a second turn is started while one is active."""


class ContextOverflowError(EngineError):
    """Raised by a provider when the request exceeds its context window."""


@dataclass(slots=True, frozen=True)
class TurnResult:
    turn_id: str
    state: EngineState
    text: str = ""
    steps: int = 0


class Engine:
    """Orchestrate planning, streaming, tool scheduling, and observation."""

    def __init__(
        self,
        provider: Provider,
        tools: ToolScheduler,
        context: ContextManager,
        permissions: PermissionGate | None,
        planner: PlanRunner | None,
        transcript: Any,
        bus: EventBus,
        config: AetherionConfig,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.context = context
        self.permissions = permissions
        self.planner = planner
        self.transcript = transcript
        self.bus = bus
        self.config = config
        self.state = EngineState.IDLE
        self.cancel = asyncio.Event()
        self.cancel_event = self.cancel
        self.failures: dict[str, int] = {}
        self.current_turn_id: str | None = None
        self.last_result: TurnResult | None = None
        self.last_error: BaseException | None = None
        self._turn_active = False

    def request_cancel(self) -> None:
        """Request cooperative cancellation at the next await boundary."""

        self.cancel.set()

    async def run_turn(self, prompt: str) -> TurnResult:
        """Run one complete turn and return its terminal result.

        Concurrent calls are rejected synchronously before the first await. A
        provider, tool scheduler, or context manager may still observe the
        shared cancellation event and stop its own work cooperatively.
        """

        if self._turn_active:
            raise TurnBusyError("an Aetherion turn is already active")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        self._turn_active = True
        turn_id = str(uuid4())
        self.current_turn_id = turn_id
        self.cancel.clear()
        self.failures.clear()
        self.last_error = None
        step = 0
        accumulated_text = ""
        context_retries = 0

        try:
            if self.state in {EngineState.DONE, EngineState.CANCELLED, EngineState.ERROR}:
                self._set(EngineState.IDLE, turn_id, step)

            self._append_transcript(
                "user",
                {"message": {"role": "user", "content": prompt}},
                turn_id=turn_id,
            )
            self._publish(EventType.USER_INPUT, {"prompt": prompt}, turn_id, step)
            self.context.add_user(prompt)

            if self.config.plan_mode:
                if self.planner is None:
                    raise EngineError("plan_mode is enabled but no planner was injected")
                self._set(EngineState.PLANNING, turn_id, step)
                plan = await self.planner.propose(prompt)
                self._publish(EventType.PLAN_READY, {"plan": _to_serializable(plan)}, turn_id, step)
                if self.cancel.is_set():
                    return await self._finish_cancelled(turn_id, step, accumulated_text)
                if not await self.planner.approve(plan):
                    self._set(EngineState.IDLE, turn_id, step)
                    return self._finish_result(turn_id, EngineState.IDLE, accumulated_text, step)

            self._set(EngineState.STREAMING, turn_id, step)
            for step in range(self.config.max_steps):
                if self.cancel.is_set():
                    return await self._finish_cancelled(turn_id, step, accumulated_text)

                if self.context.needs_compaction():
                    self._set(EngineState.COMPACTING, turn_id, step)
                    self._publish(EventType.CONTEXT_COMPACTING, {"mode": "full"}, turn_id, step)
                    await self.context.compact(mode="full")
                    self._publish(EventType.CONTEXT_COMPACTED, {"mode": "full"}, turn_id, step)
                    self._set(EngineState.STREAMING, turn_id, step)

                request = self.context.build_request(self.provider.capabilities)
                if isinstance(request, ModelRequest):
                    request = replace(request, turn_id=turn_id)
                calls: list[ToolCall] = []
                text_parts: list[str] = []
                usage: Any = None
                try:
                    async for model_event in self.provider.stream(request, cancel=self.cancel):
                        if self.cancel.is_set():
                            return await self._finish_cancelled(turn_id, step, "".join(text_parts))
                        if isinstance(model_event, TextDelta):
                            text_parts.append(model_event.text)
                            self._publish(EventType.TEXT_DELTA, {"text": model_event.text}, turn_id, step)
                        elif isinstance(model_event, ReasoningDelta):
                            self._publish(EventType.REASONING_DELTA, {"text": model_event.text}, turn_id, step)
                        elif isinstance(model_event, ToolCallEvent):
                            calls.append(model_event.call)
                            self._publish(
                                EventType.TOOL_CALL,
                                {"call_id": model_event.call.call_id, "name": model_event.call.name},
                                turn_id,
                                step,
                            )
                        elif isinstance(model_event, UsageEvent):
                            usage = model_event.usage
                        elif isinstance(model_event, DoneEvent):
                            continue
                        else:
                            raise TypeError(f"unsupported normalized model event: {type(model_event).__name__}")
                except asyncio.CancelledError:
                    self.request_cancel()
                    await self._finish_cancelled(turn_id, step, "".join(text_parts))
                    raise
                except Exception as exc:
                    if _is_context_overflow(exc):
                        context_retries += 1
                        if context_retries > self.config.max_context_retries:
                            raise ContextOverflowError(
                                "provider rejected the request after reactive context compaction"
                            ) from exc
                        self._set(EngineState.COMPACTING, turn_id, step)
                        self._publish(
                            EventType.CONTEXT_COMPACTING,
                            {"mode": "reactive", "reason": "context_overflow"},
                            turn_id,
                            step,
                        )
                        await self.context.compact(mode="reactive")
                        self._publish(
                            EventType.CONTEXT_COMPACTED,
                            {"mode": "reactive", "reason": "context_overflow"},
                            turn_id,
                            step,
                        )
                        self._set(EngineState.STREAMING, turn_id, step)
                        continue
                    raise

                text = "".join(text_parts)
                accumulated_text += text
                self.context.add_assistant(text, calls)
                if usage is not None and hasattr(self.context, "record_usage"):
                    self.context.record_usage(usage)

                if not calls:
                    self._set(EngineState.DONE, turn_id, step)
                    self._append_transcript(
                        "assistant",
                        {"message": {"role": "assistant", "content": text}},
                        turn_id=turn_id,
                    )
                    self._publish(EventType.TURN_FINISHED, {"text": accumulated_text}, turn_id, step)
                    return self._finish_result(turn_id, EngineState.DONE, accumulated_text, step + 1)

                self._set(EngineState.TOOL_PENDING, turn_id, step)
                results = await self.tools.run(calls, self, step)
                self.context.add_tool_results(results)
                self._append_tool_records(calls, results, turn_id, step)
                self._set(EngineState.OBSERVING, turn_id, step)
                self._set(EngineState.STREAMING, turn_id, step)

            self._set(EngineState.ERROR, turn_id, self.config.max_steps)
            message = "step limit reached"
            self.last_error = EngineError(message)
            self._publish(EventType.ERROR, {"message": message}, turn_id, self.config.max_steps)
            self._append_transcript("system", {"subtype": "error", "content": message}, turn_id=turn_id)
            return self._finish_result(turn_id, EngineState.ERROR, accumulated_text, self.config.max_steps)
        except asyncio.CancelledError:
            self.request_cancel()
            if self.state not in {EngineState.CANCELLED, EngineState.IDLE}:
                self._set(EngineState.CANCELLED, turn_id, step)
            raise
        except Exception as exc:
            self.last_error = exc
            if self.state not in {EngineState.ERROR, EngineState.IDLE}:
                self._set(EngineState.ERROR, turn_id, step)
            self._publish(EventType.ERROR, {"message": str(exc), "error_type": type(exc).__name__}, turn_id, step)
            self._append_transcript(
                "system",
                {"subtype": "error", "content": str(exc), "error_type": type(exc).__name__},
                turn_id=turn_id,
            )
            return self._finish_result(turn_id, EngineState.ERROR, accumulated_text, step)
        finally:
            self._turn_active = False
            self.current_turn_id = None

    def _set(self, state: EngineState, turn_id: str, step: int) -> None:
        next_state = transition(self.state, state)
        if next_state == self.state:
            return
        self.state = next_state
        self._publish(EventType.STATE_CHANGED, {"state": state.value}, turn_id, step)

    def _publish(self, event_type: EventType, data: dict[str, Any], turn_id: str, step: int) -> None:
        self.bus.publish(Event(event_type, data, step=step, turn_id=turn_id))

    def _append_transcript(self, type_: str, payload: dict[str, Any], *, turn_id: str) -> None:
        append = getattr(self.transcript, "append", None)
        if append is not None:
            append(type_, {**payload, "turn_id": turn_id})

    def _append_tool_records(
        self,
        calls: Sequence[ToolCall],
        results: Sequence[ToolResult],
        turn_id: str,
        step: int,
    ) -> None:
        # Persist one call batch and one result batch.  Keeping the complete
        # batch adjacent lets resumed context management evict calls/results
        # atomically even when a provider issued several calls in parallel.
        call_blocks = [
            {
                "type": "tool_use",
                "id": call.call_id,
                "name": call.name,
                "input": dict(call.arguments),
                "metadata": dict(call.metadata),
            }
            for call in calls
        ]
        if call_blocks:
            self._append_transcript(
                "assistant",
                {"message": {"role": "assistant", "content": call_blocks}},
                turn_id=turn_id,
            )
        result_blocks = []
        for result in results:
            result_data = result if isinstance(result, ToolResult) else _coerce_tool_result(result)
            result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": result_data.call_id,
                    "content": result_data.output if result_data.ok else result_data.error,
                    "is_error": not result_data.ok,
                }
            )
        if result_blocks:
            self._append_transcript(
                "user",
                {"message": {"role": "user", "content": result_blocks}},
                turn_id=turn_id,
            )

    async def _finish_cancelled(self, turn_id: str, step: int, text: str) -> TurnResult:
        if self.state != EngineState.CANCELLED:
            self._set(EngineState.CANCELLED, turn_id, step)
        self._publish(EventType.TURN_CANCELLED, {"text": text}, turn_id, step)
        self._append_transcript("system", {"subtype": "turn_cancelled", "content": text}, turn_id=turn_id)
        return self._finish_result(turn_id, EngineState.CANCELLED, text, step)

    def _finish_result(self, turn_id: str, state: EngineState, text: str, steps: int) -> TurnResult:
        result = TurnResult(turn_id, state, text, steps)
        self.last_result = result
        return result


def _to_serializable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _to_serializable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_to_serializable(item) for item in value]
    if hasattr(value, "model_dump"):
        return _to_serializable(value.model_dump())
    if hasattr(value, "to_list"):
        return _to_serializable(value.to_list())
    return repr(value)


def _coerce_tool_result(value: Any) -> ToolResult:
    if isinstance(value, Mapping):
        return ToolResult(
            call_id=str(value.get("call_id", "")),
            ok=bool(value.get("ok", False)),
            output=value.get("output"),
            error=value.get("error"),
        )
    return ToolResult(call_id=str(getattr(value, "call_id", "")), ok=bool(getattr(value, "ok", False)), output=getattr(value, "output", None), error=getattr(value, "error", None))


def _is_context_overflow(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", getattr(exc, "status", None))
    code = str(
        getattr(exc, "code", "")
        or getattr(exc, "error_code", "")
        or getattr(exc, "type", "")
    ).lower()
    message = str(exc).lower()
    overflow_codes = {
        "context_length_exceeded",
        "context_window_exceeded",
        "prompt_too_long",
        "max_tokens_exceeded",
    }
    if code in overflow_codes:
        return True
    clues = ("context", "token", "length", "too large", "prompt too long")
    if status == 400:
        return any(token in message for token in clues)
    # Some SDKs discard the HTTP status and expose only the provider message.
    # Keep this fallback deliberately narrow so unrelated 400s are not retried.
    return "context window" in message or "context length" in message
