"""Repository-wide pytest isolation policy.

The integration tree contains live-service and failure-path checks.  Marking
the tree centrally keeps collection deterministic while allowing operators to
run it explicitly with ``pytest -m integration``.
"""

from __future__ import annotations

from pathlib import Path
import os

import pytest


@pytest.fixture(autouse=True)
def isolate_aetherion_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep CLI config and session state inside the current test's temp dir.

    The CLI config module and session store can be imported while pytest is
    collecting tests, before fixtures run.  Patch both the settings module and
    its package-level re-exports, then invalidate the session store's cached
    connection/path so a test cannot reuse a connection opened against the
    real home directory.
    """
    isolated_dir = tmp_path / "aetherion"
    isolated_config_file = isolated_dir / "config.json"
    isolated_skills_lib = isolated_dir / "skills-library"
    isolated_skills = isolated_dir / "skills"

    monkeypatch.setenv("AETHERION_HOME", str(isolated_dir))
    monkeypatch.setenv("AETHERION_CONFIG_DIR", str(isolated_dir))
    monkeypatch.setenv("AETHERION_SKILLS_LIBRARY_DIR", str(isolated_skills_lib))
    monkeypatch.setenv("AETHERION_SKILLS_DIR", str(isolated_skills))

    from aetherion_cli import config as config_package
    from aetherion_cli.config import settings

    monkeypatch.setattr(settings, "CONFIG_DIR", isolated_dir)
    monkeypatch.setattr(settings, "CONFIG_FILE", isolated_config_file)
    monkeypatch.setattr(config_package, "CONFIG_DIR", isolated_dir)
    monkeypatch.setattr(config_package, "CONFIG_FILE", isolated_config_file)

    try:
        from aetherion_cli import skill_library
        monkeypatch.setattr(skill_library, "DEFAULT_LIBRARY_DIR", isolated_skills_lib)
    except ImportError:
        pass

    try:
        from aetherion_cli.tui import skill_creator
        monkeypatch.setattr(skill_creator, "SKILLS_DIR", isolated_skills)
    except ImportError:
        pass

    def reset_session_store() -> None:
        """Close and invalidate session-store state without masking test errors."""
        try:
            from aetherion_cli import session_store
        except ImportError:
            return

        with session_store._db_lock:
            conn = session_store._conn
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            session_store._conn = None
            session_store._DB_PATH = None
            session_store._last_open_error = None

    reset_session_store()
    try:
        yield
    finally:
        reset_session_store()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    integration_enabled = os.environ.get("AETHERION_RUN_INTEGRATION", "").strip() == "1"
    for item in items:
        if "tests" in Path(str(item.fspath)).parts and "integration" in Path(str(item.fspath)).parts:
            item.add_marker(pytest.mark.integration)
            if not integration_enabled:
                item.add_marker(
                    pytest.mark.skip(
                        reason="external integration suite disabled; set AETHERION_RUN_INTEGRATION=1"
                    )
                )
