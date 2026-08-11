"""AI 诊断 API、策略和工作流的严格数据契约。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """边界对象拒绝未知字段，避免模型输出被静默忽略。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DiagnosisStatus(str, Enum):
    CREATED = "CREATED"
    UNDERSTANDING = "UNDERSTANDING"
    NEEDS_SCOPE_CONFIRMATION = "NEEDS_SCOPE_CONFIRMATION"
    PLANNING = "PLANNING"
    ANALYZING_EXISTING_DATA = "ANALYZING_EXISTING_DATA"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COLLECTING = "COLLECTING"
    ANALYZING = "ANALYZING"
    NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"
    CONCLUDING = "CONCLUDING"
    COMPLETED = "COMPLETED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    PARTIAL_COMPLETED = "PARTIAL_COMPLETED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TOPOLOGY_UNAVAILABLE = "TOPOLOGY_UNAVAILABLE"
    USER_CANCELED = "USER_CANCELED"
    FAILED = "FAILED"


TERMINAL_DIAGNOSIS_STATUSES = {
    DiagnosisStatus.COMPLETED.value,
    DiagnosisStatus.INSUFFICIENT_EVIDENCE.value,
    DiagnosisStatus.PARTIAL_COMPLETED.value,
    DiagnosisStatus.BUDGET_EXHAUSTED.value,
    DiagnosisStatus.TOPOLOGY_UNAVAILABLE.value,
    DiagnosisStatus.USER_CANCELED.value,
    DiagnosisStatus.FAILED.value,
}


class TimeRange(StrictModel):
    start: datetime
    end: datetime
    source: Literal["user_expression", "request_context", "default_window"] = "request_context"

    @model_validator(mode="after")
    def validate_order(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("time_range 必须包含时区")
        if self.end <= self.start:
            raise ValueError("time_range.end 必须晚于 start")
        return self


class ServiceInstance(StrictModel):
    service_id: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=1, max_length=128)
    host_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    pid: int = Field(gt=0, le=4194304)
    container_id: Optional[str] = Field(default=None, max_length=128)
    environment: str = Field(default="unknown", min_length=1, max_length=64)


class DependencyEdge(StrictModel):
    source_service: str = Field(min_length=1, max_length=128)
    target_service: str = Field(min_length=1, max_length=128)
    relation: Literal[
        "CALLS", "READS_FROM", "WRITES_TO", "PUBLISHES_TO",
        "CONSUMES_FROM", "SHARES_DEPENDENCY",
    ] = "CALLS"
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    confidence: Literal["high", "medium", "low"] = "medium"
    source: str = Field(default="request_context", max_length=64)


class DiagnosisContext(StrictModel):
    service_id: Optional[str] = Field(default=None, max_length=128)
    environment: str = Field(default="unknown", min_length=1, max_length=64)
    time_range: Optional[TimeRange] = None
    instances: list[ServiceInstance] = Field(default_factory=list, max_length=100)
    dependencies: list[DependencyEdge] = Field(default_factory=list, max_length=200)


class DiagnosisBudget(StrictModel):
    max_hosts: int = Field(default=5, ge=1, le=20)
    max_service_instances: int = Field(default=10, ge=1, le=100)
    max_topology_hops: int = Field(default=1, ge=0, le=3)
    max_duration_minutes: int = Field(default=10, ge=1, le=60)
    max_parallel_probes: int = Field(default=3, ge=1, le=10)
    max_artifact_size_mb: int = Field(default=500, ge=1, le=4096)
    max_model_calls: int = Field(default=6, ge=0, le=30)
    max_medium_risk_probes: int = Field(default=1, ge=0, le=5)
    max_total_probe_cpu_seconds: int = Field(default=120, ge=0, le=3600)


class CreateDiagnosisRequest(StrictModel):
    query: str = Field(min_length=3, max_length=2000)
    context: DiagnosisContext = Field(default_factory=DiagnosisContext)
    budget_profile: Literal["production_safe", "staging", "development"] = "production_safe"
    auto_execute_policy: Literal["safe_only", "all_registered", "manual"] | None = None
    budget: Optional[DiagnosisBudget] = None


class ApprovalRequest(StrictModel):
    step_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approve", "reject"]
    scope: Literal["single_execution"] = "single_execution"
    approver_id: str = Field(default="demo_user", min_length=1, max_length=128)


class DiagnosisScope(StrictModel):
    self: bool = True
    same_host: bool = True
    downstream_hops: int = Field(default=1, ge=0, le=3)


class DiagnosisConstraints(StrictModel):
    no_high_risk_probe: bool = True
    registered_probes_only: bool = True
    no_automatic_remediation: bool = True


class NormalizedIntent(StrictModel):
    intent_type: Literal["performance_diagnosis"] = "performance_diagnosis"
    symptom: Literal[
        "latency_increase", "cpu_saturation", "io_degradation",
        "memory_pressure", "noisy_neighbor", "unknown_performance_issue",
    ]
    target_service: Optional[str] = None
    environment: str = "unknown"
    time_range: TimeRange
    scope: DiagnosisScope = Field(default_factory=DiagnosisScope)
    constraints: DiagnosisConstraints = Field(default_factory=DiagnosisConstraints)
    ambiguities: list[str] = Field(default_factory=list)


class ProbeDefinition(StrictModel):
    probe_id: str
    name: str
    purpose: str
    runner_task_kind: str
    supported_platforms: list[str]
    required_capabilities: list[str]
    risk_level: Literal["R0", "R1", "R2", "R3"]
    requires_approval: bool
    default_duration_seconds: int
    max_duration_seconds: int
    default_sample_rate: int = 99
    estimated_overhead: dict[str, str] = Field(default_factory=dict)
    applicable_hypotheses: list[str] = Field(default_factory=list)


class ProbePlan(StrictModel):
    step_id: str
    probe_id: str
    target: dict[str, Any]
    parameters: dict[str, Any]
    reason: str
    risk_level: Literal["R0", "R1", "R2", "R3"]
    requires_approval: bool
