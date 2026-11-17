# Aetherion Legacy Worktree Option B Guard Fix Report

**Status:** PASS for the authorized legacy-worktree guard scope  
**Date:** 2026-10-13  
**Release mode:** Option B — control-plane release; agent execution disabled

## 1. Executive result

The residual P1 identified by the independent Phase 2A audit has been closed in
the legacy execution worktree boundary. Physical worktree creation, discard,
session sweeping, orphan sweeping, direct physical Git mutations, and the
execution-context cleanup fallback now fail closed through the authoritative
control-plane policy before reaching governance, tenant resolution, Git,
subprocess, child-process, or filesystem mutation paths.

Read-only worktree registry and Git inspection helpers remain importable and
usable. No later phase was started, no deployment behavior was enabled, and no
production-readiness claim is made by this report.

## 2. Governing contract

The enforcement point is:

```python
from backend.execution.control_plane_policy import assert_agent_execution_enabled
```

When execution is disabled, the required exception and message are:

```text
ExecutionDisabledError
Agent execution is disabled for the approved control-plane release.
```

The guard is intentionally placed before any physical side effect. The shared
`_git()` dispatcher applies the same guard only to physical Git mutations so
that read-only inspection actions are not blocked by the new mutation lock.

## 3. Scope and changed files

### 3.1 Authorized implementation changes

- `backend/execution/worktree.py`
  - Guards `create_worktree()` before governance, tenant lookup, path creation,
    and Git dispatch.
  - Guards `discard_worktree()`, `sweep_session()`, and `sweep_orphans()` before
    registry, child-process, Git, or filesystem cleanup work.
  - Guards the shared `_git()` dispatcher for the physical actions
    `worktree_add`, `worktree_remove`, `worktree_prune`, and `branch_delete`.
- `tests/integration/test_legacy_worktree_option_b_guards.py`
  - Adds focused negative tests for rejection ordering, read-only imports, and
    no-side-effect behavior.

### 3.2 Required adjacent cleanup boundary

- `backend/execution/agent_runtime/context.py`
  - Guards `cleanup_execution_worktree()` before both the normal helper call and
    its direct `git worktree remove` fallback.

This adjacent change is limited to the direct cleanup path identified during
source inspection. It is included because leaving that fallback unguarded
would allow a direct cleanup caller to bypass the same Option B source lock.

### 3.3 Explicitly preserved

- `backend/routes/d3_features.py` was not modified by this task.
- No migrations, deployment files, services, containers, credentials,
  configuration secrets, or later-phase implementation files were changed.
- No commit, push, reset, cleanup, worktree creation, or worktree deletion was
  performed by the verification commands.

## 4. Baseline and preservation evidence

Baseline was captured before the guard changes:

| Item | Baseline |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| D3 source SHA-256 | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |
| `worktree.py` SHA-256 | `501a013de25b0d103d381ba5945057e2fda2947f9a7521c66c03db8f32e234c4` |
| `context.py` SHA-256 | `923fb3fb2d8db5fbef66a2f48f6748aecb4a82923ba6f6732f51ca646788fe93` |

Final hashes after the implementation and tests:

| Item | Final SHA-256 |
|---|---|
| `backend/execution/worktree.py` | `e356c886d0ea4b37d1280dc2c8f76363c2a8a59208a80c123ba68efdd9e5114c` |
| `backend/execution/agent_runtime/context.py` | `8f825d254f28139e835dfc50ad5dba23d9830a20545b7a7a00a6757ed9adce55` |
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |
| `tests/integration/test_legacy_worktree_option_b_guards.py` | `af28e1668b20e5231f1f12208c870d27058dadcf5ae6f667039448ebcb07eace` |

The D3 hash is unchanged from the pre-task baseline.

## 5. Test coverage and observed results

All commands were run with bytecode generation disabled, pytest cache disabled,
an isolated disposable SQLite path, a disposable pytest basetemp, and
`AETHERION_EXECUTION_DISABLED=1`. No credential values were written to the
repository or report.

### 5.1 Focused legacy boundary tests

Command:

```text
python -m pytest -q -p no:cacheprovider --basetemp=<disposable-temp> tests/integration/test_legacy_worktree_option_b_guards.py
```

Result:

```text
7 passed in 10.03s
```

The focused tests prove that:

- the module imports and read-only `list_worktrees()` / `get_worktree()` remain
  available;
- `create_worktree()` rejects before `_require_governance`, tenant lookup,
  `_git`, subprocess execution, or `os.makedirs`;
- discard and sweep helpers reject before Git, subprocess, child termination,
  or `shutil.rmtree`;
- `cleanup_execution_worktree()` rejects before both `discard_worktree()` and
  the direct Git fallback; and
- direct physical `_git("worktree_add", ...)` dispatch rejects before
  governance and subprocess work.

### 5.2 Regression checks

| Check | Result |
|---|---:|
| `tests/integration/test_phase2a_control_plane_contract.py` | 22 passed |
| `tests/integration/test_phase2_control_plane_execution_policy.py` | 10 passed |
| `tests/integration/test_d3_tracked_migrations.py` | 7 passed |
| `python -m py_compile` for changed Python files | exit code 0 |

The D3 regression suite was rerun after the final dispatcher guard was added.

## 6. Rejection ordering

The expected fail-closed ordering is now:

1. Check the authoritative execution-disabled policy.
2. Raise `ExecutionDisabledError` with the stable message.
3. Do not resolve tenants, acquire governance, inspect or mutate the registry,
   invoke child-process cleanup, call `_git()`, launch subprocesses, create
   directories, remove worktrees, delete branches, or prune Git metadata.

The focused tests patch each relevant downstream boundary and assert that it is
not called. The tests also assert that the temporary workspace remains empty.

## 7. Verification limitations

- This was a source-level and isolated-test verification. It did not execute a
  real worktree mutation, because Option B requires those mutations to remain
  disabled.
- The full repository test suite was not run.
- No production deployment, service startup, container build, migration run,
  or mutating external Git operation was performed; read-only inspection was
  limited to the focused test's registry check.
- The repository contains substantial pre-existing untracked material and one
  pre-existing tracked modification (`backend/routes/d3_features.py`). Those
  changes were preserved and are outside this task's scope.

## 8. Acceptance decision

**PASS for the narrow legacy-worktree Option B guard fix.** The identified
physical mutation and cleanup entry points are fail-closed, focused negative
tests and the relevant policy/D3 regressions pass, syntax compilation passes,
and the D3 source hash is preserved.

This report does not authorize or certify any later release phase, production
deployment, or re-enablement of agent execution.
