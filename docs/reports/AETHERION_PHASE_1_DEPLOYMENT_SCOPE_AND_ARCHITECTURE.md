# AETHERION AI — PHASE 1 DEPLOYMENT SCOPE & ARCHITECTURE LOCK

**Phase:** 1 of 10 — decision and architecture only  
**Audit/execution window:** 2026-10-12 to 2026-10-13  
**Observed execution time:** 2026-10-12 23:12 IST (UTC+05:30)  
**Target deployment date:** 2026-10-19  
**Primary workspace:** `C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli`  
**Hermes reference:** `C:\Users\mdham\Downloads\Aetherion-AI-main\Aetherion-AI-main\reference only\hermes-agent-main\hermes-agent-main`  
**Document status:** Phase 1 decision record; no implementation or deployment performed

> **Security note:** Credential values were not read, printed, copied, or included. The ignored `.env` file exists in the workspace. Earlier audit evidence reported credential-shaped OAuth values there; that report is treated as historical evidence requiring rotation, not as permission to expose or reuse any value.

---

## 1. Executive decision summary

### Phase 1 status: **BLOCKED**

The repository contains enough architecture to define a controlled single-host topology, but the first-release scope cannot be approved yet. Essential human and external decisions remain `UNDECIDED`, and the current source does not provide a safe verified path for public untrusted agent execution.

### Recommended first-release scope: **Option B — Control-plane release**

**Approval status:** `NOT APPROVED / UNDECIDED`  
This is an engineering recommendation only. No user or product-owner approval for either launch option was observed in the workspace.

The recommended initial release should expose only capabilities that can be made and verified as control-plane functionality:

- authentication, device authorization, session management and account recovery;
- tenant-scoped task records, status, review metadata and audit/replay records;
- D3 management features whose persistence and authorization are verified;
- repository metadata and worktree records only where the operation is explicitly allowed;
- health, readiness and operator diagnostics;
- no public execution of untrusted commands, plugins, code or autonomous model-driven file mutations.

The control-plane recommendation is not currently deployable as-is. The repository exposes task execution routes and does not contain an observed global execution-disabled policy. Before Phase 2 can close this scope, implementation must make execution endpoints reject or safely quarantine execution requests, while preserving task-record functionality and returning a truthful, structured limitation response.

### Why Option A is not recommended now

Option A — agent-execution release — requires all of the following to be current and verified:

1. an approved isolation backend and sandbox image/runtime;
2. proof that the real API-to-runtime path uses the approved runner for every command/untrusted workload;
3. fail-closed behavior when that runner is unavailable;
4. negative tests for unavailable Docker/runner, path escape, forbidden command, timeout, cancellation and cross-tenant access;
5. a first-party model provider, with a realistic task, timeout behavior and provider-error evidence;
6. immutable source/image provenance and target-environment evidence.

Current source shows these as components and contracts, not as a verified integrated production capability. Therefore Option A is **BLOCKED**, not merely deferred.

### Immediate architecture decision record

| Decision | Current value | Evidence/status |
|---|---|---|
| First-release scope | Control-plane recommendation | **INFERENCE / NOT APPROVED** |
| Public autonomous execution | Disabled/rejected for recommended scope | **REQUIRED IMPLEMENTATION DECISION; not currently enforced** |
| Deployment shape | Controlled single-host Compose, if gates close | **RECOMMENDATION; infrastructure UNDECIDED** |
| Public hostname/domain | `UNDECIDED` | No current ownership or DNS evidence |
| Hosting provider/host/region | `UNDECIDED` | No current provisioning evidence |
| Private image registry | `UNDECIDED` | No registry URL/access evidence in deployment files |
| First-party LLM provider | `UNDECIDED` for Option B; required for Option A | Current Compose defaults to `LLM_PROVIDER=mock` |
| Sandbox backend | `UNDECIDED`; not needed if execution is rejected | `WorkspaceBoundary` can degrade to host process; approved runner path not proven integrated |
| OAuth scope | `UNDECIDED` | Exact production callback/domain is not known |
| Operational owner/incident contact | `UNDECIDED` | No current owner recorded |
| User approval to proceed to Phase 2 | Not observed | **BLOCKER** |

---

## 2. Evidence vocabulary and audit boundary

| Label | Use in this record |
|---|---|
| **VERIFIED** | Directly observed in the current workspace or command output during Phase 1. |
| **HISTORICAL** | Taken from an earlier audit/report and not re-confirmed in this phase. |
| **INFERENCE** | Reasoned architectural conclusion from verified evidence; not runtime proof. |
| **UNDECIDED** | Requires a human, product, security, platform or external-provider decision. |
| **BLOCKER** | Prevents the selected scope from being safely approved or prevents Phase 2 from starting. |

### Actions performed

- Confirmed the workspace path, date, branch, HEAD commit, Git status, tracked/untracked counts and report/artifact inventory.
- Confirmed the requested Phase 1 output path did not exist before this document was created.
- Inspected the task API, runtime, sandbox boundary, runner contract, startup/preflight code, D3 persistence/migrations, migration CLI, Compose, environment templates, nginx configuration, Dockerfiles, release packager and deployment checklist.
- Inspected the prior one-week master audit and final release-readiness audit as historical navigation/evidence.
- Did not read `.env` values.
- Did not start or stop Docker, services, containers, databases, queues or networks.
- Did not run tests, builds, migrations, backups, image inspection, provider calls or deployment commands.
- Did not edit the Hermes reference tree.

---

## 3. Current starting state

### 3.1 Workspace and Git baseline — VERIFIED

| Item | Observed value |
|---|---|
| Project root | `C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli` |
| Branch | `main` |
| HEAD | `5c7bdcd4808a178aa677488f18b948a747f94193` — `initial commit for worktree support` |
| Tracked files | 1; `backend/routes/d3_features.py` |
| Standard untracked files | 2,218 |
| Git status entries | 129 |
| Existing source modification | `backend/routes/d3_features.py`, `222 insertions / 482 deletions` |
| Release tags | None observed |
| Phase 1 output path before work | Absent |
| Prior Phase 0 report | `AETHERION_ONE_WEEK_DEPLOYMENT_READINESS_MASTER_AUDIT.md`, already untracked before this phase |

The existing `backend/routes/d3_features.py` modification was preserved. No reset, revert, clean, stage, commit, format or source edit was performed. The pre-existing `deploy/compose/docker-compose.prod.yml` was preserved.

The practical release consequence is material: most release source files are untracked, so a future image cannot be tied to an immutable reviewable Git commit until a later approved provenance operation records the complete source baseline. This Phase 1 document does not perform that operation.

### 3.2 Protected and relevant file hashes — VERIFIED

| File | SHA-256 observed | Interpretation |
|---|---|---|
| `backend/routes/d3_features.py` | `15f81778d776f8084ffcc41917336efdc5dda30de12c81545e19c7d4cb4b64a5` | Existing protected user change preserved |
| `deploy/compose/docker-compose.prod.yml` | `73c0367c2cf90efdd7e02f559225c231e5337aa277409f72ed2872c659c2c349` | Existing deployment artifact preserved |
| `backend/execution/sandbox/boundary.py` | `43e6f376592277c3d7a6857b7995107b64a0cf996bfb3ee9382ca0f9858835b8` | Current source inspected |
| `backend/routes/tasks.py` | `886410f5ae6a87f7b7ec1b557c1a1adbc23076322efc4217ea37d13f4084dda1` | Current source inspected |

### 3.3 Runtime and release evidence boundaries

- Docker client/runtime availability was not reactivated or changed. The earlier Phase 0 audit recorded Docker Desktop Linux engine unavailability; no current candidate-image inspection was performed in this phase.
- `dist/` contains the 2026-10-10 source ZIPs and manifests. The source packager is deterministic by design, but its `RELEASE_LABEL`, `RELEASE_VERSION` and output names are fixed to the 2026-10-10 release family. No current R3 source package or Git tag was observed.
- The earlier release reports are **HISTORICAL**. Their image IDs, container health, migrations, tests and browser UAT are not current Phase 1 runtime verification.

---

## 4. Evidence-based current architecture assessment

### 4.1 Application/control plane — VERIFIED source architecture

`backend/app.py:create_app()` registers health, core auth, device auth, OAuth, recovery, workspace, tasks, artifacts, diff, replay, platform, phase4 and D3 blueprints. Startup imports `auth_system.init_db()` and invokes `core.startup_validator.validate_startup()` before creating the application instance.

The API has a meaningful control plane:

- `backend/routes/auth_core.py` provides signup, login, refresh, logout and session behavior.
- `backend/routes/auth_device.py` provides device authorization flow support.
- `backend/routes/auth_oauth.py` provides Google/GitHub callback paths with in-process state tracking and provider timeouts.
- `backend/routes/tasks.py` creates tenant-associated durable task records, exposes status/log/review/accept/reject paths, and exposes execution endpoints.
- `backend/routes/d3_features.py` exposes D3 feature APIs and is a protected pre-existing user modification.

**Assessment:** The control-plane architecture exists in source. Public HTTPS behavior, production auth configuration, cross-tenant runtime isolation and target persistence remain **NOT VERIFIED**.

### 4.2 Real API-to-runtime execution path — VERIFIED source; unsafe for Option A until changed

The current path is:

```text
POST /api/tasks
  -> create_task() in backend/routes/tasks.py
  -> durable_state.create(...)
  -> optional background thread run_task_pipeline(...)

POST /api/task/<id>/execute or /run
  -> execute_task()
  -> run_task_pipeline()
  -> TaskExecutionContext
  -> attach_execution_worktree()
  -> RealAgentRuntime(context=context, provider=prov)
  -> WorkspaceBoundary + AgentToolRegistry + provider
```

Concrete evidence:

- `backend/routes/tasks.py:91-100` starts `run_task_pipeline` when `auto_run`/`run` is requested.
- `backend/routes/tasks.py:580-639` constructs `TaskExecutionContext`, attaches a worktree, transitions the record to running and constructs `RealAgentRuntime`.
- `backend/execution/agent_runtime/runtime.py:215-218` constructs `WorkspaceBoundary`, `AgentToolRegistry` and `VerificationEngine`.
- `backend/execution/sandbox/boundary.py:178-199` checks Docker availability but reports `host_process_degraded` when Docker is absent unless `AETHERION_REQUIRE_DOCKER=1`.
- `backend/execution/sandbox/boundary.py:239-273` executes commands through `subprocess.Popen(shell=True)` within the workspace boundary.
- `backend/execution/runtime_runners.py:128-134` separately launches `agent_state_machine.py` or `main.py` using `subprocess.Popen`.
- `backend/execution/runner_contract.py:543-594` has a separate fail-closed factory that denies local/mock runners in production and requires approved backends, but current source inspection does not establish that `run_task_pipeline` and all API execution paths call `get_runner()`.

**Decision impact:** Option A is **BLOCKED** until the API-to-runtime path is proven to use one approved runner boundary or execution is explicitly disabled. The existence of `runner_contract.py` alone is not sufficient.

### 4.3 Startup and preflight — VERIFIED source; policy conflict remains

`backend/core/startup_validator.py:142-184` calls the canonical preflight report and exits in fail-closed environments when required checks fail. `backend/core/preflight.py` checks environment identity, JWT/application secrets, secret independence, development-auth bypass, public HTTPS `SERVER_URL`, PostgreSQL configuration/reachability, Redis, providers, workspace and sandbox conditions.

The policy conflict is explicit:

- `startup_validator.py:15-17` documents Docker unavailability as a production failure **with an override** `REQUIRE_SANDBOX=false`.
- `deploy/compose/docker-compose.prod.yml:84` sets `REQUIRE_SANDBOX: ${REQUIRE_SANDBOX:-false}`.
- `deploy/compose/.env.example:12` sets `REQUIRE_SANDBOX=false`.
- `deploy/production.env.example:45` recommends `REQUIRE_SANDBOX=true`.

**Assessment:** The source has a fail-closed mechanism, but the shipped production defaults permit degraded execution. This is **VERIFIED configuration risk**, not a claim that a live production container currently behaves incorrectly.

### 4.4 Persistence, D3 and migrations — VERIFIED source; target unavailable

- `backend/db/d3_migrations.py:D3_MIGRATIONS` contains a tracked D3 schema migration for six D3 tables plus `d3_schema_migrations` history.
- `verify_d3_schema_compatibility()` rejects a database newer than the application, checks migration checksums and reports missing tables/pending migrations.
- `apply_d3_migrations()` applies migrations transactionally and can re-ensure missing required tables.
- `backend/db/d3_store.py:D3Store` uses tenant-scoped query methods for automations, runs, KG data and worktrees. `create_real_worktree()` creates a physical Git worktree and records metadata; this operation is inherently filesystem-mutating and must not be offered in a control-plane-only public scope unless explicitly governed.
- `scripts/db_migrate.py` requires PostgreSQL in fail-closed environments, checks core/fabric/D3 tables, applies core/fabric/D3 migrations, and has rollback-check support.

**Assessment:** Persistence architecture is suitable for the proposed control plane. No target database, migration result, backup, restore or rollback evidence is current in Phase 1.

### 4.5 Container and network topology — VERIFIED template architecture, not provisioned infrastructure

`deploy/compose/docker-compose.prod.yml` defines:

- PostgreSQL 16 with a private bridge network and loopback host binding;
- Redis 7 with append-only persistence and loopback host binding;
- a one-shot `db-migrate` service before the backend;
- a backend API with health check and private network;
- frontend-auth with nginx health check and private network;
- two workers with backend-health dependency and shared workspace volume.

The Dockerfiles use non-root runtime users:

- `Dockerfile.backend`: UID/GID 10001, Gunicorn, health check, backend/scripts copied into image;
- `Dockerfile.worker`: UID/GID 10001, worker preflight health check and `worker_entrypoint`;
- `Dockerfile.frontend-auth`: unprivileged nginx UID 101, SPA build and internal API proxy.

`deploy/nginx/nginx.conf` listens internally on port 8080, proxies `/api/` to `backend:5000`, serves the SPA and exposes `/healthz`. It does not terminate public TLS. Therefore an external HTTPS terminator or managed ingress remains required and undecided.

**Assessment:** Compose is the preferred starting topology for one host, but it is a template. No host, registry, DNS, TLS or service state is established by these files.

### 4.6 Source packaging and provenance — VERIFIED source; not release-locked

`scripts/build_source_release.py` provides deterministic timestamps, sorting, normalized permissions, exclusions for `.env`, databases, caches, scratch/audit directories and Hermes, and per-file SHA-256 manifest entries. It writes to `dist/` by default, so a future packaging operation would mutate existing release artifacts and must be separately approved.

The current Git state does not supply the missing immutable source identity. Phase 2 must use an approved source-baseline operation that preserves the D3 change, records the complete release input set and maps it to image digests. Phase 1 does not perform that operation.

---

## 5. Launch-scope comparison

| Dimension | Option A — Agent-execution release | Option B — Control-plane release |
|---|---|---|
| User promise | Supported autonomous tasks execute commands/workloads | Authenticated management and durable task/control-plane functions only |
| Required isolation | Approved runner integrated into actual API path; no host fallback | Public execution rejected/disabled; no untrusted workload accepted |
| Model provider | One first-party provider and realistic task required | No mock provider represented as autonomous capability; provider may be deferred |
| Current evidence | **BLOCKED**; source contracts exist but integration/runtime proof is absent | **Recommended fallback**, but execution-disable policy is not yet implemented or approved |
| Security boundary | Must be enforced before public traffic | Must be enforced by rejection/feature policy before public traffic |
| Required Phase 2 work | Runner integration, fail-closed path, negative tests, provider validation | Execution-disabled policy, truthful API responses, scope-specific smoke tests |
| Deployment feasibility by 19 Oct | Not defensible without immediate external and engineering closure | More feasible, but still blocked by target, provenance, secrets, HTTPS, migration and acceptance evidence |

### Recommendation

Recommend **Option B — Control-plane release** as the default safe scope. It minimizes the trust boundary and does not pretend that `LLM_PROVIDER=mock` is functioning AI. However, this recommendation is not an approval and does not mean Option B is currently ready. A product/security owner must approve the scope, and Phase 2 must implement and verify rejection of public execution paths.

Do not expose `/api/task/<id>/execute`, `/run`, `/api/v1/agent/invoke`, or equivalent execution-capable paths to public users until the selected execution policy is enforced. Merely omitting a UI button is not sufficient.

---

## 6. Minimum viable deployment architecture

### 6.1 Proposed topology

```text
Public DNS (UNDECIDED)
        |
Public HTTPS terminator / managed ingress (UNDECIDED)
        |
127.0.0.1:3000 -> frontend-auth nginx (SPA + /api proxy)
        |
private Compose bridge network
        |
backend API :5000 ---- private PostgreSQL :5432
        |                private Redis :6379
        |
workers A/B ------------ shared durable state and coordination
        |
private registry pulls by immutable digest (UNDECIDED)
```

### 6.2 Responsibility and trust-boundary table

| Component | Responsibility | Trust boundary / required condition |
|---|---|---|
| Public DNS/TLS | Resolve the selected public host and terminate HTTPS | Provider, hostname, DNS ownership and certificate method are `UNDECIDED`; must forward only intended traffic |
| Frontend nginx | Serve auth SPA, proxy `/api/`, add browser-facing security headers | Should be reachable only through HTTPS ingress; current nginx config is internal HTTP on 8080 |
| Backend API | Auth, tenant-scoped control plane, health, D3, task records and approved execution policy | Must receive production secrets through protected injection; must reject Option B execution requests |
| PostgreSQL | Durable users, sessions, tasks, fabric and D3 state | Private network only; migration service runs before backend; backup/restore owner and destination `UNDECIDED` |
| Redis | Coordination, revocation/event/queue support | Private network only; persistence and recovery evidence required |
| `db-migrate` | Apply/check schema before backend readiness | Must run from immutable backend image against intended target DB; no current target exists |
| Workers | Register, heartbeat, claim and process approved work | For Option B, workers must not accept untrusted execution; for Option A, approved runner is mandatory |
| Registry | Store backend/worker/frontend images by immutable digest | Registry owner/access `UNDECIDED`; local tags are not sufficient |
| Secret injection | Provide independent application/JWT/DB/provider/OAuth values | Secret manager/protected file and rotation owner `UNDECIDED`; values must never be in Git/images/logs |
| Logging/metrics | Health, readiness, worker and security diagnostics | External retention, alerting and incident contact `UNDECIDED`; redact secrets |
| Backup/restore | Protect PostgreSQL/Redis/state and verify restoration | Backup destination and restoration operator `UNDECIDED`; target drill required |
| Rollback | Repoint to prior image digest and preserve data compatibility | Requires source/image manifest and migration compatibility record; no current release lock |

### 6.3 Non-negotiable network rules

- PostgreSQL and Redis must not be publicly reachable.
- Only the intended HTTPS ingress may be public.
- Backend/frontend loopback bindings are not public reachability by themselves; an external terminator is required.
- Registry credentials, provider credentials and OAuth secrets must not be embedded in Compose, Dockerfiles, source or client JavaScript.
- A control-plane deployment must not accept commands, plugins, file mutations or model-driven task execution through an alternate endpoint.

---

## 7. External dependency and ownership register

No external values are invented. `Owner/role` is an accountable role, not a claim that the person/team is assigned.

| Dependency/decision | Current evidence | Owner/role | Blocks selected scope? | Required decision or action | Closure evidence |
|---|---|---|---|---|---|
| Hosting provider, host, region | No current host/cloud evidence; only Compose/Kubernetes templates | Platform/DevOps | Yes | Select the single host and operating owner | Written target record, access owner, firewall plan |
| Public domain/DNS | Templates contain example hostnames; no ownership/current DNS proof | Infrastructure/domain owner | Yes for public release | Confirm domain, records and DNS operator | DNS resolution from an external vantage point |
| TLS termination | nginx template listens HTTP 8080; no public TLS service identified | Platform/edge owner | Yes for public release | Select managed ingress, reverse proxy or certificate service | Valid certificate and HTTPS smoke with exact host |
| Image registry | No registry URL or push workflow in `deploy/`/`scripts/` | Platform/CI owner | Yes | Select private registry and immutable-digest policy | Authenticated push/pull by digest from clean host |
| Complete source baseline | One tracked file, 2,218 untracked files, no tag | Engineering/release owner | Yes | Establish reviewed immutable source commit/tag without losing D3 change | Clean status, commit ID, manifest and source archive |
| Secret store/injection | Templates exist; `.env` is ignored; earlier audit reported credential-shaped values | Security/DevOps | Yes | Rotate affected credentials and select protected injection method | Rotation record; config check with names only; no secrets in artifacts |
| PostgreSQL | Migration code and Compose service exist; target not identified | DBA/platform | Yes | Choose private target, persistence, owner and backup policy | `db_migrate.py --status/--check`, schema/migration transcript |
| Redis | Compose service exists; target/recovery owner not identified | Platform | Yes | Choose private target and persistence/recovery policy | Connectivity/readiness and recovery evidence |
| LLM provider | Provider adapters exist; Compose defaults `LLM_PROVIDER=mock`; no current live inference | Product/AI/platform | Option A: yes; Option B: no, but mock must not claim AI | Select first-party provider and endpoint only if execution scope approved | Realistic task, timeout/error test, secret redaction |
| OAuth registrations | Routes support Google/GitHub; exact production domain unknown | Auth/product owner | If OAuth included | Decide OAuth scope and register exact callback URLs | Provider console registration and browser callback smoke |
| Approved sandbox | `runner_contract.py` has approved adapters; API path uses `WorkspaceBoundary`; availability not proven | Security/platform/engineering | Option A: yes; Option B: execution must be rejected | Approve hardened container/gVisor/Kata/microVM or disable execution | Negative unavailable-runner test and positive isolated task test |
| Monitoring/alerting | In-process health/metrics exist; external alerting absent from evidence | SRE/operator | Yes for controlled public release | Choose monitoring, escalation and incident contact | Alert delivery, dashboard/probe and runbook |
| Backup destination/restore owner | Scripts exist; no target drill/destination | DBA/platform | Yes | Select destination, retention and restoration operator | Backup checksum and disposable restore transcript |
| Product/security launch approval | No scope approval observed | Product/security owner | Yes | Approve Option B or Option A and known limitations | Recorded sign-off tied to this document |

---

## 8. P0 deployment blockers and measurable closure criteria

| ID | Blocker | Evidence label | Required closure criterion |
|---|---|---|---|
| P0-1 | Launch scope and owner approval absent | **UNDECIDED / BLOCKER** | Product/security owner records Option A or Option B, execution policy, OAuth scope and known limitations. |
| P0-2 | No host/region/operator/domain/TLS target | **UNDECIDED / BLOCKER** | Target record names host, region, operator, DNS owner and TLS method; no placeholders remain in the release record. |
| P0-3 | Source cannot be tied to a complete immutable baseline | **VERIFIED / BLOCKER** | Approved commit/tag includes intended source; `git status` is clean for release inputs; archive manifest maps to commit. |
| P0-4 | Candidate images are not current verified registry artifacts | **HISTORICAL / BLOCKER** | Build from the immutable baseline, inspect image digests, push to private registry, and clean-host pull by digest. |
| P0-5 | Option A isolation path is not integrated/proven | **VERIFIED source risk / BLOCKER** | API execution path calls the approved runner; unavailable runner returns structured failure; no host subprocess; negative and positive isolation tests recorded. |
| P0-6 | Option B execution-disable policy is not implemented | **VERIFIED source gap / BLOCKER** | Execution-capable endpoints return a documented non-success policy response; no background auto-run, invoke, worker or alternate path can execute untrusted work. |
| P0-7 | Provider scope is unresolved and Compose defaults mock | **VERIFIED + UNDECIDED / BLOCKER** | Option A has first-party provider and realistic task evidence; Option B has no claim of autonomous AI and no execution route accepting mock work. |
| P0-8 | Target DB/Redis and migration evidence absent | **UNDECIDED / BLOCKER** | Private targets provisioned; migration `--status`/`--check` is clean; workers/backend readiness and connection failure behavior recorded. |
| P0-9 | Secret rotation/injection arrangement absent | **HISTORICAL risk / BLOCKER** | Affected credentials rotated; independent secrets injected through selected protected path; secret scan/log review contains no values. |
| P0-10 | Public HTTPS/auth/callback evidence absent | **NOT VERIFIED / BLOCKER** | Exact public HTTPS endpoints resolve; login/device flow and configured OAuth scope work; origins/callbacks match exactly. |
| P0-11 | Backup/restore and rollback not evidenced | **NOT VERIFIED / BLOCKER** | Target backup checksum, disposable restore, image rollback and schema compatibility evidence are recorded. |
| P0-12 | Monitoring and incident ownership absent | **UNDECIDED / BLOCKER** | Named operator receives health/alert signal and can execute the runbook. |

---

## 9. Explicit unresolved decisions

The following must remain literal `UNDECIDED` until an authorized owner resolves them:

1. Option B control-plane release approval versus Option A agent-execution approval.
2. Whether OAuth login is in first-release scope.
3. Hosting provider, host, region and operational owner.
4. Public domain, DNS operator and TLS termination method.
5. Private registry owner, namespace and pull-auth mechanism.
6. Secret manager/protected-file mechanism and credential rotation owner.
7. PostgreSQL and Redis hosting/persistence/backup ownership.
8. First-party LLM provider, endpoint and approved model, if Option A remains desired.
9. Approved sandbox backend and runtime class, if Option A remains desired.
10. Monitoring/alerting system and incident contact.
11. Backup destination, retention and restoration operator.
12. Product/security acceptance of the control-plane known limitations.

No value above is inferred from example hostnames such as `api.aetherion.ai`, `app.aetherion.ai` or `yourdomain.com`.

---

## 10. Phase 1 release acceptance checklist

Phase 1 decision work is complete only when the following are recorded by authorized owners. This Phase 1 execution did not claim any unchecked item as complete.

### Scope and ownership

- [ ] Launch scope explicitly approved: Option A or Option B.
- [ ] Public/private launch boundary approved.
- [ ] Product owner and security owner recorded.
- [ ] Platform/DBA/operator/incident roles recorded.
- [ ] OAuth and provider scope recorded.

### Architecture and security

- [ ] Compose single-host topology accepted as the starting architecture.
- [ ] PostgreSQL and Redis are private by design and their target ownership is recorded.
- [ ] Only the intended HTTPS ingress is public.
- [ ] Option B execution rejection or Option A runner integration is selected.
- [ ] No mock provider is represented as autonomous AI capability.
- [ ] Credential rotation and protected secret injection are approved.

### Provenance and operations

- [ ] Complete source baseline and release manifest strategy accepted.
- [ ] Registry and immutable digest strategy accepted.
- [ ] Migration, backup, restore and rollback owners assigned.
- [ ] Monitoring, alerting and incident process assigned.

### Evidence required before Phase 2 exit

- [ ] Current Git/source baseline record.
- [ ] Current image inspect and registry pull transcript.
- [ ] Target preflight and migration transcript.
- [ ] Scope-specific execution-policy tests.
- [ ] HTTPS/auth smoke evidence.
- [ ] Backup/restore and rollback evidence.

---

## 11. Dependency map for Phase 2

```text
Human scope approval (Option B or A)
  |
  +--> Host/domain/TLS/registry/secret ownership decisions
  |       |
  |       +--> immutable source baseline and release manifest
  |       |       |
  |       |       +--> digest-addressed image build/publish
  |       |
  |       +--> target PostgreSQL/Redis and backup ownership
  |               |
  |               +--> migration/preflight/readiness evidence
  |
  +--> Option B execution-disabled policy
  |       |
  |       +--> endpoint/worker/auto-run negative tests
  |
  +--> Option A sandbox + provider decisions (only if selected)
          |
          +--> actual API-to-runtime runner integration
          +--> unavailable-runner fail-closed test
          +--> isolated realistic provider task

All branches
  |
  +--> public HTTPS/auth/origin/callback verification
  +--> backup/restore/rollback verification
  +--> monitoring and incident acceptance
  +--> Phase 2 implementation authorization
```

### Phase 2 cannot safely begin until

1. The launch scope is approved.
2. The host/domain/registry/secret ownership decisions are made or the project formally accepts a private non-production track.
3. The security owner accepts either the Option B execution-disabled boundary or the Option A sandbox requirements.
4. The protected D3 change and all release inputs have a documented custody plan.
5. The external dependency register has owners and target dates.

Phase 2 is not authorized by this document. It requires the user/product/security decision recorded above.

---

## 12. Phase 1 completion verdict

### Verdict: **BLOCKED — DECISION RECORD COMPLETE, IMPLEMENTATION AUTHORIZATION ABSENT**

The Phase 1 architecture decision document is complete and evidence-backed. The recommended path is a control-plane release with public execution disabled or rejected until the isolation and provider requirements are proven. The current source and templates are sufficient to plan a single-host Compose topology, but they do not prove that infrastructure exists or that the selected scope is deployable.

### Conditions to proceed

Phase 2 may begin only after the user/product/security owners explicitly approve the launch scope and assign the external dependencies listed in §7. The first Phase 2 implementation should then address the approved boundary:

- **If Option B is approved:** implement and test an execution-disabled policy across all task execution entry points, including auto-run, execute/run routes, agent invoke/stream paths, background threads and worker acceptance.
- **If Option A is approved:** integrate `runner_contract.get_runner()` or an equivalent approved boundary into the actual API-to-runtime path, remove host fallback in fail-closed environments, and verify a first-party provider and isolated task.

No Phase 2 work was started in this run.

---

## 13. Evidence separation summary

### VERIFIED in Phase 1

- Actual workspace, branch, HEAD, Git status, tracked/untracked counts and protected-file hashes.
- Requested Phase 1 output path was absent before creation.
- Flask blueprint registration and startup/preflight call sites.
- API task creation/execution routes and `run_task_pipeline` to `RealAgentRuntime` path.
- `WorkspaceBoundary` host-degraded behavior and subprocess command path.
- Separate fail-closed `runner_contract` factory and its approved backend rules.
- D3 migration/store source and migration CLI behavior.
- Production Compose topology, private service bindings, image-variable gates, sandbox/mock defaults.
- Dockerfile non-root users and healthcheck/worker entrypoints.
- Deterministic source-packager implementation and its output-mutation implications.

### HISTORICAL or not re-confirmed

- Candidate image IDs and container health.
- Prior runtime migration/test/UAT results.
- Public HTTPS, provider inference, target DB, backup/restore, registry and staging service state.
- Prior secret-scan and packaging results beyond current files/manifests.

### INFERENCE

- Option B is the smallest safer first-release scope.
- Compose is the shortest intended topology for a single host once external decisions close.
- Hermes feature parity is not a prerequisite for the first Aetherion control-plane release.

### UNDECIDED/BLOCKER

- Every external owner and value listed in §7 and §9.
- User/product/security approval of Option B.
- Whether 19 October remains feasible after those decisions.
