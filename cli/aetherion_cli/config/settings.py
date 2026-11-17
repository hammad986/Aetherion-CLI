"""
aetherion_cli/config/settings.py
Configuration and credential management for the Aetherion CLI.
Supports environment variables, OS keyring, and local ~/.aetherion/config.json.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ..security.redaction import redact_secrets

CONFIG_DIR = Path.home() / ".aetherion"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEYRING_SERVICE = "aetherion-cli"
KEYRING_TOKEN_KEY = "token"
KEYRING_REFRESH_KEY = "refresh_token"

DEFAULT_SERVER_URL = "http://127.0.0.1:5000"
DEFAULT_MODEL = "default"
DEFAULT_CATALOG_URL = ""
DEFAULT_CATALOG_TRUST_ROOT = ""
DEFAULT_CATALOG_KEYSET = ""
DEFAULT_CATALOG_TRUST_STATE = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config.json, returning an empty dict on any failure."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(cfg: dict) -> None:
    """Atomically save config.json with restrictive permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # Restrict file permissions to owner-only on POSIX systems
    if sys.platform != "win32":
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass

    tmp.replace(CONFIG_FILE)


# ---------------------------------------------------------------------------
# Server URL
# ---------------------------------------------------------------------------

def get_server_url() -> str:
    env = os.environ.get("AETHERION_URL", "").strip()
    if env:
        return env

    # Container service discovery is explicit.  The backend service in the
    # Compose stack is named ``backend``; local development always uses the
    # IPv4 loopback address to avoid localhost resolving to IPv6 first.
    default_url = "http://backend:5000" if Path("/.dockerenv").exists() else DEFAULT_SERVER_URL
    return _load_config().get("server_url", default_url)


def set_server_url(url: str) -> None:
    cfg = _load_config()
    cfg["server_url"] = url.rstrip("/")
    _save_config(cfg)


# ---------------------------------------------------------------------------
# Signed curated catalog provisioning
# ---------------------------------------------------------------------------

def get_catalog_url() -> str:
    """Return the explicitly provisioned signed-catalog endpoint, if any."""
    env = os.environ.get("AETHERION_CATALOG_URL", "").strip()
    if env:
        return env
    return str(_load_config().get("catalog_url", DEFAULT_CATALOG_URL) or "").strip()


def set_catalog_url(url: str) -> None:
    cfg = _load_config()
    cfg["catalog_url"] = url.strip()
    _save_config(cfg)


def get_catalog_trust_root() -> str:
    """Return an explicit operator-provisioned public-key-set path, if any."""
    env = os.environ.get("AETHERION_CATALOG_TRUST_ROOT", "").strip()
    if env:
        return env
    return str(_load_config().get("catalog_trust_root", DEFAULT_CATALOG_TRUST_ROOT) or "").strip()


def set_catalog_trust_root(path: str) -> None:
    cfg = _load_config()
    cfg["catalog_trust_root"] = path.strip()
    _save_config(cfg)


def get_catalog_keyset() -> str:
    """Return the explicitly provisioned delegated catalog-keyset path, if any."""
    env = os.environ.get("AETHERION_CATALOG_KEYSET", "").strip()
    if env:
        return env
    return str(_load_config().get("catalog_keyset", DEFAULT_CATALOG_KEYSET) or "").strip()


def set_catalog_keyset(path: str) -> None:
    cfg = _load_config()
    cfg["catalog_keyset"] = path.strip()
    _save_config(cfg)


def get_catalog_trust_state() -> str:
    """Return the lifecycle state path used for keyset rollback protection."""
    env = os.environ.get("AETHERION_CATALOG_TRUST_STATE", "").strip()
    if env:
        return env
    return str(_load_config().get("catalog_trust_state", DEFAULT_CATALOG_TRUST_STATE) or "").strip()


def set_catalog_trust_state(path: str) -> None:
    cfg = _load_config()
    cfg["catalog_trust_state"] = path.strip()
    _save_config(cfg)


# ---------------------------------------------------------------------------
# Auth token
# ---------------------------------------------------------------------------

def get_token() -> Optional[str]:
    # 1. Environment variable (CI / Docker / scripts)
    env = os.environ.get("AETHERION_TOKEN", "").strip()
    if env:
        return env

    # 2. OS keyring (most secure for interactive use)
    try:
        import keyring  # noqa: PLC0415
        val = keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)
        if val:
            return val
    except Exception:
        pass

    # 3. Config file fallback
    cfg = _load_config()
    return cfg.get("token") or cfg.get("access_token") or None


def set_token(token: str) -> None:
    # Try keyring first
    try:
        import keyring  # noqa: PLC0415
        keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY, token)
        # Remove from config file if it was stored there
        cfg = _load_config()
        cfg.pop("token", None)
        cfg.pop("access_token", None)
        _save_config(cfg)
        return
    except Exception:
        pass

    # Fall back to config file
    cfg = _load_config()
    cfg["token"] = token
    cfg.pop("access_token", None)
    _save_config(cfg)


def clear_token() -> None:
    # Remove from keyring
    try:
        import keyring  # noqa: PLC0415
        keyring.delete_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)
    except Exception:
        pass

    # Remove from config file
    cfg = _load_config()
    cfg.pop("token", None)
    cfg.pop("access_token", None)
    _save_config(cfg)


# ---------------------------------------------------------------------------
# Refresh token (30-day, stored separately from access token)
# ---------------------------------------------------------------------------

def get_refresh_token() -> Optional[str]:
    """Retrieve stored refresh token. Used for silent re-auth at startup."""
    # 1. OS keyring
    try:
        import keyring  # noqa: PLC0415
        val = keyring.get_password(KEYRING_SERVICE, KEYRING_REFRESH_KEY)
        if val:
            return val
    except Exception:
        pass
    # 2. Config file fallback
    return _load_config().get("refresh_token") or None


def set_refresh_token(token: str) -> None:
    """Persist refresh token to keyring, falling back to config file."""
    try:
        import keyring  # noqa: PLC0415
        keyring.set_password(KEYRING_SERVICE, KEYRING_REFRESH_KEY, token)
        cfg = _load_config()
        cfg.pop("refresh_token", None)
        _save_config(cfg)
        return
    except Exception:
        pass
    cfg = _load_config()
    cfg["refresh_token"] = token
    _save_config(cfg)


def clear_refresh_token() -> None:
    """Delete stored refresh token."""
    try:
        import keyring  # noqa: PLC0415
        keyring.delete_password(KEYRING_SERVICE, KEYRING_REFRESH_KEY)
    except Exception:
        pass
    cfg = _load_config()
    cfg.pop("refresh_token", None)
    _save_config(cfg)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def get_model() -> str:
    env = os.environ.get("AETHERION_MODEL", "").strip()
    if env:
        return env
    return _load_config().get("model", DEFAULT_MODEL)


def set_model(model: str) -> None:
    cfg = _load_config()
    cfg["model"] = model
    _save_config(cfg)


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------

def get_output_format() -> str:
    """Return 'table' | 'json' | 'plain'. Defaults to 'table'."""
    return _load_config().get("output_format", "table")


def set_output_format(fmt: str) -> None:
    cfg = _load_config()
    cfg["output_format"] = fmt
    _save_config(cfg)


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

def get_active_workspace() -> Optional[str]:
    """Retrieve currently selected workspace ID."""
    env = os.environ.get("AETHERION_WORKSPACE", "").strip()
    if env:
        return env
    return _load_config().get("workspace_id")


def set_active_workspace(workspace_id: str) -> None:
    """Persist active workspace selection."""
    cfg = _load_config()
    cfg["workspace_id"] = workspace_id.strip()
    _save_config(cfg)


# ---------------------------------------------------------------------------
# Settings Inspector & Mutator
# ---------------------------------------------------------------------------

def config_path() -> Path:
    return CONFIG_FILE


def all_config() -> dict:
    """Return full config dict for display (masks token)."""
    token = get_token()
    return {
        "server_url": get_server_url(),
        "catalog_url": get_catalog_url(),
        "catalog_trust_root": get_catalog_trust_root(),
        "catalog_trust_root_configured": bool(get_catalog_trust_root()),
        "catalog_keyset": get_catalog_keyset(),
        "catalog_keyset_configured": bool(get_catalog_keyset()),
        "catalog_trust_state": get_catalog_trust_state(),
        "model": get_model(),
        "output_format": get_output_format(),
        "workspace_id": get_active_workspace(),
        "token_set": bool(token),
        "token_source": _token_source(),
        "config_file": str(CONFIG_FILE),
    }


def get_all_settings(redacted: bool = True) -> Dict[str, Any]:
    """Return all configuration key-values, optionally redacting sensitive fields."""
    data = all_config()
    if redacted:
        return redact_secrets(data)
    return data


VALID_CONFIG_KEYS = {
    "server_url",
    "catalog_url",
    "catalog_trust_root",
    "catalog_keyset",
    "catalog_trust_state",
    "model",
    "default_model",
    "output",
    "output_format",
    "workspace",
    "workspace_id",
}


def set_setting(key: str, value: str) -> None:
    """Set a validated configuration key."""
    if key not in VALID_CONFIG_KEYS:
        raise ValueError(
            f"Invalid configuration key '{key}'. Allowed keys are: {', '.join(sorted(VALID_CONFIG_KEYS))}"
        )
    if key == "server_url":
        set_server_url(value)
    elif key == "catalog_url":
        set_catalog_url(value)
    elif key == "catalog_trust_root":
        set_catalog_trust_root(value)
    elif key == "catalog_keyset":
        set_catalog_keyset(value)
    elif key == "catalog_trust_state":
        set_catalog_trust_state(value)
    elif key in ("model", "default_model"):
        set_model(value)
    elif key in ("output", "output_format"):
        set_output_format(value)
    elif key in ("workspace", "workspace_id"):
        set_active_workspace(value)


def _token_source() -> str:
    if os.environ.get("AETHERION_TOKEN"):
        return "env"
    try:
        import keyring  # noqa: PLC0415
        if keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY):
            return "keyring"
    except Exception:
        pass
    cfg = _load_config()
    if cfg.get("token") or cfg.get("access_token"):
        return "config_file"
    return "none"
