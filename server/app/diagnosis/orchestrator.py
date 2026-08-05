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
from server.app.diagnosis.probe_registry import choose_probe_ids, get_probe
from server.app.diagnosis.schemas import (
    ApprovalRequest,
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
from server.app.schemas import CreateTaskRequest, MAX_SAMPLE_RATE, MAX_TASK_DURATION_SEC, MIN_SAMPLE_RATE


PLANNER_VERSION = "diagnosis-orchestrator-v1"
ACTIVE_TASK_STATUSES = {"PENDING", "RUNNING", "UPLOADING", "ANALYZING"}
TERMINAL_TASK_STATUSES = {"DONE", "FAILED"}
STRUCTURED_ARTIFACT_TYPES = {"top_json", "ebpf_metrics", "sys_metrics", "memory_json", "depth_evidence_json"}
ALLOWED_DIAGNOSIS_TRANSITIONS = {
    "CREATED": {"UNDERSTANDING", "USER_CANCELED", "FAILED"},
    "UNDERSTANDING": {"PLANNING", "NEEDS_SCOPE_CONFIRMATION", "TOPOLOGY_UNAVAILABLE", "FAILED"},
    "PLANNING": {"ANALYZING_EXISTING_DATA", "BUDGET_EXHAUSTED", "FAILED"},
    "ANALYZING_EXISTING_DATA": {"ANALYZING", "COLLECTING", "WAITING_APPROVAL", "INSUFFICIENT_EVIDENCE", "FAILED"},
    "COLLECTING": {"ANALYZING", "WAITING_APPROVAL", "NEED_MORE_EVIDENCE", "BUDGET_EXHAUSTED", "FAILED"},
    "ANALYZING": {"CONCLUDING", "WAITING_APPROVAL", "COLLECTING", "INSUFFICIENT_EVIDENCE", "PARTIAL_COMPLETED", "FAILED"},
    "WAITING_APPROVAL": {"COLLECTING", "NEED_MORE_EVIDENCE", "BUDGET_EXHAUSTED", "USER_CANCELED", "FAILED"},
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

        if intent.ambiguities or not target_scope["instances"]:
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
        duration_limit = min(
            int(session["resource_budget"].get("max_duration_minutes", 10)) * 60,
            int(session["resource_budget"].get("max_total_probe_cpu_seconds", 120)),
        )
        if used_duration + duration > duration_limit:
            self._transition(
                diagnosis_id,
                DiagnosisStatus.BUDGET_EXHAUSTED,
                "resource_budget_exhausted",
                {"probe_duration_limit_seconds": duration_limit},
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

        terminal_tasks = []
        active_tasks = []
        for task_id in child_ids:
            task = self.repo.tasks.get(task_id)
            if task is None:
                continue
            task_status = status_value(task.status)
            if task_status in TERMINAL_TASK_STATUSES:
                terminal_tasks.append(task)
            elif task_status in ACTIVE_TASK_STATUSES:
                active_tasks.append(task)

        if terminal_tasks:
            self._transition(diagnosis_id, DiagnosisStatus.ANALYZING, "evidence_analysis_started")
            informative = self._analyze_tasks(diagnosis_id, terminal_tasks)
            if informative:
                for probe in self.store.list_probes(diagnosis_id):
                    if probe["status"] == "WAITING_APPROVAL":
                        self.store.update_probe(probe["step_id"], status="SKIPPED")
                final_status = (
                    DiagnosisStatus.PARTIAL_COMPLETED
                    if any(status_value(task.status) == "FAILED" for task in terminal_tasks)
                    else DiagnosisStatus.COMPLETED
                )
                self._transition(diagnosis_id, DiagnosisStatus.CONCLUDING, "conclusion_generated")
                self._transition(diagnosis_id, final_status, "diagnosis_completed")
                return

        if active_tasks:
            if session["status"] != DiagnosisStatus.COLLECTING.value:
                self._transition(diagnosis_id, DiagnosisStatus.COLLECTING, "probe_started")
            return

        waiting = [probe for probe in self.store.list_probes(diagnosis_id) if probe["status"] == "WAITING_APPROVAL"]
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
        probe_ids = choose_probe_ids(symptom)
        planned: list[ProbePlan] = []
        r2_count = 0
        auto_count = 0
        planned_duration = 0
        duration_limit = min(budget.max_duration_minutes * 60, budget.max_total_probe_cpu_seconds)
        for index, instance in enumerate(instances):
            for probe_id in probe_ids:
                definition = get_probe(probe_id)
                if definition.risk_level == "R2":
                    if index > 0 or r2_count >= budget.max_medium_risk_probes:
                        continue
                    r2_count += 1
                elif auto_count >= budget.max_parallel_probes:
                    continue
                else:
                    auto_count += 1
                duration = min(definition.default_duration_seconds, definition.max_duration_seconds)
                if planned_duration + duration > duration_limit:
                    continue
                planned_duration += duration
                key = f"{diagnosis_id}:{probe_id}:{instance['instance_id']}"
                planned.append(ProbePlan(
                    step_id=f"step_{hashlib.sha256(key.encode()).hexdigest()[:14]}",
                    probe_id=probe_id,
                    target=instance,
                    parameters={"duration_sec": duration, "sample_rate": definition.default_sample_rate},
                    reason=f"用于区分 {', '.join(definition.applicable_hypotheses[:3])} 等候选假设",
                    risk_level=definition.risk_level,
                    requires_approval=definition.requires_approval,
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
            options={
                "diagnosis_id": step["diagnosis_id"],
                "diagnosis_step_id": step_id,
                "probe_id": definition.probe_id,
                "registered_probe": True,
                "collector_context": {
                    "task_id": step["diagnosis_id"],
                    "collector_kind": definition.runner_task_kind,
                    "target_pid": target["pid"],
                    "agent_id": target["agent_id"],
                    "service_id": target.get("service_id"),
                    "instance_id": target.get("instance_id"),
                    "host_id": target.get("host_id"),
                },
            },
        ))
        self.store.update_probe(step_id, status="SCHEDULED", task_id=task.id)
        self._append_child_task(step["diagnosis_id"], task.id, definition)

    def _append_child_task(self, diagnosis_id: str, task_id: str, definition) -> None:
        session = self.store.get_session(diagnosis_id)
        if session is None:
            return
        task_ids = list(session.get("child_task_ids", []))
        if task_id not in task_ids:
            task_ids.append(task_id)
        usage = dict(session.get("budget_used", {}))
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
        usage["probe_duration_seconds"] = usage.get("probe_duration_seconds", 0) + definition.default_duration_seconds
        self.store.update_session(
            diagnosis_id,
            child_task_ids=task_ids,
            budget_used=usage,
        )

    def _analyze_tasks(self, diagnosis_id: str, tasks: list[Any]) -> bool:
        all_candidates: list[dict[str, Any]] = []
        task_observations: list[dict[str, Any]] = []
        missing: list[str] = []
        failed_targets: list[str] = []
        for task in tasks:
            status = status_value(task.status)
            artifacts = self.repo.artifacts.get(task.id, [])
            evidence_ids = [self._add_task_evidence(diagnosis_id, task)]
            structured = self._structured_artifacts(artifacts)
            for artifact_type, value, artifact in structured:
                evidence_ids.append(self._add_artifact_evidence(
                    diagnosis_id, task, artifact_type, value, artifact,
                ))
            if status == "FAILED":
                failed_targets.append(f"{task.agent_id}:{task.target_pid}")
            if not structured:
                missing.append(f"{task.id}:structured_artifact")
                continue

            values = {kind: value for kind, value, _ in structured}
            task_observations.append(
                self._build_task_observation(diagnosis_id, task, values, evidence_ids)
            )
            task_events = [self.repo.as_dict(event) for event in self.repo.events if event.task_id == task.id]
            evidence = collect_evidence(
                task_id=task.id,
                task_record=task,
                top_functions=values.get("top_json") if isinstance(values.get("top_json"), list) else None,
                ebpf_metrics=values.get("ebpf_metrics") if isinstance(values.get("ebpf_metrics"), dict) else None,
                sys_metrics=values.get("sys_metrics") if isinstance(values.get("sys_metrics"), dict) else None,
                failure_events=[event.get("reason", "") for event in task_events if event.get("reason")],
                agent_stats=self.repo.agent_metrics.get(task.agent_id, {}),
                evidence_index=values.get("depth_evidence_json") if isinstance(values.get("depth_evidence_json"), dict) else {},
            )
            candidates = generate_candidates(evidence, self.repo.get_feedback_priors())
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
            deduped.append(candidate)
            if len(deduped) >= 3:
                break

        cluster_assessment = self._build_cluster_assessment(diagnosis_id, task_observations)
        diagnostic_commands = self._build_reviewable_commands(
            diagnosis_id,
            task_observations,
            cluster_assessment,
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
            "limitations": sorted(set(missing + (["部分目标采集失败"] if failed_targets else []))),
            "coverage": {
                "task_count": len(tasks),
                "failed_targets": failed_targets,
                "evidence_count": len(self.store.list_evidence(diagnosis_id)),
            },
        }
        self._append_conclusion(diagnosis_id, conclusion)
        self._update_hypotheses(diagnosis_id, deduped)
        return True

    def _build_task_observation(
        self,
        diagnosis_id: str,
        task,
        values: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        target = self._target_for_task(diagnosis_id, task)
        summary = _sys_summary(values.get("sys_metrics"))
        top_items = values.get("top_json") if isinstance(values.get("top_json"), list) else []
        top_name = str((top_items[0] or {}).get("name", "")) if top_items else ""
        top_percent = float((top_items[0] or {}).get("percent", 0.0) or 0.0) if top_items else 0.0
        pressure = _pressure_flags(summary, values)
        return {
            "task_id": task.id,
            "collector_type": task.collector_type,
            "target": target,
            "summary": summary,
            "top_function": {"name": top_name, "percent": top_percent},
            "pressure": pressure,
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

        target_hot = any(_has_self_hotspot(obs) for obs in target_obs)
        target_pressure = any(_has_pressure(obs) for obs in target_obs)
        neighbor_pressure = any(_has_pressure(obs) for obs in same_host_obs)
        downstream_pressure = any(_has_pressure(obs) for obs in downstream_obs)
        shared_iowait = (
            any(obs["pressure"].get("io_wait") for obs in target_obs)
            and any(obs["pressure"].get("io_wait") for obs in same_host_obs)
        )

        if shared_iowait:
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
            summary = "证据主要集中在目标实例自身，优先检查代码热点、线程竞争或进程资源压力。"
            if same_host_obs:
                ruled_out.append({
                    "hypothesis": "same_host_noisy_neighbor",
                    "reason": "同宿主观测未显示更强资源压力。",
                    "evidence_refs": all_refs,
                })

        return {
            "classification": classification,
            "confidence": round(confidence, 2),
            "confidence_level": _confidence_label(confidence),
            "summary": summary,
            "evidence_refs": all_refs,
            "compared_targets": compared,
            "ruled_out": ruled_out,
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
            "same_host_instance_ids": [item["instance_id"] for item in same_host],
            "downstream_service_ids": sorted(downstream_services),
            "max_topology_hops": budget.max_topology_hops,
        }

    def _build_hypotheses(self, symptom: str, target_scope: dict[str, Any]) -> list[dict[str, Any]]:
        base = {
            "cpu_saturation": ["CPU_SATURATION", "SELF_CODE_REGRESSION", "SAME_HOST_NOISY_NEIGHBOR"],
            "latency_increase": ["SELF_CODE_REGRESSION", "DOWNSTREAM_LATENCY", "SAME_HOST_NOISY_NEIGHBOR"],
            "io_degradation": ["HOST_DISK_CONTENTION", "SAME_HOST_NOISY_NEIGHBOR", "DOWNSTREAM_LATENCY"],
            "memory_pressure": ["HOST_MEMORY_PRESSURE", "MEMORY_LEAK", "SAME_HOST_NOISY_NEIGHBOR"],
            "noisy_neighbor": ["SAME_HOST_NOISY_NEIGHBOR", "HOST_DISK_CONTENTION", "TRAFFIC_SURGE"],
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
        targets = {(item["agent_id"], item["pid"]) for item in target_scope.get("instances", [])}
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
            return DiagnosisBudget(max_hosts=10, max_service_instances=20, max_parallel_probes=5, max_medium_risk_probes=2)
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
    top_items = values.get("top_json") if isinstance(values.get("top_json"), list) else []
    top_percent = _num((top_items[0] or {}).get("percent")) if top_items else 0.0
    return {
        "cpu": cpu_user + cpu_sys >= 75 or top_percent >= 45,
        "io_wait": cpu_iowait >= 20 or _has_ebpf_latency(values.get("ebpf_metrics")),
        "memory": rss_mb >= 1024 or (rss_max > 0 and rss_mb / max(rss_max, 1.0) >= 0.9),
        "fd": fd_count >= 1000 or (fd_max > 0 and fd_count / max(fd_max, 1.0) >= 0.9),
        "thread": threads >= 512,
        "load": load1m >= 4,
    }


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


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
