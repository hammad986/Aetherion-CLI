# Aetherion GitHub Publisher Evidence Phase 4 Implementation Report

**Date:** 2026-10-17  
**Status:** bounded implementation complete; production publisher identity policy remains unconfigured  
**Execution policy:** Option A / metadata and source management only / execution disabled  
**Scope:** Optional GitHub REST commit-signature evidence for an exact canonical repository and immutable commit SHA

## 1. Executive result

Implemented a small, optional GitHub commit-signature evidence adapter without
changing Aetherion catalog approval, archive integrity, compatibility, policy,
or execution semantics.

The adapter:

- accepts only an existing canonical HTTPS GitHub repository identity and a
  full 40-character commit SHA;
- constructs the GitHub REST endpoint itself;
   - uses bounded, no-redirect, content-type-checked HTTP transport with a
     finite 60-second maximum timeout;
- handles 404, 401/403, rate limiting, 5xx, redirects, oversized bodies,
  malformed JSON, and subject mismatches distinctly;
- strictly parses the provider response and rejects duplicate JSON names and
  non-finite JSON constants;
- binds evidence to the exact repository and top-level response SHA;
- preserves GitHub's provider reason and separate Git author/committer and
  GitHub-account observations;
- records bounded signature and payload digests, not raw unbounded signature or
  payload text;
   - reports GitHub's `verified=true`/`reason=valid` as
     `provider_status=verified` only when non-empty signature and payload
     metadata are present;
   - reports unsigned or absent/empty signature metadata as observed unsigned
     evidence rather than treating it as verified;
- maps that result to `publisher_auth_status=unknown_identity` because no
  independently configured signer identity policy exists;
- reports `local_signature_status=not_performed` and never claims local
  cryptographic verification;
- stores evidence separately in `publisher_evidence.json`, keyed by exact
  repository and commit subject;
- marks cached evidence `cached` or `stale` based on a 24-hour freshness
  window;
   - exposes explicit CLI and TUI evidence actions without mutating catalog pins,
  registry state, source files, catalog approval, or execution policy.

The adapter does **not** implement GPG, SSH, or S/MIME local verification.
GitHub's REST response does not expose enough signer-key material and
independent identity policy for this phase to make that claim safely.

No Sigstore, in-toto, production key, private credential, production catalog,
publisher trust root, or plugin execution path was added.

## 2. Changed files

### New implementation

- `cli/aetherion_cli/publisher_evidence.py`
  - Exact subject model.
  - GitHub response/observation/provider/local-verification status models.
  - Bounded GitHub REST adapter.
  - Strict evidence record serialization and digesting.
  - Atomic, locked, subject-bound evidence store.

### Integration

- `cli/aetherion_cli/plugin_manager.py`
  - Optional adapter/store injection.
  - `check_publisher_evidence()` metadata-only operation.
  - Read-time evidence joins for inspect, info, installed status, and catalog
    listings.
  - Evidence kept separate from catalog and registry authority.
- `cli/aetherion_cli/main.py`
  - Adds `plugins evidence SOURCE [--ref REF]`.
  - Displays qualified provider, publisher, local-verification, freshness,
    catalog, policy, and execution fields.
  - Extends existing inspect/info/install output with evidence fields.
- `cli/aetherion_cli/tui/state.py`
  - Adds publisher-evidence view fields to `Plugin`.
- `cli/aetherion_cli/tui/gateway.py`
  - Maps evidence fields and adds an explicit evidence operation through the
    existing serialized plugin-operation path.
- `cli/aetherion_cli/tui/screens/plugins.py`
  - Adds a compact `Signature` action for saved records with an exact commit.
  - Displays evidence independently from catalog auth, policy, and execution.

### Tests

   - `cli/tests/test_publisher_evidence.py`
  - Adapter subject/endpoint/response/status tests.
  - Redirect, oversized body, content type, transport, malformed/duplicate
    JSON, provider reason, subject binding, cache freshness, store integrity,
     and manager non-mutation tests;
   - finite-timeout validation, absent/empty signature metadata, provider
     availability mapping, non-canonical commit URLs, adapter subject mismatch,
     cache write-size/record limits, and raw-ref no-archive/no-registry checks.
- `cli/tests/test_cli.py`
  - Adds CLI evidence output and manager-call coverage.
- `cli/tests/test_tui_plugins.py`
  - Adds gateway evidence-operation coverage.

No existing report was overwritten. No backend, deployment, `.env`, secret,
credential, protected D3 source, or `rea/` checkout was changed.

## 3. Evidence semantics

The implementation intentionally separates four dimensions:

### Observation status

```text
observed
not_checked
unsupported
unavailable
not_found
access_denied
invalid_response
```

These describe whether a bounded provider observation exists and whether it
could be retrieved and bound to the requested subject.

### GitHub provider status

```text
verified
unsigned
unverified
invalid
malformed
unsupported
unavailable
provider_error
unknown
```

The adapter maps GitHub reasons conservatively:

- `valid` plus `verified=true` → `verified`;
- `unsigned` → `unsigned`;
- `invalid` → `invalid`;
- `malformed_signature` → `malformed`;
- `unknown_signature_type` → `unsupported`;
- `gpgverify_unavailable` → `unavailable`;
- `gpgverify_error` → `provider_error`;
- `expired_key`, `not_signing_key`, `no_user`, `unverified_email`,
  `bad_email`, `unknown_key` → `unverified`;
   - unknown future reason → `unknown`.

Contradictory `verified`/`reason` fields are `invalid_response`, not repaired
or interpreted optimistically.

### Aetherion publisher-auth aggregate

The existing `PublisherAuthStatus` vocabulary remains separate from catalog
auth and policy:

- provider-reported valid signature with no identity policy →
  `unknown_identity`;
- provider-reported unsigned commit → `unsigned`;
   - provider-reported invalid/unverified/malformed signature → `invalid`;
   - provider-reported unsupported, unavailable, provider-error, or unknown
     result → `not_checked`;
- transport/not-found/access-denied/no-observation → `not_checked`.

`publisher_auth_status=verified` is not emitted by this first adapter because
there is no independently configured signer identity policy and no local
cryptographic verification. This is deliberate.

### Local verification and identity policy

Every result carries:

```text
local_signature_status=not_performed
identity_policy_status=unconfigured
```

The GitHub REST response exposes Git commit author/committer fields and
optional GitHub account mappings, but it does not expose a signer key identity
that can safely be treated as an Aetherion publisher identity. Display names,
emails, committers, and GitHub usernames are retained as observations only.

## 4. GitHub REST adapter

### Exact request subject

The adapter accepts:

```text
repository_url: https://github.com/owner/repository
commit_sha:     full 40-character immutable SHA
```

The existing `canonicalize_github_url()` validator is used through the
validated manager boundary. The endpoint is constructed as:

```text
https://api.github.com/repos/{owner}/{repository}/commits/{commit_sha}
```

Callers cannot supply an arbitrary API URL, branch, tag, query, fragment,
credential, alternate host, or partial SHA to the adapter.

### Transport controls

- Existing `httpx` dependency only; no new runtime dependency.
- HTTPS GitHub API endpoint constructed internally.
- `follow_redirects=False`.
- Finite timeout, validated and capped at 60 seconds.
- Streamed body with a 512 KiB maximum.
- `application/json` required.
- GitHub-style JSON accept and user-agent headers.
- Optional constructor token is sent only as a request header and is never
  persisted, logged, or included in evidence output.
- 404, 401/403, 429, 5xx, redirects, and non-200 responses remain distinct.

### Strict subject binding

The response must satisfy:

1. Top-level `sha` equals the requested full commit SHA.
2. Top-level `html_url` is an HTTPS GitHub commit URL for the requested
   repository and SHA.
3. The `commit.verification` object has the supported exact field set when
   present; a missing or empty signature/payload is recorded as observed
   unsigned evidence with `publisher_signature_missing`.
4. Verification booleans, reason, signature, payload, and verification time
   have bounded types and formats.
5. Git author/committer and GitHub-account observations are separate maps.

Evidence for one repository or commit cannot be used for another subject key.

## 5. Evidence persistence and freshness

Evidence is stored separately from all trust authorities:

```text
<plugin-root>/publisher_evidence.json
<plugin-root>/.publisher_evidence.lock
```

The store contains versioned records keyed by a SHA-256 subject key derived
from:

```text
canonical_repository_url + "\n" + full_commit_sha
```

Each record includes:

- provider and evidence type;
- exact repository URL and commit SHA;
- observation status and provider status;
- provider reason;
- bounded author/committer observations;
- identity-policy state;
- local-verification state;
- signature/payload digests when GitHub supplied those values;
- observation and provider verification timestamps;
- freshness;
- deterministic evidence digest.

The store rejects duplicate JSON names, non-finite JSON values, malformed
subjects/enums/timestamps/digests/identity maps, oversized state, more than
256 records, post-write size overflow, subject mismatches, and digest
mismatches. Writes are atomic and locked. Cached evidence is labeled:

- `cached` when observed within 24 hours;
- `stale` after the 24-hour evidence freshness window.

Evidence is observational metadata. It is never added to:

- `catalog.json`;
- `registry.json`;
- `signed_catalog_cache.json`;
- trust-root/keyset lifecycle state;
- plugin transaction journals.

The manager joins evidence at read time using the current exact repository and
commit. A same-user attacker who can rewrite the local evidence file remains
outside the local cache integrity boundary; evidence is not described as
tamper-proof or as a trust root.

## 6. Plugin manager, CLI, and TUI behavior

### Manager

`PluginCatalogManager.check_publisher_evidence()`:

- uses the curated immutable commit for a curated ID;
- uses the saved immutable pin for a saved alias without `--ref`;
- resolves a raw URL/ref to a commit without saving or changing a pin;
- calls only the injected/default evidence adapter;
- writes only the separate evidence store;
- returns provider evidence alongside existing catalog/integrity/policy state;
- hard-sets execution disabled.

The operation does not call curated approval, install, update, archive fetch,
dependency resolution, package scripts, or runtime activation. Existing
catalog and registry files remain unchanged.

Existing inspect/info/list/status projections re-join stored evidence by exact
subject. User catalog records cannot forge evidence fields because those fields
are stripped from user-controlled catalog records and loaded only from the
separate store.

### CLI

The new command is:

```text
aetherion plugins evidence SOURCE
aetherion plugins evidence SOURCE --ref REF
aetherion -o json plugins evidence SOURCE
```

Table output explicitly shows:

- repository and exact commit;
- evidence provider/type and observation;
- GitHub provider result/reason;
- aggregate publisher-auth status;
- identity policy and local-verification status;
- observation freshness;
- catalog-auth status and policy;
- execution disabled.

The CLI never calls a publisher result “Aetherion verified” and never maps it
to `catalog_approved`.

### TUI

Saved plugin records with an exact source and commit receive a compact
`Signature` action. The operation uses the existing gateway worker and plugin
operation state, then reloads authoritative manager state. Discovery results
do not automatically perform evidence requests.

The detail pane displays evidence type, observation, provider result/reason,
publisher auth, identity-policy state, local-verification state, and freshness
separately from catalog auth, policy, archive integrity, and execution. The new
action cannot save an alias, install, update, remove a source, or enable
execution.

## 7. Live read-only validation

One bounded live lookup was performed with the actual adapter and no token,
source download, plugin root, catalog mutation, or execution:

```text
Repository: https://github.com/morluto/rea
Commit:     4e14452ddbf4642235c8a68435f72837510b2075
```

The provider returned:

```text
observation_status:       observed
provider_status:          verified
provider_reason:          valid
publisher_auth_status:    unknown_identity
local_signature_status:   not_performed
identity_policy_status:   unconfigured
freshness:                fresh
```

This means only:

> GitHub reports this exact commit signature as verified.

It does **not** mean that Aetherion locally verified the signature, established
the signer identity, approved the catalog entry, verified the archive, deemed
the source safe, or enabled execution.

## 8. Adversarial tests

`cli/tests/test_publisher_evidence.py` covers:

- canonical repository and exact full-SHA validation;
- endpoint construction;
- valid provider-reported signature result;
- unsigned, invalid, unknown-key, and unsupported-format reason mapping;
- response SHA and repository binding;
- redirect, oversized response, wrong content type, rate-limit/unavailable
  response, and transport failure;
- duplicate JSON and contradictory verification fields;
- evidence digest and bounded signature/payload retention;
- cached and stale evidence;
   - duplicate/oversized/symlinked evidence-store state;
   - generated cache size and record-count limits;
   - adapter subject mismatch and raw mutable-ref no-archive/no-registry
     behavior;
   - manager evidence collection without catalog or registry mutation;
- evidence remaining separate from catalog approval and execution.

`cli/tests/test_cli.py` covers qualified evidence output and the manager call.
`cli/tests/test_tui_plugins.py` covers the gateway operation path. Existing
trust, catalog, lifecycle, manager, CLI, and TUI suites continue to pass.

## 9. Verification commands and actual results

The commands below were run from the application repository.

```text
python -m pytest cli/tests/test_publisher_evidence.py cli/tests/test_cli.py cli/tests/test_tui_plugins.py -q
```

Result: **exit 0**, **duration 17.927 seconds**, **84 passed**.

```text
python -m pytest cli/tests -q
```

Result: **exit 0**, **duration 30.023 seconds**, **324 passed**, with the five existing Click
`isolated_filesystem` deprecation warnings in replay CLI tests.

```text
ruff check --select E9,F cli/aetherion_cli/publisher_evidence.py cli/tests/test_publisher_evidence.py cli/aetherion_cli/plugin_manager.py
```

Result: **exit 0**, **duration 0.160 seconds** for the focused new adapter/manager scope. The larger
repository contains pre-existing unused-import/unused-variable findings in
`main.py`, `tui/gateway.py`, and legacy command definitions; they were not
rewritten as unrelated cleanup.

```text
python -m py_compile cli/aetherion_cli/publisher_evidence.py cli/aetherion_cli/plugin_manager.py cli/aetherion_cli/main.py cli/aetherion_cli/tui/state.py cli/aetherion_cli/tui/gateway.py cli/aetherion_cli/tui/screens/plugins.py cli/tests/test_publisher_evidence.py cli/tests/test_cli.py cli/tests/test_tui_plugins.py
```

Result: **exit 0**, **duration 0.549 seconds**.

```text
python -m aetherion_cli.main --help
python -m aetherion_cli.main plugins evidence --help
```

Both commands completed successfully. The main help lists the `plugins`
command group and the evidence subcommand help shows the optional `--ref`.

The repository-wide test command was not claimed as passed. As in prior
phases, `python -m pytest -q` returned **exit 2** after **24.513 seconds** and
was blocked during integration collection because
`backend/auth_system.py` attempts PostgreSQL at `127.0.0.1:5432`, which is not
running in this environment.

## 10. Production provisioning boundary and non-claims

This phase does not provision or assume:

- an Aetherion publisher identity;
- a GitHub account as an authorized signer;
- a GPG/SSH/S/MIME public key or keyring;
- a local cryptographic verifier for GitHub commit signatures;
- a signer-to-repository identity policy;
- a GitHub OAuth token or credential;
- a Sigstore or in-toto trust root;
- a production catalog endpoint or signed REA catalog;
- source safety, compatibility, archive integrity, catalog approval, or
  execution permission based on GitHub evidence.

The current workspace remains production-trust unconfigured. A provider result
is optional metadata only and cannot promote a user source or alter a curated
catalog decision.

## 11. Protected workspace evidence

Initial evidence captured before editing:

```text
Application HEAD: 5c7bdcd4808a178aa677488f18b948a747f94193
backend/routes/d3_features.py SHA-256: 592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c
.git/index SHA-256: 18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
.git/index.lock: absent
local rea branch: main
local rea HEAD: e13da084e7c5967b80632ae5ad8d3d37bcf57469
local rea working tree: clean
```

Final verification preserved the same protected hashes and state:

```text
Application HEAD: 5c7bdcd4808a178aa677488f18b948a747f94193
backend/routes/d3_features.py SHA-256: 592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c
.git/index SHA-256: 18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
.git/index.lock: absent
local rea branch: main
local rea HEAD: e13da084e7c5967b80632ae5ad8d3d37bcf57469
local rea working tree: clean
```

No plugin was executed, imported, installed, or activated. No package script,
dependency resolver, worker, MCP server, or subprocess was started for this
feature. No commit, push, reset, clean, Git-index mutation, protected-file
change, secret/configuration change, deployment change, or unrequested deletion
was performed.

## 12. Remaining operational next steps

1. If publisher identity authorization is required, define and provision an
   independent signer identity policy through an approved operational channel.
2. If local cryptographic verification is required, implement a separate
   format-specific adapter with explicit key distribution and identity policy;
   do not infer it from GitHub's REST `verified` field.
3. Keep GitHub evidence optional and separate from Aetherion catalog approval.
4. Run the repository-wide integration suite in an environment with PostgreSQL
   available.
