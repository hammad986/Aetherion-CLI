# Aetherion AI

### An AI agent control plane you can actually audit.

Aetherion is a **CLI/TUI-first AI engineering platform**: a hardened Flask control plane, a terminal-native developer client, a lightweight identity portal, and a fail-closed deployment story — built around one principle: *no autonomous action without verifiable evidence.*

<p align="left">
  <img alt="Version" src="https://img.shields.io/badge/version-1.0.0-blue">
  <img alt="Status" src="https://img.shields.io/badge/status-Option%20A%20%C2%B7%20Control%20Plane-0b7285">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB?logo=python&logoColor=white">
  <img alt="Backend" src="https://img.shields.io/badge/backend-Flask%203-000000?logo=flask&logoColor=white">
  <img alt="Frontend" src="https://img.shields.io/badge/frontend-React%2019%20%2B%20Vite%206-61DAFB?logo=react&logoColor=black">
  <img alt="Database" src="https://img.shields.io/badge/state-PostgreSQL%20%2B%20Redis-4169E1?logo=postgresql&logoColor=white">
  <img alt="Deploy" src="https://img.shields.io/badge/deploy-Docker%20Compose%20%2B%20Kubernetes-2496ED?logo=docker&logoColor=white">
  <img alt="Tests" src="https://img.shields.io/badge/tests-1%2C716%20collected-brightgreen">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
</p>

> **Read this first — release scope.** This repository is a **v1.0.0 release candidate on the
> Option A (control-plane) track**. Authentication, tenant-scoped durable task records,
> health/readiness, D3 metadata and bounded read-only control-plane operations are live.
> Autonomous agent execution, command execution, plugin execution, untrusted workers,
> physical worktree mutation and process attachment are **implemented but deliberately
> disabled**, and return explicit non-success responses. A green local Compose start is
> **not** evidence that external infrastructure has been provisioned. See
> [Scope & Status](#scope--status).

---

## Table of Contents

- [Why Aetherion Exists](#why-aetherion-exists)
- [Scope & Status](#scope--status)
- [Feature Matrix](#feature-matrix)
- [Architecture](#architecture)
- [Technology Stack](#technology-stack)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Terminal Authentication (RFC 8628)](#terminal-authentication-rfc-8628)
- [CLI Reference](#cli-reference)
- [Testing](#testing)
- [Deployment](#deployment)
- [Security Model](#security-model)
- [Repository Structure](#repository-structure)
- [Documentation Index](#documentation-index)
- [Engineering Milestones](#engineering-milestones)
- [Known Limitations](#known-limitations)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## Why Aetherion Exists

Most "AI coding agent" stacks ask you to trust a black box. Aetherion takes the opposite
position: every capability is a **named, governed operation** with a policy check, a durable
record, and a machine-readable audit trail.

The platform is composed of four surfaces that share one contract:

| Surface | What it is | Who it is for |
| --- | --- | --- |
| `cli/` | `aetherion` command-line client + interactive TUI workspace | Developers living in the terminal |
| `backend/` | Hardened Flask control plane: JWT, device auth, durable state, tasks, policy | Operators and integrators |
| `frontend-auth/` | Minimal React portal for login, registration, device confirmation | End users confirming a terminal login |
| `deploy/` | Multi-stage Dockerfiles, Compose, Kubernetes, Nginx, preflight scripts | Platform / SRE teams |

Everything else in the repository — `tests/`, `qa/`, `audit/`, `scripts/`, `docs/` — exists to
**prove** that the four surfaces behave as claimed.

---

## Scope & Status

**Release:** `1.0.0` · **Track:** Option A (control plane) · **Maturity:** release candidate,
independently audited, not a production deployment approval.

| Area | State | Notes |
| --- | --- | --- |
| Authentication (password + JWT) | ✅ Shipped | bcrypt hashing, independent `JWT_SECRET`/`SECRET_KEY`, refresh rotation |
| Device authorization (RFC 8628) | ✅ Shipped | Terminal shows `ABCD-1234`, browser confirms, tokens land in the OS keyring |
| Account recovery | ✅ Shipped | Password reset + verification token lifecycle (timezone-aware, UTC) |
| Tenant-scoped tasks | ✅ Shipped | Durable records, ownership enforced at the store layer |
| Health / readiness / preflight | ✅ Shipped | Fail-closed startup validation in `staging` and `production` |
| CLI + TUI | ✅ Shipped | `doctor`, `version`, `auth`, `task`, `workspace`, `diff`, `logs`, `artifacts` |
| Skills library & catalog | ✅ Shipped | Signed, curated, authenticated publish path |
| Provider registry (OpenAI/Anthropic/etc.) | ✅ Shipped (read/config) | Live provider calls gated by execution scope |
| GitHub plugin manager + catalog discovery | ✅ Shipped (read-only) | Trust verifier, publisher evidence, no plugin execution |
| Repository intelligence / planning graph | 🟡 Implemented, gated | Runs under the control-plane policy only |
| Durable execution fabric / worker runtime | 🟡 Implemented, gated | Redis coordination required; in-process queues blocked |
| Autonomous agent + command execution | ⛔ Disabled by release policy | Returns explicit non-success, regardless of env flags |
| Physical worktree mutation / process attachment | ⛔ Disabled by release policy | Blocked at the contract layer |

Legend: ✅ enabled in this release · 🟡 present but policy-gated · ⛔ intentionally unreachable.

Verification of this table lives in `docs/reports/` — 100+ implementation reports, independent
audits and remediation records, one per phase and release gate.

---

## Feature Matrix

| Capability | Entry point | Backing module |
| --- | --- | --- |
| Terminal workspace (TUI) | `aetherion tui` | `cli/aetherion_cli/tui/` |
| Device login | `aetherion auth login` | `cli/aetherion_cli/auth/device_flow.py` |
| Secure token storage | automatic | `cli/aetherion_cli/auth/token_store.py` (OS keyring, `0o600` fallback) |
| Task lifecycle | `aetherion task …` | `backend/routes/tasks.py`, `backend/execution/jobs/` |
| Workspace + artifact tracking | `aetherion workspace …` | `backend/routes/workspace.py`, `backend/execution/worktree.py` |
| Diffs & patches | `aetherion diff …` | `backend/routes/diff.py`, `cli/aetherion_cli/diff/` |
| Replay of recorded runs | `aetherion replay` | `backend/execution/replay.py` |
| Memory & retrieval | internal API | `backend/execution/memory/` |
| Model routing & budgets | internal API | `backend/execution/providers/`, `…/orchestration/budget_enforcer.py` |
| Skills library | `aetherion skill …` | `cli/aetherion_cli/skill_library.py`, `backend/execution/extensions/` |
| Trust verification | internal | `cli/aetherion_cli/trust_verifier.py`, `backend/routes/phase4_routes.py` |
| Health & readiness | `GET /health`, `GET /ready` | `backend/routes/health.py`, `backend/core/preflight.py` |

---

## Architecture

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  aetherion CLI / TUI        │        │  Auth & device portal        │
│  click + rich + textual     │◄──────►│  React 19 + Vite + Tailwind  │
└──────────────┬──────────────┘        └──────────────┬───────────────┘
               │  HTTPS + JWT (RFC 8628 device flow)  │
               ▼                                      ▼
        ┌──────────────────────────────────────────────────────┐
        │        Aetherion Control Plane (Flask 3)             │
        │  auth · tasks · workspaces · diff · artifacts        │
        │  middlewares: guards · observability · api_response  │
        └───────┬──────────────────────────┬───────────────────┘
                │                          │
        ┌───────▼────────┐        ┌────────▼─────────┐
        │ PostgreSQL     │        │ Redis            │
        │ durable state, │        │ multi-worker     │
        │ D3 metadata,   │        │ coordination,    │
        │ Alembic        │        │ leases, queues   │
        └────────────────┘        └──────────────────┘
                │
        ┌───────▼────────────────────────────────────────────────┐
        │ Execution layer (policy-gated)                          │
        │ orchestration · fabric · jobs · memory · extensions ·   │
        │ verification · sandbox · provider routing               │
        └────────────────────────────────────────────────────────┘
```

Guiding invariants:

1. **Fail closed.** In `staging`/`production`, missing PostgreSQL, missing Redis, default or
   identical secrets abort startup — no silent SQLite fallback, no in-process queue.
2. **Durable first.** Task state is written before it is acknowledged.
3. **Policy over flags.** Execution capability is decided by the release-scope contract, not by
   an environment variable an operator can flip.
4. **Evidence, not adjectives.** Every claimed capability has a test and a report behind it.

---

## Technology Stack

| Layer | Stack |
| --- | --- |
| CLI / TUI | Python ≥3.10, `click`, `rich`, `textual`, `httpx`, `pydantic`, `keyring`, `cryptography` (hatchling wheel) |
| Backend | Flask 3, PyJWT, bcrypt, SQLAlchemy 2, psycopg 3, redis-py, Gunicorn |
| Frontend | React 19, Vite 6, React Router 7, Tailwind CSS 4, lucide-react |
| Data | PostgreSQL (production), SQLite (local development only), Redis (coordination) |
| Migrations | Alembic + tracked D3 migration ledger |
| Packaging | hatchling (CLI), PyInstaller (optional binary), multi-stage Docker |
| Infrastructure | Docker Compose, Kubernetes manifests, Nginx, preflight/backup/restore scripts |
| Quality | pytest (1,716 tests), ruff, mypy (strict), deterministic release builder |

---

## Quick Start

### Prerequisites

- Python 3.10+ (3.12 recommended)
- Node.js 18+ (auth portal only)
- Optional: Docker + Docker Compose (for the containerised path)

### 1. Configure the environment

```bash
cp .env.example .env
# Generate independent, high-entropy secrets:
python -c "import secrets; print(secrets.token_hex(32))"   # -> SECRET_KEY
python -c "import secrets; print(secrets.token_hex(32))"   # -> JWT_SECRET
```

Local development runs on a fallback SQLite store. `staging` and `production` refuse to boot
without PostgreSQL and Redis.

### 2. Start the backend

```bash
python -m pip install -r backend/requirements.txt
python app.py                  # http://localhost:5000  (local-development mode)
```

`app.py` at the repository root is the authoritative WSGI entry point and re-exports the exact
same Flask instance as `backend.app` — so `from app import app`, `gunicorn app:app` and
`python -m pytest` all resolve identically.

### 3. Start the auth portal

```bash
cd frontend-auth
npm install
npm run dev                    # http://localhost:3000
```

### 4. Install and run the CLI

```bash
cd cli
pip install -e ".[dev]"

aetherion doctor               # environment diagnostics (secrets redacted)
aetherion version              # runtime + build version
aetherion auth login           # RFC 8628 device flow
aetherion tui                  # interactive terminal workspace
```

---

## Configuration

All configuration is environment-driven; templates are committed, populated files are not.

| File | Purpose | Committed |
| --- | --- | --- |
| `.env.example` | Local development template | ✅ |
| `deploy/production.env.example` | Production deployment template (`REPLACE_*` placeholders) | ✅ |
| `.env` | Your local values | ⛔ gitignored |
| `deploy/production.env` | Your production values (real secrets) | ⛔ gitignored |

Key variables:

| Variable | Required | Description |
| --- | --- | --- |
| `ENVIRONMENT` | ✅ | `local-development` \| `CI-test` \| `staging` \| `production` |
| `SECRET_KEY` | ✅ | ≥32 chars, independent, high entropy |
| `JWT_SECRET` | ✅ | ≥32 chars, independent, must differ from `SECRET_KEY` |
| `SERVER_URL` | ✅ | Public HTTPS URL in production; insecure values fail preflight |
| `FRONTEND_URL` / `ALLOWED_ORIGINS` | ✅ | Portal URL and CORS allowlist |
| `DATABASE_URL` | prod | PostgreSQL DSN (SQLite fallback is local-only) |
| `REDIS_URL` | prod | Redis DSN; required for multi-worker coordination |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | prod | Container database identity |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `GROQ_API_KEY` / `DEEPSEEK_API_KEY` | optional | Provider credentials (never committed) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | optional | OAuth login |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | optional | GitHub OAuth + catalog discovery |

---

## Terminal Authentication (RFC 8628)

```text
Developer terminal                    Aetherion portal                Control plane
      │                                     │                            │
      │ 1. POST /auth/device/code ──────────┼───────────────────────────►│
      │◄── user_code = ABCD-1234 ───────────┼────────────────────────────│
      │ 2. open /activate?code=ABCD-1234 ──►│                            │
      │                                     │ 3. confirm (logged in) ───►│
      │ 4. poll /auth/device/token ─────────┼───────────────────────────►│
      │◄── access + refresh token ──────────┼────────────────────────────│
      │ 5. persist to OS keyring            │                            │
```

```bash
aetherion auth login      # or the shorthand: aetherion login
```

Tokens are stored in the platform secure vault — **Windows Credential Manager**, **macOS
Keychain**, **Linux Secret Service** — with a `0o600` file fallback on headless hosts.
All diagnostic output (`doctor`, config export, logs) redacts tokens, authorization headers,
connection strings and provider keys.

---

## CLI Reference

| Command | Description |
| --- | --- |
| `aetherion doctor` | Environment, keyring, config and connectivity diagnostics |
| `aetherion version` | Runtime, build and contract versions |
| `aetherion auth login` / `logout` | Device-flow login and token revocation |
| `aetherion task …` | Submit, inspect and track tenant-scoped tasks |
| `aetherion workspace …` | Workspace creation, listing and target validation |
| `aetherion diff …` | Diff inspection with bounded unified-diff boundaries |
| `aetherion logs …` | Stream run logs |
| `aetherion artifacts …` | List and fetch produced artifacts |
| `aetherion replay` | Replay a recorded run from durable state |
| `aetherion config` | Show effective, redacted configuration |
| `aetherion tui` | Interactive workspace: chat, jobs, memory, plugins, providers, skills, sessions |

---

## Testing

```bash
# Default suite — unit + CLI, no external services required
python -m pytest                    # 464 tests

# Integration suite — requires an approved PostgreSQL/Redis target
python -m pytest -m integration     # 1,252 tests

# CLI-only suite
cd cli && python -m pytest tests

# Lint / type check
ruff check . && mypy cli/aetherion_cli
```

`pytest.ini` sets `pythonpath = . backend cli`, so backend and CLI imports resolve from the
repository root. Integration runs are fail-closed: they need
`AETHERION_TEST_DATABASE_URL` plus an approved host/database allowlist
(`AETHERION_TEST_ALLOWED_HOSTS`, `AETHERION_TEST_ALLOWED_DBS`) — there is no silent fallback.

Release evidence — audit scorecards, browser UAT captures, CLI matrices, capacity benchmarks —
lives in `audit/`.

---

## Deployment

### Docker Compose

```bash
docker compose -f deploy/compose/docker-compose.prod.yml \
  --env-file deploy/production.env up -d
```

The production Compose file requires an explicitly selected host URL, frontend URL, allowed
origins, database, Redis endpoint and a secret injection path. It exposes **no** provider,
sandbox or execution enable switch — those are governed by the release scope, not by config.

### Kubernetes

```bash
# Plan first (no cluster mutation)
python scripts/deploy_k8s.py --plan \
  --release-id <lowercase-release-id> \
  --backend-image <immutable-backend-image> \
  --frontend-image <immutable-frontend-image>

# Apply only after the namespace Secret exists and manifests validate
python scripts/deploy_k8s.py \
  --release-id <lowercase-release-id> \
  --backend-image <immutable-backend-image> \
  --frontend-image <immutable-frontend-image>
```

### Preflight, migration, backup & restore

```bash
python scripts/production_preflight.py
python scripts/db_migrate.py --check --json
python scripts/db_backup.py
python scripts/db_restore_verify.py
```

`deploy/DEPLOYMENT_CHECKLIST.md` and `deploy/STAGING_RUNBOOK.md` hold the operator procedure.

---

## Security Model

Documented fully in [SECURITY.md](SECURITY.md). Summary:

- **Independent secrets.** Missing, default, or identical `SECRET_KEY`/`JWT_SECRET` abort
  startup in `staging`/`production`.
- **No degraded fallbacks.** SQLite and in-memory Redis are rejected outside local development.
- **Native credential storage.** OS keyring with `0o600` fallback; no plaintext tokens.
- **Redaction by default.** Diagnostics, config exports and logs strip secrets.
- **Container-bound execution.** Subprocess execution is confined by the runner contract,
  which blocks host mounts (`/etc`, `/proc`, `/sys`, `/root`, `/home`) including Windows
  device namespaces, junction points and case variations.
- **Tenant isolation.** Ownership is enforced in the store layer, not the route layer.
- **Policy over flags.** Autonomous execution stays unreachable in this release regardless of
  environment configuration.

Report vulnerabilities to **security@aetherion.ai** (acknowledgement within 24h, remediation
timeline within 72h).

---

## Repository Structure

```
.
├── app.py                     # Authoritative WSGI entry point (re-exports backend.app)
├── backend/                   # Flask control plane
│   ├── core/                  # environment, preflight, startup validator
│   ├── db/                    # stores, D3 migrations, Alembic
│   ├── execution/             # orchestration, fabric, jobs, memory, extensions, sandbox
│   ├── middleware/            # guards, observability, API response envelope
│   ├── routes/                # auth, tasks, workspace, diff, artifacts, replay, health
│   └── requirements.txt
├── cli/                       # `aetherion` CLI + TUI (hatchling wheel, MIT)
│   ├── aetherion_cli/         # commands, engine, providers, skills, tui, trust
│   └── tests/                 # CLI test suite
├── frontend-auth/             # React 19 + Vite auth & device confirmation portal
├── deploy/                    # Docker, Compose, Kubernetes, Nginx, runbooks, env templates
├── scripts/                   # preflight, migrations, backup/restore, release builder, K8s
├── tests/                     # backend unit + integration + API contract suites
├── qa/                        # manual UAT drivers and automated product-surface checks
├── audit/                     # release evidence: scorecards, UAT captures, manifests
├── docs/
│   ├── production_readiness.md
│   └── reports/               # 100+ phase reports, independent audits, remediations
├── pytest.ini                 # test paths, markers, DeprecationWarning-as-error
├── CHANGELOG.md               # phase-by-phase change history
└── SECURITY.md                # security policy and fail-closed posture
```

---

## Documentation Index

- **[CHANGELOG.md](CHANGELOG.md)** — phase-by-phase engineering history.
- **[SECURITY.md](SECURITY.md)** — supported versions, controls, disclosure process.
- **[docs/production_readiness.md](docs/production_readiness.md)** — readiness criteria.
- **[docs/reports/](docs/reports/)** — the full audit trail:
  - `AETHERION_*` — implementation reports, independent audits, remediation records.
  - `PHASE_9..24_*` — capability milestones, from the coding agent through production
    resilience and final certification.
  - `FINAL_*` — release gates, manual UAT, production certification.
  - `STAGING_*` — staging launch and hygiene verification.
- **[deploy/DEPLOYMENT_CHECKLIST.md](deploy/DEPLOYMENT_CHECKLIST.md)** and
  **[deploy/STAGING_RUNBOOK.md](deploy/STAGING_RUNBOOK.md)** — operator procedure.
- **[audit/](audit/)** — machine-readable evidence: CLI matrices, browser results, release
  manifests, scale capacity results.

---

## Engineering Milestones

| Phase | Delivered |
| --- | --- |
| 1–8 | Deployment scope, control-plane execution policy, extensions, memory/sessions/jobs, skills, repository features |
| 9–12 | Trust verifier, HITL lifecycle, replay engine, runner contract, production trust |
| 13–17 | Verified coding agent, agent orchestration, model orchestration, live budget enforcement, repository intelligence |
| 18–22 | Skills & memory, planning/execution graph, durable execution, execution fabric, control-plane worker runtime |
| 23–24 | Production system hardening, resilience, independent certification |
| Release gates | Source release baseline, reproducible candidate builds, immutable image rehearsal, UAT and certification |

Each milestone ships with tests in `tests/integration/` and a report in `docs/reports/`.

---

## Known Limitations

This is a **release candidate**, and the documentation says so on purpose:

- Autonomous execution is out of scope for Option A — the interfaces exist and are tested,
  but the capability is unreachable by design.
- A local Compose start, a mock provider or a healthy `/health` response is **not** proof of
  execution readiness or of provisioned external infrastructure.
- Sandbox guarantees are verified for the container runner path; host-level confinement on
  Windows depends on the platform's namespace handling.
- The auth portal is intentionally minimal — identity and device confirmation only, no admin UI.
- Kubernetes manifests are validated and plan-tested; applying them requires a provisioned
  namespace Secret and an immutable image pair you supply.

---

## Roadmap

1. **Option B activation** — enable the gated execution fabric behind the existing policy
   contract, with an independent security re-audit before any traffic.
2. **Provider breadth** — expand live-provider coverage and per-tenant budget enforcement.
3. **Observability** — first-class metrics/tracing surface for the control plane.
4. **Portal depth** — session management, device inventory and audit export.
5. **Packaging** — signed CLI binaries and a published wheel.

---

## Contributing

1. Branch from `main`; keep changes scoped and evidence-backed.
2. Run `python -m pytest` and `ruff check .` before opening a PR.
3. Never commit a populated env file, a database, a runtime workspace or a test result dump —
   `.gitignore` covers these; check `git status` before you push.
4. New capability means new tests **and** a report under `docs/reports/`.

---

## License

MIT — see `cli/pyproject.toml` for the declared license metadata.

---

<p align="center"><sub>Built for developers who would rather read the audit trail than trust the demo.</sub></p>
