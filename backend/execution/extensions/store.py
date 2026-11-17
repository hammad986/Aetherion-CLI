"""
backend/execution/extensions/store.py — Persistent SQLite store for plugins, skills, versions, and audit logs.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import ALL_PERMISSIONS

logger = logging.getLogger("aetherion.extensions.store")


class PublishConflict(Exception):
    """Stable publish conflict that is safe to map to HTTP 409."""

    def __init__(self, message: str, *, same_content: bool = False):
        super().__init__(message)
        self.same_content = same_content

DEFAULT_EXT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "extensions.db"


class _ManagedSQLiteConnection(sqlite3.Connection):
    """Close SQLite handles when the existing ``with`` blocks finish."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class ExtensionStore:
    """Multi-tenant SQLite store for installed plugins, skills, and historical versions."""

    def __init__(self, db_path: Optional[Path | str] = None):
        if db_path is None:
            db_path = os.environ.get("AETHERION_EXTENSIONS_DB", str(DEFAULT_EXT_DB_PATH))
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, factory=_ManagedSQLiteConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS installed_plugins (
                    id TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    description TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    license TEXT NOT NULL DEFAULT '',
                    entrypoint TEXT NOT NULL,
                    runtime TEXT NOT NULL DEFAULT 'python',
                    permissions TEXT NOT NULL DEFAULT '[]',
                    provided_tools TEXT NOT NULL DEFAULT '[]',
                    dependencies TEXT NOT NULL DEFAULT '[]',
                    install_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    trust_level TEXT NOT NULL DEFAULT 'UNTRUSTED',
                     PRIMARY KEY (id, owner_id)
                )
            """)

            try:
                conn.execute("ALTER TABLE installed_plugins ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'UNTRUSTED'")
            except Exception:
                pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS plugin_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plugin_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    install_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    installed_at REAL NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS installed_skills (
                    id TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    description TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    instructions TEXT NOT NULL,
                    required_tools TEXT NOT NULL DEFAULT '[]',
                    permissions TEXT NOT NULL DEFAULT '[]',
                    tags TEXT NOT NULL DEFAULT '[]',
                    triggers TEXT NOT NULL DEFAULT '[]',
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    install_path TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    lifecycle_status TEXT NOT NULL DEFAULT 'INSTALLED',
                    approval_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
                    published_at REAL,
                    published_by TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    source_kind TEXT NOT NULL DEFAULT 'runtime_install',
                     PRIMARY KEY (id, owner_id, workspace_id)
                )
            """)

            for column, definition in (
                ("content", "TEXT"),
                ("lifecycle_status", "TEXT NOT NULL DEFAULT 'INSTALLED'"),
                ("approval_status", "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'"),
                ("published_at", "REAL"),
                ("published_by", "TEXT NOT NULL DEFAULT ''"),
                ("workspace_id", "TEXT NOT NULL DEFAULT ''"),
                ("source_kind", "TEXT NOT NULL DEFAULT 'runtime_install'"),
            ):
                try:
                    conn.execute(f"ALTER TABLE installed_skills ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass

            # Older Phase 5.1 SQLite files keyed skills by (id, owner_id),
            # which prevented the same creator from publishing the same
            # canonical ID into two independent workspaces.  Rebuild only that
            # table, preserving every row and immutable snapshot.
            pk_columns = [row[1] for row in conn.execute("PRAGMA table_info(installed_skills)").fetchall() if row[5]]
            if pk_columns == ["id", "owner_id"]:
                conn.execute("DROP INDEX IF EXISTS ux_installed_skills_workspace_id")
                conn.execute("ALTER TABLE installed_skills RENAME TO installed_skills_legacy")
                conn.execute("""
                    CREATE TABLE installed_skills (
                        id TEXT NOT NULL,
                        owner_id TEXT NOT NULL DEFAULT '',
                        name TEXT NOT NULL,
                        version TEXT NOT NULL,
                        description TEXT NOT NULL,
                        author TEXT NOT NULL DEFAULT '',
                        instructions TEXT NOT NULL,
                        required_tools TEXT NOT NULL DEFAULT '[]',
                        permissions TEXT NOT NULL DEFAULT '[]',
                        tags TEXT NOT NULL DEFAULT '[]',
                        triggers TEXT NOT NULL DEFAULT '[]',
                        is_enabled INTEGER NOT NULL DEFAULT 1,
                        install_path TEXT NOT NULL DEFAULT '',
                        content_hash TEXT NOT NULL,
                        content TEXT NOT NULL DEFAULT '',
                        manifest_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        lifecycle_status TEXT NOT NULL DEFAULT 'INSTALLED',
                        approval_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
                        published_at REAL,
                        published_by TEXT NOT NULL DEFAULT '',
                        workspace_id TEXT NOT NULL DEFAULT '',
                        source_kind TEXT NOT NULL DEFAULT 'runtime_install',
                        PRIMARY KEY (id, owner_id, workspace_id)
                    )
                """)
                conn.execute("""
                    INSERT INTO installed_skills (
                        id, owner_id, name, version, description, author, instructions,
                        required_tools, permissions, tags, triggers, is_enabled,
                        install_path, content_hash, content, manifest_json, created_at, updated_at,
                        lifecycle_status, approval_status, published_at, published_by, workspace_id, source_kind
                    ) SELECT id, owner_id, name, version, description, author, instructions,
                        required_tools, permissions, tags, triggers, is_enabled,
                        install_path, content_hash, content, manifest_json, created_at, updated_at,
                        lifecycle_status, approval_status, published_at, published_by, workspace_id, source_kind
                    FROM installed_skills_legacy
                """)
                conn.execute("DROP TABLE installed_skills_legacy")

            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_installed_skills_workspace_id "
                    "ON installed_skills (workspace_id, id) WHERE workspace_id <> ''"
                )
            except sqlite3.IntegrityError:
                # A pre-remediation database may contain duplicate legacy
                # rows.  Publish still fails closed until an operator reconciles
                # those rows; never silently overwrite one here.
                logger.error("Existing skill workspace uniqueness conflict; publish remains unavailable")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS skill_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    instructions TEXT NOT NULL,
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    installed_at REAL NOT NULL
                )
            """)
            try:
                conn.execute("ALTER TABLE skill_versions ADD COLUMN content TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS extension_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL DEFAULT '',
                    extension_type TEXT NOT NULL,
                    extension_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            conn.commit()

    def log_audit(self, owner_id: str, ext_type: str, ext_id: str, action: str, details: str = "") -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(owner_id), ext_type, ext_id, action, details, time.time()),
            )
            conn.commit()

    # ── Plugins Store ─────────────────────────────────────────────────────────

    def save_plugin(self, record: Dict[str, Any], owner_id: str) -> Dict[str, Any]:
        now = time.time()
        pid = record["id"]
        owner = str(owner_id)

        perms_json = json.dumps(record.get("permissions", []))
        tools_json = json.dumps(record.get("provided_tools", []))
        deps_json = json.dumps(record.get("dependencies", []))
        manifest_json = json.dumps(record.get("manifest_json", {}))

        trust_level = str(record.get("trust_level") or record.get("manifest_json", {}).get("trust_level") or "UNTRUSTED")

        with self._get_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM installed_plugins WHERE id = ? AND owner_id = ?", (pid, owner)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE installed_plugins SET
                        name = ?, version = ?, description = ?, author = ?, license = ?,
                        entrypoint = ?, runtime = ?, permissions = ?, provided_tools = ?,
                        dependencies = ?, install_path = ?, content_hash = ?, is_enabled = ?,
                        manifest_json = ?, updated_at = ?, trust_level = ?
                    WHERE id = ? AND owner_id = ?
                """, (
                    record["name"], record["version"], record["description"],
                    record.get("author", ""), record.get("license", ""),
                    record["entrypoint"], record.get("runtime", "python"),
                    perms_json, tools_json, deps_json,
                    str(record["install_path"]), record["content_hash"],
                    1 if record.get("is_enabled", True) else 0,
                    manifest_json, now, trust_level, pid, owner,
                ))
            else:
                conn.execute("""
                    INSERT INTO installed_plugins (
                        id, owner_id, name, version, description, author, license,
                        entrypoint, runtime, permissions, provided_tools, dependencies,
                        install_path, content_hash, is_enabled, manifest_json, created_at, updated_at, trust_level
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pid, owner, record["name"], record["version"], record["description"],
                    record.get("author", ""), record.get("license", ""),
                    record["entrypoint"], record.get("runtime", "python"),
                    perms_json, tools_json, deps_json,
                    str(record["install_path"]), record["content_hash"],
                    1 if record.get("is_enabled", True) else 0,
                    manifest_json, now, now, trust_level,
                ))

            # Record version snapshot
            conn.execute("""
                INSERT INTO plugin_versions (plugin_id, owner_id, version, content_hash, install_path, manifest_json, installed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, owner, record["version"], record["content_hash"],
                str(record["install_path"]), manifest_json, now,
            ))
            conn.commit()

        self.log_audit(owner, "plugin", pid, "INSTALL_OR_UPDATE", f"Version {record['version']}")
        return self.get_plugin(pid, owner)

    def get_plugin(self, plugin_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM installed_plugins WHERE id = ? AND (owner_id = ? OR owner_id = '')",
                (plugin_id, str(owner_id)),
            ).fetchone()
            if not row:
                return None
            return self._row_to_plugin_dict(row)

    def list_plugins(self, owner_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            query = "SELECT * FROM installed_plugins WHERE (owner_id = ? OR owner_id = '')"
            params: List[Any] = [str(owner_id)]
            if enabled_only:
                query += " AND is_enabled = 1"
            query += " ORDER BY name ASC"
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_plugin_dict(r) for r in rows]

    def set_plugin_enabled(self, plugin_id: str, enabled: bool, owner_id: str) -> Optional[Dict[str, Any]]:
        existing = self.get_plugin(plugin_id, owner_id)
        if not existing:
            return None
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE installed_plugins SET is_enabled = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
                (1 if enabled else 0, time.time(), plugin_id, str(owner_id)),
            )
            conn.commit()
        action = "ENABLE" if enabled else "DISABLE"
        self.log_audit(str(owner_id), "plugin", plugin_id, action)
        return self.get_plugin(plugin_id, owner_id)

    def delete_plugin(self, plugin_id: str, owner_id: str) -> bool:
        existing = self.get_plugin(plugin_id, owner_id)
        if not existing:
            return False
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM installed_plugins WHERE id = ? AND owner_id = ?", (plugin_id, str(owner_id))
            )
            conn.commit()
        self.log_audit(str(owner_id), "plugin", plugin_id, "REMOVE")
        return True

    def get_plugin_versions(self, plugin_id: str, owner_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM plugin_versions WHERE plugin_id = ? AND (owner_id = ? OR owner_id = '') ORDER BY installed_at DESC",
                (plugin_id, str(owner_id)),
            ).fetchall()
            return [{
                "version": r["version"],
                "content_hash": r["content_hash"],
                "install_path": r["install_path"],
                "manifest_json": r["manifest_json"],
                "installed_at": r["installed_at"],
            } for r in rows]

    # ── Skills Store ──────────────────────────────────────────────────────────

    def save_skill(self, record: Dict[str, Any], owner_id: str) -> Dict[str, Any]:
        now = time.time()
        sid = record["id"]
        owner = str(owner_id)

        tools_json = json.dumps(record.get("required_tools", []))
        perms_json = json.dumps(record.get("permissions", []))
        tags_json = json.dumps(record.get("tags", []))
        triggers_json = json.dumps(record.get("triggers", []))
        manifest_json = json.dumps(record.get("manifest_json", {}))

        with self._get_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM installed_skills WHERE id = ? AND owner_id = ?", (sid, owner)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE installed_skills SET
                        name = ?, version = ?, description = ?, author = ?, instructions = ?,
                        required_tools = ?, permissions = ?, tags = ?, triggers = ?,
                        is_enabled = ?, install_path = ?, content_hash = ?, content = ?, manifest_json = ?, updated_at = ?,
                        lifecycle_status = ?, approval_status = ?, published_at = ?, published_by = ?,
                        workspace_id = ?, source_kind = ?
                    WHERE id = ? AND owner_id = ?
                """, (
                    record["name"], record["version"], record["description"],
                    record.get("author", ""), record["instructions"],
                    tools_json, perms_json, tags_json, triggers_json,
                    1 if record.get("is_enabled", bool(existing["is_enabled"])) else 0,
                    str(record.get("install_path", "")), record["content_hash"],
                    str(record.get("content", existing["content"] if "content" in existing.keys() else "")),
                    manifest_json, now,
                    record.get("lifecycle_status", existing["lifecycle_status"]),
                    record.get("approval_status", existing["approval_status"]),
                    record.get("published_at", existing["published_at"]),
                    record.get("published_by", existing["published_by"]),
                    record.get("workspace_id", existing["workspace_id"]),
                    record.get("source_kind", existing["source_kind"]),
                    sid, owner,
                ))
            else:
                conn.execute("""
                    INSERT INTO installed_skills (
                        id, owner_id, name, version, description, author, instructions,
                        required_tools, permissions, tags, triggers, is_enabled,
                        install_path, content_hash, content, manifest_json, created_at, updated_at,
                        lifecycle_status, approval_status, published_at, published_by, workspace_id, source_kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sid, owner, record["name"], record["version"], record["description"],
                    record.get("author", ""), record["instructions"],
                    tools_json, perms_json, tags_json, triggers_json,
                    1 if record.get("is_enabled", True) else 0,
                    str(record.get("install_path", "")), record["content_hash"], str(record.get("content", "")),
                    manifest_json, now, now,
                    record.get("lifecycle_status", "INSTALLED"), record.get("approval_status", "NOT_REQUIRED"),
                    record.get("published_at"), record.get("published_by", ""), record.get("workspace_id", ""),
                    record.get("source_kind", "runtime_install"),
                ))

            conn.execute("""
                INSERT INTO skill_versions (skill_id, owner_id, version, content_hash, content, instructions, manifest_json, installed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sid, owner, record["version"], record["content_hash"], str(record.get("content", "")),
                record["instructions"], manifest_json, now,
            ))
            conn.commit()

        self.log_audit(owner, "skill", sid, "INSTALL_OR_UPDATE", f"Version {record['version']}")
        return self.get_skill(sid, owner)

    def publish_skill(self, record: Dict[str, Any], owner_id: str) -> tuple[Dict[str, Any], str]:
        """Atomically publish a workspace-scoped immutable skill snapshot.

        The workspace/id unique index is the concurrency boundary.  The
        primary row, immutable version, and audit event are committed together;
        a lost insert race is reconciled from the committed row.
        """
        sid = str(record["id"])
        owner = str(owner_id)
        workspace = str(record.get("workspace_id") or "")
        if not workspace:
            raise ValueError("workspace_id is required for publication")
        now = float(record.get("published_at") or time.time())
        tools_json = json.dumps(record.get("required_tools", []))
        perms_json = json.dumps(record.get("permissions", []))
        tags_json = json.dumps(record.get("tags", []))
        triggers_json = json.dumps(record.get("triggers", []))
        manifest_json = json.dumps(record.get("manifest_json", {}))
        values = (
            sid, owner, record["name"], record["version"], record["description"],
            record.get("author", ""), record["instructions"], tools_json,
            perms_json, tags_json, triggers_json, 0,
            str(record.get("install_path", "")), record["content_hash"],
            str(record.get("content", "")), manifest_json, now, now,
            record.get("lifecycle_status", "PUBLISHED"), record.get("approval_status", "NOT_REQUIRED"),
            record.get("published_at", now), str(record.get("published_by", owner)), workspace,
            record.get("source_kind", "authenticated_publish"),
        )
        try:
            with self._get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM installed_skills WHERE workspace_id = ? AND id = ?",
                    (workspace, sid),
                ).fetchone()
                if existing:
                    if existing["version"] == record["version"] and existing["content_hash"] == record["content_hash"]:
                        conn.commit()
                        return self._row_to_skill_dict(existing), "ALREADY_CURRENT"
                    conn.rollback()
                    raise PublishConflict(
                        "A different published version conflict exists for this workspace and skill ID",
                        same_content=False,
                    )
                conn.execute(
                    """INSERT INTO installed_skills (
                        id, owner_id, name, version, description, author, instructions,
                        required_tools, permissions, tags, triggers, is_enabled,
                        install_path, content_hash, content, manifest_json, created_at, updated_at,
                        lifecycle_status, approval_status, published_at, published_by, workspace_id, source_kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                conn.execute(
                    "INSERT INTO skill_versions (skill_id, owner_id, version, content_hash, content, instructions, manifest_json, installed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, owner, record["version"], record["content_hash"], str(record.get("content", "")), record["instructions"], manifest_json, now),
                )
                conn.execute(
                    "INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                    (owner, "skill", sid, "PUBLISH", f"workspace={workspace};version={record['version']};digest={record['content_hash']}", now),
                )
                conn.commit()
                saved = conn.execute(
                    "SELECT * FROM installed_skills WHERE workspace_id = ? AND id = ?",
                    (workspace, sid),
                ).fetchone()
                return self._row_to_skill_dict(saved), "CREATED"
        except PublishConflict:
            raise
        except sqlite3.IntegrityError as exc:
            # Another process may have committed the unique row after our
            # pre-insert read. Reconcile it instead of leaking IntegrityError.
            if "unique constraint" not in str(exc).lower() and "unique failed" not in str(exc).lower():
                raise
            with self._get_connection() as conn:
                existing = conn.execute(
                    "SELECT * FROM installed_skills WHERE workspace_id = ? AND id = ?",
                    (workspace, sid),
                ).fetchone()
            if existing and existing["version"] == record["version"] and existing["content_hash"] == record["content_hash"]:
                return self._row_to_skill_dict(existing), "ALREADY_CURRENT"
            raise PublishConflict(
                "A different published version conflict exists for this workspace and skill ID",
                same_content=False,
            ) from exc

    def get_skill_by_workspace(self, skill_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM installed_skills WHERE id = ? AND workspace_id = ?",
                (str(skill_id), str(workspace_id)),
            ).fetchone()
            return self._row_to_skill_dict(row) if row else None

    def list_skills_by_workspace(self, workspace_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            query = "SELECT * FROM installed_skills WHERE workspace_id = ?"
            params: List[Any] = [str(workspace_id)]
            if enabled_only:
                query += " AND is_enabled = 1"
            query += " ORDER BY name ASC"
            return [self._row_to_skill_dict(row) for row in conn.execute(query, params).fetchall()]

    def get_skill(self, skill_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM installed_skills WHERE id = ? AND owner_id = ?",
                (skill_id, str(owner_id)),
            ).fetchone()
            if not row:
                return None
            return self._row_to_skill_dict(row)

    def list_skills(self, owner_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            query = "SELECT * FROM installed_skills WHERE owner_id = ?"
            params: List[Any] = [str(owner_id)]
            if enabled_only:
                query += " AND is_enabled = 1"
            query += " ORDER BY name ASC"
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_skill_dict(r) for r in rows]

    def set_skill_enabled(self, skill_id: str, enabled: bool, owner_id: str) -> Optional[Dict[str, Any]]:
        existing = self.get_skill(skill_id, owner_id)
        if not existing:
            return None
        if enabled and existing.get("approval_status") == "PENDING":
            return None
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE installed_skills SET is_enabled = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
                (1 if enabled else 0, time.time(), skill_id, str(owner_id)),
            )
            conn.execute(
                "INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (str(owner_id), "skill", skill_id, "ENABLE" if enabled else "DISABLE", "", time.time()),
            )
            conn.commit()
        return self.get_skill(skill_id, owner_id)

    def set_skill_approval(
        self, skill_id: str, owner_id: str, *, approved: bool, actor_id: str
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_skill(skill_id, owner_id)
        if not existing:
            return None
        approval = "APPROVED" if approved else "REJECTED"
        lifecycle = "PUBLISHED" if approved else "BLOCKED"
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE installed_skills SET approval_status = ?, lifecycle_status = ?, is_enabled = 0, updated_at = ? "
                "WHERE id = ? AND owner_id = ?",
                (approval, lifecycle, time.time(), skill_id, str(owner_id)),
            )
            conn.execute(
                "INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(owner_id), "skill", skill_id, "APPROVE" if approved else "REJECT", f"actor={actor_id}", time.time()),
            )
            conn.commit()
        return self.get_skill(skill_id, owner_id)

    def list_runtime_skills(self, owner_id: str, skill_ids: List[str]) -> List[Dict[str, Any]]:
        if not skill_ids:
            return []
        placeholders = ",".join("?" for _ in skill_ids)
        with self._get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM installed_skills WHERE owner_id = ? AND id IN ({placeholders}) "
                "AND lifecycle_status = 'PUBLISHED' AND approval_status IN ('APPROVED', 'NOT_REQUIRED') AND is_enabled = 1",
                [str(owner_id), *skill_ids],
            ).fetchall()
            by_id = {row["id"]: self._row_to_skill_dict(row) for row in rows}
            return [by_id[item] for item in skill_ids if item in by_id]

    def list_runtime_skills_by_workspace(self, workspace_id: str, skill_ids: List[str]) -> List[Dict[str, Any]]:
        if not skill_ids:
            return []
        placeholders = ",".join("?" for _ in skill_ids)
        with self._get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM installed_skills WHERE workspace_id = ? AND id IN ({placeholders}) "
                "AND lifecycle_status = 'PUBLISHED' AND approval_status IN ('APPROVED', 'NOT_REQUIRED') AND is_enabled = 1",
                [str(workspace_id), *skill_ids],
            ).fetchall()
            by_id = {row["id"]: self._row_to_skill_dict(row) for row in rows}
            return [by_id[item] for item in skill_ids if item in by_id]

    def delete_skill(self, skill_id: str, owner_id: str) -> bool:
        existing = self.get_skill(skill_id, owner_id)
        if not existing:
            return False
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM installed_skills WHERE id = ? AND owner_id = ?", (skill_id, str(owner_id))
            )
            conn.execute(
                "INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (str(owner_id), "skill", skill_id, "REMOVE", "", time.time()),
            )
            conn.commit()
        return True

    def get_skill_versions(self, skill_id: str, owner_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_id = ? AND owner_id = ? ORDER BY installed_at DESC",
                (skill_id, str(owner_id)),
            ).fetchall()
            return [{
                "version": r["version"],
                "content_hash": r["content_hash"],
                "content": r["content"] if "content" in r.keys() else "",
                "instructions": r["instructions"],
                "manifest_json": r["manifest_json"],
                "installed_at": r["installed_at"],
            } for r in rows]

    # ── Converters ────────────────────────────────────────────────────────────

    def _row_to_plugin_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        try: perms = json.loads(row["permissions"])
        except Exception: perms = []
        try: tools = json.loads(row["provided_tools"])
        except Exception: tools = []
        try: deps = json.loads(row["dependencies"])
        except Exception: deps = []
        try: meta = json.loads(row["manifest_json"])
        except Exception: meta = {}

        trust_lvl = "UNTRUSTED"
        if "trust_level" in row.keys() and row["trust_level"]:
            trust_lvl = row["trust_level"]
        elif meta.get("trust_level"):
            trust_lvl = meta["trust_level"]

        return {
            "id": row["id"],
            "owner_id": row["owner_id"],
            "name": row["name"],
            "version": row["version"],
            "description": row["description"],
            "author": row["author"],
            "license": row["license"],
            "entrypoint": row["entrypoint"],
            "runtime": row["runtime"],
            "permissions": perms,
            "provided_tools": tools,
            "dependencies": deps,
            "install_path": row["install_path"],
            "content_hash": row["content_hash"],
            "is_enabled": bool(row["is_enabled"]),
            "trust_level": trust_lvl,
            "manifest_json": meta,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_skill_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        errors: list[str] = []

        def decode(name: str, default: Any, expected_type: type) -> Any:
            try:
                value = json.loads(row[name])
            except Exception:
                errors.append(f"malformed_{name}")
                return default
            if not isinstance(value, expected_type):
                errors.append(f"invalid_{name}_type")
                return default
            return value

        tools = decode("required_tools", [], list)
        perms = decode("permissions", [], list)
        tags = decode("tags", [], list)
        trigs = decode("triggers", [], list)
        meta = decode("manifest_json", {}, dict)
        if any(not isinstance(item, str) for item in tools + perms + tags + trigs):
            errors.append("invalid_metadata_list_entry")
        if any(item not in ALL_PERMISSIONS for item in perms):
            errors.append("unknown_permission")

        content = row["content"] if "content" in row.keys() else ""
        content_hash = str(row["content_hash"] or "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", content_hash):
            errors.append("invalid_content_hash")
        if not isinstance(content, str) or not isinstance(row["instructions"], str):
            errors.append("invalid_content_type")
        source_kind = row["source_kind"] if "source_kind" in row.keys() else "runtime_install"
        lifecycle = row["lifecycle_status"] if "lifecycle_status" in row.keys() else "INSTALLED"
        approval = row["approval_status"] if "approval_status" in row.keys() else "NOT_REQUIRED"
        allowed_lifecycle = {"INSTALLED", "PUBLISHED", "BLOCKED"}
        allowed_approval = {"NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED"}
        if lifecycle not in allowed_lifecycle:
            errors.append("unknown_lifecycle_status")
        if approval not in allowed_approval:
            errors.append("unknown_approval_status")
        if approval == "APPROVED" and lifecycle != "PUBLISHED":
            errors.append("approved_skill_not_published")
        if approval == "REJECTED" and bool(row["is_enabled"]):
            errors.append("rejected_skill_enabled")
        if lifecycle == "BLOCKED" and approval not in {"REJECTED", "PENDING"}:
            errors.append("blocked_skill_approval_mismatch")
        if source_kind == "authenticated_publish":
            if lifecycle not in {"PUBLISHED", "BLOCKED"}:
                errors.append("invalid_published_lifecycle")
            if approval == "PENDING" and bool(row["is_enabled"]):
                errors.append("pending_skill_enabled")
            if not content or hashlib.sha256(content.encode("utf-8")).hexdigest().lower() != content_hash.lower():
                errors.append("content_digest_mismatch")

        record = {
            "id": row["id"],
            "owner_id": row["owner_id"],
            "name": row["name"],
            "version": row["version"],
            "description": row["description"],
            "author": row["author"],
            "instructions": row["instructions"],
            "content": content,
            "required_tools": tools,
            "permissions": perms,
            "tags": tags,
            "triggers": trigs,
            "is_enabled": bool(row["is_enabled"]),
            "install_path": row["install_path"],
            "content_hash": row["content_hash"],
            "manifest_json": meta,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "lifecycle_status": row["lifecycle_status"] if "lifecycle_status" in row.keys() else "INSTALLED",
            "approval_status": row["approval_status"] if "approval_status" in row.keys() else "NOT_REQUIRED",
            "published_at": row["published_at"] if "published_at" in row.keys() else None,
            "published_by": row["published_by"] if "published_by" in row.keys() else "",
            "workspace_id": row["workspace_id"] if "workspace_id" in row.keys() else "",
            "source_kind": row["source_kind"] if "source_kind" in row.keys() else "runtime_install",
        }
        if errors:
            record["record_corrupt"] = True
            record["corruption_codes"] = sorted(set(errors))
        else:
            record["record_corrupt"] = False
            record["corruption_codes"] = []
        return record


def get_extension_store(*, db_path: Optional[Path | str] = None):
    """Select the extension repository from the configured database authority.

    Production/staging never silently falls back to replica-local SQLite.  A
    local SQLite path remains available for isolated tests and development.
    """
    env = (os.environ.get("ENVIRONMENT") or os.environ.get("FLASK_ENV") or os.environ.get("AETHERION_ENV") or os.environ.get("APP_ENV") or "").strip().lower()
    dsn = os.environ.get("DATABASE_URL", "").strip()
    try:
        from core.environment import is_fail_closed_environment
        fail_closed = is_fail_closed_environment()
    except Exception:
        fail_closed = env in {"production", "prod", "staging", "stage", "unknown"}
    if fail_closed:
        if not dsn.startswith(("postgres://", "postgresql://")):
            raise RuntimeError("Production extension storage requires a shared PostgreSQL DATABASE_URL")
        from .postgres_store import PostgresExtensionStore
        return PostgresExtensionStore(dsn)
    if dsn.startswith(("postgres://", "postgresql://")) and not db_path and not os.environ.get("AETHERION_EXTENSIONS_DB"):
        try:
            from .postgres_store import PostgresExtensionStore
            return PostgresExtensionStore(dsn)
        except Exception:
            logger.warning("Configured development PostgreSQL extension store is unavailable; using explicit SQLite test store", exc_info=True)
    return ExtensionStore(db_path)
