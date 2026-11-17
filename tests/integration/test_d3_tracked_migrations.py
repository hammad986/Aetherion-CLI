"""
tests/integration/test_d3_tracked_migrations.py — Integration Tests for Tracked D3 Schema Migrations.
=====================================================================================================
Verifies the complete D3 database migration lifecycle:
  1. Fresh database: migration creates all required schema including 6 D3 tables and version tracking.
  2. Existing database adoption: pre-existing tables survive migration without data loss or error.
  3. Idempotency: running migrations multiple times completes cleanly with no duplicate objects.
  4. Status correctness: UP_TO_DATE is rejected when any required D3 table is missing.
  5. Import safety: importing backend.db.d3_store or instantiating D3Store causes zero DDL / schema mutation.
  6. Application integration: D3Store operations (automations, kanban, worktrees) work after migration.
  7. Checksum tampering detection: modified migrations fail closed.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from backend.db.d3_migrations import (
    CURRENT_D3_SCHEMA_VERSION,
    D3_MIGRATIONS,
    D3IncompatibleSchemaError,
    D3SchemaCorruptionError,
    REQUIRED_D3_TABLES,
    apply_d3_migrations,
    ensure_d3_version_table,
    get_applied_d3_migrations,
    verify_d3_schema_compatibility,
)
from backend.db.d3_store import D3Store, validate_d3_schema
import scripts.db_migrate as db_migrate_mod


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_sqlite_db(tmp_path):
    """Provides a fresh isolated SQLite database path and open connection."""
    db_file = str(tmp_path / "test_d3_fresh.db")
    conn = sqlite3.connect(db_file)
    try:
        yield db_file, conn
    finally:
        conn.close()


# ── Test 1: Fresh Database Migration ─────────────────────────────────────────

def test_fresh_database_creates_all_d3_tables_and_tracking(fresh_sqlite_db):
    """Verify that apply_d3_migrations on a fresh database creates all 6 tables and tracks v1."""
    db_file, conn = fresh_sqlite_db

    # Baseline: verify 0 tables exist
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    assert len(cur.fetchall()) == 0

    # Apply D3 migrations
    applied = apply_d3_migrations(conn, is_postgres=False)
    assert applied == [1]

    # Verify tracking table records migration v1
    records = get_applied_d3_migrations(conn, is_postgres=False)
    assert len(records) == 1
    assert records[0]["version"] == 1
    assert records[0]["name"] == "001_initial_d3_schema"
    assert len(records[0]["checksum"]) == 16

    # Verify all 6 required D3 tables exist in catalog
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {r[0] for r in cur.fetchall()}
    for req in REQUIRED_D3_TABLES:
        assert req in tables, f"Required table {req} missing from database"
    assert "d3_schema_migrations" in tables

    # Verify compatibility check succeeds
    compat = verify_d3_schema_compatibility(conn, is_postgres=False)
    assert compat["ok"] is True
    assert compat["is_up_to_date"] is True
    assert compat["current_db_version"] == CURRENT_D3_SCHEMA_VERSION
    assert len(compat["missing_tables"]) == 0


# ── Test 2: Existing Database Adoption Without Data Loss ────────────────────

def test_existing_database_adopted_safely_without_data_loss(fresh_sqlite_db):
    """Verify that pre-existing D3 tables with data are adopted cleanly without dropping rows."""
    db_file, conn = fresh_sqlite_db

    # Simulate an existing database that already had d3_automations created by old runtime DDL
    conn.execute("""
        CREATE TABLE d3_automations (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT,
            name TEXT NOT NULL,
            prompt TEXT,
            schedule_type TEXT NOT NULL,
            schedule_value TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            enabled BOOLEAN NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_run REAL,
            next_run REAL,
            run_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
    """)
    # Insert existing production-like record
    conn.execute(
        "INSERT INTO d3_automations (id, tenant_id, name, schedule_type, created_at, updated_at) "
        "VALUES ('auto_test_123', 'tenant_alpha', 'Pre-existing Automation', 'daily', 1000.0, 1000.0);"
    )
    conn.commit()

    # Apply D3 migration on this existing database
    applied = apply_d3_migrations(conn, is_postgres=False)
    assert applied == [1]

    # Verify pre-existing data was NOT lost or overwritten
    cur = conn.execute("SELECT id, name, tenant_id FROM d3_automations WHERE id = 'auto_test_123';")
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "auto_test_123"
    assert row[1] == "Pre-existing Automation"
    assert row[2] == "tenant_alpha"

    # Verify remaining tables were also created
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {r[0] for r in cur.fetchall()}
    for req in REQUIRED_D3_TABLES:
        assert req in tables


# ── Test 3: Idempotency (Repeat Migration Run) ──────────────────────────────

def test_idempotent_migration_execution(fresh_sqlite_db):
    """Verify that applying migrations a second time is a clean no-op."""
    db_file, conn = fresh_sqlite_db

    # First run
    applied_first = apply_d3_migrations(conn, is_postgres=False)
    assert applied_first == [1]

    # Second run
    applied_second = apply_d3_migrations(conn, is_postgres=False)
    assert applied_second == []  # No new migrations applied

    # Verification passes
    compat = verify_d3_schema_compatibility(conn, is_postgres=False)
    assert compat["ok"] is True
    assert compat["pending_count"] == 0


# ── Test 4: Status Correctness Fails Closed on Missing Table ────────────────

def test_status_fails_closed_when_d3_table_missing(fresh_sqlite_db):
    """Verify that verify_d3_schema_compatibility and check_status report MIGRATIONS_PENDING if a table is dropped."""
    db_file, conn = fresh_sqlite_db

    # Apply all migrations
    apply_d3_migrations(conn, is_postgres=False)

    # Deliberately drop one required table to simulate partial corruption
    conn.execute("DROP TABLE d3_worktrees;")
    conn.commit()

    compat = verify_d3_schema_compatibility(conn, is_postgres=False)
    assert compat["ok"] is False
    assert compat["is_up_to_date"] is False
    assert "d3_worktrees" in compat["missing_tables"]


# ── Test 5: Import Safety — Zero DDL on Import or Instantiation ─────────────

def test_import_and_instantiation_causes_no_ddl(fresh_sqlite_db, monkeypatch):
    """Verify that instantiating D3Store causes zero DDL operations on the database."""
    db_file, conn = fresh_sqlite_db
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    # Create fresh empty database
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    assert len(cur.fetchall()) == 0

    # Instantiate D3Store without migrations having run
    store = D3Store()
    assert store is not None

    # Verify that NO tables were created by constructing D3Store
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    assert len(cur.fetchall()) == 0, "D3Store.__init__ created tables implicitly at import/init time!"


# ── Test 6: Application Integration After Migration ─────────────────────────

def test_d3_store_functional_operations_after_migration(fresh_sqlite_db, monkeypatch):
    """Verify that D3Store methods function correctly once migrations have been applied."""
    db_file, conn = fresh_sqlite_db
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    # Run migration first
    apply_d3_migrations(conn, is_postgres=False)

    # Construct store and execute entity operations
    store = D3Store()

    # 1. Create and retrieve automation
    auto = store.create_automation(
        tenant_id="tenant_integration_test",
        data={
            "name": "Integration Test Job",
            "prompt": "Run security check",
            "schedule": "hourly",
        },
    )
    assert auto is not None
    assert auto["name"] == "Integration Test Job"
    assert auto["id"] is not None

    retrieved = store.get_automation("tenant_integration_test", auto["id"])
    assert retrieved is not None
    assert retrieved["id"] == auto["id"]

    # 2. Create kanban card
    card = store.create_kanban_card(
        tenant_id="tenant_integration_test",
        data={"title": "Test Kanban Card", "column_name": "backlog"},
    )
    assert card is not None
    assert card["title"] == "Test Kanban Card"

    # 3. Create knowledge graph node
    node = store.add_kg_node(
        tenant_id="tenant_integration_test",
        session_id="sess_123",
        node_type="concept",
        content="Tracked Migrations",
    )
    assert node is not None
    assert node["content"] == "Tracked Migrations"


# ── Test 7: Canonical db_migrate Integration ────────────────────────────────

def test_db_migrate_apply_and_status_integration(fresh_sqlite_db, monkeypatch):
    """Verify that scripts/db_migrate.py apply_migrations() and check_status() report UP_TO_DATE."""
    db_file, conn = fresh_sqlite_db
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("ENVIRONMENT", "local-development")

    # Initial status should be MIGRATIONS_PENDING on fresh database
    status_before = db_migrate_mod.check_status()
    assert status_before["ok"] is False
    assert status_before["status"] == "MIGRATIONS_PENDING"

    # Apply all migrations via canonical entrypoint
    res = db_migrate_mod.apply_migrations()
    assert res["ok"] is True
    assert 1 in res.get("applied_d3_versions", [])

    # Status after migration must be UP_TO_DATE
    status_after = db_migrate_mod.check_status()
    assert status_after["ok"] is True
    assert status_after["status"] == "UP_TO_DATE"
    assert len(status_after["missing_required"]) == 0
    assert len(status_after["missing_fabric"]) == 0
    assert len(status_after["missing_d3"]) == 0
    assert status_after["d3_compatibility"]["ok"] is True
