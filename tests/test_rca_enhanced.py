"""Tests for enhanced RCA rule matchers (sys_metrics / multi_metric / fd_trend / cross_evidence)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from server.app.rca.models import EvidenceInput
from server.app.rca.candidates import (
    _match_sys_metric_threshold,
    _match_fd_trend,
    _match_thread_trend,
    _match_cross_evidence,
    _match_multi_metric,
    _match_off_cpu_wait_hotspot,
    _match_runtime_top_function_present,
    generate_candidates,
    load_rules,
    _resolve_path,
)


class TestSysMetricMatchers:
    """Test individual matcher functions."""

    def _evidence(self, **overrides) -> EvidenceInput:
        defaults = {
            "task_metadata": {"collector_type": "sys_metrics", "status": "DONE"},
            "sys_metrics": {
                "sample_count": 10,
                "summary": {
                    "avg_cpu_sys_pct": 35.0,
                    "avg_cpu_iowait_pct": 12.0,
                    "avg_cpu_user_pct": 45.0,
                    "thread_count": 120,
                    "thread_trend": "increasing",
                    "fd_count": 350,
                    "fd_trend": "increasing",
                    "fd_max": 400,
                    "vmrss_mb": 512.0,
                    "vmrss_mb_max": 768.0,
                    "ctx_nonvoluntary_rate": 12000.0,
                    "ctx_voluntary_rate": 500.0,
                    "net_rx_kbps": 60000.0,
                    "net_tx_kbps": 15000.0,
                    "load1m": 3.5,
                },
            },
            "top_functions": [{"name": "compute_hotspot", "percent": 55.0}],
            "ebpf_metrics": {"io_latency_us": {"[0,128)": 100}},
        }
        return EvidenceInput(**{**defaults, **overrides})

    # ── sys_metric_threshold ──

    def test_sys_cpu_high_triggered(self):
        ev = self._evidence()
        assert _match_sys_metric_threshold(ev, {"metric_path": "summary.avg_cpu_sys_pct", "op": "gt", "value": 30})

    def test_sys_cpu_high_not_triggered(self):
        ev = self._evidence(sys_metrics={"summary": {"avg_cpu_sys_pct": 15.0}, "sample_count": 5})
        assert not _match_sys_metric_threshold(ev, {"metric_path": "summary.avg_cpu_sys_pct", "op": "gt", "value": 30})

    def test_min_samples_insufficient(self):
        ev = self._evidence(sys_metrics={"summary": {"avg_cpu_sys_pct": 50}, "sample_count": 2})
        assert not _match_sys_metric_threshold(ev, {"metric_path": "summary.avg_cpu_sys_pct", "op": "gt", "value": 30, "min_samples": 5})

    def test_contains_match(self):
        ev = self._evidence(sys_metrics={"summary": {"fd_trend": "increasing"}, "sample_count": 5})
        assert _match_sys_metric_threshold(ev, {"metric_path": "summary.fd_trend", "op": "contains", "value": "creas"})

    def test_gte_lte_ops(self):
        ev = self._evidence(sys_metrics={"summary": {"x": 10}, "sample_count": 1})
        assert _match_sys_metric_threshold(ev, {"metric_path": "summary.x", "op": "gte", "value": 10})
        assert _match_sys_metric_threshold(ev, {"metric_path": "summary.x", "op": "lte", "value": 10})
        assert not _match_sys_metric_threshold(ev, {"metric_path": "summary.x", "op": "gt", "value": 10})

    def test_none_for_missing_path(self):
        assert _resolve_path({"a": {"b": 1}}, "x.y.z") is None
        assert _resolve_path({"a": {"b": 1}}, "a.b") == 1

    # ── fd_trend ──

    def test_fd_trend_increasing(self):
        ev = self._evidence()
        assert _match_fd_trend(ev, {"min_fd_count": 30})

    def test_fd_trend_stable_not_matched(self):
        ev = self._evidence(sys_metrics={"summary": {"fd_count": 100, "fd_trend": "stable"}})
        assert not _match_fd_trend(ev, {"min_fd_count": 30})

    def test_fd_trend_below_threshold(self):
        ev = self._evidence(sys_metrics={"summary": {"fd_count": 10, "fd_trend": "increasing"}})
        assert not _match_fd_trend(ev, {"min_fd_count": 30})

    # ── thread_trend ──

    def test_thread_trend_increasing(self):
        ev = self._evidence()
        assert _match_thread_trend(ev, {"min_threads": 50})

    def test_thread_trend_below_threshold(self):
        ev = self._evidence(sys_metrics={"summary": {"thread_count": 20, "thread_trend": "increasing"}})
        assert not _match_thread_trend(ev, {"min_threads": 50})

    # ── multi_metric ──

    def test_multi_metric_all_conditions_met(self):
        ev = self._evidence(sys_metrics={"summary": {"fd_count": 250, "fd_trend": "increasing"}, "sample_count": 5})
        assert _match_multi_metric(ev, {
            "conditions": [
                {"metric_path": "summary.fd_count", "op": "gt", "value": 200},
                {"metric_path": "summary.fd_trend", "op": "eq", "value": "increasing"},
            ]
        })

    def test_multi_metric_one_condition_fails(self):
        ev = self._evidence(sys_metrics={"summary": {"fd_count": 50, "fd_trend": "increasing"}, "sample_count": 5})
        assert not _match_multi_metric(ev, {
            "conditions": [
                {"metric_path": "summary.fd_count", "op": "gt", "value": 200},
                {"metric_path": "summary.fd_trend", "op": "eq", "value": "increasing"},
            ]
        })

    # ── cross_evidence ──

    def test_cross_fd_thread(self):
        ev = self._evidence()
        assert _match_cross_evidence(ev, {"signals": ["fd_growth", "thread_growth"]})

    def test_cross_cpu_memory(self):
        ev = self._evidence(sys_metrics=self._evidence().sys_metrics)
        assert _match_cross_evidence(ev, {"signals": ["cpu_hotspot", "memory_growth"]})

    def test_cross_io_iowait(self):
        ev = self._evidence()
        assert _match_cross_evidence(ev, {"signals": ["io_high", "iowait_high"]})

    def test_cross_one_signal_missing(self):
        ev = self._evidence(sys_metrics={"summary": {"fd_trend": "increasing", "thread_trend": "stable"}})
        assert not _match_cross_evidence(ev, {"signals": ["fd_growth", "thread_growth"]})

    def test_runtime_top_function_present(self):
        ev = self._evidence(
            task_metadata={"status": "DONE", "collector_type": "pyspy"},
            top_functions=[{"name": "threading.Lock.acquire", "samples": 42, "percent": 70.0}],
        )
        assert _match_runtime_top_function_present(ev, {"min_percent": 1})

    def test_runtime_top_function_requires_python_runtime_collector(self):
        ev = self._evidence(
            top_functions=[{"name": "pthread_mutex_lock", "samples": 42, "percent": 70.0}],
        )
        assert not _match_runtime_top_function_present(ev, {"min_percent": 1})

    def test_off_cpu_wait_hotspot_present(self):
        ev = self._evidence(
            evidence_index={
                "off_cpu_wait": {
                    "summary": {"sample_count": 2, "top_wait_reason": "interruptible_sleep_or_lock_wait"},
                    "top_wait_stacks": [{"top_frame": "pthread_mutex_lock", "samples": 2}],
                }
            }
        )
        assert _match_off_cpu_wait_hotspot(ev, {"min_samples": 1})


class TestGenerateCandidatesEnhanced:
    """Test generate_candidates with comprehensive rules."""

    def test_all_cpu_rules_fire(self):
        ev = EvidenceInput(
            task_metadata={"status": "DONE", "collector_type": "perf_cpu"},
            top_functions=[{"name": "recursive_compute", "percent": 65.0}],
            sys_metrics={
                "sample_count": 10,
                "summary": {
                    "avg_cpu_sys_pct": 45.0,
                    "avg_cpu_user_pct": 55.0,
                    "avg_cpu_iowait_pct": 2.0,
                    "thread_count": 30,
                    "thread_trend": "stable",
                    "fd_count": 20,
                    "fd_trend": "stable",
                    "fd_max": 25,
                    "vmrss_mb": 200,
                    "vmrss_mb_max": 250,
                    "ctx_nonvoluntary_rate": 500,
                    "net_rx_kbps": 100,
                    "net_tx_kbps": 50,
                    "load1m": 0.5,
                },
            },
        )
        candidates = generate_candidates(ev)
        ids = [c.candidate_id for c in candidates]
        assert "cpu_hotspot_recursive" in ids
        assert "cpu_sys_kernel_overhead" in ids
        assert "cpu_userland_hotspot" in ids

    def test_fd_leak_rules_fire(self):
        ev = EvidenceInput(
            task_metadata={"status": "DONE"},
            sys_metrics={
                "sample_count": 10,
                "summary": {
                    "avg_cpu_sys_pct": 5.0, "avg_cpu_user_pct": 10.0, "avg_cpu_iowait_pct": 1.0,
                    "thread_count": 10, "thread_trend": "stable",
                    "fd_count": 550, "fd_trend": "increasing", "fd_max": 600,
                    "vmrss_mb": 100, "vmrss_mb_max": 150,
                    "ctx_nonvoluntary_rate": 200,
                    "net_rx_kbps": 10, "net_tx_kbps": 5, "load1m": 0.2,
                },
            },
        )
        candidates = generate_candidates(ev)
        ids = [c.candidate_id for c in candidates]
        assert "fd_leak_detected" in ids
        assert "fd_high_watermark" in ids
        assert "fd_exhaustion_risk" in ids

    def test_cross_evidence_fires(self):
        ev = EvidenceInput(
            task_metadata={"status": "DONE"},
            top_functions=[{"name": "lock_contented_func", "percent": 50.0}],
            ebpf_metrics={"io_latency_us": {"[0,128)": 500}},
            sys_metrics={
                "sample_count": 10,
                "summary": {
                    "avg_cpu_sys_pct": 10.0, "avg_cpu_user_pct": 30.0, "avg_cpu_iowait_pct": 15.0,
                    "thread_count": 20, "thread_trend": "increasing",
                    "fd_count": 15, "fd_trend": "increasing",
                    "fd_max": 50,
                    "vmrss_mb": 500, "vmrss_mb_max": 600,
                    "ctx_nonvoluntary_rate": 15000,
                    "net_rx_kbps": 100, "net_tx_kbps": 50,
                    "load1m": 1.0,
                },
            },
        )
        candidates = generate_candidates(ev)
        ids = [c.candidate_id for c in candidates]
        assert "cross_fd_plus_thread_escalation" in ids
        assert "cross_io_plus_cpu_wait" in ids
        assert "cross_cpu_plus_memory_leak" in ids
        assert "cross_cpu_ctx_contention" in ids

    def test_python_runtime_stack_candidate_fires(self):
        ev = EvidenceInput(
            task_metadata={"status": "DONE", "collector_type": "pyspy"},
            top_functions=[{"name": "threading.Lock.acquire", "samples": 42, "percent": 70.0}],
            evidence_index={"stack_summary": {"sample_count": 42}},
        )
        candidates = generate_candidates(ev)
        ids = [c.candidate_id for c in candidates]
        assert "python_runtime_stack_hotspot" in ids

    def test_off_cpu_wait_candidate_fires(self):
        ev = EvidenceInput(
            task_metadata={"status": "DONE", "collector_type": "off_cpu_wait_profile"},
            top_functions=[{"name": "pthread_mutex_lock", "samples": 2, "percent": 100.0}],
            evidence_index={
                "off_cpu_wait": {
                    "summary": {"sample_count": 2, "top_wait_reason": "interruptible_sleep_or_lock_wait"},
                    "top_wait_stacks": [{"top_frame": "pthread_mutex_lock", "samples": 2}],
                }
            },
        )
        candidates = generate_candidates(ev)
        ids = [c.candidate_id for c in candidates]
        assert "off_cpu_wait_hotspot" in ids

    def test_no_sys_metrics_skips_sys_rules(self):
        """Without sys_metrics, only legacy rules should fire."""
        ev = EvidenceInput(
            task_metadata={"status": "DONE", "collector_type": "perf_cpu"},
            top_functions=[{"name": "recursive_fib", "percent": 60.0}],
            ebpf_metrics={"io_latency_us": {"[64,128)": 50}},
        )
        candidates = generate_candidates(ev)
        ids = [c.candidate_id for c in candidates]
        assert "cpu_hotspot_recursive" in ids
        assert "io_wait_high" in ids
        # No sys_metrics-dependent rules
        assert not any(cid.startswith(("fd_", "memory_", "thread_", "network_", "cross_", "sys_cpu_")) for cid in ids)

    def test_rules_json_loads(self):
        """Verify all rules in rules.json parse correctly."""
        rules = load_rules()
        load_rules.cache_clear()
        rules2 = load_rules()
        assert len(rules) >= 25  # we have 25+ rules
        valid_types = {"top_function_keyword", "ebpf_latency_present", "collector_or_suggestion",
                       "agent_cpu_overhead", "failure_contains", "sys_metric_threshold",
                       "multi_metric", "fd_trend", "thread_trend", "cross_evidence",
                       "dependency_failure", "redis_failure", "runtime_top_function_present",
                       "off_cpu_wait_hotspot"}
        for r in rules:
            assert r["match_type"] in valid_types, f"Unknown match_type: {r['match_type']}"
            assert isinstance(r["rule_score"], (int, float))
            assert 0 <= r["rule_score"] <= 1
