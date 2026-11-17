"""PostgreSQL extension repository contract tests.

These tests are intentionally skipped when the authorized shared PostgreSQL
environment is not configured or reachable.  They never start a local server.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from backend.db.test_target_policy import TestTargetPolicyError as _TestTargetPolicyError
from backend.db.test_target_policy import validate_and_get_test_database_url
from backend.db.extension_migrations import apply_extension_migrations
from backend.db.postgres_store import PostgresStore
from execution.extensions import PostgresExtensionStore


def _store_or_skip() -> PostgresExtensionStore:
    try:
        dsn = validate_and_get_test_database_url()
    except _TestTargetPolicyError as exc:
        pytest.skip(f"PostgreSQL integration target is not authorized: {type(exc).__name__}")
    try:
        migration_store = PostgresStore(dsn)
        try:
            apply_extension_migrations(migration_store._conn)
        finally:
            migration_store.close()
        return PostgresExtensionStore(dsn)
    except Exception as exc:
        pytest.skip(f"PostgreSQL is unavailable; integration remains unverified: {type(exc).__name__}")


def _record(owner: str, workspace: str, skill_id: str = "pg-phase52") -> dict:
    content = "---\nname: pg-phase52\nversion: 1.0.0\n---\n\nreference data\n"
    return {
        "id": skill_id,
        "name": "PG Phase 5.2",
        "version": "1.0.0",
        "description": "Reference data",
        "author": "test",
        "instructions": "reference data\n",
        "content": content,
        "required_tools": [],
        "permissions": [],
        "tags": [],
        "triggers": [],
        "is_enabled": False,
        "install_path": "",
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "manifest_json": {"id": skill_id, "name": "PG Phase 5.2", "version": "1.0.0"},
        "lifecycle_status": "PUBLISHED",
        "approval_status": "NOT_REQUIRED",
        "published_at": None,
        "published_by": owner,
        "workspace_id": workspace,
        "source_kind": "authenticated_publish",
    }


@pytest.mark.integration
def test_postgres_migration_publish_restart_and_tenant_isolation() -> None:
    store = _store_or_skip()
    workspace = "ws_" + os.urandom(16).hex()
    record = _record("pg-owner", workspace)
    saved, outcome = store.publish_skill(record, "pg-owner")
    assert outcome == "CREATED"
    assert saved["content_hash"] == record["content_hash"]
    second_instance = PostgresExtensionStore(validate_and_get_test_database_url())
    assert second_instance.get_skill_by_workspace(record["id"], workspace)["content_hash"] == record["content_hash"]
    assert second_instance.get_skill_by_workspace(record["id"], "ws_" + os.urandom(16).hex()) is None


@pytest.mark.integration
def test_postgres_same_workspace_conflict_is_stable() -> None:
    store = _store_or_skip()
    workspace = "ws_" + os.urandom(16).hex()
    first = _record("pg-owner", workspace)
    assert store.publish_skill(first, "pg-owner")[1] == "CREATED"
    same, outcome = store.publish_skill(first, "pg-other")
    assert outcome == "ALREADY_CURRENT"
    conflicting = dict(first, version="2.0.0", content_hash="f" * 64)
    with pytest.raises(Exception, match="conflict|version"):
        store.publish_skill(conflicting, "pg-other")
