from __future__ import annotations

from unittest import mock

from agent.mini_drop_agent.watch_observer import (
    ProcessDeltaSampler,
    WatchObservationState,
    dict_to_sample,
    observe_watch_lease,
    sample_to_dict,
)
from server.app.generated import watch_pb2


def test_watch_observation_state_splits_baseline_and_trigger():
    state = WatchObservationState()
    for index in range(6):
        state.append({"observed_at": f"t{index}", "cpu_percent": index})

    baseline, trigger = state.split()

    assert [item["observed_at"] for item in baseline] == ["t0", "t1", "t2"]
    assert [item["observed_at"] for item in trigger] == ["t3", "t4", "t5"]


def test_sample_roundtrip_preserves_presence_flags():
    sample = watch_pb2.WatchMetricSample(
        observed_at="2026-08-14T00:00:00+00:00",
        cpu_percent=42.5,
        has_cpu_percent=True,
        rss_mb=128,
        has_rss_mb=True,
        process_state="S",
        throttled_percent=12.5,
        has_throttled_percent=True,
    )

    restored = dict_to_sample(sample_to_dict(sample))

    assert restored.has_cpu_percent is True
    assert restored.cpu_percent == 42.5
    assert restored.has_rss_mb is True
    assert restored.has_thread_count is False
    assert restored.process_state == "S"
    assert restored.throttled_percent == 12.5


def test_process_delta_sampler_reports_short_window_cpu_delta():
    sampler = ProcessDeltaSampler()
    snapshots = [
        {
            "metrics": {"thread_count": 4.0, "rss_mb": 10.0},
            "snapshot": type("Snapshot", (), {"monotonic_ts": 10.0, "cpu_seconds": 2.0})(),
            "start_ticks": 100,
        },
        {
            "metrics": {"thread_count": 4.0, "rss_mb": 10.0},
            "snapshot": type("Snapshot", (), {"monotonic_ts": 12.0, "cpu_seconds": 3.0})(),
            "start_ticks": 100,
        },
    ]
    with mock.patch(
        "agent.mini_drop_agent.watch_observer._read_pid_snapshot",
        side_effect=snapshots,
    ):
        first = sampler.read(1234)
        second = sampler.read(1234)

    assert "cpu_percent" not in first
    assert second["cpu_percent"] == 50.0


def test_process_delta_sampler_uses_cgroup_counter_deltas_for_throttling():
    sampler = ProcessDeltaSampler()
    snapshots = [
        {
            "metrics": {},
            "snapshot": type("Snapshot", (), {
                "monotonic_ts": 10.0,
                "cpu_seconds": 2.0,
                "cgroup_usage_usec": 1_000,
                "cgroup_throttled_usec": 100,
            })(),
            "start_ticks": 100,
        },
        {
            "metrics": {},
            "snapshot": type("Snapshot", (), {
                "monotonic_ts": 12.0,
                "cpu_seconds": 3.0,
                "cgroup_usage_usec": 1_800,
                "cgroup_throttled_usec": 300,
            })(),
            "start_ticks": 100,
        },
    ]
    with mock.patch(
        "agent.mini_drop_agent.watch_observer._read_pid_snapshot",
        side_effect=snapshots,
    ):
        sampler.read(1234)
        second = sampler.read(1234)

    assert second["throttled_percent"] == 20.0


def test_observe_watch_lease_uses_delta_sampler_for_cpu():
    lease = watch_pb2.WatchLease(watch_id="watch-1", target_pid=1234)
    sampler = mock.Mock()
    sampler.read.return_value = {"cpu_percent": 80.0, "thread_count": 8.0, "rss_mb": 64.0}

    with mock.patch("agent.mini_drop_agent.watch_observer._pid_exists", return_value=True):
        sample, target_exists, status = observe_watch_lease(lease, sampler)

    assert target_exists is True
    assert status == "ok"
    assert sample.has_cpu_percent is True
    assert sample.cpu_percent == 80.0
    assert sample.has_thread_count is True
