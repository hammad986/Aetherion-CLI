"""Permission-aware parallel-read/serial-write tool scheduler."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from typing import Any

from ..engine.events import Event, EventType
from ..engine.loop import Engine, ToolCall, ToolResult
from ..engine.state import EngineState
from ..security.permissions import PermissionDecision, PermissionEffect, PermissionGate
from .registry import ToolRegistry, ToolSpec

Hook = Callable[[ToolCall, ToolSpec, Any], Any]


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        permissions: PermissionGate | None = None,
        context: Any = None,
        max_parallel: int = 8,
        pre_tool_hook: Hook | None = None,
        post_tool_hook: Hook | None = None,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self.registry = registry
        self.permissions = permissions
        self.context = context
        self.max_parallel = max_parallel
        self.pre_tool_hook = pre_tool_hook
        self.post_tool_hook = post_tool_hook
        self._semaphore = asyncio.Semaphore(max_parallel)

    async def run(self, calls: Sequence[ToolCall], engine: Engine, step: int) -> list[ToolResult]:
        decisions: list[PermissionDecision] = []
        prepared: list[tuple[ToolCall, ToolSpec, Any] | None] = []
        for call in calls:
            try:
                spec = self.registry.get(call.name)
                data = self.registry.parse_arguments(spec, call.arguments)
                if self.pre_tool_hook is not None:
                    await _maybe_await(self.pre_tool_hook(call, spec, self.context or engine))
                decision = await self._check_permission(call, spec, engine)
                decisions.append(decision)
                if decision.effect is PermissionEffect.ASK:
                    decision = await self._resolve_ask(decision, call, spec, data, engine)
                    decisions[-1] = decision
                if decision.effect is not PermissionEffect.ALLOW:
                    prepared.append(None)
                    continue
                prepared.append((call, spec, data))
            except Exception as exc:
                decisions.append(PermissionDecision.deny(f"tool validation failed: {exc}"))
                prepared.append(None)

        results: list[ToolResult | None] = [None] * len(calls)
        read_items = [
            (index, item)
            for index, item in enumerate(prepared)
            if item is not None and item[1].is_read_only and item[1].is_concurrency_safe
        ]
        write_items = [
            (index, item)
            for index, item in enumerate(prepared)
            if item is not None and not (item[1].is_read_only and item[1].is_concurrency_safe)
        ]

        if read_items:
            outcomes = await asyncio.gather(
                *(self._execute(index, item, engine, step) for index, item in read_items),
                return_exceptions=True,
            )
            for (index, item), outcome in zip(read_items, outcomes):
                results[index] = _outcome_to_result(item[0], outcome)
        for index, item in write_items:
            results[index] = await self._execute(index, item, engine, step)

        for index, (call, decision) in enumerate(zip(calls, decisions)):
            if results[index] is None:
                results[index] = ToolResult(call.call_id, False, error=f"denied: {decision.reason}")
        return [
            result if result is not None else ToolResult(calls[index].call_id, False, error="tool did not produce a result")
            for index, result in enumerate(results)
        ]

    async def _execute(self, index: int, item: tuple[ToolCall, ToolSpec, Any], engine: Engine, step: int) -> ToolResult:
        call, spec, data = item
        if getattr(engine, "cancel", None) is not None and engine.cancel.is_set():
            return ToolResult(call.call_id, False, error="cancelled before tool execution")
        if hasattr(engine, "_set"):
            engine._set(EngineState.EXECUTING, getattr(engine, "current_turn_id", "") or "", step)
        await self._publish(engine, EventType.TOOL_STARTED, {"call_id": call.call_id, "name": call.name}, step)
        async with self._semaphore:
            previous_progress = getattr(self.context, "on_tool_output", None)

            async def publish_progress(stream: str, text: str) -> None:
                await self._publish(
                    engine,
                    EventType.TOOL_PROGRESS,
                    {"call_id": call.call_id, "name": call.name, "stream": stream, "text": text},
                    step,
                )

            if self.context is not None and hasattr(self.context, "on_tool_output"):
                self.context.on_tool_output = publish_progress
            try:
                output = await spec.invoke(data, self.context or engine)
                if isinstance(output, ToolResult):
                    result = (
                        output
                        if output.call_id == call.call_id
                        else ToolResult(call.call_id, output.ok, output=output.output, error=output.error)
                    )
                else:
                    result = ToolResult(call.call_id, True, output=output)
                await _maybe_await(self.post_tool_hook(call, spec, result) if self.post_tool_hook else None)
                await self._publish(engine, EventType.TOOL_FINISHED, {"call_id": call.call_id, "name": call.name, "output": result.output}, step)
                return result
            except Exception as exc:
                result = ToolResult(call.call_id, False, error=f"{type(exc).__name__}: {exc}")
                await self._publish(engine, EventType.TOOL_FAILED, {"call_id": call.call_id, "name": call.name, "error": result.error}, step)
                return result
            finally:
                if self.context is not None and hasattr(self.context, "on_tool_output"):
                    self.context.on_tool_output = previous_progress

    async def _check_permission(self, call: ToolCall, spec: ToolSpec, engine: Engine) -> PermissionDecision:
        if self.permissions is None:
            return PermissionDecision.allow("no permission gate configured for this injected scheduler")
        return await _maybe_await(self.permissions.check(call, engine, spec))

    async def _resolve_ask(
        self,
        decision: PermissionDecision,
        call: ToolCall,
        spec: ToolSpec,
        data: Any,
        engine: Engine,
    ) -> PermissionDecision:
        request_id = f"{getattr(engine, 'current_turn_id', '') or 'turn'}:permission:{call.call_id}"
        details: dict[str, Any] = {
            "request_id": request_id,
            "call_id": call.call_id,
            "tool_name": call.name,
            "arguments": dict(call.arguments),
            "reason": decision.reason,
            "risk": {
                "tier": int(decision.risk.tier) if decision.risk is not None else None,
                "reason": decision.risk.reason if decision.risk is not None else decision.reason,
            },
        }
        if spec.preview is not None:
            try:
                preview = await _maybe_await(spec.preview(data, self.context or engine))
                if isinstance(preview, dict):
                    details.update(preview)
            except Exception as exc:
                return PermissionDecision.deny(f"preview failed closed: {exc}", risk=decision.risk)
        if hasattr(engine, "_set"):
            engine._set(EngineState.AWAITING_APPROVAL, getattr(engine, "current_turn_id", "") or "", 0)
        await self._publish(engine, EventType.PERMISSION_REQUEST, details, 0)
        if self.permissions is None:
            return PermissionDecision.deny("approval required")
        resolver = getattr(self.permissions, "resolve", None)
        if resolver is None:
            return PermissionDecision.deny("approval required")
        resolved = await _maybe_await(resolver(decision, call, engine))
        await self._publish(
            engine,
            EventType.PERMISSION_RESOLVED,
            {"request_id": request_id, "call_id": call.call_id, "effect": resolved.effect.value},
            0,
        )
        return resolved

    async def _publish(self, engine: Engine, event_type: EventType, data: dict[str, Any], step: int) -> None:
        turn_id = getattr(engine, "current_turn_id", None) or ""
        engine.bus.publish(Event(event_type, data, step=step, turn_id=turn_id))


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _outcome_to_result(call: ToolCall, outcome: Any) -> ToolResult:
    if isinstance(outcome, BaseException):
        return ToolResult(call.call_id, False, error=f"{type(outcome).__name__}: {outcome}")
    return outcome if isinstance(outcome, ToolResult) else ToolResult(call.call_id, True, output=outcome)
