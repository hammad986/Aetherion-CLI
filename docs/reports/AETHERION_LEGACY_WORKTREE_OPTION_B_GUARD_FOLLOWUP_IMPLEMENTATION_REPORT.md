# AETHERION AI — LEGACY WORKTREE OPTION B GUARD FOLLOW-UP IMPLEMENTATION REPORT

**Task:** Phase 2 Option B residual boundary hardening  
**Implementation date:** 2026-10-16  
**Release scope:** Option B — control-plane release; agent, command, plugin, and untrusted worker execution remain disabled  
**Narrow-scope verdict:** **PASS** for the implementation and focused verification recorded here  
**Production verdict:** **Not assessed; this report is not a production-readiness certification**

## 1. Executive verdict

The three residual findings in
`AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_INDEPENDENT_AUDIT.md` were addressed
within the approved file scope:

1. `get_execution_diff()` no longer runs `git add -N .` or any other staging
   operation. Tracked changes are read with a no-optional-locks Git diff and
   untracked files are enumerated read-only and rendered in memory. Safe
   inspection failures raise `DiffExtractionError` rather than returning a
   partial or falsely successful result.
2. `_terminate_children()` is explicitly retained as a narrowly scoped,
   ownership-limited safety-teardown exception. It can terminate only a
   process registered against a worktree that exists in the process-local
   ownership registry; unknown/unowned IDs are a no-op. The disabled-mode
   lifecycle callers remain guarded before they can reach it.
3. `_git()` now validates a closed action set and the command shape before
   governance or subprocess dispatch. All four physical mutation actions are
   directly covered by Option B rejection tests, and unknown actions fail
   closed before governance.

The focused legacy suite passes with 18 tests, the Phase 2 policy suite passes
with 10 tests, the Phase 2A contract suite passes with 22 tests when pointed at
a disposable SQLite database, and focused syntax compilation exits 0. One
initial legacy test attempt exposed a fixture assumption about an empty Git
index; that test was corrected and rerun. One initial Phase 2A attempt was
blocked during collection by an ambient PostgreSQL connection; the safe
disposable-SQLite rerun passed.

This result does not enable execution, certify deployment, start later master
plan phases, or establish production readiness.

## 2. Independently verified baseline and custody

The baseline was captured before editing. The workspace contains extensive
pre-existing untracked content, so ordinary `git diff` is not a complete
provenance inventory.

| Item | Baseline observed before edits |
|---|---|
| Repository root | `C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli` |
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries (`git status --porcelain=v1 -uall`) | 2,231 |
| Untracked entries | 2,230 |
| Modified/staged entries | 1 — pre-existing `backend/routes/d3_features.py` |
| Project Git index SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| Required follow-up report before work | Absent |

The following files were already untracked before this task and were edited
only in place; they were not staged or added to the repository index:

- `backend/execution/agent_runtime/context.py`
- `backend/execution/worktree.py`
- `tests/integration/test_legacy_worktree_option_b_guards.py`

The actual `get_execution_diff()` implementation was verified in
`backend/execution/agent_runtime/context.py`, not `backend/routes/diff.py`.
`backend/routes/diff.py` was inspected and left unchanged.

The following protected pre-existing state was preserved:

| Protected item | Baseline SHA-256 | Final SHA-256 | Result |
|---|---|---|---|
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | Unchanged |
| `backend/routes/diff.py` | `ffdd7fefecce48b5e18188bb93159444872ddc937744ada68907d8505340c23a` | `ffdd7fefecce48b5e18188bb93159444872ddc937744ada68907d8505340c23a` | Unchanged |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FIX_REPORT.md` | `f250f11753c41bcc008d880a8ec1f64451d03d1dfa8c8ec6f47f58047314f2d9` | `f250f11753c41bcc008d880a8ec1f64451d03d1dfa8c8ec6f47f58047314f2d9` | Unchanged |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_INDEPENDENT_AUDIT.md` | `cff34467f0d46822de3ac4d16a517f514a5be5e7af2731a0595b2b9d189e986c` | `cff34467f0d46822de3ac4d16a517f514a5be5e7af2731a0595b2b9d189e986c` | Unchanged |

No Hermes reference-tree path was read or modified. No populated `.env` file,
credential, token, password, or credential-bearing archive entry was read,
printed, copied, or added to the repository. No commit, push, reset, cleanup,
real project worktree operation, migration, deployment, container, service,
worker, or production database operation was performed.

## 3. Source changes and finding mapping

### 3.1 `backend/execution/agent_runtime/context.py`

**Audit finding addressed:** P1 — `get_execution_diff()` reached
`git add -N .` while Option B was active.

The function was confirmed in this module at the start of the task. The old
intent-to-add call was removed. The replacement is:

- tracked changes: `git --no-optional-locks diff --no-ext-diff --no-textconv
  --no-color <base_ref> --`;
- untracked enumeration: `git --no-optional-locks ls-files --others
  --exclude-standard -z --`;
- untracked content: safe, in-memory unified-diff rendering for each listed
  regular file; no index entry is created;
- path handling: absolute paths, workspace traversal, `.git`, `.env`/`.env.*`,
  and symlink diff paths are rejected;
- failure handling: a nonzero Git inspection result, launch failure, unsafe
  path, missing untracked regular file, or read failure raises
  `DiffExtractionError`.

`--no-optional-locks`, `--no-ext-diff`, and `--no-textconv` keep the inspection
from refreshing the index or invoking configured external diff/text-conversion
commands. The existing plain-workspace fallback remains available with the
same function contract and now uses the same workspace path checks. No
function-level diff output limit existed in the inspected implementation; no
existing limit was removed or replaced with silent truncation.

The change does not add a policy assertion to this read-only function. That is
intentional: approved read-only inspection remains usable while all execution
and physical worktree mutation boundaries remain disabled.

### 3.2 `backend/execution/worktree.py`

**Audit findings addressed:** P1 — direct `_terminate_children()` boundary;
P2 — incomplete `_git()` mutation coverage and unknown-action handling.

`_terminate_children()` now requires the worktree ID to be present in the
process-local `_REGISTRY`; otherwise it returns zero without calling
`terminate()`, `kill()`, or `wait()`. `attach_process()` now rejects unknown
worktree IDs before registering a process. A registered owned child may still
receive SIGTERM followed by the existing grace-period SIGKILL fallback under
Option B. This is cleanup, not execution: the function never starts,
dispatches, or resumes a process. `discard_worktree()` retains its
`assert_agent_execution_enabled()` at the start, so its disabled-mode path
cannot reach child teardown, Git removal, pruning, branch deletion, or
filesystem cleanup.

`_git()` now has a closed action set containing only:

```text
rev_parse
worktree_add
worktree_remove
worktree_prune
branch_delete
worktree_diff
worktree_list
```

It validates action membership and the action-to-argv shape before
`assert_agent_execution_enabled()`, `_require_governance()`, governance
dispatch, or `subprocess.run`. The four physical actions still assert the
authoritative Option B policy before governance. An unknown action or
read-only-label/mutating-argv mismatch raises `WorktreeError` with an
unsupported/invalid-action decision and cannot fall through to a command.

### 3.3 Focused tests

`tests/integration/test_legacy_worktree_option_b_guards.py` now covers:

- all four physical `_git()` mutation actions in a parameterized test;
- unknown `_git()` action rejection before governance and subprocess;
- registered-owned versus unowned `_terminate_children()` behavior;
- the terminate-then-kill ordering after a fake timeout;
- rejection of process attachment to an unknown worktree;
- tracked and untracked diff output from a disposable Git worktree;
- absence of `git add -N` and preservation of both fixture indexes;
- truthful diff failure behavior; and
- plain-workspace traversal rejection.

All Git-writing fixture setup is confined to pytest temporary directories. No
test uses the actual project repository as a mutation fixture.

## 4. Before/after hashes for modified source and test files

These are SHA-256 byte hashes, not Git object IDs. The files were untracked at
baseline, so their baseline hashes are the independently captured disk hashes
shown below rather than `HEAD` blobs.

| Modified file | Before SHA-256 | After SHA-256 |
|---|---|---|
| `backend/execution/agent_runtime/context.py` | `8f825d254f28139e835dfc50ad5dab23d9830a20545b7a7a00a6757ed9adce55` | `bae7262ba1631e49904c87284388b9c2b29a4f006db0415b163405559df8d0b1` |
| `backend/execution/worktree.py` | `e356c886d0ea4b37d1280dc2c8f76363c2a8a59208a80c123ba68efdd9e5114c` | `de26de461b3e1236c40e1650092b244dc767f8424871a9dbab847fa064d7c48b` |
| `tests/integration/test_legacy_worktree_option_b_guards.py` | `af28e1668b20e5231f1f12208c870d27058dcf5ae6f667039448ebcb07eace` | `5c392868422a2614103744570534c7a25f234b00b3768dc77eab6ec04f57e455` |

The only new workspace artifact is this report. Its final SHA-256 is captured
after the complete report write in the task-completion custody check.
No unrelated file was intentionally changed.

## 5. Exact execution boundaries and guard evidence

### 5.1 Four physical Git mutation actions

| Action | Guard and evidence |
|---|---|
| `worktree_add` | Closed action/argv validation, then `assert_agent_execution_enabled()`; parameterized test raises `ExecutionDisabledError` before governance/process. |
| `worktree_remove` | Same ordering; parameterized test raises before governance/process. |
| `worktree_prune` | Same ordering; parameterized test raises before governance/process. |
| `branch_delete` | Same ordering; parameterized test raises before governance/process. |

For every row, the test patches `_require_governance`, `get_tool_governance`,
and the `worktree` module's `subprocess.run`; all three mocks remain uncalled.
The test also snapshots the disposable fixture repository's index and proves
that its existence and bytes are unchanged. The test fixture, not the project
repository, is the only Git repository used by these assertions.

### 5.2 Unknown actions

`_git("future_mutation", ["git", "branch", "-D", ...], ...)` raises
`WorktreeError` with an unsupported-action decision before governance or
subprocess. This closes the prior fall-through behavior in which arbitrary
action names could reach the optional governance stack. Known actions also
reject malformed command shapes before policy/governance dispatch.

### 5.3 Read-only worktree behavior

The existing registry-only `list_worktrees()` and `get_worktree()` checks remain
in the focused suite and pass. The attached-worktree diff test proves that
read-only tracked and untracked inspection remains functional under Option B.
The separate `worktree_diff()`/`list_git_worktrees()` helpers remain read-only
action names in the closed dispatcher. A real governance-backed subprocess
test for those helpers was not possible in this workspace because the optional
governance modules referenced by `worktree.py` are unavailable; no unsafe
fallback was enabled.

## 6. `_terminate_children()` contract decision

### 6.1 Decision

The finding is resolved as a **permitted safety-cleanup exception**, not by
pretending that termination is execution and not by adding a policy guard that
would make required cleanup impossible.

### 6.2 Evidence and rationale

The preceding Phase 2 control-plane report explicitly retained “explicit
operator cleanup of historical worktrees” as control-plane functionality. The
approved task also distinguishes termination of an already-owned child from
starting or executing a child. Current source inspection found:

- `_terminate_children()` was called only by `discard_worktree()`;
- `discard_worktree()` already asserts the disabled policy before lookup,
  child teardown, Git removal/pruning, branch deletion, and filesystem work;
- no current backend caller of `attach_process()` was found; and
- runtime/process-start boundaries inspected in the agent runtime, sandbox,
  runner, queue, scheduler, and worker paths retain their independent
  execution-disabled checks from the preceding policy work.

The safe exception is therefore deliberately narrower than the old helper:

1. `attach_process()` can register only against a worktree already present in
   `_REGISTRY`.
2. `_terminate_children()` is a no-op for an unknown or unowned ID.
3. Only process handles in that owned registry entry are considered.
4. An owned live handle receives `terminate()`, waits for the grace period,
   and receives `kill()` only after a timeout.
5. No call to this helper starts, dispatches, resumes, or continues worker
   execution. The normal lifecycle caller remains blocked before the helper in
   Option B.

### 6.3 Fake-process test evidence

No real child process was launched. Focused tests prove:

- an unowned process handle receives no `terminate()`, `kill()`, or `wait()`;
- an owned live process receives `terminate()` and a wait, with no kill when it
  exits within grace;
- an owned nonresponsive process receives `terminate()`, first wait, `kill()`,
  and second wait in that order; and
- attaching a process to an unknown worktree raises `WorktreeError`.

The existing cleanup-helper Option B test additionally proves that
`discard_worktree()` rejects before `_terminate_children()` is reached.

## 7. Test commands and exact results

All pytest commands below disabled Python bytecode generation and the pytest
cache. Git-dependent tests used pytest temporary directories outside the
project repository. No full repository suite was run.

### 7.1 Legacy Option B guard suite

Initial command after the first implementation/test edit:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-legacy-followup-basetemp' tests/integration/test_legacy_worktree_option_b_guards.py
```

Result: **4 failed, 14 passed**. All four failures were in the newly added
mutation-action test setup because `git init` creates no `.git/index` until an
entry is written; the test incorrectly assumed an index file existed. This was
a test-fixture defect, not an application failure. The test was corrected to
snapshot both the existence and hash of the disposable index.

Final rerun:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-legacy-followup-basetemp-2' tests/integration/test_legacy_worktree_option_b_guards.py
```

Result: **18 passed, 0 failed, 0 errors, 0 deselected**.

### 7.2 Phase 2 policy regression

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-phase2-followup-basetemp' tests/integration/test_phase2_control_plane_execution_policy.py
```

Result: **10 passed, 0 failed, 0 errors, 0 deselected**.

### 7.3 Phase 2A control-plane contract regression

Initial safe-attempt command:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-phase2a-followup-basetemp' tests/integration/test_phase2a_control_plane_contract.py
```

Result: **1 error during collection; no test cases executed; no pass/fail or
deselection count was emitted**. Import-time application initialization used an
ambient PostgreSQL configuration and failed with connection refused on
`127.0.0.1:5432`.

Safe disposable-database rerun:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; $env:DATABASE_URL='sqlite:///C:/Users/mdham/AppData/Local/Temp/omnirush/aetherion-phase2a-contract.db'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-phase2a-followup-basetemp-2' tests/integration/test_phase2a_control_plane_contract.py
```

Result: **22 passed, 0 failed, 0 errors, 0 deselected**. The SQLite database
was disposable and outside the repository; no migration or production database
operation was run.

### 7.4 Focused syntax compilation

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m py_compile backend/execution/worktree.py backend/execution/agent_runtime/context.py tests/integration/test_legacy_worktree_option_b_guards.py
```

Result: **exit code 0**; no compiler output.

## 8. Final custody and repository-index verification

After the implementation and all verification commands:

| Item | Final evidence |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries (`git status --porcelain=v1 -uall`) | 2,230 |
| Untracked entries | 2,229 |
| Modified/staged entries | 1 — only pre-existing `backend/routes/d3_features.py` |
| Project Git index SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| Initial project Git index SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| New report status | Untracked; expected new artifact |

The project index hash is byte-identical before and after. The implementation
never ran `git add`, `git add -N`, `git reset`, `git clean`, a real worktree
mutation, or an index-writing substitute against the project repository. The
read-only diff test records the exact subprocess argv and asserts that no
`git add -N` call occurs. Its parent and disposable worktree index hashes also
remain unchanged during the request.

The current relevant status remains:

```text
 M backend/routes/d3_features.py
?? AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FIX_REPORT.md
?? AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_INDEPENDENT_AUDIT.md
?? backend/execution/agent_runtime/context.py
?? backend/execution/worktree.py
?? backend/routes/diff.py
?? tests/integration/test_legacy_worktree_option_b_guards.py
?? AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FOLLOWUP_IMPLEMENTATION_REPORT.md
```

The two prior legacy reports and the pre-existing D3 source retain their exact
baseline hashes. Because the workspace already contained 2,229 untracked
entries, the status evidence establishes preservation of the protected and
intended paths but cannot cryptographically attribute every historical
untracked file.

## 9. Final limitations and outstanding prerequisites

The following are deliberately not claimed as closed by this report:

- The full repository test suite was not run, per the task boundary.
- Optional governance modules referenced by `backend/execution/worktree.py`
  were unavailable in this workspace. Mutation rejection was tested before
  governance; a real governed read-only `_git()` subprocess was not exercised.
- No production service, worker, Docker, database, migration, provider,
  deployment, image, network, or real repository worktree operation was
  exercised.
- The process registry is process-local and no current caller of
  `attach_process()` was found. Any future execution-enabled runtime must
  continue to register only genuinely owned child handles and must preserve
  the central execution policy at its startup boundary.
- The unchanged route-level `backend/routes/diff.py` was not part of the P1
  `get_execution_diff()` finding. Its existing route-level exception behavior
  and any production output limits were not re-certified here.
- The Option B source lock remains active. No agent, command, plugin, or
  untrusted worker execution was enabled.

This is implementation evidence for the narrow residual boundary task, not a
second independent audit and not a production go/no-go decision.

## 10. Recommended next step

Submit this report and the focused diff to the designated reviewer for review
of the intentional `_terminate_children()` safety exception and the exact
read-only diff contract. Do not begin a later master-plan phase or enable
execution until that review accepts the exception and the remaining production
prerequisites are separately verified.

## 11. Final report hash

This report was written as the single new implementation report required by the
task. Its final SHA-256 is captured in the task-completion custody check after
this file write; the report hash is not self-embedded because doing so would
change the hash being recorded.
