from agent.mini_drop_agent.collectors.evidence_validity import (
    baseline_evidence_state,
    off_cpu_evidence_state,
    trace_evidence_state,
)


def test_empty_off_cpu_is_not_valid_and_has_no_default_cause():
    state = off_cpu_evidence_state({
        "collector_status": "empty_window",
        "summary": {"sample_count": 0},
        "event_summary": {"observed_wait_events": 0},
    })

    assert state["evidence_status"] == "empty_window"


def test_event_only_off_cpu_is_partial():
    state = off_cpu_evidence_state({
        "collector_status": "partial",
        "summary": {"sample_count": 0},
        "event_summary": {"observed_wait_events": 4},
    })

    assert state["evidence_status"] == "partial"


def test_empty_trace_cannot_support_function_level():
    state = trace_evidence_state({
        "top_functions": [],
        "endpoint_bindings": [],
        "call_path_hotspots": [],
        "correlation_status": {"status": "partial", "max_supported_level": "process"},
    })

    assert state["evidence_status"] == "empty_window"


def test_raw_baseline_without_structured_stack_is_unparseable():
    state = baseline_evidence_state({"raw_window_count": 2, "structured_window_count": 0})

    assert state["evidence_status"] == "unparseable"
