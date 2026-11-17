# Phase 16: Live Budget Enforcement & Adaptive Escalation Report

**Document Title:** Phase 16: Live Budget Enforcement & Adaptive Escalation — Engineering Implementation & Verification Report  
**Status:** Engineering Complete — Ready for Next Engineering Phase  
**Independent Certification:** NOT PERFORMED (Role Boundary: Engineering Verification Only)  
**Date:** 2026-09-25  
**Package:** `backend/execution/orchestration/`  
**Runtime:** `backend/execution/agent_runtime/runtime.py`  
**Pipeline:** `backend/routes/tasks.py`  
**Test Suite:** `tests/integration/test_phase16_live_budget_enforcement.py`  

---

## 1. Executive Summary

Phase 16 bridges the Phase 15 static policy derivation kernel into the **live running agent execution loop**. In Phase 15, tasks were classified and assigned a `RunBudget` and `ModelProfile` prior to execution, but resource limits were not checked in-flight, model calls and tokens were not accounted turn-by-turn, and exhausted budgets could not stop a runaway loop.

Phase 16 completes this operational feedback loop by:
1. **Introducing `BudgetEnforcer`** as the single live coordinating authority for runtime budget enforcement and usage accounting.
2. **Wiring budget checks and turn recording directly into `RealAgentRuntime.run_stream()`**, checking model calls, tool calls, repair attempts, and token budgets on every turn.
3. **Handling `BudgetExhaustedError` cleanly**, emitting structured `budget_exhausted` progress events and persisting `budget.exhausted` and `orchestration.usage_final` replay events.
4. **Capturing real turn metrics** (`turn_index`, `role`, `tokens_in`, `tokens_out`, `elapsed_sec`, `tool_calls`, `was_repair`, `was_escalation`) without fabricating missing provider metrics.
5. **Enabling adaptive escalation** via `EscalationChain` when budget thresholds or failures occur, tracking escalation reasons and preventing runaway retries.
6. **Populating `usage_final` into orchestration evidence** and durable execution state payloads.

All 40 Phase 16 integration tests pass. No regressions were introduced into prior phases (Phase 15: 60/60 pass; Phase 14: 21/21 pass).

---

## 2. Architecture & Invariants

```
+-------------------------------------------------------------------------+
|                          TASK EXECUTION PIPELINE                        |
|                                                                         |
|  1. TaskClassifier -> 2. ExecutionPolicy -> 3. RunBudget (Phase 15)     |
|                                                     |                   |
|                                                     v                   |
|  4. RealAgentRuntime (Phase 16 Live Enforcement)                        |
|     +-----------------------------------------------------------------+ |
|     |  BudgetEnforcer.from_execution_budget(context.execution_budget) | |
|     |                                                                 | |
|     |  LOOP:                                                          | |
|     |    enforcer.start_turn()                                        | |
|     |    response = provider.chat(messages, tools)                    | |
|     |    enforcer.after_model_call(tokens_in, tokens_out, ...)        | |
|     |    enforcer.check()  --> [EXCEEDED? -> BudgetExhaustedError]    | |
|     |                                           |                     | |
|     |    execute_tool(...)                      v                     | |
|     |    enforcer.after_tool_call(...)     Emit: budget.exhausted     | |
|     |    enforcer.check()                  Emit: orchestration.usage  | |
|     |                                      State: FAILED (clean)      | |
|     |    repair_loop(...)                                             | |
|     |    enforcer.after_repair_attempt()                              | |
|     |    enforcer.check_repair()                                      | |
|     +-----------------------------------------------------------------+ |
|                                                                         |
|  5. Final Usage Evidence & Replay Projection (Phase 16)                |
|     - Result payload includes "usage_evidence"                         |
|     - Pipeline response includes "usage_final" in orchestration         |
|     - Replay log records "orchestration.usage_final"                   |
+-------------------------------------------------------------------------+
```

### Safety & Architecture Invariants Preserved
- **Single Authoritative Budget Authority:** Limits originate strictly from `context.execution_budget` and are managed through `BudgetEnforcer` / `RunBudget`. No conflicting secondary limits.
- **One Verifier Truth:** VerificationEngine logic is unmodified; verifier is the sole decider of task correctness.
- **One Runtime:** `RealAgentRuntime` remains the sole execution engine (not replaced, wrapped, or bypassed).
- **Zero HITL Bypass:** Operator approval for dangerous commands remains mandatory; no bypass flags exist.
- **Zero Credential Leakage:** All evidence, replay events, and usage summaries scrub credentials and API keys.
- **Honest Telemetry:** Unreported tokens remain `None`; metrics are never fabricated.

---

## 3. Implementation Details

### A. `backend/execution/orchestration/budget_enforcer.py`
- Implements `BudgetEnforcer` class coordinating `RunBudget` and `UsageAccounting`.
- Factory: `from_execution_budget(execution_budget, task_id, tenant_id, profile_name, rationale)`.
- Lifecycle methods:
  - `start_turn()`: Tracks turn duration with millisecond precision.
  - `after_model_call(...)`: Records model call count, tokens, role, and turn elapsed time.
  - `after_tool_call(...)`: Records tool calls.
  - `after_repair_attempt()`: Records repair attempts.
  - `after_escalation(reason)`: Records escalation reasons and count.
  - `check()`: Enforces all 6 budget dimensions; raises `BudgetExhaustedError` on first breach.
  - `check_repair()` and `check_escalation()`: Targeted checks for specific lifecycle transitions.
  - `final_evidence()`: Returns complete structured evidence dictionary for replay and audit.

### B. `backend/execution/agent_runtime/runtime.py`
- Imported `BudgetEnforcer` and `BudgetExhaustedError` with graceful fallback if unavailable.
- In `run_stream()`:
  - Instantiates `_budget_enforcer` from `self.context.execution_budget`.
  - Sets `self.budget_enforcer = _budget_enforcer` on the instance.
  - Instruments model calls with `start_turn()` before and `after_model_call()` + `check()` after.
  - Catches `BudgetExhaustedError` and transitions task cleanly to `STATE_FAILED` with `failure_class="budget_exhausted"`.
  - Emits `budget_exhausted` progress event and logs `budget.exhausted` replay event.
  - Instruments tool execution with `after_tool_call()`.
  - Instruments repair attempts with `after_repair_attempt()`.
  - Records `orchestration.usage_final` replay event upon both normal completion and early budget exhaustion.
  - Attaches `usage_evidence` into `result_payload` in durable state.

### C. `backend/routes/tasks.py`
- In `run_task_pipeline()`:
  - Retrieves `runtime.budget_enforcer.final_evidence()` upon stream completion.
  - Populates `orch_evidence["usage_final"]` in the returned pipeline response dictionary.

---

## 4. Test Accounting & Lineage Reconciliation

### Test Lineage Reconciliation (Pytest Collection & Execution)
The apparent discrepancy in earlier reports between baseline (826) and post-Phase 15 passing (865) is reconciled as follows:

| Metric | Collected Tests | Passing Tests | Failing Tests | Notes |
|--------|-----------------|---------------|---------------|-------|
| **Pre-Phase 15 Baseline** | 826 | 805 | 21 | 21 pre-existing Docker-inactive tests |
| **Phase 15 Addition** | +60 | +60 | 0 | `test_phase15_model_orchestration.py` (60 tests) |
| **Post-Phase 15 Cumulative** | 886 | 865 | 21 | `805 + 60 = 865 passing`; `826 + 60 = 886 collected` |
| **Phase 16 Addition** | +40 | +40 | 0 | `test_phase16_live_budget_enforcement.py` (40 tests) |
| **Post-Phase 16 Cumulative** | **926** | **905** | **21** | `865 + 40 = 905 passing`; `886 + 40 = 926 collected` |

Exact arithmetic:
* `Collected: 826 (baseline) + 60 (Phase 15) + 40 (Phase 16) = 926 total tests collected`
* `Passing:   805 (baseline) + 60 (Phase 15) + 40 (Phase 16) = 905 total tests passing`
* `Failing:   21 (baseline pre-existing Docker) + 0 (Phase 15) + 0 (Phase 16) = 21 failing`
* `Sum check: 905 passing + 21 failing = 926 total collected` (Exact match)

> **Full Suite Verification Command:**  
> `$env:PYTHONPATH=".;backend;cli"; python -m pytest tests/integration cli/tests -q --tb=no`  
> **Output:**  
> `21 failed, 905 passed, 5 subtests passed in 544.09s (0:09:04)`  
>  
> The 21 failures are 100% pre-existing Docker-unavailable failures (20 in `test_phase35_isolation.py`, 1 in `test_phase3_extensions.py::test_26`).  
> Zero regressions across all historical phases. 40/40 Phase 16 tests passing.

### Phase 16 Breakdown (40 Tests, 8 Classes)
1. **`TestBudgetEnforcerLifecycle` (9 tests):**
   - Defaults initialization
   - Custom budget initialization
   - `start_turn` and `after_model_call` timing/token tracking
   - `after_tool_call` tracking
   - `after_repair_attempt` tracking
   - `after_escalation` tracking
   - `remaining()` calculation
   - `budget_summary()` structure
   - `final_evidence()` schema and secret safety
2. **`TestLiveModelCallEnforcement` (5 tests):**
   - Calls within budget pass
   - Calls exceeding limit raise `BudgetExhaustedError`
   - Structured error dictionary verification
   - Token limit within bounds passes
   - Token limit exceeded raises `BudgetExhaustedError`
3. **`TestLiveToolCallEnforcement` (4 tests):**
   - Tool calls within limit pass
   - Tool calls exceeding limit raise `BudgetExhaustedError`
   - Tool execution failures still count toward budget
   - Exhaustion events logged in enforcer
4. **`TestLiveRepairEnforcement` (4 tests):**
   - Repairs within limit pass
   - Repairs exceeding limit raise `BudgetExhaustedError`
   - Targeted repair checks ignore unrelated limits
   - Repair turns correctly flagged in `UsageAccounting`
5. **`TestLiveEscalationEnforcement` (4 tests):**
   - Escalations within limit pass
   - Escalations exceeding limit raise `BudgetExhaustedError`
   - Targeted escalation check isolation
   - Escalation turns correctly flagged
6. **`TestRuntimeBudgetEnforcement` (5 tests):**
   - Normal execution creates enforcer and emits `orchestration.usage_final`
   - Run aborts cleanly when model call budget exhausted
   - Tool calls recorded in runtime loop
   - Provider tokens captured from `response["usage"]`
   - Final done event contains budget evidence
7. **`TestPipelineBudgetIntegration` (5 tests):**
   - Pipeline returns `usage_final` in orchestration evidence
   - Custom budget passed to pipeline is respected by runtime
   - Replay events contain `orchestration.usage_final`
   - Pipeline aborts on budget exhaustion without crash
   - Ordered replay events (`policy_selected` before `usage_final`)
8. **`TestAdaptiveEscalation` (4 tests):**
   - EscalationChain advances on `BUDGET_EXHAUSTED` reason
   - Terminal chain generates structured evidence
   - Enforcer tracks escalation reason and profile change
   - Escalation budget limit prevents runaway escalations

---

## 5. Definition of Done (DoD) Verification

- [x] `budget_enforcer.py` implemented and exported in `backend/execution/orchestration/__init__.py`
- [x] Live model call limit checked and enforced in `runtime.py`
- [x] Live tool call limit checked and recorded in `runtime.py`
- [x] Live repair attempt limit checked and recorded in `runtime.py`
- [x] Token consumption tracked live from provider response usage dict
- [x] `BudgetExhaustedError` halts runtime loop cleanly with state `FAILED` and `failure_class="budget_exhausted"`
- [x] Replay event `budget.exhausted` recorded with structured limit metadata
- [x] Replay event `orchestration.usage_final` recorded on completion and exhaustion
- [x] Pipeline `tasks.py` populates `usage_final` into orchestration evidence
- [x] All 40 Phase 16 tests pass without skips or xfails
- [x] Zero regressions across Phase 14 and Phase 15 test suites
- [x] Human-in-the-loop, WorkspaceBoundary, and Verifier Truth remain completely intact
