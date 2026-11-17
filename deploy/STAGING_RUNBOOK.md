# AETHERION STAGING CONTROL-PLANE RUNBOOK
## Authoritative staging guide for the approved Option A scope

This runbook governs deployment, verification, and promotion criteria for the
approved Option A control-plane scope in a dedicated staging environment.
Autonomous agent, command, plugin, untrusted-worker, physical worktree, and
process execution remain disabled. Execution-specific checks below are future
approval gates, not evidence that staging execution is available.

---

## 1. Authoritative Build and Packaging Artifacts

| Component | Path / Context | Dockerfile / Artifact |
|---|---|---|
| **Root Workspace** | `end to end aetherion cli/` | Repository root |
| **Backend API** | `deploy/docker/Dockerfile.backend` | Image context: root (`.`) |
| **Frontend Auth UI** | `deploy/docker/Dockerfile.frontend-auth` | Image context: root (`.`) |
| **Worker Fleet** | `deploy/docker/Dockerfile.worker` | Image context: root (`.`) |
| **Sandbox Runner** | `deploy/docker/Dockerfile.worker` | Tag: `aetherion-sandbox-runner:latest` |
| **Operator CLI** | `cli/` | Python wheel / executable: `aetherion` |

---

## 2. Container Build and Assembly

Execute from the authoritative workspace root:

```bash
# 1. Build the full production/staging container stack
docker compose -f deploy/compose/docker-compose.prod.yml --env-file deploy/staging.env build

# 2. Build the dedicated sandbox container for untrusted plugins
docker build -t aetherion-sandbox-runner:latest -f deploy/docker/Dockerfile.worker .
```

---

## 3. Staging Environment Configuration (`deploy/staging.env`)

Never commit populated `.env` files. Provision secrets into `deploy/staging.env` via a secret manager or encrypted CI/CD pipeline:

```bash
# Generate high-entropy independent cryptographic secrets
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
export JWT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
export POSTGRES_PASSWORD=$(python -c "import secrets; print(secrets.token_hex(24))")
```

Required variables for `deploy/staging.env`:
```ini
ENVIRONMENT=staging
AETHERION_ENV=staging
PORT=5000
SERVER_URL=https://staging-api.aetherion.ai
FRONTEND_URL=https://staging-app.aetherion.ai
ALLOWED_ORIGINS=https://staging-app.aetherion.ai

# Security & Secrets (Must be distinct 64-char hex)
SECRET_KEY=<GENERATED_64_CHAR_HEX>
JWT_SECRET=<GENERATED_64_CHAR_HEX>

# Managed Databases
DATABASE_URL=postgresql://aetherion_user:<GENERATED_PASSWORD>@postgres:5432/aetherion
REDIS_URL=redis://redis:6379/0

> [!NOTE]
> **Database Role Credential Note:** Updating `POSTGRES_PASSWORD` in `.env` only configures credentials for newly initialized database clusters. If rotating credentials on an already-initialized PostgreSQL persistent volume (`postgres_data`), an operator must also issue `ALTER ROLE aetherion_user WITH PASSWORD 'new_password';` within the PostgreSQL database cluster.

# Current release scope
AETHERION_RELEASE_SCOPE=control-plane
AETHERION_EXECUTION_DISABLED=1
# A future execution-enabled release must define sandbox settings separately
# after trusted-supervisor and governance approval.

# OAuth Credentials (Staging Application Redirects)
GOOGLE_CLIENT_ID=<STAGING_GOOGLE_CLIENT_ID>
GOOGLE_CLIENT_SECRET=<STAGING_GOOGLE_CLIENT_SECRET>
GITHUB_CLIENT_ID=<STAGING_GITHUB_CLIENT_ID>
GITHUB_CLIENT_SECRET=<STAGING_GITHUB_CLIENT_SECRET>

# Model Provider API Keys
ANTHROPIC_API_KEY=<OPERATOR_SUPPLIED_KEY>
OPENAI_API_KEY=<OPERATOR_SUPPLIED_KEY>
GEMINI_API_KEY=<OPERATOR_SUPPLIED_KEY>
```

---

## 4. PostgreSQL and Redis Readiness Checks

Prior to starting the application backend, verify stateful dependencies:

```bash
# 1. Start core data stores
docker compose -f deploy/compose/docker-compose.prod.yml up -d postgres redis

# 2. Verify PostgreSQL readiness
docker compose -f deploy/compose/docker-compose.prod.yml exec -T postgres pg_isready -U aetherion_user -d aetherion

# 3. Apply and verify database schema migrations
docker compose -f deploy/compose/docker-compose.prod.yml --env-file deploy/staging.env run --rm db-migrate
docker compose -f deploy/compose/docker-compose.prod.yml --env-file deploy/staging.env run --rm --no-deps db-migrate python /app/scripts/db_migrate.py --check --json

# 4. Verify Redis responsiveness
docker compose -f deploy/compose/docker-compose.prod.yml exec -T redis redis-cli ping
# Expected output: PONG
```

---

## 4A. Kubernetes Migration-Gated Release

The Kubernetes path must not apply `backend.yaml` before the migration and
schema-check Jobs succeed. Provision `aetherion-secrets` through the approved
secret mechanism first; `deploy/k8s/secrets.yaml` is a placeholder reference
and is intentionally not applied by the release gate.

Validate the exact release without contacting a cluster:

```bash
python scripts/deploy_k8s.py --plan \
  --release-id <lowercase-release-id> \
  --backend-image <immutable-backend-image> \
  --frontend-image <immutable-frontend-image>
```

The approved release operator then runs the same command without `--plan`.
The script applies namespace/configuration/PostgreSQL resources, verifies the
existing Secret keys without printing values, waits for the PostgreSQL pod,
creates a fresh release-scoped migration Job, waits for completion, runs a
separate `--check --json` Job, and only then applies and waits for the backend
and frontend rollouts. Any failed or timed-out gate stops before workload
rollout and returns a non-zero exit code.

Completed Jobs are retained for inspection. A new release always uses
`kubectl create` with a fresh generated Job name; an older completed Job cannot
satisfy a later release's gate.

This path was not executed in this session. Do not run it against a live
cluster as part of local verification.

---

## 5. Deferred Docker Sandbox Readiness & Fail-Closed Validation

The current control-plane release does not execute untrusted extensions. If a
future execution-enabled release is separately approved, it must require
container isolation and prove that no host fallback exists:

```bash
# 1. Verify Docker daemon responsiveness
docker info
docker ps

# 2. Verify sandbox runner image presence
docker image inspect aetherion-sandbox-runner:latest > /dev/null && echo "Sandbox Image: READY"

# 3. Verify Fail-Closed Enforcement
# When Docker is stopped or the future sandbox contract is unsatisfied,
# untrusted execution MUST fail with SANDBOX_UNAVAILABLE without host fallback.
python -c "from backend.execution.extensions import is_docker_available; print('Sandbox Engine Available:', is_docker_available())"
```

---

## 6. Stack Launch and Health Checks

Launch the full staging fleet:

```bash
# Launch backend, frontend-auth, and workers
docker compose -f deploy/compose/docker-compose.prod.yml --env-file deploy/staging.env up -d

# Verify all services report (healthy)
docker compose -f deploy/compose/docker-compose.prod.yml ps
```

Execute automated HTTP health probes:
```bash
# 1. Backend Control-Plane Health Probe
curl -fsSL http://localhost:5000/api/health/control-plane
# Expected: {"status": "ok", "postgres": "connected", "redis": "connected"}

# 2. Public Health Endpoint
curl -fsSL http://localhost:5000/api/health
# Expected: {"ok": true, "status": "ok", "service": "aetherion-backend"}

# 3. Frontend Auth UI Health Probe
curl -fsSL http://localhost:3000/healthz
# Expected: HTTP 200
```

---

## 7. Preflight & Control-Plane Smoke Test Validation

The current smoke scope must include explicit negative execution checks: task
execution and agent/plugin/worker execution-looking requests return the stable
`EXECUTION_DISABLED` response. Do not treat a mock provider or worker health as
an autonomous task pass.

Execute the automated staging certification scripts:

```bash
# 1. Preflight System Health Check
python scripts/production_preflight.py

# 2. Automated Production / Staging Smoke Suite
python scripts/ci_production_smoke.py

# 3. CLI Connectivity and TUI Doctor
aetherion config set server_url https://staging-api.aetherion.ai
aetherion doctor

# 4. Zero-Secret Log Audit (Verify no plaintext secrets in stdout/stderr)
docker compose -f deploy/compose/docker-compose.prod.yml logs backend 2>&1 | grep -iE "(sk-|eyJh|JWT_SECRET|password)"
# Expected: Zero matches
```

---

## 8. Rollback Procedures

If any preflight or smoke test fails, immediately initiate rollback:

```bash
# 1. Stop and tear down active containers
docker compose -f deploy/compose/docker-compose.prod.yml down

# 2. Database Snapshot Restore (if migrations caused regression)
python scripts/db_restore_verify.py --backup-file /path/to/pre_staging_backup.sql

# 3. Re-deploy Previous Release Candidate Tag
git checkout <PREVIOUS_STABLE_TAG>
docker compose -f deploy/compose/docker-compose.prod.yml --env-file deploy/staging.env up -d --build
```

---

## 9. Promotion Gate Criteria (Staging to Production)

Promotion from Staging to Production is permitted **only** when all the following gates pass:

1. **Preflight Certification:** `scripts/production_preflight.py` returns status `READY` with zero critical failures.
2. **Smoke Suite Pass Rate:** `scripts/ci_production_smoke.py` passes 10/10 tests.
3. **Database Migration Health:** All migrations applied cleanly with verified reverse rollback capability.
4. **Sandbox Integrity:** Container confinement verified; untrusted code verified blocked from host root.
5. **Secret Hygiene:** Automated log scan confirms 0 leaked API keys, tokens, or credentials.
6. **OAuth Authentication Flow:** Live OAuth callback verified against staging domain with valid JWT issued.
