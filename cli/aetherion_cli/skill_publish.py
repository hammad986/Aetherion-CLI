"""Shared publish response and reconciliation contract for CLI and TUI."""
from __future__ import annotations

import re
from typing import Any, Callable


class PublishProtocolError(Exception):
    """The server response did not prove the requested publish outcome."""


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise PublishProtocolError("server returned an invalid content digest")
    return value.lower()


def _check_skill(skill: Any, expected: dict[str, str]) -> dict[str, Any]:
    if not isinstance(skill, dict):
        raise PublishProtocolError("server response did not contain a published skill")
    if skill.get("id") != expected["skill_id"]:
        raise PublishProtocolError("server returned a different skill ID")
    if skill.get("version") != expected["version"]:
        raise PublishProtocolError("server returned a different skill version")
    if skill.get("name") != expected["name"]:
        raise PublishProtocolError("server returned different skill metadata")
    if skill.get("description", "") != expected.get("description", ""):
        raise PublishProtocolError("server returned different skill metadata")
    if skill.get("source_kind") != "authenticated_publish":
        raise PublishProtocolError("server response was not an authenticated publication")
    if skill.get("is_enabled") is not False:
        raise PublishProtocolError("server did not preserve the execution-disabled publish state")
    digest = _digest(skill.get("content_hash"))
    if digest != expected["content_sha256"].lower():
        raise PublishProtocolError("server content digest does not match the submitted content")
    return skill


def validate_publish_response(response: Any, expected: dict[str, str]) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise PublishProtocolError("server did not accept the skill publication")
    if str(response.get("outcome", "")).lower() not in {"created", "already_current"}:
        raise PublishProtocolError("server returned an unknown publish outcome")
    if response.get("execution_enabled") is not False:
        raise PublishProtocolError("server did not prove execution remains disabled")
    status = response.get("status")
    if status not in {"published_to_tenant", "pending_approval"}:
        raise PublishProtocolError("server returned an unknown publication status")
    skill = _check_skill(response.get("skill"), expected)
    return {"outcome": str(response["outcome"]).lower(), "status": status, "skill": skill, "reconciled": False}


def validate_authoritative_get(response: Any, expected: dict[str, str]) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise PublishProtocolError("authoritative skill lookup did not succeed")
    skill = _check_skill(response.get("skill"), expected)
    return {"outcome": "reconciled", "status": response.get("status", "published_to_tenant"), "skill": skill, "reconciled": True}


def reconcile_publish(getter: Callable[[str], Any], expected: dict[str, str]) -> dict[str, Any]:
    """Prove a possibly committed publish using an authoritative GET."""
    return validate_authoritative_get(getter(f"/api/skills/{expected['skill_id']}"), expected)
