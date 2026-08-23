from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.app.diagnosis.persistent_trigger import MetricSample, MetricWindow
from server.app.diagnosis.watch_runtime import (
    CreateWatchSubscriptionRequest,
    PersistentAgentRuntime,
    WatchEvaluationRequest,
    WatchRegistry,
    WatchTarget,
)
from server.app.repository import InMemoryRepository


def _window(start: datetime, *, cpu_percent: float) -> MetricWindow:
    return MetricWindow(
        start=start,
        end=start + timedelta(seconds=30),
        samples=[
            MetricSample(
                observed_at=start + timedelta(seconds=index * 10),
                cpu_percent=cpu_percent,
            )
            for index in range(3)
        ],
    )


def _watch_payload(agent_id: str = "agent_1") -> CreateWatchSubscriptionRequest:
    return CreateWatchSubscriptionRequest(
        name="order service watch",
        target=WatchTarget(
            agent_id=agent_id,
            target_pid=4242,
            service_id="order-service",
            instance_id="order-1",
            endpoint="/orders",
        ),
    )


def _watch_payload_with_action(action: str) -> CreateWatchSubscriptionRequest:
    payload = _watch_payload()
    return payload.model_copy(update={"trigger_action": action})


def test_watch_subscription_creates_agent_lease():
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, InMemoryRepository())

    watch = registry.create(_watch_payload())
    leases = runtime.list_leases("agent_1")

    assert len(leases) == 1
    assert leases[0].watch_id == watch.watch_id
    assert leases[0].target.target_pid == 4242
    assert leases[0].state == "watching"
    assert runtime.list_leases("other_agent") == []


def test_disabled_watch_is_not_returned_as_lease():
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, InMemoryRepository())

    watch = registry.create(_watch_payload())
    registry.disable(watch.watch_id)

    assert runtime.list_leases("agent_1") == []


def test_watch_evaluation_reuses_persistent_trigger_and_updates_last_trigger():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload())
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )

    assert result.trigger.trigger_event is not None
    assert result.trigger.trigger_event.trigger_type == "cpu_shift"
    assert result.watch.last_trigger_event_id == result.trigger.trigger_event.trigger_event_id
    assert result.watch.last_evidence_cohort_id == result.trigger.evidence_cohort_id
    assert result.watch.last_trigger_type == "cpu_shift"
    assert result.incident is not None
    assert result.incident.status == "collecting"
    assert result.incident.snapshot_id is not None
    assert result.incident.snapshot_refs[0]["artifact_type"] == "rolling_metrics_summary"
    assert result.incident.structured_evidence["collection_mode"] == "rolling_snapshot"
    assert result.incident.structured_evidence["timing_relation"] == "same_window"
    assert result.incident.collector_tasks == result.trigger.collector_tasks
    assert [task.probe_id for task in result.trigger.collector_tasks] == ["host_process_metrics"]
    assert result.trigger.skipped_probe_ids == [
        "process_baseline_window",
        "process_off_cpu_profile",
        "process_trace_endpoint_profile",
        "process_cpu_profile",
    ]
    assert len(repo.tasks) == 1


def test_watch_target_config_flows_into_triggered_collector_invocation():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    payload = _watch_payload().model_copy(update={
        "target_config": {
            "redis_target": {
                "dependency_id": "order-redis",
                "host": "order-redis.local",
                "port": 6379,
                "url": "redis://order-redis.local:6379",
            },
            "dependency_targets": [{
                "dependency_id": "payment",
                "protocol": "https",
                "url": "https://payment.local/health",
            }],
        },
    })
    watch = registry.create(payload)
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )

    collector_task = result.trigger.collector_tasks[0]
    invocation = collector_task.collector_invocation
    created_task = repo.tasks[collector_task.task_id]
    task_invocation = created_task.request_params["options"]["collector_invocation"]

    assert invocation["scope_source"] == "watch_subscription"
    assert invocation["watch_id"] == watch.watch_id
    assert invocation["target_config"]["redis_target"]["host"] == "order-redis.local"
    assert task_invocation == invocation
    assert created_task.request_params["options"]["target_config"] == payload.target_config
    assert result.incident.collector_tasks[0].collector_invocation == invocation


def test_watch_evaluation_merges_repeated_trigger_into_one_episode():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload())
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    first = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )
    second = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now + timedelta(seconds=30), cpu_percent=30.0),
            trigger_window=_window(now + timedelta(minutes=6), cpu_percent=70.0),
        ),
    )

    incidents = registry.list_incidents(watch.watch_id)
    assert len(incidents) == 1
    assert second.incident.incident_id == first.incident.incident_id
    assert second.incident.occurrence_count == 2
    assert second.incident.anomaly_points[0]["occurrences"] == 2
    assert second.trigger.collector_tasks == []
    assert second.trigger.skipped_probe_ids == ["merged_into_active_episode"]
    assert registry.get(watch.watch_id).incidents_count == 1


def test_recovered_episode_closes_and_later_trigger_creates_new_episode():
    repo = InMemoryRepository()
    repo.register_agent("agent_1", "host-1", "10.0.0.1", capabilities=["sys_metrics"])
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("freeze_only"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    first = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )

    for offset in range(3):
        recovered = runtime.evaluate(
            watch.watch_id,
            WatchEvaluationRequest(
                baseline_window=_window(now + timedelta(minutes=10 + offset), cpu_percent=20.0),
                trigger_window=_window(now + timedelta(minutes=11 + offset), cpu_percent=20.0),
            ),
        )
    assert recovered.incident.episode_status == "CLOSED"

    later = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now + timedelta(minutes=20), cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=25), cpu_percent=60.0),
        ),
    )
    assert later.incident.incident_id != first.incident.incident_id
    assert len(registry.list_incidents(watch.watch_id)) == 2


def test_resource_shift_without_impact_is_not_auto_diagnosis_eligible():
    repo = InMemoryRepository()
    repo.register_agent("agent_1", "host-1", "10.0.0.1", capabilities=["sys_metrics"])
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("freeze_only"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=80.0),
        ),
    )

    assert result.incident.impact_status == "impact_unconfirmed"
    assert result.incident.diagnosis_eligible is False
    assert registry.ready_auto_diagnosis_episodes(now + timedelta(minutes=1)) == []


def test_hard_process_state_is_immediately_auto_diagnosis_eligible():
    repo = InMemoryRepository()
    repo.register_agent("agent_1", "host-1", "10.0.0.1", capabilities=["sys_metrics"])
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("freeze_only"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    baseline = MetricWindow(
        start=now,
        end=now + timedelta(seconds=30),
        samples=[MetricSample(process_state="S") for _ in range(3)],
    )
    trigger = MetricWindow(
        start=now + timedelta(minutes=1),
        end=now + timedelta(minutes=1, seconds=30),
        samples=[MetricSample(process_state="T") for _ in range(3)],
    )

    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(baseline_window=baseline, trigger_window=trigger),
    )

    assert result.incident.impact_status == "hard_state"
    assert result.incident.diagnosis_eligible is True
    assert result.incident.aggregation_deadline <= result.incident.created_at
    assert registry.ready_auto_diagnosis_episodes(result.incident.created_at)

    first_claim, acquired = registry.claim_episode_diagnosis(result.incident.incident_id)
    second_claim, acquired_again = registry.claim_episode_diagnosis(result.incident.incident_id)
    assert acquired is True
    assert acquired_again is False
    assert first_claim.incident_id == second_claim.incident_id


def test_freeze_only_watch_creates_incident_without_collector_tasks():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("freeze_only"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )

    assert result.trigger.trigger_event is not None
    assert result.trigger.collector_tasks == []
    assert result.incident.status == "frozen"
    assert result.incident.evidence_cohort_id == result.trigger.evidence_cohort_id
    assert repo.tasks == {}


def test_frozen_watch_analysis_does_not_schedule_delayed_followups():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "baseline_window_profile"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("auto_all_registered"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )
    incident = result.incident
    assert incident is not None
    registry.update_incident_analysis(
        incident.incident_id,
        analysis_status="needs_evidence",
        analysis_session_id="analysis_" + incident.incident_id,
        analysis_result={"diagnosis_mode": "frozen_evidence"},
    )
    task_count = len(repo.tasks)

    assert runtime.schedule_followup_tasks(
        incident.incident_id,
        ["baseline_window_profile"],
    ) == []
    assert len(repo.tasks) == task_count


def test_auto_all_registered_watch_can_create_deeper_collector_tasks():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("auto_all_registered"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )

    assert [task.probe_id for task in result.trigger.collector_tasks] == [
        "host_process_metrics",
        "process_cpu_profile",
    ]
    assert len(repo.tasks) == 2


def test_auto_all_registered_cpu_watch_uses_industrial_depth_group():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=[
            "sys_metrics",
            "perf_cpu",
            "off_cpu_wait_profile",
            "trace_endpoint_profile",
            "baseline_window_profile",
        ],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("auto_all_registered"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )

    assert [task.probe_id for task in result.trigger.collector_tasks] == [
        "host_process_metrics",
        "process_baseline_window",
        "process_off_cpu_profile",
        "process_trace_endpoint_profile",
        "process_cpu_profile",
    ]
    assert result.trigger.skipped_probe_ids == []
    assert len(repo.tasks) == 5


def test_watch_analysis_followup_is_target_scoped_and_delayed():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "baseline_window_profile"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("auto_all_registered"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )

    incident = result.incident
    assert incident is not None
    followups = runtime.schedule_followup_tasks(
        incident.incident_id,
        ["baseline_window_profile", "baseline_window_profile"],
    )

    assert len(followups) == 1
    followup = followups[0]
    assert followup.collector_invocation["collection_mode"] == "delayed_followup"
    assert followup.collector_invocation["timing_relation"] == "delayed_followup"
    assert repo.tasks[followup.task_id].target_pid == watch.target.target_pid
    assert repo.tasks[followup.task_id].request_params["options"]["watch_id"] == watch.watch_id

    updated = runtime.ingest_collector_task_result(
        followup.task_id,
        status="DONE",
        status_reason="baseline 已生成",
        artifacts=[{
            "artifact_type": "continuous_top_json",
            "metadata": {"data": {"top_functions": [{"name": "pkg.hot", "samples": 8, "percent": 42.0}]}},
        }],
        task_options=repo.tasks[followup.task_id].request_params["options"],
    )

    assert updated is not None
    assert updated.structured_evidence["timing_relation"] == "same_window"
    delayed = updated.structured_evidence["evidence_index"]["delayed_followups"]
    assert delayed[0]["timing_relation"] == "delayed_followup"
    assert delayed[0]["task_id"] == followup.task_id


def test_multiple_delayed_followups_are_retained_independently():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "baseline_window_profile", "off_cpu_wait_profile"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("auto_all_registered"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )
    incident = result.incident
    assert incident is not None

    followups = runtime.schedule_followup_tasks(
        incident.incident_id,
        ["baseline_window_profile", "off_cpu_wait_profile"],
    )

    assert [item.probe_id for item in followups] == [
        "process_baseline_window",
        "process_off_cpu_profile",
    ]
    first = runtime.ingest_collector_task_result(
        followups[0].task_id,
        status="DONE",
        status_reason="baseline 已生成",
        artifacts=[{
            "artifact_type": "continuous_top_json",
            "metadata": {"data": {"top_functions": [{"name": "pkg.hot", "samples": 8, "percent": 42.0}]}},
        }],
        task_options=repo.tasks[followups[0].task_id].request_params["options"],
    )
    assert first is not None
    second = runtime.ingest_collector_task_result(
        followups[1].task_id,
        status="DONE",
        status_reason="off cpu 已生成",
        artifacts=[{
            "artifact_type": "off_cpu_wait_json",
            "metadata": {
                "data": {
                    "event_summary": {"sample_count": 4, "total_wait_ms": 120.5},
                    "top_wait_stacks": [{
                        "top_frame": "runtime.futex",
                        "wait_reason": "futex_wait",
                        "samples": 4,
                        "wait_ms": 120.5,
                    }],
                },
            },
        }],
        task_options=repo.tasks[followups[1].task_id].request_params["options"],
    )

    assert second is not None
    delayed = second.structured_evidence["evidence_index"]["delayed_followups"]
    assert [item["task_id"] for item in delayed] == [
        followups[0].task_id,
        followups[1].task_id,
    ]
    assert delayed[0]["top_functions"][0]["name"] == "pkg.hot"
    assert delayed[1]["evidence_index"]["off_cpu_wait"]["event_summary"]["sample_count"] == 4
    assert delayed[1]["top_functions"][0]["name"] == "runtime.futex"
    assert all(item["name"] != "pkg.hot" for item in delayed[1]["top_functions"])


def test_triggered_collector_results_are_reconciled_into_incident():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("auto_all_registered"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )
    incident = result.incident
    assert incident is not None
    sys_task, perf_task = result.trigger.collector_tasks

    updated = runtime.ingest_collector_task_result(
        sys_task.task_id,
        status="DONE",
        status_reason="系统指标已生成",
        artifacts=[{
            "artifact_type": "sys_metrics",
            "metadata": {"data": {"cpu_percent": 55.0, "rss_mb": 128.0}},
        }],
        task_options=repo.tasks[sys_task.task_id].request_params["options"],
    )
    assert updated is not None
    assert updated.status == "collecting"
    assert updated.collector_tasks[0].status == "DONE"

    updated = runtime.ingest_collector_task_result(
        perf_task.task_id,
        status="FAILED",
        status_reason="perf_event_paranoid 权限不足",
        artifacts=[],
        task_options=repo.tasks[perf_task.task_id].request_params["options"],
    )
    assert updated is not None
    assert updated.status == "ready_for_analysis"
    assert updated.collector_tasks[1].status == "FAILED"
    assert updated.collector_tasks[1].status_reason == "perf_event_paranoid 权限不足"
    states = updated.structured_evidence["evidence_index"]["watch_collector_tasks"]
    assert {item["status"] for item in states} == {"DONE", "FAILED"}
    assert any(
        item["status_reason"] == "perf_event_paranoid 权限不足"
        for item in states
    )


def test_repeated_terminal_result_is_idempotent():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu"],
    )
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    watch = registry.create(_watch_payload_with_action("auto_all_registered"))
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, cpu_percent=20.0),
            trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
    )
    incident = result.incident
    assert incident is not None
    task = result.trigger.collector_tasks[0]
    options = repo.tasks[task.task_id].request_params["options"]
    artifact = {
        "artifact_type": "sys_metrics",
        "metadata": {"data": {"cpu_percent": 55.0}},
    }

    first = runtime.ingest_collector_task_result(
        task.task_id,
        status="DONE",
        status_reason="系统指标已生成",
        artifacts=[artifact],
        task_options=options,
    )
    second = runtime.ingest_collector_task_result(
        task.task_id,
        status="DONE",
        status_reason="系统指标已生成",
        artifacts=[],
        task_options=options,
    )

    assert first is not None
    assert second is not None
    assert second.collector_tasks[0].status == "DONE"
    assert len(second.structured_evidence["artifact_refs"]) == len(
        first.structured_evidence["artifact_refs"]
    )


def test_disabled_watch_cannot_be_evaluated():
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, InMemoryRepository())
    watch = registry.create(_watch_payload())
    registry.disable(watch.watch_id)
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="disabled"):
        runtime.evaluate(
            watch.watch_id,
            WatchEvaluationRequest(
                baseline_window=_window(now, cpu_percent=20.0),
                trigger_window=_window(now + timedelta(minutes=5), cpu_percent=55.0),
            ),
        )
