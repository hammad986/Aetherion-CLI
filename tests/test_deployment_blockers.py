"""Regression coverage for offline discovery and turnkey release defaults."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_auth_module_import_does_not_connect_to_postgres() -> None:
    env = os.environ.copy()
    env.update(
        {
            "AETHERION_ENV": "CI-test",
            "ENVIRONMENT": "testing",
            "DATABASE_URL": "postgresql://127.0.0.1:59999/aetherion_test",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "import backend.auth_system; print('import-ok')"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import-ok"


def test_preflight_json_is_machine_readable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/production_preflight.py", "--config", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in {0, 1}
    payload = json.loads(result.stdout)
    assert payload["status"] in {"READY", "NOT_READY"}
    assert "results" in payload


def test_production_compose_config_is_valid() -> None:
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                "deploy/production.env.example",
                "-f",
                "deploy/compose/docker-compose.prod.yml",
                "config",
                "--quiet",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return
    assert result.returncode == 0, result.stderr
