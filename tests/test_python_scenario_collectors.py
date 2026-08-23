"""Tests for Python scenario telemetry adapters."""

from __future__ import annotations

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.python_scenarios import (
    PythonCacheProfileCollector,
    PythonExceptionProfileCollector,
    PythonInputProfileCollector,
    PythonLockWaitCollector,
    PythonPoolProfileCollector,
    PythonQueueProfileCollector,
    PythonRetryTimeoutProfileCollector,
)


def _task(collector_type: str, options: dict | None = None) -> CollectorTask:
    return CollectorTask(
        id=f"{collector_type}_test",
        collector_type=collector_type,
        target_pid=1234,
        sample_rate=1,
        duration_sec=10,
        options=options or {},
    )


def _payload(result):
    assert result.artifacts
    return result.artifacts[0]["metadata"]["data"]


def test_missing_upstream_does_not_emit_valid_lock_wait_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    result = PythonLockWaitCollector().collect(_task("python_lock_wait_profile"))

    payload = _payload(result)
    assert result.ok is False
    assert payload["evidence_validity"]["evidence_status"] == "blocked"
    assert payload["evidence_validity"]["reason"] == "source_missing"
    assert payload["wait_sites"] == []


def test_lock_wait_profile_normalizes_pyspy_or_offcpu_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    result = PythonLockWaitCollector().collect(_task(
        "python_lock_wait_profile",
        {
            "off_cpu_wait_json": {
                "collector_type": "off_cpu_wait_profile",
                "top_wait_stacks": [{
                    "stack": ["threading.py:327:wait", "/srv/app/orders.py:41:reserve"],
                    "top_frame": "threading.Condition.wait",
                    "samples": 4,
                    "wait_ms": 1200,
                }],
            }
        },
    ))

    payload = _payload(result)
    assert result.ok is True
    assert payload["evidence_validity"]["evidence_status"] == "valid"
    assert payload["evidence_status"] == "valid"
    assert payload["conclusion_eligible"] is False
    assert "scenario_root_cause_gate" in payload["missing_evidence"]
    assert payload["mechanism_evidence_refs"] == ["python_lock_wait.wait_sites[0]"]
    assert payload["primitive"] == "Condition"
    assert payload["wait_sites"][0]["file"] == "/srv/app/orders.py"
    assert payload["line_candidates"][0]["line"] == 41


def test_unmarked_generic_upstream_path_cannot_be_valid_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    raw = tmp_path / "manual.json"
    raw.write_text('{"stack":["threading.py:327:wait","/srv/app/orders.py:41:reserve"]}', encoding="utf-8")

    result = PythonLockWaitCollector().collect(_task(
        "python_lock_wait_profile",
        {"upstream_paths": [str(raw)]},
    ))

    payload = _payload(result)
    assert result.ok is False
    assert payload["adapter"]["sources"][0]["source_kind"] == "unsupported_manual_source"
    assert payload["evidence_validity"]["evidence_status"] == "blocked"
    assert payload["evidence_validity"]["reason"] == "unsupported_or_manual_source"


def test_exception_profile_requires_real_log_source(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    result = PythonExceptionProfileCollector().collect(_task(
        "python_exception_profile",
        {
            "log_window_json": {
                "adapter": {"kind": "fluent_bit"},
                "error_clusters": [{
                    "count": 3,
                    "sample": 'Traceback\n  File "/srv/app/api.py", line 88, in checkout\nValueError: bad cart 42',
                }],
            }
        },
    ))

    payload = _payload(result)
    assert payload["evidence_validity"]["evidence_status"] == "valid"
    assert payload["exception_clusters"][0]["exception_type"] == "ValueError"
    assert payload["exception_clusters"][0]["occurrence_count"] == 3
    assert payload["line_candidates"][0]["file"] == "/srv/app/api.py"


def test_queue_profile_uses_broker_or_worker_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    result = PythonQueueProfileCollector().collect(_task(
        "python_queue_profile",
        {
            "queue_metrics_json": {
                "source_kind": "celery_inspect",
                "records": [{
                    "queue_system": "celery",
                    "backlog": 27,
                    "state": "active",
                    "task_name": "orders.tasks.checkout",
                    "duration_ms": 2400,
                }],
            }
        },
    ))

    payload = _payload(result)
    assert payload["evidence_validity"]["evidence_status"] == "valid"
    assert payload["queue_system"] == "celery"
    assert payload["backlog"] == 27
    assert payload["slow_task_candidates"][0]["task_name"] == "orders.tasks.checkout"


def test_queue_profile_normalizes_celery_inspect_and_prometheus_shapes(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    result = PythonQueueProfileCollector().collect(_task(
        "python_queue_profile",
        {
            "queue_metrics_json": {
                "source_kind": "prometheus",
                "records": [{"__name__": "celery_queue_messages_ready", "value": 19}],
            },
            "broker_metrics_json": {
                "source_kind": "celery_inspect",
                "active": [{
                    "task_name": "orders.tasks.checkout",
                    "task_id": "task-1",
                    "duration_ms": 1800,
                    "stack": ["/srv/app/tasks.py:17:checkout"],
                }],
                "reserved": [{
                    "task_name": "orders.tasks.reserve",
                    "task_id": "task-2",
                    "file": "/srv/app/tasks.py",
                    "line": 31,
                }],
            },
        },
    ))

    payload = _payload(result)
    assert payload["evidence_validity"]["evidence_status"] == "valid"
    assert payload["backlog"] == 19
    assert payload["active_tasks"][0]["task_name"] == "orders.tasks.checkout"
    assert payload["reserved_tasks"][0]["task_name"] == "orders.tasks.reserve"
    assert {item["line"] for item in payload["line_candidates"]} == {17, 31}


def test_queue_profile_normalizes_application_runtime_eta_backlog(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    runtime_log = tmp_path / "workload.ndjson"
    runtime_log.write_text(
        "\n".join([
            '{"event":"eta_submission_sample","scheduled_count":250,"submitted_eta":500}',
            '{"event":"workload_complete","scheduled_count":5000,"submitted_eta":5000}',
        ]),
        encoding="utf-8",
    )

    result = PythonQueueProfileCollector().collect(_task(
        "python_queue_profile",
        {"application_runtime_log_paths": [str(runtime_log)]},
    ))

    payload = _payload(result)
    assert payload["evidence_validity"]["evidence_status"] == "valid"
    assert payload["adapter"]["sources"][0]["source_kind"] == "application_runtime_log"
    assert payload["queue_system"] == "celery"
    assert payload["backlog"] == 5000
    assert payload["reserved_tasks"][0]["task_name"] == "eta_submission_sample"


def test_inline_queue_shape_without_source_provenance_is_not_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    result = PythonQueueProfileCollector().collect(_task(
        "python_queue_profile",
        {
            "queue_metrics_json": {
                "records": [{
                    "backlog": 27,
                    "state": "active",
                    "task_name": "orders.tasks.checkout",
                    "stack": ["/srv/app/tasks.py:17:checkout"],
                }],
            },
        },
    ))

    payload = _payload(result)
    assert result.ok is False
    assert payload["adapter"]["sources"][0]["source_kind"] == "unsupported_manual_source"
    assert payload["evidence_validity"]["evidence_status"] == "blocked"
    assert payload["evidence_validity"]["reason"] == "unsupported_or_manual_source"


def test_named_upstream_path_without_source_provenance_is_not_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    raw = tmp_path / "queue-metrics.json"
    raw.write_text(
        '{"records":[{"backlog":27,"task_name":"orders.tasks.checkout","stack":["/srv/app/tasks.py:17:checkout"]}]}',
        encoding="utf-8",
    )

    result = PythonQueueProfileCollector().collect(_task(
        "python_queue_profile",
        {"queue_metrics_json_paths": [str(raw)]},
    ))

    payload = _payload(result)
    assert result.ok is False
    assert payload["adapter"]["sources"][0]["source_kind"] == "unsupported_manual_source"
    assert payload["evidence_validity"]["evidence_status"] == "blocked"
    assert payload["evidence_validity"]["reason"] == "unsupported_or_manual_source"


def test_pool_and_retry_profiles_keep_empty_windows_non_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    pool_result = PythonPoolProfileCollector().collect(_task(
        "python_pool_profile",
        {"log_window_json": {"adapter": {"kind": "fluent_bit"}, "error_clusters": [{"sample": "ordinary warning"}]}},
    ))
    retry_result = PythonRetryTimeoutProfileCollector().collect(_task(
        "python_retry_timeout_profile",
        {"trace_endpoint_profile_json": {"collector_type": "trace_endpoint_profile", "spans": [{"name": "GET /ok"}]}},
    ))

    assert _payload(pool_result)["evidence_validity"]["evidence_status"] == "empty_window"
    assert _payload(retry_result)["evidence_validity"]["evidence_status"] == "empty_window"


def test_pool_and_retry_profiles_emit_valid_only_for_matching_upstream(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    pool_result = PythonPoolProfileCollector().collect(_task(
        "python_pool_profile",
        {
            "log_window_json": {
                "adapter": {"kind": "fluent_bit"},
                "error_clusters": [{
                    "sample": "sqlalchemy.exc.TimeoutError: QueuePool limit reached",
                    "stack": ["/srv/app/db.py:12:get_session"],
                }],
            }
        },
    ))
    retry_result = PythonRetryTimeoutProfileCollector().collect(_task(
        "python_retry_timeout_profile",
        {
            "log_window_json": {
                "adapter": {"kind": "fluent_bit"},
                "error_clusters": [{
                    "sample": "retry attempt 3 after ReadTimeout from paymentservice",
                    "dependency": "paymentservice",
                    "attempt_count": 3,
                }],
            }
        },
    ))

    pool_payload = _payload(pool_result)
    retry_payload = _payload(retry_result)
    assert pool_payload["evidence_validity"]["evidence_status"] == "valid"
    assert pool_payload["pool_type"] == "sqlalchemy"
    assert pool_payload["wait_sites"][0]["file"] == "/srv/app/db.py"
    assert retry_payload["evidence_validity"]["evidence_status"] == "valid"
    assert retry_payload["attempt_count"] == 3
    assert retry_payload["dependency_context"]["dependencies"] == ["paymentservice"]


def test_pool_profile_normalizes_pool_counters_and_long_holder(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    result = PythonPoolProfileCollector().collect(_task(
        "python_pool_profile",
        {
            "log_window_json": {
                "source_kind": "application_runtime_log",
                "records": [{
                    "message": "sqlalchemy QueuePool checked out connection not returned",
                    "checked_out": 10,
                    "pool_size": 10,
                    "checkout_duration_ms": 2500,
                    "stack": ["/srv/app/db.py:44:load_order"],
                }],
            },
        },
    ))

    payload = _payload(result)
    assert payload["evidence_validity"]["evidence_status"] == "valid"
    assert payload["pool_exhausted"] is True
    assert payload["checked_out"] == 10
    assert payload["pool_size"] == 10
    assert payload["release_evidence"] == "missing"
    assert payload["long_holder_candidates"][0]["hold_ms"] == 2500
    assert payload["line_candidates"][0]["line"] == 44


def test_retry_profile_normalizes_otel_retry_attributes(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    result = PythonRetryTimeoutProfileCollector().collect(_task(
        "python_retry_timeout_profile",
        {
            "trace_endpoint_profile_json": {
                "source_kind": "otel_trace",
                "spans": [
                    {
                        "name": "HTTP GET payment attempt 2 timeout",
                        "attributes": {
                            "retry.attempt": 2,
                            "peer.service": "paymentservice",
                        },
                        "stack": ["/srv/app/client.py:22:fetch"],
                    },
                    {
                        "name": "HTTP GET payment attempt 3 timeout with backoff",
                        "attributes": {
                            "retry.attempt": 3,
                            "peer.service": "paymentservice",
                        },
                        "stack": ["/srv/app/client.py:22:fetch"],
                    },
                ],
            },
        },
    ))

    payload = _payload(result)
    assert payload["evidence_validity"]["evidence_status"] == "valid"
    assert payload["attempt_count"] == 5
    assert payload["local_amplification"] is True
    assert payload["dependency_context"]["dependencies"] == ["paymentservice"]
    assert payload["line_candidates"][0]["file"] == "/srv/app/client.py"


def test_pool_and_retry_profiles_normalize_application_runtime_error_events(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    pool_log = tmp_path / "pool.ndjson"
    pool_log.write_text(
        '{"event":"pool_request_error","error_type":"EmptyPoolError","elapsed_ms":250.3}\n',
        encoding="utf-8",
    )
    retry_log = tmp_path / "retry.ndjson"
    retry_log.write_text(
        "\n".join([
            '{"event":"retry_configuration","retry_kwargs":"Retry(total=5)"}',
            '{"event":"retry_request_error","error_type":"MaxRetryError","errors":1,"elapsed_ms":6011}',
            '{"event":"retry_request_error","error_type":"MaxRetryError","errors":2,"elapsed_ms":6009}',
        ]),
        encoding="utf-8",
    )

    pool_result = PythonPoolProfileCollector().collect(_task(
        "python_pool_profile",
        {"application_runtime_log_paths": [str(pool_log)]},
    ))
    retry_result = PythonRetryTimeoutProfileCollector().collect(_task(
        "python_retry_timeout_profile",
        {"application_runtime_log_paths": [str(retry_log)]},
    ))

    pool_payload = _payload(pool_result)
    retry_payload = _payload(retry_result)
    assert pool_payload["evidence_validity"]["evidence_status"] == "valid"
    assert pool_payload["pool_type"] == "urllib3"
    assert pool_payload["pool_exhausted"] is True
    assert retry_payload["evidence_validity"]["evidence_status"] == "valid"
    assert retry_payload["attempt_count"] >= 3
    assert retry_payload["local_amplification"] is True


def test_retry_runtime_events_are_not_input_slow_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    retry_log = tmp_path / "retry.ndjson"
    retry_log.write_text(
        "\n".join([
            '{"event":"retry_configuration","retry_kwargs":"Retry(total=5)"}',
            '{"event":"retry_request_error","error_type":"MaxRetryError","errors":1,"elapsed_ms":6011}',
            '{"event":"retry_request_error","error_type":"MaxRetryError","errors":2,"elapsed_ms":6009}',
        ]),
        encoding="utf-8",
    )

    retry_result = PythonRetryTimeoutProfileCollector().collect(_task(
        "python_retry_timeout_profile",
        {"application_runtime_log_paths": [str(retry_log)]},
    ))
    input_result = PythonInputProfileCollector().collect(_task(
        "python_input_profile",
        {"application_runtime_log_paths": [str(retry_log)]},
    ))

    retry_payload = _payload(retry_result)
    input_payload = _payload(input_result)
    assert retry_payload["evidence_validity"]["evidence_status"] == "valid"
    assert retry_payload["attempt_count"] >= 3
    assert retry_payload["local_amplification"] is True
    assert input_payload["evidence_validity"]["evidence_status"] == "empty_window"
    assert input_payload["slow_path_detected"] is False


def test_cache_and_input_profiles_normalize_application_runtime_events(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_OUTPUT_BASE", str(tmp_path))
    cache_log = tmp_path / "cache.ndjson"
    cache_log.write_text(
        '{"event":"cache_batch","cache_bytes":165067504,"cache_files":71801,"count":71800,"from_cache":false}\n',
        encoding="utf-8",
    )
    input_log = tmp_path / "input.ndjson"
    input_log.write_text(
        '{"event":"groupby_transform_sample","rows":50000,"categories":5000,"elapsed_ms":5.5}\n',
        encoding="utf-8",
    )

    cache_result = PythonCacheProfileCollector().collect(_task(
        "python_cache_profile",
        {"application_runtime_log_paths": [str(cache_log)]},
    ))
    input_result = PythonInputProfileCollector().collect(_task(
        "python_input_profile",
        {"application_runtime_log_paths": [str(input_log)]},
    ))

    cache_payload = _payload(cache_result)
    input_payload = _payload(input_result)
    assert cache_payload["evidence_validity"]["evidence_status"] == "valid"
    assert cache_payload["cache_backend"] == "filesystem"
    assert cache_payload["cache_files"] == 71801
    assert cache_payload["cache_growth"] is True
    assert input_payload["evidence_validity"]["evidence_status"] == "valid"
    assert input_payload["input_operation"] == "groupby"
    assert input_payload["rows"] == 50000
    assert input_payload["categories"] == 5000
