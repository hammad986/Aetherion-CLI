"""Release-gate tests for Phases 5–8 local synthesis."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from aetherion_cli.context import ContextManager, Transcript
from aetherion_cli.engine.loop import Capabilities, DoneEvent, ModelRequest, TextDelta
from aetherion_cli.engine.runtime import LocalEngineFactory
from aetherion_cli.main import cli
from aetherion_cli.plan import PlanScheduler, TaskDAG
from aetherion_cli.repo import build_reading_plan, index_repository


def test_context_compaction_keeps_pinned_goal_and_tool_pairs() -> None:
    context = ContextManager(context_window=32_768, pinned_goal="Keep the original coding goal")
    context.add_user("Keep the original coding goal")
    for index in range(40):
        context.add_assistant(f"verbose historical answer {index}", [])
        context.add_user(f"follow-up {index}")
    asyncio.run(context.compact(mode="full"))
    assert context.pinned_goal == "Keep the original coding goal"
    assert "Keep the original coding goal" in context.summary


def test_compaction_boundary_survives_transcript_resume(tmp_path: Path) -> None:
    transcript_path = tmp_path / "resume.jsonl"
    transcript = Transcript(transcript_path, cwd=str(tmp_path))
    context = ContextManager(
        transcript,
        pinned_goal="preserve this goal",
        recent_message_count=1,
    )
    context.add_user("old turn")
    context.add_assistant("old answer", [])
    context.add_user("latest turn")
    context.add_assistant("latest answer", [])
    context.add_user("new current turn")
    asyncio.run(context.compact(mode="full"))

    resumed = ContextManager(
        Transcript.resume(transcript_path, cwd=str(tmp_path)),
        pinned_goal="preserve this goal",
        recent_message_count=1,
    )
    resumed_text = " ".join(str(message.get("content", "")) for message in resumed.messages)
    assert "old answer" not in resumed_text
    assert "latest answer" in resumed_text
    assert "preserve this goal" in resumed.summary


@pytest.mark.asyncio
async def test_reactive_compaction_retries_a_context_overflow(tmp_path: Path) -> None:
    class OverflowProvider:
        capabilities = Capabilities(max_context=32_768)

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request: ModelRequest, *, cancel: asyncio.Event):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("HTTP 400 context window overflow")
            yield TextDelta("recovered")
            yield DoneEvent("stop")

    provider = OverflowProvider()
    controller = LocalEngineFactory.build(tmp_path, provider=provider, transcript_path=tmp_path / "session.jsonl")
    result = await controller.run_turn("original goal")
    assert result.state.value == "done"
    assert provider.calls == 2
    assert controller.engine.context.last_mode == "reactive"


def test_dag_rejects_cycles_and_scheduler_is_topological() -> None:
    with pytest.raises((ValidationError, ValueError), match="cycle"):
        TaskDAG.model_validate(
            {"tasks": [{"id": "a", "dependencies": ["b"]}, {"id": "b", "dependencies": ["a"]}]}
        )

    dag = TaskDAG.model_validate(
        {
            "tasks": [
                {"id": "inspect", "description": "inspect"},
                {"id": "edit", "description": "edit", "dependencies": ["inspect"], "declared_files": ["x"]},
                {"id": "verify", "description": "verify", "dependencies": ["edit"]},
            ]
        }
    )
    order: list[str] = []

    async def runner(task) -> str:
        order.append(task.id)
        return task.id

    report = asyncio.run(PlanScheduler(dag, runner).run())
    assert report.succeeded
    assert order == ["inspect", "edit", "verify"]


def test_headless_controller_drains_fast_provider_events(tmp_path: Path) -> None:
    seen: list[str] = []
    controller = LocalEngineFactory.build(tmp_path, transcript_path=tmp_path / "events.jsonl")

    async def on_event(event) -> None:
        if event.type.value == "text_delta":
            await asyncio.sleep(0)
            seen.append(str(event.data["text"]))

    result = asyncio.run(controller.run_turn("drain me", on_event))
    assert result.state.value == "done"
    assert seen == ["Aetherion local: drain me"]


def test_repo_plan_caps_pathological_query(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
    plan = build_reading_plan(index_repository(tmp_path), "x" * 100_000, budget_tokens=2_000, max_files=5)
    assert plan.estimated_tokens <= 2_000


def test_code_command_runs_headless_without_authentication() -> None:
    result = CliRunner().invoke(cli, ["code", "hello local engine"])
    assert result.exit_code == 0, result.output
    assert "hello local engine" in result.output
