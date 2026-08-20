"""新增深挖采集器行为测试。"""

from unittest import mock

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.baseline import BaselineWindowCollector
from agent.mini_drop_agent.collectors.off_cpu import (
    OffCPUCollector,
    _build_bpftrace_script,
    _correlate_wait_evidence,
    _parse_bpftrace_output,
)
from agent.mini_drop_agent.collectors.trace import (
    TraceEndpointCollector,
    _build_profile,
    _flatten_trace_payload,
    _normalize_trace_record,
)


def _task(collector_type: str) -> CollectorTask:
    return CollectorTask(
        id=f"{collector_type}_test",
        collector_type=collector_type,
        target_pid=1234,
        sample_rate=11,
        duration_sec=15,
        options={},
    )


def test_off_cpu_collector_requires_industrial_bpftrace(tmp_path):
    collector = OffCPUCollector()
    collector.OUTPUT_BASE = str(tmp_path)
    with mock.patch("agent.mini_drop_agent.collectors.off_cpu.shutil.which", return_value=None):
        result = collector.collect(_task("off_cpu_wait_profile"))

    assert result.ok is False
    assert "bpftrace_not_installed" in result.reason
    assert result.artifacts[0]["artifact_type"] == "off_cpu_wait_json"
    assert result.artifacts[0]["metadata"]["data"]["collector_status"] == "blocked"
    assert result.artifacts[0]["metadata"]["data"]["capability_check"]["missing_tools"] == ["bpftrace"]


def test_off_cpu_prefers_industrial_profile_spool(tmp_path):
    spool = tmp_path / "offcpu.jsonl"
    spool.write_text(
        '{"pid":1234,"tid":1234,"wait_ms":12,"state":1,"user_stack":["pthread_mutex_lock","cartservice.GetCart"],"cause_kind":"futex_or_lock"}\n',
        encoding="utf-8",
    )
    collector = OffCPUCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="off_cpu_spool",
        collector_type="off_cpu_wait_profile",
        target_pid=1234,
        sample_rate=11,
        duration_sec=15,
        options={"offcpu_profile_paths": [str(spool)]},
    )

    result = collector.collect(task)

    payload = result.artifacts[0]["metadata"]["data"]
    assert result.ok is True
    assert payload["strategy"]["adapter_kind"] == "industrial_profile_spool"
    assert payload["strategy"]["fallback_used"] is False
    assert payload["collector_status"] == "completed"
    assert payload["top_wait_stacks"][0]["top_frame"] == "pthread_mutex_lock"


def test_off_cpu_bpftrace_json_events_parse_to_wait_stacks():
    parsed = _parse_bpftrace_output(
        '{"tid":1234,"wait_ns":12000000,"state":1,"stack":["pthread_mutex_lock","service.handle"]}\n'
        '{"tid":1234,"wait_ns":8000000,"state":1,"stack":["pthread_mutex_lock","service.handle"]}\n'
    )

    assert parsed["parser_status"] == "ok"
    assert parsed["top_wait_stacks"][0]["top_frame"] == "pthread_mutex_lock"
    assert parsed["top_wait_stacks"][0]["wait_reason"] == "interruptible_sleep_or_lock_wait"
    assert parsed["top_wait_stacks"][0]["samples"] == 2
    assert parsed["thread_wait_summary"][0]["tid"] == 1234


def test_off_cpu_event_without_stack_is_partial_not_empty():
    parsed = _parse_bpftrace_output(
        '{"event":"offcpu","pid":1234,"tid":1234,"cpu":2,"start_ns":10,"end_ns":20,"wait_ns":12000000,"state":2,"stack":[]}\n'
    )

    assert parsed["observed_wait_events"] == 1
    assert parsed["parser_status"] == "events_without_stack"
    assert parsed["stackless_event_count"] == 1
    assert parsed["cause_counts"]["kernel_or_block_io"] == 1
    assert parsed["sample_events"][0]["cpu"] == 2
    assert parsed["sample_events"][0]["start_ns"] == 10


def test_off_cpu_script_uses_sched_wakeup_pid_not_prev_pid():
    script = _build_bpftrace_script([1234], {}, capability_check={"tracepoints": {}})

    wakeup_block = script.split("tracepoint:sched:sched_wakeup", 1)[1]
    assert "args->pid == 1234" in wakeup_block
    assert "args->prev_pid == 1234" not in wakeup_block


def test_off_cpu_payload_distinguishes_target_exit(tmp_path):
    collector = OffCPUCollector()
    task = _task("off_cpu_wait_profile")
    parsed = _parse_bpftrace_output("")
    with mock.patch("agent.mini_drop_agent.collectors.off_cpu.os.path.isdir", return_value=False):
        payload = collector._payload_from_parsed(
            task,
            {"window_start": 1, "window_end": 2},
            [1234],
            parsed,
            0,
            "",
            {"status": "available"},
        )

    assert payload["collector_status"] == "target_exit"


def test_off_cpu_wait_evidence_correlates_to_trace_endpoint_and_call_path():
    task = CollectorTask(
        id="off_cpu_correlated",
        collector_type="off_cpu_wait_profile",
        target_pid=1234,
        sample_rate=11,
        duration_sec=15,
        options={
            "window_start": 1720000000,
            "window_end": 1720000010,
            "target_config": {
                "pid": 1234,
                "service_id": "cartservice",
                "instance_id": "cart-1",
            },
        },
    )
    correlation = _correlate_wait_evidence(
        task,
        {
            "top_wait_stacks": [{
                "top_frame": "cartservice.GetCart",
                "samples": 12,
                "percent": 80.0,
            }],
        },
        {
            "status": "completed",
            "records": [{
                "trace_id": "trace-1",
                "span_id": "span-1",
                "service_id": "cartservice",
                "instance_id": "cart-1",
                "pid": "1234",
                "endpoint": "CartService/GetCart",
                "start": 1720000001.0,
                "end": 1720000005.0,
                "duration_ms": 4000.0,
                "call_path": ["gateway", "cartservice"],
            }],
        },
    )

    assert correlation["status"] == "confirmed"
    assert correlation["endpoint"] == "CartService/GetCart"
    assert correlation["call_path"] == ["gateway", "cartservice"]
    assert correlation["call_path_hotspots"][0]["function"] == "cartservice.GetCart"


def test_trace_endpoint_collector_keeps_stack_context_defaults():
    collector = TraceEndpointCollector()
    with mock.patch.object(
        collector._perf,
        "collect",
        return_value=CollectorResult(ok=True, reason="perf ok", artifacts=[{"artifact_type": "raw"}]),
    ) as perf_collect:
        result = collector.collect(_task("trace_endpoint_profile"))

    assert result.ok is True
    assert "Trace endpoint 复合采集完成" in result.reason
    assert any(item["artifact_type"] == "trace_endpoint_profile_json" for item in result.artifacts)
    assert perf_collect.call_args.args[0].options["callgraph"] == "fp"
    assert perf_collect.call_args.args[0].options["event"] == "cpu-cycles:u"


def test_trace_endpoint_collector_preserves_structured_blocked_profile(tmp_path):
    collector = TraceEndpointCollector()
    collector.OUTPUT_BASE = str(tmp_path)
    with mock.patch.object(
        collector._perf,
        "collect",
        return_value=CollectorResult(
            ok=False,
            reason="perf_event_paranoid 权限不足",
            artifacts=[],
        ),
    ):
        result = collector.collect(_task("trace_endpoint_profile_blocked"))

    assert result.ok is False
    profile = next(
        item["metadata"]["data"]
        for item in result.artifacts
        if item["artifact_type"] == "trace_endpoint_profile_json"
    )
    assert profile["correlation_status"]["status"] == "blocked"
    assert profile["correlation_status"]["max_supported_level"] == "process"
    assert profile["correlation_status"]["blocked_details"]["repair_action"]
    assert "capability_check" in profile


def test_trace_profile_correlates_stack_to_endpoint_and_call_path(tmp_path):
    trace_path = tmp_path / "otel.ndjson"
    trace_path.write_text(
        "\n".join([
            '{"traceId":"trace-1","spanId":"root","serviceName":"gateway","name":"GET /cart","startTime":1720000000,"endTime":1720000010}',
            '{"traceId":"trace-1","spanId":"child","parentSpanId":"root","serviceName":"cartservice","name":"CartService/GetCart","pid":1234,"startTime":1720000001,"endTime":1720000005}',
        ]),
        encoding="utf-8",
    )
    task = CollectorTask(
        id="trace_correlated",
        collector_type="trace_endpoint_profile",
        target_pid=1234,
        sample_rate=11,
        duration_sec=15,
        options={
            "window_start": 1720000000,
            "window_end": 1720000010,
            "target_config": {
                "trace_paths": [str(trace_path)],
                "service_id": "cartservice",
                "instance_id": "cart-1",
                "pid": 1234,
            },
        },
    )
    profile = _build_profile(
        task,
        task.options["target_config"],
        {
            "source": "perf",
            "status": "completed",
            "blocked_reason": "",
            "blocked_details": {},
            "artifacts": [],
            "top_functions": [{"name": "cartservice.GetCart", "samples": 12, "percent": 80.0}],
            "depth": {},
        },
        {
            "kind": "otel_ndjson",
            "status": "completed",
            "records": [
                {
                    "trace_id": "trace-1",
                    "span_id": "root",
                    "parent_span_id": "",
                    "service_id": "gateway",
                    "instance_id": "",
                    "pid": "",
                    "endpoint": "GET /cart",
                    "peer_service": "",
                    "start": 1720000000.0,
                    "end": 1720000010.0,
                    "duration_ms": 10000.0,
                    "call_path": [],
                },
                {
                    "trace_id": "trace-1",
                    "span_id": "child",
                    "parent_span_id": "root",
                    "service_id": "cartservice",
                    "instance_id": "cart-1",
                    "pid": "1234",
                    "endpoint": "CartService/GetCart",
                    "peer_service": "",
                    "start": 1720000001.0,
                    "end": 1720000005.0,
                    "duration_ms": 4000.0,
                    "call_path": [],
                },
            ],
            "blocked_reason": "",
        },
    )

    assert profile["correlation_status"]["status"] == "completed"
    assert profile["correlation_status"]["max_supported_level"] == "call_path"
    assert profile["endpoint_bindings"][0]["endpoint"] == "CartService/GetCart"
    assert profile["call_path_hotspots"][0]["call_path"] == ["gateway", "cartservice"]
    assert profile["call_path_hotspots"][0]["correlation_method"] == "pid_instance_time_overlap"


def test_trace_profile_marks_existing_empty_directory_as_empty_window(tmp_path):
    trace_path = tmp_path / "traces"
    trace_path.mkdir()
    base_task = _task("trace_empty_window")
    task = CollectorTask(
        id=base_task.id,
        collector_type=base_task.collector_type,
        target_pid=base_task.target_pid,
        sample_rate=base_task.sample_rate,
        duration_sec=base_task.duration_sec,
        options={
            "target_config": {"trace_paths": [str(trace_path)]},
            "window_start": 1720000000,
            "window_end": 1720000010,
        },
    )

    from agent.mini_drop_agent.collectors.trace import _load_trace_source

    source = _load_trace_source(task, task.options["target_config"])

    assert source["status"] == "empty_window"
    assert source["existing_sources"] == [str(trace_path)]
    assert source["records_in_window"] == 0


def test_otel_resource_spans_keep_service_and_instance_context():
    payload = {
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "cartservice"}},
                    {"key": "service.instance.id", "value": {"stringValue": "cartservice-worker2"}},
                    {"key": "process.pid", "value": {"intValue": "1234"}},
                ],
            },
            "scopeSpans": [{
                "spans": [{
                    "traceId": "trace-1",
                    "spanId": "span-1",
                    "name": "CartService/GetCart",
                    "startTime": 1720000000,
                    "endTime": 1720000005,
                }],
            }],
        }],
    }

    flattened = _flatten_trace_payload(payload)
    normalized = _normalize_trace_record(flattened[0])

    assert normalized["service_id"] == "cartservice"
    assert normalized["instance_id"] == "cartservice-worker2"
    assert normalized["pid"] == "1234"
    assert normalized["endpoint"] == "CartService/GetCart"


def test_trace_profile_service_overlap_does_not_confirm_call_path():
    task = _task("trace_endpoint_profile_service_only")
    task = CollectorTask(
        id=task.id,
        collector_type=task.collector_type,
        target_pid=task.target_pid,
        sample_rate=task.sample_rate,
        duration_sec=task.duration_sec,
        options={"window_start": 1720000000, "window_end": 1720000010},
    )
    profile = _build_profile(
        task,
        {"service_id": "cartservice"},
        {
            "source": "perf",
            "status": "completed",
            "blocked_reason": "",
            "blocked_details": {},
            "artifacts": [],
            "top_functions": [{"name": "cartservice.GetCart", "samples": 8, "percent": 60.0}],
            "depth": {},
        },
        {
            "kind": "otel_ndjson",
            "status": "completed",
            "records": [{
                "trace_id": "trace-2",
                "span_id": "span-2",
                "parent_span_id": "",
                "service_id": "cartservice",
                "instance_id": "cart-2",
                "pid": "",
                "endpoint": "CartService/GetCart",
                "peer_service": "redis",
                "start": 1720000001.0,
                "end": 1720000005.0,
                "duration_ms": 4000.0,
                "call_path": ["gateway", "cartservice", "redis"],
            }],
            "blocked_reason": "",
        },
    )

    assert profile["correlation_status"]["status"] == "partial"
    assert profile["correlation_status"]["max_supported_level"] == "endpoint"
    assert profile["call_path_hotspots"][0]["call_path"] == []
    assert profile["call_path_hotspots"][0]["correlation_method"] == "service_time_overlap"


def test_baseline_window_collector_uses_continuous_sampling_defaults():
    collector = BaselineWindowCollector()
    with mock.patch.object(
        collector._continuous,
        "collect",
        return_value=CollectorResult(ok=True, reason="continuous ok", artifacts=[{"artifact_type": "continuous_summary"}]),
    ) as continuous_collect:
        result = collector.collect(_task("baseline_window_profile"))

    assert result.ok is True
    assert result.reason == "baseline 窗口采集完成"
    assert collector._continuous.WINDOW_DURATION_SEC == 5
    assert collector._continuous.WINDOW_INTERVAL_SEC == 15
    assert collector._continuous.WINDOW_SAMPLE_RATE == 11
    continuous_collect.assert_called_once()
