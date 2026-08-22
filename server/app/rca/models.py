"""智能归因数据模型。

定义证据、候选原因、置信度校准和诊断报告的全部结构。
LLM 输出的 JSON 必须符合 DiagnosisReport 的 schema，
工程校验层通过 Pydantic 解析进行格式和引用完整性检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


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
    source_context: Optional[dict[str, Any]] = None


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

    level: Literal["resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"]
    target: Optional[str] = None
    fact_ids: list[str] = Field(default_factory=list)
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    evidence_refs: list[str] = Field(default_factory=list)


class EvidenceQuality(BaseModel):
    """事实证据的质量摘要，不表达因果裁决。"""

    evidence_ref: str
    quality: Literal["high", "medium", "low", "blocked", "unknown"] = "unknown"
    reason: str = ""


class LocalizationBoundary(BaseModel):
    """Analyzer 当前能够支持的最大定位范围。"""

    level: Literal["resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"] = "resource"
    target: Optional[str] = None
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class CandidateHint(BaseModel):
    """值得调查的方向；不能直接成为根因候选。"""

    hint_id: str
    hint_type: Literal["observation", "mechanism", "localization"] = "observation"
    statement: str
    status: Literal["unproven"] = "unproven"
    evidence_refs: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class AnalyzerFactContext(BaseModel):
    """传给 AI 的 Analyzer 事实层上下文，不包含根因裁决。"""

    facts: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_quality: list[EvidenceQuality] = Field(default_factory=list)
    localization_boundary: Optional[LocalizationBoundary] = None
    missing_evidence: list[str] = Field(default_factory=list)
    available_probes: list[str] = Field(default_factory=list)
    candidate_hints: list[CandidateHint] = Field(default_factory=list)


class AnalysisTreeDecision(BaseModel):
    """AI 树中的单个门控或叶子决策。"""

    node_id: str
    level: Literal["resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"]
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
    max_supported_level: Literal["resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"] = "resource"


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
    max_supported_level: Literal["resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"] = "resource"
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


class AITreeSelfChallenge(BaseModel):
    """一个受控 AI 树节点内部的反问约束。"""

    why_this_claim: str = ""
    why_not_other_claims: str = ""
    supporting_evidence_refs: list[str] = Field(default_factory=list)
    opposing_evidence_refs: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    what_would_change_my_mind: str = ""


class AITreeCandidateNode(BaseModel):
    """受控 AI 树某一层里的候选结论。"""

    candidate_id: str
    generated_by: Literal[
        "analyzer_observation",
        "ai_candidate",
        "ai_guarded",
        "fallback_observation",
        "analyzer_fallback",
    ] = "ai_guarded"
    lineage_id: Optional[str] = None
    cluster_id: str = ""
    branch_id: str = ""
    parent_candidate_ids: list[str] = Field(default_factory=list)
    child_candidate_ids: list[str] = Field(default_factory=list)
    origin_parent_candidate_id: Optional[str] = None
    relation: Literal[
        "root",
        "alternative",
        "refinement",
        "evidence_context",
        "mechanism",
        "boundary",
        "rejected_alternative",
        "causal_convergence",
        "shared_evidence",
    ] = "alternative"
    node_type: Literal[
        "cluster_root",
        "coarse_candidate",
        "base_cause",
        "line_anchor",
        "call_path_context",
        "mechanism_explanation",
        "stop_boundary",
        "evidence_gap",
        "rejected_candidate",
        "observation",
        "orphan",
    ] = "base_cause"
    role: Literal["primary", "secondary", "rejected", "unknown"]
    claim: str
    supported_level: Literal["resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"] = "resource"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: Literal[
        "supported",
        "weakened",
        "missing_evidence",
        "forbidden",
        "contradicted",
        "rejected",
        "unknown",
        "blocked",
        "partial",
    ] = "unknown"
    claim_type: Literal[
        "root_cause",
        "complete_root_cause",
        "direct_root_cause",
        "complete_source_root_cause",
        "direct_failure_mechanism",
        "likely_root_cause",
        "partial_localization",
        "observation_only",
        "insufficient_for_root_cause",
        "abstention",
    ] = "partial_localization"
    causal_status: Literal["supported", "unproven", "contradicted", "inconclusive"] = "unproven"
    decision: Literal["continue_probe", "reject_candidate", "conclude", "abstain", "backtrack"] = "continue_probe"
    mechanism: str = ""
    target: str = ""
    primitive_kind: Optional[Literal[
        "wait_primitive",
        "scheduler_primitive",
        "syscall_primitive",
        "runtime_primitive",
    ]] = None
    depth_kind: Literal["base", "mechanism", "boundary"] = "base"
    conclusion_eligible: bool = False
    eligibility_reason: str = ""
    stop_reason: str = ""
    blocked_probe: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    self_challenge: AITreeSelfChallenge = Field(default_factory=AITreeSelfChallenge)

    @model_validator(mode="before")
    @classmethod
    def _read_legacy_origin_field(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if not data.get("origin_parent_candidate_id") and data.get("source_parent_candidate_id"):
            data["origin_parent_candidate_id"] = data["source_parent_candidate_id"]
        data.pop("source_parent_candidate_id", None)
        if not data.get("relation"):
            data["relation"] = cls._infer_relation(data)
        return data

    @model_validator(mode="after")
    def _validate_origin_parent(self):
        if self.relation == "root" and self.parent_candidate_ids:
            raise ValueError("root 节点不能携带 parent_candidate_ids")
        if self.origin_parent_candidate_id and self.origin_parent_candidate_id not in self.parent_candidate_ids:
            raise ValueError("origin_parent_candidate_id 必须是 parent_candidate_ids 中的唯一来源父节点")
        return self

    @classmethod
    def _infer_relation(cls, data: dict[str, Any]) -> str:
        candidate_id = str(data.get("candidate_id") or "")
        node_type = str(data.get("node_type") or "")
        depth_kind = str(data.get("depth_kind") or "")
        role = str(data.get("role") or "")
        status = str(data.get("status") or "")
        supported_level = str(data.get("supported_level") or "")
        if node_type == "cluster_root" or candidate_id.startswith("coarse_"):
            return "root"
        if node_type == "observation" or supported_level in {"resource", "host", "process"} and str(data.get("claim_type") or "") == "observation_only":
            return "evidence_context"
        if depth_kind == "mechanism" or node_type == "mechanism_explanation":
            return "mechanism"
        if depth_kind == "boundary" or node_type in {"stop_boundary", "evidence_gap"}:
            return "boundary"
        if role == "rejected" or status in {"contradicted", "rejected", "forbidden"} or node_type == "rejected_candidate":
            return "rejected_alternative"
        if node_type in {"line_anchor", "call_path_context"} or supported_level in {"line", "call_path"}:
            return "refinement"
        return "alternative"


class AITreeProbeResult(BaseModel):
    """受控 AI 树边上的探针结果摘要。"""

    status: Literal["completed", "inconclusive", "blocked", "failed", "reused", "not_started", "unknown"] = "unknown"
    evidence_refs: list[str] = Field(default_factory=list)
    blocked_reason: str = ""


class AITreeProbeEdge(BaseModel):
    """两层候选之间的探针请求与回流结果。"""

    edge_id: str
    from_layer_id: str
    to_layer_id: Optional[str] = None
    from_candidate_ids: list[str] = Field(default_factory=list)
    to_candidate_ids: list[str] = Field(default_factory=list)
    probe_requests: list[str] = Field(default_factory=list)
    probe_results: list[AITreeProbeResult] = Field(default_factory=list)
    status: Literal["completed", "inconclusive", "blocked", "failed", "reused", "not_started", "unknown"] = "unknown"
    evidence_refs: list[str] = Field(default_factory=list)
    reuse_status: Literal[
        "reuse_hit",
        "reuse_blocked_result",
        "reuse_miss",
        "reuse_expired",
        "reuse_forbidden",
        "not_checked",
    ] = "not_checked"
    effect: Literal["refined", "reranked", "rejected", "added_candidate", "rollback", "no_change", "pending"] = "pending"
    transition_type: Literal["probe", "refine", "backtrack", "boundary"] = "probe"
    reason: str = ""


class AITreeLayer(BaseModel):
    """受控 AI 树的一层候选集合。"""

    layer_id: str
    depth: int
    generated_by: Literal[
        "analyzer_observation",
        "ai_candidate",
        "ai_guarded",
        "fallback_observation",
        "analyzer_fallback",
    ] = "ai_guarded"
    summary: str = ""
    primary_causes: list[AITreeCandidateNode] = Field(default_factory=list)
    secondary_causes: list[AITreeCandidateNode] = Field(default_factory=list)
    rejected_causes: list[AITreeCandidateNode] = Field(default_factory=list)
    unknown_causes: list[AITreeCandidateNode] = Field(default_factory=list)


class AITreeBudgetSnapshot(BaseModel):
    """受控 AI 树本轮可用预算快照。"""

    max_ai_rounds: int = 3
    max_tree_depth: int = 4
    max_candidates_per_layer: int = 4
    max_probe_requests_per_round: int = 3
    max_total_llm_tokens: int = 12000
    max_wall_time_seconds: int = 180
    used_ai_rounds: int = 0
    used_probe_requests: int = 0


class ControlledAITree(BaseModel):
    """完整版受控 AI 树。"""

    tree_id: str
    schema_version: str = "1.1"
    tree_kind: Literal["session_main", "child_snapshot"] = "session_main"
    renderable: bool = True
    emitted_coarse_ids: list[str] = Field(default_factory=list)
    coarse_aliases: dict[str, str] = Field(default_factory=dict)
    source_context_hash: Optional[str] = None
    final_supported_level: Literal["resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"] = "resource"
    stop_reason: str = ""
    stop_source_candidate_ids: list[str] = Field(default_factory=list)
    budget: AITreeBudgetSnapshot = Field(default_factory=AITreeBudgetSnapshot)
    layers: list[AITreeLayer] = Field(default_factory=list)
    probe_edges: list[AITreeProbeEdge] = Field(default_factory=list)
    final_primary_causes: list[str] = Field(default_factory=list)
    final_secondary_causes: list[str] = Field(default_factory=list)
    final_rejected_causes: list[str] = Field(default_factory=list)
    final_unknown_causes: list[str] = Field(default_factory=list)
    localization_chain: list["CausalExplanationStep"] = Field(default_factory=list)


class CausalExplanationStep(BaseModel):
    """One evidence-backed step from cause to observed symptom."""

    step_id: str
    statement: str
    evidence_refs: list[str] = Field(default_factory=list)


class RootCauseRecommendation(BaseModel):
    """A non-executing investigation or remediation suggestion."""

    recommendation_type: Literal["investigation", "temporary_mitigation", "permanent_fix"]
    action: str
    rationale: str = ""


class RootCauseCluster(BaseModel):
    """One independently qualified causal mechanism in a diagnosis session."""

    cluster_id: str
    candidate_ids: list[str] = Field(default_factory=list)
    source_tree_candidate_ids: list[str] = Field(default_factory=list)
    role: Literal["primary", "contributing", "independent"] = "primary"
    causal_status: Literal["primary", "contributing", "independent", "unknown"] = "unknown"
    cause_level: Literal[
        "observation",
        "direct_failure_mechanism",
        "direct_root_cause",
        "complete_source_root_cause",
    ] = "observation"
    mechanism: str
    target: str
    claim: str
    why_it_happened: str = ""
    explained_symptoms: list[str] = Field(default_factory=list)
    causal_chain: list[CausalExplanationStep] = Field(default_factory=list)
    relation_to_primary: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    residual_unknowns: list[str] = Field(default_factory=list)
    recommendations: list[RootCauseRecommendation] = Field(default_factory=list)
    conclusion_eligible: bool = False
    qualification: Literal[
        "confirmed_root_cause",
        "possible_root_cause",
        "partial_localization",
        "observation",
    ] = "observation"


class RetainedConclusion(BaseModel):
    """当前证据下仍可展示、可继续验证的父结论。"""

    candidate_id: str
    claim: str
    supported_level: Literal["resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"] = "resource"
    status: Literal["supported", "partial", "blocked", "inconclusive", "observation"] = "observation"
    qualification: Literal["formal_root_cause", "possible_root_cause", "partial_localization", "observation"] = "observation"
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_candidate_id: str = ""
    inherited: bool = False
    fallback_mode: Literal["none", "inherit_parent"] = "none"


class QualificationBoundary(BaseModel):
    """说明当前分支为何没有继续升级，不承担主结论语义。"""

    status: Literal["blocked", "partial", "inconclusive", "none"] = "none"
    message: str = ""
    missing_evidence: list[str] = Field(default_factory=list)
    origin_parent_candidate_id: Optional[str] = None


class SessionConclusionReview(BaseModel):
    """Bounded session-level LLM adjudication result."""

    headline: str
    why_it_happened: str
    primary_cluster_id: Optional[str] = None
    cluster_roles: dict[str, Literal["primary", "contributing", "independent"]] = Field(default_factory=dict)
    causal_chain: list[CausalExplanationStep] = Field(default_factory=list)
    localization_chain: list[CausalExplanationStep] = Field(default_factory=list)
    ruled_out_summary: list[str] = Field(default_factory=list)
    residual_unknowns: list[str] = Field(default_factory=list)
    recommendations: dict[str, list[RootCauseRecommendation]] = Field(default_factory=dict)


class EvidenceAttributionResult(BaseModel):
    """供报告生成使用的受证据约束分析结果。"""

    facts: list[AnalysisFact] = Field(default_factory=list)
    symptoms: list[AnalysisSymptom] = Field(default_factory=list)
    localizations: list[AnalysisLocalization] = Field(default_factory=list)
    ai_tree: list[AnalysisTreeDecision] = Field(default_factory=list)
    controlled_ai_tree: Optional[ControlledAITree] = None
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
    controlled_ai_tree: Optional[ControlledAITree] = None
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
