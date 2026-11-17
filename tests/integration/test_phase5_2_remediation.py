"""Phase 5.2 security regressions using isolated SQLite/auth fixtures.

The PostgreSQL repository is covered by the same contract in CI when a shared
database is provisioned; this file deliberately does not start a database.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import tempfile
from pathlib import Path

import pytest


_ROOT = Path(tempfile.mkdtemp(prefix="aetherion-phase52-"))
os.environ["DATABASE_URL"] = ""
os.environ["SQLITE_DB_PATH"] = str(_ROOT / "auth.db")
os.environ["AETHERION_EXTENSIONS_DB"] = str(_ROOT / "extensions.db")
os.environ.setdefault("ENVIRONMENT", "testing")

from auth_system import auth_signup  # noqa: E402
from auth_system import create_session, execute_query  # noqa: E402
from app import create_app  # noqa: E402
from execution.extensions import ExtensionManager, ExtensionSecurityException, ExtensionStore  # noqa: E402
from routes import platform_extensions  # noqa: E402
from workspace_auth import create_workspace, require_workspace, set_membership  # noqa: E402


def _content(skill_id: str = "phase52-demo", version: str = "1.0.0", trigger: str = "demo") -> str:
    return (
        "---\n"
        f"name: {skill_id}\n"
        f"version: {version}\n"
        f"triggers:\n  - {trigger}\n"
        "---\n\n"
        "Untrusted reference guidance only.\n"
    )


def _manager(tmp_path: Path) -> ExtensionManager:
    return ExtensionManager(store=ExtensionStore(tmp_path / "extensions.db"), base_install_dir=tmp_path / "data")


def _publish(manager: ExtensionManager, *, owner: str, workspace: str, skill_id: str = "phase52-demo", content: str | None = None, version: str = "1.0.0", requested_permissions=None):
    content = content or _content(skill_id, version)
    return manager.publish_skill_definition(
        owner_id=owner,
        workspace_id=workspace,
        published_by=owner,
        skill_id=skill_id,
        name="Phase 5.2 Demo",
        description="Reference guidance",
        version=version,
        content=content,
        requested_permissions=requested_permissions,
        client_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def test_legacy_skill_routes_reject_server_paths_without_reading(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    platform_extensions._extension_manager = manager
    app = create_app()
    ok, user_id = auth_signup(f"legacy-{os.urandom(4).hex()}@example.test", "Password123!")
    assert ok
    token = create_session(user_id)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        str(Path(__file__).resolve()),
        "../backend/auth_system.py",
        r"C:\Windows\win.ini",
        r"\\127.0.0.1\share\skill.md",
        "file:///etc/hosts",
        "https://example.test/skill.md",
    ]
    with app.test_client() as client:
        for source in candidates:
            for path in ("/api/skills/inspect", "/api/skills/install", "/api/skills"):
                response = client.post(path, json={"source": source, "path": source, "url": source}, headers=headers)
                assert response.status_code == 410
                body = response.get_json()
                assert body["ok"] is False
                assert body["error"]["code"] == "LEGACY_SOURCE_DISABLED"
                assert source not in response.get_data(as_text=True)
    assert manager.store.list_skills(str(user_id)) == []


def test_old_admin_token_cannot_approve_after_authoritative_demotion(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    platform_extensions._extension_manager = manager
    app = create_app()
    ok, user_id = auth_signup(f"admin-{os.urandom(4).hex()}@example.test", "Password123!")
    assert ok
    execute_query("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,), commit=True)
    workspace = require_workspace(user_id).workspace_id
    _publish(manager, owner=str(user_id), workspace=workspace, skill_id="fresh-admin-demo", content=_content("fresh-admin-demo"))
    token = create_session(user_id)["access_token"]
    with app.test_client() as client:
        approved = client.post("/api/skills/fresh-admin-demo/approve", headers={"Authorization": f"Bearer {token}"})
        assert approved.status_code == 200

    _publish(manager, owner=str(user_id), workspace=workspace, skill_id="demoted-demo", content=_content("demoted-demo"), requested_permissions=["FILESYSTEM_READ"])
    execute_query("UPDATE users SET role = 'user' WHERE id = ?", (user_id,), commit=True)
    with app.test_client() as client:
        denied = client.post("/api/skills/demoted-demo/approve", headers={"Authorization": f"Bearer {token}"})
        assert denied.status_code == 403
    assert manager.store.get_skill("demoted-demo", str(user_id))["approval_status"] == "PENDING"


def test_publish_route_uses_membership_scope_and_stable_conflicts(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    platform_extensions._extension_manager = manager
    app = create_app()
    ok, user_id = auth_signup(f"publisher-{os.urandom(4).hex()}@example.test", "Password123!")
    assert ok
    token = create_session(user_id)["access_token"]
    content = _content("route-demo")
    body = {
        "skill_id": "route-demo",
        "name": "Route Demo",
        "description": "Reference metadata",
        "version": "1.0.0",
        "content": content,
        "client_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "requested_permissions": [],
    }
    headers = {"Authorization": f"Bearer {token}"}
    with app.test_client() as client:
        created = client.post("/api/skills/publish", json=body, headers=headers)
        assert created.status_code == 201
        assert created.get_json()["ok"] is True
        repeated = client.post("/api/skills/publish", json=body, headers=headers)
        assert repeated.status_code == 200
        conflict = dict(body, version="1.0.1", content=_content("route-demo", "1.0.1", "changed"))
        conflict["client_sha256"] = hashlib.sha256(conflict["content"].encode()).hexdigest()
        assert client.post("/api/skills/publish", json=conflict, headers=headers).status_code == 409
        substituted = dict(body, workspace_id="ws_11111111111111111111111111111111")
        assert client.post("/api/skills/publish", json=substituted, headers=headers).status_code == 403


def test_workspace_membership_and_role_are_authoritative(tmp_path: Path) -> None:
    ok, owner_id = auth_signup(f"owner-{os.urandom(4).hex()}@example.test", "Password123!")
    assert ok
    ok, member_id = auth_signup(f"member-{os.urandom(4).hex()}@example.test", "Password123!")
    assert ok
    workspace = create_workspace(actor_user_id=owner_id, name="Shared test workspace", member_user_id=owner_id, role="owner")
    # An owner may add a member, then a demotion/removal is visible on the next lookup.
    set_membership(actor_user_id=owner_id, workspace_id=workspace, member_user_id=member_id, role="admin")
    assert require_workspace(member_id, workspace).role == "admin"
    set_membership(actor_user_id=owner_id, workspace_id=workspace, member_user_id=member_id, role="member")
    assert require_workspace(member_id, workspace).role == "member"
    set_membership(actor_user_id=owner_id, workspace_id=workspace, member_user_id=member_id, status="removed")
    with pytest.raises(Exception, match="active"):
        require_workspace(member_id, workspace)
    with pytest.raises(Exception):
        require_workspace(member_id, "ws_00000000000000000000000000000000")


def test_concurrent_same_workspace_publish_is_deterministic(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    content = _content()

    def publish(index: int):
        manager = ExtensionManager(store=ExtensionStore(db), base_install_dir=tmp_path / f"data-{index}")
        return _publish(manager, owner=f"user-{index}", workspace="ws_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", content=content)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(publish, range(6)))
    assert sorted(result["publish_outcome"] for result in results) == ["ALREADY_CURRENT"] * 5 + ["CREATED"]
    store = ExtensionStore(db)
    rows = store.list_skills_by_workspace("ws_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert len(rows) == 1
    assert rows[0]["content_hash"] == hashlib.sha256(content.encode()).hexdigest()
    assert len(store.get_skill_versions("phase52-demo", rows[0]["owner_id"])) == 1
    with store._get_connection() as conn:
        audit = conn.execute("SELECT COUNT(*) FROM extension_audit_log WHERE extension_id = ?", ("phase52-demo",)).fetchone()[0]
    assert audit == 1


def test_same_creator_can_publish_same_id_to_different_workspaces(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = _publish(manager, owner="same-user", workspace="ws_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
    second = _publish(manager, owner="same-user", workspace="ws_ffffffffffffffffffffffffffffffff")
    assert first["publish_outcome"] == "CREATED"
    assert second["publish_outcome"] == "CREATED"
    assert len(manager.store.list_skills_by_workspace("ws_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")) == 1
    assert len(manager.store.list_skills_by_workspace("ws_ffffffffffffffffffffffffffffffff")) == 1


def test_publish_rolls_back_when_audit_write_is_injected_to_fail(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with manager.store._get_connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_phase52_publish_audit
               BEFORE INSERT ON extension_audit_log
               WHEN NEW.action = 'PUBLISH'
               BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"""
        )
        conn.commit()
    with pytest.raises(Exception, match="injected audit failure"):
        _publish(manager, owner="audit-owner", workspace="ws_99999999999999999999999999999999")
    store = ExtensionStore(tmp_path / "extensions.db")
    assert store.get_skill("phase52-demo", "audit-owner") is None
    assert store.get_skill_versions("phase52-demo", "audit-owner") == []


def test_corrupt_metadata_and_selected_digest_fail_closed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = _publish(manager, owner="alice", workspace="ws_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    manager.store.set_skill_enabled("phase52-demo", True, "alice")
    with pytest.raises(ExtensionSecurityException, match="digest"):
        manager.resolve_runtime_skill_selection("alice", ["phase52-demo"], ["0" * 64])
    with manager.store._get_connection() as conn:
        conn.execute("UPDATE installed_skills SET permissions = ?, content_hash = ? WHERE id = ? AND owner_id = ?", ('["UNKNOWN"]', "0" * 64, "phase52-demo", "alice"))
        conn.commit()
    with pytest.raises(ExtensionSecurityException):
        manager.resolve_runtime_skill_selection("alice", ["phase52-demo"], [record["content_hash"]])


def test_metadata_mismatch_and_control_characters_are_rejected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(ValueError, match="metadata"):
        _publish(manager, owner="alice", workspace="ws_cccccccccccccccccccccccccccccccc", content=_content(version="1.0.0"), version="1.0.1")
    with pytest.raises(ValueError, match="control"):
        _publish(manager, owner="alice", workspace="ws_dddddddddddddddddddddddddddddddd", content=_content().replace("demo", "demo\x7f"))
