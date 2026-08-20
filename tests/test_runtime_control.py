from __future__ import annotations

import json
import time
from pathlib import Path

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.runtime_control import RuntimeControlCollector
from agent.mini_drop_agent.runtime_control import (
    RuntimeControlEventStore,
    RuntimeControlObserver,
    normalize_kubernetes_audit_event,
    normalize_release_event,
)


def test_runtime_control_store_filters_target_and_window(tmp_path):
    store = RuntimeControlEventStore(str(tmp_path / "events.ndjson"))
    matching = store.append({
        "source": "test",
        "event_type": "signal_sent",
        "observed_at": "2026-08-19T06:21:31Z",
        "actor": {"pid": 10, "comm": "bash"},
        "action": {"operation": "signal", "signal": "SIGSTOP"},
        "target": {"pid": 20, "comm": "python3"},
        "effect": {"expected_state": "T"},
    })
    store.append({
        "source": "test",
        "event_type": "signal_sent",
        "observed_at": "2026-08-19T06:21:31Z",
        "target": {"pid": 21},
    })

    events = store.query(target_pid=20, start=1787120490, end=1787120510)

    assert [item["event_id"] for item in events] == [matching["event_id"]]


def test_signal_line_keeps_actor_action_and_target():
    event = RuntimeControlObserver._parse_signal_line("123|10|bash|19|20|python3")

    assert event["actor"] == {"pid": 10, "comm": "bash", "uid": 0}
    assert event["action"]["signal"] == "SIGSTOP"
    assert event["target"] == {"pid": 20, "comm": "python3"}
    assert event["effect"]["expected_state"] == "T"


def test_systemd_journal_extracts_message_unit_and_normal_operation():
    event = RuntimeControlObserver._parse_journal_line(json.dumps({
        "__REALTIME_TIMESTAMP": "1787129524004050",
        "_SYSTEMD_UNIT": "init.scope",
        "_PID": "1",
        "_COMM": "systemd",
        "_UID": "0",
        "MESSAGE": (
            "Stopping md-aiopsv2-stall.service - /usr/bin/python3 "
            "/home/worker1/mini-drop/fault_helpers/python_runtime_fault.py..."
        ),
    }))

    assert event["action"]["operation"] == "stop"
    assert event["target"]["unit"] == "md-aiopsv2-stall.service"


def test_runtime_control_store_matches_terms_outside_target(tmp_path):
    store = RuntimeControlEventStore(str(tmp_path / "events.ndjson"))
    store.append({
        "source": "systemd_journal",
        "event_type": "systemd_unit_control",
        "observed_at": "2026-08-19T06:21:31Z",
        "actor": {"pid": 1, "comm": "systemd"},
        "action": {"operation": "started", "message": "Started md-aiopsv2-stall.service"},
        "target": {"unit": "md-aiopsv2-stall.service", "pid": 1},
        "effect": {"unit_state_changed": True},
    })

    events = store.query(start=1787120490, end=1787120510, target_terms=["md-aiopsv2-stall.service"])

    assert len(events) == 1


def test_docker_event_is_normalized():
    event = RuntimeControlObserver._parse_docker_line(json.dumps({
        "time": 1787120491,
        "Action": "pause",
        "Actor": {
            "ID": "abc",
            "Attributes": {"name": "cart", "com.docker.swarm.service.name": "cartservice"},
        },
    }))

    assert event["event_type"] == "container_runtime_control"
    assert event["action"]["operation"] == "pause"
    assert event["target"]["service_id"] == "cartservice"


def test_containerd_event_is_normalized():
    event = RuntimeControlObserver._parse_containerd_line(json.dumps({
        "timestamp": "2026-08-19T06:21:31Z",
        "namespace": "default",
        "topic": "/tasks/kill",
        "event": {"container_id": "payment-123"},
    }))

    assert event["source"] == "containerd_events"
    assert event["action"]["operation"] == "kill"
    assert event["target"]["container_id"] == "payment-123"


def test_kubernetes_audit_event_redacts_sensitive_fields_in_store(tmp_path):
    event = normalize_kubernetes_audit_event({
        "verb": "patch",
        "requestURI": "/apis/apps/v1/namespaces/prod/deployments/payment",
        "requestReceivedTimestamp": "2026-08-19T06:21:31Z",
        "user": {"username": "system:serviceaccount:ci:deployer", "groups": ["system:serviceaccounts"]},
        "sourceIPs": ["10.0.0.5"],
        "objectRef": {"apiGroup": "apps", "resource": "deployments", "namespace": "prod", "name": "payment"},
        "responseStatus": {"code": 200},
    })
    event["actor"]["token"] = "must-not-survive"
    store = RuntimeControlEventStore(str(tmp_path / "events.ndjson"))

    stored = store.append(event)

    assert stored["source"] == "kubernetes_audit"
    assert stored["actor"]["username"] == "system:serviceaccount:ci:deployer"
    assert stored["actor"]["token"] == "[REDACTED]"
    assert stored["target"]["name"] == "payment"


def test_release_event_uses_shared_contract():
    event = normalize_release_event({
        "timestamp": "2026-08-19T06:21:31Z",
        "pipeline": "deploy-prod",
        "action": "rollout",
        "service": "paymentservice",
        "version": "v2",
    })

    assert event["event_type"] == "deployment_or_configuration_change"
    assert event["target"]["service_id"] == "paymentservice"
    assert event["action"]["operation"] == "rollout"


def test_runtime_control_collector_builds_causal_edges(tmp_path):
    store_path = tmp_path / "events.ndjson"
    store = RuntimeControlEventStore(str(store_path))
    store.append({
        "source": "ebpf_signal_generate",
        "event_type": "signal_sent",
        "observed_at": "2026-08-19T06:21:31Z",
        "actor": {"pid": 10, "comm": "bash"},
        "action": {"operation": "signal", "signal": "SIGSTOP"},
        "target": {"pid": 20, "comm": "python3", "service_id": "paymentservice"},
        "effect": {"expected_state": "T"},
    })
    collector = RuntimeControlCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="control-history",
        collector_type="runtime_control_history",
        target_pid=20,
        sample_rate=1,
        duration_sec=60,
        options={
            "runtime_control_store": str(store_path),
            "window_start": 1787120490,
            "window_end": 1787120510,
            "target_config": {"service_id": "paymentservice"},
        },
    )

    result = collector.collect(task)
    payload = result.artifacts[0]["metadata"]["data"]

    assert result.ok is True
    assert payload["evidence_validity"]["evidence_status"] == "valid"
    assert payload["summary"]["has_direct_control_chain"] is True
    assert payload["summary"]["has_complete_source_chain"] is False
    assert payload["summary"]["origin_unknown"] is True
    assert [edge["relation"] for edge in payload["causal_edges"]] == ["ISSUED", "TARGETED", "IMPACTED"]


def test_runtime_control_collector_matches_docker_pause_by_exact_container_id(tmp_path):
    store_path = tmp_path / "events.ndjson"
    store = RuntimeControlEventStore(str(store_path))
    store.append({
        "source": "docker_events",
        "event_type": "container_runtime_control",
        "observed_at": "2026-08-19T06:21:31Z",
        "actor": {"kind": "docker_daemon"},
        "action": {"operation": "pause"},
        "target": {
            "container_id": "payment-container-123",
            "container_name": "boutique_paymentservice.1",
            "service_id": "paymentservice",
        },
        "effect": {"runtime_state_changed": True},
    })
    collector = RuntimeControlCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="docker-pause-history",
        collector_type="runtime_control_history",
        target_pid=52544,
        sample_rate=1,
        duration_sec=60,
        options={
            "runtime_control_store": str(store_path),
            "window_start": 1787120490,
            "window_end": 1787120510,
            "target_config": {
                "service_id": "paymentservice",
                "container_id": "payment-container-123",
            },
        },
    )

    payload = collector.collect(task).artifacts[0]["metadata"]["data"]

    assert payload["summary"]["has_direct_control_chain"] is True
    assert payload["events"][0]["qualification"]["exact_pid_match"] is False
    assert payload["events"][0]["qualification"]["exact_container_match"] is True
    assert payload["events"][0]["qualification"]["direct_control_chain"] is True


def test_runtime_control_collector_rejects_docker_pause_for_another_container(tmp_path):
    store_path = tmp_path / "events.ndjson"
    store = RuntimeControlEventStore(str(store_path))
    store.append({
        "source": "docker_events",
        "event_type": "container_runtime_control",
        "observed_at": "2026-08-19T06:21:31Z",
        "actor": {"kind": "docker_daemon"},
        "action": {"operation": "pause"},
        "target": {
            "container_id": "other-container-456",
            "service_id": "paymentservice",
        },
        "effect": {"runtime_state_changed": True},
    })
    collector = RuntimeControlCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="mismatched-docker-pause-history",
        collector_type="runtime_control_history",
        target_pid=52544,
        sample_rate=1,
        duration_sec=60,
        options={
            "runtime_control_store": str(store_path),
            "window_start": 1787120490,
            "window_end": 1787120510,
            "target_config": {
                "service_id": "paymentservice",
                "container_id": "payment-container-123",
            },
        },
    )

    payload = collector.collect(task).artifacts[0]["metadata"]["data"]

    assert payload["summary"]["has_direct_control_chain"] is False
    assert payload["events"][0]["qualification"]["exact_container_match"] is False
    assert payload["events"][0]["qualification"]["direct_control_chain"] is False


def test_runtime_control_collector_rejects_target_mismatch_and_invalid_order(tmp_path):
    store_path = tmp_path / "events.ndjson"
    store = RuntimeControlEventStore(str(store_path))
    store.append({
        "source": "ebpf_signal_generate",
        "event_type": "signal_sent",
        "observed_at": "2026-08-19T06:21:31Z",
        "actor": {"pid": 10, "comm": "bash"},
        "action": {"operation": "signal", "signal": "SIGSTOP"},
        "target": {"pid": 21, "comm": "python3", "service_id": "paymentservice"},
        "effect": {"expected_state": "T"},
        "source_provenance": {"controller": "fault-runner", "initiated_at": "2026-08-19T06:21:32Z"},
    })
    collector = RuntimeControlCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="mismatched-control-history",
        collector_type="runtime_control_history",
        target_pid=20,
        sample_rate=1,
        duration_sec=60,
        options={
            "runtime_control_store": str(store_path),
            "window_start": 1787120490,
            "window_end": 1787120510,
            "target_config": {"service_id": "paymentservice"},
        },
    )

    payload = collector.collect(task).artifacts[0]["metadata"]["data"]

    assert payload["summary"]["has_direct_control_chain"] is False
    assert payload["summary"]["has_complete_source_chain"] is False
    assert payload["events"][0]["qualification"]["exact_target_match"] is False
    assert payload["events"][0]["qualification"]["source_precedes_action"] is False


def test_runtime_control_collector_recognizes_complete_source_chain(tmp_path):
    store_path = tmp_path / "events.ndjson"
    store = RuntimeControlEventStore(str(store_path))
    store.append({
        "source": "ebpf_signal_generate",
        "event_type": "signal_sent",
        "observed_at": "2026-08-19T06:21:31Z",
        "actor": {"pid": 10, "comm": "bash"},
        "action": {"operation": "signal", "signal": "SIGSTOP"},
        "target": {"pid": 20, "comm": "python3"},
        "effect": {"expected_state": "T"},
        "source_provenance": {
            "controller": "fault-runner",
            "redacted_command_source": "scenario:runtime-stall",
            "initiated_at": "2026-08-19T06:21:30Z",
        },
    })
    collector = RuntimeControlCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="source-control-history",
        collector_type="runtime_control_history",
        target_pid=20,
        sample_rate=1,
        duration_sec=60,
        options={
            "runtime_control_store": str(store_path),
            "window_start": 1787120490,
            "window_end": 1787120510,
        },
    )

    payload = collector.collect(task).artifacts[0]["metadata"]["data"]

    assert payload["summary"]["has_direct_control_chain"] is True
    assert payload["summary"]["has_complete_source_chain"] is True
    assert payload["summary"]["origin_unknown"] is False
    assert payload["causal_edges"][0]["relation"] == "INITIATED"


def test_runtime_control_collector_empty_history_is_not_valid(tmp_path):
    collector = RuntimeControlCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="empty-control-history",
        collector_type="runtime_control_history",
        target_pid=20,
        sample_rate=1,
        duration_sec=60,
        options={"runtime_control_store": str(tmp_path / "missing.ndjson")},
    )

    payload = collector.collect(task).artifacts[0]["metadata"]["data"]

    assert payload["events"] == []
    assert payload["evidence_validity"]["evidence_status"] == "empty_window"


def test_runtime_control_collector_default_window_looks_back_180_seconds(tmp_path, monkeypatch):
    store_path = tmp_path / "events.ndjson"
    store = RuntimeControlEventStore(str(store_path))
    store.append({
        "source": "docker_events",
        "event_type": "container_runtime_control",
        "observed_at": time.time() - 120,
        "actor": {"kind": "docker_daemon"},
        "action": {"operation": "pause"},
        "target": {"container_id": "payment-container-123", "service_id": "paymentservice"},
        "effect": {"container_state_changed": True},
    })
    monkeypatch.setattr(
        "agent.mini_drop_agent.collectors.runtime_control.runtime_target_snapshot",
        lambda *_args, **_kwargs: {"direct_failure_mechanism_observed": False, "state_flags": {}},
    )
    collector = RuntimeControlCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="default-lookback",
        collector_type="runtime_control_history",
        target_pid=20,
        sample_rate=1,
        duration_sec=60,
        options={
            "runtime_control_store": str(store_path),
            "target_config": {"service_id": "paymentservice", "container_id": "payment-container-123"},
        },
    )

    payload = collector.collect(task).artifacts[0]["metadata"]["data"]

    assert payload["evidence_window"]["duration_sec"] >= 180
    assert payload["summary"]["has_direct_control_chain"] is True


def test_current_paused_state_is_partial_mechanism_not_historical_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.mini_drop_agent.collectors.runtime_control.runtime_target_snapshot",
        lambda *_args, **_kwargs: {
            "direct_failure_mechanism_observed": True,
            "state_flags": {"container_paused": True, "process_stopped": False, "cgroup_frozen": False},
        },
    )
    collector = RuntimeControlCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="paused-snapshot-only",
        collector_type="runtime_control_history",
        target_pid=20,
        sample_rate=1,
        duration_sec=60,
        options={"runtime_control_store": str(tmp_path / "missing.ndjson")},
    )

    payload = collector.collect(task).artifacts[0]["metadata"]["data"]

    assert payload["events"] == []
    assert payload["causal_edges"] == []
    assert payload["summary"]["has_direct_control_chain"] is False
    assert payload["summary"]["has_current_failure_mechanism"] is True
    assert payload["evidence_validity"]["evidence_status"] == "partial"
