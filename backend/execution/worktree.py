"""
execution/worktree.py — Phase 4: git worktree execution isolation.

One task, one worktree, one branch. Parallel agents stop fighting over a shared
checkout without any new sandboxing, path-confinement, locking, audit or event
machinery: every one of those already exists and this module composes them.

  rooting/confinement  infra.tenant.TenantContext.workspace_path()   (realpath)
  command governance   execution.tool_governance + security.command_policy
  serialisation        execution.workspace_lock.workspace_operation_lock
  teardown             attach_process() below, SIGTERM then SIGKILL
  execution history    execution.truth_kernel.get_ledger()
  events               execution.events + event_bus.global_event_bus
  telemetry            infra.telemetry.get_telemetry()

Deliberately NOT used: ``execution.sandbox.ExecutionSandbox``. Its
``validate_command`` is a substring test against ["rm -rf /", "mkfs", "dd"], so
it rejects ``git add .`` (the "dd" in "add") while letting ``rm -fr /`` through,
and its ``validate_path_access`` uses ``abspath`` where it claims to resolve
symlinks. ``security/command_policy.py`` is the authoritative engine — its own
docstring says it replaces that denylist.

Import safety: ``entrypoint.sh`` runs gunicorn with ``--preload``, so an
exception at import time kills the master process, not just one worker. Every
cross-subsystem import below is therefore optional and degrades to None.

Rollback: nothing here is wired into a request path by import. Delete this module
and the platform behaves exactly as it did before; a worktree left on disk is
reclaimed by ``git worktree prune`` plus normal artifact expiry.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .control_plane_policy import assert_agent_execution_enabled
from .diff_policy import (
    DIFF_MAX_GIT_TIMEOUT_SEC,
    DiffExtractionError,
    build_untracked_listing_argv,
    build_tracked_diff_argv,
    diff_pathspec_exclusions,
    extract_worktree_diff,
    resolve_authorized_worktree_path,
    run_bounded_readonly_git,
)

logger = logging.getLogger("aetherion.execution.worktree")

# ── Optional cross-subsystem imports ────────────────────────────────────────
# Every one of these is optional. A missing dependency degrades one integration
# (governance, locking, history, events, telemetry) and is reported through
# ``integration_status()``; it never raises at import time.

# One import per block, deliberately. A combined ``from X import (a, b, c)``
# would send *all three* names to None when only one is missing, silently
# disabling governance instead of degrading a single integration.
#
# ``build_safe_subprocess_kwargs`` is a *method* on CommandPolicyEngine, not a
# module-level function; it is reached through the singleton at the call site.
try:
    from security.command_policy import CommandDecision, global_command_policy
except Exception as _exc:                                    # pragma: no cover
    global_command_policy = None
    CommandDecision = None
    logger.warning("[worktree] command policy unavailable: %s", _exc)

# Order 4.1 requires every git call to pass through ``before_dispatch``. When
# this is unavailable the module refuses to run git at all -- a missing policy
# engine is a denial, not a bypass (the rule mcp_client._admit_mount follows).
try:
    from execution.tool_governance import get_tool_governance
except Exception as _exc:                                    # pragma: no cover
    get_tool_governance = None
    logger.warning("[worktree] tool governance unavailable: %s", _exc)

try:
    from execution.workspace_lock import workspace_operation_lock, LockTimeoutError
except Exception as _exc:                                    # pragma: no cover
    workspace_operation_lock = None

    class LockTimeoutError(Exception):
        """Fallback so callers can always name the timeout type."""

    logger.warning("[worktree] workspace lock unavailable: %s", _exc)

try:
    from infra.tenant import get_tenant
except Exception as _exc:                                    # pragma: no cover
    get_tenant = None
    logger.warning("[worktree] tenant context unavailable: %s", _exc)

try:
    from execution.truth_kernel import get_ledger
except Exception as _exc:                                    # pragma: no cover
    get_ledger = None
    logger.warning("[worktree] evidence ledger unavailable: %s", _exc)

try:
    from execution.events import EventTypes, create_event
except Exception as _exc:                                    # pragma: no cover
    EventTypes, create_event = None, None
    logger.warning("[worktree] typed events unavailable: %s", _exc)

try:
    from event_bus import global_event_bus
except Exception as _exc:                                    # pragma: no cover
    global_event_bus = None
    logger.warning("[worktree] event bus unavailable: %s", _exc)

try:
    from infra.telemetry import get_telemetry
except Exception as _exc:                                    # pragma: no cover
    get_telemetry = None
    logger.warning("[worktree] telemetry unavailable: %s", _exc)

# ── Configuration ───────────────────────────────────────────────────────────

_GIT_TIMEOUT_SEC   = int(os.getenv("WORKTREE_GIT_TIMEOUT_SEC", "120"))
_LOCK_TIMEOUT_SEC  = float(os.getenv("WORKTREE_LOCK_TIMEOUT_SEC", "30"))
_MAX_PER_SESSION   = int(os.getenv("WORKTREE_MAX_PER_SESSION", "8"))
_BRANCH_PREFIX     = os.getenv("WORKTREE_BRANCH_PREFIX", "aeth")
_WORKTREE_SUBDIR   = os.getenv("WORKTREE_SUBDIR", "worktrees")

# Branch/slug sanitiser. git refnames reject ~ ^ : ? * [ \ and whitespace, and a
# worktree path must never contain ".env" — command_policy's absolute denylist
# matches that substring anywhere in a command string, so such a path would make
# every git call against it undeniably DENY.
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")

_ENV_TOKEN_RE = re.compile(r"\.env", re.IGNORECASE)
_PHYSICAL_GIT_MUTATIONS = frozenset({
    "worktree_add",
    "worktree_remove",
    "worktree_prune",
    "branch_delete",
})
_SUPPORTED_GIT_ACTIONS = frozenset({
    "rev_parse",
    "worktree_add",
    "worktree_remove",
    "worktree_prune",
    "branch_delete",
    "worktree_diff",
    "worktree_diff_bounded",
    "worktree_diff_untracked_bounded",
    "worktree_list",
})


class WorktreeError(RuntimeError):
    """Worktree operation failed. Carries the governance decision when relevant."""

    def __init__(self, message: str, decision: str = "", stderr: str = ""):
        super().__init__(message)
        self.decision = decision
        self.stderr = stderr


class WorktreeDenied(WorktreeError):
    """The command policy refused the git invocation (DENY or KILLSWITCH)."""


class WorktreeEscalation(WorktreeError):
    """The command policy requires HITL approval before this git invocation."""


def _slug(value: str, fallback: str = "task") -> str:
    """A path/refname-safe fragment that can never contain '.env'."""
    cleaned = _SLUG_RE.sub("-", (value or "").strip()).strip("-.")
    cleaned = _ENV_TOKEN_RE.sub("-env", cleaned)
    return (cleaned or fallback)[:48]

# ── The worktree handle ─────────────────────────────────────────────────────


_CAPABILITY_SEAL = object()


class _ProcessOwnershipCapability:
    """Opaque capability issued only by the trusted worktree lifecycle.

    The capability is intentionally not stored on ``Worktree`` or serialized in
    API/event payloads.  Option B currently has no enabled process-creation
    caller, so no public path can obtain one and attachment remains fail-closed.
    """

    __slots__ = ("worktree_id", "tenant_id", "session_id", "execution_id", "_nonce")

    def __init__(
        self,
        worktree_id: str,
        tenant_id: str,
        session_id: str,
        execution_id: str,
        nonce: str,
        *,
        seal: object,
    ) -> None:
        if seal is not _CAPABILITY_SEAL:
            raise TypeError("process ownership capabilities are lifecycle-issued")
        self.worktree_id = worktree_id
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.execution_id = execution_id
        self._nonce = nonce


@dataclass
class Worktree:
    """
    One task's isolated checkout. ``asdict()`` of this is the event payload and
    the ledger payload, so every field here is JSON-safe on purpose.
    """

    worktree_id: str
    session_id: str
    execution_id: str
    task_id: str
    tenant_id: str
    branch: str
    path: str
    base_ref: str
    repo_root: str
    created_at: float = field(default_factory=time.time)
    status: str = "active"            # active | discarded | failed
    discarded_at: Optional[float] = None
    git_calls: int = 0
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Process-local registry. Worktrees are on-disk facts; this only caches the
# handles created by *this* worker so discard/list are cheap. ``list_worktrees``
# falls back to ``git worktree list`` for anything this process did not create,
# which is what makes cross-worker cleanup possible.
_REGISTRY: Dict[str, Worktree] = {}
_REGISTRY_LOCK = threading.Lock()

# Live child processes launched *inside* a worktree, keyed by worktree_id.
# ``git worktree remove --force`` fails on some filesystems while a process still
# holds the directory, so teardown has to finish before the removal is attempted.
# Tracking the handles here keeps that sequencing self-contained rather than
# reaching into the worker's task table.
_PROCS: Dict[str, List[tuple[_ProcessOwnershipCapability, subprocess.Popen]]] = {}
_PROCESS_CAPABILITIES: Dict[str, _ProcessOwnershipCapability] = {}


def integration_status() -> Dict[str, bool]:
    """Which optional integrations resolved at import time."""
    return {
        "command_policy": global_command_policy is not None,
        "tool_governance": get_tool_governance is not None,
        "workspace_lock": workspace_operation_lock is not None,
        "tenant": get_tenant is not None,
        "ledger": get_ledger is not None,
        "events": create_event is not None,
        "event_bus": global_event_bus is not None,
        "telemetry": get_telemetry is not None,
    }


def _event_type(name: str, fallback: str) -> str:
    """An EventTypes constant, degrading to its literal when events are absent."""
    return getattr(EventTypes, name, fallback) if EventTypes is not None else fallback


def _emit(event_type: str, session_id: str, execution_id: str, payload: Dict[str, Any]) -> None:
    """
    Best-effort evidence. ``TruthKernel.append`` raises RuntimeError when the
    store rejects the event, and an audit failure must not turn a successful
    worktree operation into a failed one — so every sink here is guarded
    independently and reports through the log.
    """
    if get_ledger is not None:
        try:
            get_ledger().append(event_type, session_id, execution_id, **payload)
        except Exception as exc:                                 # pragma: no cover
            logger.warning("[worktree] ledger append failed (%s): %s", event_type, exc)

    if global_event_bus is not None:
        try:
            global_event_bus.publish(session_id, event_type, dict(payload))
        except Exception as exc:                                 # pragma: no cover
            logger.warning("[worktree] event publish failed (%s): %s", event_type, exc)


def _telemetry(event: str, payload: Dict[str, Any], session_id: str = "", tenant_id: str = "") -> None:
    if get_telemetry is None:
        return
    try:
        get_telemetry().record("worktree", event, payload, session_id=session_id, tenant_id=tenant_id)
    except Exception as exc:                                     # pragma: no cover
        logger.debug("[worktree] telemetry unavailable: %s", exc)


# ── The governed git call path ──────────────────────────────────────────────


def _require_governance() -> None:
    """
    A missing policy engine is a denial, not a bypass.

    This module degrades to None on every optional import so that it cannot kill
    the gunicorn master under ``--preload``. Degrading *governance* to None,
    though, would leave an ungoverned subprocess path — so the refusal is here,
    at the one place that spawns git, rather than at import.
    """
    if get_tool_governance is None or global_command_policy is None or CommandDecision is None:
        raise WorktreeError(
            "worktree git execution requires both tool governance and the command "
            "policy engine; refusing to run git without them",
            decision="unavailable",
        )


def _validate_git_action(action: str, argv: List[str]) -> None:
    """Reject unknown actions and command-shape mismatches before dispatch.

    ``action`` is the closed governance key, while ``argv`` is still the actual
    process command.  Validating both prevents a future caller from pairing a
    read-only governance label with a mutating Git command.
    """
    if action not in _SUPPORTED_GIT_ACTIONS:
        raise WorktreeError(
            f"unsupported worktree git action: {action!r}",
            decision="unsupported-action",
        )
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        raise WorktreeError(
            f"invalid argv for worktree git action: {action!r}",
            decision="invalid-action-argv",
        )

    exact = {
        "rev_parse": ["git", "rev-parse", "--show-toplevel"],
        "worktree_prune": ["git", "worktree", "prune"],
        "worktree_list": ["git", "worktree", "list", "--porcelain"],
    }
    if action in exact:
        if argv != exact[action]:
            raise WorktreeError(
                f"invalid argv for worktree git action: {action!r}",
                decision="invalid-action-argv",
            )
        return

    if action == "worktree_add":
        valid = (
            len(argv) == 7
            and argv[:4] == ["git", "worktree", "add", "-b"]
            and all(argv[index] and not argv[index].startswith("-") for index in (4, 5, 6))
        )
    elif action == "worktree_remove":
        valid = (
            len(argv) == 5
            and argv[:4] == ["git", "worktree", "remove", "--force"]
            and bool(argv[4])
            and not argv[4].startswith("-")
        )
    elif action == "branch_delete":
        valid = (
            len(argv) == 4
            and argv[:3] == ["git", "branch", "-D"]
            and bool(argv[3])
            and not argv[3].startswith("-")
        )
    elif action == "worktree_diff":
        valid = (
            len(argv) == 5
            and argv[:3] == ["git", "diff", "--no-color"]
            and argv[3].startswith("--unified=")
            and argv[3][len("--unified="):].isdigit()
            and bool(argv[4])
            and not argv[4].startswith("-")
        )
    elif action == "worktree_diff_bounded":
        exclusions = list(diff_pathspec_exclusions())
        valid = (
            len(argv) == 10 + len(exclusions)
            and argv[:6] == [
                "git",
                "--no-optional-locks",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
            ]
            and argv[6].startswith("--unified=")
            and argv[6][len("--unified="):].isdigit()
            and bool(argv[7])
            and not argv[7].startswith("-")
            and argv[8:10] == ["--", "."]
            and argv[10:] == exclusions
        )
    elif action == "worktree_diff_untracked_bounded":
        expected = build_untracked_listing_argv()
        valid = argv == expected
    else:  # pragma: no cover - the closed set above is exhaustive
        valid = False

    if not valid:
        raise WorktreeError(
            f"invalid argv for worktree git action: {action!r}",
            decision="invalid-action-argv",
        )


def _git(
    action: str,
    argv: List[str],
    cwd: str,
    session_id: str,
    workspace_id: str = "",
    check: bool = True,
) -> subprocess.CompletedProcess:
    """
    Run one git command through the full policy stack.

    ``action`` is what gets governed, not ``argv``. ``ToolGovernance._command_for``
    maps the action to a path-free surrogate ("worktree_add" -> "git worktree
    add") precisely so that a caller-supplied path or branch name never reaches
    the engine's substring denylist — a task titled "dig into the planner" would
    otherwise slug into a path matching ``\\bdig\\b`` and make every git call
    against that worktree an undeniable DENY.

    ``allow_escalation=True`` is not decoration. With it False,
    ``CommandPolicyEngine.evaluate`` skips its ESCALATE branch and a command at
    or above the suspicious threshold falls through to ALLOW. Worktree ops run
    with no human in the loop, so escalation-worthy has to mean refused here.
    """
    _validate_git_action(action, argv)
    if action == "worktree_diff":
        raise WorktreeError(
            "worktree diff requires the bounded governed diff path",
            decision="bounded-diff-required",
        )
    if action in _PHYSICAL_GIT_MUTATIONS:
        assert_agent_execution_enabled()
    _require_governance()

    decision = get_tool_governance().before_dispatch(
        "git",
        {"action": action},
        session_id=session_id,
        workspace_id=workspace_id,
        allow_escalation=True,
    )

    if not decision.allowed:
        policy = (decision.policy or "").lower()
        _telemetry("git_denied", {"action": action, "policy": decision.policy,
                                  "reason": decision.reason}, session_id=session_id)
        if "escalate" in policy:
            raise WorktreeEscalation(
                f"git {action} requires approval: {decision.reason}", decision=decision.policy
            )
        raise WorktreeDenied(
            f"git {action} refused by policy: {decision.reason}", decision=decision.policy
        )

    # ``build_safe_subprocess_kwargs`` strips secret-bearing environment
    # variables and pins shell=False, but it reaches its argv by shlex.split-ing
    # the string — so anything with a space in it has to survive the round trip.
    command = shlex.join(argv)
    kwargs = global_command_policy.build_safe_subprocess_kwargs(
        command, cwd, timeout_sec=_GIT_TIMEOUT_SEC
    )
    if kwargs.get("args") != argv:
        # Belt and braces: the quoting above should make this unreachable, but a
        # silent retokenization would change which paths git touches.
        logger.warning("[worktree] argv round-trip differed for %s; using the original", action)
        kwargs["args"] = list(argv)

    started = time.time()
    try:
        completed = subprocess.run(**kwargs)
    except subprocess.TimeoutExpired as exc:
        _telemetry("git_timeout", {"action": action, "timeout_sec": _GIT_TIMEOUT_SEC},
                   session_id=session_id)
        raise WorktreeError(f"git {action} timed out after {_GIT_TIMEOUT_SEC}s") from exc
    except OSError as exc:
        raise WorktreeError(f"git {action} could not be launched: {exc}") from exc

    elapsed = time.time() - started

    try:
        get_tool_governance().after_dispatch(decision, {"ok": completed.returncode == 0})
    except Exception as exc:                                     # pragma: no cover
        logger.debug("[worktree] after_dispatch failed: %s", exc)

    _telemetry(
        "git_call",
        {"action": action, "rc": completed.returncode, "elapsed_sec": round(elapsed, 3)},
        session_id=session_id,
    )

    if check and completed.returncode != 0:
        raise WorktreeError(
            f"git {action} failed (rc={completed.returncode})",
            decision=decision.policy,
            stderr=(completed.stderr or "")[:2000],
        )
    return completed


def _governed_bounded_readonly_git(
    action: str,
    argv: List[str],
    cwd: Path,
    session_id: str,
    workspace_id: str,
) -> str:
    """Authorize an exact read-only diff argv before bounded capture.

    The command-policy/governance stack remains the admission boundary.  The
    shared diff runner owns stdout/stderr pipes and timeout enforcement only
    after that admission succeeds; it is not an ungoverned replacement for
    ``_git``.
    """
    _validate_git_action(action, argv)
    _require_governance()
    decision = get_tool_governance().before_dispatch(
        "git",
        {"action": action},
        session_id=session_id,
        workspace_id=workspace_id,
        allow_escalation=True,
    )
    if not decision.allowed:
        policy = (decision.policy or "").lower()
        _telemetry("git_denied", {"action": action, "policy": decision.policy,
                                  "reason": decision.reason}, session_id=session_id)
        if "escalate" in policy:
            raise WorktreeEscalation(
                f"git {action} requires approval: {decision.reason}", decision=decision.policy
            )
        raise WorktreeDenied(
            f"git {action} refused by policy: {decision.reason}", decision=decision.policy
        )

    command = shlex.join(argv)
    safe_kwargs = global_command_policy.build_safe_subprocess_kwargs(
        command, str(cwd), timeout_sec=DIFF_MAX_GIT_TIMEOUT_SEC
    )
    if safe_kwargs.get("args") != argv:
        raise WorktreeError(
            f"governance changed the permitted argv for {action!r}",
            decision="governance-argv-mismatch",
        )

    def after_dispatch(return_code: int) -> None:
        try:
            get_tool_governance().after_dispatch(
                decision,
                {"ok": return_code == 0},
            )
        except Exception as exc:  # pragma: no cover - optional integration
            logger.debug("[worktree] bounded after_dispatch failed: %s", exc)

    try:
        return run_bounded_readonly_git(
            argv,
            cwd,
            popen_kwargs=safe_kwargs,
            on_complete=after_dispatch,
        )
    except DiffExtractionError as exc:
        raise WorktreeError(
            f"bounded git {action} failed",
            decision="diff-extraction-failed",
        ) from exc


# ── Path confinement ────────────────────────────────────────────────────────


def _confined(ctx: Any, relative: str) -> str:
    """
    Resolve a path inside the tenant workspace and re-check the bound.

    ``TenantContext.workspace_path`` already resolves through realpath, unlike
    ``sandbox.validate_path_access`` — but it bounds the result with a bare
    ``startswith``, which admits a sibling whose name merely extends the root.
    Tenant directories are 16 hex characters so a natural collision is
    impossible, yet a symlink resolving to ``<root>-evil`` would still pass. The
    separator check below is what closes that.
    """
    try:
        target = ctx.workspace_path(relative)
        root = os.path.realpath(ctx.ensure_workspace())
    except PermissionError as exc:
        # workspace_path raises PermissionError on its own traversal check. Keep
        # the refusal, but present it as this module's error type so a caller
        # cannot mistake it for an auth failure elsewhere in the stack.
        raise WorktreeError(f"path refused by tenant confinement: {relative!r}: {exc}") from exc
    if target != root and not target.startswith(root + os.sep):
        raise WorktreeError(f"path escapes the tenant workspace: {relative!r}")
    return target


def _repo_root(start: str, session_id: str) -> str:
    """The enclosing git repository, resolved by git rather than assumed."""
    completed = _git("rev_parse", ["git", "rev-parse", "--show-toplevel"], start, session_id)
    root = (completed.stdout or "").strip()
    if not root:
        raise WorktreeError(f"{start!r} is not inside a git repository")
    return os.path.realpath(root)


# ── Lifecycle ───────────────────────────────────────────────────────────────


def _lock(session_id: str, operation: str):
    """
    Serialise the ``.git/worktrees`` metadata mutation.

    ``workspace_operation_lock`` takes a single process-wide sentinel, not a
    per-session key, so holding it serialises *every* workspace operation in this
    worker. It therefore wraps only ``git worktree add``/``remove`` — where
    concurrent writers genuinely corrupt the metadata directory — and never a
    diff read, which can run for as long as the repository is large.
    """
    if workspace_operation_lock is None:
        from contextlib import nullcontext
        return nullcontext()
    return workspace_operation_lock(session_id, operation, timeout=_LOCK_TIMEOUT_SEC)


def create_worktree(
    session_id: str,
    execution_id: str,
    task_id: str,
    tenant_id: str = "default",
    base_ref: str = "HEAD",
    repo_root: Optional[str] = None,
) -> Worktree:
    """
    Check out one isolated worktree on its own branch for one task.

    The tenant context is resolved exactly once here. ``get_tenant`` calls
    ``register_session`` on every invocation, which increments ``active_sessions``
    and raises PermissionError if this session is already bound to a different
    tenant — so calling it per git call would inflate the quota and turn a
    routine diff into an authorisation failure.
    """
    assert_agent_execution_enabled()
    _require_governance()

    if get_tenant is None:
        raise WorktreeError("tenant context unavailable; cannot root a worktree")

    with _REGISTRY_LOCK:
        active = [w for w in _REGISTRY.values()
                  if w.session_id == session_id and w.status == "active"]
    if len(active) >= _MAX_PER_SESSION:
        raise WorktreeError(
            f"session {session_id!r} already holds {len(active)} worktrees "
            f"(limit {_MAX_PER_SESSION}); discard one first"
        )

    ctx = get_tenant(session_id, tenant_id)

    worktree_id = uuid.uuid4().hex[:12]
    slug = _slug(task_id or execution_id)
    branch = f"{_BRANCH_PREFIX}/{slug}-{worktree_id[:8]}"

    parent = _confined(ctx, _WORKTREE_SUBDIR)
    os.makedirs(parent, exist_ok=True)
    path = _confined(ctx, os.path.join(_WORKTREE_SUBDIR, f"{slug}-{worktree_id[:8]}"))

    root = os.path.realpath(repo_root) if repo_root else _repo_root(os.getcwd(), session_id)

    try:
        with _lock(session_id, f"worktree_add:{worktree_id}"):
            _git(
                "worktree_add",
                ["git", "worktree", "add", "-b", branch, path, base_ref],
                root,
                session_id,
                workspace_id=worktree_id,
            )
    except LockTimeoutError as exc:
        _telemetry("lock_timeout", {"operation": "worktree_add"}, session_id=session_id)
        raise WorktreeError(f"worktree creation blocked: {exc}") from exc

    wt = Worktree(
        worktree_id=worktree_id,
        session_id=session_id,
        execution_id=execution_id,
        task_id=task_id,
        tenant_id=tenant_id,
        branch=branch,
        path=path,
        base_ref=base_ref,
        repo_root=root,
        git_calls=1,
    )
    with _REGISTRY_LOCK:
        if worktree_id in _REGISTRY:
            raise WorktreeError("worktree identifier collision; refusing registration")
        _REGISTRY[worktree_id] = wt
        _PROCESS_CAPABILITIES[worktree_id] = _ProcessOwnershipCapability(
            worktree_id,
            tenant_id,
            session_id,
            execution_id,
            uuid.uuid4().hex,
            seal=_CAPABILITY_SEAL,
        )

    _emit(_event_type("WORKSPACE_BOUND", "workspace.bound"), session_id, execution_id,
          {"worktree": wt.to_dict(), "operation": "create"})
    _telemetry("created", {"worktree_id": worktree_id, "branch": branch},
               session_id=session_id, tenant_id=tenant_id)
    logger.info("[worktree] created %s on %s at %s", worktree_id, branch, path)
    return wt


def get_worktree(worktree_id: str) -> Optional[Worktree]:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(worktree_id)


def get_authorized_worktree(
    worktree_id: str,
    tenant_id: str,
    *,
    session_id: str = "",
    execution_id: str = "",
    allow_discarded: bool = False,
) -> Worktree:
    """Return an active worktree only when trusted ownership fields match."""
    with _REGISTRY_LOCK:
        wt = _REGISTRY.get(worktree_id)
        if wt is None:
            raise WorktreeError("unknown or unauthorized worktree", decision="worktree-not-found")
        if str(wt.tenant_id) != str(tenant_id):
            raise WorktreeError("worktree ownership mismatch: tenant-mismatch", decision="worktree-tenant-mismatch")
        if session_id and str(wt.session_id) != str(session_id):
            raise WorktreeError("worktree ownership mismatch: session-mismatch", decision="worktree-session-mismatch")
        if execution_id and str(wt.execution_id) != str(execution_id):
            raise WorktreeError("worktree ownership mismatch: execution-mismatch", decision="worktree-execution-mismatch")
        if not allow_discarded and wt.status != "active":
            raise WorktreeError("worktree is not active", decision="worktree-retired")
        return wt


def attach_process(
    worktree_id: str,
    proc: subprocess.Popen,
    *,
    tenant_id: str,
    session_id: str,
    execution_id: str,
    capability: Optional[_ProcessOwnershipCapability] = None,
    owner_token: Optional[str] = None,
) -> None:
    """
    Register a child running inside this worktree so discard can tear it down.

    Callers that spawn work in a worktree must use this: ``git worktree remove``
    fails while the directory is held open, and an untracked child would survive
    the discard and keep writing into a path the registry believes is gone.
    """
    assert_agent_execution_enabled()
    if owner_token is not None or not isinstance(capability, _ProcessOwnershipCapability):
        raise WorktreeError(
            "process attachment requires a lifecycle-issued ownership capability",
            decision="missing-process-capability",
        )
    wt = get_authorized_worktree(
        worktree_id,
        tenant_id,
        session_id=session_id,
        execution_id=execution_id,
    )
    with _REGISTRY_LOCK:
        expected_capability = _PROCESS_CAPABILITIES.get(worktree_id)
        if capability is not expected_capability:
            raise WorktreeError(
                "worktree ownership mismatch: capability-mismatch",
                decision="capability-mismatch",
            )
        if (
            capability.worktree_id != worktree_id
            or capability.tenant_id != tenant_id
            or capability.session_id != session_id
            or capability.execution_id != execution_id
        ):
            raise WorktreeError(
                "worktree ownership mismatch: capability-identity-mismatch",
                decision="capability-identity-mismatch",
            )
    if not isinstance(proc, subprocess.Popen):
        raise WorktreeError(
            "process attachment requires a subprocess created by the trusted runtime",
            decision="untrusted-process-handle",
        )
    with _REGISTRY_LOCK:
        if wt.status != "active":
            raise WorktreeError("worktree is not active", decision="worktree-retired")
        entries = _PROCS.setdefault(worktree_id, [])
        if any(existing_capability is capability and existing_proc is proc for existing_capability, existing_proc in entries):
            raise WorktreeError("process is already attached to this worktree", decision="duplicate-process")
        entries.append((capability, proc))


def _terminate_children(worktree_id: str, grace_sec: float = 5.0) -> int:
    """Terminate only children registered against an owned worktree.

    This is an intentional Option B safety exception, not an execution path:
    termination can only target process handles registered through
    ``attach_process`` for a worktree present in the process-local registry. It
    never starts, dispatches, or continues a worker. Unknown/unowned IDs are a
    no-op, so a direct caller cannot use this helper as a general process-kill
    primitive. The physical worktree lifecycle callers remain separately
    guarded by ``assert_agent_execution_enabled`` before reaching this helper.
    """
    if not isinstance(grace_sec, (int, float)) or not math.isfinite(float(grace_sec)) or not 0 <= float(grace_sec) <= 300:
        raise WorktreeError("invalid process cleanup grace period", decision="invalid-cleanup-timeout")
    with _REGISTRY_LOCK:
        wt = _REGISTRY.get(worktree_id)
        if wt is None or wt.status not in {"active", "retiring"}:
            return 0
        if wt.status == "active":
            # Retirement and attachment serialize on the same registry lock.
            # Once this state changes, no new process can enter the list.
            wt.status = "retiring"
        registered = list(_PROCS.get(worktree_id, []))

    reaped = 0
    for capability, proc in registered:
        succeeded = False
        try:
            if proc.poll() is not None:
                succeeded = True
                reaped += 1
                continue
            proc.terminate()
            try:
                proc.wait(timeout=grace_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=grace_sec)
            succeeded = True
            reaped += 1
        except Exception as exc:                                 # pragma: no cover
            logger.warning("[worktree] could not reap child of %s: %s", worktree_id, exc)
        finally:
            if succeeded:
                with _REGISTRY_LOCK:
                    current = _PROCS.get(worktree_id, [])
                    _PROCS[worktree_id] = [
                        entry for entry in current
                        if not (entry[0] is capability and entry[1] is proc)
                    ]
                    if not _PROCS[worktree_id]:
                        _PROCS.pop(worktree_id, None)
    return reaped


def discard_worktree(worktree_id: str, delete_branch: bool = True) -> Dict[str, Any]:
    """
    Tear down a worktree: children first, then the checkout, then the branch.

    The ordering is not stylistic. ``git worktree remove --force`` against a
    directory a live child still holds fails outright on some filesystems, so the
    SIGTERM/SIGKILL pass has to complete before removal is attempted.
    """
    assert_agent_execution_enabled()
    wt = get_worktree(worktree_id)
    if wt is None:
        return {"ok": False, "error": f"unknown worktree {worktree_id!r}"}
    if wt.status == "discarded":
        return {"ok": True, "worktree_id": worktree_id, "already_discarded": True}

    reaped = _terminate_children(worktree_id)
    with _REGISTRY_LOCK:
        remaining_children = len(_PROCS.get(worktree_id, []))
    if remaining_children:
        logger.warning(
            "[worktree] refusing removal of %s while %d owned child handle(s) remain",
            worktree_id,
            remaining_children,
        )
        return {
            "ok": False,
            "worktree_id": worktree_id,
            "children_reaped": reaped,
            "error": "owned child cleanup incomplete; retry required",
        }
    removed, branch_deleted, errors = False, False, []

    try:
        with _lock(wt.session_id, f"worktree_remove:{worktree_id}"):
            try:
                _git("worktree_remove",
                     ["git", "worktree", "remove", "--force", wt.path],
                     wt.repo_root, wt.session_id, workspace_id=worktree_id)
                removed = True
            except WorktreeError as exc:
                # The checkout may already be gone, or its metadata stale. Clear
                # the directory and let prune reconcile rather than leaving an
                # entry git will trip over on the next add.
                errors.append(str(exc))
                shutil.rmtree(wt.path, ignore_errors=True)
                try:
                    _git("worktree_prune", ["git", "worktree", "prune"],
                         wt.repo_root, wt.session_id, workspace_id=worktree_id)
                    removed = not os.path.exists(wt.path)
                except WorktreeError as prune_exc:
                    errors.append(str(prune_exc))

        if delete_branch and removed:
            try:
                # check=False: a branch that is already gone is not a failure of
                # the discard, so the return code decides the claim rather than
                # an exception. Claiming deletion on a nonzero rc would be a lie.
                completed = _git("branch_delete", ["git", "branch", "-D", wt.branch],
                                 wt.repo_root, wt.session_id,
                                 workspace_id=worktree_id, check=False)
                branch_deleted = completed.returncode == 0
                if not branch_deleted:
                    errors.append((completed.stderr or "branch delete failed").strip()[:500])
            except WorktreeError as exc:
                errors.append(str(exc))
    except LockTimeoutError as exc:
        _telemetry("lock_timeout", {"operation": "worktree_remove"}, session_id=wt.session_id)
        return {"ok": False, "worktree_id": worktree_id, "error": str(exc)}

    wt.status = "discarded" if removed else "failed"
    wt.discarded_at = time.time()

    payload = {
        "worktree": wt.to_dict(), "operation": "discard",
        "children_reaped": reaped, "branch_deleted": branch_deleted, "errors": errors,
    }
    _emit(_event_type("WORKSPACE_BOUND", "workspace.bound"),
          wt.session_id, wt.execution_id, payload)
    _telemetry("discarded", {"worktree_id": worktree_id, "removed": removed},
               session_id=wt.session_id, tenant_id=wt.tenant_id)
    logger.info("[worktree] discarded %s (removed=%s, children=%d)", worktree_id, removed, reaped)

    return {"ok": removed, "worktree_id": worktree_id, "children_reaped": reaped,
            "branch_deleted": branch_deleted, "errors": errors}


# ── Inspection ──────────────────────────────────────────────────────────────


def worktree_diff(
    worktree_id: str,
    context_lines: int = 3,
    *,
    tenant_id: str = "",
    session_id: str = "",
    execution_id: str = "",
) -> str:
    """
    The task's work as a unified diff, against the ref the worktree branched from.

    Deliberately outside ``_lock``: the workspace lock is process-global, and a
    diff over a large repository would stall every other worktree operation in
    this worker for its whole duration.
    """
    if not tenant_id or not session_id or not execution_id:
        raise WorktreeError(
            "worktree diff requires tenant, session, and execution authorization",
            decision="missing-worktree-authorization",
        )
    wt = get_authorized_worktree(
        worktree_id,
        tenant_id,
        session_id=session_id,
        execution_id=execution_id,
    )
    try:
        _root, candidate = resolve_authorized_worktree_path(
            wt.path,
            wt.repo_root,
            require_isolated=True,
        )
        tracked_argv = build_tracked_diff_argv(wt.base_ref, context_lines=context_lines)
        # The shared extractor builds both exact argv values.  Validate the
        # tracked shape here before any governance or subprocess call so a
        # caller cannot smuggle an alternate cwd or pathspec into the runner.
        _validate_git_action("worktree_diff_bounded", tracked_argv)

        def run_governed(argv: List[str], cwd: Path) -> str:
            action = (
                "worktree_diff_bounded"
                if argv == tracked_argv
                else "worktree_diff_untracked_bounded"
            )
            return _governed_bounded_readonly_git(
                action,
                list(argv),
                cwd,
                wt.session_id,
                worktree_id,
            )

        diff_text = extract_worktree_diff(
            candidate,
            wt.base_ref,
            run_governed,
            context_lines=context_lines,
        )
    except DiffExtractionError as exc:
        raise WorktreeError("authorized worktree diff is unavailable", decision="diff-unavailable") from exc
    wt.git_calls += 2
    _emit(_event_type("CMD_RUN", "command.run"), wt.session_id, wt.execution_id,
          {"worktree_id": worktree_id, "action": "worktree_diff",
           "diff_bytes": len(diff_text.encode("utf-8", errors="replace"))})
    return diff_text


def list_worktrees(session_id: str = "") -> List[Dict[str, Any]]:
    """Handles this process created, optionally narrowed to one session."""
    with _REGISTRY_LOCK:
        handles = list(_REGISTRY.values())
    return [w.to_dict() for w in handles if not session_id or w.session_id == session_id]


def list_git_worktrees(repo_root: str, session_id: str = "") -> List[Dict[str, str]]:
    """
    What git itself believes exists, including worktrees another worker created.

    ``_REGISTRY`` is process-local, so it cannot see a peer's checkouts. This is
    the read that makes cross-worker cleanup possible.
    """
    completed = _git("worktree_list", ["git", "worktree", "list", "--porcelain"],
                     repo_root, session_id)
    entries: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


def sweep_session(session_id: str, delete_branches: bool = True) -> Dict[str, Any]:
    """Discard every active worktree a session still holds. Safe to call twice."""
    assert_agent_execution_enabled()
    with _REGISTRY_LOCK:
        targets = [w.worktree_id for w in _REGISTRY.values()
                   if w.session_id == session_id and w.status == "active"]

    results = [discard_worktree(wid, delete_branch=delete_branches) for wid in targets]
    failed = [r for r in results if not r.get("ok")]
    return {"ok": not failed, "session_id": session_id,
            "discarded": len(results) - len(failed), "failed": len(failed),
            "results": results}


# ── Orphan reclamation (order 4.3) ──────────────────────────────────────────

# A worktree older than this with no live owner is considered abandoned. The
# default is deliberately long: reclaiming a checkout a task is still using is
# destructive, whereas leaving one an extra hour costs disk.
_ORPHAN_AGE_SEC = float(os.getenv("WORKTREE_ORPHAN_AGE_SEC", "21600"))  # 6h


def sweep_orphans(
    max_age_sec: Optional[float] = None,
    repo_root: Optional[str] = None,
    session_id: str = "worktree-sweeper",
) -> Dict[str, Any]:
    """
    Reclaim worktrees whose owning task is gone. Idempotent; safe to run on a timer.

    Two distinct kinds of orphan exist and they need different treatment:

      *In-registry* — a handle this process created that is still ``active`` past
      ``max_age_sec``. The owning task either crashed without reaching its
      ``finally:`` or was lost to a worker restart. Reclaimed through
      ``discard_worktree`` so child teardown, branch deletion, events and
      telemetry all follow the normal path.

      *On-disk only* — a checkout ``git worktree list`` reports that this process
      has no handle for. Usually a peer worker's live task, since ``_REGISTRY`` is
      process-local. These are **counted and reported, never removed**: this
      process cannot distinguish a peer's healthy worktree from an abandoned one,
      and guessing would delete work in progress. ``git worktree prune`` is run
      instead, which only clears metadata for directories that no longer exist.

    Returns a report rather than raising. A sweep that cannot reach git is a
    degraded sweep, not a failed task — it runs again on the next tick.
    """
    assert_agent_execution_enabled()
    cutoff = time.time() - (max_age_sec if max_age_sec is not None else _ORPHAN_AGE_SEC)

    with _REGISTRY_LOCK:
        stale = [w.worktree_id for w in _REGISTRY.values()
                 if w.status == "active" and w.created_at < cutoff]
        known_paths = {os.path.realpath(w.path) for w in _REGISTRY.values()}
        roots = {w.repo_root for w in _REGISTRY.values() if w.repo_root}

    results = [discard_worktree(wid, delete_branch=True) for wid in stale]
    failed = [r for r in results if not r.get("ok")]

    # Reconcile against git's own view. Without an explicit root and with an empty
    # registry there is nothing to resolve a repository from, and inferring one
    # from cwd inside a scheduler thread is how a sweep ends up pointed at the
    # wrong tree — so that case reports "unreconciled" instead of guessing.
    unknown_on_disk: List[str] = []
    pruned = False
    errors: List[str] = []
    targets = [os.path.realpath(repo_root)] if repo_root else sorted(roots)

    for root in targets:
        try:
            for entry in list_git_worktrees(root, session_id):
                path = entry.get("worktree", "")
                # `bare` appears as a valueless porcelain line, so presence of the
                # key is the whole signal. The main checkout is not a candidate.
                if not path or "bare" in entry:
                    continue
                real = os.path.realpath(path)
                if real == root or real in known_paths:
                    continue
                if _WORKTREE_SUBDIR and _WORKTREE_SUBDIR in real.split(os.sep):
                    unknown_on_disk.append(real)
            _git("worktree_prune", ["git", "worktree", "prune"], root, session_id)
            pruned = True
        except Exception as exc:                                     # pragma: no cover
            errors.append(f"{root}: {exc}")

    report = {
        "ok": not failed and not errors,
        "checked": len(stale) + len(unknown_on_disk),
        "discarded": len(results) - len(failed),
        "failed": len(failed),
        "unknown_on_disk": len(unknown_on_disk),
        "unknown_paths": unknown_on_disk[:20],
        "pruned": pruned,
        "reconciled": bool(targets),
        "cutoff_age_sec": max_age_sec if max_age_sec is not None else _ORPHAN_AGE_SEC,
        "errors": errors,
        "results": results,
    }
    if stale or unknown_on_disk or errors:
        logger.info("[worktree] orphan sweep: discarded=%d failed=%d foreign=%d errors=%d",
                    report["discarded"], report["failed"],
                    report["unknown_on_disk"], len(errors))
    _telemetry("orphan_sweep", {k: report[k] for k in
                                ("discarded", "failed", "unknown_on_disk", "pruned")})
    return report


def register_orphan_sweep(scheduler: Any, interval_minutes: int = 60) -> Dict[str, Any]:
    """
    Register the sweep with an existing ``TaskScheduler``. No new daemon.

    The scheduler persists tasks in SQLite and survives restarts, so this is
    idempotent by name: a second call adopts the existing row rather than adding
    a duplicate that would sweep twice per interval.
    """
    name = "worktree-orphan-sweep"
    try:
        for task in scheduler.list_tasks():
            if getattr(task, "name", "") == name:
                return {"ok": True, "task_id": task.id, "created": False}

        task = scheduler.create_task(
            name=name,
            task_type="worktree_sweep",
            prompt="",
            schedule_type="interval",
            schedule_value=str(int(interval_minutes)),
            enabled=True,
            config={"handler": "execution.worktree.sweep_orphans"},
        )
        logger.info("[worktree] orphan sweep registered with scheduler (every %dm)",
                    interval_minutes)
        return {"ok": True, "task_id": task.id, "created": True}
    except Exception as exc:
        # Registration failure must not propagate: the caller is app startup.
        logger.warning("[worktree] could not register orphan sweep: %s", exc)
        return {"ok": False, "error": str(exc)}
