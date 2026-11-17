"""Focused Phase 7 planning DAG and scheduler tests."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from aetherion_cli.plan import (
    PlanScheduler,
    PlanStatus,
    PlanTask,
    TaskDAG,
    TaskStatus,
)


def test_dag_rejects_duplicate_unknown_self_and_cyclic_dependencies() -> None:
    with pytest.raises(ValidationError, match="duplicate task id"):
        TaskDAG(tasks=[PlanTask(id="same"), PlanTask(id="same")])
    with pytest.raises(ValidationError, match="unknown dependency"):
        TaskDAG(tasks=[PlanTask(id="task", dependencies=["missing"])])
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        TaskDAG(tasks=[PlanTask(id="task", dependencies=["task"])])
    with pytest.raises(ValidationError, match="dependencies must not contain duplicates"):
        TaskDAG(tasks=[PlanTask(id="task", dependencies=["other", "other"]), PlanTask(id="other")])
    with pytest.raises(ValidationError, match="contains a cycle"):
        TaskDAG(
            tasks=[
                PlanTask(id="a", dependencies=["b"]),
                PlanTask(id="b", dependencies=["a"]),
            ]
        )


def test_topological_levels_and_parallel_selection_are_deterministic() -> None:
    dag = TaskDAG(
        tasks=[
            PlanTask(id="z", dependencies=["root"], declared_files=["src/z.py"]),
            PlanTask(id="root", declared_files=["src/root.py"]),
            PlanTask(id="a", dependencies=["root"], declared_files=["src/shared.py"]),
            PlanTask(id="b", dependencies=["root"], declared_files=["./src/shared.py"]),
        ]
    )

    assert dag.levels == (("root",), ("a", "b", "z"))
    assert tuple(task.id for task in dag.select_parallel(("z", "b", "a"))) == ("a", "z")
    assert dag.to_dict()["tasks"][0]["dependencies"] == ["root"]
    assert "risk_tier" in PlanTask.json_schema_for_tools()["properties"]


def test_scheduler_runs_levels_async_and_preserves_events_and_skips_downstream() -> None:
    dag = TaskDAG(
        tasks=[
            PlanTask(id="fail", declared_files=["fail.py"]),
            PlanTask(id="peer", declared_files=["peer.py"]),
            PlanTask(id="child", dependencies=["fail"]),
            PlanTask(id="grandchild", dependencies=["child"]),
        ]
    )
    started: list[str] = []
    transcript: list[dict[str, object]] = []

    async def runner(task: PlanTask) -> object:
        started.append(task.id)
        await asyncio.sleep(0)
        if task.id == "fail":
            raise RuntimeError("boom")
        return {"task": task.id}

    report = asyncio.run(PlanScheduler(dag, runner, append=transcript.append).run())

    assert report.status is PlanStatus.FAILED
    assert set(started) == {"fail", "peer"}
    assert {result.task_id: result.status for result in report.results} == {
        "child": TaskStatus.SKIPPED,
        "fail": TaskStatus.FAILED,
        "grandchild": TaskStatus.SKIPPED,
        "peer": TaskStatus.SUCCEEDED,
    }
    assert "dependency 'fail' failed" in next(
        result.reason or "" for result in report.results if result.task_id == "grandchild"
    )
    assert transcript[-1]["type"] == "plan_failed"
    assert len(transcript) == len(report.events)


def test_conflicting_tasks_are_serialized_within_a_level() -> None:
    dag = TaskDAG(
        tasks=[
            PlanTask(id="first", declared_files=["same.py"]),
            PlanTask(id="second", declared_files=["same.py"]),
        ]
    )
    active = 0
    peak = 0

    async def runner(_: PlanTask) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1

    report = asyncio.run(PlanScheduler(dag, runner).run())

    assert report.succeeded
    assert peak == 1
