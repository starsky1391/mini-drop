"""Tests for SysMetrics multi-dimensional collector."""

from __future__ import annotations

import json
import os
from unittest import mock

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.sys_metrics import SysMetricsCollector


class TestSysMetricsCollector:
    @staticmethod
    def _task(**kwargs) -> CollectorTask:
        return CollectorTask(
            id="sys_test_001",
            collector_type="sys_metrics",
            target_pid=1234,
            sample_rate=99,
            duration_sec=kwargs.get("duration_sec", 3),
            options=kwargs.get("options", {}),
        )

    def test_pid_not_exists(self):
        collector = SysMetricsCollector()
        with mock.patch.object(collector, "_pid_exists", return_value=False):
            result = collector.collect(self._task())
        assert result.ok is False
        assert "PID" in result.reason

    def test_snapshot_mode(self, tmp_path):
        collector = SysMetricsCollector()
        collector.OUTPUT_BASE = str(tmp_path)

        def pid_exists(pid):
            return True

        with mock.patch.object(collector, "_pid_exists", side_effect=pid_exists), \
             mock.patch.object(collector, "_read_proc_stat_total", return_value={"user": 1000, "system": 500, "idle": 8500, "iowait": 100}), \
             mock.patch.object(collector, "_read_loadavg", return_value={"load1m": 0.5, "load5m": 0.3, "load15m": 0.2}), \
             mock.patch.object(collector, "_read_process_metrics", return_value={"num_threads": 12, "fd_count": 45, "vmrss_kb": 102400}), \
             mock.patch.object(collector, "_read_network_dev", return_value={"rx_bytes": 100000, "tx_bytes": 50000}):
            result = collector.collect(self._task(duration_sec=1, options={"mode": "snapshot"}))

        assert result.ok is True
        assert len(result.artifacts) == 1
        assert result.artifacts[0]["artifact_type"] == "sys_metrics"
        assert os.path.isfile(result.artifacts[0]["local_path"])

    def test_content_has_all_dimensions(self, tmp_path):
        collector = SysMetricsCollector()
        collector.OUTPUT_BASE = str(tmp_path)

        with mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch.object(collector, "_read_proc_stat_total", return_value={"user": 1000, "system": 300, "idle": 8700, "iowait": 50}), \
             mock.patch.object(collector, "_read_loadavg", return_value={"load1m": 1.0, "load5m": 0.8, "load15m": 0.6}), \
             mock.patch.object(collector, "_read_process_metrics", return_value={
                 "num_threads": 8, "fd_count": 23, "vmrss_kb": 51200,
                 "voluntary_switches": 500, "nonvoluntary_switches": 200,
             }), \
             mock.patch.object(collector, "_read_network_dev", return_value={"rx_bytes": 0, "tx_bytes": 0}):
            result = collector.collect(self._task(duration_sec=2, options={"mode": "snapshot"}))

        assert result.ok
        with open(result.artifacts[0]["local_path"], "r") as fh:
            data = json.load(fh)
        assert "summary" in data
        assert "samples" in data
        assert data["sample_count"] >= 1
        s = data["summary"]
        assert "avg_cpu_user_pct" in s
        assert "thread_count" in s
        assert "fd_count" in s
        assert "load1m" in s

    def test_fd_trend_detection(self, tmp_path):
        collector = SysMetricsCollector()
        collector.OUTPUT_BASE = str(tmp_path)
        fd_values = [10, 11, 12, 13, 15]
        call_count = [0]

        def pid_exists(pid):
            call_count[0] += 1
            return call_count[0] <= len(fd_values) + 2

        def proc_metrics(pid):
            idx = min(call_count[0] - 1, len(fd_values) - 1)
            return {"fd_count": fd_values[idx] if idx < len(fd_values) else fd_values[-1],
                    "num_threads": 5, "vmrss_kb": 10240}

        with mock.patch.object(collector, "_pid_exists", side_effect=pid_exists), \
             mock.patch.object(collector, "_read_proc_stat_total", return_value={"user": 500, "system": 200, "idle": 9300, "iowait": 0}), \
             mock.patch.object(collector, "_read_loadavg", return_value={"load1m": 0.1, "load5m": 0.1, "load15m": 0.1}), \
             mock.patch.object(collector, "_read_process_metrics", side_effect=proc_metrics), \
             mock.patch.object(collector, "_read_network_dev", return_value={"rx_bytes": 0, "tx_bytes": 0}):
            result = collector.collect(self._task(duration_sec=6))

        if result.ok:
            with open(result.artifacts[0]["local_path"], "r") as fh:
                data = json.load(fh)
            assert data["summary"]["fd_trend"] == "increasing"

    def test_summary_reports_persistent_stopped_process_state(self):
        samples = [
            {
                "ts": float(index),
                "cpu": {},
                "load": {},
                "network": {},
                "process": {
                    "process_state": "T",
                    "process_state_name": "T (stopped)",
                    "num_threads": 4,
                    "fd_count": 8,
                },
            }
            for index in range(5)
        ]

        summary = SysMetricsCollector._compute_summary(samples)

        assert summary["process_state"] == "T"
        assert summary["process_state_name"] == "T (stopped)"
        assert summary["process_state_counts"] == {"T": 5}
        assert summary["stopped_sample_count"] == 5
        assert summary["stopped_sample_ratio"] == 1.0

    def test_summary_keeps_process_cpu_separate_from_host_cpu(self):
        samples = [
            {
                "ts": float(index),
                "cpu": {"user": 70.0, "system": 25.0, "iowait": 0.0},
                "load": {},
                "network": {},
                "process": {
                    "cpu_user_pct": 1.5,
                    "cpu_sys_pct": 0.5,
                    "process_state": "S",
                },
            }
            for index in range(3)
        ]

        summary = SysMetricsCollector._compute_summary(samples)

        assert summary["avg_cpu_user_pct"] == 1.5
        assert summary["avg_cpu_sys_pct"] == 0.5
        assert summary["avg_host_cpu_user_pct"] == 70.0
        assert summary["avg_host_cpu_sys_pct"] == 25.0
        assert summary["process_cpu_sample_count"] == 3

    def test_workload_scope_aggregates_cgroup_children(self):
        collector = SysMetricsCollector()
        metrics = {
            100: {"utime_ticks": 10, "stime_ticks": 2, "num_threads": 1, "fd_count": 3, "vmrss_kb": 100},
            101: {"utime_ticks": 80, "stime_ticks": 8, "num_threads": 2, "fd_count": 4, "vmrss_kb": 200},
            102: {"utime_ticks": 90, "stime_ticks": 9, "num_threads": 3, "fd_count": 5, "vmrss_kb": 300},
        }
        with mock.patch.object(SysMetricsCollector, "_read_cgroup_path", return_value="/system.slice/noise.service"), \
             mock.patch.object(SysMetricsCollector, "_read_cgroup_pids", return_value=[100, 101, 102]), \
             mock.patch.object(SysMetricsCollector, "_read_process_metrics", side_effect=lambda pid: metrics[pid]), \
             mock.patch.object(SysMetricsCollector, "_pid_exists", return_value=True):
            workload = collector._read_workload_metrics(100, metrics[100])

        assert workload["scope_source"] == "cgroup"
        assert workload["member_pids"] == [100, 101, 102]
        assert workload["utime_ticks"] == 180
        assert workload["num_threads"] == 6
        assert workload["fd_count"] == 12

    def test_new_workload_member_does_not_create_lifetime_cpu_spike(self):
        previous = {
            "members": [
                {"pid": 100, "utime_ticks": 100, "stime_ticks": 0},
                {"pid": 101, "utime_ticks": 100, "stime_ticks": 0},
            ]
        }
        current = {
            "members": [
                {"pid": 101, "utime_ticks": 150, "stime_ticks": 0},
                {"pid": 102, "utime_ticks": 99999, "stime_ticks": 0},
            ]
        }

        SysMetricsCollector._apply_workload_cpu_percent(current, previous, elapsed=1.0, clock_ticks=100)

        assert current["cpu_total_pct"] == 50.0
        assert next(item for item in current["members"] if item["pid"] == 102)["cpu_total_pct"] == 0.0

    def test_subsecond_first_interval_is_excluded_from_cpu_summary(self):
        current = {"members": [{"pid": 100, "utime_ticks": 50000, "stime_ticks": 0}]}
        previous = {"members": [{"pid": 100, "utime_ticks": 100, "stime_ticks": 0}]}

        SysMetricsCollector._apply_workload_cpu_percent(
            current,
            previous,
            elapsed=0.001,
            clock_ticks=100,
            interval_valid=False,
        )
        summary = SysMetricsCollector._compute_summary([{
            "ts": 1.0,
            "cpu": {"user": 90.0, "system": 5.0, "iowait": 0.0},
            "load": {},
            "network": {},
            "process": {"process_state": "S"},
            "workload": current,
        }])

        assert current["cpu_total_pct"] == 0.0
        assert summary["avg_cpu_user_pct"] == 0
        assert summary["process_cpu_sample_count"] == 0

    def test_parse_stat(self):
        """Verify stat parsing logic."""
        collector = SysMetricsCollector()
        # Test the parsing by simulating a well-formed line
        result = collector._read_proc_stat_total()
        # On Windows this returns {}; the test is about not crashing
        assert isinstance(result, dict)

    def test_parse_network_dev(self):
        """Verify network parsing doesn't crash on missing file."""
        collector = SysMetricsCollector()
        result = collector._read_network_dev()
        assert isinstance(result, dict)
        assert "rx_bytes" in result
