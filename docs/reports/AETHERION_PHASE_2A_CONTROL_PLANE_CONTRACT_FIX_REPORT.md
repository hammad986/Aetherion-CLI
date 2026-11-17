# AETHERION AI — PHASE 2A CONTROL-PLANE CONTRACT CLOSURE

**Phase:** 2A of the canonical 10-phase plan  
**Approved release scope:** Option B — control-plane release  
**Project root:** `C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli`  
**Implementation date:** 2026-10-13  
**Executive verdict:** **PASS for the bounded Phase 2A contract-closure scope; not a production-deployment certification**

> This report records only the authorized Phase 2A source, test, and evidence
> work. It does not authorize or claim production deployment, credential work,
> infrastructure work, database migrations, image publication, or completion of
> any later canonical master-plan phase.

## 1. Executive result

The five audited Option B contract gaps were corrected and verified with
isolated tests:

| Finding | Result | Correction |
|---|---|---|
| A — D3 automation execution | **PASS** | Both supported D3 run aliases return HTTP 403 / `EXECUTION_DISABLED` before run persistence or dispatch. The store run-persistence method is also fail-closed. |
| B — Kanban deploy claim | **PASS** | The endpoint now performs only a requested D3 workflow-state transition and returns `deployment_performed: false` plus `operation: "workflow_state_transition"`. Unsupported states are rejected. |
| C — health/readiness semantics | **PASS** | Control-plane/application health remains independently observable. Worker registration is separate from execution capability, and all execution-readiness responses remain disabled under Option B. Dependency failures remain visible. |
| D — direct agent-tool mutators | **PASS** | Direct file mutation, command/test execution, and Git commit methods reject before boundary or filesystem side effects. |
| E — public physical worktrees | **PASS** | Public creation, discard, sweep, and orphan-sweep routes are disabled before physical mutation. Store-level physical worktree mutators are also fail-closed. Tenant-scoped read/list/diff behavior remains available. |

No autonomous execution, deployment, migration, external service, credential,
or infrastructure operation was started.

## 2. Baseline and custody evidence

### 2.1 Workspace identity before implementation

Verified before source edits:

| Item | Value |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Tracked files | 1 |
| Untracked files (porcelain `-uall`) | 2,223 |
| Status entries (porcelain `-uall`) | 2,224 |
| Modified/staged entries | 1 — pre-existing `backend/routes/d3_features.py` |
| Existing D3 SHA-256 | `15f81778d776f8084ffcc41917336efdc5dda30de12c81545e19c7d4cb4b64a5` |
| Phase 2A report before work | Absent |

The source-only helper at
`C:\Users\mdham\AppData\Local\Temp\omnirush\phase2a_snapshot.py` was
inspected before execution. It imports only standard-library modules, hashes
source/config files, captures Git status, excludes `.env` path parts, and writes
only to the identified temporary destination
`C:\Users\mdham\AppData\Local\Temp\omnirush\phase2a_before_20261013`.
It was run for custody evidence; no project file was written by the helper.
No populated `.env` value or credential was read, printed, copied, or reused.

The before-state manifest recorded these relevant hashes:

| File | Before SHA-256 |
|---|---|
| `backend/routes/d3_features.py` | `15f81778d776f8084ffcc41917336efdc5dda30de12c81545e19c7d4cb4b64a5` |
| `backend/db/d3_store.py` | `e1aed93520e344dd5602f1f1a07df974a4c0725bb8d0be12112cc389726443d6` |
| `backend/routes/health.py` | `47e86d13e83b720c610eb7f0dd8c78f5f6ef4684d9d10cf73ad3f02ea7d1429c` |
| `backend/routes/diff.py` | `d39ff95ca663ef1970e782e4462ba2cfb1daae50b5ae71dc1d14c12e40b42726` |
| `backend/execution/tools/registry.py` | `a5aad83bf5234e2894a5f9459feedccf419e64187a6f054d5651272e349de57d` |
| `backend/core/preflight.py` | `ba7befac854b9cd43ba4ef96407a7628c5f9917b1f75a8425b33ce20434e1451` |
| `cli/aetherion_cli/main.py` | `b3364169fd95aa4b060e7a0cfefec0207b8d89c311d5e71512c0c8ca22d1f83b` |

### 2.2 Preservation and post-implementation state

The before/after snapshot comparison reported no removed files. It reported
only the seven intended pre-existing source files as changed and the one new
focused test file. The original unrelated D3 work was preserved; only the
Phase 2A hunks documented below were added to that file.

Immediately before this report was created, the workspace had:

| Item | Value |
|---|---:|
| Status entries (porcelain `-uall`) | 2,225 |
| Untracked files (porcelain `-uall`) | 2,224 |
| Modified/staged entries | 1 |

After creating this report, verification showed 2,226 status entries, 2,225
untracked files, and 1 modified/staged entry. The one-count increase is this
required report.

The additional status entry is the new focused Phase 2A test file. The report
itself is the only additional permitted project artifact created by this step;
therefore the final status count is expected to increase by one for this
untracked report. No files were staged, committed, pushed, reset, cleaned, or
deleted.

Because the workspace contains substantial untracked content, ordinary
`git diff` is not a complete change inventory. The temporary source manifest
and its before/after hashes are the custody comparison used for this report.

## 3. Finding A — D3 automation execution contract

### Root cause

`POST /api/automation/tasks/<task_id>/run` previously called
`d3_store.create_automation_run(..., status="succeeded")` and returned a run
ID even though no execution occurred. The generic
`POST /api/automation/tasks/<task_id>/<action>` route accepted `action=run`
and reached the same false-success behavior.

### Correction

`backend/routes/d3_features.py` now:

- preserves the tenant-scoped task lookup and 404 behavior;
- returns the central `execution_disabled_payload()` with HTTP 403 for the
  canonical `/run` route;
- returns the same policy response directly for the generic `action=run`
  alias;
- performs no run-row insert, counter increment, queue submission, thread
  launch, or dispatch;
- reports automation status as not running with the scheduler disabled rather
  than claiming an active scheduler.

`backend/db/d3_store.py` also guards `create_automation_run()` directly with
`assert_agent_execution_enabled()` so a direct store caller cannot recreate a
false execution record behind the route.

### Evidence

The focused suite verifies both aliases, tenant not-found behavior, unchanged
run persistence, and direct store fail-closed behavior. The test asserts the
exact stable code and message:

- `error_code`: `EXECUTION_DISABLED`
- `error`: `Agent execution is disabled for the approved control-plane release.`
- HTTP status: `403`

## 4. Finding B — Kanban deploy truthfulness

### Root cause

The previous endpoint ignored the caller's `state`, always moved the card to
`done`, and returned `"deployed": true` without invoking any deployment
system. The CLI documentation describes the command as setting deploy state,
not performing a deployment.

### Correction

`backend/routes/d3_features.py` now:

- requires `execution_id` or `card_id`;
- accepts only the existing D3 workflow columns: `backlog`, `todo`,
  `in_progress`, `review`, `blocked`, `done`, and `archived`;
- honors the caller's requested state;
- rejects missing or unsupported state input with HTTP 400 and the supported
  state list;
- retains tenant-scoped persistence through `move_kanban_card()`;
- returns:

```json
{
  "deployment_performed": false,
  "operation": "workflow_state_transition"
}
```

There is no `deployed: true` claim and no external deployment call.

`cli/aetherion_cli/main.py` now uses the D3 workflow-state choices and prints
that the workflow state changed and no deployment was performed.

## 5. Finding C — health and readiness semantics

### Root cause

The prior health routes could describe a registered worker as
`WORKER-CAPABLE` and a healthy control plane plus worker as `EXECUTION-READY`
without incorporating the Option B execution lock.

### Correction

`backend/routes/health.py` now exposes the policy consistently and separates
the concepts:

- `/api/health/control-plane` remains healthy when the control plane/store is
  healthy, even though autonomous execution is intentionally disabled;
- `/api/health/workers` reports registered/ready worker fleet liveness, while
  `worker_capable` and `execution_ready` remain false under Option B;
- `/api/health/execution-ready` returns `503`, `ok: false`,
  `status: "EXECUTION-DISABLED"`, and `execution_ready: false` even with a
  healthy control plane and registered worker;
- `/api/health/fabric-readiness` reports `application_ok` and
  `overall_ok` for control-plane availability separately from its false
  execution-ready level; it does not make intentional execution disablement
  an application failure;
- `/readyz` exposes `application_ready` and `execution_ready` separately;
- worker/store dependency exceptions return explicit dependency-unavailable
  responses rather than being mistaken for a policy-disabled success;
- `backend/core/preflight.py` does not require an LLM provider or Docker
  sandbox for readiness when autonomous execution is disabled, while its
  genuine control-plane/database checks remain visible.

The existing `backend/execution/runner_contract.py` policy-aware runner status
was consumed by the execution-readiness endpoint; it remains fail-closed and
was not otherwise changed.

## 6. Finding D — direct agent-tool mutators

### Root cause

`AgentToolRegistry.execute_tool()` already rejected disabled execution, but a
caller invoking registry methods directly could bypass that dispatcher. The
direct methods performed side effects without an internal policy check.

### Correction

`backend/execution/tools/registry.py` adds one internal policy result helper and
checks it before any validation that could reach a side effect in:

- `write_file()`;
- `edit_file()`;
- `delete_file()`;
- `run_command()`;
- `run_tests()`; and
- `git_commit()`.

The checks do not trust caller flags and do not change the schemas or safe
read-only methods (`inspect_environment`, `read_file`, `list_dir`,
`search_files`, `check_syntax`, `git_status`, and `git_diff`). Git commit is
blocked before `git init`, `git add`, or commit invocation.

The focused tests use a disposable temporary directory and a mocked boundary.
They assert the stable policy error, `security_event`, no boundary command,
no path resolution, and no created file. A separate direct store test confirms
that physical worktree mutators fail before database access.

## 7. Finding E — public physical worktree boundary

### Root cause

The authenticated D3 `POST /api/worktrees` route was reachable by any
authenticated tenant principal and called `D3Store.create_real_worktree()`,
which created `.aetherion_worktrees`, ran `git worktree add`, and persisted
metadata. No verified operator-only boundary existed in the current source.
The discard and sweep routes also performed physical Git/filesystem mutation.

### Correction

The following public physical mutation routes return the stable HTTP 403
policy response before request parsing, UUID creation, store access, Git
commands, or filesystem mutation:

- `POST /api/worktrees`;
- `POST /api/worktrees/<worktree_id>/discard`;
- `POST /api/worktrees/sweep`; and
- `POST /api/worktrees/sweep/orphans`.

`D3Store.create_real_worktree()`, `discard_worktree()`,
`sweep_worktrees()`, and `sweep_orphans()` independently call
`assert_agent_execution_enabled()` before database or physical work. This
defense-in-depth check prevents a direct internal caller from bypassing the
public route boundary.

Safe control-plane reads remain available:

- tenant-scoped worktree list;
- tenant-scoped metadata retrieval;
- tenant-scoped diff retrieval; and
- tenant-scoped active-worktree status count.

The duplicate legacy `/api/worktrees/<id>/diff` route was removed from
`backend/routes/diff.py` so the effective route is the tenant-aware
`d3_features.worktrees_diff` handler. The D3 store already filters metadata by
tenant for these reads. No worktree test created or modified the actual
project repository.

## 8. Exact changed-file inventory

The Phase 2A implementation changed exactly these existing source files and
added exactly these permitted focused tests and report:

### Existing source files changed

1. `backend/routes/d3_features.py`
   - D3 automation run aliases and truthful automation status;
   - Kanban workflow-state contract;
   - tenant-scoped worktree status;
   - disabled public worktree physical mutation routes.
2. `backend/db/d3_store.py`
   - direct automation-run policy guard;
   - direct physical worktree mutation guards;
   - optional tenant filter for active-worktree count.
3. `backend/routes/health.py`
   - policy-aware application, worker, execution, composite, and dependency
     readiness semantics.
4. `backend/routes/diff.py`
   - removed the duplicate non-tenant-aware worktree diff route.
5. `backend/execution/tools/registry.py`
   - direct mutator and execution-method policy guards.
6. `backend/core/preflight.py`
   - provider and sandbox checks are not required for disabled execution;
     real control-plane checks remain active.
7. `cli/aetherion_cli/main.py`
   - Kanban command state choices and truthful output wording.

### New focused test file

8. `tests/integration/test_phase2a_control_plane_contract.py`
   - 22 isolated tests covering Findings A–E, tenant boundaries, route
     ownership, direct tool side effects, worker/readiness semantics, and
     dependency failure.

### Required new report

9. `AETHERION_PHASE_2A_CONTROL_PLANE_CONTRACT_FIX_REPORT.md` — this file.

No existing report was overwritten. No Compose, Dockerfile, image, deployment,
environment, migration, credential, Hermes, or external-service file was
changed.

## 9. D3 before/after hash and preservation evidence

| File | Before SHA-256 | After SHA-256 |
|---|---|---|
| `backend/routes/d3_features.py` | `15f81778d776f8084ffcc41917336efdc5dda30de12c81545e19c7d4cb4b64a5` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |

The D3 after hash differs only because the authorized Phase 2A changes listed
in Finding A, Finding B, and Finding E were added. The large pre-existing D3
modification was present in the before snapshot and was preserved. The helper's
unified comparison showed no unrelated D3 hunk added by this task.

For completeness, final hashes of the other changed source/test files before
this report was added were:

| File | After SHA-256 |
|---|---|
| `backend/db/d3_store.py` | `00d919983ad8b142dc5b6a5510472e72c1fd12b913e414ec4736e098696dd1ee` |
| `backend/routes/health.py` | `47264eb8db301d9352d031a43b08f9d24760b612b5243968bee5bc79a10e3025` |
| `backend/routes/diff.py` | `ffdd7fefecce48b5e18188bb93159444872ddc937744ada68907d8505340c23a` |
| `backend/execution/tools/registry.py` | `4de61c5332ff485ec8659d12cd77530160d243740db13fa02995c15e8bdd3d74` |
| `backend/core/preflight.py` | `aa4b667dfdf72e4da44d22df80770197ed36848982ba576d0fbbbb35217b3e38` |
| `cli/aetherion_cli/main.py` | `bac0ae9d70168ddb14359af474aaa64f0b503f705ec2c13f60db0d761105a4a0` |
| `tests/integration/test_phase2a_control_plane_contract.py` | `1a3df4a218987eba8302698e6dd121262a226c391f500b5d6e32b11b39a0bede` |

The report changes the final workspace hash/count only by adding this required
new evidence file.

## 10. Verification record

All focused test commands used:

- explicit `AETHERION_ENV=CI-test` / `ENVIRONMENT=testing`;
- a disposable SQLite `DATABASE_URL` and `SQLITE_DB_PATH` under
  `C:\Users\mdham\AppData\Local\Temp\omnirush`;
- a synthetic test-only JWT secret held in the process environment;
- `PYTHONDONTWRITEBYTECODE=1`;
- `-p no:cacheprovider`; and
- a disposable pytest `--basetemp` directory.

No successful final verification used an ambient PostgreSQL, Redis, queue,
worktree, service, or production database. The initial pre-isolation test
collection did attempt an ambient PostgreSQL connection and was stopped at
collection with connection refused; it did not mutate that database and was
not used as evidence.

### 10.1 Focused Phase 2A suite

Command:

```text
python -m pytest -q -p no:cacheprovider --basetemp=C:\Users\mdham\AppData\Local\Temp\omnirush\phase2a_pytest_final tests/integration/test_phase2a_control_plane_contract.py
```

Result: **22 passed, exit code 0**.

### 10.2 Existing Phase 2 policy regression suite

Command:

```text
python -m pytest -q -p no:cacheprovider --basetemp=C:\Users\mdham\AppData\Local\Temp\omnirush\phase2a_policy_pytest_final tests/integration/test_phase2_control_plane_execution_policy.py
```

Result: **10 passed, exit code 0**.

### 10.3 D3 migration/store regression suite

Command:

```text
python -m pytest -q -p no:cacheprovider --basetemp=C:\Users\mdham\AppData\Local\Temp\omnirush\phase2a_d3_pytest_final tests/integration/test_d3_tracked_migrations.py
```

Result: **7 passed, exit code 0**.

### 10.4 Safe health/readiness regression subset

Command:

```text
python -m pytest -q -p no:cacheprovider --basetemp=C:\Users\mdham\AppData\Local\Temp\omnirush\phase2a_health_pytest_final tests/integration/test_phase22_control_plane_worker_runtime.py -k "health or readyz"
```

Result: **4 passed, 18 deselected, exit code 0**.

An additional safe readiness subset was run from
`tests/integration/test_phase12_production_trust.py` with `-k "livez or
readyz"`: **3 passed, 26 deselected, exit code 0**.

### 10.5 Syntax verification

Command:

```text
python -m py_compile backend/routes/d3_features.py backend/db/d3_store.py backend/routes/health.py backend/routes/diff.py backend/execution/tools/registry.py backend/core/preflight.py cli/aetherion_cli/main.py tests/integration/test_phase2a_control_plane_contract.py
```

Result: **successful, exit code 0**.

### 10.6 Diff-hygiene check

Command:

```text
git diff --check -- backend/routes/d3_features.py
```

Result: **exit code 2** because the pre-existing D3 working-tree change
contains eight whitespace-only blank lines. A before/after comparison against
the custody snapshot confirmed these blank-line whitespace patterns were
already present in the D3 file before Phase 2A (line numbers shifted as the
authorized hunks were added). They were preserved rather than reformatted so
unrelated user work was not rewritten.

### 10.7 Corrected test-isolation discovery

The first attempted collection of the new test file was made before the
disposable SQLite `DATABASE_URL` was explicitly set. Import-time application
initialization attempted an ambient PostgreSQL connection and failed with
`connection refused` during collection. That attempt was stopped and was not
treated as product evidence. The test was rerun with explicit disposable
configuration and then passed as recorded above. The first shell invocation
returned nonzero, but its exact shell exit code was not captured before the
safe rerun.

An intermediate test edit also produced eight assertion/response-unpacking
failures; the test harness was corrected without weakening the assertions, and
the final focused run passed 22 tests. No source defect was hidden by changing
the acceptance assertions.

## 11. Remaining blockers and limitations

### Phase 2A contract blockers

No unresolved P0/P1 finding from the independent Phase 2 audit remains open in
the five authorized categories. In particular:

- no D3 automation route creates a false successful run under Option B;
- no retained Kanban response claims a real deployment;
- no registered worker makes execution-ready true while disabled;
- direct tool mutators reject before side effects; and
- no public D3 physical worktree mutation route reaches Git/filesystem work.

### Release and platform blockers outside this task

The following remain intentionally pending and prevent any production-release
claim:

1. The workspace still has extensive untracked source/artifact content, so
   complete immutable Git provenance has not been established.
2. Production database, Redis, runner, provider, public endpoint, recovery,
   TLS/DNS, image, and infrastructure evidence was not created or revalidated
   by this task.
3. The historical missing `backend/execution/execution_engine.py` dependency
   and the unrelated `runtime_runners.py` `return`-in-`finally` warning were
   not changed. They remain pre-existing limitations and are not claimed as
   fixed here.
4. Physical cleanup routes are intentionally disabled rather than redesigned
   with a new operator/isolation architecture. Re-enabling them requires a
   separately reviewed authorization and boundary decision.

No credential security, credential rotation, production configuration,
immutable release packaging, image publication, migration, infrastructure,
deployment, or external monitoring work was started.

## 12. Canonical master-plan mapping

This work closes the bounded Option B contract-hardening follow-up to the
Phase 2 execution-policy slice. It maps to the canonical plan as follows:

| Canonical phase | Status after this task |
|---|---|
| Phase 1 — scope and architecture | Existing decision record; not re-run or re-certified |
| Phase 2 — control-plane execution policy | Existing bounded implementation preserved; Phase 2A contract gaps closed |
| Phase 3 — credentials/security readiness | **Pending; not started** |
| Phase 4 — database/Redis/infrastructure readiness | **Pending; not started** |
| Phase 5 — runner/sandbox/provider evidence | **Pending; not started** |
| Phase 6 — immutable provenance/release packaging | **Pending; not started** |
| Phase 7 — staging/runtime validation | **Pending; not started** |
| Phase 8 — external/UAT/operational gates | **Pending; not started** |
| Phase 9 — production deployment | **Pending; not started** |
| Phase 10 — final certification | **Pending; not started** |

## 13. Explicit authorization boundary confirmation

- Production deployment was not performed.
- Later canonical master-plan phases were not started.
- No credentials or populated `.env` values were read or changed.
- No migration or schema change was made.
- No Compose, Dockerfile, image, deployment, infrastructure, DNS, TLS, or
  external service was changed.
- No commit, tag, push, or release publication was performed.
- The Hermes reference tree was not modified.
- Unrelated existing workspace changes were preserved.

**Final bounded verdict: PASS for Phase 2A control-plane contract closure;
production release remains pending and is not certified by this report.**
