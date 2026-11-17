# Aetherion Generic GitHub Catalog and Discovery Implementation Report

**Date:** 2026-10-17  
**Scope:** CLI plugin manager, CLI workflows, TUI metadata/actions, tests, and documentation  
**Execution mode:** Option A — source-locked metadata and inspection only

## Executive Summary

The Aetherion CLI plugin manager now supports a generic, persistent user catalog of public GitHub repositories in addition to the existing curated catalog. Users can discover repositories through the GitHub Search API, save arbitrary public repository URLs under local aliases, inspect repository metadata without installing, refresh branch/tag observations without silently changing a pin, and explicitly pin a new ref.

The implementation keeps curated repositories and user entries separate, records both the requested ref and resolved immutable commit SHA, classifies compatibility conservatively, and exposes provenance and integrity metadata in CLI and TUI views. No repository code is executed, no package scripts are run, and no dependency installation path was added.

## Scope and Non-Goals

### Included

- Persistent user catalog storage under the manager's managed plugin root (`catalog.json`).
- Read-only GitHub discovery with bounded result counts and optional language/star/archive filters.
- URL validation, repository metadata retrieval, ref resolution, archive inspection, and source-only fallback.
- Explicit compatibility evidence and `compatible` / `incompatible` / `unknown` status.
- Candidate refresh and explicit pin workflows.
- CLI-first user workflows with TUI metadata and refresh support.
- Deterministic fake-backed tests plus bounded live validation.

### Explicitly excluded

- Automatic installation after catalog save.
- Execution of repository or plugin code.
- Dependency installation, package-manager commands, subprocesses, workers, MCP servers, or agents.
- Automatic promotion of user repositories into the curated catalog.
- Trust decisions based only on stars, forks, topics, activity, or declared license.
- Backend wiring to `ExtensionManager`, runners, workers, or execution tables.

## Implemented Behavior

### Catalog model

User records are persisted separately from curated entries and contain, where available:

- Stable user alias and repository name.
- Canonical GitHub source URL.
- Repository metadata such as description, language, topics, stars, forks, license, and default branch.
- Requested ref and resolved commit SHA.
- Observed/candidate commit SHA during refresh.
- Manifest information and compatibility evidence.
- Catalog origin (`curated` or `user`).
- Installed-source linkage, integrity status, and execution-disabled state.
- Last inspection/refresh timestamps, update availability, and bounded error information.

The catalog schema is versioned and malformed catalog data fails closed rather than being silently repaired or executed.

### Safe source handling

The existing manager safeguards remain in force:

- Strict canonical GitHub URL validation.
- Validated branch, tag, and full-SHA refs.
- Bounded streaming metadata and archive downloads.
- GitHub rate-limit and content-type handling.
- Safe ZIP extraction with traversal, symlink/reparse, file-count, per-file, and total-size checks.
- Transaction journaling, atomic registry/catalog writes, rollback, and inter-process locking.
- Archive and manifest inspection only; no source import, subprocess, package script, or dependency execution.

Repositories without a supported Aetherion manifest are represented as source-only entries with compatibility `unknown`; they are not treated as installable or compatible merely because an archive downloaded successfully.

### Compatibility classification

Compatibility is evidence-based:

- `compatible`: explicit supported `plugin_api_version` major `1` is present.
- `incompatible`: an explicit unsupported plugin API major is present.
- `unknown`: no supported compatibility contract is present, including generic repositories without an Aetherion manifest.

The catalog preserves the reasons and evidence used for the classification.

## User Workflows

The following commands are implemented and documented in `cli/README.md`:

```text
aetherion plugins catalog list
aetherion plugins catalog add <github-url> --id <alias> [--ref <ref>]
aetherion plugins catalog info <id>
aetherion plugins catalog refresh <id>
aetherion plugins catalog refresh --all
aetherion plugins catalog pin <id> --ref <ref>
aetherion plugins catalog remove <id> [--yes]
aetherion plugins discover <query> [--limit N] [--language LANG] [--min-stars N]
```

`catalog add` validates the URL, retrieves public repository metadata, resolves the requested ref, inspects the bounded archive, and persists the record. It does not install the repository or create an executable runtime entry.

`catalog refresh` observes the current ref target and records a candidate commit when it differs from the selected pin. It does not replace `resolved_commit`. `catalog pin` is the explicit operation that changes the selected ref and immutable commit.

Discovery returns evidence only. It does not save, install, or execute results. Search results include source URL, resolved-commit placeholder when not explicitly resolved, language, topics, license metadata, stars, forks, archive status, compatibility status, and provenance.

The TUI now shows source origin, requested ref, selected commit, compatibility state, and execution-disabled status. User-catalog entries have a `Refresh pin` action that checks for a candidate without changing the saved pin. Discovery remains CLI-first because the existing TUI has no repository-search screen.

## Files Changed for This Feature

- `cli/aetherion_cli/plugin_manager.py`
  - Catalog persistence and schema validation.
  - User alias lifecycle.
  - GitHub metadata fetch and bounded discovery.
  - Source-only fallback and compatibility evidence.
  - Refresh/pin semantics.
- `cli/aetherion_cli/main.py`
  - Catalog and discovery Click commands.
  - Search/list/info rendering for origin, compatibility, and status.
- `cli/aetherion_cli/tui/state.py`
  - Plugin metadata fields for origin, refs, commit, compatibility, and integrity.
- `cli/aetherion_cli/tui/gateway.py`
  - Metadata-only catalog loading and user-ref refresh action.
- `cli/aetherion_cli/tui/screens/plugins.py`
  - Catalog metadata display and refresh control.
- `cli/tests/test_plugin_manager.py`
  - Persistence, validation, discovery, source-only, compatibility, refresh, and corruption coverage.
- `cli/tests/test_cli.py`
  - Catalog and discovery CLI behavior.
- `cli/tests/test_tui_state.py`
  - Preservation of catalog metadata in TUI state.
- `cli/README.md`
  - Catalog, discovery, pinning, compatibility, and security documentation.

## Verification Evidence

### Focused automated tests

Command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -o addopts="" -q cli/tests/test_plugin_manager.py cli/tests/test_cli.py cli/tests/test_tui_state.py
```

Result:

```text
124 passed in 11.19s
```

The suite includes the previous archive/integrity/rollback/concurrency protections and the new catalog/discovery cases. A prior intermediate run exposed a catalog-list assertion/rendering mismatch; the assertion now checks the displayed repository name and the focused suite is green.

### Lint verification

Command:

```powershell
python -m ruff check cli/aetherion_cli/plugin_manager.py cli/tests/test_plugin_manager.py --select E4,E7,E9,F,I,B,W --output-format concise
```

Result:

```text
All checks passed!
```

A broader check across the entire large CLI/TUI surface still reports existing legacy issues in `main.py`, `gateway.py`, TUI files, and older test imports. Those unrelated issues were not mass-formatted or rewritten as part of this focused feature.

### CLI smoke checks

Verified that the following help surfaces load successfully:

```text
python -m aetherion_cli.main plugins catalog --help
python -m aetherion_cli.main plugins discover --help
```

The help output exposes all catalog subcommands and discovery filters without invoking network access or installation.

### Bounded live validation

An isolated `TemporaryDirectory` under the approved temporary workspace was used; the real user plugin root and local REA checkout were not touched.

Live discovery:

- Query: `aetherion`
- Limit: `3`
- Returned results: `3`
- Returned fields included source URL, repository identity, stars, forks, language, topics, license metadata, archive state, compatibility, provenance, and execution state.

Live user-catalog save and inspect:

- Source: `https://github.com/octocat/Hello-World`
- Alias: `live-hello-world`
- Requested ref: `master`
- Resolved commit: `7fd1a60b01f91b314f59955a4e4d4e80d8edf11d`
- Compatibility: `unknown` (source-only repository)
- Catalog origin: `user`
- Temporary catalog rows observed: `2` (one curated row plus the saved user row)
- Temporary `catalog.json`: created successfully

Execution-lock smoke check:

```text
all_execution_disabled: true
```

No install, import, subprocess, package script, or dependency installation was invoked during live validation.

## Protected State and Repository Safety

The following protected items were intentionally left untouched:

- `backend/routes/d3_features.py`
  - Initial/current protected hash recorded as:
    `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c`
- `.git/index`
  - Initial/current protected hash recorded as:
    `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e`
- `.git/index.lock`
  - Confirmed absent during the initial safety check.
- Local `rea/` checkout
  - Confirmed clean on `main` at `e13da084e7c5967b80632ae5ad8d3d37bcf57469`.
- Existing audit and integration reports
  - Not overwritten.
- Secrets, `.env` files, credentials, deployment settings, and unrelated user changes
  - Not read or modified.

No commit, push, reset, rebase, dependency installation, or global configuration change was performed.

## Final SHA-256 Hashes for Touched Feature Files

These hashes were captured after the implementation and verification steps:

```text
cli/aetherion_cli/plugin_manager.py       f2e8b2aa16ec27dc10bd5638758d866e3528807982631401b34eeb79fb7b693e
cli/aetherion_cli/main.py                 b146f9299d5e27308d81b3d15b433f67d53c6f72beb0e8be9be64ea4e0d41fa7
cli/aetherion_cli/tui/gateway.py           455ca9307313cba8d104f2c1d1f58a701a62b1911279e287f20df23e4065a90f
cli/aetherion_cli/tui/screens/plugins.py  107c4882886761369933450b2fef96457e4e99386d8f62abfab626200e5d2b66
cli/aetherion_cli/tui/state.py             451ac62f2bd63a8b2b6fe81e8a12157d844206921e3268e8a86a81a09904bfbc
cli/tests/test_plugin_manager.py           eeed59d46ac1530fbfa8a08e9b3c666228e7883f78cece90bc466e1a330241b3
cli/tests/test_cli.py                      21c049904095da23aeecdaf65edc4c445bcfb9d20758883669f5ab7217784082
cli/tests/test_tui_state.py                817dcaf8b70f304962c1c8c8373a29fe15ecb13650910bb33ace4cc1f28be08a
cli/README.md                              7acc14ba31bf98a47d7e900ebba7105d0899f4ae8ed28be91e15ce679c622434
```

## Remaining Gaps

- The full repository test suite was not run because the repository contains unrelated and pre-existing surfaces outside this feature's scope.
- The broad Ruff check is not clean due to legacy issues in the large CLI/TUI modules; the focused manager and manager-test lint check is clean.
- Discovery results intentionally do not claim a resolved commit until the user explicitly saves or inspects a repository through the safe source-resolution path.
- A user catalog can save generic source-only repositories, but those entries remain `unknown` compatibility until a supported manifest explicitly establishes compatibility.
- The TUI exposes catalog metadata and refresh for saved user refs, while repository discovery is currently CLI-first.

## Final Assessment

The generic GitHub catalog and discovery flow is implemented with conservative trust semantics and preserves the existing execution-disabled architecture. The focused automated suite is green at 124 tests, the manager-specific lint check is green, the CLI help surfaces load, and isolated live discovery plus arbitrary repository save/inspect succeeded without touching protected state or executing repository code.
