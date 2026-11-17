# AETHERION AI — LEGACY WORKTREE OPTION B GUARD INDEPENDENT AUDIT

**Audit type:** Read-only independent security verification  
**Audit date:** 2026-10-13  
**Project root:** `C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli`  
**Release scope:** Option B — control-plane release; agent execution disabled  
**Independent verdict:** **PARTIAL**

## 1. Executive verdict

The reported guards on the four named legacy worktree lifecycle helpers are
**VERIFIED**. Their policy assertions occur before their relevant registry,
tenant, filesystem, child-process, Git, and metadata side effects. The direct
execution-context cleanup fallback is also **VERIFIED** for contexts that carry
a worktree ID.

The implementation report's unconditional PASS is not independently accepted as
complete because two additional direct mutation boundaries remain:

1. **P1 — `_terminate_children()` is unguarded.** A direct caller can reach
   `proc.terminate()` / `proc.kill()` under Option B. The normal caller,
   `discard_worktree()`, is guarded, but the helper itself is an independently
   callable physical cleanup boundary.
2. **P1 — `get_execution_diff()` performs `git add -N .` without a policy
   assertion.** This mutates the worktree's Git index before producing a diff.
   A safe mocked probe reached that subprocess call while Option B was active.

The shared `_git()` dispatcher correctly guards all four reported physical Git
actions, but the repository's seven-test file directly tests only
`worktree_add`. A separate read-only audit probe covered all four actions;
committed direct negative coverage remains incomplete (**P2**).

No P0 was found. The current public route graph and normal runtime wrappers do
not demonstrate an HTTP bypass, but direct internal invocation of the two P1
boundaries is demonstrated by source inspection and safe mocks.

## 2. Audit scope and method

Inspected and preserved:

- `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FIX_REPORT.md`;
- `AETHERION_PHASE_2A_POST_IMPLEMENTATION_INDEPENDENT_SECURITY_AUDIT.md`;
- `AETHERION_PHASE_2A_CONTROL_PLANE_CONTRACT_FIX_REPORT.md`;
- `backend/execution/worktree.py`;
- `backend/execution/agent_runtime/context.py`;
- `backend/execution/control_plane_policy.py`;
- `tests/integration/test_legacy_worktree_option_b_guards.py`; and
- relevant task, route, store, runtime, and wrapper callers.

The audit was read-only. No source, test, configuration, environment,
deployment, or existing report file was modified. The only project artifact
created by this audit is this report. No credentials or populated `.env` values
were read, printed, copied, or reused. No Docker, service, database, Redis,
worker, migration, deployment, image, commit, push, or real physical worktree
mutation was performed.

Evidence labels used below:

- **VERIFIED** — directly observed in current source or a safe command result;
- **PARTIALLY VERIFIED** — core behavior is present but an alternate path or
  evidence requirement remains incomplete;
- **NOT VERIFIED** — no trustworthy evidence was obtained;
- **FAILED** — a safe verification produced a failure;
- **HISTORICAL** — taken from an earlier artifact and not recreated here; and
- **INFERENCE** — a reasoned conclusion rather than direct runtime proof.

## 3. Workspace state and source integrity

### 3.1 Current workspace identity

Recorded before creating this report:

| Item | Current evidence |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| `git status --porcelain=v1 -uall` entries | 2,229 |
| Untracked entries | 2,228 |
| Modified/staged entries | 1 — `backend/routes/d3_features.py` |
| Exact audit report before audit | Absent |
| Implementation report | Present |
| Prior Phase 2A independent audit | Present |
| Prior Phase 2A implementation report | Present |

The workspace contains extensive untracked content. A clean Git HEAD or a
partial `git diff` is therefore not treated as a complete change inventory.
Relevant current status entries are the pre-existing D3 modification, the two
legacy source files, the focused test, and the implementation report. The
current audit report is added only after this pre-report state was recorded.

### 3.2 Current hashes

The following current hashes match the implementation report's claimed final
values exactly:

| File | Current SHA-256 | Result |
|---|---|---|
| `backend/execution/worktree.py` | `e356c886d0ea4b37d1280dc2c8f76363c2a8a59208a80c123ba68efdd9e5114c` | **VERIFIED** |
| `backend/execution/agent_runtime/context.py` | `8f825d254f28139e835dfc50ad5dba23d9830a20545b7a7a00a6757ed9adce55` | **VERIFIED** |
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | **VERIFIED preserved** |
| `tests/integration/test_legacy_worktree_option_b_guards.py` | `af28e1668b20e5231f1f12208c870d27058dadcf5ae6f667039448ebcb07eace` | **VERIFIED** |

The historical pre-change values were also present in the inspected temporary
custody manifest:

| File | Reported pre-change SHA-256 | Custody result |
|---|---|---|
| `backend/execution/worktree.py` | `501a013de25b0d103d381ba5945057e2fda2947f9a7521c66c03db8f32e234c4` | **HISTORICAL; manifest entry matches** |
| `backend/execution/agent_runtime/context.py` | `923fb3fb2d8db5fbef66a2f48f6748aecb4a82923ba6f6732f51ca646788fe93` | **HISTORICAL; manifest entry matches** |

This audit did not recreate the pre-change snapshot and cannot provide a
cryptographically complete provenance record for the entire untracked tree.

## 4. Policy contract

`backend/execution/control_plane_policy.py:98-101` defines the authoritative
assertion. `get_control_plane_policy()` always returns
`execution_enabled=False` in the Option B source lock, regardless of environment
values. The rejection contract is:

```text
ExecutionDisabledError
Agent execution is disabled for the approved control-plane release.
```

The isolated commands set explicit test-only environment values and never use a
populated credential. The current workspace lacks the optional
`security.command_policy`, `execution.tool_governance`, `infra.tenant`, and
related integration modules referenced by `worktree.py`; the module therefore
fails closed at `_require_governance()` for Git dispatch. The new Option B
assertions were tested before that unavailable-dependency check.

## 5. Four helper guard-order matrix

| Boundary | Current source ordering | Side effects checked | Result |
|---|---|---|---|
| `create_worktree()` `worktree.py:421-498` | `assert_agent_execution_enabled()` at line 438, then governance at line 439 | Registry read, tenant resolution, workspace confinement, `os.makedirs`, repo resolution, lock acquisition, `git worktree add`, registry write, events and telemetry | **VERIFIED** |
| `discard_worktree()` `worktree.py:540-609` | Assertion at line 548 before `get_worktree()` and `_terminate_children()` | Registry lookup/status, child termination, Git remove/prune/branch deletion, `shutil.rmtree`, status/event/telemetry mutation | **VERIFIED** |
| `sweep_session()` `worktree.py:672-683` | Assertion at line 674 before registry scan and delegated discard | Registry access, calls to `discard_worktree()`, and all delegated cleanup effects | **VERIFIED** |
| `sweep_orphans()` `worktree.py:694-778` | Assertion at line 720 before cutoff calculation, registry scan, list, prune, or discard | Child/worktree discard, `git worktree list`, `git worktree prune`, error/report paths | **VERIFIED** |

The four assertions precede their relevant physical lifecycle effects. The
`_git()` assertion is an additional defense for the four mutation action names.

### 5.1 Additional worktree-module boundaries

- `_terminate_children()` at `worktree.py:518-537` is an actual physical
  cleanup helper: it calls `proc.terminate()` at line 528 and `proc.kill()` at
  line 532. It has **no central policy assertion**. Its only current source
  caller is the guarded `discard_worktree()` path, but a direct import/call is
  demonstrably possible and was safely probed. This is finding **P1-1** below.
- `attach_process()` only updates the process-local `_PROCS` registry; it does
  not terminate a process or launch a command. It is not an independent
  physical mutation boundary for this audit.
- `get_worktree()` and `list_worktrees()` are registry reads and remain usable.
- `worktree_diff()` and `list_git_worktrees()` are intended reads routed through
  `_git()` with non-mutating action names. Their current availability is
  limited by the missing optional governance modules noted in §4; no mutation
  was attempted.
- `register_orphan_sweep()` creates/adopts a scheduler metadata task whose
  handler is `execution.worktree.sweep_orphans`; the callback itself is guarded.
  Scheduler registration is not a direct physical worktree mutation.

## 6. `_git()` mutation-action coverage matrix

`worktree.py:131-138` defines the exact mutation set:

```text
worktree_add, worktree_remove, worktree_prune, branch_delete
```

At `worktree.py:297-299`, `_git()` calls
`assert_agent_execution_enabled()` before `_require_governance()`, action
dispatch, safe subprocess argument construction, and `subprocess.run()` at line
336.

| Action | Current physical caller(s) | Source guard | Existing focused direct test | Independent mocked probe |
|---|---|---|---|---|
| `worktree_add` | `create_worktree():467-473` | **VERIFIED** | **YES**, test line 115 | **PASS** |
| `worktree_remove` | `discard_worktree():561-564` and context fallback uses direct subprocess | **VERIFIED in `_git()`** | **NO** | **PASS** |
| `worktree_prune` | discard recovery `572-575`; orphan sweep `754` | **VERIFIED** | **NO** | **PASS** |
| `branch_delete` | `discard_worktree():583-585` | **VERIFIED** | **NO** | **PASS** |

The independent probe called each action with mocked governance and process
execution. Each raised `ExecutionDisabledError` with the exact stable message;
`_require_governance`, `get_tool_governance`, and `subprocess.run` were all
asserted not to be called.

### 6.1 Action aliases, unknown actions, and direct subprocess paths

All current `_git()` call sites use the seven literal action names listed in the
source: `rev_parse`, `worktree_add`, `worktree_remove`, `worktree_prune`,
`branch_delete`, `worktree_diff`, and `worktree_list`. No current caller uses an
alias for one of the four physical actions.

The `_git()` function does not explicitly reject unknown action names or verify
that `argv` matches the action. In the current workspace the optional governance
module is absent, so `_require_governance()` fails closed before an unknown
action can launch a process. The behavior with a separately supplied
governance implementation is **NOT VERIFIED**; the current caller inventory
does not demonstrate a reachable unknown-action bypass. This is recorded as a
lower-priority hardening/test observation, not as a P1 bypass.

Separate direct subprocess inspection found:

- `context.py:232-238` (`git worktree add`) is behind
  `attach_execution_worktree()`'s line-179 assertion;
- `context.py:301-313` (`git worktree remove` and `git branch -D`) is behind
  `cleanup_execution_worktree()`'s line-283 assertion;
- `d3_store.py:602-607`, `:690-710` are behind assertions at `:586` and `:677`;
- route-level `git --version` and diff calls are read-only; and
- `context.py:414` contains a separate `git add -N .` mutation not protected by
  this fix. That path is finding **P1-2** below.

## 7. Execution-context cleanup fallback

`cleanup_execution_worktree()` at `context.py:267-323` behaves as follows:

1. With no `context.worktree_id`, it returns the non-cleanup result at line 278.
   This is before the policy assertion, but no Git, filesystem, child-process,
   or helper mutation occurs on that branch.
2. With a worktree ID, it asserts at line 283 before importing/calling
   `discard_worktree()` or evaluating the direct fallback.
3. The normal helper is called at lines 285-290. Its exception handler at
   lines 291-292 cannot be reached until after the policy check.
4. The direct Git fallback at lines 294-316 performs worktree removal and
   branch deletion only after the same policy check.

The existing test patches both `backend.execution.worktree.discard_worktree`
and global `subprocess.run`, asserts the policy exception, and confirms neither
was called. The temporary context workspace remains empty. This boundary is
therefore **VERIFIED**.

`attach_execution_worktree()` is also guarded at `context.py:179`, before its
normal helper and direct `git worktree add` fallback at lines 220-247. The
normal runtime constructor and `run_stream()` are separately guarded at
`runtime.py:180` and `runtime.py:469`.

## 8. Direct `get_execution_diff()` mutation boundary

`context.py:395-438` is not part of the four named lifecycle helpers, but it is
an alternate worktree-related path. When a worktree context exists, it runs:

```text
git add -N .
git diff <base_ref>
```

The intent-to-add command mutates the worktree's private Git index. There is no
`assert_agent_execution_enabled()` before this branch. The runtime caller is
normally protected by `RealAgentRuntime` construction/run guards, but a direct
caller of `get_execution_diff()` is not.

A safe isolated probe used a temporary directory and mocked `subprocess.run`;
it observed the exact `git add -N .` call while Option B was active. No real Git
command or filesystem mutation was performed. This is a demonstrated direct
internal bypass, not merely a hypothetical caller concern.

## 9. Caller and alternate-path analysis

### 9.1 Legacy helper callers

- `backend/routes/tasks.py:run_task_pipeline()` blocks durable execution and
  raises before workspace creation, provider resolution, queueing, or runtime
  construction.
- `backend/routes/tasks.py:reject_task()` calls
  `cleanup_execution_worktree()`; the fallback is guarded as described above.
- `context.py:attach_execution_worktree()` calls `create_worktree()` and has a
  guarded direct Git fallback.
- `sweep_session()` and `sweep_orphans()` delegate to guarded discard paths and
  have their own assertions.
- `register_orphan_sweep()` only registers the guarded orphan-sweep callback.

No current route, worker, retry, replay, recovery, or scheduler caller was
found that invokes the four named lifecycle helpers after bypassing their
current assertions.

### 9.2 Public D3 and store paths

- D3 POST routes for create, discard, session sweep, and orphan sweep return
  the disabled-policy response before invoking the store.
- D3 store `create_real_worktree()`, `discard_worktree()`, `sweep_worktrees()`,
  and `sweep_orphans()` assert before database, filesystem, subprocess, or
  branch operations.
- D3 status/list/get/diff routes are read-oriented. `git --version` and diff
  inspection do not create, remove, prune, or delete branches.
- `routes/diff.py` invokes direct `git diff` only, or the read-only
  `worktree_diff()` helper. It does not launch a worktree lifecycle mutation.

### 9.3 Remaining alternate path

The proven alternate physical paths are `_terminate_children()` and
`get_execution_diff()` as described in findings P1-1 and P1-2. They are not
demonstrated as reachable through the current public HTTP routes, but both are
callable from imported Python code without the central assertion.

## 10. Focused tests and safe execution results

### 10.1 Existing seven-test file

`tests/integration/test_legacy_worktree_option_b_guards.py` contains seven
pytest cases after parametrization:

| Coverage | Assertions | Result |
|---|---|---|
| Import plus `list_worktrees()` / `get_worktree()` | Read-only calls remain available | Covered |
| `create_worktree()` | Exact exception; governance, `_git`, tenant, subprocess, `makedirs` not called; temp directory empty | Covered |
| `discard_worktree()` | Exact exception; `_git`, subprocess, `rmtree`, child termination not called | Covered via parameterization |
| `sweep_session()` | Same downstream non-call assertions | Covered via parameterization |
| `sweep_orphans()` | Same downstream non-call assertions | Covered via parameterization |
| `cleanup_execution_worktree()` | Exact exception; primary helper and direct `subprocess.run` fallback not called; temp directory empty | Covered |
| Direct `_git()` | `worktree_add` rejects before governance/process | Covered |

The cleanup-helper parameterized test does not create a registered real
worktree; its side-effect protection is established by mocked downstream calls.
It is still meaningful rejection-order coverage, but it does not cover the
unguarded direct `_terminate_children()` function.

### 10.2 Independently executed commands

All commands used `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`, explicit
test-only policy values, disposable SQLite paths where relevant, and disposable
pytest basetemp paths. Observed results:

```text
python -m pytest -q -p no:cacheprovider --basetemp=<disposable-temp> \
  tests/integration/test_legacy_worktree_option_b_guards.py
7 passed in 9.64s
```

```text
python -m pytest -q -p no:cacheprovider --basetemp=<disposable-temp> \
  tests/integration/test_phase2_control_plane_execution_policy.py
10 passed in 2.83s
```

```text
python -m pytest -q -p no:cacheprovider --basetemp=<disposable-temp> \
  tests/integration/test_phase2a_control_plane_contract.py
22 passed in 2.60s
```

Additional non-project-output probes:

- all four `_git()` mutation actions: **PASS**, each rejected before mocked
  process launch;
- direct `_terminate_children()` with a fake process: **CONFIRMED** it reached
  `terminate()` under Option B;
- direct `get_execution_diff()` with mocked process execution: **CONFIRMED** it
  reached `git add -N .` under Option B.

The last two probes intentionally demonstrated the findings without launching
a real process or mutating a real repository.

## 11. Findings and closure evidence

### P0 findings

**None identified.** No public HTTP bypass or immediate arbitrary Git mutation
was demonstrated through the current route graph.

### P1-1 — unguarded direct child-process cleanup

**Status:** **FAILED** for complete boundary closure  
**Severity:** P1  
**Location:** `backend/execution/worktree.py:_terminate_children()` at lines
518-537; physical calls at lines 528 and 532

- **Observed behavior:** The helper pops registered processes and calls
  `proc.terminate()`, then `proc.kill()` after a timeout. It has no
  `assert_agent_execution_enabled()` call.
- **Impact:** A direct internal caller can terminate an agent-related child
  process while Option B is active. The normal `discard_worktree()` path is
  protected, but the helper itself is a callable cleanup boundary and does not
  enforce the source lock.
- **Evidence:** Current-source inspection plus the safe fake-process probe,
  which reported `DIRECT_TERMINATE_CHILDREN_REACHED_UNDER_OPTION_B=CONFIRMED`.
  Search found no current caller outside `discard_worktree()`, so the public
  route bypass is not demonstrated; the direct internal bypass is demonstrated.
- **Minimal recommended correction:** Add the central Option B assertion at the
  start of `_terminate_children()` or otherwise make the helper inaccessible
  except through an asserted cleanup boundary.
- **Closure artifact required:** A focused direct-helper test that registers a
  fake process, invokes `_terminate_children()` with execution disabled, and
  asserts `terminate()`, `kill()`, and `wait()` are never called.

### P1-2 — unguarded Git-index mutation in `get_execution_diff()`

**Status:** **FAILED** for complete physical mutation closure  
**Severity:** P1  
**Location:** `backend/execution/agent_runtime/context.py:get_execution_diff()`
at lines 395-438; direct `subprocess.run(["git", "add", "-N", "."])` at lines
414-419

- **Observed behavior:** With a worktree context, the function reaches
  `git add -N .` before `git diff`, without calling the central policy assertion.
  `git add -N` changes Git index metadata even though the function is named and
  documented as diff inspection.
- **Impact:** A direct caller can cause a repository/worktree metadata mutation
  under Option B. The normal runtime caller is behind runtime guards, but the
  function itself is an alternate imported boundary.
- **Evidence:** Safe temporary-directory probe with mocked `subprocess.run`
  observed the exact `git add -N .` argv while the Option B policy was active.
  No real Git process launched and no real repository changed.
- **Minimal recommended correction:** Remove the index-mutating intent-to-add
  step from the read path, or put an explicit policy-controlled boundary before
  it and preserve a genuinely read-only diff path for the disabled release.
- **Closure artifact required:** A direct `get_execution_diff()` test with
  execution disabled that proves no process launch/index mutation occurs, plus
  a separate test proving supported read-only diff inspection remains usable.

### P2-1 — incomplete direct negative coverage for `_git()` actions

**Status:** **PARTIALLY VERIFIED**  
**Severity:** P2  
**Location:** `tests/integration/test_legacy_worktree_option_b_guards.py:109-118`

- **Observed behavior:** The committed focused test directly exercises only
  `_git("worktree_add", ...)`. The other three mutation actions are covered by
  source classification and the independent uncommitted audit probe, not by
  repository test cases.
- **Impact:** A future edit could remove or rename one action from
  `_PHYSICAL_GIT_MUTATIONS` without a direct regression test for that action.
- **Evidence:** Test file inspection plus the all-four-action mocked probe.
- **Minimal recommended correction:** Parameterize the existing direct `_git`
  test over `worktree_add`, `worktree_remove`, `worktree_prune`, and
  `branch_delete`.
- **Closure artifact required:** The focused test file should report four
  direct action cases, each asserting the exact exception and no governance or
  process-launch call.

### P2-2 — unknown `_git()` action handling is not explicit

**Status:** **NOT VERIFIED** as a reachable bypass; **INFERENCE** as a
hardening gap  
**Severity:** P2  
**Location:** `backend/execution/worktree.py:_git()` at lines 274-336

- **Observed behavior:** The dispatcher checks membership only for the four
  known physical action strings, then passes any other `action` and caller-built
  `argv` to the optional governance stack. It has no explicit unknown-action
  rejection or action-to-argv consistency check.
- **Impact:** If a future caller supplies an unknown action with mutating Git
  argv and the separately referenced governance stack permits it, the central
  mutation assertion may not run. No such current caller was found, and the
  current workspace's optional governance imports are absent and fail closed.
- **Minimal recommended correction:** Reject unknown actions before governance
  dispatch, or use a closed action-to-command table that classifies all physical
  mutations and does not accept arbitrary caller-built mutation argv.
- **Closure artifact required:** A test for unknown mutation-shaped actions that
  proves they fail before `subprocess.run`, plus an explicit supported-action
  table review.

## 12. Preservation, provenance, and verification limitations

- The implementation report and prior Phase 2A audit were preserved and
  inspected; neither was overwritten.
- Current source/test hashes match the implementation report, and the D3 hash
  remains identical to the reported preserved value.
- The current `-uall` status has 2,229 entries, of which 2,228 are untracked.
  The status is therefore evidence of the current workspace state, not a full
  attribution of every historical untracked file.
- The pre-change worktree/context hashes match entries in the existing custody
  manifest, but this audit did not recreate or cryptographically sign that
  snapshot.
- Only the focused legacy test and the two relevant control-plane regression
  suites were run. The full repository suite was not run.
- The optional governance/policy integration modules referenced by
  `worktree.py` are not present in the current project tree. This prevented a
  real read-only `list_git_worktrees()` / `worktree_diff()` subprocess test; the
  registry-only `list_worktrees()` and `get_worktree()` checks passed. No real
  Git mutation was attempted.
- No Docker, service, database, migration, deployment, immutable packaging,
  credential, or Hermes verification was performed.

## 13. Canonical master 10-phase mapping

This bounded audit concerns the execution-disabled alternative associated with
canonical master Phase 4. It does not advance or certify later phases.

| Master phase | Current status |
|---|---|
| 1 — deployment scope and architecture | Existing decision record; not re-certified here |
| 2 — credential security and production configuration | **Pending; not started** |
| 3 — immutable source and release baseline | **Pending; provenance remains incomplete** |
| 4 — sandbox boundary or approved execution-disabled alternative | **PARTIAL; four named helpers pass, two direct alternate mutation boundaries remain P1** |
| 5 — current release tests | **Bounded focused tests verified; broader release suite pending** |
| 6 — immutable image build and registry publication | **Pending; not started** |
| 7 — infrastructure provisioning | **Pending; not started** |
| 8 — database migration and candidate deployment | **Pending; not started** |
| 9 — security, functional, and recovery acceptance | **Pending; not started** |
| 10 — release freeze and go/no-go | **Pending; not started** |

## 14. One recommended next action

Do not proceed to later phases. Perform one narrowly scoped follow-up for the
legacy worktree boundary: guard `_terminate_children()`, remove or policy-gate
the `get_execution_diff()` `git add -N` mutation while retaining read-only diff
behavior, and expand the focused negative test to all four `_git()` mutation
actions plus the two direct alternate paths. Re-audit the resulting source and
test evidence before changing the verdict to PASS.
