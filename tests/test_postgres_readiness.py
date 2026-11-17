"""Non-network regression tests for PostgreSQL readiness safeguards."""
from __future__ import annotations

import json

import pytest

from backend.db.postgres_store import PostgresStore
from scripts import postgres_extension_preflight


def test_postgres_pool_rejects_switching_database_targets(monkeypatch):
    monkeypatch.setattr(PostgresStore, "_pool", object())
    monkeypatch.setattr(PostgresStore, "_pool_dsn", "postgresql://user@host/one")

    with pytest.raises(RuntimeError, match="already bound to a different database target"):
        PostgresStore._ensure_pool("postgresql://user@host/two")


def test_extension_store_verifies_schema_without_runtime_migration(monkeypatch):
    import execution.extensions.postgres_store as postgres_extension_module

    calls = []

    class _FakeStore:
        def __init__(self, dsn):
            self._conn = object()

        def close(self):
            pass

    monkeypatch.setattr(postgres_extension_module, "PostgresStore", _FakeStore)
    monkeypatch.setattr(
        postgres_extension_module,
        "verify_extension_schema",
        lambda connection: calls.append(connection),
    )
    monkeypatch.setattr(postgres_extension_module.PostgresExtensionStore, "_schema_dsn", None)

    postgres_extension_module.PostgresExtensionStore("postgresql://user@host/aetherion")

    assert len(calls) == 1


class _ReadOnlyCursor:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=()):
        self.statements.append(sql)

    def fetchone(self):
        return (None,)

    def fetchall(self):
        return []

    def close(self):
        pass


class _ReadOnlyConnection:
    def __init__(self):
        self.cursor_obj = _ReadOnlyCursor()
        self.rollback_calls = 0

    def cursor(self):
        return self.cursor_obj

    def rollback(self):
        self.rollback_calls += 1


def test_reconciliation_preflight_starts_read_only_and_rolls_back():
    connection = _ReadOnlyConnection()

    result = postgres_extension_preflight.inspect_read_only(connection)

    assert result["read_only"] is True
    assert result["mutation_performed"] is False
    assert connection.cursor_obj.statements[0] == "SET TRANSACTION READ ONLY"
    assert connection.rollback_calls == 1


def test_preflight_main_blocks_without_explicit_isolated_target(monkeypatch, capsys):
    monkeypatch.delenv("AETHERION_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://not-used@example.invalid/prod")

    assert postgres_extension_preflight.main(["--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED"
    assert result["mutation_performed"] is False
    assert "not-used" not in json.dumps(result)
