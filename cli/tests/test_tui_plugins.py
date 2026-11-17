"""Deterministic TUI discovery/catalog tests using the real gateway paths."""
from __future__ import annotations

import io
import json
import threading
import zipfile
from unittest.mock import MagicMock

import pytest
from textual.app import App, ComposeResult

import aetherion_cli.tui.gateway as gateway_module
from aetherion_cli.tui.gateway import Gateway
from aetherion_cli.tui.screens.plugins import PluginsScreen, _safe_label
from aetherion_cli.tui.state import Plugin, PluginCandidate, state


LIVE_COMMIT = "a" * 40


def _demo_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "demo-main/plugin.json",
            json.dumps({"id": "demo", "name": "Demo", "version": "1.0.0"}),
        )
        archive.writestr("demo-main/README.md", "safe source fixture\n")
    return buffer.getvalue()


class _TuiLifecycleGitHub:
    def __init__(self) -> None:
        self.archive = _demo_archive()

    def json(self, url: str):
        if url == "https://api.github.com/repos/owner/demo":
            return {
                "name": "demo",
                "full_name": "owner/demo",
                "html_url": "https://github.com/owner/demo",
                "description": "TUI fixture",
                "language": "Python",
                "topics": ["aetherion"],
                "stargazers_count": 3,
                "forks_count": 1,
                "license": {"spdx_id": "MIT", "name": "MIT License"},
                "default_branch": "main",
            }
        if "/commits/" in url:
            return {"sha": LIVE_COMMIT}
        raise AssertionError(f"unexpected metadata URL: {url}")

    def bytes(self, url: str) -> bytes:
        assert url == f"https://codeload.github.com/owner/demo/zip/{LIVE_COMMIT}"
        return self.archive


@pytest.fixture(autouse=True)
def reset_plugin_state():
    with state._lock:
        state.plugins = []
        state.plugins_loading = False
        state.plugins_error = None
        state.plugin_discovery_results = []
        state.plugin_discovery_query = ""
        state.plugin_discovery_loading = False
        state.plugin_discovery_error = None
        state.plugin_inspection = None
        state.plugin_inspection_error = None
        state.plugin_inspection_loading = False
        state.plugin_operation = ""
        state.plugin_operation_error = None
        state.plugin_operation_message = ""
        state.plugin_view = "saved"
        state.plugin_selection = ""
        state.notifications = []
        state.active_notification = None
    yield


def _manager_double() -> MagicMock:
    manager = MagicMock()
    manager.list_catalog.return_value = []
    return manager


def test_gateway_discovery_passes_bounded_filters_and_maps_candidates(monkeypatch):
    manager = _manager_double()
    manager.discover.return_value = [
        {
            "id": "owner-demo",
            "name": "Demo",
            "full_name": "owner/demo",
            "source_url": "https://github.com/owner/demo",
            "description": "Candidate",
            "language": "Python",
            "topics": ["aetherion"],
            "stars": 42,
            "forks": 3,
            "license_id": "MIT",
            "license_declared": True,
            "archived": False,
            "default_branch": "main",
            "compatibility_status": "unknown",
            "compatibility_reasons": ["discovery_not_inspected"],
            "execution_enabled": False,
        }
    ]
    manager_factory = MagicMock(return_value=manager)
    monkeypatch.setattr(gateway_module, "PluginCatalogManager", manager_factory)
    monkeypatch.setattr(gateway_module, "_daemon", lambda target, *_args, **_kwargs: target())

    Gateway().discover_plugins(
        "agent",
        limit=7,
        language="Python",
        min_stars=25,
        include_archived=True,
    )

    manager.discover.assert_called_once_with(
        "agent",
        limit=7,
        language="Python",
        min_stars=25,
        include_archived=True,
    )
    assert state.plugin_discovery_error is None
    assert state.plugin_discovery_results[0].full_name == "owner/demo"
    assert state.plugin_discovery_results[0].stars == 42
    assert state.plugin_discovery_results[0].execution_enabled is False
    manager.catalog_add.assert_not_called()
    manager.install.assert_not_called()


def test_gateway_discovery_failure_is_visible_without_catalog_mutation(monkeypatch):
    manager = _manager_double()
    manager.discover.side_effect = RuntimeError("rate limit")
    monkeypatch.setattr(gateway_module, "PluginCatalogManager", lambda: manager)
    monkeypatch.setattr(gateway_module, "_daemon", lambda target, *_args, **_kwargs: target())

    Gateway().discover_plugins("agent")

    assert state.plugin_discovery_results == []
    assert state.plugin_discovery_error == "rate limit"
    manager.catalog_add.assert_not_called()
    manager.install.assert_not_called()


def test_gateway_discards_stale_discovery_workers(monkeypatch):
    manager = _manager_double()
    manager.discover.side_effect = lambda query, **_kwargs: [
        {"id": query, "name": query, "source_url": f"https://github.com/owner/{query}"}
    ]
    workers = []
    monkeypatch.setattr(gateway_module, "PluginCatalogManager", lambda: manager)
    monkeypatch.setattr(gateway_module, "_daemon", lambda target, *_args, **_kwargs: workers.append(target))

    gateway = Gateway()
    gateway.discover_plugins("old")
    gateway.discover_plugins("new")
    workers[0]()
    assert state.plugin_discovery_results == []
    workers[1]()
    assert [item.id for item in state.plugin_discovery_results] == ["new"]


def test_gateway_catalog_and_lifecycle_actions_use_existing_manager(monkeypatch):
    manager = _manager_double()
    manager.catalog_add.return_value = {"id": "demo", "resolved_commit": "a" * 40}
    manager.inspect.return_value = {
        "id": "demo",
        "resolved_commit": "a" * 40,
        "compatibility_status": "unknown",
        "execution_enabled": False,
    }
    manager.catalog_pin.return_value = {"id": "demo", "resolved_commit": "b" * 40}
    manager.status.return_value = {"id": "demo", "integrity": "verified", "resolved_commit": "a" * 40}
    manager.install.return_value = {"id": "demo", "integrity": "verified", "execution_enabled": False}
    manager.catalog_refresh.return_value = {"id": "demo", "resolved_commit": "a" * 40}
    manager.update.return_value = {"id": "demo", "resolved_commit": "b" * 40, "integrity": "verified"}
    manager.catalog_remove.return_value = True
    manager.remove.return_value = True
    manager.check_publisher_evidence.return_value = {
        "resolved_commit": "a" * 40,
        "publisher_auth_status": "unknown_identity",
        "execution_enabled": False,
    }
    manager_factory = MagicMock(return_value=manager)
    monkeypatch.setattr(gateway_module, "PluginCatalogManager", manager_factory)
    monkeypatch.setattr(gateway_module, "_daemon", lambda target, *_args, **_kwargs: target())

    gateway = Gateway()
    gateway.catalog_add_plugin("demo", "https://github.com/owner/demo", ref="main")
    gateway.inspect_plugin("demo", ref="main")
    gateway.pin_plugin("demo", "v2")
    gateway.install_plugin("demo", "Demo")
    gateway.verify_plugin("demo", "Demo")
    gateway.refresh_plugin_ref("demo", "Demo")
    gateway.update_plugin("demo", "Demo")
    gateway.uninstall_plugin("demo", "Demo")
    gateway.remove_catalog_plugin("demo", "Demo")
    gateway.check_publisher_evidence("demo", display_name="Demo")

    manager.catalog_add.assert_called_once_with("demo", "https://github.com/owner/demo", ref="main")
    manager.inspect.assert_called_once_with("demo", ref="main")
    manager.catalog_pin.assert_called_once_with("demo", "v2")
    manager.install.assert_called_once_with("demo")
    manager.status.assert_called_once_with("demo")
    manager.catalog_refresh.assert_called_once_with("demo")
    manager.update.assert_called_once_with("demo")
    manager.remove.assert_called_once_with("demo")
    manager.catalog_remove.assert_called_once_with("demo")
    manager.check_publisher_evidence.assert_called_once_with("demo", ref=None)
    assert any(call.kwargs == {"evidence_only": True} for call in manager_factory.call_args_list)
    manager.list_catalog.assert_any_call(evidence_only=True)


def test_tui_safe_label_neutralizes_markup_and_terminal_controls():
    rendered = _safe_label("[bold red]TRUSTED PUBLISHER[/bold red]\x1b[2J\rFORGED\nSTATUS")
    notification = gateway_module._safe_notification("[bold red]TRUSTED PUBLISHER[/bold red]\x1b[2J\rFORGED\nSTATUS")

    assert "\\[bold red]TRUSTED PUBLISHER\\[/bold red]" in rendered
    assert "\x1b" not in rendered
    assert "\\r" in rendered
    assert "\\n" in rendered
    assert "\\[bold red]TRUSTED PUBLISHER\\[/bold red]" in notification
    assert "\x1b" not in notification


def test_plugins_screen_filters_discovery_separately_from_saved_catalog():
    screen = PluginsScreen()
    screen._mode = "discovery"
    screen._filter = "owner"
    state.plugin_discovery_results = [
        PluginCandidate(id="owner-demo", name="Demo", full_name="owner/demo"),
        PluginCandidate(id="other-tool", name="Tool", full_name="other/tool"),
    ]
    state.plugins = [Plugin(id="saved", name="Saved", description="Saved catalog entry")]

    results = screen._filtered()

    assert [candidate.id for candidate in results] == ["owner-demo"]
    screen._mode = "saved"
    screen._filter = ""
    assert [plugin.id for plugin in screen._filtered()] == ["saved"]


class _PluginHarness(App):
    def compose(self) -> ComposeResult:
        yield PluginsScreen()


@pytest.mark.asyncio
async def test_plugins_screen_modes_and_custom_form_are_reachable(monkeypatch):
    import aetherion_cli.tui.gateway as gateway_module

    monkeypatch.setattr(gateway_module.gateway, "load_plugins", MagicMock())
    async with _PluginHarness().run_test(size=(140, 50)) as pilot:
        state.set_plugins([Plugin(id="saved", name="Saved", description="Saved entry")])
        await pilot.pause()
        screen = pilot.app.query_one(PluginsScreen)
        assert screen._mode == "saved"
        await pilot.click("#btn-mode-discovery")
        assert screen._mode == "discovery"
        state.set_plugin_discovery(
            [
                PluginCandidate(
                    id="owner-demo",
                    name="Demo",
                    full_name="owner/demo",
                    source_url="https://github.com/owner/demo",
                    default_branch="main",
                    compatibility_status="unknown",
                )
            ],
            query="demo",
        )
        await pilot.pause(0.2)
        assert pilot.app.query_one("#plugin-source-input").value == "https://github.com/owner/demo"
        assert pilot.app.query_one("#btn-save-catalog").disabled is False
        await pilot.click("#btn-mode-custom")
        assert screen._mode == "custom"
        assert pilot.app.query_one("#plugin-source-input").value == ""
        assert pilot.app.query_one("#btn-save-catalog").disabled is False


@pytest.mark.asyncio
async def test_saved_inspect_uses_the_saved_pin_not_the_moving_requested_ref(monkeypatch):
    import aetherion_cli.tui.gateway as gateway_module

    monkeypatch.setattr(gateway_module.gateway, "load_plugins", MagicMock())
    inspect = MagicMock()
    monkeypatch.setattr(gateway_module.gateway, "inspect_plugin", inspect)
    async with _PluginHarness().run_test(size=(140, 50)) as pilot:
        state.set_plugins(
            [
                Plugin(
                    id="saved",
                    name="Saved",
                    description="Saved entry",
                    source_url="https://github.com/owner/saved",
                    catalog_origin="user",
                    requested_ref="main",
                    resolved_commit="a" * 40,
                )
            ]
        )
        await pilot.pause()
        await pilot.click("#btn-inspect")
        inspect.assert_called_once_with("saved", ref=None, display_name="Saved")


@pytest.mark.asyncio
async def test_worker_operation_state_updates_are_dispatched_to_the_textual_ui(monkeypatch):
    import aetherion_cli.tui.gateway as gateway_module

    monkeypatch.setattr(gateway_module.gateway, "load_plugins", MagicMock())
    async with _PluginHarness().run_test(size=(140, 50)) as pilot:
        done = threading.Event()

        def worker_action() -> None:
            try:
                state.begin_plugin_operation("Test operation")
                state.finish_plugin_operation("done")
            finally:
                done.set()

        worker = threading.Thread(
            target=worker_action,
            daemon=True,
        )
        worker.start()
        await pilot.pause(0.5)
        assert done.is_set()
        operation_text = str(pilot.app.query_one("#plugin-operation").render())
        assert "done" in operation_text


@pytest.mark.asyncio
async def test_tui_manager_lifecycle_uses_temp_root_and_keeps_catalog_separate(monkeypatch, tmp_path):
    import aetherion_cli.tui.gateway as gateway_module
    from aetherion_cli.plugin_manager import PluginCatalogManager

    fake = _TuiLifecycleGitHub()
    manager = PluginCatalogManager(
        root=tmp_path / "plugins",
        json_fetcher=fake.json,
        bytes_fetcher=fake.bytes,
    )
    monkeypatch.setattr(gateway_module, "PluginCatalogManager", lambda: manager)
    monkeypatch.setattr(gateway_module, "_daemon", lambda target, *_args, **_kwargs: target())

    async with _PluginHarness().run_test(size=(140, 50)) as pilot:
        state.set_plugin_discovery(
            [
                PluginCandidate(
                    id="owner-demo",
                    name="Demo",
                    full_name="owner/demo",
                    source_url="https://github.com/owner/demo",
                    default_branch="main",
                )
            ],
            query="demo",
        )
        screen = pilot.app.query_one(PluginsScreen)
        await pilot.click("#btn-mode-discovery")
        await pilot.pause()
        pilot.app.query_one("#plugin-alias-input").value = "../bad"
        pilot.app.query_one("#btn-save-catalog").press()
        await pilot.pause()
        assert not any(item.get("catalog_origin") == "user" for item in manager.catalog_list())

        pilot.app.query_one("#plugin-alias-input").value = "demo-alias"
        pilot.app.query_one("#btn-save-catalog").press()
        await pilot.pause()

        saved = manager.info("demo-alias")
        assert saved is not None
        assert saved["resolved_commit"] == LIVE_COMMIT
        assert manager.status() == []
        assert screen._mode == "saved"

        pilot.app.query_one("#btn-install").press()
        await pilot.pause()
        assert manager.status("demo-alias")["integrity"] == "verified"
        assert manager.info("demo-alias")["installed"] is True

        pilot.app.query_one("#btn-verify").press()
        await pilot.pause()
        assert manager.status("demo-alias")["execution_enabled"] is False

        pilot.app.query_one("#btn-uninstall").press()
        pilot.app.query_one("#btn-uninstall").press()
        await pilot.pause()
        assert manager.status() == []
        assert manager.info("demo-alias") is not None

        pilot.app.query_one("#btn-remove-catalog").press()
        pilot.app.query_one("#btn-remove-catalog").press()
        await pilot.pause()
        assert manager.info("demo-alias") is None
        assert manager.status() == []
