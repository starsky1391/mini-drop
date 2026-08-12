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
    assert result.trigger.skipped_probe_ids == ["process_cpu_profile"]
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


def test_watch_evaluation_appends_multiple_incidents_without_overwriting_history():
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
            baseline_window=_window(now + timedelta(minutes=10), cpu_percent=30.0),
            trigger_window=_window(now + timedelta(minutes=15), cpu_percent=70.0),
        ),
    )

    incidents = registry.list_incidents(watch.watch_id)
    assert len(incidents) == 2
    assert {item.incident_id for item in incidents} == {
        first.incident.incident_id,
        second.incident.incident_id,
    }
    assert registry.get(watch.watch_id).incidents_count == 2


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
