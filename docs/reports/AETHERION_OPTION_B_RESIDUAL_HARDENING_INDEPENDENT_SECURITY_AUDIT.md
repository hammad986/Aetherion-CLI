# AETHERION AI — OPTION B RESIDUAL HARDENING INDEPENDENT SECURITY AUDIT

**Audit type:** Read-only independent security and release-assurance review  
**Audit date:** 2026-10-16  
**Project root:** `C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli`  
**Approved release scope:** Option B — control-plane release  
**Independent verdict:** **PARTIAL**  
**Production verdict:** **NOT CERTIFIED / NO PRODUCTION READINESS CLAIM**

## 1. Executive verdict

The GPT-6 Astra follow-up implementation is materially effective for the three
specific mechanics it changed, but the residual boundary is not fully closed
under an independent review.

### Verified closures

- The current `get_execution_diff()` implementation no longer contains or
  reaches `git add -N .`. Its attached-worktree path uses
  `git --no-optional-locks diff` and `git ls-files --others --exclude-standard`,
  and the focused test proves tracked and untracked fixture changes are
  returned while both disposable fixture indexes remain unchanged.
- `_git()` has a closed supported-action set and rejects unknown actions before
  governance and subprocess dispatch. The four named physical mutation actions
  are directly parameterized in the focused suite and reject with
  `ExecutionDisabledError` under Option B.
- The normal `discard_worktree()` path asserts the execution-disabled policy
  before child teardown, Git mutation, and filesystem cleanup. The focused fake
  process tests demonstrate the intended terminate/wait/kill ordering for the
  retained safety-cleanup exception.

### Residual findings preventing PASS

1. The ownership claim for `_terminate_children()` is only registry-label
   ownership. `attach_process()` accepts an arbitrary process-like object for
   any existing worktree ID, including a worktree whose status is already
   `discarded` or `failed`; there is no tenant, session, creator, process-PID,
   or runtime-identity proof. A safe fake-process probe confirmed that a
   process attached to a discarded registry entry is terminated.
2. The new untracked-file diff path has no file-size, aggregate-size, file-count,
   or output-size bound and exposes any untracked file that Git's standard
   excludes do not ignore. `.git` and `.env` variants are rejected, but a
   path such as `.ssh/id_rsa` is not. A disposable Git-worktree probe confirmed
   that such a private-looking untracked path is included.
3. Alternate authenticated diff routes remain outside the new implementation:
   `backend/routes/diff.py` does not enforce tenant-scoped task lookup or a
   confined workspace root, and its `task_id` path is joined then realpathed
   without rejecting `..`. A safe mocked route probe supplied `..` and observed
   an `ok: true` diff response while the subprocess `cwd` resolved to the
   parent root. Durable-state lookup in that route is also unscoped.
4. Several diff wrappers convert inspection failure into an apparently
   successful empty/default diff. `routes/diff.py`, `routes/tasks.py`, and
   `db/d3_store.py` catch or absorb failures and return `ok: true` with an empty
   or `# No changes detected.` result. This contradicts the follow-up claim
   that diff failure cannot be mistaken for a valid complete result, even
   though the changed `get_execution_diff()` function itself raises.

These are bounded source and isolated-probe findings. No public arbitrary
command execution bypass was demonstrated, and no real project repository,
service, worker, database, branch, or worktree was mutated. The correct narrow
verdict is therefore **PARTIAL**, not PASS or BLOCKED: the changed core paths
show meaningful closure, but authorization, ownership, resource bounds, and
alternate-wrapper truthfulness remain unresolved.

## 2. Scope, evidence labels, and review method

Inspected directly:

- `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FIX_REPORT.md`;
- `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_INDEPENDENT_AUDIT.md`;
- `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FOLLOWUP_IMPLEMENTATION_REPORT.md`;
- `AETHERION_PHASE_2_CONTROL_PLANE_EXECUTION_POLICY_REPORT.md`;
- `AETHERION_PHASE_2_POST_IMPLEMENTATION_INDEPENDENT_AUDIT.md`;
- `AETHERION_PHASE_2A_CONTROL_PLANE_CONTRACT_FIX_REPORT.md`;
- `AETHERION_PHASE_2A_POST_IMPLEMENTATION_INDEPENDENT_SECURITY_AUDIT.md`;
- `backend/execution/agent_runtime/context.py`;
- `backend/execution/worktree.py`;
- `backend/execution/control_plane_policy.py`;
- `backend/execution/agent_runtime/runtime.py`;
- `backend/routes/diff.py`;
- `backend/routes/tasks.py`;
- `backend/db/d3_store.py`; and
- `tests/integration/test_legacy_worktree_option_b_guards.py`.

Searches also covered direct callers of `get_execution_diff()`,
`attach_process()`, `_terminate_children()`, `_git()`, `subprocess.Popen`, and
the authoritative `assert_agent_execution_enabled()` boundary.

Evidence labels used below:

- **VERIFIED** — directly observed in current source or a safe command result;
- **CONFIRMED** — a safe isolated probe reproduced the behavior;
- **PARTIAL** — a core claim is present but an alternate path or ownership
  condition is unresolved;
- **NOT VERIFIED** — no trustworthy evidence was obtained;
- **HISTORICAL** — taken from an existing report and not recreated here; and
- **INFERENCE** — a reasoned security conclusion rather than direct runtime
  proof.

No source, test, configuration, environment, deployment asset, or existing
report was edited. The only permitted project write from this audit is this
new report. No populated `.env` contents, credential, token, password, or
credential-bearing archive entry was read, printed, or copied.

## 3. Provenance and workspace preservation

### 3.1 Current workspace identity

The following values were independently captured before this report was
created. Git commands were run with `--no-optional-locks`; the status/hash
inspection did not stage or rewrite the project index.

| Item | Independently observed value |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries (`git status --porcelain=v1 -uall`) | 2,231 |
| Untracked entries | 2,230 |
| Modified/staged entries | 1 — pre-existing `backend/routes/d3_features.py` |
| Project index SHA-256 before audit verification | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| Requested new audit report before audit | Absent |

The broad untracked tree means `git diff` alone is not a complete provenance
inventory. The one tracked modification was not attributed to this audit.
All implementation source and focused test paths reviewed below are untracked
in the current workspace; their hashes are disk hashes, not `HEAD` blob IDs.

### 3.2 Report discrepancies

The follow-up implementation report is not internally consistent on status
counts: its baseline table records 2,231 status entries and its final custody
table records 2,230. A final count cannot be lower than the stated pre-edit
count if the report itself is the only new artifact. The current pre-audit
count is 2,231, so the historical count claim is not accepted as complete
provenance.

The follow-up report labels the project index with both an “initial” and a
“final” value of
`18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e`, but it
provides no command transcript, timestamped custody manifest, or independent
artifact proving that the first capture occurred before all implementation
edits. This audit therefore treats the implementation report's two equal
labels as a claim, not as independently proven historical before/after
evidence.

This audit did obtain a genuine bounded before/after check for its own work:
the project index had the value above before targeted tests and probes, and the
same value was re-read immediately before writing this report. All test Git
repositories and disposable SQLite paths were outside the project root.

### 3.3 Protected hashes

| File | Current SHA-256 | Status/evidence |
|---|---|---|
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | Matches expected preserved hash |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FIX_REPORT.md` | `f250f11753c41bcc008d880a8ec1f64451d03d1dfa8c8ec6f47f58047314f2d9` | Unchanged from pre-audit capture |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_INDEPENDENT_AUDIT.md` | `cff34467f0d46822de3ac4d16a517f514a5be5e7af2731a0595b2b9d189e986c` | Unchanged from pre-audit capture |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FOLLOWUP_IMPLEMENTATION_REPORT.md` | `8499f6a6e1f2689297222762a5b87219ca58a03c7fddc02cc8d470cf1dfe9a84` | Matches claimed hash |

No Hermes reference-tree path was modified. No Docker, application server,
worker, service, production database, migration, deployment, branch, real
worktree, or process was started or changed.

## 4. Finding matrix

| ID | Severity | Location | Independent result | Impact / follow-up |
|---|---|---|---|---|
| F-1 | P1 | `backend/routes/diff.py:19-72` | **CONFIRMED** alternate path lacks tenant/root confinement and returns `ok: true` after failures | Cross-tenant or parent-repository diff disclosure is possible if the route is reachable with an authenticated principal. Enforce tenant-scoped record lookup and strict root confinement, or remove this alternate path. |
| F-2 | P1/P2 | `context.py:451-534` | **CONFIRMED** untracked private-looking files can be included; no output/file-size bound exists | Sensitive untracked data exposure and memory/response exhaustion remain possible for a reachable direct caller. Add explicit sensitive-file policy and bounded file/count/aggregate output. |
| F-3 | P1 | `worktree.py:586-617` | **PARTIAL** registry presence is not genuine process ownership | A direct caller can attach an arbitrary process-like object to another/old worktree ID and trigger termination. Establish creator/session/tenant/runtime identity and active ownership before permitting the exception. |
| F-4 | P2 | `worktree.py:614-633` | **VERIFIED source gap** cleanup exceptions are swallowed after handles are popped | A process can remain alive while cleanup continues and a later retry is impossible; return/error semantics do not establish complete reaping. Preserve failed handles or surface an explicit incomplete-cleanup result. |
| F-5 | P2 | `routes/diff.py`, `routes/tasks.py`, `d3_store.py` diff wrappers | **VERIFIED source gap** failures become empty/default successful diffs | Consumers can mistake an inspection failure for a complete no-change result. Propagate a non-success/error state and preserve tenant-scoped authorization. |
| F-6 | P2 / coverage gap | `worktree.py:376-443` and read-only callers | **VERIFIED closure, NOT VERIFIED real governance execution** | Closed action/argv validation and four mutation guards are present. Optional governance modules are absent, so successful governed read-only dispatch remains unverified. |
| F-7 | P2 / hardening gap | `context.py:402-427` | **PARTIAL** symlink and traversal checks exist, but there is no atomic open/read boundary | A concurrent path swap can create a TOCTOU read outside the intended file identity. This is not demonstrated in the current threat model, but remains unproven for hostile concurrent workspaces. |

F-1 through F-5 are not merely missing tests; they follow from current control
flow. F-6 and F-7 are primarily evidence/hardening gaps and are not asserted
as arbitrary execution bypasses.

## 5. Boundary A — read-only diff correctness and data exposure

### 5.1 Changed `get_execution_diff()` path

**Source reviewed:** `backend/execution/agent_runtime/context.py:402-555`.

The changed function has a real improvement over the audited predecessor:

- no `git add`, `git add -N`, index refresh, staging, commit, or worktree
  lifecycle command appears in the function;
- tracked changes use `git --no-optional-locks diff --no-ext-diff
  --no-textconv --no-color <base_ref> --`;
- untracked files use `git --no-optional-locks ls-files --others
  --exclude-standard -z --`;
- Git nonzero return codes and launch/timeout failures become
  `DiffExtractionError` from `_read_only_git()`;
- the attached-worktree path does not silently fall back to the prior
  `worktree_diff()` helper after an inspection failure; and
- the plain-workspace path also raises for unsafe/read failures rather than
  swallowing them.

The `--exclude-standard` option correctly applies Git's standard ignore
mechanisms. This prevents ignored files from being listed merely because they
are untracked. It does not constitute a general secret policy: untracked files
that are not ignored are still listed.

### 5.2 Path and sensitive-file review

`_safe_diff_path()` rejects absolute paths, NUL bytes, paths resolving outside
the workspace, a `.git` path component, `.env`, and names beginning `.env.`.
It rejects a final symlink before resolving, and a resolved path outside the
workspace is rejected. These checks cover the ordinary traversal and direct
symlink cases tested by the focused suite.

They do not establish all of the requested data boundary:

- there is no explicit block for `.ssh`, `.aws`, private-key names such as
  `id_rsa`, certificate/key bundles, local database files, or arbitrary
  application secrets;
- a concurrent replacement after the `is_symlink()`/`resolve()` check and
  before `read_bytes()` is not prevented by an atomic descriptor-relative
  open; and
- the function accepts a caller-supplied context whose `workspace_dir` is only
  compared for inequality with `repo_root`. It does not verify that the path
  is a registered worktree, belongs to the supplied `worktree_id`, belongs to
  the supplied tenant/session, or is even a worktree of the supplied repo.

The last point is an internal caller boundary rather than a demonstrated HTTP
route bypass. It is nevertheless material because `get_execution_diff()` is
exported from `backend.execution.agent_runtime` and is called from scripts,
tests, and the runtime. The helper itself has no authorization or tenant
check.

### 5.3 Independent disposable probe

A temporary repository and temporary Git worktree outside the project root
were created solely for a probe. The probe created an untracked
`.ssh/id_rsa`-named file containing a synthetic sentinel, invoked the current
`get_execution_diff()`, and printed only booleans. Observed result:

```text
UNTRACKED_PRIVATE_PATH_INCLUDED=True
ENV_PATH_INCLUDED=False
```

This did not read any real credential. It confirms the distinction between the
narrow `.env` denylist and a general sensitive-file policy.

### 5.4 Output bounds and unusual content

The current implementation reads every listed untracked file with
`read_bytes()`, decodes all text into memory, joins all per-file diffs into one
string, and the HTTP wrappers additionally call `splitlines()`. No maximum
file size, aggregate byte size, file count, line count, or response size is
enforced in this path. Binary files are recognized by a NUL byte and invalid
UTF-8 is replaced, which is safer for decoding but does not limit size.

Deleted and tracked files are delegated to Git and were not independently
exercised against a real project worktree. Empty-file formatting, unusual
non-UTF-8 filenames, and concurrent path replacement are not fully verified.
The absence of limits is a confirmed availability/data-exposure concern, not
just a missing test: a reachable caller can supply a worktree containing a
very large number or size of untracked, nonignored files.

### 5.5 Alternate diff wrappers and authorization

The new helper is not the only reachable diff implementation:

- `backend/routes/diff.py:19-72` is token-authenticated but does not obtain a
  tenant from the authenticated identity before reading `workspace/<task_id>`
  or calling `durable_state.get_store().fetch(task_id)`. The store method at
  `durable_state.py:253-259` selects only by `execution_id`, not tenant.
- `routes/diff.py` performs `realpath(join(WORKSPACE_BASE, task_id))` but has
  no `relative_to`/root-confinement check. A safe mocked probe using `task_id`
  `..` resolved the candidate to the probe's parent root and returned a
  successful response from a fake `git diff` result. No real repository command
  was launched by that probe.
- `backend/routes/tasks.py:788-815` does tenant-scope the durable record, but
  catches `worktree_diff()` errors and still returns `ok: true` with an empty
  diff. The review route at lines 818-852 has the same pattern.
- `backend/db/d3_store.py:645-674` scopes the worktree record by tenant, but
  returns `ok: true` and `# No changes detected.` when the direct Git command
  fails or raises. It also does not pass `--no-optional-locks` or impose an
  output bound.

These wrappers mean the implementation's precise `DiffExtractionError`
contract is not preserved end-to-end. The public route-level tenant/root issue
is the highest-impact alternate-path finding in this audit.

## 6. Boundary B — `_terminate_children()` safety-cleanup exception

### 6.1 What is correctly guarded

The authoritative policy in `backend/execution/control_plane_policy.py:55-101`
is source-locked to `execution_enabled=False`. The following current paths
assert it before starting or continuing execution:

- `RealAgentRuntime.__init__()` and `run_stream()`;
- `attach_execution_worktree()`;
- `cleanup_execution_worktree()` when a worktree ID exists;
- `create_worktree()`, `discard_worktree()`, `sweep_session()`, and
  `sweep_orphans()`;
- the four physical `_git()` actions; and
- the runner, sandbox, queue, fabric, and legacy session execution boundaries
  found by the caller search.

The normal `discard_worktree()` flow is therefore ordered as policy assertion,
worktree lookup, `_terminate_children()`, and only then Git/filesystem cleanup.
The existing focused test proves the disabled discard/sweep helpers do not
reach child termination. No current backend caller of `attach_process()` was
found by source search.

### 6.2 Why ownership is not adequately established

The implementation report calls the exception ownership-limited, but the
current source establishes only:

```text
worktree_id exists as a key in the process-local _REGISTRY
```

It does not establish:

- that the caller owns the tenant or session represented by the worktree;
- that the caller created the process;
- that the object is a `subprocess.Popen` created by an approved runtime;
- that its PID, creation time, parent, command, or runtime token matches the
  worktree;
- that the worktree is still active; or
- that the process is associated with the current cleanup request rather than
  another worker or tenant.

`attach_process(worktree_id, proc)` is importable and accepts an arbitrary
object with process-like methods. It checks only dictionary membership. It
accepts existing entries whose `Worktree.status` is `discarded` or `failed`.
The helper also has no session or tenant parameter. This is label-based
association, not demonstrated ownership.

An isolated fake-process probe inserted a synthetic `Worktree` with status
`discarded`, attached a process-like object using its ID, and called
`_terminate_children()`. Observed result:

```text
DISCARDED_ID_TERMINATED=True
```

The probe changed only module globals in a short-lived Python process and did
not launch or terminate a real child. It confirms the status/reuse weakness.

### 6.3 Cleanup ordering and failure semantics

For a live registered handle, the intended order is present: `poll()`,
`terminate()`, `wait(timeout=grace_sec)`, and then `kill()` plus a second
`wait()` after `TimeoutExpired`. The focused tests cover this ordering with
fake processes and no real process creation.

The failure path is weaker than the report claims:

1. `_PROCS.pop(worktree_id, [])` removes all handles before attempting them.
2. Any exception from `poll`, `terminate`, `wait`, `kill`, or the second wait
   is caught and logged.
3. The failed handle is not restored or marked for retry.
4. The function returns only the count of successfully reaped handles.
5. `discard_worktree()` can proceed to Git removal and report `ok` based on
   removal even when a child remains alive.

`grace_sec` is not validated at runtime. Negative, non-finite, or excessively
large direct inputs are not bounded by the helper. These are secondary P2
hardening concerns, but they matter because the helper is directly callable.

### 6.4 Independent decision

Termination of an already-owned child can legitimately be a control-plane
safety operation; blanket blocking would risk leaving a live child writing to
an old worktree. The current implementation, however, has not proven the
“already-owned” condition strongly enough. I therefore do **not** accept the
exception as fully closed. It is **PARTIAL**: normal disabled callers are
guarded, fake-process ordering is correct, but direct attachment and stale-ID
ownership are insufficiently constrained.

## 7. Boundary C — closed `_git()` action/argv validation

### 7.1 Verified action closure

The current `_SUPPORTED_GIT_ACTIONS` set contains exactly:

```text
rev_parse
worktree_add
worktree_remove
worktree_prune
branch_delete
worktree_diff
worktree_list
```

`_validate_git_action()` runs before the physical-mutation policy assertion,
`_require_governance()`, governance `before_dispatch()`, safe-subprocess
argument construction, and `subprocess.run()`.

The exact four mutation actions remain in `_PHYSICAL_GIT_MUTATIONS` and call
`assert_agent_execution_enabled()` before governance. The parameterized test
uses valid command shapes for all four and proves the expected
`ExecutionDisabledError`; `_require_governance`, `get_tool_governance`, and
`subprocess.run` are all uncalled. The disposable fixture repository's index
also remains unchanged.

Unknown action names raise `WorktreeError(decision="unsupported-action")`
before governance. Exact command shapes are enforced for `rev_parse`,
`worktree_prune`, and `worktree_list`; structured shapes are enforced for the
remaining supported actions. The use of argv lists and `shell=False` in the
governance-built subprocess kwargs avoids shell metacharacter injection in the
validated values.

### 7.2 Limits of the validation

The validation is a closed dispatcher, but it is not a complete ownership or
path authorization layer:

- mutation-shaped paths and branch names are syntactically accepted when
  execution is enabled; `_git()` itself does not prove they belong to the
  caller's tenant or worktree;
- `rev_parse`, `worktree_list`, and `worktree_diff` accept caller-provided
  `cwd`/repository values through their direct helpers, with authorization
  supplied only by their current callers;
- the current source has no action aliases, but no separate public guarantee
  prevents future wrappers from calling lower-level subprocess functions; and
- malformed mutation actions under Option B fail as `WorktreeError` during
  validation rather than as the stable `ExecutionDisabledError`. This is
  fail-closed and side-effect free, but callers must not treat every malformed
  input as the public policy response.

No current direct caller was found that supplies an alternate mutation alias.
The current public D3 physical worktree routes are separately blocked, and the
legacy lifecycle helpers assert before their physical effects.

### 7.3 Governance evidence limitation

The workspace does not contain the optional `security.command_policy`,
`execution.tool_governance`, and related integration modules referenced by
`worktree.py`. Their absence is fail-closed at `_require_governance()`; it was
not bypassed during this audit. Consequently:

- the negative mutation tests prove pre-governance policy ordering;
- unknown-action tests prove validation ordering; but
- successful governed `worktree_diff()` and `list_git_worktrees()` execution
  was not independently verified.

The implementation report correctly disclosed this limitation. The focused
suite's passing count must not be read as proof that the real governance-backed
read-only path works in a complete deployment.

## 8. Safe verification performed independently

No full repository suite was run. All pytest runs disabled bytecode and the
pytest cache. Git-dependent test fixtures used temporary directories under
`C:\Users\mdham\AppData\Local\Temp\omnirush`, not the Aetherion project.
The Phase 2A test used a disposable SQLite database outside the repository.
No test launched a real child process, worker, service, application server, or
production database operation.

### 8.1 Legacy residual suite

Exact command:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; $env:GIT_OPTIONAL_LOCKS='0'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-independent-legacy-basetemp' tests/integration/test_legacy_worktree_option_b_guards.py
```

Observed result:

```text
18 passed in 6.12s
```

Exact count: **18 passed, 0 failed, 0 errors, 0 deselected**.

This is a current independent rerun, not acceptance of the implementation
report's historical count. It covered all four mutation actions, unknown
actions, fake-process ordering, disposable tracked/untracked diff behavior,
index preservation in the disposable fixture, failure signaling, and plain
workspace traversal rejection.

### 8.2 Phase 2 policy regression

Exact command:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; $env:GIT_OPTIONAL_LOCKS='0'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-independent-phase2-basetemp' tests/integration/test_phase2_control_plane_execution_policy.py
```

Observed result:

```text
10 passed in 3.82s
```

Exact count: **10 passed, 0 failed, 0 errors, 0 deselected**.

### 8.3 Phase 2A control-plane contract regression

Exact command:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; $env:GIT_OPTIONAL_LOCKS='0'; $env:DATABASE_URL='sqlite:///C:/Users/mdham/AppData/Local/Temp/omnirush/aetherion-independent-phase2a.db'; python -m pytest -q -p no:cacheprovider --basetemp='C:\Users\mdham\AppData\Local\Temp\omnirush\aetherion-independent-phase2a-basetemp' tests/integration/test_phase2a_control_plane_contract.py
```

Observed result:

```text
22 passed in 3.54s
```

Exact count: **22 passed, 0 failed, 0 errors, 0 deselected**.

The disposable SQLite path was outside the project. No ambient PostgreSQL
connection was used for this audit rerun.

### 8.4 Focused syntax compilation

Exact command:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; python -m py_compile backend/execution/agent_runtime/context.py backend/execution/worktree.py backend/execution/control_plane_policy.py backend/execution/agent_runtime/runtime.py backend/routes/diff.py backend/routes/tasks.py backend/db/d3_store.py tests/integration/test_legacy_worktree_option_b_guards.py
```

Observed result: **exit code 0**, no compiler output.

### 8.5 Additional safe probes

The following bounded probes were not counted as pytest passes:

| Probe | Result |
|---|---|
| Disposable attached worktree with synthetic `.ssh/id_rsa` untracked file | `UNTRACKED_PRIVATE_PATH_INCLUDED=True`; `.env` exact/variant path was not included |
| Mocked `routes/diff.py` call with `task_id=".."` and a temporary parent containing `.git` | `ROUTE_ESCAPE_PROBE_OK=True`; fake diff returned; `cwd` resolved to the parent root |
| Fake process attached to a registry worktree with `status="discarded"` | `DISCARDED_ID_TERMINATED=True` |
| Source search for current `attach_process()` callers | No backend caller found outside the definition; direct import remains possible |
| Source search for `get_execution_diff()` callers | Runtime, package export, QA/scripts, and Phase 13/14 tests found; no route-level call found |

The first three probes used only synthetic sentinels and process-like objects.
They did not print or access real credentials and did not mutate the project
repository.

## 9. Exact reviewed source, test, and report hashes

These SHA-256 values were independently captured from the current disk after
the safe verification runs and before this report was finalized.

### 9.1 Reviewed source and test files

| Path | SHA-256 |
|---|---|
| `backend/execution/agent_runtime/context.py` | `bae7262ba1631e49904c87284388b9c2b29a4f006db0415b163405559df8d0b1` |
| `backend/execution/worktree.py` | `de26de461b3e1236c40e1650092b244dc767f8424871a9dbab847fa064d7c48b` |
| `backend/execution/control_plane_policy.py` | `0b87fbafeb444ffc049b2c36afbedc480666f5195aac349e31142823374362e7` |
| `backend/execution/agent_runtime/runtime.py` | `988dfa47ce0fc485b8d86dc9f9a14b14f6840f120c897237344dbe9aa8ace51b` |
| `backend/routes/diff.py` | `ffdd7fefecce48b5e18188bb93159444872ddc937744ada68907d8505340c23a` |
| `backend/routes/tasks.py` | `1b6b24aa67799e97ccf8a4d02a1a728005b88f703d89c8cd7b8bc308b8eff5ee` |
| `backend/db/d3_store.py` | `00d919983ad8b142dc5b6a5510472e72c1fd12b913e414ec4736e098696dd1ee` |
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |
| `tests/integration/test_legacy_worktree_option_b_guards.py` | `5c392868422a2614103744570534c7a25f234b00b3768dc77eab6ec04f57e455` |

### 9.2 Existing reports

| Path | SHA-256 |
|---|---|
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FIX_REPORT.md` | `f250f11753c41bcc008d880a8ec1f64451d03d1dfa8c8ec6f47f58047314f2d9` |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_INDEPENDENT_AUDIT.md` | `cff34467f0d46822de3ac4d16a517f514a5be5e7af2731a0595b2b9d189e986c` |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FOLLOWUP_IMPLEMENTATION_REPORT.md` | `8499f6a6e1f2689297222762a5b87219ca58a03c7fddc02cc8d470cf1dfe9a84` |
| `AETHERION_PHASE_2_CONTROL_PLANE_EXECUTION_POLICY_REPORT.md` | `aa5164279d8d57610bd5f612bec7b220ea9c83e3008c417a681a0ff42b9a068c` |
| `AETHERION_PHASE_2_POST_IMPLEMENTATION_INDEPENDENT_AUDIT.md` | `c2339dac5e268b47c8640d295452c966339a5c9ee59fc1a2566d6257ce66a0f0` |
| `AETHERION_PHASE_2A_CONTROL_PLANE_CONTRACT_FIX_REPORT.md` | `81926e678ed68511464c239165b6fdd4c64fdf9cf2fb99d62e405fdaeb5afd2d` |
| `AETHERION_PHASE_2A_POST_IMPLEMENTATION_INDEPENDENT_SECURITY_AUDIT.md` | `f45bdbb4f53d08115159e9229c486c3dee29e3c19fdd56d6380bbb59b0ebabbf` |

The SHA-256 of this new report is intentionally not embedded in its own
contents because doing so would change the bytes being hashed. It is captured
after the final write in the completion custody check and in the final response.

## 10. Index and preservation conclusion

The project index SHA-256 was:

```text
Before targeted audit verification: 18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
After targeted tests, probes, and final pre-report custody check: 18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e
```

This is genuine before/after evidence for this read-only audit window. It does
not retroactively prove the implementation task's earlier index state beyond
the historical claim discussed in §3.2.

The audit used `git --no-optional-locks`, `GIT_OPTIONAL_LOCKS=0`, file hashing,
and status inspection only. It did not invoke `git add`, `git add -N`, `git
reset`, `git clean`, checkout, commit, push, branch mutation, worktree add,
worktree remove, worktree prune, or any command against the project repository
that should change its index. The temporary Git fixture operations were outside
the project root.

Before this report was created, relevant status was:

```text
 M backend/routes/d3_features.py
?? AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FIX_REPORT.md
?? AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FOLLOWUP_IMPLEMENTATION_REPORT.md
?? AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_INDEPENDENT_AUDIT.md
?? backend/db/d3_store.py
?? backend/execution/agent_runtime/context.py
?? backend/execution/agent_runtime/runtime.py
?? backend/execution/control_plane_policy.py
?? backend/execution/worktree.py
?? backend/routes/diff.py
?? backend/routes/tasks.py
?? tests/integration/test_legacy_worktree_option_b_guards.py
```

The source/report paths listed as untracked above were already present before
this audit. No unrelated path was written by the audit. After this report is
created, the only additional project status entry should be this report.

## 11. Remaining unverified areas and production prerequisites

This audit does not certify:

- production HTTPS, deployment, image provenance, infrastructure, or release
  freeze;
- real database/Redis/service/worker behavior;
- successful execution through the unavailable governance modules;
- complete tenant authorization of every route outside the inspected diff
  wrappers;
- race-resistant file descriptor handling under hostile concurrent workspace
  mutation;
- bounded diff output under large repositories or large untracked files; or
- complete cleanup of processes created by any future execution-enabled runtime.

The Option B source lock remains active. Autonomous agent, command, plugin, and
untrusted worker execution must remain disabled. The passing focused suites
are evidence for bounded negative paths, not a production readiness gate.

## 12. Smallest justified next action

Before any release decision, perform one narrowly scoped remediation review of
the four confirmed residual behaviors: (1) tenant/root-constrain or retire the
alternate `routes/diff.py` path, (2) add an explicit sensitive-file and bounded
diff-output policy, (3) establish real process/worktree ownership and reject
stale IDs for teardown, and (4) make every diff wrapper propagate inspection
failure truthfully. Then rerun this bounded audit against the resulting source.

Do not enable agent execution or begin a later master-plan phase before that
review and remediation evidence are accepted.
