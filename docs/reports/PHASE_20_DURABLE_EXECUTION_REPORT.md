# AETHERION — PHASE 20 ENGINEERING REPORT
## DURABLE EXECUTION SUPERVISOR & CRASH-RESILIENT WORKFLOW KERNEL

**AI Role:** Principal / Staff+ Distributed Systems & Autonomous-Agent Engineer  
**Status:** `ENGINEERING COMPLETE — READY FOR NEXT ENGINEERING PHASE`  
**Independent Auditor:** NOT INVOKED  
**Certification Status:** NONE (Self-certification prohibited; strictly no "CERTIFIED" designation)  
**Date:** September 26, 2026  

---

## 1. Executive Summary

Phase 20 advances Aetherion from an **Adaptive Autonomous Verified Engineering Agent** to a **Durable Autonomous Verified Engineering Platform**.

Prior to Phase 20, Phase 19 established structured plans, validated execution DAGs, topological step scheduling, and adaptive replanning. However, execution ownership was tied to process lifetimes. Process crashes, worker evictions, or host restarts risked leaving steps in ambiguous running states, orphaned execution, or risk of duplicate non-idempotent side effects.

Phase 20 establishes one authoritative execution supervisor—`DurableExecutionSupervisor`—backed by WAL SQLite (`DurableSupervisorStore`) providing:
1. **Durable Step State Machine**: Strict, auditable transitions: `PENDING` → `READY` → `CLAIMED` → `RUNNING` → `VERIFYING` → `SUCCEEDED`, with crash/recovery states (`RUNNING` → `ORPHANED` → `RECOVERING` → `RETRYING` → `READY`/`CLAIMED`, or terminal failure).
2. **Atomic CAS Step Leasing**: Workers must acquire a time-bounded `StepLease` via Compare-And-Set (CAS). Expired or mismatched workers are rejected via `StaleLeaseError` on heartbeats, state transitions, or commit attempts.
3. **Heartbeat & Orphan Detection**: Active workers renew leases via periodic heartbeats. Stale heartbeats with expired leases are deterministically classified as `ORPHANED`.
4. **Deterministic RecoveryDecisionEngine**: Evaluates orphans into `RESUME`, `RETRY`, `WAIT`, or `FAIL_CLOSED` based on verifier truth, HITL state, tool side-effect records, idempotency, and RunBudget limits. Non-idempotent unverified side effects fail closed.
5. **Idempotency Identity**: Deterministic token generation and execution tracking prevents duplicate side-effect execution across worker retries.
6. **Boundary Checkpointing & Reconstitution**: Checkpoints captured at `plan_validation`, `step_claim`, `step_start`, `tool_batch_completed`, `verification_start`, `step_succeeded`, and `step_failed`. Reconstructs graph and budget state across simulated process crashes.
7. **Cancel-Wins Semantics**: Task cancellation wins decisively over late worker success, timeout, or recovery.
8. **End-to-End Pipeline Integration**: Integrated into `run_task_pipeline` and `RealAgentRuntime`, generating scrubbed replay events (`step.claimed`, `step.heartbeat`, `step.lease_expired`, `step.orphaned`, `step.recovery_started`, `step.recovered`, `step.retry_scheduled`, `checkpoint.created`, `execution.resumed`).

All 48 Phase 20 integration tests passed. All 45 Phase 19 tests passed. Zero regressions were introduced across the global suite (1087 passing, 21 Docker failures preserved).

---

## 2. Existing Durable Infrastructure Reused

Phase 20 respects existing architectural investments without unnecessary rewrites:
- **`durable_state.py`**: Retained as authoritative for high-level task records (`durable_executions` table) and process-wide task statuses.
- **`ExecutionPlan` & `ExecutionGraph` (Phase 19)**: Enriched with lease metadata on `PlanStep` (`worker_id`, `lease_id`, `lease_expires`, `heartbeat_at`, `idempotency_token`). Graph topological sorting and ready-step detection utilized directly by the supervisor.
- **`RunBudget` & `BudgetEnforcer` (Phase 15/16)**: Reused directly; supervisor inspects budget consumption during recovery evaluation and persists budget snapshots in checkpoints.
- **`scrub_secrets` (Phase 18)**: Reused to ensure no credentials, bearer tokens, or raw secrets are stored in checkpoints or emitted in replay events.
- **SQLite WAL Architecture**: Modeled after `StateDatabase` for concurrent write safety, explicit timeout handling, and foreign key enforcement.

---

## 3. Supervisor Architecture

The supervisor is implemented as `DurableExecutionSupervisor` in `backend/execution/orchestration/durable_supervisor.py`.

```text
USER TASK
   ↓
EXECUTION PLAN (Phase 19)
   ↓
EXECUTION GRAPH (Phase 19)
   ↓
DURABLE EXECUTION SUPERVISOR (Phase 20)
   ├── SQLite WAL Store (supervised_tasks, supervised_steps, supervised_checkpoints,
   │                     supervised_recovery_decisions, supervised_idempotency_records)
   ├── Atomic CAS Step Claiming & StepLease (worker_id, lease_id, lease_expires)
   ├── Heartbeat & Stale Lease Rejection (StaleLeaseError)
   ├── Deterministic Orphan Detection
   ├── RecoveryDecisionEngine (RESUME, RETRY, WAIT, FAIL_CLOSED)
   ├── Idempotency Engine (duplicate execution prevention)
   └── Boundary Checkpointing & Graph Reconstitution
```

### Authoritative Tables (`_SUPERVISOR_SCHEMA`)
- `supervised_tasks`: Maps `task_id` to `tenant_id`, `status`, `current_revision`, timestamps.
- `supervised_steps`: Step-level durable record with CAS `version`, `worker_id`, `lease_id`, `lease_start`, `lease_expires`, `heartbeat_at`, `attempt_number`, `result_json`, `evidence_json`, `error_message`.
- `supervised_checkpoints`: Durable graph snapshots with `checkpoint_id`, `boundary_type`, `graph_state_json`, `budget_state_json`, `timestamp`.
- `supervised_recovery_decisions`: Immutable audit log of every orphan recovery evaluation.
- `supervised_idempotency_records`: Tracks operation tokens by `(task_id, step_id, attempt_number, operation_name, worker_id, status, result_json)`.

---

## 4. Step State Machine

The step state machine enforces strict, validated transitions defined in `VALID_STEP_TRANSITIONS`:

```text
                    PENDING
                       ↓
                     READY ──────────────┐
                       ↓                 │
                    CLAIMED              │
                       ↓                 │
                    RUNNING ─────────────┤
                    /     \              │
                   /       \             │
              VERIFYING   ORPHANED       │
                 /  \        ↓           │
                /    \   RECOVERING      │
               /      \      ↓           │
          SUCCEEDED   RETRYING           │
          (Terminal)     ↓               │
                         └───────► CANCELLED (Terminal)
```

### Transition Validation Rules
- `PENDING` → `READY`, `SKIPPED`, `CANCELLED`
- `READY` → `CLAIMED`, `SKIPPED`, `CANCELLED`
- `CLAIMED` → `RUNNING`, `ORPHANED`, `FAILED`, `CANCELLED`
- `RUNNING` → `VERIFYING`, `REPAIRING`, `ORPHANED`, `FAILED`, `CANCELLED`, `SUCCEEDED`
- `VERIFYING` → `SUCCEEDED`, `REPAIRING`, `FAILED`, `ORPHANED`, `CANCELLED`
- `ORPHANED` → `RECOVERING`, `FAILED`, `CANCELLED`
- `RECOVERING` → `RETRYING`, `RUNNING`, `VERIFYING`, `FAILED`, `CANCELLED`
- `RETRYING` → `READY`, `CLAIMED`, `RUNNING`, `FAILED`, `CANCELLED`
- `SUCCEEDED`: Permanently immutable terminal state.
- `CANCELLED`: Terminal state enforcing cancel-wins semantics.

---

## 5. Lease Model & Compare-And-Set (CAS) Claiming

Every executing worker must hold a valid `StepLease`:
```python
@dataclass
class StepLease:
    step_id: str
    task_id: str
    worker_id: str
    lease_id: str
    lease_start: float
    lease_expires: float
    heartbeat_at: float
    attempt_number: int
    lease_duration_seconds: float = 30.0
```

### CAS Claim Invariants
1. Step must be in `READY`, `PENDING`, or `RETRYING`.
2. Atomic SQL update:
   ```sql
   UPDATE supervised_steps
   SET status = 'CLAIMED',
       version = version + 1,
       worker_id = ?,
       lease_id = ?,
       lease_start = ?,
       lease_expires = ?,
       heartbeat_at = ?,
       attempt_number = ?,
       updated_at = ?
   WHERE task_id = ? AND step_id = ? AND version = ?
   ```
3. If rowcount is 0, `DuplicateClaimError` is raised. Concurrent workers cannot dual-claim.
4. Mutating operations (`start_step`, `record_tool_batch`, `start_verification`, `complete_step`, `fail_step`) check `_verify_lease_holder`. If `lease_id` does not match, worker ID does not match, or `time.time() > lease_expires`, `StaleLeaseError` is raised.

---

## 6. Heartbeat & Orphan Detection

Workers must periodically invoke `supervisor.heartbeat_step(...)`:
- Verifies lease holder validity.
- Advances `lease_expires = now + lease_duration`.
- Updates `heartbeat_at = now`.
- Emits `step.heartbeat`.

### Orphan Detection Algorithm (`detect_orphans`)
A supervisor background sweep or recovery scan evaluates all non-terminal active steps (`CLAIMED`, `RUNNING`, `VERIFYING`).
A step is flagged as `ORPHANED` when:
$$(now - heartbeat\_at > heartbeat\_timeout) \land (now > lease\_expires)$$
- Transitions status to `ORPHANED`.
- Increments `version`.
- Emits `step.lease_expired` and `step.orphaned`.
- Prevents false positives by requiring BOTH conditions before classifying as orphan.

---

## 7. Recovery Decision Engine

When a worker disappears or crashes, `RecoveryDecisionEngine.evaluate(...)` deterministically evaluates the orphaned step across 6 priority rules:

| Priority | Condition | Decision | Rationale |
|:---:|:---|:---:|:---|
| 1 | Task is `CANCELLED` or `STOPPED` | `FAIL_CLOSED` | Cancel-wins invariant; cancelled tasks terminate immediately. |
| 2 | Verifier state passed (`passed=True`) prior to crash | `RESUME` | Work and verification completed; resume to commit results safely. |
| 3 | HITL gate pending (`waiting_approval=True`) | `WAIT` | Dangerous operation awaiting human gate; do not retry blindly. |
| 4 | HITL rejected | `FAIL_CLOSED` | Operator rejected action. |
| 5 | RunBudget exhausted | `FAIL_CLOSED` | Hard resource limit reached. |
| 6 | Attempt number $\ge$ max retries | `FAIL_CLOSED` | Retry budget exhausted. |
| 7 | Unverified mutations present $\land$ non-idempotent | `FAIL_CLOSED` | Prevent corrupting repository with repeated non-idempotent mutations. |
| 8 | Clean or idempotent $\land$ retries remaining | `RETRY` | Safe to retry; transitions to `RETRYING` then `READY` for new claimant. |

---

## 8. Idempotency Identity & Deduplication

To distinguish legitimate retries from duplicate in-flight workers:
- **Token Generation**:
  $$\text{token} = \text{SHA256}(task\_id : step\_id : attempt\_number : operation\_name : \text{sorted\_json}(params))$$
- **`check_or_record_operation`**:
  - If status is `COMMITTED`: Returns `(True, result_json)`. Side effect is skipped and cached result returned.
  - If status is `STARTED` and worker ID differs for the *same attempt*: Raises `DuplicateWorkerExecutionError`.
  - If new: Inserts record with status `STARTED` and allows execution.
- **`commit_operation`**: Marks record `COMMITTED` with `result_json`.
- Legitimate retries (attempt $N+1$) generate distinct tokens, allowing safe new attempts.

---

## 9. Checkpointing & Reconstitution

Checkpoints are captured at 7 authoritative boundaries:
1. `plan_validation`: Initial plan and DAG registered.
2. `step_claim`: Step claimed by worker.
3. `step_start`: Step begins active execution.
4. `tool_batch_completed`: Tool batch finished.
5. `verification_start`: Verification underway.
6. `step_succeeded`: Step verified and committed.
7. `step_failed`: Step failed or failed closed.

### Reconstitution Across Simulated Process Crash
`supervisor.reconstitute_execution(task_id)`:
1. Loads latest checkpoint from `supervised_checkpoints`.
2. Restores `ExecutionPlan` and `ExecutionGraph`.
3. Reconciles step statuses and outputs with `supervised_steps`.
4. Successfully completed steps remain immutable with outputs preserved.
5. Detects crashed in-flight steps and exposes them for orphan recovery.
6. Emits `execution.resumed`.

---

## 10. Cancellation Races (Cancel-Wins Semantics)

Cancel-wins semantics are strictly enforced:
- `cancel_task` atomically sets task status to `CANCELLED` and marks all non-terminal steps `CANCELLED`.
- If a late worker attempts `complete_step`, `claim_step`, or `heartbeat_step` after cancellation, `TaskCancelledError` is raised and the commit is rejected.
- Orphan recovery evaluating a cancelled task immediately returns `FAIL_CLOSED`.
- Succeeded steps prior to cancellation remain immutable.

---

## 11. RunBudget & HITL Integration

- **RunBudget Compliance**:
  - `RecoveryDecisionEngine` inspects `RunBudget.usage` and `check()`.
  - Retries consume attempt counters.
  - Exhausted budget terminates recovery fail-closed.
  - Checkpoints persist budget snapshots (`budget_state_json`).
- **HITL Integration**:
  - Dangerous commands awaiting approval never auto-retry.
  - Crashed workers leave the gate in `WAIT` status.
  - Approved actions resume without duplicate prompt.
  - Rejected actions terminate `FAIL_CLOSED`.

---

## 12. Observability & Replay Events

The supervisor generates scrubbed typed replay events:
- `step.claimed`: Task ID, step ID, worker ID, lease ID, attempt number, lease expires.
- `step.heartbeat`: Heartbeat timestamp, renewed lease expiry.
- `step.lease_expired`: Expired lease ID and timestamp.
- `step.orphaned`: Orphaned step and worker ID.
- `step.recovery_started`: Recovery decision and rationale.
- `step.recovered`: Successful completion of recovered step.
- `step.retry_scheduled`: Scheduled retry attempt number and reason.
- `checkpoint.created`: Boundary type and checkpoint ID.
- `execution.resumed`: Reconstituted step count and completed step list.

---

## 13. Security & Governance Invariants

- **Lease Exclusivity**: Only current lease holder can update step state.
- **Stale Write Prohibition**: Expired workers cannot commit results (`StaleLeaseError`).
- **Verifier Inviolability**: Model completion alone never marks a step `SUCCEEDED`. Verification criteria must pass.
- **Successful Step Immutability**: `SUCCEEDED` steps cannot be overwritten, deleted, or re-executed.
- **Fail-Closed Default**: Ambiguous state, exhausted budget, or non-idempotent side effects fail closed.
- **Secret Scrubbing**: Payloads and error messages scrubbed before persistence or event emission.

---

## 14. Test Coverage & Lineage Accounting

### Test Execution Results
- **Phase 20 Test File**: `tests/integration/test_phase20_durable_execution.py`
- **Phase 20 Tests Run**: 48 passed, 0 failed.

| Category | Tests | Status |
|:---|:---:|:---:|
| 1. Supervisor Core Lifecycle & Transitions | 5 | PASSED |
| 2. Lease Semantics & CAS Claiming | 8 | PASSED |
| 3. Orphan Detection & Deterministic Crash Recovery | 9 | PASSED |
| 4. Idempotency Identity & Deduplication | 5 | PASSED |
| 5. Checkpointing & Graph Reconstitution | 5 | PASSED |
| 6. Dependency DAG Recovery & Immutability | 3 | PASSED |
| 7. Cancellation Races (Cancel-Wins) | 5 | PASSED |
| 8. RunBudget Integration | 3 | PASSED |
| 9. Human-In-The-Loop (HITL) Safety | 4 | PASSED |
| 10. End-to-End Durable Execution & Crash Simulation | 1 | PASSED |
| **Total Phase 20 Tests** | **48** | **ALL PASSED** |

### Global Test Accounting Verification
- **Baseline Prior to Phase 20**:
  - Collected: 1060
  - Passing: 1039
  - Docker-dependent Failures: 21 (Docker daemon not running in local test environment)
- **Phase 20 Added**: +48 tests
- **Current Global Suite**:
  - Total Collected: 1108
  - Passing: 1087
  - Docker Failures: 21 (100% preserved; no tests skipped, deleted, or weakened)
  - Regressions: **0**

Exact arithmetic:
$$1039 \text{ (baseline passing)} + 48 \text{ (Phase 20)} = 1087 \text{ passing}$$
$$1060 \text{ (baseline collected)} + 48 \text{ (Phase 20)} = 1108 \text{ collected}$$

---

## 15. Files Changed

1. `backend/execution/orchestration/execution_plan.py`:
   - Added `CLAIMED`, `ORPHANED`, `RECOVERING`, `RETRYING`, `CANCELLED`, `REPAIRING` to `PlanStepStatus`.
   - Added `CANCELLED` to `PlanStatus`.
   - Added `revision_count` property to `ExecutionPlan`.
   - Added lease/worker fields to `PlanStep`: `worker_id`, `lease_id`, `lease_expires`, `heartbeat_at`, `idempotency_token`.
   - Updated `as_dict()` and `from_dict()` serialization.

2. `backend/execution/orchestration/durable_supervisor.py` *(NEW)*:
   - Full implementation of `DurableExecutionSupervisor`, `DurableSupervisorStore`, `RecoveryDecisionEngine`.
   - Models: `StepLease`, `RecoveryDecision`, `RecoveryEvaluation`, `ExecutionCheckpoint`, `DurableStepRecord`.
   - Exceptions: `StaleLeaseError`, `DuplicateClaimError`, `InvalidStepTransitionError`, `DuplicateWorkerExecutionError`, `TaskCancelledError`, `RecoveryFailedError`.

3. `backend/execution/orchestration/__init__.py`:
   - Exported all Phase 20 classes, enums, and exceptions.

4. `backend/execution/agent_runtime/runtime.py`:
   - Integrated `DurableExecutionSupervisor` into `RealAgentRuntime.__init__` and `execute_plan_step`.

5. `backend/routes/tasks.py`:
   - Registered generated execution plans and graphs with `DurableExecutionSupervisor` in `run_task_pipeline`.
   - Attached supervisor to `TaskExecutionContext`.

6. `tests/integration/test_phase20_durable_execution.py` *(NEW)*:
   - 48 comprehensive integration tests covering all required categories.

---

## 16. Known Limitations & Future Work

1. **Distributed Multi-Host Heartbeat Granularity**: SQLite WAL provides single-host multi-process concurrency. In a multi-host distributed cluster, distributed consensus (e.g. Postgres advisory locks or Raft) can complement this architecture.
2. **Dynamic Worktree Auto-Remount on Remote Host**: Process restart simulation currently assumes workspace filesystem persistence on the same node.

---

## 17. Current System Status

```text
================================================================================
FINAL VERDICT:
ENGINEERING COMPLETE — READY FOR NEXT ENGINEERING PHASE
Independent Auditor: NOT INVOKED
Strictly NO certification designation.
================================================================================
```
