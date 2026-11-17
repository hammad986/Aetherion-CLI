"""Deterministic Phase 5.1 publish/selection boundary tests.

These tests use the isolated extension SQLite harness and do not require
PostgreSQL, a live tenant, network access, or agent execution.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from execution.extensions import ExtensionManager, ExtensionSecurityException, ExtensionStore, SkillEngine


def _manager(tmp_path: Path) -> ExtensionManager:
    return ExtensionManager(
        store=ExtensionStore(tmp_path / "extensions.db"),
        base_install_dir=tmp_path / "data",
    )


def _content(skill_id: str = "published-demo", *, trigger: str = "demo", version: str = "1.0.0") -> str:
    return (
        "---\n"
        f"name: {skill_id}\n"
        f"version: {version}\n"
        f"triggers:\n  - {trigger}\n"
        "---\n\n"
        "This is untrusted reference guidance only.\n"
    )


def _publish(manager: ExtensionManager, owner: str = "alice", content: str | None = None, **kwargs):
    content = content or _content()
    return manager.publish_skill_definition(
        owner_id=owner,
        workspace_id=owner,
        published_by=owner,
        skill_id=kwargs.pop("skill_id", "published-demo"),
        name=kwargs.pop("name", "Published Demo"),
        description="Published reference guidance",
        version=kwargs.pop("version", "1.0.0"),
        content=content,
        client_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        **kwargs,
    )


def test_publish_is_tenant_scoped_idempotent_and_not_enabled(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = _publish(manager)
    assert record["publish_outcome"] == "CREATED"
    assert record["status"] == "published_to_tenant"
    assert record["is_enabled"] is False
    assert record["source_kind"] == "authenticated_publish"
    assert record["owner_id"] == "alice"
    assert manager.store.get_skill("published-demo", "bob") is None

    repeat = _publish(manager)
    assert repeat["publish_outcome"] == "ALREADY_CURRENT"


def test_publish_rejects_digest_conflict_and_frontmatter_escalation(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    content = _content()
    with pytest.raises(ValueError, match="digest"):
        manager.publish_skill_definition(
            owner_id="alice", workspace_id="alice", published_by="alice",
            skill_id="published-demo", name="Published Demo", description="x",
            version="1.0.0", content=content, client_sha256="0" * 64,
        )

    with pytest.raises(ValueError, match="cannot grant"):
        manager.publish_skill_definition(
            owner_id="alice", workspace_id="alice", published_by="alice",
            skill_id="dangerous-demo", name="Dangerous Demo", description="x",
            version="1.0.0", content="---\nrequired_tools: [run_command]\n---\n\nunsafe guidance\n",
        )

    _publish(manager)
    with pytest.raises(ExtensionSecurityException, match="version conflict"):
        _publish(manager, content=_content(trigger="changed", version="1.0.1"), version="1.0.1")


def test_approval_enable_and_closed_runtime_selection(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _publish(manager)
    assert manager.resolve_runtime_skill_selection("alice", []) == []

    with pytest.raises(ExtensionSecurityException):
        manager.resolve_runtime_skill_selection("alice", ["published-demo"])

    approved = manager.store.get_skill("published-demo", "alice")
    assert approved and approved["approval_status"] == "NOT_REQUIRED"
    assert manager.store.set_skill_enabled("published-demo", True, "alice")
    selected = manager.resolve_runtime_skill_selection("alice", ["published-demo"])
    assert [item["id"] for item in selected] == ["published-demo"]

    with pytest.raises(ExtensionSecurityException):
        manager.resolve_runtime_skill_selection("bob", ["published-demo"])
    with pytest.raises(ValueError, match="unique"):
        manager.resolve_runtime_skill_selection("alice", ["published-demo", "published-demo"])

    engine = SkillEngine(manager)
    assert [item["id"] for item in engine.match_and_activate("unrelated", "alice")] == []
    assert [item["id"] for item in engine.match_and_activate("unrelated", "alice", explicit_skill_ids=["published-demo"])] == ["published-demo"]


def test_permission_request_is_pending_and_not_runtime_selectable(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = _publish(manager, requested_permissions=["FILESYSTEM_READ"])
    assert record["status"] == "pending_approval"
    assert record["approval_status"] == "PENDING"
    assert manager.store.set_skill_enabled("published-demo", True, "alice") is None
    manager.approve_published_skill("published-demo", "alice", "admin-1")
    manager.store.set_skill_enabled("published-demo", True, "alice")
    with pytest.raises(ExtensionSecurityException, match="requesting permissions"):
        manager.resolve_runtime_skill_selection("alice", ["published-demo"])
