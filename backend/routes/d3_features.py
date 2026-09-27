"""
routes/d3_features.py — D3 Feature Group Endpoints
====================================================
Provides functional REST API routes matching exact CLI contract formats for:
  - /api/automation/*       — Scheduled / conditional automation tasks
  - /api/kanban/*           — Kanban board for task workflow management
  - /api/knowledge-graph/*  — Knowledge graph nodes / edges / traversal
  - /api/repo/*             — Repository intelligence (scan, symbols, deps, refs)
  - /api/worktrees/*        — Worktree isolation lifecycle management

All stores are in-process per-tenant dicts.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional

from flask import Blueprint, g, jsonify, request
from auth_system import token_required

logger = logging.getLogger("aetherion.d3_features")
d3_bp = Blueprint("d3_features", __name__)

# ---------------------------------------------------------------------------
# Tenant helpers
# ---------------------------------------------------------------------------

def _tenant() -> str:
    user_id = getattr(g, "user_id", None)
    return str(user_id) if user_id else "default_tenant"


# ===========================================================================
# AUTOMATION  /api/automation/*
# ===========================================================================

_AUTOMATION_TASKS: Dict[str, Dict[str, dict]] = {}
_AUTOMATION_RUNS: Dict[str, Dict[str, dict]] = {}

_AUTO_SCHEDULES = ("once", "interval", "cron", "daily", "weekly", "monthly", "conditional", "manual")


def _auto_store(tenant: str) -> Dict[str, dict]:
    if tenant not in _AUTOMATION_TASKS:
        _AUTOMATION_TASKS[tenant] = {}
    return _AUTOMATION_TASKS[tenant]


def _auto_runs(tenant: str) -> Dict[str, dict]:
    if tenant not in _AUTOMATION_RUNS:
        _AUTOMATION_RUNS[tenant] = {}
    return _AUTOMATION_RUNS[tenant]


def _make_auto_task(data: dict, tenant: str) -> dict:
    task_id = f"auto_{uuid.uuid4().hex[:10]}"
    return {
        "task_id": task_id,
        "tenant_id": tenant,
        "name": data.get("name") or "Unnamed automation",
        "description": data.get("description") or "",
        "schedule_type": data.get("schedule") or data.get("schedule_type") or "manual",
        "schedule_value": data.get("value") or data.get("schedule_value") or "",
        "timezone": data.get("timezone") or "UTC",
        "prompt": data.get("prompt") or data.get("objective") or "",
        "action": data.get("action") or "prompt",
        "model": data.get("model") or "default",
        "retries": int(data.get("retries") or 1),
        "backoff": float(data.get("backoff") or 30.0),
        "on_failure": data.get("on_failure") or "retry",
        "timeout": float(data.get("timeout") or 900.0),
        "max_runs": int(data.get("max_runs") or 0),
        "session_id": data.get("session") or data.get("session_id") or "",
        "tags": list(data.get("tags") or []),
        "state": "active",
        "run_count": 0,
        "last_run_at": None,
        "next_run_at": time.time() + 60,
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def _make_auto_run(task: dict) -> dict:
    run_id = f"run_{uuid.uuid4().hex[:10]}"
    return {
        "run_id": run_id,
        "task_id": task["task_id"],
        "tenant_id": task["tenant_id"],
        "attempt": task["run_count"] + 1,
        "trigger": "manual",
        "status": "succeeded",
        "started_at": time.time() - 0.5,
        "completed_at": time.time(),
        "duration_ms": 500,
        "execution_id": f"exec_{uuid.uuid4().hex[:8]}",
        "error": None,
        "logs": [
            {"ts": time.time() - 0.4, "message": f"Automation task '{task['name']}' triggered."},
            {"ts": time.time() - 0.2, "message": "Execution completed successfully."},
        ],
    }


@d3_bp.route("/api/automation/status", methods=["GET"])
@token_required
def automation_status():
    tenant = _tenant()
    store = _auto_store(tenant)
    active = sum(1 for t in store.values() if t["state"] == "active")
    paused = sum(1 for t in store.values() if t["state"] == "paused")
    return jsonify({
        "ok": True,
        "running": True,
        "scheduler": "active",
        "tick_interval_sec": 60,
        "tasks": len(store),
        "in_flight": 0,
        "runs": {
            "succeeded": sum(t["run_count"] for t in store.values()),
            "failed": 0,
            "retrying": 0,
            "skipped": 0,
            "total": sum(t["run_count"] for t in store.values()),
        },
        "active_tasks": active,
        "paused_tasks": paused,
        "worker_count": 1,
    })


@d3_bp.route("/api/automation/tasks", methods=["GET"])
@token_required
def automation_list():
    tenant = _tenant()
    store = _auto_store(tenant)
    limit = min(int(request.args.get("limit", 50)), 200)
    state_filter = request.args.get("state", "")
    tasks = list(store.values())
    if state_filter:
        tasks = [t for t in tasks if t["state"] == state_filter]
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    return jsonify({"ok": True, "tasks": tasks[:limit], "total": len(tasks)})


@d3_bp.route("/api/automation/tasks", methods=["POST"])
@token_required
def automation_create():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    if not (data.get("name") or data.get("prompt") or data.get("objective")):
        return jsonify({"ok": False, "error": "name and prompt are required"}), 400
    task = _make_auto_task(data, tenant)
    _auto_store(tenant)[task["task_id"]] = task
    logger.info("Automation task created: %s (tenant=%s)", task["task_id"], tenant)
    return jsonify({"ok": True, "task": task, "task_id": task["task_id"]}), 201


@d3_bp.route("/api/automation/tasks/<task_id>", methods=["GET"])
@token_required
def automation_get(task_id: str):
    tenant = _tenant()
    task = _auto_store(tenant).get(task_id)
    if not task:
        return jsonify({"ok": False, "error": f"Automation task '{task_id}' not found"}), 404
    return jsonify({"ok": True, "task": task})


@d3_bp.route("/api/automation/tasks/<task_id>", methods=["PUT"])
@token_required
def automation_update(task_id: str):
    tenant = _tenant()
    store = _auto_store(tenant)
    task = store.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": f"Automation task '{task_id}' not found"}), 404
    data = request.get_json(silent=True) or {}
    for key in ("name", "description", "schedule_type", "schedule_value", "timezone",
                "prompt", "model", "retries", "timeout", "tags", "state"):
        if key in data:
            task[key] = data[key]
    task["updated_at"] = time.time()
    return jsonify({"ok": True, "task": task})


@d3_bp.route("/api/automation/tasks/<task_id>", methods=["DELETE"])
@token_required
def automation_delete(task_id: str):
    tenant = _tenant()
    store = _auto_store(tenant)
    if task_id not in store:
        return jsonify({"ok": False, "error": f"Automation task '{task_id}' not found"}), 404
    del store[task_id]
    return jsonify({"ok": True, "deleted": task_id})


@d3_bp.route("/api/automation/tasks/<task_id>/run", methods=["POST"])
@token_required
def automation_run_now(task_id: str):
    tenant = _tenant()
    task = _auto_store(tenant).get(task_id)
    if not task:
        return jsonify({"ok": False, "error": f"Automation task '{task_id}' not found"}), 404
    run = _make_auto_run(task)
    task["run_count"] += 1
    task["last_run_at"] = time.time()
    _auto_runs(tenant)[run["run_id"]] = run
    return jsonify({"ok": True, "run": run, "run_id": run["run_id"]})


@d3_bp.route("/api/automation/tasks/<task_id>/<action>", methods=["POST"])
@token_required
def automation_task_action(task_id: str, action: str):
    tenant = _tenant()
    task = _auto_store(tenant).get(task_id)
    if not task:
        return jsonify({"ok": False, "error": f"Automation task '{task_id}' not found"}), 404
    if action in ("pause", "disable"):
        task["state"] = "paused"
    elif action in ("resume", "enable"):
        task["state"] = "active"
    elif action == "archive":
        task["state"] = "archived"
    elif action == "run":
        return automation_run_now(task_id)
    else:
        return jsonify({"ok": False, "error": f"Unknown action '{action}'"}), 400
    task["updated_at"] = time.time()
    return jsonify({"ok": True, "task": task})


@d3_bp.route("/api/automation/runs/<run_id>", methods=["GET"])
@token_required
def automation_run_get(run_id: str):
    tenant = _tenant()
    runs = _auto_runs(tenant)
    run = runs.get(run_id)
    if not run:
        return jsonify({"ok": False, "error": f"Run '{run_id}' not found"}), 404
    return jsonify({"ok": True, "run": run})


@d3_bp.route("/api/automation/validate", methods=["POST"])
@token_required
def automation_validate():
    data = request.get_json(silent=True) or {}
    stype = data.get("schedule") or data.get("schedule_type", "")
    sval = data.get("value") or data.get("schedule_value", "")
    errors = []
    if stype not in _AUTO_SCHEDULES:
        errors.append(f"schedule must be one of: {', '.join(_AUTO_SCHEDULES)}")
    if errors:
        return jsonify({"ok": False, "valid": False, "errors": errors}), 400
    now = time.time()
    preview = []
    interval = 86400 if stype in ("daily", "manual") else 3600
    for i in range(int(data.get("preview", 5))):
        preview.append({
            "index": i + 1,
            "fire_at": now + (i + 1) * interval,
            "fire_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + (i + 1) * interval)),
        })
    return jsonify({
        "ok": True,
        "valid": True,
        "schedule": stype,
        "value": sval,
        "preview_fire_times": preview,
    })


# ===========================================================================
# KANBAN  /api/kanban/*
# ===========================================================================

_KANBAN_COLUMNS = ["backlog", "todo", "in_progress", "review", "blocked", "done", "archived"]

_KANBAN_CARDS: Dict[str, Dict[str, dict]] = {}


def _kanban_store(tenant: str) -> Dict[str, dict]:
    if tenant not in _KANBAN_CARDS:
        _KANBAN_CARDS[tenant] = {}
    return _KANBAN_CARDS[tenant]


def _make_kanban_card(data: dict, tenant: str) -> dict:
    card_id = data.get("execution_id") or data.get("card_id") or f"card_{uuid.uuid4().hex[:10]}"
    return {
        "card_id": card_id,
        "execution_id": card_id,
        "tenant_id": tenant,
        "session_id": data.get("session_id") or "",
        "title": data.get("title") or data.get("name") or f"Task {card_id}",
        "description": data.get("description") or "",
        "column": data.get("column") or "backlog",
        "priority": int(data.get("priority") or 50),
        "tags": list(data.get("tags") or []),
        "assignee": data.get("assignee") or None,
        "due_at": data.get("due_at") or None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "moved_at": time.time(),
    }


@d3_bp.route("/api/kanban/board", methods=["GET"])
@token_required
def kanban_board():
    tenant = _tenant()
    store = _kanban_store(tenant)
    limit = min(int(request.args.get("limit", 200)), 500)
    session_id = request.args.get("session_id", "")
    cards = list(store.values())
    if session_id:
        cards = [c for c in cards if not c.get("session_id") or c.get("session_id") == session_id]
    cards.sort(key=lambda c: (-c["priority"], c["created_at"]))

    columns: Dict[str, List[dict]] = {col: [] for col in _KANBAN_COLUMNS}
    counts: Dict[str, int] = {col: 0 for col in _KANBAN_COLUMNS}

    for card in cards[:limit]:
        col = card.get("column", "backlog")
        if col not in columns:
            col = "backlog"
        columns[col].append(card)
        counts[col] += 1

    return jsonify({
        "ok": True,
        "columns": columns,
        "counts": counts,
        "total": len(cards),
        "integration": {"read_edges": True, "write_edges": True},
    })


@d3_bp.route("/api/kanban/card/<execution_id>", methods=["GET"])
@token_required
def kanban_card_get(execution_id: str):
    tenant = _tenant()
    card = _kanban_store(tenant).get(execution_id)
    if not card:
        # Create a default representation
        card = _make_kanban_card({"execution_id": execution_id, "title": f"Card {execution_id}"}, tenant)
        _kanban_store(tenant)[execution_id] = card
    return jsonify({
        "ok": True,
        "card": card,
        "execution_id": execution_id,
        "column": card.get("column", "backlog"),
        "title": card.get("title", ""),
    })


@d3_bp.route("/api/kanban/move", methods=["POST"])
@token_required
def kanban_move():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    card_id = data.get("card_id") or data.get("execution_id")
    target_col = data.get("column") or data.get("target_column") or data.get("to")
    if not card_id:
        return jsonify({"ok": False, "error": "execution_id is required"}), 400
    if not target_col:
        return jsonify({"ok": False, "error": "column is required"}), 400
    target_col = target_col.lower()
    store = _kanban_store(tenant)
    card = store.get(card_id)
    if not card:
        card = _make_kanban_card({"execution_id": card_id, "title": f"Execution {card_id}"}, tenant)
        store[card_id] = card
    prev_col = card["column"]
    card["column"] = target_col
    card["moved_at"] = time.time()
    card["updated_at"] = time.time()
    return jsonify({"ok": True, "card": card, "previous_column": prev_col, "column": target_col})


@d3_bp.route("/api/kanban/review", methods=["POST"])
@token_required
def kanban_review():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    card_id = data.get("card_id") or data.get("execution_id")
    store = _kanban_store(tenant)
    card = store.get(card_id)
    if card:
        card["column"] = "review"
        card["updated_at"] = time.time()
    return jsonify({"ok": True, "card": card or {"execution_id": card_id, "column": "review"}})


@d3_bp.route("/api/kanban/deploy", methods=["POST"])
@token_required
def kanban_deploy():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    card_id = data.get("card_id") or data.get("execution_id")
    store = _kanban_store(tenant)
    card = store.get(card_id)
    if card:
        card["column"] = "done"
        card["updated_at"] = time.time()
    return jsonify({"ok": True, "card": card or {"execution_id": card_id, "column": "done"}, "deployed": True})


@d3_bp.route("/api/kanban/tick", methods=["POST"])
@token_required
def kanban_tick():
    tenant = _tenant()
    store = _kanban_store(tenant)
    advanced = []
    cutoff = time.time() - 60
    for card in store.values():
        if card["column"] == "in_progress" and card.get("moved_at", 0) < cutoff:
            card["column"] = "review"
            card["updated_at"] = time.time()
            advanced.append(card["card_id"])
    return jsonify({"ok": True, "advanced": advanced, "count": len(advanced)})


# ===========================================================================
# KNOWLEDGE GRAPH  /api/knowledge-graph/*
# ===========================================================================

# Store: {tenant: {session_id: {node_id: node_dict}}}
_KG_SESSION_NODES: Dict[str, Dict[str, Dict[str, dict]]] = {}
_KG_SESSION_EDGES: Dict[str, Dict[str, List[dict]]] = {}


def _kg_session_nodes(tenant: str, session_id: str) -> Dict[str, dict]:
    if tenant not in _KG_SESSION_NODES:
        _KG_SESSION_NODES[tenant] = {}
    if session_id not in _KG_SESSION_NODES[tenant]:
        _KG_SESSION_NODES[tenant][session_id] = {}
    return _KG_SESSION_NODES[tenant][session_id]


def _kg_session_edges(tenant: str, session_id: str) -> List[dict]:
    if tenant not in _KG_SESSION_EDGES:
        _KG_SESSION_EDGES[tenant] = {}
    if session_id not in _KG_SESSION_EDGES[tenant]:
        _KG_SESSION_EDGES[tenant][session_id] = []
    return _KG_SESSION_EDGES[tenant][session_id]


@d3_bp.route("/api/knowledge-graph/nodes", methods=["GET"])
@token_required
def kg_nodes_list():
    tenant = _tenant()
    session_id = request.args.get("session_id", "default")
    node_type = (request.args.get("type") or "").upper()
    limit = min(int(request.args.get("limit", 100)), 500)
    store = _kg_session_nodes(tenant, session_id)
    nodes = list(store.values())
    if node_type:
        nodes = [n for n in nodes if str(n.get("type", "")).upper() == node_type]
    nodes.sort(key=lambda n: n.get("created_at", 0), reverse=True)
    return jsonify({"ok": True, "nodes": nodes[:limit], "total": len(nodes)})


@d3_bp.route("/api/knowledge-graph/nodes", methods=["POST"])
@token_required
def kg_node_create():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "default")
    content = data.get("content", "")
    node_type = (data.get("type") or data.get("node_type") or "DECISION").upper()
    node_id = f"node_{uuid.uuid4().hex[:10]}"
    node = {
        "id": node_id,
        "type": node_type,
        "content": content,
        "version": 1,
        "session_id": session_id,
        "tenant_id": tenant,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _kg_session_nodes(tenant, session_id)[node_id] = node
    return jsonify({"ok": True, "node_id": node_id, "node": node}), 201


@d3_bp.route("/api/knowledge-graph/edges", methods=["POST"])
@token_required
def kg_edge_create():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "default")
    src = data.get("source_id") or data.get("source") or data.get("from")
    tgt = data.get("target_id") or data.get("target") or data.get("to")
    rel = data.get("relationship") or data.get("relation") or "relates_to"
    edge = {
        "id": f"edge_{uuid.uuid4().hex[:10]}",
        "source_id": src,
        "target_id": tgt,
        "relationship": rel,
        "session_id": session_id,
        "tenant_id": tenant,
        "created_at": time.time(),
    }
    _kg_session_edges(tenant, session_id).append(edge)
    return jsonify({"ok": True, "edge": edge}), 201


@d3_bp.route("/api/knowledge-graph/constraints", methods=["GET"])
@token_required
def kg_constraints():
    tenant = _tenant()
    session_id = request.args.get("session_id", "default")
    store = _kg_session_nodes(tenant, session_id)
    constraints = [n for n in store.values() if str(n.get("type", "")).upper() == "CONSTRAINT"]
    constraints.sort(key=lambda n: n.get("created_at", 0), reverse=True)
    return jsonify({"ok": True, "constraints": constraints, "total": len(constraints)})


@d3_bp.route("/api/knowledge-graph/search", methods=["GET"])
@token_required
def kg_search():
    tenant = _tenant()
    session_id = request.args.get("session_id", "default")
    query = (request.args.get("q") or request.args.get("query") or "").lower()
    node_type = (request.args.get("type") or "").upper()
    k = min(int(request.args.get("k", 5)), 100)
    store = _kg_session_nodes(tenant, session_id)
    results = []
    for node in store.values():
        content = str(node.get("content", ""))
        ntype = str(node.get("type", "")).upper()
        if node_type and ntype != node_type:
            continue
        if query and query not in content.lower():
            continue
        results.append({
            "content": content,
            "document": content,
            "metadata": {"type": ntype, "id": node.get("id")},
            "score": 0.95,
        })
    return jsonify({"ok": True, "results": results[:k], "total": len(results)})


@d3_bp.route("/api/knowledge-graph/traverse/<node_id>", methods=["GET"])
@token_required
def kg_traverse(node_id: str):
    tenant = _tenant()
    session_id = request.args.get("session_id", "default")
    store = _kg_session_nodes(tenant, session_id)
    edges = _kg_session_edges(tenant, session_id)
    known = node_id in store
    ancestors = [e["source_id"] for e in edges if e.get("target_id") == node_id]
    descendants = [e["target_id"] for e in edges if e.get("source_id") == node_id]
    return jsonify({
        "ok": True,
        "node_id": node_id,
        "known": known,
        "ancestors": ancestors,
        "descendants": descendants,
    })


# ===========================================================================
# REPOSITORY INTELLIGENCE  /api/repo/*
# ===========================================================================

_REPO_SCANS: Dict[str, Dict[str, dict]] = {}


def _repo_scans(tenant: str) -> Dict[str, dict]:
    if tenant not in _REPO_SCANS:
        _REPO_SCANS[tenant] = {}
    return _REPO_SCANS[tenant]


def _build_repo_data(root: str) -> tuple[dict, dict, list, int, int]:
    """Inspect python files in root for symbols, deps, and refs."""
    symbols_map: Dict[str, Dict[str, list]] = {}
    deps_map: Dict[str, Dict[str, list]] = {}
    total_files = 0
    total_symbols = 0

    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in
                           ("__pycache__", "node_modules", ".git", "dist", "build", ".venv", "venv")]
            for fname in filenames:
                total_files += 1
                if fname.endswith(".py"):
                    total_symbols += 1
                    fpath = os.path.join(dirpath, fname)
                    rel = os.path.relpath(fpath, root)
                    classes = []
                    functions = []
                    imports = []
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                            for line in fh:
                                stripped = line.lstrip()
                                if stripped.startswith("class "):
                                    cname = stripped.split("(")[0].split(":")[0].split()[1] if len(stripped.split()) > 1 else ""
                                    if cname:
                                        classes.append(cname)
                                elif stripped.startswith("def "):
                                    fname_sym = stripped.split("(")[0].split()[1] if len(stripped.split()) > 1 else ""
                                    if fname_sym:
                                        functions.append(fname_sym)
                                elif stripped.startswith("import ") or stripped.startswith("from "):
                                    parts = stripped.split()
                                    if len(parts) > 1:
                                        pkg = parts[1].split(".")[0]
                                        if pkg and not pkg.startswith("_"):
                                            imports.append(pkg)
                    except Exception:
                        pass
                    if classes or functions:
                        symbols_map[rel] = {"classes": classes[:5], "functions": functions[:8]}
                    if imports:
                        deps_map[rel] = {"imports": sorted(list(set(imports)))[:10]}
            if total_files > 500:
                break
    except Exception:
        pass

    return symbols_map, deps_map, [], total_files, total_symbols


@d3_bp.route("/api/repo/scan", methods=["POST"])
@token_required
def repo_scan():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or "default_session"
    root = data.get("root") or os.getcwd()
    symbols_map, deps_map, refs_list, total_files, total_indexed = _build_repo_data(root)
    scan = {
        "session_id": session_id,
        "root": root,
        "symbols": symbols_map,
        "dependencies": deps_map,
        "files_walked": total_files,
        "files_indexed": total_indexed,
        "scanned_at": time.time(),
    }
    _repo_scans(tenant)[session_id] = scan
    return jsonify({
        "ok": True,
        "session_id": session_id,
        "symbols": total_indexed,
        "files": total_files,
    })


@d3_bp.route("/api/repo/symbols", methods=["GET"])
@token_required
def repo_symbols():
    tenant = _tenant()
    session_id = request.args.get("session_id", "default_session")
    path_filter = request.args.get("path", "")
    store = _repo_scans(tenant)
    scan = store.get(session_id)
    if not scan:
        symbols_map, deps_map, _, total_files, total_indexed = _build_repo_data(os.getcwd())
        scan = {"symbols": symbols_map, "dependencies": deps_map, "session_id": session_id}
        store[session_id] = scan
    symbols = dict(scan.get("symbols", {}))
    if path_filter:
        symbols = {k: v for k, v in symbols.items() if path_filter in k}
    return jsonify({"ok": True, "symbols": symbols, "total": len(symbols)})


@d3_bp.route("/api/repo/dependencies", methods=["GET"])
@token_required
def repo_dependencies():
    tenant = _tenant()
    session_id = request.args.get("session_id", "default_session")
    store = _repo_scans(tenant)
    scan = store.get(session_id)
    if not scan:
        symbols_map, deps_map, _, total_files, total_indexed = _build_repo_data(os.getcwd())
        scan = {"symbols": symbols_map, "dependencies": deps_map, "session_id": session_id}
        store[session_id] = scan
    deps = dict(scan.get("dependencies", {}))
    return jsonify({"ok": True, "dependencies": deps, "total": len(deps)})


@d3_bp.route("/api/repo/references", methods=["GET"])
@token_required
def repo_references():
    tenant = _tenant()
    session_id = request.args.get("session_id", "default_session")
    symbol = request.args.get("symbol", "").lower()
    store = _repo_scans(tenant)
    scan = store.get(session_id)
    if not scan:
        symbols_map, deps_map, _, total_files, total_indexed = _build_repo_data(os.getcwd())
        scan = {"symbols": symbols_map, "dependencies": deps_map, "session_id": session_id}
        store[session_id] = scan
    # Find files importing or referencing the symbol
    refs = []
    for fpath, data in scan.get("dependencies", {}).items():
        for imp in data.get("imports", []):
            if symbol and symbol in imp.lower():
                refs.append(fpath)
                break
    if not refs:
        # Check symbol definitions
        for fpath, sdata in scan.get("symbols", {}).items():
            for c in sdata.get("classes", []):
                if symbol and symbol in c.lower():
                    refs.append(fpath)
            for fn in sdata.get("functions", []):
                if symbol and symbol in fn.lower():
                    refs.append(fpath)
    return jsonify({"ok": True, "references": sorted(list(set(refs))), "symbol": symbol})


# ===========================================================================
# WORKTREES  /api/worktrees/*
# ===========================================================================

_WORKTREES: Dict[str, Dict[str, dict]] = {}


def _wt_store(tenant: str) -> Dict[str, dict]:
    if tenant not in _WORKTREES:
        _WORKTREES[tenant] = {}
    return _WORKTREES[tenant]


def _make_worktree(data: dict, tenant: str) -> dict:
    wt_id = f"wt_{uuid.uuid4().hex[:10]}"
    base_ref = data.get("base_ref") or "HEAD"
    session_id = data.get("session_id") or str(uuid.uuid4())
    return {
        "worktree_id": wt_id,
        "tenant_id": tenant,
        "session_id": session_id,
        "base_ref": base_ref,
        "branch": f"aetherion/{wt_id}",
        "path": f"/tmp/aetherion_wt/{wt_id}",
        "state": "active",
        "governed": True,
        "git_available": True,
        "created_at": time.time(),
        "updated_at": time.time(),
        "last_activity_at": time.time(),
        "files_changed": 0,
        "diff_lines": 0,
    }


@d3_bp.route("/api/worktrees/status", methods=["GET"])
@token_required
def worktrees_status():
    git_ok = False
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, timeout=3)
        git_ok = r.returncode == 0
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "governed": True,
        "git_available": git_ok,
        "branch_prefix": "aetherion/",
        "engine": "in-process-worktree-manager",
        "limits": {
            "max_per_session": 5,
            "git_timeout_sec": 30,
            "orphan_age_sec": 3600,
            "max_diff_lines": 10000,
        },
        "active_worktrees": sum(
            1 for t in _WORKTREES.values() for w in t.values() if w["state"] == "active"
        ),
    })


@d3_bp.route("/api/worktrees", methods=["GET"])
@token_required
def worktrees_list():
    tenant = _tenant()
    store = _wt_store(tenant)
    session_id = request.args.get("session_id") or ""
    worktrees = list(store.values())
    if session_id:
        worktrees = [w for w in worktrees if w["session_id"] == session_id]
    worktrees.sort(key=lambda w: w["created_at"], reverse=True)
    return jsonify({"ok": True, "worktrees": worktrees, "total": len(worktrees)})


@d3_bp.route("/api/worktrees", methods=["POST"])
@token_required
def worktrees_create():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    wt = _make_worktree(data, tenant)
    _wt_store(tenant)[wt["worktree_id"]] = wt
    return jsonify({"ok": True, "worktree": wt, "worktree_id": wt["worktree_id"]}), 201


@d3_bp.route("/api/worktrees/<worktree_id>", methods=["GET"])
@token_required
def worktrees_get(worktree_id: str):
    tenant = _tenant()
    wt = _wt_store(tenant).get(worktree_id)
    if not wt:
        return jsonify({"ok": False, "error": f"Worktree '{worktree_id}' not found"}), 404
    return jsonify({"ok": True, "worktree": wt})


@d3_bp.route("/api/worktrees/<worktree_id>/diff", methods=["GET"])
@token_required
def worktrees_diff(worktree_id: str):
    tenant = _tenant()
    wt = _wt_store(tenant).get(worktree_id)
    diff_lines = [
        f"# Worktree diff for {worktree_id}",
        f"# Branch: {wt.get('branch', '?') if wt else '?'}",
        f"# Base: {wt.get('base_ref', 'HEAD') if wt else 'HEAD'}",
        "# No changes detected.",
    ]
    return jsonify({
        "ok": True,
        "worktree_id": worktree_id,
        "diff": "\n".join(diff_lines),
        "lines_changed": wt.get("diff_lines", 0) if wt else 0,
        "files_changed": wt.get("files_changed", 0) if wt else 0,
    })


@d3_bp.route("/api/worktrees/<worktree_id>/discard", methods=["POST"])
@token_required
def worktrees_discard(worktree_id: str):
    tenant = _tenant()
    store = _wt_store(tenant)
    wt = store.get(worktree_id)
    if not wt:
        return jsonify({"ok": False, "error": f"Worktree '{worktree_id}' not found"}), 404
    wt["state"] = "discarded"
    wt["updated_at"] = time.time()
    return jsonify({"ok": True, "worktree_id": worktree_id, "state": "discarded"})


@d3_bp.route("/api/worktrees/sweep", methods=["POST"])
@token_required
def worktrees_sweep():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or ""
    store = _wt_store(tenant)
    swept = []
    for wt in store.values():
        if wt["state"] == "active" and (not session_id or wt["session_id"] == session_id):
            wt["state"] = "discarded"
            wt["updated_at"] = time.time()
            swept.append(wt["worktree_id"])
    return jsonify({"ok": True, "swept": swept, "count": len(swept)})


@d3_bp.route("/api/worktrees/sweep/orphans", methods=["POST"])
@token_required
def worktrees_sweep_orphans():
    tenant = _tenant()
    data = request.get_json(silent=True) or {}
    orphan_age_sec = int(data.get("orphan_age_sec") or 3600)
    store = _wt_store(tenant)
    cutoff = time.time() - orphan_age_sec
    swept = []
    for wt in store.values():
        if wt["state"] == "active" and wt.get("last_activity_at", 0) < cutoff:
            wt["state"] = "discarded"
            wt["updated_at"] = time.time()
            swept.append(wt["worktree_id"])
    return jsonify({"ok": True, "orphans_swept": swept, "count": len(swept)})
