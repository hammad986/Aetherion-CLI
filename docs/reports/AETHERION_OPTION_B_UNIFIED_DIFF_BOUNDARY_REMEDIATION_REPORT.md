# AETHERION AI — Unified Diff Boundary and Ownership Remediation
# Implementation Report

**Implementation date:** 2026-10-16  
**Release scope:** Option B — control-plane only  
**Implementation verdict:** **PARTIAL**  
**Production verdict:** **NOT CERTIFIED / NO PRODUCTION READINESS CLAIM**

## 1. Executive result

This implementation closes the confirmed permissive D3 repository-root
fallback, adds a shared bounded/sensitive diff boundary to the D3 and alternate
worktree paths, moves task-diff JSON parsing into a deliberate validation
boundary, and hardens process retirement/handle retention.

The result is intentionally **PARTIAL** rather than PASS. The repository has no
production backend caller that obtains and passes the new lifecycle-issued
process capability to `attach_process()`. The positive capability test uses a
synthetic private lifecycle registry seed and patches the execution policy to
exercise the ownership logic without launching a process. Under Option B,
attachment itself now fails closed. Real governance-backed read-only Git
execution is also unavailable in this workspace and was not claimed as
verified.

No agent, command, plugin, worker, service, container, database, real child
process, branch, actual project worktree, or project Git index was mutated.

## 2. Finding disposition

| Finding | Disposition | Evidence |
|---|---|---|
| D3 missing-root/current-directory fallback | **CLOSED in the implemented path** | `D3Store.get_worktree_diff()` requires a nonblank persisted `repo_root`; it never calls `get_repo_root()` as a diff fallback. `get_repo_root()` now returns `None` on discovery failure instead of `os.getcwd()`. Root, active state, governed state, isolation, containment, and symlink checks occur before bounded Git. Fresh tests cover missing, blank, invalid, main-root, traversal-alias, and external-symlink records. |
| Alternate `worktree_diff()` root confinement | **CLOSED for the implemented boundary** | Direct calls require tenant/session/execution identity, resolve trusted registry path/root through `resolve_authorized_worktree_path()`, reject main-root, escape, and symlink cases before governance or Git, and no longer use the old unbounded `_git("worktree_diff", ...)` path. Fresh tests cover main-root rejection before runner, external symlink rejection, and an authorized synthetic worktree. |
| Universal sensitive-file/resource policy | **CLOSED for reachable inspected diff producers, subject to governance limitation** | `diff_policy.py` now owns pathspec exclusions, candidate filtering before file open, bounded tracked/untracked extraction, output validation, and modified-file count limits. Context, D3, alternate worktree, route stored results, and task wrappers use the shared policy. The governed alternate runner fails closed when governance modules are absent. |
| Process ownership and cleanup | **PARTIAL by design** | Attachment requires a sealed lifecycle capability, active worktree, tenant/session/execution match, trusted `Popen` type, and Option B execution admission. Retirement and attachment serialize under the registry lock; failed cleanup retains handles for retry. No production registration caller exists, so real runtime ownership remains unproven. |
| Malformed durable task JSON | **CLOSED for task diff/review wrappers** | `routes/tasks.py` validates required durable fields, JSON syntax, object types, status, and stored diff type before returning data or resolving a workspace. Malformed records return sanitized `DIFF_RECORD_INVALID`; valid stored empty and nonempty diffs remain successful. |

## 3. Files changed and control-flow changes

### 3.1 Shared diff policy — `backend/execution/diff_policy.py`

The existing source policy values were preserved:

| Limit | Value |
|---|---:|
| Maximum untracked candidate files | 100 |
| Maximum path-based modified files | 100 |
| Maximum individual input file | 1 MiB / 1,048,576 bytes |
| Maximum aggregate input | 8 MiB / 8,388,608 bytes |
| Maximum complete rendered output | 10 MiB / 10,485,760 bytes |
| Maximum retained stderr | 64 KiB / 65,536 bytes |
| Read-only Git wall-clock timeout | 5 seconds |

The module now also supplies:

- deterministic Git pathspec exclusions for `.git`, `.ssh`, `.aws`, `.azure`,
  `.gnupg`, `.kube`, `.env`, `.env.*`, credential/key filenames, and configured
  key/certificate suffixes;
- `resolve_authorized_worktree_path()` for required absolute, existing,
  non-main isolated worktree/root pairs, with resolved containment and symlink
  rejection;
- `build_tracked_diff_argv()` and `build_untracked_listing_argv()` so every
  producer uses the same read-only pathspec contract;
- `render_untracked_file_diff()` and `extract_worktree_diff()` so candidate
  enumeration, sensitive filtering, file/aggregate/count limits, binary
  handling, and complete-result validation are not duplicated by callers;
- `DIFF_MAX_MODIFIED_FILES` for path-based plain-workspace output; and
- `ensure_diff_text_safe()`, which rejects sensitive paths in diff headers before
  stored/rendered output is returned.

`run_bounded_readonly_git()` still owns bounded stdout/stderr draining,
`shell=False`, timeout enforcement, nonzero-exit failure, and sanitized
`DiffExtractionError` behavior. It now accepts sanitized process-launch kwargs
from a governance decision while retaining ownership of argv, cwd, pipes,
shell mode, and the five-second timeout.

### 3.2 D3 store — `backend/db/d3_store.py`

`get_repo_root()` now returns `Optional[str]` and returns `None` when `git
rev-parse` cannot establish a root. It no longer substitutes the current
working directory. Creation and discard paths reject missing authoritative roots
instead of inferring one.

`get_worktree_diff()` now requires:

1. a tenant-scoped persisted record;
2. a nonblank persisted `repo_root` and `path`;
3. `state/status == "active"`;
4. `governed == True`;
5. an existing isolated worktree below the persisted root; and
6. a non-option-like base ref.

It calls the shared bounded extractor only after these checks. Missing roots,
invalid roots, main-root candidates, path escapes, external symlinks, limit
failures, and Git/extraction failures all return sanitized `ok: false` with
`DIFF_UNAVAILABLE`. No fallback to `os.getcwd()`, another inferred root, or
the main repository remains in the diff path.

### 3.3 Alternate worktree route — `backend/execution/worktree.py`

The old direct `_git("worktree_diff", ...)` route is now rejected with
`bounded-diff-required`, preventing an unbounded legacy dispatcher call.

`worktree_diff()` now requires tenant, session, and execution identity. It calls
`get_authorized_worktree()` and then the shared root/path validator before any
governance or subprocess operation. It rejects the main repository itself,
resolved escapes, external symlinks, missing roots, missing paths, and inactive
worktrees.

The new `_governed_bounded_readonly_git()` path:

- validates one of two closed read-only argv shapes;
- requires the existing command-policy and tool-governance integrations;
- calls governance `before_dispatch()` for the exact argv-derived command;
- rejects governance denial or argv retokenization;
- passes only sanitized launch settings to the shared bounded runner; and
- calls governance `after_dispatch()` with the bounded return code.

The route wrappers in `backend/routes/diff.py` and `backend/routes/tasks.py`
now pass the trusted worktree's session plus the durable task execution ID into
`worktree_diff()`. A caller cannot supply an arbitrary cwd.

### 3.4 Process lifecycle — `backend/execution/worktree.py`

The former `Worktree.owner_token` attachment contract was removed. A private
`_ProcessOwnershipCapability` is sealed and stored separately in the process-local
trusted lifecycle map; it is not stored on `Worktree` and is not serialized by
`to_dict()`.

`create_worktree()` creates the capability in the same registry transaction as
the trusted worktree registration. `attach_process()` now requires that exact
capability, matching tenant/session/execution identity, an active worktree, and
an actual `subprocess.Popen` instance. It also calls
`assert_agent_execution_enabled()` first, so Option B rejects attachment even
when a synthetic valid capability exists.

`_terminate_children()` now transitions active worktrees to `retiring` while
holding the registry lock. New attachment cannot race past that transition. It
retains failed handles, removes only successfully reaped/exited handles, bounds
the grace period to finite values from 0 through 300 seconds, and preserves
terminate → wait → kill only after `TimeoutExpired` ordering. `discard_worktree()`
 refuses physical removal while owned child handles remain, allowing retry.

### 3.5 Durable task wrappers — `backend/routes/tasks.py`

`_parse_diff_record()` is now the deliberate validation boundary for both task
diff and review responses. It requires the durable payload field, parses JSON
inside a controlled exception boundary, requires object values, validates the
stored diff type, and requires a nonempty status. Invalid records return:

```json
{
  "ok": false,
  "error_code": "DIFF_RECORD_INVALID",
  "error": "Diff record is invalid",
  "task_id": "..."
}
```

The task ID is included as the only request identifier; JSON content, exception
text, secrets, stack traces, and host paths are not returned. A valid stored
empty string remains a successful empty diff. Stored diff headers are also
checked by the shared sensitive-path policy through `diff_payload()`.

## 4. Focused tests added or updated

The existing focused suites were extended rather than creating a parallel test
runner.

### D3 and diff-boundary coverage

`tests/integration/test_option_b_residual_security_remediation.py` now covers:

- missing, blank, and invalid persisted D3 roots;
- root-discovery failure without current-directory fallback;
- main-repository masquerading as an isolated D3 worktree;
- traversal aliases and external symlink targets;
- a legitimate synthetic authorized D3 worktree through the shared extractor;
- D3-sensitive `.ssh/id_rsa`, `.env`, `.env.local`, and `.git/index` candidates;
- D3 candidate-count, per-file, aggregate/rendered policy boundaries;
- alternate-route main-root and external-symlink rejection before bounded Git;
- direct governed `worktree_diff()` extraction with exact-argv authorization;
- direct alternate-path sensitive candidate filtering;
- sensitive stored diff-header rejection;
- modified-file count limits;
- malformed task result/payload JSON and JSON types;
- missing required payload and invalid stored diff types;
- valid empty and nonempty stored diffs;
- malformed task review records; and
- unauthorized task access without successful empty data.

### Process ownership and retirement coverage

`tests/integration/test_legacy_worktree_option_b_guards.py` now covers:

- disabled-mode rejection of a capability-backed attachment;
- unknown, discarded, and inactive worktree attachment rejection;
- arbitrary fake process rejection;
- tenant/session/execution/capability mismatches;
- synthetic lifecycle capability attachment logic without a real child;
- retirement blocking a deterministic attachment race;
- process exit during grace without unnecessary kill;
- terminate/wait/kill ordering after timeout; and
- cleanup failure with handle retention and retry.

The positive ownership test is explicitly synthetic: it seeds the private
lifecycle capability map and patches the Option B admission function only to
exercise the production attachment checks. It does not claim that a real
runtime currently establishes this capability.

## 5. Governance and authorization preservation

The four physical Git mutation actions remain source-locked before governance
and subprocess dispatch:

- `worktree_add`;
- `worktree_remove`;
- `worktree_prune`; and
- `branch_delete`.

Unknown actions and invalid argv remain rejected before governance. The central
Option B policy remains `execution_enabled=False` regardless of environment
values; it was not changed.

The alternate bounded read-only path does **not** bypass governance. It first
requires both `security.command_policy` and `execution.tool_governance`, checks
the exact pathspec-bearing argv, and only then invokes the bounded runner. Those
optional modules are unavailable in this workspace, so a real governed diff
execution was not possible and fails closed through `_require_governance()`.

The D3 store uses tenant-scoped persisted authorization, active/governed record
state, authoritative persisted root/path validation, and the same bounded
shared extraction policy. It does not use the alternate process-local registry
or infer missing root data. Existing D3 read behavior is therefore preserved
only for a properly authorized, persisted, active, governed isolated worktree.

## 6. Process ownership limitation

Source search found `subprocess.Popen` call sites in runtime/sandbox/runner
modules, but no backend caller that both creates a worktree-owned child and calls
`attach_process()`. The new capability is issued by the trusted
`create_worktree()` registry path, but no currently enabled production runtime
retrieves and passes it after process creation. Option B blocks `create_worktree`
and `attach_process` before execution can occur.

Accordingly:

- arbitrary process-like objects, stale IDs, retired IDs, and caller-supplied
  owner strings cannot enter the teardown list through `attach_process()`;
- no real PID or process object was attached, signaled, waited on, or killed;
- cleanup of already registered internal handles is race-protected and retryable;
- the trusted production registration lifecycle remains **unverified/partial**;
  and
- this report does not claim that process ownership is production-closed.

The smallest remaining ownership dependency is to connect the existing trusted
runtime process-creation path to the sealed capability handoff after execution
is separately approved. That is outside the current Option B release and was
not fabricated here.

## 7. Tests personally run

All final pytest commands were run from the repository root with
`PYTHONDONTWRITEBYTECODE=1`, `GIT_OPTIONAL_LOCKS=0`, `-p no:cacheprovider`, and
disposable `--basetemp` directories under
`C:\Users\mdham\AppData\Local\Temp\omnirush`. No full repository suite was
run.

### Final observed results

1. Command:

   ```text
   $env:PYTHONDONTWRITEBYTECODE='1'; $env:GIT_OPTIONAL_LOCKS='0'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-unified-final-focused-basetemp' tests/integration/test_option_b_residual_security_remediation.py tests/integration/test_legacy_worktree_option_b_guards.py
   ```

   Result: **84 passed, 0 failed, 0 errors, 0 deselected** in 11.61s.

2. Command:

   ```text
   $env:PYTHONDONTWRITEBYTECODE='1'; $env:GIT_OPTIONAL_LOCKS='0'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-unified-phase2-basetemp' tests/integration/test_phase2_control_plane_execution_policy.py
   ```

   Result: **10 passed, 0 failed, 0 errors, 0 deselected** in 10.33s.

3. Command:

   ```text
   $env:PYTHONDONTWRITEBYTECODE='1'; $env:GIT_OPTIONAL_LOCKS='0'; $env:DATABASE_URL='sqlite:///C:/Users/mdham/AppData/Local/Temp/omnirush/aetherion-unified-phase2a.db'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-unified-phase2a-basetemp' tests/integration/test_phase2a_control_plane_contract.py
   ```

   Result: **22 passed, 0 failed, 0 errors, 0 deselected** in 10.13s. The
   SQLite database was disposable and outside the repository.

### Earlier implementation-run observations

During the implementation, an intermediate combined run reported **44 passed,
9 failed** because the existing process tests still referenced the removed
`Worktree.owner_token` contract. Those tests were updated to the capability
contract. A later intermediate run reported **82 passed, 2 failed** because the
synthetic governed-worktree tests had not yet supplied the unavailable
`CommandDecision` dependency. That test harness issue was corrected; neither
intermediate count is used as final evidence.

No `py_compile` command was run for this implementation. The final pytest runs
collected and executed the changed Python modules successfully; that is not
reported as a separate compile check.

## 8. Before/after SHA-256 hashes

The following before values were captured before implementation changes. The
after values were captured after the final focused tests and before this report
was written. Relevant files are untracked in this workspace, so these are disk
hashes rather than Git blob IDs.

| File | Before SHA-256 | After SHA-256 |
|---|---|---|
| `backend/db/d3_store.py` | `0dd63df9e2ae51289307e6c7c69a13f5699d94f215a20503455ef084709b79fb` | `682d6ced5e95469493e8b3b5143463f13ee6595eba123021206583c869a85d47` |
| `backend/execution/diff_policy.py` | `e66503239f3fc3dc3b0ba46e79703839792d1edb3e1652d62808f90d199047d0` | `30cf2f1f011ec18cd72dcb1f8da682d09c390298d9b80445b2ce2e3e05d2ec43` |
| `backend/execution/worktree.py` | `ac0cacb3156a8b48e061a17434d2d7f8c0b7aed2ba5db0462e8a071051e6b833` | `cd5f60d1a30a46f722c931b7ca617cd43427e11aad34af6f836a1e098ce35663` |
| `backend/execution/agent_runtime/context.py` | `25145b984f1f752c252d7cbc0a374491bceb499fe2dcf72bc84eefeb153671b4` | `95bb09270b7e54ce36d336b81f5a1bd077552c4a78e37153baee8219b3d5e0a09` |
| `backend/routes/diff.py` | `dccd1479ddd5a4743fda32e333f252e82e8d8e545cc593e38c0d2cb456d374fe` | `d59cbbdf432a52274d77d29bdde4ea123ba5a372f088eaa2d6587e6c2399bcd8` |
| `backend/routes/tasks.py` | `073af123a4924e35daa43c14f823978fdf9df802c23d70d4a581e5d35b286604` | `4edc7f39118969fc3dda80c990e6362ab134436f136c170cd37e06be1950c59f` |
| `tests/integration/test_option_b_residual_security_remediation.py` | `f44d12c6ac5fb82c946bfde07351c534a52756398d7fb370702209478af8334f` | `cfa65e77368513101c679ed82aed442a6eb493c8e7db81966646dd41050d913` |
| `tests/integration/test_legacy_worktree_option_b_guards.py` | `473b49667cda41c0f8470cca2111bd28ed5ddb1e58c3e5d6a26cf690eb5670b3` | `cfefdc77b099ff6ce6f50cc21a2e376a8db3f418fe89b3df80284a8ae1ffbe0e` |
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |

## 9. Workspace, index, and report preservation

The pre-implementation custody capture observed:

| Item | Before implementation |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries | 2,236 |
| Untracked entries | 2,235 |
| Modified/staged entries | 1 — pre-existing `backend/routes/d3_features.py` |
| `.git/index` SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| `.git/index.lock` | Absent |
| Requested implementation report | Absent |

The final read-only custody check observed:

| Item | After implementation/tests/report write |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries | 2,237 |
| Untracked entries | 2,236 |
| Modified/staged entries | 1 — pre-existing `backend/routes/d3_features.py` |
| `.git/index` SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| `.git/index.lock` | Absent |
| `git diff --name-only` | `backend/routes/d3_features.py` only |
| New project artifact | This implementation report only |

The previous implementation report remained at SHA-256
`f8f3e40c02d9f741d290c9a9597eb50e03573d6155ff47c4a481c6cf309e13f4`. The
previous residual-hardening report remained at
`a49bdd89982174377df640eac0550e1536d5007fd204620e871a7a6a0cf9c6c3`. The
earlier Phase 2 and Phase 2A report hashes remained:

| Existing report | SHA-256 |
|---|---|
| `AETHERION_PHASE_2_CONTROL_PLANE_EXECUTION_POLICY_REPORT.md` | `aa5164279d8d57610bd5f612bec7b220ea9c83e3008c417a681a0ff42b9a068c` |
| `AETHERION_PHASE_2_POST_IMPLEMENTATION_INDEPENDENT_AUDIT.md` | `c2339dac5e268b47c8640d295452c966339a5c9ee59fc1a2566d6257ce66a0f0` |
| `AETHERION_PHASE_2A_CONTROL_PLANE_CONTRACT_FIX_REPORT.md` | `81926e678ed68511464c239165b6fdd4c64fdf9cf2fb99d62e405fdaeb5afd2d` |
| `AETHERION_PHASE_2A_POST_IMPLEMENTATION_INDEPENDENT_SECURITY_AUDIT.md` | `f45bdbb4f53d08115159e9229c486c3dee29e3c19fdd56d6380bbb59b0ebabbf` |

The user-requested input name
`AETHERION_OPTION_B_RESIDUAL_SECURITY_REMEDIATION_INDEPENDENT_SECURITY_AUDIT.md`
was not present under that exact name. The closest existing report,
`AETHERION_OPTION_B_RESIDUAL_SECURITY_REMEDIATION_INDEPENDENT_AUDIT.md`, was
present and preserved at SHA-256
`73a4c676073f57e2d37c7948f3e6e680755471f7ff65d92adff4b21df689ed80`.
This filename discrepancy is recorded as a provenance limitation; no report
was renamed, overwritten, or deleted.

No Git staging, commit, push, reset, clean, checkout, rebase, branch mutation,
actual project worktree operation, or index-mutating Git command was used.
Disposable Git/SQLite fixtures were outside the project directory.

## 10. Unresolved risks and production prerequisites

This implementation does not certify:

- a live production process-registration caller or PID/creator/start-time
  binding;
- successful execution through the unavailable real governance modules;
- atomic descriptor-relative file reads under hostile concurrent replacement;
- production PostgreSQL, Redis, authentication middleware, or multi-worker
  behavior;
- deployment image contents, Compose alignment, TLS, external providers, or
  infrastructure; or
- broader repository production readiness.

The shared path validator uses resolved paths and pre-open checks. It does not
create an atomic OS descriptor boundary against every possible concurrent path
replacement. That remains a hardening limitation of the current architecture,
not a reason to reintroduce permissive fallback.

Option B must remain active: autonomous agent execution, command execution,
plugin execution, and untrusted worker execution remain disabled and fail
closed. The focused negative suites do not authorize enabling any execution
path.

## 11. Smallest justified next action

Keep this implementation in review and perform one bounded follow-up focused on
the process lifecycle: identify the approved runtime process-creation caller,
connect it to the sealed capability handoff, and exercise that path only in a
disposable non-production environment with no real child process if the test
contract permits. Separately, load the actual governance modules in an isolated
test environment and verify exact-argv bounded read-only Git admission. Do not
enable Option B execution or deploy until those two evidence gaps are accepted.
