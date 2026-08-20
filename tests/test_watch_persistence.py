from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.app import database
from server.app.diagnosis.persistent_trigger import MetricSample, MetricWindow
from server.app.diagnosis.watch_runtime import (
    CreateWatchSubscriptionRequest,
    PersistentAgentRuntime,
    WatchEvaluationRequest,
    WatchRegistry,
    WatchTarget,
)
from server.app.sql_repository import SqlRepository


def _window(start: datetime, cpu: float) -> MetricWindow:
    return MetricWindow(
        start=start,
        end=start + timedelta(seconds=30),
        samples=[
            MetricSample(
                observed_at=start + timedelta(seconds=index * 10),
                cpu_percent=cpu,
            )
            for index in range(3)
        ],
    )


def test_watch_subscription_incident_and_snapshot_survive_registry_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "watch.db"))
    database.reset_engine()
    database.init_db()
    monkeypatch.setattr(
        "server.app.diagnosis.watch_runtime.storage.upload_bytes",
        lambda payload, bucket, object_key, content_type: len(payload),
    )

    repo = SqlRepository()
    repo.register_agent("agent-1", "worker", "10.0.0.1", capabilities=["sys_metrics"])
    registry = WatchRegistry(repo)
    watch = registry.create(
        CreateWatchSubscriptionRequest(
            name="persistent watch",
            target=WatchTarget(
                agent_id="agent-1",
                target_pid=4242,
                service_id="cartservice",
            ),
        )
    )

    now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
    result = PersistentAgentRuntime(registry, repo).evaluate(
        watch.watch_id,
        WatchEvaluationRequest(
            baseline_window=_window(now, 1.0),
            trigger_window=_window(now + timedelta(minutes=5), 80.0),
        ),
    )

    assert result.incident is not None
    assert result.incident.snapshot_refs[0]["size_bytes"] > 0
    assert result.incident.snapshot_refs[0]["storage_status"] == "stored"

    reloaded = WatchRegistry(repo)
    restored_watch = reloaded.get(watch.watch_id)
    restored_incidents = reloaded.list_incidents(watch.watch_id)

    assert restored_watch is not None
    assert restored_watch.incidents_count == 1
    assert len(restored_incidents) == 1
    assert restored_incidents[0].snapshot_id == result.incident.snapshot_id
    assert restored_incidents[0].episode_id == result.incident.episode_id
    assert restored_incidents[0].occurrence_count == 1
    assert restored_incidents[0].anomaly_points[0]["trigger_type"] == "cpu_shift"
    assert restored_watch.auto_diagnosis_enabled is True
    assert restored_incidents[0].structured_evidence["evidence_cohort_id"] == result.incident.evidence_cohort_id
    snapshot = repo.get_watch_snapshot(result.incident.snapshot_id)
    assert snapshot is not None
    assert snapshot["samples"]
    assert snapshot["structured_evidence"]["collection_mode"] == "rolling_snapshot"
