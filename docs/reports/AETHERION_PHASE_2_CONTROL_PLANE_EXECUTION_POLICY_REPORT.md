# AETHERION AI — PHASE 2 CONTROL-PLANE EXECUTION POLICY REPORT

**Phase:** 2 of 10 — bounded execution-policy implementation only  
**Approved scope:** Option B — Control-plane release  
**Authorization:** Explicitly approved by the user in the Phase 2 task request  
**Project root:** `C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli`  
**Reference tree:** `C:\Users\mdham\Downloads\Aetherion-AI-main\Aetherion-AI-main\reference only\hermes-agent-main\hermes-agent-main`  
**Implementation window:** 2026-10-13  
**Final verdict:** **PASS for this bounded execution-policy slice; not a production-deployment certification**

> This report records Phase 2 only. It does not replace the master 10-phase
> deployment plan, does not mark unrelated phases complete, and does not claim
> public HTTPS, live provider execution, database recovery, or deployment
> readiness.

---

## 1. Authorization and approved objective

The user explicitly approved **Option B — Control-plane release** and
authorized the bounded implementation of an execution-disabled policy.

The implemented objective is:

- preserve authentication, task-record access, task history/log/review reads,
  health/readiness, D3 management and legitimate administrative memory
  maintenance;
- reject autonomous agent, command, plugin and untrusted worker execution;
- prevent queueing, dispatch, worker execution and host-process fallback from
  bypassing the policy;
- keep blocked task records queryable without adding a database status or
  migration;
- return a stable, non-sensitive API error;
- fail closed when policy configuration is missing, malformed or contradictory.

The Phase 1 document was verified at:

`AETHERION_PHASE_1_DEPLOYMENT_SCOPE_AND_ARCHITECTURE.md`

Its recommendation is Option B, its architecture decision says execution must
be disabled/rejected, and its P0-6 closure criterion requires endpoint,
auto-run, worker and alternate-path negative coverage. The Phase 1 document
still accurately records that approval was absent **when Phase 1 was written**;
this report records the later explicit approval and implementation authority.

No Phase 1 or master-plan numbering was changed.

---

## 2. Pre-implementation verification

### 2.1 Repository identity and state

| Item | Verified value |
|---|---|
| Project root | `C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli` |
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` — `initial commit for worktree support` |
| Tracked files before implementation | 1 |
| Untracked files before implementation | 2,219 |
| Modified tracked files before implementation | 1 |
| Existing modification preserved | `backend/routes/d3_features.py` |
| D3 SHA-256 re-confirmed | `15F81778D776F8084FFCC41917336EFDC5DDA30DE12C81545E19C7D4CB4B64A5` |
| Phase 1 report SHA-256 | `2A8EDB401ADEC0E23D2E1F1D293594B5432DF50848D338567B89779830FD9163` |

The Hermes reference tree was not modified. No Docker daemon, service,
container, network, production database, migration, external provider, public
endpoint or deployment command was started or changed.

The workspace contains many pre-existing untracked files. They were preserved;
they were not staged, reset, cleaned, or rewritten as part of this task.

### 2.2 Credential handling

- `.env` values were not read, printed, copied or reused.
- No provider key, OAuth secret, password, token or private payload was added
  to source, tests or this report.
- Focused route tests forced their auth-module import to a disposable SQLite
  path and used a synthetic test secret only in process memory.

---

## 3. Actual execution-path trace

The implementation was based on code-path inspection rather than the presence
of a sandbox class or configuration flag alone.

### 3.1 Public task and agent routes

Inspected and protected:

- `backend/routes/tasks.py`
  - `POST /api/tasks` with `auto_run` / `run`;
  - `POST /api/task/<tid>/execute` and `/run`, both sync and async in the
    original implementation;
  - `POST /api/v1/agent/invoke` and `/api/agent/stream`;
  - retry creation;
  - HITL approval transitions that could move pending work into running or
    executing states;
  - direct `run_task_pipeline()` calls;
  - task record, logs, diff, review and status reads.

Confirmed original unsafe behavior: task creation could start a background
thread, execute/run invoked the pipeline directly, the agent stream built a
runtime directly, and approval/retry lifecycle paths could create or resume
execution-capable records.

### 3.2 Runtime, tools and fallback paths

Inspected and protected:

- `backend/execution/agent_runtime/runtime.py` — `RealAgentRuntime` and
  `run_stream()`;
- `backend/execution/agent_runtime/context.py` — worktree attachment;
- `backend/execution/sandbox/boundary.py` — `WorkspaceBoundary.run_command()`;
- `backend/execution/tools/registry.py` — built-in tool dispatch;
- `backend/execution/extensions/tool_bridge.py` — plugin tool dispatch;
- `backend/execution/extensions/runner.py` — container and trusted-local
  subprocess routing;
- `backend/execution/extensions/container.py` — Docker plugin execution;
- `backend/execution/extensions/worker.py` — standalone plugin worker;
- `backend/execution/runner_contract.py` — approved runner factory and submit
  implementations;
- `backend/execution/sandbox.py` — legacy isolated-command facade;
- `backend/execution/runtime_runners.py` — legacy session runner;
- `backend/execution/docker_runner.py` — clone/install background pipeline.

Confirmed unsafe fallback: `WorkspaceBoundary` previously selected
`host_process_degraded` whenever Docker was unavailable, then reached
`subprocess.Popen(..., shell=True)`. `AETHERION_REQUIRE_DOCKER` only changed
behavior when explicitly set and was not an execution-disabled policy.

### 3.3 Background jobs, queue, scheduler and workers

Inspected and protected:

- `backend/execution/jobs/manager.py`;
- `backend/execution/jobs/runner.py`;
- `backend/execution/fabric/queue.py`;
- `backend/execution/fabric/service.py`;
- `backend/execution/fabric/scheduler.py`;
- `backend/execution/fabric/worker_runtime.py`;
- `backend/execution/fabric/worker_entrypoint.py`.

Confirmed original bypasses: background jobs could transition to `RUNNING` and
start `RealAgentRuntime`; fabric submission could enqueue graph steps; the
scheduler could dispatch claimed items; and the worker runtime could submit
claimed work to its executor callback. None of those paths consulted the
approved Option B boundary.

### 3.4 Deliberately retained administrative/read-only subprocesses

The following are not untrusted agent-command entry points and remain needed
for control-plane/operator functionality:

- D3 repository metadata and worktree administration in
  `backend/routes/d3_features.py` and `backend/db/d3_store.py`;
- read-only task diff/worktree inspection;
- explicit operator cleanup of historical worktrees;
- in-process memory-maintenance jobs from the existing allowlisted set.

They do not provide an agent-controlled arbitrary command path. Agent worktree
attachment is blocked before it can reach the worktree subprocess helpers.

---

## 4. Confirmed root causes and minimal changes

| Root cause | Minimal correction |
|---|---|
| No authoritative Option B policy existed | Added `backend/execution/control_plane_policy.py` with a source-enforced control-plane lock. Environment values are validation metadata only and cannot enable execution. |
| `auto_run` / `run` started a thread during task creation | Create-only remains supported; execution-bearing creation is recorded then transitioned to existing `CANCELLED` with a policy result and returns HTTP 403. |
| `/execute` and `/run` directly called the pipeline | Both routes now block the durable task before any provider, workspace, queue or thread operation. |
| Agent invoke/stream instantiated the runtime directly | Both public agent routes reject before parsing execution input or creating a workspace. |
| Retry and HITL approval could produce/resume executable work | Retry is rejected; approval of pending/running execution states is terminally blocked. Review of existing `REVIEW_PENDING` records remains administrative. |
| Direct pipeline callers bypassed routes | `run_task_pipeline()` blocks and raises `ExecutionDisabledError` before workspace creation, planning or runtime construction. |
| Runtime and tool objects could be instantiated directly | `RealAgentRuntime`, `WorkspaceBoundary`, tool registry and plugin bridge/runner boundaries now fail closed independently. |
| Docker absence selected host execution | Workspace and runner boundaries reject before Docker probing or host subprocess fallback. |
| Background jobs launched the agent runtime | Non-maintenance jobs are atomically moved to existing `FAILED` state before thread launch; allowlisted memory maintenance remains supported. |
| Fabric could enqueue/dispatch/execute work | Queue submission, graph enqueue, scheduler ticks, direct scheduler cycles, worker polling and claimed-item execution are disabled or fail closed. |
| Legacy and extension fallbacks had separate process paths | Legacy session, clone pipeline, runner factory, container runner, subprocess runner and standalone extension worker all have independent policy guards. |

No new task status, schema field, migration or persistence table was added.

---

## 5. Implemented policy and API behavior

### 5.1 Source-enforced policy

`backend/execution/control_plane_policy.py` defines:

- scope: `control-plane`;
- error code: `EXECUTION_DISABLED`;
- stable message: `Agent execution is disabled for the approved control-plane release.`;
- `ExecutionDisabledError`;
- durable blocking through existing `CANCELLED` state;
- strict allowlisting for existing memory-maintenance job names.

The effective Option B lock always returns `execution_enabled=False`.

The following configuration cases all remain disabled:

1. missing scope or disabled flag;
2. malformed values;
3. `agent-execution` or any unsupported scope;
4. an explicit false/contradictory disabled flag;
5. even a syntactically valid `control-plane` + true pair, because this source
   slice is not permitted to enable execution.

The configuration pair is therefore a fail-closed declaration/check, not an
environment-controlled feature flag.

### 5.2 Durable task behavior

Create-only task records remain available and tenant-scoped. When an execution
request is received, the record is transitioned to existing `CANCELLED` with a
minimal result containing the policy code and scope. The task remains readable
through existing status/history APIs.

Execution API responses are HTTP 403 with this shape:

```json
{
  "ok": false,
  "error_code": "EXECUTION_DISABLED",
  "error": "Agent execution is disabled for the approved control-plane release.",
  "execution": {"enabled": false, "scope": "control-plane"}
}
```

The response does not echo the prompt, command, host path, environment or
secret values.

### 5.3 Background and worker behavior

- Existing administrative memory-maintenance jobs continue through their
  existing JobRunner path only when their task name is allowlisted.
- Arbitrary background agent jobs fail before thread creation.
- New executable queue items cannot be enqueued.
- Existing queue items are not runnable to the scheduler, worker polling stops,
  and direct claimed-item execution raises before `mark_running` or the
  executor callback.
- Worker registration, heartbeat, drain, stop and health/control-plane
  inspection remain available.

---

## 6. Changed files

The repository HEAD remains unchanged. The workspace baseline tracks only the
D3 route; the other project files below were already untracked workspace files
and were modified in place without staging or resetting unrelated content.

### 6.1 New implementation source

- `backend/execution/control_plane_policy.py`

### 6.2 Modified implementation source

- `backend/routes/tasks.py`
- `backend/routes/phase4_routes.py`
- `backend/execution/sandbox/boundary.py`
- `backend/execution/sandbox.py`
- `backend/execution/tools/registry.py`
- `backend/execution/agent_runtime/runtime.py`
- `backend/execution/agent_runtime/context.py`
- `backend/execution/runtime_runners.py`
- `backend/execution/docker_runner.py`
- `backend/execution/runner_contract.py`
- `backend/execution/jobs/runner.py`
- `backend/execution/jobs/manager.py`
- `backend/execution/fabric/queue.py`
- `backend/execution/fabric/service.py`
- `backend/execution/fabric/scheduler.py`
- `backend/execution/fabric/worker_runtime.py`
- `backend/execution/extensions/tool_bridge.py`
- `backend/execution/extensions/container.py`
- `backend/execution/extensions/runner.py`
- `backend/execution/extensions/worker.py`

### 6.3 Focused tests

- `tests/integration/test_phase2_control_plane_execution_policy.py`

### 6.4 Required report

- `AETHERION_PHASE_2_CONTROL_PLANE_EXECUTION_POLICY_REPORT.md`

Not changed:

- `backend/routes/d3_features.py` — pre-existing user modification preserved;
- Phase 1 report;
- Hermes reference tree;
- deployment Compose/Kubernetes artifacts;
- migrations and database schemas;
- authentication, health and D3 implementation source, except for imports
  needed to load the tested route modules.

---

## 7. Test and verification evidence

All commands below were run after implementation. Exit codes are recorded from
the actual command results.

### 7.1 Focused Phase 2 security suite

```text
python -m pytest -q tests/integration/test_phase2_control_plane_execution_policy.py
```

Result: **10 passed, exit code 0**.

Coverage includes:

1. missing and contradictory policy configuration;
2. durable cancellation/blocking with historical record access;
3. auto-run request rejection without thread launch;
4. execute/run request rejection;
5. direct runtime and workspace boundary rejection before `Popen`;
6. tool-registry rejection before dispatch;
7. queue, scheduler, worker and claimed-item rejection;
8. background agent job failure before thread launch;
9. plugin worker and runner-factory rejection;
10. stable error non-disclosure of prompt/command/path-like content.

The tests use disposable `tmp_path` SQLite databases and synthetic inputs only.

### 7.2 Health and preflight regression subset

The route import was run with explicit test-only environment identity, a
disposable in-memory SQLite auth target, and a synthetic in-process JWT secret.
No production/staging database target was used for this verification command.

```text
python -m pytest -q tests/integration/test_phase22_control_plane_worker_runtime.py -k "health_control_plane_endpoint or readyz_includes_control_plane or preflight_check_control_plane_pass or preflight_check_control_plane_fails_closed_in_production or preflight_audit_scrubs_secrets_from_control_plane_diagnostics"
```

Result: **5 passed, 17 deselected, exit code 0**.

This verifies that the supported health/control-plane diagnostic surfaces still
load and report their structured checks under safe test-only configuration.

### 7.3 D3 migration regression suite

```text
python -m pytest -q tests/integration/test_d3_tracked_migrations.py
```

Result: **7 passed, exit code 0**.

This was a disposable SQLite migration/store test. It did not run a production
migration and did not alter the pre-existing D3 route file.

### 7.4 Syntax/import compilation

```text
python -m py_compile <all Phase 2 source files and focused test file>
```

Result: **exit code 0**.

The compiler emitted one existing `SyntaxWarning` in
`backend/execution/runtime_runners.py` concerning a `return` in a `finally`
block. It was not introduced or repaired in this bounded task.

### 7.5 Broader historical worker test limitations

An exploratory subset of the existing Phase 22 worker suite was also run:

```text
python -m pytest -q tests/integration/test_phase22_control_plane_worker_runtime.py -k "not worker_executes_claimed_item and not graceful_drain_waits_for_inflight_execution and not multi_worker_load_balancing_and_capacity and not capability_routing_to_specialized_worker"
```

Result: **15 passed, 3 failed, 4 deselected, exit code 1**.

The failures are documented rather than counted as passes:

- two historical health tests import the route package before establishing a
  test database target; the existing auth import attempted the ambient local
  PostgreSQL configuration and received connection refused. No connection was
  established and no data was changed. The safe rerun in §7.2 passed.
- the remaining failure submits executable queue work and expects scheduler
  dispatch. Under approved Option B, `ExecutionFabricService.submit_task()` is
  intentionally rejected. This is an expected semantic incompatibility with
  the old execution-enabled test, not a policy bypass.

The historical autonomous-runtime tests that assert file mutation, command
execution or live agent completion were not reclassified as passing. They are
not applicable to the approved control-plane behavior and were not used as
production evidence.

### 7.6 Tests not run

Not run because they are outside this authorization or require unavailable
external resources:

- public HTTPS, DNS, TLS and OAuth/browser smoke tests;
- live first-party model-provider calls;
- Docker/gVisor/Kata/microVM positive isolation tests;
- production PostgreSQL/Redis readiness or migrations;
- backup/restore, image build/publish/pull, rollback and deployment tests;
- full autonomous agent execution tests that require execution to be enabled.

### 7.7 Preservation-related diff check

```text
git diff --check -- backend/routes/d3_features.py
```

Result: **exit code 2**, reporting pre-existing trailing whitespace in the
user-modified D3 file. The file was not normalized or edited because the task
explicitly required preserving that modification. This is a workspace hygiene
finding, not a Phase 2 policy-test failure.

---

## 8. Pre/post state and preservation

### 8.1 Before implementation

- HEAD: `5c7bdcd4808a178aa677488f18b948a747f94193`.
- Branch: `main`.
- Tracked files: 1.
- Untracked files: 2,219.
- Modified tracked files: only `backend/routes/d3_features.py`.
- D3 SHA-256: `15F81778D776F8084FFCC41917336EFDC5DDA30DE12C81545E19C7D4CB4B64A5`.

### 8.2 After implementation

- HEAD remains `5c7bdcd4808a178aa677488f18b948a747f94193`.
- Branch remains `main`.
- No commit, tag, push, release, image publish or infrastructure action was
  performed.
- The D3 SHA-256 remains
  `15F81778D776F8084FFCC41917336EFDC5DDA30DE12C81545E19C7D4CB4B64A5`.
- The Phase 1 report remains unchanged.
- The workspace contains the two new implementation/test files plus this
  report, in addition to the pre-existing untracked workspace content.
- `backend/routes/d3_features.py` was not staged, reset, reverted, discarded,
  overwritten or otherwise modified by Phase 2.

The final status review was scoped to the Phase 2 file list and confirmed that
all listed source/test/report files exist. The full workspace remains
intentionally non-clean because it was already non-clean before authorization.

---

## 9. Implemented versus not proven

### Implemented and tested in a safe local environment

- Option B source policy is fail-closed.
- Public task execution requests return `EXECUTION_DISABLED`.
- Auto-run cannot start a thread.
- Direct task pipeline calls cannot create a workspace, resolve a provider,
  enqueue work or instantiate the runtime.
- Direct runtime, tool, plugin, runner and legacy entry points reject.
- Docker absence cannot select the old host-process fallback.
- Fabric submission, scheduling, worker polling and claimed-item execution do
  not proceed.
- Non-maintenance background jobs do not launch a worker thread.
- Existing task metadata/history remains stored and tenant-readable.
- Health/preflight diagnostics and D3 migration/store behavior passed their
  safe regression subsets.

### Not proven by this phase

- No public deployment target, host, region, registry, TLS terminator or
  operational owner was selected.
- No production or staging database/Redis target was exercised.
- No secret rotation or protected production injection was performed.
- No live provider, autonomous agent, Docker isolation, gVisor, Kata, microVM,
  HTTPS, OAuth, backup/restore or rollback evidence exists from this task.
- No claim is made that the application provides autonomous production-agent
  functionality. The approved release explicitly does not expose it.

---

## 10. Remaining risks and master-plan P0 blockers

The following Phase 1/master-audit blockers remain open and were not silently
closed by this implementation:

- **P0-1:** Product/security ownership record and complete launch-scope sign-off
  must be preserved outside this implementation report.
- **P0-2:** Host, region, domain, DNS and TLS target are unresolved.
- **P0-3/P0-4:** The source baseline is not an immutable reviewed commit/tag,
  and image provenance/registry digests are not established.
- **P0-5:** Option A sandbox integration is not implemented or proven; this is
  intentionally deferred because Option B was approved.
- **P0-7:** First-party provider scope remains unresolved. Compose/mock must
  not be represented as autonomous AI.
- **P0-8:** Target PostgreSQL/Redis ownership, migration and failure evidence
  remain unresolved.
- **P0-9:** Credential rotation and protected secret injection remain external
  prerequisites.
- **P0-10:** Public HTTPS/auth callback/origin evidence remains unverified.
- **P0-11:** Backup/restore and rollback evidence remains unverified.
- **P0-12:** Monitoring, alerting and incident ownership remain unresolved.

Additional implementation follow-ups before any public control-plane release:

1. Decide how to reconcile any pre-existing queued agent work records; this
   phase prevents execution but does not delete historical queue/task data.
2. Add the selected deployment environment's explicit policy declaration to
   protected configuration without making it an execution enable switch.
3. Run the release-specific auth/HTTPS/DB/Redis and recovery gates in the
   authorized target environment.
4. Re-audit all route registrations and worker startup configuration against
   the exact immutable release baseline.

---

## 11. Acceptance assessment and verdict

| Acceptance item | Result |
|---|---|
| Phase 1 file and Option B recommendation verified | **PASS** |
| Explicit Option B approval recorded for Phase 2 | **PASS** |
| Real task/API/runtime/worker path traced | **PASS** |
| All discovered agent/command/plugin/fallback boundaries protected | **PASS** in the bounded control-plane path; administrative/read-only subprocesses remain intentionally classified separately |
| Missing/malformed/contradictory policy fails closed | **PASS** |
| No tested API-to-runtime path reaches host command execution | **PASS** |
| Task metadata/history remains accessible | **PASS** |
| Mandatory focused policy tests | **PASS — 10 passed** |
| Health/preflight supported subset | **PASS — 5 passed** |
| D3 migration/store regression | **PASS — 7 passed** |
| Existing D3 user modification preserved | **PASS — SHA-256 unchanged** |
| Unauthorized infrastructure/migration/deployment action | **PASS — none performed** |
| Overall production readiness | **NOT CLAIMED — outside Phase 2** |

### Final verdict: **PASS — bounded Phase 2 implementation complete**

The approved control-plane execution-disabled slice is implemented and covered
by focused negative tests. The result is safe by rejection, not by claiming
that sandboxed autonomous execution exists. The next master-plan activity must
remain the next authorized deployment/readiness phase after the unresolved P0
ownership and infrastructure decisions are addressed. This task does not
begin that next phase.
