"""OpenAI Responses-style adapter and strict parallel-call assembler."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..engine.loop import (
    Capabilities,
    DoneEvent,
    ModelEvent,
    ModelRequest,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    UsageEvent,
)


@dataclass(slots=True)
class _PendingCall:
    call_id: str
    name: str
    arguments: str = ""
    completed: bool = False


class ResponseToolAssembler:
    """Validate Responses API call/argument/output ordering invariants."""

    def __init__(self) -> None:
        self.calls: dict[str, _PendingCall] = {}
        self.outputs: set[str] = set()

    def add_call(self, call_id: str, name: str, arguments: str = "") -> ToolCall:
        if not call_id or call_id in self.calls:
            raise ValueError("duplicate or empty OpenAI call_id")
        pending = _PendingCall(call_id, name, arguments)
        self.calls[call_id] = pending
        return ToolCall(call_id, name, _parse_arguments(arguments))

    def append_arguments(self, call_id: str, delta: str) -> None:
        pending = self.calls.get(call_id)
        if pending is None:
            raise ValueError(f"argument delta references unknown call_id {call_id}")
        if pending.completed:
            raise ValueError(f"argument delta arrives after call completion: {call_id}")
        pending.arguments += delta

    def complete_call(self, call_id: str) -> ToolCall:
        pending = self.calls.get(call_id)
        if pending is None:
            raise ValueError(f"completion references unknown call_id {call_id}")
        if pending.completed:
            raise ValueError(f"duplicate call completion: {call_id}")
        pending.completed = True
        return ToolCall(call_id, pending.name, _parse_arguments(pending.arguments))

    def accept_output(self, call_id: str) -> None:
        if call_id not in self.calls:
            raise ValueError(f"output references unknown call_id {call_id}")
        if call_id in self.outputs:
            raise ValueError(f"duplicate output for call_id {call_id}")
        self.outputs.add(call_id)


class OpenAIProvider:
    """Normalize injected Responses API clients without importing the SDK."""

    capabilities = Capabilities(native_tools=True, strict_schemas=True, parallel_calls=True, max_context=200_000)

    def __init__(self, client: Any, *, model: str, tools: Iterable[Mapping[str, Any]] = ()) -> None:
        self.client = client
        self.model = model
        self.tools = tuple(tools)

    async def stream(self, request: ModelRequest, *, cancel) -> AsyncIterator[ModelEvent]:
        payload = {
            "model": self.model,
            "input": list(request.messages),
            "instructions": request.system,
            "tools": _openai_tools(getattr(request, "tools", ()) or self.tools),
            "parallel_tool_calls": True,
            "max_output_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
        }
        response_api = getattr(self.client, "responses", self.client)
        create = getattr(response_api, "create", None)
        if create is None:
            raise TypeError("OpenAI client must expose responses.create")
        stream = create(**payload)
        if inspect.isawaitable(stream):
            stream = await stream
        assembler = ResponseToolAssembler()
        async for event in stream:
            if cancel.is_set():
                return
            normalized = _normalize_response_event(event, assembler)
            if normalized is not None:
                yield normalized
        yield DoneEvent("stop")


def _normalize_response_event(event: Any, assembler: ResponseToolAssembler) -> ModelEvent | None:
    data = _as_mapping(event)
    event_type = str(data.get("type", ""))
    if event_type in {"response.output_text.delta", "output_text.delta"}:
        return TextDelta(str(data.get("delta", data.get("text", ""))))
    if event_type in {"response.reasoning.delta", "reasoning.delta"}:
        return ReasoningDelta(str(data.get("delta", data.get("text", ""))))
    if event_type in {"response.function_call_arguments.delta", "function_call_arguments.delta"}:
        assembler.append_arguments(str(data["item_id"] if "item_id" in data else data["call_id"]), str(data.get("delta", "")))
        return None
    if event_type in {"response.output_item.added", "output_item.added"}:
        item = _as_mapping(data.get("item", data))
        if item.get("type") in {"function_call", "custom_tool_call"}:
            call_id = str(item.get("call_id") or item.get("id") or "")
            name = str(item.get("name") or item.get("function", {}).get("name") or "")
            args = item.get("arguments", "")
            if isinstance(args, dict):
                args = json.dumps(args)
            assembler.add_call(call_id, name, str(args))
        return None
    if event_type in {"response.function_call_arguments.done", "function_call_arguments.done"}:
        call = assembler.complete_call(str(data.get("call_id") or data.get("item_id")))
        return ToolCallEvent(call)
    if event_type in {"response.completed", "response.done", "completed"}:
        return DoneEvent(str(data.get("reason", "stop")))
    if "usage" in data:
        return UsageEvent(data["usage"])
    return None


def _parse_arguments(arguments: str) -> dict[str, Any]:
    if not arguments:
        return {}
    parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


def _openai_tools(tools: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize the internal registry schema to Responses function tools."""

    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "function" and isinstance(tool.get("function"), Mapping):
            normalized.append(dict(tool))
            continue
        normalized.append(
            {
                "type": "function",
                "name": str(tool.get("name", "")),
                "description": str(tool.get("description", "")),
                "parameters": dict(tool.get("parameters", {})),
                "strict": bool(tool.get("strict", True)),
            }
        )
    return normalized
