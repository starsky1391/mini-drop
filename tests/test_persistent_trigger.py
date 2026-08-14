from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.app.diagnosis.persistent_trigger import (
    MetricSample,
    MetricWindow,
    TriggerEvaluationRequest,
    TriggerTarget,
    evaluate_persistent_trigger,
)
from server.app.repository import InMemoryRepository


def _window(
    start: datetime,
    *,
    cpu_percent: float | None = None,
    latency_ms_p99: float | None = None,
    thread_count: float | None = None,
    iowait_percent: float | None = None,
    rss_mb: float | None = None,
    error_count: float | None = None,
) -> MetricWindow:
    return MetricWindow(
        start=start,
        end=start + timedelta(seconds=30),
        samples=[
            MetricSample(
                observed_at=start + timedelta(seconds=index * 10),
                cpu_percent=cpu_percent,
                latency_ms_p99=latency_ms_p99,
                thread_count=thread_count,
                iowait_percent=iowait_percent,
                rss_mb=rss_mb,
                error_count=error_count,
            )
            for index in range(3)
        ],
    )


def _request(baseline: MetricWindow, current: MetricWindow) -> TriggerEvaluationRequest:
    return TriggerEvaluationRequest(
        target=TriggerTarget(
            agent_id="agent_1",
            target_pid=4242,
            service_id="order-service",
            instance_id="order-1",
            endpoint="/orders",
        ),
        baseline_window=baseline,
        trigger_window=current,
    )


def test_cpu_shift_creates_collector_group_with_shared_cohort_metadata():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu"],
    )
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = evaluate_persistent_trigger(
        _request(
            _window(now, cpu_percent=20.0),
            _window(now + timedelta(minutes=5), cpu_percent=55.0),
        ),
        repo,
    )

    assert result.trigger_event is not None
    assert result.trigger_event.trigger_type == "cpu_shift"
    assert result.trigger_event.action == "start_collector_group"
    assert result.trigger_event.confidence == "suspected"
    assert result.evidence_cohort_id is not None
    assert [task.probe_id for task in result.collector_tasks] == [
        "host_process_metrics",
        "process_cpu_profile",
    ]
    assert len({task.evidence_cohort_id for task in result.collector_tasks}) == 1

    created_tasks = list(repo.tasks.values())
    assert len(created_tasks) == 2
    for task in created_tasks:
        options = task.request_params["options"]
        assert options["trigger_event_id"] == result.trigger_event.trigger_event_id
        assert options["evidence_cohort_id"] == result.evidence_cohort_id
        assert options["collection_mode"] == "triggered_group"
        assert options["timing_relation"] == "same_window"
        assert options["trigger_only"] is True
        assert options["root_cause_assertion"] is False
        assert options["window_start"] == result.trigger_event.trigger_window["start"]


def test_trigger_result_does_not_emit_attribution_fields():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "trace_endpoint_profile"],
    )
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = evaluate_persistent_trigger(
        _request(
            _window(now, latency_ms_p99=100.0),
            _window(now + timedelta(minutes=5), latency_ms_p99=210.0),
        ),
        repo,
    )
    payload = result.model_dump()

    assert result.trigger_event is not None
    assert result.trigger_event.trigger_type == "latency_shift"
    assert "root_cause" not in payload
    assert "ranked_causes" not in payload
    assert "diagnosis_id" not in payload
    assert "attribution" not in payload


def test_unsupported_probe_capabilities_are_skipped_without_fake_tasks():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics"],
    )
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = evaluate_persistent_trigger(
        _request(
            _window(now, iowait_percent=2.0),
            _window(now + timedelta(minutes=5), iowait_percent=14.0),
        ),
        repo,
    )

    assert result.trigger_event is not None
    assert result.trigger_event.trigger_type == "io_wait_shift"
    assert [task.probe_id for task in result.collector_tasks] == ["host_process_metrics"]
    assert result.skipped_probe_ids == [
        "process_io_latency",
        "process_off_cpu_profile",
        "process_trace_endpoint_profile",
    ]
    assert len(repo.tasks) == 1


def test_stable_window_creates_no_trigger_event_or_tasks():
    repo = InMemoryRepository()
    repo.register_agent(
        "agent_1",
        "host-1",
        "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu"],
    )
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    result = evaluate_persistent_trigger(
        _request(
            _window(now, cpu_percent=20.0, latency_ms_p99=100.0),
            _window(now + timedelta(minutes=5), cpu_percent=24.0, latency_ms_p99=105.0),
        ),
        repo,
    )

    assert result.trigger_event is None
    assert result.evidence_cohort_id is None
    assert result.collector_tasks == []
    assert repo.tasks == {}
