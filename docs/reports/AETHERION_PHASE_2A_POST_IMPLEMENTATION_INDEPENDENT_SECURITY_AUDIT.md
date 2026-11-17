# AETHERION AI — PHASE 2A POST-IMPLEMENTATION INDEPENDENT SECURITY AUDIT

**Audit type:** Read-only independent verification  
**Audit date:** 2026-10-13  
**Approved scope:** Option B — control-plane release  
**Project root:** `C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli`  
**Audit verdict:** **PARTIAL**  
**Production go/no-go:** **NO-GO for production; bounded Phase 2A public D3/health/tool corrections are substantially verified, but one direct internal worktree-helper boundary remains open**

## 1. Executive verdict

The Phase 2A implementation correctly closes the five reported public contract
defects in the following bounded sense:

- D3 automation run aliases are blocked before false-success persistence;
- Kanban workflow transitions no longer claim a real deployment;
- health/readiness endpoints distinguish application/control-plane health from
  disabled agent execution;
- direct `AgentToolRegistry` mutators reject before side effects; and
- public D3 worktree creation and public D3 physical cleanup routes are blocked.

Those conclusions are **VERIFIED** by current-source inspection and isolated
test execution below.

However, the implementation report's unconditional PASS cannot be accepted as a
complete closure of Finding E. The separate legacy execution worktree module
(`backend/execution/worktree.py`) exposes direct physical-mutator helpers that
do not themselves call the central Option B policy. Its current task-pipeline
caller is guarded before reaching it, so no public bypass was demonstrated, but
the requested “direct helper caller / alternate path” closure is not complete:

- `create_worktree()` can reach `git worktree add` through its own governance
  layer without `assert_agent_execution_enabled()`;
- `discard_worktree()`, `sweep_session()`, and `sweep_orphans()` similarly lack
  the central policy assertion; and
- `cleanup_execution_worktree()` contains a direct Git cleanup fallback after
  its helper call.

This is a **P1 defense-in-depth and future-caller boundary gap**, not a
demonstrated public HTTP escape in the current route graph. The correct audit
verdict is therefore **PARTIAL**, not PASS.

### Severity summary

| ID | Severity | Status |
|---|---|---|
| A — D3 automation execution | — | **VERIFIED CLOSED** |
| B — Kanban deploy truthfulness | P2 | **VERIFIED CLOSED for the audited deploy contract; CLI board/move state enum mismatch remains** |
| C — health/readiness | — | **VERIFIED CLOSED for the disabled-policy contract** |
| D — direct registry mutators | — | **VERIFIED CLOSED for the audited registry/plugin dispatch surfaces** |
| E — public worktree routes | P1 | **PARTIALLY VERIFIED; legacy direct worktree helper boundary remains** |

## 2. Scope and read-only methodology

### Inspected

- Phase 1 architecture/scope record;
- Phase 2 execution-policy report;
- previous Phase 2 independent audit;
- Phase 2A implementation report;
- current D3 routes/store and route registration;
- current health, preflight, policy, runner, tool, plugin, task, and legacy
  worktree code;
- current CLI client and Kanban/automation consumers; and
- `tests/integration/test_phase2a_control_plane_contract.py` plus relevant
  existing regression suites.

### Read-only controls

- No source, test, configuration, deployment, environment, or existing report
  was modified.
- The only project artifact created by this audit is this report.
- No `.env` value or credential-bearing file was read or printed.
- No Docker, PostgreSQL, Redis, worker, network, migration, deployment,
  provider, image, commit, tag, or push operation was performed.
- Safe tests used explicit CI-test identity, disposable SQLite paths, temporary
  pytest basetemp paths, `PYTHONDONTWRITEBYTECODE=1`, and
  `-p no:cacheprovider`.
- No physical worktree was created or removed in the project repository.

Evidence labels in this report:

- **VERIFIED** — directly observed in current source or a safe command result;
- **PARTIALLY VERIFIED** — current public path is covered but an alternate
  condition or boundary remains incomplete;
- **FAILED** — a safe verification produced a failure;
- **HISTORICAL** — taken from a prior report and not independently reproduced;
- **INFERENCE** — reasoned conclusion rather than runtime proof; and
- **NOT VERIFIED** — no trustworthy evidence was obtained.

## 3. Current workspace and source baseline

### 3.1 Workspace identity

| Item | Current evidence |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Tracked files | 1 |
| Status entries (`git status --porcelain -uall`) | 2,226 |
| Untracked files (`-uall`) | 2,225 |
| Modified/staged entries | 1 — `backend/routes/d3_features.py` |
| Phase 2A audit report before this audit | Absent |

The large untracked source tree means ordinary `git diff` is not a complete
change inventory.

### 3.2 Current hashes

| File | Current SHA-256 |
|---|---|
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |
| `backend/db/d3_store.py` | `00d919983ad8b142dc5b6a5510472e72c1fd12b913e414ec4736e098696dd1ee` |
| `backend/routes/health.py` | `47264eb8db301d9352d031a43b08f9d24760b612b5243968bee5bc79a10e3025` |
| `backend/routes/diff.py` | `ffdd7fefecce48b5e18188bb93159444872ddc937744ada68907d8505340c23a` |
| `backend/execution/tools/registry.py` | `4de61c5332ff485ec8659d12cd77530160d243740db13fa02995c15e8bdd3d74` |
| `backend/core/preflight.py` | `aa4b667dfdf72e4da44d22df80770197ed36848982ba576d0fbbbb35217b3e38` |
| `backend/execution/control_plane_policy.py` | `0b87fbafeb444ffc049b2c36afbedc480666f5195aac349e31142823374362e7` |
| `backend/execution/runner_contract.py` | `0e4776ee5d49ceb44f06e1702484a2596228566982801714fbedc66b6cfd5130` |
| `cli/aetherion_cli/main.py` | `bac0ae9d70168ddb14359af474aaa64f0b503f705ec2c13f60db0d761105a4a0` |
| `tests/integration/test_phase2a_control_plane_contract.py` | `1a3df4a218987eba8302698e6dd121262a226c391f500b5d6e32b11b39a0bede` |
| `AETHERION_PHASE_2A_CONTROL_PLANE_CONTRACT_FIX_REPORT.md` | `81926e678ed68511464c239165b6fdd4c64fdf9cf2fb99d62e405fdaeb5afd2d` |

The reported pre-Phase-2A D3 hash remains confirmed as:

`15f81778d776f8084ffcc41917336efdc5dda30de12c81545e19c7d4cb4b64a5`

The reported Phase 2A D3 after hash also matches current source:

`592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c`

## 4. Actual change inventory and preservation evidence

The existing custody snapshot at
`C:\Users\mdham\AppData\Local\Temp\omnirush\phase2a_before_20261013`
was inspected. The snapshot helper source had previously been inspected as a
source-only hash/status tool; it was not executed during this audit.

A read-only manifest comparison against that snapshot independently reported:

```text
changed_existing:
backend/core/preflight.py
backend/db/d3_store.py
backend/execution/tools/registry.py
backend/routes/d3_features.py
backend/routes/diff.py
backend/routes/health.py
cli/aetherion_cli/main.py

new_since_snapshot:
AETHERION_PHASE_2A_CONTROL_PLANE_CONTRACT_FIX_REPORT.md
tests/integration/test_phase2a_control_plane_contract.py

removed_since_snapshot: []
```

This matches the implementation report's intended inventory. It is strong
preservation evidence for the captured source set, but not a cryptographically
signed complete workspace provenance record.

### D3 before/after difference

The exact D3 comparison shows only the authorized categories:

- central policy import;
- truthful disabled automation status;
- blocked canonical and generic automation run aliases;
- requested-state and honest Kanban workflow transition response;
- tenant-scoped active-worktree status;
- blocked D3 public worktree creation; and
- blocked D3 public discard/sweep/orphan-sweep routes.

The large unrelated pre-existing D3 rewrite remains in the snapshot and was not
attributed to Phase 2A.

### Pre-existing whitespace

The eight whitespace-only blank lines reported by the implementation's
`git diff --check` result were present in the pre-Phase-2A D3 snapshot, with
line numbers shifting after the authorized hunks. They were not reformatted in
this audit.

### Files outside the bounded inventory

No current evidence shows a Phase 2A change to credentials, `.env`, Compose or
Docker files, migrations, release images, infrastructure, external services, or
the Hermes reference tree. This is based on the custody manifest and current
status/source inspection; it is not a claim that the entire historically
untracked workspace has immutable provenance.

## 5. Finding A — D3 automation run verification

**Status: VERIFIED CLOSED for the public D3 automation contract.**

### Current route graph

The current D3 source exposes these execution-looking aliases:

1. `POST /api/automation/tasks/<task_id>/run` at
   `backend/routes/d3_features.py:128-135`;
2. `POST /api/automation/tasks/<task_id>/<action>` at lines `138-157`, with
   `action == "run"` at lines `152-153`; and
3. CLI `aetherion automation run`, which calls the canonical `/run` endpoint
   at `cli/aetherion_cli/main.py:5413-5432`.

No `execute`, `trigger`, or `run-now` D3 alias was found. Unknown generic
actions return HTTP 400.

### Persistence and side-effect trace

Both public aliases first perform the tenant-scoped `get_automation()` lookup.
If the task is absent for that tenant, they return 404. If it exists, both
return `execution_disabled_payload()` with HTTP 403. The route no longer calls
`create_automation_run()`.

The store method at `backend/db/d3_store.py:186-217` still contains the SQL
insert and run-counter update required for a future execution-enabled release,
but now calls `assert_agent_execution_enabled()` at line 189 before `_get_db()`.
Thus a direct store caller also fails before database access under Option B.

The current test suite independently asserts:

- both aliases return 403 and the exact stable error code/message;
- the mocked `create_automation_run()` is not called;
- tenant-not-found remains 404; and
- direct store persistence raises `ExecutionDisabledError` before `_get_db()`.

The source trace confirms no queue, thread, provider, worker, or dispatch call
exists in either D3 route. Automation CRUD, tenant-filtered list, and history
reads remain present. History and run retrieval use tenant predicates in the
store.

### CLI handling

`Client._raise_for_status()` is invoked for non-success responses, and 403 is
classified as `AetherionAuthError`; `_handle_error()` exits before the CLI can
render a successful run ID. The CLI therefore does not turn the blocked API
response into a false success.

## 6. Finding B — Kanban workflow/deploy contract

**Status: VERIFIED CLOSED for the audited deploy endpoint; P2 compatibility issue remains.**

### API behavior

`backend/routes/d3_features.py:274-307`:

- requires `execution_id` or `card_id`;
- defaults missing `state` to `done` for compatibility;
- requires a string state;
- rejects values outside the D3 columns `backlog`, `todo`, `in_progress`,
  `review`, `blocked`, `done`, and `archived`;
- passes the requested state to tenant-scoped `move_kanban_card()`; and
- returns `deployment_performed: false` and
  `operation: "workflow_state_transition"`.

The response contains no `deployed: true` claim. No deployment system or
execution path is called. The underlying store update is tenant-filtered; a
different tenant cannot update another tenant's existing card. Missing or
unsupported input is rejected before the store mutation.

The focused tests parameterize all seven supported D3 states and assert the
exact response fields, requested state, store call, absence of the legacy
`deployed` field, and rejection of `deployed` as an unsupported state.

### CLI compatibility

`kanban deploy` now sends the selected D3 workflow state and prints that no
deployment occurred. The client raises on HTTP 400/403, so it does not print a
success message after a rejected transition.

### P2 compatibility finding

`cli/aetherion_cli/main.py:3821` still defines the general board/move
`_KANBAN_COLUMNS` as:

```text
backlog, ready, running, blocked, review, done, failed
```

while the server D3 board uses `todo`, `in_progress`, and `archived`, and the
deploy command separately uses the D3 set at line 3823. Consequently, the CLI
board/move surface and the deploy surface do not share one enum. This does not
reintroduce a false deployment claim or bypass Option B, so it is P2 rather
than a reason to reopen Finding B. Minimum correction: converge the CLI board,
move, and deploy choices on one server-owned workflow-state contract and add a
CLI consumer test.

## 7. Finding C — health and readiness

**Status: VERIFIED CLOSED for the Option B disabled-execution semantics; real production dependencies remain unverified.**

### Current policy behavior

`backend/execution/control_plane_policy.py:55-90` has a source lock that always
returns `execution_enabled=False`; malformed or missing environment values only
change `configuration_valid` and `reason`. No environment setting can enable
execution. `runner_contract.get_runner()` asserts this policy before selecting
a runner, while `get_runner_status()` explicitly reports
`control-plane-disabled` and unavailable execution.

### Endpoint verification

Current source inspection and the isolated tests establish:

- `/api/health/control-plane` remains healthy based on the control-plane check,
  and exposes `execution_disabled` without converting intentional disablement
  into control-plane failure;
- `/api/health/workers` reports registered/ready worker fleet state separately,
  but `worker_capable` and `execution_ready` remain false under Option B;
- `/api/health/execution-ready` returns 503 and
  `status: "EXECUTION-DISABLED"` even when the control-plane check and worker
  list are healthy;
- `/api/health/fabric-readiness` reports `application_ok`/`overall_ok` for the
  control plane separately from the false execution-ready level;
- `/readyz` reports `application_ready` and `execution_ready` independently;
  and
- worker-store dependency errors return structured 503 responses with
  `dependency_ok: false`.

The focused tests assert exact fields and statuses for a registered worker,
healthy control plane, disabled execution, a composite readiness response, and
a genuine worker-store failure.

### Preflight and disclosure review

When execution is disabled, `check_llm_providers()` and
`check_sandbox_docker()` report those execution prerequisites as not required;
database, Redis, environment, workspace, and control-plane checks remain
separate. Preflight sanitizes DSNs and audits secret leakage. The inspected
health payloads expose policy, worker IDs/status, counts, and sanitized check
details, not credential values.

This is isolated test/runtime evidence only. No production PostgreSQL, Redis,
runner, or external dependency was contacted during this audit.

## 8. Finding D — direct tool-mutator protection

**Status: VERIFIED CLOSED for the audited registry and plugin-dispatch surfaces.**

### Direct registry inventory

`backend/execution/tools/registry.py` now calls `_execution_disabled_result()`
before side effects in:

- `write_file()`;
- `edit_file()`;
- `delete_file()`;
- `run_command()`;
- `run_tests()`; and
- `git_commit()`.

The helper evaluates the central policy itself and returns the stable error
before path resolution, directory creation, file writes, boundary command calls,
`git init`, `git add`, or commit execution. Read-only methods
(`inspect_environment`, `read_file`, `list_dir`, `search_files`,
`check_syntax`, `git_status`, and `git_diff`) are not blocked by this helper.

The dispatcher also rejects before request validation and before custom handler
dispatch when execution is disabled.

### Alternate dispatch review

`backend/execution/extensions/tool_bridge.py` checks the same central policy
before plugin lookup or runner dispatch. `ExtensionRunner.execute_tool()` and
`SubprocessWorker.execute()` independently check it before process/container
execution. The current runtime caller uses `AgentToolRegistry.execute_tool()`.
No alternate public caller was found that invokes a registry mutator without
the internal guard.

The focused tests use a disposable `tmp_path` and mocked boundary, asserting
no boundary command, no safe-path resolution, no file creation, and stable
policy errors for every audited direct mutator. The source inspection confirms
the checks precede side effects rather than merely wrapping the dispatcher.

## 9. Finding E — worktree mutation and authorization

**Status: PARTIALLY VERIFIED; public D3 routes are closed, but a legacy direct internal helper boundary remains open.**

### Public D3 route result

Current D3 routes all use `@token_required`, and the public routes behave as
follows:

- `POST /api/worktrees` returns the policy 403 before request parsing, UUID
  generation, store access, or physical work;
- `POST /api/worktrees/<id>/discard` returns the same 403;
- `POST /api/worktrees/sweep` returns the same 403; and
- `POST /api/worktrees/sweep/orphans` returns the same 403.

`D3Store.create_real_worktree()`, `discard_worktree()`, `sweep_worktrees()`,
and `sweep_orphans()` also assert the central policy before database or Git
work. The focused tests mock each store method and assert no call occurs.

Tenant-safe D3 reads remain:

- `GET /api/worktrees` filters by tenant and optional session;
- `GET /api/worktrees/<id>` filters by tenant;
- `GET /api/worktrees/<id>/diff` filters by tenant before reading metadata; and
- `/api/worktrees/status` now uses the tenant-filtered active count.

The duplicate `/api/worktrees/<id>/diff` decorator is absent from
`backend/routes/diff.py`; the effective route-map test resolves this URL to
`d3_features.worktrees_diff`. The legacy `/api/diff/<task_id>` route remains
separate.

### P1 residual: legacy internal worktree module

The source graph contains a second physical worktree implementation at
`backend/execution/worktree.py`:

- `create_worktree()` at lines `411-487` reaches `_git("worktree_add", ...)`;
- `discard_worktree()` at lines `529-597` reaches Git removal/branch deletion;
- `sweep_session()` at lines `660-670` calls discard; and
- `sweep_orphans()` at lines `681-715` calls discard.

These helpers do not call `assert_agent_execution_enabled()` or
`is_agent_execution_disabled()`. They rely on the separate tool-governance
layer. Their current task-pipeline entry point,
`agent_runtime.context.attach_execution_worktree()`, does assert the central
policy at line 179, and `tasks.run_task_pipeline()` blocks at lines 398-402
before creating a context/worktree. Therefore the current public task route
does not demonstrate a reachable Option B bypass.

Nevertheless, direct helper callers are not intrinsically locked. In addition,
`cleanup_execution_worktree()` at `backend/execution/agent_runtime/context.py`
contains a direct Git cleanup fallback after calling the helper. A future or
internal caller that invokes these functions directly could mutate a physical
worktree while the central policy is disabled. This fails the requested
defense-in-depth standard for “direct helper callers or an alternate route.”

**Impact:** a future internal integration, maintenance path, or test harness
could re-enable physical Git mutation without passing the Option B source lock.
No public HTTP route to this helper was found in the current source graph, so
this is not a demonstrated public arbitrary-command escape.

**Minimum correction:** add the central fail-closed policy assertion at the
entry of `execution.worktree.create_worktree()`, `discard_worktree()`,
`sweep_session()`, `sweep_orphans()`, and the direct cleanup fallback; then add
isolated tests that patch `subprocess.run`/`_git` and assert they are not
reached while Option B is disabled.

## 10. Safe test evidence and limitations

### 10.1 Independently executed commands

All commands below were run with explicit CI-test identity, disposable SQLite
paths under `C:\Users\mdham\AppData\Local\Temp\omnirush`, synthetic in-process
test secrets, `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`, and disposable
pytest basetemp paths. The synthetic secret value is intentionally not recorded.

#### Phase 2A focused suite

```text
python -m pytest -q -p no:cacheprovider --basetemp=C:\Users\mdham\AppData\Local\Temp\omnirush\audit_phase2a_contract_pytest tests/integration/test_phase2a_control_plane_contract.py
```

**22 passed, exit code 0.**

#### Phase 2 execution-policy regression suite

```text
python -m pytest -q -p no:cacheprovider --basetemp=C:\Users\mdham\AppData\Local\Temp\omnirush\audit_phase2_policy_pytest tests/integration/test_phase2_control_plane_execution_policy.py
```

**10 passed, exit code 0.**

#### D3 migration/store regression suite

```text
python -m pytest -q -p no:cacheprovider --basetemp=C:\Users\mdham\AppData\Local\Temp\omnirush\audit_d3_pytest tests/integration/test_d3_tracked_migrations.py
```

**7 passed, exit code 0.**

#### Health/readiness subset

```text
python -m pytest -q -p no:cacheprovider --basetemp=C:\Users\mdham\AppData\Local\Temp\omnirush\audit_health_pytest tests/integration/test_phase22_control_plane_worker_runtime.py -k "health or readyz"
```

**4 passed, 18 deselected, exit code 0.**

#### Syntax check

```text
python -m py_compile backend/routes/d3_features.py backend/db/d3_store.py backend/routes/health.py backend/routes/diff.py backend/execution/tools/registry.py backend/core/preflight.py backend/execution/worktree.py backend/execution/agent_runtime/context.py cli/aetherion_cli/main.py tests/integration/test_phase2a_control_plane_contract.py
```

**Exit code 0.**

### 10.2 Not run

- No production/staging runtime verification;
- no live PostgreSQL or Redis test;
- no Docker or runner test;
- no physical worktree creation/removal test in the project repository;
- no unchanged historical execution-enabled suite run merely to obtain a green
  result; and
- no deployment, migration, image, provider, or external-service test.

The previous implementation's initial ambient-PostgreSQL collection failure is
accepted only as historical context. The isolated commands above are the audit
evidence.

## 11. Findings and closure criteria

### P1 — legacy direct physical worktree helper is not policy-locked

**Evidence:** `backend/execution/worktree.py:411-487`, `529-597`,
`660-715`; `backend/execution/agent_runtime/context.py:267-318`.  
**Trigger:** direct invocation of the legacy helper outside the currently
guarded `attach_execution_worktree()`/task-pipeline path.  
**Impact:** physical Git worktree creation/removal can be reached through the
legacy governance layer without the central Option B source lock.  
**Minimum correction:** add central policy assertions at every direct physical
helper boundary, including cleanup fallback.  
**Closure test:** disposable/mock-based tests assert `_git` and
`subprocess.run` are not called for direct create/discard/sweep/cleanup calls
while execution is disabled.

### P2 — CLI Kanban workflow enum mismatch

**Evidence:** `cli/aetherion_cli/main.py:3821-3823`; server D3 columns at
`backend/routes/d3_features.py:202`.  
**Impact:** board/move CLI choices differ from deploy choices and server D3
columns; some valid D3 states are not shown or selectable consistently.  
**Minimum correction:** use one server-owned workflow-state enum for CLI board,
move, and deploy commands.  
**Closure test:** CLI command tests parameterized over every server-supported
workflow state, including board rendering and move request payloads.

No P0 arbitrary-command or public HTTP execution bypass was demonstrated in
this audit.

## 12. Canonical master-plan mapping

The audit confirms that Phase 2A primarily advances the execution-disabled
alternative associated with canonical master Phase 4. It does not complete any
of the following:

| Master phase | Status |
|---|---|
| 1 — deployment scope and architecture | Existing decision record; not re-certified here |
| 2 — credential security and production configuration | **Pending; not started** |
| 3 — immutable source and release baseline | **Pending; provenance remains incomplete** |
| 4 — sandbox boundary or approved execution-disabled alternative | **Phase 2A public contract mostly verified; legacy helper P1 remains** |
| 5 — current release tests | **Bounded focused tests verified; broader release suite pending** |
| 6 — immutable image build and registry publication | **Pending; not started** |
| 7 — infrastructure provisioning | **Pending; not started** |
| 8 — database migration and candidate deployment | **Pending; not started** |
| 9 — security, functional, and recovery acceptance | **Pending; not started** |
| 10 — release freeze and go/no-go | **Pending; not started** |

## 13. Production-readiness blockers

The following remain outside this audit and prevent production reliance:

1. Complete immutable provenance for the large untracked workspace has not been
   established.
2. Credential security/rotation and production configuration were not audited
   or changed in this run.
3. PostgreSQL, Redis, runner/provider, recovery, TLS/DNS, image, infrastructure,
   and deployment evidence remain unverified.
4. The P1 legacy direct worktree-helper boundary must be closed before treating
   all physical worktree mutation paths as fail-closed under Option B.
5. The P2 CLI Kanban enum mismatch should be corrected before relying on the
   entire Kanban CLI surface.

## 14. Final recommendation and single next action

**Recommendation:** do not proceed to deployment or claim full Phase 2A PASS.

**Single next action:** perform a narrowly scoped, read-only or authorized
implementation follow-up that centralizes the Option B assertion in all legacy
`execution.worktree` physical-mutator and cleanup boundaries, then rerun the
isolated negative tests. After that closure, establish the immutable source
provenance baseline before any production or later release operation.

No source, test, configuration, existing report, credential, deployment
artifact, or external system was modified by this audit. The only new project
artifact is:

`AETHERION_PHASE_2A_POST_IMPLEMENTATION_INDEPENDENT_SECURITY_AUDIT.md`
