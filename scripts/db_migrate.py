#!/usr/bin/env python3
"""
scripts/db_migrate.py — Authoritative Database Migration & Schema Gate

Automates schema status inspection, compatibility validation, and migration execution:
  • local-development: Applies schema to local SQLite or PostgreSQL.
  • CI-test: Applies schema and verifies DDL integrity.
  • staging / production: Enforces PostgreSQL control plane; applies migrations with safety checks.
  • rollback-check: Verifies schema compatibility and history retention for releases.

Usage:
  python scripts/db_migrate.py --status
  python scripts/db_migrate.py --up
  python scripts/db_migrate.py --check --json
  python scripts/db_migrate.py --rollback-check --target-version 1
"""
import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if os.path.isdir(_BACKEND_DIR) and _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from core.environment import get_environment_mode, is_fail_closed_environment


def load_env_file(path: str | None) -> None:
    """Load a deployment env file before resolving database configuration."""
    if not path:
        return
    env_path = os.path.abspath(path)
    if not os.path.isfile(env_path):
        raise FileNotFoundError(f"environment file not found: {env_path}")
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=True)
        return
    except ImportError:
        pass
    with open(env_path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def sanitize_dsn(dsn: str) -> str:
    import re
    if not dsn:
        return ""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", str(dsn))


def get_db_info():
    db_url = os.getenv("DATABASE_URL", "").strip()
    is_pg = db_url.startswith(("postgresql://", "postgres://"))
    is_sqlite = "sqlite" in db_url.lower() or not db_url
    mode = get_environment_mode()
    fail_closed = is_fail_closed_environment()

    return {
        "mode": mode.value,
        "fail_closed": fail_closed,
        "is_pg": is_pg,
        "is_sqlite": is_sqlite,
        "sanitized_url": sanitize_dsn(db_url) if db_url else "sqlite:///sessions.db",
    }


def _create_sa_engine(db_url: str, is_pg: bool):
    from sqlalchemy import create_engine
    if not db_url:
        db_url = "sqlite:///sessions.db"
    sa_url = db_url
    if is_pg and db_url.startswith("postgresql://") and not db_url.startswith("postgresql+"):
        sa_url = "postgresql+psycopg2://" + db_url[len("postgresql://"):]
    elif is_pg and db_url.startswith("postgres://"):
        sa_url = "postgresql+psycopg2://" + db_url[len("postgres://"):]
    connect_args = {"connect_timeout": 5} if is_pg else {}
    return create_engine(sa_url, connect_args=connect_args)


def check_status() -> dict:
    info = get_db_info()
    if info["fail_closed"] and not info["is_pg"]:
        return {
            "ok": False,
            "status": "BLOCKED",
            "error": "Fail-closed environment requires PostgreSQL DATABASE_URL. SQLite is forbidden in production/staging.",
            **info,
        }

    try:
        from sqlalchemy import inspect, text
        db_url = os.getenv("DATABASE_URL", "")
        engine = _create_sa_engine(db_url, info["is_pg"])
        insp = inspect(engine)
        tables = sorted(insp.get_table_names())

        required_tables = {"users", "sessions", "api_keys"}
        fabric_tables = {"fabric_queue_items", "fabric_workers", "fabric_scheduler_decisions", "fabric_schema_migrations"}
        d3_tables = {
            "d3_automations",
            "d3_automation_runs",
            "d3_kanban_cards",
            "d3_kg_nodes",
            "d3_kg_edges",
            "d3_worktrees",
            "d3_schema_migrations",
        }
        extension_tables = {
            "installed_skills", "skill_versions", "extension_audit_log",
            "extension_schema_migrations", "workspace_accounts", "workspace_members",
        }
        missing_core = [t for t in required_tables if t not in tables]
        missing_fabric = [t for t in fabric_tables if t not in tables]
        missing_d3 = [t for t in d3_tables if t not in tables]
        missing_extensions = [t for t in extension_tables if t not in tables] if info["is_pg"] else []

        extension_schema_version = None
        extension_schema_version_expected = None
        extension_schema_compatibility = {"ok": True, "version": None, "expected": None}
        if info["is_pg"]:
            from db.extension_migrations import EXTENSION_SCHEMA_VERSION

            extension_schema_version_expected = EXTENSION_SCHEMA_VERSION
            if "extension_schema_migrations" in tables:
                with engine.connect() as conn:
                    extension_schema_version = conn.execute(
                        text("SELECT MAX(version) FROM extension_schema_migrations")
                    ).scalar()
            extension_schema_compatibility = {
                "ok": extension_schema_version == extension_schema_version_expected,
                "version": extension_schema_version,
                "expected": extension_schema_version_expected,
            }

        # Verify Fabric migration compatibility if migration table exists
        fabric_compat = {"ok": True, "applied_count": 0, "pending_count": 0}
        if "fabric_schema_migrations" in tables:
            try:
                from execution.fabric.migrations import verify_schema_compatibility
                with engine.connect() as conn:
                    # Raw connection for DB-API cursor
                    raw_conn = getattr(conn.connection, "dbapi_connection", conn.connection)
                    fabric_compat = verify_schema_compatibility(raw_conn, is_postgres=info["is_pg"])
            except Exception as fe:
                fabric_compat = {"ok": False, "error": sanitize_dsn(str(fe))}
        else:
            fabric_compat = {"ok": False, "pending_count": 1, "error": "fabric_schema_migrations table not found"}

        # Verify D3 migration compatibility if migration table exists
        d3_compat = {"ok": True, "applied_count": 0, "pending_count": 0}
        if "d3_schema_migrations" in tables:
            try:
                from db.d3_migrations import verify_d3_schema_compatibility
                with engine.connect() as conn:
                    raw_conn = getattr(conn.connection, "dbapi_connection", conn.connection)
                    d3_compat = verify_d3_schema_compatibility(raw_conn, is_postgres=info["is_pg"])
            except Exception as de:
                d3_compat = {"ok": False, "error": sanitize_dsn(str(de))}
        else:
            d3_compat = {"ok": False, "pending_count": 1, "error": "d3_schema_migrations table not found"}

        is_all_clean = (
            (len(missing_core) == 0)
            and (len(missing_fabric) == 0)
            and (len(missing_d3) == 0)
            and (len(missing_extensions) == 0)
            and extension_schema_compatibility.get("ok", True)
            and fabric_compat.get("ok", True)
            and d3_compat.get("ok", True)
        )

        return {
            "ok": is_all_clean,
            "status": "UP_TO_DATE" if is_all_clean else "MIGRATIONS_PENDING",
            "table_count": len(tables),
            "tables": tables,
            "missing_required": missing_core,
            "missing_fabric": missing_fabric,
            "missing_d3": missing_d3,
            "missing_extensions": missing_extensions,
            "extension_schema_version": extension_schema_version,
            "extension_schema_version_expected": extension_schema_version_expected,
            "extension_schema_compatibility": extension_schema_compatibility,
            "fabric_compatibility": fabric_compat,
            "d3_compatibility": d3_compat,
            **info,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": "ERROR",
            "error": sanitize_dsn(str(e)),
            **info,
        }


def apply_migrations() -> dict:
    info = get_db_info()
    if info["fail_closed"] and not info["is_pg"]:
        raise RuntimeError("Refusing to apply migrations: PostgreSQL is mandatory in production/staging.")

    try:
        # 1. Initialize core tables via auth_system and sqlalchemy models
        try:
            from auth_system import init_db
            init_db()
        except Exception as _e:
            raise RuntimeError("Core authentication/workspace schema migration failed") from _e

        db_url = os.getenv("DATABASE_URL", "")
        engine = _create_sa_engine(db_url, info["is_pg"])

        try:
            from db.sqlalchemy_models import Base, _SA_AVAILABLE
            if _SA_AVAILABLE:
                Base.metadata.create_all(bind=engine)
        except Exception as _e:
            raise RuntimeError("SQLAlchemy schema creation failed") from _e

        # 2. Apply Execution Fabric migrations
        from execution.fabric.migrations import apply_fabric_migrations
        with engine.connect() as conn:
            raw_conn = getattr(conn.connection, "dbapi_connection", conn.connection)
            applied_fabric = apply_fabric_migrations(raw_conn, is_postgres=info["is_pg"])

        # 3. Apply D3 tracked migrations
        from db.d3_migrations import apply_d3_migrations
        with engine.connect() as conn:
            raw_conn = getattr(conn.connection, "dbapi_connection", conn.connection)
            applied_d3 = apply_d3_migrations(raw_conn, is_postgres=info["is_pg"])

        applied_extensions = []
        if info["is_pg"]:
            from db.extension_migrations import apply_extension_migrations
            with engine.connect() as conn:
                raw_conn = getattr(conn.connection, "dbapi_connection", conn.connection)
                applied_extensions = apply_extension_migrations(raw_conn)

        # 4. Check resulting tables
        status = check_status()
        return {
            "ok": status.get("ok", False),
            "message": "Migrations applied successfully",
            "applied_fabric_versions": applied_fabric,
            "applied_d3_versions": applied_d3,
            "applied_extension_versions": applied_extensions,
            "status": status,
            **info,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": sanitize_dsn(str(e)),
            **info,
        }


def check_rollback(target_version: int = 1) -> dict:
    """Verify rollback safety without altering state or erasing task history."""
    info = get_db_info()
    try:
        db_url = os.getenv("DATABASE_URL", "")
        engine = _create_sa_engine(db_url, info["is_pg"])

        from execution.fabric.migrations import verify_rollback_safety
        with engine.connect() as conn:
            raw_conn = conn.connection
            res = verify_rollback_safety(target_version, raw_conn, is_postgres=info["is_pg"])
            return {"ok": res.get("safe", False), **res, **info}
    except Exception as e:
        return {
            "ok": False,
            "error": sanitize_dsn(str(e)),
            **info,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aetherion DB Migration Controller")
    parser.add_argument("--status", action="store_true", help="Check migration and schema status")
    parser.add_argument("--check", action="store_true", help="Assert migrations are fully applied (exit 0 if clean, 1 if pending)")
    parser.add_argument("--up", action="store_true", help="Apply pending database migrations")
    parser.add_argument("--rollback-check", action="store_true", help="Assert schema backward compatibility and history preservation")
    parser.add_argument("--target-version", type=int, default=1, help="Target schema version for rollback verification")
    parser.add_argument("--json", action="store_true", help="Output status as JSON")
    parser.add_argument("--env-file", help="load deployment environment variables from this file")
    args = parser.parse_args()

    load_env_file(args.env_file)

    if not (args.status or args.check or args.up or args.rollback_check):
        args.status = True

    if args.rollback_check:
        res = check_rollback(target_version=args.target_version)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print("=" * 60)
            print("  Aetherion Rollback Safety Verification")
            print("=" * 60)
            print(f"  Safe to Rollback: {res.get('safe', False)}")
            print(f"  Current Version:  {res.get('current_version')}")
            print(f"  Target Version:   {res.get('target_version')}")
            print(f"  Preserved Tasks:  {res.get('completed_tasks_preserved', 0)}")
            print(f"  Contract:         {res.get('contract', '')}")
            if res.get("error"):
                print(f"  Error:            {res['error']}")
            print("=" * 60)
        return 0 if res.get("ok") else 1

    if args.up:
        res = apply_migrations()
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("ok"):
                print(f"[MIGRATIONS] Successfully applied migrations ({res.get('status', {}).get('table_count', 0)} tables).")
            else:
                print(f"[MIGRATIONS] FAILED: {res.get('error')}")
        return 0 if res.get("ok") else 1

    res = check_status()
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print("=" * 60)
        print(f"  Aetherion Database Status — Mode: {res.get('mode', 'unknown').upper()}")
        print(f"  Target: {res.get('sanitized_url')}")
        print("=" * 60)
        print(f"  Status:       {res.get('status')}")
        print(f"  Tables Found: {res.get('table_count', 0)}")
        if res.get("missing_required"):
            print(f"  Missing Core:   {', '.join(res['missing_required'])}")
        if res.get("missing_fabric"):
            print(f"  Missing Fabric: {', '.join(res['missing_fabric'])}")
        if res.get("missing_d3"):
            print(f"  Missing D3:     {', '.join(res['missing_d3'])}")
        if res.get("error"):
            print(f"  Error:        {res['error']}")
        print("=" * 60)

    if args.check:
        return 0 if res.get("ok") else 1
    return 0 if res.get("status") != "ERROR" else 1


if __name__ == "__main__":
    sys.exit(main())
