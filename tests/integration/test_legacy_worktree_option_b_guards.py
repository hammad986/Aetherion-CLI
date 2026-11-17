"""Negative tests for legacy physical worktree boundaries under Option B."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["AETHERION_ENV"] = "CI-test"
os.environ["ENVIRONMENT"] = "testing"
os.environ["JWT_SECRET"] = "legacy-worktree-test-secret-" + "x" * 48
os.environ["AETHERION_RELEASE_SCOPE"] = "control-plane"
os.environ["AETHERION_EXECUTION_DISABLED"] = "1"

from backend.execution.agent_runtime.context import (
    DiffExtractionError,
    TaskExecutionContext,
    cleanup_execution_worktree,
    get_execution_diff,
)
from backend.execution.control_plane_policy import (
    EXECUTION_DISABLED_MESSAGE,
    ExecutionDisabledError,
    get_control_plane_policy,
)
from backend.execution import worktree


@pytest.fixture(autouse=True)
def option_b_is_active():
    policy = get_control_plane_policy()
    assert policy.execution_enabled is False


def test_module_import_and_read_only_registry_helpers_remain_available():
    assert worktree.list_worktrees(session_id="missing-session") == []
    assert worktree.get_worktree("missing-worktree") is None


def test_create_worktree_rejects_before_governance_git_or_filesystem(tmp_path):
    with patch.object(worktree, "_require_governance") as require_governance, patch.object(
        worktree, "_git"
    ) as git_call, patch.object(worktree.os, "makedirs") as makedirs, patch.object(
        worktree, "get_tenant"
    ) as get_tenant, patch.object(worktree.subprocess, "run") as subprocess_run:
        with pytest.raises(ExecutionDisabledError) as exc_info:
            worktree.create_worktree(
                session_id="session-a",
                execution_id="execution-a",
                task_id="task-a",
                tenant_id="tenant-a",
                repo_root=str(tmp_path),
            )

    assert str(exc_info.value) == EXECUTION_DISABLED_MESSAGE
    require_governance.assert_not_called()
    git_call.assert_not_called()
    makedirs.assert_not_called()
    get_tenant.assert_not_called()
    subprocess_run.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "helper, args",
    [
        ("discard_worktree", ("worktree-a",)),
        ("sweep_session", ("session-a",)),
        ("sweep_orphans", ()),
    ],
)
def test_cleanup_helpers_reject_before_registry_git_or_filesystem_side_effects(helper, args):
    with patch.object(worktree, "_git") as git_call, patch.object(
        worktree.subprocess, "run"
    ) as subprocess_run, patch.object(worktree.shutil, "rmtree") as rmtree, patch.object(
        worktree, "_terminate_children"
    ) as terminate_children:
        with pytest.raises(ExecutionDisabledError) as exc_info:
            getattr(worktree, helper)(*args)

    assert str(exc_info.value) == EXECUTION_DISABLED_MESSAGE
    git_call.assert_not_called()
    subprocess_run.assert_not_called()
    rmtree.assert_not_called()
    terminate_children.assert_not_called()


def test_execution_context_cleanup_rejects_before_helper_or_direct_git_fallback(tmp_path):
    context = TaskExecutionContext(
        task_id="task-a",
        tenant_id="tenant-a",
        session_id="session-a",
        workspace_dir=tmp_path,
        repo_root=str(tmp_path),
        worktree_id="worktree-a",
        branch="aether/task-a",
    )

    with patch("backend.execution.worktree.discard_worktree") as discard, patch(
        "subprocess.run"
    ) as subprocess_run:
        with pytest.raises(ExecutionDisabledError) as exc_info:
            cleanup_execution_worktree(context)

    assert str(exc_info.value) == EXECUTION_DISABLED_MESSAGE
    discard.assert_not_called()
    subprocess_run.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_direct_git_entrypoint_is_not_needed_for_policy_blocked_mutators():
    """Ensure direct physical Git mutations are blocked at the dispatcher too."""
    with patch.object(worktree, "_require_governance") as require_governance, patch.object(
        worktree, "get_tool_governance"
    ) as get_tool_governance, patch.object(worktree.subprocess, "run") as subprocess_run:
        with pytest.raises(ExecutionDisabledError):
            worktree._git(
                "worktree_add",
                ["git", "worktree", "add", "-b", "aeth/task", "C:/tmp/wt", "HEAD"],
                ".",
                "session-a",
            )
    require_governance.assert_not_called()
    get_tool_governance.assert_not_called()
    subprocess_run.assert_not_called()


@pytest.mark.parametrize(
    "action, argv",
    [
        ("worktree_add", ["git", "worktree", "add", "-b", "aeth/task", "C:/tmp/wt", "HEAD"]),
        ("worktree_remove", ["git", "worktree", "remove", "--force", "C:/tmp/wt"]),
        ("worktree_prune", ["git", "worktree", "prune"]),
        ("branch_delete", ["git", "branch", "-D", "aeth/task"]),
    ],
)
def test_all_direct_git_mutations_reject_before_governance_or_process(
    action, argv, tmp_path
):
    """Every physical action remains blocked and cannot touch even a fixture repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    index_path = repo / ".git" / "index"
    before_index = (
        hashlib.sha256(index_path.read_bytes()).hexdigest() if index_path.exists() else None
    )

    with patch.object(worktree, "_require_governance") as require_governance, patch.object(
        worktree, "get_tool_governance"
    ) as get_tool_governance, patch.object(worktree.subprocess, "run") as subprocess_run:
        with pytest.raises(ExecutionDisabledError) as exc_info:
            worktree._git(action, argv, str(repo), "session-a")

    assert str(exc_info.value) == EXECUTION_DISABLED_MESSAGE
    require_governance.assert_not_called()
    get_tool_governance.assert_not_called()
    subprocess_run.assert_not_called()
    assert index_path.exists() == (before_index is not None)
    if before_index is not None:
        assert hashlib.sha256(index_path.read_bytes()).hexdigest() == before_index


def test_unknown_git_action_rejects_before_governance_or_process(tmp_path):
    """Unknown action names cannot borrow a read-only label for a mutation argv."""
    with patch.object(worktree, "_require_governance") as require_governance, patch.object(
        worktree, "get_tool_governance"
    ) as get_tool_governance, patch.object(worktree.subprocess, "run") as subprocess_run:
        with pytest.raises(worktree.WorktreeError, match="unsupported worktree git action"):
            worktree._git(
                "future_mutation",
                ["git", "branch", "-D", "aeth/task"],
                str(tmp_path),
                "session-a",
            )

    require_governance.assert_not_called()
    get_tool_governance.assert_not_called()
    subprocess_run.assert_not_called()


class _FakeProcess:
    def __init__(self, timeout_on_first_wait=False):
        self.events = []
        self.timeout_on_first_wait = timeout_on_first_wait
        self._waits = 0

    def poll(self):
        self.events.append("poll")
        return None

    def terminate(self):
        self.events.append("terminate")

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        self._waits += 1
        if self.timeout_on_first_wait and self._waits == 1:
            raise subprocess.TimeoutExpired(["fake-child"], timeout)

    def kill(self):
        self.events.append("kill")


class _ExitDuringGraceProcess(_FakeProcess):
    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        self._waits += 1
        self.exited = True


class _RetryableProcess(_FakeProcess):
    def __init__(self):
        super().__init__()
        self.fail_terminate = True

    def terminate(self):
        self.events.append("terminate")
        if self.fail_terminate:
            raise RuntimeError("synthetic cleanup failure")


@pytest.fixture
def isolated_process_registry(monkeypatch):
    monkeypatch.setattr(worktree, "_REGISTRY", {})
    monkeypatch.setattr(worktree, "_PROCS", {})
    monkeypatch.setattr(worktree, "_PROCESS_CAPABILITIES", {})


def _owned_worktree(worktree_id: str) -> worktree.Worktree:
    return worktree.Worktree(
        worktree_id=worktree_id,
        session_id="session-a",
        execution_id="execution-a",
        task_id="task-a",
        tenant_id="tenant-a",
        branch="aeth/task-a",
        path="C:/disposable/worktree",
        base_ref="HEAD",
        repo_root="C:/disposable/repo",
    )


def _trusted_capability(wt: worktree.Worktree):
    """Seed the private lifecycle registry as a synthetic trusted creation."""
    capability = worktree._ProcessOwnershipCapability(
        wt.worktree_id,
        wt.tenant_id,
        wt.session_id,
        wt.execution_id,
        "synthetic-lifecycle-nonce",
        seal=worktree._CAPABILITY_SEAL,
    )
    worktree._PROCESS_CAPABILITIES[wt.worktree_id] = capability
    return capability


def test_terminate_children_is_only_allowed_for_registered_owned_worktree(
    isolated_process_registry,
):
    """Option B permits narrowly scoped teardown, never an unowned process kill."""
    unowned = _FakeProcess()
    worktree._PROCS["unowned"] = [("unowned-token", unowned)]
    assert worktree._terminate_children("unowned") == 0
    assert unowned.events == []

    owned_id = "owned"
    owned_worktree = _owned_worktree(owned_id)
    worktree._REGISTRY[owned_id] = owned_worktree
    owned = _FakeProcess()
    worktree._PROCS[owned_id] = [(_trusted_capability(owned_worktree), owned)]

    assert worktree._terminate_children(owned_id) == 1
    assert [event for event in owned.events if event in ("terminate", "kill")] == ["terminate"]
    assert owned.events[0] == "poll"
    assert owned.events[-1][0] == "wait"


def test_terminate_children_kills_owned_process_only_after_grace_timeout(
    isolated_process_registry,
):
    owned_id = "owned-timeout"
    owned_worktree = _owned_worktree(owned_id)
    worktree._REGISTRY[owned_id] = owned_worktree
    process = _FakeProcess(timeout_on_first_wait=True)
    worktree._PROCS[owned_id] = [(_trusted_capability(owned_worktree), process)]

    assert worktree._terminate_children(owned_id, grace_sec=0.25) == 1
    assert [event for event in process.events if event in ("terminate", "kill")] == [
        "terminate",
        "kill",
    ]
    assert process.events[0] == "poll"
    assert process.events[-1] == ("wait", 0.25)


def test_attach_process_rejects_unknown_worktree_under_option_b(isolated_process_registry):
    process = _FakeProcess()
    capability = worktree._ProcessOwnershipCapability(
        "missing", "tenant-a", "session-a", "execution-a", "synthetic",
        seal=worktree._CAPABILITY_SEAL,
    )
    with patch.object(worktree, "assert_agent_execution_enabled", return_value=None):
        with pytest.raises(worktree.WorktreeError, match="unknown or unauthorized worktree"):
            worktree.attach_process(
                "missing",
                process,
                tenant_id="tenant-a",
                session_id="session-a",
                execution_id="execution-a",
                capability=capability,
            )
    assert worktree._PROCS == {}


def test_attach_process_rejects_discarded_worktree_and_arbitrary_process_object(
    isolated_process_registry,
):
    discarded_id = "discarded"
    discarded = _owned_worktree(discarded_id)
    discarded.status = "discarded"
    worktree._REGISTRY[discarded_id] = discarded
    discarded_capability = _trusted_capability(discarded)
    with patch.object(worktree, "assert_agent_execution_enabled", return_value=None):
        with pytest.raises(worktree.WorktreeError, match="not active"):
            worktree.attach_process(
                discarded_id,
                _FakeProcess(),
                tenant_id="tenant-a",
                session_id=discarded.session_id,
                execution_id=discarded.execution_id,
                capability=discarded_capability,
            )

    active_id = "active-untrusted"
    active = _owned_worktree(active_id)
    worktree._REGISTRY[active_id] = active
    active_capability = _trusted_capability(active)
    with patch.object(worktree, "assert_agent_execution_enabled", return_value=None):
        with pytest.raises(worktree.WorktreeError, match="trusted runtime"):
            worktree.attach_process(
                active_id,
                _FakeProcess(),
                tenant_id=active.tenant_id,
                session_id=active.session_id,
                execution_id=active.execution_id,
                capability=active_capability,
            )
    assert worktree._PROCS == {}


def test_attach_process_accepts_owned_trusted_handle_and_cleanup_reaps_it(
    isolated_process_registry,
):
    owned_id = "attached-owned"
    owned = _owned_worktree(owned_id)
    worktree._REGISTRY[owned_id] = owned
    capability = _trusted_capability(owned)
    process = _FakeProcess()

    # Simulate the trusted runtime's concrete Popen class without launching a
    # child process in this test.
    with patch.object(worktree, "assert_agent_execution_enabled", return_value=None):
        with patch.object(worktree.subprocess, "Popen", _FakeProcess):
            worktree.attach_process(
                owned_id,
                process,
                tenant_id=owned.tenant_id,
                session_id=owned.session_id,
                execution_id=owned.execution_id,
                capability=capability,
            )

    assert worktree._PROCS[owned_id] == [(capability, process)]
    assert worktree._terminate_children(owned_id) == 1
    assert "terminate" in process.events


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("tenant_id", "tenant-b", "tenant-mismatch"),
        ("session_id", "session-b", "session-mismatch"),
        ("execution_id", "execution-b", "execution-mismatch"),
        ("capability", "wrong-capability", "capability-mismatch"),
    ],
)
def test_attach_process_rejects_cross_owner_capabilities(
    isolated_process_registry, field, value, message
):
    active_id = "active-owner"
    active = _owned_worktree(active_id)
    worktree._REGISTRY[active_id] = active
    valid_capability = _trusted_capability(active)
    kwargs = {
        "tenant_id": active.tenant_id,
        "session_id": active.session_id,
        "execution_id": active.execution_id,
        "capability": valid_capability,
    }
    if field == "capability":
        kwargs[field] = worktree._ProcessOwnershipCapability(
            active.worktree_id,
            active.tenant_id,
            active.session_id,
            active.execution_id,
            "wrong-capability",
            seal=worktree._CAPABILITY_SEAL,
        )
    else:
        kwargs[field] = value
    with patch.object(worktree, "assert_agent_execution_enabled", return_value=None):
        with pytest.raises(worktree.WorktreeError, match=message):
            worktree.attach_process(active_id, _FakeProcess(), **kwargs)
    assert worktree._PROCS == {}


def test_option_b_rejects_even_a_valid_lifecycle_capability_for_attachment(
    isolated_process_registry,
):
    active = _owned_worktree("disabled-attach")
    worktree._REGISTRY[active.worktree_id] = active
    capability = _trusted_capability(active)
    process = _FakeProcess()
    with pytest.raises(ExecutionDisabledError):
        worktree.attach_process(
            active.worktree_id,
            process,
            tenant_id=active.tenant_id,
            session_id=active.session_id,
            execution_id=active.execution_id,
            capability=capability,
        )
    assert worktree._PROCS == {}


def test_retirement_state_blocks_attachment_and_prevents_race(
    isolated_process_registry,
):
    active = _owned_worktree("retiring")
    worktree._REGISTRY[active.worktree_id] = active
    capability = _trusted_capability(active)
    assert worktree._terminate_children(active.worktree_id) == 0
    assert active.status == "retiring"
    with patch.object(worktree, "assert_agent_execution_enabled", return_value=None):
        with pytest.raises(worktree.WorktreeError, match="not active"):
            worktree.attach_process(
                active.worktree_id,
                _FakeProcess(),
                tenant_id=active.tenant_id,
                session_id=active.session_id,
                execution_id=active.execution_id,
                capability=capability,
            )
    assert worktree._PROCS == {}


def test_process_exit_during_grace_does_not_trigger_kill(isolated_process_registry):
    active = _owned_worktree("exits-during-grace")
    worktree._REGISTRY[active.worktree_id] = active
    capability = _trusted_capability(active)
    process = _ExitDuringGraceProcess()
    worktree._PROCS[active.worktree_id] = [(capability, process)]

    assert worktree._terminate_children(active.worktree_id, grace_sec=0.25) == 1
    assert "terminate" in process.events
    assert "kill" not in process.events
    assert active.status == "retiring"
    assert active.worktree_id not in worktree._PROCS


def test_cleanup_failure_retains_handle_for_retry(isolated_process_registry):
    active = _owned_worktree("retry-cleanup")
    worktree._REGISTRY[active.worktree_id] = active
    capability = _trusted_capability(active)
    process = _RetryableProcess()
    worktree._PROCS[active.worktree_id] = [(capability, process)]

    assert worktree._terminate_children(active.worktree_id) == 0
    assert worktree._PROCS[active.worktree_id] == [(capability, process)]
    process.fail_terminate = False
    assert worktree._terminate_children(active.worktree_id) == 1
    assert active.worktree_id not in worktree._PROCS


def _git_fixture_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "option-b-test@example.invalid"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Option B Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
        text=True,
    )
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "fixture-branch", str(checkout), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return repo, checkout


def _git_index_path(repo_or_worktree: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo_or_worktree), "rev-parse", "--git-path", "index"],
        check=True,
        capture_output=True,
        text=True,
    )
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo_or_worktree / path


def test_execution_diff_is_read_only_and_includes_tracked_and_untracked_changes(tmp_path):
    """The attached-worktree diff never stages and still reports both file classes."""
    repo, checkout = _git_fixture_worktree(tmp_path)
    (checkout / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    (checkout / "new.txt").write_text("new content\n", encoding="utf-8")
    parent_index = _git_index_path(repo)
    checkout_index = _git_index_path(checkout)
    before_indexes = {
        "parent": hashlib.sha256(parent_index.read_bytes()).hexdigest(),
        "checkout": hashlib.sha256(checkout_index.read_bytes()).hexdigest(),
    }
    context = TaskExecutionContext(
        task_id="task-diff",
        tenant_id="tenant-a",
        session_id="session-a",
        workspace_dir=checkout,
        repo_root=str(repo),
        worktree_id="fixture-worktree",
        base_ref="HEAD",
    )

    registered = worktree.Worktree(
        worktree_id="fixture-worktree",
        session_id="session-a",
        execution_id="task-diff",
        task_id="task-diff",
        tenant_id="tenant-a",
        branch="fixture-branch",
        path=str(checkout),
        base_ref="HEAD",
        repo_root=str(repo),
    )
    with patch.object(worktree, "_REGISTRY", {"fixture-worktree": registered}):
        diff = get_execution_diff(context)

    assert "tracked.txt" in diff
    assert "+changed" in diff
    assert "new.txt" in diff
    assert "+new content" in diff
    assert hashlib.sha256(parent_index.read_bytes()).hexdigest() == before_indexes["parent"]
    assert hashlib.sha256(checkout_index.read_bytes()).hexdigest() == before_indexes["checkout"]


def test_execution_diff_failure_does_not_fall_back_to_false_success(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    context = TaskExecutionContext(
        task_id="task-diff-failure",
        tenant_id="tenant-a",
        session_id="session-a",
        workspace_dir=checkout,
        repo_root=str(repo),
        worktree_id="fixture-worktree",
        base_ref="HEAD",
    )
    registered = worktree.Worktree(
        worktree_id="fixture-worktree",
        session_id="session-a",
        execution_id="task-diff-failure",
        task_id="task-diff-failure",
        tenant_id="tenant-a",
        branch="fixture-branch",
        path=str(checkout),
        base_ref="HEAD",
        repo_root=str(repo),
    )

    with patch.object(worktree, "_REGISTRY", {"fixture-worktree": registered}), patch(
        "backend.execution.agent_runtime.context.run_bounded_readonly_git",
        side_effect=DiffExtractionError("read-only Git inspection failed (rc=128)"),
    ) as run:
        with pytest.raises(DiffExtractionError, match="read-only Git inspection failed"):
            get_execution_diff(context)

    run.assert_called_once()


def test_plain_workspace_diff_keeps_path_traversal_protection(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be read\n", encoding="utf-8")
    context = TaskExecutionContext(task_id="task-path", workspace_dir=workspace)

    with pytest.raises(DiffExtractionError, match="unsafe traversal|escapes the execution workspace"):
        get_execution_diff(context, ["../outside.txt"])
