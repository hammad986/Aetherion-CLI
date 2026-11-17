# Aetherion Generic GitHub Plugin Lifecycle Completion Report

**Date:** 2026-10-17  
**Execution policy:** Option A — metadata and managed-source lifecycle only; execution disabled  
**Application root:** `end to end aetherion cli`

## Executive Summary

The existing generic GitHub catalog and discovery implementation was traced through its production paths and completed for the catalog-to-managed-source lifecycle. A saved user alias can now be explicitly staged through the existing `plugins install` command using the catalog's immutable commit, checked for integrity, updated only after an explicit catalog pin change, and removed independently from the catalog record.

The work also hardened the static manifest contract. A supported `aetherion.plugin.json` must declare schema version `1`, a string plugin API version, a valid ID, a name, and a semantic version. Generic `plugin.json` metadata that merely mentions an API version no longer receives a false `compatible` result. Malformed or unsupported explicit contracts fail closed.

No plugin runtime, dynamic import, executable entrypoint, package manager, dependency installation, subprocess, worker, MCP server, or backend execution path was added or invoked.

## Existing Paths Confirmed Before Editing

The following capabilities already existed and were preserved:

- `plugins discover` performed bounded, read-only GitHub repository search.
- `plugins catalog add/list/info/refresh/pin/remove` persisted user aliases, selected refs, and resolved commits.
- `plugins inspect` downloaded a bounded archive into staging, validated archive paths, parsed metadata, and removed the temporary inspection directory.
- `plugins install` already selected curated IDs and GitHub URLs through one transaction-safe manager path.
- Existing installation already provided bounded downloads, trusted codeload redirect handling, safe ZIP extraction, managed-root confinement, file hashing, atomic promotion, journal recovery, rollback, and execution-disabled metadata.
- `plugins status` rehashed managed source content against the integrity manifest.
- `plugins update` atomically replaced a source while requiring the same plugin ID.
- `plugins uninstall` removed managed source data separately from catalog records at the manager level.
- The TUI already loaded catalog entries through `PluginCatalogManager` and had metadata-only install/uninstall controls.

The confirmed gaps were:

1. Saved catalog aliases were accepted by the shared install path, but a caller could override a saved alias with a moving branch/tag using `--ref`.
2. Catalog aliases did not resolve to the installed manifest ID for status, update, or uninstall.
3. Catalog records did not preserve the inspected source plugin ID, so a changed source manifest could bypass catalog/source identity checks.
4. User-catalog updates could follow the saved branch/tag instead of requiring an explicit catalog pin change first.
5. CLI/TUI lifecycle output did not clearly separate catalog origin, managed-source state, integrity, compatibility, and execution state.
6. Compatibility classification treated a generic manifest containing `plugin_api_version` as an explicit Aetherion contract.
7. Explicit manifest fields were not validated conservatively enough for type, schema, API, and required-field errors.

## Completed Lifecycle

### Discovery and catalog save

`plugins discover` remains read-only. It returns bounded repository metadata and does not save or inspect archives.

`plugins catalog add <url> --id <alias> [--ref <ref>]` validates the HTTPS GitHub repository root, fetches bounded GitHub metadata, resolves the requested ref, inspects the exact archive, records the inspected source plugin ID, and persists the requested ref plus resolved full commit SHA. It does not promote source into the managed store.

The catalog reader now requires:

- Supported catalog schema version.
- Valid alias and canonical GitHub source URL.
- Valid requested ref.
- Valid resolved full commit SHA.
- Valid inspected `source_plugin_id`.
- Unique repository URLs.
- Execution state forced to `False` on every read.

Malformed catalog records fail closed.

### Catalog-to-managed-source staging

The existing single trusted `PluginCatalogManager.install()` path now safely handles a saved alias:

1. Resolve the alias to its canonical source URL.
2. Use the catalog's saved full commit SHA when no override is supplied.
3. Reject a branch/tag override for a saved alias unless the caller supplies the exact already-selected full SHA.
4. Download the exact codeload archive for the immutable commit.
5. Inspect the archive and manifest in a bounded staging directory.
6. Require the fetched manifest/source ID to equal the catalog's saved `source_plugin_id`.
7. Write metadata and integrity manifests.
8. Promote the staged source atomically under the managed plugin root.
9. Publish the registry record through the existing journaled transaction.
10. Recompute and return verified integrity status.

The selected catalog pin is not changed by installation. A saved alias is not automatically activated or executed.

The existing top-level command is the supported install interface, so no duplicate `plugins catalog install` command was added:

```text
aetherion plugins install <catalog-alias>
```

The command output now separates:

- Catalog origin.
- Resolved commit.
- Integrity state.
- Compatibility classification.
- Execution state, always `disabled`.

Repeated installation of the same saved snapshot remains safe and returns the existing idempotent result when the source is already verified.

### Alias-based status, update, and removal

Installed-source selection now accepts either the installed manifest ID or a saved catalog alias. Alias resolution matches the canonical catalog source URL to the managed registry and rejects ambiguous multiple matches.

Therefore these commands can use either form:

```text
aetherion plugins status <catalog-alias>
aetherion plugins update <catalog-alias>
aetherion plugins uninstall <catalog-alias> --yes
```

Catalog removal and managed-source removal remain independent:

- `plugins catalog remove <alias>` removes only the saved catalog record.
- `plugins uninstall <alias-or-installed-id>` removes only the managed source and registry record.

If the catalog record is removed first, the installed source remains addressable by its installed manifest ID.

### Pinned update and recovery

For an installed source that still belongs to a saved catalog entry, `plugins update` without a ref uses the current catalog-selected immutable commit. Passing `--ref` or `--source` to that catalog-backed update is rejected. The user must first perform:

```text
aetherion plugins catalog pin <alias> --ref <branch-or-tag-or-sha>
aetherion plugins update <alias>
```

`catalog refresh` continues to observe branch/tag movement without changing `resolved_commit`. `catalog pin` explicitly resolves and selects a new commit, updates the expected source ID and compatibility evidence, and only then can `plugins update` stage it.

The existing transaction mechanism remains responsible for update safety:

- Candidate source is built in managed staging.
- Existing target is moved to a journaled backup before promotion.
- Registry publication is journaled.
- Failed publication restores the previous source and registry.
- Startup recovery reads and validates the journal before normal operations.
- Successful staging is followed by integrity verification.

No update path invokes source code or installs dependencies.

## Static Compatibility Contract

Compatibility is kept separate from source provenance, archive integrity, and runtime execution support.

### Explicit supported contract

An explicit `aetherion.plugin.json`, or a manifest explicitly marked with `aetherion_plugin: true`, must contain:

- `manifest_schema_version`: string matching supported schema `1`, `1.0`, or `1.0.0` forms.
- `plugin_api_version`: string containing one to three numeric version components.
- `id`: string matching the managed ID policy.
- `name`: string with at least two characters.
- `version`: string matching the supported semantic version format.

Conflicting `schema_version`/`manifest_schema_version` or `plugin_api_version`/`aetherion_plugin_api` aliases are rejected. Explicit manifest fields with invalid types, invalid IDs, invalid versions, invalid entrypoint paths, invalid dependency/permission/tag structures, invalid `provided_tools`, malformed scripts metadata, or unsupported schema versions fail closed.

An explicit manifest with schema `1` and API major `1` is statically `compatible`. An explicit manifest with a valid schema but unsupported API major is `incompatible`. This classification does not mean a runtime exists or that execution is safe or supported.

### Generic metadata

`plugin.json`, `manifest.json`, and `package.json` can be inspected as metadata. They remain `unknown` unless they establish an explicit Aetherion plugin contract. Merely adding `plugin_api_version: "1.0"` to a generic `plugin.json` no longer produces `compatible`.

Repositories without supported metadata are source-only and `unknown`.

## Exact User Commands

```text
# 1. Discover or save
aetherion plugins discover "agent plugin" --language Python --limit 10
aetherion plugins catalog add https://github.com/owner/repo --id my-repo --ref main

# 2. Review metadata and immutable selection
aetherion plugins catalog info my-repo
aetherion plugins catalog list

# 3. Explicitly stage the saved pin
aetherion plugins install my-repo

# 4. Verify managed-source integrity/status
aetherion plugins status my-repo
aetherion plugins info my-repo

# 5. Observe upstream movement without changing the pin
aetherion plugins catalog refresh my-repo

# 6. Explicitly select a new pin, then stage it
aetherion plugins catalog pin my-repo --ref v2
aetherion plugins update my-repo

# 7. Remove managed source separately from catalog data
aetherion plugins uninstall my-repo --yes
aetherion plugins catalog remove my-repo --yes
```

All commands are metadata/source-management operations. No command in this lifecycle activates or executes a plugin.

## Modified Files

- `cli/aetherion_cli/plugin_manager.py`
  - Saved-pin enforcement for installation and inspection.
  - Alias resolution for installed status/update/uninstall.
  - Catalog source-ID persistence and mismatch rejection.
  - Catalog-backed update pin enforcement and concurrency checks.
  - Explicit manifest schema/API/type validation.
  - Conservative generic-manifest compatibility classification.
- `cli/aetherion_cli/main.py`
  - Lifecycle status output for install/update.
  - Alias-oriented help and info output.
  - Explicit distinction between staging, integrity, compatibility, and execution.
- `cli/aetherion_cli/tui/state.py`
  - Plugin status, integrity, and installed-commit fields.
- `cli/aetherion_cli/tui/gateway.py`
  - TUI mapping and notifications for managed-source lifecycle state.
- `cli/aetherion_cli/tui/screens/plugins.py`
  - Status/integrity/compatibility/commit display.
- `cli/tests/test_plugin_manager.py`
  - Catalog alias installation, pinned update, source identity mismatch, independent removal, deceptive metadata, malformed manifest, and unsupported schema coverage.
- `cli/tests/test_cli.py`
  - Lifecycle output coverage.
- `cli/tests/test_tui_state.py`
  - Lifecycle status/integrity state preservation.
- `cli/README.md`
  - Actual catalog-to-install/update/uninstall workflow and manifest contract.

## Verification Results

### Focused lifecycle and regression tests

Command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -o addopts="" -q cli/tests/test_plugin_manager.py cli/tests/test_cli.py cli/tests/test_tui_state.py
```

Result after lifecycle changes:

```text
131 passed in 12.25s
```

### Full CLI test suite

Command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -o addopts="" -q cli/tests
```

Result:

```text
216 passed in 27.21s
```

### Targeted lint

Command:

```powershell
python -m ruff check cli/aetherion_cli/plugin_manager.py cli/tests/test_plugin_manager.py --select E4,E7,E9,F,I,B,W --output-format concise
```

Result:

```text
All checks passed!
```

### CLI and TUI smoke checks

The following help/import checks passed:

```powershell
$env:PYTHONPATH='cli'
$env:PYTHONDONTWRITEBYTECODE='1'
python -m aetherion_cli.main plugins catalog --help
python -m aetherion_cli.main plugins install --help
python -m aetherion_cli.main plugins update --help
python -m aetherion_cli.main plugins status --help
python -c "import aetherion_cli.tui.gateway; import aetherion_cli.tui.screens.plugins; print('tui imports ok')"
```

The help output confirms that the existing top-level install command accepts a saved catalog alias and that update identifies `PLUGIN_ID_OR_CATALOG_ALIAS`.

## Bounded Live End-to-End Validation

Live access was available. A disposable `TemporaryDirectory` under the approved temporary workspace was used as the manager root. The real user plugin directory, catalog, registry, local REA checkout, and source workspace were not used.

Repository tested:

- URL: `https://github.com/octocat/Hello-World`
- Alias: `live-hello-world`
- Requested ref: `master`
- Resolved/pinned commit: `7fd1a60b01f91b314f59955a4e4d4e80d8edf11d`

Observed results:

- Discovery returned `1` bounded candidate.
- Catalog save and archive inspection succeeded.
- Alias installation used the saved full commit.
- Installed source integrity: `verified`.
- Repeated installation returned `idempotent: true`.
- Catalog pin remained unchanged after installation.
- Alias-based integrity status returned `verified`.
- Invalid alias installation was rejected.
- Alias-based managed-source removal succeeded.
- Catalog record remained after managed-source removal.
- Catalog execution state remained `false`.

The temporary live run did not execute upstream source, imports, package scripts, binaries, dependency installation, or subprocesses.

## Protected-State Verification

Initial baseline captured before edits:

- Branch: `main`
- Application HEAD: `5c7bdcd4808a178aa677488f18b948a747f94193`
- `backend/routes/d3_features.py`: `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c`
- `.git/index`: `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e`
- `.git/index.lock`: absent
- Local `rea/` branch: `main`
- Local `rea/` HEAD: `e13da084e7c5967b80632ae5ad8d3d37bcf57469`
- Local `rea/` tracked status: clean

Post-implementation verification:

- `backend/routes/d3_features.py` hash unchanged: `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c`
- `.git/index` hash unchanged: `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e`
- `.git/index.lock`: absent
- Local `rea/` remains on `main` at `e13da084e7c5967b80632ae5ad8d3d37bcf57469`
- The only tracked pre-existing modification remains `backend/routes/d3_features.py`; it was not edited.
- No commit, push, reset, clean, index mutation, or unrequested deletion occurred.

## Final Hashes for Lifecycle Source and Test Files

```text
cli/aetherion_cli/plugin_manager.py       12e46312c4a2b3cca1707f47a0cb746a3836514ae6b45aa87a90e6267a77d15d
cli/aetherion_cli/main.py                 0a6cf0152708dcd106c4001e4b297ba9442c61ab4ecf71c340341b8d46e17fea
cli/aetherion_cli/tui/gateway.py           abc0cccaab19e9f7f4a754de1e8ef5156a8d91aa6c90c47f0979f57a0ca35f06
cli/aetherion_cli/tui/screens/plugins.py  fd5f391554a0bc61e03efdb9a027b9e58e686b981b3a365b19473a1978b0a072
cli/aetherion_cli/tui/state.py             14850d885f2d181271735db4fb6348931f849167adf9006b0e411db100c48c42
cli/tests/test_plugin_manager.py           a5d1bc0630054d5c5f1dd2efabdc802f2005331315a69be4b2e5937fac06131d
cli/tests/test_cli.py                      cc641e908b34505c60d31a4a47dae35fb2f3f617a8906c464d4553220f7d9be4
cli/tests/test_tui_state.py                3c3fbb532dc8acb024092a35792931b86bf29ea29cd94d5a441e8f239743cc46
cli/README.md                              9807a4877d142b0fa68f81eff7142b06f08230269f9a8ac2796aae9cc14716cb
```

## Remaining Limitations and Non-Claims

- This remains a source-management lifecycle, not an executable plugin runtime.
- `compatible` means only that the static manifest contract and API major passed validation; it does not certify runtime behavior or trustworthiness.
- Generic repositories without an explicit supported manifest remain source-only and `unknown`.
- The TUI displays and stages saved entries through the same manager, but discovery remains CLI-first.
- The full repository-wide test suite and broad Ruff check were not claimed; the complete CLI suite and focused manager lint checks were run and passed.
- No dependency resolution, license approval, sandbox execution, runtime health check, or dynamic plugin activation was performed.
