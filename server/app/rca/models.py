"""智能归因数据模型。

定义证据、候选原因、置信度校准和诊断报告的全部结构。
LLM 输出的 JSON 必须符合 DiagnosisReport 的 schema，
工程校验层通过 Pydantic 解析进行格式和引用完整性检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ── 输入侧 ──


class EvidenceInput(BaseModel):
    """归因输入的全部结构化证据（传给 LLM 前构造）。"""

    task_metadata: dict = Field(default_factory=dict)
    top_functions: list[dict] = Field(default_factory=list)
    ebpf_metrics: Optional[dict] = None
    off_cpu_wait_json: Optional[dict] = None
    sys_metrics: Optional[dict] = None
    baseline_diff: Optional[dict] = None
    agent_stats: Optional[dict] = None
    evidence_index: Optional[dict] = None
    tool_results: list[dict] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    failure_events: list[str] = Field(default_factory=list)
    analysis_result: Optional[dict] = None


class CandidateCause(BaseModel):
    """规则引擎生成的候选归因。"""

    candidate_id: str
    description: str
    evidence_refs: list[str] = Field(default_factory=list)
    rule_score: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_evidence: list[str] = Field(default_factory=list)


@dataclass
class CalibratedCause:
    """经过置信度校准器加权后的候选原因。"""

    candidate_id: str
    description: str
    evidence_refs: list[str]
    final_confidence: float
    rule_score: float
    evidence_quality: float
    baseline_support: float
    cross_collector_agreement: float
    feedback_prior: float
    missing_evidence: list[str] = field(default_factory=list)


class AnalysisFact(BaseModel):
    """Analyzer 从现有证据中提取的原子事实。"""

    fact_id: str
    source: str
    evidence_ref: str
    value: Any
    status: Literal["observed", "normal"] = "observed"
    threshold_band: Literal["below", "near", "above", "unknown"] = "unknown"
    evidence_window: dict[str, Any] = Field(default_factory=dict)
    timing_relation: Literal[
        "same_window",
        "pre_trigger_window",
        "post_trigger_window",
        "delayed_followup",
        "stale_window",
        "unknown",
    ] = "unknown"


class AnalysisSymptom(BaseModel):
    """由事实支持的异常现象。"""

    symptom_id: str
    symptom_type: str
    severity: Literal["low", "medium", "high"]
    fact_ids: list[str] = Field(default_factory=list)


class AnalysisLocalization(BaseModel):
    """现有证据能够支撑的最大定位层级。"""

    level: Literal["resource", "process", "thread", "syscall", "function", "call_path", "line"]
    target: Optional[str] = None
    fact_ids: list[str] = Field(default_factory=list)
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    evidence_refs: list[str] = Field(default_factory=list)


class AnalysisTreeDecision(BaseModel):
    """AI 树中的单个门控或叶子决策。"""

    node_id: str
    level: Literal["resource", "process", "thread", "syscall", "function", "call_path", "line"]
    branch_key: str
    decision: Literal["continue", "downgrade", "stop"]
    leaf_status: Literal["clear_leaf", "conservative_leaf", "unknown_leaf"]
    conflict_type: Optional[str] = None
    evidence_family: list[str] = Field(default_factory=list)
    conflict_candidates: list[str] = Field(default_factory=list)
    next_evidence_requests: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str = ""


class AnalysisGraphEntity(BaseModel):
    """轻量图中的一个上下文实体。"""

    entity_id: str
    entity_type: Literal["trace", "endpoint", "service", "instance", "context", "call_path", "function", "line"]
    label: str
    evidence_ref: str
    context_id: Optional[str] = None


class AnalysisGraphLink(BaseModel):
    """轻量图中的稳定回连关系。"""

    source_id: str
    target_id: str
    relation: str
    evidence_ref: str
    stable: bool = True


class GuardedAttribution(BaseModel):
    """候选原因经过事实和现象约束后的归因状态。"""

    candidate_id: str
    status: Literal["supported", "weakened", "missing_evidence", "forbidden"]
    supporting_fact_ids: list[str] = Field(default_factory=list)
    opposing_fact_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    max_supported_level: Literal["resource", "process", "thread", "syscall", "function", "call_path", "line"] = "resource"


class EvidenceChallengeTest(BaseModel):
    """移除单条事实后归因结论的变化。"""

    removed_fact_id: str
    result: Literal["unchanged", "confidence_down", "downgrade_level", "forbidden"]
    meaning: str


class EvidenceChallenge(BaseModel):
    """归因结论对关键事实的依赖关系。"""

    candidate_id: str
    critical_fact_ids: list[str] = Field(default_factory=list)
    critical_fact_groups: list[list[str]] = Field(default_factory=list)
    tests: list[EvidenceChallengeTest] = Field(default_factory=list)
    conclusion_stability: Literal["stable", "fragile", "unsupported_without_key_fact"]
    evidence_window: dict[str, Any] = Field(default_factory=dict)
    timing_relation: Literal[
        "same_window",
        "pre_trigger_window",
        "post_trigger_window",
        "delayed_followup",
        "stale_window",
        "unknown",
    ] = "unknown"
    delayed_followup_reproduction_status: Literal[
        "not_applicable",
        "reproduced",
        "not_reproduced",
        "unknown",
    ] = "not_applicable"


class ConclusionBoundary(BaseModel):
    """最终报告允许表达的结论范围。"""

    can_claim_root_cause: bool
    max_supported_level: Literal["resource", "process", "thread", "syscall", "function", "call_path", "line"] = "resource"
    reason: str
    conclusion_window: dict[str, Any] = Field(default_factory=dict)
    timing_relation: Literal[
        "same_window",
        "pre_trigger_window",
        "post_trigger_window",
        "delayed_followup",
        "stale_window",
        "unknown",
    ] = "unknown"
    delayed_followup_reproduction_status: Literal[
        "not_applicable",
        "reproduced",
        "not_reproduced",
        "unknown",
    ] = "not_applicable"
    non_refutable_evidence_boundaries: list[str] = Field(default_factory=list)


class EvidenceAttributionResult(BaseModel):
    """供报告生成使用的受证据约束分析结果。"""

    facts: list[AnalysisFact] = Field(default_factory=list)
    symptoms: list[AnalysisSymptom] = Field(default_factory=list)
    localizations: list[AnalysisLocalization] = Field(default_factory=list)
    ai_tree: list[AnalysisTreeDecision] = Field(default_factory=list)
    graph_entities: list[AnalysisGraphEntity] = Field(default_factory=list)
    graph_links: list[AnalysisGraphLink] = Field(default_factory=list)
    attributions: list[GuardedAttribution] = Field(default_factory=list)
    evidence_challenges: list[EvidenceChallenge] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    blocked_upgrades: list[str] = Field(default_factory=list)
    collection_gaps: list[str] = Field(default_factory=list)
    graph_extension_points: list[str] = Field(default_factory=list)
    allowed_cause_ids: list[str] = Field(default_factory=list)
    primary_cause_id: Optional[str] = None
    stability_score: float = 0.0
    primary_cause_reason: str = ""
    conclusion_boundary: ConclusionBoundary


class FeedbackPrior(BaseModel):
    """从 rca_feedback_weights 表查询到的历史反馈先验。"""

    candidate_id: str
    positive_count: int = 0
    negative_count: int = 0
    weight_delta: float = 0.0


# ── 输出侧 ──


class CauseEntry(BaseModel):
    """LLM 输出的单条归因结论。"""

    cause_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    claim: str
    evidence_refs: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)


class DiagnosisReport(BaseModel):
    """LLM 输出的完整归因报告，schema 注入到 system prompt 中。

    工程校验层通过此 Pydantic 模型解析 LLM JSON 输出：
      - 字段类型不匹配 → 校验失败 → 触发自修复重试
      - evidence_refs 不存在于输入证据中 → 校验失败 → 触发修复
    """

    summary: str
    ranked_causes: list[CauseEntry]
    facts: list[str]
    not_enough_evidence: bool = False
    analysis_result: Optional[EvidenceAttributionResult] = None
    symptoms: list[AnalysisSymptom] = Field(default_factory=list)
    localizations: list[AnalysisLocalization] = Field(default_factory=list)
    ai_tree: list[AnalysisTreeDecision] = Field(default_factory=list)
    graph_entities: list[AnalysisGraphEntity] = Field(default_factory=list)
    graph_links: list[AnalysisGraphLink] = Field(default_factory=list)
    attributions: list[GuardedAttribution] = Field(default_factory=list)
    evidence_challenges: list[EvidenceChallenge] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    blocked_upgrades: list[str] = Field(default_factory=list)
    collection_gaps: list[str] = Field(default_factory=list)
    graph_extension_points: list[str] = Field(default_factory=list)
    structured_evidence: Optional[dict[str, Any]] = None
    primary_cause_id: Optional[str] = None
    stability_score: float = 0.0
    primary_cause_reason: str = ""
    secondary_causes: list[str] = Field(default_factory=list)
    correlated_symptoms: list[str] = Field(default_factory=list)
    unsupported_causes: list[str] = Field(default_factory=list)
    conclusion_boundary: Optional[ConclusionBoundary] = None


class ValidatedReport(BaseModel):
    """校验通过并保存到数据库的报告。"""

    task_id: str
    model_name: str
    evidence_snapshot: dict
    report: DiagnosisReport
    validated: bool = True
    validation_issues: list[str] = Field(default_factory=list)
    retry_count: int = 0


class ToolResult(BaseModel):
    """一次 RCA 工具调用结果，作为可引用证据链的一部分。"""

    tool_name: str
    status: str
    evidence_ref: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None


class RepairAction(BaseModel):
    """单个修复动作。safe_auto 可自动执行，其余只生成建议。"""

    action_id: str
    action_type: str
    risk_level: str
    description: str
    command: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "planned"
    result: Optional[str] = None


class RepairPlan(BaseModel):
    """诊断后的修复计划。"""

    plan_id: str
    task_id: str
    cause_id: str
    risk_level: str
    actions: list[RepairAction] = Field(default_factory=list)
    requires_user_confirm: bool = True
    status: str = "planned"


class DiagnosisOutcome(BaseModel):
    """一次完整诊断的工程化输出。"""

    report: ValidatedReport
    tool_results: list[ToolResult] = Field(default_factory=list)
    repair_plan: Optional[RepairPlan] = None


class RCAFeedback(BaseModel):
    """用户对归因报告的反馈。"""

    task_id: str
    report_id: str
    predicted_cause_id: str
    predicted_confidence: float
    feedback_label: str  # correct / wrong / partial / unknown
    corrected_cause_id: Optional[str] = None
    feedback_note: Optional[str] = None
