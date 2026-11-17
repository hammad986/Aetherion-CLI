"""Tracked PostgreSQL schema for authoritative extension state."""
from __future__ import annotations

import time
import re


EXTENSION_SCHEMA_VERSION = 2
REQUIRED_EXTENSION_TABLES = frozenset(
    {
        "installed_skills",
        "skill_versions",
        "extension_audit_log",
        "extension_schema_migrations",
        "workspace_accounts",
        "workspace_members",
    }
)


def _primary_key(cursor, table_name: str) -> tuple[str | None, list[str]]:
    """Return the current primary-key constraint and ordered columns."""
    cursor.execute(
        """
        SELECT constraint_name, column_name
        FROM information_schema.key_column_usage
        WHERE table_schema = current_schema()
          AND table_name = %s
          AND constraint_name IN (
              SELECT constraint_name
              FROM information_schema.table_constraints
              WHERE table_schema = current_schema()
                AND table_name = %s
                AND constraint_type = 'PRIMARY KEY'
          )
        ORDER BY constraint_name, ordinal_position
        """,
        (table_name, table_name),
    )
    rows = cursor.fetchall()
    if not rows:
        return None, []
    return rows[0][0], [row[1] for row in rows]


def verify_extension_schema(connection) -> dict[str, object]:
    """Verify the migrated extension/workspace schema without executing DDL."""
    with connection.cursor() as cursor:
        missing: list[str] = []
        for table_name in sorted(REQUIRED_EXTENSION_TABLES):
            cursor.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
            if cursor.fetchone()[0] is None:
                missing.append(table_name)
        if missing:
            raise RuntimeError(
                "Required PostgreSQL extension/workspace schema objects are missing: "
                + ", ".join(missing)
            )
        cursor.execute("SELECT MAX(version) FROM extension_schema_migrations")
        version = cursor.fetchone()[0]
        if version != EXTENSION_SCHEMA_VERSION:
            raise RuntimeError(
                f"PostgreSQL extension schema version is not current (expected {EXTENSION_SCHEMA_VERSION}, found {version})"
            )
        return {
            "ok": True,
            "version": version,
            "expected": EXTENSION_SCHEMA_VERSION,
            "missing": [],
        }


def apply_extension_migrations(connection) -> list[int]:
    """Apply the extension schema atomically on an existing psycopg2 connection.

    Version 2 repairs installations created by the original Phase 5.2
    migration.  In particular, the old implementation dropped and recreated
    the skills primary key on *every* startup.  The repair is now conditional,
    detects duplicate workspace identities before adding the unique index, and
    records a durable migration version only after the whole transaction has
    succeeded.
    """
    statements = [
        """
        CREATE TABLE IF NOT EXISTS extension_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
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
            permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
            provided_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
            dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
            install_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            trust_level TEXT NOT NULL DEFAULT 'UNTRUSTED',
            PRIMARY KEY (id, owner_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_versions (
            id BIGSERIAL PRIMARY KEY,
            plugin_id TEXT NOT NULL,
            owner_id TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            install_path TEXT NOT NULL,
            manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            installed_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS installed_skills (
            id TEXT NOT NULL,
            owner_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            description TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            instructions TEXT NOT NULL,
            required_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
            permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
            tags JSONB NOT NULL DEFAULT '[]'::jsonb,
            triggers JSONB NOT NULL DEFAULT '[]'::jsonb,
            is_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            install_path TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            lifecycle_status TEXT NOT NULL DEFAULT 'INSTALLED',
            approval_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
            published_at DOUBLE PRECISION,
            published_by TEXT NOT NULL DEFAULT '',
            workspace_id TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'runtime_install',
            PRIMARY KEY (id, owner_id, workspace_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS skill_versions (
            id BIGSERIAL PRIMARY KEY,
            skill_id TEXT NOT NULL,
            owner_id TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            instructions TEXT NOT NULL,
            manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            installed_at DOUBLE PRECISION NOT NULL
        )
        """,
        "ALTER TABLE installed_skills ADD COLUMN IF NOT EXISTS content TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE installed_skills ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'INSTALLED'",
        "ALTER TABLE installed_skills ADD COLUMN IF NOT EXISTS approval_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED'",
        "ALTER TABLE installed_skills ADD COLUMN IF NOT EXISTS published_at DOUBLE PRECISION",
        "ALTER TABLE installed_skills ADD COLUMN IF NOT EXISTS published_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE installed_skills ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE installed_skills ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'runtime_install'",
        "ALTER TABLE skill_versions ADD COLUMN IF NOT EXISTS content TEXT NOT NULL DEFAULT ''",
        """
        CREATE TABLE IF NOT EXISTS extension_audit_log (
            id BIGSERIAL PRIMARY KEY,
            owner_id TEXT NOT NULL DEFAULT '',
            extension_type TEXT NOT NULL,
            extension_id TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            timestamp DOUBLE PRECISION NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_installed_skills_workspace ON installed_skills(workspace_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_installed_skills_owner ON installed_skills(owner_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_extension_audit_lookup ON extension_audit_log(owner_id, extension_type, extension_id, timestamp)",
    ]
    with connection:
        with connection.cursor() as cursor:
            # Serialize migration work across backend processes/pods without
            # taking an application-level lock that would not cross workers.
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext('aetherion.extension_schema'))")
            for statement in statements:
                cursor.execute(statement)

            pk_name, pk_columns = _primary_key(cursor, "installed_skills")
            desired_pk = ["id", "owner_id", "workspace_id"]
            if pk_columns != desired_pk:
                cursor.execute(
                    """
                    SELECT id, owner_id, workspace_id, COUNT(*)
                    FROM installed_skills
                    GROUP BY id, owner_id, workspace_id
                    HAVING COUNT(*) > 1
                    LIMIT 1
                    """
                )
                duplicate = cursor.fetchone()
                if duplicate:
                    raise RuntimeError(
                        "Cannot migrate installed_skills: duplicate (id, owner_id, workspace_id) records require read-only reconciliation"
                    )
                if pk_name:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pk_name):
                        raise RuntimeError("Cannot migrate installed_skills: invalid primary-key constraint name")
                    cursor.execute(f'ALTER TABLE installed_skills DROP CONSTRAINT "{pk_name}"')
                cursor.execute(
                    "ALTER TABLE installed_skills ADD CONSTRAINT installed_skills_pkey PRIMARY KEY (id, owner_id, workspace_id)"
                )

            # This check makes the failure mode explicit before PostgreSQL's
            # unique-index error, while keeping the transaction fully atomic.
            cursor.execute(
                """
                SELECT workspace_id, id, COUNT(*)
                FROM installed_skills
                WHERE workspace_id <> ''
                GROUP BY workspace_id, id
                HAVING COUNT(*) > 1
                LIMIT 1
                """
            )
            duplicate_workspace_identity = cursor.fetchone()
            if duplicate_workspace_identity:
                raise RuntimeError(
                    "Cannot enforce workspace skill uniqueness: duplicate (workspace_id, id) records require read-only reconciliation"
                )
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_installed_skills_workspace_id "
                "ON installed_skills(workspace_id, id) WHERE workspace_id <> ''"
            )

            cursor.execute("SELECT version FROM extension_schema_migrations WHERE version = %s", (EXTENSION_SCHEMA_VERSION,))
            if cursor.fetchone() is None:
                cursor.execute(
                    "INSERT INTO extension_schema_migrations (version, applied_at) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                    (EXTENSION_SCHEMA_VERSION, time.time()),
                )
                return [EXTENSION_SCHEMA_VERSION] if cursor.rowcount else []
    return []
