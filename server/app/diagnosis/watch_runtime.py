"""Persistent Agent watch subscriptions and lightweight runtime glue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import threading
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from server.app.diagnosis.collector_invocation import build_collector_invocation
from server.app.diagnosis.persistent_trigger import (
    MetricSample,
    MetricWindow,
    TriggeredCollectorTask,
    TriggerEvaluationRequest,
    TriggerEvaluationResult,
    TriggerTarget,
    evaluate_persistent_trigger,
)
from server.app.diagnosis.probe_registry import get_probe
from server.app.diagnosis.rolling_buffer import (
    FrozenSnapshot,
    RollingEvidenceBuffer,
    RollingSample,
    snapshot_artifact_payloads,
    structure_snapshot_evidence,
)
from server.app.diagnosis.schemas import StrictModel
from server.app import storage
from server.app.schemas import MAX_SAMPLE_RATE, MAX_TASK_DURATION_SEC, CreateTaskRequest


WatchStatus = Literal["active", "disabled"]
WatchProfile = Literal["low_cost_default", "latency_sensitive", "resource_guarded"]
TriggerAction = Literal["freeze_only", "freeze_and_safe_probe", "auto_all_registered", "manual_approval"]
IncidentStatus = Literal[
    "frozen",
    "collecting",
    "ready_for_analysis",
    "analyzing",
    "analyzed",
    "needs_evidence",
    "analysis_failed",
    "stale",
]
EpisodeStatus = Literal["AGGREGATING", "DIAGNOSING", "ACTIVE", "RECOVERING", "CLOSED"]
ImpactStatus = Literal["hard_state", "impact_confirmed", "impact_unconfirmed"]


_FOLLOWUP_PROBES = {
    "cpu_profile": "process_cpu_profile",
    "off_cpu_wait_profile": "process_off_cpu_profile",
    "trace_endpoint_profile": "process_trace_endpoint_profile",
    "baseline_window_profile": "process_baseline_window",
    "python_runtime_profile": "process_python_runtime_profile",
    "log_scan": "process_log_scan",
    "dependency_check": "process_dependency_check",
    "redis_check": "process_redis_check",
}


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
    trigger_policy: Literal["relative_shift_only", "multi_signal_v1"] = "multi_signal_v1"
    trigger_action: TriggerAction = "freeze_and_safe_probe"
    auto_diagnosis_enabled: bool = True


class UpdateWatchSubscriptionRequest(StrictModel):
    auto_diagnosis_enabled: bool


class WatchSubscription(StrictModel):
    watch_id: str
    name: str
    target: WatchTarget
    target_config: dict[str, Any] = Field(default_factory=dict)
    watch_profile: WatchProfile
    enabled_collectors: list[str]
    retention_seconds: int
    trigger_policy: Literal["relative_shift_only", "multi_signal_v1"]
    trigger_action: TriggerAction
    auto_diagnosis_enabled: bool = True
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
    analysis_status: Literal[
        "not_started",
        "analyzing",
        "analyzed",
        "needs_evidence",
        "analysis_failed",
    ] = "not_started"
    snapshot_id: str | None = None
    snapshot_refs: list[dict[str, Any]] = Field(default_factory=list)
    structured_evidence: dict[str, Any] = Field(default_factory=dict)
    collector_tasks: list[TriggeredCollectorTask] = Field(default_factory=list)
    analysis_session_id: str | None = None
    analysis_result: dict[str, Any] | None = None
    created_at: datetime
    episode_id: str | None = None
    episode_status: EpisodeStatus = "AGGREGATING"
    aggregation_deadline: datetime | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    recovery_observations: int = 0
    occurrence_count: int = 1
    diagnosis_eligible: bool = False
    impact_status: ImpactStatus = "impact_unconfirmed"
    anomaly_points: list[dict[str, Any]] = Field(default_factory=list)
    conclusion_revision_count: int = 0


class WatchRegistry:
    """Watch cache backed by the control-plane repository when available."""

    def __init__(self, repo: Any | None = None) -> None:
        self.repo = repo
        self._items: dict[str, WatchSubscription] = {}
        self._incidents: dict[str, list[WatchIncident]] = {}
        self._loaded = False
        self._lock = threading.RLock()

    def load_persisted(self) -> None:
        if self._loaded:
            return
        if self.repo is None or not hasattr(self.repo, "list_watch_subscriptions"):
            self._loaded = True
            return
        for record in self.repo.list_watch_subscriptions(include_disabled=True):
            watch = WatchSubscription.model_validate(record)
            self._items[watch.watch_id] = watch
            self._incidents[watch.watch_id] = [
                WatchIncident.model_validate(item)
                for item in self.repo.list_watch_incidents(watch.watch_id)
            ]
        self._loaded = True

    def _persist_watch(self, watch: WatchSubscription) -> None:
        if self.repo is not None and hasattr(self.repo, "persist_watch_subscription"):
            self.repo.persist_watch_subscription(watch.model_dump(mode="json"))

    def _persist_incident(self, incident: WatchIncident) -> None:
        if self.repo is not None and hasattr(self.repo, "persist_watch_incident"):
            self.repo.persist_watch_incident(incident.model_dump(mode="json"))

    def create(self, payload: CreateWatchSubscriptionRequest) -> WatchSubscription:
        self.load_persisted()
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
            auto_diagnosis_enabled=payload.auto_diagnosis_enabled,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self._items[watch.watch_id] = watch
        self._incidents[watch.watch_id] = []
        self._persist_watch(watch)
        return watch

    def list(self, *, agent_id: str | None = None, include_disabled: bool = False) -> list[WatchSubscription]:
        self.load_persisted()
        items = list(self._items.values())
        if agent_id:
            items = [item for item in items if item.target.agent_id == agent_id]
        if not include_disabled:
            items = [item for item in items if item.status == "active"]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def get(self, watch_id: str) -> WatchSubscription | None:
        self.load_persisted()
        return self._items.get(watch_id)

    def clear(self) -> None:
        self._items.clear()
        self._incidents.clear()

    def disable(self, watch_id: str) -> WatchSubscription | None:
        self.load_persisted()
        watch = self._items.get(watch_id)
        if watch is None:
            return None
        updated = watch.model_copy(update={"status": "disabled", "updated_at": _now_utc()})
        self._items[watch_id] = updated
        self._persist_watch(updated)
        return updated

    def update_auto_diagnosis(self, watch_id: str, enabled: bool) -> WatchSubscription | None:
        self.load_persisted()
        with self._lock:
            watch = self._items.get(watch_id)
            if watch is None:
                return None
            updated = watch.model_copy(update={
                "auto_diagnosis_enabled": enabled,
                "updated_at": _now_utc(),
            })
            self._items[watch_id] = updated
            self._persist_watch(updated)
            return updated

    def update_after_evaluation(
        self,
        watch_id: str,
        result: TriggerEvaluationResult,
        incident: WatchIncident | None = None,
    ) -> WatchSubscription:
        self.load_persisted()
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
        self._persist_watch(updated)
        if incident is not None:
            self._persist_incident(incident)
        return updated

    def list_incidents(self, watch_id: str) -> list[WatchIncident]:
        self.load_persisted()
        return sorted(
            self._incidents.get(watch_id, []),
            key=lambda item: item.created_at,
            reverse=True,
        )

    def get_incident(self, incident_id: str) -> WatchIncident | None:
        self.load_persisted()
        for incidents in self._incidents.values():
            for incident in incidents:
                if incident.incident_id == incident_id:
                    return incident
        return None

    def get_active_episode(self, watch_id: str) -> WatchIncident | None:
        for incident in self.list_incidents(watch_id):
            if incident.episode_status != "CLOSED":
                return incident
        return None

    def replace_incident(self, incident: WatchIncident) -> WatchIncident:
        self.load_persisted()
        with self._lock:
            incidents = self._incidents.get(incident.watch_id, [])
            for index, current in enumerate(incidents):
                if current.incident_id == incident.incident_id:
                    incidents[index] = incident
                    self._persist_incident(incident)
                    return incident
        raise KeyError(incident.incident_id)

    def claim_episode_diagnosis(self, incident_id: str) -> tuple[WatchIncident, bool]:
        self.load_persisted()
        with self._lock:
            incident = self.get_incident(incident_id)
            if incident is None:
                raise KeyError(incident_id)
            if incident.analysis_session_id or incident.analysis_status in {"analyzing", "analyzed"}:
                return incident, False
            updated = incident.model_copy(update={
                "analysis_status": "analyzing",
                "episode_status": "DIAGNOSING",
            })
            return self.replace_incident(updated), True

    def ready_auto_diagnosis_episodes(self, now: datetime | None = None) -> list[WatchIncident]:
        now = now or _now_utc()
        ready: list[WatchIncident] = []
        for watch in self.list(include_disabled=False):
            if not watch.auto_diagnosis_enabled:
                continue
            episode = self.get_active_episode(watch.watch_id)
            if episode is None or not episode.diagnosis_eligible:
                continue
            if episode.analysis_status != "not_started":
                continue
            if (
                episode.aggregation_deadline
                and _as_utc(episode.aggregation_deadline) > _as_utc(now)
            ):
                continue
            if episode.collector_tasks and not all(
                task.status in {"DONE", "FAILED"} for task in episode.collector_tasks
            ):
                continue
            ready.append(episode)
        return ready

    def update_anomaly_explanation(
        self,
        incident_id: str,
        anomaly_point_id: str,
        explanation: dict[str, Any],
    ) -> WatchIncident:
        with self._lock:
            incident = self.get_incident(incident_id)
            if incident is None:
                raise KeyError(incident_id)
            points = [dict(item) for item in incident.anomaly_points]
            for index, point in enumerate(points):
                if point.get("anomaly_point_id") == anomaly_point_id:
                    points[index] = {**point, "explanation": explanation}
                    return self.replace_incident(incident.model_copy(update={"anomaly_points": points}))
        raise ValueError(anomaly_point_id)

    def update_incident_analysis(
        self,
        incident_id: str,
        *,
        analysis_status: Literal[
            "not_started",
            "analyzing",
            "analyzed",
            "needs_evidence",
            "analysis_failed",
        ],
        analysis_session_id: str | None = None,
        analysis_result: dict[str, Any] | None = None,
    ) -> WatchIncident:
        self.load_persisted()
        for watch_id, incidents in self._incidents.items():
            for index, incident in enumerate(incidents):
                if incident.incident_id != incident_id:
                    continue
                update: dict[str, Any] = {
                    "analysis_status": analysis_status,
                }
                if analysis_status == "analyzing":
                    update["episode_status"] = "DIAGNOSING"
                elif analysis_status in {"analyzed", "needs_evidence", "analysis_failed"}:
                    update["episode_status"] = "ACTIVE"
                if analysis_session_id is not None:
                    update["analysis_session_id"] = analysis_session_id
                if analysis_result is not None:
                    update["analysis_result"] = analysis_result
                    if analysis_status == "needs_evidence":
                        update["status"] = "needs_evidence"
                    elif analysis_status == "analyzed":
                        update["status"] = "analyzed"
                    elif analysis_status == "analysis_failed":
                        update["status"] = "analysis_failed"
                updated = incident.model_copy(update=update)
                incidents[index] = updated
                self._persist_incident(updated)
                return updated
        raise KeyError(incident_id)

    def update_incident_collection(
        self,
        incident_id: str,
        *,
        structured_evidence: dict[str, Any],
        collector_tasks: list[TriggeredCollectorTask],
        status: IncidentStatus,
    ) -> WatchIncident:
        self.load_persisted()
        for incidents in self._incidents.values():
            for index, incident in enumerate(incidents):
                if incident.incident_id != incident_id:
                    continue
                updated = incident.model_copy(update={
                    "structured_evidence": structured_evidence,
                    "collector_tasks": collector_tasks,
                    "status": status,
                })
                incidents[index] = updated
                self._persist_incident(updated)
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

    def evaluate(
        self,
        watch_id: str,
        payload: WatchEvaluationRequest,
        *,
        suppress_repeated_trigger: bool = False,
    ) -> WatchEvaluationResult:
        watch = self.registry.get(watch_id)
        if watch is None:
            raise KeyError(watch_id)
        if watch.status != "active":
            raise ValueError("watch subscription is disabled")

        request = TriggerEvaluationRequest(
            target=TriggerTarget(**watch.target.model_dump()),
            baseline_window=payload.baseline_window,
            trigger_window=payload.trigger_window,
            target_config=watch.target_config,
            scope_source="watch_subscription",
            watch_id=watch.watch_id,
        )
        active_episode = self.registry.get_active_episode(watch_id)
        probe_result = evaluate_persistent_trigger(
            request,
            self.repo,
            create_collector_tasks=False,
        )
        if probe_result.trigger_event is None:
            updated_episode = self._record_recovery(active_episode)
            updated_watch = self.registry.update_after_evaluation(watch_id, probe_result)
            return WatchEvaluationResult(
                watch=updated_watch,
                trigger=probe_result,
                incident=updated_episode,
            )
        if active_episode is not None:
            observed_at = probe_result.trigger_event.observed_at
            last_seen_at = active_episode.last_seen_at or active_episode.trigger_observed_at
            if _as_utc(observed_at) - _as_utc(last_seen_at) <= timedelta(seconds=120):
                updated_episode = self._merge_episode_trigger(active_episode, probe_result, payload)
                updated_watch = self.registry.update_after_evaluation(watch_id, probe_result)
                return WatchEvaluationResult(
                    watch=updated_watch,
                    trigger=probe_result.model_copy(update={
                        "skipped_probe_ids": ["merged_into_active_episode"],
                    }),
                    incident=updated_episode,
                )
            self.registry.replace_incident(active_episode.model_copy(update={
                "episode_status": "CLOSED",
            }))

        trigger_result = evaluate_persistent_trigger(
            request,
            self.repo,
            create_collector_tasks=watch.trigger_action in {"freeze_and_safe_probe", "auto_all_registered"},
            max_probe_risk_level="R1" if watch.trigger_action == "freeze_and_safe_probe" else None,
        )
        incident = None
        if trigger_result.trigger_event is not None and trigger_result.evidence_cohort_id is not None:
            snapshot = self._freeze_snapshot_from_window(watch, payload, trigger_result)
            incident = _build_incident(watch, trigger_result, snapshot)
            incident, snapshot = self._persist_snapshot(watch, incident, snapshot)
        updated = self.registry.update_after_evaluation(watch_id, trigger_result, incident)
        return WatchEvaluationResult(watch=updated, trigger=trigger_result, incident=incident)

    def _merge_episode_trigger(
        self,
        episode: WatchIncident,
        result: TriggerEvaluationResult,
        payload: WatchEvaluationRequest,
    ) -> WatchIncident:
        points = list(episode.anomaly_points)
        for signal in result.detected_signals:
            points = _merge_anomaly_point(
                points,
                episode.watch_id,
                signal,
                result.trigger_event.observed_at,
            )
        impact_status, eligible = _classify_episode_impact(result.detected_signals)
        updated = episode.model_copy(update={
            "episode_status": "DIAGNOSING" if episode.analysis_status == "analyzing" else "ACTIVE",
            "last_seen_at": result.trigger_event.observed_at,
            "window_end": max(
                _as_utc(episode.window_end),
                _as_utc(payload.trigger_window.end),
            ),
            "recovery_observations": 0,
            "occurrence_count": episode.occurrence_count + 1,
            "diagnosis_eligible": episode.diagnosis_eligible or eligible,
            "impact_status": _stronger_impact(episode.impact_status, impact_status),
            "anomaly_points": points,
        })
        return self.registry.replace_incident(updated)

    def _record_recovery(self, episode: WatchIncident | None) -> WatchIncident | None:
        if episode is None:
            return None
        recovery = episode.recovery_observations + 1
        status: EpisodeStatus = "CLOSED" if recovery >= 3 else "RECOVERING"
        return self.registry.replace_incident(episode.model_copy(update={
            "episode_status": status,
            "recovery_observations": recovery,
        }))

    def _persist_snapshot(
        self,
        watch: WatchSubscription,
        incident: WatchIncident,
        snapshot: FrozenSnapshot,
    ) -> tuple[WatchIncident, FrozenSnapshot]:
        refs_and_payloads = snapshot_artifact_payloads(
            snapshot.metadata.snapshot_id,
            snapshot.samples,
        )
        refs: list[dict[str, Any]] = []
        for reference, payload in refs_and_payloads:
            reference = dict(reference)
            try:
                storage.upload_bytes(
                    payload,
                    reference["bucket"],
                    reference["object_key"],
                    reference["content_type"],
                )
                reference["storage_status"] = "stored"
            except Exception as exc:
                reference["storage_status"] = "database_only"
                reference["storage_error"] = type(exc).__name__
            refs.append(reference)

        metadata = snapshot.metadata.model_copy(update={"artifact_refs": refs})
        snapshot = snapshot.model_copy(update={"metadata": metadata})
        structured = structure_snapshot_evidence(
            f"watch:{watch.watch_id}:snapshot:{metadata.snapshot_id}",
            snapshot,
        )
        incident = incident.model_copy(update={
            "snapshot_refs": refs,
            "structured_evidence": structured.model_dump(mode="json"),
        })
        if self.repo is not None and hasattr(self.repo, "persist_watch_incident"):
            self.repo.persist_watch_incident(incident.model_dump(mode="json"))
        if self.repo is not None and hasattr(self.repo, "persist_watch_snapshot"):
            self.repo.persist_watch_snapshot(
                watch_id=watch.watch_id,
                incident_id=incident.incident_id,
                snapshot=snapshot.model_dump(mode="json"),
                structured_evidence=structured.model_dump(mode="json"),
            )
        return incident, snapshot

    def _freeze_snapshot_from_window(
        self,
        watch: WatchSubscription,
        payload: WatchEvaluationRequest,
        trigger_result: TriggerEvaluationResult,
    ) -> FrozenSnapshot:
        target_key = _target_key(watch.target)
        for sample in payload.baseline_window.samples:
            rolling_sample = _metric_sample_to_rolling_sample(sample, payload.baseline_window.end)
            self.rolling_buffer.append(target_key, rolling_sample)
        for sample in payload.trigger_window.samples:
            rolling_sample = _metric_sample_to_rolling_sample(sample, payload.trigger_window.end)
            self.rolling_buffer.append(target_key, rolling_sample)
        return self.rolling_buffer.freeze_snapshot(
            target_key=target_key,
            trigger_event_id=trigger_result.trigger_event.trigger_event_id,
            evidence_cohort_id=trigger_result.evidence_cohort_id,
            trigger_observed_at=trigger_result.trigger_event.observed_at,
        )

    def ingest_collector_task_result(
        self,
        task_id: str,
        *,
        status: str,
        status_reason: str,
        artifacts: list[dict[str, Any]],
        task_options: dict[str, Any] | None = None,
    ) -> WatchIncident | None:
        """Merge a triggered collector result into its frozen Watch incident."""
        task_options = task_options or {}
        incident = self._find_incident(
            watch_id=task_options.get("watch_id"),
            evidence_cohort_id=task_options.get("evidence_cohort_id"),
            trigger_event_id=task_options.get("trigger_event_id"),
            task_id=task_id,
        )
        if incident is None:
            return None

        existing_task = next(
            (
                item
                for item in incident.collector_tasks
                if item.task_id == task_id
            ),
            None,
        )
        if (
            existing_task is not None
            and existing_task.status in {"DONE", "FAILED"}
            and existing_task.status == status
            and not artifacts
        ):
            return incident

        updated_tasks: list[TriggeredCollectorTask] = []
        matched = False
        for collector_task in incident.collector_tasks:
            if collector_task.task_id != task_id:
                updated_tasks.append(collector_task)
                continue
            matched = True
            updated_tasks.append(collector_task.model_copy(update={
                "status": status,
                "status_reason": status_reason,
                "artifact_types": sorted({
                    *collector_task.artifact_types,
                    *[
                        str(item.get("artifact_type"))
                        for item in artifacts
                        if item.get("artifact_type")
                    ],
                }),
            }))
        if not matched:
            return None

        structured = _merge_collector_evidence(
            incident,
            artifacts=artifacts,
            task_id=task_id,
            task_options=task_options,
            collector_tasks=updated_tasks,
        )
        all_terminal = bool(updated_tasks) and all(
            item.status in {"DONE", "FAILED"}
            for item in updated_tasks
        )
        incident_status: IncidentStatus = "ready_for_analysis" if all_terminal else "collecting"
        return self.registry.update_incident_collection(
            incident.incident_id,
            structured_evidence=structured,
            collector_tasks=updated_tasks,
            status=incident_status,
        )

    def schedule_followup_tasks(
        self,
        incident_id: str,
        evidence_requests: list[str],
    ) -> list[TriggeredCollectorTask]:
        """Schedule one bounded delayed-followup round for an incident.

        Follow-ups are intentionally attached to the frozen incident.  They
        never overwrite same-window evidence and are limited to one attempt
        per evidence family to avoid an AI-tree retry loop.
        """
        incident = self.registry.get_incident(incident_id)
        if incident is None:
            raise KeyError(incident_id)
        watch = self.registry.get(incident.watch_id)
        if watch is None:
            raise KeyError(incident.watch_id)
        if watch.trigger_action != "auto_all_registered":
            return []

        existing_followups = {
            item.probe_id
            for item in incident.collector_tasks
            if item.collector_invocation.get("collection_mode") == "delayed_followup"
        }
        created: list[TriggeredCollectorTask] = []
        skipped: list[str] = []
        for evidence_family in dict.fromkeys(evidence_requests):
            probe_id = _FOLLOWUP_PROBES.get(evidence_family)
            if not probe_id or probe_id in existing_followups:
                continue
            try:
                probe = get_probe(probe_id)
            except ValueError:
                skipped.append(evidence_family)
                continue
            if not _agent_supports_probe(
                self.repo,
                watch.target.agent_id,
                probe.required_capabilities,
            ):
                skipped.append(evidence_family)
                continue

            invocation = build_collector_invocation(
                scope_source="watch_followup",
                collector_family=probe.runner_task_kind,
                probe_id=probe.probe_id,
                watch_id=watch.watch_id,
                trigger_event_id=incident.trigger_event_id,
                evidence_cohort_id=incident.evidence_cohort_id,
                target_config=watch.target_config,
                target_context={
                    "agent_id": watch.target.agent_id,
                    "service_id": watch.target.service_id,
                    "instance_id": watch.target.instance_id,
                    "pid": watch.target.target_pid,
                    "endpoint": watch.target.endpoint,
                },
            )
            invocation["collection_mode"] = "delayed_followup"
            invocation["timing_relation"] = "delayed_followup"
            invocation["parent_incident_id"] = incident.incident_id

            task = self.repo.create_task(
                CreateTaskRequest(
                    name=f"watch follow-up {probe.runner_task_kind} for {incident.incident_id}",
                    agent_id=watch.target.agent_id,
                    target_pid=watch.target.target_pid,
                    collector_type=probe.runner_task_kind,
                    sample_rate=min(probe.default_sample_rate, MAX_SAMPLE_RATE),
                    duration_sec=min(probe.default_duration_seconds, MAX_TASK_DURATION_SEC),
                    options={
                        "watch_id": watch.watch_id,
                        "incident_id": incident.incident_id,
                        "parent_incident_id": incident.incident_id,
                        "trigger_event_id": incident.trigger_event_id,
                        "evidence_cohort_id": incident.evidence_cohort_id,
                        "collection_mode": "delayed_followup",
                        "timing_relation": "delayed_followup",
                        "evidence_family": evidence_family,
                        "probe_id": probe.probe_id,
                        "registered_probe": True,
                        "trigger_only": False,
                        "root_cause_assertion": False,
                        "target_config": watch.target_config,
                        "collector_invocation": invocation,
                    },
                )
            )
            created.append(
                TriggeredCollectorTask(
                    task_id=task.id,
                    probe_id=probe.probe_id,
                    collector_type=probe.runner_task_kind,
                    evidence_cohort_id=incident.evidence_cohort_id,
                    trigger_event_id=incident.trigger_event_id,
                    collector_invocation=invocation,
                )
            )

        if created:
            updated_tasks = [*incident.collector_tasks, *created]
            structured = dict(incident.structured_evidence)
            _set_watch_task_states(structured, updated_tasks)
            self.registry.update_incident_collection(
                incident.incident_id,
                structured_evidence=structured,
                collector_tasks=updated_tasks,
                status="collecting",
            )
        return created

    def _find_incident(
        self,
        *,
        watch_id: str | None,
        evidence_cohort_id: str | None,
        trigger_event_id: str | None,
        task_id: str,
    ) -> WatchIncident | None:
        watches = (
            [self.registry.get(watch_id)]
            if watch_id
            else self.registry.list(include_disabled=True)
        )
        for watch in watches:
            if watch is None:
                continue
            for incident in self.registry.list_incidents(watch.watch_id):
                if evidence_cohort_id and incident.evidence_cohort_id != evidence_cohort_id:
                    continue
                if trigger_event_id and incident.trigger_event_id != trigger_event_id:
                    continue
                if any(item.task_id == task_id for item in incident.collector_tasks):
                    return incident
        return None


def _poll_interval_for(profile: WatchProfile) -> int:
    if profile == "latency_sensitive":
        return 5
    if profile == "resource_guarded":
        return 30
    return 10


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
    points: list[dict[str, Any]] = []
    for signal in trigger_result.detected_signals:
        points = _merge_anomaly_point(
            points,
            watch.watch_id,
            signal,
            trigger_event.observed_at,
        )
    impact_status, eligible = _classify_episode_impact(trigger_result.detected_signals)
    aggregation_seconds = 0 if impact_status == "hard_state" else 30
    created_at = _now_utc()
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
        created_at=created_at,
        episode_id=f"episode_{uuid4().hex[:12]}",
        episode_status="AGGREGATING",
        aggregation_deadline=created_at + timedelta(seconds=aggregation_seconds),
        first_seen_at=trigger_event.observed_at,
        last_seen_at=trigger_event.observed_at,
        diagnosis_eligible=eligible,
        impact_status=impact_status,
        anomaly_points=points,
    )


def _merge_anomaly_point(
    points: list[dict[str, Any]],
    watch_id: str,
    signal: Any,
    observed_at: datetime,
) -> list[dict[str, Any]]:
    fingerprint = hashlib.sha256(
        f"{watch_id}|{signal.trigger_type}|{signal.metric}".encode("utf-8")
    ).hexdigest()[:20]
    result = [dict(item) for item in points]
    for index, item in enumerate(result):
        if item.get("fingerprint") != fingerprint:
            continue
        result[index] = {
            **item,
            "last_seen_at": observed_at.isoformat(),
            "latest_value": signal.current_value,
            "peak_value": max(
                float(item.get("peak_value") or signal.current_value),
                signal.current_value,
            ),
            "occurrences": int(item.get("occurrences") or 1) + 1,
            "relative_shift": signal.relative_shift,
            "absolute_delta": signal.absolute_delta,
        }
        return result
    impact_status, _ = _classify_episode_impact([signal])
    result.append({
        "anomaly_point_id": f"ap_{uuid4().hex[:12]}",
        "fingerprint": fingerprint,
        "trigger_type": signal.trigger_type,
        "metric": signal.metric,
        "baseline_value": signal.baseline_value,
        "latest_value": signal.current_value,
        "peak_value": signal.current_value,
        "absolute_delta": signal.absolute_delta,
        "relative_shift": signal.relative_shift,
        "threshold": signal.threshold,
        "robust_score": signal.robust_score,
        "detection_method": signal.detection_method,
        "first_seen_at": observed_at.isoformat(),
        "last_seen_at": observed_at.isoformat(),
        "occurrences": 1,
        "impact_status": impact_status,
        "explanation": None,
    })
    return result


def _classify_episode_impact(signals: list[Any]) -> tuple[ImpactStatus, bool]:
    types = {signal.trigger_type for signal in signals}
    metrics = {signal.metric for signal in signals}
    if "process_suspended" in types:
        return "hard_state", True
    if types.intersection({"latency_shift", "error_burst"}):
        return "impact_confirmed", True
    if metrics.intersection({"throttled_percent", "queue_depth"}):
        return "impact_confirmed", True
    return "impact_unconfirmed", False


def _stronger_impact(left: ImpactStatus, right: ImpactStatus) -> ImpactStatus:
    rank = {"impact_unconfirmed": 0, "impact_confirmed": 1, "hard_state": 2}
    return left if rank[left] >= rank[right] else right


def _merge_collector_evidence(
    incident: WatchIncident,
    *,
    artifacts: list[dict[str, Any]],
    task_id: str,
    task_options: dict[str, Any],
    collector_tasks: list[TriggeredCollectorTask],
) -> dict[str, Any]:
    """Rebuild compact evidence while retaining artifact references."""
    from server.app.diagnosis.evidence_structurer import structure_artifact_evidence

    current = incident.structured_evidence
    current_values: dict[str, Any] = {
        "top_json": current.get("top_functions") or [],
        "sys_metrics": current.get("sys_metrics"),
        "ebpf_metrics": current.get("ebpf_metrics"),
        "memory_json": current.get("memory_json"),
        "off_cpu_wait_json": current.get("off_cpu_wait_json"),
        "log_window_json": current.get("log_window_json"),
        "dependency_check_json": current.get("dependency_check_json"),
        "redis_check_json": current.get("redis_check_json"),
        "trace_endpoint_profile_json": current.get("trace_endpoint_profile_json"),
        "depth_evidence_json": current.get("stack_summary") or {},
    }
    incoming_values = dict(current_values)
    for artifact in artifacts:
        artifact_type = artifact.get("artifact_type")
        data = (artifact.get("metadata") or {}).get("data")
        if not isinstance(data, dict):
            continue
        if artifact_type in {
            "top_json",
            "sys_metrics",
            "ebpf_metrics",
            "memory_json",
            "off_cpu_wait_json",
            "log_window_json",
            "dependency_check_json",
            "redis_check_json",
            "trace_endpoint_profile_json",
            "depth_evidence_json",
        }:
            incoming_values[artifact_type] = data
        elif artifact_type in {"flamegraph_json", "continuous_top_json"}:
            incoming_values["top_json"] = (
                data.get("top_functions")
                or data.get("top")
                or incoming_values["top_json"]
            )

    collection_mode = str(
        task_options.get("collection_mode")
        or "triggered_group"
    )
    timing_relation = str(
        task_options.get("timing_relation")
        or ("delayed_followup" if collection_mode == "delayed_followup" else "same_window")
    )
    evidence_window = {
        "trigger_event_id": incident.trigger_event_id,
        "evidence_cohort_id": incident.evidence_cohort_id,
        "collection_mode": collection_mode,
        "window_start": task_options.get("window_start") or incident.window_start.isoformat(),
        "window_end": task_options.get("window_end") or incident.window_end.isoformat(),
        "trigger_observed_at": task_options.get("trigger_observed_at") or incident.trigger_observed_at.isoformat(),
        "timing_relation": timing_relation,
    }
    if collection_mode == "delayed_followup":
        followup_values = {
            "top_json": [],
            "sys_metrics": None,
            "ebpf_metrics": None,
            "memory_json": None,
            "off_cpu_wait_json": None,
            "log_window_json": None,
            "dependency_check_json": None,
            "redis_check_json": None,
            "trace_endpoint_profile_json": None,
            "depth_evidence_json": {},
        }
        for artifact in artifacts:
            artifact_type = artifact.get("artifact_type")
            data = (artifact.get("metadata") or {}).get("data")
            if not isinstance(data, dict):
                continue
            if artifact_type in followup_values:
                followup_values[artifact_type] = data
            elif artifact_type in {"flamegraph_json", "continuous_top_json"}:
                followup_values["top_json"] = (
                    data.get("top_functions")
                    or data.get("top")
                    or followup_values["top_json"]
                )
        root_window = {
            "trigger_event_id": incident.trigger_event_id,
            "evidence_cohort_id": incident.evidence_cohort_id,
            "collection_mode": "triggered_group",
            "window_start": incident.window_start.isoformat(),
            "window_end": incident.window_end.isoformat(),
            "trigger_observed_at": incident.trigger_observed_at.isoformat(),
            "timing_relation": "same_window",
        }
        root_refs = list(current.get("artifact_refs") or [])
        root = structure_artifact_evidence(
            task_id=f"watch:{incident.incident_id}",
            artifacts=root_refs,
            artifact_values=current_values,
            evidence_window=root_window,
        ).model_dump(mode="json")
        followup = structure_artifact_evidence(
            task_id=f"watch:{incident.incident_id}:followup:{task_id}",
            artifacts=artifacts,
            artifact_values=followup_values,
            evidence_window=evidence_window,
        ).model_dump(mode="json")
        existing_delayed = []
        current_index = current.get("evidence_index")
        if isinstance(current_index, dict):
            existing_delayed = list(current_index.get("delayed_followups") or [])
        delayed_by_task = {
            str(item.get("task_id")): item
            for item in existing_delayed
            if isinstance(item, dict) and item.get("task_id")
        }
        delayed_by_task[task_id] = {
            "task_id": task_id,
            "collection_mode": "delayed_followup",
            "timing_relation": "delayed_followup",
            "artifact_refs": followup.get("artifact_refs", []),
            "top_functions": followup.get("top_functions", []),
            "stack_summary": followup.get("stack_summary", {}),
            "call_path_hotspots": followup.get("call_path_hotspots", []),
            "confidence_inputs": followup.get("confidence_inputs", {}),
            "evidence_index": followup.get("evidence_index", {}),
        }
        root.setdefault("evidence_index", {})["delayed_followups"] = list(delayed_by_task.values())
        root["artifact_refs"] = [
            *root.get("artifact_refs", []),
            *followup.get("artifact_refs", []),
        ]
        root["evidence_index"]["artifact_refs"] = root["artifact_refs"]
        structured = root
    else:
        refs = [*list(current.get("artifact_refs") or []), *artifacts]
        structured = structure_artifact_evidence(
            task_id=f"watch:{incident.incident_id}",
            artifacts=refs,
            artifact_values=incoming_values,
            evidence_window=evidence_window,
        ).model_dump(mode="json")

    _set_watch_task_states(structured, collector_tasks)
    return structured


def _agent_supports_probe(
    repo: Any,
    agent_id: str,
    required_capabilities: list[str],
) -> bool:
    agents = getattr(repo, "agents", {})
    agent = agents.get(agent_id) if isinstance(agents, dict) else None
    if agent is None:
        return False
    capabilities = set(getattr(agent, "capabilities", []) or [])
    return all(capability in capabilities for capability in required_capabilities)


def _set_watch_task_states(
    structured: dict[str, Any],
    collector_tasks: list[TriggeredCollectorTask],
) -> None:
    states = [
        {
            "task_id": item.task_id,
            "probe_id": item.probe_id,
            "collector_type": item.collector_type,
            "status": item.status,
            "status_reason": item.status_reason,
            "artifact_types": item.artifact_types,
            "evidence_cohort_id": item.evidence_cohort_id,
            "trigger_event_id": item.trigger_event_id,
            "collection_mode": item.collector_invocation.get("collection_mode", "triggered_group"),
            "timing_relation": item.collector_invocation.get("timing_relation", "same_window"),
        }
        for item in collector_tasks
    ]
    structured.setdefault("evidence_index", {})["watch_collector_tasks"] = states
    structured.setdefault("confidence_inputs", {})["watch_collector_tasks"] = states
