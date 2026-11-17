# Aetherion Trust Verifier Phase 1 Implementation Report

**Date:** 2026-10-17  
**Status:** Phase 1 implemented and locally verified  
**Execution policy:** Option A / control-plane only / execution disabled  
**Scope:** Trust-state model, strict signed-catalog envelope/payload validation,
RFC 8785-compatible constrained canonicalization, Ed25519 verification, stable
failure reasons, conservative CLI/TUI representation, and adversarial tests

## 1. Executive result

Phase 1 is implemented without starting Phase 2 signed-catalog distribution,
publisher-evidence adapters, or production trust-root rollout.

The implementation now provides:

- A typed, independent trust-state model for integrity, catalog authenticity,
  publisher evidence, compatibility, policy, and execution.
- A bounded and strict catalog envelope/payload verifier.
- Duplicate-key rejection for raw JSON input and rejection of non-standard JSON
  constants.
- RFC 8785 JCS-compatible canonicalization for the catalog schema's supported
  value subset, with UTF-16 property ordering and UTF-8 output.
- Ed25519 signature verification through the maintained `cryptography` library.
- Explicit externally supplied key-provider boundaries. Envelope-embedded keys
  cannot bootstrap trust.
- Freshness, future-clock-skew, expiry, sequence replay, key scope, revocation,
  signature, subject-binding, digest, schema, and execution-policy checks.
- Stable structured result statuses and reason codes.
- Backward-compatible unsigned/unknown defaults for older manager records.
- CLI/TUI fields that distinguish archive integrity from catalog authenticity;
  execution remains source-locked to disabled.
- 31 deterministic Phase 1 tests covering valid, malformed, altered, replayed,
  expired, unknown-key, revoked-key, wrong-key, duplicate-key, subject-binding,
  migration, CLI, and model behavior.

No production signing key, catalog transport, trusted root, publisher adapter,
or activation behavior was added.

## 2. Files changed

### Production code

- `cli/aetherion_cli/trust_verifier.py` — new isolated trust model, JCS
  canonicalizer, strict schema validation, key-provider protocol, and verifier.
- `cli/aetherion_cli/plugin_manager.py` — conservative trust fields on curated,
  user, discovery, inspected, installed, and legacy records; integrity status
  remains independently derived from the existing authoritative file hashes.
- `cli/aetherion_cli/main.py` — qualified archive-integrity/catalog-auth/
  publisher-auth/policy presentation in plugin info, inspect, install, and
  status output.
- `cli/aetherion_cli/tui/state.py` — independent trust fields on `Plugin` and
  `PluginCandidate`, with `execution_status="disabled"` defaults.
- `cli/aetherion_cli/tui/gateway.py` — maps trust fields into TUI state while
  hard-coding execution disabled.
- `cli/aetherion_cli/tui/screens/plugins.py` — displays archive integrity and
  catalog/publisher/policy states separately without a broad redesign.
- `cli/pyproject.toml` — declares `cryptography>=42.0` as the maintained
  Ed25519 verification dependency.

### Tests

- `cli/tests/test_trust_verifier.py` — new test-only generated-key fixtures and
  adversarial Phase 1 tests. Private keys are generated in memory per test and
  are not committed.

### Documentation

- `AETHERION_TRUST_VERIFIER_PHASE1_IMPLEMENTATION_REPORT.md` — this report.

The existing architecture decision report and other prior reports were not
overwritten.

## 3. Dependency and canonicalization decisions

### 3.1 Cryptography

The CLI declares Python `>=3.9`. The environment was Python `3.14.4` with:

```text
cryptography 48.0.0
PyNaCl 1.6.2
```

Neither library was previously declared in `cli/pyproject.toml`. `cryptography`
was selected because its maintained Ed25519 API directly supports the required
public-key verification primitive and is already available in the test
environment. The smallest justified declaration was added:

```toml
cryptography>=42.0
```

No dependency was installed during this task. No cryptographic primitive was
implemented from scratch.

The CLI package has no existing `uv.lock`, `poetry.lock`, `Pipfile.lock`, or
`requirements.txt`; this pre-existing packaging gap was not expanded into a
separate dependency-resolution task. The declaration is therefore intentionally
small and the absence of a lockfile remains an operational follow-up.

### 3.2 RFC 8785 JCS

The environment did not have an `rfc8785`, `jcs`, or `canonicaljson` package.
Rather than silently use the existing local helper's `json.dumps(sort_keys=True)`
as if it were JCS, the new verifier contains a constrained, schema-reviewed JCS
encoder:

- object keys are sorted by UTF-16 code units, as required by JCS;
- strings are emitted as UTF-8 JSON strings with lone surrogates rejected;
- booleans, arrays, objects, and bounded integers are supported;
- floating-point values are rejected, avoiding Python/ECMAScript number-format
  differences;
- integers are limited to the safe JSON number range;
- the catalog schema contains only the supported value types;
- duplicate keys are rejected while parsing raw JSON;
- future schema fields requiring floating-point numbers require a new reviewed
  canonicalization/dependency decision.

This is not presented as a general-purpose arbitrary-JSON JCS library. It is an
explicitly constrained JCS implementation for the Phase 1 catalog schema.

## 4. Actual trust-state schema

`aetherion_cli.trust_verifier.TrustState` represents independent claims:

```text
integrity_status:
  unknown | checked | verified | mismatch | corrupt

catalog_auth_status:
  not_applicable | unsigned | unknown_key | invalid | expired | replayed |
  verified | revoked

publisher_auth_status:
  not_checked | unsigned | unknown_identity | invalid | verified | revoked |
  mismatched_identity

compatibility_status:
  unknown | compatible | incompatible | source_only

policy_status:
  unreviewed | user_approved | catalog_approved | denied | stale | revoked

execution_status:
  disabled
```

Legacy behavior is conservative:

- missing catalog fields become `catalog_auth_status=unsigned`;
- missing publisher fields become `publisher_auth_status=not_checked`;
- missing policy fields become `policy_status=unreviewed`;
- missing integrity fields become `integrity_status=unknown`;
- malformed legacy trust fields fall back to the same conservative defaults;
- `execution_status` is always `disabled`, regardless of legacy
  `execution_enabled` data;
- source-only/unknown compatibility can add a reason code of
  `source_only_execution_disabled` or `compatibility_unknown` without changing
  existing compatibility semantics.

`CatalogVerificationResult` always includes:

- `catalog_auth_status`;
- `policy_status`;
- `reason_code` or `None` on success;
- `verified_object="catalog_payload"`;
- the trusted `authority_key_id` when available;
- catalog sequence and canonical payload digest when reached;
- selected subject plugin ID and entry when subject matching was requested;
- `qualified_status`, such as `catalog_signature_verified`, never an unqualified
  generic `verified` claim;
- `execution_status="disabled"`.

## 5. Verifier API and verification order

The public entry point is:

```python
verify_catalog(
    envelope: bytes | str | Mapping[str, Any],
    trusted_keys: CatalogKeyProvider,
    *,
    now: datetime | None = None,
    highest_accepted_sequence: int | None = None,
    expected_subject: CatalogSubject | None = None,
    clock_skew: timedelta = timedelta(minutes=5),
) -> CatalogVerificationResult
```

`CatalogKeyProvider` exposes only:

```python
get_key(key_id: str) -> CatalogKey | None
```

`CatalogKey` contains a key ID, raw 32-byte Ed25519 public key, algorithm,
catalog scope, and revocation state. `MappingCatalogKeyProvider` is an explicit
adapter for controlled callers and tests; it does not claim that the mapping is
a production trust root.

The verifier performs these steps without writing files, changing pins,
installing archives, promoting source, or modifying execution state:

1. Parse bounded UTF-8 bytes/text with duplicate-key rejection and rejection of
   `NaN`, `Infinity`, and `-Infinity`.
2. Validate envelope format, exact keys, key ID, Ed25519 algorithm,
   `RFC8785-JCS` identifier, payload digest shape, and strict 64-byte base64
   signature encoding.
3. Validate payload format and exact keys; reject unsupported critical/unknown
   fields through strict schema checks.
4. Validate sequence, timestamps, entry count, deterministic plugin-ID order,
   duplicate IDs, and duplicate repository/commit subjects.
5. Validate canonical HTTPS GitHub repository roots, owner/name binding, full
   lowercase 40-character commit SHA, archive format/digest/size, safe manifest
   path/digest/schema fields, compatibility metadata, and explicit
   `execution_enabled=False`.
6. Canonicalize the validated payload and recompute `payload_sha256`.
7. Obtain the expected public key from the explicitly supplied provider. A key
   embedded in the envelope is not part of the schema and cannot bootstrap
   itself.
8. Check key identity, Ed25519 algorithm, catalog scope, revocation, and key
   length.
9. Verify the Ed25519 signature over the canonical payload bytes.
10. Reject sequence replay/rollback against `highest_accepted_sequence`.
11. Apply timestamp policy: issued times may be at most five minutes in the
    future; expiry is strict and never treated as fresh offline.
12. If supplied, compare the selected entry to `CatalogSubject` across
    repository, commit, archive digest/size, manifest path/digest/schema/API,
    compatibility, and execution policy.
13. Return a typed qualified result with a bounded detail string and stable
    reason code.

## 6. Trust-root boundary and production status

Phase 1 has no production trust root. There is no built-in arbitrary public key,
no automatic first-use enrollment, no catalog-embedded key acceptance, and no
automatic replacement after failure.

The production manager does not invoke the verifier to approve the existing
curated `rea` record. Existing curated metadata remains explicitly:

```text
catalog_auth_status=unsigned
policy_status=unreviewed
publisher_auth_status=not_checked
execution_status=disabled
```

The current protected commit pin remains metadata/integrity provenance, not a
cryptographic Aetherion approval claim. The verifier can return
`catalog_signature_verified` and `catalog_approved` only when a caller
explicitly supplies a trusted key provider and a matching subject; the test
caller uses ephemeral in-memory keys. No such provider is wired as a production
root in this phase.

This preserves the architecture decision's trust boundary: Phase 1 supplies the
verifier contract and data model, while Phase 2 must establish authenticated
catalog distribution and Phase 3 must establish operational root/key custody.

## 7. Stable reason codes

The verifier defines these stable `CatalogReason` values:

```text
catalog_signature_missing
catalog_signature_invalid
catalog_key_unknown
catalog_key_revoked
catalog_expired
catalog_not_yet_valid
catalog_sequence_rollback
catalog_entry_missing
catalog_subject_mismatch
catalog_payload_digest_mismatch
catalog_schema_invalid
catalog_key_scope_mismatch
trust_store_corrupt
archive_digest_mismatch
manifest_digest_mismatch
installed_file_digest_mismatch
compatibility_unknown
source_only_execution_disabled
```

Parser and schema failures are never converted to successful verification.
Missing signatures are `unsigned` with `catalog_signature_missing`; unknown
keys are `unknown_key` with `catalog_key_unknown`; revoked keys are `revoked`
with `catalog_key_revoked`; expired and not-yet-valid records retain their
qualified status and do not become fresh approvals.

The plugin manager uses the existing authoritative archive/per-file registry
checks for integrity. Phase 1 adds `integrity_status` and emits
`installed_file_digest_mismatch` when an installed source no longer matches the
authoritative registry hashes. It does not replace or weaken those checks.

## 8. Adversarial tests

`cli/tests/test_trust_verifier.py` uses generated ephemeral Ed25519 private keys
only in memory. It does not contain an operational private key. The test suite
covers:

- valid signature with an explicitly supplied test trust key;
- one-byte payload change and payload digest mismatch;
- changed signature and wrong public key;
- altered repository URL, commit SHA, archive digest/size, manifest path/digest,
  compatibility, and policy subject binding;
- explicit `execution_enabled=True` rejection;
- unknown key ID, revoked key, wrong key scope, and trust-provider failure path;
- wrong algorithm, wrong canonicalization identifier, malformed base64, and
  malformed signature length;
- duplicate JSON keys, duplicate plugin IDs, unsupported payload schema, and
  unsupported critical/extra envelope fields;
- expired, not-yet-valid, and replayed/rolled-back catalog sequences;
- malformed SHA, URL, timestamp, and scalar field types;
- attacker key field cannot bootstrap trust and an empty production provider
  yields `catalog_key_unknown`;
- missing signature yields unsigned rather than approval;
- legacy record defaults remain unsigned/unknown and execution disabled;
- existing catalog records are read as unapproved without silently upgrading;
- manager and TUI models keep archive integrity and catalog authentication
  independent;
- CLI output labels archive integrity, catalog auth, and publisher auth
  separately;
- JCS property ordering and float rejection;
- non-standard JSON constant rejection.

## 9. Verification results

Commands were run from:

```text
C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli\cli
```

### Phase 1 tests

```text
python -m pytest -o addopts="" -q tests/test_trust_verifier.py
31 passed in 0.49s
```

### Focused existing lifecycle/CLI/TUI regression tests

```text
python -m pytest -o addopts="" -q tests/test_plugin_manager.py tests/test_cli.py tests/test_tui_state.py tests/test_tui_plugins.py
140 passed in 10.04s
```

### Full CLI test directory

The full run completed successfully before final report generation and is
rerun after the report is written as part of final verification:

```text
python -m pytest -o addopts="" -q tests
256 passed, 5 warnings in 20.32s
exit code: 0
```

The five warnings were pre-existing Click deprecation warnings for
`CliRunner.isolated_filesystem()` in `tests/test_replay_cli.py`. No tests were
skipped and no dependency-install or network operation was used by these test
commands.

### Focused Ruff checks

```text
python -m ruff check aetherion_cli/trust_verifier.py aetherion_cli/plugin_manager.py tests/test_trust_verifier.py --select E4,E7,E9,F,B,W,I001 --output-format concise
All checks passed!
```

The full `main.py`/TUI lint surface contains pre-existing findings outside this
change; the changed trust verifier, manager path, and new tests pass the focused
rules above. Python compilation of the modified production modules also passed.

## 10. Security claims supported by Phase 1

Phase 1 supports these precise claims:

- A raw catalog envelope with duplicate JSON names or non-standard constants is
  rejected before schema interpretation can diverge.
- A valid Phase 1 envelope has a strict format, algorithm, canonicalization
  identifier, signature encoding, payload digest, schema, subject, validity,
  and execution-policy shape.
- An Ed25519 signature is verified with the public key returned by the caller's
  explicitly supplied trusted-key provider.
- A key appearing in an untrusted catalog cannot bootstrap itself because key
  material is not accepted from the envelope.
- The selected signed entry can be bound to an exact canonical repository,
  full commit, archive digest/size, manifest, compatibility value, and
  `execution_enabled=False` policy.
- Expiry, future issuance, revoked/unknown keys, signature failures, digest
  changes, sequence replay, malformed schema, and subject mismatch fail closed
  with qualified status and stable reason code.
- Older manager records remain readable but are never upgraded to signed or
  catalog-approved state merely because trust fields are absent.
- Existing archive/per-file integrity remains separate from catalog
  authenticity.
- CLI/TUI data can show archive integrity and catalog authenticity separately.
- A valid signature cannot enable downloaded source execution; execution remains
  disabled in every state.

## 11. Security claims not supported by Phase 1

Phase 1 does **not** support these claims:

- that any current Aetherion curated entry is cryptographically approved;
- that the application has an independently distributed production trust root;
- that a production catalog publisher, custody process, rotation process,
  revocation service, or recovery ceremony exists;
- that a GitHub commit signer, repository owner, or catalog signer is benign;
- that source is malware-free, vulnerability-free, or safe to execute;
- that a source archive is publisher-authenticated merely because its SHA-256
  matches a locally observed value;
- that GitHub API signature fields, Sigstore, or in-toto evidence are verified;
- that an unsigned user repository is malicious;
- that local atomic writes, file permissions, or key storage protect against a
  fully privileged host administrator;
- that signature verification alone authorizes runtime activation.

## 12. Protected-state verification

The task-start baseline was captured before editing:

```text
Branch: main
Application HEAD: 5c7bdcd4808a178aa677488f18b948a747f94193
backend/routes/d3_features.py SHA-256:
  592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c
.git/index SHA-256:
  18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
.git/index.lock: absent
Existing tracked modification: backend/routes/d3_features.py
```

The application workspace already contained many unrelated untracked files and
reports. They were preserved. No commit, push, reset, clean, index mutation,
unrequested deletion, deployment change, secret, credential, `.env`, backend
D3 source, or existing report was changed by this task.

The local `rea` checkout is outside the active application repository at the
workspace parent and remains untouched on:

```text
branch: main
HEAD: e13da084e7c5967b80632ae5ad8d3d37bcf57469
```

No downloaded plugin source, package script, worker, MCP server, subprocess,
import of managed plugin code, dependency installation, or runtime activation
was performed.

## 13. Required next steps before signed-catalog enforcement

Phase 2 must make the following operational facts explicit before production
code can return a curated `catalog_approved` result:

1. Define the authenticated catalog transport and release/operator distribution
   channel for the initial public root key.
2. Establish the accountable catalog owner, review process, signer scope,
   separation of duties, and audit trail.
3. Decide whether the root is single-key, delegated, or threshold and document
   the honest assurance level.
4. Publish a real signed catalog using the exact payload/envelope schema and
   bind repository, commit, archive, manifest, compatibility, and execution
   policy values.
5. Implement keyset rotation, expiry, revocation, emergency recovery, sequence
   rollback protection, and offline behavior.
6. Persist authenticated catalog evidence atomically in manager records without
   replacing existing integrity evidence.
7. Add end-to-end distribution, catalog refresh, CLI, TUI, rollback, offline,
   and incident-recovery tests.
8. Decide separately whether and how GitHub commit signatures, Sigstore, or
   in-toto evidence will be supported; do not imply those adapters exist.
9. Re-audit user-facing wording so every “verified” claim names the object and
   authority.

Until those decisions are implemented and verified, user-added repositories must
remain usable as explicitly unsigned/source-only metadata, and all execution
must remain disabled.
