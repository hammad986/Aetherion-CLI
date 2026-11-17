"""Hermes ChatML/XML provider adapter."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

from ..engine.loop import (
    Capabilities,
    DoneEvent,
    ModelEvent,
    ModelRequest,
    ReasoningDelta,
    TextDelta,
    ToolCallEvent,
)
from ..parsers.hermes import HermesStreamParser, ParserError, ToolArgsDelta, ToolCallStart


def render_chatml(messages: Iterable[Mapping[str, Any]], system: str, tools: Iterable[Mapping[str, Any]] = ()) -> str:
    """Render normalized messages into Hermes-compatible ChatML."""

    parts = [f"<|im_start|>system\n{system}\n<|im_end|>"]
    tool_text = list(tools)
    if tool_text:
        parts.append(
            "<|im_start|>system\nAvailable tools:\n"
            + "\n".join(str(tool) for tool in tool_text)
            + "\n<|im_end|>"
        )
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(block) for block in content)
        parts.append(f"<|im_start|>{role}\n{content}\n<|im_end|>")
    parts.append("<|im_start|>assistant\n")
    return "\n".join(parts)


class HermesProvider:
    """Bridge an injected streaming client and the normalized engine events."""

    capabilities = Capabilities(native_tools=True, strict_schemas=False, parallel_calls=True, max_context=32_768)

    def __init__(self, client: Any, *, tool_schemas: Iterable[Mapping[str, Any]] = ()) -> None:
        self.client = client
        self.tool_schemas = tuple(tool_schemas)

    async def stream(self, request: ModelRequest, *, cancel) -> AsyncIterator[ModelEvent]:
        parser = HermesStreamParser(turn_id=request.turn_id or "hermes")
        messages = render_chatml(request.messages, request.system, getattr(request, "tools", ()) or self.tool_schemas)
        source = self.client.stream(messages)
        if inspect.isawaitable(source):
            source = await source
        async for chunk in source:
            if cancel.is_set():
                return
            text = _chunk_text(chunk)
            if text == "[DONE]":
                break
            for event in parser.feed(text):
                normalized = _normalize_parser_event(event)
                if normalized is not None:
                    yield normalized
        for event in parser.finish():
            normalized = _normalize_parser_event(event)
            if normalized is not None:
                yield normalized


def _normalize_parser_event(event: Any) -> ModelEvent | None:
    if isinstance(event, (TextDelta, ReasoningDelta, ToolCallEvent, DoneEvent)):
        return event
    if isinstance(event, (ToolCallStart, ToolArgsDelta, ParserError)):
        if isinstance(event, ParserError):
            raise ValueError(f"Hermes parser error: {event.message}")
        return None
    return None


def _chunk_text(chunk: Any) -> str:
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    if isinstance(chunk, Mapping):
        for key in ("text", "content", "delta"):
            if isinstance(chunk.get(key), str):
                return chunk[key]
    for key in ("text", "content", "delta"):
        value = getattr(chunk, key, None)
        if isinstance(value, str):
            return value
    return ""
