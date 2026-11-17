# Aetherion Phase 3 Independent Adversarial Security Audit & Key-Lifecycle Hardening

**Date:** 2026-10-17  
**Assigned model:** GPT-6 Astra / `omnirush/gpt-6-astra`  
**Verdict:** **PASS WITH FINDINGS**  
**Scope:** Phase 3 root/delegated-key lifecycle, recovery, revocation, rotation,
expiry, root replacement, persistence, CLI/TUI presentation, and regression
coverage  
**Execution policy:** Option A / control-plane only / `execution_enabled=False`

## 1. Executive verdict

The independent adversarial audit confirmed several security-relevant defects
in the previously completed Phase 3 implementation. The most serious were:

1. Explicit recovery could resurrect a catalog key revoked by a newer accepted
   keyset.
2. A root replacement was authorized only by a public old-root fingerprint and
   a caller-controlled `confirmed=True` boolean; a newly generated root could
   self-authorize its own replacement.
3. Lifecycle state deletion reset the sequence/root baseline and enabled
   implicit first-seen acceptance.
4. Stale provider instances could overwrite newer lifecycle state.
5. Root/keyset/state/history publication was only individually atomic, not a
   recoverable multi-file transaction.
6. Candidate files were validated from one read and published from a second
   read, creating a time-of-check/time-of-use window.
7. Normal rotation was blocked when the current keyset had expired.
8. Persisted plugin JSON accepted duplicate fields and non-finite JSON values.
9. Signed-catalog cache-root symlink validation occurred after path
   resolution.

All confirmed high-priority lifecycle defects were remediated in this pass.
The implementation now:

- keeps historical revoked key IDs and public-key fingerprints monotonic across
  rotation and supported recovery;
- rejects older backups that would re-admit a revoked key;
- rejects later root-signed keysets that silently un-revoke or rebind a key ID;
- requires an old-root-signed transition authorization for root replacement;
- requires an independently supplied expected new-root fingerprint in the CLI;
- uses one read of candidate bytes for validation and publication;
- uses a lifecycle lock plus durable per-file transaction journal and rollback
  snapshots for keyset/root/state/history publication;
- fails closed when lifecycle state is missing after prior lifecycle material;
- reloads the persisted state at activation time, preventing stale-provider
  sequence overwrites;
- permits a valid higher-sequence rotation after the old keyset expires;
- rejects ambiguous/non-finite plugin registry, catalog, journal, manifest, and
  integrity JSON;
- rejects symlinked cache roots before resolution;
- carries lifecycle trust fields in the same catalog snapshot used for manager
  and UI records;
- preserves `execution_enabled=False` throughout.

The remaining findings are explicit limitations rather than hidden claims:

- Local JSON state/history is operational evidence, not tamper-proof or
  remotely authenticated audit evidence.
- A privileged local attacker who can rewrite the entire root, keyset, state,
  transaction, history, and application boundary can still restore a complete
  older filesystem generation. The implementation detects partial/missing
  state and protects ordinary transitions, but it does not claim protection
  against total trusted-filesystem rollback.
- The lifecycle transaction provides crash recovery and fail-closed
  consistency, not a filesystem-wide multi-file atomic primitive.
- Production root, signer custody, authenticated publication, and signed REA
  catalog material remain unprovisioned.

No production key, private signing material, production catalog, external
revocation service, publisher adapter, or plugin execution path was created.

## 2. Audit method and evidence

The audit traced the actual current code rather than relying on the previous
Phase 3 report. It covered:

- `cli/aetherion_cli/trust_store.py`
- `cli/aetherion_cli/trust_verifier.py`
- `cli/aetherion_cli/catalog_distribution.py`
- `cli/aetherion_cli/plugin_manager.py`
- `cli/aetherion_cli/config/settings.py`
- `cli/aetherion_cli/config/__init__.py`
- `cli/aetherion_cli/main.py`
- `cli/aetherion_cli/tui/state.py`
- `cli/aetherion_cli/tui/gateway.py`
- `cli/aetherion_cli/tui/screens/plugins.py`
- Phase 1/2/3 CLI and TUI tests.

The audit used disposable temporary directories and ephemeral Ed25519 test
keys. The adversarial cases included:

- active key → revoked keyset → old-backup recovery;
- revoked key → later root-signed attempted reintroduction;
- self-generated new root replacement;
- wrong old and new root fingerprints;
- mismatched transition signer and candidate keyset;
- expired active keyset followed by valid rotation;
- deleted lifecycle state after accepted material;
- two provider instances with a stale in-memory keyset;
- injected publication failure during root replacement;
- cache-root symlink redirection;
- duplicate and `NaN` JSON in persisted plugin state.

The two independent read-only audit passes agreed on the core findings:
revoked-key resurrection, unauthenticated root replacement, resettable state,
stale-provider races, and non-atomic root lifecycle publication. Those findings
were then reproduced in local tests before remediation.

## 3. Confirmed findings and remediation

### H-01 — Revoked catalog key could be resurrected through recovery

**Original evidence:** `recover_candidate()` parsed a backup with
`current=None`, compared only the candidate sequence, and then activated its
raw bytes. `verify_catalog()` checked only the revocation flag in the currently
exposed keyset. A sequence-1 active key followed by a sequence-2 revoked key
could therefore be recovered from the sequence-1 backup as active.

**Fix:**

- Lifecycle state now persists `revoked_key_ids`,
  `revoked_key_fingerprints`, and `key_id_fingerprints`.
- Candidate parsing receives those historical deny sets on ordinary rotation,
  recovery, and root replacement.
- An unrevoked candidate that contains a historically revoked key ID or public
  key fingerprint is rejected with `key_revocation_rollback`.
- A key ID cannot silently bind to different public-key material; that is
  rejected with `key_rotation_rejected`.
- Recovery keeps the durable highest sequence watermark. It may restore a
  non-revoked older keyset only through the explicit recovery operation, but it
  cannot restore a key whose authenticated history says revoked.
- The transition history records the exceptional recovery operation and the
  active state remains inspectable after restart.

**Regression:**

`test_recovery_and_future_rotation_cannot_resurrect_revoked_key` covers:

1. sequence 1 active key;
2. sequence 2 revocation;
3. old-backup recovery attempt;
4. sequence 3 reintroduction attempt;
5. restart-visible revoked authority.

Both attempts fail with `key_revocation_rollback`; the provider remains on the
revoking keyset.

### H-02 — Root replacement lacked independent new-root authorization

**Original evidence:** `replace_root()` accepted a candidate root and a
candidate keyset signed by that root after checking only the old-root
fingerprint and `confirmed=True`. A new root could self-authorize its own
replacement.

**Fix:**

- Root replacement now requires an exact old-root-signed transition envelope.
- The active root key must explicitly carry `root-replacement` scope.
- The signed transition payload binds:
  - old root fingerprint;
  - expected new root fingerprint;
  - candidate keyset fingerprint;
  - next root generation;
  - issue and expiry timestamps.
- The CLI requires all of:
  - `--old-root-fingerprint`;
  - `--new-root-fingerprint`;
  - `--transition <old-root-signed-transition>`;
  - `--confirm`.
- The new-root fingerprint must be obtained and authenticated independently by
  the operator. The CLI does not invent or fetch that external evidence.
- The candidate root and keyset bytes are read once, verified, and the exact
  verified bytes are published.
- A candidate root signed transition using the new root itself is rejected as
  `root_transition_unauthorized`.

**Regression:**

`test_root_replacement_requires_old_root_signed_transition` rejects a
self-authorized transition. The root replacement lifecycle test also rejects a
wrong expected new-root fingerprint and missing confirmation before accepting
the valid old-root-authorized pair.

### H-03 — Lifecycle publication was not crash-consistent as a multi-file unit

**Original evidence:** root replacement independently replaced keyset, root,
state, and history. Failure between those writes could leave mixed material or
report failure after the active keyset had already changed.

**Fix:**

- Added a lifecycle lock at the state-file boundary.
- Added a durable transaction marker with snapshots of the previous root,
  keyset, state, and history files.
- New data is written as a recoverable generation in this order:
  1. transaction prepared;
  2. keyset publication;
  3. root publication when applicable;
  4. state publication;
  5. history publication;
  6. committed marker.
- Each file write uses a unique temporary file, flush/fsync, atomic replace,
  and best-effort directory fsync.
- On startup or before an operation, an incomplete transaction restores the
  complete previous snapshot. A committed marker is treated as authoritative;
  cleanup failure does not report a false failed transition.
- History is prepared as a complete bounded file and participates in the same
  rollback protocol. A history failure cannot leave a successful active
  transition without its corresponding operational record.
- The provider fails closed if transaction metadata is malformed or recovery
  itself cannot complete.

**Guarantee:** This is a recoverable transaction protocol across individually
atomic files. It is not claimed to be a hardware/filesystem-wide atomic
multi-file commit.

**Regression:**

`test_failed_root_publication_rolls_back_complete_previous_pair` injects an
error while publishing the new root and verifies after a fresh provider load
that the old root, old keyset, old state, and old trust fingerprint remain
authoritative.

### M-01 — Candidate TOCTOU could publish bytes different from validated bytes

**Original evidence:** candidate validation read a path, then activation read
the same path again. Root replacement repeated this for both candidate files.

**Fix:** `_candidate()` now returns the exact bytes it parsed and
`_activate_locked()` publishes those bytes. Root replacement reads the root,
keyset, and transition candidates once before validation and publishes the same
byte strings. Candidate paths and their immediate parents are checked for
symlinks/reparse points.

### M-02 — Stale provider instances could bypass sequence/parent checks

**Original evidence:** `apply_candidate()` reused a cached `_keyset` and did
not compare-and-swap against freshly persisted state. Two provider instances
could allow an older candidate to overwrite newer state.

**Fix:** Every mutating lifecycle operation acquires the lifecycle lock and
re-reads persisted state. Sequence, parent, key identity, historical
revocation, and root fingerprint checks use that persisted baseline. The stale
provider regression proves that sequence 1 cannot overwrite sequence 2.

### M-03 — Expired active keyset blocked valid normal rotation

**Original evidence:** `apply_candidate()` called ordinary active-keyset load,
which rejected the expired current keyset before verifying a valid higher
sequence candidate.

**Fix:** Activation now uses the persisted accepted state as its rollback
baseline and verifies the candidate directly under the configured root. It does
not require the old keyset to remain within its validity interval in order to
replace it. A malformed or inconsistent old state still fails closed.

**Regression:** `test_expired_active_keyset_can_be_replaced_by_valid_higher_sequence`.

### M-04 — Missing lifecycle state reset the trust baseline

**Original evidence:** a missing state file returned sequence zero and no root
fingerprint. Existing keyset/history material could then be treated as initial
provisioning.

**Fix:** Lifecycle state is now version 2 and includes the durable watermark,
root generation, key identities, and revocation history. Missing state is
accepted only when no keyset or history material exists. Otherwise the provider
fails closed with `trust_lifecycle_state_missing`.

**Regression:** `test_missing_state_after_activation_fails_closed`.

### M-05 — Revocation was not monotonic across later root-signed rotations

**Original evidence:** a later keyset could reuse a revoked key ID with the
same public key and `revoked=false`, or bind the key ID to different key
material.

**Fix:** Historical key IDs and public-key fingerprints are checked on every
candidate path. Reintroduction is rejected even when the later keyset is
correctly signed by the root. A new, never-revoked key ID remains available for
legitimate rotation.

### M-06 — Persisted lifecycle schema was under-validated

**Original evidence:** the prior state parser checked only the required key
names, version, and sequence integer. Fingerprints, IDs, transition names, and
history lists were not fully constrained.

**Fix:** Version-2 state validation now checks exact fields, SHA-256 formats,
key-ID formats, non-negative sequence/generation values, watermark ordering,
history list uniqueness, and revoked-fingerprint formats. Unsupported version 1
state fails closed with `trust_lifecycle_state_upgrade_required` rather than
being silently reinterpreted.

The delegated keyset parser continues to reject unknown fields, duplicate JSON
names, non-finite constants, invalid timestamps, unsupported algorithms,
duplicate IDs, invalid base64, non-32-byte public keys, invalid scopes,
future-issued material, expired material, and invalid parent/sequence changes.

### M-07 — Plugin persisted JSON accepted ambiguous/non-finite values

**Original evidence:** plugin registry/catalog/journal/manifest/integrity
reads used Python `json.loads()` directly. Duplicate keys were silently
overwritten and `NaN` could enter metadata before TUI numeric conversion.

**Fix:** `plugin_manager.py` now uses a strict JSON loader for these paths:

- duplicate field names are rejected;
- `NaN`, `Infinity`, and `-Infinity` are rejected;
- registry schema/version and record types are checked rather than silently
  dropping malformed records;
- JSON writes use `allow_nan=False`.

**Regression:** `test_registry_rejects_ambiguous_or_nonfinite_json`.

### M-08 — Cache-root symlink check occurred after resolution

**Original evidence:** `SignedCatalogCache` resolved the root before checking
whether the input root was a symlink. A symlinked root could therefore be
accepted as the resolved target.

**Fix:** The unresolved cache root and its immediate parent are checked before
resolution. A symlinked or reparse-point root is rejected.

**Regression:** `test_cache_rejects_symlinked_root_before_resolution` (skipped
only when the host cannot create directory symlinks).

### M-09 — Lifecycle status fields could be sourced from a different generation

**Original evidence:** manager records separately obtained catalog snapshot data
and trust-root status. A rotation between those reads could display one
generation with another generation's keyset metadata.

**Fix:** `CatalogSnapshot` now carries the root fingerprint, keyset ID/sequence,
expiry, active/revoked IDs, lifecycle reason, and last transition captured from
the same trust-status read used by the catalog snapshot. Manager fields use
those snapshot fields instead of performing a second independent status read.
CLI plugin info/inspect/install/status output and TUI state expose the same
lifecycle fields. The active list excludes revoked and temporally unusable
keys; revoked history remains separately visible.

There remains a documented local concurrency limitation: an external process
can rotate files between independently requested commands. Each provider
operation still re-locks and re-verifies its own authoritative state before
trusting or publishing material.

## 4. Recovery, revocation, and sequence invariants

The hardened invariants are:

1. A catalog-role key is usable only if it appears in the currently verified
   root-authorized keyset, has catalog scope, is not revoked, and is within its
   validity interval.
2. A key ID cannot be rebound to different public-key material within the
   lifecycle history.
3. Once a key ID or public-key fingerprint is revoked by an accepted keyset, an
   ordinary rotation, recovery, or root replacement cannot re-admit it as
   unrevoked.
4. Ordinary rotation requires a higher sequence and matching parent keyset ID.
5. Equal sequence with changed content fails as rollback.
6. Recovery is explicit and auditable, but confirmation alone does not erase
   authenticated revocation history.
7. Recovery preserves the durable highest sequence watermark. A lower sequence
   is visible as an exceptional operator-recovery transition, not silently
   treated as ordinary rotation.
8. A missing state file after prior keyset/history material is corruption, not
   first use.
9. Root replacement requires old-root cryptographic authorization, an
   independently compared expected new-root fingerprint, candidate-keyset
   verification under the new root, valid transition generation/time, and
   explicit confirmation.
10. Cached catalog envelopes are still re-verified by the Phase 1 verifier;
    lifecycle status fields cannot approve a catalog by themselves.

### Supported recovery limitation

The local tombstone/history files defend against ordinary recovery mistakes,
stale providers, partial writes, and accidental old-backup restoration. They do
not defend against a privileged local attacker who can atomically restore every
trusted local file—including the root, keyset, state, transaction marker,
history, application, and cache—to a prior complete generation. A remote or
hardware-authenticated monotonic counter would be required for that stronger
claim; none is present or invented here.

## 5. Root replacement design

The replacement API is now:

```text
replace_root(
    root_candidate_path,
    keyset_candidate_path,
    expected_old_fingerprint=..., 
    expected_new_fingerprint=..., 
    transition_path=..., 
    confirmed=True,
)
```

The old-root-signed transition envelope is:

```json
{
  "format": "aetherion.catalog.root-transition.envelope.v1",
  "key_id": "old-root-key",
  "algorithm": "Ed25519",
  "canonicalization": "RFC8785-JCS",
  "payload_sha256": "<sha256>",
  "payload": {
    "format": "aetherion.catalog.root-transition.payload.v1",
    "old_root_fingerprint": "<old-root-sha256>",
    "new_root_fingerprint": "<new-root-sha256>",
    "new_keyset_fingerprint": "<keyset-sha256>",
    "root_generation": 2,
    "issued_at": "2026-10-17T00:00:00Z",
    "expires_at": "2026-11-17T00:00:00Z"
  },
  "signature_base64": "<old-root-signature>"
}
```

The application checks the transition signature against an active old-root
key with `root-replacement` scope. The candidate root's fingerprint must also
equal the operator-supplied expected new fingerprint. The application does not
claim to authenticate how the operator obtained that expected fingerprint;
that comparison must be made through an independently authenticated external
process.

The candidate keyset is then verified using the candidate root and checked
against historical revocation/tombstone state before the transaction commits.

## 6. Crash consistency and fault-injection outcomes

### Implemented protocol

The lifecycle uses:

- per-provider lifecycle lock;
- transaction marker adjacent to the state file;
- previous-file snapshots for root/keyset/state/history;
- unique temporary writes and atomic replaces;
- file flush/fsync and best-effort parent-directory fsync;
- startup/operation recovery of incomplete transactions;
- committed marker semantics that do not report a false failure after a
  complete generation is published.

### Injected outcomes

| Injection point | Expected/observed result |
|---|---|
| Candidate root/keyset parse | Candidate rejected; active files unchanged |
| Wrong expected old fingerprint | `trust_root_replacement_requires_operator`; no writes |
| Wrong expected new fingerprint | `root_fingerprint_mismatch`; no writes |
| New keyset signed by new root but transition signed by new root | `root_transition_unauthorized`; no writes |
| Missing confirmation | operation rejected; no writes |
| Candidate keyset read/publication | exact validated bytes are used; second path read is not used |
| Keyset publication | transaction recovery restores previous complete snapshot on failure |
| Root publication | `test_failed_root_publication_rolls_back_complete_previous_pair` restores old root/keyset/state/history |
| State publication | transaction rollback restores previous state and active files |
| History preparation/publication | operation does not commit without history; rollback restores previous history |
| Transaction marker cleanup after commit | committed generation remains authoritative; cleanup can be retried |
| Startup with incomplete transaction | previous snapshot is restored before normal validation |
| Missing state after accepted material | `trust_lifecycle_state_missing`; no implicit first use |

The protocol is intentionally described as recoverable, not as strict
multi-file atomicity.

## 7. Audit-history semantics

`<state-stem>.history.jsonl` remains local operational history. It is not a
cryptographically authenticated audit log and is not presented as tamper-proof.

Each successful lifecycle generation records:

- operation (`initial`, `delegated_rotation`, `operator_recovery`,
  `operator_recovery_rollback`, or `root_replacement`);
- UTC timestamp;
- root fingerprint;
- keyset ID and sequence;
- keyset envelope fingerprint;
- revoked key IDs present in that keyset.

History is now prepared as a complete bounded file in the lifecycle transaction
rather than appended after the security-relevant files are already committed.
Existing records are parsed with duplicate-key/non-finite-value rejection and
unsupported record shapes fail closed. A history write failure rolls back the
root/keyset/state generation rather than returning a misleading success.

The history is still only local evidence. There is no authenticated remote
collector, append-only service, hardware counter, or signed audit chain in this
workspace.

## 8. CLI/TUI consistency and fail-closed behavior

### CLI

The trust commands now expose the hardened boundaries:

```text
aetherion trust status --json
aetherion trust validate-keyset <candidate> --json
aetherion trust apply-keyset <candidate> \
  --root-fingerprint <active-root-sha256> --confirm
aetherion trust recover-keyset <candidate> \
  --root-fingerprint <active-root-sha256> --confirm
aetherion trust replace-root <root> <keyset> \
  --transition <old-root-transition> \
  --old-root-fingerprint <old-root-sha256> \
  --new-root-fingerprint <independently-confirmed-new-root-sha256> \
  --confirm
```

`trust status --json` in the current workspace returns:

```json
{
  "configured": false,
  "reason_code": "catalog_trust_root_unconfigured",
  "provisioning_boundary": "explicit_configured_file"
}
```

No production material is guessed. Trust status catches lifecycle I/O and
transaction failures and reports a stable fail-closed reason instead of
presenting stale persisted status as verified authority.

Catalog info, inspect, install, and plugin info output now include keyset ID,
keyset sequence, root fingerprint, lifecycle transition/reason, and revoked
key IDs where the record is populated. JSON output preserves the same manager
fields.

### TUI

The TUI plugin model/gateway carries:

- root fingerprint;
- keyset ID, sequence, and expiry;
- active and revoked key IDs;
- lifecycle failure reason and last transition;
- catalog auth, catalog sequence, signer key, expiry, freshness, and policy.

The plugin detail view displays lifecycle information when a delegated keyset
is present. Source-only user records remain unsigned/unreviewed and cannot
inherit curated approval. A failure to refresh or verify trust does not delete
installed source or modify repository pins. `execution_enabled=False` remains
hard-coded in manager and TUI records.

Candidate validation does not activate or write the active keyset. Failed
apply/recover/replace operations do not print success; the CLI emits a stable
error and non-zero exit.

## 9. Exact verification performed

All commands below ran in the active application repository:

```text
python -m pytest cli/tests/test_trust_store.py cli/tests/test_trust_verifier.py cli/tests/test_catalog_distribution.py cli/tests/test_plugin_manager.py cli/tests/test_cli.py cli/tests/test_tui_state.py cli/tests/test_tui_plugins.py -q
```

Result: **exit 0**, **duration 22.028 seconds**, **213 passed**.

```text
python -m pytest cli/tests -q
```

Result: **exit 0**, **duration 31.763 seconds**, **298 passed**, 5 existing
Click deprecation warnings for `isolated_filesystem` in replay CLI tests.

```text
ruff check --select E9,F cli/aetherion_cli/trust_store.py cli/aetherion_cli/trust_verifier.py cli/aetherion_cli/catalog_distribution.py cli/aetherion_cli/plugin_manager.py cli/tests/test_trust_store.py cli/tests/test_catalog_distribution.py cli/tests/test_plugin_manager.py
```

Result: **exit 0**, **duration 0.227 seconds**, all checks passed.

This focused lint scope intentionally excludes unrelated pre-existing findings
in the large CLI/TUI modules. A broader `ruff check --select E9,F` over all
modified implementation files still reports pre-existing `main.py` and
`tui/gateway.py` findings such as duplicate legacy command definitions and
unused imports; those were not rewritten as unrelated cleanup.

```text
python -m py_compile cli/aetherion_cli/trust_store.py cli/aetherion_cli/trust_verifier.py cli/aetherion_cli/catalog_distribution.py cli/aetherion_cli/plugin_manager.py cli/aetherion_cli/config/settings.py cli/aetherion_cli/config/__init__.py cli/aetherion_cli/main.py cli/aetherion_cli/tui/state.py cli/aetherion_cli/tui/gateway.py cli/aetherion_cli/tui/screens/plugins.py cli/tests/test_trust_store.py cli/tests/test_catalog_distribution.py cli/tests/test_plugin_manager.py
```

Result: **exit 0**, **duration 0.882 seconds**.

```text
python -m aetherion_cli.main --help
```

Result: **exit 0**; the `trust` command group was listed.

```text
python -m aetherion_cli.main trust status --json
```

Result: **exit 0**; honest unconfigured status was returned with
`catalog_trust_root_unconfigured`.

```text
python -m aetherion_cli.main trust replace-root --help
```

Result: **exit 0**; help showed required `--transition`,
`--old-root-fingerprint`, and `--new-root-fingerprint` options.

### Repository-wide test attempt

```text
python -m pytest -q
```

Result: **exit 2**, **duration 23.549 seconds**, collection blocked by the
pre-existing integration import of `backend/auth_system.py`, which attempts a
PostgreSQL connection to `127.0.0.1:5432` and receives connection refused.
No deployment settings or services were changed to bypass this prerequisite.

## 10. Production provisioning boundary and non-claims

The workspace still has no authenticated production trust ceremony. The audit
does not claim the existence of:

- a real production root fingerprint;
- a production private signer or KMS/HSM custody process;
- an authenticated keyset or catalog publication endpoint;
- a dual-control or threshold approval process;
- a remote revocation responder or SLA;
- a signed REA catalog;
- publisher identity evidence;
- plugin execution, dependency installation, package scripts, MCP servers, or
  runtime activation.

The implementation remains explicitly unconfigured until real operator
material is supplied. No fake production key or signature was generated.

## 11. Protected-state evidence

Initial evidence was captured before editing:

```text
Application HEAD: 5c7bdcd4808a178aa677488f18b948a747f94193
backend/routes/d3_features.py SHA-256: 592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c
.git/index SHA-256: 18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
.git/index.lock: absent
local rea branch: main
local rea HEAD: e13da084e7c5967b80632ae5ad8d3d37bcf57469
local rea working tree: clean
```

Final evidence after testing:

```text
Application HEAD: 5c7bdcd4808a178aa677488f18b948a747f94193
backend/routes/d3_features.py SHA-256: 592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c
.git/index SHA-256: 18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
.git/index.lock: absent
local rea branch: main
local rea HEAD: e13da084e7c5967b80632ae5ad8d3d37bcf57469
local rea working tree: clean
```

The pre-existing tracked modification to `backend/routes/d3_features.py` was
preserved. Pre-existing untracked workspace content was preserved. No commit,
push, reset, clean, Git-index mutation, unrelated deletion, secret/config
alteration, deployment change, or local `rea/` modification was performed.

## 12. Remaining next steps

1. Have the accountable operator authenticate and provision the real root
   fingerprint, root-replacement policy, signer custody, keyset publication,
   recovery backups, and catalog endpoint outside this repository.
2. Exercise the old-root-signed transition workflow with real operational
   approvals before accepting any production root replacement.
3. Run the repository-wide suite in the supported environment with PostgreSQL
   available.
4. If stronger rollback resistance is required against a privileged local
   attacker, add an externally authenticated monotonic counter or remote audit
   authority; do not treat local JSON history as sufficient.
