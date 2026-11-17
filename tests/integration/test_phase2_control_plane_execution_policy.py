"""Focused Phase 2 tests for the approved control-plane execution lock."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, g

from backend.execution import durable_state
from backend.execution.control_plane_policy import (
    EXECUTION_DISABLED_ERROR_CODE,
    EXECUTION_DISABLED_MESSAGE,
    ExecutionDisabledError,
    block_durable_execution,
    execution_disabled_payload,
    get_control_plane_policy,
)
from backend.execution.fabric.models import QueueItem, QueueItemStatus
from backend.execution.fabric.queue import DurableTaskQueue
from backend.execution.fabric.service import ExecutionFabricService
from backend.execution.fabric.store import FabricStore
from backend.execution.fabric.worker_runtime import WorkerRuntime
from backend.execution.jobs.models import BackgroundJob, JobStatus
from backend.execution.jobs.runner import JobRunner
from backend.execution.jobs.store import JobStore
from backend.execution.sandbox.boundary import WorkspaceBoundary
from backend.execution.tools.registry import AgentToolRegistry
from backend.execution.tools.schemas import ToolRequest


@pytest.fixture
def isolated_durable_store(tmp_path: Path):
    """Use only a disposable SQLite file; never the workspace durable store."""
    store = durable_state.SqliteDurableExecutionStore(str(tmp_path / "durable.db"))
    durable_state.set_store(store)
    # The application still has compatibility imports through the top-level
    # ``execution`` package. Point that module alias at the same disposable
    # store so route tests cannot touch any ambient database.
    from execution import durable_state as compat_durable_state
    compat_durable_state.set_store(store)
    try:
        yield store
    finally:
        durable_state.reset_store()
        compat_durable_state.reset_store()


def _request_context(payload=None):
    app = Flask("phase2-policy-tests")
    return app.test_request_context(
        "/api/tasks",
        method="POST",
        json=payload or {},
    )


def _create_record(tenant: str = "tenant-a"):
    return durable_state.create(
        kind="agent_task",
        tenant_id=tenant,
        payload={"prompt": "do not execute", "workspace_id": "test-only"},
    )


def _prepare_route_import(tmp_path: Path, monkeypatch) -> None:
    """Force route-module import side effects onto a disposable SQLite DB."""
    monkeypatch.setenv("AETHERION_ENV", "CI-test")
    monkeypatch.setenv("ENVIRONMENT", "testing")
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "auth.db"))


def test_policy_missing_and_contradictory_configuration_fail_closed():
    missing = get_control_plane_policy({})
    assert missing.execution_enabled is False
    assert missing.configuration_valid is False

    contradictory = get_control_plane_policy(
        {
            "AETHERION_RELEASE_SCOPE": "agent-execution",
            "AETHERION_EXECUTION_DISABLED": "0",
        }
    )
    assert contradictory.execution_enabled is False
    assert contradictory.configuration_valid is False

    explicit_control_plane = get_control_plane_policy(
        {
            "AETHERION_RELEASE_SCOPE": "control-plane",
            "AETHERION_EXECUTION_DISABLED": "1",
        }
    )
    assert explicit_control_plane.execution_enabled is False
    assert explicit_control_plane.configuration_valid is True


def test_execution_record_is_cancelled_but_remains_queryable(isolated_durable_store):
    record = _create_record()
    blocked = block_durable_execution(record["execution_id"], "tenant-a")

    assert blocked["status"] == durable_state.STATUS_CANCELLED
    fetched = durable_state.get(record["execution_id"], "tenant-a")
    assert fetched is not None
    result = json.loads(fetched["result_json"])
    assert result["error_code"] == EXECUTION_DISABLED_ERROR_CODE
    assert "do not execute" in json.loads(fetched["payload_json"])["prompt"]


def test_create_auto_run_is_rejected_without_starting_a_thread(isolated_durable_store, tmp_path, monkeypatch):
    _prepare_route_import(tmp_path, monkeypatch)
    from backend.routes import tasks

    monkeypatch.setattr(tasks, "_get_tenant", lambda: "tenant-a")
    with _request_context({"prompt": "secret prompt must not echo", "auto_run": True}):
        # The test intentionally calls the route body beneath token_required;
        # provide the authenticated identity that the workspace boundary now
        # requires before the execution-disabled branch is reached.
        g.user_id = 1
        response, status_code = tasks.create_task.__wrapped__()

    body = response.get_json()
    assert status_code == 403
    assert body["error_code"] == EXECUTION_DISABLED_ERROR_CODE
    assert "secret prompt" not in response.get_data(as_text=True)
    task = durable_state.get(body["task_id"], "tenant-a")
    assert task["status"] == durable_state.STATUS_CANCELLED


def test_execute_route_blocks_existing_task_and_keeps_history(isolated_durable_store, tmp_path, monkeypatch):
    _prepare_route_import(tmp_path, monkeypatch)
    from backend.routes import tasks

    record = _create_record()
    monkeypatch.setattr(tasks, "_get_tenant", lambda: "tenant-a")
    with _request_context({"sync": True}):
        response, status_code = tasks.execute_task.__wrapped__(record["execution_id"])

    body = response.get_json()
    assert status_code == 403
    assert body["error_code"] == EXECUTION_DISABLED_ERROR_CODE
    assert durable_state.get(record["execution_id"], "tenant-a")["status"] == durable_state.STATUS_CANCELLED


def test_runtime_and_workspace_boundary_cannot_reach_host_process(tmp_path: Path):
    from backend.execution.agent_runtime.runtime import RealAgentRuntime

    with patch("subprocess.Popen", side_effect=AssertionError("Popen must not be reached")):
        with pytest.raises(ExecutionDisabledError):
            WorkspaceBoundary(tmp_path)
        with pytest.raises(ExecutionDisabledError):
            RealAgentRuntime(workspace_dir=tmp_path)


def test_tool_registry_returns_policy_error_before_dispatch():
    boundary = MagicMock()
    boundary.workspace_dir = Path(".")
    registry = AgentToolRegistry(boundary)
    result = registry.execute_tool(
        ToolRequest(
            tool="run_command",
            arguments={"command": "echo should-not-run"},
            task_id="task-policy",
            session_id="session-policy",
        )
    )
    assert result.ok is False
    assert result.error == EXECUTION_DISABLED_MESSAGE
    boundary.run_command.assert_not_called()


def test_fabric_queue_worker_and_executor_fail_closed(tmp_path: Path):
    store = FabricStore(db_path=str(tmp_path / "fabric.db"))
    queue = DurableTaskQueue(store=store)
    with pytest.raises(ExecutionDisabledError):
        queue.enqueue(task_id="task-policy", tenant_id="tenant-a")

    item = QueueItem(
        item_id="qitem-policy",
        task_id="task-policy",
        tenant_id="tenant-a",
        status=QueueItemStatus.CLAIMED,
        assigned_worker_id="worker-policy",
        lease_id="lease-policy",
    )
    store.insert_queue_item(item)
    executor_called = False

    def executor(_item):
        nonlocal executor_called
        executor_called = True
        return {"ok": True}

    service = ExecutionFabricService(store=store)
    assert service.tick_scheduler() == []
    with pytest.raises(ExecutionDisabledError):
        service.execute_claimed_item("qitem-policy", "worker-policy", executor_fn=executor)
    assert executor_called is False

    worker = WorkerRuntime(
        worker_id="worker-policy",
        service=service,
        store=store,
        coordinator=MagicMock(),
        executor_fn=executor,
    )
    assert worker._poll_and_dispatch() is False


def test_background_agent_job_fails_before_thread_launch(tmp_path: Path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.create_job(
        BackgroundJob(
            owner_id="tenant-a",
            name="agent job",
            task="run an agent command",
        )
    )
    runner = JobRunner(store=store)

    with pytest.raises(ExecutionDisabledError):
        runner.start_job(job.id, "tenant-a", tmp_path)

    persisted = store.get_job(job.id, "tenant-a")
    assert persisted.status == JobStatus.FAILED.value
    assert persisted.error == EXECUTION_DISABLED_MESSAGE


def test_public_policy_error_contains_no_sensitive_request_data():
    payload = execution_disabled_payload()
    serialized = json.dumps(payload)
    assert payload["error_code"] == EXECUTION_DISABLED_ERROR_CODE
    assert "secret" not in serialized.lower()
    assert "workspace" not in serialized.lower()
    assert "command" not in serialized.lower()


def test_plugin_and_runner_entrypoints_are_disabled():
    from backend.execution.extensions.runner import ExtensionRunner
    from backend.execution.extensions.worker import execute_request
    from backend.execution.runner_contract import get_runner, get_runner_status

    req = ToolRequest(
        tool="plugin_command",
        arguments={"command": "should-not-run"},
        task_id="task-plugin-policy",
        session_id="session-plugin-policy",
    )
    result = ExtensionRunner().execute_tool(
        req,
        {"id": "plugin-policy", "trust_level": "UNTRUSTED", "permissions": []},
        Path("."),
    )
    assert result.ok is False
    assert result.error == EXECUTION_DISABLED_MESSAGE

    worker_result = execute_request({}, Path("."))
    assert worker_result["ok"] is False
    assert worker_result["error"] == EXECUTION_DISABLED_MESSAGE

    with pytest.raises(ExecutionDisabledError):
        get_runner("local")
    status = get_runner_status()
    assert status["execution_disabled"] is True
    assert status["available"] is False
