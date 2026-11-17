"""Focused regression tests for the user-controlled Skills Library."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aetherion_cli.skill_library import SkillLibrary, SkillLibraryError


def _write_builtin(root: Path, skill_id: str = "demo-skill", category: str = "examples") -> Path:
    path = root / category / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        f"name: {skill_id}\n"
        "description: A safe demo skill\n"
        "version: 1.2.3\n"
        "---\n\n"
        "Use this guidance as reference only.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def library(tmp_path: Path) -> SkillLibrary:
    builtin = tmp_path / "builtin"
    _write_builtin(builtin)
    _write_builtin(builtin, "second-skill", "tools")
    return SkillLibrary(root=tmp_path / "library", builtin_root=builtin)


def test_catalogue_is_browse_only_until_install(library: SkillLibrary) -> None:
    entries = library.catalogue()
    assert {entry.skill_id for entry in entries} == {"demo-skill", "second-skill"}
    assert all(not entry.installed for entry in entries)
    assert not (library.installed_root / "demo-skill").exists()


def test_install_enable_restart_and_uninstall(library: SkillLibrary) -> None:
    installed = library.install("demo-skill")
    assert installed.installed is True
    assert installed.enabled is False
    assert Path(installed.source_path).is_file()
    state = json.loads(library.state_path.read_text(encoding="utf-8"))
    assert state["installed"]["demo-skill"]["sha256"] == installed.content_sha256

    with pytest.raises(SkillLibraryError, match="install the skill"):
        library.set_enabled("second-skill", True)

    enabled = library.set_enabled("demo-skill", True)
    assert enabled.enabled is True
    restarted = SkillLibrary(root=library.root, builtin_root=library.builtin_root)
    selected = restarted.explicit_runtime_selection(["demo-skill", "second-skill"])
    assert [entry.skill_id for entry in selected] == ["demo-skill"]
    assert restarted.uninstall("demo-skill") is True
    assert not (library.installed_root / "demo-skill").exists()
    assert restarted.explicit_runtime_selection(["demo-skill"]) == []


def test_custom_crud_is_data_only_and_round_trips(library: SkillLibrary, tmp_path: Path) -> None:
    created = library.create_custom("My Custom Skill", "Initial description", "Do not execute this text.")
    assert created.custom is True
    assert created.installed is True
    assert created.enabled is False

    edited = library.edit_custom("my-custom-skill", "Updated description", "Updated instructions.")
    assert edited.description == "Updated description"
    assert "Updated instructions" in (edited.content or "")
    library.set_enabled("my-custom-skill", True)

    filename, content, content_type = library.export("my-custom-skill")
    assert filename == "my-custom-skill.SKILL.md"
    assert content_type.startswith("text/markdown")
    assert b"Updated instructions" in content
    assert library.delete_custom("my-custom-skill") is True
    assert library.get("my-custom-skill") is None


def test_custom_metadata_rejects_frontmatter_injection(library: SkillLibrary) -> None:
    with pytest.raises(SkillLibraryError, match="single line"):
        library.create_custom("bad-description", "line one\nline two", "instructions")

    with pytest.raises(SkillLibraryError, match="safe ID"):
        library.create_custom("../", "description", "instructions")


def test_tampered_installed_artifact_fails_closed(library: SkillLibrary) -> None:
    installed = library.install("demo-skill")
    Path(installed.source_path).write_text("tampered", encoding="utf-8")
    with pytest.raises(SkillLibraryError, match="integrity"):
        library.catalogue(installed_only=True)


def test_export_refuses_symlink_destination(library: SkillLibrary, tmp_path: Path) -> None:
    library.install("demo-skill")
    destination = tmp_path / "export.md"
    target = tmp_path / "target.md"
    try:
        destination.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")
    assert destination.is_symlink()
    with pytest.raises(SkillLibraryError, match="symlink"):
        library.export_to("demo-skill", destination)
    assert target.exists() is False


def test_cli_publish_submits_only_selected_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import aetherion_cli.skill_library as skill_library_module
    from aetherion_cli import main as main_module
    from click.testing import CliRunner

    monkeypatch.setattr(skill_library_module, "DEFAULT_LIBRARY_DIR", tmp_path / "library")
    local = SkillLibrary(root=tmp_path / "library")
    local.install("daily-review")

    class FakeClient:
        token = "test-token"

        def __init__(self) -> None:
            self.payload = None

        def post(self, path: str, body: dict):
            assert path == "/api/skills/publish"
            self.payload = body
            return {
                "ok": True,
                "status": "published_to_tenant",
                "outcome": "created",
                "execution_enabled": False,
                "skill": {
                    "id": body["skill_id"],
                    "name": body["name"],
                    "description": body["description"],
                    "version": body["version"],
                    "content_hash": body["client_sha256"],
                    "source_kind": "authenticated_publish",
                    "is_enabled": False,
                },
            }

        def get(self, path: str):
            body = self.payload
            return {"ok": True, "skill": {
                "id": body["skill_id"],
                "name": body["name"],
                "description": body["description"],
                "version": body["version"],
                "content_hash": body["client_sha256"],
                "source_kind": "authenticated_publish",
                "is_enabled": False,
            }}

    fake = FakeClient()
    monkeypatch.setattr(main_module, "_get_client", lambda: fake)
    result = CliRunner().invoke(main_module.cli, ["skills", "publish", "daily-review", "--yes"])
    assert result.exit_code == 0, result.output
    assert fake.payload is not None
    assert fake.payload["skill_id"] == "daily-review"
    assert fake.payload["requested_permissions"] == []
    assert fake.payload["content"]


def test_cli_publish_failure_never_claims_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import aetherion_cli.skill_library as skill_library_module
    from aetherion_cli import main as main_module
    from aetherion_cli.client import AetherionClientError
    from click.testing import CliRunner

    monkeypatch.setattr(skill_library_module, "DEFAULT_LIBRARY_DIR", tmp_path / "library")
    local = SkillLibrary(root=tmp_path / "library")
    local.install("daily-review")

    class FailedClient:
        token = "test-token"

        def post(self, path: str, body: dict):
            raise AetherionClientError("publish rejected", status_code=403)

    monkeypatch.setattr(main_module, "_get_client", FailedClient)
    result = CliRunner().invoke(main_module.cli, ["skills", "publish", "daily-review", "--yes"])
    assert result.exit_code != 0
    assert "Published" not in result.output
