"""Persistent Agent watch subscriptions and lightweight runtime glue."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from server.app.diagnosis.persistent_trigger import (
    MetricSample,
    MetricWindow,
    TriggeredCollectorTask,
    TriggerEvaluationRequest,
    TriggerEvaluationResult,
    TriggerTarget,
    evaluate_persistent_trigger,
)
from server.app.diagnosis.rolling_buffer import (
    FrozenSnapshot,
    RollingEvidenceBuffer,
    RollingSample,
    structure_snapshot_evidence,
)
from server.app.diagnosis.schemas import StrictModel


WatchStatus = Literal["active", "disabled"]
WatchProfile = Literal["low_cost_default", "latency_sensitive", "resource_guarded"]
TriggerAction = Literal["freeze_only", "freeze_and_safe_probe", "auto_all_registered", "manual_approval"]
IncidentStatus = Literal["frozen", "collecting", "ready_for_analysis", "analyzing", "analyzed", "needs_evidence", "stale"]


class WatchTarget(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128)
    target_pid: int = Field(gt=0, le=4194304)
    service_id: str | None = Field(default=None, max_length=128)
    instance_id: str | None = Field(default=None, max_length=128)
    endpoint: str | None = Field(default=None, max_length=256)


class CreateWatchSubscriptionRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    target: WatchTarget
    target_config: dict[str, Any] = Field(default_factory=dict)
    watch_profile: WatchProfile = "low_cost_default"
    enabled_collectors: list[str] = Field(
        default_factory=lambda: ["sys_metrics", "light_stack", "trace_window"],
        max_length=16,
    )
    retention_seconds: int = Field(default=120, ge=30, le=1800)
    trigger_policy: Literal["relative_shift_only"] = "relative_shift_only"
    trigger_action: TriggerAction = "freeze_and_safe_probe"


class WatchSubscription(StrictModel):
    watch_id: str
    name: str
    target: WatchTarget
    target_config: dict[str, Any] = Field(default_factory=dict)
    watch_profile: WatchProfile
    enabled_collectors: list[str]
    retention_seconds: int
    trigger_policy: Literal["relative_shift_only"]
    trigger_action: TriggerAction
    status: WatchStatus
    created_at: datetime
    updated_at: datetime
    incidents_count: int = 0
    last_evaluated_at: datetime | None = None
    last_trigger_event_id: str | None = None
    last_evidence_cohort_id: str | None = None
    last_trigger_type: str | None = None


class WatchLease(StrictModel):
    watch_id: str
    agent_id: str
    target: WatchTarget
    target_config: dict[str, Any] = Field(default_factory=dict)
    watch_profile: WatchProfile
    enabled_collectors: list[str]
    retention_seconds: int
    state: Literal["watching"]
    poll_interval_seconds: int


class WatchEvaluationRequest(StrictModel):
    baseline_window: MetricWindow
    trigger_window: MetricWindow


class WatchEvaluationResult(StrictModel):
    watch: WatchSubscription
    trigger: TriggerEvaluationResult
    incident: "WatchIncident | None" = None


class WatchIncident(StrictModel):
    incident_id: str
    watch_id: str
    trigger_event_id: str
    evidence_cohort_id: str
    trigger_type: str
    window_start: datetime
    window_end: datetime
    trigger_observed_at: datetime
    status: IncidentStatus
    analysis_status: Literal["not_started", "analyzing", "analyzed", "needs_evidence"] = "not_started"
    snapshot_id: str | None = None
    snapshot_refs: list[dict[str, Any]] = Field(default_factory=list)
    structured_evidence: dict[str, Any] = Field(default_factory=dict)
    collector_tasks: list[TriggeredCollectorTask] = Field(default_factory=list)
    analysis_session_id: str | None = None
    analysis_result: dict[str, Any] | None = None
    created_at: datetime


class WatchRegistry:
    """In-memory control-plane registry for watch subscriptions."""

    def __init__(self) -> None:
        self._items: dict[str, WatchSubscription] = {}
        self._incidents: dict[str, list[WatchIncident]] = {}

    def create(self, payload: CreateWatchSubscriptionRequest) -> WatchSubscription:
        now = _now_utc()
        watch = WatchSubscription(
            watch_id=f"watch_{uuid4().hex[:12]}",
            name=payload.name,
            target=payload.target,
            target_config=payload.target_config,
            watch_profile=payload.watch_profile,
            enabled_collectors=list(dict.fromkeys(payload.enabled_collectors)),
            retention_seconds=payload.retention_seconds,
            trigger_policy=payload.trigger_policy,
            trigger_action=payload.trigger_action,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self._items[watch.watch_id] = watch
        self._incidents[watch.watch_id] = []
        return watch

    def list(self, *, agent_id: str | None = None, include_disabled: bool = False) -> list[WatchSubscription]:
        items = list(self._items.values())
        if agent_id:
            items = [item for item in items if item.target.agent_id == agent_id]
        if not include_disabled:
            items = [item for item in items if item.status == "active"]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def get(self, watch_id: str) -> WatchSubscription | None:
        return self._items.get(watch_id)

    def clear(self) -> None:
        self._items.clear()
        self._incidents.clear()

    def disable(self, watch_id: str) -> WatchSubscription | None:
        watch = self._items.get(watch_id)
        if watch is None:
            return None
        updated = watch.model_copy(update={"status": "disabled", "updated_at": _now_utc()})
        self._items[watch_id] = updated
        return updated

    def update_after_evaluation(
        self,
        watch_id: str,
        result: TriggerEvaluationResult,
        incident: WatchIncident | None = None,
    ) -> WatchSubscription:
        watch = self._items[watch_id]
        update = {"last_evaluated_at": _now_utc(), "updated_at": _now_utc()}
        if result.trigger_event is not None:
            update["last_trigger_event_id"] = result.trigger_event.trigger_event_id
            update["last_trigger_type"] = result.trigger_event.trigger_type
        if result.evidence_cohort_id is not None:
            update["last_evidence_cohort_id"] = result.evidence_cohort_id
        if incident is not None:
            self._incidents.setdefault(watch_id, []).append(incident)
            update["incidents_count"] = len(self._incidents[watch_id])
        updated = watch.model_copy(update=update)
        self._items[watch_id] = updated
        return updated

    def list_incidents(self, watch_id: str) -> list[WatchIncident]:
        return sorted(
            self._incidents.get(watch_id, []),
            key=lambda item: item.created_at,
            reverse=True,
        )

    def get_incident(self, incident_id: str) -> WatchIncident | None:
        for incidents in self._incidents.values():
            for incident in incidents:
                if incident.incident_id == incident_id:
                    return incident
        return None

    def update_incident_analysis(
        self,
        incident_id: str,
        *,
        analysis_status: Literal["not_started", "analyzing", "analyzed", "needs_evidence"],
        analysis_session_id: str | None = None,
        analysis_result: dict[str, Any] | None = None,
    ) -> WatchIncident:
        for watch_id, incidents in self._incidents.items():
            for index, incident in enumerate(incidents):
                if incident.incident_id != incident_id:
                    continue
                update: dict[str, Any] = {
                    "analysis_status": analysis_status,
                }
                if analysis_session_id is not None:
                    update["analysis_session_id"] = analysis_session_id
                if analysis_result is not None:
                    update["analysis_result"] = analysis_result
                    update["status"] = "needs_evidence" if analysis_status == "needs_evidence" else "analyzed"
                updated = incident.model_copy(update=update)
                incidents[index] = updated
                return updated
        raise KeyError(incident_id)


class PersistentAgentRuntime:
    """Runtime facade that turns active watches into trigger evaluations."""

    def __init__(
        self,
        registry: WatchRegistry,
        repo,
        rolling_buffer: RollingEvidenceBuffer | None = None,
    ) -> None:
        self.registry = registry
        self.repo = repo
        self.rolling_buffer = rolling_buffer or RollingEvidenceBuffer()

    def list_leases(self, agent_id: str) -> list[WatchLease]:
        leases: list[WatchLease] = []
        for watch in self.registry.list(agent_id=agent_id):
            leases.append(
                WatchLease(
                    watch_id=watch.watch_id,
                    agent_id=watch.target.agent_id,
                    target=watch.target,
                    target_config=watch.target_config,
                    watch_profile=watch.watch_profile,
                    enabled_collectors=watch.enabled_collectors,
                    retention_seconds=watch.retention_seconds,
                    state="watching",
                    poll_interval_seconds=_poll_interval_for(watch.watch_profile),
                )
            )
        return leases

    def evaluate(self, watch_id: str, payload: WatchEvaluationRequest) -> WatchEvaluationResult:
        watch = self.registry.get(watch_id)
        if watch is None:
            raise KeyError(watch_id)
        if watch.status != "active":
            raise ValueError("watch subscription is disabled")

        trigger_result = evaluate_persistent_trigger(
            TriggerEvaluationRequest(
                target=TriggerTarget(**watch.target.model_dump()),
                baseline_window=payload.baseline_window,
                trigger_window=payload.trigger_window,
                target_config=watch.target_config,
                scope_source="watch_subscription",
                watch_id=watch.watch_id,
            ),
            self.repo,
            create_collector_tasks=watch.trigger_action in {"freeze_and_safe_probe", "auto_all_registered"},
            max_probe_risk_level="R1" if watch.trigger_action == "freeze_and_safe_probe" else None,
        )
        incident = None
        if trigger_result.trigger_event is not None and trigger_result.evidence_cohort_id is not None:
            snapshot = self._freeze_snapshot_from_window(watch, payload, trigger_result)
            incident = _build_incident(watch, trigger_result, snapshot)
        updated = self.registry.update_after_evaluation(watch_id, trigger_result, incident)
        return WatchEvaluationResult(watch=updated, trigger=trigger_result, incident=incident)

    def _freeze_snapshot_from_window(
        self,
        watch: WatchSubscription,
        payload: WatchEvaluationRequest,
        trigger_result: TriggerEvaluationResult,
    ) -> FrozenSnapshot:
        target_key = _target_key(watch.target)
        for sample in payload.trigger_window.samples:
            rolling_sample = _metric_sample_to_rolling_sample(sample, payload.trigger_window.end)
            self.rolling_buffer.append(target_key, rolling_sample)
        return self.rolling_buffer.freeze_snapshot(
            target_key=target_key,
            trigger_event_id=trigger_result.trigger_event.trigger_event_id,
            evidence_cohort_id=trigger_result.evidence_cohort_id,
            trigger_observed_at=trigger_result.trigger_event.observed_at,
        )


def _poll_interval_for(profile: WatchProfile) -> int:
    if profile == "latency_sensitive":
        return 5
    if profile == "resource_guarded":
        return 30
    return 10


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _target_key(target: WatchTarget) -> str:
    return f"{target.agent_id}:{target.target_pid}"


def _metric_sample_to_rolling_sample(sample: MetricSample, fallback_observed_at: datetime) -> RollingSample:
    observed_at = sample.observed_at or fallback_observed_at
    return RollingSample(
        observed_at=observed_at,
        family="metrics",
        payload={key: value for key, value in sample.model_dump(mode="json").items() if value is not None},
    )


def _build_incident(
    watch: WatchSubscription,
    trigger_result: TriggerEvaluationResult,
    snapshot: FrozenSnapshot,
) -> WatchIncident:
    trigger_event = trigger_result.trigger_event
    status: IncidentStatus = "collecting" if trigger_result.collector_tasks else "frozen"
    structured = structure_snapshot_evidence(
        f"watch:{watch.watch_id}:snapshot:{snapshot.metadata.snapshot_id}",
        snapshot,
    )
    return WatchIncident(
        incident_id=f"inc_{uuid4().hex[:12]}",
        watch_id=watch.watch_id,
        trigger_event_id=trigger_event.trigger_event_id,
        evidence_cohort_id=trigger_result.evidence_cohort_id,
        trigger_type=trigger_event.trigger_type,
        window_start=snapshot.metadata.window_start,
        window_end=snapshot.metadata.window_end,
        trigger_observed_at=trigger_event.observed_at,
        status=status,
        snapshot_id=snapshot.metadata.snapshot_id,
        snapshot_refs=snapshot.metadata.artifact_refs,
        structured_evidence=structured.model_dump(mode="json"),
        collector_tasks=trigger_result.collector_tasks,
        created_at=_now_utc(),
    )
