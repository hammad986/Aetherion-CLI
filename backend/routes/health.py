"""
routes/health.py — Unified System Health Probes
Exposes:
  - GET /api/health (Standard CLI health check endpoint)
  - GET /api/v2/health (Canonical v2 response contract)
"""
from __future__ import annotations

import datetime
import os
import platform
from flask import Blueprint, jsonify

from core.environment import get_environment_mode

try:
    from backend.execution.control_plane_policy import get_control_plane_policy
except ImportError:
    from execution.control_plane_policy import get_control_plane_policy

health_bp = Blueprint("health", __name__)


def _execution_policy_fields(policy=None):
    """Expose execution policy without conflating it with application health."""
    effective_policy = policy or get_control_plane_policy()
    return {
        "execution_disabled": not effective_policy.execution_enabled,
        "execution": effective_policy.as_dict(),
    }


@health_bp.route("/api/health", methods=["GET"])
def api_health():
    """Real-time system health and readiness probe."""
    env_mode = get_environment_mode()
    policy = get_control_plane_policy()

    # Determine runner mode
    runner_isolation = "container" if os.getenv("RUNNER_CONTAINER") == "1" else "process"

    # Best effort resource metrics
    cpu_pct = None
    mem_used_pct = None
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=None)
        mem_used_pct = psutil.virtual_memory().percent
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "status": "ok",
        "version": "1.0.0",
        "service": "aetherion-backend",
        "environment": env_mode.value,
        "runner_isolation": runner_isolation,
        "metrics": {
            "cpu_pct": cpu_pct,
            "mem_pct": mem_used_pct,
        },
        **_execution_policy_fields(policy),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), 200


@health_bp.route("/api/v2/health", methods=["GET"])
def api_v2_health():
    """Canonical v2 health response contract."""
    from middleware.api_response import success_response
    policy = get_control_plane_policy()
    return success_response(data={
        "status": "ok",
        "version": "2.0.0",
        "environment": get_environment_mode().value,
        **_execution_policy_fields(policy),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


_SERVER_START_TIME = datetime.datetime.now(datetime.timezone.utc)


@health_bp.route("/livez", methods=["GET"])
def livez():
    """Kubernetes liveness probe — independent of external dependencies."""
    uptime = (datetime.datetime.now(datetime.timezone.utc) - _SERVER_START_TIME).total_seconds()
    policy = get_control_plane_policy()
    return jsonify({
        "status": "ok",
        "pid": os.getpid(),
        "uptime_seconds": uptime,
        **_execution_policy_fields(policy),
    }), 200


from core.preflight import run_preflight, CheckStatus


@health_bp.route("/readyz", methods=["GET"])
def readyz():
    """Kubernetes readiness probe — reports application dependencies and policy."""
    policy = get_control_plane_policy()
    report = run_preflight(probe_live=True)
    checks_dict = {}
    for c in report.checks:
        checks_dict[c.check_id] = {
            "ok": c.status == CheckStatus.PASS,
            "required": c.required,
            "detail": c.detail,
            "status": c.status.value,
            "severity": c.severity.value,
        }

    # Backward compatibility with existing preflight scripts
    db_check = next((c for c in report.checks if c.check_id == "database.readiness"), None)
    if db_check:
        checks_dict["database"] = {
            "ok": db_check.status == CheckStatus.PASS,
            "required": db_check.required,
            "detail": db_check.detail,
        }
    checks_dict["environment"] = {
        "ok": report.healthy,
        "required": True,
        "detail": report.environment,
    }

    payload = {
        "status": "ready" if report.ready else "not_ready",
        "ready": report.ready,
        "environment": report.environment,
        "checks": checks_dict,
        "timestamp": report.timestamp,
        "summary": report.summary,
        "application_ready": report.ready,
        "execution_ready": policy.execution_enabled and report.ready,
        **_execution_policy_fields(policy),
    }
    status_code = 200 if report.ready else 503
    return jsonify(payload), status_code



@health_bp.route("/startupz", methods=["GET"])
def startupz():
    """Kubernetes startup probe."""
    policy = get_control_plane_policy()
    return jsonify({
        "status": "started",
        **_execution_policy_fields(policy),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), 200


@health_bp.route("/api/health/live", methods=["GET"])
def api_health_live():
    """Level 1: LIVE — Process liveness verification."""
    uptime = (datetime.datetime.now(datetime.timezone.utc) - _SERVER_START_TIME).total_seconds()
    policy = get_control_plane_policy()
    return jsonify({
        "level": "LIVE",
        "ok": True,
        "status": "LIVE",
        "pid": os.getpid(),
        "uptime_seconds": uptime,
        **_execution_policy_fields(policy),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), 200


@health_bp.route("/api/health/ready", methods=["GET"])
def api_health_ready():
    """Level 2: READY — Core dependencies and platform readiness."""
    policy = get_control_plane_policy()
    report = run_preflight(probe_live=True)
    return jsonify({
        "level": "READY",
        "ok": report.ready,
        "status": "READY" if report.ready else "NOT-READY",
        "environment": report.environment,
        "summary": report.summary,
        "application_ready": report.ready,
        "execution_ready": policy.execution_enabled and report.ready,
        **_execution_policy_fields(policy),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), (200 if report.ready else 503)


@health_bp.route("/api/health/control-plane", methods=["GET"])
def api_health_control_plane():
    """Level 3: CONTROL-PLANE-READY — Execution Fabric Control Plane & Store probe."""
    policy = get_control_plane_policy()
    try:
        from backend.core.preflight import check_control_plane, CheckStatus
    except ImportError:
        from core.preflight import check_control_plane, CheckStatus

    try:
        from backend.execution.fabric.store import get_fabric_store
        from backend.execution.fabric.coordination import FabricCoordinator
    except ImportError:
        from execution.fabric.store import get_fabric_store
        from execution.fabric.coordination import FabricCoordinator

    chk = check_control_plane(probe_live=True)
    status_ok = chk.status == CheckStatus.PASS

    store_type = "unknown"
    worker_count = 0
    active_items = 0
    coordinator_distributed = False

    try:
        store = get_fabric_store()
        store_type = type(store).__name__
        worker_count = len(store.list_workers())
        active_items = store.count_global_active_items()
    except Exception:
        pass

    try:
        coord = FabricCoordinator()
        coordinator_distributed = coord.is_distributed
    except Exception:
        pass

    return jsonify({
        "level": "CONTROL-PLANE-READY",
        "ok": status_ok,
        "status": "healthy" if status_ok else ("degraded" if chk.status == CheckStatus.WARN else "unhealthy"),
        "check": chk.to_dict(),
        "store": {
            "type": store_type,
            "registered_workers": worker_count,
            "active_queue_items": active_items,
        },
        "coordinator": {
            "distributed": coordinator_distributed,
            "mode": "redis" if coordinator_distributed else "local_fallback",
        },
        **_execution_policy_fields(policy),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), (200 if status_ok or chk.status == CheckStatus.WARN else 503)


@health_bp.route("/api/health/workers", methods=["GET"])
def api_health_workers():
    """Level 4: WORKER-CAPABLE — Worker fleet capability and availability probe."""
    policy = get_control_plane_policy()
    try:
        from backend.execution.fabric.store import get_fabric_store
        from backend.execution.fabric.metrics import get_metrics_collector
    except ImportError:
        from execution.fabric.store import get_fabric_store
        from execution.fabric.metrics import get_metrics_collector

    try:
        store = get_fabric_store()
        collector = get_metrics_collector()
        workers = store.list_workers()
    except Exception:
        return jsonify({
            "level": "WORKER-CAPABLE",
            "ok": False,
            "status": "DEPENDENCY-UNAVAILABLE",
            "dependency_ok": False,
            "worker_fleet_available": False,
            "worker_capable": False,
            "execution_ready": False,
            **_execution_policy_fields(policy),
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }), 503
    collector.sync_worker_fleet(workers)

    ready_workers = [
        w for w in workers
        if getattr(w.status, "value", str(w.status)) in ("READY", "BUSY") and not getattr(w, "drain_state", False)
    ]
    total_capacity = sum(getattr(w, "max_concurrency", 0) for w in ready_workers)
    total_utilized = sum(getattr(w, "active_concurrency", 0) for w in ready_workers)

    is_capable = len(ready_workers) > 0
    execution_ready = policy.execution_enabled and is_capable
    worker_capable = execution_ready
    status = (
        "EXECUTION-DISABLED"
        if not policy.execution_enabled
        else ("WORKER-CAPABLE" if is_capable else "NO-WORKERS-AVAILABLE")
    )

    return jsonify({
        "level": "WORKER-CAPABLE",
        "ok": is_capable,
        "status": status,
        "dependency_ok": True,
        "registered_workers": len(workers),
        "ready_workers_count": len(ready_workers),
        "total_concurrency_capacity": total_capacity,
        "total_concurrency_utilized": total_utilized,
        "worker_fleet_available": is_capable,
        "worker_capable": worker_capable,
        "execution_ready": execution_ready,
        "workers": [
            {
                "worker_id": w.worker_id,
                "status": getattr(w.status, "value", str(w.status)),
                "concurrency": f"{w.active_concurrency}/{w.max_concurrency}",
                "drain_state": w.drain_state,
                "capabilities": list(w.supported_capabilities),
            }
            for w in workers
        ],
        **_execution_policy_fields(policy),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), (200 if is_capable else 503)


@health_bp.route("/api/health/execution-ready", methods=["GET"])
def api_health_execution_ready():
    """Level 5: EXECUTION-READY — End-to-end task execution fabric operational readiness."""
    policy = get_control_plane_policy()
    runner_status = {}
    try:
        from backend.core.preflight import check_control_plane, CheckStatus
        from backend.execution.fabric.store import get_fabric_store
        from backend.execution.runner_contract import get_runner_status
    except ImportError:
        from core.preflight import check_control_plane, CheckStatus
        from execution.fabric.store import get_fabric_store
        from execution.runner_contract import get_runner_status

    try:
        runner_status = get_runner_status()
        chk = check_control_plane(probe_live=True)
        control_plane_ok = chk.status in (CheckStatus.PASS, CheckStatus.WARN)
        store = get_fabric_store()
        workers = store.list_workers()
    except Exception:
        return jsonify({
            "level": "EXECUTION-READY",
            "ok": False,
            "status": "DEPENDENCY-UNAVAILABLE" if policy.execution_enabled else "EXECUTION-DISABLED",
            "control_plane_ready": False,
            "workers_ready": False,
            "ready_worker_count": 0,
            "active_queue_items": 0,
            "dependency_ok": False,
            "execution_ready": False,
            "runner": runner_status,
            **_execution_policy_fields(policy),
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }), 503

    ready_workers = [
        w for w in workers
        if getattr(w.status, "value", str(w.status)) in ("READY", "BUSY") and not getattr(w, "drain_state", False)
    ]
    worker_ok = len(ready_workers) > 0

    is_exec_ready = policy.execution_enabled and control_plane_ok and worker_ok
    status_str = (
        "EXECUTION-DISABLED"
        if not policy.execution_enabled
        else ("EXECUTION-READY" if is_exec_ready else ("CONTROL-PLANE-DEGRADED" if not control_plane_ok else "NO-ACTIVE-WORKERS"))
    )

    return jsonify({
        "level": "EXECUTION-READY",
        "ok": is_exec_ready,
        "status": status_str,
        "control_plane_ready": control_plane_ok,
        "workers_ready": worker_ok,
        "ready_worker_count": len(ready_workers),
        "active_queue_items": store.count_global_active_items(),
        "dependency_ok": True,
        "execution_ready": is_exec_ready,
        "runner": runner_status,
        **_execution_policy_fields(policy),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), (200 if is_exec_ready else 503)


@health_bp.route("/api/health/fabric-readiness", methods=["GET"])
def api_health_fabric_readiness():
    """Composite 5-level system operational readiness probe."""
    policy = get_control_plane_policy()
    try:
        from backend.core.preflight import run_preflight, check_control_plane, CheckStatus
        from backend.execution.fabric.store import get_fabric_store
    except ImportError:
        from core.preflight import run_preflight, check_control_plane, CheckStatus
        from execution.fabric.store import get_fabric_store

    # Level 1: LIVE
    live_ok = True
    # Level 2: READY
    preflight_rep = run_preflight(probe_live=False)
    ready_ok = preflight_rep.ready
    # Level 3: CONTROL-PLANE-READY
    cp_chk = check_control_plane(probe_live=True)
    cp_ok = cp_chk.status in (CheckStatus.PASS, CheckStatus.WARN)
    # Level 4: WORKER-CAPABLE
    dependency_ok = True
    try:
        store = get_fabric_store()
        workers = store.list_workers()
    except Exception:
        dependency_ok = False
        store = None
        workers = []
    ready_workers = [
        w for w in workers
        if getattr(w.status, "value", str(w.status)) in ("READY", "BUSY") and not getattr(w, "drain_state", False)
    ]
    worker_ok = len(ready_workers) > 0
    # Level 5: EXECUTION-READY
    exec_ready = policy.execution_enabled and cp_ok and worker_ok

    application_ok = live_ok and ready_ok and cp_ok and dependency_ok
    # This endpoint describes control-plane availability. Autonomous execution
    # remains a separate capability and is intentionally false in Option B.
    overall_ok = application_ok
    worker_status = "EXECUTION-DISABLED" if not policy.execution_enabled else ("WORKER-CAPABLE" if worker_ok else "NO-WORKERS-AVAILABLE")
    execution_status = "EXECUTION-DISABLED" if not policy.execution_enabled else ("EXECUTION-READY" if exec_ready else "DEGRADED")

    return jsonify({
        "application_ok": application_ok,
        "overall_ok": overall_ok,
        "dependency_ok": dependency_ok,
        "execution_ready": exec_ready,
        **_execution_policy_fields(policy),
        "levels": {
            "LIVE": {"ok": live_ok, "status": "LIVE"},
            "READY": {"ok": ready_ok, "status": "READY" if ready_ok else "NOT-READY"},
            "CONTROL-PLANE-READY": {"ok": cp_ok, "status": cp_chk.status.value},
            "WORKER-CAPABLE": {"ok": worker_ok, "status": worker_status, "active_workers": len(ready_workers), "execution_capable": policy.execution_enabled and worker_ok, "execution_disabled": not policy.execution_enabled},
            "EXECUTION-READY": {"ok": exec_ready, "status": execution_status, "execution_disabled": not policy.execution_enabled},
        },
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), (200 if overall_ok else 503)


@health_bp.route("/api/health/metrics", methods=["GET"])
def api_health_metrics():
    """Execution Fabric metrics in structured JSON format."""
    policy = get_control_plane_policy()
    try:
        from backend.execution.fabric.metrics import get_metrics_collector
        from backend.execution.fabric.store import get_fabric_store
    except ImportError:
        from execution.fabric.metrics import get_metrics_collector
        from execution.fabric.store import get_fabric_store

    collector = get_metrics_collector()
    try:
        store = get_fabric_store()
        collector.sync_worker_fleet(store.list_workers())
    except Exception:
        pass

    metrics_payload = collector.get_metrics()
    metrics_payload.update(_execution_policy_fields(policy))
    return jsonify(metrics_payload), 200


@health_bp.route("/metrics", methods=["GET"])
def metrics():
    """Prometheus exposition format."""
    policy = get_control_plane_policy()
    try:
        from backend.execution.fabric.metrics import get_metrics_collector
        from backend.execution.fabric.store import get_fabric_store
    except ImportError:
        from execution.fabric.metrics import get_metrics_collector
        from execution.fabric.store import get_fabric_store

    collector = get_metrics_collector()
    try:
        store = get_fabric_store()
        collector.sync_worker_fleet(store.list_workers())
    except Exception:
        pass

    body = collector.to_prometheus_format().rstrip() + "\n"
    body += "# HELP aetherion_execution_disabled Whether agent execution is disabled by the effective control-plane policy.\n"
    body += "# TYPE aetherion_execution_disabled gauge\n"
    body += f"aetherion_execution_disabled {1 if not policy.execution_enabled else 0}\n"
    return body, 200, {"Content-Type": "text/plain; version=0.0.4"}

