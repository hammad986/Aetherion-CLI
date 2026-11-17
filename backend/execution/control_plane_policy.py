"""
Control-plane execution policy for the approved first release.

This module is intentionally fail-closed. The current release scope is a
control plane: autonomous agent, command, plugin, and worker execution are
disabled. Environment variables may describe the intended scope, but they
cannot enable execution. Enabling execution requires a separately reviewed
source change after sandbox and provider evidence exists.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional


CONTROL_PLANE_SCOPE = "control-plane"
EXECUTION_DISABLED_ERROR_CODE = "EXECUTION_DISABLED"
EXECUTION_DISABLED_MESSAGE = (
    "Agent execution is disabled for the approved control-plane release."
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_CONTROL_PLANE_VALUES = frozenset({"control-plane", "control_plane", "controlplane"})


class ExecutionDisabledError(RuntimeError):
    """Raised whenever an execution-capable path is invoked in Option B."""

    code = EXECUTION_DISABLED_ERROR_CODE

    def __init__(self) -> None:
        super().__init__(EXECUTION_DISABLED_MESSAGE)


@dataclass(frozen=True)
class ControlPlanePolicy:
    """Safe, non-secret description of the effective release policy."""

    scope: str
    execution_enabled: bool
    configuration_valid: bool
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "execution_enabled": self.execution_enabled,
            "configuration_valid": self.configuration_valid,
            "execution_disabled": not self.execution_enabled,
        }


def get_control_plane_policy(environ: Optional[Mapping[str, str]] = None) -> ControlPlanePolicy:
    """Resolve the release policy without ever allowing configuration to enable execution.

    ``AETHERION_RELEASE_SCOPE`` and ``AETHERION_EXECUTION_DISABLED`` are
    descriptive/validation inputs only. The Option B source lock always
    returns ``execution_enabled=False``. Missing or contradictory values are
    deliberately reported as invalid while remaining disabled.
    """
    env = environ if environ is not None else os.environ
    raw_scope = str(env.get("AETHERION_RELEASE_SCOPE", "")).strip().lower()
    raw_disabled = str(env.get("AETHERION_EXECUTION_DISABLED", "")).strip().lower()

    scope_ok = raw_scope in _CONTROL_PLANE_VALUES
    disabled_ok = raw_disabled in _TRUE_VALUES
    disabled_value_ok = not raw_disabled or raw_disabled in _TRUE_VALUES or raw_disabled in _FALSE_VALUES

    if not raw_scope or not raw_disabled:
        reason = "missing execution-policy configuration; fail-closed"
        configuration_valid = False
    elif not scope_ok:
        reason = "unsupported release scope; fail-closed"
        configuration_valid = False
    elif not disabled_value_ok or not disabled_ok:
        reason = "execution-disabled policy is not explicitly enabled; fail-closed"
        configuration_valid = False
    else:
        reason = "control-plane execution lock is active"
        configuration_valid = True

    # This is the Option B source lock. No environment value can turn it on.
    return ControlPlanePolicy(
        scope=CONTROL_PLANE_SCOPE,
        execution_enabled=False,
        configuration_valid=configuration_valid,
        reason=reason,
    )


def is_agent_execution_disabled() -> bool:
    """Return whether autonomous/command/plugin execution is unavailable."""
    return not get_control_plane_policy().execution_enabled


def assert_agent_execution_enabled() -> None:
    """Fail closed at an execution boundary with a stable, non-sensitive error."""
    if is_agent_execution_disabled():
        raise ExecutionDisabledError()


def execution_disabled_payload() -> Dict[str, Any]:
    """Return the stable public error body for an unsupported execution request."""
    return {
        "ok": False,
        "error_code": EXECUTION_DISABLED_ERROR_CODE,
        "error": EXECUTION_DISABLED_MESSAGE,
        "execution": {
            "enabled": False,
            "scope": CONTROL_PLANE_SCOPE,
        },
    }


def blocked_execution_result() -> Dict[str, Any]:
    """Return the minimal durable result recorded for a blocked task."""
    return {
        "error_code": EXECUTION_DISABLED_ERROR_CODE,
        "error": EXECUTION_DISABLED_MESSAGE,
        "execution": {
            "enabled": False,
            "scope": CONTROL_PLANE_SCOPE,
        },
    }


def block_durable_execution(execution_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    """Terminally block a durable task without changing its schema.

    Existing task metadata and history remain queryable. The transition to
    ``CANCELLED`` prevents a later worker or retry path from treating the task
    as executable.
    """
    from . import durable_state

    record = durable_state.get(execution_id, tenant_id)
    if not record:
        return None
    if record.get("status") in durable_state.TERMINAL_STATUSES:
        return record

    try:
        return durable_state.transition(
            execution_id,
            tenant_id,
            durable_state.STATUS_CANCELLED,
            result=blocked_execution_result(),
        )
    except durable_state.TransitionError:
        # A concurrent operator/worker transition must not make an execution
        # request look successful. Return the authoritative current record.
        return durable_state.get(execution_id, tenant_id)


def is_control_plane_maintenance_job(job: Any) -> bool:
    """Identify existing administrative memory jobs that remain supported."""
    metadata = getattr(job, "metadata", None) or {}
    name = str(getattr(job, "name", "") or "")
    task = str(getattr(job, "task", "") or "")
    allowed_tasks = {
        "embedding_backfill",
        "stale_embedding_repair",
        "duplicate_detection",
        "index_rebuild",
        "contradiction_scan",
        "consolidation_scan",
        "cache_prune_expired",
        "memory_lifecycle_cleanup",
        "staleness_prune",
    }
    return (
        task in allowed_tasks
        and (
            metadata.get("type") == "maintenance"
            or name.startswith("memory_maintenance:")
        )
    )
