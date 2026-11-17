# AETHERION AI — Trusted Process Lifecycle Integration and Governed Git Verification
# Follow-up Implementation Report

**Implementation date:** 2026-10-16  
**Release scope:** Option B — control-plane only  
**Implementation verdict:** **PARTIAL**  
**Production verdict:** **NOT CERTIFIED / NO PRODUCTION READINESS CLAIM**

## 1. Executive result

This bounded follow-up did not invent a process-supervisor architecture or
enable execution to manufacture a positive ownership result. Repository tracing
found no existing production caller that both creates a worktree-owned child and
receives the sealed `_ProcessOwnershipCapability` needed by `attach_process()`.
The process lifecycle finding therefore remains **PARTIAL**.

The safe work performed was focused regression coverage for the existing
fail-closed capability and governance adapters:

- missing governance fails before the bounded Git runner;
- governance denial propagates before policy construction or runner invocation;
- governance argv re-tokenization is rejected before runner invocation;
- bounded timeout handling was exercised with a fake `Popen` object;
- governance `after_dispatch()` callbacks were observed through the injected
  adapter contract; and
- the sealed capability remains absent from serialized `Worktree` data and has
  no public capability-recovery helper.

No source implementation file required a safe lifecycle change in the current
Option B architecture. Only the existing focused remediation test file was
extended, plus this report. No real child process, worker, service, governance
service, database, container, or Git worktree operation was started or changed.

## 2. Finding disposition

| Area | Result | Evidence |
|---|---|---|
| Trusted process creator-to-capability handoff | **PARTIAL / NOT IMPLEMENTED** | No qualifying production caller exists. Existing process creators are generic runner, sandbox, and session subprocess paths, not worktree lifecycle creators. `create_worktree()` issues an internal capability but does not expose or hand it to a process creator. |
| Direct attachment safety | **CLOSED for the current locked boundary** | `attach_process()` still requires Option B admission, active trusted worktree identity, exact private capability identity, and concrete `subprocess.Popen`. Unknown, retired, mismatched, arbitrary, and capability-reuse cases remain rejected. |
| Retirement/cleanup race and retry | **CLOSED for the tested in-process registry behavior** | Retirement serializes under `_REGISTRY_LOCK`; failed handles remain for retry; cleanup refuses worktree removal while handles remain. Tests use fake process objects only. |
| Governed bounded read-only Git | **PARTIAL / real integration unavailable** | The adapter requires governance and exact argv before `run_bounded_readonly_git()`. Injected doubles verified denial, retokenization rejection, callback sequencing, and bounded timeout behavior. The real optional modules did not execute. |
| Option B execution lock | **PRESERVED** | Policy still returns `execution_enabled=False`; process startup and attachment remain blocked before real launch/registration. |

## 3. Actual lifecycle callsites inspected

### 3.1 Worktree lifecycle

`backend/execution/worktree.py` remains the only implementation of the sealed
worktree capability lifecycle:

- `create_worktree()` at approximately lines 642–729 requires
  `assert_agent_execution_enabled()` and governance before Git worktree
  creation. It registers the `Worktree` and private capability together at
  lines 711–722.
- The capability is stored in the private `_PROCESS_CAPABILITIES` map and is
  not stored on `Worktree`, returned by `to_dict()`, serialized, or exposed by
  an accessor.
- `attach_process()` at approximately lines 761–818 requires the capability,
  tenant/session/execution identity, active status, and a concrete
  `subprocess.Popen`; it also calls the central execution admission first.
- `_terminate_children()` at approximately lines 821–872 transitions active
  worktrees to `retiring` while holding the registry lock and retains failed
  handles for retry.
- `discard_worktree()`, `sweep_session()`, and `sweep_orphans()` call cleanup,
  but their public lifecycle boundaries remain protected by
  `assert_agent_execution_enabled()`.

The agent context path calls `create_worktree()` during workspace attachment
and `discard_worktree()` during cleanup, but it does not create a child process
or receive a capability for one. The current `worktree_diff()` path is
read-only and does not create or register a child.

### 3.2 Actual `Popen` callsites

Repository-wide source search of the actual `backend/` tree found these process
creation boundaries:

| File and symbol | Observed role | Worktree ownership established? |
|---|---|---|
| `backend/execution/diff_policy.py:run_bounded_readonly_git()` | Bounded read-only Git inspection | **No**; this is a Git inspection handle, not a worktree child registration path. |
| `backend/execution/runner_contract.py:LocalSubprocessRunner.submit()` | Generic local runner in a temporary runner directory | **No**; it has its own `JobSpec`, temp directory, and policy gate; it does not receive a `Worktree` capability or call `attach_process()`. |
| `backend/execution/runtime_runners.py:run_session()` | Session/agent subprocess using `session_workspace(sid)` and `execution_id` | **No**; it does not create/register an `execution.worktree.Worktree`, does not receive a sealed capability, and does not call `attach_process()`. It is itself blocked by `assert_agent_execution_enabled()`. |
| `backend/execution/sandbox/boundary.py:WorkspaceBoundary.run_command()` | Command execution under workspace boundary | **No**; it has workspace command policy and local cleanup but no worktree registry capability handoff. It is blocked at the actual Popen boundary. |

No other actual backend caller of `attach_process()` was found. The search also
found no process-creation wrapper that returns a trusted capability alongside a
child handle. The existing process creators therefore cannot safely be wired to
`attach_process()` without introducing a new execution architecture or changing
the approved release lock.

### 3.3 Decision not to invent wiring

The only qualifying lifecycle currently present is the worktree registry itself,
but it is intentionally disabled in Option B before physical creation. The
generic runner/sandbox/session subprocesses are not proven to own a registered
worktree. Connecting any of them would require a new capability handoff contract,
process supervisor semantics, and an execution-enabled approval that this task
does not have.

The correct secure result is to leave the attachment path fail-closed and keep
the lifecycle finding PARTIAL.

## 4. Option B lock verification

`backend/execution/control_plane_policy.py` was not modified. Its source lock
continues to return `execution_enabled=False` for every environment mapping,
including contradictory or missing configuration.

The following boundaries remain protected:

- `RealAgentRuntime.__init__()` rejects direct construction before runtime setup;
- `LocalSubprocessRunner.submit()` rejects before creating its temporary script
  or calling `Popen`;
- `WorkspaceBoundary.run_command()` rejects at the actual command boundary;
- `runtime_runners.run_session()` rejects before its agent `Popen`;
- `create_worktree()` rejects before tenant/worktree filesystem or Git work;
- `attach_process()` rejects before capability lookup or process registration;
- physical Git mutation actions reject before governance and subprocess dispatch;
  and
- discard/sweep lifecycle entrypoints reject before child teardown and Git
  mutation in Option B.

The focused Phase 2 and legacy suites remained green. No environment variable,
test fixture, or injected capability was allowed to alter the production policy
lock.

## 5. Governed bounded Git verification

### 5.1 Current dependency availability

The actual import inspection was performed with a disposable SQLite database
outside the repository to avoid any ambient PostgreSQL connection. Observed
results:

```text
security.command_policy_SPEC_ERROR=ModuleNotFoundError:__path__ attribute not found on 'security' while trying to find 'security.command_policy'
execution.tool_governance_SPEC=None
INTEGRATION_STATUS={'command_policy': False, 'tool_governance': False, 'workspace_lock': False, 'tenant': False, 'ledger': False, 'events': False, 'event_bus': False, 'telemetry': False}
EXECUTION_ENABLED=False
```

The `security` name resolves to the application's `backend/security.py` module,
not a `security` package containing `command_policy`. `execution.tool_governance`
is absent. No package was installed and no external governance service was
started. The first inspection attempt without an isolated SQLite DSN stopped
during import because the unavailable local PostgreSQL server was contacted; no
test or service was started by that attempt.

### 5.2 Adapter behavior verified with doubles

The existing `_governed_bounded_readonly_git()` implementation remains
unchanged and continues to:

1. validate the closed bounded read-only argv shape;
2. fail closed through `_require_governance()` when dependencies are absent;
3. call governance `before_dispatch()` before the bounded runner;
4. reject governance denial before policy construction or runner invocation;
5. reject policy argv retokenization before runner invocation;
6. pass sanitized process-launch settings to
   `run_bounded_readonly_git()`; and
7. provide the bounded return code to `after_dispatch()`.

New tests exercise these rules with injected doubles only. They prove the
adapter sequencing and failure behavior; they do **not** prove real governance
integration. No real governed Git operation was executed.

The existing four mutation-action and unknown-action tests remain in the legacy
suite. They continue to verify rejection before governance/subprocess execution.

## 6. Focused tests added

The existing
`tests/integration/test_option_b_residual_security_remediation.py` was extended
with:

- missing-governance rejection before bounded execution;
- governance denial propagation before policy/runner calls;
- argv-retokenization rejection before bounded execution;
- fake-`Popen` timeout handling with kill and truthful
  `DiffExtractionError` semantics;
- observed governance `after_dispatch()` callback count in the synthetic
  authorized worktree test; and
- confirmation that private capability state is absent from `Worktree.to_dict()`
  and no capability-recovery API exists.

The existing legacy test suite was not changed in this follow-up. Its current
tests continue to cover unknown/discarded/retired attachment, identity
mismatches, cleanup races, retry retention, kill ordering, and Option B
attachment rejection.

All ownership tests use fake process objects. No real `Popen` child was created,
signaled, waited on, or terminated.

## 7. Tests personally run

All pytest runs used `PYTHONDONTWRITEBYTECODE=1`, `GIT_OPTIONAL_LOCKS=0`,
`-p no:cacheprovider`, and disposable basetemp paths under
`C:\Users\mdham\AppData\Local\Temp\omnirush`. No full repository suite was
run.

### Final focused results

1. Command:

   ```text
   $env:PYTHONDONTWRITEBYTECODE='1'; $env:GIT_OPTIONAL_LOCKS='0'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-process-governance-tests1-basetemp' tests/integration/test_option_b_residual_security_remediation.py tests/integration/test_legacy_worktree_option_b_guards.py
   ```

   Result: **89 passed, 0 failed, 0 errors, 0 deselected** in 12.17s.

2. Command:

   ```text
   $env:PYTHONDONTWRITEBYTECODE='1'; $env:GIT_OPTIONAL_LOCKS='0'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-process-governance-phase2-basetemp' tests/integration/test_phase2_control_plane_execution_policy.py
   ```

   Result: **10 passed, 0 failed, 0 errors, 0 deselected** in 10.24s.

3. Command:

   ```text
   $env:PYTHONDONTWRITEBYTECODE='1'; $env:GIT_OPTIONAL_LOCKS='0'; $env:DATABASE_URL='sqlite:///C:/Users/mdham/AppData/Local/Temp/omnirush/aetherion-process-governance-phase2a.db'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-process-governance-phase2a-basetemp' tests/integration/test_phase2a_control_plane_contract.py
   ```

   Result: **22 passed, 0 failed, 0 errors, 0 deselected** in 10.00s. The
   SQLite database was disposable and outside the repository.

4. A non-pytest dependency inspection with the same disposable SQLite setup
   reported both governance integrations unavailable, all optional worktree
   integrations false, and `EXECUTION_ENABLED=False`. It did not execute a Git
   operation or start a service.

No `py_compile` command was run separately. No real governance module was
executed, and no test result is presented as proof of production governance
integration.

## 8. Before/after SHA-256 hashes

The pre-change custody capture occurred before the test-file edit. No production
source file was changed in this follow-up.

| File | Before SHA-256 | After SHA-256 |
|---|---|---|
| `tests/integration/test_option_b_residual_security_remediation.py` | `cfa65e77368513101c679ed82aed442a6eb493c8e7db81966646dd41050d913` | `14b9107dc31f91048255b2d883e21d2be9da7969ac9573617369385dbf1f648d` |
| `tests/integration/test_legacy_worktree_option_b_guards.py` | `cfefdc77b099ff6ce6f50cc21a2e376a8db3f418fe89b3df80284a8ae1ffbe0e` | `cfefdc77b099ff6ce6f50cc21a2e376a8db3f418fe89b3df80284a8ae1ffbe0e` |
| `backend/execution/worktree.py` | `cd5f60d1a30a46f722c931b7ca617cd43427e11aad34af6f836a1e098ce35663` | `cd5f60d1a30a46f722c931b7ca617cd43427e11aad34af6f836a1e098ce35663` |
| `backend/execution/diff_policy.py` | `30cf2f1f011ec18cd72dcb1f8da682d09c390298d9b80445b2ce2e3e05d2ec43` | `30cf2f1f011ec18cd72dcb1f8da682d09c390298d9b80445b2ce2e3e05d2ec43` |
| `backend/execution/agent_runtime/runtime.py` | `988dfa47ce0fc485b8d86dc9f9a14b14f6840f120c897237344dbe9aa8ace51b` | `988dfa47ce0fc485b8d86dc9f9a14b14f6840f120c897237344dbe9aa8ace51b` |
| `backend/execution/runtime_runners.py` | `9f75c3e9ea24aa7df9116119d91aea080333782dbc34b6eb27e136b2d3c0f44c` | `9f75c3e9ea24aa7df9116119d91aea080333782dbc34b6eb27e136b2d3c0f44c` |
| `backend/execution/runner_contract.py` | `0e4776ee5d49ceb44f06e1702484a2596228566982801714fbedc66b6cfd5130` | `0e4776ee5d49ceb44f06e1702484a2596228566982801714fbedc66b6cfd5130` |
| `backend/execution/sandbox/boundary.py` | `c0c7ada8cee6514a5dd53a85a2d7b7d90f7b5507104a2229e95364c3678178cf` | `c0c7ada8cee6514a5dd53a85a2d7b7d90f7b5507104a2229e95364c3678178cf` |
| `backend/execution/control_plane_policy.py` | `0b87fbafeb444ffc049b2c36afbedc480666f5195aac349e31142823374362e7` | `0b87fbafeb444ffc049b2c36afbedc480666f5195aac349e31142823374362e7` |
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |

## 9. Workspace and report preservation

Pre-change custody:

| Item | Before |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries | 2,237 |
| Untracked entries | 2,236 |
| Modified/staged entries | 1 — pre-existing `backend/routes/d3_features.py` |
| `.git/index` SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| `.git/index.lock` | Absent |
| Follow-up report | Absent |

After implementation, tests, and report writing, the final custody check
observed:

| Item | After |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries | 2,238 |
| Untracked entries | 2,237 |
| Modified/staged entries | 1 — pre-existing `backend/routes/d3_features.py` |
| `.git/index` SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| `.git/index.lock` | Absent |
| `git diff --name-only` | `backend/routes/d3_features.py` only |
| New artifact | This follow-up report only |
| Protected D3 SHA-256 | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |

The previous unified remediation report remains unchanged at:
`028af84d98c9ea8042f4e3fbdaaa636cde610cf2a6bf9bbfa39fdfb2c12f1c7c`.
The prior implementation report remains unchanged at:
`f8f3e40c02d9f741d290c9a9597eb50e03573d6155ff47c4a481c6cf309e13f4`.
The prior residual-hardening independent report remains unchanged at:
`a49bdd89982174377df640eac0550e1536d5007fd204620e871a7a6a0cf9c6c3`.
The existing closest independent audit report remains unchanged at:
`73a4c676073f57e2d37c7948f3e6e680755471f7ff65d92adff4b21df689ed80`.
The requested filename with `_INDEPENDENT_SECURITY_AUDIT.md` remains absent;
the existing report uses `_INDEPENDENT_AUDIT.md` and was preserved.

No source, deployment, environment, report, index, branch, actual worktree,
process, service, worker, database, or container was mutated. The only new
project artifact is this report; the only existing focused test changed is the
residual remediation test listed above.

## 10. Remaining blockers and smallest next action

Remaining blockers are:

1. No trusted production creator-to-capability handoff exists for a
   worktree-owned child process.
2. Real governance and command-policy modules are unavailable, so only adapter
   sequencing with test doubles is verified.
3. Production process supervision, multi-worker ownership, and deployment
   behavior remain outside this bounded task.

The smallest justified next action is a separate architecture/approval decision
to define an execution-enabled trusted process supervisor and capability handoff,
followed by isolated validation with the real governance modules. Do not enable
Option B execution, add a token-recovery API, or deploy before that decision and
evidence exist.
