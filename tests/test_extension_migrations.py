"""Unit tests for PostgreSQL extension migration safety without a live server."""
from __future__ import annotations

import pytest

from backend.db.extension_migrations import apply_extension_migrations


class _MigrationCursor:
    def __init__(self, pk_columns, *, duplicate_pk=False, duplicate_workspace=False):
        self.pk_columns = pk_columns
        self.duplicate_pk = duplicate_pk
        self.duplicate_workspace = duplicate_workspace
        self.statements: list[str] = []
        self._fetchone = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=()):
        self.statements.append(statement)
        self._fetchone = None
        if "information_schema.key_column_usage" in statement:
            return
        if "GROUP BY id, owner_id, workspace_id" in statement:
            self._fetchone = ("skill", "owner", "workspace", 2) if self.duplicate_pk else None
        elif "GROUP BY workspace_id, id" in statement:
            self._fetchone = ("workspace", "skill", 2) if self.duplicate_workspace else None
        elif "SELECT version FROM extension_schema_migrations" in statement:
            self._fetchone = None

    def fetchall(self):
        if self.statements and "information_schema.key_column_usage" in self.statements[-1]:
            return [("installed_skills_pkey", column) for column in self.pk_columns]
        return []

    def fetchone(self):
        return self._fetchone

    def close(self):
        pass


class _MigrationConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.rollback_called = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            self.rollback_called = True
        return False

    def cursor(self):
        return self.cursor_obj


def test_fresh_schema_records_version_without_unconditional_primary_key_drop():
    cursor = _MigrationCursor(["id", "owner_id", "workspace_id"])
    connection = _MigrationConnection(cursor)

    assert apply_extension_migrations(connection) == [2]
    assert not any("DROP CONSTRAINT" in statement for statement in cursor.statements)
    assert any("CREATE UNIQUE INDEX IF NOT EXISTS ux_installed_skills_workspace_id" in statement for statement in cursor.statements)


def test_duplicate_primary_key_identity_blocks_reconciliation_before_ddl():
    cursor = _MigrationCursor(["id", "owner_id"], duplicate_pk=True)
    connection = _MigrationConnection(cursor)

    with pytest.raises(RuntimeError, match="read-only reconciliation"):
        apply_extension_migrations(connection)

    assert connection.rollback_called is True
    assert not any("CREATE UNIQUE INDEX IF NOT EXISTS ux_installed_skills_workspace_id" in statement for statement in cursor.statements)


def test_duplicate_workspace_identity_blocks_unique_index_creation():
    cursor = _MigrationCursor(
        ["id", "owner_id", "workspace_id"], duplicate_workspace=True
    )
    connection = _MigrationConnection(cursor)

    with pytest.raises(RuntimeError, match=r"duplicate \(workspace_id, id\)"):
        apply_extension_migrations(connection)

    assert connection.rollback_called is True
    assert not any("CREATE UNIQUE INDEX IF NOT EXISTS ux_installed_skills_workspace_id" in statement for statement in cursor.statements)
