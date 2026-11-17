"""
routes/tasks.py — Durable Task Execution, Status, HITL & Lifecycle API
Supports:
  - POST /api/tasks (Create task)
  - GET /api/tasks (List tasks)
  - GET /api/task/<tid> (Task status)
  - POST /api/task/<tid>/stop (Cancel/stop task)
  - POST /api/task/<tid>/retry (Retry task)
  - POST /api/task/<tid>/approve (Approve/reject HITL gate)
  - GET /api/task/<tid>/logs and GET /api/logs/<tid> (Task logs)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict
import requests
from flask import Blueprint, jsonify, request, g, Response, stream_with_context
from auth_system import token_required
from execution import durable_state
from execution.control_plane_policy import (
    ExecutionDisabledError,
    block_durable_execution,
    execution_disabled_payload,
)
from execution.diff_policy import DiffExtractionError, diff_payload, error_payload
from execution.worktree import WorktreeError
from workspace_auth import WorkspaceAuthorizationError, require_workspace

logger = logging.getLogger("aetherion.tasks")
tasks_bp = Blueprint("tasks", __name__)


def _get_tenant() -> str:
    """Derive tenant or user identifier from current auth context."""
    user_id = getattr(g, "user_id", None)
    if user_id:
        return str(user_id)
    return "default_tenant"


def _authorized_worktree_diff(task_id: str, tenant_id: str, payload: Dict[str, Any]) -> str:
    """Resolve only a registry worktree owned by this tenant and task."""
    worktree_id = payload.get("worktree_id")
    if not isinstance(worktree_id, str) or not worktree_id:
        raise LookupError("complete diff is unavailable")
    try:
        from execution.worktree import get_authorized_worktree, worktree_diff
        wt = get_authorized_worktree(worktree_id, tenant_id, execution_id=task_id)
        return worktree_diff(
            worktree_id,
            tenant_id=tenant_id,
            session_id=wt.session_id,
            execution_id=task_id,
        )
    except WorktreeError as exc:
        raise LookupError("authorized diff not found") from exc


def _diff_failure(task_id: str, status: int, code: str, message: str):
    return jsonify(error_payload(code, message, task_id=task_id)), status


def _authorize_task_workspace(record: Dict[str, Any]) -> Dict[str, Any]:
    """Re-check the persisted canonical workspace before returning a task."""
    try:
        payload = json.loads(record.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkspaceAuthorizationError("Task workspace context is invalid", code="WORKSPACE_FORBIDDEN") from exc
    requested = request.headers.get("X-Workspace-ID") or payload.get("workspace_id")
    require_workspace(getattr(g, "user_id", None), requested)
    return payload


def _parse_diff_record(record: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate durable diff JSON before any result or workspace disclosure."""
    if not isinstance(record, dict) or "payload_json" not in record:
        raise ValueError("durable diff record is missing required fields")
    try:
        result = json.loads(record.get("result_json") or "{}")
        payload = json.loads(record.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("durable diff record contains invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(payload, dict):
        raise ValueError("durable diff record contains invalid JSON types")
    if "diff" in result and not isinstance(result["diff"], str):
        raise ValueError("durable diff record contains an invalid diff")
    if not isinstance(record.get("status"), str) or not record.get("status"):
        raise ValueError("durable diff record is missing status")
    return result, payload


@tasks_bp.route("/api/tasks", methods=["POST"])
@token_required
def create_task():
    """Create an authoritative durable coding task."""
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt") or data.get("task") or data.get("objective")
    if not prompt:
        return jsonify({"ok": False, "error": "Task prompt is required"}), 400

    requested_workspace_id = data.get("workspace_id")
    session_id = data.get("session_id")
    # A canonical workspace is the authorization scope.  Legacy session IDs
    # remain payload metadata; they are never treated as tenant IDs.
    if requested_workspace_id is None and isinstance(session_id, str) and session_id.startswith("ws_"):
        requested_workspace_id = session_id
    try:
        workspace_context = require_workspace(getattr(g, "user_id", None), requested_workspace_id)
    except WorkspaceAuthorizationError as exc:
        return jsonify({"ok": False, "error": {"code": exc.code, "message": str(exc)}}), exc.status_code
    workspace_id = workspace_context.workspace_id
    model = data.get("model") or "default"
    idempotency_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")
    tenant_id = _get_tenant()

    options = dict(data.get("options") or {})
    repo_id = data.get("repo_id") or options.get("repo_id")
    repo_root = data.get("repo_root") or options.get("repo_root")
    base_ref = data.get("base_ref") or options.get("base_ref") or "HEAD"
    execution_constraints = dict(data.get("execution_constraints") or options.get("execution_constraints") or {})
    verification_criteria = dict(data.get("verification_criteria") or options.get("verification_criteria") or {})
    execution_budget = dict(data.get("execution_budget") or options.get("execution_budget") or {})
    security_context = dict(data.get("security_context") or options.get("security_context") or {})

    selected_skill_ids = security_context.get("selected_skill_ids")
    if selected_skill_ids is not None:
        if not isinstance(selected_skill_ids, list) or any(not isinstance(item, str) for item in selected_skill_ids):
            return jsonify({"ok": False, "error": {"code": "INVALID_SKILL_SELECTION", "message": "selected_skill_ids must be a list of strings"}}), 400
        try:
            from execution.extensions import ExtensionManager
            selected_records = ExtensionManager().resolve_runtime_skill_selection_in_workspace(workspace_id, selected_skill_ids)
            security_context["selected_skill_ids"] = [record["id"] for record in selected_records]
            security_context["selected_skill_digests"] = [record["content_hash"] for record in selected_records]
        except Exception as exc:
            return jsonify({"ok": False, "error": {"code": "INVALID_SKILL_SELECTION", "message": str(exc)}}), 403

    # Check for allow_plain_workspace policy
    if "allow_plain_workspace" in data:
        execution_constraints["allow_plain_workspace"] = bool(data["allow_plain_workspace"])
    elif "allow_plain_workspace" in options:
        execution_constraints["allow_plain_workspace"] = bool(options["allow_plain_workspace"])

    payload = {
        "prompt": prompt,
        "objective": prompt,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "model": model,
        "repo_id": repo_id,
        "repo_root": repo_root,
        "base_ref": base_ref,
        "execution_constraints": execution_constraints,
        "verification_criteria": verification_criteria,
        "execution_budget": execution_budget,
        "security_context": security_context,
        "options": options,
    }

    try:
        record = durable_state.create(
            kind="agent_task",
            tenant_id=tenant_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        task_id = record["execution_id"]

        # Record creation remains available to the control plane, but an
        # execution-bearing create request is terminally blocked before any
        # thread, provider, workspace, or worker can be started.
        if data.get("auto_run") or data.get("run"):
            blocked = block_durable_execution(task_id, tenant_id) or record
            response = execution_disabled_payload()
            response.update({
                "task_id": task_id,
                "status": blocked.get("status", durable_state.STATUS_CANCELLED).lower(),
            })
            return jsonify(response), 403

        return jsonify({
            "ok": True,
            "task_id": task_id,
            "id": task_id,
            "session_id": task_id,
            "status": record["status"].lower(),
            "workspace_id": workspace_id,
            "created_at": record["created_at"],
            "data": record,
            "execution_context": {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "objective": prompt,
                "repo_root": repo_root,
                "base_ref": base_ref,
                "execution_constraints": execution_constraints,
                "verification_criteria": verification_criteria,
                "execution_budget": execution_budget,
            },
        }), 201
    except Exception as exc:
        logger.exception("Failed to create task: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@tasks_bp.route("/api/tasks", methods=["GET"])
@token_required
def list_tasks():
    """List tasks for the authenticated user/tenant."""
    tenant_id = _get_tenant()
    limit = min(int(request.args.get("limit", 50)), 100)
    records = durable_state.list_for_tenant(tenant_id, limit=limit)
    authorized = []
    for record in records:
        try:
            _authorize_task_workspace(record)
        except WorkspaceAuthorizationError:
            continue
        authorized.append(record)
    records = authorized
    return jsonify({"ok": True, "tasks": records, "data": records})


@tasks_bp.route("/api/task/<tid>", methods=["GET"])
@tasks_bp.route("/api/tasks/<tid>", methods=["GET"])
@token_required
def get_task(tid: str):
    """Get status and result of a specific task."""
    tenant_id = _get_tenant()
    record = durable_state.get(tid, tenant_id)
    if not record:
        return jsonify({"ok": False, "error": "Task not found"}), 404

    try:
        payload = _authorize_task_workspace(record)
    except WorkspaceAuthorizationError as exc:
        return jsonify({"ok": False, "error": {"code": exc.code, "message": str(exc)}}), exc.status_code
    result = json.loads(record.get("result_json") or "{}") if record.get("result_json") else None

    return jsonify({
        "ok": True,
        "task_id": record["execution_id"],
        "id": record["execution_id"],
        "session_id": record["execution_id"],
        "status": record["status"].lower(),
        "execution_state": record["status"],
        "status_version": record["status_version"],
        "failure_class": record.get("failure_class"),
        "workspace_id": payload.get("workspace_id", "default"),
        "result": result,
        "data": record,
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    })


ACTIVE_TASK_CANCELLATIONS: Dict[str, threading.Event] = {}
_CANCELLATION_LOCK = threading.Lock()


def register_cancellation(task_id: str) -> threading.Event:
    ev = threading.Event()
    with _CANCELLATION_LOCK:
        ACTIVE_TASK_CANCELLATIONS[task_id] = ev
    return ev


def trigger_cancellation(task_id: str) -> bool:
    with _CANCELLATION_LOCK:
        ev = ACTIVE_TASK_CANCELLATIONS.get(task_id)
        if ev:
            ev.set()
            return True
    return False


def unregister_cancellation(task_id: str) -> None:
    with _CANCELLATION_LOCK:
        ACTIVE_TASK_CANCELLATIONS.pop(task_id, None)


@tasks_bp.route("/api/task/<tid>/stop", methods=["POST"])
@tasks_bp.route("/api/tasks/<tid>/stop", methods=["POST"])
@token_required
def stop_task(tid: str):
    """Cancel / stop an active task with cancel-wins semantics."""
    tenant_id = _get_tenant()
    try:
        updated = durable_state.stop(tid, tenant_id)
        trigger_cancellation(tid)
        try:
            from execution.hitl import global_hitl_tracker
            global_hitl_tracker.cancel_approval(tid, feedback="Task cancelled via stop API")
        except Exception as hitl_exc:
            logger.debug("HITL tracker cancel error for %s: %s", tid, hitl_exc)
        return jsonify({"ok": True, "task_id": tid, "status": "stopped", "data": updated})
    except (PermissionError, durable_state.AuthorizationError):
        return jsonify({"ok": False, "error": "Forbidden: Task does not belong to your tenant"}), 403
    except durable_state.TransitionError as te:
        return jsonify({"ok": False, "error": str(te)}), 409
    except durable_state.ExecutionStateError as ee:
        if "unknown execution" in str(ee).lower():
            return jsonify({"ok": False, "error": "Task not found"}), 404
        return jsonify({"ok": False, "error": str(ee)}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": "Internal server error"}), 500


@tasks_bp.route("/api/task/<tid>/retry", methods=["POST"])
@tasks_bp.route("/api/tasks/<tid>/retry", methods=["POST"])
@token_required
def retry_task(tid: str):
    """Retry a failed or stopped task."""
    tenant_id = _get_tenant()
    record = durable_state.get(tid, tenant_id)
    if not record:
        return jsonify({"ok": False, "error": "Task not found"}), 404

    # A retry is an execution request, even though the old implementation only
    # created a new pending record. Reject it explicitly so no later worker can
    # interpret the new record as an authorized execution.
    response = execution_disabled_payload()
    response["task_id"] = tid
    return jsonify(response), 403


@tasks_bp.route("/api/task/<tid>/approve", methods=["POST"])
@tasks_bp.route("/api/tasks/<tid>/approve", methods=["POST"])
@tasks_bp.route("/api/z29/governance/<decision>/<tid>", methods=["POST"])
@token_required
def approve_task(tid: str, decision: Optional[str] = None):
    """Approve or reject a Human-in-the-Loop (HITL) gate for a task."""
    tenant_id = _get_tenant()
    user_id = getattr(g, "user_id", None) or "human_operator"
    body = request.get_json(silent=True) or {}
    if not decision:
        decision = (body.get("decision") or "approve").lower()  # approve | reject

    record = durable_state.get(tid, tenant_id)
    if not record:
        return jsonify({"ok": False, "error": "Task not found"}), 404

    status = (record.get("status") or "").upper()
    if status in durable_state.TERMINAL_STATUSES:
        return jsonify({"ok": False, "error": f"Cannot approve task in terminal state: '{status}'"}), 409

    comment = body.get("comment") or body.get("comments") or ""
    is_approved = decision in ("approve", "accept")
    gate_resolved = False
    target_status = None

    # HITL approval must not become an execution switch in the control-plane
    # release. Review decisions for already-produced REVIEW_PENDING records
    # remain administrative; approval of a pending/running execution is
    # terminally blocked instead.
    if is_approved and status in {
        durable_state.STATUS_PENDING,
        durable_state.STATUS_QUEUED,
        durable_state.STATUS_CLAIMED,
        durable_state.STATUS_RUNNING,
        durable_state.STATUS_EXECUTING,
        durable_state.STATUS_AWAITING_APPROVAL,
        durable_state.STATUS_WAITING_FOR_APPROVAL,
        durable_state.STATUS_PLANNING,
        durable_state.STATUS_INSPECTING,
        durable_state.STATUS_VERIFYING,
        durable_state.STATUS_REPAIRING,
    }:
        blocked = block_durable_execution(tid, tenant_id) or record
        response = execution_disabled_payload()
        response.update({
            "task_id": tid,
            "status": blocked.get("status", durable_state.STATUS_CANCELLED).lower(),
        })
        return jsonify(response), 403

    try:
        if is_approved:
            if status == durable_state.STATUS_REVIEW_PENDING:
                target_status = durable_state.STATUS_ACCEPTED
                durable_state.accept(
                    tid,
                    tenant_id,
                    comment=comment,
                    approver=str(user_id),
                )
                gate_resolved = True
            elif status in (durable_state.STATUS_WAITING_FOR_APPROVAL, durable_state.STATUS_AWAITING_APPROVAL):
                target_status = durable_state.STATUS_EXECUTING
                durable_state.transition(tid, tenant_id, target_status)
                gate_resolved = True
            elif status in (durable_state.STATUS_PENDING, durable_state.STATUS_QUEUED):
                target_status = durable_state.STATUS_RUNNING
                durable_state.transition(tid, tenant_id, target_status)
                gate_resolved = True
            else:
                target_status = status
                gate_resolved = True
        else:
            # Rejection
            if status in (durable_state.STATUS_REVIEW_PENDING, durable_state.STATUS_SUCCEEDED):
                target_status = durable_state.STATUS_REJECTED
                durable_state.reject(
                    tid,
                    tenant_id,
                    comment=comment,
                    reviewer=str(user_id),
                )
                gate_resolved = True
            elif status in (durable_state.STATUS_WAITING_FOR_APPROVAL, durable_state.STATUS_AWAITING_APPROVAL):
                target_status = durable_state.STATUS_CANCELLED
                durable_state.transition(tid, tenant_id, target_status)
                gate_resolved = True
            else:
                target_status = durable_state.STATUS_CANCELLED
                durable_state.transition(tid, tenant_id, target_status)
                gate_resolved = True
    except (PermissionError, durable_state.AuthorizationError):
        return jsonify({"ok": False, "error": "Forbidden: Task does not belong to your tenant"}), 403
    except durable_state.TransitionError as te:
        logger.warning("Durable transition error on task %s decision: %s", tid, te)
        return jsonify({"ok": False, "error": str(te)}), 409
    except Exception as exc:
        logger.exception("Failed to transition durable state for task %s on decision: %s", tid, exc)
        return jsonify({"ok": False, "error": "Internal server error"}), 500

    # Unblock any thread waiting on the in-memory or SQLite gate tracker
    try:
        from execution.hitl import resolve_gate
        resolve_gate(tid, is_approved, comments=comment)
    except Exception as e:
        logger.debug("Failed to resolve in-process HITL gate for task %s: %s", tid, e)

    # Return authoritative durable response
    return jsonify({
        "ok": True,
        "task_id": tid,
        "decision": decision,
        "status": target_status,
        "gate_resolved": gate_resolved,
    })



@tasks_bp.route("/api/task/<tid>/logs", methods=["GET"])
@tasks_bp.route("/api/tasks/<tid>/logs", methods=["GET"])
@tasks_bp.route("/api/logs/<tid>", methods=["GET"])
@token_required
def get_task_logs(tid: str):
    """Retrieve execution logs for a task."""
    tenant_id = _get_tenant()
    record = durable_state.get(tid, tenant_id)
    if not record:
        return jsonify({"ok": False, "error": "Task not found"}), 404

    log_lines = [
        f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(record['created_at']))}] Task {tid} initialized.",
        f"Status: {record['status']}",
    ]
    if record.get("failure_class"):
        log_lines.append(f"Failure class: {record['failure_class']}")

    return jsonify({
        "ok": True,
        "task_id": tid,
        "logs": "\n".join(log_lines),
        "lines": log_lines,
    })


def run_task_pipeline(
    task_id: str,
    tenant_id: str,
    cancel_event: Optional[threading.Event] = None,
    provider: Optional[Any] = None,
) -> Dict[str, Any]:
    """Authoritative task execution pipeline connecting TaskExecutionContext and RealAgentRuntime."""
    record = durable_state.get(task_id, tenant_id)
    if not record:
        raise ValueError(f"Task '{task_id}' not found for tenant '{tenant_id}'")

    # Defense-in-depth for direct/internal callers. This check occurs before
    # workspace creation, provider resolution, planning, queueing, or runtime
    # construction.
    block_durable_execution(task_id, tenant_id)
    raise ExecutionDisabledError()

    payload = json.loads(record.get("payload_json") or "{}")
    prompt = payload.get("prompt") or payload.get("objective") or "autonomous_task"
    workspace_id = payload.get("workspace_id") or task_id
    model = payload.get("model") or "default"

    ws_base = os.path.join(os.getcwd(), "workspace")
    os.makedirs(ws_base, exist_ok=True)
    ws_dir = os.path.join(ws_base, workspace_id)
    os.makedirs(ws_dir, exist_ok=True)

    from execution.agent_runtime import (
        RealAgentRuntime,
        TaskExecutionContext,
        WorktreeRequiredError,
        attach_execution_worktree,
        get_provider,
    )

    # ------------------------------------------------------------------ #
    # Phase 15: Task Classification → Execution Policy → RunBudget        #
    # ------------------------------------------------------------------ #
    orch_evidence: Dict[str, Any] = {}
    try:
        from execution.orchestration import (
            TaskClassifier,
            derive_execution_policy,
            ContextCompiler,
            ModelRole,
            UsageAccounting,
            RepositoryIntelligence,
        )

        # 1. Classify the task (deterministic, no model call)
        classifier = TaskClassifier()
        classification = classifier.classify(
            prompt,
            hints=dict(payload.get("classification_hints") or {}),
        )
        orch_evidence["classification"] = classification.as_dict()
        logger.info(
            "Task %s classified: complexity=%s, risk=%s, severity=%d",
            task_id,
            classification.complexity.value,
            classification.risk.value,
            classification.overall_severity,
        )

        # 2. Derive execution policy (profile selection + RunBudget)
        execution_budget_payload = dict(payload.get("execution_budget") or {})
        policy = derive_execution_policy(
            classification,
            execution_budget_override=execution_budget_payload or None,
        )
        orch_evidence["policy"] = {
            "planner_profile": policy.planner_profile.name,
            "executor_profile": policy.executor_profile.name,
            "verifier_profile": policy.verifier_profile.name,
            "repair_profile": policy.repair_profile.name,
            "require_hitl": policy.require_hitl,
            "max_repair_attempts": policy.max_repair_attempts,
            "rationale": policy.rationale,
        }
        orch_evidence["run_budget_limits"] = policy.run_budget.as_dict()["limits"]

        # 3. Merge derived budget back into execution_budget so RealAgentRuntime
        #    reads it from the single authoritative source: context.execution_budget
        derived_budget = {
            "max_repair_attempts": policy.max_repair_attempts,
            "max_model_calls": policy.run_budget.max_model_calls,
            "max_tool_calls": policy.run_budget.max_tool_calls,
            "max_execution_time_sec": policy.run_budget.max_execution_time_sec,
            "max_tokens": policy.run_budget.max_tokens,
            "max_escalation_attempts": policy.run_budget.max_escalation_attempts,
        }
        # Caller-supplied budget keys take precedence over derived
        merged_budget = {**derived_budget, **execution_budget_payload}
        payload["execution_budget"] = merged_budget

        # Phase 17: Authoritative Repository Intelligence & Adaptive Context Compilation
        repo_intel = RepositoryIntelligence(repo_root=payload.get("repo_root") or ws_dir)
        relevance_items = repo_intel.get_task_context(
            prompt,
            user_files=payload.get("files") or payload.get("user_files"),
            recent_evidence=dict(payload.get("recent_evidence") or {}),
        )
        orch_evidence["repo_intelligence"] = repo_intel.as_dict()
        orch_evidence["task_relevance"] = [item.as_dict() for item in relevance_items]

        # Phase 18: Governed Skill Discovery & Verified Engineering Memory Retrieval
        from execution.orchestration import (
            SkillRegistry,
            SkillDiscoveryEngine,
            EngineeringMemoryKernel,
        )
        skill_registry = SkillRegistry()
        skill_engine = SkillDiscoveryEngine(skill_registry)
        skill_candidates = skill_engine.discover(
            task_objective=prompt,
            repo_intel=repo_intel,
            classification=classification,
        )
        orch_evidence["skills_discovered"] = [c.as_dict() for c in skill_candidates]

        memory_kernel = EngineeringMemoryKernel()
        eng_memories, memory_conflicts = memory_kernel.retrieve_relevant(
            tenant_id=tenant_id,
            query=prompt,
            repo_id=payload.get("repo_id") or payload.get("repo_root") or ws_dir,
            task_type=classification.task_type.value if hasattr(classification, "task_type") else None,
        )
        orch_evidence["engineering_memories"] = [m.as_dict() for m in eng_memories]
        orch_evidence["memory_conflicts"] = [c.as_dict() for c in memory_conflicts]

        # 4. Compile role-specific context for the EXECUTOR role
        compiler = ContextCompiler()
        compiled_ctx = compiler.compile(
            role=ModelRole.EXECUTOR,
            objective=prompt,
            context_budget_tokens=policy.executor_profile.context_budget,
            worktree_metadata={
                "repo_root": payload.get("repo_root"),
                "base_ref": payload.get("base_ref", "HEAD"),
                "workspace_id": workspace_id,
            },
            rationale=policy.rationale,
            repo_intelligence=repo_intel,
            relevance_items=relevance_items,
            verification_criteria=dict(payload.get("verification_criteria") or {}),
            selected_skills=skill_candidates,
            engineering_memories=eng_memories,
            memory_conflicts=memory_conflicts,
        )
        orch_evidence["compiled_context_summary"] = compiled_ctx.as_dict()

        # 5. Initialise usage accounting (stored in evidence; expanded post-run)
        usage = UsageAccounting(
            task_id=task_id,
            tenant_id=tenant_id,
            initial_profile_name=policy.executor_profile.name,
            initial_profile_selection_rationale=policy.rationale,
        )
        orch_evidence["usage_accounting_started"] = True

        # Phase 19: Adaptive Planning & Execution Graph Generation
        try:
            from execution.orchestration import AdaptivePlanner
            planner = AdaptivePlanner()
            exec_graph = planner.plan_task(
                task_id=task_id,
                tenant_id=tenant_id,
                objective=prompt,
                classification=classification,
                repo_intelligence=repo_intel,
                skills=skill_candidates,
                engineering_memories=eng_memories,
                memory_conflicts=memory_conflicts,
                execution_budget=payload.get("execution_budget"),
                verification_criteria=payload.get("verification_criteria"),
            )
            orch_evidence["execution_plan"] = exec_graph.plan.as_dict()
            orch_evidence["execution_graph"] = exec_graph.as_dict()

            # Phase 20: Durable Execution Supervisor & Crash-Resilient Workflow Kernel
            try:
                from execution.orchestration import DurableExecutionSupervisor
                supervisor = DurableExecutionSupervisor()
                supervisor.register_task(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    graph=exec_graph,
                    status="RUNNING",
                )
                orch_evidence["durable_supervisor_registered"] = True

                # Phase 21: Execution Fabric & Worker Control Plane
                try:
                    from execution.fabric import ExecutionFabricService
                    fabric_service = ExecutionFabricService(supervisor=supervisor)
                    local_worker_id = f"worker_{os.getpid()}_{uuid.uuid4().hex[:6]}"
                    fabric_service.register_worker(
                        worker_id=local_worker_id,
                        max_concurrency=4,
                        supported_capabilities=["tool_execution", "sandbox", "model_access", "read_file", "edit_file", "bash"],
                    )
                    enqueued_items = fabric_service.enqueue_graph_steps(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        graph=exec_graph,
                    )
                    orch_evidence["fabric_service_registered"] = True
                    orch_evidence["fabric_enqueued_steps"] = len(enqueued_items)
                except Exception as _fab_reg_exc:
                    logger.debug("Fabric service registration note: %s", _fab_reg_exc)
            except Exception as _sup_reg_exc:
                logger.debug("Supervisor registration note: %s", _sup_reg_exc)
        except Exception as _plan_exc:
            logger.debug("Plan generation note: %s", _plan_exc)


    except Exception as orch_exc:
        # Orchestration is additive — if it fails, pipeline continues unimpeded
        logger.warning(
            "Phase 15 orchestration layer error for task %s (continuing): %s",
            task_id, orch_exc,
        )
        orch_evidence["orchestration_error"] = str(orch_exc)

    exec_constraints = dict(payload.get("execution_constraints") or {})
    context = TaskExecutionContext(
        task_id=task_id,
        objective=prompt,
        workspace_dir=ws_dir,
        tenant_id=tenant_id,
        session_id=workspace_id,
        repo_id=payload.get("repo_id"),
        repo_root=payload.get("repo_root"),
        base_ref=payload.get("base_ref", "HEAD"),
        execution_constraints=exec_constraints,
        verification_criteria=dict(payload.get("verification_criteria") or {}),
        execution_budget=dict(payload.get("execution_budget") or {}),
        security_context=dict(payload.get("security_context") or {}),
    )
    if 'supervisor' in locals() and supervisor is not None:
        context.supervisor = supervisor
    if 'fabric_service' in locals() and fabric_service is not None:
        context.fabric = fabric_service

    # Attach worktree according to safety policy (isolated worktree required by default)
    try:
        context = attach_execution_worktree(context, repo_root=context.repo_root)
        durable_state.update_payload(
            task_id,
            tenant_id,
            lambda p: {
                **p,
                "worktree_id": context.worktree_id,
                "branch": context.branch,
                "base_ref": context.base_ref,
                "repo_root": context.repo_root,
                "workspace_dir": str(context.workspace_dir),
            },
        )
    except WorktreeRequiredError as wte:
        logger.error("Worktree required policy failed for task %s: %s", task_id, wte)
        fail_res = {
            "error": str(wte),
            "failure_class": durable_state.FAILURE_CLASS_QUEUE,
            "status": durable_state.STATUS_FAILED,
            "orchestration": orch_evidence,
        }
        durable_state.fail(
            task_id,
            tenant_id,
            failure_class=durable_state.FAILURE_CLASS_QUEUE,
            result=fail_res,
        )
        return fail_res

    # Transition to RUNNING
    try:
        durable_state.transition(task_id, tenant_id, durable_state.STATUS_RUNNING)
    except Exception as trans_exc:
        logger.debug("Transition to RUNNING note for task %s: %s", task_id, trans_exc)

    prov = provider or get_provider(model)
    runtime = RealAgentRuntime(context=context, provider=prov)

    # Record orchestration evidence as a replay event before execution begins
    if orch_evidence:
        if "compiled_context_summary" in orch_evidence:
            runtime._record_replay_event("context.compiled", orch_evidence["compiled_context_summary"])
        if "skills_discovered" in orch_evidence:
            runtime._record_replay_event("skill.discovered", {"skills": orch_evidence["skills_discovered"]})
        if "engineering_memories" in orch_evidence:
            runtime._record_replay_event("memory.retrieved", {
                "memories": orch_evidence["engineering_memories"],
                "conflicts": orch_evidence.get("memory_conflicts", []),
            })
        if "execution_plan" in orch_evidence:
            p_dict = orch_evidence["execution_plan"]
            runtime._record_replay_event("plan.created", {
                "plan_id": p_dict.get("plan_id"),
                "step_count": p_dict.get("step_count", 0),
                "steps": list(p_dict.get("steps", {}).keys()),
            })
            runtime._record_replay_event("plan.validated", {
                "plan_id": p_dict.get("plan_id"),
                "topological_order": orch_evidence.get("execution_graph", {}).get("ready_steps", []),
            })
        runtime._record_replay_event("orchestration.policy_selected", orch_evidence)


    ev_cancel = cancel_event or register_cancellation(task_id)
    last_event = None
    try:
        for ev in runtime.run_stream(prompt, cancel_event=ev_cancel):
            last_event = ev
    finally:
        unregister_cancellation(task_id)

    # Phase 16: populate final live usage accounting into orchestration evidence
    if hasattr(runtime, "budget_enforcer") and runtime.budget_enforcer is not None:
        orch_evidence["usage_final"] = runtime.budget_enforcer.final_evidence()

    latest_rec = durable_state.get(task_id, tenant_id)
    res_data = json.loads(latest_rec.get("result_json") or "{}") if latest_rec and latest_rec.get("result_json") else {}

    # Phase 18: Governed Learning Loop — promote verified task outcome to Engineering Memory
    if latest_rec and latest_rec.get("status") in (durable_state.STATUS_SUCCEEDED, durable_state.STATUS_REVIEW_PENDING):
        try:
            from execution.orchestration import EngineeringMemoryKernel
            mem_kernel = EngineeringMemoryKernel()
            promoted_mem = mem_kernel.promote_task_outcome(
                task_id=task_id,
                tenant_id=tenant_id,
                task_type=classification.task_type.value if hasattr(classification, "task_type") else "general",
                task_objective=prompt,
                summary=res_data.get("summary") or "Task completed and verified",
                diff=res_data.get("diff") or "",
                files_changed=res_data.get("files") or [],
                verification_summary={
                    "status": res_data.get("verification", "VERIFIED"),
                    "details": res_data.get("verification_details", ""),
                    "score": 1.0,
                },
                repo_id=payload.get("repo_id") or payload.get("repo_root"),
                skills_used=[c.skill.skill_id for c in skill_candidates] if 'skill_candidates' in locals() else [],
            )
            if promoted_mem:
                runtime._record_replay_event("memory.persisted", {"record_id": promoted_mem.record_id, "scope": promoted_mem.scope.value})
                orch_evidence["memory_persisted"] = promoted_mem.as_dict()
        except Exception as _mem_p_exc:
            logger.debug("Pipeline memory promote note: %s", _mem_p_exc)

    # Phase 19: Record plan terminal status replay event
    if "execution_plan" in orch_evidence:
        p_dict = orch_evidence["execution_plan"]
        p_id = p_dict.get("plan_id")
        p_steps = p_dict.get("steps", {})
        if latest_rec and latest_rec.get("status") in (durable_state.STATUS_SUCCEEDED, durable_state.STATUS_REVIEW_PENDING):
            runtime._record_replay_event("plan.completed", {
                "plan_id": p_id,
                "steps_completed": len(p_steps),
                "terminal_status": latest_rec.get("status"),
            })
        elif latest_rec and latest_rec.get("status") == durable_state.STATUS_FAILED:
            runtime._record_replay_event("plan.failed", {
                "plan_id": p_id,
                "terminal_status": "FAILED",
            })


    return {
        "ok": True,
        "task_id": task_id,
        "status": latest_rec["status"] if latest_rec else "unknown",
        "last_event": last_event,
        "result": res_data,
        "replay_events": list(runtime.replay_events),
        "orchestration": orch_evidence,
    }


@tasks_bp.route("/api/task/<tid>/execute", methods=["POST"])
@tasks_bp.route("/api/tasks/<tid>/execute", methods=["POST"])
@tasks_bp.route("/api/task/<tid>/run", methods=["POST"])
@tasks_bp.route("/api/tasks/<tid>/run", methods=["POST"])
@token_required
def execute_task(tid: str):
    """Execute a persisted task through the authoritative TaskExecutionContext and RealAgentRuntime pipeline."""
    tenant_id = _get_tenant()
    record = durable_state.get(tid, tenant_id)
    if not record:
        return jsonify({"ok": False, "error": "Task not found"}), 404

    blocked = block_durable_execution(tid, tenant_id) or record
    response = execution_disabled_payload()
    response.update({
        "task_id": tid,
        "status": blocked.get("status", durable_state.STATUS_CANCELLED).lower(),
    })
    return jsonify(response), 403


@tasks_bp.route("/api/task/<tid>/diff", methods=["GET"])
@tasks_bp.route("/api/tasks/<tid>/diff", methods=["GET"])
@token_required
def get_task_diff(tid: str):
    """Retrieve verified diff for a task."""
    tenant_id = _get_tenant()
    record = durable_state.get(tid, tenant_id)
    if not record:
        return jsonify({"ok": False, "error": "Task not found"}), 404

    try:
        result, payload = _parse_diff_record(record)
    except ValueError:
        logger.warning("Invalid durable task diff record for %s", tid)
        return _diff_failure(tid, 500, "DIFF_RECORD_INVALID", "Diff record is invalid")

    if "diff" in result:
        diff_text = result.get("diff")
    else:
        try:
            diff_text = _authorized_worktree_diff(tid, tenant_id, payload)
        except LookupError:
            return _diff_failure(tid, 404, "DIFF_NOT_FOUND", "Diff not found")
        except DiffExtractionError:
            return _diff_failure(tid, 413, "DIFF_LIMIT_EXCEEDED", "Diff exceeds the permitted response limit")
        except Exception:
            logger.warning("Authorized task diff extraction failed for %s", tid)
            return _diff_failure(tid, 502, "DIFF_UNAVAILABLE", "Diff could not be retrieved")

    try:
        return jsonify(diff_payload(tid, diff_text))
    except DiffExtractionError:
        return _diff_failure(tid, 413, "DIFF_LIMIT_EXCEEDED", "Diff exceeds the permitted response limit")


@tasks_bp.route("/api/task/<tid>/review", methods=["GET", "POST"])
@tasks_bp.route("/api/tasks/<tid>/review", methods=["GET", "POST"])
@token_required
def get_task_review(tid: str):
    """Retrieve full review package (diff, verification evidence, replay, repair history) for a task."""
    tenant_id = _get_tenant()
    record = durable_state.get(tid, tenant_id)
    if not record:
        return jsonify({"ok": False, "error": "Task not found"}), 404

    try:
        result, payload = _parse_diff_record(record)
    except ValueError:
        logger.warning("Invalid durable task review record for %s", tid)
        return _diff_failure(tid, 500, "DIFF_RECORD_INVALID", "Diff record is invalid")

    if "diff" in result:
        diff_text = result.get("diff")
    else:
        try:
            diff_text = _authorized_worktree_diff(tid, tenant_id, payload)
        except LookupError:
            return _diff_failure(tid, 404, "DIFF_NOT_FOUND", "Diff not found")
        except DiffExtractionError:
            return _diff_failure(tid, 413, "DIFF_LIMIT_EXCEEDED", "Diff exceeds the permitted response limit")
        except Exception:
            logger.warning("Authorized review diff extraction failed for %s", tid)
            return _diff_failure(tid, 502, "DIFF_UNAVAILABLE", "Diff could not be retrieved")

    try:
        diff_data = diff_payload(tid, diff_text)
    except DiffExtractionError:
        return _diff_failure(tid, 413, "DIFF_LIMIT_EXCEEDED", "Diff exceeds the permitted response limit")
    return jsonify({
        "ok": True,
        "task_id": tid,
        "status": record["status"],
        "review_status": result.get("review_status") or record["status"],
        "verification": result.get("verification"),
        "verification_details": result.get("verification_details"),
        "summary": result.get("summary", ""),
        "files": result.get("files", []),
        "diff": diff_data["diff"],
        "repair_history": result.get("repair_history", []),
        "hitl_decisions": result.get("hitl_decisions", []),
        "execution_context": result.get("execution_context", {}),
    })


@tasks_bp.route("/api/task/<tid>/accept", methods=["POST"])
@tasks_bp.route("/api/tasks/<tid>/accept", methods=["POST"])
@tasks_bp.route("/api/task/<tid>/promote", methods=["POST"])
@tasks_bp.route("/api/tasks/<tid>/promote", methods=["POST"])
@token_required
def accept_task(tid: str):
    """Explicit human acceptance and promotion boundary for verified task changes."""
    tenant_id = _get_tenant()
    body = request.get_json(silent=True) or {}
    comment = body.get("comment", "")
    target_ref = body.get("target_ref", "HEAD")
    user_id = getattr(g, "user_id", None) or "human_operator"

    record = durable_state.get(tid, tenant_id)
    if not record:
        return jsonify({"ok": False, "error": "Task not found"}), 404

    curr_status = (record.get("status") or "").upper()
    if curr_status not in (durable_state.STATUS_SUCCEEDED, durable_state.STATUS_REVIEW_PENDING):
        return jsonify({
            "ok": False,
            "error": f"Cannot accept task in status '{curr_status}'. Task must be SUCCEEDED or REVIEW_PENDING."
        }), 409

    payload = json.loads(record.get("payload_json") or "{}")
    result = json.loads(record.get("result_json") or "{}") if record.get("result_json") else {}

    promotion_metadata = {
        "promoted": True,
        "branch": payload.get("branch") or result.get("execution_context", {}).get("branch"),
        "target_ref": target_ref,
        "approver": str(user_id),
        "timestamp": time.time(),
        "status": durable_state.STATUS_ACCEPTED,
    }
    result["review_decision"] = {
        "decision": "accept",
        "approver": str(user_id),
        "comment": comment,
        "timestamp": time.time(),
    }
    result["promotion_metadata"] = promotion_metadata
    result["review_status"] = "ACCEPTED"

    try:
        updated = durable_state.accept(
            tid,
            tenant_id,
            comment=comment,
            approver=str(user_id),
            result=result,
        )
        return jsonify({
            "ok": True,
            "task_id": tid,
            "status": durable_state.STATUS_ACCEPTED,
            "promotion_metadata": promotion_metadata,
        })
    except durable_state.TransitionError as te:
        return jsonify({"ok": False, "error": str(te)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@tasks_bp.route("/api/task/<tid>/reject", methods=["POST"])
@tasks_bp.route("/api/tasks/<tid>/reject", methods=["POST"])
@token_required
def reject_task(tid: str):
    """Explicit human rejection of task changes; prunes isolated worktree and branch."""
    tenant_id = _get_tenant()
    body = request.get_json(silent=True) or {}
    reason = body.get("reason") or body.get("comment", "")
    user_id = getattr(g, "user_id", None) or "human_operator"

    record = durable_state.get(tid, tenant_id)
    if not record:
        return jsonify({"ok": False, "error": "Task not found"}), 404

    curr_status = (record.get("status") or "").upper()
    if curr_status not in (durable_state.STATUS_SUCCEEDED, durable_state.STATUS_REVIEW_PENDING):
        return jsonify({
            "ok": False,
            "error": f"Cannot reject task in status '{curr_status}'. Task must be SUCCEEDED or REVIEW_PENDING."
        }), 409

    payload = json.loads(record.get("payload_json") or "{}")
    result = json.loads(record.get("result_json") or "{}") if record.get("result_json") else {}

    cleanup_info = {}
    wt_id = payload.get("worktree_id") or result.get("execution_context", {}).get("worktree_id")
    branch = payload.get("branch") or result.get("execution_context", {}).get("branch")
    repo_root = payload.get("repo_root") or result.get("execution_context", {}).get("repo_root")
    ws_dir = payload.get("workspace_dir") or result.get("execution_context", {}).get("workspace_dir")

    if wt_id or branch:
        try:
            from execution.agent_runtime.context import TaskExecutionContext, cleanup_execution_worktree
            ctx = TaskExecutionContext(
                task_id=tid,
                objective="rejection_cleanup",
                workspace_dir=ws_dir or Path(repo_root or "."),
                tenant_id=tenant_id,
                repo_root=repo_root,
                worktree_id=wt_id,
                branch=branch,
            )
            cleanup_info = cleanup_execution_worktree(ctx, delete_branch=True)
        except Exception as disc_exc:
            logger.warning("Worktree cleanup failed during rejection: %s", disc_exc)
            cleanup_info = {"ok": False, "error": str(disc_exc)}

    result["review_decision"] = {
        "decision": "reject",
        "reviewer": str(user_id),
        "reason": reason,
        "timestamp": time.time(),
        "cleanup": cleanup_info,
    }
    result["review_status"] = "REJECTED"

    try:
        updated = durable_state.reject(
            tid,
            tenant_id,
            comment=reason,
            reviewer=str(user_id),
            result=result,
        )
        return jsonify({
            "ok": True,
            "task_id": tid,
            "status": durable_state.STATUS_REJECTED,
            "cleanup": cleanup_info,
        })
    except durable_state.TransitionError as te:
        return jsonify({"ok": False, "error": str(te)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@tasks_bp.route("/api/v1/agent/invoke", methods=["POST"])
@tasks_bp.route("/api/agent/stream", methods=["POST"])
@token_required
def agent_invoke():
    """Authoritative autonomous agent execution streaming with isolated worktree and TaskExecutionContext."""
    # Reject before parsing/creating a workspace or task runtime. No caller
    # parameter can opt this endpoint into execution.
    return jsonify(execution_disabled_payload()), 403
