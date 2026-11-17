# Aetherion Signed Curated Catalog Phase 2 Implementation Report

**Date:** 2026-10-17  
**Status:** Phase 2 bounded implementation complete; production trust-root rollout not provisioned  
**Execution policy:** Option A / control-plane only / execution disabled  
**Scope:** Signed curated-catalog transport, explicit trust-root loading, cache/LKG persistence, sequence and expiry enforcement, curated lifecycle binding, and CLI/TUI presentation

## 1. Executive result

Phase 2 is implemented by connecting the Phase 1 verifier to the existing
`PluginCatalogManager` without creating a second signature implementation or a
parallel source manager.

Implemented behavior:

- Signed-catalog endpoint configuration is explicit and has no developer or
  production default.
- Signed catalog transport requires HTTPS, rejects credentials/ports/query
  fragments, rejects redirects, requires `application/json`, and bounds the
  response to 2 MiB while streaming.
- Public verification keys are loaded only from an explicitly configured
  operator/release-provisioned keyset file.
- No catalog envelope can introduce or replace a verification key.
- A missing trust root produces an explicit unsigned/unconfigured state.
- Verified raw envelopes and sequence watermarks are persisted in an atomic,
  bounded last-known-good cache separate from the user alias catalog.
- Cache reopening re-verifies the raw signed envelope; cached status fields are
  not authoritative.
- Refresh failures preserve the last-known-good envelope and expose the refresh
  failure reason.
- Lower/equal catalog sequences are rejected for refresh and the accepted
  watermark survives restart.
- Expired cached catalogs are visible as expired and cannot authorize curated
  installation.
- Curated inspection reports signed-catalog state, while curated install/update
  requires an exact verified repository/commit/archive/manifest/policy match.
- User-added repositories remain explicitly uncurated and do not inherit
  approval from curated IDs, repository URLs, or local forged fields.
- CLI/TUI display archive integrity separately from catalog authentication,
  sequence, key, expiry, freshness, policy, and publisher state.
- `execution_enabled=False` remains hard locked in every path.

The current workspace has no provisioned production catalog endpoint or
independently provisioned production trust root. Consequently, the existing
curated `rea` entry remains unsigned/unapproved, and a curated install fails
closed with `catalog_trust_root_unconfigured` until those operational inputs are
provided.

No production private key, signed REA catalog, arbitrary production public key,
publisher-evidence adapter, TUF hierarchy, or runtime activation path was added.

## 2. Actual files changed

### Production implementation

- `cli/aetherion_cli/catalog_distribution.py`
  - Bounded HTTPS transport.
  - Redirect/content-type/status/size enforcement.
  - Atomic signed-envelope cache.
  - Last-known-good loading and sequence watermark.
  - Offline/cache freshness and refresh-failure state.
  - Integration wrapper around the Phase 1 verifier.
- `cli/aetherion_cli/trust_store.py`
  - Explicit configured public-key-set provider.
  - Keyset schema, algorithm/scope/revocation/encoding validation.
  - Trust-root fingerprint and provisioning-boundary reporting.
  - Explicit unconfigured and corrupt providers.
- `cli/aetherion_cli/trust_verifier.py`
  - Added Phase 2 reason codes.
  - Allowed an empty manifest schema version for source-only metadata while
    retaining bounded strict typing and policy validation.
- `cli/aetherion_cli/plugin_manager.py`
  - Constructs the signed-catalog service from explicit configuration.
  - Applies cached/verified catalog state to curated list/info/inspect/status.
  - Requires exact signed approval before curated promotion.
  - Re-verifies curated installed records from the signed cache rather than
    trusting persisted status fields.
  - Forces user catalog records to unsigned/unreviewed state and removes forged
    signed-catalog metadata.
- `cli/aetherion_cli/config/settings.py`
  - Adds `catalog_url` and `catalog_trust_root` configuration using the existing
    environment-over-config convention.
- `cli/aetherion_cli/config/__init__.py`
  - Re-exports the new catalog settings accessors/constants.
- `cli/aetherion_cli/main.py`
  - Adds catalog URL/trust-root configuration options.
  - Displays qualified catalog sequence/key/expiry/freshness data.
- `cli/aetherion_cli/tui/state.py`
  - Adds catalog issue/expiry/freshness/refresh-failure fields.
- `cli/aetherion_cli/tui/gateway.py`
  - Maps the new fields into TUI plugin state.
- `cli/aetherion_cli/tui/screens/plugins.py`
  - Displays archive integrity, catalog authentication, sequence, key, expiry,
    cache freshness, policy, and execution separately.

### Tests

- `cli/tests/test_catalog_distribution.py`
  - Signed transport, cache, LKG, sequence, expiry, HTTP status/redirects,
    content type, response size, cache corruption, curated exact binding, and
    forged user-record tests.
- `cli/tests/test_trust_store.py`
  - Explicit keyset loading, fingerprinting, missing-root, and malformed-keyset
    tests.
- `cli/tests/test_trust_verifier.py`
  - Supported-subset JCS vectors, UTF-16 ordering, safe integer boundaries, and
    surrogate rejection.
- `cli/tests/test_plugin_manager.py`
  - Updated curated REA behavior to assert unsigned inspection and fail-closed
    install without a signed catalog.

No existing report was overwritten. No backend, deployment, secret, `.env`,
protected D3 source, or local `rea` checkout was modified.

## 3. Catalog transport and cache architecture

### 3.1 Explicit configuration

The signed catalog endpoint and keyset path are separate settings:

```text
AETHERION_CATALOG_URL
AETHERION_CATALOG_TRUST_ROOT
```

They can also be stored as `catalog_url` and `catalog_trust_root` in the normal
CLI config file. Both default to empty. No URL, test endpoint, release channel,
owner identity, key, or keyset is embedded in the application.

The CLI exposes these settings through:

```text
aetherion config --catalog-url <https-url>
aetherion config --catalog-trust-root <path>
aetherion config show
```

The configured keyset path is an explicit operator/release provisioning
boundary. It is not automatically authenticated by the application. A same-
privilege attacker who can replace both the application and configured keyset
can replace the trust root; this is exposed as a documented limitation rather
than hidden behind a `trusted` label.

### 3.2 Transport

`CatalogTransport` enforces:

- HTTPS endpoint only.
- No endpoint credentials, explicit port, query, or fragment.
- HTTP 200 only.
- Redirects rejected; no redirect destination is followed.
- `Content-Type: application/json` with optional parameters accepted.
- Declared and streamed response size bounded by Phase 1's 2 MiB envelope limit.
- Bounded `httpx` timeout.
- User-Agent and JSON Accept headers.

The transport returns raw bytes. It does not parse, cache, or treat any field as
trusted before `verify_catalog()` succeeds.

### 3.3 Cache and last-known-good state

The signed cache is separate from the existing user alias file:

```text
<plugin-root>/signed_catalog_cache.json
<plugin-root>/.signed_catalog_cache.lock
```

The cache stores:

- exact raw envelope bytes, base64 encoded;
- catalog ID and sequence;
- issue/expiry times;
- verifier key ID and canonical payload digest;
- verification time;
- configured endpoint provenance;
- provisioned trust-root fingerprint;
- highest accepted sequence watermark.

The persisted status fields are not an authority. On restart, the raw envelope
is parsed and re-verified against the current explicit provider. The cache state
is written as one canonical JSON document through a unique temporary file and
atomic replacement, so a failed replacement leaves the previous last-known-good
document intact. The lock serializes cache reads/writes across processes.

The watermark and last-known-good envelope are intentionally co-located so an
ordinary failed write cannot publish one without the other. A fully privileged
local attacker who restores the entire cache file together with the application
can still roll back local state; this remains outside the host protection
boundary and is not claimed to be prevented.

## 4. Trust-root provider and provisioning status

The configured keyset format is:

```json
{
  "format": "aetherion.catalog.trust-root.v1",
  "keyset_id": "operator-selected-keyset",
  "keys": [
    {
      "key_id": "catalog-role-key",
      "algorithm": "Ed25519",
      "scopes": ["catalog"],
      "public_key_base64": "<32-byte public key>",
      "revoked": false
    }
  ]
}
```

`ConfiguredCatalogKeyProvider` validates the exact schema, duplicate key IDs,
algorithm, catalog scope, revocation flag, strict base64, and 32-byte Ed25519
key length. It reports a SHA-256 fingerprint of the canonical configured
keyset. `UnconfiguredCatalogKeyProvider` returns no key and produces an honest
unsigned/unconfigured state. A malformed configured file produces a fail-closed
corrupt provider; it is never partially loaded.

The provider has no code path for catalog-embedded key enrollment, key
replacement after signature failure, or first-use trust. Key rotation and
revocation are represented by keyset contents and status, but a production
rotation ceremony, independently authenticated release channel, and custody
workflow remain operational prerequisites.

### Current production status

Observed workspace configuration contains no provisioned catalog endpoint or
trust-root path. No production keyset was supplied or generated. Therefore:

```text
catalog_auth_status: unsigned
policy_status:       unreviewed
catalog_freshness:   unconfigured
execution_status:    disabled
```

for the current curated REA record. The application does not claim that REA is
catalog-approved.

## 5. Verification and content-binding order

### Catalog refresh

1. Check explicit endpoint and explicit trust-root provisioning.
2. Fetch bounded raw JSON bytes under the HTTPS/no-redirect/content-type policy.
3. Read the candidate catalog ID only to select the local sequence watermark;
   this preliminary value is not trusted.
4. Run Phase 1 envelope/schema/JCS/digest/key/signature/time/sequence checks.
5. Persist the exact verified envelope and evidence atomically only after all
   verification succeeds.
6. On failure, retain and re-verify the last-known-good cache. Surface the
   refresh failure reason separately from the cached verification result.

### Curated inspect/install/update

1. Resolve the curated short ID to its existing immutable protected commit.
2. Fetch and safely extract the archive using the existing bounded archive
   controls.
3. Calculate archive bytes/size, manifest path/hash/schema/API, and compatibility
   from the candidate source.
4. Obtain a fresh signed snapshot when configured, otherwise use a valid
   unexpired last-known-good cache.
5. Re-run Phase 1 subject verification against the exact candidate repository,
   commit, archive digest/size, manifest, compatibility, and policy.
6. Persist/promote only if the result is `catalog_signature_verified` and
   `policy_status=catalog_approved`.
7. Continue using the existing file-integrity manifest, registry atomicity,
   journal, rollback, and recovery paths.

A valid catalog signature cannot override an archive or manifest mismatch. A
failed verification occurs before source promotion and registry publication.

## 6. Curated versus user-managed source behavior

### Curated sources

The existing `rea` entry remains protected by its immutable commit pin, but it
is not cryptographically approved without a matching signed catalog. Inspection
shows the protected commit and unsigned trust state. Install/update fails with
an actionable error such as:

```text
Curated source 'rea' requires a valid signed catalog statement;
catalog_trust_root_unconfigured.
```

No managed source is deleted and no pin is changed on this failure.

When a test-only explicitly supplied key provider and signed fixture are used,
the exact curated archive/manifest/policy can authorize the metadata-only
install. The resulting record still has `execution_enabled=False`.

### User sources

User aliases continue to use the existing source-only lifecycle. They do not:

- inherit approval from a curated plugin ID;
- inherit approval from a repository URL;
- inherit approval from a matching commit;
- become curated because local JSON contains forged trust fields.

The user catalog and signed-catalog cache are separate files and authorities.
Legacy/user records without valid signed evidence map to unsigned/unreviewed
states and remain usable under existing archive/integrity policy.

## 7. Sequence, expiry, and offline policy

- A network candidate with sequence less than or equal to the accepted watermark
  is rejected as `catalog_sequence_rollback`.
- A failed refresh does not lower or erase the watermark.
- A valid unexpired cached envelope may be used for offline inspection and
  curated policy matching, but is labelled `catalog_freshness=cached`, not
  presented as a fresh network approval.
- An expired cached envelope is reported as `catalog_expired` with
  `catalog_freshness=expired` and cannot authorize curated promotion.
- Unknown/revoked/scope-mismatched keys do not become valid because a cached
  status field says `verified`.
- A malformed or truncated cache is `catalog_cache_corrupt` and is never
  repaired from unverified response data.
- The existing curated pin is not automatically rewritten when a signed catalog
  changes. A newer candidate must still be explicitly selected through the
  existing pin/update policy.

## 8. Canonicalization conformance

The Phase 1 constrained JCS implementation was extended with deterministic
supported-subset tests:

- property order independent of insertion order;
- supplementary Unicode key ordering by UTF-16 code units;
- nested objects, arrays, booleans, and UTF-8 strings;
- safe integer boundaries `±(2**53 - 1)`;
- out-of-range integer rejection;
- float rejection;
- lone-surrogate rejection.

The implementation remains explicitly scoped to the catalog schema's supported
value subset. It is not claimed to be a general arbitrary-JSON RFC 8785
library. No unsupported floating-point catalog field was introduced.

## 9. Adversarial and integration test coverage

The Phase 2 tests cover:

- valid signed catalog fetch and atomic cache persistence;
- cache restart and raw-envelope re-verification;
- missing/unconfigured root behavior;
- malformed keyset, invalid algorithm, duplicate/invalid key IDs, revocation,
  scope, encoding, and fingerprint behavior;
- HTTP status failures and redirect rejection;
- JSON content-type enforcement;
- declared/injected response-size limits;
- invalid signature refresh retaining last-known-good state;
- sequence rollback retaining the prior accepted sequence;
- expired cached catalogs not authorizing policy;
- corrupt cache not being treated as signed approval;
- simulated cache publication failure preserving last-known-good data;
- forged cached key/digest metadata being ignored in favor of the signed raw
  envelope and current provider;
- valid exact curated REA test fixture authorization;
- curated archive digest mismatch before promotion;
- unsigned legacy REA inspection and fail-closed install;
- forged user catalog trust fields being downgraded to unsigned/unreviewed;
- execution remaining disabled.

## 10. Verification results

Commands were run from:

```text
C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli\cli
```

### Full CLI test directory

Final full run:

```text
python -m pytest -o addopts="" -q tests
286 passed, 5 warnings in 22.27s
exit code: 0
```

The suite uses temporary roots and deterministic mocked responses. It performs
no live catalog or GitHub download. Existing warnings are Click deprecation
warnings from `tests/test_replay_cli.py` and do not concern catalog trust.

### Phase 2 focused commands

The following focused suites were run during implementation:

```text
python -m pytest -o addopts="" -q tests/test_catalog_distribution.py
24 passed

python -m pytest -o addopts="" -q tests/test_plugin_manager.py tests/test_catalog_distribution.py tests/test_trust_verifier.py
94 passed in 4.00s

python -m pytest -o addopts="" -q tests/test_trust_verifier.py tests/test_trust_store.py
37 passed
```

### Focused Ruff

```text
python -m ruff check aetherion_cli/trust_verifier.py aetherion_cli/trust_store.py aetherion_cli/catalog_distribution.py aetherion_cli/plugin_manager.py aetherion_cli/config/settings.py aetherion_cli/config/__init__.py tests/test_trust_verifier.py tests/test_trust_store.py tests/test_catalog_distribution.py --select E4,E7,E9,F,B,W,I001 --output-format concise
All checks passed!
```

Python compilation and import smoke checks passed for all modified production
modules. A broader Ruff run over the pre-existing large `main.py`/TUI files
still reports 53 historical findings outside the focused trust changes; those
were not normalized as unrelated cleanup.

## 11. Security claims supported by this phase

Phase 2 supports these precise claims:

- A configured catalog response is transported under an explicit HTTPS,
  no-redirect, JSON-content-type, bounded-body policy.
- A catalog is cached only after Phase 1 signature, schema, key, time, sequence,
  and digest verification succeeds.
- The raw envelope, not a local status flag, is the evidence re-verified after
  restart or offline use.
- A configured local keyset is validated for algorithm, catalog scope,
  revocation, encoding, and fingerprint before use.
- No missing/default/test key or catalog-embedded key is accepted as the
  production trust root.
- A lower/equal sequence cannot replace the accepted sequence under the local
  cache watermark policy.
- A failed refresh does not overwrite a valid last-known-good envelope.
- A stale/expired/offline state is qualified and does not authorize a curated
  promotion after expiry.
- A curated install/update is bound to the exact repository, commit, archive,
  manifest, compatibility, and execution policy in the signed statement.
- User source records cannot self-upgrade to curated approval through local JSON
  fields.
- Existing archive/file integrity, lifecycle atomicity, recovery, and execution
  isolation remain in force.

## 12. Security non-claims and remaining prerequisites

This phase does not claim:

- that the current curated REA source is production-approved;
- that a production catalog endpoint exists or is owned by a verified Aetherion
  organization;
- that the configured local trust-root file is protected from a same-privilege
  attacker who can replace the application and that file;
- that key rotation, revocation, recovery, custody, or separation-of-duties
  ceremonies exist in production;
- that a valid catalog signature proves source benignness, malware absence,
  vulnerability absence, publisher identity, or runtime safety;
- that GitHub commit signatures, Sigstore, or in-toto evidence are supported;
- that a cached catalog is fresh network evidence while offline;
- that sequence rollback is prevented against a privileged attacker who can
  restore the whole local state together;
- that signed metadata authorizes execution. Execution remains disabled.

Before production signed-curated-catalog enforcement can be called provisioned,
the accountable owner must supply and authenticate:

1. A real catalog endpoint and release/distribution channel.
2. An independently provisioned root public key or keyset fingerprint.
3. Catalog signer ownership, custody, review, rotation, revocation, and
   recovery procedures.
4. A real signed catalog containing the exact curated entries and archive
   digests.
5. Release/rollback and offline support policy for the keyset and catalog.
6. Operational monitoring and incident response for compromised keys or
   withdrawn catalog statements.

## 13. Protected-state evidence

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

Final verification must preserve those hashes and the absence of the index lock.
The local REA checkout remains outside the active application repository and
must remain on:

```text
branch: main
HEAD: e13da084e7c5967b80632ae5ad8d3d37bcf57469
```

No commit, push, reset, clean, Git index mutation, unrequested deletion,
deployment modification, secret, credential, `.env`, backend D3 source, or
existing report was changed. No plugin code, package script, dependency,
worker, MCP server, subprocess, or runtime was executed.
