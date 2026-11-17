# Aetherion GitHub Plugin Manager / REA Security Audit Report

**Date:** 2026-10-17  
**Verdict:** **PASS WITH FINDINGS**  
**Execution policy:** Option A / control-plane only / execution disabled

## Executive verdict

The implementation was audited adversarially rather than accepted from the
previous integration report. Eight security/lifecycle defects and one real-REA
availability defect were confirmed and fixed in the scoped manager. The final
manager now authenticates expected file hashes outside the mutable source tree,
serializes lifecycle mutations, journals promotion/removal transactions,
recovers interrupted operations, rejects managed-root symlink/reparse escapes,
normalizes archive paths, and pins curated `rea` selection to its verified
commit.

The verdict remains **PASS WITH FINDINGS**, not production-readiness approval:
custom mutable GitHub refs remain inherently time-varying, GitHub API and
`git ls-remote` observed different moving-`main` answers during this audit, no
signature/MAC trust system was added for the local registry, and execution is
intentionally unavailable.

## Scope

### Inspected

- `cli/aetherion_cli/plugin_manager.py`
- `cli/aetherion_cli/main.py`
- `cli/aetherion_cli/tui/gateway.py`
- `cli/aetherion_cli/tui/screens/plugins.py`
- `cli/aetherion_cli/tui/state.py`
- `cli/tests/test_plugin_manager.py`
- `cli/tests/test_cli.py`
- `cli/tests/test_tui_state.py`
- `backend/execution/control_plane_policy.py`
- Existing backend extension lifecycle files under
  `backend/execution/extensions/`, including manager, fetcher, store, security,
  runner, worker, container, and tool bridge paths.
- Existing integration tests covering plugin lifecycle and the execution lock.
- Workspace state, `.git/index`, protected D3 source, local `rea/` checkout, and
  read-only GitHub metadata.

### Changed

- `cli/aetherion_cli/plugin_manager.py`
- `cli/tests/test_plugin_manager.py`
- `AETHERION_GITHUB_PLUGIN_MANAGER_REA_SECURITY_AUDIT_REPORT.md` (this report)

No CLI/TUI caller source was changed during this audit because the existing
callers already route plugin actions to the metadata-only manager and fail
closed for activation.

## Confirmed findings and fixes

### Medium — F-01: mutable integrity manifest could be forged

**Evidence:** `status()` previously trusted `integrity.json["files"]`, even
though that file lived inside the mutable installed plugin directory.

**Fix:** The registry now stores the authoritative per-file hash map and a
canonical integrity-manifest digest. Status requires the source hashes,
`integrity.json`, manifest hash, source root, archive hash, and registry digest
to agree. Tampering only `integrity.json` is now reported as `mismatch`.

### Medium — F-02: uninstall was not failure-atomic

**Evidence:** Removal deleted the managed directory before updating the
registry. A registry write failure could leave a record pointing at missing
source.

**Fix:** Removal now moves the directory to managed staging, publishes the
registry change, and deletes the backup only after publication. Failure recovery
restores the source and leaves the registry consistent.

### Medium — F-03: registry read/modify/write races

**Evidence:** Concurrent CLI/TUI operations could write stale registry snapshots
and lose another plugin record.

**Fix:** Lifecycle mutations use a cross-process lock (`msvcrt` on Windows,
`fcntl` on POSIX), re-read the registry while locked, and keep installs
idempotent. A concurrent synthetic install test preserves both records.

### Medium — F-04: abrupt interruption could strand promotion state

**Evidence:** The old implementation caught ordinary `Exception` only and had
no durable transaction state between backup, promotion, and registry publish.

**Fix:** Install/update/remove now use an atomic transaction journal with
recoverable phases. Recovery runs during manager initialization and handles
`BaseException` rollback paths, including `KeyboardInterrupt`/`SystemExit`
boundaries. Fault-injection tests cover failed registry publication and startup
recovery after publication.

### Low/Medium — F-05: staging symlink/reparse escape

**Evidence:** A pre-existing `.staging` symlink could redirect temporary
extraction or backups outside the managed root.

**Fix:** The managed root, staging directory, lock, registry, journal, plugin
directories, and transaction paths reject symlinks and Windows reparse points;
resolved paths must remain under the managed root/staging directory.

### Medium — F-06: malformed registry IDs and plugin aliases were unsafe

**Evidence:** Registry-derived IDs were not validated before path construction,
and an in-root symlink alias could resolve to another plugin during removal.

**Fix:** Registry records are validated against the strict plugin ID grammar,
record IDs must match registry keys, paths are confined before use, and symlink
or reparse aliases are rejected. Malformed records are reported corrupt without
accessing an outside path.

### Low — F-07: normalized archive collisions were ambiguous

**Evidence:** Raw ZIP names were deduplicated before normalization, allowing
forms such as `root/file.txt` and `root/./file.txt`; final-dot and
case-insensitive Windows collisions were not fully rejected.

**Fix:** ZIP member names are NFC-normalized, empty/dot/traversal components are
rejected, canonical collision keys are used, and Windows-ambiguous trailing
dot/space and case-collision forms are rejected.

### Medium — F-08: curated `verified_commit` was informational only

**Evidence:** The previous curated `rea` selector resolved mutable `main` and
only recorded the catalog commit after fetching.

**Fix:** Curated short IDs now pin to their catalog `verified_commit` by
default. A different ref is rejected for the curated ID; users must use the
full repository URL for an explicitly uncurated ref. Curated updates preserve
the same pinning behavior.

### Medium — F-09: previous limits blocked the real REA archive

**Evidence:** The first live inspection failed with the old 500-entry limit.
The actual verified REA archive measured 4,561 entries, 4,192 files, 15,840,478
compressed bytes, and 53,654,076 uncompressed bytes.

**Fix:** Limits were raised only to bounded values that admit the observed REA
source: 64 MiB compressed/download and aggregate extracted content, 5,000
entries/files, and 10 MiB per file. The archive remains finite and is still
subject to path, symlink, duplicate, special-file, and ZIP-bomb checks. A test
confirms the 5,000-entry ceiling remains enforced.

## Areas confirmed safe

- Only HTTPS GitHub repository roots are accepted; credentials, ports, queries,
  fragments, non-GitHub hosts, tree/blob/raw paths, controls, and malformed
  owner/repository values are rejected.
- Archive redirects are not followed automatically. A redirect is accepted only
  when its host is exactly `codeload.github.com` over HTTPS.
- API/archive responses have bounded streaming reads and content-type checks.
- Branch/tag resolution produces a full 40-character SHA, and the archive URL
  is constructed directly from that SHA. Full SHA refs bypass mutable lookup.
- ZIP extraction rejects absolute paths, drive/stream syntax, backslashes,
  traversal, duplicate/colliding paths, symlinks, encrypted entries, special
  files, protected credential/key names, excessive size, and excessive count.
- Metadata/package scripts, dependencies, entrypoints, permissions, and tool
  declarations remain inert data. No import, subprocess, package manager,
  lifecycle hook, MCP server, or plugin entrypoint exists in this path.
- CLI/TUI plugin callers use `PluginCatalogManager`; they do not call the
  backend executable `ExtensionManager`, runners, workers, containers, tool
  bridges, execution routes, or execution tables.
- `enable`, `disable`, and executable rollback remain fail-closed. `check`,
  `verify`, and `deps` remain metadata/integrity-only.

## Live GitHub validation

Network access was available. All live checks used a disposable temporary root
under the approved temporary directory. No real `~/.aetherion/plugins/` path,
local `rea/` checkout, package manager, service, subprocess, or plugin runtime
was used.

### Curated REA snapshot workflow

The curated ID is intentionally pinned to:

```text
https://github.com/morluto/rea
verified commit: 4e14452ddbf4642235c8a68435f72837510b2075
```

The final live run against the current audited code performed:

1. `inspect("rea")` — passed; resolved the pinned SHA.
2. `install("rea")` — passed; archive size `15,840,478` bytes and source file
   count `4,192`.
3. `status("rea")` — passed with `integrity=verified`.
4. `info("rea")` — confirmed `execution_enabled=false`.
5. `remove("rea")` — passed; only the managed plugin directory was removed and
   an outside sentinel file was preserved.

Separate isolated live runs also passed:

- Repeated install returned `idempotent=true`.
- Tampering with `package.json` changed status to `mismatch`.
- `update("rea")` restored the source and returned `integrity=verified`.

An initial all-in-one live attempt timed out during a fourth repeated archive
download after inspect/install/repeat/tamper had already passed. The required
steps were rerun in bounded isolated workflows; update and removal completed
successfully. This is recorded rather than presented as one uninterrupted
network session.

### Current mutable `main` observation

Read-only upstream observations during the audit were not identical:

```text
git ls-remote refs/heads/main: 6b27d8f4e3be77bbd3f9a3a3c93d6f01b0a401e0
GitHub API /commits/main: 5a7e8a166a07a0c74700134f88671ac88830a543
```

The production manager's custom URL path used the GitHub API response and
constructed the archive URL from `5a7e8a166a07a0c74700134f88671ac88830a543`.
Live metadata inspection succeeded with version `6.5.0`, `4,205` files, and
`execution_enabled=false`. This demonstrates why mutable refs must not be
called immutable provenance. Curated `rea` remains pinned to the previously
verified `4e144...` snapshot; custom `main` remains explicitly uncurated.

### REA provenance

The local checkout remains a clean, untouched repository on branch `main` at
`e13da084e7c5967b80632ae5ad8d3d37bcf57469`. Its local package is `6.4.0`; the
observed remote package is `6.5.0`. The upstream package is MIT licensed with
`2026 morluto` attribution. No REA dependency was installed and no REA source,
script, binary, MCP server, or lifecycle hook was executed.

## Regression and verification results

### Passed

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -o addopts="" -q cli/tests/test_plugin_manager.py cli/tests/test_cli.py cli/tests/test_tui_state.py
111 passed in 9.87s

python -m ruff check cli/aetherion_cli/plugin_manager.py cli/tests/test_plugin_manager.py --select E4,E7,E9,F,I,B,W --output-format concise
All checks passed

CLI import/help smoke check
plugins help smoke: passed

Plugin execution-lock smoke check
enable/disable/rollback rejected; check/verify/deps completed through mocked metadata-only paths.
```

The focused aggregate contains 22 plugin-manager tests, 44 CLI tests, and 45
TUI state tests (111 total). The plugin
manager tests cover URL policy, curated pinning, response/archive boundaries,
integrity-manifest tampering, normalized ZIP collisions, staging and alias
symlinks, concurrent installs, failed removal/update recovery, startup journal
recovery, idempotency, source tampering, and safe removal.

### Not run / not claimed

- No full repository suite was run.
- No REA `npm`, `npx`, build, test, package, or runtime command was run.
- No Aetherion service, backend, worker, container, MCP server, or deployment
  was started.
- No production readiness or executable-plugin readiness is claimed.
- Broad repository Ruff remains outside the scoped gate because unrelated
  legacy CLI findings exist; the new manager and adversarial tests pass the
  scoped correctness/security selection above.

## Protected workspace evidence

Initial and final protected hashes remained unchanged:

```text
backend/routes/d3_features.py
592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c

.git/index
18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e

.git/index.lock: absent
```

The initial workspace already contained the pre-existing D3 modification and
extensive untracked reports/source files. This task added only the changed
manager, its security tests, and this report. The local `rea/` checkout remained
clean. No index mutation, commit, push, reset, deletion of untracked files, or
overwrite of existing reports occurred.

## File hash evidence

Hashes were captured immediately before and after this audit's source edits.
The report's own final hash is reported externally in the completion response.

| File | Before | After |
|---|---|---|
| `cli/aetherion_cli/plugin_manager.py` | `9bdf770670453aca3c84dd2981db4eab5344646dc504430924a2eefbc9143fb6` | `24e0a5b5e8dff48a6ff2f78e82639ca24eb9512f641405f16c63cc5dc3a2c014` |
| `cli/tests/test_plugin_manager.py` | `57984994f2c9876244cdf8aeef5f896876e91a89cbe3675ae291b5281fcff12d` | `47ef81768c797daa2c2566ac0bc7d0d1eaa919cae83fe075227ec8ac7764a4e9` |
| `AETHERION_GITHUB_PLUGIN_MANAGER_REA_SECURITY_AUDIT_REPORT.md` | not present | reported externally |

## Remaining risks and next necessary step

1. A custom branch/tag is mutable and the GitHub API answer can differ from a
   separate ref observation. For reproducibility, operators must use a full
   commit SHA and, where available, an out-of-band archive SHA-256.
2. The local registry is an integrity authority for ordinary source tampering,
   not a cryptographic trust anchor against a fully privileged local attacker.
   A future trust model would need an OS-protected signing key or MAC storage.
3. The manager stores untrusted source for inspection only. Any future request
   to enable execution requires a separate design, sandbox review, explicit
   policy change, and new end-to-end security evidence.

**Next necessary step:** If stronger provenance is required, add a separately
reviewed signed catalog/registry design. Do not enable execution as a follow-up
to this audit.
