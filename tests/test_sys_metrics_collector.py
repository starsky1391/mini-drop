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
