# Aetherion GitHub Source Trust Architecture Decision Report

**Date:** 2026-10-17  
**Status:** Decision-ready architecture; implementation intentionally not started  
**Execution policy:** Option A / control-plane only / execution disabled  
**Scope:** GitHub source identity, commit and archive provenance, catalog authenticity, publisher evidence, and key custody

## 1. Executive decision

Aetherion should keep the current metadata-only policy and add source trust as a
layered, explicitly labelled set of claims. It should not introduce one boolean
`trusted` or `verified` value that conflates content integrity, catalog approval,
publisher identity, compatibility, and runtime safety.

The recommended target architecture is:

1. **Retain immutable content identity.** Resolve mutable refs to a full commit
   SHA, bind downloads to that SHA, verify the archive and extracted files, and
   retain the existing atomic registry/lifecycle protections.
2. **Add an independently bootstrapped signed curated catalog.** The catalog
   signs the canonical repository identity, exact commit, archive digest,
   manifest digest, compatibility facts, and policy decision. Its verification
   key must come from a release/operator trust root, not from the catalog being
   verified.
3. **Represent publisher evidence separately.** GitHub commit-signature
   verification, user-imported publisher keys, Sigstore, and in-toto attestations
   can provide evidence about who signed or built something. None of these
   automatically means that the source is safe or approved by Aetherion.
4. **Keep execution disabled.** A valid catalog signature or publisher signature
   must never enable imports, subprocesses, dependency installation, plugin
   entrypoints, MCP servers, workers, or any other runtime path.
5. **Use short-lived signed metadata and an explicit key lifecycle.** Key
   rotation, revocation, recovery, offline operation, rollback, and custody must
   be defined before a signature becomes a user-facing trust claim.

### Decision

Adopt a **signed curated catalog plus optional publisher evidence** as the
project architecture. Use an Ed25519/EdDSA-compatible signature profile with
RFC 8785 JSON Canonicalization Scheme (JCS), or an equivalent reviewed
canonical serialization, for the first implementation. Treat the exact
algorithm and library as an implementation decision subject to dependency and
platform review; do not treat algorithm selection as the trust model.

Do **not** use any of the following as the sole authenticity solution:

- a commit SHA or archive hash without an independently trusted expected value;
- first-use or local-only TOFU for a curated trust claim;
- an HMAC whose key is stored beside the catalog or registry;
- a public key read only from the same mutable catalog it authenticates;
- GitHub's `verified` field as proof that Aetherion approved the source;
- a successful ZIP extraction, manifest parse, compatibility result, or local
  integrity check as proof of publisher identity or benign behavior.

This report records the architecture and acceptance criteria only. It does not
implement signatures, key storage, catalog transport, publisher verification,
or runtime activation.

## 2. Current baseline and evidence

### 2.1 Existing guarantees

The audited manager already provides a strong content-integrity and lifecycle
baseline for Option A:

| Area | Current behavior | Security claim supported |
| --- | --- | --- |
| Repository identity | HTTPS GitHub repository roots are canonicalized; credentials, ports, queries, fragments, non-GitHub hosts, and non-root paths are rejected. | The accepted source identifier has a narrow, deterministic form. |
| Ref handling | Mutable refs resolve to a full commit SHA; curated entries can require a recorded verified commit. | A selected install can be bound to a specific commit rather than a moving branch. |
| Retrieval | The archive URL is built from the selected SHA, with bounded responses and controlled redirect handling. | The fetched object is constrained to the selected source/commit path. |
| Archive integrity | The archive is hashed; optional expected archive digests can be compared; ZIP members are normalized and checked for traversal, collisions, links, special files, and resource abuse. | The received archive and extracted tree can be checked for accidental or transit tampering. |
| Installed integrity | Registry records retain authoritative archive/per-file/integrity-manifest data; status compares the managed source to those records. | Post-install source tampering can be detected. |
| Lifecycle atomicity | Locking, atomic JSON writes, staging, transaction journaling, rollback, and startup recovery protect install/update/remove state. | Interrupted or concurrent lifecycle operations fail closed or recover consistently. |
| Compatibility | Explicit Aetherion manifests are validated; generic repositories remain source-only/unknown when no supported contract is declared. | Metadata compatibility is not inferred from arbitrary package metadata. |
| Runtime boundary | `execution_enabled` remains `false`; metadata, scripts, dependencies, entrypoints, and MCP/tool declarations are inert. | The current path does not execute downloaded source. |

Relevant implementation locations include:

- `cli/aetherion_cli/plugin_manager.py` — URL/ref handling, catalog and
  registry persistence, bounded retrieval, archive validation, manifest
  classification, integrity records, and the execution lock.
- `cli/aetherion_cli/main.py` — CLI discovery, inspection, lifecycle commands,
  and trust-related presentation fields.
- `cli/aetherion_cli/tui/state.py`, `tui/gateway.py`, and
  `tui/screens/plugins.py` — TUI state and metadata-only lifecycle paths.
- `cli/tests/test_plugin_manager.py`, `test_cli.py`, and
  `test_tui_plugins.py` — manager, CLI, TUI, adversarial, and lifecycle tests.
- `AETHERION_GITHUB_PLUGIN_MANAGER_REA_SECURITY_AUDIT_REPORT.md` — prior
  adversarial audit and evidence for the current integrity/lifecycle boundary.

### 2.2 Current trust gap

The local catalog and registry are JSON documents protected by atomic writes,
locking, and path checks, but not by an independent authenticity mechanism.
Those controls protect consistency and recoverability; they do not prove who
authored the data. A same-user or equally privileged process that can modify
the local files can modify the source, expected metadata, or trust-relevant
records together. A local HMAC would not solve that problem if the verifier can
also read or replace the HMAC key.

The current system also has no general publisher-authentication layer. A
correctly resolved commit can still be malicious, an upstream account can be
compromised, and a repository owner can intentionally publish harmful source.
GitHub commit verification, where present, is useful evidence but is not an
Aetherion policy decision and does not by itself bind the codeload archive,
manifest policy, or compatibility decision.

The prior security audit explicitly recorded this status: mutable custom refs
remain time-varying, GitHub API and other read-only observations can differ, no
signature/MAC trust system exists for the local registry, and execution is
intentionally unavailable.

### 2.3 Claims that must remain separate

The implementation and UI should distinguish at least these dimensions:

1. **Source identity:** which canonical GitHub repository was selected.
2. **Version identity:** which exact commit SHA was selected.
3. **Content integrity:** whether the fetched archive and managed files match
   expected digests.
4. **Catalog authenticity:** whether an Aetherion-approved catalog statement
   was verified against an independently trusted key.
5. **Publisher evidence:** whether an upstream signer or build attestation was
   verified and matched the configured identity policy.
6. **Compatibility:** whether the source declares a supported Aetherion
   contract.
7. **Policy approval:** whether Aetherion permits the source for the requested
   operation.
8. **Runtime safety:** whether any execution is allowed. In the current scope,
   this remains disabled regardless of the other values.

## 3. Scope, assumptions, and non-goals

### In scope

- GitHub repository canonicalization and immutable commit selection.
- Signed catalog structure and trust-root bootstrapping.
- Archive, manifest, and policy binding.
- Optional GitHub commit-signature, Sigstore, and in-toto evidence.
- Public-key distribution, enrollment, rotation, revocation, recovery, and
  offline behavior.
- User-visible trust states and fail-closed behavior.
- Migration of existing unsigned catalog/registry records.
- Tests and observability needed to make claims auditable.

### Assumptions

- HTTPS/TLS and the GitHub service are used for transport, but transport
  authentication is not treated as publisher authentication.
- A local administrator or process with equivalent ability to replace the
  application, its trust root, and its key store is outside the protection
  boundary unless a platform-specific protected deployment is selected.
- Source remains non-executable in the current release. The proposed trust
  layer must not be used as an implicit activation gate.
- A catalog signer can be operated by an Aetherion-controlled release or
  security process. If no such operator exists, the signed curated-catalog
  claim cannot honestly be offered.

### Non-goals

- Proving that source is benign, vulnerability-free, or safe to execute.
- Replacing GitHub, Git, Sigstore, in-toto, TUF, or the operating system's key
  management facilities.
- Making arbitrary user-added repositories curated or publisher-authenticated.
- Adding runtime permission enforcement or a sandbox.
- Protecting against a fully compromised host administrator.

## 4. Threat model and security boundaries

The useful question is not whether a source is simply “trusted.” The question
is which actor can change which statement, and which independently verifiable
evidence detects that change.

| Threat or failure | What can happen | Current control | Residual risk / required decision |
| --- | --- | --- | --- |
| Mutable branch or tag moves | A later inspection of `main` selects different source. | Resolve and record a full commit SHA; curated entries can pin a known commit. | Updates are still a policy choice. A moving ref must remain labelled mutable/uncurated; signed metadata needs sequence and expiry rules. |
| API/archive inconsistency | Metadata identifies one commit while the returned archive is different or corrupted. | Archive digest, bounded retrieval, and exact-SHA archive construction. | A first observed digest is not independently authentic. A curated signed record must bind the expected archive digest to the commit. |
| Network or cache corruption | Bytes are changed in transit or in a cache. | HTTPS plus archive/per-file hashes. | A digest detects alteration only when the expected digest is trusted. |
| Same-user catalog/registry tampering | An attacker changes source, expected digest, status, or policy records together. | Atomic writes, locks, journals, path confinement. | No current independent authenticity. Signed catalog verification root and append/rollback policy are required for curated claims. |
| Malicious catalog signer | A valid signer publishes a harmful or wrong entry. | Not currently applicable. | Key custody, separation of duties, review, expiry, revocation, and incident response are required. A valid signature is not a malware verdict. |
| Compromised GitHub owner/account | A legitimate repository publishes a malicious commit. | Commit pinning and content/integrity evidence. | Publisher identity alone is insufficient. Use review/policy approval and optional build/source attestations; do not claim benign source. |
| Forged or misleading Git commit signature | A commit is signed by an unexpected or weakly trusted identity. | No general publisher-verification path today. | Verify signer identity against an independently configured policy, not merely GitHub’s display or `verified` field. |
| Malicious metadata or scripts | Manifest fields claim safe capabilities or include package scripts. | Metadata-only path; no import, subprocess, package manager, or lifecycle execution. | Preserve inert parsing. Future execution would require a separate sandbox/permission architecture. |
| ZIP path/resource attack | Archive escapes the managed root or exhausts resources. | Normalization, collision checks, link/special-file rejection, bounded sizes/counts, staging confinement. | Keep these controls independent of signatures; signed malicious archives remain malicious. |
| Unknown key or changed trust root | A catalog is signed, but the verifier has no independently trusted key or the key changed unexpectedly. | No current signature system. | Fail closed for `catalog_approved`; offer explicit source-only inspection only. Enrollment and key rotation must be explicit. |
| Key compromise | A signing key can create apparently valid metadata. | Not currently applicable. | Revoke key via a trusted keyset, expire catalogs quickly, preserve last-known-good evidence, and document emergency recovery. |
| Key loss | A legitimate signer cannot produce updates. | Not currently applicable. | Use offline recovery keys/threshold custody; do not silently generate a new trust root. |
| Replay or rollback | An old signed catalog reintroduces a vulnerable or withdrawn commit. | No signed sequence policy today. | Bind monotonically increasing sequence/version and expiry; persist highest accepted sequence where policy requires it. |
| Offline operation | No network is available during inspection or update. | Local registry and source can be inspected. | Cached signatures may verify only while within policy validity; expired or unknown metadata must be visibly stale, never fresh-approved. |
| Privileged host compromise | Administrator replaces application, trust root, source, or key store. | Outside the current host boundary. | Use signed releases, platform policy, and external operational controls; do not overstate local keychain protection. |

### 4.1 Trust boundaries

The proposed architecture has four distinct boundaries:

1. **Transport boundary:** GitHub API/codeload and optional evidence services.
   TLS authenticates the service endpoint for transport; it does not authenticate
   the source publisher to Aetherion.
2. **Content boundary:** the selected commit, archive digest, manifest digest,
   and per-file hashes. This is the manager's current integrity boundary.
3. **Policy boundary:** the signed Aetherion catalog and its independently
   bootstrapped keyset. This answers whether Aetherion approved a precise
   source snapshot under stated policy.
4. **Publisher/evidence boundary:** upstream signatures or build attestations.
   This answers a narrower identity/provenance question and must not silently
   replace the policy boundary.

The local filesystem is a persistence boundary, not an independent authority.
Atomicity and locking make state changes recoverable; they do not make a local
file authoritative against a process that can rewrite both data and metadata.

## 5. Alternatives considered

| Option | What it proves | Strengths | Weaknesses | Decision |
| --- | --- | --- | --- | --- |
| Commit SHA plus local hashes | The current bytes match a selected identity or previous observation. | Already implemented, simple, offline-friendly, useful against corruption. | Does not authenticate who selected the SHA or who controls the repository; expected values can be rewritten with local state. | **Retain as integrity layer, not authenticity.** |
| TOFU / first-use approval | The current user approved the first observed identity and later changes are detectable. | Low deployment cost; useful for personal, explicitly uncurated sources. | First observation is vulnerable; local approval is not an independent root; poor recovery and fleet governance. | **Permit only as explicit user policy, never as curated approval.** |
| User-imported publisher key | A source signature verifies against a key the user intentionally installed. | Clear per-user authority; supports private or independent publishers. | Key distribution, rotation, identity binding, and UI are user responsibilities; does not prove source safety. | **Support as optional publisher evidence.** |
| Signed curated catalog | Aetherion approved an exact repository/commit/archive/policy tuple. | Central policy, reproducible decisions, good offline verification, clear UI semantics. | Requires operational signer, root distribution, rotation, expiry, and review. | **Recommended project-level trust layer.** |
| GitHub commit signatures | Git records a signature associated with a commit signer. | Native to upstream workflows; useful corroborating evidence. | GitHub verification is not Aetherion approval; commit signature may not bind archive/policy; signer account can be compromised. | **Optional evidence, not the root decision.** |
| Sigstore / in-toto evidence | Artifact identity, signer identity, build steps, or provenance according to the evidence type. | Strong ecosystem standards; transparency and identity options. | Not every source archive has an attestation; identity/policy mapping is still required; online roots/logs add dependencies. | **Future evidence adapter; do not make a current claim without a verified artifact.** |
| Local HMAC | Data was produced by a holder of a shared secret. | Efficient and can detect accidental local edits when key storage is separate. | If key and catalog share the same attacker boundary, it adds no independent authority; poor multi-writer distribution. | **Reject as universal catalog authenticity.** |
| TUF-style delegated metadata | Signed, versioned, expiring metadata with roles and rollback protection. | Mature model for key rotation, delegation, expiry, and rollback resistance. | More metadata roles and operational complexity than the current catalog needs. | **Use its design principles; adopt full TUF only if distribution scale warrants it.** |

### 5.1 Why the recommendation is layered

No single mechanism answers all of the relevant questions:

- A commit hash identifies content history but not an approving authority.
- A publisher signature identifies a signer but not Aetherion's compatibility or
  release policy.
- A catalog signature expresses policy but does not prove that the publisher's
  account was uncompromised.
- A build attestation describes a build only if the subject and build identity
  are correctly bound to the selected source.
- A local keychain protects a secret or trust setting only within its host
  boundary.

The UI and APIs should therefore expose a vector of facts, with a derived
policy result that is conservative and explainable.

## 6. Recommended trust model

### 6.1 Trust levels and states

Use machine-readable dimensions rather than a single badge. Suggested values
are deliberately conservative:

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

Examples:

- A generic user URL with a verified archive is
  `integrity_status=verified`, `catalog_auth_status=unsigned`,
  `publisher_auth_status=not_checked`, `compatibility_status=source_only`,
  `policy_status=unreviewed`, `execution_status=disabled`.
- A curated entry with a valid catalog signature and matching bytes is
  `integrity_status=verified`, `catalog_auth_status=verified`,
  `policy_status=catalog_approved`; publisher evidence may still be
  `not_checked`.
- A previously approved entry whose signature is valid but whose expiry has
  passed is `catalog_auth_status=expired` and `policy_status=stale`, not
  `catalog_approved`.
- A digest mismatch is always `integrity_status=mismatch` and must not be
  hidden by a valid catalog or publisher signature.

The word **verified** should be qualified in user-facing text: “archive
integrity verified,” “catalog signature verified,” or “publisher signature
verified.” Bare “verified plugin” is prohibited.

### 6.2 Policy rules

The first implementation should apply these rules:

1. **Curated install/update:** require a valid, non-expired catalog statement
   whose repository, commit, archive digest, manifest digest, and policy fields
   match the fetched content. Unknown, invalid, expired, revoked, or replayed
   catalog metadata cannot produce `catalog_approved`.
2. **Custom source inspection:** allow metadata-only inspection and optional
   installation as explicitly uncurated/source-only, subject to existing
   integrity and archive safety checks. Display the missing authenticity facts.
3. **Existing installed source:** retain the current local integrity checks. A
   catalog-authenticity failure changes the trust status; it does not authorize
   destructive deletion or silently promote the source.
4. **Updates:** do not follow a mutable branch merely because the previous
   entry was approved. Resolve the new commit, require a new matching signed
   statement, and record the transition.
5. **Execution:** remain disabled for every state in this report.

## 7. Trust roots, key custody, and lifecycle

### 7.1 Recommended trust-root hierarchy

The verifier must know the catalog verification key before it reads the catalog
as an authority. The recommended hierarchy is:

```text
release/operator trust root
        |
        +-- signed catalog-role keyset (key IDs, validity, scopes, status)
                    |
                    +-- online/offline catalog signing key
```

Recommended operational properties:

- The initial root public-key fingerprint is distributed through a separately
  authenticated Aetherion release, operator configuration, or other channel
  outside the catalog payload. A catalog must never introduce its own first
  trust root merely by naming a key.
- The root private key is offline and access-controlled. A small project may
  start with one offline root and a separately stored catalog signing key, but
  the operational decision must state who can sign and who reviews entries.
- The catalog signing key is scoped to catalog metadata, not release binaries,
  application updates, or arbitrary source execution.
- A future higher-assurance deployment can use a threshold root and delegated
  roles in the style of TUF. The first implementation must not imply threshold
  security if it has only one signer.
- Public keys and key IDs are not secrets. Private signing keys must not be
  stored in the repository, catalog, application logs, or user catalog.

### 7.2 Key enrollment and distribution

Enrollment is a security decision, not a parsing detail. The implementation
must support at least these paths:

1. **Built-in/release root:** a release contains the expected root key
   fingerprint or keyset under the project's normal release-integrity process.
2. **Explicit operator override:** an administrator can install a documented
   keyset through a separate configuration channel and sees the old/new
   fingerprints before acceptance. This must be an explicit operation, not an
   automatic replacement after verification failure.
3. **User publisher key:** a user can import a publisher verification key and
   bind it to a canonical repository or publisher identity. The resulting state
   is user-approved evidence, not Aetherion curated approval.

The catalog itself may carry the current keyset and rotation metadata only
after the existing trust root verifies the keyset transition. A key ID, GitHub
username, email address, or repository owner string is not a trust root.

### 7.3 Rotation, revocation, and recovery

- **Routine rotation:** publish a signed keyset transition before the old key
  expires. Require the old root/delegation and the new key to be bound by the
  configured rotation policy; do not accept a same-name replacement.
- **Compromise:** revoke the affected key in trusted keyset metadata, reject
  new catalogs from it, and issue a new catalog under a recovery key. Record
  the incident and affected catalog sequence range.
- **Loss:** use an offline recovery key or documented operator recovery. Never
  silently create a new root because a signing key is unavailable.
- **Expiry:** use short catalog validity periods and a longer-lived root/keyset
  validity. Expiry must be visible and must not be converted into “unverified
  but probably okay” for curated updates.
- **Rollback:** persist the highest accepted catalog sequence (where a trust
  policy requires it) and reject older sequences unless an explicit, signed
  rollback authorization exists. A filesystem restore must not silently revive
  a withdrawn catalog.
- **Offline recovery:** retain the last-known-good signed catalog and keyset.
  Offline verification can prove an already cached statement, but it cannot
  prove freshness after expiry or revocation data becomes unavailable.

### 7.4 Private-key custody

Catalog signing should occur outside the CLI process and outside developer
workstations where practical. Preferred custody, in descending assurance, is a
hardware-backed signing service or token, an offline encrypted operator key
with documented access, and only as a lower-assurance fallback a protected
local key file. The CLI verifies public material; it does not need the catalog
private key.

If the application later stores private publisher keys, use the platform secret
store (for example, Windows DPAPI-backed protection or the freedesktop Secret
Service) and clearly document the host-admin boundary. Public trust anchors do
not require secret storage. The key-custody choice must be recorded as an
operational control rather than implied by a Python file permission.

## 8. Signed catalog format and binding

### 8.1 Canonical payload

The signed payload must bind every fact on which the catalog approval depends.
Do not sign a display name or mutable URL alone. A version-1 conceptual
payload is:

```json
{
  "format": "aetherion.catalog.payload.v1",
  "catalog_id": "aetherion-curated",
  "sequence": 42,
  "issued_at": "2026-10-17T12:00:00Z",
  "expires_at": "2026-11-16T12:00:00Z",
  "entries": [
    {
      "plugin_id": "rea",
      "repository": {
        "canonical_url": "https://github.com/morluto/rea",
        "owner": "morluto",
        "name": "rea"
      },
      "selection": {
        "commit_sha": "4e14452ddbf4642235c8a68435f72837510b2075",
        "ref_policy": "curated_commit_only"
      },
      "archive": {
        "format": "github-codeload-zip",
        "sha256": "<64 lowercase hex characters>",
        "size_bytes": 0
      },
      "manifest": {
        "path": "package.json",
        "sha256": "<64 lowercase hex characters>",
        "schema_version": "source-only-or-supported-version",
        "plugin_api_version": ""
      },
      "policy": {
        "compatibility": "source_only",
        "execution_enabled": false,
        "publisher_evidence": "optional",
        "review_status": "approved"
      }
    }
  ]
}
```

Required invariants:

- `canonical_url` is the manager's canonical HTTPS GitHub repository root;
  user-provided spelling or aliases are not signed as separate identities.
- `commit_sha` is a lowercase 40-hex full commit ID. Mutable refs may be
  retained as descriptive provenance, but they are not the identity.
- `archive.sha256` and `archive.size_bytes` bind the exact downloaded object;
  the manager still recomputes them and checks the expected values.
- `manifest.sha256` binds the selected manifest bytes, not only parsed fields.
  The installed file path must be validated against the extracted root.
- Compatibility and `execution_enabled` are signed policy facts but remain
  subject to local hard safety gates; a catalog cannot turn execution on in
  Option A.
- `sequence`, `issued_at`, and `expires_at` support replay and freshness
  decisions. Clock handling must be bounded and observable.
- Entries should be deterministically ordered by `plugin_id`; duplicate IDs,
  duplicate repository/commit tuples, unknown critical fields, and ambiguous
  numeric/string encodings must be rejected according to the schema policy.

### 8.2 Signature envelope

The signature should be over the exact JCS serialization of the payload, with a
small envelope such as:

```json
{
  "format": "aetherion.catalog.envelope.v1",
  "key_id": "catalog-2026-01",
  "algorithm": "Ed25519",
  "canonicalization": "RFC8785-JCS",
  "payload_sha256": "<64 lowercase hex characters>",
  "payload": { "...": "canonical payload above" },
  "signature_base64": "<signature bytes>"
}
```

The verifier must canonicalize the payload itself, recompute
`payload_sha256`, check the signature with the independently trusted key,
validate sequence/time/key scope, then validate each entry. Pretty-printing,
JSON key order, whitespace, or a transport wrapper must not change the signed
meaning.

If a detached signature or a TUF/in-toto/Sigstore envelope is selected later,
the same binding requirements still apply: source identity, exact commit,
archive digest, manifest/policy facts, signer identity, validity, and evidence
subject must be unambiguous.

### 8.3 Evidence binding

Optional publisher evidence should be stored as evidence, not silently promoted
to catalog approval. A normalized record can include:

```json
{
  "type": "github_commit_signature | sigstore_bundle | intoto_attestation",
  "subject": {
    "repository": "https://github.com/morluto/rea",
    "commit_sha": "4e14452ddbf4642235c8a68435f72837510b2075",
    "archive_sha256": "<optional when the evidence covers the archive>"
  },
  "identity": "<normalized signer or issuer identity>",
  "verification": "verified | invalid | unknown",
  "evidence_digest": "<digest of retained evidence>"
}
```

Evidence is valid only when its subject exactly matches the selected source
object and its identity matches an explicit policy. A Git commit signature that
does not cover the archive digest cannot be presented as an archive signature.

## 9. Verification workflow and failure behavior

The order below prevents a later, weaker fact from overriding an earlier
failure:

1. **Canonicalize the request.** Validate the HTTPS GitHub repository root and
   reject unsupported URL forms before network or filesystem activity.
2. **Resolve the selection.** For a full SHA, use it directly. For a branch or
   tag, resolve it to a full commit and preserve both the requested ref and
   resolved SHA as provenance. Never call a mutable ref immutable.
3. **Load the applicable policy.** Determine whether the request is curated,
   explicitly user-approved, or uncurated. Do not infer catalog authority from
   a local entry merely because it has the same plugin ID.
4. **Verify catalog authority first.** Load the independently trusted keyset,
   validate the envelope/schema/signature/key scope, check sequence and time
   policy, and select the entry matching the canonical repository and commit.
5. **Fetch bounded metadata/archive.** Use the existing transport, redirect,
   size, content-type, and resource controls. The archive URL is derived from
   the selected commit rather than from a mutable ref.
6. **Verify content identity.** Compare archive size/digest to the signed
   expected values when curated; extract only after archive safety checks; hash
   the required manifest and files; compare registry data for installed content.
7. **Verify optional publisher evidence.** Only after the subject is known,
   validate a GitHub signature, Sigstore bundle, or in-toto attestation and
   apply the configured identity policy. Keep the result in its own field.
8. **Classify compatibility and policy.** Parse the manifest using the existing
   conservative rules. A signed catalog does not override a local mismatch,
   malformed manifest, or execution hard gate.
9. **Persist an evidence record atomically.** Store selected source, commit,
   digests, catalog sequence/key ID, evidence result, policy result, and
   timestamps. Preserve enough detail for status and audit output.
10. **Present the result.** Use qualified statuses and actionable reason codes.
    A failure must not be rendered as a generic “untrusted” message that hides
    whether the cause was an unknown key, expiry, replay, digest mismatch, or
    compatibility issue.

### 9.1 Fail-closed rules

- Invalid signature, unknown key, revoked key, expired catalog, sequence rollback,
  malformed signed payload, or entry mismatch cannot produce
  `catalog_approved`.
- Archive, manifest, or per-file digest mismatch cannot produce `integrity=verified`
  even when the catalog signature is valid.
- A valid signature cannot authorize execution, dependency installation, or
  entrypoint discovery beyond inert metadata parsing.
- If evidence cannot be fetched, retain `publisher_auth_status=not_checked` or
  `unknown`, not `verified`. Offline cached evidence must retain its age and
  validity window.
- A corrupted local registry or trust store must be reported as corruption and
  must not be repaired from an unverified source automatically.
- Deletion or rollback must not be used as an implicit response to a trust
  failure; preserve evidence and require an explicit lifecycle action.

### 9.2 Reason codes and observability

Stable reason codes should be emitted in CLI/TUI status and structured logs:

```text
catalog_signature_missing
catalog_signature_invalid
catalog_key_unknown
catalog_key_revoked
catalog_expired
catalog_sequence_rollback
catalog_entry_missing
catalog_subject_mismatch
archive_digest_mismatch
manifest_digest_mismatch
installed_file_digest_mismatch
publisher_signature_missing
publisher_identity_unknown
publisher_evidence_invalid
publisher_subject_mismatch
compatibility_unknown
compatibility_incompatible
source_only_execution_disabled
trust_store_corrupt
```

Audit records should include the canonical URL, requested ref, selected commit,
archive digest/size, manifest digest, catalog ID/sequence/key ID, evidence
type/identity, policy result, reason code, and timestamp. Do not log private
keys, access tokens, or complete untrusted payloads without bounded/redacted
handling.

## 10. Migration and compatibility

The current catalog and registry records predate signed trust metadata. Migration
must preserve their integrity/lifecycle behavior while refusing to invent
authenticity:

1. Add optional versioned trust fields. Missing fields map to
   `catalog_auth_status=unsigned` or `unknown`, never `verified`.
2. Recompute current installed archive/per-file/manifest integrity using the
   existing authoritative registry records. Do not rewrite expected values from
   a changed source during migration.
3. Mark existing curated records as requiring signed-catalog refresh before a
   new curated update. Existing source-only inspection remains available.
4. Preserve requested refs, selected commit, install timestamps, and prior
   lifecycle transaction data. A migration must not silently update a mutable
   branch or replace an installed tree.
5. If a signed catalog is unavailable, expose the entry as stale/unsigned and
   keep `execution_enabled=false`. Do not automatically downgrade a policy
   failure into approval.
6. Make the migration idempotent and recoverable under the existing lock and
   journal model. A failed trust-record migration must leave the prior registry
   readable or fail closed with an actionable repair path.
7. Keep old clients from accepting new signed records as trusted merely because
   they ignore unknown fields. A signed catalog rollout therefore requires a
   minimum client/policy version or a server-side audience/version constraint.

Rollback of the implementation itself must not roll back the trust root or
accepted catalog sequence without an explicit operator action. Rollback of an
individual plugin remains a content/lifecycle operation and must be evaluated
against the catalog policy for the target commit.

## 11. Acceptance criteria and test plan

The future implementation is not complete until these cases are automated and
the user-visible state is asserted:

### Trust-root and signature tests

- Valid payload/signature verifies with the bootstrapped key.
- Same payload with one changed byte fails.
- Signature from an unknown key fails with `catalog_key_unknown`.
- Signature from a revoked key fails with `catalog_key_revoked`.
- Expired payload is `catalog_auth_status=expired`.
- Future-issued payload outside clock-skew policy is rejected or explicitly
  marked not-yet-valid.
- Malformed canonical JSON, duplicate IDs, unsupported critical fields, wrong
  algorithm, wrong key scope, and wrong payload digest all fail closed.
- Key rotation accepts the documented transition and rejects an unrelated
  same-name key replacement.
- Recovery key behavior is tested without modifying the built-in root silently.

### Subject-binding tests

- Changing canonical repository, owner, repository name, commit SHA, archive
  digest, archive size, manifest path, manifest digest, compatibility, or
  execution policy breaks catalog approval.
- A valid catalog for commit A cannot approve commit B or a mutable-ref result
  that resolves to commit B.
- Archive bytes with a valid source commit but wrong signed digest fail before
  install/promotion.
- A signed archive whose extracted manifest differs from the signed manifest
  digest fails closed.
- GitHub commit-signature evidence for a different repository or commit is
  reported as subject mismatch, not publisher verification.

### Local-state and lifecycle tests

- Editing catalog JSON, registry JSON, trust metadata, integrity manifest, or
  installed files produces the correct corruption/mismatch state.
- Replaying an older signed sequence is rejected when rollback protection is
  enabled.
- Interrupted trust-record publication recovers through the existing journal
  without losing an installed source or publishing partial approval.
- Concurrent inspect/install/update operations do not lose trust fields or
  downgrade a verified record through a stale write.
- Offline cached catalog verification works within validity and is visibly stale
  after expiry; no fresh approval is claimed offline.

### Policy/UI tests

- Unsigned custom sources remain inspectable as source-only and execution
  disabled.
- “Archive integrity verified,” “catalog signature verified,” and “publisher
  signature verified” are displayed separately.
- Bare “verified plugin” or equivalent conflated wording is absent.
- A valid catalog signature never changes `execution_enabled=false`.
- Each reason code is stable, actionable, and covered in CLI and TUI output.

## 12. Implementation plan (future work; not performed here)

The implementation should be staged so that every step remains independently
testable and preserves Option A:

### Phase 1 — Trust data model and verifier

- Add a small, pure verifier module for schema validation, JCS canonicalization,
  signature checking, key scope, validity, sequence, and reason codes.
- Add typed trust-state fields to manager records without changing execution
  behavior.
- Add fixtures for valid, altered, expired, unknown-key, revoked-key, replay,
  and subject-mismatch envelopes.

### Phase 2 — Signed curated catalog distribution

- Define the external catalog transport and built-in/operator root distribution.
- Sign the exact repository/commit/archive/manifest/policy tuple.
- Integrate verification before curated archive promotion and into inspect,
  status, update, CLI, and TUI output.
- Persist key ID, catalog sequence, validity, and evidence digests atomically.

### Phase 3 — Key lifecycle and operational controls

- Implement keyset rotation, revocation, expiry, recovery, and explicit trust
  anchor replacement.
- Document signer roles, review, custody, incident response, and offline
  procedures. Test with disposable keys; never place real private keys in the
  repository or test logs.

### Phase 4 — Optional publisher evidence

- Add adapters one at a time for GitHub commit signatures, then selected
  Sigstore/in-toto evidence where the subject and identity policy are clear.
- Keep each adapter's output separate from catalog approval and source safety.
- Add offline evidence verification only when the evidence format and root
  distribution are explicitly supported.

### Phase 5 — Operational review

- Run the complete CLI/TUI regression suite and adversarial trust tests.
- Perform a key-compromise/revocation rehearsal, offline-expiry rehearsal, and
  catalog rollback rehearsal.
- Re-audit wording, logs, documentation, and release packaging before making
  any “catalog approved” claim.

## 13. Permitted and prohibited security claims

### Permitted after implementation

- “The selected repository and full commit match the signed Aetherion catalog
  entry.”
- “The downloaded archive and installed files match the expected digests.”
- “The catalog signature was verified with key `<key_id>` under the configured
  trust root and was valid at `<time>`.”
- “Publisher evidence verified for `<identity>` and the exact subject shown.”
- “The source is classified as compatible/source-only according to the stated
  manifest policy.”
- “Execution remains disabled.”

### Prohibited unless a separate, demonstrated control exists

- “This plugin is safe, benign, secure, or malware-free.”
- “GitHub verified the plugin,” when only a GitHub API field was observed.
- “The commit signature proves the archive is approved by Aetherion.”
- “The archive hash proves who published the source.”
- “The catalog is tamper-proof” when the attacker can replace the application,
  trust root, or host administrator boundary.
- “Verified” without naming the verified object and authority.
- “Execution is safe” or any implication that signing enables execution.

## 14. Open decisions required before implementation

These choices must be made by an accountable owner; they should not be hidden
inside a library default:

1. Who owns and reviews the Aetherion curated catalog?
2. Where is the root public key distributed independently of the catalog?
3. Is the initial root single-key, threshold, or delegated, and what assurance
   level is honestly claimed?
4. What exact repository/publisher identities qualify for optional publisher
   evidence?
5. What catalog validity period and clock-skew allowance are acceptable?
6. Is an expired cached catalog allowed for inspection only, or for no new
   operation at all?
7. How is the highest accepted sequence persisted and recovered after backup
   restore?
8. Which key custody method is required for the catalog signer?
9. What is the emergency revocation/recovery contact and runbook?
10. Is the first release limited to a signed curated catalog, or does it also
    promise a GitHub/Sigstore/in-toto publisher-evidence adapter?
11. What user-facing wording and support workflow explain unsigned custom
    sources without suggesting they are malicious?

## 15. References

The architecture relies on the following primary specifications or vendor
documentation. These references describe mechanisms and formats; they do not
automatically establish Aetherion's trust policy.

1. GitHub REST API — Get a commit:  
   <https://docs.github.com/en/rest/commits/commits#get-a-commit>
2. GitHub — About commit signature verification:  
   <https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification>
3. GitHub — Using artifact attestations:  
   <https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations>
4. The Update Framework specification:  
   <https://theupdateframework.github.io/specification/latest/>
5. RFC 8785 — JSON Canonicalization Scheme:  
   <https://www.rfc-editor.org/rfc/rfc8785>
6. RFC 8032 — Edwards-Curve Digital Signature Algorithm (EdDSA):  
   <https://www.rfc-editor.org/rfc/rfc8032>
7. Sigstore overview and security model:  
   <https://docs.sigstore.dev/about/overview/>  
   <https://docs.sigstore.dev/about/security/>
8. in-toto supply-chain framework:  
   <https://in-toto.io/>  
   <https://in-toto.io/docs/specs/>
9. Microsoft DPAPI API reference:  
   <https://learn.microsoft.com/en-us/windows/win32/api/dpapi/>
10. freedesktop Secret Service API:  
    <https://specifications.freedesktop.org/secret-service/latest/>

## 16. Audit evidence and change boundary

This report was prepared from the current workspace and read-only external
references. The implementation files, tests, protected D3 source, Git index,
and local `rea/` checkout were not changed for this architecture decision.

Baseline facts observed before writing this report:

- Application repository HEAD: `5c7bdcd4808a178aa677488f18b948a747f94193`
- Branch: `main`
- Protected `backend/routes/d3_features.py` SHA-256:
  `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c`
- Protected `.git/index` SHA-256:
  `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e`
- `.git/index.lock`: absent at baseline
- Existing tracked workspace change: `backend/routes/d3_features.py`
- The workspace already contains many untracked project artifacts and reports;
  they were preserved and not normalized, deleted, staged, or committed.

The only intended new artifact for this task is this report. No runtime trust
mechanism, key, signature, catalog, or execution behavior was added.
