# AETHERION AI — OPTION B RESIDUAL SECURITY REMEDIATION IMPLEMENTATION REPORT

**Task:** Four-finding residual security remediation  
**Implementation date:** 2026-10-16  
**Release scope:** Option B — control-plane release; autonomous execution remains disabled  
**Narrow remediation verdict:** **PASS** for the implemented and focused-tested scope  
**Production verdict:** **NOT CERTIFIED / NO PRODUCTION READINESS CLAIM**

## 1. Executive result

The four residual behaviours identified by
`AETHERION_OPTION_B_RESIDUAL_HARDENING_INDEPENDENT_SECURITY_AUDIT.md` were
remediated with focused source and regression-test changes:

| Finding | Source remediation | Verification result |
|---|---|---|
| A — alternate diff route lacked tenant/root confinement | `routes/diff.py` now resolves an opaque task ID through `durable_state.get(task_id, tenant_id)`, rejects path-like IDs before lookup, never derives a filesystem path from the request, and permits only a stored result or a registry worktree authorized for the same tenant/execution | **Verified** by 29 focused remediation tests, including unknown, traversal-like, cross-tenant, and no-fallback cases |
| B — sensitive untracked files and unbounded output | New centralized `execution/diff_policy.py`; explicit sensitive-directory/name/suffix exclusions; bounded Git pipe capture; 100-file, 1 MiB/file, 8 MiB aggregate, 10 MiB rendered-output, 64 KiB stderr, and 5-second Git limits | **Verified** by synthetic sensitive-path, ignored-file, binary, per-file, count, aggregate, and rendered-output tests |
| C — process attachment was identifier-only | `Worktree.owner_token` capability, active-status requirement, tenant/session/execution matching, actual `subprocess.Popen` type requirement, duplicate prevention, stale-token filtering, and discarded-ID rejection | **Verified** by 23 legacy guard tests, including cross-owner and discarded-ID cases; no real process launched |
| D — diff failures became successful empty/default results | Alternate route, task diff/review wrappers, and D3 store now return explicit sanitized failures; only an explicitly stored empty diff is a successful no-change result | **Verified** by route/wrapper/D3 failure tests and focused source-path coverage |

The Option B source lock was not weakened or bypassed. No agent, command,
plugin, worker, service, container, real process, database, branch, or real
worktree operation was started or changed.

## 2. Baseline and preservation evidence

### 2.1 Pre-edit workspace state

Captured before source edits, using `git --no-optional-locks` and SHA-256 file
hashes:

| Item | Observed value |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries (`git status --porcelain=v1 -uall`) | 2,232 |
| Untracked entries | 2,231 |
| Modified/staged entries | 1 — pre-existing `backend/routes/d3_features.py` |
| Project Git index SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| Required remediation report before work | Absent |

The workspace already contained a very large untracked tree. No pre-existing
untracked file was overwritten, staged, cleaned, reset, or removed.

### 2.2 Scope expansion

Two additional implementation files were necessary and are explicitly included
in this report:

- `backend/execution/diff_policy.py` — required to centralize sensitive-path
  exclusions, resource limits, bounded Git capture, sanitized errors, and
  response-size validation across diff producers.
- `tests/integration/test_option_b_residual_security_remediation.py` — required
  for isolated alternate-route, sensitive-file, limit, wrapper, and D3 failure
  regression coverage.

`backend/routes/tasks.py` and `backend/db/d3_store.py` were also necessary
because the independent audit identified their catch-all/default-success diff
paths as part of Finding D. `backend/routes/d3_features.py` was not modified.

### 2.3 Protected state

| Protected item | Before SHA-256 | Final SHA-256 | Result |
|---|---|---|---|
| `backend/routes/d3_features.py` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` | Preserved |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FIX_REPORT.md` | `f250f11753c41bcc008d880a8ec1f64451d03d1dfa8c8ec6f47f58047314f2d9` | `f250f11753c41bcc008d880a8ec1f64451d03d1dfa8c8ec6f47f58047314f2d9` | Preserved |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_INDEPENDENT_AUDIT.md` | `cff34467f0d46822de3ac4d16a517f514a5be5e7af2731a0595b2b9d189e986c` | `cff34467f0d46822de3ac4d16a517f514a5be5e7af2731a0595b2b9d189e986c` | Preserved |
| `AETHERION_LEGACY_WORKTREE_OPTION_B_GUARD_FOLLOWUP_IMPLEMENTATION_REPORT.md` | `8499f6a6e1f2689297222762a5b87219ca58a03c7fddc02cc8d470cf1dfe9a84` | `8499f6a6e1f2689297222762a5b87219ca58a03c7fddc02cc8d470cf1dfe9a84` | Preserved |
| `AETHERION_OPTION_B_RESIDUAL_HARDENING_INDEPENDENT_SECURITY_AUDIT.md` | `a49bdd89982174377df640eac0550e1536d5007fd204620e871a7a6a0cf9c6c3` | `a49bdd89982174377df640eac0550e1536d5007fd204620e871a7a6a0cf9c6c3` | Preserved |

No Hermes reference-tree path, populated `.env`, credential, token, password,
key, credential-bearing archive, deployment asset, or D3 source was modified
or read for content.

## 3. Implemented changes

### 3.1 Shared diff policy — Finding B and Finding D

`backend/execution/diff_policy.py` is intentionally dependency-light and is now
the common policy module for the runtime diff producer, the alternate diff route,
task wrappers, and the D3 store.

It provides:

- NUL, absolute-path, dot-component, traversal, symlink, and workspace-root
  validation.
- Exclusion of `.git`, `.ssh`, `.aws`, `.azure`, `.gnupg`, and `.kube`
  directories.
- Exclusion of `.env` and `.env.*` files, credential/key filenames including
  `id_rsa`, `id_ed25519`, `credentials`, `credentials.json`, `secrets.json`,
  `service-account.json`, and `token.json`.
- Exclusion of `.pem`, `.key`, `.p12`, `.pfx`, and `.jks` suffixes.
- Fixed source limits that cannot be relaxed through ambient environment
  variables:

  | Limit | Value |
  |---|---:|
  | Untracked candidate count | 100 files |
  | Untracked single-file input | 1 MiB |
  | Untracked aggregate input | 8 MiB |
  | Complete rendered diff | 10 MiB |
  | Retained stderr prefix | 64 KiB |
  | Read-only Git wall-clock timeout | 5 seconds |

`run_bounded_readonly_git()` uses `shell=False`, drains stdout and stderr in
separate reader threads to avoid pipe deadlock, retains only bounded prefixes,
rejects stdout over the complete-result budget, rejects non-zero Git exits, and
converts launch/timeout/failure conditions into `DiffExtractionError`.

The result is rejected rather than silently truncated. Untracked files are
rendered in memory without staging, refreshing, or mutating any Git index.

### 3.2 Execution-context diff extraction — Finding B and Finding D

`backend/execution/agent_runtime/context.py` now:

- Requires an active registry worktree when a context carries `worktree_id`.
- Requires the authorized worktree tenant, session, execution ID, path, and
  repository root to match the context.
- Refuses to treat the main repository root as an isolated worktree.
- Refuses missing/invalid worktree metadata instead of falling back to a plain
  workspace read.
- Keeps the plain-workspace path available only when no worktree ID is present.
- Applies the shared policy to tracked diffs, untracked listings, untracked
  files, binary files, explicit modified files, and the final rendered result.
- Raises on missing files and extraction failures; it does not return an empty
  success for a failed or incomplete inspection.

### 3.3 Alternate route and task wrappers — Finding A and Finding D

`backend/routes/diff.py` now:

- Validates the opaque task identifier before any durable lookup.
- Performs the tenant-scoped lookup with `durable_state.get(task_id, tenant_id)`.
- Never joins the request ID to the current working directory or any filesystem
  path.
- Returns an explicitly stored empty string as a truthful successful no-change
  result.
- Uses only an authorized, active registry worktree matching tenant and task
  execution ID when a stored diff is absent.
- Returns sanitized `TASK_NOT_FOUND`, `DIFF_RECORD_INVALID`,
  `DIFF_UNAVAILABLE`, `DIFF_NOT_FOUND`, and `DIFF_LIMIT_EXCEEDED` outcomes.

`backend/routes/tasks.py` applies the same stored-result-versus-authorized-
worktree rule to task diff and review endpoints. Catch-all failures now return
an explicit unavailable response without exposing exception text or pretending
that the result is empty.

### 3.4 D3 worktree diff — Finding B and Finding D

`backend/db/d3_store.py` now:

- Resolves worktree metadata tenant-scoped through `get_worktree(tenant_id,
  worktree_id)`.
- Rejects a candidate that is the repository root or outside the authorized
  repository.
- Rejects missing directories and invalid base refs.
- Uses the shared bounded, read-only Git runner.
- Returns `ok: false` with a sanitized `DIFF_UNAVAILABLE` response on path,
  Git, timeout, size, or extraction failure.

### 3.5 Process/worktree ownership — Finding C

`backend/execution/worktree.py` now gives each `Worktree` an in-process
`owner_token`. The token is omitted from `to_dict()` event/API payloads and is
required, together with tenant, session, and execution identity, by
`attach_process()`.

The attachment path additionally requires a concrete `subprocess.Popen` handle,
rejects discarded/unknown worktrees, rejects cross-owner capabilities, and
prevents duplicate attachment of the same handle. `_terminate_children()` is
not a general process-kill primitive: it is a no-op for unknown or non-active
worktrees and only reaps handles whose registered token matches the active
worktree token.

The positive cleanup test uses a fake process object only as a patched stand-in
for the trusted runtime's concrete `Popen` class; it launches no child process.

## 4. Tests and verification

All commands below were run from the repository root with
`PYTHONDONTWRITEBYTECODE=1`, `GIT_OPTIONAL_LOCKS=0`, pytest cache disabled, and
disposable basetemp directories outside the workspace unless noted.

### 4.1 Final targeted results

| Command target | Result |
|---|---:|
| `tests/integration/test_legacy_worktree_option_b_guards.py` | **24 passed** in 4.64s |
| `tests/integration/test_option_b_residual_security_remediation.py` | **29 passed** in 12.71s |
| `tests/integration/test_phase2_control_plane_execution_policy.py` | **10 passed** in 10.76s |
| `tests/integration/test_phase2a_control_plane_contract.py` with disposable SQLite | **22 passed** in 10.70s |
| `python -m py_compile` on all changed source/test files | exit code 0, no output |

The final remediation-specific runs included two no-fallback tests and a
positive trusted-handle attachment test. The legacy run exposed one fixture
that had intentionally omitted the disposable repository directory; the
fixture was corrected without changing production behavior, and the final
rerun passed.

### 4.2 Earlier transient failures, retained for auditability

Earlier attempts were not treated as passing evidence:

- Initial pytest attempts failed setup because the requested disposable
  `--basetemp` parent did not exist.
- The first focused collection attempt failed because the disposable SQLite
  parent path did not exist.
- A later legacy run reported 19 passed and 4 ownership-message assertion
  failures; the implementation messages were made decision-specific.
- A focused run reported 25 passed before the two final no-fallback tests were
  added; the final focused run reported 29 passed after the strict `.` task-ID
  rejection case was added.

No full repository suite, service startup, production database, worker, agent,
provider, or real child process was run.

## 5. Acceptance checklist

| Acceptance criterion | Evidence | Status |
|---|---|---|
| Alternate diff route uses tenant-scoped durable lookup | `diff_route.durable_state.get(task_id, tenant_id)` and route tests asserting the exact call | **PASS** |
| Path-like IDs are rejected before lookup | Six focused invalid-ID cases, including `.`, traversal, Windows drive, UNC, and mixed separators | **PASS** |
| No request-ID-to-filesystem fallback remains | Route source has no `cwd`/path derivation; missing-result and no-fallback tests return `DIFF_UNAVAILABLE` | **PASS** |
| Sensitive files are excluded before file read | Shared path policy and seven focused sensitive-path cases | **PASS** |
| Ordinary ignored files remain excluded | Real disposable Git fixture with `.gitignore` | **PASS** |
| Binary and ordinary untracked files remain representable | Focused binary and ordinary-source cases | **PASS** |
| Count, per-file, aggregate, rendered and Git output bounds exist | Fixed constants, bounded reader, and explicit limit tests | **PASS** |
| Diff extraction does not mutate the Git index | Existing legacy index-hash assertions plus read-only Git argument path | **PASS** |
| Process ownership is not identifier-only | Owner token, identity tuple, concrete Popen type, active status, duplicate and stale-token checks | **PASS** in focused scope |
| Valid owned cleanup works without launching a child | Patched trusted-Popen stand-in and cleanup assertion | **PASS** |
| Failures are not returned as empty success | Route, task-wrapper, D3, missing-file, Git failure, and worktree no-fallback tests | **PASS** |
| Option B execution remains disabled | Phase 2: 10 passed; Phase 2A: 22 passed | **PASS** |

## 6. Changed files and hashes

The following are the final implementation/test file SHA-256 values captured
after the final targeted verification. Existing files were already present in
the workspace before this remediation; `diff_policy.py` and the focused
remediation test were new in this task.

| File | Before | Final |
|---|---|---|
| `backend/execution/diff_policy.py` | absent | `e66503239f3fc3dc3b0ba46e79703839792d1edb3e1652d62808f90d199047d0` |
| `backend/execution/agent_runtime/context.py` | `bae9c6d2bfc4066f88669d338b5f0e0f7a69d5f202e27b59e44b737c0e588dc4` | `25145b984f1f752c252d7cbc0a374491bceb499fe2dcf72bc84eefeb153671b4` |
| `backend/execution/worktree.py` | `de26e9647063548fd87f50591896739b2304c6f478d83ddfe3da5ffc3eae9633` | `ac0cacb3156a8b48e061a17434d2d7f8c0b7aed2ba5db0462e8a071051e6b833` |
| `backend/routes/diff.py` | `ffdd5acfe6a3659dfd956fa9b2a955d4b00841b057dcd3e06f24a28dcc3f8620` | `dccd1479ddd5a4743fda32e333f252e82e8d8e545cc593e38c0d2cb456d374fe` |
| `backend/routes/tasks.py` | `1b6eaec98bd20a4d1554fe739310f034c877c32de1f2ff3e82b21a01a753d523` | `073af123a4924e35daa43c14f823978fdf9df802c23d70d4a581e5d35b286604` |
| `backend/db/d3_store.py` | `00d4ad0002c4b349c0b2fa8e6ae6c8a68b3c82691e6d70545519fb06f1d38e26` | `0dd63df9e2ae51289307e6c7c69a13f5699d94f215a20503455ef084709b79fb` |
| `tests/integration/test_legacy_worktree_option_b_guards.py` | `5c392ca3e1c9b0dd671c03dce7adfd673457c8bcd2bcc7cf6a5bbf4432a23f06` | `473b49667cda41c0f8470cca2111bd28ed5ddb1e58c3e5d6a26cf690eb5670b3` |
| `tests/integration/test_option_b_residual_security_remediation.py` | absent | `f44d12c6ac5fb82c946bfde07351c534a52756398d7fb370702209478af8334f` |

These hashes were captured after the final targeted verification and before the
final integrity check below.

## 7. Limitations and residual risk

- Governance imports remain unavailable in this local environment, so the
  governed `_git()` path in the legacy worktree lifecycle was not exercised
  with the real policy/governance modules. The code retains its fail-closed
  import behavior and the existing negative guards were rerun.
- No production PostgreSQL, multi-worker deployment, auth middleware server,
  real process supervisor, or provider was started. SQLite Phase 2A was used
  only as a disposable contract fixture.
- The process registry is process-local by design. Durable worktree metadata
  remains tenant-scoped, but a process restart requires the trusted lifecycle
  to re-adopt handles rather than inheriting this in-memory registry.
- The new bounds are conservative source policy and may reject unusually large
  legitimate diffs instead of truncating them. This is intentional for
  fail-closed complete-result semantics.
- No claim is made that the broader repository is production-ready. The result
  is limited to these four residual findings and the checks listed above.

## 8. Final disposition

For the requested four-finding Option B remediation, the implementation is
**complete for the focused scope**: source changes are present, regression
tests cover the required boundaries, final targeted checks pass, and protected
files remain unchanged. The broader release remains a control-plane-only
release with autonomous execution disabled and is **not certified for
production deployment** by this report.

## 9. Final workspace integrity capture

Captured after the final test reruns and report completion work:

| Item | Final value |
|---|---|
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Status entries | 2,235 |
| Untracked entries | 2,234 |
| Modified/staged entries | 1 — the pre-existing `backend/routes/d3_features.py` |
| Project Git index SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| Protected D3 feature SHA-256 | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |

The only new report artifact from this task is this file:
`AETHERION_OPTION_B_RESIDUAL_SECURITY_REMEDIATION_IMPLEMENTATION_REPORT.md`.
No file was staged and no commit was created.
