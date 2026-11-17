# Aetherion Trust-Key Lifecycle Phase 3 Implementation Report

**Date:** 2026-10-17  
**Status:** Phase 3 bounded lifecycle implementation complete; production trust-root rollout not provisioned  
**Execution policy:** Option A / control-plane only / execution disabled  
**Scope:** Root-authorized delegated catalog-keysets, rotation, revocation, expiry, rollback protection, operator recovery, explicit root replacement, CLI/TUI status, and adversarial lifecycle tests

## 1. Executive result

Phase 3 adds the smallest complete lifecycle around the Phase 1 verifier and
Phase 2 signed-catalog distribution path. The root remains an independently
provisioned operator/release boundary; a signed catalog cannot enroll or
replace trust material.

Implemented behavior:

- A configured offline-root file may contain only explicitly provisioned
  Ed25519 keys with `keyset` scope for delegated-keyset authorization.
- A delegated keyset is an independently signed, versioned envelope with:
  - strict exact-object schemas;
  - RFC 8785 JCS payload signing;
  - SHA-256 payload binding;
  - root key identity and scope checks;
  - keyset ID, sequence, parent ID, issue time, and expiry;
  - catalog-role public keys with scope, validity, and revocation metadata.
- The lifecycle provider verifies the root signature before exposing any
  delegated catalog key to the Phase 1 verifier.
- The first keyset requires explicit operator activation through the same
  candidate/confirmation boundary used for rotation.
- Delegated rotation requires a strictly higher sequence and a parent ID that
  matches the active keyset.
- Equal/lower sequences and same-sequence content changes fail closed as
  `keyset_sequence_rollback`.
- Keyset state is persisted atomically with root fingerprint, keyset
  fingerprint, sequence, ID, and last transition.
- Root fingerprint changes are rejected during normal loading and catalog
  refresh; root replacement is available only as an explicit, confirmed
  root-plus-keyset operation.
- Revoked catalog keys remain visible in lifecycle status but cannot verify a
  catalog envelope.
- Key and keyset validity intervals are enforced before catalog approval.
  Expired keysets surface as stale/expired authority rather than approval.
- Operator-confirmed recovery can restore a root-authorized backup, including
  an older sequence, and records the exceptional rollback in audit history.
- Candidate validation is separate from activation and never changes the
  active keyset.
- CLI trust commands expose root/keyset status and require explicit
  confirmation for activation, recovery, and root replacement.
- TUI plugin state carries root fingerprint, keyset ID/sequence/expiry,
  active/revoked key IDs, lifecycle reason, and transition metadata. The
  detail pane displays lifecycle data when a delegated keyset is present.
- `execution_enabled=False` remains hard locked throughout the lifecycle.

The current workspace still has no production endpoint, root fingerprint,
signer custody process, or signed REA catalog. Therefore no production trust
claim is made and the curated `rea` record remains unsigned/unreviewed until
those external operational inputs are supplied.

No production private key, generated production public key, signed catalog,
threshold-signing claim, TUF repository, external revocation service, or
runtime activation path was added.

## 2. Actual files changed

### Production implementation

- `cli/aetherion_cli/trust_store.py`
  - Extends trust-root status with keyset identity, sequence, expiry,
    revocation, and transition fields.
  - Adds root-authorized delegated-keyset parsing and Ed25519 verification.
  - Adds strict keyset/payload/key validity and scope checks.
  - Adds `LifecycleCatalogKeyProvider` with atomic state/keyset activation,
    sequence rollback protection, audit history, recovery, and root
    replacement.
  - Keeps the Phase 2 static configured provider available when no delegated
    keyset path is configured, preserving the existing compatibility boundary.
- `cli/aetherion_cli/trust_verifier.py`
  - Adds stable lifecycle reason codes.
  - Extends `CatalogKey` with key validity and keyset provenance metadata.
  - Converts provider lifecycle failures into typed catalog verification
    results.
  - Enforces delegated key and keyset validity intervals.
  - Maps revoked and expired authority to conservative policy states.
- `cli/aetherion_cli/catalog_distribution.py`
  - Maps lifecycle failures into catalog snapshot status instead of collapsing
    every keyset failure into generic unsigned state.
  - Represents expired keyset/revocation authority as expired/stale trust.
- `cli/aetherion_cli/plugin_manager.py`
  - Loads the lifecycle provider from explicit root/keyset/state settings.
  - Propagates keyset identity, sequence, expiry, active/revoked IDs, and
    lifecycle transition/reason fields into curated plugin records.
  - Removes lifecycle fields from user-controlled records along with other
    signed-catalog fields.
- `cli/aetherion_cli/config/settings.py`
  - Adds `catalog_keyset` and `catalog_trust_state` settings.
  - Supports `AETHERION_CATALOG_KEYSET` and
    `AETHERION_CATALOG_TRUST_STATE` environment overrides.
- `cli/aetherion_cli/config/__init__.py`
  - Re-exports the lifecycle configuration constants and accessors.
- `cli/aetherion_cli/main.py`
  - Adds `trust status`, `trust validate-keyset`, `trust apply-keyset`,
    `trust recover-keyset`, and `trust replace-root`.
  - Adds delegated keyset and lifecycle-state configuration options.
  - Requires root fingerprints and explicit `--confirm` flags for mutating
    trust operations.
- `cli/aetherion_cli/tui/state.py`
  - Adds lifecycle fields to the plugin view model.
- `cli/aetherion_cli/tui/gateway.py`
  - Maps lifecycle status from plugin-manager records into TUI state.
- `cli/aetherion_cli/tui/screens/plugins.py`
  - Displays delegated keyset lifecycle status when present without expanding
    the default empty-state layout used by existing TUI tests.

### Tests

- `cli/tests/test_trust_store.py`
  - Adds root-signed keyset construction using ephemeral test keys only.
  - Tests candidate validation versus activation.
  - Tests lifecycle loader selection.
  - Tests root signature tampering, sequence rollback, parent-bound rotation,
    explicit recovery rollback, keyset expiry, and confirmed root replacement.
- Existing verifier, catalog-distribution, plugin-manager, and TUI suites
  - Continue to cover Phase 1/2 behavior and execution-disabled invariants.

No backend, deployment, secret, `.env`, protected D3 source, Git index, or
local `rea` checkout was modified by this phase.

## 3. Delegated keyset contract

### 3.1 Root trust boundary

The root file remains an operator/release-provisioned file and is not learned
from a catalog. Its existing Phase 2 format is used with `keyset` scope:

```json
{
  "format": "aetherion.catalog.trust-root.v1",
  "keyset_id": "offline-root-v1",
  "keys": [
    {
      "key_id": "offline-root-key",
      "algorithm": "Ed25519",
      "scopes": ["keyset"],
      "public_key_base64": "<32-byte public key>",
      "revoked": false
    }
  ]
}
```

The root fingerprint is the SHA-256 digest of the canonical root document.
Changing it during an ordinary provider load is rejected as
`trust_root_replacement_requires_operator`. There is no first-seen-root
acceptance path.

### 3.2 Signed delegated keyset envelope

The lifecycle provider accepts only the following exact envelope shape:

```json
{
  "format": "aetherion.catalog.keyset.envelope.v1",
  "key_id": "offline-root-key",
  "algorithm": "Ed25519",
  "canonicalization": "RFC8785-JCS",
  "payload_sha256": "<64 lowercase hex characters>",
  "payload": {
    "format": "aetherion.catalog.keyset.payload.v1",
    "keyset_id": "catalog-keyset-2",
    "sequence": 2,
    "parent_keyset_id": "catalog-keyset-1",
    "issued_at": "2026-10-16T00:00:00Z",
    "expires_at": "2026-11-16T00:00:00Z",
    "keys": [
      {
        "key_id": "catalog-role-2",
        "algorithm": "Ed25519",
        "scopes": ["catalog"],
        "public_key_base64": "<32-byte public key>",
        "revoked": false,
        "valid_from": null,
        "valid_until": null,
        "revocation_reason": ""
      }
    ]
  },
  "signature_base64": "<root signature over canonical payload>"
}
```

The envelope is not accepted unless all of the following hold:

1. The raw bytes are within the bounded lifecycle envelope limit.
2. JSON parsing succeeds without the verifier's duplicate-name rejection.
3. Every object has the exact supported field set.
4. The format, algorithm, and canonicalization identifiers are supported.
5. The payload digest matches the canonical payload bytes.
6. The root key ID exists in the independently loaded root file.
7. The root key is active, Ed25519, and has `keyset` scope.
8. The root signature verifies over the canonical payload.
9. The keyset interval is valid, not future-dated beyond clock skew, and not
   expired.
10. The sequence, parent relationship, key IDs, scopes, encodings, validity
    intervals, and revocation fields satisfy the schema.

Catalog-role keys are exposed only after all checks succeed. A catalog
envelope cannot supply a root key, delegated key, scope, revocation override,
or keyset transition.

## 4. Lifecycle transitions

### 4.1 Initial provisioning

Initial provisioning is deliberately not implicit. The operator/release
process supplies the root file and a candidate delegated keyset, then runs:

```text
aetherion config --catalog-trust-root <root-file>
aetherion config --catalog-keyset <active-keyset-file>
aetherion config --catalog-trust-state <state-file>
aetherion trust validate-keyset <candidate-file>
aetherion trust apply-keyset <candidate-file> \
  --root-fingerprint <active-root-sha256> \
  --confirm
```

Validation does not write the active keyset. Activation requires both the
expected current-root fingerprint and the explicit confirmation flag.

### 4.2 Delegated rotation

`apply-keyset` accepts a candidate only when the active state can verify it and:

- candidate sequence is greater than the active sequence;
- candidate parent ID equals the active keyset ID;
- candidate is root-signed by an active keyset-scoped root key;
- candidate is currently valid and unexpired;
- candidate catalog keys are schema-valid and scoped to `catalog`.

The candidate bytes are atomically replaced into the configured keyset path.
The state document is then atomically replaced with the new root/keyset
fingerprints and transition metadata. A crash before state publication leaves
the previous state recoverable; a crash after keyset publication is rechecked
against the signed bytes on the next load.

### 4.3 Revocation

Revocation is represented by the signed delegated keyset's `revoked` field and
reason. A revoked key is retained in status for diagnosis but is rejected by
`verify_catalog()` with `catalog_key_revoked`. A revoked key cannot be revived
by a catalog envelope or by an ordinary sequence replay.

This implementation intentionally does not invent an external revocation
service or online status endpoint. Freshness is bounded by the signed keyset
expiry. An expired keyset is not a valid revocation source and is surfaced as
expired/stale authority until an operator supplies a newer root-authorized
keyset.

### 4.4 Key and keyset expiry

The provider checks:

- keyset `issued_at` and `expires_at`;
- delegated key `valid_from` and `valid_until`;
- catalog verification time against those intervals;
- the existing Phase 1 catalog envelope interval.

An expired delegated keyset prevents catalog approval before any catalog key
is exposed. An expired delegated catalog key produces an expired catalog
verification result. Manager/TUI policy remains conservative and execution
stays disabled.

### 4.5 Operator recovery

Normal activation rejects lower/equal sequence state. An operator-supplied
backup can be restored only through:

```text
aetherion trust recover-keyset <backup-file> \
  --root-fingerprint <active-root-sha256> \
  --confirm
```

Recovery still requires a valid root signature and current validity interval.
The explicit confirmation boundary is the only path that can restore an older
sequence. The transition is recorded as `operator_recovery` or
`operator_recovery_rollback` in the audit history.

Recovery is not an automatic fallback to a revoked key. The operator must
provide the backup and authorize the transition; no catalog response can
trigger it.

### 4.6 Root replacement

Root replacement is a separate, high-impact operation requiring an expected
old-root fingerprint, a candidate root, a candidate keyset signed by the new
root, and explicit confirmation:

```text
aetherion trust replace-root <new-root-file> <new-keyset-file> \
  --old-root-fingerprint <active-root-sha256> \
  --confirm
```

The candidate pair is verified before activation. The root and keyset are
written through the existing atomic file boundary, and state records both the
new root fingerprint and `previous_root_fingerprint`. Ordinary refresh/load
cannot perform this transition.

## 5. Persisted lifecycle state and audit evidence

The configured state file has this exact shape:

```json
{
  "version": 1,
  "root_fingerprint": "<sha256>",
  "keyset_fingerprint": "<sha256>",
  "keyset_id": "catalog-keyset-2",
  "keyset_sequence": 2,
  "previous_root_fingerprint": null,
  "last_transition": "delegated_rotation"
}
```

State writes use a unique temporary file followed by atomic replacement.
Symlinks/reparse points and oversized lifecycle files are rejected. The
provider also appends bounded, non-secret transition records to the derived
`<state-stem>.history.jsonl` path. Audit records contain only timestamps,
operation names, root/keyset fingerprints, keyset IDs, and sequences; no
private key or credential material is written.

The lifecycle provider re-verifies signed keyset bytes on every load. Persisted
status fields are evidence for rollback detection and display, not a substitute
for signature verification.

## 6. CLI and TUI behavior

### CLI

The new commands are:

```text
aetherion trust status [--json]
aetherion trust validate-keyset <candidate> [--json]
aetherion trust apply-keyset <candidate> --root-fingerprint <sha256> --confirm
aetherion trust recover-keyset <candidate> --root-fingerprint <sha256> --confirm
aetherion trust replace-root <root> <keyset> --old-root-fingerprint <sha256> --confirm
```

`trust status` reports:

- configured state and provisioning boundary;
- root fingerprint;
- keyset ID, sequence, expiry;
- active and revoked delegated key IDs;
- last transition and stable failure reason.

The command reports the current honest unconfigured state in this workspace:

```text
configured: false
reason_code: catalog_trust_root_unconfigured
provisioning_boundary: explicit_configured_file
```

No mutating command has a default activation path. Missing confirmation,
fingerprint mismatch, unknown root material, malformed candidates, signature
failure, expiry, parent mismatch, and rollback all fail closed with a stable
reason code.

### TUI

Plugin records carry the same lifecycle fields through the existing gateway
and state model. When a delegated keyset is present, the plugin detail pane
shows its ID/sequence, lifecycle result/transition, and a shortened root
fingerprint in addition to existing catalog auth, catalog sequence, signer
key, expiry, cache freshness, policy, and execution fields.

The TUI remains a control-plane display. It does not import, enable, execute,
install dependencies for, spawn, or activate plugin code.

## 7. Verification order and fail-closed behavior

### Keyset load

1. Check the configured root and keyset paths for regular-file safety.
2. Parse and strictly validate the independently provisioned root.
3. Read lifecycle state and reject unsupported/corrupt state.
4. Reject unconfirmed root fingerprint replacement.
5. Parse the delegated keyset envelope and exact payload schema.
6. Check keyset validity, sequence, and parent transition.
7. Verify payload digest and root signature.
8. Validate every delegated catalog key, validity interval, scope, and
   revocation flag.
9. Atomically publish state only after the candidate is accepted.
10. Expose the resulting catalog-key mapping to Phase 1.

### Catalog refresh

1. `SignedCatalogService` obtains lifecycle status.
2. Missing root/keyset remains unconfigured/unsigned.
3. Invalid lifecycle state becomes invalid trust with the specific lifecycle
   reason.
4. Expired keyset/revocation authority becomes expired/stale trust.
5. Only a fully verified delegated catalog key reaches `verify_catalog()`.
6. Phase 2 transport, cache, LKG, and catalog sequence protections continue
   unchanged.

### Curated manager policy

1. Curated records must match the signed catalog subject exactly.
2. Any lifecycle failure prevents curated approval/install/update.
3. User-saved aliases cannot forge lifecycle metadata.
4. Archive and installed-file integrity remain separate from catalog trust.
5. Execution remains disabled regardless of trust result.

## 8. Tests and actual verification

Commands run after the implementation:

```text
python -m pytest cli/tests/test_trust_store.py cli/tests/test_trust_verifier.py cli/tests/test_catalog_distribution.py -q
```

Result: **65 passed**.

```text
python -m pytest cli/tests/test_tui_plugins.py -q
```

Result: **9 passed**.

```text
python -m pytest cli/tests -q
```

Result: **290 passed, 5 warnings**.

The warnings are existing Click deprecation warnings for
`isolated_filesystem` in replay CLI tests.

```text
python -m py_compile \
  cli/aetherion_cli/trust_store.py \
  cli/aetherion_cli/catalog_distribution.py \
  cli/aetherion_cli/trust_verifier.py \
  cli/aetherion_cli/plugin_manager.py \
  cli/aetherion_cli/config/settings.py \
  cli/aetherion_cli/config/__init__.py \
  cli/aetherion_cli/main.py \
  cli/aetherion_cli/tui/state.py \
  cli/aetherion_cli/tui/gateway.py \
  cli/aetherion_cli/tui/screens/plugins.py \
  cli/tests/test_trust_store.py
```

Result: passed.

```text
ruff check --select E9 <changed implementation and test files>
```

Result: passed. A broader repository Ruff run still reports pre-existing style
and typing findings outside this focused lifecycle change; those were not
rewritten as part of Phase 3.

```text
python -m aetherion_cli.main --help
python -m aetherion_cli.main trust status --json
```

Both commands completed. The status command reports the honest unconfigured
root state rather than creating or guessing trust material.

The repository-wide command was also attempted:

```text
python -m pytest -q
```

Collection is blocked before application tests by the pre-existing integration
fixture importing `backend/auth_system.py` and attempting PostgreSQL at
`127.0.0.1:5432`; the local PostgreSQL service is not running. This is an
environmental integration prerequisite, not a Phase 3 CLI test failure.

## 9. Current production boundary

The following are intentionally not claimed or supplied:

- production root public-key fingerprint;
- production catalog-role key or private signer;
- signer custody, dual control, or approval roster;
- authenticated keyset publication endpoint;
- signed catalog publication endpoint;
- revocation distribution SLA or online responder;
- external KMS/HSM integration;
- recovery escrow or offline backup inventory;
- a real signed REA catalog;
- plugin execution or dependency installation.

The implemented code is therefore a testable, explicit lifecycle boundary,
not proof that the production trust ceremony has occurred. Before production
curated approval can be enabled, the external operators must provision and
document the root, signer custody, keyset publication, rotation/revocation
runbook, recovery backups, and signed catalog endpoint.

## 10. Protected-state verification

The following protected state was preserved throughout the phase:

```text
Application branch: main
Application HEAD:   5c7bdcd4808a178aa677488f18b948a747f94193
Protected D3 SHA:   592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c
Git index SHA:      18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
Git index lock:     absent
REA checkout:       unchanged and clean
```

The pre-existing tracked `backend/routes/d3_features.py` modification and
pre-existing untracked workspace content were not touched. No commit, push,
reset, clean, or Git-index mutation was performed.

## 11. Remaining operational next steps

1. Provision the real root public key through the approved release/operator
   channel; do not commit private material.
2. Produce and independently review the first root-signed delegated keyset.
3. Publish a signed catalog whose signer key is present in that keyset.
4. Exercise rotation, revocation, expiry, and recovery with the real custody
   process before enabling any curated approval policy.
5. Start PostgreSQL and rerun the repository-wide integration suite in the
   supported environment.
