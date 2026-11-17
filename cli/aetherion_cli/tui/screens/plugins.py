"""
aetherion_cli/tui/screens/plugins.py
Plugin Manager — marketplace + installed, 2-pane with category filter.
"""
from __future__ import annotations

import threading
from typing import ClassVar, List, Optional

from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Static

from aetherion_cli.tui.state import NotifLevel, Plugin, PluginCandidate, state
from aetherion_cli.tui.theme import Colors, Glyphs


def _safe_label(value: object, fallback: str = "—") -> str:
    """Escape untrusted repository metadata before Rich markup rendering."""
    text = str(value) if value not in (None, "") else fallback
    safe_chars: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char == "\n":
            safe_chars.append("\\n")
        elif char == "\r":
            safe_chars.append("\\r")
        elif char == "\t":
            safe_chars.append("\\t")
        elif codepoint < 0x20 or codepoint == 0x7F or 0x80 <= codepoint <= 0x9F:
            safe_chars.append(f"\\x{codepoint:02x}")
        else:
            safe_chars.append(char)
    return escape("".join(safe_chars))


class PluginCard(Static):
    """Single plugin card in the list."""

    DEFAULT_CSS = """
    PluginCard {
        height: 3;
        padding: 0 1;
        border-bottom: solid #1e2433;
    }
    PluginCard.selected {
        background: #4c2288;
    }
    PluginCard:hover {
        background: #1a1e2e;
    }
    """

    def __init__(self, plugin: Plugin, selected: bool = False) -> None:
        super().__init__()
        self._plugin = plugin
        if selected:
            self.add_class("selected")

    def render(self) -> Text:  # type: ignore[override]
        p   = self._plugin
        t   = Text()
        sel = "selected" in self.classes
        prefix = f"{Glyphs.CHEVRON_RIGHT} " if sel else "  "
        t.append(prefix, style=f"bold {Colors.ACCENT}" if sel else Colors.TEXT_DIM)

        if p.installed:
            t.append(f"{Glyphs.DONE} ", style=Colors.SUCCESS)
        else:
            t.append(f"{Glyphs.PLUGINS} ", style=Colors.TEXT_DIM)

        t.append(p.name[:28], style=f"bold {Colors.TEXT_PRIMARY}")
        t.append(f"  v{p.version}", style=Colors.TEXT_DIM)
        t.append("\n")
        t.append(f"  {p.description[:52]}", style=Colors.TEXT_MUTED)
        return t

    def on_mouse_down(self, event) -> None:
        event.stop()

    def on_click(self, _event) -> None:
        parent = self.parent
        while parent is not None and not isinstance(parent, PluginsScreen):
            parent = parent.parent
        if parent is not None:
            parent.select_item(self._plugin.id)


class PluginCandidateCard(Static):
    """Single read-only GitHub discovery result."""

    DEFAULT_CSS = """
    PluginCandidateCard {
        height: 4;
        padding: 0 1;
        border-bottom: solid #1e2433;
    }
    PluginCandidateCard.selected {
        background: #25406a;
    }
    PluginCandidateCard:hover {
        background: #1a1e2e;
    }
    """

    def __init__(self, candidate: PluginCandidate, selected: bool = False) -> None:
        super().__init__()
        self._candidate = candidate
        if selected:
            self.add_class("selected")

    def render(self) -> Text:  # type: ignore[override]
        c = self._candidate
        selected = "selected" in self.classes
        t = Text()
        t.append(f"{Glyphs.CHEVRON_RIGHT} " if selected else "  ",
                 style=f"bold {Colors.ACCENT}" if selected else Colors.TEXT_DIM)
        t.append(f"{Glyphs.BULLET} ", style=Colors.ACCENT_BRIGHT)
        t.append((c.full_name or c.name)[:38], style=f"bold {Colors.TEXT_PRIMARY}")
        t.append(f"  ★{c.stars}  {c.language or '—'}", style=Colors.TEXT_DIM)
        t.append("\n")
        t.append(f"  {c.description[:52]}", style=Colors.TEXT_MUTED)
        t.append("\n")
        license_text = c.license_id or c.license_name or "license missing"
        t.append(f"  {license_text[:22]}  ·  {c.compatibility_status}  ·  not saved", style=Colors.TEXT_DIM)
        return t

    def on_mouse_down(self, event) -> None:
        event.stop()

    def on_click(self, _event) -> None:
        parent = self.parent
        while parent is not None and not isinstance(parent, PluginsScreen):
            parent = parent.parent
        if parent is not None:
            parent.select_item(self._candidate.id)


class PluginDetailPane(Widget):
    """Right pane: discovery details, catalog form, and safe lifecycle actions."""

    DEFAULT_CSS = """
    PluginDetailPane {
        width: 1fr;
        padding: 1 2;
        background: #0a0c12;
        height: 1fr;
        overflow-y: auto;
    }
    #plugin-title {
        color: #9d5cf0;
        text-style: bold;
        height: 2;
    }
    #plugin-meta {
        color: #64748b;
        height: auto;
        margin-bottom: 1;
    }
    #plugin-desc {
        color: #e2e8f0;
        height: auto;
        border: solid #1e2433;
        padding: 1;
        margin-bottom: 1;
        background: #12151f;
    }
    #plugin-inspection {
        color: #94a3b8;
        height: auto;
        border: solid #1e2433;
        padding: 1;
        margin-bottom: 1;
        background: #0f172a;
    }
    #plugin-operation {
        color: #94a3b8;
        height: auto;
        margin-bottom: 1;
    }
    #plugin-catalog-form {
        height: auto;
        border-top: solid #1e2433;
        padding-top: 1;
        margin-bottom: 1;
    }
    #plugin-catalog-form Input {
        margin-bottom: 1;
    }
    #plugin-catalog-actions, #plugin-lifecycle-actions, #plugin-actions {
        height: auto;
        margin-bottom: 1;
    }
    #plugin-confirm {
        color: #f59e0b;
        height: auto;
        margin-bottom: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._plugin: Optional[Plugin] = None
        self._candidate: Optional[PluginCandidate] = None
        self._custom_source = False
        self._pending_confirmation: Optional[str] = None
        self._inspection_target = ""

    def compose(self) -> ComposeResult:
        yield Label(f"{Glyphs.PLUGINS} Select a plugin →", id="plugin-title")
        yield Label("", id="plugin-meta")
        yield Label("", id="plugin-desc")
        yield Label("Select Inspect to fetch bounded metadata; nothing is saved or executed automatically.", id="plugin-inspection")
        yield Label("", id="plugin-operation")
        with Container(id="plugin-catalog-form"):
            yield Label(f" {Glyphs.SPARKLE} Repository catalog", classes="dash-section-title")
            yield Input(placeholder="Canonical HTTPS GitHub URL", id="plugin-source-input")
            yield Input(placeholder="Local alias (3–64 safe characters)", id="plugin-alias-input")
            yield Input(placeholder="Requested ref (branch, tag, or full SHA)", id="plugin-ref-input")
            with Horizontal(id="plugin-catalog-actions"):
                yield Button(f"{Glyphs.DONE} Save alias", id="btn-save-catalog", variant="default")
                yield Button(f"{Glyphs.BULLET} Inspect", id="btn-inspect", variant="default")
                yield Button(f"{Glyphs.STAR} Signature", id="btn-evidence", variant="default")
                yield Button(f"{Glyphs.STAR} Pin ref", id="btn-pin", variant="default")
        with Horizontal(id="plugin-lifecycle-actions"):
            yield Button(f"{Glyphs.PLUGINS} Stage source", id="btn-install", variant="default")
            yield Button(f"{Glyphs.DONE} Verify", id="btn-verify", variant="default")
            yield Button(f"{Glyphs.RUNNING} Refresh", id="btn-refresh", variant="default")
            yield Button(f"{Glyphs.RUNNING} Update", id="btn-update", variant="default")
        with Horizontal(id="plugin-actions"):
            yield Button(f"{Glyphs.ERROR} Uninstall source", id="btn-uninstall", variant="default")
            yield Button(f"{Glyphs.ERROR} Remove catalog", id="btn-remove-catalog", variant="default")
        yield Label("", id="plugin-confirm")

    def _set_form(self, source: str = "", alias: str = "", ref: str = "") -> None:
        self.query_one("#plugin-source-input", Input).value = source
        self.query_one("#plugin-alias-input", Input).value = alias
        self.query_one("#plugin-ref-input", Input).value = ref

    def _clear_confirmation(self) -> None:
        self._pending_confirmation = None
        self.query_one("#plugin-confirm", Label).update("")

    def _set_confirmation(self, action: str, message: str) -> bool:
        if self._pending_confirmation != action:
            self._pending_confirmation = action
            self.query_one("#plugin-confirm", Label).update(
                f"{Glyphs.WAITING} {message} Press the same action again to confirm."
            )
            return False
        self._clear_confirmation()
        return True

    def _set_common_button_state(self, *, saved: bool, candidate: bool, custom: bool) -> None:
        source_input = self.query_one("#plugin-source-input", Input)
        alias_input = self.query_one("#plugin-alias-input", Input)
        source_input.disabled = saved
        alias_input.disabled = saved or not (candidate or custom)
        self.query_one("#btn-save-catalog", Button).disabled = saved or not (candidate or custom)
        self.query_one("#btn-inspect", Button).disabled = not (
            bool(source_input.value.strip()) or bool(self._plugin and self._plugin.source_url)
        )
        self.query_one("#btn-pin", Button).disabled = not (
            saved and self._plugin is not None and self._plugin.catalog_origin == "user"
        )
        self.query_one("#btn-evidence", Button).disabled = not (
            saved and self._plugin is not None and bool(self._plugin.source_url) and bool(self._plugin.resolved_commit)
        )

    def _show_inspection(self) -> None:
        target = self.query_one("#plugin-inspection", Label)
        if state.plugin_inspection_selector and state.plugin_inspection_selector != self._inspection_target:
            target.update("Select Inspect to fetch bounded metadata; nothing is saved or executed automatically.")
            return
        if state.plugin_inspection_loading:
            target.update(f"{Glyphs.RUNNING} Inspecting bounded source metadata…")
            return
        if state.plugin_inspection_error:
            target.update(f"{Glyphs.ERROR} Inspection failed: {_safe_label(state.plugin_inspection_error)}")
            return
        result = state.plugin_inspection
        if not result:
            target.update("Select Inspect to fetch bounded metadata; nothing is saved or executed automatically.")
            return
        reasons = ", ".join(str(reason) for reason in result.get("compatibility_reasons", [])) or "no evidence"
        target.update(
            f"Requested ref: {_safe_label(result.get('requested_ref'))}  ·  "
            f"Resolved commit: {_safe_label(str(result.get('resolved_commit', '—'))[:16])}\n"
            f"Manifest: {_safe_label(result.get('manifest_file') or 'source-only')}  ·  "
            f"Compatibility: {_safe_label(result.get('compatibility_status', 'unknown'))} ({_safe_label(reasons)})\n"
            f"Archive integrity: {_safe_label(result.get('integrity', 'not-installed'))}  ·  "
            f"Catalog auth: {_safe_label(result.get('catalog_auth_status', 'unsigned'))}  ·  "
            f"Catalog sequence: {_safe_label(result.get('catalog_sequence', '—'))}  ·  "
            f"Expiry: {_safe_label(result.get('catalog_expires_at', '—'))}\n"
            f"Installed: {_safe_label(result.get('installed', False))}  ·  Execution: disabled"
        )

    def _show_operation(self) -> None:
        target = self.query_one("#plugin-operation", Label)
        if state.plugin_operation_error:
            target.update(f"{Glyphs.ERROR} {_safe_label(state.plugin_operation_error)}")
        elif state.plugin_operation:
            target.update(f"{Glyphs.RUNNING} {state.plugin_operation}…")
        else:
            target.update(_safe_label(state.plugin_operation_message, ""))

    def show_plugin(self, p: Plugin) -> None:
        self._plugin = p
        self._candidate = None
        self._custom_source = False
        self._clear_confirmation()
        self._inspection_target = p.id
        self.query_one("#plugin-title", Label).update(f"{Glyphs.PLUGINS} {_safe_label(p.name)}  v{_safe_label(p.version)}")
        status = f"{Glyphs.DONE} Stored (metadata-only)" if p.installed else f"{Glyphs.IDLE} Not stored"
        status_color = Colors.SUCCESS if p.installed else Colors.TEXT_MUTED
        lifecycle_line = ""
        if p.catalog_keyset_id or p.catalog_trust_lifecycle_reason or p.catalog_trust_last_transition:
            lifecycle_line = (
                f"Trust keyset: {_safe_label(p.catalog_keyset_id or '—')}/{_safe_label(p.catalog_keyset_sequence)}   "
                f"Lifecycle: {_safe_label(p.catalog_trust_lifecycle_reason or p.catalog_trust_last_transition or '—')}   "
                f"Root: {_safe_label((p.catalog_trust_root_fingerprint or '—')[:12])}\n"
            )
        publisher_line = ""
        if p.publisher_evidence_type or p.publisher_evidence_observation_status != "not_checked":
            publisher_line = (
                f"Publisher evidence: {_safe_label(p.publisher_evidence_type or '—')}   "
                f"Observation: {_safe_label(p.publisher_evidence_observation_status)}   "
                f"GitHub provider result: {_safe_label(p.publisher_evidence_provider_status or '—')}   "
                f"Aetherion publisher identity: {_safe_label(p.publisher_auth_status)}\n"
                f"Reason: {_safe_label(p.publisher_evidence_reason or p.publisher_evidence_provider_reason or '—')}   "
                f"Identity policy: {_safe_label(p.publisher_evidence_identity_policy_status)}   "
                f"Local cryptographic verification: {_safe_label(p.publisher_evidence_local_signature_status)}   "
                f"Freshness: {_safe_label(p.publisher_evidence_freshness)}\n"
            )
        self.query_one("#plugin-meta", Label).update(
            f"Author: {_safe_label(p.author)}   Category: {_safe_label(p.category)}\n"
            f"[{status_color}]{status}[/{status_color}]\n"
            f"Source: {_safe_label(p.catalog_origin or 'custom')}   Status: {_safe_label(p.status)}   Archive integrity: {_safe_label(p.integrity)}\n"
            f"Catalog auth: {_safe_label(p.catalog_auth_status)}   Publisher auth: {_safe_label(p.publisher_auth_status)}   Policy: {_safe_label(p.policy_status)}\n"
             f"Catalog sequence: {_safe_label(p.catalog_sequence)}   Key: {_safe_label(p.catalog_key_id or '—')}   "
             f"Expiry: {_safe_label(p.catalog_expires_at or '—')}   Cache: {_safe_label(p.catalog_freshness)}\n"
             f"{publisher_line}"
             f"{lifecycle_line}"
            f"Compatibility: {_safe_label(p.compatibility_status)}   Execution: disabled\n"
            f"Ref: {_safe_label(p.requested_ref)}   Commit: {_safe_label((p.resolved_commit or '—')[:12])}\n"
            f"Installed commit: {_safe_label((p.installed_commit or '—')[:12])}\n"
            f"License: {_safe_label(p.license_id or p.license_name or 'missing')}   Stars: {p.stars}   Forks: {p.forks}"
        )
        self.query_one("#plugin-desc", Label).update(_safe_label(p.description, "No description."))
        self.query_one("#plugin-inspection", Label).update("Select Inspect to fetch bounded metadata; nothing is saved or executed automatically.")
        self._set_form(p.source_url, p.id, p.requested_ref)
        self._set_common_button_state(saved=True, candidate=False, custom=False)
        self.query_one("#btn-install", Button).disabled = p.installed
        self.query_one("#btn-verify", Button).disabled = not p.installed
        self.query_one("#btn-refresh", Button).disabled = p.catalog_origin != "user"
        self.query_one("#btn-update", Button).disabled = not p.installed
        self.query_one("#btn-uninstall", Button).disabled = not p.installed
        self.query_one("#btn-remove-catalog", Button).disabled = p.catalog_origin != "user"

    def show_candidate(self, candidate: PluginCandidate) -> None:
        self._plugin = None
        self._candidate = candidate
        self._custom_source = False
        self._clear_confirmation()
        self._inspection_target = candidate.source_url
        self.query_one("#plugin-title", Label).update(f"{Glyphs.BULLET} {_safe_label(candidate.full_name or candidate.name)}")
        license_text = candidate.license_id or candidate.license_name or "missing"
        topics = ", ".join(candidate.topics[:6]) or "none"
        self.query_one("#plugin-meta", Label).update(
            f"Discovery result · {_safe_label(candidate.language, 'language unknown')} · ★ {candidate.stars} · forks {candidate.forks}\n"
            f"License: {_safe_label(license_text)} · Archived: {candidate.archived} · Compatibility: {_safe_label(candidate.compatibility_status)}\n"
            f"Default branch: {_safe_label(candidate.default_branch)} · Updated: {_safe_label(candidate.updated_at or candidate.pushed_at)}\n"
            f"Topics: {_safe_label(topics)}\n"
            "Not saved · not pinned · not installed · execution disabled"
        )
        self.query_one("#plugin-desc", Label).update(_safe_label(candidate.description, "No description."))
        self.query_one("#plugin-inspection", Label).update("Select Inspect to fetch bounded metadata; nothing is saved or executed automatically.")
        self._set_form(candidate.source_url, "", candidate.default_branch)
        self._set_common_button_state(saved=False, candidate=True, custom=False)
        for button_id in ("#btn-install", "#btn-verify", "#btn-refresh", "#btn-update", "#btn-uninstall", "#btn-remove-catalog", "#btn-pin"):
            self.query_one(button_id, Button).disabled = True

    def show_custom(self) -> None:
        self._plugin = None
        self._candidate = None
        self._custom_source = True
        self._clear_confirmation()
        self._inspection_target = ""
        self.query_one("#plugin-title", Label).update(f"{Glyphs.SPARKLE} Add GitHub repository")
        self.query_one("#plugin-meta", Label).update(
            "Enter a public HTTPS GitHub repository root and a safe local alias. "
            "Saving inspects and pins metadata only; it does not install the source."
        )
        self.query_one("#plugin-desc", Label).update(
            "The trusted manager will validate the URL, alias, ref, archive, manifest, and compatibility."
        )
        self.query_one("#plugin-inspection", Label).update("Inspect first if you want metadata evidence before saving.")
        self._set_form()
        self._set_common_button_state(saved=False, candidate=False, custom=True)
        for button_id in ("#btn-install", "#btn-verify", "#btn-refresh", "#btn-update", "#btn-uninstall", "#btn-remove-catalog", "#btn-pin"):
            self.query_one(button_id, Button).disabled = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from aetherion_cli.tui.gateway import gateway
        button_id = event.button.id
        source = self.query_one("#plugin-source-input", Input).value.strip()
        alias = self.query_one("#plugin-alias-input", Input).value.strip()
        ref = self.query_one("#plugin-ref-input", Input).value.strip()
        display_name = self._plugin.name if self._plugin else (self._candidate.name if self._candidate else alias or "repository")

        if button_id == "btn-save-catalog" and (self._candidate or self._custom_source):
            if not source or not alias:
                state.push_notification("A GitHub URL and local alias are required before saving.", NotifLevel.ERROR)
            else:
                gateway.catalog_add_plugin(alias, source, ref or None)
        elif button_id == "btn-inspect" and (source or self._plugin):
            source = source or (self._plugin.source_url if self._plugin else "")
            selector = self._plugin.id if self._plugin else source
            inspect_ref = None if self._plugin else (ref or None)
            gateway.inspect_plugin(selector, ref=inspect_ref, display_name=display_name)
        elif button_id == "btn-evidence" and self._plugin and self._plugin.resolved_commit:
            gateway.check_publisher_evidence(self._plugin.id, display_name=display_name)
        elif button_id == "btn-pin" and self._plugin and self._plugin.catalog_origin == "user" and ref:
            gateway.pin_plugin(self._plugin.id, ref, display_name=display_name)
        elif button_id == "btn-install" and self._plugin and not self._plugin.installed:
            gateway.install_plugin(self._plugin.id, self._plugin.name)
        elif button_id == "btn-verify" and self._plugin and self._plugin.installed:
            gateway.verify_plugin(self._plugin.id, self._plugin.name)
        elif button_id == "btn-refresh" and self._plugin and self._plugin.catalog_origin == "user":
            gateway.refresh_plugin_ref(self._plugin.id, self._plugin.name)
        elif button_id == "btn-update" and self._plugin and self._plugin.installed:
            if self._set_confirmation("update", "This will replace the managed source from the selected pin."):
                gateway.update_plugin(self._plugin.id, self._plugin.name)
        elif button_id == "btn-uninstall" and self._plugin and self._plugin.installed:
            if self._set_confirmation("uninstall", "This removes managed source data but keeps catalog metadata."):
                gateway.uninstall_plugin(self._plugin.id, self._plugin.name)
        elif button_id == "btn-remove-catalog" and self._plugin and self._plugin.catalog_origin == "user":
            if self._set_confirmation("remove-catalog", "This removes only the catalog alias; managed source remains."):
                gateway.remove_catalog_plugin(self._plugin.id, self._plugin.name)



class PluginsScreen(Widget):
    """Plugin Manager: filter bar + scrollable card list + detail pane."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r",      "refresh", "Refresh", show=True),
        Binding("escape", "back",    "Back",    show=True),
        Binding("i",      "show_installed", "Installed only", show=True),
        Binding("a",      "show_all",       "All",            show=True),
    ]

    DEFAULT_CSS = """
    PluginsScreen {
        height: 1fr;
        width: 1fr;
    }
    #plugins-toolbar {
        height: 3;
        background: #12151f;
        border-bottom: solid #1e2433;
        padding: 0 1;
        color: #64748b;
    }
    #plugins-mode-bar {
        height: 3;
        background: #0a0c12;
        padding: 0 1;
    }
    #plugins-discovery-controls {
        height: auto;
        padding: 0 1;
        border-bottom: solid #1e2433;
    }
    #plugins-discovery-controls Input {
        margin-bottom: 1;
    }
    #plugins-discovery-actions {
        height: auto;
        margin-bottom: 1;
    }
    #plugins-left {
        width: 44;
        border-right: solid #1e2433;
        background: #12151f;
        height: 1fr;
    }
    #plugins-filter-bar {
        height: 3;
        border-bottom: solid #1e2433;
        padding: 0 1;
    }
    #plugins-filter-bar Input {
        border: none;
        background: #12151f;
    }
    #plugins-list-scroll {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._filter       = ""
        self._selected_idx = 0
        self._installed_only = False
        self._mode = state.plugin_view if state.plugin_view in {"saved", "discovery"} else "saved"
        self._include_archived = False
        self._discovery_query = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="plugins-toolbar"):
            yield Label(
                f" {Glyphs.PLUGINS} Plugins  [{Colors.TEXT_DIM}]↑↓ navigate · r refresh · Esc back[/]",
                id="plugins-toolbar-label",
            )
        with Horizontal(id="plugins-mode-bar"):
            yield Button("Saved catalog", id="btn-mode-saved", variant="default")
            yield Button("GitHub discovery", id="btn-mode-discovery", variant="default")
            yield Button("Add custom URL", id="btn-mode-custom", variant="default")
        with Horizontal():
            with Vertical(id="plugins-left"):
                with Container(id="plugins-filter-bar"):
                    yield Input(placeholder=f"{Glyphs.BULLET} Filter plugins…", id="plugins-filter")
                with Container(id="plugins-discovery-controls"):
                    yield Label("GitHub search · read-only until you save an alias")
                    yield Input(placeholder="Search repositories…", id="plugin-discovery-query")
                    yield Input(placeholder="Language (optional)", id="plugin-discovery-language")
                    yield Input(placeholder="Minimum stars (0+)", id="plugin-discovery-stars")
                    yield Input(placeholder="Result limit (1–25)", id="plugin-discovery-limit", value="20")
                    with Horizontal(id="plugins-discovery-actions"):
                        yield Button(f"{Glyphs.BULLET} Search GitHub", id="btn-discover", variant="default")
                        yield Button("Archived: off", id="btn-archived", variant="default")
                yield ScrollableContainer(id="plugins-list-scroll")
            yield PluginDetailPane(id="plugins-detail")

    def on_mount(self) -> None:
        self._unsubs = [
            state.subscribe("plugins", self._on_plugins_changed),
            state.subscribe("plugin_discovery", self._on_plugins_changed),
            state.subscribe("plugin_inspection", self._on_inspection_changed),
            state.subscribe("plugin_operation", self._on_operation_changed),
        ]
        from aetherion_cli.tui.gateway import gateway
        gateway.load_plugins()
        self._sync_discovery_controls()

    def on_unmount(self) -> None:
        for unsub in getattr(self, "_unsubs", []):
            unsub()

    def _sync_discovery_controls(self) -> None:
        controls = self.query_one("#plugins-discovery-controls", Container)
        controls.display = self._mode == "discovery"

    def _filtered(self) -> List[object]:
        q = self._filter.lower()
        if self._mode == "discovery":
            ps: List[object] = list(state.plugin_discovery_results)
        else:
            ps = list(state.plugins)
            if self._installed_only:
                ps = [p for p in ps if isinstance(p, Plugin) and p.installed]
        if q:
            ps = [
                p for p in ps
                if q in str(getattr(p, "name", "")).lower()
                or q in str(getattr(p, "description", "")).lower()
                or q in str(getattr(p, "source_url", "")).lower()
                or q in str(getattr(p, "full_name", "")).lower()
            ]
        return ps

    def _on_plugins_changed(self) -> None:
        if threading.current_thread() is threading.main_thread():
            self._rebuild()
        else:
            self.app.call_from_thread(self._rebuild)

    def _on_inspection_changed(self) -> None:
        def callback() -> None:
            self.query_one(PluginDetailPane)._show_inspection()
        if threading.current_thread() is threading.main_thread():
            callback()
        else:
            self.app.call_from_thread(callback)

    def _on_operation_changed(self) -> None:
        def callback() -> None:
            if (
                not state.plugin_operation
                and not state.plugin_operation_error
                and state.plugin_operation_message.startswith("Save catalog")
            ):
                self._mode = "saved"
                self._selected_idx = 0
                self._sync_discovery_controls()
                self._rebuild()
            self.query_one(PluginDetailPane)._show_operation()
        if threading.current_thread() is threading.main_thread():
            callback()
        else:
            self.app.call_from_thread(callback)

    def _rebuild(self) -> None:
        scroll = self.query_one("#plugins-list-scroll")
        scroll.remove_children()
        items = self._filtered()

        loading = state.plugin_discovery_loading if self._mode == "discovery" else state.plugins_loading
        error = state.plugin_discovery_error if self._mode == "discovery" else state.plugins_error
        if loading:
            scroll.mount(Static(f"  {Glyphs.RUNNING} Loading {'GitHub repositories' if self._mode == 'discovery' else 'saved catalog'}…", classes="loading-indicator"))
            return

        if error:
            scroll.mount(Static(f"  {Glyphs.ERROR} {_safe_label(error)}\n\n  Press Search or r to retry.", classes="error-state"))
            return

        if not items:
            state.set_plugin_view(self._mode, "")
            scroll.mount(Static(
                f"\n  {Glyphs.BULLET if self._mode == 'discovery' else Glyphs.PLUGINS}  "
                f"No {'GitHub repositories matched' if self._mode == 'discovery' else 'saved plugins found'}.\n\n"
                f"  {'Try a different query or include archived repositories.' if self._mode == 'discovery' else 'Use GitHub discovery or Add custom URL.'}",
                classes="empty-state"
            ))
            return

        saved_selection = state.plugin_selection
        if saved_selection:
            for index, item in enumerate(items):
                if str(getattr(item, "id", "")) == saved_selection:
                    self._selected_idx = index
                    break
        self._selected_idx = max(0, min(self._selected_idx, len(items) - 1))
        for i, item in enumerate(items[:80]):
            card = (
                PluginCandidateCard(item, selected=i == self._selected_idx)
                if self._mode == "discovery"
                else PluginCard(item, selected=i == self._selected_idx)
            )
            scroll.mount(card)

        if 0 <= self._selected_idx < len(items):
            detail = self.query_one(PluginDetailPane)
            if self._mode == "discovery":
                detail.show_candidate(items[self._selected_idx])
            else:
                detail.show_plugin(items[self._selected_idx])

        state.set_plugin_view(self._mode, str(getattr(items[self._selected_idx], "id", "")))

    def _search_from_form(self) -> None:
        from aetherion_cli.tui.gateway import gateway
        query = self.query_one("#plugin-discovery-query", Input).value.strip()
        language = self.query_one("#plugin-discovery-language", Input).value.strip() or None
        try:
            limit = int(self.query_one("#plugin-discovery-limit", Input).value.strip() or "20")
            min_stars = int(self.query_one("#plugin-discovery-stars", Input).value.strip() or "0")
        except ValueError:
            state.push_notification("Discovery limit and minimum stars must be whole numbers.", NotifLevel.ERROR)
            return
        if not query:
            state.push_notification("Enter a GitHub search query first.", NotifLevel.ERROR)
            return
        self._discovery_query = query
        state.set_plugin_search_form(
            query,
            {"limit": str(limit), "language": language or "", "min_stars": str(min_stars), "include_archived": self._include_archived},
        )
        gateway.discover_plugins(
            query,
            limit=limit,
            language=language,
            min_stars=min_stars,
            include_archived=self._include_archived,
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "plugins-filter":
            self._filter = event.value
            self._selected_idx = 0
            self._rebuild()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "plugin-discovery-query":
            self._search_from_form()

    def select_item(self, item_id: str) -> None:
        items = self._filtered()
        for index, item in enumerate(items):
            if str(getattr(item, "id", "")) == item_id:
                self._selected_idx = index
                self._rebuild()
                return

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-mode-saved":
            self._mode = "saved"
        elif button_id == "btn-mode-discovery":
            self._mode = "discovery"
        elif button_id == "btn-mode-custom":
            self._mode = "custom"
            self._sync_discovery_controls()
            self.query_one(PluginDetailPane).show_custom()
            return
        elif button_id == "btn-discover":
            self._search_from_form()
            return
        elif button_id == "btn-archived":
            self._include_archived = not self._include_archived
            event.button.label = f"Archived: {'on' if self._include_archived else 'off'}"
            return
        else:
            return
        self._selected_idx = 0
        self._installed_only = False
        state.set_plugin_view(self._mode, "")
        self._sync_discovery_controls()
        self._rebuild()

    def on_key(self, event: events.Key) -> None:
        plugins = self._filtered()
        n = len(plugins)
        if event.key == "up" and n:
            self._selected_idx = max(0, self._selected_idx - 1)
            self._rebuild()
            event.prevent_default()
        elif event.key == "down" and n:
            self._selected_idx = min(n - 1, self._selected_idx + 1)
            self._rebuild()
            event.prevent_default()
        elif event.key == "enter" and n:
            pass  # detail already updated in _rebuild

    def action_show_installed(self) -> None:
        self._installed_only = True
        self._selected_idx   = 0
        self._rebuild()

    def action_show_all(self) -> None:
        self._installed_only = False
        self._selected_idx   = 0
        self._rebuild()

    def action_refresh(self) -> None:
        from aetherion_cli.tui.gateway import gateway
        if self._mode == "discovery":
            self._search_from_form()
        else:
            gateway.load_plugins()

    def action_back(self) -> None:
        state.navigate_back()
