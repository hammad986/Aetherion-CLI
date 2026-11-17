"""Focused offline tests for the metadata-only GitHub plugin manager."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import pytest

from aetherion_cli.plugin_manager import (
    CURATED_CATALOG,
    PluginCatalogManager,
    PluginManagerError,
    canonicalize_github_url,
)

SOURCE_URL = "https://github.com/example/demo-plugin"
COMMIT_ONE = "1" * 40
COMMIT_TWO = "2" * 40


def make_archive(version: str, commit: str, unsafe: bool = False) -> bytes:
    buffer = io.BytesIO()
    root = f"demo-plugin-{commit[:8]}"
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if unsafe:
            archive.writestr("../escape.txt", "must not extract")
        else:
            archive.writestr(
                f"{root}/plugin.json",
                json.dumps(
                    {
                        "id": "demo-plugin",
                        "name": "Demo Plugin",
                        "version": version,
                        "description": "Synthetic test plugin metadata.",
                        "author": "tests",
                        "license": "MIT",
                        "entrypoint": "main.py:run",
                        "runtime": "python",
                        "required_dependencies": ["not-installed"],
                        "required_permissions": ["NETWORK_ACCESS"],
                    }
                ),
            )
            # This is deliberately source text only. The manager must never
            # import it or call it while inspecting or storing the archive.
            archive.writestr(f"{root}/main.py", "raise RuntimeError('must never execute')\n")
            archive.writestr(f"{root}/README.md", "Synthetic plugin source.\n")
    return buffer.getvalue()


def make_archive_entries(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def make_named_archive(plugin_id: str, commit: str) -> bytes:
    root = f"{plugin_id}-{commit[:8]}"
    return make_archive_entries(
        {
            f"{root}/plugin.json": json.dumps(
                {"id": plugin_id, "name": plugin_id.title(), "version": "1.0.0"}
            ),
            f"{root}/README.md": "Synthetic concurrent install.\n",
        }
    )


class FakeGitHub:
    def __init__(self) -> None:
        self.commit = COMMIT_ONE
        self.archive = make_archive("1.0.0", COMMIT_ONE)

    def json(self, url: str):
        assert url.startswith("https://api.github.com/repos/example/demo-plugin/commits/")
        return {"sha": self.commit}

    def bytes(self, url: str) -> bytes:
        assert url == f"https://codeload.github.com/example/demo-plugin/zip/{self.commit}"
        return self.archive


CATALOG_URL = "https://github.com/example/catalog-repo"
CATALOG_COMMIT_A = "a" * 40
CATALOG_COMMIT_B = "b" * 40


class CatalogFake:
    def __init__(self) -> None:
        self.commit = CATALOG_COMMIT_A
        self.archive = make_archive("1.0.0", CATALOG_COMMIT_A)

    def json(self, url: str):
        if "/search/repositories?" in url:
            return {
                "total_count": 2,
                "items": [
                    {
                        "name": "catalog-repo",
                        "full_name": "example/catalog-repo",
                        "html_url": CATALOG_URL,
                        "description": "A candidate repository",
                        "language": "Python",
                        "topics": ["aetherion", "plugin"],
                        "stargazers_count": 42,
                        "forks_count": 3,
                        "license": {"spdx_id": "MIT", "name": "MIT License"},
                        "archived": False,
                        "default_branch": "main",
                        "pushed_at": "2026-10-17T00:00:00Z",
                    },
                    {
                        "name": "archived-repo",
                        "full_name": "example/archived-repo",
                        "html_url": "https://github.com/example/archived-repo",
                        "description": "Archived candidate",
                        "language": "Rust",
                        "topics": [],
                        "stargazers_count": 9,
                        "forks_count": 1,
                        "license": None,
                        "archived": True,
                        "default_branch": "main",
                    },
                ],
            }
        if url == "https://api.github.com/repos/example/catalog-repo":
            return {
                "name": "catalog-repo",
                "full_name": "example/catalog-repo",
                "html_url": CATALOG_URL,
                "description": "A saved candidate repository",
                "language": "Python",
                "topics": ["aetherion"],
                "stargazers_count": 42,
                "forks_count": 3,
                "license": {"spdx_id": "MIT", "name": "MIT License"},
                "archived": False,
                "default_branch": "main",
                "pushed_at": "2026-10-17T00:00:00Z",
            }
        if "/commits/" in url:
            return {"sha": self.commit}
        raise AssertionError(f"unexpected fake GitHub URL: {url}")

    def bytes(self, url: str) -> bytes:
        assert url == f"https://codeload.github.com/example/catalog-repo/zip/{self.commit}"
        return self.archive


def make_catalog_manager(tmp_path: Path, fake: Optional[CatalogFake] = None):
    fake = fake or CatalogFake()
    manager = PluginCatalogManager(
        root=tmp_path / "plugins",
        json_fetcher=fake.json,
        bytes_fetcher=fake.bytes,
    )
    return manager, fake


def make_manager(tmp_path: Path, fake: Optional[FakeGitHub] = None) -> tuple[PluginCatalogManager, FakeGitHub]:
    fake = fake or FakeGitHub()
    manager = PluginCatalogManager(
        root=tmp_path / "plugins",
        json_fetcher=fake.json,
        bytes_fetcher=fake.bytes,
    )
    return manager, fake


@pytest.mark.parametrize(
    "source",
    [
        "http://github.com/example/repo",
        "https://gitlab.com/example/repo",
        "https://github.com/example/repo/tree/main",
        "https://user:password@github.com/example/repo",
        "https://github.com/example/repo?download=1",
        "https://github.com/example/../repo",
    ],
)
def test_github_source_policy_rejects_ambiguous_urls(source: str) -> None:
    with pytest.raises(PluginManagerError):
        canonicalize_github_url(source)


def test_curated_rea_metadata_is_explicitly_non_executable(tmp_path: Path) -> None:
    manager = PluginCatalogManager(root=tmp_path / "plugins")
    rea = manager.info("rea")
    assert rea is not None
    assert rea["source_url"] == "https://github.com/morluto/rea"
    assert rea["license"] == "MIT"
    assert rea["package_name"] == "rea-agents"
    assert rea["execution_enabled"] is False
    assert rea["installed"] is False


def test_curated_catalog_has_exact_pinned_metadata_only_entries(tmp_path: Path) -> None:
    expected = {
        "rea": "https://github.com/morluto/rea",
        "repomix": "https://github.com/yamadashy/repomix",
        "browser-use": "https://github.com/browser-use/browser-use",
        "mcp-filesystem": "https://github.com/modelcontextprotocol/servers",
        "mcp-github": "https://github.com/modelcontextprotocol/servers",
        "ast-grep": "https://github.com/ast-grep/ast-grep",
        "ripgrep-all": "https://github.com/phiresky/ripgrep-all",
        "trufflehog": "https://github.com/trufflesecurity/trufflehog",
        "code-interpreter": "https://github.com/dryvist/nix-agent-sandbox",
        "aider-tools": "https://github.com/Aider-AI/aider",
    }

    manager = PluginCatalogManager(root=tmp_path / "plugins")
    catalog = manager.list_catalog(include_installed=False)

    assert [entry.id for entry in CURATED_CATALOG] == list(expected)
    assert {entry.id: entry.source_url for entry in CURATED_CATALOG} == expected
    assert len(CURATED_CATALOG) == 10
    for entry in catalog:
        if entry["id"] not in expected:
            continue
        assert len(entry["verified_commit"]) == 40
        int(entry["verified_commit"], 16)
        assert entry["source_url"] == expected[entry["id"]]
        assert entry["source_type"] == "curated"
        assert entry["execution_enabled"] is False
        assert entry["execution_status"] == "disabled"


def test_user_catalog_persists_alias_and_does_not_install_source(tmp_path: Path) -> None:
    manager, _fake = make_catalog_manager(tmp_path)
    saved = manager.catalog_add("custom-repo", CATALOG_URL, ref="main")
    assert saved["id"] == "custom-repo"
    assert saved["source_type"] == "user"
    assert saved["resolved_commit"] == CATALOG_COMMIT_A
    assert saved["source_plugin_id"] == "demo-plugin"
    assert saved["compatibility_status"] == "unknown"
    assert not (tmp_path / "plugins" / "demo-plugin").exists()

    reopened, _fake = make_catalog_manager(tmp_path)
    records = reopened.catalog_list()
    custom = next(item for item in records if item["id"] == "custom-repo")
    assert custom["requested_ref"] == "main"
    assert custom["resolved_commit"] == CATALOG_COMMIT_A
    assert custom["installed"] is False
    assert reopened.catalog_remove("custom-repo") is True
    assert not any(item["id"] == "custom-repo" for item in reopened.catalog_list())
    assert any(item["id"] == "rea" for item in reopened.catalog_list())


def test_catalog_aliases_are_unique_and_curated_ids_are_protected(tmp_path: Path) -> None:
    manager, _fake = make_catalog_manager(tmp_path)
    manager.catalog_add("custom-repo", CATALOG_URL)
    with pytest.raises(PluginManagerError, match="already exists"):
        manager.catalog_add("custom-repo", "https://github.com/example/other")
    with pytest.raises(PluginManagerError, match="reserved"):
        manager.catalog_add("rea", "https://github.com/example/other")
    with pytest.raises(PluginManagerError, match="reserved"):
        manager.catalog_remove("rea")


def test_catalog_refresh_reports_candidate_without_moving_pin(tmp_path: Path) -> None:
    manager, fake = make_catalog_manager(tmp_path)
    manager.catalog_add("custom-repo", CATALOG_URL)
    fake.commit = CATALOG_COMMIT_B
    fake.archive = make_archive("1.1.0", CATALOG_COMMIT_B)
    refreshed = manager.catalog_refresh("custom-repo")
    assert refreshed["resolved_commit"] == CATALOG_COMMIT_A
    assert refreshed["candidate_commit"] == CATALOG_COMMIT_B
    assert refreshed["update_available"] is True

    pinned = manager.catalog_pin("custom-repo", "main")
    assert pinned["resolved_commit"] == CATALOG_COMMIT_B
    assert pinned["update_available"] is False


def test_discovery_is_bounded_read_only_and_marks_compatibility_unknown(tmp_path: Path) -> None:
    manager, _fake = make_catalog_manager(tmp_path)
    results = manager.discover("aetherion", limit=2, language="Python", min_stars=10)
    assert len(results) == 1
    assert results[0]["source_url"] == CATALOG_URL
    assert results[0]["license_declared"] is True
    assert results[0]["compatibility_status"] == "unknown"
    assert results[0]["resolved_commit"] is None
    assert not manager.catalog_path.exists()


def test_source_only_repository_can_be_inspected_and_stored(tmp_path: Path) -> None:
    fake = CatalogFake()
    fake.archive = make_archive_entries({"catalog-repo-main/README.md": "source only\n"})
    manager, _fake = make_catalog_manager(tmp_path, fake)
    inspected = manager.inspect(CATALOG_URL)
    assert inspected["source_only"] is True
    assert inspected["compatibility_status"] == "unknown"
    installed = manager.install(CATALOG_URL)
    assert installed["execution_enabled"] is False
    assert installed["compatibility_status"] == "unknown"
    assert manager.status(installed["id"])["integrity"] == "verified"


def test_explicit_plugin_api_classification_is_conservative(tmp_path: Path) -> None:
    fake = CatalogFake()
    fake.archive = make_archive_entries(
        {
            "catalog-repo-main/aetherion.plugin.json": json.dumps(
                {
                    "id": "catalog-repo",
                    "name": "Catalog Repo",
                    "version": "1.0.0",
                    "manifest_schema_version": "1",
                    "plugin_api_version": "1.0",
                }
            )
        }
    )
    manager, _fake = make_catalog_manager(tmp_path, fake)
    assert manager.inspect(CATALOG_URL)["compatibility_status"] == "compatible"
    fake.archive = make_archive_entries(
        {
            "catalog-repo-main/aetherion.plugin.json": json.dumps(
                {
                    "id": "catalog-repo",
                    "name": "Catalog Repo",
                    "version": "1.0.0",
                    "manifest_schema_version": "1",
                    "plugin_api_version": "9.0",
                }
            )
        }
    )
    assert manager.inspect(CATALOG_URL)["compatibility_status"] == "incompatible"


def test_generic_api_metadata_without_explicit_contract_is_unknown(tmp_path: Path) -> None:
    fake = CatalogFake()
    fake.archive = make_archive_entries(
        {
            "catalog-repo-main/plugin.json": json.dumps(
                {
                    "id": "catalog-repo",
                    "name": "Catalog Repo",
                    "version": "1.0.0",
                    "plugin_api_version": "1.0",
                }
            )
        }
    )
    manager, _fake = make_catalog_manager(tmp_path, fake)
    result = manager.inspect(CATALOG_URL)
    assert result["compatibility_status"] == "unknown"
    assert result["compatibility_reasons"] == ["no_explicit_aetherion_plugin_contract"]


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (
            {"id": "catalog-repo", "name": "Catalog Repo", "version": "1.0.0", "manifest_schema_version": "1"},
            "plugin_api_version",
        ),
        (
            {
                "id": "catalog-repo",
                "name": "Catalog Repo",
                "version": "1.0.0",
                "manifest_schema_version": "2",
                "plugin_api_version": "1.0",
            },
            "Unsupported Aetherion manifest schema",
        ),
        (
            {
                "id": "catalog-repo",
                "name": "Catalog Repo",
                "version": "1.0.0",
                "manifest_schema_version": "1",
                "plugin_api_version": 1,
            },
            "must be a string",
        ),
    ],
)
def test_explicit_manifest_contract_rejects_malformed_or_unsupported_fields(
    tmp_path: Path,
    manifest: dict,
    message: str,
) -> None:
    fake = CatalogFake()
    fake.archive = make_archive_entries({"catalog-repo-main/aetherion.plugin.json": json.dumps(manifest)})
    manager, _fake = make_catalog_manager(tmp_path, fake)
    with pytest.raises(PluginManagerError, match=message):
        manager.inspect(CATALOG_URL)


def test_catalog_alias_install_uses_saved_pin_and_update_requires_explicit_pin(tmp_path: Path) -> None:
    class SnapshotCatalogFake(CatalogFake):
        def __init__(self) -> None:
            super().__init__()
            self.archives = {CATALOG_COMMIT_A: self.archive}
            self.requested_commits: list[str] = []

        def bytes(self, url: str) -> bytes:
            commit = url.rsplit("/", 1)[-1]
            self.requested_commits.append(commit)
            return self.archives[commit]

    fake = SnapshotCatalogFake()
    manager, _fake = make_catalog_manager(tmp_path, fake)
    manager.catalog_add("saved-repo", CATALOG_URL, ref="main")
    fake.commit = CATALOG_COMMIT_B
    fake.archives[CATALOG_COMMIT_B] = make_archive("1.1.0", CATALOG_COMMIT_B)

    installed = manager.install("saved-repo")
    assert installed["resolved_commit"] == CATALOG_COMMIT_A
    assert installed["catalog_id"] == "saved-repo"
    assert installed["integrity"] == "verified"
    assert fake.requested_commits[-1] == CATALOG_COMMIT_A
    with pytest.raises(PluginManagerError, match="pinned commit"):
        manager.install("saved-repo", ref="main")

    unchanged = manager.update("demo-plugin")
    assert unchanged["resolved_commit"] == CATALOG_COMMIT_A
    assert unchanged["integrity"] == "verified"
    pinned = manager.catalog_pin("saved-repo", "main")
    assert pinned["resolved_commit"] == CATALOG_COMMIT_B
    updated = manager.update("saved-repo")
    assert updated["resolved_commit"] == CATALOG_COMMIT_B
    assert manager.status("saved-repo")["integrity"] == "verified"


def test_catalog_removal_and_source_uninstall_are_independent(tmp_path: Path) -> None:
    manager, _fake = make_catalog_manager(tmp_path)
    manager.catalog_add("saved-repo", CATALOG_URL)
    manager.install("saved-repo")
    assert manager.catalog_remove("saved-repo") is True
    assert manager.status("demo-plugin")["integrity"] == "verified"
    assert manager.remove("demo-plugin") is True
    assert manager.status() == []


def test_catalog_source_id_mismatch_fails_closed_before_promotion(tmp_path: Path) -> None:
    fake = CatalogFake()
    manager, _fake = make_catalog_manager(tmp_path, fake)
    manager.catalog_add("saved-repo", CATALOG_URL)
    fake.archive = make_archive_entries(
        {
            "catalog-repo-main/plugin.json": json.dumps(
                {"id": "different-plugin", "name": "Different Plugin", "version": "1.0.0"}
            )
        }
    )
    with pytest.raises(PluginManagerError, match="Saved catalog source ID"):
        manager.install("saved-repo")
    assert manager.status() == []


def test_corrupt_catalog_fails_closed(tmp_path: Path) -> None:
    manager, _fake = make_catalog_manager(tmp_path)
    manager.catalog_path.write_text(json.dumps({"version": 999, "entries": {}}), encoding="utf-8")
    with pytest.raises(PluginManagerError, match="schema version"):
        manager.catalog_list()


def test_inspect_validates_and_never_executes_source(tmp_path: Path) -> None:
    manager, _fake = make_manager(tmp_path)
    result = manager.inspect(SOURCE_URL, ref="main")
    assert result["id"] == "demo-plugin"
    assert result["resolved_commit"] == COMMIT_ONE
    assert result["manifest_file"] == "plugin.json"
    assert result["dependencies"] == ["not-installed"]
    assert result["requested_permissions"] == ["NETWORK_ACCESS"]
    assert result["execution_enabled"] is False
    assert result["installed"] is False
    assert not (tmp_path / "must-never-execute").exists()


def test_install_is_atomic_idempotent_and_integrity_checked(tmp_path: Path) -> None:
    manager, fake = make_manager(tmp_path)
    installed = manager.install(SOURCE_URL, ref="main")
    assert installed["status"] == "installed"
    assert installed["integrity"] == "verified"
    assert installed["execution_enabled"] is False
    assert manager.status("demo-plugin")["integrity"] == "verified"

    repeated = manager.install(SOURCE_URL, ref="main")
    assert repeated["idempotent"] is True

    source_root = tmp_path / "plugins" / "demo-plugin" / installed["source_root"]
    (source_root / "main.py").write_text("tampered", encoding="utf-8")
    assert manager.status("demo-plugin")["integrity"] == "mismatch"

    fake.commit = COMMIT_TWO
    fake.archive = make_archive("1.1.0", COMMIT_TWO)
    updated = manager.update("demo-plugin", ref="main")
    assert updated["version"] == "1.1.0"
    assert updated["resolved_commit"] == COMMIT_TWO
    assert manager.status("demo-plugin")["integrity"] == "verified"


def test_remove_only_deletes_managed_record(tmp_path: Path) -> None:
    manager, _fake = make_manager(tmp_path)
    manager.install(SOURCE_URL)
    outside = tmp_path / "outside.txt"
    outside.write_text("preserve", encoding="utf-8")
    assert manager.remove("demo-plugin") is True
    assert manager.status() == []
    assert outside.read_text(encoding="utf-8") == "preserve"


def test_archive_traversal_is_rejected_before_promotion(tmp_path: Path) -> None:
    fake = FakeGitHub()
    fake.archive = make_archive("1.0.0", COMMIT_ONE, unsafe=True)
    manager, _fake = make_manager(tmp_path, fake)
    with pytest.raises(PluginManagerError, match="traversal"):
        manager.install(SOURCE_URL)
    assert not (tmp_path / "plugins" / "demo-plugin").exists()


def test_integrity_manifest_tampering_is_not_authoritative(tmp_path: Path) -> None:
    manager, _fake = make_manager(tmp_path)
    installed = manager.install(SOURCE_URL)
    integrity_path = tmp_path / "plugins" / "demo-plugin" / "integrity.json"
    integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    integrity["files"]["main.py"] = hashlib.sha256(b"forged source").hexdigest()
    integrity_path.write_text(json.dumps(integrity), encoding="utf-8")
    assert manager.status("demo-plugin")["integrity"] == "mismatch"
    assert installed["execution_enabled"] is False


def test_failed_remove_restores_source_and_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _fake = make_manager(tmp_path)
    manager.install(SOURCE_URL)

    def fail_registry(_records):
        raise OSError("simulated registry failure")

    monkeypatch.setattr(manager, "_write_registry", fail_registry)
    with pytest.raises(OSError, match="simulated"):
        manager.remove("demo-plugin")
    assert (tmp_path / "plugins" / "demo-plugin").is_dir()
    assert manager.status("demo-plugin")["integrity"] == "verified"


def test_failed_update_recovers_previous_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, fake = make_manager(tmp_path)
    manager.install(SOURCE_URL)
    fake.commit = COMMIT_TWO
    fake.archive = make_archive("1.1.0", COMMIT_TWO)

    def fail_registry(_records):
        raise OSError("simulated registry failure")

    monkeypatch.setattr(manager, "_write_registry", fail_registry)
    with pytest.raises(OSError, match="simulated"):
        manager.update("demo-plugin")
    assert manager.info("demo-plugin")["version"] == "1.0.0"
    assert manager.status("demo-plugin")["resolved_commit"] == COMMIT_ONE
    assert not (tmp_path / "plugins" / ".transaction.json").exists()


def test_startup_recovers_after_registry_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, fake = make_manager(tmp_path)
    manager.install(SOURCE_URL)
    fake.commit = COMMIT_TWO
    fake.archive = make_archive("1.1.0", COMMIT_TWO)

    def fail_clear():
        raise OSError("simulated process interruption")

    monkeypatch.setattr(manager, "_clear_journal", fail_clear)
    with pytest.raises(OSError, match="interruption"):
        manager.update("demo-plugin")
    assert (tmp_path / "plugins" / ".transaction.json").exists()

    recovered = PluginCatalogManager(
        root=tmp_path / "plugins",
        json_fetcher=fake.json,
        bytes_fetcher=fake.bytes,
    )
    assert recovered.status("demo-plugin")["integrity"] == "verified"
    assert recovered.info("demo-plugin")["version"] == "1.1.0"
    assert not (tmp_path / "plugins" / ".transaction.json").exists()


def test_malformed_registry_id_is_reported_without_path_access(tmp_path: Path) -> None:
    manager, _fake = make_manager(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    manager.registry_path.write_text(
        json.dumps({"version": 1, "plugins": {"../outside": {"id": "../outside"}}}),
        encoding="utf-8",
    )
    records = manager.list_installed()
    assert records[0]["integrity"] == "invalid-id"
    assert outside.is_dir()


def test_registry_rejects_ambiguous_or_nonfinite_json(tmp_path: Path) -> None:
    manager, _fake = make_manager(tmp_path)
    manager.registry_path.write_text('{"version":1,"version":1,"plugins":{}}', encoding="utf-8")
    with pytest.raises(PluginManagerError, match="unreadable"):
        manager._read_registry()

    manager.registry_path.write_text('{"version":1,"plugins":{"demo-plugin":{"stars":NaN}}}', encoding="utf-8")
    with pytest.raises(PluginManagerError, match="unreadable"):
        manager._read_registry()


def test_staging_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / ".staging").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(PluginManagerError, match="staging"):
        PluginCatalogManager(root=root)


def test_plugin_alias_symlink_cannot_be_removed(tmp_path: Path) -> None:
    manager, _fake = make_manager(tmp_path)
    manager.install(SOURCE_URL)
    alias = tmp_path / "plugins" / "alias"
    target = tmp_path / "plugins" / "demo-plugin"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    records = manager._read_registry()
    alias_record = dict(records["demo-plugin"])
    alias_record["id"] = "alias"
    records["alias"] = alias_record
    manager._write_registry(records)
    with pytest.raises(PluginManagerError, match="symlink"):
        manager.remove("alias")
    assert target.is_dir()


def test_normalized_archive_collisions_are_rejected(tmp_path: Path) -> None:
    fake = FakeGitHub()
    manifest = json.dumps({"id": "demo-plugin", "name": "Demo Plugin", "version": "1.0.0"})
    fake.archive = make_archive_entries(
        {
            "root/plugin.json": manifest,
            "root/file.txt": "one",
            "root/./file.txt": "two",
        }
    )
    manager, _fake = make_manager(tmp_path, fake)
    with pytest.raises(PluginManagerError, match="normalized"):
        manager.install(SOURCE_URL)
    assert not (tmp_path / "plugins" / "demo-plugin").exists()


def test_archive_entry_limit_remains_bounded(tmp_path: Path) -> None:
    fake = FakeGitHub()
    fake.archive = make_archive_entries({f"root/file-{index}.txt": "x" for index in range(5001)})
    manager, _fake = make_manager(tmp_path, fake)
    with pytest.raises(PluginManagerError, match="5,000-entry"):
        manager.install(SOURCE_URL)
    assert not (tmp_path / "plugins" / "demo-plugin").exists()


def test_curated_rea_id_is_pinned_to_verified_commit(tmp_path: Path) -> None:
    entry = CURATED_CATALOG[0]
    root = f"rea-{entry.verified_commit[:8]}"
    archive = make_archive_entries(
        {
            f"{root}/package.json": json.dumps(
                {
                    "name": "rea-agents",
                    "version": "6.5.0",
                    "description": "REA metadata fixture",
                    "author": "morluto",
                    "license": "MIT",
                    "repository": {"url": "https://github.com/morluto/rea.git"},
                }
            )
        }
    )

    def no_api_call(_url: str):
        raise AssertionError("curated install must not resolve mutable main")

    def archive_fetcher(url: str) -> bytes:
        assert url.endswith(f"/zip/{entry.verified_commit}")
        return archive

    manager = PluginCatalogManager(
        root=tmp_path / "plugins",
        json_fetcher=no_api_call,
        bytes_fetcher=archive_fetcher,
    )
    inspected = manager.inspect("rea", ref="main")
    assert inspected["resolved_commit"] == entry.verified_commit
    assert inspected["catalog_commit_verified"] is True
    assert inspected["catalog_auth_status"] == "unsigned"

    with pytest.raises(PluginManagerError, match="requires a valid signed catalog"):
        manager.install("rea", ref="main")
    assert not (tmp_path / "plugins" / "rea").exists()

    with pytest.raises(PluginManagerError, match="verified commit"):
        manager.install("rea", ref=COMMIT_TWO)


def test_concurrent_managers_preserve_registry_records(tmp_path: Path) -> None:
    commit_a = "3" * 40
    commit_b = "4" * 40

    class NamedFake:
        def __init__(self, plugin_id: str, commit: str) -> None:
            self.plugin_id = plugin_id
            self.commit = commit

        def json(self, _url: str):
            return {"sha": self.commit}

        def bytes(self, url: str) -> bytes:
            assert url.endswith(f"/zip/{self.commit}")
            return make_named_archive(self.plugin_id, self.commit)

    fake_a = NamedFake("alpha", commit_a)
    fake_b = NamedFake("bravo", commit_b)
    root = tmp_path / "plugins"
    manager_a = PluginCatalogManager(root=root, json_fetcher=fake_a.json, bytes_fetcher=fake_a.bytes)
    manager_b = PluginCatalogManager(root=root, json_fetcher=fake_b.json, bytes_fetcher=fake_b.bytes)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(manager_a.install, "https://github.com/example/alpha"),
            pool.submit(manager_b.install, "https://github.com/example/bravo"),
        ]
        [future.result() for future in futures]

    assert {item["id"] for item in manager_a.list_installed()} == {"alpha", "bravo"}
