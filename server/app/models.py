"""SQLAlchemy ORM 模型定义。

与 InMemoryRepository 的数据类结构对齐，
通过 SQLAlchemy 2.0 DeclarativeBase 映射。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _summarize_latest_conclusion(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {}

    retained = value.get("retained_conclusion")
    retained = retained if isinstance(retained, dict) else {}
    qualification = value.get("qualification")
    qualification = qualification if isinstance(qualification, dict) else {}
    boundary = value.get("qualification_boundary")
    boundary = boundary if isinstance(boundary, dict) else {}
    line_probe = value.get("line_probe_diagnostic")
    line_probe = line_probe if isinstance(line_probe, dict) else {}

    return {
        "version": value.get("version"),
        "generated_at": value.get("generated_at"),
        "summary": value.get("summary", ""),
        "headline": value.get("headline", ""),
        "classification": value.get("classification", ""),
        "why_it_happened": value.get("why_it_happened", ""),
        "ai_review_status": value.get("ai_review_status", ""),
        "retained_conclusion": {
            "candidate_id": retained.get("candidate_id"),
            "claim": retained.get("claim"),
            "supported_level": retained.get("supported_level"),
            "status": retained.get("status"),
            "confidence": retained.get("confidence"),
            "causal_status": retained.get("causal_status"),
            "evidence_refs": retained.get("evidence_refs", []),
        },
        "formal_root_cause": value.get("formal_root_cause"),
        "qualification_boundary": {
            "status": boundary.get("status"),
            "message": boundary.get("message"),
            "boundary_message": boundary.get("boundary_message"),
            "missing_evidence": boundary.get("missing_evidence", []),
        },
        "active_retained_candidate_id": value.get("active_retained_candidate_id"),
        "confidence_level": value.get("confidence_level", ""),
        "qualification": {
            "level": qualification.get("level"),
            "decision": qualification.get("decision"),
            "causal_status": qualification.get("causal_status"),
            "confidence": qualification.get("confidence"),
            "confidence_level": qualification.get("confidence_level"),
            "supported_level": qualification.get("supported_level"),
            "reason": qualification.get("reason"),
            "missing_evidence": qualification.get("missing_evidence", []),
        },
        "line_probe_diagnostic": {
            "status": line_probe.get("status"),
            "supported_level": line_probe.get("supported_level"),
            "source_snapshot_status": line_probe.get("source_snapshot_status"),
            "source_snapshot_completed": line_probe.get("source_snapshot_completed"),
            "runtime_line_candidate_count": line_probe.get("runtime_line_candidate_count"),
            "line_localization_status": line_probe.get("line_localization_status"),
            "source_verification_status": line_probe.get("source_verification_status"),
        },
        "coverage": value.get("coverage", {}),
        "abstained": bool(value.get("abstained")),
    }


# ── Agent ────────────────────────────────────────────────────────


class AgentModel(Base):
    __tablename__ = "agents"

    id = Column(String(128), primary_key=True)
    hostname = Column(String(256), nullable=False)
    ip_addr = Column(String(64), nullable=False)
    version = Column(String(32), default="0.1.0")
    os_info = Column(String(256), default="unknown")
    capabilities = Column(JSON, default=list)
    status = Column(String(16), default="ONLINE")
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "hostname": self.hostname,
            "ip_addr": self.ip_addr,
            "version": self.version,
            "os_info": self.os_info,
            "capabilities": self.capabilities or [],
            "status": self.status,
            "last_heartbeat_at": self.last_heartbeat_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ── Task ────────────────────────────────────────────────────────


class TaskModel(Base):
    __tablename__ = "tasks"

    id = Column(String(128), primary_key=True)
    name = Column(String(256), nullable=False)
    agent_id = Column(String(128), ForeignKey("agents.id"), nullable=False)
    target_pid = Column(Integer, nullable=False)
    collector_type = Column(String(32), nullable=False)
    sample_rate = Column(Integer, default=99)
    duration_sec = Column(Integer, default=15)
    status = Column(String(16), nullable=False)
    status_reason = Column(Text, default="")
    request_params = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    agent = relationship("AgentModel", lazy="selectin")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "agent_id": self.agent_id,
            "target_pid": self.target_pid,
            "collector_type": self.collector_type,
            "sample_rate": self.sample_rate,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "status_reason": self.status_reason or "",
            "request_params": self.request_params or {},
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ── 状态事件 ────────────────────────────────────────────────────


class StatusEventModel(Base):
    __tablename__ = "task_status_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(128), ForeignKey("tasks.id"), nullable=False, index=True)
    from_status = Column(String(16), nullable=True)
    to_status = Column(String(16), nullable=False)
    reason = Column(Text, nullable=False)
    actor = Column(String(16), nullable=False)
    meta_json = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
            "actor": self.actor,
            "metadata": self.meta_json or {},
            "created_at": self.created_at,
        }


# ── 审计日志 ────────────────────────────────────────────────────


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(32), nullable=False)
    message = Column(Text, nullable=False)
    agent_id = Column(String(128), nullable=True)
    task_id = Column(String(128), nullable=True)
    meta_json = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "message": self.message,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "metadata": self.meta_json or {},
            "created_at": self.created_at,
        }


# ── 产物 ───────────────────────────────────────────────────────


class ArtifactModel(Base):
    __tablename__ = "artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(128), ForeignKey("tasks.id"), nullable=False, index=True)
    artifact_type = Column(String(128), nullable=False)
    bucket = Column(String(64), default="mini-drop")
    object_key = Column(String(512), nullable=False)
    filename = Column(String(256), nullable=True)
    local_path = Column(String(512), nullable=True)
    content_type = Column(String(128), default="application/octet-stream")
    size_bytes = Column(Integer, default=0)
    meta_json = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "artifact_type": self.artifact_type,
            "bucket": self.bucket,
            "object_key": self.object_key,
            "filename": self.filename,
            "local_path": self.local_path,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "metadata": self.meta_json or {},
        }


# ── 智能归因 ───────────────────────────────────────────────────


class DiagnosisRunModel(Base):
    __tablename__ = "diagnosis_runs"

    id = Column(String(128), primary_key=True)
    task_id = Column(String(128), ForeignKey("tasks.id"), nullable=False, index=True)
    status = Column(String(32), nullable=False)
    model_name = Column(String(64), nullable=False)
    summary = Column(Text, default="")
    validated = Column(Integer, default=0)
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "status": self.status,
            "model_name": self.model_name,
            "summary": self.summary or "",
            "validated": bool(self.validated),
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class DiagnosisToolResultModel(Base):
    __tablename__ = "diagnosis_tool_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    diagnosis_id = Column(String(128), ForeignKey("diagnosis_runs.id"), nullable=False, index=True)
    tool_name = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False)
    evidence_ref = Column(String(128), nullable=False)
    input_json = Column(JSON, default=dict)
    output_json = Column(JSON, default=dict)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "status": self.status,
            "evidence_ref": self.evidence_ref,
            "input": self.input_json or {},
            "output": self.output_json or {},
            "error_message": self.error_message,
            "created_at": self.created_at,
        }


class DiagnosisReportModel(Base):
    __tablename__ = "diagnosis_reports"

    id = Column(String(128), primary_key=True)
    diagnosis_id = Column(String(128), ForeignKey("diagnosis_runs.id"), nullable=False, index=True)
    report_json = Column(JSON, default=dict)
    ranked_causes_json = Column(JSON, default=list)
    confidence = Column(Integer, default=0)
    not_enough_evidence = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "diagnosis_id": self.diagnosis_id,
            "report": self.report_json or {},
            "ranked_causes": self.ranked_causes_json or [],
            "confidence": (self.confidence or 0) / 1000,
            "not_enough_evidence": bool(self.not_enough_evidence),
            "created_at": self.created_at,
        }


class RepairPlanModel(Base):
    __tablename__ = "repair_plans"

    id = Column(String(128), primary_key=True)
    diagnosis_id = Column(String(128), ForeignKey("diagnosis_runs.id"), nullable=False, index=True)
    cause_id = Column(String(128), nullable=False)
    risk_level = Column(String(32), nullable=False)
    actions_json = Column(JSON, default=list)
    executed_actions_json = Column(JSON, default=list)
    requires_user_confirm = Column(Integer, default=1)
    status = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "diagnosis_id": self.diagnosis_id,
            "cause_id": self.cause_id,
            "risk_level": self.risk_level,
            "actions": self.actions_json or [],
            "executed_actions": self.executed_actions_json or [],
            "requires_user_confirm": bool(self.requires_user_confirm),
            "status": self.status,
            "created_at": self.created_at,
        }


class RCAFeedbackModel(Base):
    __tablename__ = "rca_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    diagnosis_id = Column(String(128), ForeignKey("diagnosis_runs.id"), nullable=False, index=True)
    task_id = Column(String(128), nullable=False, index=True)
    predicted_cause_id = Column(String(128), nullable=False)
    feedback_label = Column(String(32), nullable=False)
    corrected_cause_id = Column(String(128), nullable=True)
    feedback_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class RCAFeedbackWeightModel(Base):
    __tablename__ = "rca_feedback_weights"

    candidate_id = Column(String(128), primary_key=True)
    positive_count = Column(Integer, default=0)
    negative_count = Column(Integer, default=0)
    partial_count = Column(Integer, default=0)
    weight_delta = Column(Integer, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)


# ── AI Provider Profiles ─────────────────────────────────────────


class AIProviderProfileModel(Base):
    """OpenAI-compatible AI provider runtime configuration."""

    __tablename__ = "ai_provider_profiles"

    id = Column(String(128), primary_key=True)
    name = Column(String(128), nullable=False)
    provider_label = Column(String(128), nullable=False)
    base_url = Column(String(512), nullable=False)
    model = Column(String(128), nullable=False)
    api_key = Column(Text, nullable=False)
    enabled = Column(String(32), nullable=False)
    is_active = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    def to_safe_dict(self) -> dict:
        suffix = self.api_key[-4:] if self.api_key else ""
        return {
            "profile_id": self.id,
            "name": self.name,
            "provider_label": self.provider_label,
            "base_url": self.base_url,
            "model": self.model,
            "enabled": self.enabled,
            "is_active": bool(self.is_active),
            "has_api_key": bool(self.api_key),
            "api_key_hint": f"****{suffix}" if suffix else "",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_settings_dict(self) -> dict:
        return {
            "profile_id": self.id,
            "enabled": self.enabled,
            "provider": self.provider_label,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "source": "profile",
        }


# ── Agent 指标快照 ───────────────────────────────────────────────


class AgentMetricSnapshotModel(Base):
    """Agent 周期性资源开销快照，用于趋势分析和容量规划。"""

    __tablename__ = "agent_metric_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(String(128), ForeignKey("agents.id"), nullable=False, index=True)
    cpu_percent = Column(Integer, default=0)
    rss_mb = Column(Integer, default=0)
    read_kb_s = Column(Integer, default=0)
    write_kb_s = Column(Integer, default=0)
    children_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "cpu_percent": self.cpu_percent,
            "rss_mb": self.rss_mb,
            "read_kb_s": self.read_kb_s,
            "write_kb_s": self.write_kb_s,
            "children_count": self.children_count,
            "created_at": self.created_at,
        }


# ── AI 集群诊断控制层 ────────────────────────────────────────────


class TopologySnapshotModel(Base):
    """诊断创建时冻结的服务/实例/宿主机拓扑。"""

    __tablename__ = "topology_snapshots"

    id = Column(String(128), primary_key=True)
    effective_at = Column(DateTime(timezone=True), nullable=False)
    generated_at = Column(DateTime(timezone=True), nullable=False)
    nodes_json = Column(JSON, default=list)
    edges_json = Column(JSON, default=list)
    source_versions_json = Column(JSON, default=dict)
    confidence_summary_json = Column(JSON, default=dict)

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.id,
            "effective_at": self.effective_at,
            "generated_at": self.generated_at,
            "nodes": self.nodes_json or [],
            "edges": self.edges_json or [],
            "source_versions": self.source_versions_json or {},
            "confidence_summary": self.confidence_summary_json or {},
        }


class DiagnosisSessionModel(Base):
    """独立于单个采集 Task 的、可恢复的诊断工作流。"""

    __tablename__ = "diagnosis_sessions"

    id = Column(String(128), primary_key=True)
    creator_id = Column(String(128), nullable=False)
    raw_query = Column(Text, nullable=False)
    normalized_intent_json = Column(JSON, default=dict)
    target_scope_json = Column(JSON, default=dict)
    requested_time_range_json = Column(JSON, default=dict)
    effective_time_range_json = Column(JSON, default=dict)
    topology_snapshot_id = Column(
        String(128), ForeignKey("topology_snapshots.id"), nullable=True, index=True,
    )
    baseline_snapshot_id = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False)
    policy_profile = Column(String(64), nullable=False)
    risk_budget_json = Column(JSON, default=dict)
    resource_budget_json = Column(JSON, default=dict)
    budget_used_json = Column(JSON, default=dict)
    hypothesis_graph_json = Column(JSON, default=dict)
    child_task_ids_json = Column(JSON, default=list)
    conclusion_versions_json = Column(JSON, default=list)
    runner_control_json = Column(JSON, default=dict)
    model_version = Column(String(128), nullable=False)
    planner_version = Column(String(64), nullable=False)
    lease_owner = Column(String(128), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        target_scope = self.target_scope_json or {}
        return {
            "diagnosis_id": self.id,
            "creator_id": self.creator_id,
            "raw_query": self.raw_query,
            "normalized_intent": self.normalized_intent_json or {},
            "target_scope": target_scope,
            "diagnosis_mode": target_scope.get("diagnosis_mode", "live_collection"),
            "evidence_package_id": target_scope.get("evidence_package_id"),
            "evidence_cohort_id": target_scope.get("evidence_cohort_id"),
            "source_evidence_cohort_id": target_scope.get("source_evidence_cohort_id"),
            "source_incident_id": target_scope.get("source_incident_id"),
            "requested_time_range": self.requested_time_range_json or {},
            "effective_time_range": self.effective_time_range_json or {},
            "topology_snapshot_id": self.topology_snapshot_id,
            "baseline_snapshot_id": self.baseline_snapshot_id,
            "status": self.status,
            "policy_profile": self.policy_profile,
            "risk_budget": self.risk_budget_json or {},
            "resource_budget": self.resource_budget_json or {},
            "budget_used": self.budget_used_json or {},
            "hypothesis_graph": self.hypothesis_graph_json or {},
            "child_task_ids": self.child_task_ids_json or [],
            "conclusion_versions": self.conclusion_versions_json or [],
            "runner_control": self.runner_control_json or {},
            "model_version": self.model_version,
            "planner_version": self.planner_version,
            "lease_owner": self.lease_owner,
            "lease_until": self.lease_until,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_summary_dict(self) -> dict:
        target_scope = self.target_scope_json or {}
        child_task_ids = self.child_task_ids_json or []
        conclusion_versions = self.conclusion_versions_json or []
        latest_conclusion = _summarize_latest_conclusion(
            conclusion_versions[-1] if conclusion_versions else None
        )
        return {
            "diagnosis_id": self.id,
            "creator_id": self.creator_id,
            "raw_query": self.raw_query,
            "normalized_intent": self.normalized_intent_json or {},
            "target_scope": target_scope,
            "diagnosis_mode": target_scope.get("diagnosis_mode", "live_collection"),
            "evidence_package_id": target_scope.get("evidence_package_id"),
            "evidence_cohort_id": target_scope.get("evidence_cohort_id"),
            "source_evidence_cohort_id": target_scope.get("source_evidence_cohort_id"),
            "source_incident_id": target_scope.get("source_incident_id"),
            "status": self.status,
            "policy_profile": self.policy_profile,
            "budget_used": self.budget_used_json or {},
            "child_task_count": len(child_task_ids),
            "latest_conclusion": latest_conclusion,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DiagnosisEventModel(Base):
    __tablename__ = "diagnosis_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    diagnosis_id = Column(
        String(128), ForeignKey("diagnosis_sessions.id"), nullable=False, index=True,
    )
    event_type = Column(String(64), nullable=False)
    from_status = Column(String(32), nullable=True)
    to_status = Column(String(32), nullable=False)
    payload_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "diagnosis_id": self.diagnosis_id,
            "event_type": self.event_type,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "payload": self.payload_json or {},
            "created_at": self.created_at,
        }


class ProbeExecutionModel(Base):
    """一次受控探针计划/审批/执行记录；step id 同时作为幂等键。"""

    __tablename__ = "diagnosis_probe_executions"

    id = Column(String(128), primary_key=True)
    diagnosis_id = Column(
        String(128), ForeignKey("diagnosis_sessions.id"), nullable=False, index=True,
    )
    probe_id = Column(String(128), nullable=False)
    target_json = Column(JSON, default=dict)
    parameters_json = Column(JSON, default=dict)
    reason = Column(Text, nullable=False)
    risk_level = Column(String(8), nullable=False)
    status = Column(String(32), nullable=False)
    requires_approval = Column(Integer, default=0)
    task_id = Column(String(128), ForeignKey("tasks.id"), nullable=True, index=True)
    approved_by = Column(String(128), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    evidence_status = Column(String(32), nullable=True)
    evidence_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "step_id": self.id,
            "diagnosis_id": self.diagnosis_id,
            "probe_id": self.probe_id,
            "target": self.target_json or {},
            "parameters": self.parameters_json or {},
            "reason": self.reason,
            "risk_level": self.risk_level,
            "status": self.status,
            "requires_approval": bool(self.requires_approval),
            "task_id": self.task_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "evidence_status": self.evidence_status,
            "evidence_reason": self.evidence_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DiagnosisEvidenceModel(Base):
    """可追溯到 Task/Artifact 的不可变证据摘要。"""

    __tablename__ = "diagnosis_evidence"

    id = Column(String(128), primary_key=True)
    diagnosis_id = Column(
        String(128), ForeignKey("diagnosis_sessions.id"), nullable=False, index=True,
    )
    source_type = Column(String(32), nullable=False)
    source_system = Column(String(64), nullable=False)
    target_json = Column(JSON, default=dict)
    event_time_range_json = Column(JSON, default=dict)
    ingestion_time = Column(DateTime(timezone=True), nullable=False)
    query_or_probe = Column(String(256), nullable=False)
    raw_artifact_ref = Column(String(512), nullable=True)
    derived_artifact_ref = Column(String(512), nullable=True)
    derivation_version = Column(String(64), nullable=False)
    evidence_index_json = Column(JSON, default=dict)
    observed_value_json = Column(JSON, default=dict)
    baseline_value_json = Column(JSON, default=dict)
    anomaly_score_json = Column(JSON, default=dict)
    data_quality_json = Column(JSON, default=dict)
    integrity_hash = Column(String(80), nullable=False)
    claim_links_json = Column(JSON, default=list)

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.id,
            "diagnosis_id": self.diagnosis_id,
            "source_type": self.source_type,
            "source_system": self.source_system,
            "target": self.target_json or {},
            "event_time_range": self.event_time_range_json or {},
            "ingestion_time": self.ingestion_time,
            "query_or_probe": self.query_or_probe,
            "raw_artifact_ref": self.raw_artifact_ref,
            "derived_artifact_ref": self.derived_artifact_ref,
            "derivation_version": self.derivation_version,
            "evidence_index": self.evidence_index_json or {},
            "observed_value": self.observed_value_json or {},
            "baseline_value": self.baseline_value_json or {},
            "anomaly_score": self.anomaly_score_json or {},
            "data_quality": self.data_quality_json or {},
            "integrity_hash": self.integrity_hash,
            "claim_links": self.claim_links_json or [],
        }


# ── Persistent Watch ─────────────────────────────────────────────


class WatchSubscriptionModel(Base):
    __tablename__ = "watch_subscriptions"

    id = Column(String(128), primary_key=True)
    name = Column(String(128), nullable=False)
    target_json = Column(JSON, default=dict)
    target_config_json = Column(JSON, default=dict)
    watch_profile = Column(String(32), nullable=False)
    enabled_collectors_json = Column(JSON, default=list)
    retention_seconds = Column(Integer, nullable=False)
    trigger_policy = Column(String(32), nullable=False)
    trigger_action = Column(String(32), nullable=False)
    auto_diagnosis_enabled = Column(Boolean, nullable=False, default=True)
    status = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    incidents_count = Column(Integer, default=0)
    last_evaluated_at = Column(DateTime(timezone=True), nullable=True)
    last_trigger_event_id = Column(String(128), nullable=True)
    last_evidence_cohort_id = Column(String(128), nullable=True)
    last_trigger_type = Column(String(64), nullable=True)

    def to_dict(self) -> dict:
        return {
            "watch_id": self.id,
            "name": self.name,
            "target": self.target_json or {},
            "target_config": self.target_config_json or {},
            "watch_profile": self.watch_profile,
            "enabled_collectors": self.enabled_collectors_json or [],
            "retention_seconds": self.retention_seconds,
            "trigger_policy": self.trigger_policy,
            "trigger_action": self.trigger_action,
            "auto_diagnosis_enabled": self.auto_diagnosis_enabled is not False,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "incidents_count": self.incidents_count or 0,
            "last_evaluated_at": self.last_evaluated_at,
            "last_trigger_event_id": self.last_trigger_event_id,
            "last_evidence_cohort_id": self.last_evidence_cohort_id,
            "last_trigger_type": self.last_trigger_type,
        }


class WatchIncidentModel(Base):
    __tablename__ = "watch_incidents"

    id = Column(String(128), primary_key=True)
    watch_id = Column(
        String(128), ForeignKey("watch_subscriptions.id"), nullable=False, index=True,
    )
    trigger_event_id = Column(String(128), nullable=False)
    evidence_cohort_id = Column(String(128), nullable=False)
    trigger_type = Column(String(64), nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=False)
    window_end = Column(DateTime(timezone=True), nullable=False)
    trigger_observed_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(32), nullable=False)
    analysis_status = Column(String(32), nullable=False)
    snapshot_id = Column(String(128), nullable=True)
    snapshot_refs_json = Column(JSON, default=list)
    structured_evidence_json = Column(JSON, default=dict)
    collector_tasks_json = Column(JSON, default=list)
    analysis_session_id = Column(String(128), nullable=True)
    analysis_result_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    episode_id = Column(String(128), nullable=True, index=True)
    episode_status = Column(String(32), nullable=False, default="AGGREGATING")
    aggregation_deadline = Column(DateTime(timezone=True), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    recovery_observations = Column(Integer, nullable=False, default=0)
    occurrence_count = Column(Integer, nullable=False, default=1)
    diagnosis_eligible = Column(Boolean, nullable=False, default=False)
    impact_status = Column(String(32), nullable=False, default="impact_unconfirmed")
    anomaly_points_json = Column(JSON, default=list)
    conclusion_revision_count = Column(Integer, nullable=False, default=0)

    def to_dict(self) -> dict:
        return {
            "incident_id": self.id,
            "watch_id": self.watch_id,
            "trigger_event_id": self.trigger_event_id,
            "evidence_cohort_id": self.evidence_cohort_id,
            "trigger_type": self.trigger_type,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "trigger_observed_at": self.trigger_observed_at,
            "status": self.status,
            "analysis_status": self.analysis_status,
            "snapshot_id": self.snapshot_id,
            "snapshot_refs": self.snapshot_refs_json or [],
            "structured_evidence": self.structured_evidence_json or {},
            "collector_tasks": self.collector_tasks_json or [],
            "analysis_session_id": self.analysis_session_id,
            "analysis_result": self.analysis_result_json,
            "created_at": self.created_at,
            "episode_id": self.episode_id,
            "episode_status": self.episode_status or "AGGREGATING",
            "aggregation_deadline": self.aggregation_deadline,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "recovery_observations": self.recovery_observations or 0,
            "occurrence_count": self.occurrence_count or 1,
            "diagnosis_eligible": bool(self.diagnosis_eligible),
            "impact_status": self.impact_status or "impact_unconfirmed",
            "anomaly_points": self.anomaly_points_json or [],
            "conclusion_revision_count": self.conclusion_revision_count or 0,
        }


class WatchSnapshotModel(Base):
    __tablename__ = "watch_snapshots"

    id = Column(String(128), primary_key=True)
    watch_id = Column(
        String(128), ForeignKey("watch_subscriptions.id"), nullable=False, index=True,
    )
    incident_id = Column(
        String(128), ForeignKey("watch_incidents.id"), nullable=True, index=True,
    )
    metadata_json = Column(JSON, default=dict)
    samples_json = Column(JSON, default=list)
    structured_evidence_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.id,
            "watch_id": self.watch_id,
            "incident_id": self.incident_id,
            "metadata": self.metadata_json or {},
            "samples": self.samples_json or [],
            "structured_evidence": self.structured_evidence_json or {},
            "created_at": self.created_at,
        }
