# Aetherion Phase 4 — GitHub Publisher Evidence Security Audit

**Audit date:** 2026-10-17  
**Overall verdict:** **PARTIAL**  
**Audit mode:** Read-only adversarial review; no implementation remediation performed  
**Execution policy:** Option A — metadata/source management only; execution remains disabled

## 1. Executive verdict

The Phase 4 implementation has a strong default-provider boundary: it requires a
canonical GitHub repository and full immutable commit SHA, constructs the API
endpoint internally, rejects redirects and oversized responses, rejects duplicate
JSON names and non-finite constants, separates GitHub provider evidence from
Aetherion publisher identity, and keeps catalog approval, archive integrity,
policy, and execution disabled.

However, the implementation does not fully satisfy its read-only and integrity
guarantees in the actual call graph:

1. **High — the evidence command constructs `PluginCatalogManager`, whose
   constructor unconditionally acquires the lifecycle lock and runs transaction
   recovery.** An evidence-only invocation can therefore delete/restore managed
   source directories and clear a transaction journal when an interrupted
   lifecycle journal exists. This contradicts the metadata-only/non-mutation
   guarantee.
2. **Medium — locally cached evidence can be semantically forged.** The cache
   validator accepts `publisher_auth_status=verified`, matched identity policy,
   and locally verified status when a caller supplies a recomputed ordinary SHA
   consistency digest. A user-writable cache is not an authenticity boundary,
   but the forged status is still projected to the CLI/TUI as publisher evidence.
3. **Medium — provider-controlled reason text reaches Rich CLI markup rendering
   without escaping or control-character rejection.** A deterministic probe
   produced terminal ANSI styling from a provider reason such as
   `[bold red]TRUSTED PUBLISHER[/bold red]`, enabling deceptive presentation.
4. **Medium — evidence-store path and size checks have filesystem TOCTOU gaps.**
   `stat()` and `read_bytes()` are separate operations, and only the immediate
   parent is checked for links. A same-user concurrent filesystem attacker can
   race a replacement after validation, undermine the intended bounded read, or
   redirect a local cache write. This remains a local cache integrity/availability
   issue, not a catalog trust-root compromise.
5. **Medium — common inspect/info/install views use the ambiguous label
   `Signature result` and omit some provider/local/identity dimensions.** The
   dedicated evidence command is more explicit, but these common projections can
   cause a human operator to over-read a provider `verified` value.

These findings do **not** show a path to plugin execution or catalog approval:
the manager repeatedly forces `execution_enabled=False`, catalog fields are
recomputed independently, and user-controlled catalog evidence fields are
removed. The verdict is nevertheless **PARTIAL**, not PASS WITH FINDINGS,
because a central read-only operation can trigger lifecycle recovery and the
cache can surface forged positive publisher status.

## 2. Actual repository and baseline

The application repository was located by Git, not assumed from the prompt:

```text
Repository root: C:/Users/mdham/Downloads/Aetherion-AI-main/end to end aetherion cli
Active branch:  main
Application HEAD: 5c7bdcd4808a178aa677488f18b948a747f94193
```

The audit started with existing workspace changes. They were preserved exactly:

- `backend/routes/d3_features.py` was already modified and is user-owned/protected.
- The workspace contained a large set of pre-existing untracked application,
  report, audit, deployment, and scratch paths, including the Phase 4
  implementation report.
- No staged changes were present.
- `AETHERION_GITHUB_PUBLISHER_EVIDENCE_PHASE4_SECURITY_AUDIT_REPORT.md` did not
  exist at audit start.
- `.git/index.lock` was absent.

Initial protected-state evidence:

```text
backend/routes/d3_features.py SHA-256:
592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c

.git/index SHA-256:
18fc638834262aff249e3bd5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e

local rea checkout: C:/Users/mdham/Downloads/Aetherion-AI-main/rea
local rea branch:  main
local rea HEAD:    e13da084e7c5967b80632ae5ad8d3d37bcf57469
local rea tree:    clean
```

No live GitHub provider request was made during this audit. All behavioral probes
used local values, deterministic injected responses, or the existing test suite.

## 3. Scope and actual data flow

### Primary files inspected

- `cli/aetherion_cli/publisher_evidence.py`
- `cli/aetherion_cli/plugin_manager.py`
- `cli/aetherion_cli/main.py`
- `cli/aetherion_cli/tui/state.py`
- `cli/aetherion_cli/tui/gateway.py`
- `cli/aetherion_cli/tui/screens/plugins.py`
- `cli/tests/test_publisher_evidence.py`
- `cli/tests/test_cli.py`
- `cli/tests/test_tui_plugins.py`

### Supporting files inspected

- `cli/aetherion_cli/trust_verifier.py` for `PublisherAuthStatus`,
  `TrustState`, `trust_fields_for_record()`, and forced disabled execution.
- Catalog distribution/trust-store integration used by
  `PluginCatalogManager.__init__()` and catalog projections.
- Plugin manager source canonicalization, ref validation, catalog loading,
  lifecycle lock, staging, registry, and transaction recovery paths.

### Call graph followed

1. CLI `main.py:158` obtains a fresh `PluginCatalogManager`; the evidence command
   is `main.py:2515-2545`.
2. TUI `gateway.py:1110-1115` invokes the same manager method through
   `_plugin_operation()` at `gateway.py:998-1032`, which serializes the action
   and reloads plugin state after both success and failure.
3. `PluginCatalogManager.__init__()` at `plugin_manager.py:322-364` creates the
   manager root/staging path, opens the manager lock, and calls
   `_recover_transaction()`.
4. `check_publisher_evidence()` at `plugin_manager.py:1493-1547` resolves a
   selector/ref, creates an exact subject, invokes the adapter, writes the
   separate evidence store, and joins the stored result into a response.
5. Ref resolution at `plugin_manager.py:612-669` uses the bounded metadata fetch
   path for a mutable branch/tag, while a full SHA avoids that metadata lookup.
6. `GithubCommitSignatureAdapter.check()` at
   `publisher_evidence.py:362-481` performs provider response validation and
   status mapping.
7. `PublisherEvidenceStore` at `publisher_evidence.py:650-711` validates,
   locks, reads, writes, and freshness-projects records.
8. Manager read projections at `plugin_manager.py:730-755` join evidence by
   the current canonical source and resolved commit.

This constructor and lifecycle path is material: it runs before the nominally
read-only evidence method and is not bypassed by the CLI or TUI.

## 4. Confirmed findings

### F-001 — Evidence inspection can trigger lifecycle recovery

**Severity:** High  
**Affected trust boundary:** Read-only evidence operation versus source-management lifecycle  
**Status:** Confirmed vulnerability / guarantee contradiction

**Code evidence:**

- `cli/aetherion_cli/main.py:158` creates the manager for CLI operations.
- `cli/aetherion_cli/plugin_manager.py:331-364` creates the manager root and
  staging directory, opens `_operation_lock()`, and unconditionally calls
  `_recover_transaction()` during construction.
- `plugin_manager.py:431-503` reads a transaction journal and can call
  `shutil.rmtree()` on a target/candidate/backup path, `os.replace()` a backup
  into the managed source path, and `_clear_journal()`.
- `plugin_manager.py:1493-1547` does not establish a separate read-only manager
  mode before this constructor path runs.

**Attack/precondition:** A managed plugin root contains an interrupted lifecycle
journal, whether from a real prior crash or from a local process that can write
the manager control directory. A user then invokes `plugins evidence` or the TUI
Signature action.

**Proof:** The evidence method is not reached until the constructor's
`with self._operation_lock(): self._recover_transaction()` completes. Recovery
is not conditional on the requested operation. The recovery code has destructive
filesystem branches for install/remove phases.

**Impact/exploitability:** A command advertised as metadata-only can mutate or
delete managed source data and clear transaction state. A malformed or hostile
journal can instead cause evidence inspection to fail. This is not a plugin
execution path, but it violates the read-only operation boundary and can create
data loss or an unexpected source-management transition. Exploitability is local
and requires a pre-existing journal/control-path condition.

**Existing-test coverage:** The raw-ref test in
`cli/tests/test_publisher_evidence.py:314-342` proves that the normal evidence
method does not call the injected archive fetcher and does not create
`registry.json`/`catalog.json`. It does not create a transaction journal, assert
that `_recover_transaction()` is not called, or check `.staging`, manager-lock,
managed-source, and journal state.

**Minimal corrective action:** Use a read-only manager construction path for
evidence that does not recover lifecycle journals, create staging state, or
perform source-management recovery. Keep recovery in explicitly mutating
lifecycle operations. Add a journal-sentinel regression test asserting that an
evidence request cannot remove, restore, or clear any lifecycle state.

### F-002 — Cache records can carry forged positive publisher status

**Severity:** Medium  
**Affected trust boundary:** Local evidence-cache integrity versus publisher-status presentation  

**Code evidence:**

- `publisher_evidence.py:569-647` validates record shape, enums, subject, and a
  plain SHA-256 `evidence_digest`, but does not enforce the semantic invariant
  that this first adapter must never persist `PublisherAuthStatus.VERIFIED`,
  `IdentityPolicyStatus.MATCHED`, or `LocalSignatureStatus.VERIFIED`.
- `publisher_evidence.py:687-699` accepts any `PublisherEvidenceResult` with a
  subject-matching, locally recomputed consistency digest.
- `plugin_manager.py:730-755` projects the stored `publisher_auth_status` and
  local/identity status directly into manager output.

**Reproduction:** An in-memory probe constructed a valid-shaped
`PublisherEvidenceResult` with `provider_status=verified`,
`publisher_auth_status=verified`, `identity_policy_status=matched`, and
`local_signature_status=locally_verified`. Passing `result.to_dict()` through
the actual private `_result_from_dict()` returned:

```text
accepted_publisher_auth_status: verified
accepted_identity_policy_status: matched
accepted_local_signature_status: locally_verified
```

No external signature or MAC is involved. A local actor able to rewrite the
cache can recompute the ordinary consistency digest.

**Attack/precondition:** Write access to the local evidence cache, or an
in-process caller able to provide the manager's injectable evidence adapter.

**Impact/exploitability:** The CLI/TUI can present a forged positive publisher,
identity-policy, or local-verification state. The current code still forces
catalog policy and execution disabled, so this does not directly approve or
execute a plugin. It does undermine the integrity of a security signal that an
operator could use for a publisher decision.

**Existing-test coverage:** Existing tests validate legitimate provider output,
digest mismatch/corruption, subject mismatch, and bounds. They do not mutate
the status enums to positive values and recompute the digest.

**Minimal corrective action:** Treat the local cache as advisory and reject or
quarantine positive identity/local-verification states unless produced by an
explicit, separately authorized verifier with its own policy. Do not make a
plain consistency digest an authenticity mechanism. Add a semantic-tampering
test for every positive status field.

### F-003 — Provider reason text is rendered as Rich markup/control data

**Severity:** Medium  
**Affected trust boundary:** Untrusted provider/cache text to terminal presentation  

**Code evidence:**

- `publisher_evidence.py:204-209` bounds provider text length but does not reject
  control characters or Rich markup delimiters.
- `main.py:2535-2537` passes provider status, provider reason, and reason code
  through f-strings directly to `console.print()`.
- `main.py:115-125` similarly renders exception text through Rich markup.
- The TUI `_safe_label()` at `tui/screens/plugins.py:23-26` escapes Rich markup,
  but it does not explicitly remove terminal control characters.

**Reproduction:** The deterministic Rich probe:

```python
from rich.console import Console
value = "[bold red]TRUSTED PUBLISHER[/bold red]"
console = Console(record=True, force_terminal=True, color_system="standard")
console.print(f"Provider reason: {value}")
print(repr(console.export_text(styles=True)))
```

produced ANSI styling containing `\x1b[1;31mTRUSTED PUBLISHER\x1b[0m`.

**Attack/precondition:** A provider response or locally forged cache contains
Rich markup/control text in `verification.reason`, `reason_code`, or an error
field. Unknown provider reasons are intentionally accepted as bounded text.

**Impact/exploitability:** A malicious or compromised provider response can
style text to make an untrusted result look authoritative, or malformed text can
disrupt rendering. JSON mode remains structurally serialized, but table/error
mode is not a neutral text sink. This is a presentation-integrity issue, not
shell execution.

**Existing-test coverage:** No test supplies markup, ANSI, newline, or terminal
control characters in provider reason/cache error fields and asserts safe table
rendering.

**Minimal corrective action:** Render untrusted values as escaped Rich `Text` or
use `markup=False`; reject or neutralize terminal controls before rendering.
Add table, error, TUI, and JSON-mode injection regression cases.

### F-004 — Evidence-store bounds and link checks have TOCTOU gaps

**Severity:** Medium  
**Affected trust boundary:** Local evidence-store filesystem integrity and bounded resource use  

**Code evidence:**

- `publisher_evidence.py:653-660` checks only the requested root and its
  immediate parent. An absent nested parent can still be created through a
  symlinked ancestor.
- `publisher_evidence.py:662-685` checks `self.path.stat().st_size` and then
  separately calls `self.path.read_bytes()`. The file can be replaced between
  those operations, so the bound is not enforced on the bytes actually read.
- `publisher_evidence.py:516-531` checks the destination/parent and then opens a
  random temporary path and calls `os.replace()`; the check and publication are
  not performed through a directory handle or no-follow operation.
- `publisher_evidence.py:534-566` has the same check-then-open pattern for the
  lock file.

**Attack/precondition:** A same-user or same-principal process can modify the
cache directory concurrently with an evidence read/write. The normal
single-process and cooperative-lock case is not affected.

**Impact/exploitability:** A race can cause an oversized replacement to be read
despite the intended 512 KiB limit, producing resource exhaustion, or redirect
local cache reads/writes through a swapped path. The cache is not used as a
trust root, so this does not authenticate a plugin; it weakens the documented
filesystem-integrity and boundedness guarantees.

**Existing-test coverage:** Tests cover an already-present oversized file,
duplicate JSON, and a directly symlinked root/file. They do not cover a symlinked
ancestor, lock-file swap, temp-file swap, concurrent replacement, failed
`os.replace()`, or read-size TOCTOU.

**Minimal corrective action:** Open and validate through a trusted directory
handle with platform-appropriate no-follow semantics, enforce the byte cap on
the opened stream itself, and make temporary publication/cleanup use the same
secure directory boundary. Add race and failure-injection tests.

### F-005 — Common CLI views under-qualify provider result semantics

**Severity:** Medium  
**Affected trust boundary:** Human interpretation of provider evidence in ordinary status views  

**Code evidence:**

- The dedicated evidence command at `main.py:2528-2545` shows observation,
  provider reason, publisher auth, identity policy, local verification,
  catalog policy, and disabled execution separately.
- The ordinary inspect view at `main.py:2501-2507`, install output at
  `main.py:2567-2569`, and info view at `main.py:2734-2741` use the generic
  label `Signature result` and omit some combination of observation status,
  reason code, provider reason, identity policy, and local verification.

**Attack/precondition:** An operator relies on `plugins inspect`, `plugins
install`, or `plugins info` rather than the dedicated evidence command. A
provider result is `verified` while publisher identity remains
`unknown_identity`.

**Impact/exploitability:** The code does not literally label the result
“Aetherion verified,” and the dedicated view is explicit, but a generic
`Signature result: verified` can be over-read as a trusted publisher decision.

**Existing-test coverage:** `test_cli.py:411-442` covers the dedicated evidence
command only. There are no assertions that inspect/info/install qualify all
provider/local/identity fields or avoid ambiguous positive wording.

**Minimal corrective action:** Use explicit labels such as `GitHub provider
result`, `Aetherion publisher identity`, `Local verification`, and `Identity
policy` in every projection, or intentionally omit evidence from non-dedicated
views. Add output assertions for verified, unsigned, unavailable, stale, and
unknown states.

## 5. Security-boundary results without material findings

### Exact subject binding and canonicalization

The primary boundary is strong and fail-closed in the default path:

- `canonicalize_github_url()` at `plugin_manager.py:223-261` requires HTTPS,
  GitHub host, no credentials, no port, no query/fragment, exactly two path
  components, and a constrained owner/repository grammar.
- `.git` is normalized, and `PublisherEvidenceSubject.create()` plus its
  `__post_init__()` require the canonical result and a lowercase full 40-byte
  hexadecimal SHA.
- `validate_ref()` rejects traversal-like ref forms and `_resolve_commit()` uses
  an internally constructed GitHub API URL.
- `GithubCommitSignatureAdapter.endpoint()` derives the API URL from the
  validated subject rather than accepting a caller-supplied endpoint.
- `_commit_url_matches()` rejects non-HTTPS, non-GitHub, credential-bearing,
  non-default-port, query, and fragment forms, and matches the repository path
  and full SHA.
- The top-level provider `sha` must equal the subject SHA before the response is
  accepted.

The remaining concern is normalization completeness rather than a demonstrated
cross-host bypass: owner/repository case and some alternate textual path forms
can produce separate observational cache keys even when GitHub treats them as
the same repository. That is a cache duplication/hardening concern, not an
observed cross-binding or authorization bypass.

### Provider response and status mapping

The default adapter correctly keeps these dimensions separate:

```text
observation_status
provider_status / provider_reason
publisher_auth_status
identity_policy_status
local_signature_status
catalog_auth_status / policy_status
execution_enabled
```

Default behavior verified in source and targeted tests:

- `valid` plus `verified=true` and non-empty signature/payload produces
  `provider_status=verified` and `publisher_auth_status=unknown_identity`.
- No local GPG/SSH/S/MIME verification is attempted.
- No identity policy is configured or inferred from author email, username,
  display name, or GitHub account data.
- Unsigned and absent signature metadata are not upgraded.
- Invalid, unknown-key, and malformed provider results remain non-authorizing.
- Unsupported, unavailable, provider-error, and unknown results map to
  `publisher_auth_status=not_checked`.
- Contradictory `verified`/`reason` fields fail closed as invalid response.
- HTTP 401/403, 404, redirects, 429, 5xx, unexpected status, content-type
  failure, transport error, oversized response, duplicate JSON, and malformed
  response paths do not produce a positive publisher result.

The positive-status cache finding in F-002 is the boundary exception: the
default adapter is conservative, but the end-to-end cache projection does not
enforce those same semantic invariants.

### Catalog, policy, archive, compatibility, and execution separation

This separation is **verified for the normal manager path**:

- `trust_fields_for_record()` forces `execution_enabled=False` at
  `trust_verifier.py:925-958`.
- Catalog loading strips user-controlled catalog trust and publisher-evidence
  fields at `plugin_manager.py:873-905`.
- `check_publisher_evidence()` writes only the evidence store from its method
  body and does not call archive extraction, install, update, dependency
  resolution, package scripts, or activation.
- The raw-ref test injects an archive-fetch sentinel and confirms it is not
  called; it also confirms catalog/registry files are not created by the
  method under a fresh manager.
- Evidence joins do not overwrite catalog authentication, catalog policy,
  archive integrity, or execution fields.

F-001 is the important qualification: manager construction and transaction
recovery occur before the evidence method and are outside that method-level
test boundary.

### HTTP transport and credential hygiene

The source shows the intended controls:

- `httpx.Client` uses a fixed internally constructed HTTPS GitHub API endpoint,
  `follow_redirects=False`, a validated timeout capped by
  `MAX_REQUEST_TIMEOUT=60.0`, and streamed 64 KiB chunks.
- Response accumulation stops over 512 KiB.
- `application/json` is required for successful provider responses.
- JSON duplicate names and non-finite constants are rejected.
- The optional token is only placed in the `Authorization` request header.
- `_http_fetch()` catches transport exceptions and does not include the token in
  the returned evidence or error text.

This is **partially verified**, not fully proven: existing tests inject a
`response_fetcher` and do not exercise the real `httpx.Client` seam for timeout
construction, stream overflow, redirect handling, or exception/logging paths.
Static inspection found no path that serializes or prints the token. No live
provider request was made.

### Evidence store and freshness

The store has meaningful protections:

- Exact top-level store/record schemas, duplicate-key rejection, enum/timestamp/
  digest/identity validation, subject-key matching, size and record limits, and
  post-serialization write checks.
- Atomic temp-file replacement and a cooperative lock file.
- Separate evidence file from catalog, registry, signed-catalog, trust-root,
  keyset, and transaction files.
- Freshness calculation labels an observation `cached` only when age is between
  zero and 24 hours inclusive; future timestamps become `stale`, and malformed
  timestamps are rejected.

The cache remains a consistency mechanism, not authenticity. The digest is a
plain SHA-256 over canonical record data and is recomputable by a local writer.
F-002 and F-004 materially qualify the cache guarantees.

## 6. Phase 4 claim-verification matrix

| Phase 4 claim | Independent result | Evidence |
|---|---|---|
| Exact canonical repository + full immutable SHA subject | **VERIFIED IN CODE AND/OR REPRODUCIBLE TEST** | Canonicalizer, subject post-init, endpoint construction, response SHA/URL checks, targeted subject tests |
| API endpoint is internally constructed and redirects are rejected | **PARTIALLY VERIFIED** | Static code is conservative; real `httpx` transport seam lacks targeted tests |
| Finite timeout and bounded streamed body | **PARTIALLY VERIFIED** | Constructor cap and loop exist; injected-response tests do not exercise actual stream/client behavior |
| Duplicate JSON and non-finite constants fail closed | **VERIFIED IN CODE AND/OR REPRODUCIBLE TEST** | `_strict_json()` and duplicate/contradiction test |
| GitHub `verified` is not Aetherion publisher authorization | **PARTIALLY VERIFIED** | Default adapter emits `unknown_identity`; F-002 permits forged positive cache status |
| No local cryptographic verification or identity policy is claimed | **VERIFIED IN CODE AND/OR REPRODUCIBLE TEST** | Explicit enum defaults, no crypto path, targeted assertions |
| Catalog approval, archive integrity, policy, and execution remain separate | **VERIFIED IN CODE AND/OR REPRODUCIBLE TEST** | Trust projection forces disabled execution; user evidence fields stripped; manager tests |
| Evidence is subject-bound in storage and read joins | **PARTIALLY VERIFIED** | Exact subject key and current-record joins are strong; local cache tampering and filesystem TOCTOU remain |
| Cache is bounded, atomic, locked, and corruption-failing | **PARTIALLY VERIFIED** | Bounds and normal atomic/lock path exist; race, ancestor symlink, and failed-replace paths are not proven |
| Freshness is 24 hours and stale evidence is not fresh | **PARTIALLY VERIFIED** | Code handles negative/exact/old age conservatively; future/exact/clock-skew regression coverage is limited |
| Raw/ref evidence collection is metadata-only and non-mutating | **CONTRADICTED BY SOURCE OR TEST RESULTS** | Method avoids archive fetches, but manager construction invokes lifecycle recovery and creates manager state (F-001) |
| CLI/TUI statuses are distinct and safe to render | **PARTIALLY VERIFIED** | Dedicated/TUI fields are separated; common CLI labels are ambiguous and provider text is rendered as Rich markup (F-003/F-005) |
| Existing CLI test directory is healthy | **VERIFIED IN CODE AND/OR REPRODUCIBLE TEST** | `326 passed, 5 warnings` in current audit run |
| Repository-wide integration suite is healthy | **NOT INDEPENDENTLY VERIFIED** | Collection stopped at PostgreSQL connection refusal, exit 2 |
| Implementation report is independently sufficient evidence | **CONTRADICTED BY SOURCE OR TEST RESULTS** | F-001 through F-005 were not represented in the report's completion claim |

## 7. Test quality and coverage gaps

No tests were added or changed during this audit. The existing suite is useful
for normal status mapping and happy-path subject/store behavior, but it does not
cover the following security cases:

1. **Lifecycle isolation:** create a transaction journal and managed
   source/backup sentinels, invoke the evidence CLI/manager, and assert no
   recovery, deletion, restoration, journal clearing, staging creation, or
   manager-lock side effect.
2. **Cache semantic tampering:** alter every positive status enum and recompute
   the ordinary digest; assert the record is rejected or downgraded and cannot
   project `publisher_auth_status=verified`.
3. **Filesystem races:** replace the evidence path between size check and read;
   swap a symlinked ancestor, lock file, or temp path; inject `os.replace()` and
   cleanup failures; run cooperating concurrent writers.
4. **Real HTTP seam:** mock `httpx.Client` and streamed responses to assert
   timeout value, no redirects, fixed host, header-only token use, stream-size
   enforcement, all status classes, and absence of token text in errors/logs.
5. **Response schema matrix:** test extra top-level/commit/identity fields,
   wrong nested types, invalid timestamps, empty/missing verification fields,
   future provider reasons, and provider-supplied control characters.
6. **Pin/alias transitions:** write evidence for commit A, reassign or repin an
   alias to commit B, and assert A is never joined to B; test case and alternate
   canonical URL forms explicitly.
7. **Presentation safety:** render provider reason/control text through CLI table,
   error, TUI, and JSON paths; assert no ANSI/markup injection and no ambiguous
   “verified” label without its provider/identity qualifier.
8. **Storage failure behavior:** make lock acquisition, read, temp creation,
   replacement, and cleanup fail and assert fail-closed status without a
   trusted fallback.
9. **TUI operation ownership:** assert an evidence action remains serialized,
   does not invoke install/update/remove, and does not turn a reload after
   failure into a lifecycle recovery.

## 8. Actual audit commands and results

All commands below were run read-only from the application repository. Python
bytecode writing and pytest cache provider were disabled where relevant.

### Targeted existing tests

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -p no:cacheprovider -o addopts= cli/tests/test_publisher_evidence.py cli/tests/test_plugin_manager.py cli/tests/test_cli.py cli/tests/test_tui_plugins.py cli/tests/test_trust_verifier.py cli/tests/test_trust_store.py -q
```

Result: **exit 0**, **170 passed**.

### Full CLI test directory

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -p no:cacheprovider -o addopts= -q cli/tests
```

Result: **exit 0**, **326 passed**, **5 existing Click deprecation warnings** in
replay CLI tests.

### Repository-wide suite

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -p no:cacheprovider -o addopts= -q
```

Result: **exit 2** during collection. `tests/integration/test_api_key_auth.py`
imports `backend/auth_system.py`, which attempts PostgreSQL at
`127.0.0.1:5432`; the connection was refused. PostgreSQL was not started or
modified, and the integration suite is not claimed as passed.

### Static/syntax checks

```text
ruff check --no-cache --select E9,F cli/aetherion_cli/publisher_evidence.py cli/aetherion_cli/plugin_manager.py cli/aetherion_cli/main.py cli/aetherion_cli/tui/state.py cli/aetherion_cli/tui/gateway.py cli/aetherion_cli/tui/screens/plugins.py cli/tests/test_publisher_evidence.py cli/tests/test_cli.py cli/tests/test_tui_plugins.py
```

Result: **exit 1**, 32 findings across pre-existing CLI/TUI unused imports,
duplicate command definitions, f-string issues, and one undefined `Any` in
`main.py`. No remediation was made. A narrower check of the new adapter,
manager, and evidence tests passed with **exit 0**.

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -c "from pathlib import Path; files=['cli/aetherion_cli/publisher_evidence.py','cli/aetherion_cli/plugin_manager.py','cli/aetherion_cli/main.py','cli/aetherion_cli/tui/state.py','cli/aetherion_cli/tui/gateway.py','cli/aetherion_cli/tui/screens/plugins.py','cli/tests/test_publisher_evidence.py','cli/tests/test_cli.py','cli/tests/test_tui_plugins.py']; [compile(Path(f).read_text(encoding='utf-8'), f, 'exec') for f in files]; print('COMPILE_OK', len(files));"
```

Result: **exit 0**, `COMPILE_OK 9`.

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m aetherion_cli.main --help
$env:PYTHONDONTWRITEBYTECODE='1'; python -m aetherion_cli.main plugins evidence --help
```

Result: both smoke commands **exit 0**.

### Deterministic adversarial probes

- A local in-memory status-forgery probe passed a recomputed record through the
  actual `_result_from_dict()` validator and observed positive publisher,
  identity-policy, and local-verification statuses accepted.
- A local Rich rendering probe produced ANSI styling from provider text
  `[bold red]TRUSTED PUBLISHER[/bold red]`.
- No live provider API, archive fetch, plugin installation, package script,
  subprocess-based plugin workflow, MCP server, or PostgreSQL service was
  started.

## 9. Confirmed limitations and production prerequisites

1. A GitHub provider `verified=true` result is not an Aetherion publisher
   authorization. The current code has no independently provisioned signer
   identity, keyring, trust policy, or local cryptographic verifier.
2. The local evidence digest is a consistency digest, not a signature, MAC, or
   trust-root assertion. A user-writable cache cannot be treated as an
   authenticity source.
3. Any future publisher authorization requires an independently approved
   identity policy and key-distribution process. It must not infer authorization
   from GitHub account names, commit emails, display names, or provider
   `verified=true` alone.
4. The repository-wide integration suite needs PostgreSQL available for a
   meaningful result. This audit did not start or repair PostgreSQL.
5. The audit did not provision a GitHub token, publisher key, catalog endpoint,
   Sigstore identity, in-toto trust chain, or production trust root.
6. Option A remains intact as a policy boundary: evidence is optional metadata,
   source lifecycle is separate, and executable plugin activation remains
   disabled.

## 10. Initial-versus-final protected-state comparison

The final comparison was performed after writing this report. The only intended
new workspace artifact is the named audit report itself; existing user-owned
and phase reports remain untouched.

| Protected item | Initial audit state | Final audit state |
|---|---|---|
| Application branch/HEAD | `main` / `5c7bdcd4808a178aa677488f18b948a747f94193` | unchanged |
| `backend/routes/d3_features.py` | SHA-256 `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | unchanged |
| `.git/index` | SHA-256 `18fc638834262aff249e3bd5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` | unchanged |
| `.git/index.lock` | absent | absent |
| local `rea/` | `main`, HEAD `e13da084e7c5967b80632ae5ad8d3d37bcf57469`, clean | unchanged |
| Existing staged/unstaged/untracked work | pre-existing noisy workspace; D3 file modified | preserved; audit report is the only new intended file |

## 11. Final no-implementation statement

This audit made **no implementation, configuration, dependency, migration,
catalog, registry, evidence-cache, trust-state, deployment, or protected-file
changes**. No tests, fixtures, scripts, or source modules were modified. No
commit, push, reset, checkout, cleanup, index mutation, live provider request,
plugin workflow, subprocess, or database service was started.

The required audit deliverable is:

```text
AETHERION_GITHUB_PUBLISHER_EVIDENCE_PHASE4_SECURITY_AUDIT_REPORT.md
```

The final file SHA-256 is calculated after the final write and is repeated in
the completion response. Because a file cannot contain a self-referential hash
without changing its own bytes, the completion response is the authoritative
detached hash record for this exact final file.

## 12. Single recommended next phase

**Remediation Phase 4R — isolate read-only evidence from lifecycle recovery,
then harden cache authenticity semantics and terminal rendering.**

Start with F-001 because it violates the operation boundary. In the same
remediation phase, add the semantic cache-status invariant from F-002, secure
the store path/read boundary from F-004, and neutralize provider text rendering
from F-003/F-005. Re-run the targeted and full CLI suites, then repeat the
repository-wide suite only when PostgreSQL is available.

## 13. Detached report-body digest

```text
SHA-256 of the report body before this digest metadata section:
808892c442c345a6214350f89237e3d0fd42af329fa130996587a5a045626c87
```

The exact final-file SHA-256, which necessarily changes when a hash value is
embedded into the file, is recorded out-of-band in the audit completion
response.
