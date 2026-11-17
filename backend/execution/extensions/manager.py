"""
backend/execution/extensions/manager.py — Core lifecycle orchestrator for Plugins and Skills.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .fetcher import SourceFetcher
from .models import (
    ALL_PERMISSIONS,
    ExtensionStatus,
    ExtensionTrustLevel,
    HealthStatus,
    PermissionEnum,
    PluginManifest,
    SkillManifest,
)
from .security import (
    ExtensionSecurityException,
    analyze_permissions,
    calculate_content_hash,
)
from .store import ExtensionStore, PublishConflict, get_extension_store

logger = logging.getLogger("aetherion.extensions.manager")

DEFAULT_EXTENSIONS_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MAX_PUBLISHED_SKILL_BYTES = 512 * 1024
CANONICAL_SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
RESERVED_PUBLISHED_SKILL_IDS = {
    "system", "default", "core", "admin", "root", "finish", "run_command",
    "refactor_safe", "unit_test_generator", "diagnostic_repair", "config_updater",
}


class ExtensionManager:
    """Manages secure lifecycle, isolation, validation, and execution of plugins and skills."""

    def __init__(
        self,
        store: Optional[ExtensionStore] = None,
        base_install_dir: Optional[Path | str] = None,
        fetcher: Optional[SourceFetcher] = None,
    ):
        self.store = store or get_extension_store()
        self.base_dir = Path(base_install_dir or DEFAULT_EXTENSIONS_DIR).resolve()
        self.plugins_dir = self.base_dir / "plugins"
        self.skills_dir = self.base_dir / "skills"
        self.versions_dir = self.base_dir / "versions"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.fetcher = fetcher or SourceFetcher()
        self._pending_previews: Dict[str, Dict[str, Any]] = {}
        self.preview_ttl_seconds = 15 * 60

    @staticmethod
    def _validate_published_text(
        value: Any,
        field: str,
        limit: int,
        *,
        required: bool = False,
        allow_line_breaks: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be text")
        if required and not value.strip():
            raise ValueError(f"{field} is required")
        if len(value.encode("utf-8")) > limit:
            raise ValueError(f"{field} exceeds its size limit")
        allowed_whitespace = "\n\r\t" if allow_line_breaks else ""
        if any(
            (ord(char) < 32 and char not in allowed_whitespace)
            or ord(char) == 0x7F
            or 0x80 <= ord(char) <= 0x9F
            for char in value
        ):
            raise ValueError(f"{field} contains unsupported control characters")
        return value

    @classmethod
    def _parse_published_markdown(cls, content: str, skill_id: str) -> tuple[dict[str, Any], str]:
        """Parse the deliberately small publish frontmatter grammar.

        This avoids executing or importing user content and rejects duplicate
        keys instead of letting a YAML parser silently choose one value.
        """
        content = cls._validate_published_text(content, "content", MAX_PUBLISHED_SKILL_BYTES, required=True, allow_line_breaks=True)
        fields: dict[str, Any] = {}
        body = content
        allowed = {"id", "name", "version", "description", "author", "triggers", "tags"}
        forbidden = {"tools", "required_tools", "permissions", "required_permissions", "execution", "packages", "url"}
        if content.startswith("---\n") or content.startswith("---\r\n"):
            lines = content.splitlines(keepends=True)
            end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
            if end is None:
                raise ValueError("content frontmatter is missing its closing delimiter")
            current_list: Optional[str] = None
            for raw in lines[1:end]:
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                if line.startswith("  -") and current_list:
                    fields.setdefault(current_list, []).append(line[3:].strip().strip("'\""))
                    continue
                if ":" not in line or line.startswith(" "):
                    raise ValueError("content frontmatter contains an invalid field")
                key, raw_value = line.split(":", 1)
                key = key.strip()
                if key in fields:
                    raise ValueError(f"content frontmatter repeats field '{key}'")
                if key in forbidden:
                    raise ValueError(f"content frontmatter cannot grant '{key}'")
                if key not in allowed:
                    raise ValueError(f"content frontmatter field '{key}' is not supported")
                value = raw_value.strip().strip("'\"")
                if value:
                    fields[key] = value
                    current_list = None
                else:
                    fields[key] = []
                    current_list = key
            body = "".join(lines[end + 1:]).lstrip("\r\n")

        declared_id = str(fields.get("id") or "").strip().lower()
        if declared_id and declared_id != skill_id:
            raise ValueError("content frontmatter ID does not match the selected canonical skill ID")
        fields["id"] = skill_id
        return fields, cls._validate_published_text(body, "instructions", MAX_PUBLISHED_SKILL_BYTES, required=True, allow_line_breaks=True)

    @staticmethod
    def _frontmatter_list(fields: dict[str, Any], key: str) -> List[str]:
        value = fields.get(key, [])
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"content frontmatter field '{key}' must be a list of strings")
        return [item.strip() for item in value if item.strip()]

    def _published_manifest(
        self,
        *,
        skill_id: str,
        name: str,
        description: str,
        version: str,
        content: str,
        requested_permissions: Optional[List[str]],
    ) -> tuple[SkillManifest, str, str]:
        if not CANONICAL_SKILL_ID.fullmatch(skill_id) or skill_id in RESERVED_PUBLISHED_SKILL_IDS:
            raise ValueError("skill_id is not an allowed publishable canonical ID")
        fields, instructions = self._parse_published_markdown(content, skill_id)
        for field, supplied in (("name", name), ("description", description), ("version", version)):
            declared = fields.get(field)
            # Older Skills Library documents use the canonical ID as a
            # shorthand name while the request carries the display name.  It
            # does not introduce a second semantic name; all other explicit
            # declarations must match exactly.
            if field == "name" and declared == skill_id:
                continue
            if declared not in (None, "") and str(supplied) != str(declared):
                raise ValueError(f"content frontmatter {field} does not match request metadata")
        name = self._validate_published_text(name or str(fields.get("name") or skill_id), "name", 256, required=True)
        description = self._validate_published_text(description or str(fields.get("description") or ""), "description", 2_000)
        version = self._validate_published_text(version or str(fields.get("version") or "1.0.0"), "version", 64, required=True)
        permissions = list(requested_permissions or [])
        if any(not isinstance(permission, str) or permission not in ALL_PERMISSIONS for permission in permissions):
            raise ExtensionSecurityException("requested_permissions contains an unsupported permission")
        manifest = SkillManifest.from_dict({
            "id": skill_id,
            "name": name,
            "version": version,
            "description": description,
            "author": str(fields.get("author") or "workspace owner"),
            "instructions": instructions,
            "required_tools": [],
            "required_permissions": sorted(set(permissions)),
            "tags": self._frontmatter_list(fields, "tags"),
            "activation_triggers": self._frontmatter_list(fields, "triggers"),
        })
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return manifest, content, digest

    # ── Inspection & Pre-Install Preview ───────────────────────────────────────

    def inspect_plugin(self, source: str, owner_id: str) -> Dict[str, Any]:
        """Fetch, validate manifest, analyze permissions, and generate install preview."""
        source_dir, content_hash, raw_manifest = self.fetcher.fetch_source(source, extension_type="plugin")
        manifest = PluginManifest.from_dict(raw_manifest)
        perm_analysis = analyze_permissions(manifest.required_permissions)

        preview_id = f"prev_{uuid.uuid4().hex[:12]}"
        preview = {
            "preview_id": preview_id,
            "extension_type": "plugin",
            "source": source,
            "source_dir": str(source_dir),
            "content_hash": content_hash,
            "manifest": manifest.to_dict(),
            "permissions_requested": perm_analysis["valid_permissions"],
            "risk_level": perm_analysis["risk_level"],
            "requires_explicit_approval": perm_analysis["requires_explicit_approval"],
            "critical_flags": perm_analysis["critical_flags"],
            "owner_id": str(owner_id),
            "created_at": time.time(),
            "status": "READY_FOR_APPROVAL",
        }
        self._pending_previews[preview_id] = preview
        return preview

    def inspect_skill(self, source: str, owner_id: str) -> Dict[str, Any]:
        """Fetch, validate skill manifest, and generate install preview."""
        source_dir, content_hash, raw_manifest = self.fetcher.fetch_source(source, extension_type="skill")
        manifest = SkillManifest.from_dict(raw_manifest)
        perm_analysis = analyze_permissions(manifest.required_permissions)

        preview_id = f"prev_{uuid.uuid4().hex[:12]}"
        preview = {
            "preview_id": preview_id,
            "extension_type": "skill",
            "source": source,
            "source_dir": str(source_dir),
            "content_hash": content_hash,
            "manifest": manifest.to_dict(),
            "permissions_requested": perm_analysis["valid_permissions"],
            "risk_level": perm_analysis["risk_level"],
            "requires_explicit_approval": perm_analysis["requires_explicit_approval"],
            "critical_flags": perm_analysis["critical_flags"],
            "owner_id": str(owner_id),
            "created_at": time.time(),
            "status": "READY_FOR_APPROVAL",
        }
        self._pending_previews[preview_id] = preview
        return preview

    # ── Installation Pipeline ─────────────────────────────────────────────────

    def install_plugin(
        self,
        preview_id_or_source: str,
        owner_id: str,
        approved_permissions: Optional[List[str]] = None,
        auto_approve: bool = False,
        trust_level: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Install a validated plugin into isolated directory with permission granting."""
        if preview_id_or_source in self._pending_previews:
            preview = self._pending_previews[preview_id_or_source]
        else:
            preview = self.inspect_plugin(preview_id_or_source, owner_id)

        manifest_data = preview["manifest"]
        manifest = PluginManifest.from_dict(manifest_data)
        pid = manifest.id
        owner = str(owner_id)

        # Enforce approval for high/critical permissions
        if preview["requires_explicit_approval"] and not auto_approve and approved_permissions is None:
            raise ExtensionSecurityException(
                f"Plugin '{pid}' requests privileged permissions ({preview['critical_flags']}) "
                f"and requires explicit user approval before installation."
            )

        granted_perms = approved_permissions if approved_permissions is not None else manifest.required_permissions

        # ── Trust level resolution with ceiling enforcement ──────────────────
        # If the caller explicitly sets trust_level, honour it (CLI/API with authority).
        # If the caller does NOT set it, take the manifest's declared level BUT
        # cap it at TRUSTED_LOCAL — a remote manifest cannot self-escalate to SYSTEM.
        if trust_level is not None:
            effective_trust = trust_level
        else:
            manifest_trust = getattr(manifest, "trust_level", ExtensionTrustLevel.UNTRUSTED.value) or ExtensionTrustLevel.UNTRUSTED.value
            if manifest_trust == ExtensionTrustLevel.SYSTEM.value:
                logger.warning(
                    "Plugin '%s' self-declared trust_level=SYSTEM in manifest but caller did not "
                    "grant SYSTEM explicitly — downgrading to TRUSTED_LOCAL for safety.",
                    pid,
                )
                effective_trust = ExtensionTrustLevel.TRUSTED_LOCAL.value
            else:
                effective_trust = manifest_trust

        # Destination in isolated data/plugins/<owner_id>/<plugin_id>/
        plugin_install_dir = self.plugins_dir / owner / pid
        if plugin_install_dir.exists():
            shutil.rmtree(plugin_install_dir)
        plugin_install_dir.mkdir(parents=True, exist_ok=True)

        # Copy source contents safely into isolated installation dir
        source_dir = Path(preview["source_dir"])
        for item in source_dir.iterdir():
            dest = plugin_install_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # Persist manifest file in install dir
        manifest_dict = manifest.to_dict()
        manifest_dict["trust_level"] = effective_trust
        (plugin_install_dir / "plugin.json").write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")
        final_hash = calculate_content_hash(plugin_install_dir)

        # Archive version snapshot for faithful rollback restoration
        version_snapshot_dir = self.versions_dir / "plugins" / owner / pid / manifest.version
        if version_snapshot_dir.exists():
            shutil.rmtree(version_snapshot_dir)
        version_snapshot_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(plugin_install_dir, version_snapshot_dir, dirs_exist_ok=True)

        record = {
            "id": pid,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "license": manifest.license,
            "entrypoint": manifest.entrypoint,
            "runtime": manifest.runtime,
            "permissions": granted_perms,
            "provided_tools": manifest.provided_tools,
            "dependencies": manifest.required_dependencies,
            "install_path": str(plugin_install_dir),
            "content_hash": final_hash,
            "is_enabled": True,
            "trust_level": effective_trust,
            "manifest_json": manifest_dict,
        }
        saved = self.store.save_plugin(record, owner)
        self._pending_previews.pop(preview_id_or_source, None)
        return saved

    def install_skill(
        self,
        preview_id_or_source: str,
        owner_id: str,
        approved_permissions: Optional[List[str]] = None,
        auto_approve: bool = False,
    ) -> Dict[str, Any]:
        """Install a validated skill into isolated storage."""
        if preview_id_or_source in self._pending_previews:
            preview = self._pending_previews[preview_id_or_source]
            if preview.get("extension_type") != "skill":
                raise ExtensionSecurityException("Preview does not describe a skill installation.")
            if str(preview.get("owner_id")) != str(owner_id):
                raise ExtensionSecurityException("Skill installation preview belongs to another owner.")
            if time.time() - float(preview.get("created_at", 0)) > self.preview_ttl_seconds:
                self._pending_previews.pop(preview_id_or_source, None)
                raise ExtensionSecurityException("Skill installation preview has expired.")
        else:
            preview = self.inspect_skill(preview_id_or_source, owner_id)

        manifest_data = preview["manifest"]
        manifest = SkillManifest.from_dict(manifest_data)
        sid = manifest.id
        owner = str(owner_id)

        perm_analysis = analyze_permissions(manifest.required_permissions)
        if perm_analysis["requires_explicit_approval"] and not auto_approve and approved_permissions is None:
            raise ExtensionSecurityException(
                f"Skill '{sid}' requests privileged permissions ({perm_analysis['critical_flags']}) "
                "and requires explicit user approval before installation."
            )

        requested_perms = set(manifest.required_permissions)
        if approved_permissions is not None:
            approved_set = {str(permission) for permission in approved_permissions}
            if not approved_set.issubset(requested_perms):
                raise ExtensionSecurityException("Approved skill permissions must be a subset of requested permissions.")
            granted_perms = sorted(approved_set)
        else:
            granted_perms = sorted(requested_perms)

        # Destination in isolated data/skills/<owner_id>/<skill_id>/
        skill_install_dir = self.skills_dir / owner / sid
        if skill_install_dir.exists():
            shutil.rmtree(skill_install_dir)
        skill_install_dir.mkdir(parents=True, exist_ok=True)

        (skill_install_dir / "skill.json").write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
        (skill_install_dir / "SKILL.md").write_text(manifest.instructions, encoding="utf-8")
        final_hash = calculate_content_hash(skill_install_dir)

        record = {
            "id": sid,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "instructions": manifest.instructions,
            "content": manifest.instructions,
            "required_tools": manifest.required_tools,
            "permissions": granted_perms,
            "tags": manifest.tags,
            "triggers": manifest.activation_triggers,
            "is_enabled": True,
            "install_path": str(skill_install_dir),
            "content_hash": final_hash,
            "manifest_json": manifest.to_dict(),
        }
        saved = self.store.save_skill(record, owner)
        self._pending_previews.pop(preview_id_or_source, None)
        return saved

    def publish_skill_definition(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        published_by: str,
        skill_id: str,
        name: str,
        description: str,
        version: str,
        content: str,
        requested_permissions: Optional[List[str]] = None,
        client_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist one authenticated, data-only skill definition.

        The caller supplies content, never a server filesystem path. Publishing
        does not enable the skill and does not execute or install any asset.
        """
        manifest, canonical_content, digest = self._published_manifest(
            skill_id=skill_id,
            name=name,
            description=description,
            version=version,
            content=content,
            requested_permissions=requested_permissions,
        )
        if client_sha256 and (not re.fullmatch(r"[0-9a-fA-F]{64}", client_sha256) or client_sha256.lower() != digest):
            raise ValueError("client_sha256 does not match the server-computed content digest")

        approval_status = "PENDING" if manifest.required_permissions else "NOT_REQUIRED"
        lifecycle_status = "PUBLISHED"
        record = {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "instructions": manifest.instructions,
            "required_tools": [],
            "permissions": manifest.required_permissions,
            "tags": manifest.tags,
            "triggers": manifest.activation_triggers,
            "is_enabled": False,
            "install_path": "",
            "content_hash": digest,
            "content": canonical_content,
            "manifest_json": manifest.to_dict(),
            "lifecycle_status": lifecycle_status,
            "approval_status": approval_status,
            "published_at": time.time(),
            "published_by": str(published_by),
            "workspace_id": str(workspace_id),
            "source_kind": "authenticated_publish",
        }
        saved, outcome = self.extension_store_publish(record, owner_id)
        saved["publish_outcome"] = outcome
        saved["status"] = "pending_approval" if approval_status == "PENDING" else "published_to_tenant"
        saved["client_sha256_verified"] = bool(client_sha256)
        return saved

    def extension_store_publish(self, record: Dict[str, Any], owner_id: str) -> tuple[Dict[str, Any], str]:
        """Publish through the existing store with idempotent conflict semantics."""
        try:
            if hasattr(self.store, "publish_skill"):
                return self.store.publish_skill(record, str(owner_id))
            existing = self.store.get_skill(record["id"], str(owner_id))
            if existing:
                if existing.get("version") == record["version"] and existing.get("content_hash") == record["content_hash"]:
                    return existing, "ALREADY_CURRENT"
                raise PublishConflict("A different published version already exists", same_content=False)
            saved = self.store.save_skill(record, str(owner_id))
            return saved, "CREATED"
        except PublishConflict as exc:
            raise ExtensionSecurityException(str(exc)) from exc

    def approve_published_skill(self, skill_id: str, owner_id: str, actor_id: str) -> Optional[Dict[str, Any]]:
        existing = self.store.get_skill(skill_id, owner_id)
        if not existing or existing.get("source_kind") != "authenticated_publish":
            return None
        updated = self.store.set_skill_approval(skill_id, owner_id, approved=True, actor_id=actor_id)
        if updated:
            updated["status"] = "approved_but_not_selected"
        return updated

    def detach_published_skill(self, skill_id: str, owner_id: str) -> bool:
        existing = self.store.get_skill(skill_id, owner_id)
        if not existing or existing.get("source_kind") != "authenticated_publish":
            return False
        return self.store.delete_skill(skill_id, owner_id)

    def resolve_runtime_skill_selection(
        self,
        owner_id: str,
        skill_ids: Sequence[str],
        expected_skill_digests: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        requested = [str(item).strip() for item in skill_ids if str(item).strip()]
        if len(requested) > 10 or len(set(requested)) != len(requested):
            raise ValueError("selected_skill_ids must contain at most 10 unique IDs")
        if expected_skill_digests is not None:
            expected = list(expected_skill_digests)
            if len(expected) != len(requested) or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", item) for item in expected):
                raise ExtensionSecurityException("selected_skill_digests is malformed")
        else:
            expected = []
        records = {item["id"]: item for item in self.store.list_skills(owner_id, enabled_only=False)}
        if any(item not in records for item in requested):
            raise ExtensionSecurityException("one or more selected skills are unavailable, unapproved, disabled, or owned by another tenant")
        for index, skill_id in enumerate(requested):
            record = records[skill_id]
            if record.get("record_corrupt"):
                raise ExtensionSecurityException("selected skill metadata is corrupt")
            if expected and record.get("content_hash", "").lower() != expected[index].lower():
                raise ExtensionSecurityException("selected skill content digest no longer matches the task snapshot")
        published_ids = [item for item in requested if records[item].get("source_kind") == "authenticated_publish"]
        selected = self.store.list_runtime_skills(owner_id, published_ids) if published_ids else []
        if len(selected) != len(published_ids):
            raise ExtensionSecurityException("one or more published skills are unavailable, unapproved, or disabled")
        if any(item.get("permissions") for item in selected):
            raise ExtensionSecurityException("published skills requesting permissions are not runtime-selectable under the metadata-only policy")
        selected_by_id = {item["id"]: item for item in selected}
        for skill_id in requested:
            record = records[skill_id]
            if record.get("source_kind") != "authenticated_publish":
                if not record.get("is_enabled"):
                    raise ExtensionSecurityException(f"skill '{skill_id}' is disabled")
                selected_by_id[skill_id] = record
        selected = [selected_by_id[skill_id] for skill_id in requested]
        if sum(len(str(item.get("instructions", ""))) for item in selected) > 12_000:
            raise ValueError("selected skill guidance exceeds the runtime content budget")
        return selected

    def resolve_runtime_skill_selection_in_workspace(
        self,
        workspace_id: str,
        skill_ids: Sequence[str],
        expected_skill_digests: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Resolve an explicit selection against the canonical workspace.

        This variant is used by task creation when a member selects a skill
        published by another member of the same authorized workspace.
        """
        requested = [str(item).strip() for item in skill_ids if str(item).strip()]
        if len(requested) > 10 or len(set(requested)) != len(requested):
            raise ValueError("selected_skill_ids must contain at most 10 unique IDs")
        expected = list(expected_skill_digests or [])
        if expected_skill_digests is not None and (
            len(expected) != len(requested)
            or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", item) for item in expected)
        ):
            raise ExtensionSecurityException("selected_skill_digests is malformed")
        records = {item["id"]: item for item in self.list_skills_in_workspace(workspace_id, enabled_only=False)}
        if any(item not in records for item in requested):
            raise ExtensionSecurityException("one or more selected skills are unavailable in this workspace")
        for index, skill_id in enumerate(requested):
            record = records[skill_id]
            if record.get("record_corrupt"):
                raise ExtensionSecurityException("selected skill metadata is corrupt")
            if expected_skill_digests is not None and record.get("content_hash", "").lower() != expected[index].lower():
                raise ExtensionSecurityException("selected skill content digest no longer matches the task snapshot")
        published_ids = [item for item in requested if records[item].get("source_kind") == "authenticated_publish"]
        selected = self.store.list_runtime_skills_by_workspace(workspace_id, published_ids) if published_ids else []
        if len(selected) != len(published_ids):
            raise ExtensionSecurityException("one or more published skills are unavailable, unapproved, or disabled")
        if any(item.get("record_corrupt") or item.get("permissions") for item in selected):
            raise ExtensionSecurityException("published skills with corrupt or permission-bearing metadata are not runtime-selectable")
        selected_by_id = {item["id"]: item for item in selected}
        for skill_id in requested:
            record = records[skill_id]
            if record.get("source_kind") != "authenticated_publish":
                if not record.get("is_enabled"):
                    raise ExtensionSecurityException(f"skill '{skill_id}' is disabled")
                selected_by_id[skill_id] = record
        result = [selected_by_id[skill_id] for skill_id in requested]
        if sum(len(str(item.get("instructions", ""))) for item in result) > 12_000:
            raise ValueError("selected skill guidance exceeds the runtime content budget")
        return result

    def get_skill_in_workspace(self, skill_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        if hasattr(self.store, "get_skill_by_workspace"):
            return self.store.get_skill_by_workspace(skill_id, workspace_id)
        return None

    def list_skills_in_workspace(self, workspace_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
        if hasattr(self.store, "list_skills_by_workspace"):
            return self.store.list_skills_by_workspace(workspace_id, enabled_only=enabled_only)
        return []

    def approve_published_skill_in_workspace(self, skill_id: str, workspace_id: str, actor_id: str) -> Optional[Dict[str, Any]]:
        existing = self.get_skill_in_workspace(skill_id, workspace_id)
        if not existing or existing.get("source_kind") != "authenticated_publish":
            return None
        updated = self.store.set_skill_approval(skill_id, existing["owner_id"], approved=True, actor_id=actor_id)
        if updated:
            updated["status"] = "approved_but_not_selected"
        return updated

    def set_skill_enabled_in_workspace(self, skill_id: str, workspace_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        existing = self.get_skill_in_workspace(skill_id, workspace_id)
        if not existing:
            return None
        return self.store.set_skill_enabled(skill_id, owner_id=existing["owner_id"], enabled=enabled)

    def detach_published_skill_in_workspace(self, skill_id: str, workspace_id: str) -> bool:
        existing = self.get_skill_in_workspace(skill_id, workspace_id)
        if not existing or existing.get("source_kind") != "authenticated_publish":
            return False
        return self.store.delete_skill(skill_id, existing["owner_id"])

    # ── Enable / Disable Semantics ─────────────────────────────────────────────

    def enable_plugin(self, plugin_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        return self.store.set_plugin_enabled(plugin_id, True, owner_id)

    def disable_plugin(self, plugin_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        return self.store.set_plugin_enabled(plugin_id, False, owner_id)

    def enable_skill(self, skill_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        return self.store.set_skill_enabled(skill_id, True, owner_id)

    def disable_skill(self, skill_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        return self.store.set_skill_enabled(skill_id, False, owner_id)

    # ── Removal Safety ────────────────────────────────────────────────────────

    def remove_plugin(self, plugin_id: str, owner_id: str) -> bool:
        """Safely uninstall a plugin and remove its isolated directory."""
        existing = self.store.get_plugin(plugin_id, owner_id)
        if not existing:
            return False

        install_path = Path(existing["install_path"])
        if install_path.exists():
            try:
                shutil.rmtree(install_path)
            except Exception as exc:
                logger.warning("Failed to remove plugin directory %s: %s", install_path, exc)

        return self.store.delete_plugin(plugin_id, owner_id)

    def remove_skill(self, skill_id: str, owner_id: str) -> bool:
        """Safely uninstall a skill and clean up files."""
        existing = self.store.get_skill(skill_id, owner_id)
        if not existing:
            return False

        if existing.get("install_path"):
            p = Path(existing["install_path"])
            if p.exists():
                try:
                    shutil.rmtree(p)
                except Exception as exc:
                    logger.warning("Failed to remove skill directory %s: %s", p, exc)

        return self.store.delete_skill(skill_id, owner_id)

    # ── Update & Rollback ─────────────────────────────────────────────────────

    def update_plugin(self, plugin_id: str, new_source: str, owner_id: str, auto_approve: bool = False) -> Dict[str, Any]:
        """Update an existing plugin with rollback preservation on health check failure."""
        existing = self.store.get_plugin(plugin_id, owner_id)
        if not existing:
            raise ValueError(f"Plugin '{plugin_id}' not found.")

        old_version = existing["version"]
        preview = self.inspect_plugin(new_source, owner_id)
        new_manifest = PluginManifest.from_dict(preview["manifest"])

        if new_manifest.id != plugin_id:
            raise ValueError(f"Updated plugin ID '{new_manifest.id}' does not match existing ID '{plugin_id}'.")

        # Check for expanded permissions
        old_perms = set(existing.get("permissions", []))
        new_perms = set(new_manifest.required_permissions)
        expanded = new_perms - old_perms

        if expanded and not auto_approve:
            raise ExtensionSecurityException(
                f"Update requires expanded permissions: {sorted(expanded)}. Explicit approval required."
            )

        updated = self.install_plugin(preview["preview_id"], owner_id, auto_approve=True)

        # Health check validation
        health = self.health_check_plugin(plugin_id, owner_id)
        if health["status"] not in (HealthStatus.VALID.value, HealthStatus.DISABLED.value):
            logger.error("Updated plugin failed health check: %s. Rolling back to %s.", health, old_version)
            self.rollback_plugin(plugin_id, old_version, owner_id)
            raise ValueError(f"Update failed health check ({health.get('diagnostics')}). Rolled back to version {old_version}.")

        return updated

    def rollback_plugin(self, plugin_id: str, target_version: str, owner_id: str) -> Optional[Dict[str, Any]]:
        """Rollback a plugin to a prior version from the version snapshot log."""
        versions = self.store.get_plugin_versions(plugin_id, owner_id)
        target_snapshot = next((v for v in versions if v["version"] == target_version), None)
        if not target_snapshot:
            return None

        # Re-apply version in store
        manifest_data = json.loads(target_snapshot.get("manifest_json", "{}"))
        if manifest_data:
            manifest = PluginManifest.from_dict(manifest_data)
            inst_p = Path(target_snapshot["install_path"])

            # Restore physical files from version snapshot if available
            snapshot_dir = self.versions_dir / "plugins" / owner_id / plugin_id / target_version
            if snapshot_dir.exists():
                if inst_p.exists():
                    shutil.rmtree(inst_p)
                inst_p.mkdir(parents=True, exist_ok=True)
                shutil.copytree(snapshot_dir, inst_p, dirs_exist_ok=True)

            record = {
                "id": plugin_id,
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "author": manifest.author,
                "license": manifest.license,
                "entrypoint": manifest.entrypoint,
                "runtime": manifest.runtime,
                "permissions": manifest.required_permissions,
                "provided_tools": manifest.provided_tools,
                "dependencies": manifest.required_dependencies,
                "install_path": str(inst_p),
                "content_hash": target_snapshot["content_hash"],
                "is_enabled": True,
                "trust_level": getattr(manifest, "trust_level", ExtensionTrustLevel.UNTRUSTED.value),
                "manifest_json": manifest.to_dict(),
            }
            self.store.save_plugin(record, owner_id)
            self.store.log_audit(owner_id, "plugin", plugin_id, "ROLLBACK", f"Rolled back to {target_version}")
            return self.store.get_plugin(plugin_id, owner_id)
        return None

    def rollback_skill(self, skill_id: str, target_version: str, owner_id: str) -> Optional[Dict[str, Any]]:
        """Rollback a skill to a prior version from snapshot."""
        versions = self.store.get_skill_versions(skill_id, owner_id)
        target_snapshot = next((v for v in versions if v["version"] == target_version), None)
        if not target_snapshot:
            return None

        manifest_data = json.loads(target_snapshot.get("manifest_json", "{}"))
        if manifest_data:
            manifest = SkillManifest.from_dict(manifest_data)
            skill_dir = self.skills_dir / str(owner_id) / str(skill_id)
            if skill_dir.exists():
                (skill_dir / "skill.json").write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
                (skill_dir / "SKILL.md").write_text(manifest.instructions, encoding="utf-8")

            record = {
                "id": skill_id,
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "author": manifest.author,
                "instructions": manifest.instructions,
                "required_tools": manifest.required_tools,
                "permissions": manifest.required_permissions,
                "tags": manifest.tags,
                "triggers": manifest.activation_triggers,
                "is_enabled": True,
                "install_path": str(skill_dir) if skill_dir.exists() else "",
                "content_hash": target_snapshot["content_hash"],
                "manifest_json": manifest.to_dict(),
            }
            self.store.save_skill(record, owner_id)
            self.store.log_audit(owner_id, "skill", skill_id, "ROLLBACK", f"Rolled back to {target_version}")
            return self.store.get_skill(skill_id, owner_id)
        return None

    # ── Health Diagnostics ────────────────────────────────────────────────────

    def health_check_plugin(self, plugin_id: str, owner_id: str) -> Dict[str, Any]:
        """Perform diagnostic health check on installed plugin."""
        plugin = self.store.get_plugin(plugin_id, owner_id)
        if not plugin:
            return {"status": HealthStatus.INVALID.value, "diagnostics": f"Plugin '{plugin_id}' not found."}

        if not plugin.get("is_enabled"):
            return {"status": HealthStatus.DISABLED.value, "diagnostics": "Plugin is currently disabled."}

        install_path = Path(plugin["install_path"])
        if not install_path.exists():
            return {"status": HealthStatus.INVALID.value, "diagnostics": "Installation directory is missing."}

        entrypoint = plugin.get("entrypoint", "")
        entry_file = install_path / entrypoint.split(":")[0]
        if not entry_file.exists():
            return {
                "status": HealthStatus.INVALID.value,
                "diagnostics": f"Entrypoint file '{entry_file.name}' not found in installation path.",
            }

        # Check dependencies importability if declared
        deps = plugin.get("dependencies", [])
        missing = []
        for dep in deps:
            pkg_name = dep.split("==")[0].split(">=")[0].split("<=")[0].strip()
            if pkg_name and importlib.util.find_spec(pkg_name) is None:
                missing.append(pkg_name)

        if missing:
            return {
                "status": HealthStatus.MISSING_DEPENDENCY.value,
                "diagnostics": f"Missing python dependencies: {', '.join(missing)}",
                "missing": missing,
            }

        return {
            "status": HealthStatus.VALID.value,
            "diagnostics": f"Plugin '{plugin['name']}' v{plugin['version']} verified operational.",
            "tools_count": len(plugin.get("provided_tools", [])),
            "trust_level": plugin.get("trust_level", ExtensionTrustLevel.UNTRUSTED.value),
        }

    def health_check_skill(self, skill_id: str, owner_id: str) -> Dict[str, Any]:
        """Perform health check on installed skill."""
        skill = self.store.get_skill(skill_id, owner_id)
        if not skill:
            return {"status": HealthStatus.INVALID.value, "diagnostics": f"Skill '{skill_id}' not found."}

        if not skill.get("is_enabled"):
            return {"status": HealthStatus.DISABLED.value, "diagnostics": "Skill is disabled."}

        if not skill.get("instructions") or len(skill["instructions"].strip()) < 10:
            return {"status": HealthStatus.INVALID.value, "diagnostics": "Skill instructions are missing or empty."}

        return {
            "status": HealthStatus.VALID.value,
            "diagnostics": f"Skill '{skill['name']}' operational with {len(skill.get('instructions', ''))} bytes of guidance.",
        }

    # ── Retrieval & Search ────────────────────────────────────────────────────

    def get_plugin(self, plugin_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get_plugin(plugin_id, owner_id)

    def list_plugins(self, owner_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
        return self.store.list_plugins(owner_id, enabled_only=enabled_only)

    def get_skill(self, skill_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get_skill(skill_id, owner_id)

    def list_skills(self, owner_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
        return self.store.list_skills(owner_id, enabled_only=enabled_only)
