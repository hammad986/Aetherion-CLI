#!/usr/bin/env python3
"""Read-only PostgreSQL extension/workspace reconciliation preflight.

This entrypoint is deliberately separate from the migration runner.  It only
connects to the explicitly approved ``AETHERION_TEST_DATABASE_URL`` target,
starts a read-only transaction, and reports aggregate counts plus hashed
identifier samples.  It never creates a pool, runs migrations, writes rows, or
falls back to ``DATABASE_URL``/SQLite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(REPO_ROOT, "backend")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _safe_identifier(*values: Any) -> str:
    """Return a stable non-reversible sample identifier for operator output."""
    material = "|".join("" if value is None else str(value) for value in values)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
    return cursor.fetchone()[0] is not None


def _count(cursor, query: str, params: tuple = ()) -> int:
    cursor.execute(query, params)
    return int(cursor.fetchone()[0])


def inspect_read_only(connection, *, sample_limit: int = 10) -> dict[str, Any]:
    """Inspect extension/workspace consistency without changing database state."""
    if sample_limit < 1 or sample_limit > 100:
        raise ValueError("sample_limit must be between 1 and 100")

    cursor = connection.cursor()
    try:
        cursor.execute("SET TRANSACTION READ ONLY")
        tables = {
            table: _table_exists(cursor, table)
            for table in (
                "installed_skills",
                "skill_versions",
                "extension_audit_log",
                "extension_schema_migrations",
                "workspace_accounts",
                "workspace_members",
            )
        }
        report: dict[str, Any] = {
            "read_only": True,
            "mutation_performed": False,
            "tables": tables,
            "schema_version": None,
            "counts": {},
            "duplicate_workspace_identity_count": 0,
            "duplicate_workspace_identity_samples": [],
            "orphan_workspace_row_count": None,
            "orphan_workspace_samples": [],
            "legacy_empty_workspace_row_count": 0,
            "legacy_empty_workspace_samples": [],
        }

        if tables["extension_schema_migrations"]:
            cursor.execute("SELECT MAX(version) FROM extension_schema_migrations")
            report["schema_version"] = cursor.fetchone()[0]

        if not tables["installed_skills"]:
            return report

        report["counts"] = {
            "installed_skills": _count(cursor, "SELECT COUNT(*) FROM installed_skills"),
            "workspace_scoped_skills": _count(
                cursor, "SELECT COUNT(*) FROM installed_skills WHERE workspace_id <> ''"
            ),
            "distinct_skill_ids": _count(cursor, "SELECT COUNT(DISTINCT id) FROM installed_skills"),
            "distinct_owner_ids": _count(cursor, "SELECT COUNT(DISTINCT owner_id) FROM installed_skills"),
        }
        report["legacy_empty_workspace_row_count"] = _count(
            cursor, "SELECT COUNT(*) FROM installed_skills WHERE workspace_id = ''"
        )

        cursor.execute(
            """
            SELECT id, owner_id, COUNT(*)
            FROM installed_skills
            WHERE workspace_id = ''
            GROUP BY id, owner_id
            ORDER BY COUNT(*) DESC, id, owner_id
            LIMIT %s
            """,
            (sample_limit,),
        )
        report["legacy_empty_workspace_samples"] = [
            {"identifier": _safe_identifier(row[0], row[1], ""), "row_count": int(row[2])}
            for row in cursor.fetchall()
        ]

        report["duplicate_workspace_identity_count"] = _count(
            cursor,
            """
            SELECT COUNT(*) FROM (
                SELECT workspace_id, id
                FROM installed_skills
                WHERE workspace_id <> ''
                GROUP BY workspace_id, id
                HAVING COUNT(*) > 1
            ) AS duplicate_groups
            """,
        )
        cursor.execute(
            """
            SELECT workspace_id, id, COUNT(*)
            FROM installed_skills
            WHERE workspace_id <> ''
            GROUP BY workspace_id, id
            HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC, workspace_id, id
            LIMIT %s
            """,
            (sample_limit,),
        )
        report["duplicate_workspace_identity_samples"] = [
            {"identifier": _safe_identifier(row[0], row[1]), "row_count": int(row[2])}
            for row in cursor.fetchall()
        ]

        if tables["workspace_accounts"]:
            report["orphan_workspace_row_count"] = _count(
                cursor,
                """
                SELECT COUNT(*)
                FROM installed_skills s
                LEFT JOIN workspace_accounts w ON w.id = s.workspace_id
                WHERE s.workspace_id <> '' AND w.id IS NULL
                """,
            )
            cursor.execute(
                """
                SELECT s.workspace_id, COUNT(*)
                FROM installed_skills s
                LEFT JOIN workspace_accounts w ON w.id = s.workspace_id
                WHERE s.workspace_id <> '' AND w.id IS NULL
                GROUP BY s.workspace_id
                ORDER BY COUNT(*) DESC, s.workspace_id
                LIMIT %s
                """,
                (sample_limit,),
            )
            report["orphan_workspace_samples"] = [
                {"identifier": _safe_identifier(row[0]), "row_count": int(row[1])}
                for row in cursor.fetchall()
            ]
        return report
    finally:
        cursor.close()
        # The caller owns the connection.  Roll back the read-only transaction
        # so even future caller changes cannot accidentally commit this scope.
        connection.rollback()


def _sanitize_error(exc: BaseException) -> str:
    message = re.sub(r"postgres(?:ql)?://[^\s]+", "<redacted-target>", str(exc), flags=re.IGNORECASE)
    message = re.sub(r"password\s*[:=]\s*\S+", "password=<redacted>", message, flags=re.IGNORECASE)
    return message or type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only PostgreSQL extension reconciliation preflight")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--sample-limit", type=int, default=10, help="Maximum hashed samples per category (1-100)")
    args = parser.parse_args(argv)

    result: dict[str, Any]
    exit_code = 0
    try:
        from backend.db.test_target_policy import validate_and_get_test_database_url

        dsn = validate_and_get_test_database_url()
        import psycopg2

        connection = psycopg2.connect(dsn, connect_timeout=5)
        try:
            connection.autocommit = False
            result = inspect_read_only(connection, sample_limit=args.sample_limit)
        finally:
            connection.close()
    except Exception as exc:
        # The preflight is a gate: unavailable or unauthorized targets are
        # reported as blocked and never retried against another environment.
        result = {
            "read_only": True,
            "mutation_performed": False,
            "status": "BLOCKED",
            "error_type": type(exc).__name__,
            "error": _sanitize_error(exc),
        }
        exit_code = 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print("PostgreSQL Extension Reconciliation Preflight")
        print("-" * 48)
        print(f"Status:             {result.get('status', 'READY')}")
        print(f"Read-only:          {result.get('read_only', False)}")
        print(f"Mutation performed: {result.get('mutation_performed', True)}")
        if result.get("error"):
            print(f"Error:              {result['error']}")
        else:
            print(f"Schema version:     {result.get('schema_version')}")
            print(f"Counts:             {json.dumps(result.get('counts', {}), sort_keys=True)}")
            print(f"Duplicate groups:   {result.get('duplicate_workspace_identity_count', 0)}")
            print(f"Orphan rows:        {result.get('orphan_workspace_row_count')}")
            print(f"Legacy rows:        {result.get('legacy_empty_workspace_row_count', 0)}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
