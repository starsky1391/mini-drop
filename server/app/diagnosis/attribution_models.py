"""统一证据归因契约。

这些模型只描述证据和资格状态，不承载场景到根因的固定映射。
旧 RCA 字段继续保留；本模块作为跨场景的稳定边界。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


AttributionLevel = Literal[
    "resource",
    "host",
    "process",
    "thread",
    "syscall",
    "dependency",
    "service",
    "endpoint",
    "function",
    "call_path",
    "line",
]

RelationKind = Literal[
    "calls",
    "triggers",
    "activates",
    "propagates_to",
    "amplifies",
    "waits_on",
    "retains",
    "explains",
    "acquires",
    "holds",
    "releases",
]


class EvidenceQualityEntry(BaseModel):
    evidence_ref: str
    family: str = ""
    status: Literal["valid", "partial", "empty", "blocked", "stale", "unknown"] = "unknown"
    reason: str = ""
    window: dict[str, Any] = Field(default_factory=dict)


class EvidenceSignal(BaseModel):
    signal_id: str
    signal_type: str
    value: Any = None
    evidence_refs: list[str] = Field(default_factory=list)
    window: dict[str, Any] = Field(default_factory=dict)
    status: Literal["observed", "normal", "unknown"] = "unknown"


class CostCenterCandidate(BaseModel):
    candidate_id: str
    level: AttributionLevel
    target: str
    evidence_refs: list[str] = Field(default_factory=list)
    source: str = ""
    primitive: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class TriggerCandidate(BaseModel):
    candidate_id: str
    statement: str
    evidence_refs: list[str] = Field(default_factory=list)
    status: Literal["observed", "unproven", "unknown"] = "unknown"


class ImpactCandidate(BaseModel):
    candidate_id: str
    statement: str
    evidence_refs: list[str] = Field(default_factory=list)
    status: Literal["observed", "unproven", "unknown"] = "unknown"


class ObservedRelation(BaseModel):
    relation_id: str
    relation: RelationKind
    source_ref: str
    target_ref: str
    evidence_refs: list[str] = Field(default_factory=list)
    status: Literal["observed", "unproven", "contradicted"] = "unproven"
    statement: str = ""


class AttributionNode(BaseModel):
    node_id: str
    role: Literal["cost_center", "trigger", "mechanism", "impact", "repair_cluster", "runtime", "source"] = "runtime"
    symbol: str = ""
    file: str = ""
    line: int | None = Field(default=None, ge=1)
    supported_level: AttributionLevel = "resource"
    evidence_refs: list[str] = Field(default_factory=list)
    source_status: Literal["none", "unproven", "supported", "verified", "contradicted"] = "none"
    window: dict[str, Any] = Field(default_factory=dict)
    label: str = ""


class AttributionEdge(BaseModel):
    edge_id: str
    from_node: str
    relation: RelationKind
    to_node: str
    evidence_refs: list[str] = Field(default_factory=list)
    same_window: bool | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: Literal["observed", "supported", "verified", "unproven", "contradicted"] = "unproven"


class RepairCluster(BaseModel):
    cluster_id: str
    node_refs: list[str] = Field(default_factory=list)
    mechanism_refs: list[str] = Field(default_factory=list)
    source_relation_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    status: Literal["candidate", "supported", "blocked"] = "candidate"
    reason: str = ""


class ScenarioFacts(BaseModel):
    schema_version: str = "2.0"
    symptom_signals: list[EvidenceSignal] = Field(default_factory=list)
    cost_centers: list[CostCenterCandidate] = Field(default_factory=list)
    trigger_candidates: list[TriggerCandidate] = Field(default_factory=list)
    impact_candidates: list[ImpactCandidate] = Field(default_factory=list)
    observed_relations: list[ObservedRelation] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    evidence_quality: list[EvidenceQualityEntry] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_window: dict[str, Any] = Field(default_factory=dict)


class SourceRelation(BaseModel):
    relation_id: str
    relation: RelationKind
    source_ref: str
    target_ref: str
    evidence_refs: list[str] = Field(default_factory=list)
    file_path: str = ""
    line_number: int | None = Field(default=None, ge=1)
    status: Literal["verified", "supported", "unproven", "contradicted"] = "unproven"
    statement: str = ""


class AttributionGraph(BaseModel):
    schema_version: str = "2.0"
    graph_id: str
    target: dict[str, Any] = Field(default_factory=dict)
    facts: ScenarioFacts = Field(default_factory=ScenarioFacts)
    nodes: list[AttributionNode] = Field(default_factory=list)
    runtime_relations: list[AttributionEdge] = Field(default_factory=list)
    source_relations: list[SourceRelation] = Field(default_factory=list)
    causal_edges: list[AttributionEdge] = Field(default_factory=list)
    repair_clusters: list[RepairCluster] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    graph_relations: list[ObservedRelation] = Field(default_factory=list)


class QualificationResult(BaseModel):
    """统一的 L0-L3 资格结果，所有展示层应读取同一结果。"""

    schema_version: str = "2.0"
    level: Literal["L0", "L1", "L2", "L3"] = "L0"
    qualification: Literal[
        "observation",
        "partial_localization",
        "mechanism_hypothesis",
        "formal_root_cause",
    ] = "observation"
    decision: Literal["continue_probe", "conclude", "abstain"] = "abstain"
    causal_status: Literal["supported", "unproven", "contradicted", "inconclusive"] = "inconclusive"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_level: Literal["高", "中", "低", "不可判断"] = "不可判断"
    target: dict[str, Any] = Field(default_factory=dict)
    window: dict[str, Any] = Field(default_factory=dict)
    symptom_refs: list[str] = Field(default_factory=list)
    cost_center_refs: list[str] = Field(default_factory=list)
    trigger_refs: list[str] = Field(default_factory=list)
    mechanism_refs: list[str] = Field(default_factory=list)
    impact_refs: list[str] = Field(default_factory=list)
    source_relation_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    disconfirming_evidence_refs: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    eligible_candidate_ids: list[str] = Field(default_factory=list)
    supported_level: AttributionLevel = "resource"
    reason: str = ""


class ProbeManifestEntry(BaseModel):
    """AI 可见的探针能力声明，不是根因白名单。"""

    probe_id: str
    evidence_family: str
    name: str
    capability_role: Literal[
        "symptom",
        "localization",
        "mechanism",
        "source_relation",
        "impact",
        "context",
    ] = "context"
    purpose: str
    cannot_establish: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    input_requirements: list[str] = Field(default_factory=list)
    quality_gate: list[str] = Field(default_factory=list)
    next_probe_hints: list[str] = Field(default_factory=list)
    can_answer: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    required_target_fields: list[str] = Field(default_factory=list)
    risk_level: Literal["R0", "R1", "R2", "R3"]
    auto_executable_when_policy_all_registered: bool = False
    max_duration_seconds: int = 0
    output_contract: str = ""
    may_help_distinguish: list[str] = Field(default_factory=list)
    # 兼容旧客户端和旧提示词；不再作为根因白名单使用。
    applicable_hypotheses: list[str] = Field(default_factory=list)
