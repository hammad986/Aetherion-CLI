# Aetherion Phase 4R — GitHub Publisher Evidence Security Remediation

**Date:** 2026-10-17  
**Scope:** F-001 through F-005 from the Phase 4 security audit  
**Mode:** Implementation with regression testing; no commit or push  
**Option A policy:** Metadata/source management only; plugin execution remains disabled

## 1. Executive result

Phase 4R remediation is implemented and verified for the supported normal
paths. The five audited findings were addressed as follows:

| Finding | Result |
|---|---|
| F-001 lifecycle recovery from evidence inspection | **Fixed for CLI and TUI evidence paths** with an explicit evidence-only manager mode and evidence-only TUI reload path. Default lifecycle construction/recovery remains unchanged. |
| F-002 forged positive cached status | **Fixed for this adapter's supported semantics.** Positive publisher authorization, matched identity policy, and local cryptographic verification are rejected during serialization/write/load. A semantically invalid individual record is ignored rather than projected or allowed to deny unrelated subjects. |
| F-003 Rich/terminal text injection | **Fixed in audited CLI/TUI evidence paths.** Untrusted values are converted to single-line escaped literal text before human rendering; JSON output remains structured. |
| F-004 evidence-store filesystem boundaries | **Hardened.** Actual opened-stream reads are bounded; all existing ancestors are checked; POSIX `O_NOFOLLOW` is used where available; locks use descriptor-based opening; temp files use exclusive `mkstemp`; replacement/cleanup failures fail closed. A residual Windows/ancestor-swap limitation is documented below. |
| F-005 ambiguous signature projections | **Fixed in audited common CLI/TUI projections.** Provider result, provider reason, Aetherion publisher identity, identity policy, local cryptographic verification, observation, and freshness are explicitly labelled. |

The remediation does not provision publisher identity, signer keys, a trust
root, a GitHub token, local GPG/SSH/S/MIME verification, Sigstore, in-toto, or
any production trust material. GitHub `verified` remains provider observation
only. `execution_enabled=False` remains enforced.

## 2. Actual repository and protected baseline

The actual repository was verified from Git metadata:

```text
Repository root: C:/Users/mdham/Downloads/Aetherion-AI-main/end to end aetherion cli
Branch:         main
Application HEAD: 5c7bdcd4808a178aa677488f18b948a747f94193
```

At the start of remediation, the workspace already contained user-owned and
phase-owned changes:

- `backend/routes/d3_features.py` was already modified.
- Numerous application, report, audit, deployment, scratch, and source paths
  were already untracked, including the prior Phase 4 implementation and
  security-audit reports.
- No staged changes were present.
- No `.git/index.lock` existed.
- The local `rea/` checkout was already on `main` at
  `e13da084e7c5967b80632ae5ad8d3d37bcf57469` and was clean.

Initial protected hashes:

```text
backend/routes/d3_features.py:
592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c

.git/index:
18fc638834262aff249e3bd5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
```

Those values, the application HEAD, branch, `rea/` state, and absence of
`.git/index.lock` were rechecked after implementation and remained unchanged.

## 3. Files changed and purpose

### Implementation

- `cli/aetherion_cli/plugin_manager.py`
  - Adds an explicit `evidence_only` construction mode.
  - Skips lifecycle root/staging initialization, manager lock acquisition,
    transaction recovery, and signed-catalog service construction in that mode.
  - Adds a no-lifecycle-lock evidence-only catalog/registry projection used by
    the TUI reload after evidence actions.
  - Provides conservative unavailable catalog/integrity fields for that
    metadata-only projection.
  - Leaves default install/update/remove construction and recovery unchanged.
- `cli/aetherion_cli/main.py`
  - Routes `plugins evidence` through `evidence_only=True`.
  - Adds a shared Rich-safe, single-line renderer for human-facing values.
  - Qualifies inspect, evidence, install, and info publisher fields.
  - Keeps JSON mode structured and unmodified semantically.
- `cli/aetherion_cli/tui/gateway.py`
  - Routes the Signature action and its post-action reload through evidence-only
    manager/list paths.
  - Sanitizes plugin-operation errors before TUI notifications.
- `cli/aetherion_cli/tui/screens/plugins.py`
  - Sanitizes Rich/TUI labels against markup and terminal controls.
  - Uses explicit provider, publisher identity, identity-policy, and local
    verification labels.
- `cli/aetherion_cli/publisher_evidence.py`
  - Adds adapter-semantic invariant validation.
  - Rejects positive publisher authorization, matched identity policy, and local
    cryptographic verification for this provider-only adapter.
  - Makes individual malformed/semantically impossible records non-projecting.
  - Replaces path-level size checks with bounded reads from opened descriptors.
  - Adds complete ancestor checks, descriptor-based lock handling, exclusive
    temporary files, safe replacement checks, and conservative cleanup errors.

### Directly related regression tests

- `cli/tests/test_publisher_evidence.py`
  - Evidence-only lifecycle journal/sentinel tests.
  - Storage-failure lifecycle preservation.
  - Fresh-root no-staging/no-manager-lock test.
  - Recomputed semantic-status forgery tests.
  - Symlinked ancestor/lock tests.
  - Bounded opened-stream read test.
  - Failed replacement/temp-cleanup test.
- `cli/tests/test_cli.py`
  - Evidence-only manager construction assertion.
  - Markup/control sanitization test.
  - Explicit inspect/info/install projection-label tests.
- `cli/tests/test_tui_plugins.py`
  - Evidence-only manager and evidence-only reload assertions.
  - TUI label and notification sanitization tests.

No unrelated backend, deployment, trust-root, catalog, registry, credential, or
production configuration file was changed.

## 4. F-001 — lifecycle isolation

### Root cause

The previous CLI and TUI paths constructed `PluginCatalogManager()` with its
normal lifecycle behavior. The constructor created the managed root/staging
directory, acquired `.manager.lock`, and called `_recover_transaction()` before
the evidence method. The TUI then reconstructed a normal manager again while
reloading state after the evidence action.

### Fix

`PluginCatalogManager(..., evidence_only=True)` now:

- preserves the requested root path without resolving away symlink ancestors;
- does not create the managed root or `.staging` directory;
- does not validate/create the lifecycle lock or transaction files;
- does not call `_recover_transaction()`;
- does not construct `SignedCatalogService` or its signed-catalog cache;
- still creates only the separate evidence store when evidence persistence is
  actually requested;
- uses conservative catalog display defaults without contacting catalog trust
  state.

The normal constructor default remains `evidence_only=False`, so explicit
install/update/remove lifecycle operations retain their existing startup
recovery path.

The CLI evidence command calls `_get_plugin_manager(evidence_only=True)`. The TUI
Signature operation passes the same mode into `_plugin_operation()` and passes it
again to `load_plugins(evidence_only=True)` in the success/failure reload path.
The evidence-only reload reads catalog/registry metadata without acquiring the
lifecycle lock or running integrity/lifecycle recovery. It marks installed
archive integrity as `not_checked` rather than presenting a stale integrity
claim from raw registry metadata.

### Regression evidence

Tests create an interrupted install journal with target, backup, candidate, and
sentinel files. Both success and storage-failure evidence paths assert that:

- journal bytes are unchanged;
- target/backup/candidate sentinel contents are unchanged;
- `.manager.lock` is not created by evidence-only construction;
- signed catalog cache state is not created;
- only evidence-store files (`publisher_evidence.json` and its dedicated lock)
  are created on a successful fresh-root check;
- no `.staging` or `.transaction.json` is created on a fresh evidence-only root.

Existing default-manager lifecycle recovery tests remain in
`cli/tests/test_plugin_manager.py` and passed in the targeted suite.

## 5. F-002 — cache semantic invariants

### Root cause

The previous record validator checked schema, subject, digest, and enum syntax,
but allowed a caller or cache writer to construct a record with positive
`publisher_auth_status`, matched identity policy, or local verification and
recompute the ordinary SHA-256 consistency digest.

### Fix

`PublisherEvidenceSemanticError` and `_validate_result_semantics()` now enforce
the first adapter's supported status model at serialization, cache parsing, and
store writes:

- `publisher_auth_status=verified` is rejected.
- `identity_policy_status=matched` is rejected.
- `local_signature_status=locally_verified` is rejected.
- `provider_status=verified` requires provider reason `valid` and aggregate
  publisher status `unknown_identity`.
- Unsigned, invalid/unverified/malformed, unsupported/unavailable/provider-error,
  unknown, and no-provider statuses must retain their conservative aggregate
  mapping.
- A provider status cannot appear on a non-observed result.

`PublisherEvidenceResult.to_dict()` validates semantics before serialization.
`PublisherEvidenceStore.write()` therefore rejects forged adapter results before
publication. `PublisherEvidenceStore.get()` treats a malformed or semantically
impossible individual record as absent for that subject rather than projecting
it or denying unrelated subjects. The raw record is not upgraded or treated as
trusted.

The ordinary SHA-256 digest remains a consistency check only. It is not a MAC,
signature, trust root, or proof that the cache writer is authorized.

### Regression evidence

Parameterized tests tamper each forbidden positive field and recompute a valid
record digest. They verify both that:

1. new positive records are rejected on write; and
2. a manually persisted recomputed positive record returns no public evidence
   result for its subject.

The existing provider-verified test still expects
`publisher_auth_status=unknown_identity`, proving that the provider observation
remains available without becoming Aetherion publisher authorization.

## 6. F-003 — terminal and human-output integrity

### Root cause

Provider reasons and exception text were inserted directly into Rich markup
strings. Length limiting did not prevent Rich tags, ANSI controls, carriage
returns, or newline-based visual manipulation.

### Fix

`main._safe_console_value()` now:

- converts values to literal text;
- converts newline, carriage return, tab, C0, DEL, and C1 controls to visible
  escaped forms;
- escapes Rich markup delimiters;
- remains limited to human output paths, leaving JSON serialization semantically
  unchanged.

The helper is applied to provider evidence fields and dynamic values in inspect,
evidence, install, info, and CLI error rendering. Common views now use explicit
labels such as:

```text
GitHub provider result
Provider reason
Aetherion publisher identity
Identity policy
Local cryptographic verification
Evidence observation
Evidence freshness
```

The TUI `_safe_label()` applies the same control-character normalization and
Rich escaping. TUI plugin-operation errors pass through a notification-safe
renderer before entering state. The TUI evidence detail now names provider
result, publisher identity, identity policy, local verification, reason, and
freshness separately.

### Regression evidence

Tests cover:

- `[bold red]TRUSTED PUBLISHER[/bold red]` as a literal, non-styled value;
- ANSI CSI-style input;
- carriage returns and newlines;
- CLI dedicated evidence output;
- CLI inspect/info/install projections;
- TUI labels and operation notifications.

The deterministic render assertion confirms that the ANSI styling sequence
`\x1b[1;31m` is not emitted for the attacker-controlled value.

## 7. F-004 — evidence-store filesystem hardening

### Root cause

The previous store used path-level `stat()` followed by `read_bytes()`, checked
only the immediate parent for symlinks, opened lock files through normal path
semantics, and used predictable path construction for temporary files.

### Fix

The store now provides the following bounded primitives:

- `_assert_safe_path()` walks every existing component from the absolute path to
  its root and rejects symlink/reparse components.
- `_bounded_read_file()` opens the file first, uses `O_NOFOLLOW` where the
  runtime exposes it, verifies a regular file with `fstat()`, enforces the size
  limit on the bytes actually read, and closes the descriptor on all paths.
- Lock files use descriptor-based `os.open()` with create/read-write flags and
  `O_NOFOLLOW` where available, followed by regular-file validation.
- Temporary writes use `tempfile.mkstemp()` in the validated parent directory,
  write/flush/fsync the opened descriptor, revalidate the destination boundary,
  and publish with `os.replace()`.
- Failed replacement and cleanup paths return a storage error rather than
  silently projecting a trusted result.
- Individual malformed/semantically impossible records do not become public
  evidence for unrelated subjects.

### Residual platform limitation

On POSIX, `O_NOFOLLOW` protects the opened leaf paths. Python's portable standard
library does not expose a complete Windows equivalent for preventing every
ancestor reparse-point swap between validation and open/replace. The code
rechecks the path after opening and fails closed on detectable reparse changes,
but it does not claim race immunity against a hostile same-user process that can
swap an ancestor at precisely the remaining Windows race window. The evidence
cache is observational and never a trust root; the residual impact is local
cache integrity/availability rather than catalog approval or execution.

### Regression evidence

Deterministic tests cover:

- nested symlinked ancestors;
- symlinked lock files;
- bytes returned after a deliberately small opened-stream limit;
- failed `os.replace()`;
- cleanup of the exclusive temporary file;
- corrupted records and semantic-positive records.

Timing-only race tests were not introduced.

## 8. F-005 — qualified CLI/TUI projections

The dedicated evidence command already exposed separated dimensions; Phase 4R
extends the same truthfulness to inspect, install, info, and TUI detail views.

The generic `Signature result` wording was removed from the audited common
views. Provider `verified` is now shown as `GitHub provider result: verified`
alongside `Publisher auth: Aetherion publisher identity: unknown_identity`,
identity policy, and local cryptographic verification. This makes it explicit
that the provider observation is not an Aetherion publisher authorization.

Tests cover provider-verified/unknown-identity output and confirm the old
ambiguous label is absent from inspect, info, install, and dedicated evidence
rendering. Existing unsigned, invalid, unavailable, stale, and forged-cache
status paths remain conservative through the manager/store tests.

## 9. Acceptance-criteria status

| Criterion | Result |
|---|---|
| CLI evidence cannot trigger lifecycle recovery or mutate transaction journals/source paths | **Pass, tested** |
| TUI evidence action and post-action reload cannot trigger lifecycle recovery | **Pass, tested through evidence-only manager/reload wiring** |
| Explicit install/update/remove recovery remains enabled | **Pass, existing lifecycle recovery tests pass** |
| Recomputed digests cannot project positive publisher/identity/local status | **Pass, tested** |
| GitHub provider `verified` remains distinct from Aetherion authorization | **Pass, tested** |
| Provider/cache markup and terminal controls cannot manipulate audited human output | **Pass, tested** |
| Filesystem reads enforce bounds on opened-stream bytes | **Pass, tested** |
| Symlink/lock/temp/replacement behavior is hardened | **Pass with documented Windows hostile-ancestor residual limitation** |
| Ordinary CLI/TUI views are qualified | **Pass, tested** |
| Catalog approval and execution remain separate; execution disabled | **Pass, existing and focused tests** |
| Existing source-management/trust/catalog/CLI/TUI regressions pass | **CLI/focused suites pass; repository integration blocked by PostgreSQL** |
| Protected workspace remains unchanged | **Pass, hashes/state rechecked** |

## 10. Verification commands and actual results

Commands were run from the actual application root. Pytest cache and Python
bytecode writing were disabled for the audit/remediation verification runs.

### Focused remediation and trust/lifecycle tests

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -p no:cacheprovider -o addopts= cli/tests/test_publisher_evidence.py cli/tests/test_plugin_manager.py cli/tests/test_cli.py cli/tests/test_tui_plugins.py cli/tests/test_trust_verifier.py cli/tests/test_trust_store.py -q
```

Result: **exit 0**, **183 passed**, pytest-reported **17.31 seconds**; measured
command wall time **21.212 seconds**.

### Complete CLI test directory

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -p no:cacheprovider -o addopts= -q cli/tests
```

Result: **exit 0**, **339 passed**, **25.91 seconds**, with 5 existing Click
`isolated_filesystem` deprecation warnings in replay CLI tests.

### Repository-wide integration suite

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -p no:cacheprovider -o addopts= -q
```

Result: **exit 2**, **15.78 seconds**, during collection. The first failure is
`tests/integration/test_api_key_auth.py` importing `backend/auth_system.py`,
which attempts PostgreSQL at `127.0.0.1:5432`; the connection was refused.
PostgreSQL was not started or repaired, and the repository-wide suite is not
claimed as passed.

### Static checks

Focused changed-core check:

```text
ruff check --no-cache --select E9,F cli/aetherion_cli/publisher_evidence.py cli/aetherion_cli/plugin_manager.py cli/tests/test_publisher_evidence.py
```

Result: **exit 0**, **0.055 seconds**.

The broader audited-surface check:

```text
ruff check --no-cache --select E9,F cli/aetherion_cli/publisher_evidence.py cli/aetherion_cli/plugin_manager.py cli/aetherion_cli/main.py cli/aetherion_cli/tui/state.py cli/aetherion_cli/tui/gateway.py cli/aetherion_cli/tui/screens/plugins.py cli/tests/test_publisher_evidence.py cli/tests/test_plugin_manager.py cli/tests/test_cli.py cli/tests/test_tui_plugins.py
```

Result: **exit 1**, **0.069 seconds**, 32 findings. These are pre-existing
unused imports/variables, duplicate legacy command definitions, f-string
warnings, and the existing undefined `Any` in `main.py`/TUI code. No unrelated
cleanup was performed. The changed adapter/manager/test core is clean under the
focused check.

### Compilation and smoke checks

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -c "from pathlib import Path; files=['cli/aetherion_cli/publisher_evidence.py','cli/aetherion_cli/plugin_manager.py','cli/aetherion_cli/main.py','cli/aetherion_cli/tui/state.py','cli/aetherion_cli/tui/gateway.py','cli/aetherion_cli/tui/screens/plugins.py','cli/tests/test_publisher_evidence.py','cli/tests/test_plugin_manager.py','cli/tests/test_cli.py','cli/tests/test_tui_plugins.py']; [compile(Path(f).read_text(encoding='utf-8'), f, 'exec') for f in files]; print('COMPILE_OK', len(files))"
```

Result: **exit 0**, `COMPILE_OK 10`, **0.459 seconds**.

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m aetherion_cli.main --help
$env:PYTHONDONTWRITEBYTECODE='1'; python -m aetherion_cli.main plugins evidence --help
```

Result: both commands **exit 0**.

## 11. Remaining risks and prerequisites

1. The evidence cache remains user-writable observational state. Its ordinary
   SHA-256 digest proves record consistency only; it does not authenticate the
   writer. Positive publisher authorization still requires a separately
   provisioned identity policy and an authorized verification mechanism.
2. The Windows hostile same-user ancestor-reparse race cannot be claimed fully
   race-free with the portable primitives used here. The code detects normal
   symlink/reparse cases and fails closed, while the residual limitation is
   explicitly retained.
3. Provider `verified=true` remains GitHub-reported evidence only. No signer
   identity, local cryptography, catalog approval, archive safety, compatibility,
   policy approval, or execution permission follows from it.
4. The repository-wide integration suite requires PostgreSQL at
   `127.0.0.1:5432` for a complete environmental result.

No production publisher identity, signing key, trust root, GitHub token, catalog
endpoint, Sigstore identity, or in-toto trust chain was provisioned.

## 12. Protected-state comparison and no-commit statement

Final verification after implementation preserved:

```text
Branch: main
Application HEAD: 5c7bdcd4808a178aa677488f18b948a747f94193
backend/routes/d3_features.py SHA-256:
592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c
.git/index SHA-256:
18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
.git/index.lock: absent
rea branch/HEAD: main / e13da084e7c5967b80632ae5ad8d3d37bcf57469
rea working tree: clean
```

The D3 file, Git index, local `rea/` checkout, existing phase reports, and
pre-existing user-owned changes were preserved. No plugin was executed,
imported for execution, installed, activated, or used to launch subprocesses.
No package script, dependency resolver, MCP server, archive download/extraction,
PostgreSQL service, or live GitHub request was started.

No commit, amend, reset, checkout, cleanup, push, or Git-index mutation was
performed.

The exact final report-file SHA-256 is recorded in the completion response. A
cryptographic hash embedded into the same file would necessarily change that
file's bytes; the completion response is therefore the detached final-file
digest record required for this artifact.

## 13. Recommended next step

Review this Phase 4R implementation and its residual Windows filesystem race
limitation. If accepted, the next phase should be a narrowly scoped platform
validation pass for Windows reparse-point behavior and a PostgreSQL-enabled
repository-wide integration run. Do not provision publisher identity or enable
plugin execution as part of that validation.
