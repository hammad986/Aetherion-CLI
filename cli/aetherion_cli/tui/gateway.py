"""
aetherion_cli/tui/gateway.py
Aetherion TUI Gateway — complete async bridge to the unified backend.

Owns all network I/O. The UI thread never touches HTTP.

Supports:
- SSE agent streaming with reconnect budget (3 retries, exponential backoff)
- Health polling every 30s with latency measurement
- Dashboard metrics, timeline, session browser
- Memory search/list, plugin marketplace, skills, providers
- Background jobs, notifications stream
- Git status, scheduler tasks
- Crash hooks writing to ~/.aetherion/logs/tui_crash.log
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional

import httpx
from rich.markup import escape as escape_markup

from ..config import get_server_url, get_token
from ..plugin_manager import PluginCatalogManager, PluginManagerError
from .state import (
    AgentStatus,
    ApprovalRequest,
    BackgroundJob,
    DashboardMetrics,
    MemoryEntry,
    Message,
    NotifLevel,
    Notification,
    Plugin,
    PluginCandidate,
    Provider,
    RuntimeState,
    Session,
    Screen,
    Skill,
    TimelineEvent,
    state,
)


def _safe_notification(value: object) -> str:
    """Keep provider/cache errors literal and single-line in TUI notices."""
    text = str(value)
    safe_chars: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char == "\n":
            safe_chars.append("\\n")
        elif char == "\r":
            safe_chars.append("\\r")
        elif char == "\t":
            safe_chars.append("\\t")
        elif codepoint < 0x20 or codepoint == 0x7F or 0x80 <= codepoint <= 0x9F:
            safe_chars.append(f"\\x{codepoint:02x}")
        else:
            safe_chars.append(char)
    return escape_markup("".join(safe_chars))

log = logging.getLogger("aetherion.tui.gateway")

def classify_network_error(exc_or_resp: Any) -> tuple[RuntimeState, str]:
    """
    Classify an exception or HTTP response into (RuntimeState, user_facing_explanation).
    Never exposes raw OS socket/DNS traces (e.g. Errno 11001) as primary UX.
    """
    if isinstance(exc_or_resp, httpx.Response):
        status = exc_or_resp.status_code
        if status == 401:
            return RuntimeState.AUTH_REQUIRED, "Authentication required or token expired. Please log in."
        elif status == 403:
            return RuntimeState.AUTH_REQUIRED, "Access forbidden. Your account lacks permission for this action."
        elif status in (500, 502, 503, 504):
            return RuntimeState.ERROR, f"Server error (HTTP {status}). The service is currently degraded or unavailable."
        else:
            return RuntimeState.DEGRADED, f"Server returned unexpected HTTP status {status}."

    exc = exc_or_resp
    err_str = str(exc).lower()

    # DNS resolution failure (Windows Errno 11001 or socket.gaierror)
    if "11001" in err_str or "getaddrinfo" in err_str or "nameresolutionerror" in err_str:
        return RuntimeState.OFFLINE, "Unable to resolve server host. Backend may be offline or DNS is unreachable."

    # Connection refused / Connect timeout / Connect error
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) or "connection refused" in err_str or "connecterror" in err_str or "10061" in err_str:
        return RuntimeState.OFFLINE, "Cannot connect to server. Ensure the Aetherion backend is running on the configured port."

    # Read / Write timeout
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)) or "timeout" in err_str:
        return RuntimeState.DEGRADED, "Request timed out waiting for server response."

    # HTTPStatusError
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return RuntimeState.AUTH_REQUIRED, "Authentication required or token expired. Please log in."
        elif status == 403:
            return RuntimeState.AUTH_REQUIRED, "Access forbidden. Insufficient permissions."
        elif status in (500, 502, 503, 504):
            return RuntimeState.ERROR, f"Server error (HTTP {status})."
        return RuntimeState.DEGRADED, f"HTTP error {status}."

    # General network error
    if isinstance(exc, httpx.NetworkError):
        return RuntimeState.DEGRADED, "Network communication error. Check your connection."

    return RuntimeState.ERROR, f"Communication error: {str(exc)[:120]}"

# ── Crash logging ─────────────────────────────────────────────────────────────
_CRASH_LOG = Path.home() / ".aetherion" / "logs" / "tui_crash.log"


def _install_crash_hooks() -> None:
    orig_hook = sys.excepthook

    def _panic(exc_type, exc_val, exc_tb):
        import traceback
        trace = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        try:
            _CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _CRASH_LOG.open("a", encoding="utf-8") as f:
                f.write(f"\n=== crash · {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n{trace}")
        except Exception:
            pass
        first = str(exc_val).strip().splitlines()[0] if str(exc_val).strip() else exc_type.__name__
        state.set_error(f"[crash] {exc_type.__name__}: {first}")
        orig_hook(exc_type, exc_val, exc_tb)

    sys.excepthook = _panic

    orig_thread_hook = getattr(threading, "excepthook", None)

    def _thread_panic(args):
        import traceback
        trace = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        try:
            _CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _CRASH_LOG.open("a", encoding="utf-8") as f:
                f.write(f"\n=== thread crash · {time.strftime('%Y-%m-%dT%H:%M:%S')} "
                        f"· {args.thread.name} ===\n{trace}")
        except Exception:
            pass
        first = (str(args.exc_value).strip().splitlines()[0]
                 if str(args.exc_value).strip() else args.exc_type.__name__)
        state.set_error(f"[thread] {args.exc_type.__name__}: {first}")
        if orig_thread_hook:
            orig_thread_hook(args)

    threading.excepthook = _thread_panic


# ── Constants ─────────────────────────────────────────────────────────────────
CONNECT_TIMEOUT  = 8.0
READ_TIMEOUT     = 900.0
STREAM_TIMEOUT   = 1200.0
HEALTH_INTERVAL  = 30.0
RECONNECT_BACKOFF = [2.0, 4.0, 8.0]
MAX_RETRIES       = 3


def _daemon(target: Callable, name: str, *args, **kwargs) -> threading.Thread:
    t = threading.Thread(target=target, args=args, kwargs=kwargs,
                         daemon=True, name=name)
    t.start()
    return t


class Gateway:
    """Single gateway instance for the TUI process."""

    def __init__(self) -> None:
        self.base_url       = get_server_url().rstrip("/")
        self._lock          = threading.RLock()
        self._token: Optional[str] = get_token()
        self._cancel_stream = threading.Event()
        self._started       = False
        self._plugin_discovery_generation = 0

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        _install_crash_hooks()
        _daemon(self._health_loop, "aetherion-health")
        _daemon(self._notifications_loop, "aetherion-notifications")

    def refresh_token(self) -> None:
        """Reload access token from keyring/config into memory."""
        with self._lock:
            self._token = get_token()

    # ── Device Authorization Flow ─────────────────────────────────────────────

    def device_request(self, action: str = "login") -> dict:
        """
        Request a device code from the backend.
        Returns {device_code, user_code, verification_uri, expires_in, interval}.
        Synchronous — called from a Textual worker thread.
        """
        try:
            with self._client(read=15.0) as http:
                r = http.post(
                    self._url("/api/auth/device/request"),
                    json={"action": action},
                    headers={"Content-Type": "application/json", "User-Agent": "aetherion-tui/2.0"},
                )
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            return {"error": str(exc)}

    def device_poll(self, device_code: str) -> dict:
        """
        Poll authorization status.
        Returns {status, access_token?, refresh_token?, user?}.
        status in: pending | authorized | denied | expired
        """
        try:
            with self._client(read=10.0) as http:
                r = http.get(
                    self._url("/api/auth/device/poll"),
                    params={"device_code": device_code},
                    headers={"User-Agent": "aetherion-tui/2.0"},
                )
                if r.status_code == 400:
                    try:
                        err_body = r.json()
                        err_code = err_body.get("error", "")
                        if err_code in ("authorization_pending", "slow_down"):
                            return {"status": "pending"}
                    except Exception:
                        pass
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def complete_device_auth(self, access_token: str, refresh_token: str, user: dict) -> None:
        """
        Called after successful device poll — persists tokens and updates state.
        """
        from ..config import set_token, set_refresh_token
        set_token(access_token)
        if refresh_token:
            set_refresh_token(refresh_token)
        with self._lock:
            self._token = access_token
        state.set_user(
            email=user.get("email", ""),
            plan=user.get("plan", "Lite"),
            user_id=str(user.get("id", "")),
        )
        state.set_connected(True)

    def silent_login(self) -> bool:
        """
        Attempt silent re-authentication using stored access token or refresh token.
        Returns True if authenticated, False otherwise.
        Called at TUI startup before deciding which screen to show.
        """
        from ..config import get_token, get_refresh_token, set_token, set_refresh_token

        # 1. First attempt: check if existing access token is still valid
        token = get_token()
        token_is_expired = False
        if token and token.count(".") == 2:
            try:
                import base64
                payload_part = token.split(".")[1]
                padded = payload_part + "=" * (-len(payload_part) % 4)
                claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
                exp = claims.get("exp")
                if exp is not None and isinstance(exp, (int, float)) and exp <= time.time():
                    token_is_expired = True
            except Exception:
                pass

        if token and not token_is_expired:
            with self._lock:
                self._token = token
            try:
                with self._client(read=10.0) as http:
                    r = http.get(
                        self._url("/api/auth/me"),
                        headers={"Authorization": f"Bearer {token}", "User-Agent": "aetherion-tui/2.0"},
                    )
                    if r.is_success:
                        data = r.json()
                        user = data.get("user") or data.get("data") or data
                        state.set_user(
                            email=user.get("email", ""),
                            plan=user.get("plan", "Lite"),
                            user_id=str(user.get("id", user.get("user_id", ""))),
                        )
                        state.set_connected(True)
                        return True
            except Exception as exc:
                log.debug("Silent token verification failed: %s", exc)

        # 2. Second attempt: refresh token
        rt = get_refresh_token()
        if not rt:
            return False
        try:
            with self._client(read=15.0) as http:
                r = http.post(
                    self._url("/api/auth/refresh"),
                    json={"refresh_token": rt},
                    headers={"Content-Type": "application/json", "User-Agent": "aetherion-tui/2.0"},
                )
                if r.status_code in (401, 403):
                    from ..config import clear_token, clear_refresh_token
                    clear_token()
                    clear_refresh_token()
                    return False
                r.raise_for_status()
                data = r.json()
                new_at = data.get("access_token")
                new_rt = data.get("refresh_token", rt)  # rolling refresh
                if not new_at:
                    return False
                set_token(new_at)
                set_refresh_token(new_rt)
                with self._lock:
                    self._token = new_at
                user = data.get("user") or data.get("data") or data
                state.set_user(
                    email=user.get("email", ""),
                    plan=user.get("plan", "Lite"),
                    user_id=str(user.get("id", user.get("user_id", ""))),
                )
                state.set_connected(True)
                return True
        except Exception as exc:
            log.debug("Silent login failed: %s", exc)
            return False

    def logout(self) -> None:
        """Revoke current session server-side and clear local credentials."""
        def _do():
            try:
                self._post("/api/auth/logout", {})
            except Exception:
                pass
            from ..config import clear_token, clear_refresh_token
            clear_token()
            clear_refresh_token()
            with self._lock:
                self._token = None
            state.set_user(None)
            state.navigate(Screen.WELCOME)
            state.push_notification("Logged out", NotifLevel.SUCCESS)
        _daemon(_do, "aetherion-logout")

    def logout_everywhere(self) -> None:
        """Revoke all sessions server-side and clear local credentials."""
        def _do():
            try:
                self._post("/api/auth/logout_everywhere", {})
            except Exception:
                pass
            from ..config import clear_token, clear_refresh_token
            clear_token()
            clear_refresh_token()
            with self._lock:
                self._token = None
            state.set_user(None)
            state.navigate(Screen.WELCOME)
            state.push_notification("Logged out from all devices", NotifLevel.SUCCESS)
        _daemon(_do, "aetherion-logout")

    def change_password(
        self,
        current_password: str,
        new_password: str,
        on_result: Optional[Callable[[object], None]] = None,
    ) -> None:
        """Change the authenticated account password through the backend API."""
        def _do() -> None:
            result: object
            try:
                result = self._post(
                    "/api/auth/change-password",
                    {"old_password": current_password, "new_password": new_password},
                )
                if not isinstance(result, dict):
                    result = {"ok": True, "message": str(result)}
            except httpx.HTTPStatusError as exc:
                try:
                    payload = exc.response.json()
                except Exception:
                    payload = {}
                result = payload if isinstance(payload, dict) else {
                    "ok": False,
                    "error": classify_network_error(exc)[1],
                }
            except Exception as exc:
                result = {"ok": False, "error": classify_network_error(exc)[1]}

            if on_result:
                try:
                    on_result(result)
                except Exception:
                    log.exception("Password change result callback failed")

        _daemon(_do, "aetherion-change-password")

    # ── Internal HTTP helpers ──────────────────────────────────────────────────

    def _headers(self, stream: bool = False) -> Dict[str, str]:
        with self._lock:
            token = self._token
        if not token:
            from ..config import get_token
            token = get_token()
            if token:
                with self._lock:
                    self._token = token
        h: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "aetherion-tui/2.0",
        }
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _client(self, read: float = READ_TIMEOUT) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT, read=read, write=30.0, pool=5.0
        ))

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        if state.offline_mode:
            return {}
        with self._client() as http:
            r = http.get(self._url(path), params=params, headers=self._headers())
            if r.status_code == 401 and state.current_screen != Screen.WELCOME:
                state.set_user(None)
                state.navigate(Screen.WELCOME)
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, body: Optional[dict] = None) -> Any:
        if state.offline_mode:
            return {}
        with self._client() as http:
            r = http.post(self._url(path), json=body or {}, headers=self._headers())
            if r.status_code == 401 and state.current_screen != Screen.WELCOME:
                state.set_user(None)
                state.navigate(Screen.WELCOME)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            return r.json() if "application/json" in ct else r.text

    def _put(self, path: str, body: Optional[dict] = None) -> Any:
        if state.offline_mode:
            return {}
        with self._client() as http:
            r = http.put(self._url(path), json=body or {}, headers=self._headers())
            if r.status_code == 401 and state.current_screen != Screen.WELCOME:
                state.set_user(None)
                state.navigate(Screen.WELCOME)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            return r.json() if "application/json" in ct else r.text

    def _delete(self, path: str) -> Any:
        if state.offline_mode:
            return {}
        with self._client() as http:
            r = http.delete(self._url(path), headers=self._headers())
            if r.status_code == 401 and state.current_screen != Screen.WELCOME:
                state.set_user(None)
                state.navigate(Screen.WELCOME)
            r.raise_for_status()
            return r.json() if r.content else {}

    def _sse_stream(self, r: httpx.Response) -> Generator[dict, None, None]:
        """Parse SSE response into event dicts."""
        buf = ""
        parts: List[str] = []
        last_event_id = ""

        def _decode_event(data_lines: List[str]) -> dict:
            """Decode one SSE event after joining its data fields per the SSE spec."""
            payload = "\n".join(data_lines)
            try:
                event = json.loads(payload)
                if last_event_id and isinstance(event, dict):
                    event.setdefault("_last_id", last_event_id)
                return event
            except json.JSONDecodeError:
                return {"type": "raw", "text": payload}

        for chunk in r.iter_text():
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")
                if line == "":
                    if parts:
                        yield _decode_event(parts)
                    parts = []
                elif line.startswith("id:"):
                    last_event_id = line[3:].strip()
                elif line.startswith("data:"):
                    payload = line[5:].lstrip(" ")
                    if payload and payload != "[DONE]":
                        parts.append(payload)
        if parts:
            yield _decode_event(parts)

    # ── Health polling ─────────────────────────────────────────────────────────

    def _health_loop(self) -> None:
        while True:
            try:
                t0 = time.monotonic()
                with self._client(read=10.0) as http:
                    r = http.get(self._url("/api/health"), headers=self._headers())
                lat = int((time.monotonic() - t0) * 1000)
                if r.is_success:
                    data = r.json()
                    state.set_connected(True, lat)
                    state.set_runtime_state(RuntimeState.ONLINE, f"Healthy · {lat}ms")
                    state.set_error(None)
                    # Try to pull user info from health response
                    if data.get("user"):
                        state.set_user(
                            email=data["user"].get("email"),
                            plan=data["user"].get("plan", "Lite"),
                            user_id=str(data["user"].get("id", "")),
                        )
                else:
                    rs, detail = classify_network_error(r)
                    state.set_runtime_state(rs, detail)
                    state.set_connected(False)
            except Exception as exc:
                rs, detail = classify_network_error(exc)
                state.set_runtime_state(rs, detail)
                state.set_connected(False)
                log.debug("Health check failed: %s", exc)
            time.sleep(HEALTH_INTERVAL)

    # ── Notifications SSE ──────────────────────────────────────────────────────

    def _notifications_loop(self) -> None:
        """Subscribe to /api/notifications/stream and push to state."""
        while True:
            try:
                with self._client(read=STREAM_TIMEOUT) as http:
                    with http.stream("GET", self._url("/api/notifications/stream"),
                                     headers=self._headers(stream=True)) as r:
                        if not r.is_success:
                            time.sleep(30)
                            continue
                        for event in self._sse_stream(r):
                            msg = event.get("message", "")
                            level_str = event.get("level", "info")
                            level = {
                                "success": NotifLevel.SUCCESS,
                                "warning": NotifLevel.WARNING,
                                "error": NotifLevel.ERROR,
                            }.get(level_str, NotifLevel.INFO)
                            if msg:
                                state.push_notification(msg, level)
            except Exception as exc:
                log.debug("Notifications stream disconnected: %s", exc)
            time.sleep(10)

    # ── Agent streaming ────────────────────────────────────────────────────────

    def start_stream(self, task: str, session_id: Optional[str] = None,
                     model: Optional[str] = None) -> None:
        """Launch a background agent task. Non-blocking."""
        with self._lock:
            self._cancel_stream.set()
            cancel_event = threading.Event()
            self._cancel_stream = cancel_event
        sid = session_id or state.active_session_id or f"tui-{uuid.uuid4().hex[:12]}"
        
        # Ensure local SQLite session exists and persist the user task
        self._ensure_local_session(sid, model or state.active_model)
        self._persist_message(sid, "user", task)
        
        state.set_session(sid)
        state.set_status(AgentStatus.RUNNING)
        state.set_error(None)
        # streaming placeholder
        state.append_message(Message.assistant("", streaming=True))
        _daemon(self._stream_worker, "aetherion-stream",
                task, sid, model or state.active_model, cancel_event)

    def cancel_stream(self) -> None:
        with self._lock:
            self._cancel_stream.set()

    def _execute_local_provider_stream(
        self,
        task: str,
        session_id: str,
        model: str,
        cancel_event: threading.Event,
    ) -> None:
        """Stream directly from user-configured BYOK provider when backend execution is disabled or offline."""
        active_p = None
        for p in getattr(state, "providers", []):
            if getattr(p, "is_default", False) and getattr(p, "is_enabled", True):
                active_p = p
                break
        if not active_p and getattr(state, "providers", []):
            for p in state.providers:
                if getattr(p, "is_enabled", True):
                    active_p = p
                    break
        if not active_p and getattr(state, "providers", []):
            active_p = state.providers[0]

        if not active_p or not getattr(active_p, "base_url", ""):
            state.set_error("No local BYOK provider configured. Please go to Providers screen to set up a provider.")
            state.finalize_streaming_message()
            state.set_status(AgentStatus.ERROR)
            return

        base_url = active_p.base_url.strip().rstrip("/")
        model_name = getattr(active_p, "model", "") or model or state.active_model or "gpt-4o"
        ptype = getattr(active_p, "provider_type", "OPENAI_COMPATIBLE").upper()

        # Resolve unmasked raw API key
        api_key = getattr(active_p, "api_key_raw", "")
        if not api_key or "..." in api_key or "…" in api_key:
            try:
                from ..session_store import load_local_providers
                local_data = load_local_providers()
                for lp in local_data:
                    if lp.get("id") == getattr(active_p, "id", "") or lp.get("name") == getattr(active_p, "name", ""):
                        raw_candidate = lp.get("api_key") or ""
                        if raw_candidate and "..." not in raw_candidate and "…" not in raw_candidate:
                            api_key = raw_candidate
                            break
            except Exception:
                pass

        # Check if key is missing
        if not api_key or "..." in api_key or "…" in api_key:
            state.set_error("No valid unmasked API key found. Please edit the provider in Providers screen and enter your API key.")
            state.finalize_streaming_message()
            state.set_status(AgentStatus.ERROR)
            return

        # Resolve endpoint URL intelligently
        if ptype == "ANTHROPIC_COMPATIBLE":
            if base_url.endswith("/messages"):
                chat_url = base_url
            elif base_url.endswith("/v1"):
                chat_url = f"{base_url}/messages"
            else:
                chat_url = f"{base_url}/v1/messages"
        else:
            if base_url.endswith("/chat/completions"):
                chat_url = base_url
            elif base_url.endswith("/v1") or "/v1/" in base_url:
                chat_url = f"{base_url}/chat/completions"
            else:
                chat_url = f"{base_url}/v1/chat/completions"

        # Build messages payload with system prompt & memories
        system_parts = ["You are Aetherion, an advanced AI coding assistant and terminal companion."]
        try:
            from ..session_store import list_local_memories
            mems = list_local_memories(limit=8)
            if mems:
                mem_lines = "\n".join(f"- {m['content']}" for m in mems if m.get("content"))
                system_parts.append(f"User Long-Term Memories:\n{mem_lines}")
        except Exception:
            pass

        messages_for_llm = []
        if ptype != "ANTHROPIC_COMPATIBLE":
            messages_for_llm.append({"role": "system", "content": "\n\n".join(system_parts)})

        try:
            from ..session_store import get_messages
            past_msgs = get_messages(session_id, limit=20)
            for pm in past_msgs:
                r = pm.get("role")
                c = pm.get("content")
                if r in ("user", "assistant") and c:
                    messages_for_llm.append({"role": r, "content": c})
        except Exception:
            pass

        # Ensure current task is the latest user turn
        if not messages_for_llm or messages_for_llm[-1].get("role") != "user" or messages_for_llm[-1].get("content") != task:
            messages_for_llm.append({"role": "user", "content": task})

        try:
            state.set_status(AgentStatus.STREAMING)

            if ptype == "ANTHROPIC_COMPATIBLE":
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                }
                body = {
                    "model": model_name,
                    "system": "\n\n".join(system_parts),
                    "messages": [m for m in messages_for_llm if m.get("role") in ("user", "assistant")],
                    "max_tokens": 4096,
                    "stream": True,
                }
            else:
                headers = {
                    "Authorization": f"Bearer {api_key}" if api_key else "",
                    "Content-Type": "application/json",
                }
                body = {
                    "model": model_name,
                    "messages": messages_for_llm,
                    "stream": True,
                }

            received_any = False
            with httpx.Client(timeout=90.0) as client:
                with client.stream("POST", chat_url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        err_text = resp.read().decode("utf-8", errors="ignore")[:300]
                        try:
                            err_json = json.loads(err_text)
                            err_msg = (
                                err_json.get("error", {}).get("message")
                                or err_json.get("message")
                                or err_text
                            )
                        except Exception:
                            err_msg = err_text
                        state.set_error(f"Provider Error {resp.status_code}: {err_msg}")
                        state.finalize_streaming_message()
                        state.set_status(AgentStatus.ERROR)
                        return

                    content_type = resp.headers.get("content-type", "").lower()
                    if "text/html" in content_type:
                        state.set_error(f"Provider returned an HTML page (not API response). Check Base URL '{base_url}' (e.g. '{base_url}/v1').")
                        state.finalize_streaming_message()
                        state.set_status(AgentStatus.ERROR)
                        return

                    if "application/json" in content_type:
                        raw_body = resp.read().decode("utf-8", errors="ignore")
                        try:
                            data = json.loads(raw_body)
                            if "error" in data:
                                err_msg = data.get("error", {}).get("message") or str(data["error"])
                                state.set_error(f"Provider Error: {err_msg}")
                                state.finalize_streaming_message()
                                state.set_status(AgentStatus.ERROR)
                                return
                            choices = data.get("choices", [])
                            if choices:
                                text = choices[0].get("message", {}).get("content") or choices[0].get("text") or ""
                                if text:
                                    state.update_last_assistant_message(text)
                                    received_any = True
                            elif data.get("content"):
                                text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                                if text:
                                    state.update_last_assistant_message(text)
                                    received_any = True
                        except Exception as exc:
                            state.set_error(f"Failed to parse JSON response: {exc}")
                            state.finalize_streaming_message()
                            state.set_status(AgentStatus.ERROR)
                            return
                    else:
                        for line in resp.iter_lines():
                            if cancel_event.is_set():
                                break
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("data:"):
                                raw = line[5:].strip()
                                if raw == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(raw)
                                    if "error" in chunk:
                                        err_msg = chunk.get("error", {}).get("message") or str(chunk["error"])
                                        state.set_error(f"Provider Error: {err_msg}")
                                        break
                                    choices = chunk.get("choices", [])
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        text = (
                                            delta.get("content")
                                            or delta.get("reasoning_content")
                                            or choices[0].get("text")
                                            or ""
                                        )
                                        if text:
                                            state.update_last_assistant_message(text)
                                            received_any = True
                                    elif chunk.get("type") == "content_block_delta":
                                        text = chunk.get("delta", {}).get("text", "")
                                        if text:
                                            state.update_last_assistant_message(text)
                                            received_any = True
                                except Exception:
                                    pass

            if cancel_event.is_set():
                state.finalize_streaming_message()
                state.set_status(AgentStatus.CANCELLED)
                return

            if not received_any:
                state.set_error("Provider returned empty response. Verify model identifier, base URL, and API key permissions.")
                state.finalize_streaming_message()
                state.set_status(AgentStatus.ERROR)
                return

            state.finalize_streaming_message()
            state.set_status(AgentStatus.DONE)
            try:
                for m in reversed(state.messages):
                    if m.role == "assistant" and not m.is_streaming:
                        self._persist_message(session_id, "assistant", m.content)
                        break
            except Exception:
                pass
        except Exception as exc:
            state.set_error(f"Provider streaming failed: {exc}")
            state.finalize_streaming_message()
            state.set_status(AgentStatus.ERROR)

    def _stream_worker(
        self,
        task: str,
        session_id: str,
        model: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        cancel_event = cancel_event or self._cancel_stream
        if getattr(state, "offline_mode", False):
            self._execute_local_provider_stream(task, session_id, model, cancel_event)
            return

        payload = {
            "task": task,
            "session_id": session_id,
            "model": model,
        }
        url = self._url("/api/v1/agent/invoke")
        retries = 0
        last_event_id = ""

        while retries <= MAX_RETRIES:
            if cancel_event.is_set():
                state.finalize_streaming_message()
                state.set_status(AgentStatus.IDLE)
                return
            try:
                terminal_event = False
                headers = self._headers(stream=True)
                if last_event_id:
                    headers["Last-Event-ID"] = last_event_id

                with self._client(read=STREAM_TIMEOUT) as http:
                    with http.stream("POST", url, json=payload,
                                     headers=headers) as r:
                        if not r.is_success:
                            r.read()
                            body = ""
                            try:
                                body = r.json().get("error", r.text[:200])
                            except Exception:
                                body = r.text[:200]
                            if r.status_code == 403 and "execution is disabled" in body.lower():
                                log.info("Backend execution is disabled; falling back to direct BYOK provider stream.")
                                self._execute_local_provider_stream(task, session_id, model, cancel_event)
                                return
                            state.set_error(f"HTTP {r.status_code}: {body}")
                            state.finalize_streaming_message()
                            state.set_status(AgentStatus.ERROR)
                            return

                        for event in self._sse_stream(r):
                            if cancel_event.is_set():
                                break
                            if event.get("_last_id"):
                                last_event_id = str(event["_last_id"])
                            self._handle_stream_event(event)
                            if event.get("type") in ("done", "error"):
                                terminal_event = True

                if cancel_event.is_set():
                    state.finalize_streaming_message()
                    state.set_status(AgentStatus.IDLE)
                    return

                if terminal_event:
                    if state.status not in (
                        AgentStatus.ERROR,
                        AgentStatus.WAITING_APPROVAL,
                    ):
                        state.set_status(AgentStatus.DONE)
                    self.list_local_sessions()
                    return

                raise httpx.RemoteProtocolError(
                    "SSE stream ended before a terminal event"
                )

            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as exc:
                if retries < MAX_RETRIES:
                    backoff = RECONNECT_BACKOFF[min(retries, len(RECONNECT_BACKOFF) - 1)]
                    log.warning("Stream disconnected, retry %d/%d in %.1fs", retries + 1, MAX_RETRIES, backoff)
                    retries += 1
                    if cancel_event.wait(backoff):
                        state.finalize_streaming_message()
                        state.set_status(AgentStatus.IDLE)
                        return
                    continue
                state.set_error(f"Connection lost after {MAX_RETRIES} retries: {exc}")
                state.finalize_streaming_message()
                state.set_status(AgentStatus.ERROR)
                return
            except Exception as exc:
                state.set_error(f"Stream error: {exc}")
                log.exception("Unexpected stream error")
                state.finalize_streaming_message()
                state.set_status(AgentStatus.ERROR)
                return

    def _handle_stream_event(self, event: dict) -> None:
        ev = event.get("type", "")

        if ev == "queued":
            if state.status not in (
                AgentStatus.STREAMING,
                AgentStatus.WAITING_APPROVAL,
            ):
                state.set_status(AgentStatus.RUNNING)

        elif ev in ("text", "delta"):
            state.update_last_assistant_message(event.get("text", ""))
            if state.status != AgentStatus.STREAMING:
                state.set_status(AgentStatus.STREAMING)

        elif ev in ("reasoning", "thought", "scratchpad", "reasoning_delta"):
            # Keep private model reasoning attached to the assistant row.  The
            # chat renderer shows it collapsed by default and exposes a
            # keyboard toggle without leaking it into the user-facing text.
            with state._lock:
                for message in reversed(state.messages):
                    if message.role == "assistant":
                        message.metadata["reasoning"] = (
                            message.metadata.get("reasoning", "")
                            + str(event.get("text", event.get("content", "")))
                        )
                        break
            state._notify("messages")

        elif ev == "raw":
            state.update_last_assistant_message(event.get("text", ""))

        elif ev == "tool_call":
            tool = event.get("tool", "unknown")
            args_summary = str(event.get("args", ""))[:80]
            state.append_message(Message.tool_call(tool, f"{tool}({args_summary})"))

        elif ev == "state_change":
            agent_state = event.get("agent_state", "")
            if agent_state:
                state.append_message(Message.system(f"● {agent_state}"))

        elif ev == "usage":
            in_tok  = event.get("inputTokens", 0) or event.get("input_tokens", 0)
            out_tok = event.get("outputTokens", 0) or event.get("output_tokens", 0)
            state.update_context(in_tok, out_tok)

        elif ev == "approval":
            req = ApprovalRequest(
                request_id  = event.get("request_id", uuid.uuid4().hex[:8]),
                tool_name   = event.get("tool", "unknown"),
                description = event.get("description", "Agent requests permission"),
                risk_level  = event.get("risk", "low"),
                details     = event.get("details", {}),
            )
            state.set_approval(req)
            state.set_status(AgentStatus.WAITING_APPROVAL)

        elif ev == "job":
            # Background job update
            job = BackgroundJob(
                id      = event.get("job_id", uuid.uuid4().hex[:8]),
                name    = event.get("name", "Background task"),
                status  = event.get("status", "running"),
                progress= event.get("progress", 0),
            )
            state.upsert_job(job)

        elif ev == "notification":
            level_str = event.get("level", "info")
            level = {
                "success": NotifLevel.SUCCESS,
                "warning": NotifLevel.WARNING,
                "error":   NotifLevel.ERROR,
            }.get(level_str, NotifLevel.INFO)
            state.push_notification(event.get("message", ""), level)

        elif ev == "error":
            state.set_error(event.get("error", "Unknown agent error"))
            state.finalize_streaming_message()
            state.set_status(AgentStatus.ERROR)

        elif ev == "done":
            in_tok  = event.get("inputTokens", 0) or event.get("input_tokens", 0)
            out_tok = event.get("outputTokens", 0) or event.get("output_tokens", 0)
            summary = event.get("summary", "")
            if in_tok or out_tok:
                state.update_context(in_tok, out_tok)

            with state._lock:
                for m in reversed(state.messages):
                    if m.role == "assistant":
                        if not m.content.strip() and summary:
                            m.content = summary
                        break

            state.finalize_streaming_message(token_count=out_tok)
            
            # Persist completed assistant message
            with state._lock:
                for m in reversed(state.messages):
                    if m.role == "assistant" and not m.is_streaming:
                        if state.active_session_id:
                            self._persist_message(state.active_session_id, "assistant", m.content, tokens=out_tok)
                        break
                        
            status = event.get("status", "done")
            failure_class = event.get("failure_class", "")
            ver_status = event.get("verification", "")
            err_msg = event.get("error")

            if status in ("error", "failed") or failure_class in ("budget_exhausted", "verifier_failure", "blocked"):
                if failure_class == "budget_exhausted":
                    err_msg = err_msg or "Task halted: Execution budget exhausted."
                    state.set_error(err_msg)
                    state.set_status(AgentStatus.BUDGET_EXHAUSTED)
                elif failure_class == "blocked" or status == "blocked":
                    err_msg = err_msg or "Task blocked: Policy or permission constraint."
                    state.set_error(err_msg)
                    state.set_status(AgentStatus.BLOCKED)
                elif failure_class == "verifier_failure" or ver_status == "VERIFICATION_FAILED":
                    err_msg = err_msg or f"Task verification failed ({ver_status}). Repair attempts exhausted."
                    state.set_error(err_msg)
                    state.set_status(AgentStatus.FAILED)
                else:
                    err_msg = err_msg or "Task failed."
                    state.set_error(err_msg)
                    state.set_status(AgentStatus.FAILED)
            elif status in ("cancelled", "canceled", "stopped"):
                state.set_status(AgentStatus.CANCELLED)
            else:
                state.set_status(AgentStatus.SUCCEEDED)

    # ── Approval ──────────────────────────────────────────────────────────────

    def approve_tool(self, request_id: str, decision: str) -> None:
        def _send():
            try:
                payload = {}
                if decision in ("always", "session"):
                    payload["duration"] = decision

                self._post("/api/z29/governance/approve/" + request_id
                           if decision in ("allow", "always", "session")
                           else "/api/z29/governance/reject/" + request_id, payload)
            except Exception as exc:
                log.warning("Approval send failed: %s", exc)
            finally:
                state.set_approval(None)
                state.set_status(AgentStatus.RUNNING if decision in ("allow", "always", "session") else AgentStatus.IDLE)

        _daemon(_send, "aetherion-approval")

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def load_dashboard(self) -> None:
        def _fetch():
            try:
                data = self._get("/api/dashboard/metrics")
                m = DashboardMetrics(
                    sessions_today = data.get("sessions_today", 0),
                    tasks_done     = data.get("tasks_done", 0),
                    tokens_used    = data.get("tokens_used", 0),
                    cost_usd       = float(data.get("cost_usd", 0)),
                    uptime_pct     = float(data.get("uptime_pct", 100)),
                    active_agents  = data.get("active_agents", 0),
                    memory_entries = data.get("memory_entries", 0),
                    plugins_active = data.get("plugins_active", 0),
                )
                state.set_dashboard_metrics(m)
            except Exception as exc:
                log.debug("Dashboard metrics failed: %s", exc)
                state.set_dashboard_metrics(DashboardMetrics())

            # Load timeline
            try:
                tdata = self._get("/api/dashboard/timeline")
                events = [
                    TimelineEvent(
                        id     = str(e.get("id", uuid.uuid4().hex[:8])),
                        ts     = float(e.get("ts", time.time())),
                        event  = str(e.get("event", "?")),
                        detail = str(e.get("detail", "")),
                        level  = str(e.get("level", "info")),
                    )
                    for e in (tdata if isinstance(tdata, list) else tdata.get("events", []))
                ]
                state.set_timeline(events)
            except Exception as exc:
                log.debug("Timeline load failed: %s", exc)

        _daemon(_fetch, "aetherion-dashboard")

    # ── Sessions ──────────────────────────────────────────────────────────────

    def load_sessions(self, limit: int = 30) -> None:
        state.set_sessions_loading(True)

        def _fetch():
            try:
                res = self._get("/api/sessions", params={"limit": str(limit)})
                items = res if isinstance(res, list) else res.get("sessions", [])
                sessions = [
                    Session(
                        id         = str(s.get("id", "")),
                        title      = str(s.get("title") or s.get("task") or "Untitled")[:60],
                        status     = str(s.get("status", "?")),
                        model      = s.get("model"),
                        created_at = s.get("created_at"),
                        updated_at = s.get("updated_at"),
                        task       = s.get("task"),
                        tags       = s.get("tags", []),
                    )
                    for s in items
                ]
                state.set_sessions(sessions)
            except Exception as exc:
                state.set_sessions([], error=str(exc))

        _daemon(_fetch, "aetherion-sessions")

    def create_session(self, title: str = "New session") -> None:
        def _create():
            try:
                sid = None
                if not state.offline_mode:
                    try:
                        res = self._post("/api/sessions/create", {"title": title})
                        if isinstance(res, dict):
                            sid = res.get("id") or res.get("session_id")
                    except Exception:
                        pass
                if not sid:
                    sid = f"sess_{uuid.uuid4().hex[:12]}"
                    try:
                        from ..session_store import get_store
                        get_store().save_session(sid, title, state.active_model)
                    except Exception:
                        pass
                state.set_session(str(sid))
                state.clear_messages()
                state.push_notification(f"Session created: {sid[:16]}", NotifLevel.SUCCESS)
                self.list_local_sessions()
            except Exception as exc:
                state.set_error(f"Failed to create session: {exc}")

        _daemon(_create, "aetherion-create-session")

    new_session = create_session

    def rename_session(self, session_id: str, new_name: str) -> bool:
        """Rename an existing session locally and in remote backend if available."""
        from ..session_store import update_session_title
        ok = update_session_title(session_id, new_name)

        # Update in-memory state
        with state._lock:
            for s in state.sessions:
                if s.id == session_id:
                    s.title = new_name
                    break
        state._notify("sessions")

        # If online, optionally notify backend (best-effort)
        if state.connected and not state.offline_mode:
            def _patch():
                try:
                    with self._client(read=5.0) as http:
                        http.patch(
                            self._url(f"/api/sessions/{session_id}"),
                            json={"title": new_name},
                            headers={"Authorization": f"Bearer {self._token}"},
                        )
                except Exception:
                    pass
            _daemon(_patch, "aetherion-rename-session")

        return ok

    def delete_session(self, session_id: str) -> None:
        def _del():
            try:
                from ..session_store import delete_session as _del_local
                _del_local(session_id)
            except Exception:
                pass
            if not state.offline_mode and state.connected:
                try:
                    self._post(f"/api/session/{session_id}/stop", {})
                except Exception:
                    pass
            self.load_sessions()
            state.push_notification("Session deleted", NotifLevel.SUCCESS)

        _daemon(_del, "aetherion-del-session")

    def clear_all_sessions(self) -> None:
        def _clear():
            try:
                from ..session_store import _open, _db_lock
                conn = _open()
                if conn is not None:
                    with _db_lock:
                        conn.execute("DELETE FROM messages")
                        conn.execute("DELETE FROM sessions")
                        conn.commit()
                state.clear_messages()
                self.list_local_sessions()
                state.push_notification("All local sessions cleared", NotifLevel.SUCCESS)
            except Exception as exc:
                state.set_error(f"Failed to clear sessions: {exc}")

        _daemon(_clear, "aetherion-clear-sessions")


    def load_session_messages(self, session_id: str) -> None:
        def _fetch():
            try:
                res = self._get(f"/api/session/{session_id}/steps")
                steps = res if isinstance(res, list) else res.get("steps", [])
                state.clear_messages()
                for s in steps:
                    role = s.get("role", s.get("type", "system"))
                    content = s.get("content", s.get("text", ""))
                    if role in ("user", "human"):
                        state.append_message(Message.user(content))
                    elif role in ("assistant", "ai", "agent"):
                        state.append_message(Message.assistant(content))
                    elif role == "tool":
                        state.append_message(Message.tool_call(
                            s.get("tool_name", "tool"), content
                        ))
                    else:
                        state.append_message(Message.system(content))
            except Exception as exc:
                state.append_message(Message.system(f"Could not load history: {exc}"))

        _daemon(_fetch, "aetherion-load-history")

    # ── Memory ────────────────────────────────────────────────────────────────

    def load_memory(self, query: str = "", limit: int = 50) -> None:
        state.set_memory_loading(True)

        def _fetch():
            def _set_local_memory() -> None:
                from ..session_store import list_local_memories

                items = list_local_memories(query=query, limit=limit)
                entries = [
                    MemoryEntry(
                        id=str(item.get("id", uuid.uuid4().hex[:8])),
                        content=str(item.get("content", "")),
                        importance=float(item.get("importance", 0.5)),
                        created_at=item.get("created_at"),
                        tags=[],
                    )
                    for item in items
                ]
                state.set_memory(entries)

            if state.offline_mode:
                try:
                    _set_local_memory()
                except Exception as exc:
                    state.set_memory([], error=str(exc))
                return

            try:
                if query:
                    res = self._post("/api/ltm/search", {"query": query, "limit": limit})
                else:
                    res = self._get("/api/memory/recent", params={"limit": str(limit)})
                items = res if isinstance(res, list) else res.get("results", res.get("entries", []))
                entries = [
                    MemoryEntry(
                        id         = str(e.get("id", uuid.uuid4().hex[:8])),
                        content    = str(e.get("content", e.get("text", ""))),
                        importance = float(e.get("importance", e.get("score", 0.5))),
                        created_at = e.get("created_at"),
                        tags       = e.get("tags", []),
                    )
                    for e in items
                ]
                state.set_memory(entries)
            except Exception as exc:
                # A failed read is also a backend-unreachable condition.  Keep
                # the memory screen useful from the local SQLite store.
                try:
                    _set_local_memory()
                except Exception:
                    state.set_memory([], error=str(exc))

        _daemon(_fetch, "aetherion-memory")

    def add_memory(self, content: str, on_success: Optional[Callable[[], None]] = None) -> None:
        def _save_locally() -> bool:
            from ..session_store import insert_local_memory

            saved = insert_local_memory(content, importance=0.7)
            if saved is None:
                raise RuntimeError("local memory store is unavailable")
            state.push_notification("Memory saved successfully", NotifLevel.SUCCESS)
            state.set_error(None)
            state.set_failed_action(None, None)
            if on_success:
                on_success()
            self.load_memory()
            return True

        def _do_post():
            if state.offline_mode:
                try:
                    _save_locally()
                except Exception as exc:
                    state.set_error(f"Memory save failed locally: {exc}")
                    state.set_failed_action("Save memory", _do_post)
                return

            try:
                self._post("/api/ltm/remember", {"content": content, "importance": 0.7})
                state.push_notification("Memory saved successfully", NotifLevel.SUCCESS)
                state.set_error(None)
                state.set_failed_action(None, None)
                if on_success:
                    on_success()
                self.load_memory()
            except Exception as exc:
                rs, detail = classify_network_error(exc)
                state.set_runtime_state(rs, detail)
                try:
                    _save_locally()
                except Exception as local_exc:
                    state.set_error(f"Memory save failed: {detail}; local save failed: {local_exc}")
                    state.set_failed_action("Save memory", _do_post)

        _daemon(_do_post, "aetherion-add-memory")

    # ── Plugins ───────────────────────────────────────────────────────────────

    def _plugin_operation(
        self,
        label: str,
        action: Callable[[PluginCatalogManager], Any],
        *,
        evidence_only: bool = False,
    ) -> None:
        """Serialize explicit TUI actions; the manager owns all validation and I/O."""
        if not state.begin_plugin_operation(label):
            return

        def _do() -> None:
            message = ""
            error = None
            try:
                manager = PluginCatalogManager(evidence_only=True) if evidence_only else PluginCatalogManager()
                result = action(manager)
                if isinstance(result, dict):
                    if result.get("last_error"):
                        raise PluginManagerError(str(result["last_error"]))
                    message = (
                        f"{label} complete. Selected commit: {result.get('resolved_commit') or '—'}; "
                        f"candidate: {result.get('candidate_commit') or '—'}; "
                        f"integrity: {result.get('integrity') or 'not-installed'}. Execution disabled."
                    )
                elif result is False:
                    raise PluginManagerError("The selected record no longer exists. Reload the catalog.")
                else:
                    message = f"{label} complete. Execution disabled."
            except Exception as exc:
                error = _safe_notification(str(exc)[:1500])
            finally:
                # Re-read after success AND failure (including partial/crash recovery).
                self.load_plugins(evidence_only=evidence_only)
                state.finish_plugin_operation(message=message, error=error)
                state.push_notification(
                    f"{label} failed: {error}" if error else message,
                    NotifLevel.ERROR if error else NotifLevel.SUCCESS,
                )

        _daemon(_do, "aetherion-plugin-operation")

    def discover_plugins(
        self,
        query: str,
        limit: int = 20,
        language: Optional[str] = None,
        min_stars: int = 0,
        include_archived: bool = False,
    ) -> None:
        """Run bounded read-only GitHub discovery and publish authoritative results."""
        with self._lock:
            self._plugin_discovery_generation += 1
            generation = self._plugin_discovery_generation
        state.set_plugin_discovery_loading(True, query=query)

        def _fetch() -> None:
            try:
                items = PluginCatalogManager().discover(
                    query,
                    limit=limit,
                    language=language,
                    min_stars=min_stars,
                    include_archived=include_archived,
                )
                results = [
                    PluginCandidate(
                        id=str(item.get("id", "")),
                        name=str(item.get("name", "")),
                        full_name=str(item.get("full_name", "")),
                        source_url=str(item.get("source_url", "")),
                        description=str(item.get("description", "")),
                        language=str(item.get("language", "")),
                        topics=[str(topic) for topic in item.get("topics", [])],
                        stars=int(item.get("stars", 0) or 0),
                        forks=int(item.get("forks", 0) or 0),
                        license_id=str(item.get("license_id", "")),
                        license_name=str(item.get("license_name", "")),
                        license_declared=bool(item.get("license_declared", False)),
                        archived=bool(item.get("archived", False)),
                        default_branch=str(item.get("default_branch", "")),
                        pushed_at=str(item.get("pushed_at", "")),
                        updated_at=str(item.get("updated_at", "")),
                        compatibility_status=str(item.get("compatibility_status", "unknown")),
                         compatibility_reasons=[str(reason) for reason in item.get("compatibility_reasons", [])],
                         resolved_commit=str(item.get("resolved_commit") or ""),
                         integrity_status=str(item.get("integrity_status", "unknown")),
                         catalog_auth_status=str(item.get("catalog_auth_status", "unsigned")),
                         publisher_auth_status=str(item.get("publisher_auth_status", "not_checked")),
                         policy_status=str(item.get("policy_status", "unreviewed")),
                         execution_status="disabled",
                         execution_enabled=False,
                    )
                    for item in items
                ]
                with self._lock:
                    if generation == self._plugin_discovery_generation:
                        state.set_plugin_discovery(results, query=query)
            except Exception as exc:
                with self._lock:
                    if generation == self._plugin_discovery_generation:
                        state.set_plugin_discovery([], query=query, error=str(exc)[:1500])

        _daemon(_fetch, "aetherion-plugin-discovery")

    def inspect_plugin(self, selector: str, ref: Optional[str] = None, display_name: str = "repository") -> None:
        """Inspect source metadata without saving, installing, importing, or executing it."""
        def _inspect(manager: PluginCatalogManager) -> Dict[str, Any]:
            state.set_plugin_inspection(None, selector=selector, loading=True)
            try:
                result = manager.inspect(selector, ref=ref)
                state.set_plugin_inspection(result, selector=selector)
                return result
            except Exception as exc:
                state.set_plugin_inspection(None, error=str(exc)[:1500], selector=selector)
                raise
        self._plugin_operation("Inspect metadata", _inspect)

    def check_publisher_evidence(self, selector: str, ref: Optional[str] = None, display_name: str = "plugin") -> None:
        """Collect optional GitHub commit-signature evidence without mutating source state."""
        self._plugin_operation(
            f"Check commit signature for {display_name}",
            lambda manager: manager.check_publisher_evidence(selector, ref=ref),
            evidence_only=True,
        )

    def catalog_add_plugin(self, alias: str, source: str, ref: Optional[str] = None) -> None:
        """Save a user repository through the existing persistent catalog manager."""
        self._plugin_operation("Save catalog (source not installed)", lambda manager: manager.catalog_add(alias, source, ref=ref or None))

    def pin_plugin(self, alias: str, ref: str, display_name: str = "repository") -> None:
        """Explicitly select a new catalog ref; no managed source is changed here."""
        self._plugin_operation("Select catalog pin (source unchanged)", lambda manager: manager.catalog_pin(alias, ref))

    def verify_plugin(self, selector: str, display_name: str = "plugin") -> None:
        """Verify managed source integrity without importing source files."""
        self._plugin_operation(f"Verify {display_name}", lambda manager: manager.status(selector))

    def update_plugin(self, selector: str, display_name: str = "plugin") -> None:
        """Re-stage the selected saved pin through the transaction-safe manager."""
        self._plugin_operation(f"Update {display_name}", lambda manager: manager.update(selector))

    def remove_catalog_plugin(self, alias: str, display_name: str = "repository") -> None:
        """Remove only a saved catalog record; managed source remains untouched."""
        def _remove(manager: PluginCatalogManager) -> bool:
            if not manager.catalog_remove(alias):
                raise PluginManagerError(f"Catalog entry '{alias}' was not found")
            return True
        self._plugin_operation(f"Remove catalog {display_name} (managed source unchanged)", _remove)

    def load_plugins(self, *, evidence_only: bool = False) -> None:
        def _fetch():
            try:
                # Keep this control-plane path separate from the executable
                # backend extension manager. Nothing is imported or enabled.
                manager = PluginCatalogManager(evidence_only=True) if evidence_only else PluginCatalogManager()
                items = manager.list_catalog(evidence_only=evidence_only)
                all_plugins: List[Plugin] = []

                for item in items:
                    pid = str(item.get("id", item.get("name", "?")))
                    all_plugins.append(Plugin(
                        id          = pid,
                        name        = str(item.get("name", pid)),
                        description = str(item.get("description", "")),
                        category    = str(item.get("category", "general")),
                        version     = str(item.get("version", "1.0")),
                        installed   = bool(item.get("installed", False)),
                        enabled     = False,
                        author      = str(item.get("author", "")),
                        source_url  = str(item.get("source_url", "")),
                        catalog_origin = str(item.get("catalog_origin", item.get("source_type", ""))),
                        requested_ref = str(item.get("requested_ref", "")),
                         resolved_commit = str(item.get("resolved_commit", item.get("verified_commit", ""))),
                         compatibility_status = str(item.get("compatibility_status", "unknown")),
                         compatibility_reasons = list(item.get("compatibility_reasons", [])),
                         status      = str(item.get("status", "available")),
                         integrity   = str(item.get("integrity", "not-installed")),
                         integrity_status = str(item.get("integrity_status", "unknown")),
                          catalog_auth_status = str(item.get("catalog_auth_status", "unsigned")),
                          publisher_auth_status = str(item.get("publisher_auth_status", "not_checked")),
                          publisher_evidence_provider = str(item.get("publisher_evidence_provider", "") or ""),
                          publisher_evidence_type = str(item.get("publisher_evidence_type", "") or ""),
                          publisher_evidence_observation_status = str(item.get("publisher_evidence_observation_status", "not_checked") or "not_checked"),
                          publisher_evidence_provider_status = str(item.get("publisher_evidence_provider_status", "") or ""),
                          publisher_evidence_reason = str(item.get("publisher_evidence_reason", "") or ""),
                          publisher_evidence_provider_reason = str(item.get("publisher_evidence_provider_reason", "") or ""),
                          publisher_evidence_identity_policy_status = str(item.get("publisher_evidence_identity_policy_status", "unconfigured") or "unconfigured"),
                          publisher_evidence_local_signature_status = str(item.get("publisher_evidence_local_signature_status", "not_performed") or "not_performed"),
                          publisher_evidence_signature_format = str(item.get("publisher_evidence_signature_format", "") or ""),
                          publisher_evidence_observed_at = str(item.get("publisher_evidence_observed_at", "") or ""),
                          publisher_evidence_freshness = str(item.get("publisher_evidence_freshness", "unavailable") or "unavailable"),
                          publisher_evidence_commit = str(item.get("publisher_evidence_commit", "") or ""),
                          publisher_evidence_digest = str(item.get("publisher_evidence_digest", "") or ""),
                          policy_status = str(item.get("policy_status", "unreviewed")),
                         execution_status = "disabled",
                          trust_reason_code = str(item.get("trust_reason_code", "") or ""),
                          catalog_key_id = str(item.get("catalog_key_id", "") or ""),
                          catalog_trust_root_fingerprint = str(item.get("catalog_trust_root_fingerprint", "") or ""),
                          catalog_keyset_id = str(item.get("catalog_keyset_id", "") or ""),
                          catalog_keyset_sequence = item.get("catalog_keyset_sequence"),
                          catalog_keyset_expires_at = str(item.get("catalog_keyset_expires_at", "") or ""),
                          catalog_keyset_active_key_ids = [str(key_id) for key_id in item.get("catalog_keyset_active_key_ids", [])],
                          catalog_keyset_revoked_key_ids = [str(key_id) for key_id in item.get("catalog_keyset_revoked_key_ids", [])],
                          catalog_trust_lifecycle_reason = str(item.get("catalog_trust_lifecycle_reason", "") or ""),
                          catalog_trust_last_transition = str(item.get("catalog_trust_last_transition", "") or ""),
                          catalog_sequence = item.get("catalog_sequence"),
                         catalog_issued_at = str(item.get("catalog_issued_at", "") or ""),
                         catalog_expires_at = str(item.get("catalog_expires_at", "") or ""),
                         catalog_freshness = str(item.get("catalog_freshness", "unavailable") or "unavailable"),
                         catalog_refresh_failure_reason = str(item.get("catalog_refresh_failure_reason", "") or ""),
                         installed_commit = str(item.get("installed_commit", "")),
                         execution_enabled = False,
                         update_available = bool(item.get("update_available", False)),
                         source_plugin_id = str(item.get("source_plugin_id", "")),
                         license_id = str(item.get("license_id", "")),
                         license_name = str(item.get("license_name", item.get("license", ""))),
                         license_declared = bool(item.get("license_declared", False) or item.get("license")),
                         topics = [str(topic) for topic in item.get("topics", [])],
                         stars = int(item.get("stars", 0) or 0),
                         forks = int(item.get("forks", 0) or 0),
                         archived = bool(item.get("archived", False)),
                         default_branch = str(item.get("default_branch", "")),
                         pushed_at = str(item.get("pushed_at", "")),
                         updated_at = str(item.get("updated_at", "")),
                         candidate_commit = str(item.get("candidate_commit", "")),
                         catalog_present = bool(item.get("catalog_present", item.get("catalog_origin") in {"curated", "user"})),
                         installed_id = str(item.get("installed_id", "")),
                         language = str(item.get("language", "")),
                         last_error = str(item.get("last_error", "") or ""),
                     ))

                state.set_plugins(all_plugins)
            except Exception as exc:
                state.set_plugins([], error=str(exc))

        _daemon(_fetch, "aetherion-plugins")

    def install_plugin(self, plugin_id: str, plugin_name: str) -> None:
        """Install a curated plugin source without enabling execution."""
        self._plugin_operation(f"Stage {plugin_name}", lambda manager: manager.install(plugin_id))

    def refresh_plugin_ref(self, plugin_id: str, plugin_name: str) -> None:
        """Check a saved user ref without changing its selected commit."""
        self._plugin_operation(f"Refresh pin {plugin_name}", lambda manager: manager.catalog_refresh(plugin_id))

    def uninstall_plugin(self, plugin_id: str, plugin_name: str) -> None:
        """Remove a locally stored metadata-only plugin source."""
        def _remove(manager: PluginCatalogManager) -> bool:
            if not manager.remove(plugin_id):
                raise PluginManagerError(f"Plugin '{plugin_id}' is not installed")
            return True
        self._plugin_operation(f"Uninstall {plugin_name} (catalog preserved)", _remove)

    def toggle_plugin(self, plugin_id: str, plugin_name: str, enable: bool) -> None:
        """Keep plugin activation fail-closed in the control-plane TUI."""
        state.push_notification(
            f"Plugin execution is disabled; '{plugin_name}' remains metadata-only.",
            NotifLevel.WARNING,
        )

    # ── Skills ────────────────────────────────────────────────────────────────

    def load_skills(self) -> None:
        def _fetch():
            try:
                from ..skill_library import SkillLibrary
                library = SkillLibrary()
                remote_by_id = {}
                if self._token and not state.offline_mode:
                    try:
                        remote_response = self._get("/api/skills")
                        remote_items = remote_response.get("skills", []) if isinstance(remote_response, dict) else []
                        remote_by_id = {str(item.get("id")): item for item in remote_items}
                    except Exception as remote_exc:
                        log.debug("Published skill inventory unavailable: %s", remote_exc)
                skills = [
                    Skill(
                        name=item.name,
                        description=item.description,
                        version=item.version,
                        enabled=item.enabled,
                        builtin=not item.custom,
                        skill_id=item.skill_id,
                        category=item.category,
                        source_kind=item.source_kind,
                        installed=item.installed,
                        custom=item.custom,
                        readiness=item.readiness,
                        runtime_connected=item.runtime_connected,
                        requirements=list(item.requirements),
                        source_path=item.source_path,
                        content_sha256=item.content_sha256,
                        published_status=(remote_by_id.get(item.skill_id) or {}).get("status", "local_definition"),
                        approval_status=(remote_by_id.get(item.skill_id) or {}).get("approval_status", "NOT_REQUIRED"),
                        tenant_digest=(remote_by_id.get(item.skill_id) or {}).get("content_hash", ""),
                    )
                    for item in library.catalogue()
                ]
                state.set_skills(skills)
            except Exception as exc:
                state.set_skills([])
                state.push_notification(f"Skills Library failed: {_safe_notification(str(exc))}", NotifLevel.ERROR)

        _daemon(_fetch, "aetherion-skills")

    def enable_skill(self, name: str) -> None:
        """Enable an explicitly installed user-scoped skill."""
        def _do():
            try:
                from ..skill_library import SkillLibrary
                SkillLibrary().set_enabled(name, True)
                state.push_notification(f"Skill '{name}' enabled", NotifLevel.SUCCESS)
                self.load_skills()
            except Exception as exc:
                state.push_notification(f"Failed to enable skill: {_safe_notification(exc)}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-skill-enable")

    def disable_skill(self, name: str) -> None:
        """Disable an installed skill without deleting its definition."""
        def _do():
            try:
                from ..skill_library import SkillLibrary
                SkillLibrary().set_enabled(name, False)
                state.push_notification(f"Skill '{name}' disabled", NotifLevel.SUCCESS)
                self.load_skills()
            except Exception as exc:
                state.push_notification(f"Failed to disable skill: {_safe_notification(exc)}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-skill-disable")

    def install_skill(self, name: str) -> None:
        """Install one selected catalogue skill into the user scope."""
        def _do():
            try:
                from ..skill_library import SkillLibrary
                SkillLibrary().install(name)
                state.push_notification(f"Skill '{name}' installed", NotifLevel.SUCCESS)
                self.load_skills()
            except Exception as exc:
                state.push_notification(f"Skill installation failed: {_safe_notification(exc)}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-skill-install")

    def uninstall_skill(self, name: str) -> None:
        """Uninstall one skill while preserving bundled catalogue content."""
        def _do():
            try:
                from ..skill_library import SkillLibrary
                SkillLibrary().uninstall(name)
                state.push_notification(f"Skill '{name}' uninstalled", NotifLevel.SUCCESS)
                self.load_skills()
            except Exception as exc:
                state.push_notification(f"Skill uninstall failed: {_safe_notification(exc)}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-skill-uninstall")

    def delete_custom_skill(self, name: str) -> None:
        """Permanently delete a custom definition after explicit UI confirmation."""
        def _do():
            try:
                from ..skill_library import SkillLibrary
                SkillLibrary().delete_custom(name)
                state.push_notification(f"Custom skill '{name}' deleted", NotifLevel.SUCCESS)
                self.load_skills()
            except Exception as exc:
                state.push_notification(f"Custom skill delete failed: {_safe_notification(exc)}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-skill-delete")

    def create_custom_skill(self, name: str, description: str, instructions: str) -> None:
        """Persist custom skill text as data; never execute it."""
        def _do():
            try:
                from ..skill_library import SkillLibrary
                SkillLibrary().create_custom(name, description, instructions)
                state.push_notification(f"Custom skill '{name}' saved disabled", NotifLevel.SUCCESS)
                self.load_skills()
            except Exception as exc:
                state.push_notification(f"Custom skill save failed: {_safe_notification(exc)}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-skill-create")

    def edit_custom_skill(self, name: str, description: str, instructions: str) -> None:
        """Edit custom skill text without activating it."""
        def _do():
            try:
                from ..skill_library import SkillLibrary
                SkillLibrary().edit_custom(name, description, instructions)
                state.push_notification(f"Custom skill '{name}' updated", NotifLevel.SUCCESS)
                self.load_skills()
            except Exception as exc:
                state.push_notification(f"Custom skill edit failed: {_safe_notification(exc)}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-skill-edit")

    def export_skill(self, name: str) -> None:
        """Export one selected definition to the user's Downloads directory."""
        def _do():
            try:
                from ..skill_library import SkillLibrary
                filename, content, _content_type = SkillLibrary().export(name)
                destination = Path.home() / "Downloads" / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                state.push_notification(f"Skill exported to {destination}", NotifLevel.SUCCESS)
            except Exception as exc:
                state.push_notification(f"Skill export failed: {_safe_notification(exc)}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-skill-export")

    def publish_skill(self, name: str) -> None:
        """Publish exactly one selected installed definition to the authenticated tenant."""
        def _do():
            try:
                from ..skill_publish import reconcile_publish, validate_authoritative_get, validate_publish_response
                if not self._token or state.offline_mode:
                    raise RuntimeError("authenticated backend connection is unavailable")
                from ..skill_library import SkillLibrary
                entry = SkillLibrary().get(name, include_content=True)
                if entry is None or not entry.installed:
                    raise RuntimeError("install the selected local skill before publishing it")
                expected = {
                    "skill_id": entry.skill_id,
                    "name": entry.name,
                    "description": entry.description or "",
                    "version": entry.version,
                    "content_sha256": entry.content_sha256,
                }
                response = self._post("/api/skills/publish", {
                    "skill_id": entry.skill_id,
                    "name": entry.name,
                    "description": entry.description,
                    "version": entry.version,
                    "content": entry.content or "",
                    "client_sha256": entry.content_sha256,
                    "requested_permissions": [],
                })
                result = validate_publish_response(response, expected)
                authoritative = validate_authoritative_get(self._get(f"/api/skills/{entry.skill_id}"), expected)
                status = authoritative["status"]
                state.push_notification(f"Skill '{entry.skill_id}' published: {status}", NotifLevel.SUCCESS)
                self.load_skills()
            except Exception as exc:
                # A transport timeout can occur after the server commits.  A
                # matching authoritative GET is the only success proof.
                try:
                    if "entry" in locals() and "expected" in locals():
                        reconciled = reconcile_publish(self._get, expected)
                        state.push_notification(f"Skill '{entry.skill_id}' published: {reconciled['status']} (reconciled)", NotifLevel.SUCCESS)
                        self.load_skills()
                        return
                except Exception:
                    pass
                state.push_notification(f"Skill publish failed: {_safe_notification(exc)}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-skill-publish")

    # ── Providers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _provider_identity(payload: dict) -> set[str]:
        values: set[str] = set()
        for field in ("id", "key", "provider", "name"):
            value = payload.get(field)
            if value is not None and str(value).strip():
                values.add(str(value).strip().casefold())
        return values

    @staticmethod
    def _provider_from_payload(payload: dict, *, system: bool = False) -> Provider:
        pid = str(payload.get("id") or payload.get("key") or payload.get("provider") or payload.get("name", ""))
        name = str(payload.get("name") or payload.get("provider") or pid)
        raw_key = str(payload.get("api_key") or "")
        masked_key = str(payload.get("api_key_masked") or "")
        if not masked_key and raw_key:
            masked_key = f"{raw_key[:3]}...{raw_key[-4:]}" if len(raw_key) > 8 else "********"
        if system:
            provider_type = (
                "OPENAI_COMPATIBLE" if "openai" in pid.lower()
                else "ANTHROPIC_COMPATIBLE" if "anthropic" in pid.lower()
                else "CHAT_COMPATIBLE"
            )
            model = str(payload.get("model") or payload.get("default_model") or "?")
            available = bool(payload.get("available", payload.get("healthy", False)))
            quality = str(payload.get("quality", "?"))
            cost = str(payload.get("cost", "?"))
            configured = False
        else:
            provider_type = str(payload.get("provider_type", "OPENAI_COMPATIBLE"))
            model = str(payload.get("default_model") or payload.get("model", ""))
            available = bool(payload.get("is_enabled", True))
            quality = str(payload.get("quality", "A"))
            cost = str(payload.get("cost", "$"))
            configured = True
        return Provider(
            key=str(payload.get("key") or payload.get("provider") or pid),
            name=name,
            model=model,
            available=available,
            latency=payload.get("latency_ms"),
            quality=quality,
            cost=cost,
            error=payload.get("error"),
            id=pid,
            provider_type=provider_type,
            base_url=str(payload.get("base_url", "")),
            api_key_masked=masked_key or raw_key,
            api_key_raw=raw_key,
            is_default=bool(payload.get("is_default", False)),
            is_enabled=bool(payload.get("is_enabled", True)),
            configured=configured,
        )

    def _save_provider_locally(self, data: dict) -> None:
        from ..session_store import save_local_provider
        record = dict(data)
        record.setdefault("is_enabled", True)
        record.setdefault("configured", True)
        if save_local_provider(record) is None:
            raise RuntimeError("local provider store is unavailable")
        state.set_error(None)
        state.set_failed_action(None, None)

    def load_providers(self) -> None:
        def _fetch():
            try:
                from ..session_store import load_local_providers
                local_provs = load_local_providers()
                user_provs = []
                system_provs = []
                if not state.offline_mode:
                    try:
                        res = self._get("/api/providers")
                        user_provs = res.get("providers", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
                    except Exception as exc:
                        log.debug("User providers fetch failed: %s", exc)
                    try:
                        res2 = self._get("/api/p9/providers")
                        system_provs = res2.get("providers", []) if isinstance(res2, dict) else (res2 if isinstance(res2, list) else [])
                    except Exception as exc:
                        log.debug("System providers fetch failed: %s", exc)

                providers: List[Provider] = []
                seen_keys: set[str] = set()

                def append_provider(payload: object, *, system: bool = False) -> None:
                    if not isinstance(payload, dict):
                        return
                    identity = self._provider_identity(payload)
                    if identity and identity.intersection(seen_keys):
                        # Merge raw api_key or missing base_url into existing provider
                        for p in providers:
                            p_id = self._provider_identity({"id": p.id, "key": p.key, "name": p.name})
                            if p_id and p_id.intersection(identity):
                                if not p.api_key_raw and payload.get("api_key"):
                                    p.api_key_raw = str(payload.get("api_key"))
                                if not p.base_url and payload.get("base_url"):
                                    p.base_url = str(payload.get("base_url"))
                                break
                        return
                    providers.append(self._provider_from_payload(payload, system=system))
                    seen_keys.update(identity)

                for payload in user_provs:
                    append_provider(payload)
                for payload in local_provs:
                    append_provider(payload)
                for payload in system_provs:
                    append_provider(payload, system=True)

                state.set_providers(providers)
            except Exception as exc:
                state.set_providers([])

        _daemon(_fetch, "aetherion-providers")

    def save_provider(self, data: dict, on_success: Optional[Callable[[], None]] = None) -> None:
        """Create or update a provider."""
        def _do():
            # ALWAYS persist locally so the client has the raw unmasked API key
            try:
                self._save_provider_locally(data)
            except Exception as exc:
                log.warning("Failed to save provider locally: %s", exc)

            if state.offline_mode:
                state.push_notification(f"Provider '{data.get('name')}' saved locally (BYOK active)", NotifLevel.SUCCESS)
                if on_success:
                    on_success()
                self.load_providers()
                return

            try:
                pid = data.get("id")
                if pid and not pid.startswith("new_") and not pid.startswith("prov_temp") and not pid.startswith("local_"):
                    # Update
                    res = self._put(f"/api/providers/{pid}", data)
                    state.push_notification(f"Provider '{data.get('name')}' updated & synced", NotifLevel.SUCCESS)
                else:
                    # Create
                    payload = dict(data)
                    payload.pop("id", None)
                    res = self._post("/api/providers", payload)
                    state.push_notification(f"Provider '{data.get('name')}' created & synced", NotifLevel.SUCCESS)
            except Exception as exc:
                rs, detail = classify_network_error(exc)
                state.push_notification(f"Provider '{data.get('name')}' saved locally (BYOK active)", NotifLevel.SUCCESS)

            if on_success:
                on_success()
            self.load_providers()

        _daemon(_do, "aetherion-save-provider")

    def delete_provider(self, provider_id: str) -> None:
        """Delete a configured provider."""
        def _do():
            try:
                from ..session_store import get_local_providers_path, _read_local_providers_unlocked, _write_json_atomic, _db_lock
                with _db_lock:
                    recs = _read_local_providers_unlocked()
                    recs = [r for r in recs if r.get("id") != provider_id and r.get("key") != provider_id and r.get("name") != provider_id]
                    _write_json_atomic(get_local_providers_path(), recs)
            except Exception as exc:
                log.warning("Local provider delete error: %s", exc)

            if not state.offline_mode:
                try:
                    self._delete(f"/api/providers/{provider_id}")
                except Exception:
                    pass

            state.push_notification("Provider deleted", NotifLevel.SUCCESS)
            self.load_providers()

        _daemon(_do, "aetherion-delete-provider")

    def test_provider(self, provider_id: str, on_result: Optional[Callable[[dict], None]] = None) -> None:
        """Run connection diagnostic test against a specific provider."""
        def _do():
            target_p = None
            for p in getattr(state, "providers", []):
                if p.id == provider_id or p.key == provider_id or p.name == provider_id:
                    target_p = p
                    break

            if target_p and target_p.base_url:
                base_url = target_p.base_url.strip().rstrip("/")
                ptype = getattr(target_p, "provider_type", "OPENAI_COMPATIBLE").upper()
                model_name = getattr(target_p, "model", "") or "default"

                api_key = getattr(target_p, "api_key_raw", "")
                if not api_key or "..." in api_key or "…" in api_key:
                    try:
                        from ..session_store import load_local_providers
                        for lp in load_local_providers():
                            if lp.get("id") == target_p.id or lp.get("name") == target_p.name:
                                api_key = lp.get("api_key") or api_key
                                break
                    except Exception:
                        pass

                if ptype == "ANTHROPIC_COMPATIBLE":
                    test_url = base_url if base_url.endswith("/messages") else (f"{base_url}/messages" if base_url.endswith("/v1") else f"{base_url}/v1/messages")
                    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
                    body = {"model": model_name, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
                else:
                    test_url = base_url if base_url.endswith("/chat/completions") else (f"{base_url}/chat/completions" if (base_url.endswith("/v1") or "/v1/" in base_url) else f"{base_url}/v1/chat/completions")
                    headers = {"Authorization": f"Bearer {api_key}" if api_key else "", "Content-Type": "application/json"}
                    body = {"model": model_name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}

                t0 = time.time()
                try:
                    with httpx.Client(timeout=15.0) as client:
                        resp = client.post(test_url, json=body, headers=headers)
                        latency_ms = int((time.time() - t0) * 1000)
                        ct = resp.headers.get("content-type", "").lower()
                        if "text/html" in ct:
                            msg = f"Test FAILED: Provider returned HTML page. Check Base URL (did you mean '{base_url}/v1'?)"
                            state.push_notification(msg, NotifLevel.ERROR)
                            target_p.available = False
                            target_p.error = "Returned HTML instead of API"
                        elif resp.status_code in (200, 400):
                            msg = f"Provider test PASSED: CONNECTED ({latency_ms}ms)"
                            state.push_notification(msg, NotifLevel.SUCCESS)
                            target_p.available = True
                            target_p.latency = latency_ms
                            target_p.error = None
                        elif resp.status_code == 401:
                            state.push_notification("Provider test FAILED: Invalid API Key (HTTP 401)", NotifLevel.ERROR)
                            target_p.available = False
                            target_p.error = "Invalid API Key (HTTP 401)"
                        else:
                            state.push_notification(f"Provider test FAILED: HTTP {resp.status_code}", NotifLevel.ERROR)
                            target_p.available = False
                            target_p.error = f"HTTP {resp.status_code}"

                        if on_result:
                            on_result({"ok": target_p.available, "latency_ms": latency_ms, "status": "CONNECTED" if target_p.available else "ERROR"})
                        self.load_providers()
                        return
                except Exception as exc:
                    state.push_notification(f"Provider connection error: {exc}", NotifLevel.ERROR)
                    target_p.available = False
                    target_p.error = str(exc)[:40]
                    self.load_providers()
                    return

            try:
                res = self._post(f"/api/providers/{provider_id}/test", {})
                ok = res.get("ok", False) or res.get("status") == "CONNECTED"
                msg = f"Provider test {'PASSED' if ok else 'FAILED'}: {res.get('status', 'ERROR')}"
                if res.get("latency_ms"):
                    msg += f" ({res['latency_ms']}ms)"
                state.push_notification(msg, NotifLevel.SUCCESS if ok else NotifLevel.ERROR)
                if on_result:
                    on_result(res)
                self.load_providers()
            except Exception as exc:
                state.push_notification(f"Provider test error: {exc}", NotifLevel.ERROR)

        _daemon(_do, "aetherion-test-provider")

    def set_default_provider(self, provider_id: str) -> None:
        """Mark a provider as default."""
        def _do():
            try:
                from ..session_store import get_local_providers_path, _read_local_providers_unlocked, _write_json_atomic, _db_lock
                with _db_lock:
                    recs = _read_local_providers_unlocked()
                    for r in recs:
                        if r.get("id") == provider_id or r.get("key") == provider_id or r.get("name") == provider_id:
                            r["is_default"] = True
                            if r.get("model"):
                                state.set_model(r["model"])
                        else:
                            r["is_default"] = False
                    _write_json_atomic(get_local_providers_path(), recs)
            except Exception as exc:
                log.warning("Local default provider update error: %s", exc)

            for p in getattr(state, "providers", []):
                if p.id == provider_id or p.key == provider_id or p.name == provider_id:
                    p.is_default = True
                    if p.model:
                        state.set_model(p.model)
                else:
                    p.is_default = False

            if not state.offline_mode:
                try:
                    self._post(f"/api/providers/{provider_id}/default", {})
                except Exception:
                    pass

            state.push_notification("Default provider updated", NotifLevel.SUCCESS)
            self.load_providers()

        _daemon(_do, "aetherion-default-provider")

    def toggle_provider(self, provider_id: str, enabled: bool) -> None:
        """Enable or disable a provider."""
        def _do():
            try:
                from ..session_store import get_local_providers_path, _read_local_providers_unlocked, _write_json_atomic, _db_lock
                with _db_lock:
                    recs = _read_local_providers_unlocked()
                    for r in recs:
                        if r.get("id") == provider_id or r.get("key") == provider_id or r.get("name") == provider_id:
                            r["is_enabled"] = enabled
                    _write_json_atomic(get_local_providers_path(), recs)
            except Exception as exc:
                log.warning("Local toggle provider error: %s", exc)

            for p in getattr(state, "providers", []):
                if p.id == provider_id or p.key == provider_id or p.name == provider_id:
                    p.is_enabled = enabled

            if not state.offline_mode:
                try:
                    action = "enable" if enabled else "disable"
                    self._post(f"/api/providers/{provider_id}/{action}", {})
                except Exception:
                    pass

            verb = "enabled" if enabled else "disabled"
            state.push_notification(f"Provider {verb}", NotifLevel.SUCCESS)
            self.load_providers()

        _daemon(_do, "aetherion-toggle-provider")

    # ── Background Jobs ───────────────────────────────────────────────────────

    def load_jobs(self) -> None:
        def _fetch():
            try:
                res = self._get("/api/scheduler/tasks")
                items = res if isinstance(res, list) else res.get("tasks", [])
                jobs = [
                    BackgroundJob(
                        id         = str(j.get("id", uuid.uuid4().hex[:8])),
                        name       = str(j.get("name", j.get("description", "Task"))),
                        status     = str(j.get("status", "?")),
                        created_at = float(j.get("created_at", time.time())),
                        updated_at = float(j.get("updated_at", time.time())),
                        progress   = int(j.get("progress", 0)),
                        result     = j.get("result"),
                    )
                    for j in items
                ]
                state.set_jobs(jobs)
            except Exception as exc:
                state.set_jobs([])

        _daemon(_fetch, "aetherion-jobs")

    # ── Git ───────────────────────────────────────────────────────────────────

    def load_git_status(self) -> None:
        """Load git status for current workspace."""
        def _fetch():
            try:
                res = self._get("/api/github/analyze")
                if res:
                    state.append_message(Message.system(
                        f"Git: {res.get('branch', '?')} "
                        f"({res.get('commits_ahead', 0)} ahead, "
                        f"{res.get('commits_behind', 0)} behind)"
                    ))
            except Exception:
                pass  # Git is optional

        _daemon(_fetch, "aetherion-git")

    # ── User profile ──────────────────────────────────────────────────────────

    def load_user_profile(self) -> None:
        def _fetch():
            try:
                res = self._get("/api/auth/me")
                state.set_user(
                    email   = res.get("email"),
                    plan    = res.get("plan", "Lite"),
                    user_id = str(res.get("id", "")),
                )
            except Exception:
                pass

        _daemon(_fetch, "aetherion-user")

    def update_profile(self, display_name: str, on_success: Optional[Callable[[], None]] = None) -> None:
        def _do():
            try:
                res = self._put("/api/auth/profile", {"display_name": display_name})
                if res.get("ok"):
                    state.push_notification("Profile updated successfully", NotifLevel.SUCCESS)
                    self.load_user_profile()
                    if on_success:
                        on_success()
                else:
                    err = res.get("error", "Profile update failed")
                    state.push_notification(err, NotifLevel.ERROR)
            except Exception as e:
                st, err_str = classify_network_error(e)
                state.set_runtime_state(st)
                state.push_notification(f"Profile update error: {err_str}", NotifLevel.ERROR)
        _daemon(_do, "aetherion-profile-update")

    # ── Personal API keys ──────────────────────────────────────────────────────

    def list_personal_api_keys(self) -> List[Dict[str, Any]]:
        """Fetch authoritative API keys for authenticated user from backend."""
        with self._lock:
            token = self._token
        if not token:
            from ..config import get_token
            token = get_token()

        if token and not state.offline_mode:
            try:
                with self._client(read=10.0) as http:
                    r = http.get(
                        self._url("/api/auth/api-keys"),
                        headers=self._headers(),
                    )
                    if r.is_success:
                        data = r.json()
                        keys = data.get("api_keys", [])
                        for k in keys:
                            k["type"] = "Server Key"
                        try:
                            key_file = Path.home() / ".aetherion" / "api_keys.json"
                            key_file.parent.mkdir(parents=True, exist_ok=True)
                            key_file.write_text(json.dumps(keys, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                        return keys
            except Exception as exc:
                log.debug("Backend list_personal_api_keys failed: %s", exc)

        key_file = Path.home() / ".aetherion" / "api_keys.json"
        if not key_file.exists():
            return []
        try:
            raw_keys = json.loads(key_file.read_text(encoding="utf-8"))
            for k in raw_keys:
                if not k.get("type"):
                    kid = str(k.get("id", ""))
                    k["type"] = "Local / Offline" if (kid.startswith("dev_") or kid == "default") else "Server Key"
            return raw_keys
        except Exception:
            return []

    def generate_personal_api_key(self, label: str) -> Dict[str, Any]:
        """Issue an authoritative personal API key from backend."""
        with self._lock:
            token = self._token
        if not token:
            from ..config import get_token
            token = get_token()

        if token and not state.offline_mode:
            try:
                with self._client(read=10.0) as http:
                    r = http.post(
                        self._url("/api/auth/api-keys"),
                        json={"name": label},
                        headers=self._headers(),
                    )
                    if r.is_success:
                        data = r.json()
                        entry = data.get("api_key", {})
                        entry["type"] = "Server Key"
                        try:
                            key_file = Path.home() / ".aetherion" / "api_keys.json"
                            keys = []
                            if key_file.exists():
                                keys = json.loads(key_file.read_text(encoding="utf-8"))
                            keys.append({
                                "id": entry.get("id"),
                                "type": "Server Key",
                                "name": entry.get("name"),
                                "created": entry.get("created"),
                                "last_used": entry.get("last_used", "Never"),
                                "prefix": entry.get("prefix"),
                            })
                            key_file.parent.mkdir(parents=True, exist_ok=True)
                            key_file.write_text(json.dumps(keys, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                        return entry
            except Exception as exc:
                log.debug("Backend generate_personal_api_key failed: %s", exc)

        key_file = Path.home() / ".aetherion" / "api_keys.json"
        keys = []
        if key_file.exists():
            try:
                keys = json.loads(key_file.read_text(encoding="utf-8"))
            except Exception:
                keys = []
        raw_token = f"aeth_live_{uuid.uuid4().hex}"
        prefix = f"aeth_{raw_token[10:14]}****"
        created_str = time.strftime("%Y-%m-%d %H:%M")
        entry = {
            "id": f"dev_{str(uuid.uuid4())[:8]}",
            "type": "Local / Offline",
            "name": label or "unnamed",
            "created": created_str,
            "last_used": "Never",
            "prefix": prefix,
            "key": raw_token
        }
        keys.append(entry)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(json.dumps(keys, indent=2), encoding="utf-8")
        return entry

    def revoke_personal_api_key(self, key_id_or_name: str) -> bool:
        """Revoke API key on backend and update local cache."""
        with self._lock:
            token = self._token
        if not token:
            from ..config import get_token
            token = get_token()

        revoked = False
        if token and not state.offline_mode:
            try:
                with self._client(read=10.0) as http:
                    r = http.delete(
                        self._url(f"/api/auth/api-keys/{key_id_or_name}"),
                        headers=self._headers(),
                    )
                    if r.is_success:
                        revoked = True
            except Exception as exc:
                log.debug("Backend revoke_personal_api_key failed: %s", exc)

        key_file = Path.home() / ".aetherion" / "api_keys.json"
        if key_file.exists():
            try:
                keys = json.loads(key_file.read_text(encoding="utf-8"))
                new_keys = [k for k in keys if k.get("id") != key_id_or_name and k.get("name") != key_id_or_name]
                if len(new_keys) != len(keys):
                    key_file.write_text(json.dumps(new_keys, indent=2), encoding="utf-8")
                    return True
            except Exception:
                pass
        return revoked


    # ── Doctor ────────────────────────────────────────────────────────────────

    def run_doctor(self, callback: Callable[[str], None]) -> None:
        """Run health checks and call callback with result string."""
        from ..session_store import get_store_status

        def _check():
            lines = [f"Aetherion Doctor — {self.base_url}\n{'─'*40}"]
            # Backend health
            try:
                t0 = time.monotonic()
                r  = self._get("/api/health")
                lat = int((time.monotonic() - t0) * 1000)
                lines.append(f"✓ Backend reachable  ({lat}ms)  version={r.get('version', '?')}")
            except Exception as e:
                lines.append(f"✗ Backend unreachable: {e}")

            # Auth
            token = get_token()
            lines.append("✓ Token configured" if token else "✗ No token — run `aetherion login`")

            # User
            try:
                u = self._get("/api/auth/me")
                lines.append(f"✓ Logged in as: {u.get('email', '?')}  plan={u.get('plan', '?')}")
            except Exception:
                lines.append("✗ Not authenticated (token may be expired)")

            # Providers
            try:
                ps = self._get("/api/models/status")
                ok = sum(1 for p in (ps if isinstance(ps, list) else ps.get("providers", [])) if p.get("available"))
                lines.append(f"✓ Providers: {ok} available")
            except Exception:
                lines.append("? Provider status unavailable")

            # Local session store
            try:
                ss = get_store_status()
                if ss["available"]:
                    lines.append(
                        f"✓ Local session store: {ss['session_count']} sessions, "
                        f"{ss['message_count']} messages, "
                        f"journal={ss['journal_mode']}  ({ss['path']})"
                    )
                else:
                    lines.append(f"✗ Session store unavailable: {ss.get('error', '?')}")
            except Exception as exc:
                lines.append(f"? Session store: {exc}")

            lines.append(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
            callback("\n".join(lines))

        _daemon(_check, "aetherion-doctor")

    # ── Context compression ────────────────────────────────────────────────────

    def compress_context(self, session_id: Optional[str] = None) -> None:
        """Request context compression from the backend."""
        sid = session_id or state.active_session_id
        if not sid:
            state.push_notification("No active session to compress", "warning")
            return

        def _do():
            try:
                self._post(f"/api/sessions/{sid}/compress")
                state.push_notification("Context compressed successfully", "success")
            except Exception as exc:
                state.push_notification(f"Compression failed: {exc}", "error")

        _daemon(_do, "aetherion-compress")

    def branch_session(self, session_id: Optional[str] = None, branch_name: str = "") -> None:
        """Branch the current session."""
        sid = session_id or state.active_session_id
        if not sid:
            state.push_notification("No active session to branch", "warning")
            return
        if not branch_name:
            state.push_notification("Branch name required", "warning")
            return

        def _do():
            try:
                res = self._post(f"/api/sessions/{sid}/branch", {"name": branch_name})
                new_sid = res.get("session_id")
                if new_sid:
                    state.push_notification(f"Session branched successfully", "success")
                    self.load_session(new_sid)
                else:
                    state.push_notification("Branch succeeded but no new ID returned", "warning")
            except Exception as exc:
                state.push_notification(f"Branch failed: {exc}", "error")

        _daemon(_do, "aetherion-branch")

    # ── Session persistence integration ───────────────────────────────────────

    def _persist_message(self, session_id: str, role: str, content: str,
                         tokens: int = 0) -> None:
        """Persist a message to the local SQLite session store (best-effort)."""
        try:
            from ..session_store import save_message
            save_message(session_id, role, content, tokens=tokens)
        except Exception as exc:
            log.debug("Failed to persist message: %s", exc)

    def _ensure_local_session(self, session_id: str, model: str) -> None:
        """Create local session record if it doesn't exist yet."""
        try:
            from ..session_store import create_session, get_session
            if not get_session(session_id):
                create_session(
                    session_id=session_id,
                    title=f"Session {session_id[:8]}",
                    model=model,
                )
        except Exception as exc:
            log.debug("Failed to create local session: %s", exc)

    # ── Session resume ────────────────────────────────────────────────────────

    def resume_session(self, session_id: str) -> None:
        """
        Resume a session from local storage into the TUI conversation view.

        Loads messages from the local SQLite store and replays them into state.
        The backend session state is NOT re-executed — this is display-only resume.
        """
        def _do():
            try:
                from ..session_store import get_messages, get_session
                meta = get_session(session_id)
                if not meta:
                    state.push_notification(
                        f"Session {session_id[:8]} not found in local store", "warning"
                    )
                    return

                msgs = get_messages(session_id, limit=200)
                if not msgs:
                    state.push_notification("Session has no messages to restore", "info")
                    return

                state.clear_messages()
                state.set_session(session_id)
                if meta.get("model"):
                    state.set_model(meta["model"])

                for m in msgs:
                    role    = m["role"]
                    content = m["content"]
                    ts      = m.get("ts", time.time())
                    tokens  = m.get("tokens", 0)
                    msg = Message(
                        id=uuid.uuid4().hex[:8],
                        role=role,
                        content=content,
                        timestamp=ts,
                        token_count=tokens or None,
                    )
                    state.append_message(msg)

                state.push_notification(
                    f"Resumed session {session_id[:8]} — {len(msgs)} messages restored",
                    "success"
                )
            except Exception as exc:
                log.error("resume_session failed: %s", exc)
                state.push_notification(f"Resume failed: {exc}", "error")

        _daemon(_do, "aetherion-resume")

    def list_local_sessions(self, query: Optional[str] = None) -> None:
        """Load local sessions from SQLite into state.sessions."""
        def _do():
            try:
                from ..session_store import list_sessions
                rows = list_sessions(limit=50, query=query)
                sessions = [
                    Session(
                        id=r["id"],
                        title=r.get("title") or f"Session {r['id'][:8]}",
                        status="done",
                        model=r.get("model", ""),
                        created_at=r.get("created_at"),
                        updated_at=r.get("updated_at"),
                    )
                    for r in rows
                ]
                state.set_sessions(sessions)
            except Exception as exc:
                state.set_sessions([], error=str(exc))

        _daemon(_do, "aetherion-local-sessions")


# Module-level singleton
gateway = Gateway()
