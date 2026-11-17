"""Shared PostgreSQL repository for published extensions and skills.

The repository uses the existing ``db.postgres_store.PostgresStore`` pool and
never creates a second database authority.  Each operation leases one pooled
connection, performs bounded parameterized SQL, and returns it after commit or
rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from db.postgres_store import PostgresStore
from db.extension_migrations import verify_extension_schema
from execution.extensions.models import ALL_PERMISSIONS
from execution.extensions.store import PublishConflict


try:
    from psycopg2.extras import Json
except ImportError:  # pragma: no cover - constructor fails clearly first
    def Json(value):  # type: ignore
        return value


class PostgresExtensionStore:
    """ExtensionStore-compatible repository backed by shared PostgreSQL."""

    _schema_dsn: Optional[str] = None

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or os.environ.get("DATABASE_URL", "")
        if not self.dsn.startswith(("postgres://", "postgresql://")):
            raise RuntimeError("A PostgreSQL DATABASE_URL is required for the production extension store")
        db = PostgresStore(self.dsn)
        try:
            if self.__class__._schema_dsn != self.dsn:
                # Schema ownership belongs to the controlled migration Job.
                # Backend workers only verify the already-migrated contract;
                # they never race to execute DDL during request handling.
                verify_extension_schema(db._conn)
                self.__class__._schema_dsn = self.dsn
        finally:
            db.close()

    def _run(self, callback, *, commit: bool = False):
        db = PostgresStore(self.dsn)
        try:
            result = callback(db)
            if commit:
                db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(value: Any, default: Any, expected: type, errors: list[str], name: str):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                errors.append(f"malformed_{name}")
                return default
        if not isinstance(value, expected):
            errors.append(f"invalid_{name}_type")
            return default
        return value

    @classmethod
    def _skill_dict(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        errors: list[str] = []
        tools = cls._decode(row.get("required_tools"), [], list, errors, "required_tools")
        perms = cls._decode(row.get("permissions"), [], list, errors, "permissions")
        tags = cls._decode(row.get("tags"), [], list, errors, "tags")
        triggers = cls._decode(row.get("triggers"), [], list, errors, "triggers")
        meta = cls._decode(row.get("manifest_json"), {}, dict, errors, "manifest_json")
        if any(not isinstance(item, str) for item in tools + perms + tags + triggers):
            errors.append("invalid_metadata_list_entry")
        if any(item not in ALL_PERMISSIONS for item in perms):
            errors.append("unknown_permission")
        content = row.get("content") or ""
        digest = str(row.get("content_hash") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            errors.append("invalid_content_hash")
        source_kind = row.get("source_kind") or "runtime_install"
        lifecycle = row.get("lifecycle_status") or "INSTALLED"
        approval = row.get("approval_status") or "NOT_REQUIRED"
        if lifecycle not in {"INSTALLED", "PUBLISHED", "BLOCKED"}:
            errors.append("unknown_lifecycle_status")
        if approval not in {"NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED"}:
            errors.append("unknown_approval_status")
        if approval == "APPROVED" and lifecycle != "PUBLISHED":
            errors.append("approved_skill_not_published")
        if approval == "REJECTED" and bool(row.get("is_enabled")):
            errors.append("rejected_skill_enabled")
        if lifecycle == "BLOCKED" and approval not in {"REJECTED", "PENDING"}:
            errors.append("blocked_skill_approval_mismatch")
        if source_kind == "authenticated_publish":
            if approval == "PENDING" and bool(row.get("is_enabled")):
                errors.append("pending_skill_enabled")
            if not content or hashlib.sha256(str(content).encode("utf-8")).hexdigest().lower() != digest.lower():
                errors.append("content_digest_mismatch")
        result = {
            "id": row.get("id"), "owner_id": row.get("owner_id"), "name": row.get("name"),
            "version": row.get("version"), "description": row.get("description"), "author": row.get("author"),
            "instructions": row.get("instructions"), "content": content,
            "required_tools": tools, "permissions": perms, "tags": tags, "triggers": triggers,
            "is_enabled": bool(row.get("is_enabled")), "install_path": row.get("install_path", ""),
            "content_hash": digest, "manifest_json": meta, "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"), "lifecycle_status": lifecycle,
            "approval_status": approval, "published_at": row.get("published_at"),
            "published_by": row.get("published_by", ""), "workspace_id": row.get("workspace_id", ""),
            "source_kind": source_kind,
        }
        result["record_corrupt"] = bool(errors)
        result["corruption_codes"] = sorted(set(errors))
        return result

    @staticmethod
    def _plugin_dict(row: Dict[str, Any]) -> Dict[str, Any]:
        def value(name, default):
            raw = row.get(name, default)
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except Exception:
                    return default
            return raw
        return {**row, "permissions": value("permissions", []), "provided_tools": value("provided_tools", []),
                "dependencies": value("dependencies", []), "manifest_json": value("manifest_json", {})}

    def log_audit(self, owner_id: str, ext_type: str, ext_id: str, action: str, details: str = "") -> None:
        self._run(lambda db: db.execute(
            "INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (str(owner_id), ext_type, ext_id, action, details, time.time()),
        ), commit=True)

    def publish_skill(self, record: Dict[str, Any], owner_id: str) -> tuple[Dict[str, Any], str]:
        sid = str(record["id"])
        owner = str(owner_id)
        workspace = str(record.get("workspace_id") or "")
        if not workspace:
            raise ValueError("workspace_id is required for publication")
        now = float(record.get("published_at") or time.time())

        def op(db):
            row = db.fetchone("SELECT * FROM installed_skills WHERE workspace_id = ? AND id = ? FOR UPDATE", (workspace, sid))
            if row:
                existing = self._skill_dict(row)
                if row["version"] == record["version"] and row["content_hash"] == record["content_hash"]:
                    return existing, "ALREADY_CURRENT"
                raise PublishConflict("A different published version conflict exists for this workspace and skill ID")
            db.execute(
                """INSERT INTO installed_skills (
                    id, owner_id, name, version, description, author, instructions, required_tools,
                    permissions, tags, triggers, is_enabled, install_path, content_hash, content,
                    manifest_json, created_at, updated_at, lifecycle_status, approval_status,
                    published_at, published_by, workspace_id, source_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, owner, record["name"], record["version"], record["description"], record.get("author", ""),
                 record["instructions"], Json(record.get("required_tools", [])), Json(record.get("permissions", [])),
                 Json(record.get("tags", [])), Json(record.get("triggers", [])), False, str(record.get("install_path", "")),
                 record["content_hash"], str(record.get("content", "")), Json(record.get("manifest_json", {})), now, now,
                 record.get("lifecycle_status", "PUBLISHED"), record.get("approval_status", "NOT_REQUIRED"),
                 record.get("published_at", now), str(record.get("published_by", owner)), workspace,
                 record.get("source_kind", "authenticated_publish")),
            )
            db.execute(
                "INSERT INTO skill_versions (skill_id, owner_id, version, content_hash, content, instructions, manifest_json, installed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, owner, record["version"], record["content_hash"], str(record.get("content", "")), record["instructions"], Json(record.get("manifest_json", {})), now),
            )
            db.execute(
                "INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (owner, "skill", sid, "PUBLISH", f"workspace={workspace};version={record['version']};digest={record['content_hash']}", now),
            )
            saved = db.fetchone("SELECT * FROM installed_skills WHERE workspace_id = ? AND id = ?", (workspace, sid))
            return self._skill_dict(saved), "CREATED"

        try:
            return self._run(op, commit=True)
        except PublishConflict:
            raise
        except Exception as exc:
            # A unique race is reconciled after rollback. Other storage errors
            # remain explicit and are mapped by the route as unavailable.
            if "duplicate key" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise
            row = self.get_skill_by_workspace(sid, workspace)
            if row and row.get("version") == record["version"] and row.get("content_hash") == record["content_hash"]:
                return row, "ALREADY_CURRENT"
            raise PublishConflict("A different published version conflict exists for this workspace and skill ID") from exc

    def get_skill(self, skill_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        return self._run(lambda db: (lambda row: self._skill_dict(row) if row else None)(db.fetchone("SELECT * FROM installed_skills WHERE id = ? AND owner_id = ?", (skill_id, str(owner_id)))))

    def get_skill_by_workspace(self, skill_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        return self._run(lambda db: (lambda row: self._skill_dict(row) if row else None)(db.fetchone("SELECT * FROM installed_skills WHERE id = ? AND workspace_id = ?", (skill_id, str(workspace_id)))))

    def list_skills(self, owner_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM installed_skills WHERE owner_id = ?"
        if enabled_only:
            query += " AND is_enabled = TRUE"
        query += " ORDER BY name ASC"
        return self._run(lambda db: [self._skill_dict(row) for row in db.fetchall(query, (str(owner_id),))])

    def list_skills_by_workspace(self, workspace_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM installed_skills WHERE workspace_id = ?"
        if enabled_only:
            query += " AND is_enabled = TRUE"
        query += " ORDER BY name ASC"
        return self._run(lambda db: [self._skill_dict(row) for row in db.fetchall(query, (str(workspace_id),))])

    def list_runtime_skills(self, owner_id: str, skill_ids: List[str]) -> List[Dict[str, Any]]:
        if not skill_ids:
            return []
        marks = ",".join("?" for _ in skill_ids)
        return self._run(lambda db: [self._skill_dict(row) for row in db.fetchall(
            f"SELECT * FROM installed_skills WHERE owner_id = ? AND id IN ({marks}) AND lifecycle_status = 'PUBLISHED' AND approval_status IN ('APPROVED', 'NOT_REQUIRED') AND is_enabled = TRUE",
            (str(owner_id), *skill_ids),
        )])

    def list_runtime_skills_by_workspace(self, workspace_id: str, skill_ids: List[str]) -> List[Dict[str, Any]]:
        if not skill_ids:
            return []
        marks = ",".join("?" for _ in skill_ids)
        return self._run(lambda db: [self._skill_dict(row) for row in db.fetchall(
            f"SELECT * FROM installed_skills WHERE workspace_id = ? AND id IN ({marks}) AND lifecycle_status = 'PUBLISHED' AND approval_status IN ('APPROVED', 'NOT_REQUIRED') AND is_enabled = TRUE",
            (str(workspace_id), *skill_ids),
        )])

    def set_skill_enabled(self, skill_id: str, owner_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        def op(db):
            row = db.fetchone("SELECT * FROM installed_skills WHERE id = ? AND owner_id = ? FOR UPDATE", (skill_id, str(owner_id)))
            if not row or (enabled and row["approval_status"] == "PENDING"):
                return None
            db.execute("UPDATE installed_skills SET is_enabled = ?, updated_at = ? WHERE id = ? AND owner_id = ?", (enabled, time.time(), skill_id, str(owner_id)))
            db.execute("INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)", (str(owner_id), "skill", skill_id, "ENABLE" if enabled else "DISABLE", "", time.time()))
            return db.fetchone("SELECT * FROM installed_skills WHERE id = ? AND owner_id = ?", (skill_id, str(owner_id)))
        row = self._run(op, commit=True)
        return self._skill_dict(row) if row else None

    def set_skill_approval(self, skill_id: str, owner_id: str, *, approved: bool, actor_id: str) -> Optional[Dict[str, Any]]:
        def op(db):
            row = db.fetchone("SELECT * FROM installed_skills WHERE id = ? AND owner_id = ? FOR UPDATE", (skill_id, str(owner_id)))
            if not row:
                return None
            approval = "APPROVED" if approved else "REJECTED"
            lifecycle = "PUBLISHED" if approved else "BLOCKED"
            db.execute("UPDATE installed_skills SET approval_status = ?, lifecycle_status = ?, is_enabled = FALSE, updated_at = ? WHERE id = ? AND owner_id = ?", (approval, lifecycle, time.time(), skill_id, str(owner_id)))
            db.execute("INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)", (str(owner_id), "skill", skill_id, "APPROVE" if approved else "REJECT", f"actor={actor_id}", time.time()))
            return db.fetchone("SELECT * FROM installed_skills WHERE id = ? AND owner_id = ?", (skill_id, str(owner_id)))
        row = self._run(op, commit=True)
        return self._skill_dict(row) if row else None

    def delete_skill(self, skill_id: str, owner_id: str) -> bool:
        def op(db):
            row = db.fetchone("SELECT id FROM installed_skills WHERE id = ? AND owner_id = ? FOR UPDATE", (skill_id, str(owner_id)))
            if not row:
                return False
            db.execute("DELETE FROM installed_skills WHERE id = ? AND owner_id = ?", (skill_id, str(owner_id)))
            db.execute("INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)", (str(owner_id), "skill", skill_id, "REMOVE", "", time.time()))
            return True
        return bool(self._run(op, commit=True))

    def get_skill_versions(self, skill_id: str, owner_id: str) -> List[Dict[str, Any]]:
        return self._run(lambda db: db.fetchall("SELECT version, content_hash, content, instructions, manifest_json, installed_at FROM skill_versions WHERE skill_id = ? AND owner_id = ? ORDER BY installed_at DESC", (skill_id, str(owner_id))))

    # Plugin methods keep the existing extension contract on PostgreSQL.  They
    # intentionally do not execute plugin code; they only persist metadata.
    def save_plugin(self, record: Dict[str, Any], owner_id: str) -> Dict[str, Any]:
        now = time.time()
        owner = str(owner_id)
        pid = record["id"]
        def op(db):
            db.execute("""INSERT INTO installed_plugins (id, owner_id, name, version, description, author, license, entrypoint, runtime, permissions, provided_tools, dependencies, install_path, content_hash, is_enabled, manifest_json, created_at, updated_at, trust_level)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id, owner_id) DO UPDATE SET name=EXCLUDED.name, version=EXCLUDED.version, description=EXCLUDED.description, author=EXCLUDED.author, license=EXCLUDED.license, entrypoint=EXCLUDED.entrypoint, runtime=EXCLUDED.runtime, permissions=EXCLUDED.permissions, provided_tools=EXCLUDED.provided_tools, dependencies=EXCLUDED.dependencies, install_path=EXCLUDED.install_path, content_hash=EXCLUDED.content_hash, is_enabled=EXCLUDED.is_enabled, manifest_json=EXCLUDED.manifest_json, updated_at=EXCLUDED.updated_at, trust_level=EXCLUDED.trust_level""",
                (pid, owner, record["name"], record["version"], record["description"], record.get("author", ""), record.get("license", ""), record["entrypoint"], record.get("runtime", "python"), Json(record.get("permissions", [])), Json(record.get("provided_tools", [])), Json(record.get("dependencies", [])), str(record["install_path"]), record["content_hash"], bool(record.get("is_enabled", True)), Json(record.get("manifest_json", {})), now, now, record.get("trust_level", "UNTRUSTED")))
            db.execute("INSERT INTO plugin_versions (plugin_id, owner_id, version, content_hash, install_path, manifest_json, installed_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (pid, owner, record["version"], record["content_hash"], str(record["install_path"]), Json(record.get("manifest_json", {})), now))
            db.execute("INSERT INTO extension_audit_log (owner_id, extension_type, extension_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)", (owner, "plugin", pid, "INSTALL_OR_UPDATE", f"Version {record['version']}", now))
            return db.fetchone("SELECT * FROM installed_plugins WHERE id = ? AND owner_id = ?", (pid, owner))
        return self._run(lambda db: self._plugin_dict(op(db)), commit=True)

    def get_plugin(self, plugin_id: str, owner_id: str, **_kwargs) -> Optional[Dict[str, Any]]:
        return self._run(lambda db: (lambda row: self._plugin_dict(row) if row else None)(db.fetchone("SELECT * FROM installed_plugins WHERE id = ? AND (owner_id = ? OR owner_id = '')", (plugin_id, str(owner_id)))))

    def list_plugins(self, owner_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM installed_plugins WHERE (owner_id = ? OR owner_id = '')"
        if enabled_only:
            query += " AND is_enabled = TRUE"
        return self._run(lambda db: [self._plugin_dict(row) for row in db.fetchall(query + " ORDER BY name ASC", (str(owner_id),))])

    def set_plugin_enabled(self, plugin_id: str, enabled: bool, owner_id: str) -> Optional[Dict[str, Any]]:
        self._run(lambda db: db.execute("UPDATE installed_plugins SET is_enabled = ?, updated_at = ? WHERE id = ? AND owner_id = ?", (enabled, time.time(), plugin_id, str(owner_id))), commit=True)
        return self.get_plugin(plugin_id, owner_id)

    def delete_plugin(self, plugin_id: str, owner_id: str) -> bool:
        self._run(lambda db: db.execute("DELETE FROM installed_plugins WHERE id = ? AND owner_id = ?", (plugin_id, str(owner_id))), commit=True)
        return True

    def get_plugin_versions(self, plugin_id: str, owner_id: str) -> List[Dict[str, Any]]:
        return self._run(lambda db: db.fetchall("SELECT version, content_hash, install_path, manifest_json, installed_at FROM plugin_versions WHERE plugin_id = ? AND (owner_id = ? OR owner_id = '') ORDER BY installed_at DESC", (plugin_id, str(owner_id))))
