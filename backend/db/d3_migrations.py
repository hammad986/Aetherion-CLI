"""
backend/db/d3_migrations.py — Authoritative Tracked Migrations for D3 Enterprise Subsystem.
==========================================================================================
Provides deterministic migration execution, version tracking, schema compatibility
validation, and safe adoption for D3 tables across PostgreSQL and SQLite.

Domains covered:
  - Automations & Automation Runs (d3_automations, d3_automation_runs)
  - Kanban Board Cards (d3_kanban_cards)
  - Knowledge Graph Nodes & Edges (d3_kg_nodes, d3_kg_edges)
  - Real Git Worktrees Governance (d3_worktrees)
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("aetherion.d3_migrations")


class D3MigrationError(Exception):
    """Base error for D3 migration failures."""
    pass


class D3IncompatibleSchemaError(D3MigrationError):
    """Raised when D3 database schema version is incompatible with running code."""
    pass


class D3SchemaCorruptionError(D3MigrationError):
    """Raised when D3 migration history checksums or structure are corrupted."""
    pass


@dataclass(frozen=True)
class D3MigrationDefinition:
    version: int
    name: str
    up_sql_sqlite: str
    up_sql_postgres: str
    description: str

    @property
    def checksum(self) -> str:
        """Deterministic sha256 checksum of migration definition."""
        content = f"{self.version}:{self.name}:{self.up_sql_sqlite.strip()}:{self.up_sql_postgres.strip()}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# ── Canonical D3 Migration Registry (Strictly Additive, Non-Destructive) ─────

D3_MIGRATIONS: List[D3MigrationDefinition] = [
    D3MigrationDefinition(
        version=1,
        name="001_initial_d3_schema",
        description="Creates D3 automations, automation runs, kanban cards, knowledge graph, and worktrees tables",
        up_sql_sqlite="""
        CREATE TABLE IF NOT EXISTS d3_automations (
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

        CREATE INDEX IF NOT EXISTS ix_d3_auto_tenant ON d3_automations (tenant_id);

        CREATE TABLE IF NOT EXISTS d3_automation_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            completed_at REAL,
            exit_code INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            logs TEXT,
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS ix_d3_autoruns_task ON d3_automation_runs (task_id);

        CREATE TABLE IF NOT EXISTS d3_kanban_cards (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT,
            title TEXT NOT NULL,
            content TEXT,
            column_name TEXT NOT NULL DEFAULT 'backlog',
            status TEXT NOT NULL DEFAULT 'backlog',
            priority TEXT NOT NULL DEFAULT 'medium',
            ordering INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS ix_d3_kanban_tenant ON d3_kanban_cards (tenant_id, column_name);

        CREATE TABLE IF NOT EXISTS d3_kg_nodes (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            content TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS ix_d3_kg_nodes_session ON d3_kg_nodes (session_id, node_type);

        CREATE TABLE IF NOT EXISTS d3_kg_edges (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relationship TEXT NOT NULL,
            created_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS ix_d3_kg_edges_session ON d3_kg_edges (session_id);

        CREATE TABLE IF NOT EXISTS d3_worktrees (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT,
            execution_id TEXT,
            task_id TEXT,
            base_ref TEXT NOT NULL DEFAULT 'HEAD',
            branch TEXT NOT NULL,
            path TEXT NOT NULL,
            repo_root TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            governed BOOLEAN NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_activity_at REAL NOT NULL,
            discarded_at REAL,
            files_changed INTEGER NOT NULL DEFAULT 0,
            diff_lines INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS ix_d3_wt_tenant ON d3_worktrees (tenant_id, session_id);
        """,
        up_sql_postgres="""
        CREATE TABLE IF NOT EXISTS d3_automations (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT,
            name TEXT NOT NULL,
            prompt TEXT,
            schedule_type TEXT NOT NULL,
            schedule_value TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            last_run DOUBLE PRECISION,
            next_run DOUBLE PRECISION,
            run_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS ix_d3_auto_tenant ON d3_automations (tenant_id);

        CREATE TABLE IF NOT EXISTS d3_automation_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at DOUBLE PRECISION NOT NULL,
            completed_at DOUBLE PRECISION,
            exit_code INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            logs TEXT,
            created_at DOUBLE PRECISION NOT NULL
        );

        CREATE INDEX IF NOT EXISTS ix_d3_autoruns_task ON d3_automation_runs (task_id);

        CREATE TABLE IF NOT EXISTS d3_kanban_cards (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT,
            title TEXT NOT NULL,
            content TEXT,
            column_name TEXT NOT NULL DEFAULT 'backlog',
            status TEXT NOT NULL DEFAULT 'backlog',
            priority TEXT NOT NULL DEFAULT 'medium',
            ordering INTEGER NOT NULL DEFAULT 0,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS ix_d3_kanban_tenant ON d3_kanban_cards (tenant_id, column_name);

        CREATE TABLE IF NOT EXISTS d3_kg_nodes (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            content TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at DOUBLE PRECISION NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS ix_d3_kg_nodes_session ON d3_kg_nodes (session_id, node_type);

        CREATE TABLE IF NOT EXISTS d3_kg_edges (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relationship TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS ix_d3_kg_edges_session ON d3_kg_edges (session_id);

        CREATE TABLE IF NOT EXISTS d3_worktrees (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT,
            execution_id TEXT,
            task_id TEXT,
            base_ref TEXT NOT NULL DEFAULT 'HEAD',
            branch TEXT NOT NULL,
            path TEXT NOT NULL,
            repo_root TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            governed BOOLEAN NOT NULL DEFAULT TRUE,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            last_activity_at DOUBLE PRECISION NOT NULL,
            discarded_at DOUBLE PRECISION,
            files_changed INTEGER NOT NULL DEFAULT 0,
            diff_lines INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS ix_d3_wt_tenant ON d3_worktrees (tenant_id, session_id);
        """,
    ),
]

CURRENT_D3_SCHEMA_VERSION = max(m.version for m in D3_MIGRATIONS)

REQUIRED_D3_TABLES: Set[str] = {
    "d3_automations",
    "d3_automation_runs",
    "d3_kanban_cards",
    "d3_kg_nodes",
    "d3_kg_edges",
    "d3_worktrees",
}


# ── Migration Engine ─────────────────────────────────────────────────────────

def ensure_d3_version_table(conn, is_postgres: bool = False) -> None:
    """Ensure the d3_schema_migrations table exists."""
    sql = (
        """
        CREATE TABLE IF NOT EXISTS d3_schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  DOUBLE PRECISION NOT NULL,
            checksum    TEXT NOT NULL
        );
        """
        if is_postgres
        else """
        CREATE TABLE IF NOT EXISTS d3_schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  REAL NOT NULL,
            checksum    TEXT NOT NULL
        );
        """
    )
    if is_postgres:
        with conn.cursor() as cur:
            cur.execute(sql)
    else:
        conn.execute(sql)
        conn.commit()


def get_applied_d3_migrations(conn, is_postgres: bool = False) -> List[Dict[str, Any]]:
    """Return all currently applied D3 migration records ordered by version ASC."""
    ensure_d3_version_table(conn, is_postgres=is_postgres)
    sql = "SELECT version, name, applied_at, checksum FROM d3_schema_migrations ORDER BY version ASC;"
    if is_postgres:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return [
                {"version": r[0], "name": r[1], "applied_at": r[2], "checksum": r[3]}
                for r in rows
            ]
    else:
        cur = conn.execute(sql)
        rows = cur.fetchall()
        return [
            {"version": r[0], "name": r[1], "applied_at": r[2], "checksum": r[3]}
            for r in rows
        ]


def _get_existing_tables(conn, is_postgres: bool = False) -> Set[str]:
    """Inspect existing base table names in current database."""
    if is_postgres:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public';"
            )
            return {r[0] for r in cur.fetchall()}
    else:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        )
        return {r[0] for r in cur.fetchall()}


def verify_d3_schema_compatibility(conn, is_postgres: bool = False) -> Dict[str, Any]:
    """
    Assert D3 database schema is compatible with the running code version.
    Fails closed if schema is newer than code, or if migration history has checksum mismatch,
    or if any of the required D3 tables are absent.
    """
    applied = get_applied_d3_migrations(conn, is_postgres=is_postgres)
    applied_versions = {m["version"]: m for m in applied}
    max_applied = max(applied_versions.keys()) if applied_versions else 0

    # 1. Forward incompatibility check (Fail closed)
    if max_applied > CURRENT_D3_SCHEMA_VERSION:
        raise D3IncompatibleSchemaError(
            f"D3 database schema version {max_applied} is newer than current application code version "
            f"{CURRENT_D3_SCHEMA_VERSION}. Startup aborted to prevent corrupting forward data."
        )

    # 2. History checksum integrity check
    for m in D3_MIGRATIONS:
        if m.version in applied_versions:
            rec = applied_versions[m.version]
            if rec["checksum"] != m.checksum:
                raise D3SchemaCorruptionError(
                    f"D3 Migration {m.version} ('{m.name}') checksum mismatch. "
                    f"Database has '{rec['checksum']}', expected '{m.checksum}'. Schema tampering detected."
                )

    # 3. Check actual presence of all required D3 tables
    existing_tables = _get_existing_tables(conn, is_postgres=is_postgres)
    missing_tables = [t for t in sorted(REQUIRED_D3_TABLES) if t not in existing_tables]

    pending_count = sum(1 for m in D3_MIGRATIONS if m.version not in applied_versions)
    is_up_to_date = (pending_count == 0) and (len(missing_tables) == 0)

    return {
        "ok": is_up_to_date,
        "current_db_version": max_applied,
        "code_schema_version": CURRENT_D3_SCHEMA_VERSION,
        "applied_count": len(applied),
        "pending_count": pending_count,
        "missing_tables": missing_tables,
        "is_up_to_date": is_up_to_date,
    }


def apply_d3_migrations(conn, is_postgres: bool = False) -> List[int]:
    """
    Deterministically apply all pending D3 migrations in order within transactions.
    Safely adopts pre-existing D3 tables without data loss.
    Returns list of newly applied migration versions.
    """
    ensure_d3_version_table(conn, is_postgres=is_postgres)
    applied_versions = {
        m["version"] for m in get_applied_d3_migrations(conn, is_postgres=is_postgres)
    }

    newly_applied: List[int] = []
    existing_tables = _get_existing_tables(conn, is_postgres=is_postgres)

    for migration in D3_MIGRATIONS:
        if migration.version in applied_versions:
            # Check for schema drift: if migration was recorded as applied but a required table was dropped,
            # idempotently re-ensure the missing tables without touching existing data.
            missing_required = [t for t in REQUIRED_D3_TABLES if t not in existing_tables]
            if missing_required:
                logger.warning(
                    "Detected schema drift for D3 migration %d (missing: %s); healing missing tables.",
                    migration.version,
                    ", ".join(missing_required),
                )
                sql = migration.up_sql_postgres if is_postgres else migration.up_sql_sqlite
                if is_postgres:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(sql)
                else:
                    conn.executescript(sql)
                    conn.commit()
                existing_tables = _get_existing_tables(conn, is_postgres=is_postgres)
            continue

        logger.info(
            "Applying D3 migration %d: %s (%s)",
            migration.version,
            migration.name,
            "PostgreSQL" if is_postgres else "SQLite",
        )
        sql = migration.up_sql_postgres if is_postgres else migration.up_sql_sqlite
        now = time.time()

        if is_postgres:
            with conn:
                with conn.cursor() as cur:
                    # Execute idempotent DDL script
                    cur.execute(sql)
                    # Record migration in history table
                    cur.execute(
                        """
                        INSERT INTO d3_schema_migrations (version, name, applied_at, checksum)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (migration.version, migration.name, now, migration.checksum),
                    )
        else:
            conn.executescript(sql)
            conn.execute(
                """
                INSERT INTO d3_schema_migrations (version, name, applied_at, checksum)
                VALUES (?, ?, ?, ?);
                """,
                (migration.version, migration.name, now, migration.checksum),
            )
            conn.commit()

        newly_applied.append(migration.version)

    return newly_applied
