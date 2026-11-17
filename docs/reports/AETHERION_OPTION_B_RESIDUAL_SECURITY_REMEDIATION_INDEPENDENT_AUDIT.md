# AETHERION AI — Option B Residual Security Remediation
# Independent Verification Audit

**Audit type:** Read-only independent application-security and release-assurance review  
**Audit date:** 2026-10-16  
**Approved scope:** Option B — control-plane release  
**Independent verdict:** **PARTIAL**  
**Production verdict:** **NOT CERTIFIED / NO PRODUCTION READINESS CLAIM**

## 1. Executive verdict

The latest remediation materially closes several mechanics in the central
execution-context diff path and in the legacy Option B guard suite. It does not,
however, close the residual boundary across all reachable diff producers and it
does not provide sufficient production evidence for process ownership.

The most material independent result is a remaining fallback in
`backend/db/d3_store.py`: a missing persisted `repo_root` is replaced by
`get_repo_root()`, which itself can fall back to the current working directory.
The resulting candidate can then be read if it lies below that inferred root.
A safe mocked probe reproduced successful diff extraction from this fallback
path. This is a confirmed source-level authorization/root-confinement gap, not
merely a missing test.

The alternate authenticated route now correctly treats task IDs as opaque,
performs tenant-scoped durable lookup, rejects path-like IDs, and rejects an
absent stored diff rather than returning an empty success. Those are verified
closures. The route nevertheless delegates filesystem execution to
`worktree_diff()` without an explicit root relationship, symlink, or bounded
Git-capture check. The shared sensitive-file/resource policy is therefore not a
universal policy for every reachable diff producer.

The process changes are stronger than the previously audited identifier-only
attachment: active status, tenant/session/execution identity, an owner token,
concrete `subprocess.Popen` type, duplicate prevention, and token filtering are
present. But no production backend caller of `attach_process()` was found, and
the owner token is an in-process capability rather than evidence binding a PID
to a trusted runtime. The positive test patches the `Popen` class and launches
no child. The correct result is **PARTIAL**, not PASS.

No arbitrary command-execution bypass was demonstrated. Option B remains
fail-closed in source and the focused negative suites passed. Governance-backed
read-only execution remains unverified because the optional governance modules
are unavailable in this workspace. Focused tests must not be treated as a
production-readiness certification.

## 2. Finding-by-finding disposition

| Finding | Current independent result | Evidence and remaining concern |
|---|---|---|
| A — alternate diff route tenant/task/root confinement | **PARTIAL** | Opaque ID validation and `durable_state.get(task_id, tenant_id)` are present and tested. The route then trusts a process-local registry path through `worktree_diff()`; neither the route nor `worktree_diff()` proves `wt.path` is a confined, non-main, symlink-safe child of `wt.repo_root`. |
| B — sensitive-file exclusion and bounded extraction | **PARTIAL** | The shared policy has explicit exclusions and numeric limits, and the central `get_execution_diff()` tests pass. The alternate `worktree_diff()` route uses `_git()`/`subprocess.run` rather than the bounded shared Git runner, and the D3 path has a missing-root fallback. Universal closure is not established. |
| C — process attachment and genuine ownership | **PARTIAL** | Discarded/unknown worktrees and arbitrary non-`Popen` objects are rejected; identity and token checks exist. No production attachment caller or PID/creator/runtime binding was found, and cleanup removes handles before failures can be retried. |
| D — truthful empty-diff versus extraction failure | **MOSTLY CLOSED / PARTIAL** | Stored empty strings are successful no-change results; missing, unauthorized, and extraction failures return explicit non-success results in the inspected wrappers. `routes/tasks.py` parses durable JSON outside its failure boundary, and full route-level production behavior was not exercised. |
| Additional fallback remediation — missing root/main-repository fallback | **OPEN / CONFIRMED** | `get_execution_diff()` rejects a worktree context without `repo_root`, but `D3Store.get_worktree_diff()` still uses `wt.get("repo_root") or self.get_repo_root()`, and `get_repo_root()` can return `os.getcwd()`. A safe mocked probe observed successful fallback extraction. |

The open/partial items are material to the requested residual-hardening scope;
therefore a complete residual-closure PASS is not justified.

## 3. Scope, method, and evidence labels

Reviewed directly:

- `backend/routes/diff.py`;
- `backend/execution/agent_runtime/context.py`;
- `backend/execution/worktree.py`;
- `backend/execution/diff_policy.py`;
- `backend/execution/control_plane_policy.py`;
- `backend/execution/agent_runtime/runtime.py`;
- `backend/routes/tasks.py`;
- `backend/db/d3_store.py`;
- `backend/execution/durable_state.py`;
- `backend/auth_system.py` for authenticated tenant derivation;
- `tests/integration/test_legacy_worktree_option_b_guards.py`;
- `tests/integration/test_option_b_residual_security_remediation.py`;
- `tests/integration/test_phase2_control_plane_execution_policy.py`;
- `tests/integration/test_phase2a_control_plane_contract.py`; and
- the latest implementation report, previous independent report, and Phase 2/
  Phase 2A reports listed in the hash inventory below.

Evidence labels used in this report:

- **VERIFIED** — directly observed in current source or in a fresh safe test;
- **CONFIRMED** — a safe isolated probe reproduced the behavior;
- **PARTIAL** — a control exists but a material alternate path or ownership
  condition is unresolved;
- **UNVERIFIED** — the required behavior could not safely or credibly be
  established;
- **HISTORICAL** — taken from an existing report and not counted as this audit's
  test result; and
- **INFERENCE** — a security conclusion derived from the observed control flow.

No populated `.env` file, credential, token, password, private key, or
credential-bearing archive entry was read, printed, copied, or used as a test
fixture. Synthetic values were used only where the existing isolated tests or
safe probes required configuration.

## 4. Finding A — alternate diff route

### 4.1 Controls independently verified

`backend/routes/diff.py:31-38` rejects non-string, empty, overlong, padded,
dot, traversal-like, separator-containing, NUL-containing, drive-like, and
colon-containing task IDs. This includes `.`, `..`, `../task-a`, Windows drive
syntax, UNC syntax, and mixed separators. The route does not join `task_id` to
`cwd`, a workspace base, or any filesystem path.

At `backend/routes/diff.py:51-54`, the route obtains the authenticated caller's
tenant from `g.user_id` and calls `durable_state.get(task_id, tenant_id)`. The
durable store fetches by execution ID and then compares the stored tenant at
`backend/execution/durable_state.py:564-573`; cross-tenant records are returned
as not found. This is materially different from the prior unscoped lookup.

The route's stored-result branch at `diff.py:63-70` accepts an explicitly stored
string, including `""`, only after bounded validation. A missing stored diff
requires a non-empty `payload.worktree_id`; otherwise it returns
`DIFF_UNAVAILABLE` with HTTP 409. The fallback branch calls
`get_authorized_worktree(wt_id, tenant_id, execution_id=task_id)` before
`worktree_diff(wt_id)`.

The fresh residual suite independently passed all six path-like task-ID cases,
the authorized stored-result case, the unknown/cross-tenant case, and the
missing-diff non-success case. These are genuine current test results, not an
acceptance of the implementation report's count.

### 4.2 Root-confinement limitation

`get_authorized_worktree()` at `backend/execution/worktree.py:591-612` checks
registry existence, tenant, optional session, optional execution ID, and active
status. It does **not** check that `wt.path` is a non-main path below
`wt.repo_root`, that the resolved path is symlink-safe, or that the path remains
the path originally created by the trusted lifecycle. `worktree_diff()` at
`worktree.py:761-784` passes `wt.path` directly as the Git `cwd` and does not
repeat those checks.

This is a source-level alternate-path gap. A bounded mocked probe constructed an
active registry object whose `path` and `repo_root` were the main repository,
then invoked the route with a tenant-matching durable record. The route returned
the synthetic diff:

```text
ALTERNATE_ROUTE_MAIN_ROOT_ACCEPTED=True
```

The probe did not execute Git or read the project repository; it demonstrates
that the route's own control flow does not reject a main-root registry object.
It is not evidence that an external HTTP caller can manufacture such a registry
entry. The required end-to-end root-confinement property is therefore **not
proven**, and the helper boundary is not defense-in-depth.

The route also does not independently validate absolute paths, traversal aliases,
symlinks, or time-of-check/time-of-use replacement once it delegates to the
registry path. The central `resolve_diff_path()` protects the central context
diff path, but it is not called by `worktree_diff()`.

### 4.3 A finding decision

**Closed:** opaque task IDs, tenant-scoped durable lookup, path-like rejection,
unknown-task denial, and no stored-result-to-plain-workspace fallback.

**Open:** end-to-end root confinement for the alternate registry/Git path and
proof that a real authorized task resolves only to its expected workspace. This
is a material partial closure, not merely a test-coverage preference.

## 5. Finding B — sensitive files and bounded extraction

### 5.1 Actual policy values

The values below are read directly from
`backend/execution/diff_policy.py:17-24`; they are fixed source constants and
are not relaxed through ambient environment variables:

| Control | Actual value |
|---|---:|
| Maximum untracked candidate files | 100 |
| Maximum individual untracked/workspace input | 1 MiB (1,048,576 bytes) |
| Maximum aggregate untracked/workspace input | 8 MiB (8,388,608 bytes) |
| Maximum complete rendered diff/output | 10 MiB (10,485,760 bytes) |
| Retained stderr prefix | 64 KiB (65,536 bytes) |
| Shared read-only Git wall-clock timeout | 5 seconds |

The central attached-worktree path at `context.py:501-538` uses
`git --no-optional-locks diff` and `git --no-optional-locks ls-files --others
--exclude-standard -z`, rejects more than 100 candidates, bounds each file and
aggregate read, and rejects rendered output over 10 MiB. It never stages files.
`run_bounded_readonly_git()` uses `shell=False`, separate pipe-draining reader
threads, a 10 MiB stdout budget, 64 KiB retained stderr, a five-second wait,
and raises `DiffExtractionError` for timeout, nonzero exit, launch failure, or
excessive stdout. The result is rejected rather than silently truncated.

### 5.2 Sensitive-path policy

`diff_policy.py:27-46` excludes these directory components:
`.git`, `.ssh`, `.aws`, `.azure`, `.gnupg`, and `.kube`. It excludes `.env` and
`.env.*` components, the file names `id_rsa`, `id_ed25519`, `id_ecdsa`,
`id_dsa`, `credentials`, `credentials.json`, `secrets.json`,
`service-account.json`, and `token.json`, and the suffixes `.pem`, `.key`,
`.p12`, `.pfx`, and `.jks`.

`resolve_diff_path()` validates NULs, absolute paths, dot/traversal components,
every existing symlink component, resolved containment, and the resolved
sensitive-path policy before the caller opens the file. Git standard ignores
are used for enumeration, but are not the sole sensitive-data barrier.

The fresh residual suite independently exercised synthetic `.ssh`, key, `.env`,
`.git`, `.aws`, and `.kube` paths, ordinary untracked source, a Git-ignored
candidate, binary content, symlink escape, count, per-file, aggregate, and
rendered-output limits. The synthetic fixtures contained no real secret.

### 5.3 Remaining scope gap

The policy is not universal across the inspected producers:

1. The alternate route calls `worktree_diff()` (`worktree.py:773-784`), which
   uses `_git()` and the governance-built `subprocess.run` path. It does not use
   `run_bounded_readonly_git()`, the sensitive-path candidate policy, or the
   fixed five-second shared timeout. The route applies
   `ensure_diff_text_bounded()` only after the full Git result has already been
   captured. Thus the source does not prove bounded capture or sensitive-path
   exclusion at that alternate boundary.
2. The D3 store path (`d3_store.py:650-698`) uses the shared bounded runner for
   a tracked `git diff`, but it does not enumerate untracked candidates through
   the policy and has the unsafe missing-root fallback described in §6.
3. `resolve_diff_path()` checks symlinks, then returns a `Path`; the subsequent
   `stat()`/`open()` in `context.py:428-440` is not descriptor-relative or
   atomic. A concurrent replacement between validation and open could change
   the target. No hostile concurrent race was run against the workspace; this
   remains **UNVERIFIED / hardening gap**, not a demonstrated project-tree
   escape.
4. The plain-workspace `modified_files` path has per-file, aggregate, and
   rendered limits, but no separate modified-file count limit. Unusual filename
   serialization and concurrent path replacement were not fully tested.

Accordingly, sensitive-file and resource-limit closure is accepted for the
central `get_execution_diff()` path, but not for the complete residual scope.

## 6. Additional fallback remediation — missing roots and main repository

### 6.1 Central context path

`context.py:475-497` rejects a context with `worktree_id` but no `repo_root`,
rejects a missing root/workspace, rejects a workspace equal to the repository
root, and requires an active registry worktree with matching tenant, session,
execution ID, path, and repository root. The fresh tests
`test_worktree_context_never_falls_back_to_plain_workspace` and
`test_worktree_context_without_repo_root_cannot_use_plain_fallback` passed.
This specific remediation is **VERIFIED**.

### 6.2 D3 store path remains vulnerable to permissive fallback

`backend/db/d3_store.py:650-667` contains:

```python
repo_root = wt.get("repo_root") or self.get_repo_root()
```

`get_repo_root()` at `d3_store.py:566-581` uses `git rev-parse`, then returns
`os.getcwd()` if that lookup fails. This is a default workspace/root inference
despite missing authoritative root data. The candidate is then checked only
against the inferred root and passed to the bounded Git runner.

A safe probe supplied a tenant-scoped synthetic record with `repo_root=None`
and a candidate below the current project root, replaced Git extraction with a
synthetic return value, and observed:

```text
D3_MISSING_REPO_ROOT_FALLBACK_ACCEPTED=True
```

No project file was read and no Git process was launched by the probe. The
result confirms the source-level failure to fail closed. It also means the
implementation report's broad claim that missing `repo_root` cannot lead to an
unsafe fallback is not independently supported across all reachable diff
implementations.

## 7. Finding C — process attachment and genuine ownership

### 7.1 Controls present

`attach_process()` at `worktree.py:615-648` requires:

- an active registry worktree;
- matching tenant, session, and execution identity;
- the in-process `owner_token` matching the worktree;
- an object passing `isinstance(proc, subprocess.Popen)`; and
- no duplicate identity/token/object entry.

`_terminate_children()` at `worktree.py:651-683` returns zero for unknown or
non-active worktrees, filters registered entries to the active worktree's token,
and performs `poll → terminate → wait(timeout) → kill → wait(timeout)` only after
the expected timeout. The 24-test legacy suite passed the discarded-ID,
unknown-ID, arbitrary-process-object, cross-tenant/session/execution/token, and
cleanup-order cases.

These are real closures over the prior arbitrary process-like-object and
discarded-worktree behavior. The previous discarded-ID termination probe is no
longer reproducible through `attach_process()`; the current suite rejects it.

### 7.2 Why ownership remains PARTIAL

The owner token is generated when a `Worktree` object is constructed and omitted
from `to_dict()`, but it is not bound to a PID, process start time, parent,
command, creator, runtime instance, or durable worker/session lease. The public
in-process `get_worktree()` returns the `Worktree` object itself, so a caller
with direct module access can obtain its token. `isinstance(Popen)` proves a
concrete class, not that the handle belongs to the relevant Aetherion runtime.

Repository search found no backend caller of `attach_process()` outside its
definition. The actual `subprocess.Popen` call sites in runner/sandbox modules
do not establish that they register the returned handle through this ownership
path. The positive test patches `worktree.subprocess.Popen` with a fake class to
stand in for a trusted handle; it launches no process. Therefore the production
ownership lifecycle is **UNVERIFIED**, despite the focused negative checks.

There is also a concurrency concern: `_terminate_children()` pops the process
list before attempting termination. A failure is logged and the handle is not
restored for retry. A concurrent attach can occur after the active-status check
and before the worktree is marked discarded, leaving a newly attached handle
outside the popped list. No real process or hostile concurrent teardown was run.

`discard_worktree()` is correctly blocked by `assert_agent_execution_enabled()`
before teardown under Option B. The narrow termination exception is therefore
not an execution enablement bypass in the current locked release, but its
ownership proof is insufficient for a future or direct enabled lifecycle.

## 8. Finding D — truthful diff failure handling

The inspected response paths now distinguish the following outcomes:

- `routes/diff.py:63-70`: an explicitly stored string, including an empty string,
  is a successful bounded result;
- `routes/diff.py:72-87`: missing worktree data, unauthorized/stale worktrees,
  extraction failures, and unexpected failures return `ok: false` with
  `DIFF_UNAVAILABLE`, `DIFF_NOT_FOUND`, or `DIFF_LIMIT_EXCEEDED`;
- `routes/tasks.py:820-838` and `:854-872`: stored results are bounded and
  authorized extraction failures become explicit non-success responses;
- `d3_store.py:650-698`: path, Git, timeout, limit, and extraction exceptions
  become `ok: false` with sanitized `DIFF_UNAVAILABLE`; and
- `diff_policy.py:195-200` centralizes sanitized error payload construction.

The fresh residual suite passed route missing-diff, task-wrapper extraction
failure, and D3 extraction-failure tests. No inspected path converts the tested
failure into `ok: true` with an empty diff. HTTP status and the `ok` field are
failure-consistent in the tested wrappers, although D3's route maps all store
failures to HTTP 404 rather than distinguishing limit/server classes.

Remaining limitations:

- `routes/tasks.py` parses `result_json` and `payload_json` before entering the
  extraction failure `try` blocks. A malformed durable record can therefore
  escape the sanitized response boundary with an unhandled exception rather
  than a deliberate `DIFF_RECORD_INVALID` response.
- The real governed `worktree_diff()` path was not run because the optional
  governance modules are unavailable. Its error mapping was checked by source
  and mocks, not by a live authorized Git operation.

This finding is **mostly closed for the exercised wrappers**, but not a complete
production contract verification.

## 9. Carry-over Option B and Git mutation controls

`backend/execution/worktree.py:132-146` retains exactly the four physical Git
mutation actions:

- `worktree_add`;
- `worktree_remove`;
- `worktree_prune`; and
- `branch_delete`.

`_validate_git_action()` runs before governance and subprocess dispatch. The
four physical actions call `assert_agent_execution_enabled()` before
`_require_governance()`. Unknown actions and malformed argv are rejected before
both governance and subprocess calls. The fresh legacy suite independently
passed all four mutation guards and the unknown-action guard.

The central diff path contains no `git add`, `git add -N`, refresh, staging,
commit, or worktree mutation. It uses read-only `git diff` and
`git ls-files --others --exclude-standard`; the fresh disposable Git fixture
test proved both disposable indexes were unchanged. No new diff/fallback path
was found to invoke a Git mutation.

`control_plane_policy.py:55-90` is a source lock: regardless of environment
configuration, the effective policy returns `execution_enabled=False`. The
Phase 2 regression suite passed 10/10, and the Phase 2A contract suite passed
22/22 with a disposable SQLite database. Source inspection also found execution
assertions at the runtime and control-plane boundaries; no agent, command,
plugin, worker, service, container, or real child process was started for this
audit.

The optional `security.command_policy` and `execution.tool_governance` imports
are unavailable in this workspace. `_require_governance()` fails closed when
they are missing. Therefore negative pre-governance policy ordering is verified,
but successful real governance-backed read-only Git execution is **UNVERIFIED**.

## 10. Provenance and preservation

### 10.1 Current repository identity before report creation

Captured with `git --no-optional-locks` before this report was written:

| Item | Observed value |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries | 2,235 |
| Untracked entries | 2,234 |
| Modified/staged entries | 1 — `backend/routes/d3_features.py` |
| `.git/index` SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| `.git/index.lock` | Absent |
| Requested independent-audit report | Absent before this write |

The D3 modification was present before this audit and was not edited. The
current SHA-256 of `backend/routes/d3_features.py` is the historical expected
value. The repository contains a very large untracked tree, so ordinary
`git diff` is not a complete preservation manifest.

### 10.2 Historical provenance limitation

The implementation report claims the same project index hash before and after
its work, but supplies no command transcript, timestamped custody manifest, or
independent artifact proving that the first hash was captured before all edits.
Its status tables report 2,232 entries before and 2,235 after; the current
pre-report count is 2,235, which is compatible with the report's stated final
count but does not independently prove the earlier baseline. The previous
independent report contains a different historical status-count account. These
are provenance limitations, not evidence that the index is currently changed.

For this audit window, the index hash was captured after the focused tests and
safe probes and was equal to the pre-test capture shown above. No `git add`,
`git add -N`, `git reset`, `git clean`, checkout, commit, push, branch mutation,
or actual worktree mutation command was executed. Temporary Git fixtures and
SQLite paths were outside the project root.

### 10.3 Existing report/source hash inventory

Hashes below are current disk SHA-256 values captured before writing this
report. They are not Git blob IDs because the relevant tree is untracked.

#### Reviewed source and tests

| Path | SHA-256 |
|---|---|
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |
| `backend/routes/diff.py` | `dccd1479ddd5a4743fda32e333f252e82e8d8e545cc593e38c0d2cb456d374fe` |
| `backend/execution/agent_runtime/context.py` | `25145b984f1f752c252d7cbc0a374491bceb499fe2dcf72bc84eefeb153671b4` |
| `backend/execution/worktree.py` | `ac0cacb3156a8b48e061a17434d2d7f8c0b7aed2ba5db0462e8a071051e6b833` |
| `backend/execution/diff_policy.py` | `e66503239f3fc3dc3b0ba46e79703839792d1edb3e1652d62808f90d199047d0` |
| `backend/execution/control_plane_policy.py` | `0b87fbafeb444ffc049b2c36afbedc480666f5195aac349e31142823374362e7` |
| `backend/execution/agent_runtime/runtime.py` | `988dfa47ce0fc485b8d86dc9f9a14b14f6840f120c897237344dbe9aa8ace51b` |
| `backend/routes/tasks.py` | `073af123a4924e35daa43c14f823978fdf9df802c23d70d4a581e5d35b286604` |
| `backend/db/d3_store.py` | `0dd63df9e2ae51289307e6c7c69a13f5699d94f215a20503455ef084709b79fb` |
| `tests/integration/test_legacy_worktree_option_b_guards.py` | `473b49667cda41c0f8470cca2111bd28ed5ddb1e58c3e5d6a26cf690eb5670b3` |
| `tests/integration/test_option_b_residual_security_remediation.py` | `f44d12c6ac5fb82c946bfde07351c534a52756398d7fb370702209478af8334f` |
| `tests/integration/test_phase2_control_plane_execution_policy.py` | `86004b9db65a76ae82d55e71cc010dab68e5ea3b8a7b744a49a673d5abfb3e7b` |
| `tests/integration/test_phase2a_control_plane_contract.py` | `1a3df4a218987eba8302698e6dd121262a226c391f500b5d6e32b11b39a0bede` |

#### Reviewed reports

| Path | SHA-256 |
|---|---|
| `AETHERION_OPTION_B_RESIDUAL_SECURITY_REMEDIATION_IMPLEMENTATION_REPORT.md` | `f8f3e40c02d9f741d290c9a9597eb50e03573d6155ff47c4a481c6cf309e13f4` |
| `AETHERION_OPTION_B_RESIDUAL_HARDENING_INDEPENDENT_SECURITY_AUDIT.md` | `a49bdd89982174377df640eac0550e1536d5007fd204620e871a7a6a0cf9c6c3` |
| `AETHERION_PHASE_2_CONTROL_PLANE_EXECUTION_POLICY_REPORT.md` | `aa5164279d8d57610bd5f612bec7b220ea9c83e3008c417a681a0ff42b9a068c` |
| `AETHERION_PHASE_2_POST_IMPLEMENTATION_INDEPENDENT_AUDIT.md` | `c2339dac5e268b47c8640d295452c966339a5c9ee59fc1a2566d6257ce66a0f0` |
| `AETHERION_PHASE_2A_CONTROL_PLANE_CONTRACT_FIX_REPORT.md` | `81926e678ed68511464c239165b6fdd4c64fdf9cf2fb99d62e405fdaeb5afd2d` |
| `AETHERION_PHASE_2A_POST_IMPLEMENTATION_INDEPENDENT_SECURITY_AUDIT.md` | `f45bdbb4f53d08115159e9229c486c3dee29e3c19fdd56d6380bbb59b0ebabbf` |

The implementation report's claimed hash
`f8f3e40c02d9f741d290c9a9597eb50e03573d6155ff47c4a481c6cf309e13f4` matches
the independently captured current hash. The expected D3 hash also matches.

## 11. Tests personally run

All pytest runs used `PYTHONDONTWRITEBYTECODE=1`, `GIT_OPTIONAL_LOCKS=0`,
`-p no:cacheprovider`, and disposable basetemp paths under the approved
temporary directory. No full repository suite was run.

### 11.1 Fresh results

1. `python -m pytest -q -p no:cacheprovider --basetemp=<approved-temp> tests/integration/test_option_b_residual_security_remediation.py`
   — **29 passed, 0 failed, 0 errors, 0 deselected** in 11.87s.
2. `python -m pytest -q -p no:cacheprovider --basetemp=<approved-temp> tests/integration/test_legacy_worktree_option_b_guards.py`
   — **24 passed, 0 failed, 0 errors, 0 deselected** in 4.35s.
3. `python -m pytest -q -p no:cacheprovider --basetemp=<approved-temp> tests/integration/test_phase2_control_plane_execution_policy.py`
   — **10 passed, 0 failed, 0 errors, 0 deselected** in 10.66s.
4. The first Phase 2A attempt without a disposable SQLite DSN stopped during
   collection with a PostgreSQL connection-refused error; no test ran.
5. Rerun with
   `DATABASE_URL=sqlite:///C:/Users/mdham/AppData/Local/Temp/omnirush/aetherion-independent-phase2a.db`:
   **22 passed, 0 failed, 0 errors, 0 deselected** in 10.24s.

The Phase 2A SQLite database was outside the project. No production database,
service, container, worker, or real child process was started.

### 11.2 Fresh safe probes, not pytest passes

| Probe | Observed result |
|---|---|
| Main-repository path supplied by a synthetic active registry object to alternate route | `ALTERNATE_ROUTE_MAIN_ROOT_ACCEPTED=True` |
| D3 record with missing `repo_root`, synthetic Git extraction | `D3_MISSING_REPO_ROOT_FALLBACK_ACCEPTED=True` |
| Governance import availability | `security.command_policy` and `execution.tool_governance` unavailable; source fails closed |
| Project index before/after focused checks | Same SHA-256: `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |

The two behavioral probes patched extraction/registry boundaries and did not
read the project source through Git or start a real process.

### 11.3 Historical claims kept separate

The implementation report claims 24 legacy tests, 29 residual tests, 10 Phase 2
tests, 22 Phase 2A tests, and successful `py_compile`. The first four counts
match this audit's fresh results. The implementation report's `py_compile`
result was **not rerun by this audit** and is therefore historical. The matching
counts do not resolve the source-level D3 fallback, alternate-route helper gap,
ownership evidence gap, or unavailable governance integration.

## 12. Governance, production, and release limitations

This report does not certify:

- successful operation through the real governance/command-policy modules;
- production PostgreSQL, Redis, authentication middleware, or multi-worker
  behavior;
- real process-supervisor ownership or teardown;
- race-resistant descriptor-level file reads under concurrent workspace changes;
- complete bounded behavior of the alternate `_git()` diff path;
- deployment images, production Compose alignment, TLS, external providers, or
  infrastructure; or
- production readiness of the broader Aetherion repository.

Autonomous agent execution, command execution, plugin execution, and untrusted
worker execution must remain disabled and fail-closed under Option B. A passing
negative test suite is not permission to enable any of those paths.

## 13. Smallest justified next step

Perform one narrowly bounded remediation and re-audit of the reachable diff
producers: require an authoritative non-main `repo_root` and confined active
worktree in the D3 and alternate route paths; route every diff extraction
through one bounded sensitive-file policy; and document or establish the real
runtime process-registration/ownership lifecycle before allowing the cleanup
exception in any execution-enabled scope. Add a malformed-durable-record test
for the task diff/review wrappers. Do not enable execution or begin a later
release phase before that evidence is independently accepted.

## 14. Final custody check

The post-write read-only custody check observed:

| Item | Final observed value |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries | 2,236 |
| Untracked entries | 2,235 |
| Modified/staged entries | 1 — pre-existing `backend/routes/d3_features.py` |
| `.git/index` SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| `.git/index.lock` | Absent |
| `git diff --name-only` | `backend/routes/d3_features.py` only |
| New project status entry | This report only |
| Protected D3 SHA-256 | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |

The implementation report, previous independent report, and earlier Phase 2/
Phase 2A reports retained the hashes listed in §10.3. The final SHA-256 of this
new report is captured outside its own bytes in the completion summary. No
source, test, configuration, environment, deployment, branch, index, process,
service, worker, database, container, or actual worktree was changed by this
audit.
