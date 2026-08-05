"""诊断探针注册表测试。"""

from server.app.diagnosis.probe_registry import choose_probe_ids, get_probe, list_probes


def test_probe_registry_exposes_depth_probes():
    probes = {probe.probe_id: probe for probe in list_probes()}

    assert "process_off_cpu_profile" in probes
    assert "process_trace_endpoint_profile" in probes
    assert "process_baseline_window" in probes
    assert get_probe("process_off_cpu_profile").runner_task_kind == "off_cpu_wait_profile"
    assert get_probe("process_trace_endpoint_profile").runner_task_kind == "trace_endpoint_profile"
    assert get_probe("process_baseline_window").runner_task_kind == "baseline_window_profile"


def test_choose_probe_ids_prefers_deeper_collection_for_cpu_and_latency():
    assert choose_probe_ids("cpu_saturation")[:4] == [
        "host_process_metrics",
        "process_cpu_profile",
        "process_off_cpu_profile",
        "process_trace_endpoint_profile",
    ]
    assert choose_probe_ids("latency_increase")[:3] == [
        "host_process_metrics",
        "process_trace_endpoint_profile",
        "process_cpu_profile",
    ]
    assert "process_off_cpu_profile" in choose_probe_ids("io_degradation")
    assert "process_baseline_window" in choose_probe_ids("memory_pressure")

