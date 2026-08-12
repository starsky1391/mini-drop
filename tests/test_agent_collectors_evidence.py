"""Tests for industrial collector adapters."""

from __future__ import annotations

import json

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.dependency import DependencyCheckCollector
from agent.mini_drop_agent.collectors.log_scan import LogScanCollector
from agent.mini_drop_agent.collectors.redis_check import RedisCheckCollector


def test_log_scan_adapter_normalizes_fluent_bit_or_otel_output(tmp_path):
    source = tmp_path / "logs.ndjson"
    source.write_text(
        "\n".join([
            json.dumps({"timestamp": 1720000000, "severity": "INFO", "message": "boot ok"}),
            json.dumps({
                "timestamp": 1720000001,
                "severity": "ERROR",
                "message": "trace_id=trace-abc GET /checkout Redis timeout while calling paymentservice",
                "attributes": {"peer.service": "redis"},
            }),
            json.dumps({
                "timestamp": 1720000002,
                "severity": "WARN",
                "body": "traceId=trace-def POST /cart paymentservice connection refused",
            }),
        ]),
        encoding="utf-8",
    )
    collector = LogScanCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")

    result = collector.collect(CollectorTask(
        id="task_log",
        collector_type="log_scan",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={
            "source_path": str(source),
            "window_start": 1719999999,
            "window_end": 1720000003,
            "trigger_event_id": "evt_1",
            "evidence_cohort_id": "cohort_1",
            "collection_mode": "triggered_group",
            "timing_relation": "same_window",
        },
    ))

    payload = _artifact_json(result.artifacts[0])
    assert result.ok is True
    assert result.artifacts[0]["artifact_type"] == "log_window_json"
    assert result.artifacts[0]["collector_family"] == "log_scan"
    assert result.artifacts[0]["evidence_cohort_id"] == "cohort_1"
    assert payload["adapter"]["kind"] == "fluent_bit_or_otel_filelog"
    assert payload["evidence_window"]["timing_relation"] == "same_window"
    assert payload["summary"]["matched_records"] == 2
    assert payload["summary"]["first_seen"] == 1720000001
    assert payload["summary"]["last_seen"] == 1720000002
    assert payload["error_clusters"]
    assert "paymentservice" in payload["evidence_index"]["dependencies"]
    assert "redis" in payload["evidence_index"]["dependencies"]
    assert "trace-abc" in payload["evidence_index"]["trace_ids"]
    assert "/checkout" in payload["evidence_index"]["endpoints"]


def test_dependency_check_adapter_normalizes_blackbox_exporter_metrics(tmp_path):
    collector = DependencyCheckCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    ok_metrics = """
probe_success 1
probe_duration_seconds 0.051
probe_dns_lookup_time_seconds 0.002
probe_tcp_connect_duration_seconds 0.006
probe_http_status_code 200
"""
    bad_metrics = """
probe_success 0
probe_duration_seconds 3
probe_dns_lookup_time_seconds 0
probe_tcp_connect_duration_seconds 0
probe_http_status_code 0
"""

    result = collector.collect(CollectorTask(
        id="task_dependency",
        collector_type="dependency_check",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={
            "trigger_event_id": "evt_1",
            "evidence_cohort_id": "cohort_1",
            "collection_mode": "triggered_group",
            "timing_relation": "same_window",
            "targets": [
                {"dependency_id": "api", "protocol": "http", "url": "http://api.local/health", "blackbox_metrics": ok_metrics},
                {"dependency_id": "bad-dns", "protocol": "tcp", "host": "dns-fail.local", "port": 9, "blackbox_metrics": bad_metrics},
            ],
        },
    ))

    payload = _artifact_json(result.artifacts[0])
    checks = {item["dependency_id"]: item for item in payload["checks"]}
    assert result.ok is True
    assert result.artifacts[0]["artifact_type"] == "dependency_check_json"
    assert result.artifacts[0]["collector_family"] == "dependency_check"
    assert result.artifacts[0]["evidence_cohort_id"] == "cohort_1"
    assert payload["adapter"]["kind"] == "prometheus_blackbox_exporter"
    assert payload["collector_invocation"]["target_config"]["dependency_targets"][0]["dependency_id"] == "api"
    assert payload["evidence_window"]["timing_relation"] == "same_window"
    assert checks["api"]["success"] is True
    assert checks["api"]["http_status_code"] == 200
    assert checks["bad-dns"]["success"] is False
    assert checks["bad-dns"]["failure_phase"] == "dns"
    assert payload["summary"]["dns_failures"] == 1


def test_redis_check_adapter_normalizes_redis_exporter_metrics(tmp_path):
    collector = RedisCheckCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    metrics = """
redis_up 1
redis_connected_clients 2
redis_blocked_clients 0
redis_memory_used_bytes 1024
redis_evicted_keys_total 3
redis_rejected_connections_total 1
redis_total_error_replies 4
redis_slowlog_length 5
redis_slowlog_last_duration_seconds 0.032
redis_latency_spike_last_ms 30
redis_exporter_last_scrape_duration_seconds 0.007
"""

    result = collector.collect(CollectorTask(
        id="task_redis",
        collector_type="redis_check",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={
            "metrics_text": metrics,
            "target_config": {
                "redis_target": {
                    "dependency_id": "redis-main",
                    "host": "redis.local",
                    "port": 6379,
                    "url": "redis://redis.local:6379",
                },
            },
            "trigger_event_id": "evt_1",
            "evidence_cohort_id": "cohort_1",
            "collection_mode": "triggered_group",
            "timing_relation": "same_window",
        },
    ))

    payload = _artifact_json(result.artifacts[0])
    assert result.ok is True
    assert result.artifacts[0]["artifact_type"] == "redis_check_json"
    assert result.artifacts[0]["collector_family"] == "redis_check"
    assert result.artifacts[0]["evidence_cohort_id"] == "cohort_1"
    assert payload["adapter"]["kind"] == "redis_exporter_prometheus"
    assert payload["collector_invocation"]["target_config"]["redis_target"]["host"] == "redis.local"
    assert payload["target"]["url"] == "redis://redis.local:6379"
    assert payload["evidence_window"]["timing_relation"] == "same_window"
    assert payload["connectivity"]["ping_ok"] is True
    assert payload["info_summary"]["connected_clients"] == 2
    assert payload["info_summary"]["evicted_keys_total"] == 3
    assert payload["slowlog_summary"]["entry_count"] == 5
    assert payload["slowlog_summary"]["max_duration_us"] == 32000
    assert payload["latency_summary"]["max_latency_ms"] == 30


def test_redis_check_requires_target_config_without_fixture_metrics(tmp_path, monkeypatch):
    collector = RedisCheckCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    monkeypatch.setenv("MINI_DROP_REDIS_EXPORTER_URL", "http://redis-exporter:9121")

    result = collector.collect(CollectorTask(
        id="task_redis_missing_target",
        collector_type="redis_check",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    ))

    assert result.ok is False
    assert "Redis target_config" in result.reason


def _artifact_json(artifact: dict) -> dict:
    return json.loads(open(artifact["local_path"], encoding="utf-8").read())
