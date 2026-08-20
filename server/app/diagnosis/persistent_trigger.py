"""轻量触发器：只负责冻结可疑采证窗口，不输出归因结论。"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from server.app.diagnosis.collector_invocation import build_collector_invocation
from server.app.diagnosis.probe_registry import get_probe
from server.app.diagnosis.schemas import StrictModel
from server.app.schemas import CreateTaskRequest, MAX_SAMPLE_RATE, MAX_TASK_DURATION_SEC


TriggerType = Literal[
    "cpu_shift",
    "latency_shift",
    "thread_growth_shift",
    "io_wait_shift",
    "memory_growth_shift",
    "error_burst",
    "process_suspended",
    "cgroup_throttling",
    "queue_growth_shift",
]


class TriggerTarget(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128)
    target_pid: int = Field(gt=0, le=4194304)
    service_id: str | None = Field(default=None, max_length=128)
    instance_id: str | None = Field(default=None, max_length=128)
    endpoint: str | None = Field(default=None, max_length=256)


class MetricSample(StrictModel):
    observed_at: datetime | None = None
    cpu_percent: float | None = Field(default=None, ge=0)
    latency_ms_p99: float | None = Field(default=None, ge=0)
    thread_count: float | None = Field(default=None, ge=0)
    iowait_percent: float | None = Field(default=None, ge=0)
    rss_mb: float | None = Field(default=None, ge=0)
    error_count: float | None = Field(default=None, ge=0)
    process_state: str | None = Field(default=None, max_length=8)
    throttled_percent: float | None = Field(default=None, ge=0)
    queue_depth: float | None = Field(default=None, ge=0)


class MetricWindow(StrictModel):
    start: datetime
    end: datetime
    samples: list[MetricSample] = Field(default_factory=list)


class TriggerSignal(StrictModel):
    trigger_type: TriggerType
    metric: str
    baseline_value: float
    current_value: float
    absolute_delta: float
    relative_shift: float
    threshold: float
    robust_score: float | None = None
    detection_method: Literal["hard_state", "relative_shift", "robust_change"] = "relative_shift"


class TriggerEvent(StrictModel):
    trigger_event_id: str
    trigger_type: TriggerType
    target: TriggerTarget
    observed_at: datetime
    baseline_window: dict[str, Any]
    trigger_window: dict[str, Any]
    trigger_signal: TriggerSignal
    confidence: Literal["suspected"]
    action: Literal["start_collector_group"]


class TriggerEvaluationRequest(StrictModel):
    target: TriggerTarget
    baseline_window: MetricWindow
    trigger_window: MetricWindow
    target_config: dict[str, Any] = Field(default_factory=dict)
    scope_source: Literal["persistent_trigger", "watch_subscription"] = "persistent_trigger"
    watch_id: str | None = None


class TriggeredCollectorTask(StrictModel):
    task_id: str
    probe_id: str
    collector_type: str
    evidence_cohort_id: str
    trigger_event_id: str
    collector_invocation: dict[str, Any] = Field(default_factory=dict)
    status: Literal["PENDING", "RUNNING", "UPLOADING", "ANALYZING", "DONE", "FAILED"] = "PENDING"
    status_reason: str = ""
    artifact_types: list[str] = Field(default_factory=list)


class TriggerEvaluationResult(StrictModel):
    trigger_event: TriggerEvent | None = None
    evidence_cohort_id: str | None = None
    collector_tasks: list[TriggeredCollectorTask] = Field(default_factory=list)
    skipped_probe_ids: list[str] = Field(default_factory=list)
    detected_signals: list[TriggerSignal] = Field(default_factory=list)


_TRIGGER_THRESHOLDS: dict[TriggerType, tuple[str, float, float]] = {
    "cpu_shift": ("cpu_percent", 0.5, 10.0),
    "latency_shift": ("latency_ms_p99", 0.5, 20.0),
    "thread_growth_shift": ("thread_count", 0.35, 5.0),
    "io_wait_shift": ("iowait_percent", 0.5, 5.0),
    "memory_growth_shift": ("rss_mb", 0.25, 64.0),
    "error_burst": ("error_count", 1.0, 3.0),
    "cgroup_throttling": ("throttled_percent", 0.5, 5.0),
    "queue_growth_shift": ("queue_depth", 0.5, 3.0),
}

_TRIGGER_PRIORITY: tuple[TriggerType, ...] = (
    "process_suspended",
    "latency_shift",
    "error_burst",
    "cgroup_throttling",
    "queue_growth_shift",
    "cpu_shift",
    "io_wait_shift",
    "thread_growth_shift",
    "memory_growth_shift",
)

_COLLECTOR_GROUPS: dict[TriggerType, tuple[str, ...]] = {
    # A trigger keeps multiple industrial depth paths in the same cohort.
    # One stack source may be blocked by host policy while another still
    # produces useful evidence.
    "cpu_shift": (
        "host_process_metrics",
        "process_baseline_window",
        "process_off_cpu_profile",
        "process_trace_endpoint_profile",
        "process_cpu_profile",
    ),
    "latency_shift": (
        "host_process_metrics",
        "process_dependency_check",
        "process_log_scan",
        "process_trace_endpoint_profile",
        "process_off_cpu_profile",
        "process_baseline_window",
    ),
    "thread_growth_shift": (
        "host_process_metrics",
        "process_off_cpu_profile",
        "process_trace_endpoint_profile",
        "process_baseline_window",
    ),
    "io_wait_shift": (
        "host_process_metrics",
        "process_io_latency",
        "process_off_cpu_profile",
        "process_trace_endpoint_profile",
    ),
    "memory_growth_shift": (
        "host_process_metrics",
        "process_memory_map",
        "process_baseline_window",
        "process_log_scan",
    ),
    "error_burst": (
        "host_process_metrics",
        "process_log_scan",
        "process_dependency_check",
        "process_trace_endpoint_profile",
        "process_off_cpu_profile",
    ),
    "process_suspended": (
        "host_process_metrics",
        "process_off_cpu_profile",
        "process_trace_endpoint_profile",
        "process_log_scan",
    ),
    "cgroup_throttling": (
        "host_process_metrics",
        "process_cpu_profile",
        "process_trace_endpoint_profile",
    ),
    "queue_growth_shift": (
        "host_process_metrics",
        "process_trace_endpoint_profile",
        "process_off_cpu_profile",
    ),
}


def evaluate_persistent_trigger(
    request: TriggerEvaluationRequest,
    repo: Any,
    *,
    create_collector_tasks: bool = True,
    max_probe_risk_level: Literal["R1", "R2"] | None = None,
    suppressed_trigger_types: set[str] | None = None,
) -> TriggerEvaluationResult:
    """评估相对偏移并创建同窗采集任务。

    该函数故意不返回 diagnosis、root_cause 或 ranked_causes 字段；触发规则只选择采集器。
    """

    signals = detect_trigger_signals(request)
    if not signals:
        return TriggerEvaluationResult()
    signal = signals[0]
    if suppressed_trigger_types and signal.trigger_type in suppressed_trigger_types:
        return TriggerEvaluationResult()

    trigger_event = _build_trigger_event(request, signal)
    evidence_cohort_id = f"cohort_{uuid4().hex[:12]}"
    collector_tasks: list[TriggeredCollectorTask] = []
    skipped_probe_ids: list[str] = []

    if not create_collector_tasks:
        return TriggerEvaluationResult(
            trigger_event=trigger_event,
            evidence_cohort_id=evidence_cohort_id,
            collector_tasks=[],
            skipped_probe_ids=[],
            detected_signals=signals,
        )

    for probe_id in _COLLECTOR_GROUPS[signal.trigger_type]:
        probe = get_probe(probe_id)
        if max_probe_risk_level is not None and not _risk_allowed(probe.risk_level, max_probe_risk_level):
            skipped_probe_ids.append(probe_id)
            continue
        if not _agent_supports_probe(repo, request.target.agent_id, probe.required_capabilities):
            skipped_probe_ids.append(probe_id)
            continue
        collector_invocation = build_collector_invocation(
            scope_source=request.scope_source,
            collector_family=probe.runner_task_kind,
            probe_id=probe.probe_id,
            watch_id=request.watch_id,
            trigger_event_id=trigger_event.trigger_event_id,
            evidence_cohort_id=evidence_cohort_id,
            target_config=request.target_config,
            target_context={
                "agent_id": request.target.agent_id,
                "service_id": request.target.service_id,
                "instance_id": request.target.instance_id,
                "pid": request.target.target_pid,
                "endpoint": request.target.endpoint,
            },
        )

        task = repo.create_task(
            CreateTaskRequest(
                name=f"triggered {probe.runner_task_kind} for {trigger_event.trigger_event_id}",
                agent_id=request.target.agent_id,
                target_pid=request.target.target_pid,
                collector_type=probe.runner_task_kind,
                sample_rate=min(probe.default_sample_rate, MAX_SAMPLE_RATE),
                duration_sec=min(probe.default_duration_seconds, MAX_TASK_DURATION_SEC),
                options={
                    "watch_id": request.watch_id,
                    "trigger_event_id": trigger_event.trigger_event_id,
                    "evidence_cohort_id": evidence_cohort_id,
                    "collection_mode": "triggered_group",
                    "timing_relation": "same_window",
                    "trigger_type": signal.trigger_type,
                    "probe_id": probe.probe_id,
                    "registered_probe": True,
                    "trigger_only": True,
                    "root_cause_assertion": False,
                    "window_start": request.trigger_window.start.isoformat(),
                    "window_end": request.trigger_window.end.isoformat(),
                    "trigger_observed_at": trigger_event.observed_at.isoformat(),
                    "target_config": request.target_config,
                    "collector_invocation": collector_invocation,
                },
            )
        )
        collector_tasks.append(
            TriggeredCollectorTask(
                task_id=task.id,
                probe_id=probe.probe_id,
                collector_type=probe.runner_task_kind,
                evidence_cohort_id=evidence_cohort_id,
                trigger_event_id=trigger_event.trigger_event_id,
                collector_invocation=collector_invocation,
            )
        )

    return TriggerEvaluationResult(
        trigger_event=trigger_event,
        evidence_cohort_id=evidence_cohort_id,
        collector_tasks=collector_tasks,
        skipped_probe_ids=skipped_probe_ids,
        detected_signals=signals,
    )


def detect_trigger_signals(request: TriggerEvaluationRequest) -> list[TriggerSignal]:
    candidates: list[TriggerSignal] = []
    current_states = {
        str(sample.process_state or "").strip()
        for sample in request.trigger_window.samples
    }
    if current_states.intersection({"T", "t"}):
        candidates.append(TriggerSignal(
            trigger_type="process_suspended",
            metric="process_state",
            baseline_value=0.0,
            current_value=1.0,
            absolute_delta=1.0,
            relative_shift=1.0,
            threshold=1.0,
            detection_method="hard_state",
        ))
    for trigger_type, (metric, threshold, min_delta) in _TRIGGER_THRESHOLDS.items():
        baseline = _window_center(request.baseline_window, metric)
        current = _window_center(request.trigger_window, metric)
        if baseline is None or current is None:
            continue
        delta = current - baseline
        if delta < min_delta:
            continue
        relative_shift = delta / baseline if baseline > 0 else delta
        robust_score = _robust_change_score(request.baseline_window, metric, delta, min_delta)
        relative_passed = relative_shift >= threshold
        if not relative_passed and robust_score < 6.0:
            continue
        candidates.append(
            TriggerSignal(
                trigger_type=trigger_type,
                metric=metric,
                baseline_value=round(baseline, 4),
                current_value=round(current, 4),
                absolute_delta=round(delta, 4),
                relative_shift=round(relative_shift, 4),
                threshold=threshold,
                robust_score=round(robust_score, 4),
                detection_method="relative_shift" if relative_passed else "robust_change",
            )
        )

    priority = {trigger_type: index for index, trigger_type in enumerate(_TRIGGER_PRIORITY)}
    candidates.sort(
        key=lambda item: (
            priority[item.trigger_type],
            -item.relative_shift,
            item.metric,
        )
    )
    return candidates


def _window_center(window: MetricWindow, metric: str) -> float | None:
    values = [getattr(sample, metric) for sample in window.samples]
    numeric_values = [float(value) for value in values if value is not None]
    if not numeric_values:
        return None
    return median(numeric_values)


def _robust_change_score(
    window: MetricWindow,
    metric: str,
    delta: float,
    min_delta: float,
) -> float:
    values = [
        float(getattr(sample, metric))
        for sample in window.samples
        if getattr(sample, metric) is not None
    ]
    if not values:
        return 0.0
    center = median(values)
    mad = median(abs(value - center) for value in values)
    scale = max(1.4826 * mad, min_delta / 3.0, 0.001)
    return delta / scale


def _build_trigger_event(request: TriggerEvaluationRequest, signal: TriggerSignal) -> TriggerEvent:
    observed_at = request.trigger_window.end
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return TriggerEvent(
        trigger_event_id=f"evt_{uuid4().hex[:12]}",
        trigger_type=signal.trigger_type,
        target=request.target,
        observed_at=observed_at,
        baseline_window=_window_ref(request.baseline_window),
        trigger_window=_window_ref(request.trigger_window),
        trigger_signal=signal,
        confidence="suspected",
        action="start_collector_group",
    )


def _window_ref(window: MetricWindow) -> dict[str, Any]:
    return {
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "sample_count": len(window.samples),
    }


def _agent_supports_probe(repo: Any, agent_id: str, required_capabilities: list[str]) -> bool:
    agents = getattr(repo, "agents", {})
    agent = agents.get(agent_id) if isinstance(agents, dict) else None
    if agent is None:
        return False
    capabilities = set(getattr(agent, "capabilities", []) or [])
    return all(capability in capabilities for capability in required_capabilities)


def _risk_allowed(risk_level: str, max_risk_level: Literal["R1", "R2"]) -> bool:
    order = {"R1": 1, "R2": 2}
    return order.get(risk_level, 99) <= order[max_risk_level]
