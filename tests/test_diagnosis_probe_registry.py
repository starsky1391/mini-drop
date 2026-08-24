"""诊断探针注册表测试。"""

from server.app.diagnosis.probe_registry import build_probe_manifest, choose_probe_ids, get_probe, list_probes


def test_probe_registry_exposes_depth_probes():
    probes = {probe.probe_id: probe for probe in list_probes()}

    assert build_probe_manifest()["available_probes"][0]["evidence_family"]
    assert any(
        item["evidence_family"] == "host_process_metrics"
        for item in build_probe_manifest()["available_probes"]
    )
    assert "process_off_cpu_profile" in probes
    assert "process_trace_endpoint_profile" in probes
    assert "process_baseline_window" in probes
    assert "process_log_scan" in probes
    assert "process_dependency_check" in probes
    assert "process_redis_check" in probes
    assert "process_python_runtime_profile" in probes
    assert "process_python_heap_profile" in probes
    assert "process_go_heap_profile" in probes
    assert "process_source_snapshot" in probes
    assert "process_source_mechanism_query" in probes
    assert "process_python_heap_reference" in probes
    assert "process_python_lock_wait_profile" in probes
    assert "process_python_exception_profile" in probes
    assert "process_python_queue_profile" in probes
    assert "process_python_pool_profile" in probes
    assert "process_python_retry_timeout_profile" in probes
    assert "process_python_cache_profile" in probes
    assert "process_python_input_profile" in probes
    assert get_probe("process_off_cpu_profile").runner_task_kind == "off_cpu_wait_profile"
    assert get_probe("process_trace_endpoint_profile").runner_task_kind == "trace_endpoint_profile"
    assert get_probe("process_baseline_window").runner_task_kind == "baseline_window_profile"
    assert get_probe("process_log_scan").runner_task_kind == "log_scan"
    assert get_probe("process_dependency_check").runner_task_kind == "dependency_check"
    assert get_probe("process_redis_check").runner_task_kind == "redis_check"
    assert get_probe("process_python_runtime_profile").runner_task_kind == "pyspy"
    assert get_probe("process_python_heap_profile").runner_task_kind == "python_heap_profile"
    assert get_probe("process_go_heap_profile").runner_task_kind == "go_pprof"
    assert get_probe("process_source_snapshot").runner_task_kind == "source_snapshot"
    assert get_probe("process_source_mechanism_query").runner_task_kind == "source_mechanism_query"
    assert get_probe("process_python_heap_reference").runner_task_kind == "python_heap_reference"
    assert get_probe("process_python_lock_wait_profile").runner_task_kind == "python_lock_wait_profile"
    assert get_probe("process_python_exception_profile").runner_task_kind == "python_exception_profile"
    assert get_probe("process_python_queue_profile").runner_task_kind == "python_queue_profile"
    assert get_probe("process_python_pool_profile").runner_task_kind == "python_pool_profile"
    assert get_probe("process_python_retry_timeout_profile").runner_task_kind == "python_retry_timeout_profile"
    assert get_probe("process_python_cache_profile").runner_task_kind == "python_cache_profile"
    assert get_probe("process_python_input_profile").runner_task_kind == "python_input_profile"

    initial = {probe for symptom in ("cpu_saturation", "latency_increase", "memory_pressure") for probe in choose_probe_ids(symptom)}
    assert "process_source_mechanism_query" not in initial
    assert "process_python_heap_reference" not in initial


def test_choose_probe_ids_prefers_deeper_collection_for_cpu_and_latency():
    assert choose_probe_ids("cpu_saturation")[:4] == [
        "host_process_metrics",
        "process_cpu_profile",
        "process_off_cpu_profile",
        "process_trace_endpoint_profile",
    ]
    assert choose_probe_ids("latency_increase")[:3] == [
        "host_process_metrics",
        "process_dependency_check",
        "process_log_scan",
    ]
    assert "process_off_cpu_profile" in choose_probe_ids("io_degradation")
    assert "process_baseline_window" in choose_probe_ids("memory_pressure")
    assert choose_probe_ids("runtime_contention")[:3] == [
        "host_process_metrics",
        "process_log_scan",
        "process_python_lock_wait_profile",
    ]
    assert "process_python_exception_profile" in choose_probe_ids("exception_storm")
    assert "process_python_queue_profile" in choose_probe_ids("queue_backlog")
    assert "process_python_pool_profile" in choose_probe_ids("pool_exhaustion")
    assert "process_python_retry_timeout_profile" in choose_probe_ids("retry_timeout")
    assert "process_python_cache_profile" in choose_probe_ids("cache_growth")
    assert "process_python_input_profile" in choose_probe_ids("input_slow_path")
    assert "process_source_snapshot" in choose_probe_ids("queue_backlog")
    assert "process_source_snapshot" in choose_probe_ids("retry_timeout")


def test_legacy_hypotheses_are_exposed_only_as_distinguishing_hints():
    probe = get_probe("process_python_input_profile")
    assert probe.may_help_distinguish == probe.applicable_hypotheses
    manifest_item = next(
        item for item in build_probe_manifest()["available_probes"]
        if item["probe_id"] == "process_python_input_profile"
    )
    assert manifest_item["may_help_distinguish"] == probe.may_help_distinguish
    assert manifest_item["cannot_establish"]
