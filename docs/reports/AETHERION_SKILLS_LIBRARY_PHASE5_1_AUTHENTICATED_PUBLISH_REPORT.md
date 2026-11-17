# Aetherion Skills Library Phase 5.1 — Authenticated Publish Report

Date: 2026-10-18  
Repository: `C:/Users/mdham/Downloads/Aetherion-AI-main/end to end aetherion cli`  
Scope: authenticated local-to-tenant publishing, tenant ownership, explicit runtime selection, inventory revalidation, and regression evidence.

## 1. Executive implementation result

Phase 5.1 implements a safe authenticated publish subset without enabling agent or plugin execution.

Implemented:

- `POST /api/skills/publish` accepts the selected local definition **content**, never a client file path or server-read URL.
- The server derives the tenant/workspace scope from the authenticated principal (`g.user_id`); a request-supplied workspace cannot override it.
- The server validates canonical IDs, reserved IDs, bounded UTF-8 content, strict supported frontmatter, duplicate fields, version/schema fields, and requested permissions.
- The server computes the authoritative SHA-256 and persists the tenant-owned record through the existing SQLite `ExtensionStore`.
- Repeated identical publishes are idempotent (`already_current`); a different version/content for an existing tenant/ID returns a conflict rather than silently replacing the record.
- Published records begin disabled. Permission-bearing records begin `pending_approval`; an admin-only approval route can move them to approved-but-not-selected.
- `POST /api/skills/<id>/detach` removes only the authenticated tenant's published record. It does not modify the local bundled or custom definition.
- `POST /api/skills/<id>/approve` requires the existing `admin_required` path.
- `selected_skill_ids` is validated at task creation and again at runtime selection. Only the selected permitted records are loaded; published records are never auto-injected by prompt matching.
- Published records requesting permissions remain unavailable to runtime selection under the current metadata-only Option A policy. Approval does not grant tools or execution.
- CLI `aetherion skills publish <id>` and the TUI Publish action submit exactly one installed local definition and display server-reported state.

The safe subset is functional for deterministic local SQLite and authenticated Flask test-client smoke paths. The production PostgreSQL-backed integration remains environment-blocked and was not bypassed.

## 2. Repository and protected-state baseline

Verified before editing:

| Item | Value |
|---|---|
| Repository root | `C:/Users/mdham/Downloads/Aetherion-AI-main/end to end aetherion cli` |
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` |
| Initial tracked change | `M backend/routes/d3_features.py` |
| Initial workspace | Large pre-existing untracked application/report/scratch tree; preserved in place |
| D3 SHA-256 | `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c` |
| `.git/index` SHA-256 | `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e` |
| `.git/index.lock` | absent (`False` from `Test-Path`) |
| sibling `rea/` | branch `main`, HEAD `e13da084e7c5967b80632ae5ad8d3d37bcf57469`, clean |

The D3 file, Git index, sibling `rea/`, prior Phase 4/4R/5 reports, credentials, deployment files, catalog files, and unrelated workspace changes were not reset, cleaned, committed, or pushed.

Final protected-state comparison:

- D3 SHA unchanged: **yes**, still `592e397ec9d8edf583661c2ead5852780f9bbfb96515ab997f556001783d6f5c`.
- Git index SHA unchanged: **yes**, still `18fc638834262aff249e3bddb5bf1ff23f3fca3c85adf22d4a6499168ae2ab3e`.
- `.git/index.lock` remains absent.
- No commit, push, branch switch, reset, clean, or Git-index mutation occurred.

## 3. Current inventory revalidation

The deterministic scan was run from the repository root with this exact command:

```powershell
@'
from pathlib import Path
import json,re,hashlib,collections
root=Path('cli/aetherion_cli/skills')
rows=[]; malformed=[]; no_frontmatter=[]
for p in sorted(root.rglob('SKILL.md')):
 text=p.read_text(encoding='utf-8')
 digest=hashlib.sha256(p.read_bytes()).hexdigest()[:12]
 rel=p.relative_to(root)
 m=re.search(r'(?im)^name:\s*(.+)$',text)
 raw=(m.group(1).strip().strip("'\"") if m else p.parent.name)
 sid=re.sub(r'[^a-z0-9_-]+','-',raw.lower()).strip('-_')[:64].rstrip('-_')
 if text.startswith('---'):
  lines=text.splitlines()
  closes=[i for i,line in enumerate(lines[1:],1) if line.strip()=='---']
  if not closes: malformed.append((str(rel),'missing closing frontmatter'))
 else: no_frontmatter.append(str(rel))
 rows.append((sid,str(rel),digest))
ids=[r[0] for r in rows]
map_obj=json.loads((root/'skill-mapping.json').read_text(encoding='utf-8'))
map_ids=[]
for category in map_obj.get('skill-mapping',{}).get('categories',{}).values(): map_ids.extend(category.get('skills',[]))
map_ids=set(map_ids); row_ids=set(ids)
old={}
for line in Path('AETHERION_SKILLS_LIBRARY_INVENTORY_AND_IMPLEMENTATION_REPORT.md').read_text(encoding='utf-8').splitlines():
 if line.startswith('| ') and 'bundled / definition_only_not_runtime_wired' in line:
  parts=[x.strip() for x in line.strip('|').split('|')]; old[parts[0]]=parts[4]
changed=[(sid,digest,old.get(sid)) for sid,rel,digest in rows if old.get(sid) and old[sid]!=digest]
print(json.dumps({'files':len(rows),'unique_ids':len(row_ids),'duplicate_ids':{k:v for k,v in collections.Counter(ids).items() if v>1},'duplicate_paths':len(rows)-len(set(r[1] for r in rows)),'malformed_frontmatter':malformed,'no_frontmatter':len(no_frontmatter),'mapping_ids':len(map_ids),'definitions_not_in_mapping':len(row_ids-map_ids),'mapping_entries_without_definition':len(map_ids-row_ids),'digest_changes_since_phase5_report':changed},indent=2))
'@ | python -
```

Observed result:

```json
{
  "files": 192,
  "unique_ids": 192,
  "duplicate_ids": {},
  "duplicate_paths": 0,
  "malformed_frontmatter": [],
  "no_frontmatter": 14,
  "mapping_ids": 77,
  "definitions_not_in_mapping": 129,
  "mapping_entries_without_definition": 14,
  "digest_changes_since_phase5_report": []
}
```

Interpretation:

- Current packaged catalogue: **192 files / 192 unique canonical IDs / 0 duplicates**.
- 14 definitions have no frontmatter and are classified by safe containing-directory fallback; they are not treated as malformed solely for lacking optional metadata.
- The embedded `skill-mapping.json` is a partial/inconsistent catalogue: 129 shipped definitions are not listed there and 14 mapping entries have no shipped definition. It is not used as the authoritative file inventory.
- No packaged definition changed after the Phase 5 report. The complete current per-definition SHA-256-prefix ledger is in Appendix A below.

Dependency evidence remains descriptive only: 42 definitions contain recognizable dependency/requirement text and 150 do not. No dependency was installed, and no packaged skill asset was executed.

## 4. Reconciliation of prior test evidence

The prior completion response and prior report describe two different filtered invocations, not one contradictory result:

| Exact command | Current result | Meaning |
|---|---:|---|
| `python -m pytest tests/integration/test_phase3_extensions.py -k "17a or 30 or 31a or 31_skill" -q` | `4 passed, 43 deselected`, exit 0 | Narrow Phase 5 explicit-selection/preview subset; matches the prior report's recorded evidence. |
| `python -m pytest tests/integration/test_phase3_extensions.py -k "not (25 or 26 or 27 or 28 or 29 or 33 or 38 or 39 or 40)" -q` | `38 passed, 9 deselected`, exit 0 | Broader non-execution/non-PostgreSQL subset; matches the prior completion response. |
| `python -m pytest tests/integration/test_phase3_extensions.py -q` | `38 passed, 9 failed`, exit 1 | Full file: five execution-boundary failures, one runtime-execution failure, and three PostgreSQL-dependent REST failures. |

The two prior counts are therefore reconciled as separate filtered commands. No result is silently conflated.

## 5. Authenticated publish and runtime call graph

### Publish/attach path

```text
CLI: skills publish <id>
  -> local SkillLibrary.get(id, include_content=True)
  -> require installed local record
  -> Client.post('/api/skills/publish', selected content + metadata + client digest)
  -> auth_system.token_required
  -> platform_extensions._get_tenant() from flask.g.user_id
  -> workspace scope = authenticated tenant; supplied workspace_id cannot override it
  -> ExtensionManager.publish_skill_definition()
       -> strict bounded UTF-8/frontmatter parser
       -> canonical ID/reserved-ID/schema/permission validation
       -> server SHA-256
       -> ExtensionStore.save_skill() transaction
       -> tenant owner/version/content conflict handling
       -> audit log
  -> typed created/already-current/pending response
  -> CLI GET /api/skills/<id> refreshes authoritative status
```

### Selection path

```text
POST /api/tasks with security_context.selected_skill_ids
  -> authenticated tenant derived from g.user_id
  -> ExtensionManager.resolve_runtime_skill_selection(tenant, ids)
  -> reject missing/duplicate/excess/cross-tenant/unapproved/disabled IDs
  -> store only selected IDs and server digests in task security context
  -> RealAgentRuntime reads selected_skill_ids
  -> SkillEngine explicit path resolves the same tenant-owned records
  -> only selected instruction records are rendered
```

The direct `/api/v1/agent/invoke` and durable execution paths remain blocked by the existing Option A execution policy. Selection validation is still implemented and fail-closed; it cannot turn execution on.

## 6. Files changed

| File | Purpose |
|---|---|
| `backend/execution/extensions/store.py` | Added publish lifecycle/approval/workspace/source metadata, exact owner reads, approval transition, runtime-selection query, and managed SQLite connection closure. |
| `backend/execution/extensions/manager.py` | Added strict data-only publish parser, canonical ID/reserved-ID checks, server digest validation, idempotent/conflict semantics, approval/detach operations, and bounded runtime selection. |
| `backend/execution/extensions/skill_engine.py` | Routes explicit selections through the closed runtime allow-list; prevents authenticated published records from prompt-only auto-injection while preserving legacy installed-skill compatibility. |
| `backend/routes/platform_extensions.py` | Added authenticated publish, admin approval, tenant-scoped detach, truthful published status rendering, and pending-approval enable handling. |
| `backend/routes/tasks.py` | Validates `selected_skill_ids` at authenticated task creation and stores only authorized IDs/digests. |
| `cli/aetherion_cli/main.py` | Added explicit `skills publish` action; submits one installed definition and refreshes authoritative server state. |
| `cli/aetherion_cli/tui/state.py` | Added local/tenant publication and approval state fields. |
| `cli/aetherion_cli/tui/gateway.py` | Fetches tenant status, adds authenticated publish flow, and refreshes after server response. |
| `cli/aetherion_cli/tui/screens/skills.py` | Added Publish control and truthful tenant status display. |
| `cli/tests/test_skill_library.py` | Added CLI publish success and failed-request-no-false-success tests. |
| `tests/integration/test_phase5_1_published_skills.py` | Added deterministic publish, conflict, tenant boundary, approval, permission, and closed-selection tests. |
| `AETHERION_SKILLS_LIBRARY_PHASE5_1_AUTHENTICATED_PUBLISH_REPORT.md` | This supplemental report; prior reports were not overwritten. |

No packaged `SKILL.md` file was modified.

## 7. Authentication, tenant, approval, persistence, and integrity rules

- Authentication uses the existing `token_required` decorator. Unauthenticated publish returns the existing structured 401 response.
- Tenant ownership is derived from `g.user_id`. Request `owner_id` is not accepted. The optional request `workspace_id` must equal the server-derived tenant scope.
- Published records use `(skill_id, owner_id)` as the existing store identity boundary. Cross-tenant reads return no record.
- Publish is separate from approval when permissions are requested. Approval uses the existing `admin_required` decorator and records the actor in the audit log.
- No permission is granted by Markdown. `required_tools`/`required_permissions` in publish frontmatter are rejected. Requested permissions are an explicit API field and remain pending metadata.
- Content is bounded to 512 KiB, decoded as strict UTF-8, and checked for unsupported controls.
- Only exact `---` delimiter lines are recognized. Duplicate fields and unsupported capability-granting fields are rejected.
- IDs must match `[a-z0-9][a-z0-9_-]{2,63}`, and reserved core/system IDs are rejected.
- The server computes SHA-256. A client digest is only a consistency hint and must match the server digest; it never becomes authoritative.
- Identical `(owner, ID, version, digest)` publishes are idempotent. Different content/version for the same owner/ID conflicts instead of silently replacing a published definition.
- Local uninstall removes only local state/artifacts. Remote detach removes only the authenticated tenant record.
- Failed validation or persistence returns an error before a success response. No client UI success is emitted before the API response.

## 8. Status semantics

| Status | Meaning |
|---|---|
| `local_definition` | Definition exists in the local bundled/custom Skills Library; no tenant record is implied. |
| `published_to_tenant` | Server has persisted the selected definition for the authenticated tenant; it remains disabled until explicitly enabled. |
| `pending_approval` | Server persisted a permission-bearing publish, but approval is required; it cannot be enabled or selected. |
| `approved_but_not_selected` | Published definition is approved and may be enabled, but it is not loaded unless explicitly named in `selected_skill_ids`. |
| `selected_for_runtime` | The ID passed both tenant-owned approved/enabled resolution and the closed selection allow-list. Under Option A, the actual execution endpoint remains blocked. |
| `tool_backed_and_tested` | Not claimed for any packaged or published skill. No Phase 5.1 publish creates a handler or tool. |
| `blocked_or_unavailable` | Missing, cross-tenant, pending, disabled, permission-bearing under Option A, malformed, conflicted, or otherwise rejected selection. |

Current verified counts:

| Population | Count | Evidence |
|---|---:|---|
| Packaged definitions | 192 | deterministic scan |
| Locally available packaged definitions | 192 | local catalogue |
| Locally installed in a fresh test scope | only explicitly selected fixtures | `SkillLibrary` tests |
| Published in repository test fixtures | 1 at a time in isolated tests | Phase 5.1 tests/smokes |
| Published in the user's live tenant | 0 claimed | no production tenant was used |
| Selected for runtime in verified tests | 1 isolated published fixture | closed resolver test |
| Genuinely tool-backed/tested packaged definitions | 0 | no handler is created by publish |

## 9. Security review and execution boundary

### Content and parser

The publish endpoint receives content only. It does not accept `path`, `url`, archive, package, subprocess, MCP, or dependency-install fields. The parser has a bounded grammar and rejects duplicate/unsupported capability fields. Content is stored as instructions and never imported or executed.

### Tenant isolation

The backend uses authenticated `g.user_id` for owner/workspace scope. Store skill reads/lists/version queries use exact owner matching rather than the previous global-or-owner fallback. The deterministic Phase 5.1 test confirms Alice's record is absent for Bob. The authenticated Flask smoke confirms a cross-tenant GET returns 404.

### Selection bypasses

Selection IDs are bounded to 10 unique IDs and total guidance is bounded to 12,000 characters. Missing, cross-tenant, pending, disabled, or permission-bearing published records are rejected. Unselected published definitions are not prompt-matched. Skill text cannot alter the allow-list, execution policy, tool registry, or permission state.

### Execution policy

Agent/plugin execution remains disabled. Publishing and selection do not call a tool, import a handler, install a dependency, start a subprocess, start an MCP server, or flip `execution_enabled`. API publish responses explicitly report `execution_enabled: false`.

## 10. Verification results

### Passed checks

```text
python -m pytest cli/tests -q
  exit 0; 347 tests collected and passed; 5 existing Click deprecation warnings.

python -m pytest cli/tests/test_skill_library.py -q
  exit 0; 8 tests passed.

python -m pytest tests/integration/test_phase5_1_published_skills.py -q
  exit 0; 4 tests passed.

python -m pytest cli/tests/test_skill_library.py tests/integration/test_phase5_1_published_skills.py -q
  exit 0; 12 tests passed.

python -m pytest tests/integration/test_phase3_extensions.py -k "17a or 30 or 31a or 31_skill" -q
  exit 0; 4 passed, 43 deselected.

python -m pytest tests/integration/test_phase3_extensions.py -k "not (25 or 26 or 27 or 28 or 29 or 33 or 38 or 39 or 40)" -q
  exit 0; 38 passed, 9 deselected.

python -m py_compile backend/execution/extensions/store.py backend/execution/extensions/manager.py backend/execution/extensions/skill_engine.py backend/routes/platform_extensions.py backend/routes/tasks.py cli/aetherion_cli/main.py cli/aetherion_cli/tui/gateway.py cli/aetherion_cli/tui/screens/skills.py cli/aetherion_cli/tui/state.py cli/tests/test_skill_library.py tests/integration/test_phase5_1_published_skills.py
  exit 0.
```

Authenticated Flask smoke checks were also run in an isolated SQLite/test environment (not PostgreSQL):

- unauthenticated `POST /api/skills/publish`: `401`, `UNAUTHORIZED`;
- authenticated user 101 publish: `201`, `created`, `published_to_tenant`, `execution_enabled: false`;
- authenticated user 101 GET: `200`, owner `101`;
- authenticated user 202 cross-tenant GET: `404`;
- permission-bearing publish: `201`, `pending_approval`;
- admin approval: `200`, `approved_but_not_selected`;
- publish/approval/enable did not enable execution.

### Full existing Phase 3 suite

```text
python -m pytest tests/integration/test_phase3_extensions.py -q
  exit 1; 38 passed, 9 failed.
```

The nine failures are not caused by this publish implementation:

- five tool-bridge/runtime tests stop at the intentional `ExecutionDisabledError` boundary;
- three REST tests fail while importing `auth_system` because the configured PostgreSQL server at `127.0.0.1:5432` is unavailable;
- one runtime-stream test also stops at the intentional execution-disabled boundary.

PostgreSQL was not started and execution policy was not bypassed.

### Static check

```text
python -m ruff check backend/execution/extensions/store.py backend/execution/extensions/manager.py backend/execution/extensions/skill_engine.py backend/routes/platform_extensions.py backend/routes/tasks.py tests/integration/test_phase5_1_published_skills.py cli/tests/test_skill_library.py
  exit 1; 44 findings.
```

The findings are existing repository style/undefined-name issues in the touched legacy files (compact `try` statements in `store.py`, unused imports in `manager.py`, pre-existing undefined `memory_remember`/`Optional`/`Path` in legacy routes), plus local-import style warnings in the new CLI tests. Compilation and all focused behavior tests passed. No broad lint refactor was performed.

## 11. Remaining gaps and next step

1. The production integration suite still needs its authorized PostgreSQL test environment to validate the same route behavior against the deployed database backend. This phase intentionally used isolated SQLite and deterministic fixtures instead.
2. The API currently treats authenticated user ID as workspace scope because the inspected route architecture has no separate workspace authorization helper. A future workspace-aware phase should replace `_get_workspace_scope()` with the existing workspace membership/authorization service before exposing multiple workspace IDs.
3. Legacy runtime-installed skills remain compatible with explicit selection. New authenticated published skills are stricter: they are never auto-injected and permission-bearing records remain unavailable under Option A.
4. No published definition is tool-backed. A future handler-registration phase would need a separate reviewed capability contract and must not be inferred from Markdown.
5. The prior `skill-mapping.json` remains partial; the verified `SKILL.md` tree remains the inventory source of truth.

Recommended next step: review this phase and, only after approval, add a separate workspace-membership authorization adapter and production-database integration tests. Do not enable execution as part of that work.

## 12. Final confirmation

- Authenticated publish/attach is functional: **yes, deterministic SQLite and authenticated Flask test-client paths**.
- Current verified catalogue: **192 definitions**.
- Locally available: **192 packaged definitions**; published/selected live-tenant counts are not claimed.
- Genuinely tool-backed/tested: **0 from this publish flow**.
- Closed allow-list and tenant authorization: **enforced by store owner scope, publish/list/get/approve/detach paths, task validation, and runtime resolver**.
- Custom skills remain separate from built-ins: **yes**.
- Publishing or selection accidentally enabled execution: **no**.
- Prior 4/43 versus 38/9 discrepancy: **reconciled as two distinct filtered commands**.
- No commit or push: **confirmed**.

## Appendix A — current packaged-definition digest ledger

The following SHA-256 values are the first 12 hexadecimal characters of the
current full-file digest. The exact scan above reads bytes with UTF-8 validation,
hashes the original file bytes, and compares these values with the Phase 5
ledger. There were zero changes. The full files remain in the repository at the
relative paths recorded in the prior inventory report.

| ID | SHA-256 prefix |
|---|---|
| biopython | 9079af86248f |
| claude-mem | 96c68f744ef0 |
| vercel-deploy | 31a4a5b49db7 |
| dispatching-parallel-agents | c628917689cc |
| gemini | 29205943f8ad |
| hugging-face-cli | c011e33ff672 |
| hugging-face-datasets | ca8fb141e351 |
| hugging-face-evaluation | a3acc51eb6df |
| hugging-face-model-trainer | b1d3607420e1 |
| hugging-face-trackio | da2f366b8181 |
| imagen | 3024eae8a603 |
| model-failover | 92d446b756ab |
| multi-agent-patterns | c905d5c6f3ef |
| networkx | 1aac65cf38ee |
| openai-image-gen | 366917d32d45 |
| openai-whisper | 6211652a4ffe |
| openai-whisper-api | c0ad50049a94 |
| pytorch-lightning | b747b79c9fae |
| scikit-learn | b7de6b8d95a2 |
| statsmodels | da2754e05c3d |
| subagent-driven-development | 994b89a5d294 |
| baidu-image-crawler | 7647a95fcb23 |
| daily-review | 7b741abe3586 |
| csv-analyzer | 43cada14c097 |
| d3-viz | 4b1f820cd87c |
| deep-research | 80eb6d22b774 |
| implementing-search-filter | c3c09869c8b7 |
| matplotlib | 0c20cceb3a05 |
| postgres | d6d6b2c5c7ea |
| statistical-analysis | 5134736448ee |
| visualizing-data | 31cda9f7f770 |
| building-forms | e7d715b1ade2 |
| algorand-vulnerability-scanner | 0ad3e96d8d58 |
| audit-prep-assistant | ea7db549ff26 |
| cairo-vulnerability-scanner | e2e64b00cfcf |
| code-maturity-assessor | 10e55d278593 |
| cosmos-vulnerability-scanner | 0af2e52a6fe7 |
| guidelines-advisor | 15b4864cd905 |
| secure-workflow-guide | d4f765622ae6 |
| solana-vulnerability-scanner | 5e9040940ac0 |
| substrate-vulnerability-scanner | 611f69b41974 |
| token-integration-analyzer | f700683abe74 |
| ton-vulnerability-scanner | 3a0fbdedb155 |
| building-tables | 793af3d451a2 |
| canvas | 931e65501872 |
| vercel-composition-patterns | e38e0eaa6093 |
| creating-dashboards | 3c150031250e |
| designing-apis | 8474e7e1d019 |
| designing-layouts | 2bc1d2881961 |
| frontend-design | b81e2ff87ed8 |
| pg-aiguide | cb8720251958 |
| ppt-style-guide | 19a6bb09da34 |
| ui-ux-pro-max | b3ab45c714a7 |
| ux-ui-design-system | 0cb3d288d044 |
| web-design-guidelines | f4647ca866a3 |
| coding-agent | 98e98a9e1a09 |
| deploying-applications | 7b67605c4aed |
| differential-review | 72a1e8dc98d5 |
| finishing-a-development-branch | dd2f82c6dc85 |
| gh-issues | 9cfc67329014 |
| git-commit | aea40e0a50ba |
| git-workflow-ci | 3f66bae5eb77 |
| github-pr-creation | 749416050543 |
| github-pr-merge | 4e36449a8fff |
| github-pr-review | 22f4e7c8559c |
| property-based-testing | a027f88bb17a |
| receiving-code-review | c9382e92b8f3 |
| requesting-code-review | 2da31af22a58 |
| root-cause-tracing | 4c3faa28453c |
| systematic-debugging | 4999cb851360 |
| tdd | 0207117930d8 |
| test-driven-development | 7dee67b4af6b |
| testing-strategies | a3da25e3647d |
| verification-before-completion | ea52d15aabaf |
| writing-dockerfiles | 588e76add5cb |
| writing-github-actions | af94116c2cbc |
| changelog-generator | 819efc88f699 |
| diagram-generator | 96c2c0b24063 |
| markdown-to-epub-converter | fa7ceaf36ab0 |
| writing-plans | 8a9198d4d9ef |
| writing-skills | d83a09d6a1c6 |
| fenglin-workflow | 7667c3bf0514 |
| playwright-skill | f38b55f1a3f1 |
| vercel-react-best-practices | b0bc1161fa20 |
| vercel-react-native-skills | 4012750a5ccb |
| apple-notes | c07631bb6ec0 |
| apple-reminders | 75f6b4bbe4eb |
| aws-agentic-ai | 2ab9bb0da68e |
| aws-cdk-development | 9064f0fa50ca |
| aws-mcp-setup | 2e7273ce47dc |
| aws-cost-operations | 0df831407198 |
| bear-notes | 956251b2fca2 |
| agents-sdk | 6ee0d50d51db |
| durable-objects | 66053a19786c |
| wrangler | b1192e4c6701 |
| discord | 06554ae01f77 |
| firebase-apk-scanner | a1bc78a5e7e7 |
| gog | ceb5ee5bb4f0 |
| google-calendar | afaad2a70228 |
| google-docs | c0379b7f2fcf |
| google-drive | 5db33f7f7af8 |
| google-sheets | 7221e7339f97 |
| himalaya | afef5fe4e398 |
| imsg | 55dd0913fbc1 |
| notion | b1ec55de3a74 |
| obsidian | b184f7079cf2 |
| obsidian-bases | 2b01f105aba1 |
| obsidian-markdown | 50fd2c27aafa |
| managing-secrets | 4325ff6482ac |
| aws-serverless-eda | 3c063487fc3b |
| slack | e3bf25727b1f |
| supabase-postgres-best-practices | 8e5a86aa9299 |
| youtube-transcript | 61c6ff19145b |
| 1password | 9e9dace009cf |
| blogwatcher | 33011ee6e0c3 |
| blucli | 38b96f404b6b |
| bluebubbles | 41a9218692a5 |
| ceo-advisor | 9d3f78c6a917 |
| cto-advisor | 4d093a539810 |
| camsnap | 95039a10f662 |
| clawhub | 2bae787841a3 |
| eightctl | a67bcb20983a |
| aws-solution-architect | bae0f87a960a |
| code-reviewer | 428a98e3e3bd |
| ms365-tenant-manager | 2c76be07748f |
| senior-architect | 469ef270e575 |
| senior-backend | b1171953e867 |
| senior-computer-vision | 6fb196ef397a |
| senior-data-engineer | 15dca8b0e5db |
| senior-data-scientist | 1e6974ea6045 |
| senior-devops | 478aed6e0ef1 |
| senior-frontend | 328da03c2918 |
| senior-fullstack | 49626b216897 |
| senior-ml-engineer | 9d37b6e6396c |
| senior-prompt-engineer | ce3f1ef8aa55 |
| senior-qa | 186539ec7a1e |
| senior-secops | 4003717f9491 |
| senior-security | 7c94d61bce07 |
| tdd-guide | 313ee021abe3 |
| tech-stack-evaluator | e87d3b2bfb27 |
| gifgrep | 3445f7eeed1b |
| goplaces | 36b7cca469b1 |
| healthcheck | adba53ffae39 |
| json-canvas | 174481b16ead |
| app-store-optimization | ed379bddb746 |
| content-creator | 61ad2cd9ef57 |
| marketing-demand-acquisition | 864582e41d7a |
| marketing-strategy-pmm | b456c659e355 |
| social-media-analyzer | e3d4cfd78c2b |
| message-sending | dcde1eb9b5a2 |
| model-usage | dbdd6e603d27 |
| nano-banana-pro | 9ab0838e959f |
| openhue | dae9ba55a656 |
| ordercli | 14aef89ed6da |
| peekaboo | bb06f126ec6c |
| remotion-video | ae3125ca9c9d |
| sag | d86b98ff6586 |
| sherpa-onnx-tts | 4ed0e7d7b771 |
| smart-search | e812c54f930d |
| sonoscli | 0deb7507b233 |
| summarize | 3ec4a66d4f27 |
| using-git-worktrees | de9dcde34840 |
| using-superpowers | 07d73726944e |
| web-asset-generator | 97c0a124fb5a |
| brainstorming | 7a238df1ebf0 |
| executing-plans | d099fa42fd75 |
| planning-with-files | d99ce66ad9af |
| agile-product-owner | 9ad964fe904d |
| product-manager-toolkit | 55940b94f44a |
| product-strategist | 6fb94d13b7d1 |
| ui-design-system | 0ce37ac6b0f8 |
| ux-researcher-designer | 43a66a9c1683 |
| programming-workflow | 4b6102775d9c |
| project-standards | c67a82a2333b |
| constant-time-analysis | 0ffc2357999f |
| insecure-defaults | 6c6c54ab48dc |
| semgrep-rule-creator | 1ae1e8d030f5 |
| designer | e704f5e4d518 |
| context-fundamentals | 4f6b700280f6 |
| creating-skills | 9e5a4a38f3e7 |
| knowledge-base-maintenance | 12524b1b6738 |
| manus | 7f6a151b402e |
| mcp-builder | 0f4592dcb53c |
| mcporter | f4e9e568b20c |
| memory-systems | 96fa481e734a |
| notebooklm | 8fcfe1aa0452 |
| oracle | 903a053178a3 |
| session-logs | 7c6a007031f8 |
| skill-creator | b2e3d83f6042 |
| skill-seekers | 1c08899e7c8c |
| skills-updater | 45a95902917d |
| tool-design | 0f1d9661a832 |
