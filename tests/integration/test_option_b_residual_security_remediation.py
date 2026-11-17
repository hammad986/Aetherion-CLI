"""Focused regression tests for the four residual Option B findings."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, g

os.environ["AETHERION_ENV"] = "CI-test"
os.environ["ENVIRONMENT"] = "testing"
os.environ["JWT_SECRET"] = "residual-remediation-test-secret-" + "x" * 40
os.environ["AETHERION_RELEASE_SCOPE"] = "control-plane"
os.environ["AETHERION_EXECUTION_DISABLED"] = "1"
os.environ["DATABASE_URL"] = "sqlite:///C:/Users/mdham/AppData/Local/Temp/omnirush/aetherion-residual-route.db"

from backend.execution import worktree
from backend.execution.agent_runtime import context as context_module
from backend.execution.agent_runtime.context import (
    DiffExtractionError,
    TaskExecutionContext,
    get_execution_diff,
)
from backend.execution.diff_policy import (
    DIFF_MAX_MODIFIED_FILES,
    DIFF_MAX_RENDERED_BYTES,
    DIFF_MAX_UNTRACKED_FILE_BYTES,
    DIFF_MAX_UNTRACKED_FILES,
    DIFF_MAX_UNTRACKED_TOTAL_BYTES,
    build_tracked_diff_argv,
    run_bounded_readonly_git,
)
from backend.routes import diff as diff_route
from backend.routes import tasks


def _record(task_id: str, tenant_id: str, *, result=None, payload=None):
    return {
        "execution_id": task_id,
        "tenant_id": tenant_id,
        "result_json": json.dumps(result) if result is not None else None,
        "payload_json": json.dumps(payload or {}),
        "status": "SUCCEEDED",
    }


def _route_context(path: str = "/api/diff/task-a"):
    app = Flask("residual-remediation")
    ctx = app.test_request_context(path, method="GET")
    ctx.push()
    g.user_id = "tenant-a"
    return app, ctx


def _response_parts(response):
    if isinstance(response, tuple):
        return response[0], response[1]
    return response, response.status_code


def test_alternate_diff_route_returns_authorized_stored_diff():
    app, ctx = _route_context()
    try:
        record = _record("task-a", "tenant-a", result={"diff": "diff --git a/a b/a\n"})
        with patch.object(diff_route.durable_state, "get", return_value=record) as get_record:
            response = diff_route.get_diff.__wrapped__("task-a")
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        assert response.get_json()["diff"].startswith("diff --git")
        get_record.assert_called_once_with("task-a", "tenant-a")
    finally:
        ctx.pop()


@pytest.mark.parametrize("task_id", [".", "..", "../task-a", "C:/outside", "\\\\server\\share", "a\\..\\b"])
def test_alternate_diff_route_rejects_path_like_task_ids_before_lookup(task_id):
    app, ctx = _route_context("/api/diff/probe")
    try:
        with patch.object(diff_route.durable_state, "get") as get_record:
            response = diff_route.get_diff.__wrapped__(task_id)
        response_obj, status = _response_parts(response)
        assert status == 400
        assert response_obj.get_json()["ok"] is False
        get_record.assert_not_called()
    finally:
        ctx.pop()


def test_alternate_diff_route_unknown_and_cross_tenant_tasks_fail_closed():
    app, ctx = _route_context()
    try:
        with patch.object(diff_route.durable_state, "get", return_value=None) as get_record:
            response = diff_route.get_diff.__wrapped__("missing")
        response_obj, status = _response_parts(response)
        assert status == 404
        assert response_obj.get_json()["ok"] is False
        get_record.assert_called_once_with("missing", "tenant-a")

        foreign = _record("foreign", "tenant-b", result={"diff": "private"})
        with patch.object(diff_route.durable_state, "get", return_value=None):
            response = diff_route.get_diff.__wrapped__("foreign")
        response_obj, status = _response_parts(response)
        assert status == 404
        assert "private" not in response_obj.get_data(as_text=True)
    finally:
        ctx.pop()


def test_alternate_diff_route_does_not_turn_missing_diff_into_success():
    app, ctx = _route_context()
    try:
        record = _record("task-empty", "tenant-a", payload={})
        with patch.object(diff_route.durable_state, "get", return_value=record):
            response = diff_route.get_diff.__wrapped__("task-empty")
        response_obj, status = _response_parts(response)
        assert status == 409
        assert response_obj.get_json() == {
            "ok": False,
            "error_code": "DIFF_UNAVAILABLE",
            "error": "A complete diff is not available",
            "task_id": "task-empty",
        }
    finally:
        ctx.pop()


def test_task_diff_wrapper_propagates_worktree_failure():
    app = Flask("task-diff-remediation")
    ctx = app.test_request_context("/api/task/task-a/diff", method="GET")
    ctx.push()
    g.user_id = "tenant-a"
    try:
        record = _record("task-a", "tenant-a", payload={"worktree_id": "wt-a"})
        with patch.object(tasks.durable_state, "get", return_value=record), patch.object(
            tasks, "_authorized_worktree_diff", side_effect=RuntimeError("internal path")
        ):
            response = tasks.get_task_diff.__wrapped__("task-a")
        assert response[1] == 502
        body = response[0].get_json()
        assert body["ok"] is False
        assert body["error_code"] == "DIFF_UNAVAILABLE"
        assert "internal path" not in response[0].get_data(as_text=True)
    finally:
        ctx.pop()


def _registered_context(tmp_path: Path, *, worktree_id="wt-a", task_id="task-a"):
    repo = tmp_path / "repo"
    repo.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    registered = worktree.Worktree(
        worktree_id=worktree_id,
        session_id="session-a",
        execution_id=task_id,
        task_id=task_id,
        tenant_id="tenant-a",
        branch="fixture-branch",
        path=str(checkout),
        base_ref="HEAD",
        repo_root=str(repo),
    )
    context = TaskExecutionContext(
        task_id=task_id,
        tenant_id="tenant-a",
        session_id="session-a",
        workspace_dir=checkout,
        repo_root=str(repo),
        worktree_id=worktree_id,
        base_ref="HEAD",
    )
    return repo, checkout, registered, context


@pytest.mark.parametrize(
    "relative",
    [".ssh/id_rsa", ".ssh/id_ed25519", ".env", ".env.local", ".git/index", ".aws/credentials", ".kube/config"],
)
def test_sensitive_diff_paths_are_rejected_before_file_read(tmp_path, relative):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = workspace / Path(relative)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("synthetic-not-a-secret", encoding="utf-8")
    with pytest.raises(DiffExtractionError, match="excluded"):
        context_module._safe_diff_path(workspace, relative)


def test_ordinary_untracked_source_is_still_rendered(tmp_path):
    repo, checkout, registered, context = _registered_context(tmp_path)
    source = checkout / "module.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    with patch.object(worktree, "_REGISTRY", {registered.worktree_id: registered}), patch.object(
        context_module, "run_bounded_readonly_git", side_effect=["", "module.py\x00"]
    ):
        diff = get_execution_diff(context)
    assert "module.py" in diff
    assert "+print('ok')" in diff


def test_git_standard_ignore_excludes_ignored_candidates(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda args: subprocess.run(args, check=True, capture_output=True, text=True)
    run(["git", "init", str(repo)])
    run(["git", "-C", str(repo), "config", "user.email", "diff-test@example.invalid"])
    run(["git", "-C", str(repo), "config", "user.name", "Diff Test"])
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    run(["git", "-C", str(repo), "add", "tracked.txt", ".gitignore"])
    run(["git", "-C", str(repo), "commit", "-m", "fixture"])
    checkout = tmp_path / "checkout"
    run(["git", "-C", str(repo), "worktree", "add", "-b", "ignored-branch", str(checkout), "HEAD"])
    (checkout / "ignored").mkdir()
    (checkout / "ignored" / "secret.txt").write_text("ignored", encoding="utf-8")
    (checkout / "visible.py").write_text("print('visible')\n", encoding="utf-8")
    registered = worktree.Worktree(
        worktree_id="ignored-wt",
        session_id="session-a",
        execution_id="task-ignore",
        task_id="task-ignore",
        tenant_id="tenant-a",
        branch="ignored-branch",
        path=str(checkout),
        base_ref="HEAD",
        repo_root=str(repo),
    )
    context = TaskExecutionContext(
        task_id="task-ignore",
        tenant_id="tenant-a",
        session_id="session-a",
        workspace_dir=checkout,
        repo_root=str(repo),
        worktree_id="ignored-wt",
    )
    with patch.object(worktree, "_REGISTRY", {"ignored-wt": registered}):
        diff = get_execution_diff(context)
    assert "visible.py" in diff
    assert "ignored/secret.txt" not in diff


def test_binary_untracked_file_is_bounded_and_rendered_without_decoding_failure(tmp_path):
    repo, checkout, registered, context = _registered_context(tmp_path, task_id="task-binary")
    binary = checkout / "image.bin"
    binary.write_bytes(b"\x00\x01\x02")
    with patch.object(worktree, "_REGISTRY", {registered.worktree_id: registered}), patch.object(
        context_module, "run_bounded_readonly_git", side_effect=["", "image.bin\x00"]
    ):
        diff = get_execution_diff(context)
    assert "Binary files /dev/null and b/image.bin differ" in diff


def test_untracked_file_count_limit_is_explicit(tmp_path):
    repo, checkout, registered, context = _registered_context(tmp_path)
    listing = "\x00".join(f"file-{i}.txt" for i in range(DIFF_MAX_UNTRACKED_FILES + 1)) + "\x00"
    with patch.object(worktree, "_REGISTRY", {registered.worktree_id: registered}), patch.object(
        context_module, "run_bounded_readonly_git", side_effect=["", listing]
    ):
        with pytest.raises(DiffExtractionError, match="file count"):
            get_execution_diff(context)


def test_untracked_per_file_and_aggregate_limits_are_explicit(tmp_path):
    repo, checkout, registered, context = _registered_context(tmp_path)
    oversized = checkout / "large.txt"
    oversized.write_bytes(b"x" * (DIFF_MAX_UNTRACKED_FILE_BYTES + 1))
    with patch.object(worktree, "_REGISTRY", {registered.worktree_id: registered}), patch.object(
        context_module, "run_bounded_readonly_git", side_effect=["", "large.txt\x00"]
    ):
        with pytest.raises(DiffExtractionError, match="per-file"):
            get_execution_diff(context)

    names = []
    for index in range(9):
        name = f"aggregate-{index}.txt"
        (checkout / name).write_bytes(b"x" * (DIFF_MAX_UNTRACKED_TOTAL_BYTES // 9 + 1))
        names.append(name)
    listing = "\x00".join(names) + "\x00"
    with patch.object(worktree, "_REGISTRY", {registered.worktree_id: registered}), patch.object(
        context_module, "run_bounded_readonly_git", side_effect=["", listing]
    ):
        with pytest.raises(DiffExtractionError, match="aggregate"):
            get_execution_diff(context)


def test_rendered_output_limit_is_explicit(tmp_path):
    repo, checkout, registered, context = _registered_context(tmp_path)
    oversized_diff = "x" * (DIFF_MAX_RENDERED_BYTES + 1)
    with patch.object(worktree, "_REGISTRY", {registered.worktree_id: registered}), patch.object(
        context_module, "run_bounded_readonly_git", side_effect=[oversized_diff, ""]
    ):
        with pytest.raises(DiffExtractionError, match="rendered diff"):
            get_execution_diff(context)


def test_symlink_outside_workspace_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("outside", encoding="utf-8")
    link = workspace / "linked.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(DiffExtractionError, match="symlink"):
        context_module._safe_diff_path(workspace, "linked.txt")


def test_missing_plain_workspace_file_is_not_reported_as_empty_success(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = TaskExecutionContext(task_id="task-missing", workspace_dir=workspace)
    with pytest.raises(DiffExtractionError, match="unavailable"):
        get_execution_diff(context, ["missing.py"])


def test_plain_workspace_modified_file_count_is_bounded(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = TaskExecutionContext(task_id="task-too-many", workspace_dir=workspace)
    with pytest.raises(DiffExtractionError, match="modified file count"):
        get_execution_diff(context, [f"file-{i}.py" for i in range(DIFF_MAX_MODIFIED_FILES + 1)])


def test_worktree_context_never_falls_back_to_plain_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main-repo-file.py").write_text("must not be read\n", encoding="utf-8")
    context = TaskExecutionContext(
        task_id="task-main-repo",
        workspace_dir=repo,
        repo_root=str(repo),
        worktree_id="missing-worktree",
    )
    with pytest.raises(DiffExtractionError, match="isolated worktree"):
        get_execution_diff(context, ["main-repo-file.py"])


def test_worktree_context_without_repo_root_cannot_use_plain_fallback(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("must not be read\n", encoding="utf-8")
    context = TaskExecutionContext(
        task_id="task-no-root",
        workspace_dir=workspace,
        worktree_id="missing-worktree",
    )
    with pytest.raises(DiffExtractionError, match="authorized execution worktree"):
        get_execution_diff(context, ["candidate.py"])


def test_worktree_diff_authorization_rejects_cross_tenant_registry_entry(tmp_path):
    repo, checkout, registered, context = _registered_context(tmp_path)
    registered.tenant_id = "tenant-b"
    with patch.object(worktree, "_REGISTRY", {registered.worktree_id: registered}):
        with pytest.raises(DiffExtractionError, match="ownership"):
            get_execution_diff(context)


def test_d3_store_diff_failure_is_not_successful_empty_result(tmp_path):
    from backend.db.d3_store import D3Store

    repo = tmp_path / "repo"
    checkout = repo / "checkout"
    checkout.mkdir(parents=True)
    store = D3Store()
    record = {
        "path": str(checkout),
        "repo_root": str(repo),
        "base_ref": "HEAD",
        "state": "active",
        "status": "active",
        "governed": True,
        "session_id": "session-a",
        "execution_id": "execution-a",
    }
    with patch.object(store, "get_worktree", return_value=record), patch(
        "backend.db.d3_store.run_bounded_readonly_git",
        side_effect=DiffExtractionError("fixture failure"),
    ):
        result = store.get_worktree_diff("tenant-a", "wt-a")
    assert result["ok"] is False
    assert result["error_code"] == "DIFF_UNAVAILABLE"


_D3_DEFAULT_ROOT = object()


def _d3_record(repo: Path, checkout: Path, *, repo_root=_D3_DEFAULT_ROOT, state="active", governed=True):
    return {
        "path": str(checkout),
        "repo_root": str(repo) if repo_root is _D3_DEFAULT_ROOT else repo_root,
        "base_ref": "HEAD",
        "state": state,
        "status": state,
        "governed": governed,
        "session_id": "session-a",
        "execution_id": "execution-a",
    }


@pytest.mark.parametrize("repo_root", [None, "", "   "])
def test_d3_missing_or_blank_root_fails_closed_without_root_discovery(tmp_path, repo_root):
    repo = tmp_path / "repo"
    checkout = repo / "checkout"
    checkout.mkdir(parents=True)
    store = __import__("backend.db.d3_store", fromlist=["D3Store"]).D3Store()
    record = _d3_record(repo, checkout, repo_root=repo_root)
    with patch.object(store, "get_worktree", return_value=record), patch.object(
        store, "get_repo_root", side_effect=AssertionError("cwd fallback must not run")
    ), patch("backend.db.d3_store.run_bounded_readonly_git") as run_git:
        result = store.get_worktree_diff("tenant-a", "wt-missing-root")
    assert result["ok"] is False
    assert result["error_code"] == "DIFF_UNAVAILABLE"
    run_git.assert_not_called()


def test_d3_root_discovery_failure_does_not_fall_back_to_cwd(tmp_path):
    repo = tmp_path / "repo"
    checkout = repo / "checkout"
    checkout.mkdir(parents=True)
    store = __import__("backend.db.d3_store", fromlist=["D3Store"]).D3Store()
    record = _d3_record(repo, checkout, repo_root=str(tmp_path / "does-not-exist"))
    with patch.object(store, "get_worktree", return_value=record), patch.object(
        store, "get_repo_root", return_value=str(Path.cwd())
    ), patch("backend.db.d3_store.run_bounded_readonly_git") as run_git:
        result = store.get_worktree_diff("tenant-a", "wt-invalid-root")
    assert result["ok"] is False
    assert result["error_code"] == "DIFF_UNAVAILABLE"
    run_git.assert_not_called()


def test_d3_main_repository_cannot_masquerade_as_isolated_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = __import__("backend.db.d3_store", fromlist=["D3Store"]).D3Store()
    record = _d3_record(repo, repo, repo_root=str(repo))
    with patch.object(store, "get_worktree", return_value=record), patch(
        "backend.db.d3_store.run_bounded_readonly_git"
    ) as run_git:
        result = store.get_worktree_diff("tenant-a", "wt-main")
    assert result["ok"] is False
    run_git.assert_not_called()


def test_d3_traversal_alias_and_external_symlink_are_rejected(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "nested"
    nested.mkdir(parents=True)
    store = __import__("backend.db.d3_store", fromlist=["D3Store"]).D3Store()
    traversal_record = _d3_record(repo, nested / "..", repo_root=str(repo))
    with patch.object(store, "get_worktree", return_value=traversal_record), patch(
        "backend.db.d3_store.run_bounded_readonly_git"
    ) as run_git:
        result = store.get_worktree_diff("tenant-a", "wt-traversal")
    assert result["ok"] is False
    run_git.assert_not_called()

    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo / "linked-worktree"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    symlink_record = _d3_record(repo, link, repo_root=str(repo))
    with patch.object(store, "get_worktree", return_value=symlink_record), patch(
        "backend.db.d3_store.run_bounded_readonly_git"
    ) as run_git:
        result = store.get_worktree_diff("tenant-a", "wt-symlink")
    assert result["ok"] is False
    run_git.assert_not_called()


def test_d3_authorized_worktree_uses_shared_bounded_extractor(tmp_path):
    repo = tmp_path / "repo"
    checkout = repo / "checkout"
    checkout.mkdir(parents=True)
    (checkout / "module.py").write_text("print('safe')\n", encoding="utf-8")
    store = __import__("backend.db.d3_store", fromlist=["D3Store"]).D3Store()
    record = _d3_record(repo, checkout)
    with patch.object(store, "get_worktree", return_value=record), patch(
        "backend.db.d3_store.run_bounded_readonly_git", side_effect=["", "module.py\x00"]
    ) as run_git:
        result = store.get_worktree_diff("tenant-a", "wt-safe")
    assert result["ok"] is True
    assert "module.py" in result["diff"]
    assert "+print('safe')" in result["diff"]
    assert run_git.call_count == 2


@pytest.mark.parametrize("relative", [".ssh/id_rsa", ".env", ".env.local", ".git/index"])
def test_d3_excludes_sensitive_untracked_paths_before_file_read(tmp_path, relative):
    repo = tmp_path / "repo"
    checkout = repo / "checkout"
    checkout.mkdir(parents=True)
    candidate = checkout / Path(relative)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("synthetic-sentinel", encoding="utf-8")
    store = __import__("backend.db.d3_store", fromlist=["D3Store"]).D3Store()
    record = _d3_record(repo, checkout)
    with patch.object(store, "get_worktree", return_value=record), patch(
        "backend.db.d3_store.run_bounded_readonly_git", side_effect=["", relative + "\x00"]
    ):
        result = store.get_worktree_diff("tenant-a", "wt-sensitive")
    assert result["ok"] is True
    assert "synthetic-sentinel" not in result["diff"]


def test_d3_count_limit_is_applied_through_wrapper(tmp_path):
    repo = tmp_path / "repo"
    checkout = repo / "checkout"
    checkout.mkdir(parents=True)
    listing = "\x00".join(f"candidate-{i}.txt" for i in range(101)) + "\x00"
    store = __import__("backend.db.d3_store", fromlist=["D3Store"]).D3Store()
    record = _d3_record(repo, checkout)
    with patch.object(store, "get_worktree", return_value=record), patch(
        "backend.db.d3_store.run_bounded_readonly_git", side_effect=["", listing]
    ):
        result = store.get_worktree_diff("tenant-a", "wt-count")
    assert result["ok"] is False
    assert result["error_code"] == "DIFF_UNAVAILABLE"


def test_d3_per_file_and_rendered_limits_are_applied_through_wrapper(tmp_path):
    repo = tmp_path / "repo"
    checkout = repo / "checkout"
    checkout.mkdir(parents=True)
    (checkout / "large.txt").write_bytes(b"x" * (DIFF_MAX_UNTRACKED_FILE_BYTES + 1))
    store = __import__("backend.db.d3_store", fromlist=["D3Store"]).D3Store()
    record = _d3_record(repo, checkout)
    with patch.object(store, "get_worktree", return_value=record), patch(
        "backend.db.d3_store.run_bounded_readonly_git", side_effect=["", "large.txt\x00"]
    ):
        result = store.get_worktree_diff("tenant-a", "wt-file-limit")
    assert result["ok"] is False

    with patch.object(store, "get_worktree", return_value=record), patch(
        "backend.db.d3_store.run_bounded_readonly_git", side_effect=["x" * (DIFF_MAX_RENDERED_BYTES + 1), ""]
    ):
        result = store.get_worktree_diff("tenant-a", "wt-render-limit")
    assert result["ok"] is False


def test_alternate_route_rejects_main_root_registry_object_before_git(tmp_path):
    app, ctx = _route_context()
    repo = tmp_path / "repo"
    repo.mkdir()
    wt = worktree.Worktree(
        worktree_id="wt-main-route",
        session_id="session-a",
        execution_id="task-main-route",
        task_id="task-main-route",
        tenant_id="tenant-a",
        branch="fixture",
        path=str(repo),
        base_ref="HEAD",
        repo_root=str(repo),
    )
    record = _record("task-main-route", "tenant-a", payload={"worktree_id": wt.worktree_id})
    try:
        with patch.object(diff_route.durable_state, "get", return_value=record), patch.object(
            worktree, "_REGISTRY", {wt.worktree_id: wt}
        ), patch.object(worktree, "run_bounded_readonly_git") as run_git:
            response = diff_route.get_diff.__wrapped__("task-main-route")
        response_obj, status = _response_parts(response)
        assert status == 404
        assert response_obj.get_json()["ok"] is False
        run_git.assert_not_called()
    finally:
        ctx.pop()


def test_alternate_route_rejects_external_symlink_before_git(tmp_path):
    app, ctx = _route_context()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    link = repo / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    wt = worktree.Worktree(
        worktree_id="wt-link-route",
        session_id="session-a",
        execution_id="task-link-route",
        task_id="task-link-route",
        tenant_id="tenant-a",
        branch="fixture",
        path=str(link),
        base_ref="HEAD",
        repo_root=str(repo),
    )
    record = _record("task-link-route", "tenant-a", payload={"worktree_id": wt.worktree_id})
    try:
        with patch.object(diff_route.durable_state, "get", return_value=record), patch.object(
            worktree, "_REGISTRY", {wt.worktree_id: wt}
        ), patch.object(worktree, "run_bounded_readonly_git") as run_git:
            response = diff_route.get_diff.__wrapped__("task-link-route")
        response_obj, status = _response_parts(response)
        assert status == 404
        assert response_obj.get_json()["ok"] is False
        run_git.assert_not_called()
    finally:
        ctx.pop()


def test_direct_worktree_diff_uses_governance_before_bounded_shared_policy(tmp_path):
    repo = tmp_path / "repo"
    checkout = repo / "checkout"
    checkout.mkdir(parents=True)
    (checkout / "module.py").write_text("print('governed')\n", encoding="utf-8")
    wt = worktree.Worktree(
        worktree_id="wt-governed",
        session_id="session-a",
        execution_id="task-governed",
        task_id="task-governed",
        tenant_id="tenant-a",
        branch="fixture",
        path=str(checkout),
        base_ref="HEAD",
        repo_root=str(repo),
    )
    governance_calls = []
    governance_after = []

    class _Governance:
        def before_dispatch(self, *args, **kwargs):
            governance_calls.append((args, kwargs))
            return SimpleNamespace(allowed=True, policy="allow", reason="synthetic")

        def after_dispatch(self, *args, **kwargs):
            governance_after.append((args, kwargs))
            return None

    class _Policy:
        def build_safe_subprocess_kwargs(self, command, cwd, timeout_sec):
            return {
                "args": shlex.split(command),
                "cwd": cwd,
                "env": {},
                "shell": False,
            }

    outputs = iter(("", "module.py\x00"))
    bounded_calls = []

    def bounded(argv, cwd, **kwargs):
        bounded_calls.append((list(argv), cwd, kwargs))
        kwargs["on_complete"](0)
        return next(outputs)

    with patch.object(worktree, "_REGISTRY", {wt.worktree_id: wt}), patch.object(
        worktree, "get_tool_governance", return_value=_Governance()
    ), patch.object(worktree, "global_command_policy", _Policy()), patch.object(
        worktree, "CommandDecision", object
    ), patch.object(
        worktree, "run_bounded_readonly_git", side_effect=bounded
    ):
        diff = worktree.worktree_diff(
            wt.worktree_id,
            tenant_id="tenant-a",
            session_id="session-a",
            execution_id="task-governed",
        )

    assert "module.py" in diff
    assert "+print('governed')" in diff
    assert len(governance_calls) == 2
    assert len(governance_after) == 2
    assert len(bounded_calls) == 2
    assert all(call[2]["popen_kwargs"]["shell"] is False for call in bounded_calls)


def test_direct_worktree_diff_sensitive_listing_is_filtered_before_read(tmp_path):
    repo = tmp_path / "repo"
    checkout = repo / "checkout"
    checkout.mkdir(parents=True)
    secret_path = checkout / ".ssh" / "id_rsa"
    secret_path.parent.mkdir()
    secret_path.write_text("synthetic-not-a-secret", encoding="utf-8")
    wt = worktree.Worktree(
        worktree_id="wt-sensitive-governed",
        session_id="session-a",
        execution_id="task-sensitive-governed",
        task_id="task-sensitive-governed",
        tenant_id="tenant-a",
        branch="fixture",
        path=str(checkout),
        base_ref="HEAD",
        repo_root=str(repo),
    )

    class _Governance:
        def before_dispatch(self, *args, **kwargs):
            return SimpleNamespace(allowed=True, policy="allow", reason="synthetic")

        def after_dispatch(self, *args, **kwargs):
            return None

    class _Policy:
        def build_safe_subprocess_kwargs(self, command, cwd, timeout_sec):
            return {"args": shlex.split(command), "cwd": cwd, "env": {}, "shell": False}

    outputs = iter(("", ".ssh/id_rsa\x00"))

    def bounded(argv, cwd, **kwargs):
        kwargs["on_complete"](0)
        return next(outputs)

    with patch.object(worktree, "_REGISTRY", {wt.worktree_id: wt}), patch.object(
        worktree, "get_tool_governance", return_value=_Governance()
    ), patch.object(worktree, "global_command_policy", _Policy()), patch.object(
        worktree, "CommandDecision", object
    ), patch.object(
        worktree, "run_bounded_readonly_git", side_effect=bounded
    ):
        diff = worktree.worktree_diff(
            wt.worktree_id,
            tenant_id="tenant-a",
            session_id="session-a",
            execution_id="task-sensitive-governed",
        )

    assert diff == ""
    assert "synthetic-not-a-secret" not in diff


def _bounded_tracked_argv():
    return build_tracked_diff_argv("HEAD", context_lines=3)


def test_missing_governance_fails_before_bounded_runner():
    with patch.object(worktree, "run_bounded_readonly_git") as run_git:
        with pytest.raises(worktree.WorktreeError, match="requires both tool governance"):
            worktree._governed_bounded_readonly_git(
                "worktree_diff_bounded",
                _bounded_tracked_argv(),
                Path("C:/disposable/worktree"),
                "session-a",
                "worktree-a",
            )
    run_git.assert_not_called()


def test_governance_denial_propagates_before_bounded_runner():
    class _DenyGovernance:
        def before_dispatch(self, *args, **kwargs):
            return SimpleNamespace(allowed=False, policy="deny", reason="synthetic denial")

    class _Policy:
        def build_safe_subprocess_kwargs(self, *args, **kwargs):
            raise AssertionError("policy builder must not run after denial")

    with patch.object(worktree, "get_tool_governance", return_value=_DenyGovernance()), patch.object(
        worktree, "global_command_policy", _Policy()
    ), patch.object(worktree, "CommandDecision", object), patch.object(
        worktree, "run_bounded_readonly_git"
    ) as run_git:
        with pytest.raises(worktree.WorktreeDenied, match="refused by policy"):
            worktree._governed_bounded_readonly_git(
                "worktree_diff_bounded",
                _bounded_tracked_argv(),
                Path("C:/disposable/worktree"),
                "session-a",
                "worktree-a",
            )
    run_git.assert_not_called()


def test_governance_argv_retokenization_is_rejected_before_bounded_runner():
    class _AllowGovernance:
        def before_dispatch(self, *args, **kwargs):
            return SimpleNamespace(allowed=True, policy="allow", reason="synthetic")

    class _TamperingPolicy:
        def build_safe_subprocess_kwargs(self, command, cwd, timeout_sec):
            return {"args": ["git", "status"], "cwd": cwd, "shell": False}

    with patch.object(worktree, "get_tool_governance", return_value=_AllowGovernance()), patch.object(
        worktree, "global_command_policy", _TamperingPolicy()
    ), patch.object(worktree, "CommandDecision", object), patch.object(
        worktree, "run_bounded_readonly_git"
    ) as run_git:
        with pytest.raises(worktree.WorktreeError, match="changed the permitted argv"):
            worktree._governed_bounded_readonly_git(
                "worktree_diff_bounded",
                _bounded_tracked_argv(),
                Path("C:/disposable/worktree"),
                "session-a",
                "worktree-a",
            )
    run_git.assert_not_called()


class _SyntheticPipe:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def read(self, _size):
        return next(self._chunks, b"")


class _SyntheticTimeoutPopen:
    def __init__(self, **kwargs):
        self.stdout = _SyntheticPipe([b""])
        self.stderr = _SyntheticPipe([b""])
        self.returncode = None
        self.events = []
        self._waits = 0

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        self._waits += 1
        if self._waits == 1:
            raise subprocess.TimeoutExpired(["git"], timeout)
        self.returncode = -9
        return self.returncode

    def kill(self):
        self.events.append("kill")


def test_bounded_runner_timeout_uses_fake_process_and_fails_truthfully(monkeypatch, tmp_path):
    fake = _SyntheticTimeoutPopen()
    monkeypatch.setattr("backend.execution.diff_policy.subprocess.Popen", lambda **kwargs: fake)
    with pytest.raises(DiffExtractionError, match="timed out"):
        run_bounded_readonly_git(["git", "diff"], tmp_path)
    assert "kill" in fake.events


def test_worktree_capability_is_not_serialized_or_recoverable_from_worktree():
    wt = worktree.Worktree(
        worktree_id="private-capability",
        session_id="session-a",
        execution_id="execution-a",
        task_id="task-a",
        tenant_id="tenant-a",
        branch="fixture",
        path="C:/disposable/worktree",
        base_ref="HEAD",
        repo_root="C:/disposable/repo",
    )
    payload = wt.to_dict()
    assert "owner_token" not in payload
    assert "capability" not in payload
    assert "_PROCESS_CAPABILITIES" not in payload
    assert not hasattr(wt, "owner_token")
    assert not hasattr(worktree, "get_process_capability")


def test_alternate_stored_sensitive_diff_is_rejected():
    app, ctx = _route_context()
    try:
        record = _record(
            "task-sensitive-stored",
            "tenant-a",
            result={"diff": "diff --git a/.ssh/id_rsa b/.ssh/id_rsa\n"},
        )
        with patch.object(diff_route.durable_state, "get", return_value=record):
            response = diff_route.get_diff.__wrapped__("task-sensitive-stored")
        response_obj, status = _response_parts(response)
        assert status == 413
        assert response_obj.get_json()["ok"] is False
        assert ".ssh/id_rsa" not in response_obj.get_data(as_text=True)
    finally:
        ctx.pop()


@pytest.mark.parametrize(
    "field, value",
    [
        ("result_json", "{"),
        ("payload_json", "{"),
        ("result_json", "[]"),
        ("payload_json", "[]"),
    ],
)
def test_task_diff_malformed_durable_json_fails_closed(field, value):
    app, ctx = _route_context("/api/task/task-invalid/diff")
    try:
        record = _record("task-invalid", "tenant-a", result={"diff": ""})
        record[field] = value
        with patch.object(tasks.durable_state, "get", return_value=record):
            response = tasks.get_task_diff.__wrapped__("task-invalid")
        response_obj, status = _response_parts(response)
        assert status == 500
        assert response_obj.get_json() == {
            "ok": False,
            "error_code": "DIFF_RECORD_INVALID",
            "error": "Diff record is invalid",
            "task_id": "task-invalid",
        }
    finally:
        ctx.pop()


def test_task_diff_missing_required_payload_and_invalid_stored_diff_fail_closed():
    app, ctx = _route_context("/api/task/task-invalid/diff")
    try:
        missing = _record("task-invalid", "tenant-a", result={"diff": ""})
        missing.pop("payload_json")
        invalid = _record("task-invalid", "tenant-a", result={"diff": 123})
        for record in (missing, invalid):
            with patch.object(tasks.durable_state, "get", return_value=record):
                response = tasks.get_task_diff.__wrapped__("task-invalid")
            response_obj, status = _response_parts(response)
            assert status == 500
            assert response_obj.get_json()["error_code"] == "DIFF_RECORD_INVALID"
    finally:
        ctx.pop()


def test_task_diff_valid_empty_and_nonempty_stored_results_remain_successful():
    app, ctx = _route_context("/api/task/task-valid/diff")
    try:
        for diff_text in ("", "diff --git a/src.py b/src.py\n"):
            record = _record("task-valid", "tenant-a", result={"diff": diff_text})
            with patch.object(tasks.durable_state, "get", return_value=record):
                response = tasks.get_task_diff.__wrapped__("task-valid")
            response_obj, status = _response_parts(response)
            assert status == 200
            assert response_obj.get_json()["ok"] is True
            assert response_obj.get_json()["diff"] == diff_text
    finally:
        ctx.pop()


def test_task_review_malformed_record_is_sanitized():
    app, ctx = _route_context("/api/task/task-review/review")
    try:
        record = _record("task-review", "tenant-a", result={"diff": ""})
        record["payload_json"] = "not-json"
        with patch.object(tasks.durable_state, "get", return_value=record):
            response = tasks.get_task_review.__wrapped__("task-review")
        response_obj, status = _response_parts(response)
        assert status == 500
        assert response_obj.get_json()["error_code"] == "DIFF_RECORD_INVALID"
    finally:
        ctx.pop()


def test_task_diff_unauthorized_task_does_not_return_empty_success():
    app, ctx = _route_context("/api/task/foreign/diff")
    try:
        with patch.object(tasks.durable_state, "get", return_value=None):
            response = tasks.get_task_diff.__wrapped__("foreign")
        response_obj, status = _response_parts(response)
        assert status == 404
        assert response_obj.get_json()["ok"] is False
    finally:
        ctx.pop()
