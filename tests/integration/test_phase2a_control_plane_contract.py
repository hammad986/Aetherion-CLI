"""Focused Phase 2A contract tests using only mocks and disposable objects.

These tests deliberately do not initialize the application's ambient database,
create a physical worktree, invoke a shell, or contact an external service.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, g

os.environ["AETHERION_ENV"] = "CI-test"
os.environ["ENVIRONMENT"] = "testing"
os.environ["JWT_SECRET"] = "phase2a-test-secret-" + "x" * 48
os.environ["AETHERION_RELEASE_SCOPE"] = "control-plane"
os.environ["AETHERION_EXECUTION_DISABLED"] = "1"

from backend.core.preflight import CheckStatus, PreflightReport
from backend.db.d3_store import D3Store
from backend.execution.control_plane_policy import (
    EXECUTION_DISABLED_ERROR_CODE,
    EXECUTION_DISABLED_MESSAGE,
    ExecutionDisabledError,
)
from backend.execution.fabric.models import WorkerStatus
from backend.execution.tools.registry import AgentToolRegistry
from backend.routes import d3_features, diff, health


@pytest.fixture
def route_context():
    app = Flask("phase2a-contract-tests")
    with app.test_request_context("/phase2a", method="POST", json={}):
        g.user_id = "tenant-a"
        yield app


def _body(response):
    return response.get_json()


def _assert_policy_error(body):
    assert body["ok"] is False
    assert body["error_code"] == EXECUTION_DISABLED_ERROR_CODE
    assert body["error"] == EXECUTION_DISABLED_MESSAGE
    assert body["execution"] == {"enabled": False, "scope": "control-plane"}


def test_automation_run_aliases_block_before_false_success_persistence(route_context):
    store = MagicMock()
    store.get_automation.return_value = {"id": "auto-1", "run_count": 0}
    with patch.object(d3_features, "d3_store", store), patch.object(
        store, "create_automation_run"
    ) as create_run:
        response, status = d3_features.automation_run_now.__wrapped__("auto-1")
        assert status == 403
        _assert_policy_error(_body(response))
        response, status = d3_features.automation_task_action.__wrapped__("auto-1", "run")
        assert status == 403
        _assert_policy_error(_body(response))

    create_run.assert_not_called()
    assert store.get_automation.call_count == 2


def test_automation_run_preserves_tenant_not_found_boundary(route_context):
    store = MagicMock()
    store.get_automation.return_value = None
    with patch.object(d3_features, "d3_store", store), patch.object(
        store, "create_automation_run"
    ) as create_run:
        response, status = d3_features.automation_run_now.__wrapped__("auto-owned-by-other")

    assert status == 404
    assert _body(response)["ok"] is False
    create_run.assert_not_called()


def test_direct_automation_run_persistence_is_fail_closed():
    store = D3Store()
    with patch("backend.db.d3_store.assert_agent_execution_enabled", side_effect=ExecutionDisabledError), patch(
        "backend.db.d3_store._get_db", side_effect=AssertionError("database must not be reached")
    ) as get_db:
        with pytest.raises(ExecutionDisabledError):
            store.create_automation_run("tenant-a", "auto-a")
    get_db.assert_not_called()


@pytest.mark.parametrize("state", ["backlog", "todo", "in_progress", "review", "blocked", "done", "archived"])
def test_kanban_deploy_is_only_a_requested_workflow_transition(route_context, state):
    card = {"id": "card-1", "column": state, "status": state}
    store = MagicMock()
    store.move_kanban_card.return_value = card
    app = route_context
    with app.test_request_context("/api/kanban/deploy", method="POST", json={"execution_id": "card-1", "state": state}):
        g.user_id = "tenant-a"
        with patch.object(d3_features, "d3_store", store):
            response = d3_features.kanban_deploy.__wrapped__()
            status = response.status_code

    assert status == 200
    body = _body(response)
    assert body["ok"] is True
    assert body["requested_state"] == state
    assert body["workflow_state"] == state
    assert body["deployment_performed"] is False
    assert body["operation"] == "workflow_state_transition"
    assert "deployed" not in body
    store.move_kanban_card.assert_called_with("tenant-a", "card-1", state)


def test_kanban_deploy_rejects_unsupported_or_missing_state_without_mutation(route_context):
    store = MagicMock()
    app = route_context
    with app.test_request_context("/api/kanban/deploy", method="POST", json={"execution_id": "card-1", "state": "deployed"}):
        g.user_id = "tenant-a"
        with patch.object(d3_features, "d3_store", store):
            response, status = d3_features.kanban_deploy.__wrapped__()
    assert status == 400
    assert "supported_states" in _body(response)
    store.move_kanban_card.assert_not_called()

    with app.test_request_context("/api/kanban/deploy", method="POST", json={}):
        g.user_id = "tenant-a"
        with patch.object(d3_features, "d3_store", store):
            response, status = d3_features.kanban_deploy.__wrapped__()
    assert status == 400
    store.move_kanban_card.assert_not_called()


def test_public_worktree_creation_and_physical_cleanup_are_blocked_before_side_effects(route_context):
    store = MagicMock()
    app = route_context
    for function, path, payload in (
        (d3_features.worktrees_create, "/api/worktrees", {"base_ref": "HEAD"}),
        (d3_features.worktrees_discard, "/api/worktrees/wt-1/discard", None),
        (d3_features.worktrees_sweep, "/api/worktrees/sweep", {}),
        (d3_features.worktrees_sweep_orphans, "/api/worktrees/sweep/orphans", {}),
    ):
        with app.test_request_context(path, method="POST", json=payload):
            g.user_id = "tenant-a"
            with patch.object(d3_features, "d3_store", store):
                if function is d3_features.worktrees_create:
                    response, status = function.__wrapped__()
                else:
                    argument = "wt-1" if function is d3_features.worktrees_discard else None
                    response, status = function.__wrapped__(argument) if argument else function.__wrapped__()
        assert status == 403
        _assert_policy_error(_body(response))

    store.create_real_worktree.assert_not_called()
    store.discard_worktree.assert_not_called()
    store.sweep_worktrees.assert_not_called()
    store.sweep_orphans.assert_not_called()


def test_worktree_reads_remain_tenant_scoped(route_context):
    store = MagicMock()
    store.list_worktrees.return_value = [{"worktree_id": "wt-a", "tenant_id": "tenant-a"}]
    store.get_worktree.return_value = None
    with route_context.test_request_context("/api/worktrees", method="GET"):
        g.user_id = "tenant-a"
        with patch.object(d3_features, "d3_store", store):
            response = d3_features.worktrees_list.__wrapped__()
            status = response.status_code
        assert status == 200
        assert _body(response)["worktrees"] == [{"worktree_id": "wt-a", "tenant_id": "tenant-a"}]

    with route_context.test_request_context("/api/worktrees/wt-b", method="GET"):
        g.user_id = "tenant-a"
        with patch.object(d3_features, "d3_store", store):
            response = d3_features.worktrees_get.__wrapped__("wt-b")
            status = response[1] if isinstance(response, tuple) else response.status_code

    assert status == 404
    store.list_worktrees.assert_called_once_with("tenant-a", session_id=None)
    store.get_worktree.assert_called_once_with("tenant-a", "wt-b")


def test_effective_worktree_diff_route_is_tenant_aware():
    app = Flask("route-map")
    app.register_blueprint(diff.diff_bp)
    app.register_blueprint(d3_features.d3_bp)
    rules = [
        rule for rule in app.url_map.iter_rules()
        if str(rule) == "/api/worktrees/<worktree_id>/diff" and "GET" in rule.methods
    ]
    assert len(rules) == 1
    assert rules[0].endpoint == "d3_features.worktrees_diff"


def test_direct_tool_mutators_fail_before_boundary_side_effects(tmp_path):
    boundary = MagicMock()
    boundary.workspace_dir = tmp_path
    registry = AgentToolRegistry(boundary)

    results = [
        registry.write_file("blocked.txt", "no write"),
        registry.edit_file("blocked.txt", "old", "new"),
        registry.delete_file("blocked.txt"),
        registry.run_command("echo blocked"),
        registry.run_tests("pytest"),
        registry.git_commit("blocked commit"),
    ]
    assert all(not result.ok for result in results)
    assert all(result.error == EXECUTION_DISABLED_MESSAGE for result in results)
    assert all(result.security_event for result in results)
    boundary.resolve_safe_path.assert_not_called()
    boundary.run_command.assert_not_called()
    assert not (tmp_path / "blocked.txt").exists()


def test_direct_store_worktree_mutators_fail_before_database_or_git_access():
    store = D3Store()
    with patch("backend.db.d3_store.assert_agent_execution_enabled", side_effect=ExecutionDisabledError), patch(
        "backend.db.d3_store._get_db", side_effect=AssertionError("database must not be reached")
    ) as get_db:
        with pytest.raises(ExecutionDisabledError):
            store.create_real_worktree("tenant-a", "session-a", "execution-a")
        with pytest.raises(ExecutionDisabledError):
            store.discard_worktree("tenant-a", "wt-a")
        with pytest.raises(ExecutionDisabledError):
            store.sweep_worktrees("tenant-a")
        with pytest.raises(ExecutionDisabledError):
            store.sweep_orphans("tenant-a")
    get_db.assert_not_called()


def _worker(worker_id="worker-a"):
    return SimpleNamespace(
        worker_id=worker_id,
        status=WorkerStatus.READY,
        active_concurrency=0,
        max_concurrency=2,
        drain_state=False,
        supported_capabilities={"DEFAULT"},
    )


def _fabric_store(workers):
    store = MagicMock()
    store.list_workers.return_value = workers
    store.count_global_active_items.return_value = 0
    return store


def _passing_check():
    check = MagicMock()
    check.status = CheckStatus.PASS
    check.to_dict.return_value = {"check_id": "control_plane.fabric", "status": "PASS", "ok": True}
    return check


def _ready_preflight():
    return PreflightReport(
        environment="CI-test",
        healthy=True,
        ready=True,
        checks=[],
        timestamp="2026-10-13T00:00:00+00:00",
        summary={"pass": 0, "warn": 0, "fail": 0, "critical_fails": 0},
    )


def test_control_plane_health_stays_healthy_while_execution_is_disabled():
    app = Flask("health-control-plane")
    store = _fabric_store([_worker()])
    with app.test_request_context("/api/health/control-plane"):
        with patch("backend.core.preflight.check_control_plane", return_value=_passing_check()), patch(
            "backend.execution.fabric.store.get_fabric_store", return_value=store
        ), patch(
            "backend.execution.fabric.coordination.FabricCoordinator",
            return_value=SimpleNamespace(is_distributed=False),
        ):
            response, status = health.api_health_control_plane()

    body = _body(response)
    assert status == 200
    assert body["ok"] is True
    assert body["execution_disabled"] is True
    assert body["execution"]["execution_enabled"] is False


def test_registered_worker_is_reported_but_not_execution_capable():
    app = Flask("health-workers")
    store = _fabric_store([_worker()])
    collector = MagicMock()
    with app.test_request_context("/api/health/workers"):
        with patch("backend.execution.fabric.store.get_fabric_store", return_value=store), patch(
            "backend.execution.fabric.metrics.get_metrics_collector", return_value=collector
        ):
            response, status = health.api_health_workers()

    body = _body(response)
    assert status == 200
    assert body["ok"] is True
    assert body["registered_workers"] == 1
    assert body["worker_fleet_available"] is True
    assert body["worker_capable"] is False
    assert body["execution_ready"] is False
    assert body["status"] == "EXECUTION-DISABLED"
    assert body["execution_disabled"] is True


def test_execution_ready_remains_false_even_with_healthy_control_plane_and_worker():
    app = Flask("health-execution-ready")
    store = _fabric_store([_worker()])
    with app.test_request_context("/api/health/execution-ready"):
        with patch("backend.core.preflight.check_control_plane", return_value=_passing_check()), patch(
            "backend.execution.fabric.store.get_fabric_store", return_value=store
        ), patch(
            "backend.execution.runner_contract.get_runner_status",
            return_value={"active_backend": "control-plane-disabled", "available": False, "execution_disabled": True},
        ):
            response, status = health.api_health_execution_ready()

    body = _body(response)
    assert status == 503
    assert body["ok"] is False
    assert body["execution_ready"] is False
    assert body["status"] == "EXECUTION-DISABLED"
    assert body["control_plane_ready"] is True
    assert body["workers_ready"] is True


def test_composite_readiness_reports_control_plane_ready_separately_from_execution():
    app = Flask("health-fabric-readiness")
    store = _fabric_store([_worker()])
    with app.test_request_context("/api/health/fabric-readiness"):
        with patch.object(health, "run_preflight", return_value=_ready_preflight()), patch(
            "backend.core.preflight.check_control_plane", return_value=_passing_check()
        ), patch("backend.execution.fabric.store.get_fabric_store", return_value=store):
            response, status = health.api_health_fabric_readiness()

    body = _body(response)
    assert status == 200
    assert body["application_ok"] is True
    assert body["overall_ok"] is True
    assert body["execution_ready"] is False
    assert body["levels"]["WORKER-CAPABLE"]["ok"] is True
    assert body["levels"]["WORKER-CAPABLE"]["execution_capable"] is False
    assert body["levels"]["EXECUTION-READY"]["ok"] is False
    assert body["levels"]["EXECUTION-READY"]["status"] == "EXECUTION-DISABLED"


def test_readyz_exposes_application_readiness_and_disabled_execution_separately():
    app = Flask("health-readyz")
    with app.test_request_context("/readyz"):
        with patch.object(health, "run_preflight", return_value=_ready_preflight()):
            response, status = health.readyz()

    body = _body(response)
    assert status == 200
    assert body["ready"] is True
    assert body["application_ready"] is True
    assert body["execution_ready"] is False
    assert body["execution_disabled"] is True


def test_worker_health_preserves_real_dependency_failure():
    app = Flask("health-dependency-failure")
    with app.test_request_context("/api/health/workers"):
        with patch("backend.execution.fabric.store.get_fabric_store", side_effect=RuntimeError("isolated store unavailable")):
            response, status = health.api_health_workers()

    body = _body(response)
    assert status == 503
    assert body["ok"] is False
    assert body["dependency_ok"] is False
    assert body["status"] == "DEPENDENCY-UNAVAILABLE"
    assert body["execution_disabled"] is True
