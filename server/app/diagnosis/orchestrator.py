"""可恢复、受预算约束的 AI 集群诊断编排器。"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from server.app import storage
from server.app.ai_provider import get_ai_settings, is_feature_enabled
from server.app.common_utils import status_value
from server.app.diagnosis.intent import parse_diagnosis_intent
from server.app.diagnosis.collector_invocation import build_collector_invocation, collector_request_fingerprint
from server.app.diagnosis.probe_registry import build_probe_manifest, choose_probe_ids, evidence_gap_to_probe_id, get_probe
from server.app.diagnosis.schemas import (
    ApprovalRequest,
    BulkApprovalRequest,
    CreateDiagnosisRequest,
    DiagnosisBudget,
    DiagnosisStatus,
    ProbePlan,
    TERMINAL_DIAGNOSIS_STATUSES,
)
from server.app.diagnosis.store import DiagnosisStore, utcnow
from server.app.event_bus import BUS
from server.app.rca.calibrator import calibrate
from server.app.rca.candidates import generate_candidates
from server.app.rca.evidence import collect_evidence
from server.app.rca.attribution import analyze_evidence
from server.app.rca.llm_client import generate_controlled_ai_tree, generate_compact_guarded_tree
from server.app.rca.models import (
    AITreeBudgetSnapshot,
    AITreeCandidateNode,
    AITreeLayer,
    AITreeProbeEdge,
    AITreeProbeResult,
    AITreeSelfChallenge,
    CandidateCause,
    ControlledAITree,
    EvidenceInput,
)
from server.app.diagnosis.evidence_structurer import rca_inputs_from_structured, structure_artifact_evidence
from server.app.schemas import CreateTaskRequest, MAX_SAMPLE_RATE, MAX_TASK_DURATION_SEC, MIN_SAMPLE_RATE


PLANNER_VERSION = "diagnosis-orchestrator-v1"
ACTIVE_TASK_STATUSES = {"PENDING", "RUNNING", "UPLOADING", "ANALYZING"}
TERMINAL_TASK_STATUSES = {"DONE", "FAILED"}
STRUCTURED_ARTIFACT_TYPES = {
    "top_json",
    "flamegraph_json",
    "ebpf_metrics",
    "sys_metrics",
    "memory_json",
    "depth_evidence_json",
    "continuous_top_json",
    "continuous_flamegraph_json",
    "continuous_summary",
    "off_cpu_wait_json",
    "log_window_json",
    "dependency_check_json",
    "redis_check_json",
    "trace_endpoint_profile_json",
}
DEPENDENCY_EVIDENCE_GAPS = {"dependency_check", "log_scan", "redis_check"}
FUNCTION_DEPTH_EVIDENCE_GAPS = {
    "baseline_window_profile",
    "cpu_profile",
    "off_cpu_wait_profile",
    "trace_endpoint_profile",
    "python_runtime_profile",
}
ALLOWED_DIAGNOSIS_TRANSITIONS = {
    "CREATED": {"UNDERSTANDING", "USER_CANCELED", "FAILED"},
    "UNDERSTANDING": {"PLANNING", "NEEDS_SCOPE_CONFIRMATION", "TOPOLOGY_UNAVAILABLE", "FAILED"},
    "PLANNING": {"ANALYZING_EXISTING_DATA", "BUDGET_EXHAUSTED", "FAILED"},
    "ANALYZING_EXISTING_DATA": {"ANALYZING", "COLLECTING", "WAITING_APPROVAL", "INSUFFICIENT_EVIDENCE", "FAILED"},
    "COLLECTING": {"ANALYZING", "WAITING_APPROVAL", "NEED_MORE_EVIDENCE", "BUDGET_EXHAUSTED", "FAILED"},
    "ANALYZING": {"CONCLUDING", "WAITING_APPROVAL", "COLLECTING", "INSUFFICIENT_EVIDENCE", "PARTIAL_COMPLETED", "FAILED"},
    "WAITING_APPROVAL": {"ANALYZING", "COLLECTING", "NEED_MORE_EVIDENCE", "BUDGET_EXHAUSTED", "USER_CANCELED", "FAILED"},
    "NEED_MORE_EVIDENCE": {"ANALYZING", "COLLECTING", "WAITING_APPROVAL", "INSUFFICIENT_EVIDENCE", "PARTIAL_COMPLETED", "FAILED"},
    "CONCLUDING": {"COMPLETED", "INSUFFICIENT_EVIDENCE", "PARTIAL_COMPLETED", "FAILED"},
}


class DiagnosisOrchestrator:
    def __init__(self, task_repository, store: DiagnosisStore | None = None):
        self.repo = task_repository
        self.store = store or DiagnosisStore()
        self.owner = f"{socket.gethostname()}:{os.getpid()}"

    def create(self, request: CreateDiagnosisRequest, creator_id: str = "demo_user") -> dict[str, Any]:
        intent = parse_diagnosis_intent(request)
        self._enforce_service_scope(intent.target_service)
        budget = self._effective_budget(request.budget_profile, request.budget)
        diagnosis_id = f"diag_session_{utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        snapshot = self._build_topology_snapshot(request, intent)
        self.store.create_topology_snapshot(snapshot)

        target_scope = self._build_target_scope(request, intent, budget)
        hypotheses = self._build_hypotheses(intent.symptom, target_scope)
        budget_usage = self._empty_budget_usage()
        budget_usage["model_calls"] = 1 if is_feature_enabled("nlp") else 0
        self.store.create_session({
            "diagnosis_id": diagnosis_id,
            "creator_id": creator_id,
            "raw_query": request.query,
            "normalized_intent": intent.model_dump(mode="json"),
            "target_scope": target_scope,
            "requested_time_range": intent.time_range.model_dump(mode="json"),
            "effective_time_range": intent.time_range.model_dump(mode="json"),
            "topology_snapshot_id": snapshot["snapshot_id"],
            "status": DiagnosisStatus.CREATED.value,
            "policy_profile": request.budget_profile,
            "risk_budget": {
                "max_medium_risk_probes": budget.max_medium_risk_probes,
                "no_automatic_remediation": True,
                "registered_probes_only": True,
                "auto_execute_policy": request.auto_execute_policy or os.getenv("MINI_DROP_DIAGNOSIS_AUTO_EXECUTE_POLICY", "all_registered"),
            },
            "resource_budget": budget.model_dump(mode="json"),
            "budget_used": budget_usage,
            "hypothesis_graph": {"hypotheses": hypotheses, "edges": []},
            "child_task_ids": [],
            "conclusion_versions": [],
            "model_version": get_ai_settings().model,
            "planner_version": PLANNER_VERSION,
        })
        self._transition(diagnosis_id, DiagnosisStatus.UNDERSTANDING, "intent_parsed")

        if not target_scope["instances"]:
            self._transition(
                diagnosis_id,
                DiagnosisStatus.NEEDS_SCOPE_CONFIRMATION,
                "scope_confirmation_required",
                {"ambiguities": intent.ambiguities},
            )
            self._append_scope_help_conclusion(diagnosis_id, request.query, intent.ambiguities)
            return self.store.get_detail(diagnosis_id) or {}

        self._transition(diagnosis_id, DiagnosisStatus.PLANNING, "plan_created")
        self._transition(
            diagnosis_id,
            DiagnosisStatus.ANALYZING_EXISTING_DATA,
            "existing_data_analysis_started",
        )

        existing_ids = self._find_reusable_tasks(target_scope, intent.time_range.start, intent.time_range.end)
        if existing_ids:
            self.store.update_session(diagnosis_id, child_task_ids=existing_ids)
            existing_tasks = [self.repo.tasks[task_id] for task_id in existing_ids if task_id in self.repo.tasks]
            self._transition(
                diagnosis_id,
                DiagnosisStatus.ANALYZING,
                "evidence_analysis_started",
            )
            if self._analyze_tasks(diagnosis_id, existing_tasks):
                if self._last_followup_scheduled:
                    return self.store.get_detail(diagnosis_id) or {}
                self._transition(diagnosis_id, DiagnosisStatus.CONCLUDING, "conclusion_generated")
                self._transition(diagnosis_id, DiagnosisStatus.COMPLETED, "diagnosis_completed")
                return self.store.get_detail(diagnosis_id) or {}

        self._plan_and_schedule(diagnosis_id, intent.symptom, target_scope, budget)
        self._advance_locked(diagnosis_id)
        return self.store.get_detail(diagnosis_id) or {}

    def get(self, diagnosis_id: str, advance: bool = True) -> dict[str, Any] | None:
        item = self.store.get_session(diagnosis_id)
        if item is None:
            return None
        if advance and item["status"] not in TERMINAL_DIAGNOSIS_STATUSES:
            self.advance(diagnosis_id)
        return self.store.get_detail(diagnosis_id)

    def list(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self.store.list_sessions(limit=limit, offset=offset)

    def advance(self, diagnosis_id: str) -> dict[str, Any] | None:
        if not self.store.acquire_lease(diagnosis_id, self.owner):
            return self.store.get_detail(diagnosis_id)
        try:
            self._advance_locked(diagnosis_id)
        finally:
            self.store.release_lease(diagnosis_id, self.owner)
        return self.store.get_detail(diagnosis_id)

    def advance_active(self, limit: int = 100) -> None:
        """由后台扫描器调用，使恢复不依赖用户 GET 请求。"""
        for item in self.store.list_sessions(limit=limit, offset=0):
            if item["status"] in TERMINAL_DIAGNOSIS_STATUSES:
                continue
            try:
                self.advance(item["diagnosis_id"])
            except Exception:
                # 单个诊断异常不能阻塞其他会话；HTTP 读取仍可暴露原状态供排查。
                continue

    def approve(self, diagnosis_id: str, request: ApprovalRequest) -> dict[str, Any]:
        session = self.store.get_session(diagnosis_id)
        if session is None:
            raise ValueError("诊断不存在")
        if session["status"] in TERMINAL_DIAGNOSIS_STATUSES:
            raise ValueError(f"终态诊断不能审批: {session['status']}")
        step = self.store.get_probe(request.step_id)
        if step is None or step["diagnosis_id"] != diagnosis_id:
            raise ValueError("审批步骤不存在或不属于当前诊断")
        if not step["requires_approval"]:
            raise ValueError("该探针不需要审批")
        if step["status"] not in {"WAITING_APPROVAL", "APPROVED"}:
            raise ValueError(f"当前探针状态不可审批: {step['status']}")

        if request.decision == "reject":
            self.store.update_probe(
                request.step_id,
                status="REJECTED",
                approved_by=request.approver_id,
                approved_at=utcnow(),
            )
            self._transition(
                diagnosis_id,
                DiagnosisStatus.NEED_MORE_EVIDENCE,
                "approval_rejected",
                {"step_id": request.step_id, "approver_id": request.approver_id},
            )
            return self.advance(diagnosis_id) or {}

        approved_r2 = sum(
            1 for probe in self.store.list_probes(diagnosis_id)
            if probe["risk_level"] == "R2" and probe["status"] in {
                "APPROVED", "SCHEDULED", "RUNNING", "COMPLETED",
            }
        )
        limit = int(session["risk_budget"].get("max_medium_risk_probes", 0))
        if approved_r2 >= limit:
            self._transition(
                diagnosis_id,
                DiagnosisStatus.BUDGET_EXHAUSTED,
                "risk_budget_exhausted",
                {"max_medium_risk_probes": limit},
            )
            return self.store.get_detail(diagnosis_id) or {}

        active_count = 0
        for probe in self.store.list_probes(diagnosis_id):
            task_id = probe.get("task_id")
            task = self.repo.tasks.get(task_id) if task_id else None
            if task is not None and status_value(task.status) in ACTIVE_TASK_STATUSES:
                active_count += 1
        parallel_limit = int(session["resource_budget"].get("max_parallel_probes", 1))
        if active_count >= parallel_limit:
            raise ValueError("并发探针预算已用尽，请等待当前探针完成后重试审批")

        duration = int(step["parameters"].get("duration_sec", 0))
        used_duration = int(session["budget_used"].get("probe_duration_seconds", 0))
        phase = _probe_budget_phase(step)
        duration_limit = self._duration_limit(session, phase)
        if used_duration + duration > duration_limit:
            self._transition(
                diagnosis_id,
                DiagnosisStatus.BUDGET_EXHAUSTED,
                "resource_budget_exhausted",
                {
                    "budget_phase": phase,
                    "probe_duration_limit_seconds": duration_limit,
                    "used_seconds": used_duration,
                    "requested_seconds": duration,
                    "reserved_seconds": self._follow_up_reserve(session),
                    "next_action": "减少初始采集范围或进入 AI 树 follow-up 阶段",
                },
            )
            return self.store.get_detail(diagnosis_id) or {}

        self.store.update_probe(
            request.step_id,
            status="APPROVED",
            approved_by=request.approver_id,
            approved_at=utcnow(),
        )
        self.store.record_event(
            diagnosis_id,
            "approval_granted",
            {"step_id": request.step_id, "approver_id": request.approver_id, "scope": request.scope},
        )
        self._schedule_probe(request.step_id)
        self._transition(
            diagnosis_id,
            DiagnosisStatus.COLLECTING,
            "probe_started",
            {"step_id": request.step_id},
        )
        return self.store.get_detail(diagnosis_id) or {}

    def approve_waiting(self, diagnosis_id: str, request: BulkApprovalRequest) -> dict[str, Any]:
        session = self.store.get_session(diagnosis_id)
        if session is None:
            raise ValueError("诊断不存在")
        if session["status"] in TERMINAL_DIAGNOSIS_STATUSES:
            raise ValueError(f"终态诊断不能审批: {session['status']}")
        waiting = [
            probe for probe in self.store.list_probes(diagnosis_id)
            if probe["status"] == "WAITING_APPROVAL" and probe["requires_approval"]
        ]
        if not waiting:
            raise ValueError("当前没有待审批探针")

        detail: dict[str, Any] = self.store.get_detail(diagnosis_id) or {}
        for probe in waiting:
            detail = self.approve(diagnosis_id, ApprovalRequest(
                step_id=probe["step_id"],
                decision=request.decision,
                scope="single_execution",
                approver_id=request.approver_id,
            ))
            if detail.get("status") in TERMINAL_DIAGNOSIS_STATUSES:
                break
        self.store.record_event(
            diagnosis_id,
            "bulk_approval_applied",
            {
                "decision": request.decision,
                "requested_scope": request.scope,
                "approver_id": request.approver_id,
                "step_ids": [probe["step_id"] for probe in waiting],
            },
        )
        return self.store.get_detail(diagnosis_id) or detail

    def _advance_locked(self, diagnosis_id: str) -> None:
        session = self.store.get_session(diagnosis_id)
        if session is None or session["status"] in TERMINAL_DIAGNOSIS_STATUSES:
            return
        probes = self.store.list_probes(diagnosis_id)
        child_ids = list(session.get("child_task_ids", []))

        for probe in probes:
            task_id = probe.get("task_id")
            if not task_id:
                continue
            task = self.repo.tasks.get(task_id)
            if task is None:
                self.store.update_probe(probe["step_id"], status="FAILED")
                continue
            task_status = status_value(task.status)
            if task_status in ACTIVE_TASK_STATUSES and probe["status"] != "RUNNING":
                self.store.update_probe(probe["step_id"], status="RUNNING")
            elif task_status == "DONE" and probe["status"] != "COMPLETED":
                self.store.update_probe(probe["step_id"], status="COMPLETED")
            elif task_status == "FAILED" and probe["status"] != "FAILED":
                self.store.update_probe(probe["step_id"], status="FAILED")
            if task_id not in child_ids:
                child_ids.append(task_id)

        if child_ids != session.get("child_task_ids", []):
            session = self.store.update_session(diagnosis_id, child_task_ids=child_ids)
        self._schedule_deferred_followups(diagnosis_id)
        session = self.store.get_session(diagnosis_id) or session
        child_ids = list(session.get("child_task_ids", []))

        terminal_tasks = []
        active_tasks = []
        pending_tasks = []
        for task_id in child_ids:
            task = self.repo.tasks.get(task_id)
            if task is None:
                continue
            task_status = status_value(task.status)
            if task_status in TERMINAL_TASK_STATUSES:
                terminal_tasks.append(task)
            elif task_status in ACTIVE_TASK_STATUSES:
                active_tasks.append(task)
            else:
                pending_tasks.append(task)

        waiting = [probe for probe in self.store.list_probes(diagnosis_id) if probe["status"] == "WAITING_APPROVAL"]

        if terminal_tasks:
            if active_tasks or pending_tasks:
                if session["status"] != DiagnosisStatus.COLLECTING.value:
                    self._transition(diagnosis_id, DiagnosisStatus.COLLECTING, "probe_started")
                return
            self._transition(diagnosis_id, DiagnosisStatus.ANALYZING, "evidence_analysis_started")
            informative = self._analyze_tasks(diagnosis_id, terminal_tasks)
            if self._last_followup_scheduled:
                return
            if informative:
                for probe in self.store.list_probes(diagnosis_id):
                    if probe["status"] == "WAITING_APPROVAL":
                        self.store.update_probe(probe["step_id"], status="SKIPPED")
                latest = (self.store.get_session(diagnosis_id) or {}).get("conclusion_versions", [])
                latest_conclusion = latest[-1] if latest else {}
                nonblocking_failed_depth = bool(
                    (latest_conclusion.get("coverage") or {}).get("nonblocking_failed_depth")
                )
                final_status = (
                    DiagnosisStatus.PARTIAL_COMPLETED
                    if any(status_value(task.status) == "FAILED" for task in terminal_tasks) and not nonblocking_failed_depth
                    else DiagnosisStatus.COMPLETED
                )
                latest_session = self.store.get_session(diagnosis_id) or session
                if latest_session["status"] != DiagnosisStatus.ANALYZING.value:
                    self._transition(diagnosis_id, DiagnosisStatus.ANALYZING, "evidence_analysis_started")
                self._transition(diagnosis_id, DiagnosisStatus.CONCLUDING, "conclusion_generated")
                self._transition(diagnosis_id, final_status, "diagnosis_completed")
                return

        if active_tasks:
            if session["status"] != DiagnosisStatus.COLLECTING.value:
                self._transition(diagnosis_id, DiagnosisStatus.COLLECTING, "probe_started")
            return

        if waiting:
            self._transition(
                diagnosis_id,
                DiagnosisStatus.WAITING_APPROVAL,
                "approval_required",
                {"step_ids": [probe["step_id"] for probe in waiting]},
            )
            return

        if terminal_tasks:
            final = (
                DiagnosisStatus.PARTIAL_COMPLETED
                if any(status_value(task.status) == "FAILED" for task in terminal_tasks)
                else DiagnosisStatus.INSUFFICIENT_EVIDENCE
            )
            self._ensure_insufficient_conclusion(diagnosis_id, terminal_tasks)
            self._transition(diagnosis_id, final, "diagnosis_completed")
            return

        probes = self.store.list_probes(diagnosis_id)
        if probes and all(p["status"] in {
            "UNAVAILABLE", "REJECTED", "REJECTED_POLICY", "INVALID", "FAILED", "SKIPPED",
        } for p in probes):
            self._ensure_insufficient_conclusion(diagnosis_id, [])
            self._transition(
                diagnosis_id,
                DiagnosisStatus.INSUFFICIENT_EVIDENCE,
                "diagnosis_completed",
            )

    def _plan_and_schedule(
        self,
        diagnosis_id: str,
        symptom: str,
        target_scope: dict[str, Any],
        budget: DiagnosisBudget,
    ) -> None:
        instances = target_scope["instances"][:budget.max_service_instances]
        probe_ids = _scope_probe_ids(symptom, target_scope)
        planned: list[ProbePlan] = []
        r2_count = 0
        auto_count = 0
        planned_duration = 0
        total_limit = min(
            budget.max_duration_minutes * 60,
            budget.max_total_probe_cpu_seconds,
        )
        reserve = max(
            0,
            min(budget.follow_up_reserve_seconds, total_limit),
        )
        duration_limit = max(0, total_limit - reserve)
        session = self.store.get_session(diagnosis_id) or {}
        auto_policy = str((session.get("risk_budget") or {}).get("auto_execute_policy") or "safe_only")
        for index, instance in enumerate(instances):
            for probe_id in probe_ids:
                definition = get_probe(probe_id)
                if definition.risk_level == "R2" and auto_policy != "all_registered":
                    if index > 0 or r2_count >= budget.max_medium_risk_probes:
                        continue
                    r2_count += 1
                elif probe_id != "host_process_metrics":
                    if auto_count >= budget.max_parallel_probes:
                        continue
                    auto_count += 1
                duration = min(definition.default_duration_seconds, definition.max_duration_seconds)
                if planned_duration + duration > duration_limit:
                    continue
                planned_duration += duration
                key = f"{diagnosis_id}:{probe_id}:{instance['instance_id']}"
                step_id = f"step_{hashlib.sha256(key.encode()).hexdigest()[:14]}"
                collector_parameters = self._collector_probe_parameters(probe_id, target_scope, instance)
                collector_invocation = _collector_invocation(
                    step={"diagnosis_id": diagnosis_id, "step_id": step_id},
                    definition=definition,
                    target=instance,
                    collector_parameters=collector_parameters,
                )
                fingerprint_inputs = {
                    "duration_sec": duration,
                    "sample_rate": definition.default_sample_rate,
                    "evidence_gap": self._probe_evidence_gap(probe_id),
                    "budget_phase": "initial",
                }
                planned.append(ProbePlan(
                    step_id=step_id,
                    probe_id=probe_id,
                    target=instance,
                    parameters={
                        "duration_sec": duration,
                        "sample_rate": definition.default_sample_rate,
                        "evidence_gap": self._probe_evidence_gap(probe_id),
                        "budget_phase": "initial",
                        "execution_policy": auto_policy,
                        **collector_parameters,
                        "collector_invocation": collector_invocation,
                        "collector_fingerprint": collector_request_fingerprint(
                            collector_invocation,
                            {**fingerprint_inputs, **collector_parameters},
                        ),
                    },
                    reason=f"用于区分 {', '.join(definition.applicable_hypotheses[:3])} 等候选假设",
                    risk_level=definition.risk_level,
                    requires_approval=definition.requires_approval and auto_policy != "all_registered",
                ))

        for plan in planned:
            status = "WAITING_APPROVAL" if plan.requires_approval else "PLANNED"
            self.store.add_probe({
                **plan.model_dump(mode="json"),
                "diagnosis_id": diagnosis_id,
                "status": status,
            })
            if not plan.requires_approval:
                self._schedule_probe(plan.step_id)

    def _schedule_probe(self, step_id: str) -> None:
        step = self.store.get_probe(step_id)
        if step is None or step.get("task_id"):
            return
        definition = get_probe(step["probe_id"])
        target = step["target"]
        session = self.store.get_session(step["diagnosis_id"])
        if session is None:
            self.store.update_probe(step_id, status="INVALID")
            return
        allowed_targets = {
            (item.get("instance_id"), item.get("agent_id"), item.get("pid"))
            for item in session.get("target_scope", {}).get("instances", [])
        }
        target_key = (target.get("instance_id"), target.get("agent_id"), target.get("pid"))
        if target_key not in allowed_targets or step["risk_level"] != definition.risk_level:
            self.store.update_probe(step_id, status="REJECTED_POLICY")
            return
        if definition.requires_approval and step["status"] != "APPROVED":
            if (step.get("parameters") or {}).get("execution_policy") != "all_registered":
                self.store.update_probe(step_id, status="WAITING_APPROVAL")
                return
        self._enforce_service_scope(target.get("service_id"))
        try:
            duration = int(step["parameters"]["duration_sec"])
            sample_rate = int(step["parameters"]["sample_rate"])
        except (KeyError, TypeError, ValueError):
            self.store.update_probe(step_id, status="INVALID")
            return
        if not (1 <= duration <= min(definition.max_duration_seconds, MAX_TASK_DURATION_SEC)):
            self.store.update_probe(step_id, status="REJECTED_POLICY")
            return
        if not (MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE):
            self.store.update_probe(step_id, status="REJECTED_POLICY")
            return
        agent = self.repo.agents.get(target["agent_id"])
        if agent is None or status_value(agent.status) != "ONLINE":
            self.store.update_probe(step_id, status="UNAVAILABLE")
            return
        capabilities = set(getattr(agent, "capabilities", []) or [])
        if definition.runner_task_kind not in capabilities:
            self.store.update_probe(step_id, status="UNAVAILABLE")
            return

        reusable = self._find_reusable_probe_task(step)
        if reusable is not None:
            reused_status = "COMPLETED" if status_value(reusable.status) == "DONE" else "FAILED"
            self.store.update_probe(step_id, status=reused_status, task_id=reusable.id)
            self._append_child_task(step["diagnosis_id"], reusable.id, definition)
            self.store.record_event(
                step["diagnosis_id"],
                "probe_reused",
                {
                    "step_id": step_id,
                    "task_id": reusable.id,
                    "collector_fingerprint": (step.get("parameters") or {}).get("collector_fingerprint"),
                    "reuse_status": "reuse_hit" if reused_status == "COMPLETED" else "reuse_blocked_result",
                },
            )
            return

        # 恢复时先通过幂等键查找已创建任务，避免重复下发。
        for task in self.repo.tasks.values():
            options = (task.request_params or {}).get("options", {})
            if options.get("diagnosis_step_id") == step_id:
                self.store.update_probe(step_id, status="SCHEDULED", task_id=task.id)
                self._append_child_task(step["diagnosis_id"], task.id, definition)
                return

        task = self.repo.create_task(CreateTaskRequest(
            name=f"AI诊断:{definition.name}:{target['service_id']}",
            agent_id=target["agent_id"],
            target_pid=target["pid"],
            collector_type=definition.runner_task_kind,
            duration_sec=duration,
            sample_rate=sample_rate,
            options=self._task_options_for_probe(
                step,
                definition,
                session.get("target_scope", {}),
                target,
            ),
        ))
        self.store.update_probe(step_id, status="SCHEDULED", task_id=task.id)
        self._append_child_task(step["diagnosis_id"], task.id, definition)

    def _schedule_deferred_followups(self, diagnosis_id: str) -> None:
        """并发槽位释放后，继续调度已获准但暂时排队的 follow-up。"""
        session = self.store.get_session(diagnosis_id)
        if session is None:
            return
        policy = str((session.get("risk_budget") or {}).get("auto_execute_policy") or "safe_only")
        if policy != "all_registered":
            return
        for probe in self.store.list_probes(diagnosis_id):
            if probe.get("status") != "PLANNED":
                continue
            if (probe.get("parameters") or {}).get("execution_policy") != "all_registered":
                continue
            definition = get_probe(probe["probe_id"])
            duration = int((probe.get("parameters") or {}).get("duration_sec") or 0)
            if self._followup_budget_block(diagnosis_id, definition, duration):
                continue
            self._schedule_probe(probe["step_id"])

    def _task_options_for_probe(
        self,
        step: dict[str, Any],
        definition,
        target_scope: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        collector_parameters = self._collector_probe_parameters(definition.probe_id, target_scope, target)
        invocation = _collector_invocation(
            step=step,
            definition=definition,
            target=target,
            collector_parameters=collector_parameters,
        )
        options = {
            "diagnosis_id": step["diagnosis_id"],
            "diagnosis_step_id": step["step_id"],
            "probe_id": definition.probe_id,
            "registered_probe": True,
            "duration_sec": (step.get("parameters") or {}).get("duration_sec"),
            "sample_rate": (step.get("parameters") or {}).get("sample_rate"),
            "evidence_gap": (step.get("parameters") or {}).get("evidence_gap"),
            "budget_phase": (step.get("parameters") or {}).get("budget_phase"),
            "parent_task_id": (step.get("parameters") or {}).get("parent_task_id"),
            **collector_parameters,
            "collector_context": {
                "task_id": step["diagnosis_id"],
                "collector_kind": definition.runner_task_kind,
                "target_pid": target["pid"],
                "agent_id": target["agent_id"],
                "service_id": target.get("service_id"),
                "instance_id": target.get("instance_id"),
                "host_id": target.get("host_id"),
            },
            "collector_invocation": invocation,
        }
        options["collector_fingerprint"] = collector_request_fingerprint(invocation, options)
        return options

    def _collector_probe_parameters(
        self,
        probe_id: str,
        target_scope: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        if probe_id == "process_dependency_check":
            targets = _dependency_targets(target_scope)
            return {
                "target_config": {"dependency_targets": targets},
                "targets": targets,
            } if targets else {
                "target_config": {"dependency_targets": []},
                "missing_dependency_targets": True,
            }
        if probe_id == "process_redis_check":
            target_info = _redis_target(target_scope)
            dependency_targets = _dependency_targets(target_scope)
            return {
                "target_config": {"redis_target": target_info, "dependency_targets": dependency_targets},
                "dependency_targets": dependency_targets,
                **target_info,
            } if target_info else {
                "target_config": {"redis_target": {}, "dependency_targets": dependency_targets},
                "dependency_targets": dependency_targets,
                "missing_redis_target": True,
            }
        if probe_id == "process_log_scan":
            log_paths = target.get("log_paths") or target_scope.get("log_paths")
            source_paths = target.get("source_paths") or target_scope.get("source_paths")
            target_config = {
                "log_paths": log_paths or [],
                "source_paths": source_paths or [],
                "container_id": target.get("container_id"),
                "service_id": target.get("service_id"),
                "instance_id": target.get("instance_id"),
            }
            return {
                "target_config": target_config,
                **({"log_paths": log_paths} if log_paths else {}),
                **({"source_paths": source_paths} if source_paths else {}),
            }
        if probe_id == "process_trace_endpoint_profile":
            source_context = _source_context_for_target(target, target_scope)
            return {
                "target_config": {
                    "stack_source": "auto",
                    "trace_source": "auto",
                    "trace_paths": target.get("trace_paths") or target_scope.get("trace_paths") or [],
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "host_id": target.get("host_id"),
                    "endpoint": target.get("endpoint") or target_scope.get("endpoint"),
                    "container_id": target.get("container_id"),
                    "source_context": source_context,
                },
                "trace_paths": target.get("trace_paths") or target_scope.get("trace_paths") or [],
                "source_context": source_context,
            }
        if probe_id == "process_off_cpu_profile":
            source_context = _source_context_for_target(target, target_scope)
            return {
                "target_config": {
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "host_id": target.get("host_id"),
                    "endpoint": target.get("endpoint") or target_scope.get("endpoint"),
                    "trace_paths": target.get("trace_paths") or target_scope.get("trace_paths") or [],
                    "source_context": source_context,
                },
                "min_wait_ms": 1,
                "stack_depth": 32,
                "trace_paths": target.get("trace_paths") or target_scope.get("trace_paths") or [],
                "source_context": source_context,
            }
        if probe_id in {"process_cpu_profile", "process_baseline_window", "process_python_runtime_profile"}:
            source_context = _source_context_for_target(target, target_scope)
            return {
                "target_config": {
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "host_id": target.get("host_id"),
                    "container_id": target.get("container_id"),
                    "source_context": source_context,
                },
                "source_context": source_context,
            } if source_context else {}
        return {}

    def _append_child_task(self, diagnosis_id: str, task_id: str, definition) -> None:
        session = self.store.get_session(diagnosis_id)
        if session is None:
            return
        task_ids = list(session.get("child_task_ids", []))
        is_new_child = task_id not in task_ids
        if task_id not in task_ids:
            task_ids.append(task_id)
        if not is_new_child:
            return
        usage = dict(session.get("budget_used", {}))
        probe = next(
            (
                item for item in self.store.list_probes(diagnosis_id)
                if item.get("task_id") == task_id
            ),
            {},
        )
        phase = _probe_budget_phase(probe)
        usage["hosts"] = len({
            probe["target"].get("host_id")
            for probe in self.store.list_probes(diagnosis_id)
            if probe.get("task_id")
        })
        usage["service_instances"] = len({
            probe["target"].get("instance_id")
            for probe in self.store.list_probes(diagnosis_id)
            if probe.get("task_id")
        })
        usage["probes"] = sum(1 for probe in self.store.list_probes(diagnosis_id) if probe.get("task_id"))
        usage["medium_risk_probes"] = sum(
            1 for probe in self.store.list_probes(diagnosis_id)
            if probe.get("task_id") and probe["risk_level"] == "R2"
        )
        duration = int(
            (probe.get("parameters") or {}).get("duration_sec")
            or definition.default_duration_seconds
        )
        usage["probe_duration_seconds"] = usage.get("probe_duration_seconds", 0) + duration
        phase_key = f"{phase}_probe_duration_seconds"
        usage[phase_key] = usage.get(phase_key, 0) + duration
        self.store.update_session(
            diagnosis_id,
            child_task_ids=task_ids,
            budget_used=usage,
        )

    def _analyze_tasks(self, diagnosis_id: str, tasks: list[Any]) -> bool:
        self._last_followup_scheduled = False
        all_candidates: list[dict[str, Any]] = []
        followup_requests: list[str] = []
        controlled_ai_trees: list[dict[str, Any]] = []
        task_observations: list[dict[str, Any]] = []
        missing: list[str] = []
        failed_targets: list[str] = []
        for task in tasks:
            status = status_value(task.status)
            artifacts = self.repo.artifacts.get(task.id, [])
            evidence_ids = [self._add_task_evidence(diagnosis_id, task)]
            structured = self._structured_artifacts(artifacts)
            artifact_values = {kind: value for kind, value, _ in structured}
            artifact_values = _normalize_structured_artifact_values(artifact_values)
            flamegraph_svg = next(
                (self._read_artifact_text(artifact) for artifact in artifacts if artifact.get("artifact_type") == "flamegraph_svg"),
                None,
            )
            if flamegraph_svg:
                artifact_values["flamegraph_svg"] = flamegraph_svg
            structured_evidence = structure_artifact_evidence(
                task_id=task.id,
                artifacts=artifacts,
                artifact_values=artifact_values,
            )
            structured_inputs = rca_inputs_from_structured(structured_evidence)
            evidence_ids.append(self._add_artifact_evidence(
                diagnosis_id,
                task,
                "structured_evidence_json",
                structured_evidence.model_dump(mode="json"),
                {"object_key": f"task:{task.id}:artifact:structured_evidence_json"},
            ))
            for artifact_type, value, artifact in structured:
                evidence_ids.append(self._add_artifact_evidence(
                    diagnosis_id, task, artifact_type, value, artifact,
                ))
            if status == "FAILED":
                failed_targets.append(f"{task.agent_id}:{task.target_pid}")
            if not structured and not structured_evidence.artifact_refs:
                missing.append(f"{task.id}:structured_artifact")
                continue

            values = {**structured_inputs, **artifact_values}
            values["artifact_refs"] = structured_evidence.artifact_refs
            values["stack_summary"] = structured_evidence.stack_summary
            values["call_path_hotspots"] = structured_evidence.call_path_hotspots
            values["confidence_inputs"] = structured_evidence.confidence_inputs
            task_observations.append(
                self._build_task_observation(diagnosis_id, task, values, evidence_ids)
            )
            task_events = [self.repo.as_dict(event) for event in self.repo.events if event.task_id == task.id]
            session_for_evidence = self.store.get_session(diagnosis_id) or {}
            evidence = collect_evidence(
                task_id=task.id,
                task_record=task,
                top_functions=values.get("top_functions") if isinstance(values.get("top_functions"), list) else None,
                ebpf_metrics=values.get("ebpf_metrics") if isinstance(values.get("ebpf_metrics"), dict) else None,
                sys_metrics=values.get("sys_metrics") if isinstance(values.get("sys_metrics"), dict) else None,
                failure_events=[event.get("reason", "") for event in task_events if event.get("reason")],
                agent_stats=self.repo.agent_metrics.get(task.agent_id, {}),
                evidence_index=values.get("evidence_index") if isinstance(values.get("evidence_index"), dict) else {},
                tool_results=_tool_results_from_structured_values(values),
                source_context=_source_context_for_target(
                    self._target_for_task(diagnosis_id, task),
                    session_for_evidence.get("target_scope", {}),
                ),
            )
            candidates = generate_candidates(evidence, self.repo.get_feedback_priors())
            analysis_result = analyze_evidence(evidence, candidates)
            probe_manifest = build_probe_manifest()
            analysis_payload = analysis_result.model_dump(mode="json")
            analysis_payload["probe_registry_manifest"] = probe_manifest
            controlled_tree = generate_controlled_ai_tree(
                task_id=task.id,
                evidence=evidence.model_copy(update={"analysis_result": analysis_payload}),
                analyzer_result=analysis_result,
                probe_manifest=probe_manifest,
            )
            if _tree_generated_by(controlled_tree) == {"analyzer_fallback"} and is_feature_enabled("rca"):
                compact_tree = generate_compact_guarded_tree(
                    task_id=task.id,
                    evidence=evidence.model_copy(update={"analysis_result": analysis_payload}),
                    analyzer_tree=analysis_result.controlled_ai_tree,
                    probe_manifest=probe_manifest,
                )
                if compact_tree is not None:
                    controlled_tree = compact_tree
            ai_settings = get_ai_settings()
            self.store.record_event(
                diagnosis_id,
                "controlled_ai_tree_guard_result",
                {
                    "task_id": task.id,
                    "ai_source": ai_settings.source,
                    "ai_enabled": ai_settings.enabled,
                    "rca_enabled": is_feature_enabled("rca"),
                    "has_api_key": bool(ai_settings.api_key),
                    "generated_by": sorted({
                        layer.generated_by
                        for layer in (controlled_tree.layers if controlled_tree else [])
                    }),
                    "layer_count": len(controlled_tree.layers) if controlled_tree else 0,
                },
            )
            analysis_result = analysis_result.model_copy(update={"controlled_ai_tree": controlled_tree})
            if analysis_result.controlled_ai_tree is not None:
                controlled_ai_trees.append(analysis_result.controlled_ai_tree.model_dump(mode="json"))
            for tree_node in analysis_result.ai_tree:
                for request_id in tree_node.next_evidence_requests:
                    if request_id not in followup_requests:
                        followup_requests.append(request_id)
            if analysis_result.controlled_ai_tree is not None:
                for edge in analysis_result.controlled_ai_tree.probe_edges:
                    for request_id in edge.probe_requests:
                        if request_id not in followup_requests:
                            followup_requests.append(request_id)
            calibrated = calibrate(candidates, evidence, self.repo.get_feedback_priors())
            for candidate in calibrated:
                if candidate.candidate_id == "insufficient_data":
                    continue
                all_candidates.append({
                    "candidate_id": candidate.candidate_id,
                    "description": candidate.description,
                    "evidence_refs": evidence_ids,
                    "missing_evidence": candidate.missing_evidence,
                    "score_components": {
                        "rule_match": _quality(candidate.rule_score),
                        "evidence_quality": _quality(candidate.evidence_quality),
                        "baseline_support": _quality(candidate.baseline_support),
                        "source_independence": _quality(candidate.cross_collector_agreement),
                    },
                    "sort_score": candidate.final_confidence,
                })

        if not all_candidates and not task_observations:
            return False
        all_candidates.sort(key=lambda item: item["sort_score"], reverse=True)
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in all_candidates:
            if candidate["candidate_id"] in seen:
                continue
            seen.add(candidate["candidate_id"])
            candidate.pop("sort_score", None)
            candidate["rank"] = len(deduped) + 1
            candidate["confidence_level"] = self._confidence_level(candidate)
            candidate["supporting_claims"] = [{
                "statement": candidate["description"],
                "evidence_refs": candidate["evidence_refs"],
                "strength": "medium" if len(candidate["evidence_refs"]) > 1 else "weak",
            }]
            candidate.update(_candidate_location_fields(candidate, self.store.get_session(diagnosis_id) or {}))
            deduped.append(candidate)
            if len(deduped) >= 3:
                break

        cluster_assessment = self._build_cluster_assessment(diagnosis_id, task_observations)
        cluster_assessment.update(_assessment_location_fields(cluster_assessment, deduped, self.store.get_session(diagnosis_id) or {}))
        sufficient_dependency = _has_sufficient_dependency_conclusion(
            cluster_assessment,
            deduped,
            task_observations,
        )
        for request_id in _assessment_followup_requests(
            cluster_assessment,
            self.store.get_session(diagnosis_id) or {},
        ):
            if sufficient_dependency and request_id in DEPENDENCY_EVIDENCE_GAPS:
                continue
            if request_id not in followup_requests:
                followup_requests.append(request_id)
        if sufficient_dependency and _is_database_dependency_assessment(cluster_assessment):
            followup_requests = [
                request_id for request_id in followup_requests
                if request_id not in FUNCTION_DEPTH_EVIDENCE_GAPS
            ]
        diagnostic_commands = self._build_reviewable_commands(
            diagnosis_id,
            task_observations,
            cluster_assessment,
        )
        followup_requests = _filter_pending_evidence_requests(
            diagnosis_id,
            followup_requests,
            self.store.list_probes(diagnosis_id),
            task_observations,
        )
        nonblocking_failed_depth = (
            sufficient_dependency
            and _is_database_dependency_assessment(cluster_assessment)
            and _failed_tasks_are_only_depth_followups(diagnosis_id, tasks, self.store.list_probes(diagnosis_id))
        )
        session_controlled_tree = _build_session_controlled_ai_tree(
            diagnosis_id=diagnosis_id,
            cluster_assessment=cluster_assessment,
            candidates=deduped,
            followup_requests=followup_requests,
            probes=self.store.list_probes(diagnosis_id),
            child_trees=controlled_ai_trees,
        )
        conclusion = {
            "version": len((self.store.get_session(diagnosis_id) or {}).get("conclusion_versions", [])) + 1,
            "generated_at": utcnow().isoformat(),
            "summary": cluster_assessment["summary"] or f"形成 {len(deduped)} 个有证据关联的根因候选；结论仍需结合反证和人工确认。",
            "confidence_level": cluster_assessment["confidence_level"] or (deduped[0]["confidence_level"] if deduped else "不可判断"),
            "cluster_assessment": cluster_assessment,
            "root_cause_candidates": deduped,
            "ruled_out": cluster_assessment["ruled_out"],
            "diagnostic_commands": diagnostic_commands,
            "recommendations": [{
                "action": "由人工依据证据确认根因后再执行变更；本诊断不会自动重启、迁移或修改配置。",
                "risk_level": "R3",
                "execution": "manual_confirmation_required",
            }],
            "limitations": sorted(set(missing + (["部分目标采集失败"] if failed_targets and not nonblocking_failed_depth else []))),
            "next_evidence_requests": followup_requests,
            "controlled_ai_tree": session_controlled_tree.model_dump(mode="json") if session_controlled_tree else (
                controlled_ai_trees[-1] if controlled_ai_trees else None
            ),
            "controlled_ai_trees": controlled_ai_trees,
            "coverage": {
                "task_count": len(tasks),
                "failed_targets": failed_targets,
                "nonblocking_failed_depth": nonblocking_failed_depth,
                "evidence_count": len(self.store.list_evidence(diagnosis_id)),
            },
        }
        self._append_conclusion(diagnosis_id, conclusion)
        self._update_hypotheses(diagnosis_id, deduped)
        if followup_requests and tasks:
            self._plan_followup_requests(diagnosis_id, followup_requests, tasks[-1])
        return True

    @staticmethod
    def _probe_evidence_gap(probe_id: str) -> str:
        return {
            "process_cpu_profile": "cpu_profile",
            "process_off_cpu_profile": "off_cpu_wait_profile",
            "process_trace_endpoint_profile": "trace_endpoint_profile",
            "process_baseline_window": "baseline_window_profile",
            "process_python_runtime_profile": "python_runtime_profile",
            "process_log_scan": "log_scan",
            "process_dependency_check": "dependency_check",
            "process_redis_check": "redis_check",
            "process_io_latency": "io_latency",
            "process_memory_map": "memory_map",
        }.get(probe_id, "")

    def _plan_followup_requests(self, diagnosis_id: str, request_ids: list[str], parent_task) -> int:
        """Map AI tree evidence requests to registered follow-up probe plans."""
        session = self.store.get_session(diagnosis_id)
        if session is None or session["status"] in TERMINAL_DIAGNOSIS_STATUSES:
            return 0
        target = self._target_for_task(diagnosis_id, parent_task)
        existing_gaps = {
            str((probe.get("parameters") or {}).get("evidence_gap") or "")
            for probe in self.store.list_probes(diagnosis_id)
        }
        policy = str((session.get("risk_budget") or {}).get("auto_execute_policy") or "safe_only")
        created = 0
        self._last_followup_scheduled = False
        for evidence_gap in request_ids:
            probe_id = evidence_gap_to_probe_id(evidence_gap)
            if not probe_id or evidence_gap in existing_gaps:
                continue
            definition = get_probe(probe_id)
            if definition is None:
                continue
            if definition.risk_level in {"R2", "R3"} and policy == "safe_only":
                requires_approval = True
            elif policy == "manual":
                requires_approval = True
            else:
                requires_approval = False
            key = f"{diagnosis_id}:followup:{evidence_gap}"
            step_id = f"step_{hashlib.sha256(key.encode()).hexdigest()[:14]}"
            duration = min(definition.default_duration_seconds, definition.max_duration_seconds)
            deferred = False
            if not requires_approval:
                budget_block = self._followup_budget_block(
                    diagnosis_id,
                    definition,
                    duration,
                )
                if budget_block:
                    if budget_block.startswith("并发探针预算已用尽"):
                        deferred = True
                    else:
                        self.store.record_event(
                            diagnosis_id,
                            "followup_probe_blocked",
                            {
                                "evidence_gap": evidence_gap,
                                "probe_id": probe_id,
                                "execution_policy": policy,
                                **self._budget_block_event_payload(
                                    session,
                                    phase="followup",
                                    requested_seconds=duration,
                                    reason=budget_block,
                                ),
                            },
                        )
                        continue
            collector_parameters = self._collector_probe_parameters(
                probe_id,
                session.get("target_scope", {}),
                target,
            )
            collector_invocation = _collector_invocation(
                step={"diagnosis_id": diagnosis_id, "step_id": step_id},
                definition=definition,
                target=target,
                collector_parameters=collector_parameters,
            )
            plan = ProbePlan(
                step_id=step_id,
                probe_id=probe_id,
                target=target,
                parameters={
                    "duration_sec": duration,
                    "sample_rate": definition.default_sample_rate,
                    "evidence_gap": evidence_gap,
                    "budget_phase": "followup",
                    "parent_task_id": parent_task.id,
                    "execution_policy": policy,
                    **collector_parameters,
                    "collector_invocation": collector_invocation,
                    "collector_fingerprint": collector_request_fingerprint(
                        collector_invocation,
                        {
                            "duration_sec": duration,
                            "sample_rate": definition.default_sample_rate,
                            "evidence_gap": evidence_gap,
                            "budget_phase": "followup",
                            "parent_task_id": parent_task.id,
                            **collector_parameters,
                        },
                    ),
                },
                reason=f"AI 树请求补充证据: {evidence_gap}",
                risk_level=definition.risk_level,
                requires_approval=requires_approval,
            )
            self.store.add_probe({
                **plan.model_dump(mode="json"),
                "diagnosis_id": diagnosis_id,
                "status": "WAITING_APPROVAL" if requires_approval else "PLANNED",
            })
            existing_gaps.add(evidence_gap)
            created += 1
            if not requires_approval and not deferred:
                self._schedule_probe(plan.step_id)
        if created:
            self._last_followup_scheduled = True
            self._transition(
                diagnosis_id,
                DiagnosisStatus.WAITING_APPROVAL if policy == "manual" or any(
                    probe.get("status") == "WAITING_APPROVAL"
                    for probe in self.store.list_probes(diagnosis_id)
                ) else DiagnosisStatus.COLLECTING,
                "followup_evidence_requested",
                {"evidence_gaps": [item for item in request_ids if item in existing_gaps]},
            )
        return created

    def _followup_budget_block(self, diagnosis_id: str, definition, duration: int) -> str | None:
        """自动 follow-up 只能跳过人工审批，不能绕过资源预算。"""
        session = self.store.get_session(diagnosis_id)
        if session is None:
            return "诊断会话不存在"

        probes = self.store.list_probes(diagnosis_id)
        policy = str((session.get("risk_budget") or {}).get("auto_execute_policy") or "all_registered")
        if definition.risk_level == "R2" and policy != "all_registered":
            used_r2 = sum(
                1
                for probe in probes
                if probe["risk_level"] == "R2"
                and probe["status"] in {"APPROVED", "SCHEDULED", "RUNNING", "COMPLETED"}
            )
            limit = int(session["risk_budget"].get("max_medium_risk_probes", 0))
            if used_r2 >= limit:
                return f"R2 探针预算已用尽 ({used_r2}/{limit})"

        active_count = sum(
            1
            for probe in probes
            if (task := self.repo.tasks.get(probe.get("task_id")))
            and status_value(task.status) in ACTIVE_TASK_STATUSES
        )
        parallel_limit = int(session["resource_budget"].get("max_parallel_probes", 1))
        if active_count >= parallel_limit:
            return f"并发探针预算已用尽 ({active_count}/{parallel_limit})"

        used_duration = int(session["budget_used"].get("probe_duration_seconds", 0))
        duration_limit = self._duration_limit(session, "followup")
        if used_duration + duration > duration_limit:
            reserve = self._follow_up_reserve(session)
            return (
                "总采集时长预算已用尽 "
                f"(phase=followup, used={used_duration}s, requested={duration}s, "
                f"limit={duration_limit}s, reserved={reserve}s)"
            )
        return None

    def _find_reusable_probe_task(self, step: dict[str, Any]):
        parameters = step.get("parameters") if isinstance(step.get("parameters"), dict) else {}
        fingerprint = parameters.get("collector_fingerprint")
        if not fingerprint:
            return None
        for task in self.repo.tasks.values():
            options = (task.request_params or {}).get("options", {})
            if not isinstance(options, dict):
                continue
            if options.get("collector_fingerprint") != fingerprint:
                continue
            task_status = status_value(task.status)
            if task_status == "DONE":
                return task
            if task_status == "FAILED" and _is_stable_blocked_result(task.status_reason):
                return task
        return None

    @classmethod
    def _budget_block_event_payload(
        cls,
        session: dict[str, Any],
        *,
        phase: str,
        requested_seconds: int,
        reason: str,
    ) -> dict[str, Any]:
        used_seconds = int((session.get("budget_used") or {}).get("probe_duration_seconds", 0))
        limit_seconds = cls._duration_limit(session, phase)
        reserved_seconds = cls._follow_up_reserve(session)
        return {
            "budget_phase": phase,
            "used_seconds": used_seconds,
            "limit_seconds": limit_seconds,
            "reserved_seconds": reserved_seconds,
            "requested_seconds": requested_seconds,
            "reason": reason,
            "next_action": (
                "等待并发槽位释放后重试"
                if reason.startswith("并发探针预算已用尽")
                else "减少采集范围，或保留预算给下一轮 AI 树 follow-up"
            ),
        }

    @staticmethod
    def _follow_up_reserve(session: dict[str, Any]) -> int:
        budget = session.get("resource_budget") or {}
        total = int(budget.get("max_total_probe_cpu_seconds", 180))
        configured = int(budget.get("follow_up_reserve_seconds", 60))
        return max(0, min(configured, total))

    @classmethod
    def _duration_limit(cls, session: dict[str, Any], phase: str) -> int:
        budget = session.get("resource_budget") or {}
        total = int(budget.get("max_total_probe_cpu_seconds", 180))
        wall_limit = int(budget.get("max_duration_minutes", 10)) * 60
        total_limit = min(wall_limit, total)
        if phase == "initial":
            return max(0, total_limit - cls._follow_up_reserve(session))
        return total_limit

    def _build_task_observation(
        self,
        diagnosis_id: str,
        task,
        values: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        target = self._target_for_task(diagnosis_id, task)
        summary = _sys_summary(values.get("sys_metrics"))
        top_items = values.get("top_functions") if isinstance(values.get("top_functions"), list) else values.get("top_json") if isinstance(values.get("top_json"), list) else []
        top_name = str((top_items[0] or {}).get("name", "")) if top_items else ""
        top_percent = float((top_items[0] or {}).get("percent", 0.0) or 0.0) if top_items else 0.0
        pressure = _pressure_flags(summary, values)
        return {
            "task_id": task.id,
            "collector_type": task.collector_type,
            "target": target,
            "summary": summary,
            "top_function": {"name": top_name, "percent": top_percent},
            "specific_anchor": _specific_diagnostic_anchor(values, target, summary, top_items),
            "pressure": pressure,
            "dependency": _dependency_signal(values.get("dependency_check_json")),
            "redis": _redis_signal(values.get("redis_check_json")),
            "logs": _log_signal(values.get("log_window_json")),
            "confidence_inputs": values.get("confidence_inputs") if isinstance(values.get("confidence_inputs"), dict) else {},
            "evidence_refs": evidence_refs,
        }

    def _target_for_task(self, diagnosis_id: str, task) -> dict[str, Any]:
        session = self.store.get_session(diagnosis_id) or {}
        probes = self.store.list_probes(diagnosis_id)
        for probe in probes:
            if probe.get("task_id") == task.id:
                return dict(probe.get("target", {}))
        for item in session.get("target_scope", {}).get("instances", []):
            if item.get("agent_id") == task.agent_id and int(item.get("pid", 0) or 0) == int(task.target_pid):
                return dict(item)
        return {
            "service_id": "unknown",
            "instance_id": f"{task.agent_id}:{task.target_pid}",
            "host_id": "unknown",
            "agent_id": task.agent_id,
            "pid": task.target_pid,
        }

    def _build_cluster_assessment(
        self,
        diagnosis_id: str,
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        session = self.store.get_session(diagnosis_id) or {}
        scope = session.get("target_scope", {})
        target_service = scope.get("target_service")
        same_host_ids = set(scope.get("same_host_instance_ids", []))
        downstream_services = set(scope.get("downstream_service_ids", []))
        target_obs = [
            obs for obs in observations
            if obs["target"].get("service_id") == target_service
        ]
        same_host_obs = [
            obs for obs in observations
            if obs["target"].get("instance_id") in same_host_ids
        ]
        downstream_obs = [
            obs for obs in observations
            if obs["target"].get("service_id") in downstream_services
        ]
        all_refs = _unique_refs(obs for obs in observations)
        compared_by_instance: dict[tuple[Any, ...], dict[str, Any]] = {}
        for obs in observations:
            target = obs["target"]
            key = (
                target.get("instance_id"),
                target.get("agent_id"),
                target.get("pid"),
            )
            item = compared_by_instance.setdefault(key, {
                "instance_id": target.get("instance_id"),
                "service_id": target.get("service_id"),
                "host_id": target.get("host_id"),
                "agent_id": target.get("agent_id"),
                "pid": target.get("pid"),
                "pressure": {name: False for name in obs["pressure"]},
                "evidence_refs": [],
                "collector_types": [],
                "observation_count": 0,
            })
            for name, flagged in obs["pressure"].items():
                item["pressure"][name] = item["pressure"].get(name, False) or bool(flagged)
            for ref in obs["evidence_refs"]:
                if ref not in item["evidence_refs"]:
                    item["evidence_refs"].append(ref)
            if obs["collector_type"] not in item["collector_types"]:
                item["collector_types"].append(obs["collector_type"])
            item["observation_count"] += 1
        compared = list(compared_by_instance.values())

        classification = "insufficient_evidence"
        confidence = 0.3
        summary = "已有证据不足以区分自身代码、同宿主噪声邻居或下游依赖问题。"
        ruled_out: list[dict[str, Any]] = []
        alternative_hypotheses: list[dict[str, Any]] = []

        target_hot = any(_has_self_hotspot(obs) for obs in target_obs)
        target_pressure = any(_has_pressure(obs) for obs in target_obs)
        neighbor_pressure = any(_has_pressure(obs) for obs in same_host_obs)
        downstream_pressure = any(_has_pressure(obs) for obs in downstream_obs)
        downstream_dependency_failure = any(_has_dependency_failure(obs) or _has_redis_failure(obs) for obs in observations)
        target_anchor = _best_specific_anchor(target_obs)
        shared_iowait = (
            any(obs["pressure"].get("io_wait") for obs in target_obs)
            and any(obs["pressure"].get("io_wait") for obs in same_host_obs)
        )

        if downstream_dependency_failure:
            classification = "downstream_dependency"
            confidence = 0.82
            summary = _downstream_dependency_summary(
                target_anchor,
                observations,
                session,
            )
            ruled_out.append({
                "hypothesis": "self_code_regression",
                "reason": "当前强证据来自依赖可达性/Redis 专项检查，而不是目标进程代码热点。",
                "evidence_refs": all_refs,
            })
        elif shared_iowait:
            classification = "host_resource_contention"
            confidence = 0.7
            summary = "目标实例和同宿主实例同时表现出 I/O 等待，倾向于宿主机或共享块设备争抢。"
        elif same_host_obs and neighbor_pressure and not target_hot:
            classification = "same_host_noisy_neighbor"
            confidence = 0.78 if target_obs else 0.62
            summary = "同宿主其他实例存在明显资源压力，当前更像被噪声邻居或宿主机资源争抢拖累。"
            ruled_out.append({
                "hypothesis": "self_code_regression",
                "reason": "目标实例缺少高占比代码热点，且同宿主实例压力更明显。",
                "evidence_refs": all_refs,
            })
        elif downstream_obs and downstream_pressure and not target_hot:
            classification = "downstream_dependency"
            confidence = 0.72
            summary = "下游依赖实例出现资源压力，根因节点可能不在最先告警的服务上。"
            ruled_out.append({
                "hypothesis": "same_host_noisy_neighbor",
                "reason": "当前证据更集中在一跳下游，而不是同宿主横向干扰。",
                "evidence_refs": all_refs,
            })
        elif target_hot or target_pressure:
            classification = "self_code_or_process_pressure"
            confidence = 0.68 if target_hot else 0.58
            summary = _self_pressure_summary(target_anchor)
            if same_host_obs:
                ruled_out.append({
                    "hypothesis": "same_host_noisy_neighbor",
                    "reason": "同宿主观测未显示更强资源压力。",
                    "evidence_refs": all_refs,
                })

        alternative_hypotheses = _cluster_alternative_hypotheses(
            classification=classification,
            all_refs=all_refs,
            target_obs=target_obs,
            same_host_obs=same_host_obs,
            downstream_obs=downstream_obs,
            target_hot=target_hot,
            target_pressure=target_pressure,
            neighbor_pressure=neighbor_pressure,
            downstream_pressure=downstream_pressure,
            downstream_dependency_failure=downstream_dependency_failure,
            shared_iowait=shared_iowait,
            scope=scope,
        )

        return {
            "classification": classification,
            "confidence": round(confidence, 2),
            "confidence_level": _confidence_label(confidence),
            "summary": summary,
            "evidence_refs": all_refs,
            "compared_targets": compared,
            "supported_level": target_anchor.get("supported_level") if target_anchor else None,
            "primary_anchor": target_anchor,
            "ruled_out": ruled_out,
            "alternative_hypotheses": alternative_hypotheses,
        }

    def _build_reviewable_commands(
        self,
        diagnosis_id: str,
        observations: list[dict[str, Any]],
        assessment: dict[str, Any],
    ) -> list[dict[str, Any]]:
        commands = [
            _command_suggestion(
                "cmd_review_session",
                "回看诊断证据链",
                f"curl -s http://localhost:8191/api/v1/diagnoses/{diagnosis_id}",
                "只读查询当前诊断会话，核对 cluster_assessment、evidence_refs 和探针状态。",
                "R0",
                assessment.get("evidence_refs", []),
                confidence=0.95,
            )
        ]
        target_obs = observations[0] if observations else None
        if target_obs:
            agent_id = target_obs["target"].get("agent_id")
            pid = target_obs["target"].get("pid")
            commands.append(_command_suggestion(
                "cmd_low_risk_metrics",
                "补充低风险系统指标",
                f"micro-drop collect --agent {agent_id} --pid {pid} --collector sys_metrics --duration 15 --sample-rate 11 --watch",
                "低开销采集 CPU、内存、线程、FD、网络与 I/O 等待趋势，适合复核当前判断。",
                "R1",
                target_obs.get("evidence_refs", []),
                confidence=0.82,
            ))
            if assessment.get("classification") in {
                "self_code_or_process_pressure",
                "insufficient_evidence",
            }:
                commands.append(_command_suggestion(
                    "cmd_cpu_profile",
                    "申请一次 CPU Profile",
                    f"micro-drop collect --agent {agent_id} --pid {pid} --collector perf_cpu --duration 15 --sample-rate 49 --watch",
                    "中风险深度采样，可能带来额外开销；必须由人确认窗口和目标后再执行。",
                    "R2",
                    target_obs.get("evidence_refs", []),
                    requires_approval=True,
                    confidence=0.72,
                ))
            if assessment.get("classification") in {
                "same_host_noisy_neighbor",
                "host_resource_contention",
                "insufficient_evidence",
            }:
                commands.append(_command_suggestion(
                    "cmd_io_latency",
                    "申请一次 I/O 延迟探针",
                    f"micro-drop collect --agent {agent_id} --pid {pid} --collector ebpf_io --duration 15 --sample-rate 11 --watch",
                    "中风险 eBPF 探针，用于确认块设备延迟和宿主机级 I/O 争抢；需要人工审批。",
                    "R2",
                    assessment.get("evidence_refs", []),
                    requires_approval=True,
                    confidence=0.68,
                ))
        return commands

    def _append_scope_help_conclusion(
        self,
        diagnosis_id: str,
        query: str,
        ambiguities: list[str],
    ) -> None:
        """没有可靠拓扑时，只给可审核排查命令，不假装已经诊断。"""
        conclusion = {
            "version": 1,
            "generated_at": utcnow().isoformat(),
            "summary": "当前缺少服务实例到 Agent/PID 的映射，无法安全扩散采集范围。",
            "confidence_level": "不可判断",
            "cluster_assessment": {
                "classification": "scope_unresolved",
                "confidence": 0.0,
                "confidence_level": "不可判断",
                "summary": "请先补充服务实例、宿主机、Agent 和 PID 映射。",
                "evidence_refs": [],
                "compared_targets": [],
                "ruled_out": [],
            },
            "root_cause_candidates": [],
            "ruled_out": [],
            "diagnostic_commands": [
                _command_suggestion(
                    "cmd_list_agents",
                    "列出可用 Agent",
                    "micro-drop status --agents",
                    "确认哪些 Agent 在线，以及它们是否具备 sys_metrics/perf_cpu/ebpf_io 等诊断能力。",
                    "R0",
                    [],
                    confidence=0.9,
                ),
                _command_suggestion(
                    "cmd_parse_intent",
                    "解析自然语言意图",
                    f"micro-drop parse {json.dumps(query, ensure_ascii=False)}",
                    "仅解析意图，不创建采集任务；适合人工核对服务名、采集器和安全参数。",
                    "R0",
                    [],
                    confidence=0.75,
                ),
            ],
            "recommendations": [{
                "action": "补充 context.instances 后重新创建诊断会话；AI 不会猜测 PID 或跨服务扩散采集。",
                "risk_level": "R0",
                "execution": "manual_confirmation_required",
            }],
            "limitations": ambiguities or ["service_instance_mapping"],
            "coverage": {"task_count": 0, "evidence_count": 0},
        }
        self._append_conclusion(diagnosis_id, conclusion)

    def _ensure_insufficient_conclusion(self, diagnosis_id: str, tasks: list[Any]) -> None:
        session = self.store.get_session(diagnosis_id) or {}
        if session.get("conclusion_versions"):
            return
        probes = self.store.list_probes(diagnosis_id)
        missing = []
        if not tasks:
            missing.append("没有可用的已完成采集任务")
        if any(probe["status"] == "UNAVAILABLE" for probe in probes):
            missing.append("目标 Agent 未注册所需采集能力或当前离线")
        if any(probe["status"] == "REJECTED" for probe in probes):
            missing.append("需要审批的深度探针被拒绝")
        stored_evidence = self.store.list_evidence(diagnosis_id)
        if tasks and not any(item["source_type"] == "derived_artifact" for item in stored_evidence):
            missing.append("任务缺少结构化分析产物")
        conclusion = {
            "version": 1,
            "generated_at": utcnow().isoformat(),
            "summary": "当前证据不足，不能可靠给出根因候选。",
            "confidence_level": "不可判断",
            "root_cause_candidates": [],
            "ruled_out": [],
            "recommendations": [],
            "limitations": missing or ["缺少能够区分候选假设的独立证据"],
            "coverage": {"task_count": len(tasks), "evidence_count": len(stored_evidence)},
        }
        self._append_conclusion(diagnosis_id, conclusion)

    def _append_conclusion(self, diagnosis_id: str, conclusion: dict[str, Any]) -> None:
        session = self.store.get_session(diagnosis_id)
        if session is None:
            return
        versions = list(session.get("conclusion_versions", []))
        fingerprint = hashlib.sha256(
            json.dumps(conclusion, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        conclusion["integrity_hash"] = f"sha256:{fingerprint}"
        versions.append(conclusion)
        self.store.update_session(diagnosis_id, conclusion_versions=versions)

    def _add_task_evidence(self, diagnosis_id: str, task) -> str:
        payload = {
            "task_id": task.id,
            "status": status_value(task.status),
            "status_reason": task.status_reason,
            "collector_type": task.collector_type,
            "agent_id": task.agent_id,
            "target_pid": task.target_pid,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        identity = hashlib.sha256(f"{diagnosis_id}:{task.id}:task".encode()).hexdigest()
        evidence_id = f"ev_{identity[:20]}"
        self.store.add_evidence({
            "evidence_id": evidence_id,
            "diagnosis_id": diagnosis_id,
            "source_type": "task_event",
            "source_system": "mini_drop",
            "target": {"agent_id": task.agent_id, "pid": task.target_pid},
            "event_time_range": {
                "start": _iso(task.started_at or task.created_at),
                "end": _iso(task.finished_at or utcnow()),
                "clock_skew_estimate_ms": None,
            },
            "query_or_probe": task.collector_type,
            "derived_artifact_ref": f"task:{task.id}",
            "derivation_version": PLANNER_VERSION,
            "observed_value": payload,
            "data_quality": {"completeness": "high" if status_value(task.status) == "DONE" else "low"},
            "integrity_hash": f"sha256:{digest}",
        })
        return evidence_id

    def _add_artifact_evidence(
        self,
        diagnosis_id: str,
        task,
        artifact_type: str,
        value: Any,
        artifact: dict[str, Any],
    ) -> str:
        serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
        digest = hashlib.sha256(serialized).hexdigest()
        identity = hashlib.sha256(
            f"{diagnosis_id}:{task.id}:{artifact_type}:{digest}".encode()
        ).hexdigest()
        evidence_id = f"ev_{identity[:20]}"
        self.store.add_evidence({
            "evidence_id": evidence_id,
            "diagnosis_id": diagnosis_id,
            "source_type": "derived_artifact",
            "source_system": "mini_drop_analyzer",
            "target": {"agent_id": task.agent_id, "pid": task.target_pid},
            "event_time_range": {
                "start": _iso(task.started_at or task.created_at),
                "end": _iso(task.finished_at or utcnow()),
                "sampling_period_seconds": task.duration_sec,
                "clock_skew_estimate_ms": None,
            },
            "query_or_probe": task.collector_type,
            "raw_artifact_ref": f"task:{task.id}:artifact:{artifact_type}",
            "derived_artifact_ref": artifact.get("object_key") or artifact.get("local_path"),
            "derivation_version": PLANNER_VERSION,
            "observed_value": _summarize_value(value),
            "data_quality": {"completeness": "medium", "size_bytes": len(serialized)},
            "integrity_hash": f"sha256:{digest}",
        })
        session = self.store.get_session(diagnosis_id)
        if session is not None:
            usage = dict(session.get("budget_used", {}))
            usage["artifact_size_mb"] = round(sum(
                int(item.get("data_quality", {}).get("size_bytes", 0))
                for item in self.store.list_evidence(diagnosis_id)
            ) / (1024 * 1024), 3)
            self.store.update_session(diagnosis_id, budget_used=usage)
        return evidence_id

    def _structured_artifacts(self, artifacts: list[dict[str, Any]]) -> list[tuple[str, Any, dict[str, Any]]]:
        results = []
        for artifact in artifacts:
            artifact_type = artifact.get("artifact_type", "")
            if artifact_type not in STRUCTURED_ARTIFACT_TYPES:
                continue
            value = self._read_artifact_json(artifact)
            if value is not None:
                results.append((artifact_type, value, artifact))
        return results

    def _read_artifact_json(self, artifact: dict[str, Any]) -> Any | None:
        metadata = artifact.get("metadata", {})
        if "data" in metadata and isinstance(metadata["data"], (dict, list)):
            return metadata["data"]
        try:
            local_path = artifact.get("local_path")
            if local_path:
                root = Path(os.getenv("MINI_DROP_ARTIFACT_ROOT", "/tmp/mini-drop")).resolve()
                path = Path(local_path).expanduser().resolve()
                # Agent 的 local_path 属于远端 Worker；Control 上不存在时必须继续
                # 回退 object_key，而不是因 stat() 抛 FileNotFoundError 提前退出。
                if (path == root or root in path.parents) and path.is_file():
                    if path.stat().st_size > 2 * 1024 * 1024:
                        return None
                    return json.loads(path.read_text(encoding="utf-8", errors="strict"))
            object_key = artifact.get("object_key")
            if object_key:
                raw = storage.read_object_bytes(artifact.get("bucket", "mini-drop"), object_key)
                if len(raw) <= 2 * 1024 * 1024:
                    return json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        return None

    def _read_artifact_text(self, artifact: dict[str, Any]) -> str | None:
        metadata = artifact.get("metadata", {})
        if "text" in metadata and isinstance(metadata["text"], str):
            return metadata["text"]
        try:
            local_path = artifact.get("local_path")
            if local_path:
                root = Path(os.getenv("MINI_DROP_ARTIFACT_ROOT", "/tmp/mini-drop")).resolve()
                path = Path(local_path).expanduser().resolve()
                if (path == root or root in path.parents) and path.is_file():
                    if path.stat().st_size > 2 * 1024 * 1024:
                        return None
                    return path.read_text(encoding="utf-8", errors="replace")
            object_key = artifact.get("object_key")
            if object_key:
                raw = storage.read_object_bytes(artifact.get("bucket", "mini-drop"), object_key)
                if len(raw) <= 2 * 1024 * 1024:
                    return raw.decode("utf-8", errors="replace")
        except Exception:
            return None
        return None

    def _build_topology_snapshot(self, request, intent) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        service_id = intent.target_service
        if service_id:
            nodes[f"service:{service_id}"] = {
                "id": service_id, "type": "Service", "environment": intent.environment,
            }
        for instance in request.context.instances:
            data = instance.model_dump(mode="json")
            nodes[f"service:{instance.service_id}"] = {
                "id": instance.service_id, "type": "Service", "environment": instance.environment,
            }
            nodes[f"instance:{instance.instance_id}"] = {
                "id": instance.instance_id, "type": "ServiceInstance", **data,
            }
            nodes[f"host:{instance.host_id}"] = {"id": instance.host_id, "type": "Host"}
            nodes[f"process:{instance.agent_id}:{instance.pid}"] = {
                "id": f"{instance.agent_id}:{instance.pid}", "type": "Process",
                "agent_id": instance.agent_id, "pid": instance.pid,
            }
            edges.extend([
                {"source": instance.instance_id, "target": instance.host_id, "type": "DEPLOYED_ON", "confidence": "high"},
                {"source": instance.instance_id, "target": f"{instance.agent_id}:{instance.pid}", "type": "RUNS_AS", "confidence": "high"},
            ])
        for dependency in request.context.dependencies:
            nodes.setdefault(
                f"service:{dependency.source_service}",
                {"id": dependency.source_service, "type": "Service", "environment": intent.environment},
            )
            nodes.setdefault(
                f"service:{dependency.target_service}",
                {"id": dependency.target_service, "type": "Service", "environment": intent.environment},
            )
            edges.append({
                "source": dependency.source_service,
                "target": dependency.target_service,
                "type": dependency.relation,
                "effective_from": _iso(dependency.effective_from),
                "effective_to": _iso(dependency.effective_to),
                "confidence": dependency.confidence,
                "discovery_source": dependency.source,
            })
        now = utcnow()
        return {
            "snapshot_id": f"topo_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}",
            "effective_at": intent.time_range.end,
            "generated_at": now,
            "nodes": list(nodes.values()),
            "edges": edges,
            "source_versions": {"request_context": "v1"},
            "confidence_summary": {
                "level": "high" if request.context.instances else "low",
                "source": "request_context",
                "historical_snapshot": True,
            },
        }

    def _build_target_scope(self, request, intent, budget: DiagnosisBudget) -> dict[str, Any]:
        all_instances = [item.model_dump(mode="json") for item in request.context.instances]
        target_instances = [item for item in all_instances if item["service_id"] == intent.target_service]
        host_ids = {item["host_id"] for item in target_instances}
        same_host = [item for item in all_instances if item["host_id"] in host_ids and item not in target_instances]
        downstream_services = {
            edge.target_service for edge in request.context.dependencies
            if edge.source_service == intent.target_service and edge.relation == "CALLS"
        }
        dependency_targets = [
            {
                "dependency_id": edge.target_service,
                "source_service": edge.source_service,
                "target_service": edge.target_service,
                "relation": edge.relation,
                "protocol": edge.protocol,
                "host": edge.host,
                "port": edge.port,
                "url": edge.url,
                "path": edge.path,
                "confidence": edge.confidence,
                "source": edge.source,
            }
            for edge in request.context.dependencies
            if edge.source_service == intent.target_service
        ]
        downstream = [item for item in all_instances if item["service_id"] in downstream_services]
        ordered = target_instances + same_host + downstream
        unique = []
        seen = set()
        for item in ordered:
            key = item["instance_id"]
            if key in seen:
                continue
            if len({entry["host_id"] for entry in unique} | {item["host_id"]}) > budget.max_hosts:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= budget.max_service_instances:
                break
        return {
            "target_service": intent.target_service,
            "environment": intent.environment,
            "instances": unique,
            "source_context": request.context.source_context.model_dump(mode="json") if request.context.source_context else {},
            "same_host_instance_ids": [item["instance_id"] for item in same_host],
            "downstream_service_ids": sorted(downstream_services),
            "dependency_targets": dependency_targets,
            "max_topology_hops": budget.max_topology_hops,
        }

    def _build_hypotheses(self, symptom: str, target_scope: dict[str, Any]) -> list[dict[str, Any]]:
        base = {
            "cpu_saturation": ["CPU_SATURATION", "SELF_CODE_REGRESSION", "SAME_HOST_NOISY_NEIGHBOR"],
            "latency_increase": ["SELF_CODE_REGRESSION", "DOWNSTREAM_LATENCY", "SAME_HOST_NOISY_NEIGHBOR"],
            "io_degradation": ["HOST_DISK_CONTENTION", "SAME_HOST_NOISY_NEIGHBOR", "DOWNSTREAM_LATENCY"],
            "memory_pressure": ["HOST_MEMORY_PRESSURE", "MEMORY_LEAK", "SAME_HOST_NOISY_NEIGHBOR"],
            "noisy_neighbor": ["SAME_HOST_NOISY_NEIGHBOR", "HOST_DISK_CONTENTION", "TRAFFIC_SURGE"],
            "runtime_contention": ["LOCK_CONTENTION", "SELF_CODE_REGRESSION", "CPU_SATURATION"],
        }.get(symptom, ["CPU_SATURATION", "DOWNSTREAM_LATENCY", "INSUFFICIENT_EVIDENCE"])
        targets = [item["instance_id"] for item in target_scope.get("instances", [])]
        return [{
            "hypothesis_id": f"hyp_{index + 1}_{kind.lower()}",
            "type": kind,
            "description": kind.replace("_", " ").title(),
            "affected_targets": targets,
            "status": "UNTESTED",
            "supporting_evidence_refs": [],
            "contradicting_evidence_refs": [],
            "missing_evidence_requirements": [],
            "score_components": {},
            "next_probe_candidates": choose_probe_ids(symptom),
        } for index, kind in enumerate(base)]

    def _update_hypotheses(self, diagnosis_id: str, candidates: list[dict[str, Any]]) -> None:
        session = self.store.get_session(diagnosis_id)
        if session is None:
            return
        graph = dict(session.get("hypothesis_graph", {}))
        hypotheses = list(graph.get("hypotheses", []))
        for hypothesis in hypotheses:
            matched = next((c for c in candidates if _candidate_matches_hypothesis(c["candidate_id"], hypothesis["type"])), None)
            if matched:
                hypothesis["status"] = "SUPPORTED"
                hypothesis["supporting_evidence_refs"] = matched["evidence_refs"]
                hypothesis["missing_evidence_requirements"] = matched["missing_evidence"]
                hypothesis["score_components"] = matched["score_components"]
        graph["hypotheses"] = hypotheses
        self.store.update_session(diagnosis_id, hypothesis_graph=graph)

    def _find_reusable_tasks(self, target_scope: dict[str, Any], start: datetime, end: datetime) -> list[str]:
        reuse_max_age = _reuse_max_age_seconds()
        if reuse_max_age == 0:
            return []
        targets = {(item["agent_id"], item["pid"]) for item in target_scope.get("instances", [])}
        now = utcnow()
        result = []
        for task in self.repo.tasks.values():
            if (task.agent_id, task.target_pid) not in targets:
                continue
            task_start = task.started_at or task.created_at
            task_end = task.finished_at or task_start
            if task_start.tzinfo is None:
                task_start = task_start.replace(tzinfo=timezone.utc)
            if task_end.tzinfo is None:
                task_end = task_end.replace(tzinfo=timezone.utc)
            if reuse_max_age is not None and (now - task_end).total_seconds() > reuse_max_age:
                continue
            if task_end >= start and task_start <= end and status_value(task.status) in TERMINAL_TASK_STATUSES:
                result.append(task.id)
        return sorted(result)

    def _transition(
        self,
        diagnosis_id: str,
        status: DiagnosisStatus,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        current = self.store.get_session(diagnosis_id)
        if current is None or current["status"] == status.value:
            return
        allowed = ALLOWED_DIAGNOSIS_TRANSITIONS.get(current["status"], set())
        if status.value not in allowed:
            raise ValueError(f"非法诊断状态迁移: {current['status']} -> {status.value}")
        self.store.transition(diagnosis_id, status.value, event_type, payload)
        BUS.publish(event_type, {"diagnosis_id": diagnosis_id, "status": status.value, **(payload or {})})

    @staticmethod
    def _budget_for_profile(profile: str) -> DiagnosisBudget:
        if profile == "development":
            return DiagnosisBudget(max_hosts=10, max_service_instances=20, max_parallel_probes=5, max_medium_risk_probes=5)
        if profile == "staging":
            return DiagnosisBudget(max_hosts=8, max_service_instances=15, max_parallel_probes=4, max_medium_risk_probes=2)
        return DiagnosisBudget()

    @classmethod
    def _effective_budget(cls, profile: str, requested: DiagnosisBudget | None) -> DiagnosisBudget:
        policy_cap = cls._budget_for_profile(profile)
        if requested is None:
            return policy_cap
        requested_values = requested.model_dump()
        cap_values = policy_cap.model_dump()
        return DiagnosisBudget(**{
            key: min(int(requested_values[key]), int(cap_values[key]))
            for key in cap_values
        })

    @staticmethod
    def _empty_budget_usage() -> dict[str, int]:
        return {
            "hosts": 0,
            "service_instances": 0,
            "probes": 0,
            "medium_risk_probes": 0,
            "probe_duration_seconds": 0,
            "initial_probe_duration_seconds": 0,
            "followup_probe_duration_seconds": 0,
            "model_calls": 0,
            "artifact_size_mb": 0,
        }

    @staticmethod
    def _confidence_level(candidate: dict[str, Any]) -> str:
        refs = candidate.get("evidence_refs", [])
        components = candidate.get("score_components", {})
        if (
            len(refs) >= 3
            and not candidate.get("missing_evidence", [])
            and components.get("baseline_support") == "high"
            and components.get("source_independence") == "high"
        ):
            return "高"
        if len(refs) >= 2:
            return "中"
        return "低"

    @staticmethod
    def _enforce_service_scope(service_id: str | None) -> None:
        allowed = {item.strip() for item in os.getenv("MINI_DROP_ALLOWED_SERVICES", "").split(",") if item.strip()}
        if allowed and service_id not in allowed:
            raise PermissionError(f"当前身份无权诊断服务 {service_id}")


def _quality(value: float) -> str:
    if value >= 0.75:
        return "high"
    if value >= 0.4:
        return "medium"
    return "low"


def _reuse_max_age_seconds() -> int | None:
    raw = os.getenv("MINI_DROP_DIAGNOSIS_REUSE_MAX_AGE_SECONDS")
    if raw is None or raw == "":
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _scope_probe_ids(symptom: str, target_scope: dict[str, Any]) -> list[str]:
    probe_ids = list(choose_probe_ids(symptom))
    if symptom == "runtime_contention" and any(_is_python_target(item) for item in target_scope.get("instances", [])):
        probe_ids = [
            "host_process_metrics",
            "process_off_cpu_profile",
            "process_python_runtime_profile",
            "process_trace_endpoint_profile",
        ]
    dependency_targets = [
        item for item in target_scope.get("dependency_targets", [])
        if isinstance(item, dict)
    ]
    if dependency_targets:
        _append_once(probe_ids, "process_dependency_check", after="host_process_metrics")
        _append_once(probe_ids, "process_log_scan", after="process_dependency_check")
        if any(_is_redis_dependency(item) for item in dependency_targets):
            _append_once(probe_ids, "process_redis_check", after="process_dependency_check")
    return probe_ids


def _append_once(items: list[str], value: str, *, after: str | None = None) -> None:
    if value in items:
        return
    if after and after in items:
        items.insert(items.index(after) + 1, value)
        return
    items.append(value)


def _is_python_target(instance: dict[str, Any]) -> bool:
    text = " ".join(
        str(instance.get(key) or "")
        for key in ("service_id", "instance_id", "container_id")
    ).lower()
    return "python" in text or "py-" in text


def _confidence_label(value: float) -> str:
    if value >= 0.75:
        return "高"
    if value >= 0.5:
        return "中"
    if value > 0:
        return "低"
    return "不可判断"


def _command_suggestion(
    command_id: str,
    title: str,
    command: str,
    comment: str,
    risk_level: str,
    evidence_refs: list[str],
    *,
    requires_approval: bool = False,
    confidence: float = 0.5,
) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "title": title,
        "command": command,
        "comment": comment,
        "risk_level": risk_level,
        "requires_approval": requires_approval,
        "auto_execute": False,
        "execution_policy": "human_review_required",
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "confidence": round(confidence, 2),
    }


def _sys_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("summary"), dict):
        return value["summary"]
    return {}


def _pressure_flags(summary: dict[str, Any], values: dict[str, Any]) -> dict[str, bool]:
    cpu_user = _num(summary.get("avg_cpu_user_pct"))
    cpu_sys = _num(summary.get("avg_cpu_sys_pct"))
    cpu_iowait = _num(summary.get("avg_cpu_iowait_pct"))
    load1m = _num(summary.get("load1m"))
    rss_mb = _num(summary.get("vmrss_mb"))
    rss_max = _num(summary.get("vmrss_mb_max"))
    fd_count = _num(summary.get("fd_count"))
    fd_max = _num(summary.get("fd_max"))
    threads = _num(summary.get("thread_count"))
    top_items = values.get("top_functions") if isinstance(values.get("top_functions"), list) else values.get("top_json") if isinstance(values.get("top_json"), list) else []
    top_percent = _num((top_items[0] or {}).get("percent")) if top_items else 0.0
    return {
        "cpu": cpu_user + cpu_sys >= 75 or top_percent >= 45,
        "io_wait": cpu_iowait >= 20 or _has_ebpf_latency(values.get("ebpf_metrics")),
        "memory": rss_mb >= 1024 or (rss_max > 0 and rss_mb / max(rss_max, 1.0) >= 0.9),
        "fd": fd_count >= 1000 or (fd_max > 0 and fd_count / max(fd_max, 1.0) >= 0.9),
        "thread": threads >= 512,
        "load": load1m >= 4,
    }


def _dependency_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"has_signal": False, "failed_count": 0}
    summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
    failed = summary.get("failed_dependencies")
    if isinstance(failed, list):
        failed_count = len(failed)
    else:
        checks = value.get("checks")
        failed_count = sum(1 for item in checks or [] if isinstance(item, dict) and item.get("success") is False)
    return {
        "has_signal": bool(value.get("checks")),
        "failed_count": failed_count,
        "failed_dependencies": failed if isinstance(failed, list) else [],
    }


def _redis_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"has_signal": False, "failed": False}
    connectivity = value.get("connectivity") if isinstance(value.get("connectivity"), dict) else {}
    latency = value.get("latency_summary") if isinstance(value.get("latency_summary"), dict) else {}
    slowlog = value.get("slowlog_summary") if isinstance(value.get("slowlog_summary"), dict) else {}
    failed = connectivity.get("ping_ok") is False or connectivity.get("exporter_up") is False
    return {
        "has_signal": bool(connectivity or latency or slowlog),
        "failed": failed,
        "max_latency_ms": _num(latency.get("max_latency_ms")),
        "slowlog_entry_count": int(_num(slowlog.get("entry_count"))),
        "error_type": connectivity.get("error_type") or "",
    }


def _log_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"has_signal": False, "error_cluster_count": 0}
    summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
    clusters = value.get("error_clusters")
    return {
        "has_signal": bool(clusters),
        "error_cluster_count": int(_num(summary.get("error_cluster_count") or len(clusters or []))),
    }


def _specific_diagnostic_anchor(
    values: dict[str, Any],
    target: dict[str, Any],
    summary: dict[str, Any],
    top_items: list[dict[str, Any]],
) -> dict[str, Any]:
    base = {
        "service_id": target.get("service_id"),
        "instance_id": target.get("instance_id"),
        "pid": target.get("pid"),
    }
    off_cpu = values.get("off_cpu_wait_json")
    if isinstance(off_cpu, dict):
        stacks = off_cpu.get("top_wait_stacks")
        if isinstance(stacks, list) and stacks:
            top = next((item for item in stacks if isinstance(item, dict)), {})
            if top:
                top_frame = str(
                    top.get("top_frame")
                    or (top.get("stack") or [""])[0]
                    or ""
                ).strip()
                symbolized = _is_symbolized_frame(top_frame)
                return {
                    **base,
                    "supported_level": "function" if symbolized else "process",
                    "anchor_type": (
                        "off_cpu_wait_top_frame"
                        if symbolized
                        else "off_cpu_wait_unsymbolized_address"
                    ),
                    "anchor": top_frame,
                    "wait_reason": top.get("wait_reason") or (off_cpu.get("summary") or {}).get("top_wait_reason"),
                    "samples": int(_num(top.get("samples"))),
                    "percent": _num(top.get("percent")),
                    "wait_ms": _num(top.get("wait_ms")),
                    "evidence_ref": top.get("evidence_ref") or "off_cpu_wait.top_wait_stacks[0]",
                    "blocked_upgrade_reason": (
                        "缺少锁持有者线程、业务调用栈或源码符号映射，不能直接升级到代码行。"
                        if symbolized
                        else "当前等待栈顶部仍是未符号化地址，需补 debuginfo/符号映射后才能升级到函数。"
                    ),
                }

    call_paths = values.get("call_path_hotspots")
    if isinstance(call_paths, list) and call_paths:
        top = next((item for item in call_paths if isinstance(item, dict)), {})
        call_path = top.get("call_path") if isinstance(top.get("call_path"), list) else []
        if top and call_path:
            return {
                **base,
                "supported_level": "call_path",
                "anchor_type": "call_path_hotspot",
                "anchor": " -> ".join(str(item) for item in call_path),
                "function": top.get("function"),
                "samples": int(_num(top.get("samples"))),
                "percent": _num(top.get("percent")),
                "evidence_ref": top.get("evidence_ref") or "structured_evidence.call_path_hotspots[0]",
                "blocked_upgrade_reason": "缺少 line profiler 或源码映射，不能直接升级到具体代码行。",
            }

    if top_items:
        top = top_items[0] or {}
        name = str(top.get("name") or top.get("function") or top.get("symbol") or "").strip()
        if name:
            return {
                **base,
                "supported_level": "function",
                "anchor_type": "top_function",
                "anchor": name,
                "samples": int(_num(top.get("samples"))),
                "percent": _num(top.get("percent")),
                "evidence_ref": top.get("evidence_ref") or "top_functions[0]",
                "blocked_upgrade_reason": "缺少源码符号映射或行级采样证据，不能直接升级到代码行。",
            }

    pressure_names = [
        name for name, value in _pressure_flags(summary, values).items()
        if value
    ]
    return {
        **base,
        "supported_level": "process",
        "anchor_type": "process_pressure",
        "anchor": ",".join(pressure_names) or "process_metrics",
        "thread_count": int(_num(summary.get("thread_count"))),
        "ctx_nonvoluntary_rate": _num(summary.get("ctx_nonvoluntary_rate")),
        "avg_cpu_user_pct": _num(summary.get("avg_cpu_user_pct")),
        "avg_cpu_sys_pct": _num(summary.get("avg_cpu_sys_pct")),
        "evidence_ref": "sys_metrics.summary",
        "blocked_upgrade_reason": "缺少 off-CPU 等待栈、CPU profile 或 trace 回连，当前不能判断具体函数。",
    }


def _best_specific_anchor(observations: list[dict[str, Any]]) -> dict[str, Any]:
    order = {"line": 0, "call_path": 1, "function": 2, "syscall": 3, "thread": 4, "process": 5, "resource": 6}
    anchors = [
        obs.get("specific_anchor")
        for obs in observations
        if isinstance(obs.get("specific_anchor"), dict)
    ]
    if not anchors:
        return {}
    return sorted(
        anchors,
        key=lambda item: (
            order.get(str(item.get("supported_level") or "resource"), 99),
            -_num(item.get("percent")),
            -_num(item.get("samples")),
        ),
    )[0]


def _self_pressure_summary(anchor: dict[str, Any]) -> str:
    if not anchor:
        return "当前不能给出可操作结论：缺少函数、等待点、调用路径或进程指标锚点，需要先补结构化采集证据。"
    instance = anchor.get("instance_id") or anchor.get("service_id") or "目标实例"
    pid = f"(pid={anchor.get('pid')})" if anchor.get("pid") else ""
    level = anchor.get("supported_level") or "process"
    anchor_name = anchor.get("anchor") or anchor.get("anchor_type") or "unknown"
    reason = anchor.get("blocked_upgrade_reason") or "证据不足，不能继续下钻。"
    if anchor.get("anchor_type") == "off_cpu_wait_top_frame":
        samples = int(_num(anchor.get("samples")))
        wait_reason = anchor.get("wait_reason") or "unknown"
        percent = _num(anchor.get("percent"))
        percent_text = f"，占比 {percent:.1f}%" if percent > 0 else ""
        return (
            f"这次不要停在泛化的进程压力判断：same-window off-CPU 证据已把问题收敛到 "
            f"{instance}{pid} 的等待点 {anchor_name}，wait_reason={wait_reason}，"
            f"samples={samples}{percent_text}。当前更像线程同步/锁等待方向；结论先停在 {level} 层，"
            f"{reason}"
        )
    if anchor.get("anchor_type") == "off_cpu_wait_unsymbolized_address":
        samples = int(_num(anchor.get("samples")))
        wait_reason = anchor.get("wait_reason") or "unknown"
        percent = _num(anchor.get("percent"))
        percent_text = f"，占比 {percent:.1f}%" if percent > 0 else ""
        return (
            f"same-window off-CPU 证据已把问题收敛到 {instance}{pid} 的未符号化等待地址 "
            f"{anchor_name}，wait_reason={wait_reason}，samples={samples}{percent_text}。"
            f"这证明存在具体等待点，但当前不能把地址冒充函数；{reason}"
        )
    if anchor.get("anchor_type") == "call_path_hotspot":
        function = anchor.get("function")
        function_text = f"，top_function={function}" if function else ""
        percent = _num(anchor.get("percent"))
        percent_text = f"，占比 {percent:.1f}%" if percent > 0 else ""
        return (
            f"热点不是均匀分布在整个服务上，而是集中在 {instance}{pid} 的调用路径 {anchor_name}"
            f"{function_text}{percent_text}。当前更像特定请求路径内的问题；结论先停在 {level} 层，{reason}"
        )
    if anchor.get("anchor_type") == "top_function":
        percent = _num(anchor.get("percent"))
        samples = int(_num(anchor.get("samples")))
        metrics = []
        if percent > 0:
            metrics.append(f"percent={percent:.1f}%")
        if samples > 0:
            metrics.append(f"samples={samples}")
        metric_text = f"，{', '.join(metrics)}" if metrics else ""
        return (
            f"当前证据已定位到 {instance}{pid} 的函数热点 {anchor_name}{metric_text}。"
            f"这更像自身代码/运行时热点，而不是泛查资源压力；结论先停在 {level} 层，{reason}"
        )
    pressure_bits = []
    if _num(anchor.get("avg_cpu_user_pct")) or _num(anchor.get("avg_cpu_sys_pct")):
        pressure_bits.append(
            f"cpu={_num(anchor.get('avg_cpu_user_pct')) + _num(anchor.get('avg_cpu_sys_pct')):.1f}%"
        )
    if _num(anchor.get("thread_count")):
        pressure_bits.append(f"thread_count={int(_num(anchor.get('thread_count')))}")
    if _num(anchor.get("ctx_nonvoluntary_rate")):
        pressure_bits.append(f"ctx_switch_rate={_num(anchor.get('ctx_nonvoluntary_rate')):.1f}/s")
    metric_text = f"，{', '.join(pressure_bits)}" if pressure_bits else ""
    return (
        f"当前只能保守停在 {level} 层，但不是空泛排查：{instance}{pid} 的 {anchor_name} 出现异常{metric_text}。"
        f"{reason}"
    )


def _downstream_dependency_summary(
    anchor: dict[str, Any],
    observations: list[dict[str, Any]],
    session: dict[str, Any],
) -> str:
    root = _dependency_root_entity(session, prefer_redis=True) or _dependency_root_entity(session) or "下游依赖"
    redis_obs = next((obs for obs in observations if _has_redis_failure(obs)), None)
    dependency_obs = next((obs for obs in observations if _has_dependency_failure(obs)), None)
    facts: list[str] = []
    if redis_obs:
        redis = redis_obs.get("redis") if isinstance(redis_obs.get("redis"), dict) else {}
        if redis.get("failed"):
            facts.append("Redis ping/exporter 可达性失败")
        if _num(redis.get("max_latency_ms")) >= 1000:
            facts.append(f"Redis 最大延迟 {_num(redis.get('max_latency_ms')):.0f}ms")
        if int(redis.get("slowlog_entry_count") or 0) > 0:
            facts.append(f"Redis slowlog {int(redis.get('slowlog_entry_count') or 0)} 条")
    if dependency_obs:
        dependency = dependency_obs.get("dependency") if isinstance(dependency_obs.get("dependency"), dict) else {}
        failed_count = int(dependency.get("failed_count") or 0)
        if failed_count:
            facts.append(f"依赖可达性失败 {failed_count} 项")
    facts_text = "；".join(facts) if facts else "依赖检查或 Redis 专项证据异常"

    anchor_text = ""
    if anchor:
        instance = anchor.get("instance_id") or anchor.get("service_id") or "目标实例"
        pid = f"(pid={anchor.get('pid')})" if anchor.get("pid") else ""
        reason = anchor.get("blocked_upgrade_reason") or ""
        if anchor.get("anchor_type") == "off_cpu_wait_unsymbolized_address":
            anchor_text = (
                f"同窗 off-CPU 还捕获到 {instance}{pid} 的等待地址 {anchor.get('anchor')}，"
                f"wait_reason={anchor.get('wait_reason') or 'unknown'}，samples={int(_num(anchor.get('samples')))}；"
                f"{reason}"
            )
        elif anchor.get("anchor_type") == "off_cpu_wait_top_frame":
            anchor_text = (
                f"同窗 off-CPU 等待点为 {instance}{pid} 的 {anchor.get('anchor')}，"
                f"wait_reason={anchor.get('wait_reason') or 'unknown'}，samples={int(_num(anchor.get('samples')))}。"
            )
    if anchor_text:
        return f"根因优先指向 Redis 下游依赖 {root}：{facts_text}。{anchor_text}"
    return f"根因优先指向下游依赖 {root}：{facts_text}。"


def _has_dependency_failure(observation: dict[str, Any]) -> bool:
    signal = observation.get("dependency") if isinstance(observation.get("dependency"), dict) else {}
    return int(signal.get("failed_count") or 0) > 0


def _has_redis_failure(observation: dict[str, Any]) -> bool:
    signal = observation.get("redis") if isinstance(observation.get("redis"), dict) else {}
    return bool(signal.get("failed")) or _num(signal.get("max_latency_ms")) >= 1000 or int(signal.get("slowlog_entry_count") or 0) > 0


def _has_ebpf_latency(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    summary = value.get("summary")
    if isinstance(summary, dict) and _num(summary.get("p95_us")) >= 10000:
        return True
    hist = value.get("io_latency_us")
    if not isinstance(hist, dict):
        return False
    for bucket, count in hist.items():
        if _num(count) <= 0:
            continue
        if any(token in str(bucket) for token in ("8192", "16384", "32768", "65536")):
            return True
    return False


def _has_self_hotspot(observation: dict[str, Any]) -> bool:
    top = observation.get("top_function", {})
    return bool(top.get("name")) and _num(top.get("percent")) >= 35


def _has_pressure(observation: dict[str, Any]) -> bool:
    pressure = observation.get("pressure", {})
    return any(bool(value) for value in pressure.values())


def _unique_refs(observations) -> list[str]:
    refs: list[str] = []
    for obs in observations:
        for ref in obs.get("evidence_refs", []):
            if ref not in refs:
                refs.append(ref)
    return refs


def _unique_strings(items) -> list[str]:
    values: list[str] = []
    for item in items or []:
        text = str(item or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_symbolized_frame(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text or text.startswith("0x"):
        return False
    return not any(token in text for token in ("unknown", "[unknown]", "??"))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _summarize_value(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"item_count": len(value), "top_items": _minimize(value[:5])}
    if isinstance(value, dict):
        return {"keys": sorted(value.keys())[:30], "summary": _minimize(value.get("summary", value))}
    return {"value": str(value)[:500]}


def _minimize(value: Any, depth: int = 0) -> Any:
    """限制进入证据摘要的数据量，并按字段名做基础脱敏。"""
    if depth >= 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:50]:
            key_text = str(key)[:128]
            if any(token in key_text.lower() for token in ("token", "secret", "password", "cookie", "authorization")):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _minimize(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_minimize(item, depth + 1) for item in value[:10]]
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:256]


def _normalize_structured_artifact_values(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    trace_profile = normalized.get("trace_endpoint_profile_json")
    if isinstance(trace_profile, dict):
        normalized.setdefault("top_json", trace_profile.get("top_functions") or [])
        normalized.setdefault("depth_evidence_json", {
            "stack_samples": trace_profile.get("call_path_hotspots") or [],
            "context": trace_profile.get("target") or {},
            "call_path_hotspots": trace_profile.get("call_path_hotspots") or [],
        })
    if "top_json" not in normalized and "continuous_top_json" in normalized:
        normalized["top_json"] = normalized["continuous_top_json"]
    if "top_json" not in normalized and isinstance(normalized.get("off_cpu_wait_json"), dict):
        stacks = normalized["off_cpu_wait_json"].get("top_wait_stacks")
        if isinstance(stacks, list):
            normalized["top_json"] = [
                {
                    "name": item.get("top_frame") or (item.get("stack") or [""])[0],
                    "samples": item.get("samples"),
                    "percent": item.get("percent"),
                    "wait_reason": item.get("wait_reason"),
                }
                for item in stacks
                if isinstance(item, dict) and (item.get("top_frame") or item.get("stack"))
            ]
    if "flamegraph_json" not in normalized and "continuous_flamegraph_json" in normalized:
        normalized["flamegraph_json"] = normalized["continuous_flamegraph_json"]
    summary = normalized.get("continuous_summary")
    if isinstance(summary, dict):
        depth = normalized.get("depth_evidence_json")
        if not isinstance(depth, dict):
            depth = {}
        depth.setdefault("baseline_summary", summary)
        normalized["depth_evidence_json"] = depth
    return normalized


def _tool_results_from_structured_values(values: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    dependency = values.get("dependency_check_json")
    if isinstance(dependency, dict):
        summary = dependency.get("summary") if isinstance(dependency.get("summary"), dict) else {}
        results.append({
            "tool_name": "dependency_check",
            "failed_dependency_count": len(summary.get("failed_dependencies") or []),
            "summary": summary,
        })
    redis = values.get("redis_check_json")
    if isinstance(redis, dict):
        connectivity = redis.get("connectivity") if isinstance(redis.get("connectivity"), dict) else {}
        latency = redis.get("latency_summary") if isinstance(redis.get("latency_summary"), dict) else {}
        slowlog = redis.get("slowlog_summary") if isinstance(redis.get("slowlog_summary"), dict) else {}
        results.append({
            "tool_name": "redis_check",
            "ping_ok": connectivity.get("ping_ok"),
            "exporter_up": connectivity.get("exporter_up"),
            "max_latency_ms": latency.get("max_latency_ms"),
            "slowlog_entry_count": slowlog.get("entry_count"),
        })
    log_window = values.get("log_window_json")
    if isinstance(log_window, dict):
        summary = log_window.get("summary") if isinstance(log_window.get("summary"), dict) else {}
        results.append({
            "tool_name": "log_scan",
            "error_cluster_count": summary.get("error_cluster_count"),
        })
    off_cpu = values.get("off_cpu_wait_json")
    if isinstance(off_cpu, dict):
        summary = off_cpu.get("summary") if isinstance(off_cpu.get("summary"), dict) else {}
        results.append({
            "tool_name": "off_cpu_wait_profile",
            "sample_count": summary.get("sample_count"),
            "blocked_thread_count": summary.get("blocked_thread_count"),
            "top_wait_reason": summary.get("top_wait_reason"),
            "collector_status": off_cpu.get("collector_status"),
            "parser_status": off_cpu.get("parser_status"),
        })
    return results


def _dependency_targets(target_scope: dict[str, Any]) -> list[dict[str, Any]]:
    dependencies = target_scope.get("dependency_targets") or []
    targets = []
    for item in dependencies:
        if not isinstance(item, dict):
            continue
        if not (item.get("url") or item.get("host")):
            continue
        targets.append({
            "dependency_id": item.get("dependency_id") or item.get("target_service") or item.get("host"),
            "protocol": item.get("protocol") or ("http" if item.get("url") else "tcp"),
            "host": item.get("host"),
            "port": item.get("port"),
            "url": item.get("url"),
            "path": item.get("path") or "/",
        })
    return targets


def _source_context_for_target(target: dict[str, Any], target_scope: dict[str, Any]) -> dict[str, Any]:
    scoped = target_scope.get("source_context") if isinstance(target_scope.get("source_context"), dict) else {}
    local = target.get("source_context") if isinstance(target.get("source_context"), dict) else {}
    merged = {**scoped, **local}
    return {key: value for key, value in merged.items() if value not in (None, "", [])}


def _is_stable_blocked_result(reason: str) -> bool:
    normalized = str(reason or "").lower()
    return any(
        token in normalized
        for token in (
            "perf_event_paranoid",
            "permission denied",
            "operation not permitted",
            "missing capability",
            "capability",
            "seccomp",
            "bpf",
            "perfmon",
        )
    )


def _is_redis_dependency(item: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(item.get(key) or "")
        for key in ("dependency_id", "target_service", "protocol", "host", "url")
    ).lower()
    return "redis" in haystack


def _redis_target(target_scope: dict[str, Any]) -> dict[str, Any]:
    for item in _dependency_targets(target_scope):
        protocol = str(item.get("protocol") or "").lower()
        if protocol == "redis" or _is_redis_dependency(item):
            host = item.get("host") or _host_from_url(str(item.get("url") or ""))
            port = int(item.get("port") or 6379)
            return {
                "dependency_id": item.get("dependency_id"),
                "protocol": "redis",
                "host": host,
                "port": port,
                "url": item.get("url") or f"redis://{host}:{port}",
            }
    return {}


def _collector_invocation(
    *,
    step: dict[str, Any],
    definition,
    target: dict[str, Any],
    collector_parameters: dict[str, Any],
) -> dict[str, Any]:
    target_config = collector_parameters.get("target_config")
    if not isinstance(target_config, dict):
        target_config = {}
    return build_collector_invocation(
        scope_source="diagnosis_target_scope",
        collector_family=definition.runner_task_kind,
        probe_id=definition.probe_id,
        diagnosis_id=step["diagnosis_id"],
        diagnosis_step_id=step["step_id"],
        target_config=target_config,
        target_context={
            "agent_id": target.get("agent_id"),
            "service_id": target.get("service_id"),
            "instance_id": target.get("instance_id"),
            "host_id": target.get("host_id"),
            "pid": target.get("pid"),
        },
    )


def _assessment_followup_requests(assessment: dict[str, Any], session: dict[str, Any]) -> list[str]:
    classification = assessment.get("classification")
    target_scope = session.get("target_scope", {}) if isinstance(session.get("target_scope"), dict) else {}
    requests: list[str] = []
    if classification == "downstream_dependency":
        requests.extend(["dependency_check", "log_scan"])
        if any(
            "redis" in " ".join(str(item.get(key) or "") for key in ("dependency_id", "protocol", "host", "url")).lower()
            for item in target_scope.get("dependency_targets", [])
            if isinstance(item, dict)
        ):
            requests.append("redis_check")
    if _needs_function_depth(assessment) and not _is_database_dependency_assessment(assessment):
        requests.extend(["cpu_profile", "off_cpu_wait_profile", "trace_endpoint_profile"])
    return requests


def _cluster_alternative_hypotheses(
    *,
    classification: str,
    all_refs: list[str],
    target_obs: list[dict[str, Any]],
    same_host_obs: list[dict[str, Any]],
    downstream_obs: list[dict[str, Any]],
    target_hot: bool,
    target_pressure: bool,
    neighbor_pressure: bool,
    downstream_pressure: bool,
    downstream_dependency_failure: bool,
    shared_iowait: bool,
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    alternatives: list[dict[str, Any]] = []

    def add(
        hypothesis: str,
        *,
        status: str,
        reason: str,
        supported_level: str = "resource",
        evidence_refs: list[str] | None = None,
        missing_evidence: list[str] | None = None,
    ) -> None:
        if hypothesis == classification:
            return
        alternatives.append({
            "hypothesis": hypothesis,
            "status": status,
            "reason": reason,
            "supported_level": supported_level,
            "evidence_refs": _unique_strings(evidence_refs or []),
            "missing_evidence": _unique_strings(missing_evidence or []),
        })

    if classification != "self_code_or_process_pressure":
        if target_obs and not target_hot and not target_pressure:
            add(
                "self_code_or_process_pressure",
                status="weakened",
                supported_level="process",
                reason="目标实例同窗证据未显示比其他分支更强的自身 CPU/等待/进程压力。",
                evidence_refs=all_refs,
            )
        else:
            add(
                "self_code_or_process_pressure",
                status="missing_evidence",
                supported_level="process",
                reason="当前证据不足以确认目标进程内部代码热点或线程等待是否为主因。",
                missing_evidence=["cpu_profile", "off_cpu_wait_profile", "trace_endpoint_profile"],
            )

    if classification != "same_host_noisy_neighbor":
        if same_host_obs and not neighbor_pressure:
            add(
                "same_host_noisy_neighbor",
                status="weakened",
                supported_level="host",
                reason="同宿主观测未显示比目标实例更强的资源压力，噪声邻居分支被压低。",
                evidence_refs=all_refs,
            )
        else:
            add(
                "same_host_noisy_neighbor",
                status="missing_evidence",
                supported_level="host",
                reason="当前没有足够的同宿主实例窗口，不能把噪声邻居分支强行淘汰。",
                missing_evidence=["same_host_sys_metrics", "same_host_baseline_window"],
            )

    if classification != "host_resource_contention":
        if target_obs and same_host_obs and not shared_iowait:
            add(
                "host_resource_contention",
                status="weakened",
                supported_level="host",
                reason="目标实例和同宿主实例没有同时出现共享 I/O wait 或宿主级资源争抢证据。",
                evidence_refs=all_refs,
            )
        else:
            add(
                "host_resource_contention",
                status="missing_evidence",
                supported_level="host",
                reason="当前宿主共享资源证据不足，不能确认或淘汰宿主争抢分支。",
                missing_evidence=["host_baseline_window", "same_host_sys_metrics"],
            )

    has_dependency_scope = bool(scope.get("dependency_targets") or scope.get("downstream_service_ids"))
    if classification != "downstream_dependency":
        if has_dependency_scope and downstream_obs and not downstream_pressure and not downstream_dependency_failure:
            add(
                "downstream_dependency",
                status="weakened",
                supported_level="service",
                reason="已覆盖的下游同窗证据未显示依赖不可达、Redis 异常或下游资源压力。",
                evidence_refs=all_refs,
            )
        else:
            add(
                "downstream_dependency",
                status="missing_evidence",
                supported_level="service",
                reason="当前没有足够的下游依赖可达性、日志或 trace 证据，不能强行淘汰下游分支。",
                missing_evidence=["dependency_check", "log_scan", "trace_endpoint_profile"],
            )

    return alternatives[:4]


def _build_session_controlled_ai_tree(
    *,
    diagnosis_id: str,
    cluster_assessment: dict[str, Any],
    candidates: list[dict[str, Any]],
    followup_requests: list[str],
    probes: list[dict[str, Any]],
    child_trees: list[dict[str, Any]],
) -> ControlledAITree | None:
    """Build the user-facing AI tree from the session-level conclusion."""
    if not cluster_assessment and not candidates:
        return None

    generated_by = "ai_guarded" if any(
        layer.get("generated_by") == "ai_guarded"
        for tree in child_trees
        for layer in tree.get("layers", [])
        if isinstance(layer, dict)
    ) else "analyzer_fallback"
    evidence_refs = _unique_strings(cluster_assessment.get("evidence_refs", []))
    final_level = _best_supported_level(
        cluster_assessment.get("supported_level"),
        cluster_assessment.get("max_supported_level"),
        "resource",
    )
    primary_nodes: list[AITreeCandidateNode] = []
    secondary_nodes: list[AITreeCandidateNode] = []
    rejected_nodes: list[AITreeCandidateNode] = []
    unknown_nodes: list[AITreeCandidateNode] = []

    for item in candidates:
        candidate_id = str(item.get("candidate_id") or f"candidate_{len(primary_nodes) + len(secondary_nodes) + 1}")
        role = "primary" if int(item.get("rank") or 999) == 1 else "secondary"
        node = AITreeCandidateNode(
            candidate_id=candidate_id,
            lineage_id=candidate_id,
            role=role,
            claim=str(item.get("description") or cluster_assessment.get("summary") or candidate_id),
            supported_level=str(item.get("max_supported_level") or final_level),
            confidence=_confidence_from_label(item.get("confidence_level"), cluster_assessment.get("confidence")),
            status="supported",
            evidence_refs=_unique_strings(item.get("evidence_refs", [])),
            self_challenge=AITreeSelfChallenge(
                why_this_claim=str(cluster_assessment.get("summary") or item.get("description") or ""),
                why_not_other_claims=_ruled_out_summary(cluster_assessment),
                supporting_evidence_refs=_unique_strings(item.get("evidence_refs", []) or evidence_refs),
                opposing_evidence_refs=[],
                missing_evidence=followup_requests[:3],
                what_would_change_my_mind="同窗 dependency/log/trace 证据显示该依赖可达且错误仍然集中在目标进程代码热点。",
            ),
        )
        if role == "primary":
            primary_nodes.append(node)
        else:
            secondary_nodes.append(node)

    if not primary_nodes and cluster_assessment.get("classification"):
        candidate_id = str(cluster_assessment.get("root_entity") or cluster_assessment.get("classification"))
        primary_nodes.append(AITreeCandidateNode(
            candidate_id=candidate_id,
            lineage_id=candidate_id,
            role="primary",
            claim=str(cluster_assessment.get("summary") or candidate_id),
            supported_level=final_level,
            confidence=_num(cluster_assessment.get("confidence")),
            status="supported",
            evidence_refs=evidence_refs,
            self_challenge=AITreeSelfChallenge(
                why_this_claim=str(cluster_assessment.get("summary") or ""),
                why_not_other_claims=_ruled_out_summary(cluster_assessment),
                supporting_evidence_refs=evidence_refs,
                missing_evidence=followup_requests[:3],
                what_would_change_my_mind="补充证据证明该依赖在异常同窗内健康，且另一个候选获得更强同窗证据。",
            ),
        ))

    for item in cluster_assessment.get("ruled_out", []) or []:
        if not isinstance(item, dict):
            continue
        hypothesis = str(item.get("hypothesis") or "ruled_out")
        rejected_nodes.append(AITreeCandidateNode(
            candidate_id=f"ruled_out_{hypothesis}",
            lineage_id=f"ruled_out_{hypothesis}",
            parent_candidate_ids=[node.candidate_id for node in primary_nodes],
            role="rejected",
            claim=str(item.get("reason") or hypothesis),
            supported_level=final_level,
            confidence=0.2,
            status="weakened",
            evidence_refs=_unique_strings(item.get("evidence_refs", [])),
            self_challenge=AITreeSelfChallenge(
                why_this_claim=str(item.get("reason") or ""),
                opposing_evidence_refs=_unique_strings(item.get("evidence_refs", [])),
                why_not_other_claims="该分支被当前主证据压低，不能作为主因输出。",
                what_would_change_my_mind="该分支出现比主因更强的同窗直接证据。",
            ),
        ))

    for item in cluster_assessment.get("alternative_hypotheses", []) or []:
        if not isinstance(item, dict):
            continue
        hypothesis = str(item.get("hypothesis") or "alternative")
        status = str(item.get("status") or "missing_evidence")
        role = "rejected" if status == "weakened" else "unknown"
        node = AITreeCandidateNode(
            candidate_id=f"{role}_{hypothesis}",
            lineage_id=f"{role}_{hypothesis}",
            parent_candidate_ids=[node.candidate_id for node in primary_nodes],
            role=role,
            claim=str(item.get("reason") or hypothesis),
            supported_level=str(item.get("supported_level") or final_level),
            confidence=0.18 if role == "rejected" else 0.25,
            status="weakened" if role == "rejected" else "missing_evidence",
            evidence_refs=_unique_strings(item.get("evidence_refs", [])),
            self_challenge=AITreeSelfChallenge(
                why_this_claim=str(item.get("reason") or ""),
                why_not_other_claims=(
                    "该分支没有当前主因更强的同窗证据。"
                    if role == "rejected"
                    else "该分支还缺少必要证据，不能作为主因也不能强行淘汰。"
                ),
                supporting_evidence_refs=[],
                opposing_evidence_refs=_unique_strings(item.get("evidence_refs", [])) if role == "rejected" else [],
                missing_evidence=_unique_strings(item.get("missing_evidence", [])),
                what_would_change_my_mind="该分支出现同窗直接证据并压过当前主因分支。",
            ),
        )
        if role == "rejected":
            rejected_nodes.append(node)
        else:
            unknown_nodes.append(node)

    coarse_id = f"coarse_{cluster_assessment.get('classification') or 'assessment'}"
    coarse_node = AITreeCandidateNode(
        candidate_id=coarse_id,
        lineage_id=coarse_id,
        role="primary",
        claim=str(cluster_assessment.get("summary") or cluster_assessment.get("classification") or "形成会话级粗候选。"),
        supported_level=str(cluster_assessment.get("supported_level") or "resource"),
        confidence=_num(cluster_assessment.get("confidence")),
        status="supported" if cluster_assessment.get("classification") else "unknown",
        evidence_refs=evidence_refs,
        self_challenge=AITreeSelfChallenge(
            why_this_claim=str(cluster_assessment.get("summary") or ""),
            why_not_other_claims=_ruled_out_summary(cluster_assessment),
            supporting_evidence_refs=evidence_refs,
            missing_evidence=[],
            what_would_change_my_mind="同窗依赖、日志或 trace 证据指向不同传播路径。",
        ),
    )
    primary_nodes = [
        node.model_copy(update={"parent_candidate_ids": [coarse_id]})
        for node in primary_nodes
    ]
    secondary_nodes = [
        node.model_copy(update={"parent_candidate_ids": [coarse_id]})
        for node in secondary_nodes
    ]
    rejected_nodes = [
        node.model_copy(update={"parent_candidate_ids": [coarse_id]})
        for node in rejected_nodes
    ]
    unknown_nodes = [
        node.model_copy(update={"parent_candidate_ids": [coarse_id]})
        for node in unknown_nodes
    ]

    layer0 = AITreeLayer(
        layer_id="session_layer_0_coarse_assessment",
        depth=0,
        generated_by=generated_by,
        summary="会话级 AI 树先形成粗粒度归因方向，再通过已完成探针证据收敛到具体主因。",
        primary_causes=[coarse_node],
    )
    layer1 = AITreeLayer(
        layer_id="session_layer_1_supported_causes",
        depth=1,
        generated_by=generated_by,
        summary="已完成的 dependency/log/Redis 等证据将粗候选收敛为当前会话级主因和反证分支。",
        primary_causes=primary_nodes[:1],
        secondary_causes=[*primary_nodes[1:], *secondary_nodes][:3],
        rejected_causes=rejected_nodes[:4],
        unknown_causes=unknown_nodes[:4],
    )

    layers = [layer0, layer1]
    completed_probe_requests = _completed_session_probe_requests(probes)
    edges: list[AITreeProbeEdge] = [
        AITreeProbeEdge(
            edge_id="session_edge_coarse_to_supported_causes",
            from_layer_id=layer0.layer_id,
            to_layer_id=layer1.layer_id,
            from_candidate_ids=[coarse_id],
            to_candidate_ids=[node.candidate_id for node in [*layer1.primary_causes, *layer1.secondary_causes, *layer1.rejected_causes]],
            probe_requests=completed_probe_requests or ["cluster_assessment"],
            probe_results=_probe_results_for_requests(completed_probe_requests, probes) if completed_probe_requests else [
                AITreeProbeResult(status="completed", evidence_refs=evidence_refs),
            ],
            status="completed",
            evidence_refs=evidence_refs,
            reuse_status="not_checked",
            effect="refined",
            reason="已完成的结构化证据把粗粒度候选收敛为当前会话级结论。",
        )
    ]
    blocked_upgrade_node = _blocked_upgrade_node(cluster_assessment, final_level, layer1.primary_causes)
    if followup_requests or blocked_upgrade_node is not None:
        boundary_nodes = [
            AITreeCandidateNode(
                candidate_id=f"gap_{request_id}",
                lineage_id=f"gap_{request_id}",
                parent_candidate_ids=[node.candidate_id for node in layer1.primary_causes],
                role="unknown",
                claim=f"如果需要继续下钻，需要补充 {request_id}。",
                supported_level=final_level,
                confidence=0.25,
                status="missing_evidence",
                self_challenge=AITreeSelfChallenge(
                    missing_evidence=[request_id],
                    what_would_change_my_mind=f"{request_id} 产生同窗结构化证据并改变主因排序。",
                ),
            )
            for request_id in followup_requests[:3]
        ]
        if blocked_upgrade_node is not None:
            boundary_nodes.append(blocked_upgrade_node)
        layer2 = AITreeLayer(
            layer_id="session_layer_2_remaining_evidence",
            depth=2,
            generated_by=generated_by,
            summary="剩余补证或升级阻断只作为继续下钻的边界，不覆盖当前已支持的会话级主因。",
            unknown_causes=boundary_nodes,
        )
        layers.append(layer2)
        edge_status = _edge_status_for_requests(followup_requests[:3], probes) if followup_requests else "blocked"
        edges.append(AITreeProbeEdge(
            edge_id="session_edge_conclusion_to_remaining_evidence",
            from_layer_id=layer1.layer_id,
            to_layer_id=layer2.layer_id,
            from_candidate_ids=[node.candidate_id for node in layer1.primary_causes],
            to_candidate_ids=[node.candidate_id for node in boundary_nodes],
            probe_requests=followup_requests[:3],
            probe_results=_probe_results_for_requests(followup_requests[:3], probes),
            status=edge_status,
            evidence_refs=evidence_refs,
            reuse_status="not_checked",
            effect="pending" if followup_requests else "no_change",
            reason=(
                "当前结论已形成；这些请求只用于进一步细化或反证。"
                if followup_requests
                else "当前证据已到可支持边界，缺少源码、锁持有者或行级证据，不能继续升级。"
            ),
        ))

    final_primary = [node.candidate_id for node in layer1.primary_causes]
    final_secondary = [node.candidate_id for node in layer1.secondary_causes]
    final_rejected = [node.candidate_id for node in layer1.rejected_causes]
    final_unknown = [
        node.candidate_id for layer in layers for node in layer.unknown_causes
    ]
    stop_reason = _session_tree_stop_reason(cluster_assessment, followup_requests, final_level)
    return ControlledAITree(
        tree_id=f"session_controlled_ai_tree_{hashlib.sha256(f'{diagnosis_id}:{final_primary}:{followup_requests}'.encode()).hexdigest()[:16]}",
        final_supported_level=final_level,
        stop_reason=stop_reason,
        budget=AITreeBudgetSnapshot(
            used_ai_rounds=len(layers),
            used_probe_requests=len(completed_probe_requests) + len(followup_requests),
        ),
        layers=layers,
        probe_edges=edges,
        final_primary_causes=final_primary,
        final_secondary_causes=final_secondary,
        final_rejected_causes=final_rejected,
        final_unknown_causes=final_unknown,
    )


def _completed_session_probe_requests(probes: list[dict[str, Any]]) -> list[str]:
    completed: list[str] = []
    for probe in probes:
        if probe.get("status") != "COMPLETED":
            continue
        gap = str((probe.get("parameters") or {}).get("evidence_gap") or "")
        if gap and gap not in completed:
            completed.append(gap)
    return completed


def _tree_generated_by(tree: ControlledAITree | None) -> set[str]:
    if tree is None:
        return set()
    return {
        layer.generated_by
        for layer in tree.layers
    }


def _blocked_upgrade_node(
    assessment: dict[str, Any],
    final_level: str,
    primary_nodes: list[AITreeCandidateNode],
) -> AITreeCandidateNode | None:
    anchor = assessment.get("primary_anchor")
    if not isinstance(anchor, dict):
        return None
    reason = str(anchor.get("blocked_upgrade_reason") or "").strip()
    if not reason:
        return None
    current_level = str(anchor.get("supported_level") or final_level)
    missing = ["source_context", "line_level_profile"]
    if "锁持有者" in reason:
        missing.append("lock_owner_thread")
    if "符号" in reason:
        missing.append("symbol_mapping")
    return AITreeCandidateNode(
        candidate_id="blocked_line_upgrade",
        lineage_id="blocked_line_upgrade",
        parent_candidate_ids=[node.candidate_id for node in primary_nodes],
        role="unknown",
        claim=f"当前只能停在 {current_level} 层：{reason}",
        supported_level=current_level,
        confidence=0.3,
        status="forbidden",
        evidence_refs=_unique_strings([anchor.get("evidence_ref")] + assessment.get("evidence_refs", [])[:2]),
        self_challenge=AITreeSelfChallenge(
            why_this_claim=reason,
            why_not_other_claims="这不是新的根因候选，而是当前主因分支继续升级到代码行的证据边界。",
            supporting_evidence_refs=_unique_strings([anchor.get("evidence_ref")] + assessment.get("evidence_refs", [])[:2]),
            missing_evidence=_unique_strings(missing),
            what_would_change_my_mind="提供源码上下文、符号映射、锁持有者线程或行级采样证据后，才允许从函数/调用路径升级到代码行。",
        ),
    )


def _best_supported_level(*levels: Any) -> str:
    order = {
        "line": 0,
        "call_path": 1,
        "function": 2,
        "endpoint": 3,
        "service": 4,
        "dependency": 4,
        "syscall": 5,
        "thread": 6,
        "process": 7,
        "host": 8,
        "resource": 9,
    }
    candidates = [str(level) for level in levels if level]
    if not candidates:
        return "resource"
    return min(candidates, key=lambda item: order.get(item, 99))


def _probe_results_for_requests(requests: list[str], probes: list[dict[str, Any]]) -> list[AITreeProbeResult]:
    results: list[AITreeProbeResult] = []
    for request_id in requests:
        matched = [
            probe for probe in probes
            if str((probe.get("parameters") or {}).get("evidence_gap") or "") == request_id
        ]
        if not matched:
            results.append(AITreeProbeResult(status="not_started", blocked_reason="尚未创建对应采集任务。"))
            continue
        latest = matched[-1]
        status = str(latest.get("status") or "unknown")
        if status == "COMPLETED":
            mapped = "completed"
        elif status in {"FAILED", "UNAVAILABLE", "REJECTED_POLICY"}:
            mapped = "failed" if status == "FAILED" else "blocked"
        elif status in {"SCHEDULED", "RUNNING", "WAITING_APPROVAL"}:
            mapped = "not_started"
        else:
            mapped = "unknown"
        results.append(AITreeProbeResult(
            status=mapped,
            blocked_reason=str(latest.get("result_summary") or latest.get("reason") or ""),
        ))
    return results


def _edge_status_for_requests(requests: list[str], probes: list[dict[str, Any]]) -> str:
    statuses = [item.status for item in _probe_results_for_requests(requests, probes)]
    if statuses and all(status == "completed" for status in statuses):
        return "completed"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "blocked" for status in statuses):
        return "blocked"
    if any(status == "not_started" for status in statuses):
        return "not_started"
    return "unknown"


def _session_tree_stop_reason(assessment: dict[str, Any], followup_requests: list[str], final_level: str) -> str:
    if _is_database_dependency_assessment(assessment) and not followup_requests:
        return f"下游数据库/Redis 依赖证据已支持到 {final_level} 层，继续代码级 profile 不是当前主因所必需。"
    if followup_requests:
        return f"当前主因已形成，若要继续细化需补充：{', '.join(followup_requests[:3])}。"
    return f"当前证据支持停在 {final_level} 层。"


def _ruled_out_summary(assessment: dict[str, Any]) -> str:
    reasons = [
        str(item.get("reason") or "")
        for item in assessment.get("ruled_out", []) or []
        if isinstance(item, dict) and item.get("reason")
    ]
    return "；".join(reasons[:2]) or "其他候选缺少更强同窗证据。"


def _confidence_from_label(label: Any, fallback: Any) -> float:
    mapping = {"高": 0.82, "中": 0.64, "低": 0.35, "不可判断": 0.0}
    return mapping.get(str(label or ""), _num(fallback))


def _is_database_dependency_assessment(assessment: dict[str, Any]) -> bool:
    if assessment.get("classification") != "downstream_dependency":
        return False
    domain = str(assessment.get("domain_type") or "").lower()
    root = str(assessment.get("root_entity") or "").lower()
    summary = str(assessment.get("summary") or "").lower()
    return domain == "database" or "redis" in root or "redis" in summary


def _failed_tasks_are_only_depth_followups(
    diagnosis_id: str,
    tasks: list[Any],
    probes: list[dict[str, Any]],
) -> bool:
    failed_task_ids = {
        task.id for task in tasks
        if status_value(task.status) == "FAILED"
    }
    if not failed_task_ids:
        return False
    gap_by_task = {
        probe.get("task_id"): str((probe.get("parameters") or {}).get("evidence_gap") or "")
        for probe in probes
        if probe.get("diagnosis_id") == diagnosis_id
    }
    return all(gap_by_task.get(task_id) in FUNCTION_DEPTH_EVIDENCE_GAPS for task_id in failed_task_ids)


def _filter_pending_evidence_requests(
    diagnosis_id: str,
    requests: list[str],
    probes: list[dict[str, Any]],
    task_observations: list[dict[str, Any]] | None = None,
) -> list[str]:
    """按证据质量去重，避免依赖检查完成后误删函数级深探请求。"""
    terminal_success = {"COMPLETED"}
    terminal_skip = {
        "UNAVAILABLE",
        "REJECTED",
        "REJECTED_POLICY",
        "INVALID",
        "SKIPPED",
    }
    status_by_gap: dict[str, list[str]] = {}
    for probe in probes:
        gap = str((probe.get("parameters") or {}).get("evidence_gap") or "")
        if gap:
            status_by_gap.setdefault(gap, []).append(str(probe.get("status") or ""))

    result: list[str] = []
    depth_completed = _completed_depth_evidence_gaps(task_observations or [])
    for request in requests:
        statuses = status_by_gap.get(request, [])
        if statuses and any(status in terminal_success for status in statuses):
            continue
        if request not in DEPENDENCY_EVIDENCE_GAPS and request in depth_completed:
            continue
        if statuses and all(status in terminal_skip for status in statuses):
            continue
        if request not in result:
            result.append(request)
    return result


def _completed_depth_evidence_gaps(observations: list[dict[str, Any]]) -> set[str]:
    """只有拿到对应的非空深度证据，才认为深度请求已经完成。"""
    completed: set[str] = set()
    for observation in observations:
        collector_type = str(observation.get("collector_type") or "")
        top_function = observation.get("top_function") if isinstance(observation.get("top_function"), dict) else {}
        confidence = observation.get("confidence_inputs") if isinstance(observation.get("confidence_inputs"), dict) else {}
        has_top = bool(str(top_function.get("name") or "").strip())
        has_wait = bool(confidence.get("has_wait_or_io_signal"))
        trace_level = str(confidence.get("trace_max_supported_level") or "")
        trace_status = str(confidence.get("trace_correlation_status") or "")

        if collector_type in {"perf_cpu", "pyspy", "baseline_window_profile", "trace_endpoint_profile"} and has_top:
            completed.add("cpu_profile")
        if collector_type == "off_cpu_wait_profile" and has_wait:
            completed.add("off_cpu_wait_profile")
        if collector_type == "trace_endpoint_profile" and trace_status in {"completed", "partial"} and trace_level in {
            "endpoint",
            "call_path",
        }:
            completed.add("trace_endpoint_profile")
        if collector_type == "baseline_window_profile" and has_top:
            completed.add("baseline_window_profile")
        if collector_type == "pyspy" and has_top:
            completed.add("python_runtime_profile")
    return completed


def _needs_function_depth(assessment: dict[str, Any]) -> bool:
    """服务或进程层结论成立时，仍允许继续请求函数/调用链证据。"""
    anchor = assessment.get("primary_anchor")
    anchor_level = anchor.get("supported_level") if isinstance(anchor, dict) else None
    supported_level = str(anchor_level or assessment.get("supported_level") or "")
    return supported_level not in {"line", "call_path", "function"}


def _has_sufficient_dependency_conclusion(
    assessment: dict[str, Any],
    candidates: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> bool:
    if assessment.get("classification") != "downstream_dependency":
        return False
    if float(assessment.get("confidence") or 0.0) < 0.75:
        return False
    has_dependency = any(_has_dependency_failure(obs) for obs in observations)
    has_redis = any(_has_redis_failure(obs) for obs in observations)
    return bool(candidates) and (has_dependency or has_redis)


def _assessment_location_fields(
    assessment: dict[str, Any],
    candidates: list[dict[str, Any]],
    session: dict[str, Any],
) -> dict[str, Any]:
    classification = str(assessment.get("classification") or "")
    if classification == "downstream_dependency":
        root_entity = _dependency_root_entity(session)
        if not root_entity and candidates:
            root_entity = str(candidates[0].get("root_entity") or "")
        return {
            "location_type": "downstream",
            "domain_type": "database" if _dependency_root_entity(session, prefer_redis=True) else "network",
            "root_entity": root_entity or "downstream_dependency",
            "max_supported_level": "service",
        }
    if classification == "same_host_noisy_neighbor":
        return {
            "location_type": "host",
            "domain_type": "resource_contention",
            "root_entity": _target_host(session) or "same_host",
            "max_supported_level": "host",
        }
    if classification == "host_resource_contention":
        return {
            "location_type": "host",
            "domain_type": "resource_contention",
            "root_entity": _target_host(session) or "host",
            "max_supported_level": "host",
        }
    if classification == "self_code_or_process_pressure":
        return {
            "location_type": "process",
            "domain_type": "process_pressure",
            "root_entity": _target_service(session) or "target_process",
            "max_supported_level": "process",
        }
    return {}


def _probe_budget_phase(probe: dict[str, Any]) -> str:
    parameters = probe.get("parameters") if isinstance(probe, dict) else {}
    if not isinstance(parameters, dict):
        return "initial"
    phase = str(parameters.get("budget_phase") or "").strip().lower()
    if phase in {"initial", "followup"}:
        return phase
    return "followup" if parameters.get("parent_task_id") else "initial"


def _candidate_location_fields(candidate: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    candidate_id = str(candidate.get("candidate_id") or "")
    if "redis" in candidate_id:
        return {
            "location_type": "downstream",
            "domain_type": "database",
            "classification": "downstream_dependency",
            "root_entity": _dependency_root_entity(session, prefer_redis=True) or "redis",
            "max_supported_level": "service",
        }
    if "downstream_dependency" in candidate_id or "dependency" in candidate_id:
        return {
            "location_type": "downstream",
            "domain_type": "network",
            "classification": "downstream_dependency",
            "root_entity": _dependency_root_entity(session) or "downstream_dependency",
            "max_supported_level": "service",
        }
    return {}


def _dependency_root_entity(session: dict[str, Any], *, prefer_redis: bool = False) -> str:
    scope = session.get("target_scope", {}) if isinstance(session.get("target_scope"), dict) else {}
    targets = [item for item in scope.get("dependency_targets", []) if isinstance(item, dict)]
    if prefer_redis:
        for item in targets:
            if _is_redis_dependency(item):
                return str(item.get("dependency_id") or item.get("target_service") or item.get("host") or "redis")
    if targets:
        item = targets[0]
        return str(item.get("dependency_id") or item.get("target_service") or item.get("host") or "")
    return ""


def _target_service(session: dict[str, Any]) -> str:
    scope = session.get("target_scope", {}) if isinstance(session.get("target_scope"), dict) else {}
    return str(scope.get("target_service") or scope.get("service_id") or "")


def _target_host(session: dict[str, Any]) -> str:
    scope = session.get("target_scope", {}) if isinstance(session.get("target_scope"), dict) else {}
    instances = scope.get("instances") if isinstance(scope.get("instances"), list) else []
    first = instances[0] if instances and isinstance(instances[0], dict) else {}
    return str(first.get("host_id") or "")


def _host_from_url(url: str) -> str:
    if "://" not in url:
        return ""
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _candidate_matches_hypothesis(candidate_id: str, hypothesis_type: str) -> bool:
    tokens = {
        "CPU_SATURATION": ("cpu", "hotspot"),
        "SELF_CODE_REGRESSION": ("hotspot", "recursive", "code"),
        "SAME_HOST_NOISY_NEIGHBOR": ("io_wait", "cross_", "cpu"),
        "HOST_DISK_CONTENTION": ("io_wait", "iowait", "disk"),
        "HOST_MEMORY_PRESSURE": ("memory", "swap", "oom"),
        "MEMORY_LEAK": ("memory", "fd_leak"),
        "DOWNSTREAM_LATENCY": ("network", "latency"),
        "TRAFFIC_SURGE": ("network", "load"),
    }.get(hypothesis_type, ())
    return any(token in candidate_id for token in tokens)
