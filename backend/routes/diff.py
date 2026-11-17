"""Tenant-authorized task diff inspection API.

The route intentionally resolves an opaque durable task ID first. It never
turns the request ID into a filesystem path and never falls back to the current
working directory.
"""
from __future__ import annotations

import json
import logging
from flask import Blueprint, g, jsonify
from auth_system import token_required
from execution import durable_state
from execution.diff_policy import (
    DiffExtractionError,
    diff_payload,
    ensure_diff_text_bounded,
    error_payload,
)
from execution.worktree import WorktreeError, get_authorized_worktree, worktree_diff

diff_bp = Blueprint("diff", __name__)
logger = logging.getLogger("aetherion.routes.diff")


def _tenant_id() -> str:
    user_id = getattr(g, "user_id", None)
    return str(user_id) if user_id is not None else "default_tenant"


def _valid_task_id(task_id: str) -> bool:
    """Task IDs are opaque identifiers, never path expressions."""
    if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
        return False
    if task_id in {".", ".."} or task_id != task_id.strip():
        return False
    return not any(token in task_id for token in ("/", "\\", "..", "\x00", ":"))


def _failure(task_id: str, status: int, code: str, message: str):
    return jsonify(error_payload(code, message, task_id=task_id)), status


@diff_bp.route("/api/diff/<task_id>", methods=["GET"])
@token_required
def get_diff(task_id: str):
    """Retrieve a tenant-authorized, bounded diff for a durable task."""
    if not _valid_task_id(task_id):
        return _failure(task_id, 400, "INVALID_TASK_ID", "Invalid task identifier")

    tenant_id = _tenant_id()
    record = durable_state.get(task_id, tenant_id)
    if not record:
        return _failure(task_id, 404, "TASK_NOT_FOUND", "Task not found")

    try:
        result = json.loads(record.get("result_json") or "{}") if record.get("result_json") else {}
        payload = json.loads(record.get("payload_json") or "{}") if record.get("payload_json") else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Malformed diff record for task %s", task_id)
        return _failure(task_id, 500, "DIFF_RECORD_INVALID", "Diff record is invalid")

    # An explicit stored empty string is a truthful successful no-change result.
    if "diff" in result:
        if not isinstance(result["diff"], str):
            return _failure(task_id, 500, "DIFF_RECORD_INVALID", "Diff record is invalid")
        try:
            return jsonify(diff_payload(task_id, result["diff"]))
        except DiffExtractionError:
            return _failure(task_id, 413, "DIFF_LIMIT_EXCEEDED", "Diff exceeds the permitted response limit")

    wt_id = payload.get("worktree_id")
    if not isinstance(wt_id, str) or not wt_id:
        return _failure(task_id, 409, "DIFF_UNAVAILABLE", "A complete diff is not available")

    try:
        wt = get_authorized_worktree(wt_id, tenant_id, execution_id=task_id)
        diff_text = worktree_diff(
            wt_id,
            tenant_id=tenant_id,
            session_id=wt.session_id,
            execution_id=task_id,
        )
        ensure_diff_text_bounded(diff_text)
        return jsonify(diff_payload(task_id, diff_text))
    except WorktreeError:
        return _failure(task_id, 404, "DIFF_NOT_FOUND", "Diff not found")
    except DiffExtractionError:
        return _failure(task_id, 413, "DIFF_LIMIT_EXCEEDED", "Diff exceeds the permitted response limit")
    except Exception:
        logger.warning("Authorized diff extraction failed for task %s", task_id)
        return _failure(task_id, 502, "DIFF_UNAVAILABLE", "Diff could not be retrieved")
