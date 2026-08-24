"""可恢复、受预算约束的 AI 集群诊断编排器。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from server.app import storage
from server.app.ai_provider import get_ai_settings, is_feature_enabled
from server.app.common_utils import status_value
from server.app.diagnosis.intent import parse_diagnosis_intent
from server.app.diagnosis.canonical_candidate_state import reduce_candidate_state
from server.app.diagnosis.canonical_claim_lineage import apply_ai_claim_update, canonical_claim_fields
from server.app.diagnosis.canonical_probe_plan import (
    merge_probe_input_maps as merge_canonical_probe_inputs,
    normalize_probe_plan,
    union_probe_families,
)
from server.app.diagnosis.collector_invocation import (
    build_collector_invocation,
    collector_capability_fingerprint,
    collector_request_fingerprint,
)
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
from server.app.rca.llm_client import (
    _source_anchor_catalog,
    generate_session_candidate_review,
    generate_session_conclusion_review,
    generate_session_investigation_review,
)
from server.app.rca.controlled_tree import (
    classify_primitive,
    enforce_conclusion_eligibility,
    evidence_refs_with_anchors,
    evidence_refs_with_line_anchors,
    evidence_refs_with_runtime_anchors,
)
from server.app.rca.models import (
    AITreeBudgetSnapshot,
    AITreeCandidateNode,
    AITreeLayer,
    AITreeProbeEdge,
    AITreeProbeResult,
    AITreeSelfChallenge,
    CandidateCause,
    CausalExplanationStep,
    ControlledAITree,
    AnalyzerFactContext,
    CandidateHint,
    LocalizationBoundary,
    EvidenceInput,
    ProbeRequestSpec,
)
from server.app.diagnosis.evidence_structurer import structure_artifact_evidence
from server.app.diagnosis.attribution_engine import build_attribution_graph, qualify_attribution
from server.app.diagnosis.attribution_models import AttributionGraph
from server.app.diagnosis.session_conclusion import (
    apply_session_review,
    build_qualification_boundary,
    build_fallback_explanation,
    build_retained_conclusion,
    build_root_cause_clusters,
    build_scenario_retained_conclusion,
    build_session_qualification,
    apply_session_qualification,
    collect_candidate_generation_gate_failures,
    collect_ai_gate_failures,
    derive_localization_frontier_from_ai_tree,
    derive_root_cause_clusters_from_ai_tree,
)
from server.app.schemas import CreateTaskRequest, MAX_SAMPLE_RATE, MAX_TASK_DURATION_SEC, MIN_SAMPLE_RATE
from server.app.state_machine import Actor, TaskStatus


PLANNER_VERSION = "diagnosis-orchestrator-v1"
MAX_FOLLOWUP_ROUNDS = 5
MAX_FOLLOWUP_REQUESTS_PER_ROUND = 3
MAX_SOURCE_MECHANISM_ATTEMPTS = 3
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
    "runtime_control_event_json",
    "pyspy_status_json",
    "python_stack_samples_json",
    "python_heap_profile_json",
    "go_heap_profile_json",
    "source_snapshot_json",
    "source_mechanism_json",
    "python_heap_reference_json",
    "python_lock_wait_profile_json",
    "python_exception_profile_json",
    "python_queue_profile_json",
    "python_pool_profile_json",
    "python_retry_timeout_profile_json",
    "python_cache_profile_json",
    "python_input_profile_json",
}
DEPENDENCY_EVIDENCE_GAPS = {"dependency_check", "log_scan", "redis_check"}
FUNCTION_DEPTH_EVIDENCE_GAPS = {
    "baseline_window_profile",
    "cpu_profile",
    "off_cpu_wait_profile",
    "trace_endpoint_profile",
    "python_runtime_profile",
    "python_heap_profile",
    "go_heap_profile",
    "source_snapshot",
    "source_mechanism_query",
    "python_heap_reference",
    "python_lock_wait_profile",
    "python_exception_profile",
    "python_queue_profile",
    "python_pool_profile",
    "python_retry_timeout_profile",
    "python_cache_profile",
    "python_input_profile",
}
MEMORY_DEPTH_EVIDENCE_GAPS = {
    "python_heap_profile",
    "go_heap_profile",
    "python_runtime_profile",
    "source_snapshot",
    "source_mechanism_query",
    "python_heap_reference",
}
OBSERVATION_CANDIDATE_IDS = {
    "python_runtime_stack_hotspot",
    "python_userland_hotspot",
    "off_cpu_wait_hotspot",
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
        target_scope["evidence_cohort_id"] = diagnosis_id
        target_scope["diagnosis_mode"] = request.diagnosis_mode
        if request.evidence_package_id:
            target_scope["evidence_package_id"] = request.evidence_package_id
        if request.evidence_cohort_id:
            target_scope["source_evidence_cohort_id"] = request.evidence_cohort_id
        if request.source_incident_id:
            target_scope["source_incident_id"] = request.source_incident_id
        hypotheses = self._build_hypotheses(intent.symptom, target_scope)
        budget_usage = self._empty_budget_usage()
        budget_usage["model_calls"] = 0
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
        self.store.record_event(
            diagnosis_id,
            "scope_resolved",
            {
                "target_service": target_scope.get("target_service"),
                "instance_ids": [item.get("instance_id") for item in target_scope.get("instances", [])],
                "service_topology_hops": target_scope.get("service_topology_hops", {}),
                "max_topology_hops": target_scope.get("max_topology_hops", budget.max_topology_hops),
            },
        )

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
                latest = (self.store.get_session(diagnosis_id) or {}).get("conclusion_versions", [])
                self._transition(
                    diagnosis_id,
                    _diagnosis_terminal_status(latest[-1] if latest else {}),
                    "diagnosis_completed",
                )
                return self.store.get_detail(diagnosis_id) or {}

        if request.diagnosis_mode == "frozen_evidence":
            self.store.record_event(
                diagnosis_id,
                "frozen_evidence_no_probe",
                {
                    "evidence_package_id": request.evidence_package_id,
                    "evidence_cohort_id": request.evidence_cohort_id,
                    "source_incident_id": request.source_incident_id,
                    "reason": "冻结证据模式只消费已保存证据，不创建实时探针",
                },
            )
            self._ensure_insufficient_conclusion(diagnosis_id, [])
            self._transition(
                diagnosis_id,
                DiagnosisStatus.INSUFFICIENT_EVIDENCE,
                "diagnosis_completed",
            )
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
        lease_ttl = max(30, int(os.getenv("MINI_DROP_DIAGNOSIS_LEASE_TTL_SEC", "90")))
        if not self.store.acquire_lease(diagnosis_id, self.owner, ttl_seconds=lease_ttl):
            return self.store.get_detail(diagnosis_id)
        stop_renewal = threading.Event()

        def renew_lease() -> None:
            interval = max(5, lease_ttl // 3)
            while not stop_renewal.wait(interval):
                try:
                    if not self.store.renew_lease(diagnosis_id, self.owner, ttl_seconds=lease_ttl):
                        return
                except Exception:
                    return

        renewal_thread = threading.Thread(
            target=renew_lease,
            name=f"diagnosis-lease-{diagnosis_id[-8:]}",
            daemon=True,
        )
        renewal_thread.start()
        try:
            self._advance_locked(diagnosis_id)
        finally:
            stop_renewal.set()
            renewal_thread.join(timeout=5)
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
        frozen_evidence = _is_frozen_evidence_session(session)
        probes = self.store.list_probes(diagnosis_id)
        child_ids = list(session.get("child_task_ids", []))
        self._expire_stale_child_tasks(child_ids)

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
        if not frozen_evidence:
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
                open_probes = [
                    probe
                    for probe in self.store.list_probes(diagnosis_id)
                    if probe.get("status") in {"PLANNED", "SCHEDULED", "RUNNING", "APPROVED"}
                ]
                if open_probes:
                    latest_session = self.store.get_session(diagnosis_id) or session
                    if latest_session["status"] != DiagnosisStatus.COLLECTING.value:
                        self._transition(diagnosis_id, DiagnosisStatus.COLLECTING, "probe_started")
                    return
                for probe in self.store.list_probes(diagnosis_id):
                    if probe["status"] == "WAITING_APPROVAL":
                        self.store.update_probe(probe["step_id"], status="SKIPPED")
                latest = (self.store.get_session(diagnosis_id) or {}).get("conclusion_versions", [])
                latest_conclusion = latest[-1] if latest else {}
                nonblocking_failed_depth = bool(
                    (latest_conclusion.get("coverage") or {}).get("nonblocking_failed_depth")
                )
                final_status = _diagnosis_terminal_status(
                    latest_conclusion,
                    had_failures=any(status_value(task.status) == "FAILED" for task in terminal_tasks),
                    nonblocking_failures=nonblocking_failed_depth,
                )
                latest_session = self.store.get_session(diagnosis_id) or session
                if latest_session["status"] in TERMINAL_DIAGNOSIS_STATUSES:
                    return
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

    def _expire_stale_child_tasks(self, child_ids: list[str]) -> None:
        grace = max(0, int(os.getenv("MINI_DROP_DIAGNOSIS_TASK_STALE_GRACE_SEC", "120")))
        now = utcnow()
        for task_id in child_ids:
            task = self.repo.tasks.get(task_id)
            if task is None or status_value(task.status) not in ACTIVE_TASK_STATUSES:
                continue
            anchor = task.started_at or task.created_at
            if anchor is None:
                continue
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
            timeout_sec = max(1, int(task.duration_sec or 0)) + grace
            if (now - anchor).total_seconds() <= timeout_sec:
                continue
            self.repo.transition_task(
                task_id,
                TaskStatus.FAILED,
                f"诊断探针超过执行窗口未回传，已自动标记失败 (timeout={timeout_sec}s)",
                Actor.SERVER,
            )

    def _plan_and_schedule(
        self,
        diagnosis_id: str,
        symptom: str,
        target_scope: dict[str, Any],
        budget: DiagnosisBudget,
    ) -> None:
        session = self.store.get_session(diagnosis_id)
        if _is_frozen_evidence_session(session):
            return
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
        session = session or {}
        collection_context = _session_collection_context(session)
        auto_policy = str((session.get("risk_budget") or {}).get("auto_execute_policy") or "safe_only")
        for index, instance in enumerate(instances):
            instance_probe_ids = list(probe_ids)
            outgoing_dependencies = _dependency_targets(
                target_scope,
                source_service=str(instance.get("service_id") or ""),
            )
            if not outgoing_dependencies:
                instance_probe_ids = [
                    probe_id for probe_id in instance_probe_ids
                    if probe_id not in {"process_dependency_check", "process_redis_check"}
                ]
            if (
                instance.get("instance_id") in target_scope.get("downstream_instance_ids", [])
                and (instance.get("container_id") or instance.get("systemd_unit"))
            ):
                _append_once(
                    instance_probe_ids,
                    "process_runtime_control_history",
                    after="host_process_metrics",
                )
            for probe_id in instance_probe_ids:
                definition = get_probe(probe_id)
                if definition.risk_level == "R2" and auto_policy != "all_registered":
                    if index > 0 or r2_count >= budget.max_medium_risk_probes:
                        continue
                    r2_count += 1
                elif probe_id != "host_process_metrics" and not _is_runtime_log_scenario_probe(probe_id):
                    if auto_count >= budget.max_parallel_probes:
                        continue
                    auto_count += 1
                duration = _initial_probe_duration(probe_id, definition)
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
                    **collection_context,
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
                        **collection_context,
                        **collector_parameters,
                        "collector_invocation": collector_invocation,
                        "collector_capability_fingerprint": collector_capability_fingerprint(collector_invocation),
                        "collector_fingerprint": collector_request_fingerprint(
                            collector_invocation,
                            {**fingerprint_inputs, **collector_parameters},
                        ),
                    },
                    reason=f"用于区分 {', '.join(definition.may_help_distinguish[:3])} 等候选假设",
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
        if _is_frozen_evidence_session(session):
            self.store.update_probe(step_id, status="SKIPPED")
            self.store.record_event(
                step["diagnosis_id"],
                "frozen_evidence_probe_blocked",
                {"step_id": step_id, "probe_id": step.get("probe_id")},
            )
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
        if session is None or _is_frozen_evidence_session(session):
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
        step_parameters = step.get("parameters") or {}
        collector_parameters = self._collector_probe_parameters(
            definition.probe_id,
            target_scope,
            target,
            str(step.get("diagnosis_id") or ""),
        )
        if definition.probe_id == "process_source_mechanism_query":
            generated_query = step_parameters.get("ai_generated_query")
            total_timeout_sec = step_parameters.get("total_timeout_sec")
            if total_timeout_sec:
                collector_parameters["total_timeout_sec"] = total_timeout_sec
            if isinstance(generated_query, dict):
                collector_parameters["ai_generated_query"] = generated_query
        elif definition.probe_id == "process_python_heap_reference":
            candidate_id = str(step_parameters.get("candidate_id") or "").strip()
            object_type_hints = step_parameters.get("object_type_hints")
            if candidate_id:
                collector_parameters["candidate_id"] = candidate_id
            if isinstance(object_type_hints, list):
                collector_parameters["object_type_hints"] = object_type_hints
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
            "duration_sec": step_parameters.get("duration_sec"),
            "sample_rate": step_parameters.get("sample_rate"),
            "evidence_gap": step_parameters.get("evidence_gap"),
            "budget_phase": step_parameters.get("budget_phase"),
            "followup_round": step_parameters.get("followup_round"),
            "parent_task_id": step_parameters.get("parent_task_id"),
            **{
                key: step_parameters.get(key)
                for key in (
                    "evidence_cohort_id", "collection_mode", "window_start", "window_end", "timing_relation",
                    "candidate_id", "origin_parent_candidate_id",
                )
                if step_parameters.get(key) is not None
            },
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
        options["collector_capability_fingerprint"] = collector_capability_fingerprint(invocation)
        options["collector_fingerprint"] = collector_request_fingerprint(invocation, options)
        return options

    def _collector_probe_parameters(
        self,
        probe_id: str,
        target_scope: dict[str, Any],
        target: dict[str, Any],
        diagnosis_id: str = "",
    ) -> dict[str, Any]:
        if probe_id == "process_dependency_check":
            targets = _dependency_targets(target_scope, source_service=str(target.get("service_id") or ""))
            return {
                "target_config": {"dependency_targets": targets},
                "targets": targets,
            } if targets else {
                "target_config": {"dependency_targets": []},
                "missing_dependency_targets": True,
            }
        if probe_id == "process_redis_check":
            dependency_targets = _dependency_targets(target_scope, source_service=str(target.get("service_id") or ""))
            target_info = _redis_target(target_scope, source_service=str(target.get("service_id") or ""))
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
        if probe_id == "process_runtime_control_history":
            return {
                "target_config": {
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "host_id": target.get("host_id"),
                    "systemd_unit": target.get("systemd_unit"),
                    "container_id": target.get("container_id"),
                },
            }
        if probe_id == "process_python_heap_profile":
            source_context = _source_context_for_target(target, target_scope)
            result_path = str(
                target.get("memray_result_path")
                or target_scope.get("memray_result_path")
                or source_context.get("memray_result_path")
                or ""
            )
            stats_path = str(
                target.get("memray_stats_path")
                or target_scope.get("memray_stats_path")
                or source_context.get("memray_stats_path")
                or ""
            )
            leaks_path = str(
                target.get("memray_leaks_path")
                or target_scope.get("memray_leaks_path")
                or source_context.get("memray_leaks_path")
                or ""
            )
            return {
                "target_config": {
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "source_context": source_context,
                },
                **({"instrumented_result": result_path} if result_path else {}),
                **({"memray_stats_path": stats_path} if stats_path else {}),
                **({"memray_leaks_path": leaks_path} if leaks_path else {}),
            }
        if probe_id == "process_go_heap_profile":
            source_context = _source_context_for_target(target, target_scope)
            endpoint = str(
                target.get("pprof_endpoint")
                or target_scope.get("pprof_endpoint")
                or source_context.get("pprof_endpoint")
                or "/debug/pprof/heap"
            )
            port = target.get("pprof_port") or target_scope.get("pprof_port") or source_context.get("pprof_port")
            if port is None:
                port = target.get("port") or target_scope.get("port") or source_context.get("port") or 6060
            return {
                "target_config": {
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "source_context": source_context,
                },
                "profile_kind": "heap",
                "pprof_endpoint": endpoint,
                "port": int(port),
                "heap_mode": "gc=1",
            }
        if probe_id == "process_source_snapshot":
            source_context = _source_context_for_target(target, target_scope)
            source_paths = source_context.get("source_paths") if isinstance(source_context.get("source_paths"), list) else []
            host_source_paths = [
                str(path)
                for path in source_paths
                if str(path).startswith(("/home/", "/usr/src/", "/host/home/"))
            ]
            source_root = str(
                (host_source_paths[0] if host_source_paths else "")
                or (source_paths[0] if source_paths else "")
                or source_context.get("container_workdir")
                or ""
            )
            return {
                "target_config": {
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "source_context": source_context,
                },
                "source_root": source_root,
                "source_revision": str(source_context.get("repo_revision") or ""),
                "line_candidates": self._session_line_candidates(diagnosis_id) if diagnosis_id else [],
            }
        if probe_id == "process_source_mechanism_query":
            source_context = _source_context_for_target(target, target_scope)
            source_paths = source_context.get("source_paths") if isinstance(source_context.get("source_paths"), list) else []
            host_source_paths = [
                str(path)
                for path in source_paths
                if str(path).startswith(("/home/", "/usr/src/", "/host/home/"))
            ]
            source_root = str(
                (host_source_paths[0] if host_source_paths else "")
                or (source_paths[0] if source_paths else "")
                or source_context.get("container_workdir")
                or ""
            )
            sarif_path = str(source_context.get("codeql_sarif_path") or target_scope.get("codeql_sarif_path") or "")
            return {
                "target_config": {
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "source_context": source_context,
                },
                "source_root": source_root,
                "source_revision": str(source_context.get("repo_revision") or ""),
                "line_candidates": self._session_line_candidates(diagnosis_id) if diagnosis_id else [],
                **({"codeql_sarif_path": sarif_path} if sarif_path else {}),
            }
        if probe_id == "process_python_heap_reference":
            source_context = _source_context_for_target(target, target_scope)
            dump_path = str(source_context.get("pyheap_dump_path") or target_scope.get("pyheap_dump_path") or "")
            return {
                "target_config": {
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "container_id": target.get("container_id"),
                    "source_context": source_context,
                },
                "container_id": str(target.get("container_id") or ""),
                "object_type_hints": source_context.get("object_type_hints") or ["function", "code", "method", "Map", "Rule"],
                **({"pyheap_dump_path": dump_path} if dump_path else {}),
            }
        if probe_id in {
            "process_python_lock_wait_profile",
            "process_python_exception_profile",
            "process_python_queue_profile",
            "process_python_pool_profile",
            "process_python_retry_timeout_profile",
            "process_python_cache_profile",
            "process_python_input_profile",
        }:
            source_context = _source_context_for_target(target, target_scope)
            runtime_log_paths = _application_runtime_log_paths(target, target_scope, source_context)
            trace_paths = target.get("trace_paths") or target_scope.get("trace_paths") or source_context.get("trace_paths") or []
            offcpu_paths = target.get("offcpu_profile_paths") or target_scope.get("offcpu_profile_paths") or source_context.get("offcpu_profile_paths") or []
            queue_paths = target.get("queue_metrics_paths") or target_scope.get("queue_metrics_paths") or source_context.get("queue_metrics_paths") or []
            broker_paths = target.get("broker_metrics_paths") or target_scope.get("broker_metrics_paths") or source_context.get("broker_metrics_paths") or []
            return {
                "target_config": {
                    "pid": target.get("pid"),
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "host_id": target.get("host_id"),
                    "container_id": target.get("container_id"),
                    "source_context": source_context,
                    "application_runtime_log_paths": runtime_log_paths,
                    "trace_paths": trace_paths,
                    "offcpu_profile_paths": offcpu_paths,
                    "queue_metrics_paths": queue_paths,
                    "broker_metrics_paths": broker_paths,
                },
                "source_context": source_context,
                **({"application_runtime_log_paths": runtime_log_paths} if runtime_log_paths else {}),
                **({"runtime_log_paths": runtime_log_paths} if runtime_log_paths else {}),
                **({"trace_endpoint_profile_json_paths": trace_paths} if trace_paths else {}),
                **({"off_cpu_wait_json_paths": offcpu_paths} if offcpu_paths else {}),
                **({"queue_metrics_json_paths": queue_paths} if queue_paths else {}),
                **({"broker_metrics_json_paths": broker_paths} if broker_paths else {}),
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

    def _session_line_candidates(self, diagnosis_id: str) -> list[dict[str, Any]]:
        session = self.store.get_session(diagnosis_id) or {}
        candidates: list[dict[str, Any]] = []

        def add(file_name: Any, line: Any, symbol: Any = "") -> None:
            try:
                line_number = int(line or 0)
            except (TypeError, ValueError):
                return
            if not file_name or line_number <= 0:
                return
            normalized = {
                "file": str(file_name),
                "line": line_number,
                "symbol": str(symbol or ""),
            }
            if normalized not in candidates:
                candidates.append(normalized)

        # Source inspection can discover mechanism lines that were not present in
        # the original runtime stack. Feed those verified lines into the next probe.
        for task_id in session.get("child_task_ids", []):
            for artifact in self.repo.artifacts.get(task_id, []):
                if artifact.get("artifact_type") != "source_snapshot_json":
                    continue
                value = self._read_artifact_json(artifact)
                if not isinstance(value, dict):
                    continue
                for snippet in value.get("snippets", []):
                    if isinstance(snippet, dict):
                        add(snippet.get("file"), snippet.get("focus_line"), snippet.get("symbol"))
                for context in value.get("enclosing_contexts", []):
                    if not isinstance(context, dict):
                        continue
                    file_name = context.get("file")
                    symbol = context.get("symbol")
                    for path in context.get("reference_paths", []):
                        if not isinstance(path, dict):
                            continue
                        for upstream in path.get("upstream_candidates", []):
                            if isinstance(upstream, dict):
                                add(file_name, upstream.get("line"), symbol)
                        for source_line in path.get("source_lines", []):
                            if isinstance(source_line, dict):
                                add(file_name, source_line.get("line"), symbol)
        for task_id in session.get("child_task_ids", []):
            for artifact in self.repo.artifacts.get(task_id, []):
                if artifact.get("artifact_type") not in {"python_stack_samples_json", "python_heap_profile_json", "go_heap_profile_json", "depth_evidence_json"}:
                    continue
                value = self._read_artifact_json(artifact)
                if not isinstance(value, dict):
                    continue
                for item in value.get("line_candidates", []):
                    if not isinstance(item, dict) or not item.get("file") or int(item.get("line") or 0) <= 0:
                        continue
                    add(item["file"], item["line"], item.get("symbol") or item.get("function"))
                for item in value.get("stack_samples", []):
                    if not isinstance(item, dict):
                        continue
                    for candidate in _line_candidates_from_runtime_stack_sample(item):
                        add(candidate["file"], candidate["line"], candidate.get("function"))
        return _prioritized_line_candidates(candidates)[:64]

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
            evidence_status, evidence_reason = _task_evidence_validity(
                task.collector_type,
                artifact_values,
                structured_evidence.model_dump(mode="json"),
            )
            for probe in self.store.list_probes(diagnosis_id):
                if probe.get("task_id") == task.id:
                    self.store.update_probe(
                        probe["step_id"],
                        evidence_status=evidence_status,
                        evidence_reason=evidence_reason,
                    )
                    break
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

            values = {
                **artifact_values,
                **structured_evidence.model_dump(mode="json"),
            }
            values["artifact_refs"] = structured_evidence.artifact_refs
            values["stack_summary"] = structured_evidence.stack_summary
            values["call_path_hotspots"] = structured_evidence.call_path_hotspots
            values["confidence_inputs"] = structured_evidence.confidence_inputs
            target = self._target_for_task(diagnosis_id, task)
            attribution_graph = build_attribution_graph(
                evidence=structured_evidence.model_dump(mode="json"),
                target=target,
                source_snapshot=values.get("source_snapshot_json"),
                source_mechanism=values.get("source_mechanism_json"),
            )
            qualification = qualify_attribution(attribution_graph)
            values["attribution_graph"] = attribution_graph.model_dump(mode="json")
            values["qualification"] = qualification.model_dump(mode="json")
            task_observations.append(
                self._build_task_observation(diagnosis_id, task, values, evidence_ids)
            )

        if not task_observations:
            return False
        # Current session AI receives Analyzer facts and the localization
        # boundary. It creates new candidates after the structured evidence
        # stage; no legacy rule candidate is inserted here.
        deduped: list[dict[str, Any]] = []

        cluster_assessment = self._build_cluster_assessment(diagnosis_id, task_observations)
        _enrich_dependency_control_assessment(cluster_assessment, task_observations)
        cluster_assessment.update(_assessment_location_fields(cluster_assessment, deduped, self.store.get_session(diagnosis_id) or {}))
        # Location enrichment can replace a generic process anchor with a
        # more specific wait/function anchor. Keep the persisted assessment
        # claim synchronized with that final anchor.
        if cluster_assessment.get("classification") == "self_code_or_process_pressure":
            anchor = cluster_assessment.get("primary_anchor")
            if isinstance(anchor, dict) and anchor:
                cluster_assessment["diagnostic_claim"] = _self_pressure_summary(anchor)
                cluster_assessment["summary"] = cluster_assessment["diagnostic_claim"]
        if cluster_assessment.get("classification") == "runtime_stall":
            followup_requests = [
                request for request in followup_requests
                if request in {"runtime_control_history", "log_scan"}
            ]
        if cluster_assessment.get("classification") == "runtime_stall":
            deduped = (
                [_runtime_control_candidate(cluster_assessment)]
                if cluster_assessment.get("conclusion_eligible")
                else []
            )
        sufficient_dependency = _has_sufficient_dependency_conclusion(
            cluster_assessment,
            deduped,
            task_observations,
        )
        followup_session = dict(self.store.get_session(diagnosis_id) or {})
        followup_session["completed_depth_evidence_gaps"] = sorted(_completed_depth_evidence_gaps(task_observations))
        followup_session["probe_evidence_status"] = {
            str((probe.get("parameters") or {}).get("evidence_gap") or ""): str(probe.get("evidence_status") or "")
            for probe in self.store.list_probes(diagnosis_id)
        }
        followup_session["probe_attempt_counts"] = {
            gap: sum(
                1
                for probe in self.store.list_probes(diagnosis_id)
                if str((probe.get("parameters") or {}).get("evidence_gap") or "") == gap
            )
            for gap in {"source_mechanism_query", "python_heap_reference"}
        }
        assessment_followups = _assessment_followup_requests(cluster_assessment, followup_session)
        followup_requests = _merge_assessment_followups(
            followup_requests,
            assessment_followups,
            sufficient_dependency=sufficient_dependency,
            memory_only=str((followup_session.get("normalized_intent") or {}).get("symptom") or "") == "memory_pressure",
        )
        cluster_assessment["line_probe_diagnostic"] = _line_probe_diagnostic(
            cluster_assessment,
            followup_session,
            requested_requests=assessment_followups,
            final_requests=followup_requests,
        )
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
        ) or _failed_tasks_are_only_optional_mechanism_followups(
            diagnosis_id,
            tasks,
            self.store.list_probes(diagnosis_id),
        )
        current_session = self.store.get_session(diagnosis_id) or {}
        previous_conclusions = current_session.get("conclusion_versions") or []
        previous_tree = (
            previous_conclusions[-1].get("controlled_ai_tree")
            if previous_conclusions and isinstance(previous_conclusions[-1], dict)
            else None
        )
        session_controlled_tree = _build_session_controlled_ai_tree(
            diagnosis_id=diagnosis_id,
            cluster_assessment=cluster_assessment,
            candidates=deduped,
            followup_requests=followup_requests,
            probes=self.store.list_probes(diagnosis_id),
            evidence_catalog=self.store.list_evidence(diagnosis_id),
            child_trees=controlled_ai_trees,
            source_snapshot_hashes=_source_snapshot_hashes(task_observations),
            previous_tree=previous_tree,
        )
        candidate_review = None
        fact_context: dict[str, Any] = {}
        if session_controlled_tree is not None:
            model_calls_used = int((current_session.get("budget_used") or {}).get("model_calls", 0) or 0)
            model_calls_limit = int((current_session.get("resource_budget") or {}).get("max_model_calls", 0) or 0)
            remaining_model_calls = max(0, model_calls_limit - model_calls_used)
            if remaining_model_calls > 0:
                raw_fact_context = {
                    "facts": task_observations,
                    "observations": [
                        {
                            "task_id": item.get("task_id"),
                            "collector_type": item.get("collector_type"),
                            "observation": item.get("observation") or item.get("summary") or "",
                        }
                        for item in task_observations
                    ],
                    "evidence_refs": sorted({
                        str(ref)
                        for item in self.store.list_evidence(diagnosis_id)
                        if isinstance(item, dict)
                        for ref in (item.get("evidence_id"), item.get("evidence_ref"))
                        if ref
                    }),
                    "localization_boundary": {
                        "level": cluster_assessment.get("max_supported_level") or cluster_assessment.get("supported_level") or "resource",
                        "target": cluster_assessment.get("claim_target") or cluster_assessment.get("root_entity"),
                        "reason": cluster_assessment.get("blocked_upgrade_reason") or "Analyzer 仅提供当前定位边界。",
                        "evidence_refs": _unique_strings(cluster_assessment.get("evidence_refs", [])),
                    },
                    "missing_evidence": followup_requests,
                    "source_anchor_catalog": _source_anchor_catalog(
                        self.store.list_evidence(diagnosis_id)
                    ),
                    "available_probes": [
                        str(item.get("evidence_family") or "")
                        for item in build_probe_manifest().get("available_probes", [])
                        if isinstance(item, dict) and item.get("evidence_family")
                    ],
                    "candidate_hints": [
                        {
                            "hint_id": str(item.get("candidate_id") or ""),
                            "hint_type": "mechanism",
                            "statement": str(item.get("description") or ""),
                            "status": "unproven",
                            "evidence_refs": _unique_strings(item.get("evidence_refs", [])),
                            "missing_evidence": _unique_strings(item.get("missing_evidence", [])),
                        }
                        for item in deduped
                    ],
                }
                fact_context_model = AnalyzerFactContext(
                    facts=raw_fact_context["facts"],
                    observations=raw_fact_context["observations"],
                    evidence_refs=raw_fact_context["evidence_refs"],
                    localization_boundary=LocalizationBoundary.model_validate(
                        raw_fact_context["localization_boundary"]
                    ),
                    missing_evidence=raw_fact_context["missing_evidence"],
                    available_probes=raw_fact_context["available_probes"],
                    candidate_hints=[
                        CandidateHint.model_validate(item)
                        for item in raw_fact_context["candidate_hints"]
                    ],
                )
                fact_context = {
                    **fact_context_model.model_dump(mode="json"),
                    # Source anchors are query context, not a candidate or
                    # conclusion field, and must remain available to AI.
                    "source_anchor_catalog": raw_fact_context["source_anchor_catalog"],
                }
                candidate_review = generate_session_candidate_review(
                    diagnosis_id=diagnosis_id,
                    fact_context=fact_context,
                    session_tree=session_controlled_tree,
                    evidence_catalog=self.store.list_evidence(diagnosis_id),
                    probe_manifest=build_probe_manifest(),
                    max_attempts=remaining_model_calls,
                )
                attempts = int(candidate_review.get("ai_review_attempts", 0) or 0)
                if attempts:
                    usage = dict(current_session.get("budget_used") or {})
                    usage["model_calls"] = model_calls_used + attempts
                    self.store.update_session(diagnosis_id, budget_used=usage)
                    current_session = self.store.get_session(diagnosis_id) or current_session
                if candidate_review.get("ai_review_status") == "succeeded":
                    selected = _registered_probe_families(
                        candidate_review,
                        build_probe_manifest(),
                    )
                    followup_requests = _unique_strings([*followup_requests, *selected])
                    session_controlled_tree = _apply_candidate_review(session_controlled_tree, candidate_review)
                else:
                    # Analyzer directions are fallback investigation candidates
                    # only when the complete first AI candidate round failed.
                    session_controlled_tree = _mark_analyzer_fallback_tree(session_controlled_tree)
            else:
                # No call was available for the first candidate round. Keep
                # the state explicit so Analyzer directions are understood as
                # fallback investigation candidates, never formal causes.
                candidate_review = {
                    "ai_review_scope": "candidate_generation",
                    "ai_review_status": "fallback",
                    "ai_review_attempts": 0,
                    "ai_review_model": "",
                    "ai_review_error": "首轮 AI 候选生成没有可用的模型调用预算。",
                    "candidate_proposals": [],
                    "active_candidate_ids": [],
                    "deferred_candidate_ids": [],
                    "validation_diagnostics": [],
                    "candidate_generation_attempts": [],
                    "initial_evidence_context": {
                        "evidence_refs": sorted({
                            str(ref)
                            for item in self.store.list_evidence(diagnosis_id)
                            if isinstance(item, dict)
                            for ref in (
                                item.get("evidence_id"),
                                item.get("evidence_ref"),
                            )
                            if ref
                        })[:256],
                    },
                }
                session_controlled_tree = _mark_analyzer_fallback_tree(session_controlled_tree)
        if session_controlled_tree is not None:
            followup_session["session_main"] = session_controlled_tree.model_dump(mode="json")
            active_ai_candidate_ids = None
            if isinstance(candidate_review, dict) and "active_candidate_ids" in candidate_review:
                active_ai_candidate_ids = _unique_strings(
                    candidate_review.get("active_candidate_ids", [])
                )
            if active_ai_candidate_ids is None and previous_conclusions:
                previous_review = previous_conclusions[-1].get("candidate_review")
                if isinstance(previous_review, dict):
                    active_ai_candidate_ids = _unique_strings(
                        previous_review.get("active_candidate_ids", [])
                    )
            followup_session["active_ai_candidate_ids"] = active_ai_candidate_ids or []
            cluster_assessment["effective_investigation_level"] = _effective_investigation_level(
                cluster_assessment,
                followup_session,
            )
            effective_assessment_followups = _assessment_followup_requests(
                cluster_assessment,
                followup_session,
            )
            assessment_followups = _unique_strings([
                *assessment_followups,
                *effective_assessment_followups,
            ])
            followup_requests = _merge_assessment_followups(
                followup_requests,
                effective_assessment_followups,
                sufficient_dependency=sufficient_dependency,
                memory_only=str(
                    (followup_session.get("normalized_intent") or {}).get("symptom") or ""
                ) == "memory_pressure",
            )
        previous_retained = (
            previous_conclusions[-1].get("retained_conclusion")
            if previous_conclusions and isinstance(previous_conclusions[-1], dict)
            else None
        )
        investigation_review = None
        if followup_requests and session_controlled_tree is not None:
            model_calls_used = int((current_session.get("budget_used") or {}).get("model_calls", 0) or 0)
            model_calls_limit = int((current_session.get("resource_budget") or {}).get("max_model_calls", 0) or 0)
            remaining_model_calls = max(0, model_calls_limit - model_calls_used)
            if remaining_model_calls > 0:
                investigation_review = generate_session_investigation_review(
                    diagnosis_id=diagnosis_id,
                    session_tree=session_controlled_tree,
                    evidence_catalog=self.store.list_evidence(diagnosis_id),
                    probe_manifest=build_probe_manifest(),
                    allowed_evidence_families=followup_requests,
                    max_attempts=remaining_model_calls,
                )
                attempts = int(investigation_review.get("ai_review_attempts", 0) or 0)
                if attempts:
                    usage = dict(current_session.get("budget_used") or {})
                    usage["model_calls"] = model_calls_used + attempts
                    self.store.update_session(diagnosis_id, budget_used=usage)
                    current_session = self.store.get_session(diagnosis_id) or current_session
                if investigation_review.get("ai_review_status") == "succeeded":
                    followup_requests = union_probe_families(
                        followup_requests,
                        _registered_probe_families(
                            investigation_review,
                            build_probe_manifest(),
                        ),
                    )
                    session_controlled_tree = _apply_investigation_review(session_controlled_tree, investigation_review)
        # Plan the selected follow-up before composing the persisted conclusion.
        # This makes blocked input/provenance and collector failures part of the
        # same conclusion instead of appearing only in a later event stream.
        if followup_requests and tasks:
            probe_input_merge = merge_canonical_probe_inputs(
                (candidate_review or {}).get("probe_inputs") if isinstance(candidate_review, dict) else {},
                (investigation_review or {}).get("probe_inputs") if isinstance(investigation_review, dict) else {},
            )
            probe_inputs = probe_input_merge.inputs
            if session_controlled_tree is not None:
                session_controlled_tree = session_controlled_tree.model_copy(update={
                    "canonical_probe_plan": normalize_probe_plan(followup_requests, probe_inputs),
                    "probe_conflicts": probe_input_merge.conflicts,
                })
            self._plan_followup_requests(
                diagnosis_id,
                followup_requests,
                tasks[-1],
                probe_inputs=probe_inputs,
            )
        if session_controlled_tree is not None:
            session_controlled_tree = reduce_candidate_state(session_controlled_tree)
        tree_payload = session_controlled_tree.model_dump(mode="json") if session_controlled_tree else None
        valid_session_evidence_refs = {
            str(item.get(key) or "")
            for item in self.store.list_evidence(diagnosis_id)
            if isinstance(item, dict)
            for key in ("evidence_id", "evidence_ref", "raw_artifact_ref", "derived_artifact_ref")
            if item.get(key)
        }
        anchor_session_evidence_refs = evidence_refs_with_anchors(
            self.store.list_evidence(diagnosis_id)
        )
        runtime_anchor_session_evidence_refs = evidence_refs_with_runtime_anchors(
            self.store.list_evidence(diagnosis_id)
        )
        line_anchor_session_evidence_refs = evidence_refs_with_line_anchors(
            self.store.list_evidence(diagnosis_id)
        )
        root_cause_clusters = derive_root_cause_clusters_from_ai_tree(
            tree_payload,
            valid_evidence_refs=valid_session_evidence_refs,
            anchor_evidence_refs=anchor_session_evidence_refs,
            runtime_anchor_evidence_refs=runtime_anchor_session_evidence_refs,
            line_anchor_evidence_refs=line_anchor_session_evidence_refs,
        )
        # Analyzer/scenario gates remain evidence and localization inputs. They
        # are intentionally not promoted into formal session clusters without
        # an eligible AI candidate from the canonical session tree.
        engineering_clusters = build_root_cause_clusters(
            task_observations,
            cluster_assessment,
            current_session,
            tree_payload,
        )
        localization_frontier = derive_localization_frontier_from_ai_tree(
            tree_payload,
            valid_evidence_refs=valid_session_evidence_refs,
            anchor_evidence_refs=anchor_session_evidence_refs,
            runtime_anchor_evidence_refs=runtime_anchor_session_evidence_refs,
            line_anchor_evidence_refs=line_anchor_session_evidence_refs,
        )
        ai_gate_failures = [
            *collect_candidate_generation_gate_failures(candidate_review),
            *collect_ai_gate_failures(
                tree_payload,
                valid_evidence_refs=valid_session_evidence_refs,
                evidence_catalog=self.store.list_evidence(diagnosis_id),
            ),
            *_collect_probe_gate_failures(
                self.store.list_probes(diagnosis_id),
                (self.store.get_detail(diagnosis_id) or {}).get("events", []),
                initial_evidence_refs=valid_session_evidence_refs,
            ),
        ]
        session_tree_payload = session_controlled_tree.model_dump(mode="json") if session_controlled_tree else None
        scenario_retained = build_scenario_retained_conclusion(
            task_observations,
            session_tree_payload,
            target_service=_target_service(current_session) or str(cluster_assessment.get("target") or "目标服务"),
        )
        if scenario_retained and not any(cluster.conclusion_eligible for cluster in root_cause_clusters):
            cluster_assessment["scenario_retained_conclusion"] = scenario_retained
        retained_conclusion = build_retained_conclusion(
            root_cause_clusters,
            cluster_assessment,
            session_tree_payload,
            previous_retained=previous_retained,
            inherited=False,
        )
        qualification_boundary = build_qualification_boundary(
            cluster_assessment,
            followup_requests=followup_requests,
            probes=[
                probe
                for probe in self.store.list_probes(diagnosis_id)
                if str((probe.get("parameters") or {}).get("evidence_gap") or "") in set(followup_requests)
            ],
            origin_parent_candidate_id=(retained_conclusion or {}).get("candidate_id"),
        )
        probes_for_review = self.store.list_probes(diagnosis_id)
        if self._has_schedulable_followup_work(
            diagnosis_id,
            followup_requests,
            probes_for_review,
            tasks[-1] if tasks else None,
        ):
            ai_review = {
                "ai_review_status": "deferred_for_evidence",
                "ai_review_attempts": int((investigation_review or {}).get("ai_review_attempts", 0) or 0),
                "ai_review_model": str((investigation_review or {}).get("ai_review_model") or ""),
                "ai_review_error": (
                    "等待 AI 已选择的最小必要证据返回"
                    if (investigation_review or {}).get("ai_review_status") == "succeeded"
                    else str((investigation_review or {}).get("ai_review_error") or "AI 调查轮未成功，按工程层最小补证继续")
                ),
                "review": None,
            }
        else:
            model_calls_used = int((current_session.get("budget_used") or {}).get("model_calls", 0) or 0)
            model_calls_limit = int((current_session.get("resource_budget") or {}).get("max_model_calls", 0) or 0)
            remaining_model_calls = max(0, model_calls_limit - model_calls_used)
            if remaining_model_calls == 0:
                ai_review = {
                    "ai_review_status": "budget_exhausted",
                    "ai_review_attempts": 0,
                    "ai_review_model": "",
                    "ai_review_error": "会话级 AI 调用预算已用尽",
                    "review": None,
                }
            else:
                ai_review = generate_session_conclusion_review(
                    diagnosis_id=diagnosis_id,
                    clusters=root_cause_clusters,
                    session_tree=session_controlled_tree,
                    evidence_catalog=self.store.list_evidence(diagnosis_id),
                    probe_manifest=build_probe_manifest(),
                    localization_frontier=localization_frontier,
                    max_attempts=remaining_model_calls,
                )
                attempts = int(ai_review.get("ai_review_attempts", 0) or 0)
                if attempts:
                    usage = dict(current_session.get("budget_used") or {})
                    usage["model_calls"] = model_calls_used + attempts
                    self.store.update_session(diagnosis_id, budget_used=usage)
        if ai_review["ai_review_status"] == "succeeded":
            explanation = apply_session_review(
                root_cause_clusters,
                ai_review["review"],
                attempts=ai_review["ai_review_attempts"],
                model=ai_review["ai_review_model"],
                retained_conclusion=retained_conclusion,
                qualification_boundary=qualification_boundary,
                localization_frontier=localization_frontier,
            )
            if session_controlled_tree:
                session_controlled_tree = reduce_candidate_state(session_controlled_tree)
        else:
            explanation = build_fallback_explanation(
                root_cause_clusters,
                cluster_assessment,
                status=ai_review["ai_review_status"],
                attempts=ai_review["ai_review_attempts"],
                model=ai_review["ai_review_model"],
                error=ai_review["ai_review_error"],
                session_tree=session_tree_payload,
                previous_retained=previous_retained,
                qualification_boundary=qualification_boundary,
                localization_frontier=localization_frontier,
            )
        if session_controlled_tree is not None:
            session_controlled_tree = _sync_tree_retained_conclusion(
                session_controlled_tree,
                explanation.get("retained_conclusion"),
            )
            final_retained_id = str(
                (explanation.get("retained_conclusion") or {}).get("candidate_id") or ""
            ).strip()
            if final_retained_id:
                qualification_boundary = {
                    **qualification_boundary,
                    "origin_parent_candidate_id": final_retained_id,
                }
                explanation["qualification_boundary"] = qualification_boundary
                explanation["localization_chain"] = _tree_localization_chain(
                    session_controlled_tree.layers,
                    final_retained_id,
                )
        session_qualification = build_session_qualification(
            root_cause_clusters,
            session_tree_payload,
            base=cluster_assessment,
            retained_conclusion=explanation.get("retained_conclusion"),
            session_ai_review_status=str(ai_review.get("ai_review_status") or ""),
            attribution_qualification=_session_attribution_qualification(
                _merge_attribution_graphs(task_observations),
                session_tree_payload,
            ),
        )
        cluster_assessment["unified_qualification"] = session_qualification
        explanation = apply_session_qualification(
            explanation,
            session_qualification,
            all_clusters=root_cause_clusters,
        )
        if explanation["classification"] == "compound_incident":
            cluster_assessment["classification"] = "compound_incident"
            cluster_assessment.update(_compound_location_fields(explanation["root_cause_clusters"], current_session))
        cluster_candidates = _root_cause_cluster_candidates(explanation["root_cause_clusters"], current_session)
        hypothesis_candidates = cluster_candidates or (
            [] if cluster_assessment.get("classification") == "runtime_stall" else deduped
        )
        possible_clusters = [
            item for item in engineering_clusters
            if (
                not item.conclusion_eligible
                and item.qualification in {"possible_root_cause", "partial_localization"}
                and item.cause_level not in {"observation", "call_path"}
            )
        ]
        if not cluster_candidates:
            cluster_assessment["observation_confidence"] = cluster_assessment.get("confidence")
            cluster_assessment["confidence"] = min(_num(cluster_assessment.get("confidence")), 0.49)
            cluster_assessment["confidence_level"] = "低" if possible_clusters else "不可判断"
        candidate_review_summary = _summarize_investigation_review(candidate_review)
        controlled_tree_payload = session_controlled_tree.model_dump(mode="json") if session_controlled_tree else None
        controlled_tree_payload = _apply_session_tree_qualification(
            controlled_tree_payload,
            session_qualification,
        )
        gate_failures = ai_gate_failures
        candidate_generation_output = _candidate_generation_output(candidate_review_summary)
        candidate_generation_output["line_probe_diagnostic"] = (
            cluster_assessment.get("line_probe_diagnostic") or {}
        )
        candidate_generation_output["gate_failures"] = [
            item for item in gate_failures
            if isinstance(item, dict)
            and str(item.get("stage") or "").startswith(("candidate_generation", "candidate_tree_ingestion"))
        ]
        conclusion_summary = explanation["headline"]
        retained_claim = str(
            (explanation.get("retained_conclusion") or {}).get("claim") or ""
        ).strip()
        observation_claim = str(
            cluster_assessment.get("diagnostic_claim") or retained_claim
        ).strip()
        if explanation.get("abstained") and observation_claim:
            conclusion_summary = (
                f"{conclusion_summary} 原始分析摘要（未作为正式根因）：{observation_claim}"
            )
        conclusion = {
            "version": len((self.store.get_session(diagnosis_id) or {}).get("conclusion_versions", [])) + 1,
            "generated_at": utcnow().isoformat(),
            "summary": conclusion_summary,
            "classification": explanation["classification"],
            "headline": explanation["headline"],
            "why_it_happened": explanation["why_it_happened"],
            "causal_chain": [item.model_dump(mode="json") for item in explanation["causal_chain"]],
            "localization_chain": [item.model_dump(mode="json") for item in explanation.get("localization_chain", [])],
            "root_cause_clusters": [item.model_dump(mode="json") for item in explanation["root_cause_clusters"]],
            "ruled_out_summary": explanation["ruled_out_summary"],
            "residual_unknowns": explanation["residual_unknowns"],
            "ai_review_status": explanation["ai_review_status"],
            "ai_review_scope": explanation["ai_review_scope"],
            "ai_review_attempts": explanation["ai_review_attempts"],
            "ai_review_model": explanation["ai_review_model"],
            "ai_review_error": explanation["ai_review_error"],
            "retained_conclusion": explanation.get("retained_conclusion"),
            "formal_root_cause": explanation.get("formal_root_cause"),
            "qualification_boundary": explanation.get("qualification_boundary") or qualification_boundary,
            "active_retained_candidate_id": explanation.get("active_retained_candidate_id") or (
                (explanation.get("retained_conclusion") or {}).get("candidate_id")
            ),
            "investigation_review": _summarize_investigation_review(investigation_review),
            "candidate_review": candidate_review_summary,
            "analyzer_fact_context": fact_context,
            "candidate_validation_diagnostics": (
                (candidate_review or {}).get("validation_diagnostics") or []
                if isinstance(candidate_review, dict)
                else []
            ),
            "ai_gate_failures": gate_failures,
            # Stable, explicit diagnostic fields. The older names above remain
            # for API compatibility, while these fields let the UI and offline
            # replay explain why promotion stopped without reading raw probes.
            "gate_failures": gate_failures,
            "candidate_generation_output": candidate_generation_output,
            "line_probe_diagnostic": cluster_assessment.get("line_probe_diagnostic") or {},
            "observations": _conclusion_observations(task_observations),
            "boundaries": _conclusion_boundaries(
                controlled_tree_payload,
                qualification_boundary,
            ),
            "retained_parent_conclusions": _retained_parent_conclusions(
                controlled_tree_payload,
                explanation.get("retained_conclusion"),
            ),
            "confidence_level": session_qualification.get("confidence_level") or "不可判断",
            "cluster_assessment": cluster_assessment,
            "qualification": session_qualification,
            "attribution_graph": _merge_attribution_graphs(task_observations),
            "root_cause_candidates": cluster_candidates,
            "possible_root_causes": [item.model_dump(mode="json") for item in possible_clusters],
            "abstained": session_qualification.get("qualification") != "formal_root_cause",
            "ruled_out": cluster_assessment["ruled_out"],
            "diagnostic_commands": diagnostic_commands,
            "recommendations": [
                {**item.model_dump(mode="json"), "execution": "suggestion_only"}
                for item in explanation["recommendations"]
            ],
            "limitations": sorted(set(missing + (["部分目标采集失败"] if failed_targets and not nonblocking_failed_depth else []))),
            "next_evidence_requests": followup_requests,
            # Child task trees are audit/replay snapshots only. They must never
            # be promoted to the session's canonical tree when the session
            # tree is absent.
            "controlled_ai_tree": controlled_tree_payload,
            "controlled_ai_trees": controlled_ai_trees,
            "coverage": {
                "task_count": len(tasks),
                "failed_targets": failed_targets,
                "nonblocking_failed_depth": nonblocking_failed_depth,
                "evidence_count": len(self.store.list_evidence(diagnosis_id)),
            },
        }
        self._append_conclusion(diagnosis_id, conclusion)
        self._update_hypotheses(diagnosis_id, hypothesis_candidates)
        return True

    @staticmethod
    def _probe_evidence_gap(probe_id: str) -> str:
        return {
            "process_cpu_profile": "cpu_profile",
            "process_off_cpu_profile": "off_cpu_wait_profile",
            "process_trace_endpoint_profile": "trace_endpoint_profile",
            "process_baseline_window": "baseline_window_profile",
            "process_python_runtime_profile": "python_runtime_profile",
            "process_python_heap_profile": "python_heap_profile",
            "process_go_heap_profile": "go_heap_profile",
            "process_source_snapshot": "source_snapshot",
            "process_log_scan": "log_scan",
            "process_dependency_check": "dependency_check",
            "process_redis_check": "redis_check",
            "process_io_latency": "io_latency",
            "process_memory_map": "memory_map",
            "process_runtime_control_history": "runtime_control_history",
            "process_python_lock_wait_profile": "python_lock_wait_profile",
            "process_python_exception_profile": "python_exception_profile",
            "process_python_queue_profile": "python_queue_profile",
            "process_python_pool_profile": "python_pool_profile",
            "process_python_retry_timeout_profile": "python_retry_timeout_profile",
            "process_python_cache_profile": "python_cache_profile",
            "process_python_input_profile": "python_input_profile",
        }.get(probe_id, "")

    def _plan_followup_requests(
        self,
        diagnosis_id: str,
        request_ids: list[str],
        parent_task,
        *,
        probe_inputs: dict[str, Any] | None = None,
    ) -> int:
        """Map AI tree evidence requests to registered follow-up probe plans."""
        session = self.store.get_session(diagnosis_id)
        if (
            session is None
            or session["status"] in TERMINAL_DIAGNOSIS_STATUSES
            or _is_frozen_evidence_session(session)
        ):
            if session is not None and _is_frozen_evidence_session(session):
                self.store.record_event(
                    diagnosis_id,
                    "frozen_evidence_followup_blocked",
                    {"requested_evidence_gaps": list(dict.fromkeys(request_ids))[:MAX_FOLLOWUP_REQUESTS_PER_ROUND]},
                )
            return 0
        parent_target = self._target_for_task(diagnosis_id, parent_task)
        existing_probes = self.store.list_probes(diagnosis_id)
        policy = str((session.get("risk_budget") or {}).get("auto_execute_policy") or "safe_only")
        collection_context = _session_collection_context(session)
        followup_round = max(0, len(session.get("conclusion_versions") or []) - 1)
        self._last_followup_scheduled = False
        max_followup_rounds = _max_followup_rounds(session)
        if followup_round >= max_followup_rounds:
            self.store.record_event(
                diagnosis_id,
                "followup_round_limit_reached",
                {
                    "followup_round": followup_round,
                    "max_followup_rounds": max_followup_rounds,
                    "requested_evidence_gaps": list(dict.fromkeys(request_ids))[:MAX_FOLLOWUP_REQUESTS_PER_ROUND],
                },
            )
            return 0
        round_created = sum(
            1
            for probe in self.store.list_probes(diagnosis_id)
            if (probe.get("parameters") or {}).get("budget_phase") == "followup"
            and int((probe.get("parameters") or {}).get("followup_round") or 0) == followup_round
        )
        remaining_slots = max(0, MAX_FOLLOWUP_REQUESTS_PER_ROUND - round_created)
        created = 0
        for evidence_gap in dict.fromkeys(request_ids):
            if created >= remaining_slots:
                break
            probe_id = evidence_gap_to_probe_id(evidence_gap)
            target = self._select_followup_target(session, evidence_gap, parent_target)
            target_key = str(target.get("instance_id") or "")
            matching_probes = [
                probe
                for probe in existing_probes
                if str((probe.get("parameters") or {}).get("evidence_gap") or "") == evidence_gap
                and str((probe.get("target") or {}).get("instance_id") or "") == target_key
            ]
            if not probe_id or not _can_schedule_evidence_attempt(evidence_gap, matching_probes):
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
            attempt_number = len(matching_probes) + 1
            key = f"{diagnosis_id}:followup:{evidence_gap}:{target_key}:{attempt_number}"
            step_id = f"step_{hashlib.sha256(key.encode()).hexdigest()[:14]}"
            duration = _followup_probe_duration(evidence_gap, definition)
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
                            "target_instance_id": target_key,
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
                diagnosis_id,
            )
            if evidence_gap == "source_mechanism_query":
                collector_parameters["total_timeout_sec"] = duration
            guarded_probe_input = (
                (probe_inputs or {}).get(evidence_gap)
                if isinstance((probe_inputs or {}).get(evidence_gap), dict)
                else {}
            )
            deep_candidate_id, origin_parent_candidate_id = _followup_provenance(
                evidence_gap,
                guarded_probe_input,
            )
            if evidence_gap == "source_mechanism_query" and not _source_mechanism_input_ready(
                guarded_probe_input,
                collector_parameters,
            ):
                self.store.record_event(
                    diagnosis_id,
                    "followup_probe_input_missing",
                    {
                        "evidence_gap": evidence_gap,
                        "probe_id": probe_id,
                        "target_instance_id": target_key,
                        "candidate_id": deep_candidate_id,
                        "origin_parent_candidate_id": origin_parent_candidate_id,
                        "query_spec_hash": _guarded_query_spec_hash(guarded_probe_input),
                        "reason": "缺少受控 AI 查询、受管理 SARIF 或显式受管理 query suite",
                    },
                )
                continue
            if evidence_gap == "source_mechanism_query" and _repeats_guarded_query(
                guarded_probe_input,
                matching_probes,
            ):
                self.store.record_event(
                    diagnosis_id,
                    "followup_probe_duplicate_query_skipped",
                    {
                        "evidence_gap": evidence_gap,
                        "target_instance_id": target_key,
                        "query_spec_hash": _guarded_query_spec_hash(guarded_probe_input),
                    },
                )
                continue
            provenance_required = bool(guarded_probe_input) or evidence_gap in {
                "source_mechanism_query",
                "python_heap_reference",
            }
            if provenance_required and evidence_gap in {
                "cpu_profile",
                "python_runtime_profile",
                "python_heap_profile",
                "source_snapshot",
                "source_mechanism_query",
                "python_heap_reference",
            } and (
                not deep_candidate_id or not origin_parent_candidate_id
            ):
                self.store.record_event(
                    diagnosis_id,
                    "followup_probe_provenance_missing",
                    {
                        "evidence_gap": evidence_gap,
                        "candidate_id": deep_candidate_id,
                        "origin_parent_candidate_id": origin_parent_candidate_id,
                        "target_instance_id": target_key,
                        "reason": "深探任务必须绑定唯一来源父节点，禁止从候选顺序推断。",
                    },
                )
                continue
            collector_parameters = {**collector_parameters, **guarded_probe_input}
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
                    "followup_round": followup_round,
                    "parent_task_id": parent_task.id,
                    **(
                        {
                            "candidate_id": deep_candidate_id,
                            "origin_parent_candidate_id": origin_parent_candidate_id,
                        }
                        if deep_candidate_id and origin_parent_candidate_id
                        else {}
                    ),
                    "execution_policy": policy,
                    **collection_context,
                    **collector_parameters,
                    "collector_invocation": collector_invocation,
                    "collector_capability_fingerprint": collector_capability_fingerprint(collector_invocation),
                    "collector_fingerprint": collector_request_fingerprint(
                        collector_invocation,
                        {
                            "duration_sec": duration,
                            "sample_rate": definition.default_sample_rate,
                            "evidence_gap": evidence_gap,
                            "budget_phase": "followup",
                            "followup_round": followup_round,
                            "parent_task_id": parent_task.id,
                            **collection_context,
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
            existing_probes.append(self.store.get_probe(step_id) or {})
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
                {"evidence_gaps": list(dict.fromkeys(request_ids))[:MAX_FOLLOWUP_REQUESTS_PER_ROUND]},
            )
        return created

    def _has_schedulable_followup_work(
        self,
        diagnosis_id: str,
        requests: list[str],
        probes: list[dict[str, Any]],
        parent_task,
    ) -> bool:
        session = self.store.get_session(diagnosis_id) or {}
        if _is_frozen_evidence_session(session) or not requests or parent_task is None:
            return False
        open_statuses = {"PLANNED", "APPROVED", "SCHEDULED", "RUNNING", "WAITING_APPROVAL"}
        parent_target = self._target_for_task(diagnosis_id, parent_task)
        bounded_requests = requests[:MAX_FOLLOWUP_REQUESTS_PER_ROUND]
        for request in bounded_requests:
            matching = [
                probe for probe in probes
                if str((probe.get("parameters") or {}).get("evidence_gap") or "") == request
            ]
            if any(str(probe.get("status") or "") in open_statuses for probe in matching):
                return True
        followup_round = max(0, len(session.get("conclusion_versions") or []) - 1)
        if followup_round >= _max_followup_rounds(session):
            return False
        for request in bounded_requests:
            matching = [
                probe for probe in probes
                if str((probe.get("parameters") or {}).get("evidence_gap") or "") == request
            ]
            if matching and not _can_schedule_evidence_attempt(request, matching):
                continue
            probe_id = evidence_gap_to_probe_id(request)
            definition = get_probe(probe_id) if probe_id else None
            if definition is None:
                continue
            target = self._select_followup_target(session, request, parent_target)
            agent = self.repo.agents.get(target.get("agent_id"))
            if agent and status_value(agent.status) == "ONLINE" and definition.runner_task_kind in set(agent.capabilities or []):
                return True
        return False

    @staticmethod
    def _select_followup_target(
        session: dict[str, Any],
        evidence_gap: str,
        parent_target: dict[str, Any],
    ) -> dict[str, Any]:
        scope = session.get("target_scope") if isinstance(session.get("target_scope"), dict) else {}
        instances = [item for item in scope.get("instances", []) if isinstance(item, dict)]
        target_service = str(scope.get("target_service") or "")
        conclusions = session.get("conclusion_versions") if isinstance(session.get("conclusion_versions"), list) else []
        latest = conclusions[-1] if conclusions and isinstance(conclusions[-1], dict) else {}
        assessment = latest.get("cluster_assessment") if isinstance(latest.get("cluster_assessment"), dict) else {}
        claim_target = str(assessment.get("claim_target") or assessment.get("root_entity") or "")

        if evidence_gap == "runtime_control_history":
            candidates = [
                item for item in instances
                if item.get("scope_role") == "downstream" and (item.get("container_id") or item.get("systemd_unit"))
            ]
            matched = next(
                (item for item in candidates if claim_target in {str(item.get("service_id") or ""), str(item.get("instance_id") or "")}),
                None,
            )
            if matched or candidates:
                return dict(matched or candidates[0])
        if evidence_gap in {"dependency_check", "redis_check", "trace_endpoint_profile"}:
            matched = next((item for item in instances if item.get("service_id") == target_service), None)
            if matched:
                return dict(matched)
        if evidence_gap == "log_scan" and claim_target:
            matched = next(
                (item for item in instances if claim_target in {str(item.get("service_id") or ""), str(item.get("instance_id") or "")}),
                None,
            )
            if matched:
                return dict(matched)
        if evidence_gap in FUNCTION_DEPTH_EVIDENCE_GAPS and assessment.get("classification") in {
            "same_host_noisy_neighbor", "compound_incident",
        }:
            compared = assessment.get("compared_targets") if isinstance(assessment.get("compared_targets"), list) else []
            noisy_ids = {
                str(item.get("instance_id") or "")
                for item in compared
                if isinstance(item, dict) and bool((item.get("pressure") or {}).get("cpu"))
            }
            matched = next((item for item in instances if str(item.get("instance_id") or "") in noisy_ids), None)
            if matched:
                return dict(matched)
        return dict(parent_target)

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
        capability_fingerprint = parameters.get("collector_capability_fingerprint")
        if not fingerprint:
            return None
        for task in self.repo.tasks.values():
            options = (task.request_params or {}).get("options", {})
            if not isinstance(options, dict):
                continue
            task_status = status_value(task.status)
            exact_request = options.get("collector_fingerprint") == fingerprint
            same_capability = bool(
                capability_fingerprint
                and options.get("collector_capability_fingerprint") == capability_fingerprint
            )
            if task_status == "DONE" and exact_request and self._task_has_acceptable_evidence(task.id):
                return task
            if task_status == "FAILED" and same_capability and _is_stable_blocked_result(task.status_reason):
                return task
        return None

    def _task_has_acceptable_evidence(self, task_id: str) -> bool:
        for artifact in self.repo.artifacts.get(task_id, []):
            metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
            value = metadata.get("data") if isinstance(metadata.get("data"), dict) else metadata
            validity = value.get("evidence_validity") if isinstance(value, dict) else None
            if isinstance(validity, dict):
                if validity.get("evidence_status") in {"valid", "partial"}:
                    return True
                continue
            summary = value.get("summary") if isinstance(value.get("summary"), dict) else value
            if isinstance(summary, dict) and any(
                summary.get(key)
                for key in ("process_cpu_sample_count", "sample_count", "matched_records", "check_count")
            ):
                return True
            if isinstance(value, dict) and any(isinstance(value.get(key), list) and value.get(key) for key in ("checks", "events", "top_functions")):
                return True
        return False

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
            "runtime_control": values.get("runtime_control_event_json") if isinstance(values.get("runtime_control_event_json"), dict) else {},
            "python_heap_profile": values.get("python_heap_profile_json") if isinstance(values.get("python_heap_profile_json"), dict) else {},
            "go_heap_profile": values.get("go_heap_profile_json") if isinstance(values.get("go_heap_profile_json"), dict) else {},
            "source_snapshot": values.get("source_snapshot_json") if isinstance(values.get("source_snapshot_json"), dict) else {},
            "source_mechanism": values.get("source_mechanism_json") if isinstance(values.get("source_mechanism_json"), dict) else {},
            "python_heap_reference": values.get("python_heap_reference_json") if isinstance(values.get("python_heap_reference_json"), dict) else {},
            "confidence_inputs": values.get("confidence_inputs") if isinstance(values.get("confidence_inputs"), dict) else {},
            "attribution_graph": values.get("attribution_graph") if isinstance(values.get("attribution_graph"), dict) else {},
            "qualification": values.get("qualification") if isinstance(values.get("qualification"), dict) else {},
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
        target_runtime_stall = any(obs["pressure"].get("runtime_stall") for obs in target_obs)
        runtime_control = _runtime_control_chain(target_obs)
        neighbor_pressure = any(_has_pressure(obs) for obs in same_host_obs)
        downstream_pressure = any(_has_pressure(obs) for obs in downstream_obs)
        downstream_dependency_failure = any(_has_dependency_failure(obs) or _has_redis_failure(obs) for obs in observations)
        memory_anchor: dict[str, Any] = {}
        if str((session.get("normalized_intent") or {}).get("symptom") or "") == "memory_pressure":
            scope = session.get("target_scope", {}) if isinstance(session.get("target_scope"), dict) else {}
            memory_anchor = _go_heap_growth_anchor(target_obs) if _is_go_target_scope(scope) else _memory_retention_anchor(target_obs)
        target_anchor = memory_anchor or _best_specific_anchor(target_obs)
        target_anchor = _verified_source_anchor(target_anchor, target_obs)
        target_anchor = _mechanism_enriched_anchor(target_anchor, target_obs)
        assessment_refs = _unique_strings(target_anchor.get("evidence_refs", [])) or all_refs
        shared_iowait = (
            any(obs["pressure"].get("io_wait") for obs in target_obs)
            and any(obs["pressure"].get("io_wait") for obs in same_host_obs)
        )

        if memory_anchor:
            if memory_anchor.get("anchor_type") in {"go_heap_hotspot", "go_heap_verified_source_line"}:
                classification = "go_heap_growth_candidate"
                confidence = 0.82 if target_anchor.get("source_context_hash") else 0.74
                summary = _go_heap_growth_summary(target_anchor)
            else:
                classification = "python_memory_retention"
                confidence = 0.92 if target_anchor.get("source_context_hash") else 0.84
                summary = _memory_retention_summary(target_anchor)
        elif target_runtime_stall:
            classification = "runtime_stall"
            confidence = 0.96 if runtime_control else 0.7
            summary = _runtime_stall_summary(target_anchor, runtime_control)
            ruled_out.append({
                "hypothesis": "self_code_regression",
                "reason": "目标进程在整个采样窗口持续处于 stopped/tracing-stop 状态，不需要用代码热点解释当前无进展。",
                "evidence_refs": all_refs,
            })
        elif downstream_dependency_failure:
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
        claim_metadata = _assessment_claim_metadata(
            classification=classification,
            session=session,
            anchor=target_anchor,
            evidence_refs=assessment_refs,
            downstream_dependency_failure=downstream_dependency_failure,
            shared_iowait=shared_iowait,
            neighbor_pressure=neighbor_pressure,
            runtime_control=runtime_control,
        )
        if claim_metadata.get("diagnostic_claim"):
            summary = str(claim_metadata["diagnostic_claim"])

        unified_qualifications = [
            obs.get("qualification")
            for obs in observations
            if isinstance(obs.get("qualification"), dict)
        ]
        unified_qualification = _merge_unified_qualifications(unified_qualifications)

        return {
            "classification": classification,
            "confidence": round(confidence, 2),
            "confidence_level": _confidence_label(confidence),
            "summary": summary,
            "evidence_refs": assessment_refs,
            "compared_targets": compared,
            "supported_level": target_anchor.get("supported_level") if target_anchor else None,
            "primary_anchor": target_anchor,
            "ruled_out": ruled_out,
            "alternative_hypotheses": alternative_hypotheses,
            "unified_qualification": unified_qualification,
            **claim_metadata,
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
            "observed_value": _summarize_artifact_value(artifact_type, value),
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
            if not _contains_truncated_marker(metadata["data"]):
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
        adjacency: dict[str, list[Any]] = {}
        for edge in request.context.dependencies:
            adjacency.setdefault(edge.source_service, []).append(edge)
        service_hops = {intent.target_service: 0}
        pending_services = [intent.target_service]
        while pending_services:
            source_service = pending_services.pop(0)
            source_hop = service_hops[source_service]
            if source_hop >= budget.max_topology_hops:
                continue
            for edge in adjacency.get(source_service, []):
                next_hop = source_hop + 1
                previous_hop = service_hops.get(edge.target_service)
                if previous_hop is None or next_hop < previous_hop:
                    service_hops[edge.target_service] = next_hop
                    pending_services.append(edge.target_service)

        for item in all_instances:
            hop = service_hops.get(item["service_id"])
            item["topology_hop"] = hop
            item["scope_role"] = "target" if hop == 0 else "downstream" if hop is not None else "unrelated"
        target_instances = [item for item in all_instances if item["service_id"] == intent.target_service]
        host_ids = {item["host_id"] for item in target_instances}
        same_host = [item for item in all_instances if item["host_id"] in host_ids and item not in target_instances]
        downstream_services = {service for service, hop in service_hops.items() if 0 < hop <= budget.max_topology_hops}
        for item in same_host:
            if item["service_id"] not in downstream_services:
                item["scope_role"] = "same_host"
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
                "source_topology_hop": service_hops.get(edge.source_service),
                "topology_hop": service_hops.get(edge.target_service),
            }
            for edge in request.context.dependencies
            if edge.source_service in service_hops
            and service_hops[edge.source_service] < budget.max_topology_hops
            and service_hops.get(edge.target_service, budget.max_topology_hops + 1) <= budget.max_topology_hops
        ]
        downstream = [item for item in all_instances if item["service_id"] in downstream_services]
        downstream.sort(key=lambda item: (int(item.get("topology_hop") or 0), item["instance_id"]))
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
            "downstream_instance_ids": [item["instance_id"] for item in downstream],
            "service_topology_hops": service_hops,
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
        cohort_id = str(target_scope.get("evidence_cohort_id") or "")
        now = utcnow()
        result = []
        for task in self.repo.tasks.values():
            if (task.agent_id, task.target_pid) not in targets:
                continue
            options = (task.request_params or {}).get("options", {})
            if not isinstance(options, dict) or str(options.get("evidence_cohort_id") or "") != cohort_id:
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
            return DiagnosisBudget(
                max_hosts=10,
                max_service_instances=20,
                max_parallel_probes=5,
                max_medium_risk_probes=5,
                max_model_calls=30,
            )
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


def _merge_unified_qualifications(values: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-task qualification conservatively for the session boundary."""
    items = [item for item in values if isinstance(item, dict)]
    if not items:
        return {}
    order = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
    common = min(
        items,
        key=lambda item: order.get(str(item.get("level") or "L0"), 0),
    )
    result = dict(common)
    result["source_task_count"] = len(items)
    result["per_task_levels"] = [
        str(item.get("level") or "L0")
        for item in items
    ]
    result["per_task_decisions"] = [
        str(item.get("decision") or "abstain")
        for item in items
    ]
    if len(set(result["per_task_levels"])) > 1:
        result["missing_evidence"] = _unique_strings([
            *(result.get("missing_evidence") or []),
            "统一目标/窗口下的最小共同资格层级",
        ])
        result["reason"] = (
            "不同采集任务的资格层级不一致，会话结论按最保守的共同层级输出。"
        )
    return result


def _merge_attribution_graphs(observations: list[dict[str, Any]]) -> dict[str, Any]:
    graphs = [
        item.get("attribution_graph")
        for item in observations
        if isinstance(item.get("attribution_graph"), dict)
    ]
    if not graphs:
        return {}
    result = {
        "schema_version": "2.0",
        "graph_id": "session:attribution",
        "target": {},
        "facts": {
            "schema_version": "2.0",
            "symptom_signals": [],
            "cost_centers": [],
            "trigger_candidates": [],
            "impact_candidates": [],
            "observed_relations": [],
            "missing_evidence": [],
            "evidence_quality": [],
            "evidence_refs": [],
            "evidence_window": {},
        },
        "nodes": [],
        "runtime_relations": [],
        "source_relations": [],
        "causal_edges": [],
        "repair_clusters": [],
        "boundaries": [],
        "entities": [],
        "graph_relations": [],
    }
    list_fields = (
        ("facts", "symptom_signals"),
        ("facts", "cost_centers"),
        ("facts", "trigger_candidates"),
        ("facts", "impact_candidates"),
        ("facts", "observed_relations"),
        ("facts", "evidence_quality"),
        ("nodes", None),
        ("runtime_relations", None),
        ("source_relations", None),
        ("causal_edges", None),
        ("repair_clusters", None),
        ("entities", None),
        ("graph_relations", None),
    )
    for graph in graphs:
        result["target"] = result["target"] or graph.get("target") or {}
        facts = graph.get("facts") if isinstance(graph.get("facts"), dict) else {}
        result["facts"]["missing_evidence"].extend(facts.get("missing_evidence") or [])
        result["facts"]["evidence_refs"].extend(facts.get("evidence_refs") or [])
        if not result["facts"]["evidence_window"]:
            result["facts"]["evidence_window"] = facts.get("evidence_window") or {}
        for container, field in list_fields:
            source = graph.get(container) if container != "facts" else facts
            if not isinstance(source, dict):
                continue
            values = source.get(field) if field else source
            if not isinstance(values, list):
                continue
            result[container][field].extend(values) if field else result[container].extend(values)
        result["boundaries"].extend(graph.get("boundaries") or [])
    for container, field in list_fields:
        values = result[container][field] if field else result[container]
        seen = set()
        deduped = []
        for value in values:
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        if field:
            result[container][field] = deduped
        else:
            result[container] = deduped
    return result


def _session_attribution_qualification(
    graph_payload: dict[str, Any],
    session_tree: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(graph_payload, dict) or not graph_payload:
        return None
    try:
        graph = AttributionGraph.model_validate(graph_payload)
    except Exception:
        return None
    nodes = _all_session_tree_nodes(session_tree)
    ai_candidates = [
        {
            "candidate_id": str(node.get("candidate_id") or ""),
            "generated_by": node.get("generated_by"),
            "claim": node.get("claim"),
            "mechanism": node.get("mechanism"),
            "target": node.get("target"),
            "supported_level": node.get("supported_level"),
            "evidence_refs": node.get("evidence_refs") or [],
            "causal_status": node.get("causal_status"),
            "decision": node.get("decision"),
            "parent_candidate_ids": node.get("parent_candidate_ids") or [],
            "origin_parent_candidate_id": node.get("origin_parent_candidate_id"),
            "cost_center_refs": node.get("cost_center_refs") or [],
            "trigger_refs": node.get("trigger_refs") or [],
            "mechanism_refs": node.get("mechanism_refs") or [],
            "impact_refs": node.get("impact_refs") or [],
            "source_relation_refs": node.get("source_relation_refs") or [],
        }
        for node in nodes
        if str(node.get("generated_by") or "") in {"ai", "ai_candidate", "ai_guarded"}
        and node.get("candidate_id")
    ]
    result = qualify_attribution(
        graph,
        ai_candidate_ids=[str(item["candidate_id"]) for item in ai_candidates],
        ai_candidates=ai_candidates,
    )
    return result.model_dump(mode="json")


def _all_session_tree_nodes(tree: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(tree, dict):
        return []
    return [
        node
        for layer in tree.get("layers", [])
        if isinstance(layer, dict)
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes")
        for node in layer.get(group, [])
        if isinstance(node, dict)
    ]


def _apply_session_tree_qualification(
    tree: dict[str, Any] | None,
    qualification: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(tree, dict):
        return tree
    eligible = {
        str(item)
        for item in qualification.get("eligible_candidate_ids", [])
        if item
    }
    result = dict(tree)
    if qualification.get("qualification") != "formal_root_cause":
        result["final_primary_causes"] = []
        result["final_secondary_causes"] = []
        return result
    result["final_primary_causes"] = [
        str(item) for item in tree.get("final_primary_causes", [])
        if str(item) in eligible
    ]
    result["final_secondary_causes"] = [
        str(item) for item in tree.get("final_secondary_causes", [])
        if str(item) in eligible
    ]
    return result


def _registered_probe_families(review: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    """Derive executable evidence families from validated request objects."""
    registered = {
        str(item.get("evidence_family") or "")
        for item in manifest.get("available_probes", [])
        if isinstance(item, dict) and item.get("evidence_family")
    }
    requested: list[str] = []
    raw_specs = review.get("probe_requests")
    if isinstance(raw_specs, list):
        for item in raw_specs:
            family = (
                str(item.get("evidence_family") or "").strip()
                if isinstance(item, dict)
                else str(item or "").strip()
            )
            if family in registered and family not in requested:
                requested.append(family)
    if not requested:
        for item in review.get("candidate_proposals", []) or []:
            if not isinstance(item, dict):
                continue
            for spec in item.get("probe_request_specs", []) or []:
                family = str(spec.get("evidence_family") or "").strip() if isinstance(spec, dict) else ""
                if family in registered and family not in requested:
                    requested.append(family)
            for family in item.get("probe_requests", []) or []:
                family = str(family or "").strip()
                if family in registered and family not in requested:
                    requested.append(family)
    if not requested:
        for family in review.get("selected_evidence_families", []) or []:
            family = str(family or "").strip()
            if family in registered and family not in requested:
                requested.append(family)
    return requested[:3]


def _reuse_max_age_seconds() -> int | None:
    raw = os.getenv("MINI_DROP_DIAGNOSIS_REUSE_MAX_AGE_SECONDS")
    if raw is None or raw == "":
        return None


def _session_collection_context(session: dict[str, Any]) -> dict[str, Any]:
    diagnosis_id = str(session.get("diagnosis_id") or "")
    time_range = session.get("effective_time_range") if isinstance(session.get("effective_time_range"), dict) else {}
    start = time_range.get("start")
    end = time_range.get("end")
    end_epoch = _epoch(end)
    now = utcnow()
    fresh_requested_window = end_epoch is not None and abs(now.timestamp() - end_epoch) <= 180
    context: dict[str, Any] = {
        "evidence_cohort_id": diagnosis_id,
        "collection_mode": "manual_group" if fresh_requested_window else "delayed_followup",
    }
    if fresh_requested_window:
        if start is not None:
            context["window_start"] = start
        if end is not None:
            context["window_end"] = end
        context["timing_relation"] = "same_window"
    else:
        context["window_start"] = datetime.fromtimestamp(now.timestamp() - 180, timezone.utc).isoformat()
        context["window_end"] = now.isoformat()
        context["timing_relation"] = "delayed_followup"
    return {key: value for key, value in context.items() if value not in (None, "")}


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _scope_probe_ids(symptom: str, target_scope: dict[str, Any]) -> list[str]:
    if (
        symptom == "memory_pressure"
        and (_has_memray_source_context(target_scope) or _is_go_target_scope(target_scope))
        and not target_scope.get("dependency_targets")
    ):
        return ["host_process_metrics"]
    probe_ids = list(choose_probe_ids(symptom))
    if symptom == "runtime_contention":
        probe_ids = ["host_process_metrics", "process_log_scan", "process_runtime_control_history"]
    dependency_targets = [
        item for item in target_scope.get("dependency_targets", [])
        if isinstance(item, dict)
    ]
    if dependency_targets:
        _append_once(probe_ids, "process_dependency_check", after="host_process_metrics")
        _append_once(probe_ids, "process_log_scan", after="process_dependency_check")
        if any(_is_redis_dependency(item) for item in dependency_targets):
            _append_once(probe_ids, "process_redis_check", after="process_dependency_check")
    if _has_python_runtime_log_context(target_scope):
        probe_ids = [
            "host_process_metrics",
            *_RUNTIME_LOG_SCENARIO_PROBES,
            *[
                probe_id for probe_id in probe_ids
                if probe_id != "host_process_metrics" and probe_id not in _RUNTIME_LOG_SCENARIO_PROBES
            ],
        ]
    return probe_ids


_RUNTIME_LOG_SCENARIO_PROBES = (
    "process_python_queue_profile",
    "process_python_pool_profile",
    "process_python_retry_timeout_profile",
    "process_python_cache_profile",
    "process_python_input_profile",
)


def _initial_probe_duration(probe_id: str, definition) -> int:
    if _is_runtime_log_scenario_probe(probe_id):
        return 1
    return min(definition.default_duration_seconds, definition.max_duration_seconds)


def _followup_probe_duration(evidence_gap: str, definition) -> int:
    if evidence_gap == "source_mechanism_query":
        return min(max(definition.default_duration_seconds + 90, definition.default_duration_seconds), definition.max_duration_seconds)
    return min(definition.default_duration_seconds, definition.max_duration_seconds)


def _is_runtime_log_scenario_probe(probe_id: str) -> bool:
    return probe_id in _RUNTIME_LOG_SCENARIO_PROBES


def _has_python_runtime_log_context(target_scope: dict[str, Any]) -> bool:
    contexts = [target_scope.get("source_context")]
    instances = [
        item
        for item in target_scope.get("instances", [])
        if isinstance(item, dict)
    ]
    contexts.extend(item.get("source_context") for item in instances)
    language_hints = [
        target_scope.get("language"),
        *[
            context.get("language")
            for context in contexts
            if isinstance(context, dict)
        ],
    ]
    service_hints = [
        target_scope.get("service_id"),
        target_scope.get("target_service"),
        *[item.get("service_id") for item in instances],
        *[item.get("instance_id") for item in instances],
    ]
    is_python = any(str(value or "").lower() == "python" for value in language_hints)
    if not is_python:
        is_python = any("python" in str(value or "").lower() or "py-" in str(value or "").lower() for value in service_hints)
    if not is_python:
        return False
    source_context = target_scope.get("source_context") if isinstance(target_scope.get("source_context"), dict) else {}
    return any(
        _application_runtime_log_paths(
            instance,
            target_scope,
            {
                **source_context,
                **(instance.get("source_context") if isinstance(instance.get("source_context"), dict) else {}),
            },
        )
        for instance in (instances or [{}])
    )


def _has_memray_source_context(target_scope: dict[str, Any]) -> bool:
    contexts = [target_scope.get("source_context")]
    contexts.extend(
        item.get("source_context")
        for item in target_scope.get("instances", [])
        if isinstance(item, dict)
    )
    return any(
        isinstance(context, dict)
        and str(context.get("language") or "").lower() == "python"
        and any(
            str(context.get(key) or "").strip()
            for key in ("memray_result_path", "memray_stats_path", "memray_leaks_path")
        )
        for context in contexts
    )


def _is_go_target_scope(target_scope: dict[str, Any]) -> bool:
    contexts = [target_scope.get("source_context")]
    contexts.extend(
        item.get("source_context")
        for item in target_scope.get("instances", [])
        if isinstance(item, dict)
    )
    if any(
        isinstance(context, dict)
        and str(context.get("language") or "").lower() in {"go", "golang"}
        for context in contexts
    ):
        return True
    text = " ".join(
        str(value or "")
        for value in (
            target_scope.get("language"),
            target_scope.get("target_service"),
            target_scope.get("service_id"),
            *[
                item.get("service_id")
                for item in target_scope.get("instances", [])
                if isinstance(item, dict)
            ],
            *[
                item.get("runtime")
                for item in target_scope.get("instances", [])
                if isinstance(item, dict)
            ],
        )
    ).lower()
    return "golang" in text or "go-" in text or text.endswith(" go")


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
    fd_count = _num(summary.get("fd_count"))
    threads = _num(summary.get("thread_count"))
    process_state = str(summary.get("process_state") or "")
    stopped_ratio = _num(summary.get("stopped_sample_ratio"))
    top_items = values.get("top_functions") if isinstance(values.get("top_functions"), list) else values.get("top_json") if isinstance(values.get("top_json"), list) else []
    top_percent = _num((top_items[0] or {}).get("percent")) if top_items else 0.0
    return {
        "cpu": cpu_user + cpu_sys >= 75 or top_percent >= 45,
        "io_wait": cpu_iowait >= 20 or _has_ebpf_latency(values.get("ebpf_metrics")),
        "memory": rss_mb >= 1024,
        "fd": fd_count >= 1000 or summary.get("fd_trend") == "increasing",
        "thread": threads >= 512,
        "load": load1m >= 4,
        "runtime_stall": process_state in {"T", "t"} and stopped_ratio >= 0.8,
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
                primitive_kind = classify_primitive(top_frame)
                return {
                    **base,
                    "supported_level": "syscall" if primitive_kind else "function" if symbolized else "process",
                    "anchor_type": (
                        "off_cpu_wait_primitive"
                        if primitive_kind
                        else "off_cpu_wait_top_frame" if symbolized
                        else "off_cpu_wait_unsymbolized_address"
                    ),
                    "anchor": top_frame,
                    "primitive_kind": primitive_kind,
                    "root_claim_allowed": bool(symbolized and not primitive_kind),
                    "wait_reason": top.get("wait_reason") or (off_cpu.get("summary") or {}).get("top_wait_reason"),
                    "samples": int(_num(top.get("samples"))),
                    "percent": _num(top.get("percent")),
                    "wait_ms": _num(top.get("wait_ms")),
                    "evidence_ref": top.get("evidence_ref") or "off_cpu_wait.top_wait_stacks[0]",
                    "blocked_upgrade_reason": (
                        "当前仅定位到等待/调度/系统调用原语；必须取得上层业务栈、调用路径或锁持有者后才能形成根因结论。"
                        if primitive_kind
                        else
                        "缺少锁持有者线程、业务调用栈或源码符号映射，不能直接升级到代码行。"
                        if symbolized
                        else "当前等待栈顶部仍是未符号化地址，需补 debuginfo/符号映射后才能升级到函数。"
                    ),
                }

    process_state = str(summary.get("process_state") or "")
    if process_state in {"T", "t"} and _num(summary.get("stopped_sample_ratio")) >= 0.8:
        return {
            **base,
            "supported_level": "process",
            "anchor_type": "process_state_stopped",
            "anchor": f"process_state={process_state}",
            "process_state": process_state,
            "process_state_name": summary.get("process_state_name") or "stopped",
            "stopped_sample_count": int(_num(summary.get("stopped_sample_count"))),
            "stopped_sample_ratio": _num(summary.get("stopped_sample_ratio")),
            "root_claim_allowed": True,
            "evidence_ref": "sys_metrics.summary",
            "blocked_upgrade_reason": "该故障机制位于进程运行状态层，不需要伪造函数或代码行定位。",
        }

    call_paths = values.get("call_path_hotspots")
    if isinstance(call_paths, list) and call_paths:
        top = next((item for item in call_paths if isinstance(item, dict)), {})
        call_path = top.get("call_path") if isinstance(top.get("call_path"), list) else []
        if top and call_path:
            source_candidate = _source_line_candidate_for_call_path(values, call_paths)
            runtime_line_candidates = _runtime_line_candidates_from_values(values, call_paths)
            return {
                **base,
                "supported_level": "call_path",
                "anchor_type": "call_path_hotspot",
                "anchor": " -> ".join(str(item) for item in call_path),
                "function": (source_candidate or {}).get("symbol") or top.get("function"),
                "file": (source_candidate or {}).get("file") or top.get("file"),
                "line": int(_num((source_candidate or {}).get("line") or top.get("line"))),
                "samples": int(_num(top.get("samples"))),
                "percent": _num(top.get("percent")),
                "evidence_ref": top.get("evidence_ref") or "structured_evidence.call_path_hotspots[0]",
                "runtime_line_candidates": runtime_line_candidates,
                "blocked_upgrade_reason": "缺少 line profiler 或源码映射，不能直接升级到具体代码行。",
            }

    if top_items:
        top = top_items[0] or {}
        name = str(top.get("name") or top.get("function") or top.get("symbol") or "").strip()
        if name:
            primitive_kind = classify_primitive(name)
            runtime_line_candidates = _runtime_line_candidates_from_values(values, top_items)
            return {
                **base,
                "supported_level": "syscall" if primitive_kind else "function",
                "anchor_type": "top_function_primitive" if primitive_kind else "top_function",
                "anchor": name,
                "file": str(top.get("file") or ""),
                "line": int(_num(top.get("line"))),
                "primitive_kind": primitive_kind,
                "root_claim_allowed": not primitive_kind,
                "samples": int(_num(top.get("samples"))),
                "percent": _num(top.get("percent")),
                "evidence_ref": top.get("evidence_ref") or "top_functions[0]",
                "runtime_line_candidates": runtime_line_candidates,
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
        "avg_host_cpu_busy_pct": _num(summary.get("avg_host_cpu_busy_pct")),
        "host_cpu_saturated": bool(summary.get("host_cpu_saturated")),
        "avg_cgroup_throttled_pct": _num(summary.get("avg_cgroup_throttled_pct")),
        "cgroup_nr_throttled_delta": int(_num(summary.get("cgroup_nr_throttled_delta"))),
        "target_scheduling_pressure": bool(summary.get("target_scheduling_pressure")),
        "workload_member_count": int(_num(summary.get("workload_member_count"))),
        "top_cpu_members": summary.get("top_cpu_members") if isinstance(summary.get("top_cpu_members"), list) else [],
        "evidence_ref": "sys_metrics.summary",
        "blocked_upgrade_reason": "缺少 off-CPU 等待栈、CPU profile 或 trace 回连，当前不能判断具体函数。",
    }


def _runtime_line_candidates_from_values(
    values: dict[str, Any],
    *candidate_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add(item: dict[str, Any]) -> None:
        file_name = str(item.get("file") or "")
        line_value = _num(item.get("line") or item.get("focus_line"))
        line = int(line_value) if line_value is not None else 0
        if not file_name or line <= 0:
            return
        normalized = {
            "file": file_name,
            "line": line,
            "symbol": str(item.get("symbol") or item.get("function") or item.get("name") or ""),
            "samples": int(_num(item.get("samples") or item.get("sample_count"))),
            "percent": _num(item.get("percent")),
            "evidence_ref": item.get("evidence_ref") or item.get("evidence_id"),
            "call_path": item.get("call_path") if isinstance(item.get("call_path"), list) else [],
        }
        if normalized not in candidates:
            candidates.append(normalized)

    for group in candidate_groups:
        for item in group:
            if isinstance(item, dict):
                add(item)
    for key in ("depth_evidence_json", "python_stack_samples_json", "go_heap_profile_json"):
        payload = values.get(key)
        if not isinstance(payload, dict):
            continue
        for list_key in ("line_candidates", "top_functions", "call_path_hotspots", "hotspots"):
            for item in payload.get(list_key, []):
                if isinstance(item, dict):
                    add(item)
        for item in payload.get("stack_samples", []):
            if not isinstance(item, dict):
                continue
            for candidate in _line_candidates_from_runtime_stack_sample(item):
                add(candidate)
    return _prioritized_line_candidates(candidates)[:64]


def _source_line_candidate_for_call_path(
    values: dict[str, Any],
    call_paths: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select a project-source frame that is actually present in a sampled path."""
    depth = values.get("depth_evidence_json")
    python_stacks = values.get("python_stack_samples_json")
    candidate_sources = []
    if isinstance(depth, dict):
        candidate_sources.extend(depth.get("line_candidates", []))
    if isinstance(python_stacks, dict):
        candidate_sources.extend(python_stacks.get("line_candidates", []))
    candidates = [
        item for item in candidate_sources
        if isinstance(item, dict) and item.get("file") and int(_num(item.get("line"))) > 0
    ]
    if not candidates:
        return None

    path_symbols = {
        str(symbol).strip()
        for hotspot in call_paths
        if isinstance(hotspot, dict)
        for path in [hotspot.get("call_path")]
        if isinstance(path, list)
        for symbol in path
        if str(symbol).strip()
    }
    if not path_symbols:
        return None

    def score(candidate: dict[str, Any]) -> tuple[int, int, int, int, int]:
        symbol = str(candidate.get("symbol") or "").strip()
        file_name = str(candidate.get("file") or "").replace("\\", "/")
        semantic = _source_symbol_priority(symbol, file_name)
        generic_runtime_frame = int(symbol.lower() in {
            "start", "worker", "main", "caller", "invoke", "new_func",
            "asynloop", "create_loop", "poll", "fire_timers",
        })
        project_source = int(
            not file_name.startswith((
                "/usr/local/lib/python",
                "/usr/lib/python",
                "/usr/local/lib/python3",
            ))
            and "/site-packages/" not in file_name
        )
        return (
            int(bool(symbol and symbol in path_symbols)),
            semantic,
            -generic_runtime_frame,
            project_source,
            -candidates.index(candidate),
        )

    selected = max(candidates, key=score)
    selected_symbol = str(selected.get("symbol") or "").strip()
    if selected_symbol not in path_symbols:
        return None
    if not score(selected)[1] or score(selected)[2] == -1:
        return None
    return {
        "file": str(selected["file"]),
        "line": int(_num(selected["line"])),
        "symbol": str(selected.get("symbol") or ""),
    }


def _memory_retention_anchor(observations: list[dict[str, Any]]) -> dict[str, Any]:
    retained: list[dict[str, Any]] = []
    evidence_refs: list[str] = []
    target: dict[str, Any] = {}
    for observation in observations:
        if observation.get("collector_type") in {
            "python_heap_profile",
            "pyspy",
            "source_snapshot",
            "source_mechanism_query",
            "python_heap_reference",
        }:
            evidence_refs.extend(observation.get("evidence_refs") or [])
        heap = observation.get("python_heap_profile")
        if not isinstance(heap, dict):
            continue
        validity = heap.get("evidence_validity") if isinstance(heap.get("evidence_validity"), dict) else {}
        if validity.get("evidence_status") != "valid":
            continue
        target = observation.get("target") if isinstance(observation.get("target"), dict) else target
        retained.extend(
            item for item in (heap.get("retained_allocation_hotspots") or [])
            if isinstance(item, dict) and _num(item.get("size_bytes")) > 0
        )
    if not retained:
        return {}
    retained.sort(
        key=lambda item: (_num(item.get("size_bytes")), _num(item.get("allocation_count"))),
        reverse=True,
    )
    top = retained[0]
    call_path = [str(item) for item in (top.get("call_path") or []) if str(item).strip()]
    file_name = str(top.get("file") or "")
    line = int(_num(top.get("line")))
    function = str(top.get("function") or "")
    return {
        "service_id": target.get("service_id"),
        "instance_id": target.get("instance_id"),
        "pid": target.get("pid"),
        "supported_level": "call_path" if call_path else "function",
        "anchor_type": "retained_allocation_hotspot",
        "anchor": " -> ".join(call_path) if call_path else function,
        "function": function,
        "file": file_name,
        "line": line,
        "size_bytes": int(_num(top.get("size_bytes"))),
        "allocation_count": int(_num(top.get("allocation_count"))),
        "retained_hotspots": retained[:5],
        "evidence_refs": _unique_strings(evidence_refs),
        "evidence_ref": "python_heap_profile.retained_allocation_hotspots[0]",
        "root_claim_allowed": bool(file_name and line > 0),
        "blocked_upgrade_reason": "缺少 revision 匹配的源码片段，不能把 retained allocation 升级到代码行。",
    }


def _go_heap_growth_anchor(observations: list[dict[str, Any]]) -> dict[str, Any]:
    hotspots: list[dict[str, Any]] = []
    evidence_refs: list[str] = []
    target: dict[str, Any] = {}
    for observation in observations:
        if observation.get("collector_type") in {"go_pprof", "source_snapshot"}:
            evidence_refs.extend(observation.get("evidence_refs") or [])
        heap = observation.get("go_heap_profile")
        if not isinstance(heap, dict):
            continue
        validity = heap.get("evidence_validity") if isinstance(heap.get("evidence_validity"), dict) else {}
        if validity.get("evidence_status") not in {"valid", "partial"}:
            continue
        target = observation.get("target") if isinstance(observation.get("target"), dict) else target
        hotspots.extend(
            item for item in (heap.get("hotspots") or [])
            if isinstance(item, dict) and (_num(item.get("flat_bytes")) > 0 or _num(item.get("cum_bytes")) > 0)
        )
    if not hotspots:
        return {}
    hotspots.sort(
        key=lambda item: (_num(item.get("flat_bytes")), _num(item.get("cum_bytes"))),
        reverse=True,
    )
    top = hotspots[0]
    file_name = str(top.get("file") or "")
    line = int(_num(top.get("line")))
    function = str(top.get("function") or "")
    return {
        "service_id": target.get("service_id"),
        "instance_id": target.get("instance_id"),
        "pid": target.get("pid"),
        "supported_level": "function" if not (file_name and line > 0) else "line_candidate",
        "anchor_type": "go_heap_hotspot",
        "anchor": f"{file_name}:{line} {function}" if file_name and line > 0 else function,
        "function": function,
        "file": file_name,
        "line": line,
        "flat_bytes": int(_num(top.get("flat_bytes"))),
        "cum_bytes": int(_num(top.get("cum_bytes"))),
        "flat_percent": _num(top.get("flat_percent")),
        "cum_percent": _num(top.get("cum_percent")),
        "runtime_line_candidates": [
            {
                "file": item.get("file"),
                "line": item.get("line"),
                "symbol": item.get("function"),
            }
            for item in hotspots[:8]
            if isinstance(item, dict) and item.get("file") and int(_num(item.get("line"))) > 0
        ],
        "hotspots": hotspots[:5],
        "evidence_refs": _unique_strings(evidence_refs),
        "evidence_ref": "go_heap_profile.hotspots[0]",
        "root_claim_allowed": False,
        "blocked_upgrade_reason": "Go heap pprof 只能证明分配/in-use 热点，不能单独证明完整泄漏根因或保留链。",
    }


def _memory_retention_summary(anchor: dict[str, Any]) -> str:
    location = f"{anchor.get('file')}:{int(_num(anchor.get('line')))}"
    size_mib = _num(anchor.get("size_bytes")) / (1024 * 1024)
    path = str(anchor.get("anchor") or anchor.get("function") or "unknown")
    source_text = (
        f"源码 revision={anchor.get('source_revision')} 已验证该行上下文"
        if anchor.get("source_context_hash")
        else "源码 revision 尚未验证"
    )
    mechanism_paths = [
        item for item in (anchor.get("mechanism_paths") or [])
        if isinstance(item, dict)
        and item.get("candidate_relation") == "supports"
        and item.get("anchor_matches")
    ]
    runtime_paths = [
        item for item in (anchor.get("runtime_reference_paths") or [])
        if isinstance(item, dict) and item.get("nodes")
    ]
    source_hints = [
        item for item in (anchor.get("source_reference_hints") or [])
        if isinstance(item, dict) and item.get("retention_chain")
    ]
    if mechanism_paths and runtime_paths:
        return (
            f"Memray 将 retained allocation 的分配来源收敛到 {location} 的 {anchor.get('function') or 'unknown'}，"
            f"约 {size_mib:.2f} MiB / {int(_num(anchor.get('allocation_count')))} 次分配，调用路径为 {path}；"
            f"{source_text}；CodeQL 提供 {len(mechanism_paths)} 条受支持的跨函数机制路径，PyHeap 提供 "
            f"{len(runtime_paths)} 条实际入向引用路径。分配位置、源码机制和运行时持有关系已经分别取证，"
            "最终根因仍必须引用对应路径并解释三者如何闭合。"
        )
    if mechanism_paths:
        return (
            f"Memray 将 retained allocation 的分配来源收敛到 {location} 的 {anchor.get('function') or 'unknown'}，"
            f"约 {size_mib:.2f} MiB / {int(_num(anchor.get('allocation_count')))} 次分配，调用路径为 {path}；"
            f"{source_text}，CodeQL 已提取 {len(mechanism_paths)} 条受支持的源码机制路径。"
            "当前仍缺少 PyHeap 实际入向引用链，不能把源码可达路径等同于运行时长期持有关系。"
        )
    if source_hints:
        hint = source_hints[0]
        upstream = next(
            (
                str(item.get("expression") or "")
                for item in (hint.get("upstream_candidates") or [])
                if isinstance(item, dict) and item.get("expression")
            ),
            str(hint.get("source_expression") or "unknown"),
        )
        chain = " -> ".join(str(item) for item in hint.get("retention_chain") or [] if str(item))
        return (
            f"可能根因位于 {location} 附近的动态代码构建机制：源码局部引用路径显示 {upstream} 可能沿 "
            f"{chain} 被 {hint.get('retained_by') or '生成函数'} 长期持有；Memray 同时确认该位置持续保留约 "
            f"{size_mib:.2f} MiB / {int(_num(anchor.get('allocation_count')))} 次分配。{source_text}。"
            "该路径目前只是源码级候选，仍需 CodeQL 验证完整传播段，并由 PyHeap 验证运行时入向引用链。"
        )
    return (
        f"Memray retained allocation 将分配来源收敛到 {location} 的 {anchor.get('function') or 'unknown'}，"
        f"约 {size_mib:.2f} MiB / {int(_num(anchor.get('allocation_count')))} 次分配，调用路径为 {path}；"
        f"{source_text}。当前只证明对象持续保留和分配来源，尚未证明长期持有引用链。"
    )


def _best_specific_anchor(observations: list[dict[str, Any]]) -> dict[str, Any]:
    order = {"line": 0, "call_path": 1, "function": 2, "syscall": 3, "thread": 4, "process": 5, "resource": 6}
    anchors = [
        obs.get("specific_anchor")
        for obs in observations
        if isinstance(obs.get("specific_anchor"), dict)
    ]
    if not anchors:
        return {}
    selected = sorted(
        anchors,
        key=lambda item: (
            order.get(str(item.get("supported_level") or "resource"), 99),
            -_num(item.get("percent")),
            -_num(item.get("samples")),
        ),
    )[0]
    runtime_candidates: list[dict[str, Any]] = []
    for anchor in anchors:
        for candidate in anchor.get("runtime_line_candidates", []):
            if not isinstance(candidate, dict):
                continue
            file_name = str(candidate.get("file") or "")
            line = int(_num(candidate.get("line")))
            if not file_name or line <= 0:
                continue
            normalized = {
                **candidate,
                "file": file_name,
                "line": line,
            }
            if normalized not in runtime_candidates:
                runtime_candidates.append(normalized)
    if not runtime_candidates:
        return selected
    return {
        **selected,
        "runtime_line_candidates": _prioritized_line_candidates(runtime_candidates)[:64],
        "evidence_refs": _unique_strings([
            *selected.get("evidence_refs", []),
            *[
                ref
                for anchor in anchors
                for ref in anchor.get("evidence_refs", [])
            ],
        ]),
    }


def _verified_source_anchor(anchor: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    if not anchor:
        return anchor
    anchor_file = str(anchor.get("file") or "").replace("\\", "/")
    anchor_line = int(_num(anchor.get("line")))
    runtime_candidates = [
        item for item in anchor.get("runtime_line_candidates", [])
        if isinstance(item, dict) and item.get("file") and int(_num(item.get("line"))) > 0
    ]
    source_matches: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for observation in observations:
        snapshot = observation.get("source_snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("source_context_hash") or not snapshot.get("revision"):
            continue
        validity = snapshot.get("evidence_validity") if isinstance(snapshot.get("evidence_validity"), dict) else {}
        if str(validity.get("evidence_status") or "valid") not in {"valid", "partial"}:
            continue
        for snippet in snapshot.get("snippets", []):
            if not isinstance(snippet, dict):
                continue
            snippet_file = str(snippet.get("file") or "").replace("\\", "/")
            lines = {
                int(item.get("line") or 0)
                for item in snippet.get("lines", [])
                if isinstance(item, dict)
            }
            focus_line = int(_num(snippet.get("focus_line")))
            if anchor.get("file") and anchor_line > 0 and (
                not _source_files_match(anchor_file, snippet_file)
                or anchor_line not in lines
            ):
                runtime_match = next(
                    (
                        item for item in runtime_candidates
                        if int(_num(item.get("line"))) == focus_line
                        and _source_files_match(str(item.get("file") or ""), snippet_file)
                    ),
                    None,
                )
                if runtime_match is None:
                    continue
            elif not anchor.get("file") or anchor_line <= 0:
                runtime_match = next(
                    (
                        item for item in runtime_candidates
                        if int(_num(item.get("line"))) == focus_line
                        and _source_files_match(str(item.get("file") or ""), snippet_file)
                    ),
                    None,
                )
                if runtime_match is None:
                    continue
            else:
                runtime_match = None
            symbol = str(anchor.get("function") or anchor.get("anchor") or snippet.get("symbol") or "")
            direct_symbol = str(
                anchor.get("function") or anchor.get("symbol") or anchor.get("anchor") or snippet.get("symbol") or ""
            )
            direct_semantic = _source_symbol_semantic(direct_symbol, snippet_file)
            if (
                anchor.get("file")
                and anchor_line > 0
                and _source_files_match(anchor_file, snippet_file)
                and anchor_line in lines
                and direct_semantic
            ):
                return {
                    **anchor,
                    "supported_level": "line",
                    "anchor_type": "verified_source_line",
                    "anchor": f"{snippet_file}:{anchor_line} {direct_symbol}".strip(),
                    "source_context_hash": str(snapshot["source_context_hash"]),
                    "source_revision": str(snapshot.get("revision") or ""),
                    "source_reference_hints": [
                        path for path in (snapshot.get("reference_paths") or [])
                        if isinstance(path, dict)
                    ][:12],
                    "source_hint_level": "partial_localization",
                    "root_claim_allowed": False,
                    "blocked_upgrade_reason": "源码行已验证，但手写 AST reference_paths 只用于选择机制查询锚点，不能证明跨函数因果或运行时持有链。",
                }
            source_matches.append((runtime_match or {}, snippet, snapshot))

    if source_matches:
        def source_score(item: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> tuple[int, int, int, int, float]:
            runtime, snippet, _snapshot = item
            file_name = str(snippet.get("file") or "").replace("\\", "/")
            symbol = str(snippet.get("symbol") or runtime.get("symbol") or "").lower()
            semantic = _source_symbol_priority(symbol, file_name)
            generic_runtime_frame = int(symbol in _GENERIC_RUNTIME_SOURCE_SYMBOLS)
            project = int("/site-packages/" not in file_name and not file_name.startswith("/usr/"))
            samples = int(_num(runtime.get("samples") or runtime.get("sample_count")))
            percent = _num(runtime.get("percent"))
            return semantic, project, -generic_runtime_frame, samples, percent

        best_score = max(source_score(item) for item in source_matches)
        if best_score[0] == 0 and (best_score[1] == 0 or best_score[2] < 0):
            return anchor
        best = [item for item in source_matches if source_score(item) == best_score]
        if len(best) != 1:
            return anchor
        runtime, snippet, snapshot = best[0]
        focus_line = int(_num(snippet.get("focus_line") or runtime.get("line")))
        symbol = str(snippet.get("symbol") or runtime.get("symbol") or anchor.get("function") or "")
        source_refs = [
            ref for observation in observations
            if isinstance(observation.get("source_snapshot"), dict)
            and observation["source_snapshot"].get("source_context_hash") == snapshot.get("source_context_hash")
            for ref in observation.get("evidence_refs", [])
        ]
        return {
            **anchor,
            "supported_level": "line",
            "anchor_type": "verified_source_line",
            "anchor": f"{snippet.get('file')}:{focus_line} {symbol}".strip(),
            "file": snippet.get("file"),
            "line": focus_line,
            "function": symbol,
            "source_context_hash": str(snapshot["source_context_hash"]),
            "source_revision": str(snapshot.get("revision") or ""),
            "source_evidence_refs": _unique_strings(source_refs),
            "source_reference_hints": [
                path for path in (snapshot.get("reference_paths") or [])
                if isinstance(path, dict)
            ][:12],
            "source_hint_level": "partial_localization",
            "root_claim_allowed": False,
            "blocked_upgrade_reason": "源码行已验证，但当前运行时证据只支持 partial_localization，不能把该行直接提升为根因。",
        }
    return anchor


def _source_files_match(left: Any, right: Any) -> bool:
    left_text = str(left or "").replace("\\", "/").lstrip("/")
    right_text = str(right or "").replace("\\", "/").lstrip("/")
    if not left_text or not right_text:
        return False
    if left_text == right_text or left_text.endswith(f"/{right_text}") or right_text.endswith(f"/{left_text}"):
        return True
    left_parts = [part for part in left_text.split("/") if part]
    right_parts = [part for part in right_text.split("/") if part]
    max_suffix = min(len(left_parts), len(right_parts))
    for length in range(max_suffix, 1, -1):
        if left_parts[-length:] == right_parts[-length:]:
            return True
    return False


def _mechanism_enriched_anchor(anchor: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    if not anchor or not anchor.get("source_context_hash") or not anchor.get("source_revision"):
        return anchor
    revision = str(anchor.get("source_revision") or "")
    mechanism_paths: list[dict[str, Any]] = []
    rejected_mechanism_paths: list[dict[str, Any]] = []
    runtime_paths: list[dict[str, Any]] = []
    evidence_refs = list(anchor.get("evidence_refs") or [])
    for observation in observations:
        mechanism = observation.get("source_mechanism")
        if isinstance(mechanism, dict):
            validity = mechanism.get("evidence_validity") if isinstance(mechanism.get("evidence_validity"), dict) else {}
            if validity.get("evidence_status") == "valid" and str(mechanism.get("revision") or "") == revision:
                mechanism_paths.extend(
                    item for item in (mechanism.get("mechanism_paths") or [])
                    if isinstance(item, dict)
                )
                evidence_refs.extend(observation.get("evidence_refs") or [])
            elif validity.get("evidence_status") == "partial" and str(mechanism.get("revision") or "") == revision:
                query = mechanism.get("query") if isinstance(mechanism.get("query"), dict) else {}
                candidate_id = str(query.get("candidate_id") or "")
                if candidate_id:
                    paths = [
                        item for item in (mechanism.get("mechanism_paths") or [])
                        if isinstance(item, dict)
                    ]
                    rejected_mechanism_paths.append({
                        **(paths[0] if paths else {}),
                        "candidate_id": candidate_id,
                        "candidate_relation": "inconclusive",
                        "summary": (
                            f"候选 {candidate_id} 的 CodeQL 有序锚点链未完整闭合："
                            f"{validity.get('reason') or 'incomplete segment coverage'}。"
                        ),
                        "verification_outcome": "inconclusive",
                        "segment_coverage": mechanism.get("segment_coverage") or {},
                        "query_spec_hash": query.get("query_spec_hash"),
                        "evidence_ref": (
                            paths[0].get("evidence_ref")
                            if paths else "source_mechanism.segment_coverage"
                        ),
                    })
                    evidence_refs.extend(observation.get("evidence_refs") or [])
        runtime = observation.get("python_heap_reference")
        if isinstance(runtime, dict):
            validity = runtime.get("evidence_validity") if isinstance(runtime.get("evidence_validity"), dict) else {}
            if validity.get("evidence_status") in {"valid", "partial"}:
                runtime_paths.extend(
                    item for item in (runtime.get("reference_paths") or [])
                    if isinstance(item, dict) and item.get("nodes")
                )
                evidence_refs.extend(observation.get("evidence_refs") or [])
    mechanism_paths.extend(rejected_mechanism_paths)
    supported_paths = [
        item for item in mechanism_paths
        if item.get("candidate_relation") == "supports" and item.get("anchor_matches")
    ]
    if not mechanism_paths and not runtime_paths:
        return anchor
    supported_candidate_ids = {
        str(item.get("candidate_id") or "")
        for item in supported_paths
        if item.get("candidate_id")
    }
    matching_runtime_paths = [
        item for item in runtime_paths
        if item.get("candidate_id") and str(item.get("candidate_id")) in supported_candidate_ids
    ]
    complete = bool(supported_paths and matching_runtime_paths)
    return {
        **anchor,
        "mechanism_paths": mechanism_paths[:12],
        "runtime_reference_paths": matching_runtime_paths[:12],
        "mechanism_verified": bool(supported_paths),
        "runtime_reference_verified": bool(matching_runtime_paths),
        "evidence_refs": _unique_strings(evidence_refs),
        "root_claim_allowed": complete,
        "blocked_upgrade_reason": (
            "" if complete
            else "CodeQL 源码机制路径已取得，但缺少与候选一致的 PyHeap 运行时入向引用链。"
            if supported_paths
            else "尚无 CodeQL 路径支持当前机制候选。"
        ),
    }


def _self_pressure_summary(anchor: dict[str, Any]) -> str:
    if not anchor:
        return "当前不能给出可操作结论：缺少函数、等待点、调用路径或进程指标锚点，需要先补结构化采集证据。"
    instance = anchor.get("instance_id") or anchor.get("service_id") or "目标实例"
    pid = f"(pid={anchor.get('pid')})" if anchor.get("pid") else ""
    level = anchor.get("supported_level") or "process"
    anchor_name = anchor.get("anchor") or anchor.get("anchor_type") or "unknown"
    reason = anchor.get("blocked_upgrade_reason") or "证据不足，不能继续下钻。"
    if anchor.get("anchor_type") in {"off_cpu_wait_primitive", "top_function_primitive"}:
        samples = int(_num(anchor.get("samples")))
        percent = _num(anchor.get("percent"))
        details = [f"samples={samples}"] if samples else []
        if percent > 0:
            details.append(f"percent={percent:.1f}%")
        detail_text = f"（{', '.join(details)}）" if details else ""
        return (
            f"观察到 {instance}{pid} 主要停留在 {anchor_name}{detail_text}，它属于"
            f"{anchor.get('primitive_kind') or 'runtime primitive'}，只能说明线程在等待或让出执行权。"
            f"该证据不能区分主动 sleep、退避重试、锁等待或上游阻塞，因此不是根因结论；{reason}"
        )
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


def _go_heap_growth_summary(anchor: dict[str, Any]) -> str:
    instance = anchor.get("instance_id") or anchor.get("service_id") or "目标实例"
    pid = f"(pid={anchor.get('pid')})" if anchor.get("pid") else ""
    function = anchor.get("function") or anchor.get("anchor") or "unknown"
    location = ""
    if anchor.get("file") and int(_num(anchor.get("line"))) > 0:
        location = f" at {anchor.get('file')}:{int(_num(anchor.get('line')))}"
    flat_mib = _num(anchor.get("flat_bytes")) / (1024 * 1024)
    cum_mib = _num(anchor.get("cum_bytes")) / (1024 * 1024)
    source_text = "，源码行已由 source_snapshot 验证" if anchor.get("source_context_hash") else "，源码 revision 尚未验证"
    return (
        f"Go heap pprof 将 {instance}{pid} 的内存压力收敛到 {function}{location}"
        f"（flat={flat_mib:.2f}MiB, cum={cum_mib:.2f}MiB）{source_text}。"
        "该证据只支持分配/in-use 热点或增长候选，不能单独宣称完整内存泄漏根因。"
    )


def _runtime_stall_summary(anchor: dict[str, Any], control: dict[str, Any] | None = None) -> str:
    instance = anchor.get("instance_id") or anchor.get("service_id") or "目标实例"
    pid = f"(pid={anchor.get('pid')})" if anchor.get("pid") else ""
    state = anchor.get("process_state") or "T"
    state_name = anchor.get("process_state_name") or "stopped"
    count = int(_num(anchor.get("stopped_sample_count")))
    ratio = _num(anchor.get("stopped_sample_ratio")) * 100
    mechanism = (
        f"{instance}{pid} 在系统指标采样窗口内持续处于进程状态 {state} ({state_name})，"
        f"停止样本 {count} 个，占比 {ratio:.1f}%。该状态会阻止进程继续调度和处理业务，"
        "能够直接解释进程存在但工作不再推进。"
    )
    if not control:
        return mechanism + " 当前未捕获同窗控制事件，因此只能确认直接故障机制，无法确认是谁或哪个控制面暂停了进程。"
    event = control.get("event") if isinstance(control.get("event"), dict) else {}
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    actor_text = str(actor.get("comm") or actor.get("username") or actor.get("kind") or actor.get("pid") or "unknown_actor")
    action_text = str(action.get("signal") or action.get("operation") or event.get("event_type") or "control_action")
    provenance = control.get("source_provenance") if isinstance(control.get("source_provenance"), dict) else {}
    if control.get("complete_source_chain"):
        source_text = str(
            provenance.get("audit_actor")
            or provenance.get("controller")
            or provenance.get("parent_process")
            or provenance.get("systemd_unit")
            or provenance.get("release_event_id")
            or "上游控制源"
        )
        return (
            mechanism
            + f" 同窗控制证据证明 {source_text} 发起控制，{actor_text} 执行 {action_text} 作用于该目标，来源到故障的链路完整。"
        )
    return (
        mechanism
        + f" 同窗控制事件证明 {actor_text} 执行 {action_text} 直接暂停了该目标，这是已确认的直接根因；"
        "但当前没有父进程、控制器、systemd/cgroup、发布或审计身份等上游来源证据，因此尚不知道是谁或什么流程发起了该动作。"
    )


def _runtime_control_chain(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    for observation in observations:
        payload = observation.get("runtime_control")
        if not isinstance(payload, dict):
            continue
        validity = payload.get("evidence_validity") if isinstance(payload.get("evidence_validity"), dict) else {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        if validity.get("evidence_status") not in {"valid", "partial"}:
            continue
        observation_target = observation.get("target") if isinstance(observation.get("target"), dict) else {}
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            if not _event_can_suspend_process(event) or not _runtime_event_matches_target(event, observation_target, payload):
                continue
            actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
            action = event.get("action") if isinstance(event.get("action"), dict) else {}
            target = event.get("target") if isinstance(event.get("target"), dict) else {}
            if actor and action and target and event.get("observed_at"):
                qualification = event.get("qualification") if isinstance(event.get("qualification"), dict) else {}
                if qualification and not qualification.get("direct_control_chain"):
                    continue
                provenance = event.get("source_provenance") if isinstance(event.get("source_provenance"), dict) else {}
                complete_source = _runtime_source_precedes_action(provenance, event.get("observed_at"))
                return {
                    "event": event,
                    "evidence_refs": _unique_strings(observation.get("evidence_refs", [])),
                    "source_provenance": provenance,
                    "complete_source_chain": complete_source,
                    "origin_unknown": not complete_source,
                }
    return None


def _runtime_event_matches_target(
    event: dict[str, Any],
    observation_target: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    expected_pid = int(observation_target.get("pid") or payload.get("target_pid") or 0)
    actual_pid = int(target.get("pid") or 0)
    expected_container = str(observation_target.get("container_id") or "").strip()
    actual_container = str(target.get("container_id") or "").strip()
    if event.get("event_type") == "container_runtime_control":
        return bool(expected_container and actual_container and expected_container == actual_container)
    if expected_pid:
        return actual_pid == expected_pid
    expected_terms = {
        str(observation_target.get(key) or "").strip()
        for key in ("service_id", "instance_id", "systemd_unit", "container_id")
        if str(observation_target.get(key) or "").strip()
    }
    actual_terms = {
        str(target.get(key) or "").strip()
        for key in ("service_id", "instance_id", "unit", "container_id", "name")
        if str(target.get(key) or "").strip()
    }
    return bool(expected_terms & actual_terms)


def _enrich_dependency_control_assessment(
    assessment: dict[str, Any],
    observations: list[dict[str, Any]],
) -> None:
    if assessment.get("classification") != "downstream_dependency":
        return
    target_service = str(assessment.get("claim_target") or "").strip()
    matching = [
        observation
        for observation in observations
        if str((observation.get("target") or {}).get("service_id") or "") == target_service
    ]
    control = _runtime_control_chain(matching)
    if not control:
        return
    event = control["event"]
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    actor_text = str(actor.get("comm") or actor.get("kind") or actor.get("pid") or "运行控制面")
    action_text = _runtime_action_label(action)
    assessment.update({
        "confidence": max(0.94, _num(assessment.get("confidence"))),
        "confidence_level": "高",
        "summary": f"{target_service} 的依赖失败已下钻到同窗运行控制事件：{actor_text} 对目标执行 {action_text}。",
        "evidence_refs": _unique_strings([
            *assessment.get("evidence_refs", []),
            *control.get("evidence_refs", []),
        ]),
        "claim_type": "complete_source_root_cause" if control.get("complete_source_chain") else "direct_root_cause",
        "causal_status": "supported",
        "conclusion_eligible": True,
        "eligibility_reason": "同窗容器运行控制事件与依赖失败构成直接因果和传播链。",
        "mechanism": "process_suspended",
        "diagnostic_claim": (
            f"{actor_text} 在异常同窗对 {target_service} 执行 {action_text}，使该服务停止处理请求，"
            "并直接导致上游依赖调用失败。"
        ),
        "runtime_control_event": event,
        "source_provenance": control.get("source_provenance", {}),
        "origin_unknown": bool(control.get("origin_unknown", True)),
    })


def _runtime_action_label(action: dict[str, Any]) -> str:
    raw = str(action.get("signal") or action.get("operation") or "pause")
    return {
        "pause": "暂停",
        "freeze": "冻结",
        "cgroup.freeze": "cgroup 冻结",
        "SIGSTOP": "SIGSTOP 暂停信号",
        "SIGTSTP": "SIGTSTP 暂停信号",
    }.get(raw, raw)


def _runtime_source_precedes_action(provenance: dict[str, Any], observed_at: Any) -> bool:
    if not any(
        provenance.get(key)
        for key in (
            "parent_process", "redacted_command_source", "systemd_unit", "cgroup_identity",
            "release_event_id", "audit_actor", "controller",
        )
    ):
        return False
    try:
        initiated = datetime.fromisoformat(str(provenance.get("initiated_at") or "").replace("Z", "+00:00"))
        observed = datetime.fromisoformat(str(observed_at or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return initiated <= observed


def _event_can_suspend_process(event: dict[str, Any]) -> bool:
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    effect = event.get("effect") if isinstance(event.get("effect"), dict) else {}
    signal_name = str(action.get("signal") or "").upper()
    operation = str(action.get("operation") or "").lower()
    return bool(
        effect.get("expected_state") == "T"
        or signal_name in {"SIGSTOP", "SIGTSTP"}
        or operation in {"pause", "freeze", "cgroup.freeze"}
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


def _verified_line_candidate_id(anchor: dict[str, Any], classification: str) -> str:
    file_name = str(anchor.get("file") or "source").replace("\\", "/")
    symbol = str(anchor.get("function") or anchor.get("symbol") or "line")
    identity = f"{classification}:{file_name}:{int(_num(anchor.get('line')))}:{symbol}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"verified_line_{digest}"


_GENERIC_RUNTIME_SOURCE_SYMBOLS = {
    "start", "worker", "main", "caller", "invoke", "new_func",
    "asynloop", "create_loop", "poll", "fire_timers",
}


def _line_candidates_from_runtime_stack_sample(item: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def append_candidate(file_name: Any, line: Any, function: Any = "") -> None:
        line_number = int(_num(line))
        file_text = str(file_name or "")
        function_text = str(function or "")
        if not file_text or line_number <= 0:
            return
        if _is_low_value_runtime_file(file_text, function_text):
            return
        candidate = {
            "file": file_text,
            "line": line_number,
            "function": function_text,
        }
        if candidate not in candidates:
            candidates.append(candidate)

    append_candidate(item.get("file"), item.get("line"), item.get("function") or item.get("hot_frame"))
    stack = item.get("stack")
    if isinstance(stack, list):
        for frame in reversed(stack):
            parsed = _parse_runtime_frame_line(frame)
            if parsed:
                append_candidate(parsed["file"], parsed["line"], parsed.get("function"))
    return candidates


def _parse_runtime_frame_line(frame: Any) -> dict[str, Any] | None:
    text = str(frame or "").strip()
    if not text:
        return None
    traceback_match = re.search(r'File "([^"]+)", line (\d+), in ([^\s]+)', text)
    if traceback_match:
        return {
            "file": traceback_match.group(1),
            "line": int(traceback_match.group(2)),
            "function": traceback_match.group(3),
        }
    colon_match = re.search(r"(.+):(\d+)(?::([^:]+))?$", text)
    if colon_match:
        return {
            "file": colon_match.group(1),
            "line": int(colon_match.group(2)),
            "function": colon_match.group(3) or "",
        }
    return None


def _is_low_value_runtime_file(file_name: str, symbol: str = "") -> bool:
    text = f"{file_name} {symbol}".replace("\\", "/").lower()
    return (
        text.startswith(("/usr/local/lib/python", "/usr/lib/python"))
        or "/site-packages/" in text
        or any(
            token in text
            for token in (
                "/lib/python",
                "threading.py",
                "queue.py",
                "socket.py",
                "selectors.py",
                "asyncio/",
                "concurrent/futures",
            )
        )
    )


def _source_symbol_semantic(symbol: Any, file_name: Any = "") -> int:
    return int(_source_symbol_priority(symbol, file_name) > 0)


def _source_symbol_priority(symbol: Any, file_name: Any = "") -> int:
    text = f"{str(symbol or '')} {str(file_name or '')}".lower()
    if "celery/app/trace.py" in text and any(
        token in text
        for token in ("handle_failure", "_log_error", "on_error", "trace_task", "fast_trace_task")
    ):
        return 3
    if "get_pickleable_exception" in text or "celery/utils/serialization.py" in text:
        return 1
    if any(
        token in text
        for token in (
            "error", "failure", "exception", "traceback", "retention",
            "compile", "handle_failure", "on_error", "trace_task",
        )
    ):
        return 2
    if ".go" in text:
        return 1
    return 0


def _prioritized_line_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def score(index_and_candidate: tuple[int, dict[str, Any]]) -> tuple[int, int, int, int]:
        index, candidate = index_and_candidate
        file_name = str(candidate.get("file") or "").replace("\\", "/")
        symbol = str(candidate.get("symbol") or candidate.get("function") or "")
        project_source = int(
            not file_name.startswith((
                "/usr/local/lib/python",
                "/usr/lib/python",
                "/usr/local/lib/python3",
            ))
            and "/site-packages/" not in file_name
        )
        generic_runtime_frame = int(symbol.lower() in _GENERIC_RUNTIME_SOURCE_SYMBOLS)
        return (
            _source_symbol_priority(symbol, file_name),
            project_source,
            -generic_runtime_frame,
            -index,
        )

    return [
        candidate
        for _, candidate in sorted(
            enumerate(candidates),
            key=score,
            reverse=True,
        )
    ]


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


def _summarize_artifact_value(artifact_type: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _summarize_value(value)
    if artifact_type == "python_heap_profile_json":
        hotspots = value.get("retained_allocation_hotspots") or value.get("allocation_hotspots") or []
        return {
            "producer": value.get("producer"),
            "mode": value.get("mode"),
            "summary": value.get("summary") if isinstance(value.get("summary"), dict) else {},
            "retained_allocation_hotspots": [
                {
                    key: item.get(key)
                    for key in ("function", "file", "line", "size_bytes", "allocation_count", "call_path")
                }
                for item in hotspots[:8]
                if isinstance(item, dict)
            ],
            "evidence_validity": value.get("evidence_validity") or {},
        }
    if artifact_type == "go_heap_profile_json":
        hotspots = value.get("hotspots") or []
        return {
            "producer": value.get("producer"),
            "profile_kind": value.get("profile_kind"),
            "heap_mode": value.get("heap_mode"),
            "sample_type": value.get("sample_type"),
            "summary": value.get("summary") if isinstance(value.get("summary"), dict) else {},
            "hotspots": [
                {
                    key: item.get(key)
                    for key in ("function", "file", "line", "flat_bytes", "cum_bytes", "flat_percent", "cum_percent")
                }
                for item in hotspots[:8]
                if isinstance(item, dict)
            ],
            "line_candidates": [
                {
                    key: item.get(key)
                    for key in ("file", "line", "symbol", "function", "evidence_ref")
                }
                for item in (value.get("line_candidates") or [])[:8]
                if isinstance(item, dict)
            ],
            "evidence_validity": value.get("evidence_validity") or {},
        }
    if artifact_type == "source_snapshot_json":
        snippets = []
        for snippet in (value.get("snippets") or [])[:4]:
            if not isinstance(snippet, dict):
                continue
            snippets.append({
                "file": snippet.get("file"),
                "focus_line": snippet.get("focus_line"),
                "symbol": snippet.get("symbol"),
                "lines": [
                    {"line": line.get("line"), "text": str(line.get("text") or "")[:300]}
                    for line in (snippet.get("lines") or [])[:60]
                    if isinstance(line, dict)
                ],
            })
        enclosing_contexts = []
        for context in (value.get("enclosing_contexts") or [])[:2]:
            if not isinstance(context, dict):
                continue
            enclosing_contexts.append({
                "file": context.get("file"),
                "symbol": context.get("symbol"),
                "kind": context.get("kind"),
                "start_line": context.get("start_line"),
                "end_line": context.get("end_line"),
                "lines": [
                    {"line": line.get("line"), "text": str(line.get("text") or "")[:300]}
                    for line in (context.get("lines") or [])[:400]
                    if isinstance(line, dict)
                ],
            })
        return {
            "producer": value.get("producer"),
            "revision": value.get("revision"),
            "source_context_hash": value.get("source_context_hash"),
            "snippets": snippets,
            "enclosing_contexts": enclosing_contexts,
            "reference_paths": [
                {
                    "source_expression": path.get("source_expression"),
                    "source_kind": path.get("source_kind"),
                    "upstream_candidates": (path.get("upstream_candidates") or [])[:8],
                    "stored_via": path.get("stored_via"),
                    "container": path.get("container"),
                    "sink": path.get("sink"),
                    "runtime_slot": path.get("runtime_slot"),
                    "retained_by": path.get("retained_by"),
                    "retention_chain": (path.get("retention_chain") or [])[:8],
                    "source_lines": (path.get("source_lines") or [])[:8],
                }
                for path in (value.get("reference_paths") or [])[:12]
                if isinstance(path, dict)
            ],
            "evidence_validity": value.get("evidence_validity") or {},
        }
    if artifact_type == "source_mechanism_json":
        return {
            "producer": value.get("producer"),
            "revision": value.get("revision"),
            "source_context_hash": value.get("source_context_hash"),
            "query_pack_version": value.get("query_pack_version"),
            "cache": value.get("cache") if isinstance(value.get("cache"), dict) else {},
            "database_ref": value.get("database_ref"),
            "query": value.get("query") if isinstance(value.get("query"), dict) else {},
            "segment_coverage": value.get("segment_coverage") if isinstance(value.get("segment_coverage"), dict) else {},
            "mechanism_paths": [
                {
                    "path_id": path.get("path_id"),
                    "rule_id": path.get("rule_id"),
                    "summary": str(path.get("summary") or "")[:300],
                    "candidate_relation": path.get("candidate_relation"),
                    "candidate_id": path.get("candidate_id"),
                    "nodes": [
                        {
                            key: node.get(key)
                            for key in ("node_id", "file", "line", "symbol", "message")
                        }
                        for node in (path.get("nodes") or [])[:16]
                        if isinstance(node, dict)
                    ],
                    "edges": (path.get("edges") or [])[:16],
                    "evidence_ref": path.get("evidence_ref"),
                }
                for path in (value.get("mechanism_paths") or [])[:8]
                if isinstance(path, dict)
            ],
            "evidence_validity": value.get("evidence_validity") or {},
        }
    if artifact_type == "python_heap_reference_json":
        return {
            "producer": value.get("producer"),
            "candidate_id": value.get("candidate_id"),
            "top_retained_objects": [
                {
                    key: item.get(key)
                    for key in ("node_id", "type", "size_bytes", "retained_bytes", "repr")
                }
                for item in (value.get("top_retained_objects") or [])[:10]
                if isinstance(item, dict)
            ],
            "reference_paths": [
                {
                    "path_id": path.get("path_id"),
                    "candidate_id": path.get("candidate_id"),
                    "root_type": path.get("root_type"),
                    "target_type": path.get("target_type"),
                    "nodes": [
                        {
                            key: node.get(key)
                            for key in ("node_id", "type", "size_bytes", "retained_bytes", "repr")
                        }
                        for node in (path.get("nodes") or [])[:16]
                        if isinstance(node, dict)
                    ],
                    "edges": (path.get("edges") or [])[:16],
                    "retained_bytes": path.get("retained_bytes"),
                    "evidence_ref": path.get("evidence_ref"),
                }
                for path in (value.get("reference_paths") or [])[:8]
                if isinstance(path, dict)
            ],
            "evidence_validity": value.get("evidence_validity") or {},
        }
    return _summarize_value(value)


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


def _contains_truncated_marker(value: Any) -> bool:
    if value == "[TRUNCATED]":
        return True
    if isinstance(value, dict):
        return any(_contains_truncated_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_truncated_marker(item) for item in value)
    return False


def _normalize_structured_artifact_values(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    python_stacks = normalized.get("python_stack_samples_json")
    if isinstance(python_stacks, dict):
        if not isinstance(normalized.get("top_json"), list) or not normalized.get("top_json"):
            normalized["top_json"] = python_stacks.get("top_functions") or []
        python_depth = {
            "stack_samples": [
                {
                    **item,
                    "call_path": ";".join(str(part) for part in item.get("call_path", []))
                    if isinstance(item.get("call_path"), list)
                    else str(item.get("call_path") or ""),
                    "line_hint": f"{item.get('file')}:{item.get('line')}"
                    if item.get("file") and int(item.get("line") or 0) > 0
                    else "",
                }
                for item in python_stacks.get("stack_samples", [])
                if isinstance(item, dict)
            ],
            "line_candidates": python_stacks.get("line_candidates") or [],
            "call_path_hotspots": python_stacks.get("call_path_hotspots") or [],
            "context": {"collection_mode": "stack_sampling"},
        }
        existing_depth = normalized.get("depth_evidence_json")
        if not isinstance(existing_depth, dict):
            existing_depth = {}
        for key, value in python_depth.items():
            if not existing_depth.get(key) and value:
                existing_depth[key] = value
        normalized["depth_evidence_json"] = existing_depth
    heap_profile = normalized.get("python_heap_profile_json")
    if isinstance(heap_profile, dict) and "top_json" not in normalized:
        heap_hotspots = heap_profile.get("retained_allocation_hotspots") or heap_profile.get("allocation_hotspots") or []
        normalized["top_json"] = [
            {
                "name": item.get("function"),
                "file": item.get("file"),
                "line": item.get("line"),
                "samples": item.get("allocation_count"),
                "percent": 0,
                "call_path": item.get("call_path") or [],
            }
            for item in heap_hotspots
            if isinstance(item, dict)
        ]
    go_heap_profile = normalized.get("go_heap_profile_json")
    if isinstance(go_heap_profile, dict):
        hotspots = go_heap_profile.get("hotspots") if isinstance(go_heap_profile.get("hotspots"), list) else []
        if not isinstance(normalized.get("top_json"), list) or not normalized.get("top_json"):
            normalized["top_json"] = [
                {
                    "name": item.get("function"),
                    "file": item.get("file"),
                    "line": item.get("line"),
                    "samples": item.get("flat_bytes") or item.get("cum_bytes"),
                    "percent": item.get("flat_percent") or item.get("cum_percent") or 0,
                    "call_path": [item.get("function")] if item.get("function") else [],
                }
                for item in hotspots
                if isinstance(item, dict)
            ]
        go_depth = {
            "stack_samples": [
                {
                    "hot_frame": item.get("function"),
                    "sample_count": item.get("flat_bytes") or item.get("cum_bytes"),
                    "percent": item.get("flat_percent") or item.get("cum_percent") or 0,
                    "file": item.get("file"),
                    "line": item.get("line"),
                    "line_hint": f"{item.get('file')}:{item.get('line')}"
                    if item.get("file") and int(item.get("line") or 0) > 0
                    else "",
                    "call_path": str(item.get("function") or ""),
                }
                for item in hotspots
                if isinstance(item, dict)
            ],
            "line_candidates": go_heap_profile.get("line_candidates") or [],
            "context": {"collection_mode": "go_heap_profile"},
        }
        existing_depth = normalized.get("depth_evidence_json")
        if not isinstance(existing_depth, dict):
            existing_depth = {}
        for key, value in go_depth.items():
            if not existing_depth.get(key) and value:
                existing_depth[key] = value
        normalized["depth_evidence_json"] = existing_depth
    trace_profile = normalized.get("trace_endpoint_profile_json")
    if isinstance(trace_profile, dict):
        if not isinstance(normalized.get("top_json"), list) or not normalized.get("top_json"):
            normalized["top_json"] = trace_profile.get("top_functions") or []
        trace_depth = {
            "stack_samples": trace_profile.get("call_path_hotspots") or [],
            "context": trace_profile.get("target") or {},
            "call_path_hotspots": trace_profile.get("call_path_hotspots") or [],
        }
        existing_depth = normalized.get("depth_evidence_json")
        if not isinstance(existing_depth, dict):
            existing_depth = {}
        for key, value in trace_depth.items():
            if not existing_depth.get(key) and value:
                existing_depth[key] = value
        normalized["depth_evidence_json"] = existing_depth
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
    go_heap = values.get("go_heap_profile_json")
    if isinstance(go_heap, dict):
        summary = go_heap.get("summary") if isinstance(go_heap.get("summary"), dict) else {}
        validity = go_heap.get("evidence_validity") if isinstance(go_heap.get("evidence_validity"), dict) else {}
        results.append({
            "tool_name": "go_heap_profile",
            "profile_kind": go_heap.get("profile_kind"),
            "sample_type": go_heap.get("sample_type"),
            "hotspot_count": summary.get("hotspot_count"),
            "top_function": summary.get("top_function"),
            "top_file": summary.get("top_file"),
            "top_line": summary.get("top_line"),
            "evidence_status": validity.get("evidence_status"),
        })
    return results


def _dependency_targets(target_scope: dict[str, Any], source_service: str = "") -> list[dict[str, Any]]:
    dependencies = target_scope.get("dependency_targets") or []
    targets = []
    for item in dependencies:
        if not isinstance(item, dict):
            continue
        if source_service and item.get("source_service") != source_service:
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
            "source_service": item.get("source_service"),
            "target_service": item.get("target_service"),
            "topology_hop": item.get("topology_hop"),
        })
    return targets


def _source_context_for_target(target: dict[str, Any], target_scope: dict[str, Any]) -> dict[str, Any]:
    scoped = target_scope.get("source_context") if isinstance(target_scope.get("source_context"), dict) else {}
    local = target.get("source_context") if isinstance(target.get("source_context"), dict) else {}
    merged = {**scoped, **local}
    return {key: value for key, value in merged.items() if value not in (None, "", [])}


def _application_runtime_log_paths(
    target: dict[str, Any],
    target_scope: dict[str, Any],
    source_context: dict[str, Any],
) -> list[str]:
    paths: list[str] = []
    for holder in (target, target_scope, source_context):
        if not isinstance(holder, dict):
            continue
        for key in (
            "application_runtime_log_paths",
            "runtime_log_paths",
            "workload_log_paths",
            "log_paths",
        ):
            paths.extend(_string_list(holder.get(key)))
    return list(dict.fromkeys(path for path in paths if path))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


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


def _redis_target(target_scope: dict[str, Any], source_service: str = "") -> dict[str, Any]:
    for item in _dependency_targets(target_scope, source_service=source_service):
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
    else:
        target_config = dict(target_config)
    for key in ("ai_generated_query", "candidate_id", "object_type_hints"):
        value = collector_parameters.get(key)
        if value not in (None, "", []):
            target_config[key] = value
    return build_collector_invocation(
        scope_source="diagnosis_target_scope",
        collector_family=definition.runner_task_kind,
        probe_id=definition.probe_id,
        diagnosis_id=step["diagnosis_id"],
        diagnosis_step_id=step["step_id"],
        evidence_cohort_id=step["diagnosis_id"],
        target_config=target_config,
        target_context={
            "agent_id": target.get("agent_id"),
            "service_id": target.get("service_id"),
            "instance_id": target.get("instance_id"),
            "host_id": target.get("host_id"),
            "pid": target.get("pid"),
            "topology_hop": target.get("topology_hop"),
            "scope_role": target.get("scope_role"),
        },
    )


_INVESTIGATION_LEVEL_ORDER = {
    "resource": 0,
    "host": 1,
    "process": 2,
    "thread": 3,
    "syscall": 4,
    "dependency": 5,
    "service": 6,
    "endpoint": 7,
    "function": 8,
    "call_path": 9,
    "line": 10,
}


def _effective_investigation_level(
    assessment: dict[str, Any],
    session: dict[str, Any],
) -> str:
    """Use active AI base candidates as an input to existing follow-up planning."""
    analyzer_level = str(
        assessment.get("max_supported_level")
        or assessment.get("supported_level")
        or "resource"
    )
    best_level = analyzer_level
    tree = session.get("session_main") or session.get("controlled_ai_tree")
    if hasattr(tree, "model_dump"):
        tree = tree.model_dump(mode="json")
    if not isinstance(tree, dict):
        return best_level
    active_ids_value = session.get("active_ai_candidate_ids")
    active_ids_declared = isinstance(active_ids_value, list)
    active_ids = {
        str(value).strip()
        for value in (active_ids_value or [])
        if str(value).strip()
    }
    for layer in tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for node in (
            *(layer.get("primary_causes") or []),
            *(layer.get("secondary_causes") or []),
            *(layer.get("unknown_causes") or []),
        ):
            if not isinstance(node, dict):
                continue
            candidate_id = str(node.get("candidate_id") or "").strip()
            if active_ids_declared and candidate_id not in active_ids:
                continue
            if str(node.get("generated_by") or "") not in {"ai", "ai_candidate", "ai_guarded"}:
                continue
            if str(node.get("role") or "") == "rejected":
                continue
            if str(node.get("status") or "") in {"rejected", "contradicted", "forbidden"}:
                continue
            if str(node.get("depth_kind") or "") in {"mechanism", "boundary"}:
                continue
            if str(node.get("node_type") or "") in {
                "observation",
                "mechanism_explanation",
                "stop_boundary",
                "evidence_gap",
                "orphan",
            }:
                continue
            level = str(node.get("supported_level") or "").strip()
            if _INVESTIGATION_LEVEL_ORDER.get(level, -1) > _INVESTIGATION_LEVEL_ORDER.get(best_level, -1):
                best_level = level
    return best_level


def _assessment_followup_requests(assessment: dict[str, Any], session: dict[str, Any]) -> list[str]:
    effective_level = _effective_investigation_level(assessment, session)
    assessment = {
        **assessment,
        "effective_investigation_level": effective_level,
        "supported_level": effective_level,
        "max_supported_level": effective_level,
    }
    classification = assessment.get("classification")
    if classification == "runtime_stall":
        return ["runtime_control_history", "log_scan"]
    target_scope = session.get("target_scope", {}) if isinstance(session.get("target_scope"), dict) else {}
    intent = session.get("normalized_intent", {}) if isinstance(session.get("normalized_intent"), dict) else {}
    is_memory = str(intent.get("symptom") or "") == "memory_pressure" or str(assessment.get("mechanism") or "") in {
        "memory_leak",
        "python_memory_retention",
        "go_allocation_hotspot",
    } or classification in {
        "python_memory_retention",
        "go_heap_growth_candidate",
    }
    if is_memory:
        completed = set(session.get("completed_depth_evidence_gaps") or [])
        probe_status = session.get("probe_evidence_status") if isinstance(session.get("probe_evidence_status"), dict) else {}
        attempt_counts = session.get("probe_attempt_counts") if isinstance(session.get("probe_attempt_counts"), dict) else {}
        terminal_depth_statuses = {
            "valid", "partial", "blocked", "failed", "unavailable",
            "invalid", "empty_window", "unparseable", "target_exit",
        }
        if _is_go_target_scope(target_scope):
            for family in ("go_heap_profile", "source_snapshot"):
                status = str(probe_status.get(family) or "").lower()
                if family in completed or status in terminal_depth_statuses:
                    continue
                return [family]
            return []
        # A failed or blocked deep probe is terminal for that evidence family,
        # but it must not terminate the whole memory expansion chain.
        depth_status = {
            family: str(probe_status.get(family) or "").lower()
            for family in ("python_heap_profile", "python_runtime_profile", "source_snapshot")
        }
        for family in ("python_heap_profile", "python_runtime_profile", "source_snapshot"):
            if family in completed or depth_status[family] in terminal_depth_statuses:
                continue
            return [family]
        anchor = assessment.get("primary_anchor") if isinstance(assessment.get("primary_anchor"), dict) else {}
        source_ready = bool(
            anchor.get("source_context_hash")
            and anchor.get("source_revision")
            and anchor.get("file")
            and int(anchor.get("line") or 0) > 0
        )
        mechanism_status = str(probe_status.get("source_mechanism_query") or "").lower()
        mechanism_attempts = int(attempt_counts.get("source_mechanism_query") or 0)
        if (
            source_ready
            and "source_mechanism_query" not in completed
            and mechanism_status not in {"unavailable", "invalid"}
            and mechanism_attempts < MAX_SOURCE_MECHANISM_ATTEMPTS
        ):
            return ["source_mechanism_query"]
        if (
            source_ready
            and (
                probe_status.get("source_mechanism_query") == "valid"
                or mechanism_attempts >= MAX_SOURCE_MECHANISM_ATTEMPTS
            )
            and "python_heap_reference" not in completed
            and not probe_status.get("python_heap_reference")
        ):
            return ["python_heap_reference"]
        return []
    scenario_requests = _python_scenario_followup_requests(assessment, session)
    if scenario_requests is not None:
        return scenario_requests
    requests: list[str] = []
    probe_status = (
        session.get("probe_evidence_status")
        if isinstance(session.get("probe_evidence_status"), dict)
        else {}
    )
    completed_depth = set(session.get("completed_depth_evidence_gaps") or [])
    source_context = (
        target_scope.get("source_context")
        if isinstance(target_scope.get("source_context"), dict)
        else {}
    )
    instance_source_contexts = [
        item.get("source_context")
        for item in target_scope.get("instances", [])
        if isinstance(item, dict) and isinstance(item.get("source_context"), dict)
    ]
    source_available = bool(
        source_context.get("source_paths")
        or source_context.get("repo_revision")
        or target_scope.get("source_paths")
        or target_scope.get("repo_revision")
        or any(
            context.get("source_paths") or context.get("repo_revision")
            for context in instance_source_contexts
            if isinstance(context, dict)
        )
    )
    supported_level = str(
        assessment.get("max_supported_level")
        or assessment.get("supported_level")
        or ""
    )
    source_status = str(probe_status.get("source_snapshot") or "").lower()
    if (
        source_available
        and supported_level in {"function", "call_path", "line"}
        and "source_snapshot" not in completed_depth
        and source_status not in {
            "valid",
            "partial",
            "blocked",
            "failed",
            "unavailable",
            "invalid",
            "empty_window",
            "unparseable",
            "target_exit",
        }
    ):
        # This is the evidence bridge from a runtime/function observation to
        # a verified file:line anchor; it is not an answer supplied by runner.
        requests.append("source_snapshot")
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


def _python_scenario_followup_requests(assessment: dict[str, Any], session: dict[str, Any]) -> list[str] | None:
    """Route Python-only scenario expansion without changing conclusion gates."""
    target_scope = session.get("target_scope", {}) if isinstance(session.get("target_scope"), dict) else {}
    intent = session.get("normalized_intent", {}) if isinstance(session.get("normalized_intent"), dict) else {}
    classification = str(assessment.get("classification") or "")
    if classification == "downstream_dependency":
        return None
    token_text = " ".join(
        str(value or "")
        for value in (
            intent.get("symptom"),
            classification,
            assessment.get("mechanism"),
            assessment.get("symptom"),
            assessment.get("domain_type"),
        )
    ).lower()
    scenario_plan = _python_scenario_plan(token_text)
    if scenario_plan is None:
        return None

    completed = set(session.get("completed_depth_evidence_gaps") or [])
    probe_status = session.get("probe_evidence_status") if isinstance(session.get("probe_evidence_status"), dict) else {}
    terminal_statuses = {
        "valid", "partial", "blocked", "failed", "unavailable",
        "invalid", "empty_window", "unparseable", "target_exit",
    }
    for family in scenario_plan:
        if family == "source_snapshot" and not _source_context_available(target_scope):
            continue
        status = str(probe_status.get(family) or "").lower()
        if family not in completed and status not in terminal_statuses:
            return [family]
    return []


def _python_scenario_plan(token_text: str) -> list[str] | None:
    if any(token in token_text for token in ("python_cpu_hotspot", "self_code_regression", "cpu_saturation", "cpu hotspot")):
        return ["python_runtime_profile", "source_snapshot", "baseline_window_profile"]
    if any(token in token_text for token in ("python_endpoint_latency", "endpoint latency", "latency_increase", "request latency")):
        return ["trace_endpoint_profile", "dependency_check", "log_scan", "source_snapshot"]
    if any(token in token_text for token in ("python_io_blocking", "io_blocking", "io blocking", "blocking call", "io_degradation")):
        return ["off_cpu_wait_profile", "dependency_check", "source_snapshot"]
    if any(token in token_text for token in ("exception_storm", "exception storm", "traceback", "error storm")):
        return ["python_exception_profile", "source_snapshot"]
    if any(token in token_text for token in ("queue_backlog", "queue backlog", "celery", "rq", "worker backlog")):
        return ["python_queue_profile", "source_snapshot"]
    if any(token in token_text for token in ("pool_exhaustion", "pool exhaustion", "connection pool", "queuepool")):
        return ["python_pool_profile", "source_snapshot"]
    if any(token in token_text for token in ("retry_timeout", "retry timeout", "retry storm", "timeout config")):
        return ["python_retry_timeout_profile", "source_snapshot"]
    if any(token in token_text for token in ("cache_growth", "cache growth", "cache key", "cache grows", "cachedsession", "file cache")):
        return ["python_cache_profile", "source_snapshot"]
    if any(token in token_text for token in ("input_slow_path", "input slow", "groupby", "categorical", "cardinality", "input-triggered")):
        return ["python_input_profile", "python_runtime_profile", "source_snapshot"]
    if any(token in token_text for token in ("lock_wait", "lock wait", "lock contention", "runtime_contention")):
        return ["off_cpu_wait_profile", "python_lock_wait_profile", "source_snapshot"]
    return None


def _source_context_available(target_scope: dict[str, Any]) -> bool:
    source_context = (
        target_scope.get("source_context")
        if isinstance(target_scope.get("source_context"), dict)
        else {}
    )
    instances = target_scope.get("instances") if isinstance(target_scope.get("instances"), list) else []
    return bool(
        source_context.get("source_paths")
        or source_context.get("repo_revision")
        or target_scope.get("source_paths")
        or target_scope.get("repo_revision")
        or any(
            isinstance(item, dict)
            and isinstance(item.get("source_context"), dict)
            and (item["source_context"].get("source_paths") or item["source_context"].get("repo_revision"))
            for item in instances
        )
    )


def _line_probe_diagnostic(
    assessment: dict[str, Any],
    session: dict[str, Any],
    *,
    requested_requests: list[str],
    final_requests: list[str],
) -> dict[str, Any]:
    """Explain the existing source_snapshot decision without changing scheduling."""
    target_scope = session.get("target_scope", {}) if isinstance(session.get("target_scope"), dict) else {}
    source_context = target_scope.get("source_context")
    source_context = source_context if isinstance(source_context, dict) else {}
    instance_source_contexts = [
        item.get("source_context")
        for item in target_scope.get("instances", [])
        if isinstance(item, dict) and isinstance(item.get("source_context"), dict)
    ]
    source_available = bool(
        source_context.get("source_paths")
        or source_context.get("repo_revision")
        or target_scope.get("source_paths")
        or target_scope.get("repo_revision")
        or any(
            context.get("source_paths") or context.get("repo_revision")
            for context in instance_source_contexts
            if isinstance(context, dict)
        )
    )
    supported_level = str(
        assessment.get("effective_investigation_level")
        or
        assessment.get("max_supported_level")
        or assessment.get("supported_level")
        or "resource"
    )
    probe_status = session.get("probe_evidence_status")
    probe_status = probe_status if isinstance(probe_status, dict) else {}
    source_status = str(probe_status.get("source_snapshot") or "").lower()
    completed_depth = set(session.get("completed_depth_evidence_gaps") or [])
    anchor = assessment.get("primary_anchor")
    anchor = anchor if isinstance(anchor, dict) else {}
    runtime_line_candidates = [
        item
        for item in anchor.get("runtime_line_candidates", [])
        if isinstance(item, dict) and item.get("file") and int(_num(item.get("line"))) > 0
    ]
    source_snapshot_terminal = {
        "valid",
        "partial",
        "blocked",
        "failed",
        "unavailable",
        "invalid",
        "empty_window",
        "unparseable",
        "target_exit",
    }
    requested = "source_snapshot" in requested_requests or "source_snapshot" in final_requests
    already_completed = "source_snapshot" in completed_depth or source_status in source_snapshot_terminal
    verified_anchor = bool(
        anchor.get("source_context_hash")
        and anchor.get("source_revision")
        and anchor.get("file")
        and int(_num(anchor.get("line"))) > 0
        and runtime_line_candidates
    )
    if verified_anchor and not requested:
        status = "skipped"
        skip_reason = "already_verified"
    elif requested:
        status = "requested"
        skip_reason = ""
    elif already_completed:
        status = "completed" if source_status in {"valid", "partial"} else source_status
        skip_reason = "terminal_source_snapshot_status"
    elif not source_available:
        status = "skipped"
        skip_reason = "source_context_unavailable"
    elif supported_level not in {"function", "call_path", "line"}:
        status = "skipped"
        skip_reason = "supported_level_below_function"
    else:
        status = "not_scheduled"
        skip_reason = "source_snapshot_not_selected"
    return {
        "chain": [
            "runtime_or_analyzer_anchor",
            "source_snapshot",
            "line_anchor_eligibility",
        ],
        "status": status,
        "requested": requested,
        "requested_by": "assessment_followup" if "source_snapshot" in requested_requests else "",
        "skip_reason": skip_reason,
        "source_available": source_available,
        "supported_level": supported_level,
        "source_snapshot_status": source_status or "not_started",
        "source_snapshot_completed": already_completed,
        "runtime_line_candidate_count": len(runtime_line_candidates),
        "final_followup_requests": _unique_strings(final_requests),
        "eligibility_status": "verified" if verified_anchor else "not_verified",
    }


def _merge_assessment_followups(
    existing: list[str],
    assessment_requests: list[str],
    *,
    sufficient_dependency: bool,
    memory_only: bool = False,
) -> list[str]:
    if memory_only:
        return assessment_requests[:1]
    if assessment_requests and assessment_requests[0] in MEMORY_DEPTH_EVIDENCE_GAPS:
        return assessment_requests[:1]
    merged = list(existing)
    for request_id in assessment_requests:
        if sufficient_dependency and request_id in DEPENDENCY_EVIDENCE_GAPS:
            continue
        if request_id not in merged:
            merged.append(request_id)
    return merged


def _assessment_claim_metadata(
    *,
    classification: str,
    session: dict[str, Any],
    anchor: dict[str, Any],
    evidence_refs: list[str],
    downstream_dependency_failure: bool,
    shared_iowait: bool,
    neighbor_pressure: bool,
    runtime_control: dict[str, Any] | None,
) -> dict[str, Any]:
    scope = session.get("target_scope", {}) if isinstance(session.get("target_scope"), dict) else {}
    target_service = str(scope.get("target_service") or "目标服务")
    anchor_target = str(anchor.get("anchor") or anchor.get("instance_id") or anchor.get("service_id") or "").strip()

    if classification == "python_memory_retention":
        source_verified = bool(anchor.get("source_context_hash"))
        mechanism_paths = [
            item for item in (anchor.get("mechanism_paths") or [])
            if isinstance(item, dict)
            and item.get("candidate_relation") == "supports"
            and item.get("anchor_matches")
        ]
        runtime_paths = [
            item for item in (anchor.get("runtime_reference_paths") or [])
            if isinstance(item, dict) and item.get("nodes")
        ]
        source_mechanism_verified = bool(source_verified and mechanism_paths)
        retention_chain_verified = bool(source_mechanism_verified and runtime_paths)
        return {
            "claim_type": "direct_root_cause" if retention_chain_verified else "direct_failure_mechanism",
            "causal_status": "supported" if source_mechanism_verified else "unproven",
            "conclusion_eligible": bool(evidence_refs and retention_chain_verified),
            "eligibility_reason": (
                "Memray 证明持续对象保留，CodeQL 证明 revision 匹配的跨函数机制路径，PyHeap 证明与候选一致的实际入向引用链。"
                if retention_chain_verified
                else "Memray 与 CodeQL 已支持源码机制，但缺少 PyHeap 实际入向引用链，不能声明完整长期持有关系。"
                if source_mechanism_verified
                else "Memray 只证明持续对象保留和分配来源；源码快照中的手写 AST 路径仅为 partial_localization，缺少 CodeQL 机制路径。"
            ),
            "mechanism": "python_code_constant_retention" if retention_chain_verified else "python_memory_retention",
            "claim_target": anchor_target or target_service,
            "diagnostic_claim": _memory_retention_summary(anchor),
        }
    if classification == "go_heap_growth_candidate":
        source_verified = bool(anchor.get("source_context_hash"))
        return {
            "claim_type": "direct_failure_mechanism",
            "causal_status": "unproven",
            "conclusion_eligible": False,
            "eligibility_reason": (
                "Go heap pprof 热点和源码行已验证，但缺少对象保留链、增长趋势闭环或业务机制证据，不能声明完整内存泄漏根因。"
                if source_verified
                else "Go heap pprof 只证明分配/in-use 热点；源码 revision 尚未验证，且没有保留链或增长趋势闭环。"
            ),
            "mechanism": "go_allocation_hotspot",
            "claim_target": anchor_target or target_service,
            "diagnostic_claim": _go_heap_growth_summary(anchor),
        }
    if classification == "runtime_stall":
        instance = str(anchor.get("instance_id") or anchor.get("service_id") or target_service)
        direct = runtime_control is not None
        complete_source = bool(runtime_control and runtime_control.get("complete_source_chain"))
        control_refs = runtime_control.get("evidence_refs", []) if runtime_control else []
        return {
            "claim_type": (
                "complete_source_root_cause"
                if complete_source
                else "direct_root_cause" if direct else "direct_failure_mechanism"
            ),
            "causal_status": "supported",
            "conclusion_eligible": direct and bool(control_refs),
            "eligibility_reason": (
                "同窗来源证据、控制动作和停止态按时间有序，且目标完全匹配。"
                if complete_source
                else "同窗控制动作与目标完全匹配，且系统指标证明目标处于停止态；上游发起来源仍未知。"
                if direct
                else "系统指标只能证明 process_suspended 直接故障机制；缺少同窗 actor/action/target 控制事件，不能升级为完整根因。"
            ),
            "mechanism": "process_suspended",
            "claim_target": instance,
            "diagnostic_claim": _runtime_stall_summary(anchor, runtime_control),
            "runtime_control_event": runtime_control.get("event") if runtime_control else None,
            "source_provenance": runtime_control.get("source_provenance") if runtime_control else {},
            "origin_unknown": not complete_source,
        }

    if classification == "downstream_dependency" and downstream_dependency_failure:
        dependency = _dependency_root_entity(session, prefer_redis=True) or _dependency_root_entity(session) or "下游依赖"
        return {
            "claim_type": "root_cause",
            "causal_status": "supported",
            "conclusion_eligible": bool(evidence_refs),
            "eligibility_reason": "同窗依赖专项检查直接观测到不可达、高延迟或慢查询信号。",
            "mechanism": "downstream_dependency_failure",
            "claim_target": dependency,
            "diagnostic_claim": f"{dependency} 在异常窗口内发生依赖失败或严重延迟，导致 {target_service} 的请求被阻塞或失败。",
        }
    if classification == "host_resource_contention" and shared_iowait:
        host = _target_host(session) or "目标宿主机"
        return {
            "claim_type": "likely_root_cause",
            "causal_status": "supported",
            "conclusion_eligible": bool(evidence_refs),
            "eligibility_reason": "目标实例与同宿主实例在同窗内共同出现 I/O wait，符合共享资源争抢机制。",
            "mechanism": "shared_host_io_contention",
            "claim_target": host,
            "diagnostic_claim": f"{host} 的共享 I/O 资源争抢同时拖慢目标实例和同宿主实例。",
        }
    if classification == "same_host_noisy_neighbor" and neighbor_pressure:
        host = _target_host(session) or "目标宿主机"
        return {
            "claim_type": "likely_root_cause",
            "causal_status": "supported",
            "conclusion_eligible": bool(evidence_refs),
            "eligibility_reason": "同宿主其他实例压力显著强于目标实例，支持噪声邻居传播机制。",
            "mechanism": "same_host_noisy_neighbor",
            "claim_target": host,
            "diagnostic_claim": f"{host} 上其他实例的资源争用通过共享宿主资源拖慢 {target_service}。",
        }
    if classification == "self_code_or_process_pressure":
        primitive_kind = anchor.get("primitive_kind") or classify_primitive(anchor_target)
        reason = (
            "当前锚点是系统/运行时原语，只能作为等待观察，不能证明具体业务机制。"
            if primitive_kind
            else "当前证据定位了进程、函数或调用路径，但尚未证明该热点如何导致用户症状。"
        )
        return {
            "claim_type": "observation_only" if primitive_kind else "partial_localization",
            "causal_status": "unproven",
            "conclusion_eligible": False,
            "eligibility_reason": reason,
            "mechanism": "",
            "claim_target": anchor_target or target_service,
            "diagnostic_claim": _self_pressure_summary(anchor),
        }
    return {
        "claim_type": "abstention",
        "causal_status": "inconclusive",
        "conclusion_eligible": False,
        "eligibility_reason": "现有证据无法区分候选机制。",
        "mechanism": "",
        "claim_target": anchor_target or target_service,
        "diagnostic_claim": (
            "现有证据只能描述异常现象，尚不能形成根因结论；"
            f"当前可见锚点为 {anchor_target or '目标进程'}。"
        ),
    }


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
            "independent": True,
        })

    if classification != "self_code_or_process_pressure":
        if classification == "runtime_stall":
            add(
                "self_code_or_process_pressure",
                status="weakened",
                supported_level="process",
                reason="目标进程持续处于 stopped/tracing-stop 状态，代码热点或一般进程压力不是解释业务无进展所必需的机制。",
                evidence_refs=all_refs,
            )
        elif target_obs and not target_hot and not target_pressure:
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
    evidence_catalog: list[dict[str, Any]] | None = None,
    child_trees: list[dict[str, Any]],
    source_snapshot_hashes: list[str] | None = None,
    previous_tree: dict[str, Any] | None = None,
) -> ControlledAITree | None:
    """Build the user-facing AI tree from the session-level conclusion."""
    if not cluster_assessment and not candidates:
        return None

    # The initial tree contains Analyzer observations and localization
    # boundaries, not fallback conclusions. It is converted to
    # analyzer_fallback only when the complete first AI candidate round fails.
    generated_by = "analyzer_observation"
    # The Analyzer may describe a strong observation, but it is never the
    # source of a formal session candidate.  A later AI review must create
    # the candidate and the shared qualification gate must promote it.
    assessment_eligible = False
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
    line_refinement_nodes: list[AITreeCandidateNode] = []
    coarse_id = f"coarse_{cluster_assessment.get('classification') or 'assessment'}"
    explicit_source_parent_ids: dict[str, str] = {}

    seen_candidate_ids: set[str] = set()
    for item in candidates:
        candidate_id = str(item.get("candidate_id") or f"candidate_{len(primary_nodes) + len(secondary_nodes) + 1}")
        if candidate_id in seen_candidate_ids:
            continue
        seen_candidate_ids.add(candidate_id)
        candidate_eligible = False
        role = "unknown"
        target = str(item.get("root_entity") or cluster_assessment.get("claim_target") or "").strip()
        candidate_parent_ids = _unique_strings(item.get("parent_candidate_ids", []))
        candidate_parent_ids = [
            coarse_id if parent_id == "coarse_insufficient_evidence" else parent_id
            for parent_id in candidate_parent_ids
        ]
        candidate_origin_parent = str(
            item.get("origin_parent_candidate_id")
            or item.get("source_candidate_id")
            or _single_parent_id(candidate_parent_ids)
        ).strip()
        if candidate_origin_parent == "coarse_insufficient_evidence":
            candidate_origin_parent = coarse_id
        if candidate_origin_parent:
            explicit_source_parent_ids[candidate_id] = candidate_origin_parent
        if candidate_origin_parent and candidate_origin_parent not in candidate_parent_ids:
            candidate_parent_ids.append(candidate_origin_parent)
        candidate_relation = str(item.get("relation") or "").strip()
        if not candidate_relation:
            candidate_relation = "refinement" if candidate_parent_ids else "alternative"
        # Analyzer's calibrated candidate list is the explicit set of
        # independent alternatives for this emitted coarse assessment.
        # Preserve that root-level provenance instead of leaving the
        # candidate orphaned merely because the upstream rule candidate has
        # no parent field.
        if (
            not candidate_parent_ids
            and candidate_relation == "alternative"
            and item.get("independent") is True
        ):
            # Only an explicitly independent alternative may attach to the
            # emitted coarse root. Other unparented hints remain orphan/data
            # quality records instead of receiving invented provenance.
            candidate_parent_ids = [coarse_id]
            candidate_origin_parent = coarse_id
        node_evidence_refs = _unique_strings([
            *item.get("evidence_refs", []),
            *(evidence_refs if candidate_eligible and int(item.get("rank") or 999) == 1 else []),
        ])
        node = AITreeCandidateNode(
            candidate_id=candidate_id,
            generated_by="analyzer_observation",
            claim_origin="analyzer_diagnostic",
            lineage_id=candidate_id,
            relation=candidate_relation,
            parent_candidate_ids=candidate_parent_ids,
            origin_parent_candidate_id=candidate_origin_parent or None,
            role=role,
            claim=str(item.get("description") or cluster_assessment.get("summary") or candidate_id),
            supported_level=str(item.get("max_supported_level") or final_level),
            confidence=_confidence_from_label(item.get("confidence_level"), cluster_assessment.get("confidence")),
            status="supported",
            claim_type="partial_localization",
            causal_status="unproven",
            decision="continue_probe",
            mechanism="",
            target=target,
            evidence_refs=node_evidence_refs,
            self_challenge=AITreeSelfChallenge(
                why_this_claim=str(cluster_assessment.get("summary") or item.get("description") or ""),
                why_not_other_claims=_ruled_out_summary(cluster_assessment),
                supporting_evidence_refs=node_evidence_refs,
                opposing_evidence_refs=[],
                missing_evidence=followup_requests[:3],
                what_would_change_my_mind="同窗 dependency/log/trace 证据显示该依赖可达且错误仍然集中在目标进程代码热点。",
            ),
        )
        if role == "primary":
            primary_nodes.append(node)
        elif role == "secondary":
            secondary_nodes.append(node)
        else:
            unknown_nodes.append(node)

    has_non_observational_candidate = any(
        node.candidate_id not in OBSERVATION_CANDIDATE_IDS
        for node in [
            *primary_nodes,
            *secondary_nodes,
            *rejected_nodes,
            *unknown_nodes,
        ]
    )
    if not has_non_observational_candidate and cluster_assessment.get("classification") and not any(
        node.candidate_id == str(cluster_assessment.get("root_entity") or cluster_assessment.get("classification"))
        for node in [*secondary_nodes, *rejected_nodes, *unknown_nodes]
    ):
        candidate_id = str(cluster_assessment.get("root_entity") or cluster_assessment.get("classification"))
        fallback_role = "unknown"
        fallback_node = AITreeCandidateNode(
            candidate_id=candidate_id,
            generated_by="analyzer_observation",
            lineage_id=candidate_id,
            role=fallback_role,
            relation="alternative",
            parent_candidate_ids=[coarse_id],
            origin_parent_candidate_id=coarse_id,
            claim=str(cluster_assessment.get("diagnostic_claim") or cluster_assessment.get("summary") or candidate_id),
            supported_level=final_level,
            confidence=_num(cluster_assessment.get("confidence")),
            status="missing_evidence",
            claim_type="partial_localization",
            causal_status="unproven",
            decision="continue_probe",
            mechanism="",
            target=str(cluster_assessment.get("claim_target") or cluster_assessment.get("root_entity") or ""),
            primitive_kind=(cluster_assessment.get("primary_anchor") or {}).get("primitive_kind"),
            evidence_refs=evidence_refs,
            self_challenge=AITreeSelfChallenge(
                why_this_claim=str(cluster_assessment.get("summary") or ""),
                why_not_other_claims=_ruled_out_summary(cluster_assessment),
                supporting_evidence_refs=evidence_refs,
                missing_evidence=followup_requests[:3],
                what_would_change_my_mind="补充证据证明该依赖在异常同窗内健康，且另一个候选获得更强同窗证据。",
            ),
        )
        if fallback_role == "primary":
            primary_nodes.append(fallback_node)
        else:
            unknown_nodes.append(fallback_node)

    for item in cluster_assessment.get("ruled_out", []) or []:
        if not isinstance(item, dict):
            continue
        hypothesis = str(item.get("hypothesis") or "ruled_out")
        parent_id = str(
            item.get("origin_parent_candidate_id")
            or item.get("source_candidate_id")
            or item.get("parent_candidate_id")
            or ""
        ).strip()
        if parent_id == "coarse_insufficient_evidence":
            parent_id = coarse_id
        if not parent_id and item.get("independent") is True:
            parent_id = coarse_id
        rejected_nodes.append(AITreeCandidateNode(
            candidate_id=f"ruled_out_{hypothesis}",
            lineage_id=f"ruled_out_{hypothesis}",
            relation="rejected_alternative",
            parent_candidate_ids=[parent_id] if parent_id else [],
            origin_parent_candidate_id=parent_id,
            role="rejected",
            claim=str(item.get("reason") or hypothesis),
            supported_level=final_level,
            confidence=0.2,
            status="contradicted",
            claim_type="insufficient_for_root_cause",
            causal_status="contradicted",
            decision="reject_candidate",
            mechanism=hypothesis,
            target=str(cluster_assessment.get("claim_target") or ""),
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
        parent_id = str(
            item.get("origin_parent_candidate_id")
            or item.get("source_candidate_id")
            or item.get("parent_candidate_id")
            or ""
        ).strip()
        if parent_id == "coarse_insufficient_evidence":
            parent_id = coarse_id
        if not parent_id and item.get("independent") is True:
            parent_id = coarse_id
        node = AITreeCandidateNode(
            candidate_id=f"{role}_{hypothesis}",
            lineage_id=f"{role}_{hypothesis}",
            relation="alternative" if role != "rejected" else "rejected_alternative",
            parent_candidate_ids=[parent_id] if parent_id else [],
            origin_parent_candidate_id=parent_id,
            role=role,
            claim=str(item.get("reason") or hypothesis),
            supported_level=str(item.get("supported_level") or final_level),
            confidence=0.18 if role == "rejected" else 0.25,
            status="weakened" if role == "rejected" else "missing_evidence",
            claim_type="insufficient_for_root_cause" if role == "rejected" else "partial_localization",
            causal_status="contradicted" if role == "rejected" else "unproven",
            decision="reject_candidate" if role == "rejected" else "continue_probe",
            mechanism=hypothesis,
            target=str(cluster_assessment.get("claim_target") or ""),
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

    existing_node_ids = {
        node.candidate_id
        for node in [*primary_nodes, *secondary_nodes, *rejected_nodes, *unknown_nodes]
    }
    for prior_node in _unresolved_prior_ai_candidates(previous_tree):
        if prior_node.candidate_id not in existing_node_ids:
            unknown_nodes.append(prior_node.model_copy(update={
                "self_challenge": prior_node.self_challenge.model_copy(update={
                    "missing_evidence": _unique_strings([
                        *prior_node.self_challenge.missing_evidence,
                        *followup_requests,
                    ]),
                }),
            }))
            existing_node_ids.add(prior_node.candidate_id)

    primary_anchor = (
        cluster_assessment.get("primary_anchor")
        if isinstance(cluster_assessment.get("primary_anchor"), dict)
        else {}
    )
    primary_anchor_origin = str(
        primary_anchor.get("origin_parent_candidate_id") or ""
    ).strip()
    has_line_anchor_candidate = bool(
        final_level == "line"
        and primary_anchor.get("source_context_hash")
        and primary_anchor.get("source_revision")
        and primary_anchor.get("file")
        and int(primary_anchor.get("line") or 0) > 0
    )
    has_verified_line_anchor = bool(
        has_line_anchor_candidate
        and (
            primary_anchor.get("runtime_line_candidates")
            or cluster_assessment.get("conclusion_eligible")
        )
    )
    line_anchor_eligibility = _line_anchor_eligibility_summary(
        primary_anchor,
        has_verified_line_anchor=has_verified_line_anchor,
        source_snapshot_hashes=source_snapshot_hashes or [],
        origin_parent_candidate_id=primary_anchor_origin or coarse_id,
    )

    # A verified source line is itself a base localization node. Mechanism
    # paths may attach to it, but they must never use a service/function node
    # as an inferred substitute parent.
    base_line_nodes = {
        node.candidate_id: node
        for node in [*primary_nodes, *secondary_nodes, *rejected_nodes, *unknown_nodes]
        if (
            node.depth_kind == "base"
            and node.supported_level == "line"
            and node.candidate_id not in OBSERVATION_CANDIDATE_IDS
        )
    }
    if has_line_anchor_candidate:
        line_candidate_id = _verified_line_candidate_id(
            primary_anchor,
            str(cluster_assessment.get("classification") or "source"),
        )
        anchor_file = str(primary_anchor.get("file") or "").replace("\\", "/")
        anchor_label = f"{anchor_file}:{int(_num(primary_anchor.get('line')))}"
        matching_line_nodes = [
            node
            for node in base_line_nodes.values()
            if anchor_label in str(node.target or "") or anchor_label in str(node.claim or "")
        ]
        declared_path_origins = {
            str(path.get("origin_parent_candidate_id") or "").strip()
            for path in primary_anchor.get("mechanism_paths", [])
            if isinstance(path, dict)
        }
        declared_probe_origins = {
            str((probe.get("parameters") or {}).get("origin_parent_candidate_id") or "").strip()
            for probe in probes
            if isinstance(probe, dict)
        }
        declared_line_origins = declared_path_origins | declared_probe_origins
        matching_line_nodes.extend(
            node
            for node in base_line_nodes.values()
            if node.candidate_id in declared_line_origins and node not in matching_line_nodes
        )
        # Analyzer candidates can already be line-level while still carrying a
        # service-level ID. With one unambiguous line candidate, normalize that
        # ID instead of creating a second visual parent for the same source line.
        if not matching_line_nodes and len(base_line_nodes) == 1:
            matching_line_nodes = list(base_line_nodes.values())
        resolved_coarse_parent_id = _resolve_real_coarse_parent_id(
            "coarse_insufficient_evidence",
            coarse_id,
            {coarse_id, *[node.candidate_id for node in [*primary_nodes, *secondary_nodes, *rejected_nodes, *unknown_nodes]]},
        )
        line_node = next(
            (
                node for node in matching_line_nodes
                if node.candidate_id in declared_line_origins
            ),
            None,
        ) if declared_line_origins else None
        if line_node is None and len(matching_line_nodes) == 1 and not declared_line_origins:
            line_node = matching_line_nodes[0]
        if line_node is not None:
            if resolved_coarse_parent_id:
                line_node = line_node.model_copy(update={
                    "parent_candidate_ids": [resolved_coarse_parent_id],
                    "origin_parent_candidate_id": resolved_coarse_parent_id,
                    "relation": "refinement",
                })
            # The matching node came from one of the layer collections. Keep
            # the canonical line parent in the emitted tree as well as in the
            # lookup map; otherwise later mechanism/observation nodes see the
            # repaired parent while the frontend still receives the stale node.
            for collection in (primary_nodes, secondary_nodes, rejected_nodes, unknown_nodes):
                for index, existing in enumerate(collection):
                    if existing.candidate_id == line_node.candidate_id:
                        collection[index] = line_node
            line_refinement_nodes.append(line_node)
            for collection in (primary_nodes, secondary_nodes, rejected_nodes, unknown_nodes):
                collection[:] = [
                    existing
                    for existing in collection
                    if existing.candidate_id != line_node.candidate_id
                ]
            line_parent_id = line_node.candidate_id
            # The matched node is already the canonical source node. It may
            # itself be a child from an earlier round, so never copy it into a
            # synthetic parent or rewrite its claim/level. A later mechanism
            # node can use this stable ID as its parent.
            base_line_nodes[line_parent_id] = line_node
        else:
            line_refs = _unique_strings([
                *evidence_refs,
                *primary_anchor.get("source_evidence_refs", []),
                *primary_anchor.get("evidence_refs", []),
            ])
            source_parent_id = resolved_coarse_parent_id
            line_node = AITreeCandidateNode(
                candidate_id=line_candidate_id,
                generated_by="analyzer_observation",
                claim_origin="analyzer_diagnostic",
                lineage_id=line_candidate_id,
                relation="refinement",
                role="primary" if assessment_eligible else "unknown",
                claim=str(
                    cluster_assessment.get("diagnostic_claim")
                    or cluster_assessment.get("summary")
                    or f"已验证源码行 {primary_anchor.get('file')}:{int(_num(primary_anchor.get('line')))}"
                ),
                supported_level="line",
                confidence=_num(cluster_assessment.get("confidence")),
                status="supported" if assessment_eligible else "missing_evidence",
                claim_type="partial_localization",
                causal_status="unproven",
                decision="continue_probe",
                mechanism="",
                target=anchor_label,
                parent_candidate_ids=[source_parent_id],
                origin_parent_candidate_id=source_parent_id,
                conclusion_eligible=False,
                eligibility_reason="源码行只提供定位上下文，正式因果资格必须由 AI 候选和统一门禁产生。",
                evidence_refs=line_refs,
                self_challenge=AITreeSelfChallenge(
                    why_this_claim=str(cluster_assessment.get("summary") or "已验证源码行是当前基础定位。"),
                    supporting_evidence_refs=line_refs,
                    missing_evidence=followup_requests[:3],
                    what_would_change_my_mind="补证反驳该源码行，或同窗因果证据转向另一条基础定位。",
                ),
            )
            line_refinement_nodes.append(line_node)
            base_line_nodes[line_candidate_id] = line_node

    deep_origins = _deep_probe_provenance(probes)
    mechanism_paths = (
        primary_anchor.get("mechanism_paths") or []
        if has_verified_line_anchor
        else []
    )
    mechanism_nodes: list[AITreeCandidateNode] = []
    rejected_mechanism_nodes: list[AITreeCandidateNode] = []
    prior_mechanism_nodes = _prior_deep_candidates(previous_tree)
    for path in mechanism_paths:
        if not isinstance(path, dict) or not path.get("anchor_matches"):
            continue
        candidate_id = str(path.get("candidate_id") or "").strip()
        relation = str(path.get("candidate_relation") or "unknown")
        if not candidate_id or relation not in {"supports", "refutes", "inconclusive"}:
            continue
        mechanism_parent_id = str(
            path.get("origin_parent_candidate_id")
            or deep_origins.get(candidate_id)
            or ""
        ).strip()
        if not mechanism_parent_id:
            continue
        parent_node = base_line_nodes.get(mechanism_parent_id)
        if parent_node is None:
            # A source mechanism has no valid visual parent unless the recorded
            # origin is the verified line-level base node. Never infer one from
            # service/function order or from the first candidate in a layer.
            continue
        ref = str(path.get("evidence_ref") or "")
        if relation == "inconclusive":
            continue
        mechanism_candidate_id = (
            f"mechanism_{candidate_id}" if candidate_id in existing_node_ids else candidate_id
        )
        complete = bool(
            relation == "supports"
            and cluster_assessment.get("conclusion_eligible")
            and any(
                isinstance(runtime_path, dict)
                and runtime_path.get("candidate_id") == candidate_id
                for runtime_path in ((cluster_assessment.get("primary_anchor") or {}).get("runtime_reference_paths") or [])
            )
        )
        role = "rejected" if relation == "refutes" else "unknown"
        node = AITreeCandidateNode(
            candidate_id=mechanism_candidate_id,
            lineage_id=candidate_id,
            parent_candidate_ids=[mechanism_parent_id],
            origin_parent_candidate_id=mechanism_parent_id,
            role=role,
            claim=str(path.get("summary") or f"CodeQL mechanism path {candidate_id}"),
            # The source line is the verified base anchor. The path itself is
            # an explanatory call-chain layer, so it must not masquerade as a
            # second line-level primary conclusion.
            supported_level="call_path",
            confidence=0.8 if complete else 0.2 if role == "rejected" else 0.55,
            status="supported" if complete else "contradicted" if role == "rejected" else "missing_evidence",
            claim_type="direct_failure_mechanism" if complete else "insufficient_for_root_cause" if role == "rejected" else "partial_localization",
            causal_status="supported" if complete else "contradicted" if role == "rejected" else "inconclusive" if relation == "inconclusive" else "unproven",
            decision="continue_probe" if complete else "reject_candidate" if role == "rejected" else "continue_probe",
            mechanism=str(path.get("rule_id") or "codeql_source_mechanism"),
            target=str(cluster_assessment.get("claim_target") or ""),
            depth_kind="mechanism",
            conclusion_eligible=False,
            eligibility_reason=(
                "该节点只解释基础行级定位的持有机制，不单独进入主因或次因结论。"
                if complete
                else "CodeQL 路径反驳该候选，保留灰色分支供回放。"
                if role == "rejected"
                else "CodeQL 已支持源码机制，但仍需同候选 PyHeap 运行时引用链。"
            ),
            evidence_refs=_unique_strings([ref]),
            self_challenge=AITreeSelfChallenge(
                why_this_claim=str(path.get("summary") or "CodeQL 返回锚定的跨函数机制路径。"),
                supporting_evidence_refs=_unique_strings([ref]) if relation == "supports" else [],
                opposing_evidence_refs=_unique_strings([ref]) if relation == "refutes" else [],
                missing_evidence=[] if complete or role == "rejected" else ["python_heap_reference"],
                what_would_change_my_mind=(
                    "PyHeap 返回同 candidate_id 的实际入向引用链。"
                    if relation == "supports"
                    else "新的锚定 CodeQL path 与运行时引用链共同支持该候选。"
                ),
            ),
        )
        if role == "rejected":
            rejected_mechanism_nodes.append(node)
        else:
            mechanism_nodes.append(node)
        existing_node_ids.add(mechanism_candidate_id)

    for prior_node in prior_mechanism_nodes:
        if prior_node.candidate_id not in existing_node_ids and prior_node.origin_parent_candidate_id:
            mechanism_nodes.append(prior_node)
            existing_node_ids.add(prior_node.candidate_id)

    coarse_node = AITreeCandidateNode(
        candidate_id=coarse_id,
        lineage_id=coarse_id,
        cluster_id=coarse_id,
        branch_id=coarse_id,
        relation="root",
        node_type="cluster_root",
        role="unknown",
        claim=(
            f"目标服务出现{cluster_assessment.get('symptom') or '当前异常'}，"
            f"需要在{cluster_assessment.get('classification') or '多个候选方向'}之间继续核验。"
        ),
        supported_level="resource",
        confidence=min(_num(cluster_assessment.get("confidence")), 0.45),
        status="missing_evidence" if cluster_assessment.get("classification") else "unknown",
        claim_type="partial_localization",
        causal_status="unproven",
        decision="continue_probe",
        target=str(cluster_assessment.get("claim_target") or "目标服务"),
        evidence_refs=evidence_refs,
        self_challenge=AITreeSelfChallenge(
            why_this_claim=str(cluster_assessment.get("summary") or ""),
            why_not_other_claims=_ruled_out_summary(cluster_assessment),
            supporting_evidence_refs=evidence_refs,
            missing_evidence=[],
            what_would_change_my_mind="同窗依赖、日志或 trace 证据指向不同传播路径。",
        ),
    )
    # Classify observation/deep-context nodes before applying the root fallback.
    # Otherwise an observation that arrived without provenance is first made a
    # coarse child and can never be repaired into an orphan afterward.
    def _restore_verified_line_parent(node: AITreeCandidateNode) -> AITreeCandidateNode:
        source = base_line_nodes.get(node.candidate_id)
        if source is None or node.parent_candidate_ids or not source.parent_candidate_ids:
            return node
        return node.model_copy(update={
            "parent_candidate_ids": list(source.parent_candidate_ids),
            "origin_parent_candidate_id": source.origin_parent_candidate_id,
            "relation": "refinement",
            "node_type": "line_anchor",
        })

    primary_nodes = [_classify_tree_node(_restore_verified_line_parent(node)) for node in primary_nodes]
    secondary_nodes = [_classify_tree_node(_restore_verified_line_parent(node)) for node in secondary_nodes]
    rejected_nodes = [_classify_tree_node(_restore_verified_line_parent(node)) for node in rejected_nodes]
    unknown_nodes = [_classify_tree_node(_restore_verified_line_parent(node)) for node in unknown_nodes]
    # Runtime/userland hotspots can arrive in any Analyzer role collection.
    # Split them before the coarse-parent fallback so a valid line parent is
    # preserved instead of turning the observation into an orphan.
    observation_nodes: list[AITreeCandidateNode] = []
    split_collections: list[list[AITreeCandidateNode]] = []
    for collection in (primary_nodes, secondary_nodes, rejected_nodes, unknown_nodes):
        observations, base_nodes = _split_observation_context_nodes(collection)
        observation_nodes.extend(observations)
        split_collections.append(base_nodes)
    primary_nodes, secondary_nodes, rejected_nodes, unknown_nodes = split_collections
    primary_nodes = [
        _attach_coarse_parent_if_missing(node, coarse_id)
        for node in primary_nodes
    ]
    secondary_nodes = [
        _attach_coarse_parent_if_missing(node, coarse_id)
        for node in secondary_nodes
    ]
    rejected_nodes = [
        _attach_coarse_parent_if_missing(node, coarse_id)
        for node in rejected_nodes
    ]
    unknown_nodes = [
        _attach_coarse_parent_if_missing(node, coarse_id)
        for node in unknown_nodes
    ]
    observation_nodes = [
        _ensure_explicit_origin(
            node
            if node.parent_candidate_ids
            else _attach_observation_parent(
                node,
                explicit_source_parent_ids.get(node.candidate_id)
                or _observation_parent_from_verified_line(
                    node,
                    line_refinement_nodes,
                    primary_anchor,
                ),
            )
        )
        for node in observation_nodes
    ]
    layer0 = AITreeLayer(
        layer_id="session_layer_0_coarse_assessment",
        depth=0,
        generated_by=generated_by,
        summary="会话级 AI 树先形成粗粒度归因方向，再通过已完成探针证据收敛到基础定位。",
        unknown_causes=[coarse_node],
    )
    layer1 = AITreeLayer(
        layer_id="session_layer_1_supported_causes",
        depth=1,
        generated_by=generated_by,
        summary="已完成证据将粗候选收敛为基础定位；只有通过会话级门禁的节点才是主因或次因。",
        primary_causes=primary_nodes,
        secondary_causes=secondary_nodes,
        rejected_causes=rejected_nodes,
        unknown_causes=unknown_nodes,
    )

    layers = [layer0, layer1]
    line_refinement_layer_id = ""
    if line_refinement_nodes:
        line_refinement_layer_id = "session_layer_2_line_refinement"
        line_refinement_layer = AITreeLayer(
            layer_id=line_refinement_layer_id,
            depth=2,
            generated_by=generated_by,
            summary="已验证源码行只作为来源锚点和细化候选，不与 service/function 级粗候选并列。",
            primary_causes=[
                _classify_tree_node(_ensure_explicit_origin(node))
                for node in line_refinement_nodes
                if node.supported_level == "line" and node.conclusion_eligible
            ],
            secondary_causes=[],
            rejected_causes=[],
            unknown_causes=[
                _classify_tree_node(_ensure_explicit_origin(node))
                for node in line_refinement_nodes
                if not node.conclusion_eligible
            ],
        )
        layers.append(line_refinement_layer)
    observation_layer_id = ""
    if observation_nodes:
        observation_layer_id = "session_layer_3_observation_context" if line_refinement_nodes else "session_layer_2_observation_context"
        observation_layer = AITreeLayer(
            layer_id=observation_layer_id,
            depth=3 if line_refinement_nodes else 2,
            generated_by=generated_by,
            summary="采样热点、运行时栈和调用链只作为基础定位后的观察上下文，不单独进入正式主因。",
            unknown_causes=observation_nodes,
        )
        layers.append(observation_layer)
    mechanism_layer_id = ""
    if mechanism_nodes or rejected_mechanism_nodes:
        mechanism_depth = 4 if observation_nodes and line_refinement_nodes else 3 if observation_nodes or line_refinement_nodes else 2
        mechanism_layer_id = f"session_layer_{mechanism_depth}_mechanism_branch"
        layers.append(AITreeLayer(
            layer_id=mechanism_layer_id,
            depth=mechanism_depth,
            generated_by=generated_by,
            summary="机制链只解释已验证代码行后的对象调用或持有路径，不改变基础定位和最终结论资格。",
            rejected_causes=[_classify_tree_node(node) for node in rejected_mechanism_nodes],
            unknown_causes=[_classify_tree_node(node) for node in mechanism_nodes],
        ))
    lineage_quality_records = _tree_lineage_quality_records(layers)
    layers = _validate_tree_lineage(layers)
    retained_candidate_id = str(cluster_assessment.get("active_retained_candidate_id") or "")
    localization_chain = _tree_localization_chain(layers, retained_candidate_id)
    completed_probe_requests = _completed_session_probe_requests(probes)
    edges: list[AITreeProbeEdge] = [
        AITreeProbeEdge(
            edge_id="session_edge_coarse_to_supported_causes",
            from_layer_id=layer0.layer_id,
            to_layer_id=layer1.layer_id,
            from_candidate_ids=[coarse_id],
            to_candidate_ids=[
                node.candidate_id
                for node in [
                    *layer1.primary_causes,
                    *layer1.secondary_causes,
                    *layer1.rejected_causes,
                    *layer1.unknown_causes,
                ]
            ],
            probe_requests=completed_probe_requests or ["cluster_assessment"],
            probe_results=_probe_results_for_requests(completed_probe_requests, probes) if completed_probe_requests else [
                AITreeProbeResult(status="completed", evidence_refs=evidence_refs),
            ],
            status="completed",
            evidence_refs=evidence_refs,
            reuse_status="not_checked",
            effect="refined",
            transition_type="refine",
            reason="已完成的结构化证据把粗粒度候选收敛为当前会话级结论。",
        )
    ]
    emitted_candidate_ids = {
        node.candidate_id
        for layer in layers
        for node in _layer_nodes_for_validation(layer)
    }
    for rejected in layer1.rejected_causes:
        backtrack_target = str(rejected.origin_parent_candidate_id or "").strip()
        if not backtrack_target or backtrack_target not in emitted_candidate_ids:
            continue
        edges.append(AITreeProbeEdge(
            edge_id=f"session_backtrack_{rejected.candidate_id}_to_{backtrack_target}",
            from_layer_id=layer1.layer_id,
            to_layer_id=layer1.layer_id,
            from_candidate_ids=[rejected.candidate_id],
            to_candidate_ids=[backtrack_target],
            probe_requests=[],
            probe_results=[],
            status="completed",
            evidence_refs=_unique_strings(rejected.evidence_refs),
            effect="rollback",
            transition_type="backtrack",
            reason=(
                f"{rejected.candidate_id} 被反证后保留为灰色分支；"
                f"按其来源父节点回退到 {backtrack_target}。"
            ),
        ))
    source_hashes = sorted(set(_unique_strings(source_snapshot_hashes or [])))
    source_context_hash = source_hashes[0] if len(source_hashes) == 1 else None
    stop_reason = _session_tree_stop_reason(cluster_assessment, followup_requests, final_level)
    if len(source_hashes) > 1:
        stop_reason = "源码 revision 上下文冲突，禁止把多个 source_context_hash 合并为行级结论。"
        if final_level == "line":
            final_level = "function"
    blocked_upgrade_node = _blocked_upgrade_node(
        cluster_assessment,
        final_level,
        primary_anchor_origin or None,
    )
    boundary_nodes: list[AITreeCandidateNode] = []
    boundary_probe_candidates: dict[str, str] = {}
    local_stop_sources = [
        node for node in [*layer1.primary_causes, *layer1.secondary_causes]
        if node.depth_kind == "base" and node.status not in {"contradicted", "rejected"}
    ]
    if not has_verified_line_anchor and followup_requests:
        local_stop_sources = [
            node for node in local_stop_sources
            if node.supported_level != "line"
        ]
    if not local_stop_sources:
        local_stop_sources = [
            node for node in layer1.unknown_causes
            if node.depth_kind == "base"
            and node.node_type != "observation"
            and node.status not in {"contradicted", "rejected"}
        ][:1]
    if not has_verified_line_anchor and followup_requests:
        local_stop_sources = []
    if not local_stop_sources:
        local_stop_sources = [coarse_node]
    if not has_verified_line_anchor and followup_requests:
        local_stop_sources = []
    for source_node in local_stop_sources:
        stop_id = f"stop_boundary_{source_node.candidate_id}"
        boundary_nodes.append(_stop_boundary_node(
            candidate_id=stop_id,
            parent_candidate_id=source_node.candidate_id,
            supported_level=source_node.supported_level,
            stop_reason=stop_reason,
            status="supported",
            evidence_refs=source_node.evidence_refs,
        ))
    if followup_requests or blocked_upgrade_node is not None or boundary_nodes:
        for request_id in followup_requests:
            provenance_pairs = {
                (
                    str((probe.get("parameters") or {}).get("candidate_id") or "").strip(),
                    str((probe.get("parameters") or {}).get("origin_parent_candidate_id") or "").strip(),
                )
                for probe in probes
                if isinstance(probe, dict)
                and str((probe.get("parameters") or {}).get("evidence_gap") or "") == request_id
                and str((probe.get("parameters") or {}).get("candidate_id") or "").strip()
                and str((probe.get("parameters") or {}).get("origin_parent_candidate_id") or "").strip()
            }
            # A follow-up without persisted provenance is not a tree node. It
            # remains a scheduling/data-quality gap rather than an orphan that
            # the renderer could attach to an arbitrary parent.
            if not provenance_pairs:
                continue
            for candidate_id, origin_parent in sorted(provenance_pairs):
                node_id = f"gap_{request_id}" + (f"_{candidate_id}" if candidate_id else "")
                parent_ids = [origin_parent] if origin_parent else []
                boundary_nodes.append(AITreeCandidateNode(
                    candidate_id=node_id,
                    lineage_id=node_id,
                    cluster_id=origin_parent,
                    branch_id=origin_parent,
                    node_type="stop_boundary",
                    parent_candidate_ids=parent_ids,
                    origin_parent_candidate_id=origin_parent or None,
                    role="unknown",
                    claim=f"如果需要继续下钻，需要补充 {request_id}。",
                    supported_level=final_level,
                    confidence=0.25,
                    status=_node_status_for_requests([request_id], probes),
                    claim_type="partial_localization",
                    causal_status="inconclusive",
                    decision="backtrack",
                    depth_kind="boundary",
                    conclusion_eligible=False,
                    stop_reason=f"缺少或未完成 {request_id} 证据，当前分支回到来源父节点。",
                    blocked_probe=request_id,
                    self_challenge=AITreeSelfChallenge(
                        missing_evidence=[request_id],
                        what_would_change_my_mind=f"{request_id} 产生同窗结构化证据并改变主因排序。",
                    ),
                ))
                if candidate_id:
                    boundary_probe_candidates[node_id] = candidate_id
        if blocked_upgrade_node is not None:
            boundary_nodes.append(_classify_tree_node(blocked_upgrade_node))
        boundary_depth = 2 + int(bool(line_refinement_nodes)) + int(bool(observation_nodes)) + int(bool(mechanism_nodes or rejected_mechanism_nodes))
        layer2 = AITreeLayer(
            layer_id=f"session_layer_{boundary_depth}_remaining_evidence",
            depth=boundary_depth,
            generated_by=generated_by,
            summary="剩余补证或升级阻断只作为继续下钻的边界，不覆盖当前已支持的会话级主因。",
            unknown_causes=boundary_nodes,
        )
        layers.append(layer2)
        for node in local_stop_sources:
            stop_node_id = f"stop_boundary_{node.candidate_id}"
            stop_from_layer_id = (
                layer0.layer_id
                if node.candidate_id == coarse_id
                else line_refinement_layer.layer_id
                if node in line_refinement_nodes
                else layer1.layer_id
            )
            edges.append(AITreeProbeEdge(
                edge_id=f"session_stop_{node.candidate_id}",
                from_layer_id=stop_from_layer_id,
                to_layer_id=layer2.layer_id,
                from_candidate_ids=[node.candidate_id],
                to_candidate_ids=[stop_node_id],
                probe_requests=[],
                status="completed",
                evidence_refs=node.evidence_refs,
                reuse_status="not_checked",
                effect="no_change",
                transition_type="boundary",
                reason="当前分支已经停在证据支持的最细基础定位；STOP 只解释本分支边界。",
            ))
        for node in boundary_nodes:
            request_id = next(
                (request for request in followup_requests if node.candidate_id.startswith(f"gap_{request}")),
                None,
            )
            if not request_id or not node.parent_candidate_ids:
                continue
            edges.append(AITreeProbeEdge(
                edge_id=f"session_edge_{node.candidate_id}",
                from_layer_id=layer1.layer_id,
                to_layer_id=layer2.layer_id,
                from_candidate_ids=list(node.parent_candidate_ids),
                to_candidate_ids=[node.candidate_id],
                probe_requests=[request_id],
                probe_results=_probe_results_for_requests([request_id], probes),
                status=_edge_status_for_requests([request_id], probes),
                evidence_refs=evidence_refs,
                reuse_status="not_checked",
                effect="pending",
                transition_type="probe",
                reason="深探节点只保留明确来源父节点，失败时由该节点回退到同一来源。",
            ))
        if blocked_upgrade_node is not None and blocked_upgrade_node.parent_candidate_ids:
            edges.append(AITreeProbeEdge(
                edge_id="session_edge_blocked_line_upgrade",
                from_layer_id=line_refinement_layer_id or layer1.layer_id,
                to_layer_id=layer2.layer_id,
                from_candidate_ids=list(blocked_upgrade_node.parent_candidate_ids),
                to_candidate_ids=[blocked_upgrade_node.candidate_id],
                probe_requests=[],
                status="blocked",
                evidence_refs=evidence_refs,
                effect="no_change",
                transition_type="boundary",
                reason="行级升级缺少已验证的来源任务，只保留显式来源父节点。",
            ))
        for node in [*mechanism_nodes, *rejected_mechanism_nodes, *boundary_nodes]:
            origin = node.origin_parent_candidate_id
            probe_candidate_id = boundary_probe_candidates.get(node.candidate_id, node.candidate_id)
            failed_probe = _probe_failure_for_candidate(probe_candidate_id, probes)
            if not origin or not failed_probe:
                continue
            request_id = str((failed_probe.get("parameters") or {}).get("evidence_gap") or "")
            edges.append(AITreeProbeEdge(
                edge_id=f"rollback_{node.candidate_id}_to_{origin}",
                from_layer_id=layer2.layer_id if node in boundary_nodes else (
                    mechanism_layer_id
                    if node in [*mechanism_nodes, *rejected_mechanism_nodes]
                    else line_refinement_layer_id
                    if node in line_refinement_nodes
                    else layer1.layer_id
                ),
                to_layer_id=layer1.layer_id,
                from_candidate_ids=[node.candidate_id],
                to_candidate_ids=[origin],
                probe_requests=[request_id] if request_id else [],
                probe_results=_probe_results_for_requests([request_id], probes) if request_id else [],
                status=_rollback_edge_status(failed_probe),
                effect="rollback",
                transition_type="backtrack",
                reason=f"{node.candidate_id} 的深探未完成，精确回退到创建任务时记录的来源父节点 {origin}。",
            ))

    final_primary = [
        node.candidate_id
        for layer in layers
        for node in layer.primary_causes
    ]
    final_secondary = [
        node.candidate_id
        for layer in layers
        for node in layer.secondary_causes
    ]
    final_rejected = [node.candidate_id for node in layer1.rejected_causes]
    final_unknown = [
        node.candidate_id for layer in layers for node in layer.unknown_causes
    ]
    tree = ControlledAITree(
        tree_id=f"session_controlled_ai_tree_{hashlib.sha256(f'{diagnosis_id}:{final_primary}:{followup_requests}'.encode()).hexdigest()[:16]}",
        final_supported_level=final_level,
        emitted_coarse_ids=[coarse_id],
        coarse_aliases={
            "coarse_insufficient_evidence": coarse_id,
            coarse_id: coarse_id,
        },
        source_context_hash=source_context_hash,
        line_anchor_eligibility=line_anchor_eligibility,
        heap_probe_outcome=_heap_probe_outcome(
            probes,
            evidence_catalog=evidence_catalog or [],
        ),
        data_quality={
            "orphan_candidate_ids": [
                node.candidate_id
                for layer in layers
                for node in _layer_nodes_for_validation(layer)
                if node.node_type == "orphan"
            ],
            "records": [
                {
                    "candidate_id": node.candidate_id,
                    "status": "missing_provenance",
                    "relation": node.relation,
                    "parent_candidate_ids": list(node.parent_candidate_ids),
                    "claim": node.eligibility_reason or "节点缺少当前 session_main 中可验证的来源父节点。",
                }
                for layer in layers
                for node in _layer_nodes_for_validation(layer)
                if node.node_type == "orphan"
            ] + lineage_quality_records,
            "child_snapshot_count": len(child_trees),
        },
        stop_reason=stop_reason,
        stop_source_candidate_ids=[],
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
        retained_candidate_id=retained_candidate_id or None,
        localization_chain=localization_chain,
    )
    guarded_tree = enforce_conclusion_eligibility(tree)
    if guarded_tree is None:
        return None
    return guarded_tree.model_copy(update={"stop_source_candidate_ids": []})


def _attach_coarse_parent_if_missing(node: AITreeCandidateNode, coarse_id: str) -> AITreeCandidateNode:
    if "coarse_insufficient_evidence" in node.parent_candidate_ids:
        parent_ids = [
            coarse_id if parent_id == "coarse_insufficient_evidence" else parent_id
            for parent_id in node.parent_candidate_ids
        ]
        origin = (
            coarse_id
            if node.origin_parent_candidate_id == "coarse_insufficient_evidence"
            else node.origin_parent_candidate_id
        )
        return node.model_copy(update={
            "parent_candidate_ids": list(dict.fromkeys(parent_ids)),
            "origin_parent_candidate_id": origin,
        })
    if node.parent_candidate_ids:
        return node
    # Missing provenance is a data-quality boundary. Do not manufacture a
    # coarse edge from node type, rank, or layer order.
    return node.model_copy(update={
        "node_type": "orphan",
        "eligibility_reason": (
            node.eligibility_reason
            or f"节点缺少显式来源父节点，未自动挂到 {coarse_id}。"
        ),
    })


def _single_parent_id(parent_ids: list[str]) -> str:
    unique = _unique_strings(parent_ids)
    return unique[0] if len(unique) == 1 else ""


def _ensure_explicit_origin(node: AITreeCandidateNode) -> AITreeCandidateNode:
    """Keep explicit provenance and never choose a global or ranked parent."""
    parent_ids = _unique_strings(node.parent_candidate_ids)
    origin = str(node.origin_parent_candidate_id or "").strip()
    if origin and origin in parent_ids:
        return node.model_copy(update={
            "parent_candidate_ids": parent_ids,
            "origin_parent_candidate_id": origin,
        })
    if len(parent_ids) == 1:
        return node.model_copy(update={
            "parent_candidate_ids": parent_ids,
            "origin_parent_candidate_id": _single_parent_id(parent_ids),
        })
    return node.model_copy(update={
        "parent_candidate_ids": parent_ids,
        "origin_parent_candidate_id": None,
        "node_type": "orphan",
        "conclusion_eligible": False,
        "eligibility_reason": node.eligibility_reason or "节点缺少唯一来源父节点，未自动接入主树。",
    })


def _tree_localization_chain(
    layers: list[AITreeLayer],
    retained_candidate_id: str,
) -> list[CausalExplanationStep]:
    nodes = {
        node.candidate_id: node
        for layer in layers
        for node in _layer_nodes_for_validation(layer)
    }
    current_id = retained_candidate_id
    ordered: list[AITreeCandidateNode] = []
    visited: set[str] = set()
    while current_id and current_id not in visited and current_id in nodes:
        visited.add(current_id)
        node = nodes[current_id]
        ordered.append(node)
        origin = str(node.origin_parent_candidate_id or "").strip()
        current_id = origin if origin in nodes else ""
    ordered.reverse()
    if ordered:
        children_by_parent: dict[str, list[str]] = {}
        for node in nodes.values():
            origin = str(node.origin_parent_candidate_id or "").strip()
            if origin:
                children_by_parent.setdefault(origin, []).append(node.candidate_id)
        line_path: list[AITreeCandidateNode] | None = None
        queue: list[tuple[str, list[AITreeCandidateNode]]] = [
            (ordered[-1].candidate_id, [ordered[-1]])
        ]
        visited_descendants: set[str] = set()
        while queue:
            parent_id, path = queue.pop(0)
            for child_id in sorted(children_by_parent.get(parent_id, [])):
                if child_id in visited_descendants or child_id not in nodes:
                    continue
                visited_descendants.add(child_id)
                child = nodes[child_id]
                child_path = [*path, child]
                if (
                    child.generated_by in {"ai", "ai_candidate", "ai_guarded"}
                    and child.node_type == "line_anchor"
                    and child.supported_level == "line"
                    and child.claim_transform == "refined"
                ):
                    line_path = child_path
                queue.append((child_id, child_path))
        if line_path:
            ordered.extend(line_path[1:])
    return [
        CausalExplanationStep(
            step_id=f"localization_{node.candidate_id}",
            candidate_id=node.candidate_id,
            statement=node.claim,
            evidence_refs=_unique_strings(node.evidence_refs),
            claim_status=node.claim_status,
            supported_level=node.supported_level,
        )
        for node in ordered
        if node.claim.strip()
    ]


def _resolve_real_coarse_parent_id(
    candidate_id: str,
    emitted_coarse_id: str,
    emitted_candidate_ids: set[str] | None = None,
) -> str:
    candidate_id = str(candidate_id or "").strip()
    emitted_ids = {
        str(value).strip()
        for value in (emitted_candidate_ids or {emitted_coarse_id})
        if str(value).strip()
    }
    if candidate_id in emitted_ids:
        return candidate_id
    if candidate_id.startswith("coarse_") or not candidate_id:
        return str(emitted_coarse_id or "").strip()
    return ""


def _split_observation_context_nodes(
    nodes: list[AITreeCandidateNode],
) -> tuple[list[AITreeCandidateNode], list[AITreeCandidateNode]]:
    observation_nodes: list[AITreeCandidateNode] = []
    base_nodes: list[AITreeCandidateNode] = []
    for node in nodes:
        if node.candidate_id in OBSERVATION_CANDIDATE_IDS:
            observation_nodes.append(node.model_copy(update={
                "node_type": "observation",
                "claim_type": "observation_only",
                "conclusion_eligible": False,
                "eligibility_reason": "该节点只是采样热点或运行时栈观察，必须挂在基础定位之后作为上下文。",
            }))
        else:
            base_nodes.append(node)
    return observation_nodes, base_nodes


def _observation_parent_from_verified_line(
    node: AITreeCandidateNode,
    line_nodes: list[AITreeCandidateNode],
    anchor: dict[str, Any] | None,
) -> str:
    """Attach an unbound observation only when its evidence cohort names one line."""
    if len(line_nodes) != 1:
        return ""
    observation_refs = set(_unique_strings(node.evidence_refs))
    if not observation_refs:
        return ""
    anchor = anchor if isinstance(anchor, dict) else {}
    line_node = line_nodes[0]
    cohort_refs = set(_unique_strings([
        *line_node.evidence_refs,
        *anchor.get("evidence_refs", []),
        *anchor.get("source_evidence_refs", []),
        *anchor.get("runtime_source_evidence_refs", []),
        *[
            item.get("evidence_ref")
            for item in anchor.get("runtime_line_candidates", [])
            if isinstance(item, dict)
        ],
    ]))
    return line_node.candidate_id if observation_refs & cohort_refs else ""


def _attach_observation_parent(node: AITreeCandidateNode, parent_id: str) -> AITreeCandidateNode:
    if not parent_id:
        return node
    return node.model_copy(update={
        "parent_candidate_ids": [parent_id],
        "origin_parent_candidate_id": parent_id,
        "cluster_id": parent_id,
        "branch_id": parent_id,
        "relation": "evidence_context",
        "node_type": "observation",
        "claim_type": "observation_only",
        "conclusion_eligible": False,
        "decision": "continue_probe",
    })


def _validate_tree_lineage(layers: list[AITreeLayer]) -> list[AITreeLayer]:
    """Validate explicit DAG edges and rebuild their reverse child links."""
    emitted_ids = {
        node.candidate_id
        for layer in layers
        for node in _layer_nodes_for_validation(layer)
    }
    occurrences: dict[str, int] = {}
    for layer in layers:
        for node in _layer_nodes_for_validation(layer):
            occurrences[node.candidate_id] = occurrences.get(node.candidate_id, 0) + 1
    duplicates = [candidate_id for candidate_id, count in occurrences.items() if count > 1]
    if duplicates:
        raise ValueError(f"DAG candidate_id 重复发出: {', '.join(sorted(duplicates))}")
    parents_by_child: dict[str, list[str]] = {}
    validated: list[AITreeLayer] = []
    for layer in layers:
        groups: dict[str, list[AITreeCandidateNode]] = {}
        for group_name in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            nodes: list[AITreeCandidateNode] = []
            for node in getattr(layer, group_name):
                declared_parent_ids = _unique_strings(node.parent_candidate_ids)
                missing_parent_ids = [
                    parent for parent in declared_parent_ids
                    if parent not in emitted_ids
                ]
                parent_ids = [
                    parent for parent in declared_parent_ids
                    if parent in emitted_ids
                ]
                declared_origin = str(node.origin_parent_candidate_id or "").strip()
                origin = declared_origin if declared_origin in emitted_ids else None
                missing_origin = (
                    node.relation != "root"
                    and bool(declared_parent_ids)
                    and origin is None
                )
                if node.relation != "root" and (
                    not parent_ids
                    or missing_parent_ids
                    or missing_origin
                    or len(parent_ids) > 1 and origin is None
                ):
                    missing_detail = _unique_strings([
                        *missing_parent_ids,
                        "origin_parent_candidate_id"
                        if missing_origin
                        else "",
                    ])
                    node = node.model_copy(update={
                        "node_type": "orphan",
                        "parent_candidate_ids": declared_parent_ids,
                        "origin_parent_candidate_id": None,
                        "relation": node.relation,
                        "conclusion_eligible": False,
                        "claim_type": "abstention" if node.depth_kind == "base" else "partial_localization",
                        "causal_status": node.causal_status if node.depth_kind == "base" else "inconclusive",
                        "decision": node.decision if node.depth_kind == "base" else "abstain",
                        "eligibility_reason": (
                            "缺少指向当前树已发出节点的显式来源父节点："
                            + "、".join(missing_detail)
                        ),
                    })
                else:
                    if origin is None and len(parent_ids) == 1:
                        origin = _single_parent_id(parent_ids)
                    node = node.model_copy(update={
                        "parent_candidate_ids": parent_ids,
                        "origin_parent_candidate_id": origin,
                    })
                    for parent_id in parent_ids:
                        if parent_id == node.candidate_id:
                            raise ValueError(f"DAG 节点不能指向自身: {node.candidate_id}")
                        parents_by_child.setdefault(node.candidate_id, []).append(parent_id)
                nodes.append(node)
            groups[group_name] = nodes
        validated.append(layer.model_copy(update=groups))
    canonical = {
        node.candidate_id: node
        for layer in validated
        for node in _layer_nodes_for_validation(layer)
    }
    children_by_parent: dict[str, list[str]] = {}
    for child_id, parent_ids in parents_by_child.items():
        for parent_id in parent_ids:
            children_by_parent.setdefault(parent_id, []).append(child_id)
    for child_id, parent_ids in parents_by_child.items():
        canonical[child_id] = canonical[child_id].model_copy(update={
            "parent_candidate_ids": _unique_strings(parent_ids),
        })
    for parent_id, parent_node in canonical.items():
        canonical[parent_id] = parent_node.model_copy(update={
            "child_candidate_ids": _unique_strings(children_by_parent.get(parent_id, [])),
        })
    # Validate the resulting directed graph instead of relying on layer order.
    adjacency = {candidate_id: list(node.child_candidate_ids) for candidate_id, node in canonical.items()}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(candidate_id: str) -> None:
        if candidate_id in visiting:
            raise ValueError(f"DAG lineage 存在环: {candidate_id}")
        if candidate_id in visited:
            return
        visiting.add(candidate_id)
        for child_id in adjacency.get(candidate_id, []):
            if child_id not in canonical:
                raise ValueError(f"DAG child_candidate_id 不存在: {child_id}")
            visit(child_id)
        visiting.remove(candidate_id)
        visited.add(candidate_id)

    for candidate_id in canonical:
        visit(candidate_id)
    return [
        layer.model_copy(update={
            group: [canonical[node.candidate_id] for node in getattr(layer, group)]
            for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes")
        })
        for layer in validated
    ]


def _tree_lineage_quality_records(layers: list[AITreeLayer]) -> list[dict[str, Any]]:
    emitted_ids = {
        node.candidate_id
        for layer in layers
        for node in _layer_nodes_for_validation(layer)
    }
    records: list[dict[str, Any]] = []
    for layer in layers:
        for node in _layer_nodes_for_validation(layer):
            missing_children = [
                child_id
                for child_id in _unique_strings(node.child_candidate_ids)
                if child_id not in emitted_ids
            ]
            if not missing_children:
                continue
            records.append({
                "candidate_id": node.candidate_id,
                "status": "missing_child",
                "relation": node.relation,
                "child_candidate_ids": missing_children,
                "claim": "节点声明的 child_candidate_ids 未在当前 session_main 中发出，已从主树布局边中移除。",
            })
    return records


def _layer_nodes_for_validation(layer: AITreeLayer) -> list[AITreeCandidateNode]:
    return [
        *layer.primary_causes,
        *layer.secondary_causes,
        *layer.rejected_causes,
        *layer.unknown_causes,
    ]


def _candidate_generation_output(review: dict[str, Any] | None) -> dict[str, Any]:
    """Expose bounded first-round model output and the evidence it saw."""
    if not isinstance(review, dict):
        return {
            "status": "not_started",
            "attempts": [],
            "candidate_count": 0,
            "candidate_ids": [],
            "last_response_excerpt": "",
            "accepted_candidate_ids": [],
            "accepted_candidates": [],
            "rejected_candidate_ids": [],
            "active_candidate_ids": [],
            "deferred_candidate_ids": [],
            "selection_diagnostics": [],
            "tree_ingestion_diagnostics": [],
            "line_refinement_diagnostics": [],
            "validation_diagnostics": [],
            "selected_evidence_families": [],
            "initial_evidence_context": {},
        }
    proposals = review.get("candidate_proposals")
    proposals = proposals if isinstance(proposals, list) else []
    attempts = review.get("candidate_generation_attempts")
    attempts = attempts if isinstance(attempts, list) else []
    initial_context = review.get("initial_evidence_context")
    initial_context = initial_context if isinstance(initial_context, dict) else {}
    validation_diagnostics = review.get("validation_diagnostics")
    validation_diagnostics = validation_diagnostics if isinstance(validation_diagnostics, list) else []
    accepted_ids = [
        str(item.get("candidate_id"))
        for item in proposals
        if isinstance(item, dict) and item.get("candidate_id")
    ]
    accepted_candidates = []
    for item in proposals:
        if not isinstance(item, dict) or not item.get("candidate_id"):
            continue
        accepted_candidates.append({
            "candidate_id": str(item.get("candidate_id")),
            "claim": str(item.get("claim") or "")[:1000],
            "mechanism": str(item.get("mechanism") or "")[:240],
            "target": str(item.get("target") or "")[:240],
            "role": str(item.get("role") or "unknown"),
            "relation": str(item.get("relation") or ""),
            "supported_level": str(item.get("supported_level") or "resource"),
            "decision": str(item.get("decision") or ""),
            "causal_status": str(item.get("causal_status") or ""),
            "evidence_refs": _unique_strings(item.get("evidence_refs", []))[:128],
            "missing_evidence": _unique_strings(item.get("missing_evidence", []))[:32],
            "parent_candidate_ids": _unique_strings(item.get("parent_candidate_ids", []))[:32],
            "origin_parent_candidate_id": str(item.get("origin_parent_candidate_id") or ""),
            "probe_requests": _unique_strings(item.get("probe_requests", []))[:16],
            "probe_request_specs": [
                spec.model_dump(mode="json")
                for spec in _probe_request_specs(item.get("probe_request_specs"))
            ][:16],
        })
    rejected_ids = [
        str(item.get("candidate_id"))
        for item in validation_diagnostics
        if isinstance(item, dict) and item.get("candidate_id")
    ]
    selection_diagnostics = review.get("candidate_selection_diagnostics")
    if not isinstance(selection_diagnostics, list):
        selection_diagnostics = review.get("selection_diagnostics")
    if not isinstance(selection_diagnostics, list):
        selection_diagnostics = []
    gate_failures = review.get("gate_failures")
    if not isinstance(gate_failures, list):
        gate_failures = review.get("ai_gate_failures")
    if not isinstance(gate_failures, list):
        gate_failures = []
    return {
        "status": str(review.get("ai_review_status") or "unknown"),
        "error": str(review.get("ai_review_error") or "")[:500],
        "attempts": attempts,
        "candidate_count": len(accepted_candidates),
        "candidate_ids": [
            item["candidate_id"]
            for item in accepted_candidates
        ],
        "last_response_excerpt": str(
            attempts[-1].get("response_excerpt") or ""
        )[:1200] if attempts and isinstance(attempts[-1], dict) else "",
        "accepted_candidate_ids": accepted_ids,
        "accepted_candidates": accepted_candidates,
        "rejected_candidate_ids": list(dict.fromkeys(rejected_ids)),
        "active_candidate_ids": [
            str(value)
            for value in (review.get("active_candidate_ids") or [])
            if str(value)
        ],
        "deferred_candidate_ids": [
            str(value)
            for value in (review.get("deferred_candidate_ids") or [])
            if str(value)
        ],
        "selection_diagnostics": list(selection_diagnostics),
        "tree_ingestion_diagnostics": list(
            review.get("tree_ingestion_diagnostics") or []
        ),
        "line_refinement_diagnostics": list(
            review.get("line_refinement_diagnostics") or []
        ),
        "validation_diagnostics": validation_diagnostics,
        "gate_failures": list(gate_failures),
        "selected_evidence_families": [
            str(value)
            for value in (review.get("selected_evidence_families") or [])
            if str(value)
        ],
        "probe_request_specs": [
            spec.model_dump(mode="json")
            for spec in _probe_request_specs(review.get("probe_requests"))
        ][:16],
        "initial_evidence_context": initial_context,
    }


def _conclusion_observations(task_observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep a compact, answer-free summary of the evidence used for gating."""
    result: list[dict[str, Any]] = []
    for item in task_observations:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary")
        if isinstance(summary, (dict, list)):
            summary = json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str)
        result.append({
            "task_id": str(item.get("task_id") or ""),
            "collector_type": str(item.get("collector_type") or ""),
            "status": str(item.get("status") or item.get("evidence_status") or "completed"),
            "evidence_status": str(item.get("evidence_status") or ""),
            "summary": str(summary or "")[:800],
            "evidence_refs": _unique_strings(item.get("evidence_refs", []))[:64],
            "top_function": item.get("top_function") if isinstance(item.get("top_function"), dict) else {},
            "specific_anchor": item.get("specific_anchor") if isinstance(item.get("specific_anchor"), dict) else {},
        })
    return result


def _conclusion_boundaries(
    tree: dict[str, Any] | None,
    qualification_boundary: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return local boundary nodes plus the session qualification boundary."""
    boundaries: list[dict[str, Any]] = []
    if isinstance(qualification_boundary, dict) and qualification_boundary.get("status") not in {None, "none"}:
        boundaries.append({
            "kind": "qualification",
            **qualification_boundary,
        })
    if not isinstance(tree, dict):
        return boundaries
    for layer in tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            for node in layer.get(group, []):
                if not isinstance(node, dict):
                    continue
                if node.get("depth_kind") != "boundary" and node.get("node_type") not in {
                    "stop_boundary", "evidence_gap", "orphan",
                }:
                    continue
                boundaries.append({
                    "kind": "tree_boundary",
                    "candidate_id": str(node.get("candidate_id") or ""),
                    "parent_candidate_ids": _unique_strings(node.get("parent_candidate_ids", [])),
                    "origin_parent_candidate_id": node.get("origin_parent_candidate_id"),
                    "status": node.get("status"),
                    "blocked_probe": node.get("blocked_probe"),
                    "claim": str(node.get("claim") or "")[:500],
                    "reason": str(node.get("stop_reason") or node.get("eligibility_reason") or "")[:500],
                    "evidence_refs": _unique_strings(node.get("evidence_refs", []))[:64],
                })
    return boundaries


def _retained_parent_conclusions(
    tree: dict[str, Any] | None,
    retained: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Describe the retained node and its real ancestor chain."""
    if not isinstance(retained, dict) or not retained.get("candidate_id"):
        return []
    nodes: dict[str, dict[str, Any]] = {}
    if isinstance(tree, dict):
        for layer in tree.get("layers", []):
            if not isinstance(layer, dict):
                continue
            for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
                for node in layer.get(group, []):
                    if isinstance(node, dict) and node.get("candidate_id"):
                        nodes.setdefault(str(node["candidate_id"]), node)
    chain: list[dict[str, Any]] = []
    current_id = str(retained.get("candidate_id") or "")
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        node = nodes.get(current_id)
        if node is None:
            if current_id == str(retained.get("candidate_id")):
                chain.append(dict(retained))
            break
        chain.append({
            "candidate_id": current_id,
            "claim": str(node.get("claim") or "")[:800],
            "supported_level": node.get("supported_level"),
            "status": node.get("status"),
            "qualification": retained.get("qualification") if current_id == str(retained.get("candidate_id")) else "observation",
            "evidence_refs": _unique_strings(node.get("evidence_refs", []))[:64],
            "origin_parent_candidate_id": node.get("origin_parent_candidate_id"),
            "retained": current_id == str(retained.get("candidate_id")),
            "inherited": bool(retained.get("inherited")) if current_id == str(retained.get("candidate_id")) else False,
            "fallback_mode": retained.get("fallback_mode") if current_id == str(retained.get("candidate_id")) else "none",
        })
        current_id = str(node.get("origin_parent_candidate_id") or "")
    return list(reversed(chain))


def _classify_tree_node(node: AITreeCandidateNode) -> AITreeCandidateNode:
    declared_relation = node.relation
    has_declared_parent = bool(node.parent_candidate_ids or node.origin_parent_candidate_id)
    if node.node_type == "observation":
        node_type = "observation"
        relation = "evidence_context"
    elif node.depth_kind == "mechanism":
        node_type = "mechanism_explanation"
        relation = "mechanism"
    elif node.depth_kind == "boundary":
        node_type = node.node_type if node.node_type in {"stop_boundary", "evidence_gap"} else "stop_boundary"
        relation = "boundary"
    elif node.role == "rejected" or node.status in {"contradicted", "rejected"}:
        node_type = "rejected_candidate"
        relation = "rejected_alternative"
    elif node.supported_level == "line":
        node_type = "line_anchor"
        relation = "refinement" if has_declared_parent or declared_relation == "refinement" else "alternative"
    elif node.supported_level == "call_path":
        node_type = "call_path_context"
        relation = "refinement" if has_declared_parent or declared_relation == "refinement" else "alternative"
    elif node.status in {"missing_evidence", "unknown"} and not node.conclusion_eligible:
        node_type = "coarse_candidate"
        relation = "alternative"
    else:
        node_type = "base_cause"
        relation = node.relation if node.relation in {"root", "alternative", "refinement", "evidence_context", "mechanism", "boundary", "rejected_alternative"} else "alternative"
    origin = node.origin_parent_candidate_id or ""
    if (
        not origin
        and declared_relation in {"refinement", "evidence_context", "mechanism", "boundary"}
        and node_type not in {"line_anchor", "call_path_context"}
    ):
        node_type = "orphan"
        relation = node.relation
    elif not origin and node.depth_kind == "base" and node_type in {
        "base_cause", "coarse_candidate", "line_anchor", "call_path_context"
    }:
        relation = "alternative"
    return node.model_copy(update={
        "node_type": node_type,
        "relation": relation,
        "cluster_id": node.cluster_id or origin or node.candidate_id,
        "branch_id": node.branch_id or node.candidate_id,
    })


def _stop_boundary_node(
    *,
    candidate_id: str,
    parent_candidate_id: str,
    supported_level: str,
    stop_reason: str,
    status: str,
    evidence_refs: list[str],
    blocked_probe: str = "",
) -> AITreeCandidateNode:
    return AITreeCandidateNode(
        candidate_id=candidate_id,
        lineage_id=candidate_id,
        cluster_id=parent_candidate_id,
        branch_id=parent_candidate_id,
        parent_candidate_ids=[parent_candidate_id],
        origin_parent_candidate_id=parent_candidate_id,
        node_type="stop_boundary",
        role="unknown",
        claim=stop_reason or "当前分支已停在证据支持边界。",
        supported_level=supported_level,  # type: ignore[arg-type]
        confidence=0.2,
        status=status,  # type: ignore[arg-type]
        claim_type="partial_localization",
        causal_status="inconclusive",
        decision="backtrack",
        depth_kind="boundary",
        conclusion_eligible=False,
        eligibility_reason="STOP 是局部分支的停止/边界说明节点，不能进入最终主因或次因。",
        stop_reason=stop_reason,
        blocked_probe=blocked_probe,
        evidence_refs=evidence_refs,
        self_challenge=AITreeSelfChallenge(
            why_this_claim=stop_reason,
            supporting_evidence_refs=evidence_refs,
            missing_evidence=[blocked_probe] if blocked_probe else [],
            what_would_change_my_mind="新的同窗证据支持继续下钻，或反证当前来源父节点。",
        ),
    )


def _unresolved_prior_ai_candidates(tree: dict[str, Any] | None) -> list[AITreeCandidateNode]:
    if not isinstance(tree, dict):
        return []
    result: list[AITreeCandidateNode] = []
    seen: set[str] = set()
    for layer in tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for group in ("primary_causes", "secondary_causes", "unknown_causes"):
            for raw in layer.get(group, []) or []:
                if not isinstance(raw, dict):
                    continue
                candidate_id = str(raw.get("candidate_id") or "")
                if (
                    not candidate_id
                    or candidate_id in seen
                    or candidate_id.startswith(("coarse_", "gap_", "blocked_", "unknown_"))
                    or raw.get("depth_kind", "base") != "base"
                    or raw.get("node_type") == "line_anchor"
                    or raw.get("conclusion_eligible")
                    or raw.get("status") in {"contradicted", "rejected", "forbidden"}
                ):
                    continue
                challenge = raw.get("self_challenge") if isinstance(raw.get("self_challenge"), dict) else {}
                missing = _unique_strings([
                    *challenge.get("missing_evidence", []),
                    "source_mechanism_query",
                ])
                try:
                    node = AITreeCandidateNode.model_validate({
                        **raw,
                        "role": "unknown",
                        "conclusion_eligible": False,
                        "confidence": min(float(raw.get("confidence") or 0.0), 0.65),
                        "self_challenge": {
                            **challenge,
                            "missing_evidence": missing,
                            "what_would_change_my_mind": challenge.get("what_would_change_my_mind") or "补齐深探证据后重新判断该基础定位。",
                        },
                    })
                except (TypeError, ValueError):
                    continue
                result.append(node)
                seen.add(candidate_id)
    return result


def _prior_deep_candidates(tree: dict[str, Any] | None) -> list[AITreeCandidateNode]:
    if not isinstance(tree, dict):
        return []
    result: list[AITreeCandidateNode] = []
    seen: set[str] = set()
    for layer in tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            for raw in layer.get(group, []) or []:
                if not isinstance(raw, dict):
                    continue
                if raw.get("depth_kind") != "mechanism":
                    continue
                candidate_id = str(raw.get("candidate_id") or "")
                if not candidate_id or candidate_id in seen or raw.get("status") in {"contradicted", "rejected"}:
                    continue
                try:
                    node = AITreeCandidateNode.model_validate({
                        **raw,
                        "role": "unknown",
                        "conclusion_eligible": False,
                        "decision": "continue_probe",
                        "supported_level": "call_path",
                    })
                except (TypeError, ValueError):
                    continue
                result.append(node)
                seen.add(candidate_id)
    return result


def _source_snapshot_hashes(observations: list[dict[str, Any]]) -> list[str]:
    hashes = []
    for observation in observations:
        snapshot = observation.get("source_snapshot")
        if not isinstance(snapshot, dict):
            continue
        validity = snapshot.get("evidence_validity") if isinstance(snapshot.get("evidence_validity"), dict) else {}
        evidence_status = str(validity.get("evidence_status") or "valid")
        value = str(snapshot.get("source_context_hash") or "").strip()
        if value and snapshot.get("revision") and evidence_status in {"valid", "partial"} and value not in hashes:
            hashes.append(value)
    return hashes


def _completed_session_probe_requests(probes: list[dict[str, Any]]) -> list[str]:
    completed: list[str] = []
    for probe in probes:
        if probe.get("status") != "COMPLETED" or probe.get("evidence_status") not in {"valid", "partial"}:
            continue
        gap = str((probe.get("parameters") or {}).get("evidence_gap") or "")
        if gap and gap not in completed:
            completed.append(gap)
    return completed


def _probe_request_specs(
    value: Any,
    *,
    selected: set[str] | None = None,
) -> list[ProbeRequestSpec]:
    """Keep only validated structured probe requests for tree persistence."""
    if not isinstance(value, list):
        return []
    result: list[ProbeRequestSpec] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, ProbeRequestSpec):
            spec = item
        elif isinstance(item, dict):
            try:
                spec = ProbeRequestSpec.model_validate(item)
            except (TypeError, ValueError):
                continue
        else:
            continue
        family = str(spec.evidence_family or "").strip()
        if not family or family in seen or (selected is not None and family not in selected):
            continue
        seen.add(family)
        result.append(spec.model_copy(update={"evidence_family": family}))
    return result


def _followup_provenance(evidence_gap: str, probe_input: dict[str, Any]) -> tuple[str, str]:
    """Read deep-probe provenance supplied by the guarded review without inference."""
    item = probe_input if isinstance(probe_input, dict) else {}
    generated = item.get("ai_generated_query") if isinstance(item.get("ai_generated_query"), dict) else {}
    candidate_id = str(item.get("candidate_id") or generated.get("candidate_id") or "").strip()
    origin_parent = str(
        item.get("origin_parent_candidate_id")
        or generated.get("origin_parent_candidate_id")
        or ""
    ).strip()
    return candidate_id, origin_parent


def _merge_probe_input_maps(*maps: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Compatibility wrapper around canonical field-level input merging."""
    result = merge_canonical_probe_inputs(*maps)
    return {
        family: value
        for family, value in result.inputs.items()
        if all(_followup_provenance(family, value))
    }


def _deep_probe_provenance(probes: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for probe in probes:
        parameters = probe.get("parameters") if isinstance(probe, dict) else {}
        if not isinstance(parameters, dict):
            continue
        candidate_id = str(parameters.get("candidate_id") or "").strip()
        origin_parent = str(parameters.get("origin_parent_candidate_id") or "").strip()
        if candidate_id and origin_parent and candidate_id not in result:
            result[candidate_id] = origin_parent
        elif candidate_id and origin_parent and result.get(candidate_id) == origin_parent:
            continue
        elif candidate_id and origin_parent and result.get(candidate_id) != origin_parent:
            result.pop(candidate_id, None)
    return result


def _probe_needs_rollback(probe: dict[str, Any]) -> bool:
    status = str(probe.get("status") or "").strip().lower()
    evidence_status = str(probe.get("evidence_status") or "").strip().lower()
    return status in {"failed", "blocked", "timed_out", "timeout"} or evidence_status in {
        "partial",
        "blocked",
        "failed",
        "empty_window",
        "unparseable",
        "target_exit",
    }


def _rollback_edge_status(probe: dict[str, Any]) -> str:
    status = str(probe.get("status") or "").strip().lower()
    evidence_status = str(probe.get("evidence_status") or "").strip().lower()
    if status in {"failed", "timed_out", "timeout"} or evidence_status in {"failed", "unparseable"}:
        return "failed"
    if status == "blocked" or evidence_status in {"blocked", "empty_window", "target_exit"}:
        return "blocked"
    if evidence_status == "partial":
        return "inconclusive"
    return "unknown"


def _probe_failure_for_candidate(candidate_id: str, probes: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = []
    for probe in probes:
        parameters = probe.get("parameters") if isinstance(probe, dict) else {}
        if not isinstance(parameters, dict):
            continue
        if str(parameters.get("candidate_id") or "").strip() in {candidate_id, candidate_id.removeprefix("mechanism_")}:
            matches.append(probe)
    failed = [probe for probe in matches if _probe_needs_rollback(probe)]
    return failed[-1] if failed else None


def _sync_tree_retained_conclusion(
    tree: ControlledAITree,
    retained_conclusion: dict[str, Any] | None,
) -> ControlledAITree:
    """Keep the canonical tree pointer aligned with the final retained claim."""
    if not isinstance(retained_conclusion, dict):
        return tree
    retained_id = str(retained_conclusion.get("candidate_id") or "").strip()
    if not retained_id:
        return tree

    nodes = [
        node
        for layer in tree.layers
        for node in _layer_nodes_for_validation(layer)
    ]
    emitted_ids = {node.candidate_id for node in nodes}
    if retained_id not in emitted_ids:
        data_quality = dict(tree.data_quality or {})
        records = list(data_quality.get("records") or [])
        if not any(
            isinstance(record, dict)
            and record.get("status") == "retained_candidate_not_emitted"
            and record.get("candidate_id") == retained_id
            for record in records
        ):
            records.append({
                "candidate_id": retained_id,
                "status": "retained_candidate_not_emitted",
                "claim": "最终保留结论没有对应的 emitted session_main 节点，树未伪造该节点。",
                "retained_conclusion": True,
            })
        data_quality["records"] = records
        return tree.model_copy(update={
            "data_quality": data_quality,
            "retained_candidate_id": None,
            "localization_chain": [],
        })

    return tree.model_copy(update={
        "retained_candidate_id": retained_id,
        "localization_chain": _tree_localization_chain(tree.layers, retained_id),
    })


def _promote_ai_layer_to_guarded(layer: AITreeLayer) -> AITreeLayer:
    return layer


def _promote_ai_nodes_to_guarded(tree: ControlledAITree) -> ControlledAITree:
    """Compatibility shim; session review metadata now records AI participation."""
    return tree


def _mark_analyzer_fallback_tree(tree: ControlledAITree | None) -> ControlledAITree | None:
    """Mark Analyzer directions as fallback-only after total AI failure."""
    if tree is None:
        return None
    layers: list[AITreeLayer] = []
    for layer in tree.layers:
        updated_nodes: list[AITreeCandidateNode] = []
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]:
            if (
                node.relation != "root"
                and node.generated_by not in {"ai", "ai_candidate", "ai_guarded"}
                and node.node_type not in {
                    "observation",
                    "mechanism_explanation",
                    "stop_boundary",
                    "orphan",
                }
            ):
                node = node.model_copy(update={
                    "generated_by": "analyzer_fallback",
                    "role": "unknown",
                    "status": "missing_evidence",
                    "claim_type": "partial_localization",
                    "causal_status": "unproven",
                    "decision": "continue_probe",
                    "conclusion_eligible": False,
                    "eligibility_reason": "AI 首轮未形成可用候选，该节点仅作为 Analyzer fallback 调查方向。",
                })
            updated_nodes.append(node)
        grouped = {"primary": [], "secondary": [], "rejected": [], "unknown": []}
        for node in updated_nodes:
            grouped[node.role].append(node)
        layers.append(layer.model_copy(update={
            "generated_by": "analyzer_fallback",
            "primary_causes": grouped["primary"],
            "secondary_causes": grouped["secondary"],
            "rejected_causes": grouped["rejected"],
            "unknown_causes": grouped["unknown"],
        }))
    return tree.model_copy(update={"layers": layers})


def _retained_parent_candidate_id(
    nodes: list[AITreeCandidateNode],
    candidate_id: str,
) -> str:
    """Return the nearest retained base candidate for an investigation node."""
    by_id = {node.candidate_id: node for node in nodes}
    current_id = str(candidate_id or "").strip()
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        node = by_id.get(current_id)
        if node is None:
            return ""
        if (
            node.generated_by in {"ai", "ai_candidate", "ai_guarded"}
            and node.node_type not in {
                "observation",
                "mechanism_explanation",
                "stop_boundary",
                "evidence_gap",
                "orphan",
            }
            and node.depth_kind not in {"mechanism", "boundary"}
            and node.role != "rejected"
        ):
            return node.candidate_id
        current_id = str(
            node.origin_parent_candidate_id
            or _single_parent_id(node.parent_candidate_ids)
            or ""
        ).strip()
    return ""


def _add_ai_line_refinements(
    tree: ControlledAITree,
) -> tuple[ControlledAITree, list[dict[str, Any]]]:
    """Bind AI call-path candidates to a verified source line without bypassing gates."""
    if str((tree.line_anchor_eligibility or {}).get("status") or "") != "verified":
        return tree, []

    line_nodes = [
        node
        for layer in tree.layers
        for node in _layer_nodes_for_validation(layer)
        if (
            node.supported_level == "line"
            and node.node_type == "line_anchor"
            and node.depth_kind == "base"
        )
    ]
    if not line_nodes:
        return tree, []

    eligibility = tree.line_anchor_eligibility or {}
    file_name = str(eligibility.get("file") or "").replace("\\", "/")
    line_number = int(_num(eligibility.get("line")))
    if not file_name or line_number <= 0:
        return tree, []
    anchor_label = f"{file_name}:{line_number}"

    all_nodes = [
        node
        for layer in tree.layers
        for node in _layer_nodes_for_validation(layer)
    ]
    by_id = {node.candidate_id: node for node in all_nodes}
    layer_by_id = {
        node.candidate_id: layer
        for layer in tree.layers
        for node in _layer_nodes_for_validation(layer)
    }
    existing_ids = set(by_id)
    diagnostics: list[dict[str, Any]] = []
    refinements_by_depth: dict[int, list[AITreeCandidateNode]] = {}
    updated_layers = list(tree.layers)

    for candidate in all_nodes:
        if (
            candidate.generated_by not in {"ai", "ai_candidate", "ai_guarded"}
            or candidate.depth_kind != "base"
            or candidate.node_type in {
                "observation",
                "mechanism_explanation",
                "stop_boundary",
                "evidence_gap",
                "orphan",
                "line_anchor",
            }
            or candidate.role == "rejected"
            or candidate.supported_level not in {"function", "call_path"}
        ):
            continue
        if any(
            by_id.get(parent_id) is not None
            and by_id[parent_id].supported_level == "line"
            for parent_id in candidate.parent_candidate_ids
        ):
            continue

        candidate_refs = set(candidate.evidence_refs)
        matching_lines = [
            line_node
            for line_node in line_nodes
            if candidate_refs.intersection(line_node.evidence_refs)
        ]
        if len(matching_lines) != 1:
            diagnostics.append({
                "candidate_id": candidate.candidate_id,
                "status": "not_refined",
                "reason": (
                    "verified line 存在，但 AI candidate 没有唯一同证据 cohort，"
                    "未猜测其源码父行。"
                ),
                "supported_level": candidate.supported_level,
                "verified_line": anchor_label,
            })
            continue

        line_node = matching_lines[0]
        refinement_id = f"{candidate.candidate_id}#line"
        parent_layer = layer_by_id.get(candidate.candidate_id)
        if parent_layer is None:
            continue
        target_depth = parent_layer.depth + 1
        if target_depth > int(tree.budget.max_tree_depth):
            diagnostics.append({
                "candidate_id": candidate.candidate_id,
                "status": "blocked",
                "reason": "AI line refinement 超出当前 session_main 深度预算。",
                "verified_line": anchor_label,
                "target_depth": target_depth,
                "max_tree_depth": tree.budget.max_tree_depth,
            })
            continue

        missing = list(candidate.self_challenge.missing_evidence)
        if not candidate.mechanism.strip() and "mechanism" not in missing:
            missing.append("mechanism")
        if not candidate.source_relation_refs and "verified_source_relation" not in missing:
            missing.append("verified_source_relation")
        line_claim = (
            f"{candidate.claim}（运行时 file:line 与 source_snapshot 已定位到 "
            f"{anchor_label}。）"
        )
        generated_by = (
            "ai_guarded"
            if candidate.generated_by == "ai_guarded"
            else "ai_candidate"
        )
        claim_origin = "ai_update" if generated_by == "ai_guarded" else "ai_proposal"
        line_evidence_refs = list(dict.fromkeys([
            *candidate.evidence_refs,
            *line_node.evidence_refs,
        ]))
        line_challenge = candidate.self_challenge.model_copy(update={
            "missing_evidence": list(dict.fromkeys(missing)),
            "supporting_evidence_refs": list(dict.fromkeys([
                *candidate.self_challenge.supporting_evidence_refs,
                *line_node.evidence_refs,
            ])),
            "why_this_claim": line_claim,
        })
        line_fields = {
            **canonical_claim_fields(
                line_claim,
                generated_by=generated_by,
                claim_origin=claim_origin,
                claim_transform="refined",
                source_candidate_id=candidate.candidate_id,
                source_claim_hash=candidate.claim_hash,
            ),
            "lineage_id": f"{candidate.lineage_id or candidate.candidate_id}#line",
            "parent_candidate_ids": [candidate.candidate_id],
            "origin_parent_candidate_id": candidate.candidate_id,
            "relation": "refinement",
            "node_type": "line_anchor",
            "role": candidate.role,
            "claim": line_claim,
            "supported_level": "line",
            "confidence": candidate.confidence,
            "status": candidate.status,
            "claim_type": candidate.claim_type,
            "causal_status": candidate.causal_status,
            "decision": candidate.decision,
            "mechanism": candidate.mechanism,
            "target": f"{candidate.target} @ {anchor_label}",
            "primitive_kind": candidate.primitive_kind,
            "depth_kind": "base",
            "conclusion_eligible": False,
            "eligibility_reason": (
                f"已定位到 {anchor_label}；"
                + (
                    "仍缺少机制和已验证 source relation，不能形成正式源码根因。"
                    if missing
                    else "仍需通过统一 session qualification gate。"
                )
            ),
            "evidence_refs": line_evidence_refs,
            "cost_center_refs": list(candidate.cost_center_refs),
            "trigger_refs": list(candidate.trigger_refs),
            "mechanism_refs": list(candidate.mechanism_refs),
            "impact_refs": list(candidate.impact_refs),
            "source_relation_refs": list(candidate.source_relation_refs),
            "self_challenge": line_challenge,
        }
        if refinement_id in existing_ids:
            updated_line = by_id[refinement_id].model_copy(update=line_fields)
            for layer_index, layer in enumerate(updated_layers):
                updated_layers[layer_index] = layer.model_copy(update={
                    group: [
                        updated_line if item.candidate_id == refinement_id else item
                        for item in getattr(layer, group)
                    ]
                    for group in (
                        "primary_causes",
                        "secondary_causes",
                        "rejected_causes",
                        "unknown_causes",
                    )
                })
            by_id[refinement_id] = updated_line
            diagnostics.append({
                "candidate_id": candidate.candidate_id,
                "line_candidate_id": refinement_id,
                "status": "updated",
                "verified_line": anchor_label,
                "missing_evidence": list(dict.fromkeys(missing)),
            })
            continue
        line_refinement = AITreeCandidateNode(
            candidate_id=refinement_id,
            **line_fields,
        )
        refinements_by_depth.setdefault(target_depth, []).append(line_refinement)
        existing_ids.add(refinement_id)
        diagnostics.append({
            "candidate_id": candidate.candidate_id,
            "line_candidate_id": refinement_id,
            "status": "refined",
            "verified_line": anchor_label,
            "parent_candidate_id": candidate.candidate_id,
            "line_anchor_candidate_id": line_node.candidate_id,
            "formal_root_cause_eligible": False,
            "missing_evidence": list(dict.fromkeys(missing)),
        })

    if not refinements_by_depth:
        return tree, diagnostics

    for depth, nodes in sorted(refinements_by_depth.items()):
        updated_layers.append(AITreeLayer(
            layer_id=f"session_ai_line_refinement_{depth}_{len(updated_layers)}",
            depth=depth,
            generated_by="ai_candidate",
            summary=(
                "真实 AI 候选沿同证据 cohort 细化到 verified source line；"
                "line refinement 不自动等同于正式根因。"
            ),
            unknown_causes=nodes,
        ))

    updated = tree.model_copy(update={"layers": updated_layers})
    retained_id = str(updated.retained_candidate_id or "").strip()
    if retained_id:
        updated = updated.model_copy(update={
            "localization_chain": _tree_localization_chain(updated.layers, retained_id),
        })
    return updated, diagnostics


def _apply_candidate_review(tree: ControlledAITree, review: dict[str, Any]) -> ControlledAITree:
    """Attach first-round AI candidates to the existing canonical DAG."""
    proposals = review.get("candidate_proposals") if isinstance(review.get("candidate_proposals"), list) else []
    ingestion_diagnostics: list[dict[str, Any]] = []
    if not proposals:
        tree, line_refinement_diagnostics = _add_ai_line_refinements(tree)
        review["line_refinement_diagnostics"] = line_refinement_diagnostics
        review["tree_ingestion_diagnostics"] = ingestion_diagnostics
        return enforce_conclusion_eligibility(tree)
    existing_ids = {
        node.candidate_id
        for layer in tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
    }
    aliases = {
        **dict(tree.coarse_aliases or {}),
        "coarse_insufficient_evidence": (
            tree.emitted_coarse_ids[0] if len(tree.emitted_coarse_ids) == 1 else ""
        ),
    }
    active_candidate_ids_declared = isinstance(review.get("active_candidate_ids"), list)
    active_candidate_ids = {
        str(value)
        for value in review.get("active_candidate_ids", [])
        if str(value)
    }
    eligible_proposals: list[dict[str, Any]] = []
    for item in proposals:
        candidate_id = str(item.get("candidate_id") or "")
        parent_ids = [
            aliases.get(str(parent_id), str(parent_id))
            for parent_id in item.get("parent_candidate_ids", [])
            if str(parent_id)
        ]
        origin = aliases.get(
            str(item.get("origin_parent_candidate_id") or ""),
            str(item.get("origin_parent_candidate_id") or ""),
        )
        if origin and origin not in parent_ids:
            parent_ids.append(origin)
        normalized_item = {
            **item,
            "parent_candidate_ids": list(dict.fromkeys(parent_ids)),
            "origin_parent_candidate_id": origin or None,
        }
        if candidate_id in existing_ids:
            ingestion_diagnostics.append({
                "candidate_id": candidate_id,
                "failure_code": "candidate_already_emitted",
                "reason": "候选 ID 已存在于当前 session_main，未重复发出。",
                "parent_candidate_ids": parent_ids,
            })
            continue
        missing_parents = [parent_id for parent_id in parent_ids if parent_id not in existing_ids]
        if missing_parents:
            ingestion_diagnostics.append({
                "candidate_id": candidate_id,
                "failure_code": "tree_parent_not_emitted",
                "reason": "AI 候选的父节点不在当前 emitted session_main 中，未接入主树。",
                "parent_candidate_ids": parent_ids,
                "missing_parent_candidate_ids": missing_parents,
            })
            continue
        if active_candidate_ids_declared and candidate_id not in active_candidate_ids:
            ingestion_diagnostics.append({
                "candidate_id": candidate_id,
                "failure_code": "candidate_deferred_from_tree",
                "reason": "候选结构有效，但本轮仅进入 deferred 审计集合，未消耗深探预算。",
                "selection": "deferred",
                "parent_candidate_ids": parent_ids,
            })
            continue
        eligible_proposals.append(normalized_item)
    proposals = eligible_proposals
    if not proposals:
        tree, line_refinement_diagnostics = _add_ai_line_refinements(tree)
        review["line_refinement_diagnostics"] = line_refinement_diagnostics
        review["tree_ingestion_diagnostics"] = ingestion_diagnostics
        return enforce_conclusion_eligibility(tree)
    valid_levels = {"resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"}
    valid_relations = {"root", "alternative", "refinement", "causal_convergence", "shared_evidence"}
    nodes = []
    for item in proposals:
        parent_ids = list(dict.fromkeys(str(value) for value in item.get("parent_candidate_ids", []) if str(value)))
        relation = str(item.get("relation") or (
            "root"
            if not parent_ids
            else "causal_convergence"
            if len(parent_ids) > 1
            else "refinement"
        ))
        if relation not in valid_relations or (len(parent_ids) > 1 and relation not in {"causal_convergence", "shared_evidence", "alternative"}):
            continue
        level = str(item.get("supported_level") or "resource")
        if level not in valid_levels:
            continue
        refs = list(dict.fromkeys(str(value) for value in item.get("evidence_refs", []) if str(value)))
        role = str(item.get("role") or "unknown")
        if role not in {"primary", "secondary", "rejected", "unknown"}:
            role = "unknown"
        raw_decision = str(item.get("decision") or "needs_more_evidence")
        raw_causal_status = str(item.get("causal_status") or "needs_more_evidence")
        is_rejected = role == "rejected" or raw_decision in {"reject", "reject_candidate"} or raw_causal_status in {"rejected", "contradicted"}
        nodes.append(AITreeCandidateNode(
                candidate_id=str(item["candidate_id"]),
                **canonical_claim_fields(
                    item["claim"],
                    generated_by="ai_candidate",
                    claim_origin="ai_proposal",
                    claim_transform="refined" if parent_ids else "original",
                    source_candidate_id=str(item.get("origin_parent_candidate_id") or _single_parent_id(parent_ids) or ""),
                ),
                lineage_id=str(item["candidate_id"]),
                parent_candidate_ids=parent_ids,
                origin_parent_candidate_id=str(
                    item.get("origin_parent_candidate_id")
                    or _single_parent_id(parent_ids)
                    or ""
                ) or None,
            relation=relation,
            role=role,
            claim=str(item["claim"]),
            supported_level=level,
            confidence=float(item.get("confidence") or 0.45),
            # First-round AI output is an investigation direction, never a
            # formal conclusion. The original model decision/status remain in
            # candidate_review for audit; only a later evidence回流 round may
            # promote this node through the shared qualification gate.
            status="rejected" if is_rejected else "missing_evidence",
            claim_type="insufficient_for_root_cause" if is_rejected else "likely_root_cause",
            causal_status="contradicted" if is_rejected else "unproven",
            decision="reject_candidate" if is_rejected else "continue_probe",
            mechanism=str(item["mechanism"]),
            target=str(item["target"]),
            depth_kind="base",
            conclusion_eligible=False,
            eligibility_reason=(
                "首轮 AI 候选只是调查方向；必须等待同候选深探证据回流后，"
                "再由统一正式门禁判断是否可升级。"
                if not is_rejected
                else "首轮 AI 已将该方向标记为拒绝，保留为反证分支。"
            ),
            evidence_refs=refs,
            cost_center_refs=list(dict.fromkeys(
                str(value) for value in item.get("cost_center_refs", []) if str(value)
            )),
            trigger_refs=list(dict.fromkeys(
                str(value) for value in item.get("trigger_refs", []) if str(value)
            )),
            mechanism_refs=list(dict.fromkeys(
                str(value) for value in item.get("mechanism_refs", []) if str(value)
            )),
            impact_refs=list(dict.fromkeys(
                str(value) for value in item.get("impact_refs", []) if str(value)
            )),
            source_relation_refs=list(dict.fromkeys(
                str(value) for value in item.get("source_relation_refs", []) if str(value)
            )),
            probe_request_specs=_probe_request_specs(item.get("probe_request_specs")),
            self_challenge=AITreeSelfChallenge(
                why_this_claim=str(item["claim"]),
                supporting_evidence_refs=refs,
                missing_evidence=list(dict.fromkeys(str(value) for value in item.get("missing_evidence", []) if str(value))),
                what_would_change_my_mind="出现明确反证，或补充证据支持另一条候选路径。",
            ),
        ))
    if not nodes:
        tree, line_refinement_diagnostics = _add_ai_line_refinements(tree)
        review["line_refinement_diagnostics"] = line_refinement_diagnostics
        review["tree_ingestion_diagnostics"] = ingestion_diagnostics
        return enforce_conclusion_eligibility(tree)

    # Candidate review runs after the Analyzer has already emitted line,
    # observation and boundary layers. Appending a new layer here makes a
    # valid AI candidate look like it exceeded the tree budget even though
    # its real parent is at a shallower depth. Place each proposal directly
    # below its declared parent layer instead.
    layer_by_candidate_id = {
        node.candidate_id: layer
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    max_depth = int(tree.budget.max_tree_depth)
    nodes_by_depth: dict[int, list[AITreeCandidateNode]] = {}
    for node in nodes:
        parent_layers = [
            layer_by_candidate_id[parent_id]
            for parent_id in node.parent_candidate_ids
            if parent_id in layer_by_candidate_id
        ]
        if not parent_layers:
            ingestion_diagnostics.append({
                "candidate_id": node.candidate_id,
                "failure_code": "tree_parent_layer_missing",
                "reason": "候选父节点虽声明存在，但无法解析其当前 session_main 层级。",
                "parent_candidate_ids": list(node.parent_candidate_ids),
            })
            continue
        target_depth = max(layer.depth for layer in parent_layers) + 1
        if target_depth > max_depth:
            ingestion_diagnostics.append({
                "candidate_id": node.candidate_id,
                "failure_code": "tree_depth_budget_exhausted",
                "reason": (
                    "候选的真实来源父节点位于当前树预算之外，"
                    "不是因为已有历史/边界层数量而丢弃。"
                ),
                "parent_candidate_ids": list(node.parent_candidate_ids),
                "origin_parent_candidate_id": node.origin_parent_candidate_id,
                "parent_depth": max(layer.depth for layer in parent_layers),
                "target_depth": target_depth,
                "max_tree_depth": max_depth,
            })
            continue
        nodes_by_depth.setdefault(target_depth, []).append(node)

    if not nodes_by_depth:
        tree, line_refinement_diagnostics = _add_ai_line_refinements(tree)
        review["line_refinement_diagnostics"] = line_refinement_diagnostics
        review["tree_ingestion_diagnostics"] = ingestion_diagnostics
        return enforce_conclusion_eligibility(tree)

    # Keep the source semantics of an existing layer intact. An AI proposal
    # may share the same depth as an Analyzer observation, but it must not
    # relabel that observation as AI-generated. The explicit lineage edges,
    # rather than list position, define the hierarchy.
    proposal_layers = [
        AITreeLayer(
            layer_id=f"session_ai_candidate_{depth}",
            depth=depth,
            generated_by="ai_candidate",
            summary="AI 基于 Analyzer 事实和证据边界生成首轮可证伪候选。",
            primary_causes=[node for node in depth_nodes if node.role == "primary"],
            secondary_causes=[node for node in depth_nodes if node.role == "secondary"],
            rejected_causes=[node for node in depth_nodes if node.role == "rejected"],
            unknown_causes=[node for node in depth_nodes if node.role == "unknown"],
        )
        for depth, depth_nodes in nodes_by_depth.items()
    ]
    layers = [*tree.layers, *proposal_layers]
    parent_ids = {
        parent_id
        for depth_nodes in nodes_by_depth.values()
        for child in depth_nodes
        for parent_id in child.parent_candidate_ids
    }
    updated_layers = []
    for layer in layers:
        updated_nodes = []
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]:
            if node.candidate_id in parent_ids:
                child_ids = list(dict.fromkeys([
                    *node.child_candidate_ids,
                    *[
                        child.candidate_id
                        for depth_nodes in nodes_by_depth.values()
                        for child in depth_nodes
                        if node.candidate_id in child.parent_candidate_ids
                    ],
                ]))
                node = node.model_copy(update={"child_candidate_ids": child_ids})
            updated_nodes.append(node)
        grouped = {"primary": [], "secondary": [], "rejected": [], "unknown": []}
        for node in updated_nodes:
            grouped[node.role].append(node)
        updated_layers.append(layer.model_copy(update={
            "primary_causes": grouped["primary"],
            "secondary_causes": grouped["secondary"],
            "rejected_causes": grouped["rejected"],
            "unknown_causes": grouped["unknown"],
        }))
    layers = updated_layers
    updated = tree.model_copy(update={"layers": layers})
    updated, line_refinement_diagnostics = _add_ai_line_refinements(updated)
    review["line_refinement_diagnostics"] = line_refinement_diagnostics
    layers = updated.layers
    all_emitted_nodes = [
        node
        for layer in layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    ]
    retained_candidate_id = next(
        (
            retained_id
            for candidate_id in review.get("active_candidate_ids", [])
            if (
                retained_id := _retained_parent_candidate_id(
                    all_emitted_nodes,
                    str(candidate_id),
                )
            )
        ),
        tree.retained_candidate_id,
    )
    updated = updated.model_copy(update={
        "layers": layers,
        "retained_candidate_id": retained_candidate_id or None,
    })
    if retained_candidate_id:
        updated = updated.model_copy(update={
            "localization_chain": _tree_localization_chain(updated.layers, retained_candidate_id),
        })
    review["tree_ingestion_diagnostics"] = ingestion_diagnostics
    return enforce_conclusion_eligibility(updated)


def _apply_investigation_review(tree: ControlledAITree, review: dict[str, Any]) -> ControlledAITree:
    selected = [str(item) for item in review.get("selected_evidence_families", []) if str(item)]
    selected_specs = _probe_request_specs(
        review.get("probe_requests"),
        selected=set(selected),
    )
    proposals = review.get("candidate_proposals") if isinstance(review.get("candidate_proposals"), list) else []
    candidate_updates = review.get("candidate_updates") if isinstance(review.get("candidate_updates"), dict) else {}
    rollback_specs = review.get("rollback_edges") if isinstance(review.get("rollback_edges"), list) else []
    layers = [_promote_ai_layer_to_guarded(layer) for layer in tree.layers]
    edges = list(tree.probe_edges)
    all_nodes = {
        node.candidate_id: (layer.layer_id, node)
        for layer in layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    base_line_ids = {
        candidate_id
        for candidate_id, (_, node) in all_nodes.items()
        if node.depth_kind == "base" and node.supported_level == "line"
    }
    proposals = [
        item for item in proposals
        if (
            str(item.get("origin_parent_candidate_id") or "") in base_line_ids
            and all(
                str(parent_id) in all_nodes
                for parent_id in item.get("parent_candidate_ids", [])
            )
        )
    ]

    def apply_update(node: AITreeCandidateNode) -> AITreeCandidateNode:
        raw = candidate_updates.get(node.candidate_id)
        if not isinstance(raw, dict):
            return node
        update = {}
        for key in (
            "claim", "mechanism", "target", "role", "status", "causal_status",
            "decision", "relation", "eligibility_reason", "stop_reason", "blocked_probe",
            "evidence_refs", "opposing_evidence_refs", "missing_evidence",
        ):
            if key in raw:
                update[key] = raw[key]
        if "parent_candidate_ids" in raw:
            parent_ids = [
                str(parent_id)
                for parent_id in raw.get("parent_candidate_ids", [])
                if str(parent_id) in all_nodes
            ]
            origin = str(raw.get("origin_parent_candidate_id") or "")
            if parent_ids and origin in parent_ids:
                update["parent_candidate_ids"] = list(dict.fromkeys(parent_ids))
                update["origin_parent_candidate_id"] = origin
        if "child_candidate_ids" in raw:
            update["child_candidate_ids"] = list(dict.fromkeys(
                str(child_id) for child_id in raw.get("child_candidate_ids", []) if str(child_id) in all_nodes
            ))
        if "missing_evidence" in raw:
            challenge = node.self_challenge.model_copy(update={
                "missing_evidence": list(dict.fromkeys(
                    str(value) for value in raw.get("missing_evidence", []) if str(value)
                )),
            })
            update["self_challenge"] = challenge
        if not update:
            return node
        lineage = apply_ai_claim_update(node.model_dump(mode="python"), raw)
        update["claim"] = lineage["claim"]
        for field in (
            "generated_by", "claim_origin", "claim_transform", "claim_status",
            "claim_hash", "source_claim_hash", "source_candidate_id", "source_round",
            "source_event_id",
        ):
            update[field] = lineage[field]
        if (
            str(update.get("status") or node.status) == "supported"
            and str(update.get("causal_status") or node.causal_status) == "supported"
            and str(update.get("decision") or node.decision) == "conclude"
            and str(update.get("claim_status") or node.claim_status) not in {"boundary", "duplicate", "rejected"}
        ):
            update["generated_by"] = "ai_guarded"
            update["claim_origin"] = "ai_update"
            update["claim_transform"] = "refined"
        return node.model_copy(update=update)

    updated_layers = []
    for layer in layers:
        updated_nodes = [
            apply_update(node)
            for node in [
                *layer.primary_causes,
                *layer.secondary_causes,
                *layer.rejected_causes,
                *layer.unknown_causes,
            ]
        ]
        grouped = {"primary": [], "secondary": [], "rejected": [], "unknown": []}
        for node in updated_nodes:
            grouped[node.role].append(node)
        updated_layers.append(layer.model_copy(update={
            "primary_causes": grouped["primary"],
            "secondary_causes": grouped["secondary"],
            "rejected_causes": grouped["rejected"],
            "unknown_causes": grouped["unknown"],
        }))
    layers = updated_layers

    if proposals:
        layer_depth_by_id = {
            layer.layer_id: layer.depth
            for layer in layers
        }
        proposal_nodes = [
            AITreeCandidateNode(
                candidate_id=item["candidate_id"],
                **canonical_claim_fields(
                    item["claim"],
                    generated_by="ai_candidate",
                    claim_origin="ai_proposal",
                    claim_transform="refined",
                    source_candidate_id=str(item["origin_parent_candidate_id"]),
                ),
                lineage_id=item["candidate_id"],
                parent_candidate_ids=list(dict.fromkeys(
                    str(parent_id) for parent_id in item.get("parent_candidate_ids", [])
                )),
                child_candidate_ids=list(dict.fromkeys(
                    str(child_id) for child_id in item.get("child_candidate_ids", [])
                )),
                origin_parent_candidate_id=str(item["origin_parent_candidate_id"]),
                relation=str(item.get("relation") or "refinement"),
                role="unknown",
                claim=item["claim"],
                supported_level="call_path",
                confidence=0.45,
                status="missing_evidence",
                claim_type="partial_localization",
                causal_status="unproven",
                decision="continue_probe",
                mechanism=item["mechanism"],
                target=item["target"],
                depth_kind="mechanism",
                conclusion_eligible=False,
                eligibility_reason="AI 提出的机制候选仍需所选探针返回真实证据后才能升级。",
                evidence_refs=item["evidence_refs"],
                cost_center_refs=list(dict.fromkeys(
                    str(value) for value in item.get("cost_center_refs", []) if str(value)
                )),
                trigger_refs=list(dict.fromkeys(
                    str(value) for value in item.get("trigger_refs", []) if str(value)
                )),
                mechanism_refs=list(dict.fromkeys(
                    str(value) for value in item.get("mechanism_refs", []) if str(value)
                )),
                impact_refs=list(dict.fromkeys(
                    str(value) for value in item.get("impact_refs", []) if str(value)
                )),
                source_relation_refs=list(dict.fromkeys(
                    str(value) for value in item.get("source_relation_refs", []) if str(value)
                )),
                probe_request_specs=_probe_request_specs(item.get("probe_request_specs")),
                self_challenge=AITreeSelfChallenge(
                    why_this_claim=item["claim"],
                    supporting_evidence_refs=item["evidence_refs"],
                    opposing_evidence_refs=item.get("opposing_evidence_refs", []),
                    missing_evidence=item.get("missing_evidence", selected),
                    what_would_change_my_mind=item["what_would_change_my_mind"],
                ),
            )
            for item in proposals
        ]
        proposal_groups: dict[int, list[AITreeCandidateNode]] = {}
        for node in proposal_nodes:
            parent_depths = [
                layer_depth_by_id[all_nodes[parent_id][0]]
                for parent_id in node.parent_candidate_ids
                if parent_id in all_nodes and all_nodes[parent_id][0] in layer_depth_by_id
            ]
            target_depth = max(parent_depths, default=-1) + 1
            if target_depth > tree.budget.max_tree_depth:
                review.setdefault("tree_ingestion_diagnostics", []).append({
                    "candidate_id": node.candidate_id,
                    "failure_code": "tree_depth_budget_exhausted",
                    "reason": (
                        "候选的真实来源父节点继续下钻会超过主树深度预算；"
                        "辅助 observation/boundary 层不参与深度计算。"
                    ),
                    "parent_candidate_ids": list(node.parent_candidate_ids),
                    "origin_parent_candidate_id": node.origin_parent_candidate_id,
                    "target_depth": target_depth,
                    "max_tree_depth": tree.budget.max_tree_depth,
                })
                continue
            proposal_groups.setdefault(target_depth, []).append(node)

        for target_depth, grouped_nodes in sorted(proposal_groups.items()):
            parent_ids = list(dict.fromkeys(
                parent_id
                for node in grouped_nodes
                for parent_id in node.parent_candidate_ids
            ))
            parent_layer_id = next(
                (
                    all_nodes[parent_id][0]
                    for parent_id in parent_ids
                    if parent_id in all_nodes
                ),
                layers[0].layer_id if layers else "",
            )
            proposal_layer = AITreeLayer(
                layer_id=f"session_ai_investigation_{target_depth}_{len(layers)}",
                depth=target_depth,
                generated_by="ai_candidate",
                summary="AI 基于当前证据提出可证伪机制，并从注册探针中选择下一轮补证。",
                unknown_causes=grouped_nodes,
            )
            layers.append(proposal_layer)
            all_nodes.update({
                node.candidate_id: (proposal_layer.layer_id, node)
                for node in grouped_nodes
            })
            edges.append(AITreeProbeEdge(
                edge_id=f"session_ai_probe_{len(edges)}",
                from_layer_id=parent_layer_id,
                to_layer_id=proposal_layer.layer_id,
                from_candidate_ids=parent_ids,
                to_candidate_ids=[node.candidate_id for node in grouped_nodes],
                probe_requests=selected,
                probe_request_specs=selected_specs,
                status="not_started",
                effect="added_candidate",
                transition_type="probe",
                reason="AI 调查轮选择注册探针验证新机制候选。",
            ))
        layers.sort(key=lambda layer: (layer.depth, layer.layer_id))
    elif selected and edges:
        edges[-1] = edges[-1].model_copy(update={
            "probe_requests": selected,
            "probe_request_specs": selected_specs,
            "reason": "AI 调查轮从本轮允许的注册探针中选择最小必要补证。",
        })

    for spec in rollback_specs:
        from_ids = [
            str(candidate_id)
            for candidate_id in spec.get("from_candidate_ids", [])
            if str(candidate_id) in all_nodes
        ]
        to_ids = [
            str(candidate_id)
            for candidate_id in spec.get("to_candidate_ids", [])
            if str(candidate_id) in all_nodes
        ]
        if not from_ids or not to_ids:
            continue
        edges.append(AITreeProbeEdge(
            edge_id=str(spec.get("edge_id") or f"ai_rollback_{len(edges)}"),
            from_layer_id=all_nodes[from_ids[0]][0],
            to_layer_id=all_nodes[to_ids[0]][0],
            from_candidate_ids=list(dict.fromkeys(from_ids)),
            to_candidate_ids=list(dict.fromkeys(to_ids)),
            probe_requests=[str(value) for value in spec.get("probe_requests", []) if str(value)],
            probe_request_specs=_probe_request_specs(spec.get("probe_request_specs")),
            status=str(spec.get("status") or "inconclusive"),
            evidence_refs=list(dict.fromkeys(str(value) for value in spec.get("evidence_refs", []) if str(value))),
            effect="rollback",
            transition_type="backtrack",
            reason=str(spec.get("reason") or "AI 依据证据回流回到来源父节点。"),
        ))
    retained_candidate_id = tree.retained_candidate_id
    probe_inputs = review.get("probe_inputs") if isinstance(review.get("probe_inputs"), dict) else {}
    all_emitted_nodes = [
        node
        for layer in layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    ]
    for value in probe_inputs.values():
        if not isinstance(value, dict):
            continue
        candidate_id = _retained_parent_candidate_id(
            all_emitted_nodes,
            str(value.get("candidate_id") or ""),
        )
        if candidate_id:
            retained_candidate_id = candidate_id
            break
    updated = tree.model_copy(update={
        "layers": layers,
        "probe_edges": edges,
        "retained_candidate_id": retained_candidate_id,
        "final_unknown_causes": list(dict.fromkeys([
            *tree.final_unknown_causes,
            *(str(item.get("candidate_id")) for item in proposals),
        ])),
        "budget": tree.budget.model_copy(update={
            "used_ai_rounds": tree.budget.used_ai_rounds + 1,
            "used_probe_requests": tree.budget.used_probe_requests + len(selected),
        }),
    })
    updated, line_refinement_diagnostics = _add_ai_line_refinements(updated)
    review["line_refinement_diagnostics"] = line_refinement_diagnostics
    return enforce_conclusion_eligibility(updated)


def _summarize_investigation_review(review: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review, dict):
        return review
    result = dict(review)
    probe_inputs = review.get("probe_inputs") if isinstance(review.get("probe_inputs"), dict) else {}
    summarized: dict[str, Any] = {}
    for family, value in probe_inputs.items():
        if not isinstance(value, dict):
            continue
        item = dict(value)
        generated = item.get("ai_generated_query") if isinstance(item.get("ai_generated_query"), dict) else None
        if generated is not None:
            item["ai_generated_query"] = {
                key: generated.get(key)
                for key in (
                    "origin",
                    "investigation_question",
                    "candidate_id",
                    "expected_relation",
                    "source_anchor",
                    "sink_anchor",
                    "path_anchors",
                    "query_spec_hash",
                    "raw_query_hash",
                    "template_version",
                )
                if generated.get(key)
            }
        summarized[str(family)] = item
    result["probe_inputs"] = summarized
    return result


def _blocked_upgrade_node(
    assessment: dict[str, Any],
    final_level: str,
    parent_candidate_id: str | None,
) -> AITreeCandidateNode | None:
    anchor = assessment.get("primary_anchor")
    if not isinstance(anchor, dict):
        return None
    reason = str(anchor.get("blocked_upgrade_reason") or "").strip()
    if not reason:
        return None
    if not parent_candidate_id:
        return None
    current_level = str(anchor.get("supported_level") or final_level)
    if current_level == "line":
        return None
    missing = ["source_context", "line_level_profile"]
    if "锁持有者" in reason:
        missing.append("lock_owner_thread")
    if "符号" in reason:
        missing.append("symbol_mapping")
    return AITreeCandidateNode(
        candidate_id="blocked_line_upgrade",
        lineage_id="blocked_line_upgrade",
        parent_candidate_ids=[parent_candidate_id] if parent_candidate_id else [],
        origin_parent_candidate_id=parent_candidate_id,
        role="unknown",
        claim=f"当前只能停在 {current_level} 层：{reason}",
        supported_level=current_level,
        confidence=0.3,
        status="forbidden",
        depth_kind="boundary",
        evidence_refs=_unique_strings([anchor.get("evidence_ref")] + assessment.get("evidence_refs", [])[:2]),
        self_challenge=AITreeSelfChallenge(
            why_this_claim=reason,
            why_not_other_claims="这不是新的根因候选，而是当前主因分支继续升级到代码行的证据边界。",
            supporting_evidence_refs=_unique_strings([anchor.get("evidence_ref")] + assessment.get("evidence_refs", [])[:2]),
            missing_evidence=_unique_strings(missing),
            what_would_change_my_mind="提供源码上下文、符号映射、锁持有者线程或行级采样证据后，才允许从函数/调用路径升级到代码行。",
        ),
    )


def _line_anchor_eligibility_summary(
    anchor: dict[str, Any],
    *,
    has_verified_line_anchor: bool,
    source_snapshot_hashes: list[str],
    origin_parent_candidate_id: str | None = None,
) -> dict[str, Any]:
    anchor = anchor if isinstance(anchor, dict) else {}
    file_name = str(anchor.get("file") or "").replace("\\", "/")
    line_number = int(_num(anchor.get("line")))
    runtime_candidates = [
        item for item in anchor.get("runtime_line_candidates", [])
        if isinstance(item, dict) and item.get("file") and int(_num(item.get("line"))) > 0
    ]
    checks = {
        "source_revision": bool(anchor.get("source_revision")),
        "source_context_hash": bool(anchor.get("source_context_hash") or source_snapshot_hashes),
        "source_context_valid": bool(anchor.get("source_context_hash") or source_snapshot_hashes),
        "file": bool(file_name),
        "positive_line": line_number > 0,
        "runtime_line_candidate": bool(runtime_candidates),
        "runtime_source_match": has_verified_line_anchor,
        "parent_provenance": bool(origin_parent_candidate_id),
    }
    failure_reasons: list[str] = []
    if not runtime_candidates:
        failure_reasons.append("runtime_anchor_missing")
        if anchor.get("source_context_hash") or source_snapshot_hashes:
            failure_reasons.append("source_snapshot_valid_but_no_runtime_line")
    if runtime_candidates and not has_verified_line_anchor:
        failure_reasons.append("runtime_source_match_failed")
    if not (anchor.get("source_context_hash") or source_snapshot_hashes):
        failure_reasons.append("source_context_invalid")
    if not origin_parent_candidate_id:
        failure_reasons.append("parent_provenance_missing")
    if has_verified_line_anchor:
        return {
            "status": "verified",
            "eligibility_status": "verified",
            "checks": checks,
            "failure_reasons": [],
            "file": file_name,
            "line": line_number,
            "reason": "运行时行候选、源码 revision、文件和正行号已形成可验证锚点。",
        }
    if not runtime_candidates:
        reason = "没有运行时 file:line 候选，source_snapshot 不能单独制造 line 锚点。"
    elif not source_snapshot_hashes and not anchor.get("source_context_hash"):
        reason = "没有有效 source_snapshot revision/hash，不能验证运行时行对应的真实源码。"
    elif not file_name or line_number <= 0:
        reason = "当前运行时定位只有函数/调用路径，缺少真实 file:line。"
    else:
        reason = (
            "运行时 file:line 与 source_snapshot 的源码片段未匹配；"
            "source_snapshot 只证明源码存在，不能替运行时证据选择主因行。"
        )
    return {
        "status": "blocked",
        "eligibility_status": "not_verified",
        "checks": checks,
        "failure_reasons": list(dict.fromkeys(failure_reasons)),
        "file": file_name,
        "line": line_number,
        "runtime_candidates": runtime_candidates[:12],
        "reason": reason,
    }


def _heap_probe_outcome(
    probes: list[dict[str, Any]],
    *,
    evidence_catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    relevant = [
        probe for probe in probes
        if str((probe.get("parameters") or {}).get("evidence_gap") or "") == "python_heap_profile"
    ]
    if not relevant:
        return {"status": "not_started", "attempts": 0}
    probe = relevant[-1]
    parameters = probe.get("parameters") if isinstance(probe.get("parameters"), dict) else {}
    outcome = probe.get("outcome") if isinstance(probe.get("outcome"), dict) else {}
    task_id = str(probe.get("task_id") or "")

    # ProbeExecution stores the scheduling result, while the collector payload
    # is persisted as diagnosis evidence. Merge both sources so the session
    # tree does not lose attach phases and artifact references.
    matching_evidence: list[dict[str, Any]] = []
    for evidence in evidence_catalog or []:
        if not isinstance(evidence, dict):
            continue
        if str(evidence.get("query_or_probe") or "") != "python_heap_profile":
            continue
        refs = {
            str(evidence.get("raw_artifact_ref") or ""),
            str(evidence.get("derived_artifact_ref") or ""),
        }
        observed_value = evidence.get("observed_value")
        observed_task_id = (
            str(observed_value.get("task_id") or "")
            if isinstance(observed_value, dict)
            else ""
        )
        if task_id and not (
            observed_task_id == task_id
            or any(task_id in ref for ref in refs)
        ):
            continue
        matching_evidence.append(evidence)
    evidence = matching_evidence[-1] if matching_evidence else {}
    observed = evidence.get("observed_value") if isinstance(evidence.get("observed_value"), dict) else {}
    validity = (
        observed.get("evidence_validity")
        if isinstance(observed.get("evidence_validity"), dict)
        else {}
    )
    if not validity and isinstance(outcome.get("evidence_validity"), dict):
        validity = outcome["evidence_validity"]
    evidence_status = str(
        probe.get("evidence_status")
        or outcome.get("evidence_status")
        or validity.get("evidence_status")
        or "unknown"
    )
    mode = str(observed.get("mode") or outcome.get("mode") or "")
    native_fallback = (
        mode == "native_live"
        or str(observed.get("heap_semantics") or "") == "native_allocation_observation"
        or str(validity.get("reason") or "") == "native_allocator_observation_only"
    )
    if native_fallback:
        python_heap_status = "failed"
        fallback_status = "native_observation"
    elif evidence_status == "valid":
        python_heap_status = "valid"
        fallback_status = "none"
    elif evidence_status in {"blocked", "failed", "unparseable", "target_exit"}:
        python_heap_status = evidence_status
        fallback_status = "none"
    else:
        python_heap_status = "partial" if evidence_status == "partial" else "unknown"
        fallback_status = "none"
    attach_preflight = (
        observed.get("attach_preflight")
        or validity.get("attach_preflight")
        or outcome.get("attach_preflight")
        or {}
    )
    helper_trace = (
        observed.get("helper_trace")
        or attach_preflight.get("helper_trace")
        or outcome.get("helper_trace")
        or {}
    )
    artifact_refs = _unique_strings([
        *(
            [
                evidence.get("raw_artifact_ref"),
                evidence.get("derived_artifact_ref"),
            ]
            if evidence
            else []
        ),
        *(
            observed.get("raw_artifact_refs")
            if isinstance(observed.get("raw_artifact_refs"), list)
            else []
        ),
        *(
            outcome.get("evidence_refs")
            if isinstance(outcome.get("evidence_refs"), list)
            else []
        ),
        *(
            probe.get("evidence_refs")
            if isinstance(probe.get("evidence_refs"), list)
            else []
        ),
    ])[:32]
    return {
        "status": str(probe.get("status") or "unknown").lower(),
        "execution_status": str(probe.get("status") or "unknown").lower(),
        "evidence_status": evidence_status,
        "python_heap_status": python_heap_status,
        "fallback_status": fallback_status,
        "formal_heap_retention": evidence_status == "valid" and not native_fallback,
        "attempts": len(relevant),
        "candidate_id": str(parameters.get("candidate_id") or ""),
        "origin_parent_candidate_id": str(parameters.get("origin_parent_candidate_id") or ""),
        "failure_type": str(
            probe.get("failure_type")
            or outcome.get("failure_type")
            or validity.get("failure_type")
            or ("memray_attach_failed" if native_fallback else "")
            or "",
        ),
        "reason": str(
            probe.get("reason")
            or outcome.get("reason")
            or validity.get("reason")
            or validity.get("detail")
            or "",
        )[:500],
        "failure_detail": str(
            outcome.get("failure_detail")
            or validity.get("detail")
            or probe.get("reason")
            or "",
        )[:800],
        "blocked_reason": str(
            probe.get("blocked_reason")
            or outcome.get("blocked_reason")
            or validity.get("reason")
            or "",
        )[:240],
        "retry_attempted": bool(
            outcome.get("retry_attempted")
            or validity.get("retry_attempted")
        ),
        "retry_skipped_reason": str(
            outcome.get("retry_skipped_reason")
            or validity.get("retry_skipped_reason")
            or "",
        )[:240],
        "attach_preflight": attach_preflight,
        "helper_trace": helper_trace,
        "artifact_refs": artifact_refs,
    }


def _collect_probe_gate_failures(
    probes: list[dict[str, Any]],
    events: list[dict[str, Any]] | None = None,
    *,
    initial_evidence_refs: set[str] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Expose blocked follow-up work as first-class gate diagnostics."""
    failures: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(
        *,
        gate: str,
        failure_code: str,
        reason: str,
        candidate_id: str = "",
        origin_parent_candidate_id: str = "",
        evidence_refs: list[str] | None = None,
        status: str = "blocked",
        actual_value: str = "",
        probe_id: str = "",
    ) -> None:
        key = (gate, failure_code, candidate_id)
        if key in seen:
            return
        seen.add(key)
        failures.append({
            "stage": "followup_probe",
            "gate": gate,
            "failure_code": failure_code,
            "candidate_id": candidate_id,
            "probe_id": probe_id,
            "reason": str(reason or "深探没有形成可用证据。")[:500],
            "actual_value": str(actual_value or "")[:240],
            "status": status,
            "evidence_refs": _unique_strings(evidence_refs or []),
            "initial_evidence_refs": sorted({
                str(ref) for ref in (initial_evidence_refs or set()) if str(ref)
            })[:256],
            "missing_initial_evidence_refs": [],
            "parent_candidate_id": origin_parent_candidate_id,
            "origin_parent_candidate_id": origin_parent_candidate_id,
            "retained_parent_candidate_id": origin_parent_candidate_id,
            "conclusion_eligible": False,
        })

    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "followup_probe_input_missing":
            add(
                gate=str(payload.get("evidence_gap") or "followup_probe"),
                failure_code="missing_guarded_probe_input",
                reason=str(payload.get("reason") or ""),
                candidate_id=str(payload.get("candidate_id") or ""),
                origin_parent_candidate_id=str(payload.get("origin_parent_candidate_id") or ""),
                actual_value=str(payload.get("query_spec_hash") or "missing"),
                probe_id=str(payload.get("probe_id") or ""),
            )
        elif event_type == "followup_probe_provenance_missing":
            add(
                gate=str(payload.get("evidence_gap") or "followup_probe"),
                failure_code="missing_probe_provenance",
                reason=str(payload.get("reason") or ""),
                candidate_id=str(payload.get("candidate_id") or ""),
                origin_parent_candidate_id=str(payload.get("origin_parent_candidate_id") or ""),
            )
        elif event_type == "followup_probe_blocked":
            add(
                gate=str(payload.get("evidence_gap") or "followup_probe"),
                failure_code="probe_budget_or_policy_blocked",
                reason=str(payload.get("reason") or payload.get("blocked_reason") or ""),
                actual_value=str(payload.get("execution_policy") or ""),
            )
        elif event_type == "followup_round_limit_reached":
            add(
                gate="followup_round",
                failure_code="followup_round_limit_reached",
                reason=str(payload.get("reason") or "已达到深探轮次上限。"),
                actual_value=str(payload.get("followup_round") or ""),
            )

    for probe in probes:
        if not isinstance(probe, dict):
            continue
        status = str(probe.get("status") or "").upper()
        evidence_status = str(probe.get("evidence_status") or "").lower()
        if status not in {
            "FAILED",
            "BLOCKED",
            "UNAVAILABLE",
            "REJECTED_POLICY",
            "TIMED_OUT",
            "TIMEOUT",
        } and evidence_status not in {
            "failed",
            "blocked",
            "empty_window",
            "unparseable",
            "target_exit",
        }:
            continue
        parameters = probe.get("parameters") if isinstance(probe.get("parameters"), dict) else {}
        outcome = probe.get("outcome") if isinstance(probe.get("outcome"), dict) else {}
        gate = str(parameters.get("evidence_gap") or probe.get("probe_id") or "followup_probe")
        add(
            gate=gate,
            failure_code=(
                "collector_evidence_failed"
                if status in {"FAILED", "TIMED_OUT", "TIMEOUT"} or evidence_status in {"failed", "unparseable"}
                else "collector_evidence_blocked"
            ),
            reason=str(
                probe.get("reason")
                or outcome.get("reason")
                or (outcome.get("evidence_validity") or {}).get("detail")
                or "深探采集未形成有效证据。"
            ),
            candidate_id=str(parameters.get("candidate_id") or ""),
            origin_parent_candidate_id=str(parameters.get("origin_parent_candidate_id") or ""),
            evidence_refs=_unique_strings(
                probe.get("evidence_refs")
                or outcome.get("evidence_refs")
                or []
            ),
            status=status.lower() or evidence_status or "blocked",
            actual_value=evidence_status,
            probe_id=str(probe.get("probe_id") or ""),
        )
    return failures


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
        evidence_status = str(latest.get("evidence_status") or "")
        if status == "COMPLETED":
            mapped = "completed" if evidence_status == "valid" else "inconclusive" if evidence_status == "partial" else "blocked" if evidence_status == "blocked" else "failed"
        elif status in {"FAILED", "UNAVAILABLE", "REJECTED_POLICY"}:
            mapped = "blocked" if evidence_status == "blocked" or status != "FAILED" else "failed"
        elif status in {"SCHEDULED", "RUNNING", "WAITING_APPROVAL"}:
            mapped = "not_started"
        else:
            mapped = "unknown"
        results.append(AITreeProbeResult(
            status=mapped,
            blocked_reason=str(latest.get("evidence_reason") or latest.get("result_summary") or latest.get("reason") or ""),
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
    if any(status == "inconclusive" for status in statuses):
        return "inconclusive"
    if any(status == "not_started" for status in statuses):
        return "not_started"
    return "unknown"


def _node_status_for_requests(requests: list[str], probes: list[dict[str, Any]]) -> str:
    status = _edge_status_for_requests(requests, probes)
    if status == "completed":
        return "partial"
    if status in {"blocked", "failed"}:
        return "blocked"
    if status == "inconclusive":
        return "partial"
    return "missing_evidence"


def _session_tree_stop_reason(assessment: dict[str, Any], followup_requests: list[str], final_level: str) -> str:
    if not assessment.get("conclusion_eligible"):
        if followup_requests:
            return f"当前只有观察或局部定位，尚无候选通过根因门禁；正在转查并补充：{', '.join(followup_requests[:3])}。"
        return f"当前只能停在 {final_level} 层的观察/局部定位，没有候选满足因果结论资格。"
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


def _failed_tasks_are_only_optional_mechanism_followups(
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
    return all(
        gap_by_task.get(task_id) in {"source_mechanism_query", "python_heap_reference"}
        for task_id in failed_task_ids
    )


def _diagnosis_terminal_status(
    conclusion: dict[str, Any],
    *,
    had_failures: bool = False,
    nonblocking_failures: bool = False,
) -> DiagnosisStatus:
    tree = conclusion.get("controlled_ai_tree") if isinstance(conclusion.get("controlled_ai_tree"), dict) else {}
    clusters = conclusion.get("root_cause_clusters") if isinstance(conclusion.get("root_cause_clusters"), list) else []
    has_eligible_root = bool(
        not conclusion.get("abstained")
        and (
            tree.get("final_primary_causes")
            or any(isinstance(item, dict) and item.get("conclusion_eligible") for item in clusters)
        )
    )
    if has_eligible_root:
        if had_failures and not nonblocking_failures:
            return DiagnosisStatus.PARTIAL_COMPLETED
        return DiagnosisStatus.COMPLETED
    has_partial = bool(
        conclusion.get("possible_root_causes")
        or any(
            isinstance(item, dict)
            and item.get("qualification") in {"possible_root_cause", "partial_localization"}
            for item in clusters
        )
    )
    return DiagnosisStatus.PARTIAL_COMPLETED if has_partial else DiagnosisStatus.INSUFFICIENT_EVIDENCE


def _max_followup_rounds(session: dict[str, Any]) -> int:
    return 8 if str(session.get("policy_profile") or "") == "development" else MAX_FOLLOWUP_ROUNDS


def _can_schedule_evidence_attempt(evidence_gap: str, matching_probes: list[dict[str, Any]]) -> bool:
    if not matching_probes:
        return True
    if evidence_gap != "source_mechanism_query" or len(matching_probes) >= MAX_SOURCE_MECHANISM_ATTEMPTS:
        return False
    terminal_statuses = {"COMPLETED", "FAILED", "UNAVAILABLE", "INVALID", "SKIPPED"}
    if any(str(probe.get("status") or "") not in terminal_statuses for probe in matching_probes):
        return False
    return not any(
        str(probe.get("status") or "") == "COMPLETED"
        and str(probe.get("evidence_status") or "") == "valid"
        for probe in matching_probes
    )


def _guarded_query_spec_hash(probe_input: dict[str, Any]) -> str:
    query = probe_input.get("ai_generated_query") if isinstance(probe_input.get("ai_generated_query"), dict) else {}
    return str(query.get("query_spec_hash") or "")


def _source_mechanism_input_ready(
    probe_input: dict[str, Any],
    collector_parameters: dict[str, Any],
) -> bool:
    return bool(
        _guarded_query_spec_hash(probe_input)
        or collector_parameters.get("codeql_sarif_path")
        or os.getenv("MINI_DROP_CODEQL_QUERY_SUITE", "").strip()
    )


def _repeats_guarded_query(probe_input: dict[str, Any], matching_probes: list[dict[str, Any]]) -> bool:
    query_spec_hash = _guarded_query_spec_hash(probe_input)
    if not query_spec_hash:
        return False
    return any(
        _guarded_query_spec_hash(probe.get("parameters") or {}) == query_spec_hash
        for probe in matching_probes
    )


def _filter_pending_evidence_requests(
    diagnosis_id: str,
    requests: list[str],
    probes: list[dict[str, Any]],
    task_observations: list[dict[str, Any]] | None = None,
) -> list[str]:
    """按证据质量去重，避免依赖检查完成后误删函数级深探请求。"""
    terminal_skip = {
        "UNAVAILABLE",
        "REJECTED",
        "REJECTED_POLICY",
        "INVALID",
        "SKIPPED",
    }
    status_by_gap: dict[str, list[tuple[str, str]]] = {}
    for probe in probes:
        gap = str((probe.get("parameters") or {}).get("evidence_gap") or "")
        if gap:
            status_by_gap.setdefault(gap, []).append((
                str(probe.get("status") or ""),
                str(probe.get("evidence_status") or ""),
            ))

    result: list[str] = []
    depth_completed = _completed_depth_evidence_gaps(task_observations or [])
    for request in requests:
        status_items = status_by_gap.get(request, [])
        statuses = [item[0] for item in status_items]
        has_valid = any(status == "COMPLETED" and evidence in {"valid", "partial"} for status, evidence in status_items)
        if request in DEPENDENCY_EVIDENCE_GAPS:
            if has_valid:
                continue
        elif request == "runtime_control_history" and has_valid:
            continue
        elif request in depth_completed:
            continue
        if statuses and all(status in terminal_skip for status in statuses):
            continue
        if request not in result:
            result.append(request)
    return result


def _task_evidence_validity(
    collector_type: str,
    values: dict[str, Any],
    structured: dict[str, Any],
) -> tuple[str, str]:
    artifact_key = {
        "off_cpu_wait_profile": "off_cpu_wait_json",
        "trace_endpoint_profile": "trace_endpoint_profile_json",
        "baseline_window_profile": "continuous_summary",
        "pyspy": "pyspy_status_json",
        "go_pprof": "go_heap_profile_json",
        "python_heap_profile": "python_heap_profile_json",
        "source_snapshot": "source_snapshot_json",
        "source_mechanism_query": "source_mechanism_json",
        "python_heap_reference": "python_heap_reference_json",
        "log_scan": "log_window_json",
        "runtime_control_history": "runtime_control_event_json",
        "python_lock_wait_profile": "python_lock_wait_profile_json",
        "python_exception_profile": "python_exception_profile_json",
        "python_queue_profile": "python_queue_profile_json",
        "python_pool_profile": "python_pool_profile_json",
        "python_retry_timeout_profile": "python_retry_timeout_profile_json",
        "python_cache_profile": "python_cache_profile_json",
        "python_input_profile": "python_input_profile_json",
    }.get(collector_type)
    payload = values.get(artifact_key) if artifact_key else None
    if isinstance(payload, dict):
        validity = payload.get("evidence_validity")
        if isinstance(validity, dict) and validity.get("evidence_status"):
            return str(validity["evidence_status"]), str(validity.get("reason") or "")
    summary = structured.get("stack_summary") if isinstance(structured.get("stack_summary"), dict) else {}
    confidence = structured.get("confidence_inputs") if isinstance(structured.get("confidence_inputs"), dict) else {}
    if collector_type in {"perf_cpu", "pyspy", "baseline_window_profile"}:
        if int(summary.get("stack_sample_count") or summary.get("sample_count") or 0) > 0:
            return "valid", "structured stack samples available"
        return "empty_window", "no structured stack samples available"
    if collector_type == "sys_metrics" and isinstance(values.get("sys_metrics"), dict):
        return "valid", "structured system metrics available"
    if collector_type == "dependency_check" and isinstance(values.get("dependency_check_json"), dict):
        return "valid", "structured dependency checks available"
    if collector_type == "redis_check" and isinstance(values.get("redis_check_json"), dict):
        return "valid", "structured Redis checks available"
    if collector_type in {"ebpf_io", "memory_smaps"} and structured.get("artifact_refs"):
        return "valid", "structured collector artifact available"
    if confidence.get("has_wait_or_io_signal"):
        return "partial", "structured wait or IO signal available"
    return "unparseable", "collector completed without usable structured evidence"


def _completed_depth_evidence_gaps(observations: list[dict[str, Any]]) -> set[str]:
    """只有拿到对应的非空深度证据，才认为深度请求已经完成。"""
    completed: set[str] = set()
    for observation in observations:
        collector_type = str(observation.get("collector_type") or "")
        top_function = observation.get("top_function") if isinstance(observation.get("top_function"), dict) else {}
        confidence = observation.get("confidence_inputs") if isinstance(observation.get("confidence_inputs"), dict) else {}
        validity = confidence.get("evidence_validity_by_family") if isinstance(confidence.get("evidence_validity_by_family"), dict) else {}
        has_top = bool(str(top_function.get("name") or "").strip())
        has_wait = bool(confidence.get("has_wait_or_io_signal"))
        trace_level = str(confidence.get("trace_max_supported_level") or "")
        trace_status = str(confidence.get("trace_correlation_status") or "")

        if collector_type in {"perf_cpu", "pyspy", "baseline_window_profile", "trace_endpoint_profile"} and has_top:
            completed.add("cpu_profile")
        if collector_type == "off_cpu_wait_profile" and has_wait and validity.get("off_cpu_wait_profile") in {"valid", "partial"}:
            completed.add("off_cpu_wait_profile")
        if collector_type == "trace_endpoint_profile" and validity.get("trace_endpoint_profile") in {"valid", "partial"} and trace_status in {"completed", "partial"} and trace_level in {
            "endpoint",
            "call_path",
        }:
            completed.add("trace_endpoint_profile")
        if collector_type == "baseline_window_profile" and has_top and validity.get("baseline_window_profile") in {"valid", "partial"}:
            completed.add("baseline_window_profile")
        if collector_type == "pyspy" and has_top and validity.get("python_runtime_profile", "valid") in {"valid", "partial"}:
            completed.add("python_runtime_profile")
        if collector_type == "python_heap_profile" and has_top:
            completed.add("python_heap_profile")
        go_heap = observation.get("go_heap_profile") if isinstance(observation.get("go_heap_profile"), dict) else {}
        go_heap_validity = go_heap.get("evidence_validity") if isinstance(go_heap.get("evidence_validity"), dict) else {}
        if (
            collector_type == "go_pprof"
            and has_top
            and go_heap_validity.get("evidence_status") in {"valid", "partial"}
        ):
            completed.add("go_heap_profile")
        if collector_type == "source_snapshot" and validity.get("source_snapshot", "valid") in {"valid", "partial"}:
            completed.add("source_snapshot")
        mechanism = observation.get("source_mechanism") if isinstance(observation.get("source_mechanism"), dict) else {}
        mechanism_validity = mechanism.get("evidence_validity") if isinstance(mechanism.get("evidence_validity"), dict) else {}
        if collector_type == "source_mechanism_query" and mechanism_validity.get("evidence_status") == "valid" and mechanism.get("mechanism_paths"):
            completed.add("source_mechanism_query")
        heap_reference = observation.get("python_heap_reference") if isinstance(observation.get("python_heap_reference"), dict) else {}
        heap_validity = heap_reference.get("evidence_validity") if isinstance(heap_reference.get("evidence_validity"), dict) else {}
        if collector_type == "python_heap_reference" and heap_validity.get("evidence_status") in {"valid", "partial"} and heap_reference.get("reference_paths"):
            completed.add("python_heap_reference")
        scenario_statuses = confidence.get("python_scenario_statuses") if isinstance(confidence.get("python_scenario_statuses"), dict) else {}
        for family in (
            "python_lock_wait_profile",
            "python_exception_profile",
            "python_queue_profile",
            "python_pool_profile",
            "python_retry_timeout_profile",
            "python_cache_profile",
            "python_input_profile",
        ):
            if collector_type == family and scenario_statuses.get(family) in {"valid", "partial"}:
                completed.add(family)
    return completed


def _needs_function_depth(assessment: dict[str, Any]) -> bool:
    """服务或进程层结论成立时，仍允许继续请求函数/调用链证据。"""
    anchor = assessment.get("primary_anchor")
    anchor_level = anchor.get("supported_level") if isinstance(anchor, dict) else None
    supported_level = str(
        assessment.get("effective_investigation_level")
        or anchor_level
        or assessment.get("supported_level")
        or ""
    )
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
    anchor = assessment.get("primary_anchor")
    anchor_level = (
        str(anchor.get("supported_level") or "").strip()
        if isinstance(anchor, dict)
        else ""
    )
    supported_level = (
        anchor_level
        if anchor_level in _INVESTIGATION_LEVEL_ORDER
        else ""
    )
    if classification == "runtime_stall":
        return {
            "location_type": "self",
            "domain_type": "runtime",
            "root_entity": _target_service(session) or "target_process",
            "max_supported_level": supported_level or "process",
        }
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
            "max_supported_level": supported_level or "process",
        }
    return {}


def _compound_location_fields(clusters, session: dict[str, Any]) -> dict[str, Any]:
    locations: list[str] = []
    domains: list[str] = []
    entities: list[str] = []
    for cluster in clusters:
        if not cluster.conclusion_eligible:
            continue
        location, cluster_domains = _cluster_dimensions(cluster, session)
        if location not in locations:
            locations.append(location)
        for domain in cluster_domains:
            if domain not in domains:
                domains.append(domain)
        if cluster.target not in entities:
            entities.append(cluster.target)
    return {
        "location_type": locations,
        "domain_type": domains,
        "root_entity": entities,
    }


def _root_cause_cluster_candidates(clusters, session: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    for cluster in clusters:
        if not cluster.conclusion_eligible:
            continue
        location, domains = _cluster_dimensions(cluster, session)
        parent_ids = _unique_strings([
            *cluster.source_tree_candidate_ids,
            *cluster.candidate_ids,
        ])
        candidates.append({
            "candidate_id": cluster.candidate_ids[0] if cluster.candidate_ids else cluster.cluster_id,
            "description": cluster.claim,
            "evidence_refs": cluster.evidence_refs,
            "missing_evidence": cluster.residual_unknowns,
            "score_components": {
                "rule_match": "high",
                "evidence_quality": "high" if len(cluster.evidence_refs) > 1 else "medium",
                "baseline_support": "medium",
                "source_independence": "high" if len(cluster.evidence_refs) > 1 else "medium",
            },
            "rank": len(candidates) + 1,
            "confidence_level": _confidence_label(cluster.confidence),
            "supporting_claims": [{
                "statement": step.statement,
                "evidence_refs": step.evidence_refs,
                "strength": "strong",
            } for step in cluster.causal_chain],
            "location_type": location,
            "domain_type": domains[0],
            "classification": cluster.mechanism,
            "root_entity": cluster.target,
            "max_supported_level": (
                cluster.supported_level
                if cluster.supported_level and cluster.supported_level != "resource"
                else "service" if cluster.mechanism == "downstream_dependency_failure" else "process"
            ),
            "root_cause_cluster_id": cluster.cluster_id,
            "cause_level": cluster.cause_level,
            "role": cluster.role,
            "parent_candidate_ids": parent_ids,
            "origin_parent_candidate_id": _single_parent_id(parent_ids),
        })
    return candidates


def _merge_python_scenario_gate_clusters(
    primary_clusters,
    engineering_clusters,
    *,
    valid_session_evidence_refs: set[str],
) -> list[Any]:
    # Compatibility helper for old callers. Scenario gates are not formal
    # clusters; only qualified session AI candidates may enter the result.
    return list(primary_clusters or [])


def _cluster_dimensions(cluster, session: dict[str, Any]) -> tuple[str, list[str]]:
    scope = session.get("target_scope") if isinstance(session.get("target_scope"), dict) else {}
    downstream_targets = {
        *[str(item) for item in scope.get("downstream_service_ids", [])],
        *[str(item) for item in scope.get("downstream_instance_ids", [])],
    }
    is_downstream = str(cluster.target) in downstream_targets
    if cluster.mechanism == "downstream_dependency_failure":
        return "downstream", ["network"]
    if cluster.mechanism == "same_host_cpu_contention":
        return "same_host", ["cpu"]
    if cluster.mechanism == "process_suspended":
        return ("downstream", ["runtime", "network"]) if is_downstream else ("self", ["runtime"])
    return "self", ["process"]


def _runtime_control_candidate(assessment: dict[str, Any]) -> dict[str, Any]:
    refs = _unique_strings(assessment.get("evidence_refs", []))
    target = str(assessment.get("claim_target") or assessment.get("root_entity") or "target_process")
    claim = str(assessment.get("diagnostic_claim") or assessment.get("summary") or "")
    return {
        "candidate_id": "runtime_control_process_suspended",
        "description": claim,
        "evidence_refs": refs,
        "missing_evidence": [],
        "score_components": {
            "rule_match": "high",
            "evidence_quality": "high",
            "baseline_support": "medium",
            "source_independence": "high",
        },
        "rank": 1,
        "confidence_level": assessment.get("confidence_level") or "高",
        "supporting_claims": [{"statement": claim, "evidence_refs": refs, "strength": "strong"}],
        "location_type": "self",
        "domain_type": "runtime",
        "classification": "runtime_stall",
        "root_entity": target,
        "max_supported_level": "process",
    }


def _probe_budget_phase(probe: dict[str, Any]) -> str:
    parameters = probe.get("parameters") if isinstance(probe, dict) else {}
    if not isinstance(parameters, dict):
        return "initial"
    phase = str(parameters.get("budget_phase") or "").strip().lower()
    if phase in {"initial", "followup"}:
        return phase
    return "followup" if parameters.get("parent_task_id") else "initial"


def _is_frozen_evidence_session(session: dict[str, Any] | None) -> bool:
    if not isinstance(session, dict):
        return False
    mode = session.get("diagnosis_mode")
    if mode is None:
        mode = (session.get("target_scope") or {}).get("diagnosis_mode")
    return mode == "frozen_evidence"


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
