"""Anthropic native content-block accumulator with ordering guards."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Iterable, Mapping
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


class NativeContentAccumulator:
    """Guard content-block start/delta/stop ordering and assemble tool JSON."""

    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.stopped: set[int] = set()

    def start(self, index: int, block: Mapping[str, Any]) -> None:
        if index in self.blocks:
            raise ValueError(f"duplicate content block start: {index}")
        block_type = str(block.get("type", ""))
        self.blocks[index] = {
            "type": block_type,
            "id": block.get("id", f"anthropic-block-{index}"),
            "name": block.get("name", ""),
            "text": "",
            "json": "",
        }

    def delta(self, index: int, delta: Mapping[str, Any]) -> ModelEvent | None:
        block = self.blocks.get(index)
        if block is None or index in self.stopped:
            raise ValueError(f"content block delta out of order: {index}")
        delta_type = str(delta.get("type", ""))
        if delta_type in {"text_delta", "thinking_delta"}:
            text = str(delta.get("text", delta.get("thinking", "")))
            block["text"] += text
            return ReasoningDelta(text) if delta_type == "thinking_delta" else TextDelta(text)
        if delta_type == "input_json_delta":
            block["json"] += str(delta.get("partial_json", ""))
            return None
        raise ValueError(f"unsupported Anthropic content delta: {delta_type}")

    def stop(self, index: int) -> ToolCall | None:
        block = self.blocks.get(index)
        if block is None or index in self.stopped:
            raise ValueError(f"content block stop out of order: {index}")
        self.stopped.add(index)
        if block["type"] != "tool_use":
            return None
        arguments = json.loads(block["json"] or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("Anthropic tool input must be an object")
        return ToolCall(str(block["id"]), str(block["name"]), arguments)


class AnthropicProvider:
    """Normalize an injected Anthropic Messages streaming client."""

    capabilities = Capabilities(native_tools=True, strict_schemas=False, parallel_calls=True, max_context=200_000)

    def __init__(self, client: Any, *, model: str, tools: Iterable[Mapping[str, Any]] = ()) -> None:
        self.client = client
        self.model = model
        self.tools = tuple(tools)

    async def stream(self, request: ModelRequest, *, cancel) -> AsyncIterator[ModelEvent]:
        messages_api = getattr(self.client, "messages", self.client)
        create = getattr(messages_api, "stream", None) or getattr(messages_api, "create", None)
        if create is None:
            raise TypeError("Anthropic client must expose messages.stream or messages.create")
        result = create(
            model=self.model,
            system=request.system,
            messages=list(request.messages),
            tools=_anthropic_tools(getattr(request, "tools", ()) or self.tools),
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            stream=True,
        )
        if inspect.isawaitable(result):
            result = await result
        accumulator = NativeContentAccumulator()
        async for event in result:
            if cancel.is_set():
                return
            for normalized in _normalize_anthropic_event(event, accumulator):
                yield normalized
        yield DoneEvent("stop")


def _normalize_anthropic_event(event: Any, accumulator: NativeContentAccumulator) -> list[ModelEvent]:
    data = event if isinstance(event, Mapping) else vars(event) if hasattr(event, "__dict__") else {}
    event_type = str(data.get("type", ""))
    if event_type == "content_block_start":
        accumulator.start(int(data.get("index", 0)), data.get("content_block", {}))
        return []
    if event_type == "content_block_delta":
        normalized = accumulator.delta(int(data.get("index", 0)), data.get("delta", {}))
        return [normalized] if normalized is not None else []
    if event_type == "content_block_stop":
        call = accumulator.stop(int(data.get("index", 0)))
        return [ToolCallEvent(call)] if call else []
    if event_type == "message_delta" and data.get("usage") is not None:
        return [UsageEvent(data["usage"])]
    if event_type == "message_stop":
        return [DoneEvent("stop")]
    return []


def _anthropic_tools(tools: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize internal and OpenAI-shaped schemas to Anthropic tools."""

    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if "input_schema" in tool:
            normalized.append(dict(tool))
            continue
        function = tool.get("function")
        source = function if isinstance(function, Mapping) else tool
        normalized.append(
            {
                "name": str(source.get("name", tool.get("name", ""))),
                "description": str(source.get("description", tool.get("description", ""))),
                "input_schema": dict(source.get("parameters", {})),
            }
        )
    return normalized
