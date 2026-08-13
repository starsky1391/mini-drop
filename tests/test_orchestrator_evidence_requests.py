from server.app.diagnosis.orchestrator import _filter_pending_evidence_requests


def test_dependency_success_does_not_remove_function_depth_requests():
    observations = [{
        "collector_type": "dependency_check",
        "top_function": {},
        "confidence_inputs": {},
    }]
    requests = _filter_pending_evidence_requests(
        "diag-1",
        ["dependency_check", "cpu_profile", "off_cpu_wait_profile", "trace_endpoint_profile"],
        [
            {"parameters": {"evidence_gap": "dependency_check"}, "status": "COMPLETED"},
            {"parameters": {"evidence_gap": "cpu_profile"}, "status": "COMPLETED"},
        ],
        observations,
    )

    assert "dependency_check" not in requests
    assert "cpu_profile" in requests
    assert "off_cpu_wait_profile" in requests
    assert "trace_endpoint_profile" in requests


def test_non_empty_function_profile_can_close_cpu_depth_request():
    observations = [{
        "collector_type": "perf_cpu",
        "top_function": {"name": "service.handle"},
        "confidence_inputs": {},
    }]
    requests = _filter_pending_evidence_requests(
        "diag-1",
        ["cpu_profile", "trace_endpoint_profile"],
        [{"parameters": {"evidence_gap": "cpu_profile"}, "status": "COMPLETED"}],
        observations,
    )

    assert "cpu_profile" not in requests
    assert "trace_endpoint_profile" in requests
