"""Phase 0/1 foundation tests: events, state, budget, engine, and transcript."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aetherion_cli.context.transcript import Transcript
from aetherion_cli.engine.budget import TokenBudget, UsageSnapshot, estimate_tokens
from aetherion_cli.engine.events import Event, EventBus, EventType
from aetherion_cli.engine.loop import (
    AetherionConfig,
    Capabilities,
    DoneEvent,
    Engine,
    ModelRequest,
    TextDelta,
)
from aetherion_cli.engine.state import EngineState, InvalidTransition, transition


def test_event_protocol_has_all_map_events_and_broadcasts() -> None:
    assert len(EventType) == 22
    bus = EventBus(maxsize=4)
    first = bus.subscribe()
    second = bus.subscribe()
    event = Event(EventType.TEXT_DELTA, {"text": "hi"}, step=2, turn_id="turn")

    bus.publish(event)

    assert asyncio.run(first.next()) == event
    assert asyncio.run(second.next()) == event


def test_event_bus_raises_without_partial_delivery_when_full() -> None:
    bus = EventBus(maxsize=1)
    subscriber = bus.subscribe()
    bus.publish(Event(EventType.TEXT_DELTA, {"text": "one"}))
    with pytest.raises(asyncio.QueueFull):
        bus.publish(Event(EventType.TEXT_DELTA, {"text": "two"}))
    assert asyncio.run(subscriber.next()).data["text"] == "one"


def test_state_machine_allows_universal_cancel_and_fatal_error() -> None:
    for state in EngineState:
        if state not in {EngineState.DONE, EngineState.CANCELLED, EngineState.ERROR}:
            assert transition(state, EngineState.CANCELLED) is EngineState.CANCELLED
            assert transition(state, EngineState.ERROR) is EngineState.ERROR
    with pytest.raises(InvalidTransition):
        transition(EngineState.IDLE, EngineState.DONE)


def test_token_budget_accounts_for_cache_and_slices_exactly() -> None:
    budget = TokenBudget(context_window=100_000, reserved_output=16_384, safety_margin=1_024)
    usage = UsageSnapshot(input_tokens=10_000, cache_read_input_tokens=2_000, cache_creation_input_tokens=500)
    assert budget.usage_ratio(usage) == pytest.approx(12_500 / 82_592)
    slices = budget.slices()
    assert slices.system_environment + slices.repo_map + slices.memory + slices.summary + slices.conversation == budget.effective_input_budget
    assert estimate_tokens([{"role": "user", "content": "hello"}], [{"name": "read_file"}], "system") > 0


class _Context:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def add_user(self, prompt: str) -> None:
        self.messages.append(("user", prompt))

    def add_assistant(self, text: str, calls) -> None:
        self.messages.append(("assistant", text, list(calls)))

    def add_tool_results(self, results) -> None:
        self.messages.append(("tools", list(results)))

    def needs_compaction(self) -> bool:
        return False

    async def compact(self, *, mode: str = "full") -> None:
        raise AssertionError(f"unexpected compaction: {mode}")

    def build_request(self, capabilities: Capabilities) -> ModelRequest:
        return ModelRequest(list(self.messages), [], "system", 100, 0.2)


class _Provider:
    capabilities = Capabilities(native_tools=True, max_context=100_000)

    async def stream(self, request, *, cancel):
        yield TextDelta("hello")
        yield DoneEvent("stop")


class _Tools:
    async def run(self, calls, engine, step):
        return []


@pytest.mark.asyncio
async def test_engine_runs_streaming_turn_and_persists_terminal_event(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path / "session.jsonl", cwd=str(tmp_path))
    bus = EventBus()
    subscriber = bus.subscribe()
    engine = Engine(
        provider=_Provider(),
        tools=_Tools(),
        context=_Context(),
        permissions=None,
        planner=None,
        transcript=transcript,
        bus=bus,
        config=AetherionConfig(max_steps=2),
    )

    result = await engine.run_turn("say hello")

    assert result.state is EngineState.DONE
    assert result.text == "hello"
    events = []
    while not subscriber._queue.empty():
        events.append(subscriber._queue.get_nowait())
    assert any(event.type is EventType.TEXT_DELTA for event in events)
    assert any(event.type is EventType.TURN_FINISHED for event in events)
    assert transcript.branch[-1]["type"] == "assistant"


def test_transcript_resume_tolerates_corrupt_rows_and_forks(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    transcript = Transcript(path, cwd=str(tmp_path))
    record = transcript.append_message("user", "hello")

    resumed = Transcript.resume(path, cwd=str(tmp_path))
    assert resumed.rows_unreadable == (1,)
    assert resumed.branch[-1]["uuid"] == record["uuid"]

    child = resumed.fork(path=tmp_path / "child.jsonl")
    assert child.session_id != resumed.session_id
    child_rows = [json.loads(line) for line in (tmp_path / "child.jsonl").read_text(encoding="utf-8").splitlines()]
    assert child_rows[0]["type"] == "fork-context-ref"
    assert child_rows[0]["forkParentSessionId"] == resumed.session_id
