# Aetherion TUI GitHub Discovery Implementation Report

**Date:** 2026-10-17  
**Execution policy:** Option A — metadata and managed-source lifecycle only; execution disabled  
**Application root:** `end to end aetherion cli`

**Verdict:** `PASS WITH FINDINGS`

## Summary

The existing Plugins screen now provides a complete TUI-first GitHub repository discovery and catalog-management experience while reusing the existing `PluginCatalogManager` and gateway architecture.

Users can:

- Browse saved curated/user catalog entries and managed-only sources.
- Search GitHub repositories with bounded query, language, minimum-stars, result-limit, and archived filters.
- Select discovery candidates and review provenance, metadata, license, compatibility evidence, and explicit unsaved/uninstalled state.
- Add a custom public GitHub URL with a local alias and optional ref.
- Inspect source metadata without saving or installing.
- Save a candidate to the persistent catalog without staging source.
- Refresh, explicitly pin, stage, verify, update, uninstall, and remove catalog entries using the same trusted manager methods as the CLI.
- See catalog presence, managed-source state, integrity, selected and installed commits, compatibility, and execution-disabled status separately.

No plugin code, entrypoint, package script, dependency, binary, worker, MCP server, or runtime was executed or added.

## Confirmed Findings and Remediation

### High — Saved-entry inspection followed the editable requested ref

**Evidence:** `PluginDetailPane.show_plugin()` populated the ref input with the saved requested branch/tag, and the Inspect action passed that value to `gateway.inspect_plugin()`. The manager correctly rejects a moving-ref override for a saved alias, so the visible Inspect action failed for otherwise valid saved entries such as `main`.

**Fix:** Saved-entry inspection now passes `ref=None`, which makes the existing manager resolve and inspect the saved immutable catalog pin. Candidate/custom inspection continues to use the explicitly entered ref.

**Regression:** `test_saved_inspect_uses_the_saved_pin_not_the_moving_requested_ref` asserts the gateway boundary call.

### Medium — Worker-thread state callbacks queried Textual widgets off-thread

**Evidence:** The original inspection/operation state handlers called `self.query_one(...)` before dispatching to the Textual UI thread. Gateway mutations publish state from daemon workers, so this crossed the Textual thread boundary.

**Fix:** The callbacks now dispatch closures that perform `query_one()` and widget updates only on the UI thread. Main-thread notifications still render directly.

**Regression:** `test_worker_operation_state_updates_are_dispatched_to_the_textual_ui` drives a worker-thread state update through a real Textual `run_test()` harness.

### Medium — Successful catalog save changed view state without rebuilding the detail pane

**Evidence:** A successful save reloaded authoritative plugins while the screen was still in discovery mode. The later mode switch changed `_mode` but did not rebuild the list/detail pane, leaving discovery controls/details visible while the state claimed saved mode.

**Fix:** Successful catalog-save operation completion now switches to saved mode and rebuilds the authoritative list/detail view.

**Regression:** `test_tui_manager_lifecycle_uses_temp_root_and_keeps_catalog_separate` drives discovery → save → stage → verify → uninstall → catalog removal through the real screen/gateway/manager path.

### Low — Prior coverage did not exercise the full TUI lifecycle boundary

**Evidence:** Earlier tests covered gateway mocks and screen modes, but did not run a synthetic archive through the Textual screen, gateway, and real manager together.

**Fix:** Added an isolated temporary-root Textual lifecycle test with a synthetic GitHub response/archive. It verifies pinned save, staging, integrity, verification, uninstall/catalog separation, and execution lock without executing fixture source.

## Existing Architecture Reused

The implementation extends the current Textual application rather than creating a second plugin UI or manager:

- `AetherionApp` continues to route `Screen.PLUGINS` to `PluginsScreen`.
- `PluginsScreen` remains the existing two-pane screen with keyboard and mouse selection.
- `Gateway` remains the only worker-thread bridge for network/filesystem actions.
- `TUIState` remains the thread-safe source of UI truth and notification state.
- `PluginCatalogManager` remains the only implementation of GitHub validation, discovery, catalog persistence, archive inspection, source promotion, integrity, pinning, update, uninstall, locking, and recovery.
- No backend `ExtensionManager`, runner, worker, execution route, or tool bridge was connected.

The existing gateway daemon-thread pattern was reused for discovery and catalog/lifecycle actions. No new event loop or concurrency framework was introduced.

## TUI Features Implemented

### Saved catalog view

The saved view displays curated entries, user aliases, and installed managed-only records. It distinguishes:

- Catalog presence.
- Catalog origin.
- Requested ref.
- Selected/pinned commit.
- Installed commit.
- Managed-source status.
- Integrity state.
- Compatibility classification and reasons.
- License metadata, stars, forks, language, and archive metadata when available.
- Execution state, forced to disabled.

Managed-only sources remain visible after catalog removal and can be addressed by their installed source ID.

### GitHub discovery view

Discovery uses the existing bounded `PluginCatalogManager.discover()` method. The TUI exposes:

- Search query.
- Language qualifier.
- Minimum stars.
- Result limit.
- Archived inclusion toggle.

Each candidate renders repository identity, canonical HTTPS URL in the detail form, description, language, stars, forks, declared/missing license, archived state, default branch, timestamps, topics, compatibility status, and the explicit `not saved / not pinned / not installed / execution disabled` state.

Discovery results are never automatically saved, inspected, installed, activated, or executed.

### Add custom URL

The `Add custom URL` action opens a form for:

- Canonical HTTPS GitHub repository URL.
- Local alias.
- Optional branch, tag, or full SHA ref.

Submission calls `PluginCatalogManager.catalog_add()` only. URL validation, alias validation, reserved curated-ID rejection, duplicate handling, metadata retrieval, archive inspection, ref resolution, source-ID recording, catalog persistence, and error handling remain manager-owned.

The UI does not optimistically mark the source installed. After success or failure, it reloads authoritative plugin state.

### Details and lifecycle actions

The detail pane provides:

- `Inspect` — bounded metadata inspection only.
- `Save alias` — catalog save only.
- `Pin ref` — explicit catalog pin change, without installation.
- `Stage source` — existing safe install path using the saved immutable pin.
- `Verify` — existing integrity status path.
- `Refresh` — candidate ref observation without changing the pin.
- `Update` — transaction-safe update from the selected saved pin, with confirmation.
- `Uninstall source` — managed-source removal only, with confirmation.
- `Remove catalog` — catalog alias removal only, with confirmation.

Destructive or source-replacement actions require a second deliberate action. Operation state, errors, and authoritative reloads are visible in the detail pane and global notification banner.

## State and Responsiveness Decisions

`TUIState` now distinguishes:

- Saved/installed `Plugin` records.
- Read-only `PluginCandidate` discovery results.
- Discovery query, filters, loading, empty, and error state.
- Inspection result, selector, loading, and error state.
- Current plugin view and selection.
- Serialized mutation operation, success message, and error state.

Discovery requests use a generation token. If a newer search starts before an older worker completes, the older response is discarded and cannot overwrite the current result set.

Mutating operations use a gateway operation guard so duplicate clicks do not start concurrent catalog/source operations. Every mutation reloads the manager's authoritative catalog/managed-source state in a `finally` path, including failure paths that may have involved transaction recovery.

The existing worker-thread gateway keeps network and filesystem calls off the UI thread. Textual callbacks dispatch safely to the UI thread and handle main-thread state notifications directly, preventing stale or missed refreshes in the screen.

Repository metadata is escaped before being rendered through Rich markup-bearing labels. Popularity, activity, topics, declared license, and compatibility are presented as evidence fields, not trust claims.

## Security Boundaries Preserved

- `execution_enabled=False` remains source-locked in manager records, discovery candidates, TUI state, and displayed lifecycle output.
- Discovery is read-only and bounded by the existing manager limits.
- Catalog save uses the existing strict GitHub URL and alias validation.
- Installation, pinning, update, uninstall, integrity, transaction journaling, locking, archive safety, redirect policy, and managed-root confinement remain manager-owned.
- Saved aliases never bypass immutable pin selection.
- The TUI does not import repository files, run entrypoints, run package scripts, install dependencies, invoke subprocesses, or activate plugins.
- Catalog removal and managed-source uninstall remain independent.
- A valid static compatibility result does not imply runtime support or source trustworthiness.

## Exact User Workflow

```text
# Open the TUI and Plugins screen
aetherion tui
# Press 5

# In Plugins → GitHub discovery:
# enter a query, optional language, minimum stars, limit, and archived toggle
# select a candidate, review details, then enter an alias/ref and choose Save alias

# In Plugins → Saved catalog:
# select the saved alias, choose Inspect, then Stage source
# choose Verify to re-check integrity
# choose Refresh to observe a candidate without changing the pin
# enter a new ref and choose Pin ref, then choose Update and confirm
# choose Uninstall source and confirm to remove only managed source
# choose Remove catalog and confirm to remove only the saved alias
```

## Modified Files

- `cli/aetherion_cli/tui/state.py`
  - Added `PluginCandidate`, discovery state, inspection state, operation state, lifecycle metadata, and execution-disabled defaults.
- `cli/aetherion_cli/tui/gateway.py`
  - Added bounded discovery mapping, stale-response suppression, catalog save/inspect/pin/verify/update/remove actions, operation serialization, and authoritative reload behavior.
- `cli/aetherion_cli/tui/screens/plugins.py`
  - Added saved/discovery/custom modes, filters, candidate cards, details, catalog form, lifecycle actions, confirmations, loading/error states, and safe metadata rendering.
- `cli/aetherion_cli/plugin_manager.py`
  - Added catalog presence metadata to the TUI-facing catalog records.
- `cli/tests/test_tui_plugins.py`
  - Added gateway discovery/action tests, error/no-mutation checks, selection filtering, and a Textual harness test for discovery/custom modes.
- `cli/README.md`
  - Documented the TUI GitHub discovery, catalog, inspection, staging, pinning, verification, update, uninstall, and removal workflow.

## Verification Results

### TUI and lifecycle regression suite

Command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -o addopts="" -q cli/tests/test_plugin_manager.py cli/tests/test_cli.py cli/tests/test_tui_state.py cli/tests/test_tui_plugins.py cli/tests/test_p0_tui_streaming.py
```

Result:

```text
143 passed in 16.35s
```

The new `test_tui_plugins.py` suite includes nine tests covering gateway filter forwarding, stale-response rejection, candidate mapping, failure/no-mutation behavior, manager action routing, saved/discovery filtering, saved-pin inspection, worker-thread UI dispatch, and a real Textual `run_test()` lifecycle harness.

### Full CLI suite

Command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -o addopts="" -q cli/tests
```

Result after the TUI changes:

```text
225 passed in 54.41s
```

### Targeted lint and smoke checks

The lifecycle manager and manager tests continue to pass the focused Ruff check:

```powershell
python -m ruff check cli/aetherion_cli/plugin_manager.py cli/tests/test_plugin_manager.py --select E4,E7,E9,F,I,B,W --output-format concise
```

Result:

```text
All checks passed!
```

The changed TUI files pass the syntax/error check:

```powershell
python -m ruff check cli/aetherion_cli/tui/state.py cli/aetherion_cli/tui/gateway.py cli/aetherion_cli/tui/screens/plugins.py cli/tests/test_tui_plugins.py --select E9 --output-format concise
```

Result:

```text
All checks passed!
```

CLI help and import smoke checks passed:

```powershell
$env:PYTHONPATH='cli'
$env:PYTHONDONTWRITEBYTECODE='1'
python -m aetherion_cli.main plugins discover --help
python -m aetherion_cli.main plugins install --help
python -c "import aetherion_cli.tui.app; import aetherion_cli.tui.gateway; import aetherion_cli.tui.screens.plugins; print('tui import smoke ok')"
```

The full broad Ruff selection over legacy TUI files still reports pre-existing unused imports/locals and import-order issues outside this focused change; it was not mass-formatted.

### Bounded live TUI gateway check

Live GitHub access was available. The TUI gateway was run through its real `discover_plugins()` production path with only the manager constructor redirected to an approved disposable temporary root. No catalog or managed source was written.

- Query: `Hello-World`
- Limit: `1`
- Result count: `1`
- Candidate returned: `leachim6/hello-world`
- Discovery error: none
- Candidate `execution_enabled`: `false`

No upstream repository code, package script, dependency, entrypoint, binary, worker, MCP server, or runtime was executed.

## Protected-State Verification

Initial baseline before this TUI implementation:

- Application branch: `main`
- Application HEAD: `5c7bdcd4808a178aa677488f18b948a747f94193`
- `backend/routes/d3_features.py`: `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c`
- `.git/index`: `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e`
- `.git/index.lock`: absent
- Local `rea/` branch: `main`
- Local `rea/` HEAD: `e13da084e7c5967b80632ae5ad8d3d37bcf57469`
- Local `rea/` tracked status: clean

Post-implementation verification:

- `backend/routes/d3_features.py` remains unchanged at hash `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c`.
- `.git/index` remains unchanged at hash `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e`.
- `.git/index.lock` remains absent.
- Local `rea/` remains on `main` at `e13da084e7c5967b80632ae5ad8d3d37bcf57469`.
- The pre-existing tracked modification remains only `backend/routes/d3_features.py`; it was not edited.
- No `.env`, credential, secret, deployment setting, unrelated user file, commit, push, reset, clean, index mutation, or unrequested deletion occurred.

## Limitations and Non-Claims

- The TUI is CLI-first only for discovery API parity; it does not add a backend/plugin runtime.
- Search results are not trust decisions. Stars, forks, activity, topics, and declared licenses are not security evidence.
- Static compatibility is not runtime compatibility or execution approval.
- Live TUI validation used a temporary manager-root redirect and did not stage a live source; deterministic lifecycle tests cover staging/update/uninstall semantics.
- The full repository-wide test suite was not run.
- Broad Ruff remains non-clean because of pre-existing issues in the large gateway/TUI surface; syntax/error checks and focused manager lint passed.
- No dependency resolution, license approval, sandbox execution, dynamic import, plugin activation, or runtime health check was performed.
