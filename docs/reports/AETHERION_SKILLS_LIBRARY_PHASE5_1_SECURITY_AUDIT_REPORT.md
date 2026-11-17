# Aetherion Phase 5.1 Principal Security Audit

**Scope:** Authenticated Skills Library publishing, tenant/workspace isolation,
approval, storage, lifecycle, closed runtime selection, CLI/TUI truthfulness,
inventory, and the Option A execution boundary.

**Audit date:** 2026-10-18  
**Audit type:** Adversarial, read-only source review with safe deterministic local
probes. This is not a full penetration test and does not establish that the
application is secure in every deployment.

## 1. Executive verdict

## **PARTIAL — important security guarantees remain unverified and material findings remain**

The narrow authenticated data-only publish path is substantially fail-closed in
the code inspected and in the isolated tests that were run:

- `POST /api/skills/publish` requires `token_required`, derives its owner from
  `g.user_id`, accepts content rather than a server path, computes a server-side
  SHA-256 digest, and starts published records disabled.
- Published records are keyed by `(skill_id, owner_id)` in the current
  `ExtensionStore`; list, get, enable, disable, detach, and selection lookups use
  the owner supplied by the authenticated route context.
- Permission-bearing published records start pending, approval is a separate
  admin-decorated operation, and approval does not grant tools or enable agent
  execution.
- `selected_skill_ids` is checked at task creation and resolved again by the
  runtime `SkillEngine`; duplicate, missing, cross-owner, pending, disabled, and
  permission-bearing published selections fail in the deterministic harness.
- The current Option A control-plane lock remains active. Publishing, approval,
  enablement, and selection did not flip `execution_enabled` and no execution,
  subprocess, plugin handler, package installation, or MCP server was started.

However, the audit found a **High** authenticated local-file disclosure and
legacy-install surface: `/api/skills/inspect` and `/api/skills/install` still
accept caller-supplied `path`/`url`/`source` values. The legacy `SourceFetcher`
reads arbitrary server-readable local files for the skill type. A safe local
probe passed `backend/routes/platform_extensions.py` to the authenticated
inspection route and received HTTP 200 with 31,249 bytes of that source as skill
instructions. This is not the data-only publish route, but it is an alternate
skill route in the same deployed application and violates the broader claim that
authenticated skill handling cannot make the server read arbitrary paths.

Other material gaps are:

1. **Role freshness is not immediate (Medium):** `admin_required` trusts the
   role claim already embedded in a signed JWT/session. A safe temporary-DB probe
   demoted an admin in the database and then used the pre-demotion token to
   approve a permission-bearing skill successfully (HTTP 200).
2. **Publish conflict handling has a race (Medium):** the manager performs a
   check-then-save. A deterministic two-thread probe made both writers observe
   absence; one then received an uncaught SQLite `IntegrityError` rather than the
   documented conflict response. A second deterministic interleaving showed that
   a different writer can enter the generic upsert path after the first commit.
3. **Production extension persistence is not PostgreSQL-backed (High production
   prerequisite):** `ExtensionStore` is a direct SQLite implementation and does
   not use `db.store_factory` or `PostgresStore`. The production compose file
   supplies PostgreSQL and Kubernetes runs two backend replicas, but the skill
   store has no demonstrated shared/durable PostgreSQL path.
4. **Corrupt extension metadata is fail-open (Medium hardening issue):** JSON
   conversion errors become empty arrays and runtime selection trusts the stored
   lifecycle/approval/enabled columns without recomputing or validating the
   content digest. A temporary-store probe changed the permissions JSON and hash
   to invalid values; selection still returned the record.
5. **There is no distinct workspace-membership authorization boundary:** the
   inspected architecture uses authenticated user ID as tenant/workspace scope.
   The code proves per-user record isolation, not that a user is a member of a
   requested workspace or that a workspace admin may administer another member's
   records. This is a deployment prerequisite and is a principal reason the
   verdict is PARTIAL, not PASS.

No Critical finding was confirmed. No cross-tenant published record read,
overwrite, approval, detach, or runtime-selection bypass was demonstrated for
the narrow owner-ID store boundary. That result must not be generalized to the
missing workspace-membership model or to a production multi-replica deployment.

## 2. Repository and protected-workspace baseline

Git metadata, rather than a guessed directory, identified the audited repository:

| Item | Initial observed value |
|---|---|
| Repository root | `C:/Users/mdham/Downloads/Aetherion-AI-main/end to end aetherion cli` |
| Git directory | `<repository root>/.git` |
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Tracked files | `1` (the checkout has a large pre-existing untracked tree) |
| Modified tracked paths | `backend/routes/d3_features.py` only |
| Staged paths | none |
| Untracked paths | `2,269` |
| `backend/routes/d3_features.py` SHA-256 | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |
| `.git/index` SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| `.git/index.lock` | absent |
| `rea/` under the audited root | absent |
| parent `rea/` checkout | branch `main`, HEAD `e13da084e7c5967b80632ae5ad8d3d37bcf57469`, clean |

The initial status output identified the pre-existing untracked application tree,
deployment files, reports, test tree, scratch material, and credentials/config
placeholders. Relevant pre-existing untracked reports included:

- `AETHERION_SKILLS_LIBRARY_INVENTORY_AND_IMPLEMENTATION_REPORT.md`
- `AETHERION_SKILLS_LIBRARY_PHASE5_1_AUTHENTICATED_PUBLISH_REPORT.md`
- the Phase 4 and 4R GitHub publisher implementation/security reports
- the broader `AETHERION_*`, `PHASE_*`, `FINAL_*`, `tests/`, `backend/`, `cli/`,
  `deploy/`, and `docs/` paths shown by the initial status

The pre-existing modified D3 file, the index, all untracked user work, prior
reports, deployment configuration, and credentials were preserved. The reported
historical sibling checkout at `rea/` was not present under the audited root;
the parent directory contains a separate `rea/` directory, but it was not
selected as the repository because Git metadata identified the current root
unambiguously. The parent `rea/` was not modified or used as a source of
evidence. Its final check remained branch `main`, HEAD
`e13da084e7c5967b80632ae5ad8d3d37bcf57469`, with a clean working tree.

## 3. Files inspected and call-graph summary

Primary implementation files inspected:

- `backend/routes/platform_extensions.py`
- `backend/routes/tasks.py`
- `backend/execution/extensions/store.py`
- `backend/execution/extensions/manager.py`
- `backend/execution/extensions/skill_engine.py`
- `backend/execution/extensions/fetcher.py`
- `backend/execution/extensions/models.py`
- `backend/execution/extensions/security.py`
- `backend/execution/agent_runtime/runtime.py`
- `backend/execution/agent_runtime/context.py`
- `backend/execution/control_plane_policy.py`
- `backend/auth_system.py`
- `backend/security.py`, `backend/middleware/guards.py`
- `backend/app.py`, `backend/db/store_factory.py`, and `backend/db/postgres_store.py`
- `backend/routes/workspace.py` and `backend/execution/durable_state.py`
- `cli/aetherion_cli/skill_library.py`
- `cli/aetherion_cli/main.py`
- `cli/aetherion_cli/tui/gateway.py`
- `cli/aetherion_cli/tui/screens/skills.py`
- `cli/aetherion_cli/tui/state.py`

Relevant tests and reports inspected:

- `tests/integration/test_phase5_1_published_skills.py`
- `tests/integration/test_phase3_extensions.py`
- `tests/integration/test_api_key_auth.py`
- `cli/tests/test_skill_library.py`
- `cli/tests/test_tui_state.py`
- `AETHERION_SKILLS_LIBRARY_PHASE5_1_AUTHENTICATED_PUBLISH_REPORT.md`
- `AETHERION_SKILLS_LIBRARY_INVENTORY_AND_IMPLEMENTATION_REPORT.md`

### Publish path

```text
CLI `skills publish <id>`
  -> local SkillLibrary.get(id, include_content=True)
  -> require the selected local definition to be installed
  -> Client.post('/api/skills/publish', content + metadata + client digest)
  -> auth_system.token_required
  -> platform_extensions._get_tenant() from g.user_id
  -> optional workspace_id must equal _get_workspace_scope()
  -> ExtensionManager.publish_skill_definition()
       -> bounded UTF-8/control validation
       -> restricted frontmatter parser
       -> canonical/reserved ID and permission validation
       -> authoritative SHA-256
       -> ExtensionStore read-then-save
  -> response status/outcome/execution_enabled=false
  -> CLI refreshes GET /api/skills/<id>
```

### Lifecycle path

```text
POST /api/skills/<id>/approve
  -> admin_required -> token_required
  -> g.user_id is both owner lookup and audit actor in the route
  -> ExtensionManager.approve_published_skill()
  -> ExtensionStore.set_skill_approval()
  -> APPROVED/PUBLISHED/is_enabled=0 + audit row

POST /api/skills/<id>/enable|disable|detach
  -> token_required
  -> owner = g.user_id
  -> manager/store exact owner operation
```

### Selection and runtime path

```text
POST /api/tasks
  -> token_required -> tenant_id = g.user_id
  -> validate selected_skill_ids is a list of strings
  -> ExtensionManager.resolve_runtime_skill_selection(tenant_id, ids)
  -> persist canonical IDs and current content_hash values in payload JSON
  -> durable_state.create(kind='agent_task', tenant_id=tenant_id)

RealAgentRuntime
  -> reads persisted selected_skill_ids
  -> SkillEngine.match_and_activate(... explicit_skill_ids=...)
  -> resolve_runtime_skill_selection() again with runtime tenant_id
  -> build bounded untrusted guidance
```

The runtime resolver rechecks current owner/state, but it does not compare the
persisted `selected_skill_digests` with the content currently returned by the
store. Current execution is blocked before the runtime is constructed.

## 4. Authentication, identity, tenant, and workspace authorization

### What is proven

The target publish, skill lifecycle, and task routes are decorated with
`auth_system.token_required`; the approval route uses `admin_required`, which
wraps `token_required`. JWT signature, type, expiry, token revocation, and ban
checks are present for bearer tokens. API keys are authenticated separately and
are explicitly rejected by `admin_required`. The authenticated Flask probe
observed:

```text
unauthenticated publish: 401 / UNAUTHORIZED
authenticated user 101 publish: 201 / published_to_tenant / execution_enabled=false
user 101 GET: 200 with owner_id=101
user 202 cross-owner GET: 404
workspace_id=202 supplied by user 101: 403
```

The publish route ignores caller-supplied `owner_id`, `user_id`, and
`published_by`; it passes the server-derived tenant value to the manager. The
route's optional `workspace_id` is only accepted when its string form exactly
matches `_get_workspace_scope()`, which currently returns the authenticated
tenant string.

The skill store's published reads use exact `(id, owner_id)` predicates. The
older plugin store retains an `owner_id = ? OR owner_id = ''` compatibility
fallback, but that fallback is not used by the published skill store methods
audited here.

### What is not proven

`_get_workspace_scope()` is not a membership lookup. There is no inspected
workspace-membership/access adapter, workspace role table, or request-time
authorization check that answers:

- whether user U is a member of workspace W;
- whether U's role in W permits publishing, approving, enabling, or detaching;
- whether a role change or membership removal is immediately respected;
- whether an admin in workspace A may administer a record owned by a member of
  workspace A without also being the record's `owner_id`;
- whether the same user belongs to multiple workspaces with different roles.

The current model is effectively **one user ID equals one tenant scope**. That
can be safe only if the deployed product contract truly has no shared
workspaces and no alternate tenant identity. It is not sufficient evidence for
a multi-workspace product. Request-supplied `workspace_id` is equality-checked
on publish, but the value is not resolved through membership. Task creation is
looser: it accepts a caller-supplied `workspace_id`/`session_id` and stores it
in the task payload without a workspace-membership check. Execution is disabled,
so no current workspace filesystem consequence was demonstrated; enabling
execution requires a separate review of this boundary.

### Role freshness finding

`_make_access_token()` embeds the role claim. `token_required` reloads ban and
token-revocation state, but it does not reload the current database role. The
`admin_required` wrapper compares `g.user_role` from the token/session claim to
`admin`.

A safe probe used a temporary SQLite auth database:

1. create user U and issue a token with `role=admin`;
2. publish U's permission-bearing skill;
3. update U's database role to `user` without changing the already-issued token;
4. call `/api/skills/role-demo/approve` with that token.

Observed result: HTTP 200, `status=approved_but_not_selected`, and an APPROVED
record. This is a confirmed role-revocation freshness gap. It does not cross a
different owner in the current route because approval also looks up the current
admin's owner ID, and Option A still blocks permission-bearing runtime
selection. It nevertheless violates the stated admin authorization contract.

## 5. Publish validation, persistence, digest, and conflict review

### Positive controls verified

`ExtensionManager._published_manifest()` enforces the lowercase canonical ID
pattern, reserved IDs, bounded text, a small frontmatter grammar, duplicate
frontmatter keys, forbidden capability fields, supported fields, UTF-8 decoding,
and the allowed permission enum. The server hashes the original content string
with SHA-256. A supplied client digest must be a 64-hex value equal to that
server digest; it is not used as the authoritative value. The route has a
rough 700 KiB request-length guard and the manager has a 512 KiB content limit.

The publish method does not accept a source path, URL, archive, package, command,
subprocess, or execution directive. It stores `install_path=""`,
`required_tools=[]`, `is_enabled=False`, `source_kind="authenticated_publish"`,
and `execution_enabled=False` in the response contract. Identical current
retries return `ALREADY_CURRENT`; a sequential different version/content
returns an `ExtensionSecurityException` conflict rather than replacing the
record.

### Validation limitations

The parser rejects ASCII control characters below 0x20 other than newline,
carriage return, and tab, but it does not reject all C1/DEL controls handled by
the local CLI validator. Request metadata can also disagree with optional
frontmatter `name`, `version`, or `description`: the request fields are used as
the canonical manifest values while the original frontmatter remains in the
hashed content. This is an integrity/UX hardening issue, not a demonstrated
tenant escape.

The manager checks for an existing record and then calls `save_skill()` in a
separate operation. `save_skill()` uses a primary-key constraint, but no atomic
compare-and-insert or database-native conflict clause is used for this publish
contract. The route catches `ExtensionSecurityException`, `ValueError`, and
`TypeError`, not `sqlite3.IntegrityError`.

### Deterministic race evidence

A temporary SQLite probe used two threads and a barrier so both calls passed the
manager's initial `get_skill()` absence check. One publish returned `CREATED`; the
other raised:

```text
IntegrityError: UNIQUE constraint failed: installed_skills.id, installed_skills.owner_id
```

The client therefore receives an internal failure rather than a deterministic
409 conflict. A separate deterministic interleaving used two temporary manager
instances: writer two entered the generic save path after writer one committed
and returned `CREATED` while the final record was version `2.0.0` with two
version snapshots. These results do not demonstrate a cross-tenant overwrite,
but they do demonstrate that the documented conflict policy is not an atomic
storage boundary.

### Transaction and audit atomicity

`save_skill()` commits the installed skill and version snapshot, then calls
`log_audit()` in a separate connection/transaction. A temporary store probe
replaced `log_audit()` with a synthetic failure: publish raised
`OperationalError`, the primary skill row remained persisted, and the audit row
count remained zero. This is a confirmed post-commit failure mode: callers can
see an error even though the record exists, and a successful record can lack its
audit event. It is an audit/transaction consistency gap rather than a false
HTTP success.

### Actual persistence backend

`ExtensionStore` imports `sqlite3`, defaults to
`backend/data/extensions.db`, and only recognizes the optional
`AETHERION_EXTENSIONS_DB` path. It does not call `db.store_factory`, does not
implement a PostgreSQL schema for installed skills or skill versions, and does
not use the application `DATABASE_URL` selection. The authentication and durable
task stores have PostgreSQL paths, but that does not make the extension store
PostgreSQL-compatible. The isolated Phase 5.1 tests prove SQLite behavior only.

The production compose configuration supplies PostgreSQL and a workspace volume;
the Kubernetes deployment declares two backend replicas. No shared extension
volume or extension PostgreSQL table was found in the inspected deployment
configuration. Therefore production durability, replica consistency, backup,
concurrent PostgreSQL behavior, and PostgreSQL row-level tenant isolation remain
unverified and are not claimed here.

## 6. Approval, enablement, detach, and lifecycle state machine

The intended state transitions are visible in the code:

| Operation | Result observed in source |
|---|---|
| publish without permissions | `PUBLISHED`, `NOT_REQUIRED`, disabled |
| publish with permissions | `PUBLISHED`, `PENDING`, disabled |
| approve | `PUBLISHED`, `APPROVED`, disabled, audit actor recorded |
| enable pending | rejected by store/route |
| enable approved metadata-only record | allowed by owner |
| runtime selection | requires current owner, `PUBLISHED`, `APPROVED` or `NOT_REQUIRED`, enabled; permission-bearing published records are rejected by policy |
| detach | exact owner delete of `authenticated_publish` record |

Publishing is not approval, enabling is separate, and the published flow never
registers a plugin handler or tool. Approval actor identity on the route is
server-derived from `g.user_id`. A request field, frontmatter field, or stored
description cannot grant a permission because frontmatter capability fields are
rejected and requested permissions are checked against `ALL_PERMISSIONS`.

The approval route has an important design limitation: it uses the approving
admin's `g.user_id` as `owner_id`, so a global admin cannot approve a skill owned
by another user through the route. The safe probe showed a permission-bearing
skill owned by user 101 and an admin token for user 303 receiving 404. That is
not an escalation; it is a broken cross-member approval contract and evidence
that there is no separate workspace-admin authorization model.

There is no public rejection/revocation endpoint for published records in the
audited route set. `set_skill_approval()` can mark a record rejected when called
internally, but the route exposes approval only. The store has no CHECK
constraints for lifecycle/approval values, no content-digest validation on
read, and no transaction tying the audit log to the state transition.

Detach is scoped to the authenticated owner and deletes only the extension-store
row. Published records have no install path, so the operation does not delete
the local bundled/custom definition. The legacy runtime-install surface remains
separate and is discussed in Finding F-01.

## 7. Legacy alternate route: confirmed arbitrary local-file read

The narrow publish endpoint is data-only, but the same blueprint still exposes:

- `POST /api/skills/inspect`, which accepts `source`, `url`, or `path`;
- `POST /api/skills/install` and `POST /api/skills`, which accept `source`,
  `preview_id`, `path`, or `name`.

Those routes call `ExtensionManager.inspect_skill()`/`install_skill()`, which
call `SourceFetcher.fetch_source()`. For `extension_type="skill"`, a local file
is accepted whenever `Path(source).is_file()` and then parsed as the skill
document. No server-side allowed-root check exists. The inspection response
includes the parsed `instructions` in the returned manifest.

This is an alternate legacy route, not a bypass of the narrow publish method's
argument list. It is nevertheless in the actual authenticated Skills Library
API and defeats the broader server-read restriction claimed for the feature.

## 8. Closed allow-list and execution-policy review

The explicit selection implementation has strong controls in the current
disabled release:

- at most 10 unique IDs;
- input strings are normalized at resolver entry and task route validation
  rejects non-string list members;
- every requested ID must exist under the authenticated owner;
- published IDs must pass lifecycle, approval, and enabled filters;
- permission-bearing published records are rejected under metadata-only policy;
- total instruction length is bounded at 12,000 characters;
- published records are excluded from prompt-only matching;
- explicit empty selection returns no skills and does not fall through to
  matching;
- runtime resolution uses the runtime tenant ID again.

The task probe observed:

```text
owner 101 selected enabled published skill: HTTP 201
persisted selected_skill_ids: ["selection-demo"]
persisted selected_skill_digests: one digest
cross-tenant selection by user 202: HTTP 403
duplicate selection: HTTP 403
pending permission-bearing selection: HTTP 403
```

The persisted digest is currently an evidence field, not a TOCTOU check. Runtime
resolution uses the current record by ID and does not require its current
`content_hash` to equal the persisted digest. Current published updates are
mostly blocked by conflict semantics, but legacy update/rollback and direct
storage changes mean the invariant is not cryptographically enforced.

The control-plane policy is source-locked to `execution_enabled=False` regardless
of environment values. `run_task_pipeline`, `RealAgentRuntime`, plugin tool
bridging, and task execution routes all fail or return the stable
`EXECUTION_DISABLED` response before execution. A pure policy probe with
contradictory environment values returned `execution_enabled=false` and
`disabled=true`. No execution policy was bypassed during this audit.

## 9. CLI/TUI and error-path review

The local `SkillLibrary` reads one selected installed definition, validates its
local digest, and the CLI publish command submits exactly that entry's ID,
metadata, full content, digest, and an empty requested-permissions list. It does
not submit a local path. The TUI publish action performs the same selected-entry
lookup and checks for a token/offline state before posting. Both refresh server
state after the HTTP call; the CLI performs a follow-up GET and the TUI calls
`load_skills()`.

The existing client retries only 429/502/503/504 and retries a 401 once after
refresh. It does not retry a 409 publish conflict. Identical publish retries are
safe at the manager level when sequential; the race described above is not
handled atomically.

The focused CLI tests confirm:

- only selected local content is submitted;
- no success is printed when the client raises a 403 publish error.

Hardening gap: the CLI and TUI primarily treat a successful HTTP status as
success and do not independently require a JSON `ok: true` value before their
success notification. Current server publish failures use non-2xx responses, so
this was not a demonstrated production bypass; a proxy or future endpoint that
returns `200 {"ok": false}` could produce a misleading success message. Error
strings are control-character sanitized for Rich/TUI display, but legacy server
path errors can still expose filesystem details to the authenticated caller.

The local and remote concepts remain visibly distinct: local install/enable
mutates the user-controlled library; remote publish calls the authenticated API;
the local catalogue reports `definition_only_not_runtime_wired` and
`runtime_connected=False`.

## 10. Inventory and capability claims

Two deterministic scans were run against `cli/aetherion_cli/skills/**/SKILL.md`:

| Metric | Result |
|---|---:|
| Packaged `SKILL.md` files | 192 |
| Unique library IDs | 192 |
| Duplicate filesystem-derived IDs | 0 |
| Definitions without frontmatter | 14 |
| Library readiness `definition_only_not_runtime_wired` | 192 |
| Library `runtime_connected=True` | 0 |
| Mapping rows in current direct scan | 88 |
| Unique mapping IDs | 77 |
| Mapping duplicate IDs | 11 IDs duplicated |
| Mapping entries without a matching tree definition | 14 |

The prior Phase 5.1 scan compared current content digests to its report ledger
and observed zero digest changes. The direct library scan also reported the
current tree digest ledger aggregate
`80df5c0a609de3b05ce4d2663f7a5f158968a165f47a58c313005526a458c3d1`.

The mapping file is not a complete source-of-truth inventory. Its 88 rows contain
11 duplicate IDs, while the filesystem has 192 unique definitions. The prior
report's normalized mapping comparison also found 129 tree IDs absent from the
mapping and 14 mapping-only entries; the differing counts reflect the mapping
parser/normalization conventions. No packaged definition was executed or
installed by this audit.

The evidence supports only these distinctions:

- **192 local packaged definitions:** yes;
- **tenant-published records:** only isolated temporary fixtures, not a live
  tenant count;
- **approved/enabled records:** only temporary deterministic fixtures;
- **explicitly selected records:** temporary resolver/task probes only;
- **tool-backed and independently tested through this publish flow:** zero.

The UI/API status fields do not prove runtime readiness. The backend response
explicitly reports `execution_enabled=false`; the local library reports
definition-only readiness; no published flow creates a tool handler. Legacy
runtime-installed skills can still be enabled and explicitly selected, but that
is compatibility behavior and not evidence that packaged definitions are
tool-backed.

## 11. Detailed finding matrix

| ID | Classification / severity | Affected boundary | Evidence and impact | Existing detection | Minimal corrective action | Regression test proposal |
|---|---|---|---|---|---|---|
| F-01 | **Confirmed vulnerability — High** | Authenticated user to backend filesystem and legacy skill store | `platform_extensions.py:687-726` accepts path/source/url; `fetcher.py:38-90, 244-316` reads any local file for a skill. Probe: authenticated `POST /api/skills/inspect` with `source=backend/routes/platform_extensions.py` returned `200`, manifest ID `platform-extensions`, and 31,249 bytes of Python source as instructions. A caller can also stage arbitrary server-readable skill content into their owner scope through the legacy install route. Option A prevented execution, but it does not prevent disclosure or unreviewed ingestion. | No. `test_39_rest_api_skill_crud_lifecycle` intentionally uses an arbitrary temporary local skill path and has no traversal/server-root negative case. | Remove `path`/`url`/arbitrary `source` from the authenticated tenant skill surface, or require a narrowly allow-listed server catalogue root and separate reviewed import service. Keep data-only publish as the only local-to-tenant attach path. | Authenticated user supplies `/etc/hosts`, an application Python file, a parent traversal, and a symlink/UNC path to `/api/skills/inspect`; assert no source bytes are returned. Assert legacy install cannot read outside the approved catalogue root. |
| F-02 | **Confirmed authorization freshness gap — Medium** | JWT/session role claim to admin approval | `auth_system.py:1103-1140` validates token/bans/revocation but does not reload `users.role`; `admin_required` at `1144-1154` compares the embedded `g.user_role`. Temporary SQLite probe demoted an admin in the database after issuing the token; the old token still approved a permission-bearing record with HTTP 200. | No. Existing auth tests cover API-key admin rejection and static role claims, not demotion/deletion of an already-issued JWT. | On admin routes, load current role from the authoritative user store and require active membership, or revoke all access tokens whenever role/membership changes. Reject missing users rather than treating the token as valid. | Issue an admin JWT, demote/delete the user, then call `/api/skills/<id>/approve`; assert 401/403. Repeat after workspace membership removal and after token revocation. |
| F-03 | **Confirmed race / conflict-contract gap — Medium** | Concurrent publish to same `(owner_id, skill_id)` | `manager.py:454-462` checks existence before `store.save_skill`; `store.py:305-361` performs a separate read/insert/update transaction. Barrier probe forced two writers through the absence check; one returned `CREATED`, the other raised uncaught `sqlite3.IntegrityError` instead of the documented conflict response. A second interleaving let a later writer enter the generic save path after the first commit. | No. Dedicated tests cover sequential idempotency and sequential conflict only. | Use an atomic insert/compare-and-conflict operation with a unique constraint, catch the unique violation, reload the row, and return `ALREADY_CURRENT` only for identical content or a stable 409 for different content. | Run two concurrent same-owner/different-content publishes and assert exactly one created, one 409, no 500, and final content equal to the winner. Repeat across separate manager/store instances. |
| F-04 | **Confirmed production architecture gap — High prerequisite** | Extension persistence across production replicas and database boundary | `store.py:2, 32-40` is SQLite-only; `db/store_factory.py` is not used by `ExtensionStore`; no PostgreSQL installed-skills schema or migration was found. `deploy/k8s/backend.yaml:9` declares two replicas, while production compose supplies PostgreSQL but no shared extension store. SQLite tests cannot prove production durability, replica consistency, backup, or PostgreSQL isolation. | No. All Phase 5.1 tests inject temporary SQLite. The three REST tests in full Phase 3 failed while PostgreSQL was unavailable; no production PostgreSQL integration ran. | Add a reviewed PostgreSQL extension repository/schema with tenant keys and transaction semantics, or explicitly prohibit the feature in production until a shared durable store is configured. Add a real PostgreSQL integration job. | Start an authorized disposable PostgreSQL instance in CI, migrate extension tables, run publish/list/approve/selection/concurrency tests across two app processes, then restart one process and verify the record remains visible to both. |
| F-05 | **Confirmed corruption fail-open — Medium hardening** | Stored extension metadata/content integrity to runtime selection | `store.py:424-435` filters trusted lifecycle/approval/enabled columns but does not validate content hash; `_row_to_skill_dict` at `503-539` converts malformed JSON to empty lists. Temporary probe changed permissions JSON to `{invalid-json` and hash to `not-a-digest`; resolver still returned the record with empty permissions. | No. Tests cover malformed publish input, not post-storage corruption or hash mismatch. | Validate row schema and digest before returning a runtime candidate. Fail closed on malformed JSON, invalid state combinations, unknown permissions, or digest mismatch; quarantine and audit corrupt rows. | Corrupt each metadata JSON column, hash, lifecycle, approval, and enabled flag; assert GET/list may report corruption but runtime resolution rejects the record and never treats it as approved. |
| P-01 | **Workspace authorization prerequisite — not proven as an exploit** | Authenticated user to workspace membership/role boundary | `_get_workspace_scope()` is `str(g.user_id)` (`platform_extensions.py:32-39`); no membership adapter was found. The owner-ID store boundary is exact, but the implementation cannot establish shared-workspace membership, per-workspace role, immediate removal, or cross-member approval. | No. Existing tenant tests use Alice/Bob as owner IDs, not shared workspaces or membership changes. | Introduce one authoritative workspace/membership authorization service and require it consistently for publish, list, detail, approval, enable/disable, detach, task creation, and runtime resolution. Store `workspace_id` as an independently authorized tenant key. | Create users U1/U2, workspaces W1/W2, memberships and role changes; assert every operation follows membership/role state and that user IDs cannot be substituted for workspace IDs. |
| H-01 | **Hardening opportunity — Low/Medium** | Client/UI truthfulness | CLI `main.py:3164-3181` and TUI `gateway.py:1422-1446` use HTTP success as the main success signal and do not require `ok is True` before the success notification. Current server errors are non-2xx, so no current false-success response was demonstrated. | Partial. `cli/tests/test_skill_library.py` covers a raised 403, not `200 {"ok": false}`. | Validate the response schema and `ok` field before success; refresh only after a confirmed successful state transition. | Mock a 200 JSON `ok:false` publish response and assert CLI/TUI show failure and do not claim publication. |
| H-02 | **Hardening opportunity — Low** | Publish metadata integrity | Request metadata can disagree with optional frontmatter name/version/description; C1/DEL control characters are not rejected by the server parser. This can produce misleading display/version metadata but was not a privilege or tenant escape. | No focused test. | Require canonical request/frontmatter agreement or ignore metadata fields not signed by the selected content; reject the full control-character range. | Publish conflicting frontmatter/request metadata and C1/DEL characters; assert rejection or an explicitly canonicalized response. |
| H-03 | **Transaction/audit consistency gap — Medium** | Publish persistence to audit log | `store.py:354-363` commits the skill/version rows before `log_audit()` on a separate connection. Synthetic audit failure left a persisted record while publish raised an exception and produced no audit row. | No. No audit failure-injection test exists. | Commit state and audit atomically, or use an outbox with a durable status that cannot report an un-audited publish as complete. Make retry semantics reconcile the existing row and missing audit event. | Inject audit-store failure after the primary write; assert the API returns a defined recoverable state and that no untracked approved/published state is exposed. |

### Findings explicitly not confirmed

- No cross-owner GET/list/enable/disable/detach/selection bypass was observed for
  the exact owner-ID predicates in the published skill store.
- No published content was imported as executable code by the publish flow.
- No permission-bearing published record became runtime-selectable through the
  tested manager path; its permission check remained active even after approval
  and enablement.
- No request field, frontmatter field, or publication response changed the
  global `execution_enabled` policy.
- No real tenant record, live production database, package registry, external
  API, subprocess, plugin handler, or MCP server was used.

## 12. Actual test commands and results

All commands below were run from the audited repository root. `-p
no:cacheprovider` was used to avoid creating a repository pytest cache, and
`PYTHONDONTWRITEBYTECODE=1` was used for the Python checks. Temporary databases
and fixtures were created outside the repository under the pre-existing
OmniRush temporary directory. No PostgreSQL service was started.

| Command | Result | Exit | Duration / notes |
|---|---|---:|---|
| `python -m pytest -p no:cacheprovider tests/integration/test_phase5_1_published_skills.py -q` | 4 passed | 0 | pytest 2.31s |
| `python -m pytest -p no:cacheprovider cli/tests/test_skill_library.py -q` | 8 passed | 0 | shell stopwatch 10.704s |
| `python -m pytest -p no:cacheprovider tests/integration/test_phase3_extensions.py -k "17a or 30 or 31a or 31_skill" -q` | 4 passed, 43 deselected | 0 | 1.98s |
| `python -m pytest -p no:cacheprovider cli/tests -q` | 347 collected and passed | 0 | shell stopwatch 31.891s; five existing Click deprecation warnings |
| `python -m pytest -p no:cacheprovider tests/integration/test_phase3_extensions.py -q` with temporary extension/durable paths | 38 passed, 9 failed | 1 | pytest 11.51s; shell stopwatch 16.495s |
| AST parse of the 12 audited implementation/test files | 12 parsed | 0 | deterministic, no writes |
| `ruff check` on the audited backend/CLI files | 369 findings | 1 | existing style/typing/import findings; no `--fix` used |

The full Phase 3 failures were exactly:

- five plugin tool-bridge/sandbox tests stopped at the intentional
  `ExecutionDisabledError` boundary (`test_25` through `test_29`);
- one runtime-stream skill activation test stopped at the same disabled boundary
  (`test_33`);
- three REST lifecycle/BOLA tests (`test_38`–`test_40`) could not import the
  authentication module because the configured PostgreSQL endpoint on the local
  machine refused the connection.

The connection failure was recorded only as a local unavailable PostgreSQL
service; credentials printed by the failing library traceback were not copied
into this report or any deliverable.

### Safe deterministic probes

The following read-only-audit probes used temporary SQLite databases or existing
repository source only:

1. **Authenticated Flask publish/tenant probe:** unauthenticated publish 401;
   owner publish 201; owner GET 200; cross-owner GET 404; workspace mismatch
   403; permission publish pending 201; admin for a different owner approval
   404.
2. **Task selection probe:** owner selection 201 with one canonical ID and one
   digest persisted; cross-owner, duplicate, and pending selection each 403.
3. **Role demotion probe:** old admin token approved after database demotion,
   HTTP 200.
4. **Concurrent publish probe:** one `CREATED`, one unique-constraint
   `IntegrityError` under a forced absence-check race.
5. **Audit failure probe:** publish raised synthetic `OperationalError` after the
   skill row persisted; audit row count remained zero.
6. **Corruption probe:** invalid stored hash/JSON still selected; a separate
   permission corruption probe turned a previously rejected permission-bearing
   record into a selected record because malformed permissions became `[]`.
7. **Legacy path probe:** authenticated `/api/skills/inspect` of
   `backend/routes/platform_extensions.py` returned HTTP 200 and 31,249 bytes
   of source as instructions.
8. **Policy probe:** contradictory policy input still returned
   `execution_enabled=false` and `disabled=true`.
9. **Inventory probe:** 192 files, 192 unique library IDs, all readiness
   `definition_only_not_runtime_wired`, zero runtime-connected entries; current
   mapping contained 88 rows/77 unique IDs with 11 duplicate IDs and 14
   mapping-only entries.

## 13. Coverage gaps and specific missing regression tests

The current tests are valuable for the local state machine but do not cover the
most important adversarial boundaries. The following tests should be added in a
future implementation phase, not in this read-only audit:

### Authorization and identity

1. **Deleted-token test:** issue a JWT, delete the user and all sessions, then
   assert publish/list/get/approve/task routes reject the old token.
2. **Role-demotion test:** issue an admin JWT, demote the user, and assert
   approval is 403 immediately.
3. **Workspace membership test:** one user belongs to W1 only; a request for W2
   must fail for every skill and task operation, including `workspace_id` and
   `session_id` aliases.
4. **Cross-member approval test:** a W1 admin must be able to approve a W1
   member's pending record only if the membership service grants that role, and
   must be denied in W2.
5. **Identity collision test:** use mixed numeric/string IDs, leading zeros,
   casing, empty values, and deleted/recreated users; assert canonical tenant
   identity cannot alias another owner.

### Legacy and input boundaries

6. **Local path disclosure test:** `/api/skills/inspect` and `/api/skills/install`
   must reject absolute paths, parent traversal, UNC paths, symlinks, and files
   outside a reviewed catalogue root.
7. **Remote source test:** if the legacy import feature remains, test redirects,
   DNS rebinding, IPv4/IPv6 private ranges, oversized responses, non-UTF-8,
   archives, and content-type confusion without live external calls.
8. **Frontmatter consistency test:** conflicting request/frontmatter metadata,
   duplicate keys with alternate casing, C1 controls, DEL, and oversized list
   inputs must fail closed.

### Storage, races, and recovery

9. **Concurrent publish matrix:** same content, different content, same version,
   different version, and different owners across independent connections; assert
   stable 200/201/409 behavior and no unhandled database error.
10. **Audit atomicity test:** inject primary-write, version-write, audit-write,
    and response serialization failures; assert no misleading state or reconcile
    with a durable outbox.
11. **Corruption quarantine test:** malformed JSON, invalid hash, unknown state,
    invalid permissions, and impossible lifecycle combinations must never be
    runtime-selectable.
12. **PostgreSQL integration test:** run the same suite against the actual
    production extension schema, not only the auth/durable-state store.

### Selection TOCTOU and lifecycle

13. **Digest TOCTOU test:** create a task, change the selected record before
    execution, and assert execution rejects the stale digest or records the
    expected immutable version.
14. **Detach/disable race:** detach or disable after task creation and before
    runtime resolution; assert the runtime fails closed.
15. **Approval-content binding:** change/rollback content after approval and
    assert approval is invalidated unless the exact immutable digest/version is
    retained.
16. **Payload tamper test:** mutate durable task `security_context` between
    creation and runtime using the supported CAS store API; assert unauthorized
    IDs cannot be introduced and the identity tenant cannot be changed.

### Client truthfulness

17. **HTTP 200/`ok:false` test:** mock publish responses with application-level
    failure and assert CLI/TUI do not announce success.
18. **Transient-after-commit test:** simulate a 503 after a committed publish and
    assert retry is idempotent and status refresh is authoritative.

## 14. Environment and production limitations

The following guarantees were not independently verified:

- no production PostgreSQL integration for the extension store;
- no two-process or two-replica extension-store test;
- no live workspace/membership service, because none was present in the audited
  route architecture;
- no production token/key/session lifecycle test;
- no enabled execution test, because Option A explicitly blocks it;
- no real tenant publish, approve, enable, detach, delete, or selection;
- no external URL, package, archive, plugin, subprocess, or MCP execution.

The full Phase 3 REST tests remain environment-blocked by the unavailable local
PostgreSQL service. The audit did not start PostgreSQL, change `DATABASE_URL`,
change the execution policy, or use a real tenant to make those tests pass.

The source tree also contains many pre-existing untracked worktrees and reports.
Those were treated as user-owned state. The audit did not select an alternate
worktree, clean a worktree, switch branches, or modify Git metadata.

## 15. Final protected-state comparison

The final read-only check after all source inspection, probes, and tests recorded:

| Item | Initial | Final | Changed? |
|---|---|---|---|
| Branch | `main` | `main` | no |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` | same | no |
| `backend/routes/d3_features.py` SHA-256 | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | same | no |
| `.git/index` SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` | same | no |
| `.git/index.lock` | absent | absent | no |
| staged paths | none | none | no |
| modified tracked paths | `backend/routes/d3_features.py` | same path only | no audit change |
| untracked path count | 2,269 | 2,269 before this report was added | no test-created workspace files |
| audited-root `rea/` | absent | absent | no |
| parent `rea/` | branch `main`, HEAD `e13da084e7c5967b80632ae5ad8d3d37bcf57469`, clean | same | no |

The only new workspace file created by this audit is this report:
`AETHERION_SKILLS_LIBRARY_PHASE5_1_SECURITY_AUDIT_REPORT.md`.

No implementation file, test, fixture, manifest, credential, configuration,
deployment setting, trust configuration, catalogue file, Git index, or real
tenant record was modified. No commit, push, reset, clean, branch switch, or
index mutation was performed.

## 16. Remaining production prerequisites and recommended next phase

Before treating Phase 5.1 as production-ready, the following prerequisites are
required:

1. Remove or constrain the legacy arbitrary-path/URL skill inspection and install
   routes; make the data-only publish contract the only supported local-to-tenant
   path for this feature.
2. Add authoritative workspace membership and role authorization, including
   immediate role/membership revocation and a separate approver/owner model.
3. Move extension records to the same reviewed shared PostgreSQL authority as the
   rest of the production control plane, with migrations, unique constraints,
   transactionally safe conflict handling, and replica tests.
4. Fail closed on corrupt metadata and bind runtime selection to an immutable
   content digest/version.
5. Add the adversarial regression tests listed in Section 13.
6. Keep the Option A execution lock enabled until those controls and a separate
   execution-capability audit are complete.

### Single recommended next phase

**Phase 5.2: Workspace Authorization and Durable Extension Storage Hardening.**

That phase should first implement the membership/role adapter and PostgreSQL
extension repository, then remove the legacy path disclosure surface, add atomic
publish/audit semantics and immutable selection records, and run the complete
cross-workspace/concurrency/corruption regression matrix. It should not enable
agent or plugin execution.

## 17. Completion and report integrity

This report is the only audit artifact created in the workspace. Because a
cryptographic hash inserted into the bytes it hashes is self-referential, the
authoritative SHA-256 of the **final completed report file bytes** is calculated
after the final write and is recorded in the completion response. The completion
response is the source of truth for that final file hash; no earlier or
placeholder hash is claimed in this document.
