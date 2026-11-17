# Aetherion CLI

**The official command-line interface and terminal workspace for the Aetherion AI Platform.**

[![PyPI version](https://img.shields.io/pypi/v/aetherion?color=%237c3aed&label=PyPI)](https://pypi.org/project/aetherion/)
[![Python 3.9+](https://img.shields.io/pypi/pyversions/aetherion?color=%237c3aed)](https://pypi.org/project/aetherion/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE.md)
[![CI](https://github.com/aetherion/aetherion-ai/actions/workflows/release.yml/badge.svg)](https://github.com/aetherion/aetherion-ai/actions)

---

Aetherion is not a command-line wrapper. It is a **complete AI operating environment** that runs inside your terminal — designed for developers who want to work with AI agents without leaving their workflow.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  ✦ Aetherion                          ✦ Dashboard   ↑↓ navigate  Ctrl+P   │
│  AI Workspace                                                               │
│  user@aetherion.ai  ──────────────────────────────────────────────────────  │
│  ★ Pro                                                                      │
│                      ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  WORKSPACE           │ Sessions │ │  Tasks   │ │  Tokens  │ │ Latency  │  │
│  ✦ Dashboard  1      │    42    │ │    18    │ │  1.2M    │ │  320ms   │  │
│  ◈ Chat       2      └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│  ⊞ Sessions   3                                                             │
│  ◷ Memory     4      Recent Sessions                                        │
│  ⊕ Plugins    5      ✓ Build REST API       3m ago                         │
│  ◆ Skills     6      ✓ Code review PR #42   1h ago                         │
│  ⬡ Providers  7      ◎ Deploy pipeline      running                        │
│  ⊟ Jobs       8                                                             │
│                                                                             │
│  ● online 320ms  ─────────────────────────────────────────────────────────  │
│  Ctrl+P palette                                                             │
│  Ctrl+B toggle   ◎ idle  │  gpt-4o  │  ✓ online 320ms  │  ctx 12%         │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Installation

### PyPI (recommended)

```bash
pip install aetherion-cli

# With TUI support (full terminal workspace)
pip install "aetherion-cli[tui]"

# Editable install from source
git clone https://github.com/aetherion/aetherion-ai
cd aetherion-ai/client-cli/aetherion-cli
pip install -e ".[dev]"
```

### Standalone binary (no Python required)

> **Coming soon** — binaries will be available on GitHub Releases for Linux, macOS, and Windows.

### Package managers

> **Coming soon** — Homebrew, Scoop, and Winget packages are planned for v1.1.

---

## Quickstart

```bash
# 1. Authenticate (creates ~/.aetherion/config.json)
aetherion login

# 2. Launch the terminal workspace
aetherion tui

# 3. Or run a one-shot task from the command line
aetherion chat "Build a FastAPI CRUD API for a todo app"

# 4. Non-interactive (pipe-friendly)
aetherion run "Summarise this file" < notes.txt
```

---

## Terminal Workspace (TUI)

Launch with `aetherion tui`. The workspace opens with a full keyboard-driven interface.

### Global shortcuts

| Key | Action |
|-----|--------|
| `1` – `8` | Switch workspace screen |
| `Ctrl+P` | Command palette (fuzzy search all actions) |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+N` | New session |
| `Escape` | Back / dismiss overlay |
| `F1` | Keyboard help |

### Workspace screens

| Screen | Key | Description |
|--------|-----|-------------|
| Dashboard | `1` | KPI metrics, recent sessions, live activity timeline |
| Chat | `2` | Streaming AI conversation with markdown rendering |
| Sessions | `3` | Browse, search, resume, and delete sessions |
| Memory | `4` | Persistent agent memory — search, add, and review |
| Plugins | `5` | GitHub discovery, catalog pins, and metadata-only source lifecycle |
| Skills | `6` | Skill library manager + workflow-to-skill creator |
| Providers | `7` | Live provider status, latency, BYOK key management |
| Jobs | `8` | Background tasks with real-time progress bars |

### Chat screen shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Submit message |
| `Shift+Enter` | New line |
| `↑` / `↓` | Navigate input history |
| `Ctrl+U` | Clear input |
| `Ctrl+L` | Clear conversation |
| `Y` | Approve a tool/file change (when prompted) |
| `N` / `Escape` | Deny tool approval |
| `A` | Always approve this tool for the session |

### TUI GitHub repository workflow

Open the Plugins screen with `5`. The screen has three explicit views/actions:

1. **Saved catalog** — review curated and user aliases, requested refs, selected
   commit SHAs, compatibility evidence, catalog presence, managed-source status,
   integrity, and execution state.
2. **GitHub discovery** — enter a query and optional language, minimum-stars,
   result-limit, and archived-repository filters. Results are read-only until
   you save one. Popularity, activity, and declared licenses are signals only;
   they do not establish trust or compatibility.
3. **Add custom URL** — enter a canonical HTTPS GitHub repository root, local
   alias, and optional ref. The existing safe manager validates and inspects the
   source, resolves its full commit SHA, and saves metadata without installing.

Select a result with the arrow keys or mouse to review its owner, URL,
description, language, stars, forks, topics, license, archive state, timestamps,
compatibility evidence, and the explicit `not saved / not installed / execution
disabled` state. Use **Inspect** for bounded source metadata without saving or
installing.

For a saved entry, the lifecycle actions use the same `PluginCatalogManager`
methods as the CLI:

- **Save alias** persists only catalog metadata; it never stages source.
- **Stage source** installs the selected immutable catalog commit.
- **Verify** checks the managed-source integrity manifest.
- **Refresh** observes a branch/tag candidate without moving the selected pin.
- **Pin ref** explicitly changes the saved ref and commit; it does not install.
- **Update** re-stages the selected pin after confirmation.
- **Uninstall source** removes managed source after confirmation but preserves the catalog alias.
- **Remove catalog** removes only the alias after confirmation; any managed source remains.

All network and filesystem actions run through the existing gateway and safe
manager. Failed searches or mutations show retryable errors and reload the
authoritative catalog/source state. No repository code, package scripts,
entrypoints, dependencies, binaries, workers, or MCP servers are executed, and
`execution_enabled` remains `False` throughout.

---

## Command Reference

### Authentication

```bash
aetherion login                          # Interactive prompt (email + password)
aetherion login --email you@example.com  # Non-interactive email/password login
aetherion login --email you@example.com --password 'secret'  # Fully non-interactive
aetherion whoami                         # Show current user profile
aetherion logout                         # Clear stored credentials
```

### Agent tasks

```bash
# Interactive streaming chat
aetherion chat "Refactor this function to use async/await"

# One-shot, non-interactive
aetherion run "Write unit tests for auth.py" --model gpt-4o

# Generate a plan without executing
aetherion plan "Migrate PostgreSQL to CockroachDB"

# Resume a session
aetherion chat --session <session-id>

# Choose output format
aetherion chat "List all routes" -o json
```

### Session management

```bash
aetherion history list             # List recent sessions
aetherion history show <id>        # Show session transcript
```

### Replay (time-travel debugging)

Replay is a projection over the append-only evidence ledger — nothing is stored,
every answer is recomputed. Frames are addressed by `seq` (a stable 0-based index
into the ordered event list) because two events can share a millisecond.

```bash
aetherion replay status                       # Engine + read-edge health
aetherion replay list -s <session-id>         # Replayable executions
aetherion replay show <execution-id>          # Summary + trailing frames
aetherion replay show <id> --kinds tool,filesystem --tail 50
aetherion replay frame <id> <seq>             # One frame, with its state
aetherion replay seek <id> --seq 42           # State at an instant
aetherion replay seek <id> --timestamp 1700000000 --transcript
aetherion replay diff <id> --from 0 --to 100  # What changed between frames
aetherion replay export <id> -f bundle.json   # Portable redacted bundle
```

`replay export -o json` writes the bundle to **stdout only** — piping to `jq`
should not also litter the working directory. Full capability guide:
[`docs/replay.md`](../../docs/replay.md).

### Memory

```bash
aetherion memory search "postgres"    # Search agent memory
aetherion memory remember "Always use async SQLAlchemy"
aetherion memory list
aetherion memory clear
```

### Plugins

```bash
aetherion plugins list                         # Curated + locally stored records
aetherion plugins search agent                 # Search metadata
aetherion plugins catalog list                 # Curated + saved repository pins
aetherion plugins catalog add https://github.com/owner/repo --id my-repo --ref main
aetherion plugins catalog info my-repo
aetherion plugins catalog refresh my-repo       # Check only; does not move the pin
aetherion plugins catalog pin my-repo --ref v2   # Explicitly move the saved pin
aetherion plugins catalog remove my-repo --yes
aetherion plugins discover "agent plugin" --language Python --limit 10
aetherion plugins info rea                     # Show curated or installed details
aetherion plugins inspect https://github.com/owner/repo --ref main
aetherion plugins install my-repo              # Stage the saved immutable catalog pin
aetherion plugins status                       # Verify local source integrity
aetherion plugins update my-repo               # Re-stage the selected catalog pin
aetherion plugins uninstall my-repo --yes      # Remove managed source only
```

Plugin management is intentionally **metadata-only** in the current Option A
control-plane release. Sources must be HTTPS GitHub repository roots. Branches
and tags are resolved to immutable commit SHAs, archives are validated for
traversal, symlink, credential-file, file-count, and size hazards, and the
source is promoted atomically under `~/.aetherion/plugins/`. Package scripts,
dependencies, entrypoints, tools, and permissions are recorded as untrusted
metadata only: no import, package installation, lifecycle hook, subprocess, or
plugin execution is performed. Use `--expected-sha256` when an out-of-band
archive digest is available.

The curated REA entry points to `https://github.com/morluto/rea`, package
`rea-agents`, and MIT attribution from `morluto`. The manager preserves the
repository source and records the resolved commit; it does not run `npm`,
`npx`, the `rea` binary, or the MCP server.

The repository catalog is separate from installed source data. `catalog add`
persists a user alias, the requested ref, and the resolved full commit SHA, but
does not install the repository. `catalog refresh` checks whether the saved ref
now points somewhere else and reports a candidate SHA without changing the
selected pin. Use `catalog pin` for an explicit pin change. `catalog remove`
removes only the saved record; use `plugins uninstall` to remove an installed
metadata-only source.

For a saved user repository, `plugins install <alias>` always stages the
catalogued immutable commit. Supplying a moving branch or tag with that alias
is rejected; select a new ref with `plugins catalog pin <alias> --ref <ref>`
first. After pinning, `plugins update <alias>` atomically stages that selected
snapshot, verifies its integrity, and leaves execution disabled. The installed
source ID may also be used with `plugins status`, `plugins update`, and
`plugins uninstall`.

`plugins discover` performs a bounded, read-only GitHub repository search. Stars,
forks, topics, activity, and declared licenses are discovery signals only; they
do not prove security, quality, or Aetherion compatibility. Compatibility is
classified conservatively: a supported explicit Aetherion plugin API can be
`compatible`, an explicit unsupported API can be `incompatible`, and generic
repositories or package metadata remain `unknown`/source-only until affirmative
plugin-contract evidence exists.

The supported static manifest contract is `aetherion.plugin.json` with string
`manifest_schema_version` (currently `1`), string `plugin_api_version`, a
valid `id`, a name, and a semantic version. Malformed fields, conflicting
schema/API aliases, unsupported schema versions, invalid IDs, and invalid
version strings fail closed. A valid schema with an unsupported API major is
classified `incompatible`; a generic `plugin.json` that merely mentions an
API version is not treated as an Aetherion contract and remains `unknown`.

### Skills

```bash
# List skills from backend
aetherion skills list

# Install a skill
aetherion skills install code-review

# Create a new skill from a description
aetherion skills create --name git-workflow \
  --description "Automate git commit, tag, and push"

# List locally created skills
aetherion skills local
```

Skills are written to `~/.aetherion/config/skills/<name>/SKILL.md` in standard
SKILL.md format (name, description, triggers, body). Create them from the TUI
via the Skills screen → "Create Skill" panel, which automatically captures
the current conversation as workflow steps.

### Providers

```bash
aetherion providers list           # All providers + status
aetherion providers test openai    # Test connectivity + latency
```

### Configuration

```bash
aetherion config --show                        # Show current config
aetherion config --url https://api.aetherion.ai
aetherion config --model claude-3-5-sonnet
aetherion config --output json
```

### Health

```bash
aetherion doctor          # Connectivity, auth, environment check
```

---

## Configuration

Settings are stored in `~/.aetherion/config.json`. Credentials are stored in
the OS keyring (macOS Keychain, Windows Credential Manager, libsecret on Linux)
with a plain-text file fallback.

| Variable | Priority | Description |
|----------|----------|-------------|
| `AETHERION_URL` | Env (highest) | Backend server URL |
| `AETHERION_TOKEN` | Env | Auth token (CI/CD use) |
| `AETHERION_MODEL` | Env | Default model override |
| OS keyring | Keyring | Token for interactive use |
| `~/.aetherion/config.json` | File | All other settings |

### Example: CI/CD usage

```bash
# Set env vars — no interactive login required
export AETHERION_URL=https://api.aetherion.ai
export AETHERION_TOKEN=${{ secrets.AETHERION_TOKEN }}

aetherion run "Run security audit on the PR diff" -o json > audit.json
```

---

## Architecture

```
aetherion (CLI + TUI)
├── aetherion_cli/
│   ├── main.py          # All Click commands
│   ├── client.py        # HTTP client (httpx, retries, auth)
│   ├── config.py        # Config + keyring credential management
│   └── tui/
│       ├── app.py       # Textual App — single layout, screen swap
│       ├── state.py     # Thread-safe reactive state store
│       ├── gateway.py   # Async bridge: SSE streaming + REST API
│       ├── theme.py     # Design system: colors, glyphs, CSS
│       ├── sidebar.py   # Navigation sidebar
│       ├── overlays.py  # Command palette, approval, notifications
│       ├── skill_creator.py   # Workflow → SKILL.md converter
│       └── screens/
│           ├── dashboard.py   # KPI cards + timeline
│           ├── chat.py        # Streaming conversation
│           ├── sessions.py    # 2-pane session browser
│           ├── memory.py      # Memory explorer
│           ├── plugins.py     # Plugin manager
│           ├── skills.py      # Skills manager
│           ├── providers.py   # Provider BYOK management
│           └── jobs.py        # Background task monitor
```

The CLI and TUI are **pure clients** of the unified Python backend. All business
logic lives in the backend. No frontend-only persistence. The backend is the
single source of truth.

---

## Development

```bash
# Clone and install in editable mode with all extras
git clone https://github.com/aetherion/aetherion-ai
cd aetherion-ai/client-cli/aetherion-cli
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=aetherion_cli --cov-report=term-missing

# Lint
ruff check aetherion_cli/

# Type check
mypy aetherion_cli/ --ignore-missing-imports

# Build standalone binary
pip install pyinstaller
pyinstaller aetherion.spec --noconfirm --clean
./dist/aetherion --version
```

---

## Distribution

| Channel | Status | Command |
|---------|--------|---------|
| PyPI | ✅ | `pip install aetherion-cli` |
| pipx | ✅ | `pipx install aetherion-cli` |
| Homebrew | 🔜 Coming soon | — |
| Scoop | 🔜 Coming soon | — |
| Winget | 🔜 Coming soon | — |
| Standalone binary | 🔜 Coming soon | GitHub Releases |

Releases are automated via GitHub Actions on `v*.*.*` tag push.

---

## License

MIT © 2024 Aetherion. See [LICENSE.md](LICENSE.md).

---

> **Aetherion CLI** — not a wrapper. A workspace.
