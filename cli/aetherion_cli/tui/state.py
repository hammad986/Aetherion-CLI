"""
aetherion_cli/tui/state.py
Central, thread-safe application state for the Aetherion TUI workspace.

Full state model supporting: dashboard, chat, sessions, memory, plugins,
skills, providers, metrics, jobs, git, notifications, approvals, timeline.

All mutations go through typed setter methods that emit change events.
No business logic here — state is a pure data store.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class AgentStatus(str, Enum):
    IDLE             = "idle"
    RUNNING          = "running"
    STREAMING        = "streaming"
    WAITING_APPROVAL = "waiting_approval"
    ERROR            = "error"
    DONE             = "done"
    FAILED           = "failed"
    BLOCKED          = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED        = "cancelled"
    SUCCEEDED        = "succeeded"


class Screen(str, Enum):
    DASHBOARD   = "dashboard"
    CHAT        = "chat"
    SESSIONS    = "sessions"
    MEMORY      = "memory"
    PLUGINS     = "plugins"
    SKILLS      = "skills"
    PROVIDERS   = "providers"
    METRICS     = "metrics"
    JOBS        = "jobs"
    TIMELINE    = "timeline"
    GIT         = "git"
    HELP        = "help"
    WELCOME     = "welcome"
    PROFILE     = "profile"


class NotifLevel(str, Enum):
    INFO    = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR   = "error"


class RuntimeState(str, Enum):
    ONLINE        = "ONLINE"
    DEGRADED      = "DEGRADED"
    OFFLINE       = "OFFLINE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ERROR         = "ERROR"


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Message:
    """A single message in the conversation transcript."""
    id:           str
    role:         str        # user | assistant | system | tool | error
    content:      str
    timestamp:    float      = field(default_factory=time.time)
    is_streaming: bool       = False
    tool_name:    Optional[str] = None
    used_skills:  List[str]  = field(default_factory=list)
    token_count:  Optional[int] = None
    ttft:         Optional[float] = None
    metadata:     Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(id=uuid.uuid4().hex[:8], role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", streaming: bool = False) -> "Message":
        return cls(id=uuid.uuid4().hex[:8], role="assistant",
                   content=content, is_streaming=streaming)

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(id=uuid.uuid4().hex[:8], role="system", content=content)

    @classmethod
    def tool_call(cls, tool_name: str, content: str) -> "Message":
        return cls(id=uuid.uuid4().hex[:8], role="tool",
                   content=content, tool_name=tool_name)

    @classmethod
    def error(cls, content: str) -> "Message":
        return cls(id=uuid.uuid4().hex[:8], role="error", content=content)


@dataclass
class Session:
    """A persisted agent session/workspace."""
    id:         str
    title:      str
    status:     str
    model:      Optional[str]
    created_at: Optional[float]
    updated_at: Optional[float] = None
    task:       Optional[str]   = None
    tags:       List[str]       = field(default_factory=list)
    # Extended metadata (Phase 1)
    provider:   Optional[str]   = None   # openai | anthropic | ollama | …
    workspace:  Optional[str]   = None   # repo path / workspace label
    compressed: bool            = False  # context was auto-compressed


@dataclass
class ContextUsage:
    """Live context window usage metrics."""
    input_tokens:  int = 0
    output_tokens: int = 0
    context_limit: int = 200_000
    memory_usage_mb: float = 0.0


    @property
    def used_fraction(self) -> float:
        if self.context_limit <= 0:
            return 0.0
        return min(1.0, self.input_tokens / self.context_limit)

    @property
    def used_pct(self) -> int:
        return int(self.used_fraction * 100)


@dataclass
class ApprovalRequest:
    """A blocking approval required from the user before agent continues."""
    request_id:  str
    tool_name:   str
    description: str
    risk_level:  str = "low"    # low | medium | high
    details:     Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryEntry:
    """A single entry in the memory store."""
    id:         str
    content:    str
    importance: float = 0.5
    created_at: Optional[float] = None
    tags:       List[str] = field(default_factory=list)


@dataclass
class Plugin:
    id:          str
    name:        str
    description: str
    category:    str     = "general"
    version:     str     = "1.0.0"
    installed:   bool    = False
    author:      str     = ""
    enabled:     bool    = False
    source_url:  str     = ""
    catalog_origin: str  = ""
    requested_ref: str   = ""
    resolved_commit: str = ""
    compatibility_status: str = "unknown"
    compatibility_reasons: List[str] = field(default_factory=list)
    status: str = "available"
    integrity: str = "not-installed"
    integrity_status: str = "unknown"
    catalog_auth_status: str = "unsigned"
    publisher_auth_status: str = "not_checked"
    publisher_evidence_provider: str = ""
    publisher_evidence_type: str = ""
    publisher_evidence_observation_status: str = "not_checked"
    publisher_evidence_provider_status: str = ""
    publisher_evidence_reason: str = ""
    publisher_evidence_provider_reason: str = ""
    publisher_evidence_identity_policy_status: str = "unconfigured"
    publisher_evidence_local_signature_status: str = "not_performed"
    publisher_evidence_signature_format: str = ""
    publisher_evidence_observed_at: str = ""
    publisher_evidence_freshness: str = "unavailable"
    publisher_evidence_commit: str = ""
    publisher_evidence_digest: str = ""
    policy_status: str = "unreviewed"
    execution_status: str = "disabled"
    trust_reason_code: str = ""
    catalog_key_id: str = ""
    catalog_trust_root_fingerprint: str = ""
    catalog_keyset_id: str = ""
    catalog_keyset_sequence: Optional[int] = None
    catalog_keyset_expires_at: str = ""
    catalog_keyset_active_key_ids: List[str] = field(default_factory=list)
    catalog_keyset_revoked_key_ids: List[str] = field(default_factory=list)
    catalog_trust_lifecycle_reason: str = ""
    catalog_trust_last_transition: str = ""
    catalog_sequence: Optional[int] = None
    catalog_issued_at: str = ""
    catalog_expires_at: str = ""
    catalog_freshness: str = "unavailable"
    catalog_refresh_failure_reason: str = ""
    installed_commit: str = ""
    execution_enabled: bool = False
    update_available: bool = False
    source_plugin_id: str = ""
    license_id: str = ""
    license_name: str = ""
    license_declared: bool = False
    topics: List[str] = field(default_factory=list)
    stars: int = 0
    forks: int = 0
    archived: bool = False
    default_branch: str = ""
    pushed_at: str = ""
    updated_at: str = ""
    candidate_commit: str = ""
    catalog_present: bool = False
    installed_id: str = ""
    language: str = ""
    last_error: str = ""


@dataclass
class PluginCandidate:
    """A read-only GitHub discovery result; it is not catalogued or installed."""
    id: str
    name: str
    full_name: str = ""
    source_url: str = ""
    description: str = ""
    language: str = ""
    topics: List[str] = field(default_factory=list)
    stars: int = 0
    forks: int = 0
    license_id: str = ""
    license_name: str = ""
    license_declared: bool = False
    archived: bool = False
    default_branch: str = ""
    pushed_at: str = ""
    updated_at: str = ""
    compatibility_status: str = "unknown"
    compatibility_reasons: List[str] = field(default_factory=list)
    resolved_commit: str = ""
    integrity_status: str = "unknown"
    catalog_auth_status: str = "unsigned"
    publisher_auth_status: str = "not_checked"
    policy_status: str = "unreviewed"
    execution_status: str = "disabled"
    catalog_origin: str = "discovery"
    execution_enabled: bool = False


@dataclass
class Skill:
    name:        str
    description: str
    version:     str  = "1.0.0"
    enabled:     bool = True
    builtin:     bool = False
    last_used:   Optional[float] = None
    invocation_count: int = 0
    skill_id:    str = ""
    category:    str = "uncategorized"
    source_kind: str = "bundled"
    installed:   bool = False
    custom:      bool = False
    readiness:   str = "definition_only_not_runtime_wired"
    runtime_connected: bool = False
    requirements: List[str] = field(default_factory=list)
    source_path: str = ""
    content_sha256: str = ""
    published_status: str = "local_definition"
    approval_status: str = "NOT_REQUIRED"
    tenant_digest: str = ""


@dataclass
class Provider:
    key:            str
    name:           str
    model:          str
    available:      bool          = False
    latency:        Optional[int] = None   # ms
    quality:        str           = "?"
    cost:           str           = "?"
    error:          Optional[str] = None
    # Extended configuration model fields
    id:             str           = ""
    provider_type:  str           = "OPENAI_COMPATIBLE"
    base_url:       str           = ""
    api_key_masked: str           = ""
    api_key_raw:    str           = ""
    is_default:     bool          = False
    is_enabled:     bool          = True
    configured:     bool          = False


@dataclass
class BackgroundJob:
    id:         str
    name:       str
    status:     str           # queued | running | done | error
    created_at: float         = field(default_factory=time.time)
    updated_at: float         = field(default_factory=time.time)
    result:     Optional[str] = None
    progress:   int           = 0   # 0–100


@dataclass
class TimelineEvent:
    id:        str
    ts:        float
    event:     str
    detail:    str     = ""
    level:     str     = "info"   # info | success | warning | error


@dataclass
class Notification:
    id:      str
    message: str
    level:   NotifLevel = NotifLevel.INFO
    ts:      float      = field(default_factory=time.time)
    read:    bool       = False


@dataclass
class DashboardMetrics:
    """Snapshot from /api/dashboard/metrics."""
    sessions_today: int   = 0
    tasks_done:     int   = 0
    tokens_used:    int   = 0
    cost_usd:       float = 0.0
    uptime_pct:     float = 100.0
    active_agents:  int   = 0
    memory_entries: int   = 0
    plugins_active: int   = 0


# ─────────────────────────────────────────────────────────────────────────────
# TUIState
# ─────────────────────────────────────────────────────────────────────────────

def _load_persisted_screen() -> Screen:
    try:
        from pathlib import Path
        import json
        p = Path.home() / ".aetherion" / "tui_state.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            val = data.get("current_screen")
            if val and val != "welcome":
                return Screen(val)
    except Exception:
        pass
    return Screen.CHAT


def _save_persisted_screen(screen: Screen) -> None:
    if screen == Screen.WELCOME:
        return
    try:
        from pathlib import Path
        import json
        p = Path.home() / ".aetherion" / "tui_state.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        data["current_screen"] = screen.value
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


class TUIState:
    """
    Thread-safe global state for the Aetherion terminal workspace.

    Rule: Widgets subscribe to fields and re-render when notified.
          Gateway mutates state from worker threads.
          App routes changes to widgets via call_from_thread().
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[Callable]] = {}

        # ── Navigation ───────────────────────────────────────────────────────
        self.current_screen: Screen  = Screen.DASHBOARD
        self.previous_screen: Screen = Screen.DASHBOARD
        self.saved_screen: Screen    = _load_persisted_screen()

        # ── Auth / connection ────────────────────────────────────────────────
        self.connected:            bool           = False
        self.latency_ms:           Optional[int]  = None
        self.user_email:           Optional[str]  = None
        self.user_plan:            str            = "Lite"
        self.user_id:              Optional[str]  = None
        self.offline_mode:         bool           = False
        self.offline_banner_dismissed: bool       = False
        self.runtime_state:        RuntimeState   = RuntimeState.OFFLINE
        self.runtime_state_detail: str            = "Connecting..."
        self.last_failed_action:   Optional[Callable[[], None]] = None
        self.last_failed_action_label: Optional[str] = None

        # ── Agent ────────────────────────────────────────────────────────────
        self.status:            AgentStatus          = AgentStatus.IDLE
        self.active_model:      str                  = "default"
        self.active_session_id: Optional[str]        = None
        self.pending_approval:  Optional[ApprovalRequest] = None
        self.active_provider:   str                  = ""     # openai | anthropic | gemini | …

        # ── Workspace / git ──────────────────────────────────────────────────
        # Populated at boot by detecting the local working directory and repo.
        self.workspace_path: str           = ""    # short label, e.g. "my-project"
        self.git_branch:     str           = ""    # e.g. "main", "feat/xyz"
        self.git_dirty:      bool          = False # True if there are uncommitted changes

        # ── Conversation ─────────────────────────────────────────────────────
        self.messages:       List[Message] = []
        self.context:        ContextUsage  = ContextUsage()
        # Files explicitly added to context (shown in ChatHeader context widget)
        self.context_files:  List[str]     = []

        # ── Session browser ──────────────────────────────────────────────────
        self.sessions:              List[Session] = []
        self.sessions_loading:      bool          = False
        self.sessions_error:        Optional[str] = None

        # ── Dashboard ────────────────────────────────────────────────────────
        self.dashboard_metrics:  DashboardMetrics  = DashboardMetrics()
        self.dashboard_loading:  bool              = False
        self.timeline:           List[TimelineEvent] = []

        # ── Memory ───────────────────────────────────────────────────────────
        self.memory_entries:  List[MemoryEntry] = []
        self.memory_query:    str               = ""
        self.memory_loading:  bool              = False
        self.memory_error:    Optional[str]     = None

        # ── Plugins ──────────────────────────────────────────────────────────
        self.plugins:         List[Plugin] = []
        self.plugins_loading: bool         = False
        self.plugins_error:   Optional[str] = None
        self.plugin_discovery_results: List[PluginCandidate] = []
        self.plugin_discovery_query: str = ""
        self.plugin_discovery_loading: bool = False
        self.plugin_discovery_error: Optional[str] = None
        self.plugin_inspection: Optional[Dict[str, Any]] = None
        self.plugin_inspection_error: Optional[str] = None
        self.plugin_inspection_selector: str = ""
        self.plugin_inspection_loading: bool = False
        self.plugin_operation: str = ""
        self.plugin_operation_error: Optional[str] = None
        self.plugin_operation_message: str = ""
        self.plugin_view: str = "saved"
        self.plugin_selection: str = ""
        self.plugin_discovery_filters: Dict[str, Any] = {
            "limit": "20", "language": "", "min_stars": "0", "include_archived": False,
        }

        # ── Skills ───────────────────────────────────────────────────────────
        self.skills:         List[Skill] = []
        self.skills_loading: bool        = False

        # ── Providers ────────────────────────────────────────────────────────
        self.providers:         List[Provider] = []
        self.providers_loading: bool           = False

        # ── Jobs ─────────────────────────────────────────────────────────────
        self.jobs: List[BackgroundJob] = []

        # ── Notifications ────────────────────────────────────────────────────
        self.notifications:        List[Notification] = []
        self.active_notification:  Optional[Notification] = None

        # ── UI state ─────────────────────────────────────────────────────────
        self.left_sidebar_open:  bool = True
        self.right_panel_open:   bool = False
        self.palette_open:       bool = False
        self.palette_query:      str  = ""
        self.error_banner:       Optional[str] = None

        # ── Interrupt / queue ────────────────────────────────────────────────
        self.queued_interrupt: bool = False   # True = let generation finish, send queued next

        # ── Global error ─────────────────────────────────────────────────────
        self.error_message: Optional[str] = None

    # ── Subscription system ───────────────────────────────────────────────────

    def subscribe(self, field: str, callback: Callable[[], None]) -> Callable:
        """Register callback for a field. Returns unsubscribe fn."""
        with self._lock:
            if field not in self._subscribers:
                self._subscribers[field] = []
            self._subscribers[field].append(callback)

        def _unsub() -> None:
            with self._lock:
                lst = self._subscribers.get(field, [])
                self._subscribers[field] = [c for c in lst if c is not callback]

        return _unsub

    def _notify(self, *fields: str) -> None:
        callbacks: List[Callable] = []
        with self._lock:
            for f in fields:
                callbacks.extend(self._subscribers.get(f, []))
        for cb in set(callbacks):
            try:
                cb()
            except Exception:
                pass

    # ── Navigation ────────────────────────────────────────────────────────────

    def navigate(self, screen: Screen) -> None:
        with self._lock:
            self.previous_screen = self.current_screen
            self.current_screen  = screen
            if screen != Screen.WELCOME:
                self.saved_screen = screen
                _save_persisted_screen(screen)
        self._notify("current_screen")

    def navigate_back(self) -> None:
        with self._lock:
            self.current_screen, self.previous_screen = (
                self.previous_screen, self.current_screen
            )
        self._notify("current_screen")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def set_user(self, email: Optional[str], plan: str = "Lite",
                 user_id: Optional[str] = None) -> None:
        with self._lock:
            self.user_email = email
            self.user_plan  = plan
            self.user_id    = user_id
        self._notify("user_email")

    def set_connected(self, connected: bool, latency_ms: Optional[int] = None) -> None:
        with self._lock:
            self.connected  = connected
            self.latency_ms = latency_ms
        self._notify("connected")

    def set_offline_mode(self, offline: bool) -> None:
        with self._lock:
            self.offline_mode = offline
            if offline:
                self.runtime_state = RuntimeState.OFFLINE
        self._notify("offline_mode")
        self._notify("runtime_state")


    # ── Agent / stream ────────────────────────────────────────────────────────

    def set_status(self, s: AgentStatus) -> None:
        with self._lock:
            self.status = s
        self._notify("status")

    def set_model(self, model: str) -> None:
        with self._lock:
            self.active_model = model
        self._notify("active_model")

    def set_session(self, session_id: Optional[str]) -> None:
        with self._lock:
            self.active_session_id = session_id
        self._notify("active_session_id")

    # ── Messages ──────────────────────────────────────────────────────────────

    def append_message(self, msg: Message) -> None:
        with self._lock:
            self.messages.append(msg)
        self._notify("messages")

    def update_last_assistant_message(self, delta: str) -> None:
        with self._lock:
            for m in reversed(self.messages):
                if m.role == "assistant" and m.is_streaming:
                    m.content += delta
                    break
        self._notify("messages")

    def append_last_reasoning_delta(self, delta: str) -> None:
        """Keep model reasoning separate from the user-visible answer."""
        with self._lock:
            for message in reversed(self.messages):
                if message.role == "assistant" and message.is_streaming:
                    current = str(message.metadata.get("reasoning", ""))
                    message.metadata["reasoning"] = current + delta
                    break
        self._notify("messages")

    def finalize_streaming_message(self, token_count: Optional[int] = None) -> None:
        with self._lock:
            for m in reversed(self.messages):
                if m.role == "assistant" and m.is_streaming:
                    m.is_streaming = False
                    if token_count:
                        m.token_count = token_count
                    break
        self._notify("messages")

    def clear_messages(self) -> None:
        with self._lock:
            self.messages = []
        self._notify("messages")

    def update_context(self, input_tokens: int, output_tokens: int,
                       limit: Optional[int] = None) -> None:
        with self._lock:
            self.context.input_tokens  = input_tokens
            self.context.output_tokens = output_tokens
            if limit:
                self.context.context_limit = limit
        self._notify("context")

    # ── Approval ──────────────────────────────────────────────────────────────

    def set_approval(self, req: Optional[ApprovalRequest]) -> None:
        with self._lock:
            self.pending_approval = req
        self._notify("pending_approval")

    # ── Sessions ──────────────────────────────────────────────────────────────

    def set_sessions(self, sessions: List[Session], error: Optional[str] = None) -> None:
        with self._lock:
            self.sessions        = sessions
            self.sessions_loading = False
            self.sessions_error  = error
        self._notify("sessions")

    def set_sessions_loading(self, loading: bool) -> None:
        with self._lock:
            self.sessions_loading = loading
        self._notify("sessions")

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def set_dashboard_metrics(self, m: DashboardMetrics) -> None:
        with self._lock:
            self.dashboard_metrics  = m
            self.dashboard_loading  = False
        self._notify("dashboard_metrics")

    def set_timeline(self, events: List[TimelineEvent]) -> None:
        with self._lock:
            self.timeline = events
        self._notify("timeline")

    # ── Memory ────────────────────────────────────────────────────────────────

    def set_memory(self, entries: List[MemoryEntry], error: Optional[str] = None) -> None:
        with self._lock:
            self.memory_entries = entries
            self.memory_loading = False
            self.memory_error   = error
        self._notify("memory_entries")

    def set_memory_loading(self, loading: bool) -> None:
        with self._lock:
            self.memory_loading = loading
        self._notify("memory_entries")

    # ── Plugins ───────────────────────────────────────────────────────────────

    def set_plugins(self, plugins: List[Plugin], error: Optional[str] = None) -> None:
        with self._lock:
            self.plugins         = plugins
            self.plugins_loading = False
            self.plugins_error   = error
        self._notify("plugins")

    def set_plugins_loading(self) -> None:
        with self._lock:
            self.plugins_loading = True
            self.plugins_error = None
        self._notify("plugins")

    def set_plugin_view(self, mode: str, selection: str = "") -> None:
        with self._lock:
            self.plugin_view = mode
            self.plugin_selection = selection

    def set_plugin_search_form(self, query: str, filters: Dict[str, Any]) -> None:
        with self._lock:
            self.plugin_discovery_query = query
            self.plugin_discovery_filters = dict(filters)

    def begin_plugin_operation(self, operation: str) -> bool:
        with self._lock:
            if self.plugin_operation:
                return False
            self.plugin_operation = operation
            self.plugin_operation_error = None
            self.plugin_operation_message = ""
        self._notify("plugin_operation")
        return True

    def finish_plugin_operation(self, message: str = "", error: Optional[str] = None) -> None:
        with self._lock:
            self.plugin_operation = ""
            self.plugin_operation_message = message
            self.plugin_operation_error = error
        self._notify("plugin_operation")

    def set_plugin_discovery(
        self,
        results: List[PluginCandidate],
        query: str = "",
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            self.plugin_discovery_results = results
            self.plugin_discovery_query = query
            self.plugin_discovery_loading = False
            self.plugin_discovery_error = error
        self._notify("plugin_discovery")

    def set_plugin_discovery_loading(self, loading: bool, query: Optional[str] = None) -> None:
        with self._lock:
            self.plugin_discovery_loading = loading
            if query is not None:
                self.plugin_discovery_query = query
            if loading:
                self.plugin_discovery_error = None
        self._notify("plugin_discovery")

    def set_plugin_inspection(
        self,
        result: Optional[Dict[str, Any]],
        error: Optional[str] = None,
        selector: str = "",
        loading: bool = False,
    ) -> None:
        with self._lock:
            self.plugin_inspection = result
            self.plugin_inspection_error = error
            self.plugin_inspection_selector = selector
            self.plugin_inspection_loading = loading
        self._notify("plugin_inspection")

    # ── Skills ────────────────────────────────────────────────────────────────

    def set_skills(self, skills: List[Skill]) -> None:
        with self._lock:
            self.skills         = skills
            self.skills_loading = False
        self._notify("skills")

    # ── Providers ─────────────────────────────────────────────────────────────

    def set_providers(self, providers: List[Provider]) -> None:
        with self._lock:
            self.providers         = providers
            self.providers_loading = False
        self._notify("providers")

    # ── Jobs ──────────────────────────────────────────────────────────────────

    def set_jobs(self, jobs: List[BackgroundJob]) -> None:
        with self._lock:
            self.jobs = jobs
        self._notify("jobs")

    def upsert_job(self, job: BackgroundJob) -> None:
        with self._lock:
            for i, j in enumerate(self.jobs):
                if j.id == job.id:
                    self.jobs[i] = job
                    break
            else:
                self.jobs.insert(0, job)
        self._notify("jobs")

    # ── Notifications ─────────────────────────────────────────────────────────

    def push_notification(
        self, message: str, level: "NotifLevel | str" = NotifLevel.INFO
    ) -> None:
        if isinstance(level, str):
            level = {
                "info": NotifLevel.INFO,
                "success": NotifLevel.SUCCESS,
                "warning": NotifLevel.WARNING,
                "error": NotifLevel.ERROR,
            }.get(level.lower(), NotifLevel.INFO)
        notif = Notification(id=uuid.uuid4().hex[:8], message=message, level=level)
        with self._lock:
            self.notifications.insert(0, notif)
            self.active_notification = notif
        self._notify("notifications", "active_notification")

    def dismiss_notification(self) -> None:
        with self._lock:
            self.active_notification = None
        self._notify("active_notification")

    # ── UI toggles ────────────────────────────────────────────────────────────

    def toggle_sidebar(self) -> None:
        with self._lock:
            self.left_sidebar_open = not self.left_sidebar_open
        self._notify("left_sidebar_open")

    def toggle_right_panel(self) -> None:
        with self._lock:
            self.right_panel_open = not self.right_panel_open
        self._notify("right_panel_open")

    def set_palette(self, open: bool, query: str = "") -> None:
        with self._lock:
            self.palette_open  = open
            self.palette_query = query
        self._notify("palette_open")

    # ── Interrupt / queue ─────────────────────────────────────────────────────

    def set_queued_interrupt(self, queued: bool) -> None:
        """Set whether the next submit should wait for current generation."""
        with self._lock:
            self.queued_interrupt = queued
        self._notify("status")

    # ── Errors ────────────────────────────────────────────────────────────────

    def set_error(self, msg: Optional[str]) -> None:
        with self._lock:
            self.error_message = msg
        self._notify("error_message")

    def set_error_banner(self, msg: Optional[str]) -> None:
        with self._lock:
            self.error_banner = msg
        self._notify("error_banner")

    def set_runtime_state(self, rs: RuntimeState, detail: str = "") -> None:
        with self._lock:
            self.runtime_state = rs
            self.runtime_state_detail = detail
            self.connected = (rs == RuntimeState.ONLINE)
            if not self.offline_mode or rs == RuntimeState.OFFLINE:
                self.offline_mode = (rs == RuntimeState.OFFLINE)
        self._notify("runtime_state")
        self._notify("connected")


    def set_failed_action(self, label: Optional[str], retry_fn: Optional[Callable[[], None]]) -> None:
        with self._lock:
            self.last_failed_action_label = label
            self.last_failed_action = retry_fn
        self._notify("last_failed_action")

    # ── Workspace / git ───────────────────────────────────────────────────────

    def set_workspace_info(
        self,
        workspace_path: str = "",
        git_branch: str = "",
        git_dirty: bool = False,
    ) -> None:
        """Set local workspace and git metadata. Called once at boot."""
        with self._lock:
            self.workspace_path = workspace_path
            self.git_branch     = git_branch
            self.git_dirty      = git_dirty
        self._notify("workspace_info")

    def set_provider(self, provider: str) -> None:
        """Set the active AI provider label (e.g. 'openai', 'anthropic')."""
        with self._lock:
            self.active_provider = provider
        self._notify("active_provider")

    def add_context_file(self, path: str) -> None:
        """Add a file to the active context file list."""
        with self._lock:
            if path not in self.context_files:
                self.context_files.append(path)
        self._notify("context_files")

    def remove_context_file(self, path: str) -> None:
        """Remove a file from the active context file list."""
        with self._lock:
            self.context_files = [f for f in self.context_files if f != path]
        self._notify("context_files")

    def clear_context_files(self) -> None:
        """Remove all context files (e.g. on /clear or new session)."""
        with self._lock:
            self.context_files = []
        self._notify("context_files")


# Module-level singleton
state = TUIState()
