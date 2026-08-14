from __future__ import annotations

from datetime import datetime, timedelta, timezone

import grpc

from server.app.diagnosis.persistent_trigger import MetricSample, MetricWindow
from server.app.diagnosis.watch_runtime import (
    CreateWatchSubscriptionRequest,
    PersistentAgentRuntime,
    WatchEvaluationRequest,
    WatchRegistry,
    WatchTarget,
)
from server.app.generated import watch_pb2, watch_pb2_grpc
from server.app.grpc_services.watch_runtime_service import WatchRuntimeService
from server.app.repository import InMemoryRepository


def _sample(ts: datetime, cpu: float) -> watch_pb2.WatchMetricSample:
    return watch_pb2.WatchMetricSample(
        observed_at=ts.isoformat(),
        cpu_percent=cpu,
        has_cpu_percent=True,
    )


def _runtime() -> tuple[InMemoryRepository, WatchRegistry, PersistentAgentRuntime]:
    repo = InMemoryRepository()
    repo.register_agent("agent-1", "worker", "10.0.0.1", capabilities=["sys_metrics"])
    registry = WatchRegistry()
    runtime = PersistentAgentRuntime(registry, repo)
    registry.create(
        CreateWatchSubscriptionRequest(
            name="redis watch",
            target=WatchTarget(agent_id="agent-1", target_pid=1234),
            trigger_action="freeze_only",
        )
    )
    return repo, registry, runtime


def test_watch_service_returns_leases_and_evaluates_windows():
    repo, registry, runtime = _runtime()
    service = WatchRuntimeService(runtime)
    watch = registry.list(agent_id="agent-1")[0]
    now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
    request = watch_pb2.WatchSyncRequest(
        agent_id="agent-1",
        ip_addr="10.0.0.1",
        observations=[
            watch_pb2.WatchObservation(
                watch_id=watch.watch_id,
                target_exists=True,
                baseline_samples=[_sample(now + timedelta(seconds=i), 20) for i in range(3)],
                trigger_samples=[_sample(now + timedelta(minutes=5, seconds=i), 60) for i in range(3)],
            )
        ],
    )

    response = service.Sync(request, None)

    assert [item.watch_id for item in response.lease] == [watch.watch_id]
    assert len(registry.list_incidents(watch.watch_id)) == 1
    assert repo.agents["agent-1"].status == "ONLINE"


def test_watch_service_suppresses_repeated_active_trigger_window():
    repo, registry, runtime = _runtime()
    service = WatchRuntimeService(runtime)
    watch = registry.list(agent_id="agent-1")[0]
    now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
    observation = watch_pb2.WatchObservation(
        watch_id=watch.watch_id,
        target_exists=True,
        baseline_samples=[_sample(now + timedelta(seconds=i), 20) for i in range(3)],
        trigger_samples=[_sample(now + timedelta(minutes=5, seconds=i), 60) for i in range(3)],
    )

    service.Sync(watch_pb2.WatchSyncRequest(agent_id="agent-1", ip_addr="10.0.0.1", observations=[observation]), None)
    service.Sync(watch_pb2.WatchSyncRequest(agent_id="agent-1", ip_addr="10.0.0.1", observations=[observation]), None)
    service.Sync(watch_pb2.WatchSyncRequest(agent_id="agent-1", ip_addr="10.0.0.1", observations=[observation]), None)

    assert len(registry.list_incidents(watch.watch_id)) == 1


def test_watch_service_allows_new_trigger_after_recovery_window():
    repo, registry, runtime = _runtime()
    service = WatchRuntimeService(runtime)
    watch = registry.list(agent_id="agent-1")[0]
    now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
    high = watch_pb2.WatchObservation(
        watch_id=watch.watch_id,
        target_exists=True,
        baseline_samples=[_sample(now + timedelta(seconds=i), 20) for i in range(3)],
        trigger_samples=[_sample(now + timedelta(minutes=5, seconds=i), 60) for i in range(3)],
    )
    normal = watch_pb2.WatchObservation(
        watch_id=watch.watch_id,
        target_exists=True,
        baseline_samples=[_sample(now + timedelta(minutes=10, seconds=i), 20) for i in range(3)],
        trigger_samples=[_sample(now + timedelta(minutes=15, seconds=i), 20) for i in range(3)],
    )

    service.Sync(watch_pb2.WatchSyncRequest(agent_id="agent-1", ip_addr="10.0.0.1", observations=[high]), None)
    service.Sync(watch_pb2.WatchSyncRequest(agent_id="agent-1", ip_addr="10.0.0.1", observations=[normal]), None)
    service.Sync(watch_pb2.WatchSyncRequest(agent_id="agent-1", ip_addr="10.0.0.1", observations=[high]), None)

    assert len(registry.list_incidents(watch.watch_id)) == 2


def test_watch_service_ignores_unknown_watch_and_target_exit():
    repo, registry, runtime = _runtime()
    service = WatchRuntimeService(runtime)
    request = watch_pb2.WatchSyncRequest(
        agent_id="agent-1",
        ip_addr="10.0.0.1",
        observations=[
            watch_pb2.WatchObservation(watch_id="unknown", target_exists=True),
            watch_pb2.WatchObservation(watch_id=registry.list(agent_id="agent-1")[0].watch_id),
        ],
    )

    response = service.Sync(request, None)

    assert len(response.lease) == 1
    assert repo.tasks == {}
