"""Focused checks for the repository-owned Option A release artifacts."""
from __future__ import annotations

from pathlib import Path

from core.preflight import CheckStatus, check_allowed_origins


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_compose_artifacts_declare_the_source_locked_control_plane_scope():
    for relative in (
        "deploy/compose/docker-compose.yml",
        "deploy/compose/docker-compose.preflight.yml",
        "deploy/compose/docker-compose.prod.yml",
    ):
        text = _read(relative)
        assert "AETHERION_RELEASE_SCOPE: control-plane" in text
        assert 'AETHERION_EXECUTION_DISABLED: "1"' in text
        assert "REQUIRE_SANDBOX: ${REQUIRE_SANDBOX:-false}" not in text
        assert 'LLM_PROVIDER: ${LLM_PROVIDER:-mock}' not in text


def test_production_compose_requires_external_runtime_identity_and_origins():
    text = _read("deploy/compose/docker-compose.prod.yml")
    assert 'SERVER_URL: "${SERVER_URL:?Error: SERVER_URL must be configured for production}"' in text
    assert 'FRONTEND_URL: "${FRONTEND_URL:?Error: FRONTEND_URL must be configured for production}"' in text
    assert 'ALLOWED_ORIGINS: "${ALLOWED_ORIGINS:?Error: ALLOWED_ORIGINS must be configured for production}"' in text


def test_safe_environment_templates_do_not_present_mock_execution_as_available():
    compose_template = _read("deploy/compose/.env.example")
    production_template = _read("deploy/production.env.example")
    for text in (compose_template, production_template):
        assert "AETHERION_RELEASE_SCOPE=control-plane" in text
        assert "AETHERION_EXECUTION_DISABLED=1" in text
        assert "LLM_PROVIDER=mock" not in text
    assert "REQUIRE_SANDBOX=false" not in compose_template


def test_cli_uses_the_server_owned_d3_workflow_state_enum():
    text = _read("cli/aetherion_cli/main.py")
    expected = '["backlog", "todo", "in_progress", "review", "blocked", "done", "archived"]'
    assert f"_KANBAN_COLUMNS = {expected}" in text


def test_release_docs_disclose_control_plane_only_scope():
    readme = _read("README.md")
    checklist = _read("deploy/DEPLOYMENT_CHECKLIST.md")
    runbook = _read("deploy/STAGING_RUNBOOK.md")
    for text in (readme, checklist, runbook):
        assert "control-plane" in text.lower()
        assert "execution" in text.lower()
    assert "not a production deployment" in readme
    assert "approval" in readme


def test_fail_closed_origin_policy_rejects_missing_and_wildcard_origins(monkeypatch):
    monkeypatch.setenv("AETHERION_ENV", "production")
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    assert check_allowed_origins().status == CheckStatus.FAIL

    monkeypatch.setenv("ALLOWED_ORIGINS", "*")
    assert check_allowed_origins().status == CheckStatus.FAIL

    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.invalid")
    assert check_allowed_origins().status == CheckStatus.PASS
