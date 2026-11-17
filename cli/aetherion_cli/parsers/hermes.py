"""Incremental Hermes XML tool-call and scratchpad parser."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any

from ..engine.loop import DoneEvent, ReasoningDelta, TextDelta, ToolCall, ToolCallEvent


@dataclass(slots=True, frozen=True)
class ToolCallStart:
    index: int


@dataclass(slots=True, frozen=True)
class ToolArgsDelta:
    index: int
    text: str


@dataclass(slots=True, frozen=True)
class ParserError:
    message: str
    index: int | None = None
    raw: str = ""
    recoverable: bool = True


ParserEvent = TextDelta | ReasoningDelta | ToolCallStart | ToolArgsDelta | ToolCallEvent | ParserError | DoneEvent


_OPEN_TAGS = ("<tool_call>", "<scratchpad>")
_CLOSE_TOOL = "</tool_call>"
_CLOSE_SCRATCH = "</scratchpad>"
_MAX_TOOL_BUFFER = 1_048_576


class HermesStreamParser:
    """Parse arbitrary stream chunk boundaries without delaying text output."""

    def __init__(self, *, turn_id: str = "hermes", max_call_bytes: int = _MAX_TOOL_BUFFER) -> None:
        if max_call_bytes < 1024:
            raise ValueError("max_call_bytes is too small")
        self.turn_id = turn_id or "hermes"
        self.max_call_bytes = max_call_bytes
        self._buffer = ""
        self._mode = "text"
        self._tool_raw = ""
        self._scratch_raw = ""
        self._call_index = 0
        self._in_code_fence = False
        self._finished = False

    def feed(self, chunk: str) -> list[ParserEvent]:
        if self._finished:
            raise RuntimeError("parser is already finished")
        if not isinstance(chunk, str):
            raise TypeError("Hermes parser chunks must be strings")
        if not chunk:
            return []
        self._buffer += chunk
        return self._drain()

    def finish(self) -> list[ParserEvent]:
        if self._finished:
            return []
        self._finished = True
        events = self._drain(final=True)
        if self._mode == "scratchpad":
            if self._scratch_raw:
                events.append(ReasoningDelta(self._scratch_raw))
            events.append(ParserError("unterminated scratchpad", raw=self._scratch_raw))
        elif self._mode == "tool":
            events.append(ParserError("unterminated tool_call", self._call_index, self._tool_raw))
        if self._mode == "text" and self._buffer:
            events.append(TextDelta(self._buffer))
            self._buffer = ""
        events.append(DoneEvent("stop"))
        return events

    def _drain(self, *, final: bool = False) -> list[ParserEvent]:
        events: list[ParserEvent] = []
        while True:
            if self._mode == "text":
                progressed = self._drain_text(events, final=final)
            elif self._mode == "scratchpad":
                progressed = self._drain_scratchpad(events, final=final)
            else:
                progressed = self._drain_tool(events, final=final)
            if not progressed:
                break
        return events

    def _drain_text(self, events: list[ParserEvent], *, final: bool) -> bool:
        marker = self._find_open_marker(self._buffer)
        if marker is None:
            if final:
                if self._buffer:
                    events.append(TextDelta(self._buffer))
                    self._buffer = ""
                    return True
                return False
            keep = self._partial_marker_suffix(self._buffer)
            emit = self._buffer[:-keep] if keep else self._buffer
            if emit:
                events.append(TextDelta(emit))
                self._buffer = self._buffer[len(emit) :]
                self._update_code_fence(emit)
                return True
            return False

        index, tag = marker
        if index:
            text = self._buffer[:index]
            events.append(TextDelta(text))
            self._update_code_fence(text)
            self._buffer = self._buffer[index:]
            return True
        self._buffer = self._buffer[len(tag) :]
        if tag == "<tool_call>":
            self._mode = "tool"
            self._tool_raw = ""
            events.append(ToolCallStart(self._call_index))
        else:
            self._mode = "scratchpad"
            self._scratch_raw = ""
        return True

    def _drain_scratchpad(self, events: list[ParserEvent], *, final: bool) -> bool:
        close = self._buffer.find(_CLOSE_SCRATCH)
        if close < 0:
            if final:
                if self._buffer:
                    self._scratch_raw += self._buffer
                    events.append(ReasoningDelta(self._buffer))
                    self._buffer = ""
                    return True
                return False
            keep = self._partial_suffix(self._buffer, _CLOSE_SCRATCH)
            emit = self._buffer[:-keep] if keep else self._buffer
            if emit:
                self._scratch_raw += emit
                events.append(ReasoningDelta(emit))
                self._buffer = self._buffer[len(emit) :]
                return True
            return False
        if close:
            text = self._buffer[:close]
            self._scratch_raw += text
            if text:
                events.append(ReasoningDelta(text))
        self._buffer = self._buffer[close + len(_CLOSE_SCRATCH) :]
        self._mode = "text"
        self._scratch_raw = ""
        return True

    def _drain_tool(self, events: list[ParserEvent], *, final: bool) -> bool:
        combined = self._tool_raw + self._buffer
        close_absolute = self._find_tool_close(combined)
        if close_absolute < 0:
            if len(combined) > self.max_call_bytes:
                raw = combined
                self._tool_raw = ""
                self._buffer = ""
                self._mode = "text"
                events.append(ParserError("tool_call buffer limit exceeded", self._call_index, raw, False))
                self._call_index += 1
                return True
            if final:
                self._tool_raw += self._buffer
                self._buffer = ""
                return False
            keep = self._partial_suffix(self._buffer, _CLOSE_TOOL)
            emit = self._buffer[:-keep] if keep else self._buffer
            if emit:
                self._tool_raw += emit
                events.append(ToolArgsDelta(self._call_index, emit))
                self._buffer = self._buffer[len(emit) :]
                return True
            return False
        close = max(0, close_absolute - len(self._tool_raw))
        payload = self._buffer[:close]
        self._tool_raw += payload
        if payload:
            events.append(ToolArgsDelta(self._call_index, payload))
        self._buffer = self._buffer[close + len(_CLOSE_TOOL) :]
        parsed, error = self._parse_call(self._tool_raw, self._call_index)
        if parsed is not None:
            events.append(ToolCallEvent(parsed))
        if error is not None:
            events.append(error)
        self._call_index += 1
        self._tool_raw = ""
        self._mode = "text"
        return True

    def _find_open_marker(self, text: str) -> tuple[int, str] | None:
        fence = self._in_code_fence
        index = 0
        while index < len(text):
            if text.startswith("```", index):
                fence = not fence
                index += 3
                continue
            if not fence:
                matches = [(text.find(tag, index), tag) for tag in _OPEN_TAGS]
                matches = [(position, tag) for position, tag in matches if position >= 0]
                if matches:
                    return min(matches, key=lambda item: item[0])
            index += 1
        return None

    def _update_code_fence(self, text: str) -> None:
        if text.count("```") % 2:
            self._in_code_fence = not self._in_code_fence

    @staticmethod
    def _partial_marker_suffix(text: str) -> int:
        return HermesStreamParser._partial_suffix_any(text, _OPEN_TAGS)

    @staticmethod
    def _partial_suffix_any(text: str, tags: tuple[str, ...]) -> int:
        for size in range(min(len(text), max(map(len, tags))), 0, -1):
            suffix = text[-size:]
            if any(tag.startswith(suffix) for tag in tags):
                return size
        return 0

    @staticmethod
    def _partial_suffix(text: str, tag: str) -> int:
        for size in range(min(len(text), len(tag)), 0, -1):
            if tag.startswith(text[-size:]):
                return size
        return 0

    @staticmethod
    def _find_tool_close(text: str) -> int:
        in_string = False
        escaped = False
        index = 0
        while index < len(text):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif text.startswith(_CLOSE_TOOL, index):
                return index
            index += 1
        return -1

    def _parse_call(self, raw: str, index: int) -> tuple[ToolCall | None, ParserError | None]:
        value: Any = None
        repaired: list[str] = []
        try:
            value = json.loads(raw.strip())
        except json.JSONDecodeError:
            stripped = raw.strip().strip("`").strip()
            candidate = re.sub(r",\s*([}\]])", r"\1", stripped)
            if candidate != stripped:
                try:
                    value = json.loads(candidate)
                    repaired.append("trailing_comma")
                except json.JSONDecodeError:
                    value = None
            if value is None:
                try:
                    literal = ast.literal_eval(candidate)
                    if isinstance(literal, dict):
                        value = literal
                        repaired.append("python_literal_quotes")
                except (SyntaxError, ValueError):
                    value = None
        if not isinstance(value, dict):
            return None, ParserError("tool_call arguments must be a JSON object", index, raw)
        if set(value) - {"name", "arguments"}:
            return None, ParserError("tool_call contains unknown envelope fields", index, raw)
        name = value.get("name")
        arguments = value.get("arguments")
        if not isinstance(name, str) or not name.strip():
            return None, ParserError("tool_call name is required", index, raw)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
                repaired.append("string_arguments")
            except json.JSONDecodeError:
                return None, ParserError("tool_call arguments string is not JSON", index, raw)
        if not isinstance(arguments, dict):
            return None, ParserError("tool_call arguments must be an object", index, raw)
        metadata = {"raw_call": raw, "repaired": bool(repaired), "repairs": repaired, "parser": "hermes"}
        call_id = f"{self.turn_id}:tool:{index}"
        return ToolCall(call_id, name.strip(), arguments, metadata=metadata), None
