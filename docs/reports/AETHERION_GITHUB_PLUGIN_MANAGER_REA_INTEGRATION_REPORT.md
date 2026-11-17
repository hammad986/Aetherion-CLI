# Aetherion GitHub Plugin Manager / REA Integration Report

**Date:** 2026-10-17  
**Scope:** Aetherion CLI control-plane plugin management  
**Execution policy:** Option A / execution disabled  
**Result:** Implemented and verified with offline synthetic repositories

## 1. User-visible result

The CLI now has a separate metadata-only GitHub plugin manager. The following
commands operate on a local catalog and quarantined source records under
`~/.aetherion/plugins/`:

```text
aetherion plugins list [--installed]
aetherion plugins search [query]
aetherion plugins info <id-or-source>
aetherion plugins inspect <github-url-or-curated-id> [--ref REF]
aetherion plugins install <github-url-or-curated-id> [--ref REF] [--expected-sha256 HASH]
aetherion plugins status [plugin-id]
aetherion plugins update <plugin-id> [--source SOURCE] [--ref REF]
aetherion plugins uninstall <plugin-id> [--yes]
```

`check`, `verify`, and `deps` now inspect local metadata only. `enable`,
`disable`, and executable-plugin `rollback` fail closed with an explicit
Option A message. The TUI Plugins screen uses the same local manager and
labels stored sources as **metadata-only**.

## 2. Safety and lifecycle contract

- Only HTTPS GitHub repository-root URLs are accepted for custom sources.
- Credentials, ports, query strings, fragments, non-GitHub hosts, tree/blob/raw
  URLs, control characters, traversal-like paths, and ambiguous refs are
  rejected.
- Branches and tags are resolved through the GitHub API to a full 40-character
  commit SHA. The requested ref, resolved commit, archive URL, archive SHA-256,
  manifest SHA-256, file count, and per-file SHA-256 values are persisted.
- Archive validation rejects absolute paths, backslash/stream paths, traversal,
  duplicate paths, symlinks, encrypted entries, protected credential/key file
  names, files over 10 MiB, archives over 50 MiB, and archives over 500 entries.
- Metadata is read from `aetherion.plugin.json`, `plugin.json`, `manifest.json`,
  or `package.json`; no metadata is imported as Python or JavaScript.
- Dependencies, package scripts, entrypoints, permissions, and tool declarations
  remain untrusted informational metadata. No npm, npx, package installer,
  lifecycle hook, subprocess, MCP server, or plugin entrypoint is run.
- Installs are staged under a temporary managed directory, written with an
  integrity manifest, promoted with an atomic rename, and rolled back if the
  registry update fails.
- Reinstalling the same archive is idempotent. Updating requires the source
  metadata ID to match the installed ID. Removal is restricted to the managed
  plugin root.
- Integrity status is recalculated from the stored source tree and is reported
  as `verified`, `mismatch`, `missing`, `invalid-path`, or `corrupt`.
- The implementation does not use the executable backend `ExtensionManager`,
  `installed_plugins` table, runner, worker, container, tool bridge, health
  execution, or enable/disable routes.

## 3. REA integration and provenance

The curated catalog contains one entry:

| Field | Verified value |
|---|---|
| ID | `rea` |
| Repository | `https://github.com/morluto/rea` |
| Package | `rea-agents` |
| Remote default branch | `main` |
| Remote main SHA observed | `4e14452ddbf4642235c8a68435f72837510b2075` |
| Remote package/server version | `6.5.0` |
| Runtime requirement | Node `^22.19.0 || ^24.11.0 || >=26.0.0` |
| npm requirement | `11.16.0` |
| License | MIT |
| Attribution | `2026 morluto` |
| Execution state in Aetherion | Disabled; metadata-only |

The workspace-local `rea/` checkout was inspected read-only and was clean on
branch `main` at commit `e13da084e7c5967b80632ae5ad8d3d37bcf57469`. It reports
version `6.4.0`, which differs from the observed remote `6.5.0`; the manager
does not claim those versions are identical. The local checkout was not copied,
modified, installed, built, or executed.

The local checkout's MIT attribution evidence was preserved and recorded as:

```text
rea/package.json  SHA-256 4dab2dc0f78506940e89277c6696e96d0fe461d20c09ebe7ed91a9ed4a303820
rea/README.md     SHA-256 eaf16011c791c8a34692eb2637e222e6c17fa70f479db3b82995e55e23c7db7a
rea/LICENSE       SHA-256 798f9e4d18e49b30bf2f12696c8ced73dc4e1413da375a333d01281543a1ea8b
```

The curated entry records the observed remote commit as provenance metadata,
while a normal install resolves the requested ref at install time. A full SHA
can be supplied with `--ref` for an explicitly pinned install.

## 4. Changed files and SHA-256 evidence

The repository contained extensive pre-existing untracked source and report
files. Hashes below are workspace-file hashes captured immediately before and
after this task; `not present` means the file was newly added by this task.

| File | Before | After |
|---|---|---|
| `cli/aetherion_cli/plugin_manager.py` | not present | `9bdf770670453aca3c84dd2981db4eab5344646dc504430924a2eefbc9143fb6` |
| `cli/aetherion_cli/main.py` | `3ab485aa266aa3b3557cb8af28461125fea0876eca1955850e0fdcb2f0e91f9c` | `73a343b1bfe22e8d410da285de23c586cb6b47c20a75be9d36a0fbe446059cb1` |
| `cli/aetherion_cli/tui/gateway.py` | `df9dc03aa4f436e3d6ba773edc6ea0cae7c34c0c4efff92ac7484d4102b44438` | `3d7a4a0daa7bd75bd731dac79a34ad9427189276d4aa558c6613d0884edd01d8` |
| `cli/aetherion_cli/tui/screens/plugins.py` | `2eedf8158eabdb98b97238fafcfdbea30535e9bfabe909364165086bb60e35ac` | `c6bea483f46b00c94420858932856078769c136bcd6406bb6153630948e12e44` |
| `cli/README.md` | `5b5b323dd298d90bda87769323af215476627053bf2cb39a6bd86989052de81c` | `251c13f0247ddc6b1dd2ac2c1cda3345a04cb8194d5dc74939e5c347231caf8b` |
| `cli/tests/test_cli.py` | `b7926b687f3278d1fd220015975ef0ab0b143aa529cd19d64f4cbbfb65fdba47` | `7d47062cba03f9a9124b4f8db7da818e70dfbe4232653f688c67e68cf338b732` |
| `cli/tests/test_plugin_manager.py` | not present | `57984994f2c9876244cdf8aeef5f896876e91a89cbe3675ae291b5281fcff12d` |

## 5. Verification

### Passed

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q cli/tests/test_plugin_manager.py cli/tests/test_cli.py cli/tests/test_tui_state.py
100 passed

python -m ruff check cli/aetherion_cli/plugin_manager.py cli/tests/test_plugin_manager.py --select E4,E7,E9,F,I,B,W --output-format concise
All checks passed

CLI import/help smoke check
`aetherion plugins --help` listed list, search, info, inspect, install, status,
update, uninstall, safe integrity/dependency commands, and fail-closed activation commands.
```

The synthetic manager suite contains 11 tests, including URL policy rejection,
curated REA metadata, non-execution inspection, idempotent install, tamper
detection, atomic update, managed removal, and archive traversal rejection.
The CLI suite contains 44 tests and the TUI state suite contains 45 tests.

### Protected-state evidence

```text
backend/routes/d3_features.py SHA-256: 592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c
.git/index SHA-256:               18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
.git/index.lock:                  absent
```

No backend, D3, environment, credential, deployment, or local REA files were
changed. No Git index mutation, commit, push, service start, package install,
or plugin execution was performed.

## 6. Gaps and explicit non-claims

- No live GitHub archive was installed during verification; network behavior was
  tested through deterministic synthetic fetchers to keep the check offline.
- The upstream REA repository was not run, built, installed, or used as a test
  fixture. Its installation/build instructions were recorded as provenance only.
- The full repository test suite was not run because the requested verification
  scope is focused and the workspace contains unrelated legacy suites.
- Broad repository Ruff output is not a clean gate because the existing CLI has
  unrelated legacy lint findings; the new manager and its focused tests pass the
  scoped correctness/security lint command above.
- This feature deliberately does not enable executable plugins, install their
  dependencies, or bridge them into agent tools. Those require a separate,
  explicitly approved execution-policy change.

## 7. Next step

If executable plugin support is later requested, first create a separate design
and security review for that policy change. Until then, use a full commit SHA
with `aetherion plugins inspect/install/update` when reproducible provenance is
required, and keep the source in its metadata-only state.

> The report's own SHA-256 is intentionally reported by the final verification
> command rather than embedded in this file, avoiding a self-referential hash.
