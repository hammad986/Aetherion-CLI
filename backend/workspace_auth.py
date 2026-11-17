"""Authoritative workspace membership and role checks.

This module deliberately keeps user identity separate from workspace identity.
The runtime authentication module owns the database connection; this adapter
only uses its existing ``execute_query`` boundary and never trusts a JWT role
claim or a caller-supplied owner/publisher field.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Optional


WORKSPACE_ID_RE = re.compile(r"^ws_[0-9a-f]{32}$")
WORKSPACE_ROLES = frozenset({"owner", "admin", "operator", "member", "viewer"})
ACTIVE_MEMBER = "active"


class WorkspaceAuthorizationError(Exception):
    """Structured authorization failure with an HTTP-compatible code."""

    def __init__(self, message: str, *, status_code: int = 403, code: str = "FORBIDDEN"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_id: str
    user_id: str
    role: str
    global_role: str


def _auth_module():
    # Import lazily to avoid the auth_system -> workspace_auth init cycle.
    from auth_system import execute_query

    return execute_query


def _db_row_value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    if hasattr(row, "keys"):
        try:
            return row[key]
        except (KeyError, IndexError):
            pass
    return row[index]


def _canonical_user_id(user_id: Any) -> str:
    if isinstance(user_id, bool) or user_id is None:
        raise WorkspaceAuthorizationError("Authenticated user is invalid", status_code=401, code="UNAUTHORIZED")
    value = str(user_id)
    if not value or (value.isdigit() and value.startswith("0") and value != "0"):
        raise WorkspaceAuthorizationError("Authenticated user is invalid", status_code=401, code="UNAUTHORIZED")
    return value


def _canonical_workspace_id(workspace_id: Any) -> str:
    # Do not coerce integers, empty strings, or path-like aliases into a
    # workspace identifier.  Opaque IDs are persisted as exact strings.
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_RE.fullmatch(workspace_id):
        raise WorkspaceAuthorizationError("Workspace identifier is invalid", code="INVALID_WORKSPACE")
    return workspace_id


def ensure_workspace_schema() -> None:
    """Create the additive membership schema and backfill personal scopes.

    The function is idempotent and uses the application's existing auth store,
    so production deployments and local fixtures share the same authority.
    """
    execute_query = _auth_module()
    execute_query(
        """
        CREATE TABLE IF NOT EXISTS workspace_accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """,
        commit=True,
    )
    execute_query(
        """
        CREATE TABLE IF NOT EXISTS workspace_members (
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            removed_at REAL,
            PRIMARY KEY (workspace_id, user_id)
        )
        """,
        commit=True,
    )
    execute_query(
        "CREATE INDEX IF NOT EXISTS idx_workspace_members_user ON workspace_members(user_id, status)",
        commit=True,
    )
    execute_query(
        "CREATE INDEX IF NOT EXISTS idx_workspace_members_workspace ON workspace_members(workspace_id, status, role)",
        commit=True,
    )

    # Existing installations were user-scoped.  Give each existing account an
    # opaque personal workspace without making the user ID itself the tenant.
    users = execute_query("SELECT id FROM users", fetchall=True) or []
    now = time.time()
    for row in users:
        user_id = str(_db_row_value(row, "id"))
        existing = execute_query(
            "SELECT workspace_id FROM workspace_members WHERE user_id = ? AND role = 'owner' AND status = 'active' LIMIT 1",
            (user_id,),
            fetchone=True,
        )
        if existing:
            continue
        workspace_id = f"ws_{uuid.uuid4().hex}"
        execute_query(
            "INSERT INTO workspace_accounts (id, name, created_by, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
            (workspace_id, f"Personal workspace {user_id}", user_id, now, now),
            commit=True,
        )
        execute_query(
            "INSERT INTO workspace_members (workspace_id, user_id, role, status, created_at, updated_at) VALUES (?, ?, 'owner', 'active', ?, ?)",
            (workspace_id, user_id, now, now),
            commit=True,
        )


def _load_current_user(user_id: Any) -> tuple[str, str]:
    execute_query = _auth_module()
    canonical_user = _canonical_user_id(user_id)
    row = execute_query(
        "SELECT id, role, is_banned FROM users WHERE id = ?",
        (canonical_user,),
        fetchone=True,
    )
    if not row:
        raise WorkspaceAuthorizationError("Authenticated user is unavailable", status_code=401, code="UNAUTHORIZED")
    if _db_row_value(row, "is_banned", 2):
        raise WorkspaceAuthorizationError("Account suspended", status_code=403, code="BANNED")
    return canonical_user, str(_db_row_value(row, "role", 1) or "user")


def _find_membership(user_id: str, workspace_id: str) -> Optional[dict[str, Any]]:
    execute_query = _auth_module()
    row = execute_query(
        "SELECT workspace_id, user_id, role, status FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user_id),
        fetchone=True,
    )
    if not row:
        return None
    return {
        "workspace_id": str(_db_row_value(row, "workspace_id")),
        "user_id": str(_db_row_value(row, "user_id", 1)),
        "role": str(_db_row_value(row, "role", 2) or ""),
        "status": str(_db_row_value(row, "status", 3) or ""),
    }


def resolve_workspace(user_id: Any, requested_workspace_id: Any = None, *, allowed_roles: Iterable[str] | None = None) -> WorkspaceContext:
    """Resolve and authorize a workspace using current persisted membership."""
    canonical_user, global_role = _load_current_user(user_id)
    execute_query = _auth_module()

    if requested_workspace_id is not None:
        workspace_id = _canonical_workspace_id(requested_workspace_id)
    else:
        row = execute_query(
            "SELECT workspace_id FROM workspace_members WHERE user_id = ? AND role = 'owner' AND status = 'active' ORDER BY created_at ASC LIMIT 1",
            (canonical_user,),
            fetchone=True,
        )
        if not row:
            # New accounts created after the additive migration receive their
            # personal workspace on first protected request.  Do not recreate
            # one after a removal/suspension: any historical membership keeps
            # the fail-closed decision authoritative.
            historical = execute_query(
                "SELECT workspace_id FROM workspace_members WHERE user_id = ? LIMIT 1",
                (canonical_user,),
                fetchone=True,
            )
            if historical:
                raise WorkspaceAuthorizationError("No active workspace membership exists", code="WORKSPACE_REQUIRED")
            workspace_id = f"ws_{uuid.uuid4().hex}"
            now = time.time()
            execute_query(
                "INSERT INTO workspace_accounts (id, name, created_by, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                (workspace_id, f"Personal workspace {canonical_user}", canonical_user, now, now),
                commit=True,
            )
            execute_query(
                "INSERT INTO workspace_members (workspace_id, user_id, role, status, created_at, updated_at) VALUES (?, ?, 'owner', 'active', ?, ?)",
                (workspace_id, canonical_user, now, now),
                commit=True,
            )
        else:
            workspace_id = str(_db_row_value(row, "workspace_id"))

    account = execute_query(
        "SELECT status FROM workspace_accounts WHERE id = ?",
        (workspace_id,),
        fetchone=True,
    )
    if not account or str(_db_row_value(account, "status") or "") != "active":
        raise WorkspaceAuthorizationError("Workspace is unavailable", code="WORKSPACE_FORBIDDEN")
    membership = _find_membership(canonical_user, workspace_id)
    if not membership or membership["status"] != ACTIVE_MEMBER:
        raise WorkspaceAuthorizationError("Workspace membership is not active", code="WORKSPACE_FORBIDDEN")
    role = membership["role"]
    if role not in WORKSPACE_ROLES:
        raise WorkspaceAuthorizationError("Workspace role is invalid", code="WORKSPACE_FORBIDDEN")
    if allowed_roles is not None and role not in set(allowed_roles):
        raise WorkspaceAuthorizationError("Workspace role does not authorize this operation", code="WORKSPACE_FORBIDDEN")
    return WorkspaceContext(workspace_id, canonical_user, role, global_role)


def require_workspace(user_id: Any, requested_workspace_id: Any = None, *, allowed_roles: Iterable[str] | None = None) -> WorkspaceContext:
    return resolve_workspace(user_id, requested_workspace_id, allowed_roles=allowed_roles)


def create_workspace(*, actor_user_id: Any, name: str, member_user_id: Any | None = None, role: str = "member") -> str:
    """Create a workspace and its initial member through the authoritative DB."""
    actor, _global = _load_current_user(actor_user_id)
    if role not in WORKSPACE_ROLES:
        raise ValueError("unknown workspace role")
    member = _canonical_user_id(member_user_id if member_user_id is not None else actor)
    execute_query = _auth_module()
    if not execute_query("SELECT id FROM users WHERE id = ?", (member,), fetchone=True):
        raise WorkspaceAuthorizationError("Workspace member is unavailable", status_code=404, code="USER_NOT_FOUND")
    workspace_id = f"ws_{uuid.uuid4().hex}"
    now = time.time()
    execute_query(
        "INSERT INTO workspace_accounts (id, name, created_by, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
        (workspace_id, name.strip() or "Workspace", actor, now, now),
        commit=True,
    )
    execute_query(
        "INSERT INTO workspace_members (workspace_id, user_id, role, status, created_at, updated_at) VALUES (?, ?, 'owner', 'active', ?, ?)",
        (workspace_id, actor, now, now),
        commit=True,
    )
    if member != actor:
        execute_query(
            "INSERT INTO workspace_members (workspace_id, user_id, role, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
            (workspace_id, member, role, now, now),
            commit=True,
        )
    return workspace_id


def set_membership(*, actor_user_id: Any, workspace_id: str, member_user_id: Any, role: str | None = None, status: str | None = None) -> None:
    """Change membership only by an active workspace owner/admin."""
    actor = require_workspace(actor_user_id, workspace_id, allowed_roles=("owner", "admin"))
    member = _canonical_user_id(member_user_id)
    if role is not None and role not in WORKSPACE_ROLES:
        raise ValueError("unknown workspace role")
    if status is not None and status not in {"active", "suspended", "removed"}:
        raise ValueError("unknown membership status")
    execute_query = _auth_module()
    row = execute_query(
        "SELECT role, status FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
        (actor.workspace_id, member),
        fetchone=True,
    )
    if not row:
        if not execute_query("SELECT id FROM users WHERE id = ?", (member,), fetchone=True):
            raise WorkspaceAuthorizationError("Workspace member not found", status_code=404, code="MEMBER_NOT_FOUND")
        now = time.time()
        execute_query(
            "INSERT INTO workspace_members (workspace_id, user_id, role, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (actor.workspace_id, member, role or "member", status or "active", now, now),
            commit=True,
        )
        return
    now = time.time()
    new_status = status or str(_db_row_value(row, "status", 1) or "active")
    execute_query(
        "UPDATE workspace_members SET role = ?, status = ?, updated_at = ?, removed_at = ? WHERE workspace_id = ? AND user_id = ?",
        (role or str(_db_row_value(row, "role", 0) or "member"), new_status, now, now if new_status == "removed" else None, actor.workspace_id, member),
        commit=True,
    )
