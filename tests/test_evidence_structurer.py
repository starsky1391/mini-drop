"""Tests for non-AI artifact evidence structuring."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from server.app.diagnosis.evidence_structurer import rca_inputs_from_structured, structure_artifact_evidence
from server.app.rca.attribution import analyze_evidence
from server.app.rca.evidence import collect_evidence, evidence_to_json
from server.app.rca.models import CandidateCause, EvidenceInput
from server.app.rca.report import run_diagnosis_context


class _Task:
    id = "task_structured"
    agent_id = "agent-1"
    collector_type = "pyspy"
    target_pid = 1234
    duration_sec = 10
    sample_rate = 99
    status = "DONE"
    status_reason = ""


def _candidate(candidate_id: str, refs: list[str]) -> CandidateCause:
    return CandidateCause(
        candidate_id=candidate_id,
        description=candidate_id,
        evidence_refs=refs,
        rule_score=0.8,
    )


def test_structured_evidence_is_deterministic_for_same_artifacts():
    artifacts = [
        {"artifact_type": "flamegraph_svg", "filename": "pyspy.svg", "local_path": "/tmp/mini-drop/t/pyspy.svg", "size_bytes": 4096},
        {"artifact_type": "top_json", "filename": "top.json", "local_path": "/tmp/mini-drop/t/top.json", "size_bytes": 128},
    ]
    values = {
        "top_json": [
            {"name": "worker", "samples": 20, "percent": 20.0},
            {"name": "busy_cpu", "samples": 70, "percent": 70.0},
        ],
    }

    first = structure_artifact_evidence(task_id="t", artifacts=artifacts, artifact_values=values)
    second = structure_artifact_evidence(task_id="t", artifacts=list(reversed(artifacts)), artifact_values=values)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.top_functions[0]["name"] == "busy_cpu"
    assert first.top_functions[0]["evidence_ref"] == "structured_evidence.top_functions[0]"
    assert first.confidence_inputs["token_safety"] == "compact_summary_only"


def test_pyspy_sample_quality_is_preserved_in_structured_evidence():
    structured = structure_artifact_evidence(
        task_id="pyspy_quality",
        artifacts=[{"artifact_type": "python_stack_samples_json", "filename": "stack_samples.json"}],
        artifact_values={
            "python_stack_samples_json": {
                "sample_quality": {
                    "diagnostic_value": "low",
                    "dominant_state": "blocked_io",
                    "non_idle_ratio": 0.12,
                    "primitive_frame_ratio": 0.88,
                    "framework_loop_ratio": 0.73,
                    "target_code_ratio": 0.05,
                    "sample_count": 50,
                    "stable_across_samples": False,
                    "reason": "样本主要集中在 poll/select，缺少业务执行栈。",
                }
            },
            "top_json": [{"name": "poll", "samples": 50, "percent": 100.0}],
        },
    )

    assert structured.stack_summary["sample_quality"]["diagnostic_value"] == "low"
    assert structured.confidence_inputs["runtime_profile_quality"] == "low"
    assert structured.confidence_inputs["runtime_profile_target_code_ratio"] == 0.05
    assert structured.evidence_index["runtime_profile_quality"]["dominant_state"] == "blocked_io"


def test_runtime_stack_business_frame_becomes_cpu_line_candidate():
    structured = structure_artifact_evidence(
        task_id="runtime_business_frame",
        artifacts=[
            {"artifact_type": "python_stack_samples_json", "filename": "stack_samples.json"},
        ],
        artifact_values={
            "top_json": [{"name": "_PyEval_EvalFrameDefault", "samples": 8, "percent": 80.0}],
            "python_stack_samples_json": {
                "sample_quality": {
                    "diagnostic_value": "medium",
                    "primitive_frame_ratio": 0.0,
                    "framework_loop_ratio": 0.0,
                    "target_code_ratio": 0.9,
                    "sample_count": 8,
                },
                "stack_samples": [{
                    "hot_frame": "_PyEval_EvalFrameDefault",
                    "sample_count": 8,
                    "percent": 80.0,
                    "stack": [
                        "/usr/local/lib/python3.11/runpy.py:198:_run_module_as_main",
                        "/srv/app/orders.py:42:calculate_total",
                        "/usr/local/lib/python3.11/site-packages/framework/router.py:21:dispatch",
                    ],
                }],
            },
        },
    )

    gate = structured.confidence_inputs["python_scenario_gates"]["python_cpu_hotspot"]

    assert gate["gate_checks"]["runtime_line_candidate"] is True
    assert gate["line_candidates"][0]["file"] == "/srv/app/orders.py"
    assert gate["line_candidates"][0]["line"] == 42
    assert gate["conclusion_eligible"] is False


def test_evidence_window_metadata_distinguishes_same_window_from_followup():
    same_window = structure_artifact_evidence(
        task_id="triggered_task",
        artifacts=[{"artifact_type": "top_json", "filename": "top.json"}],
        artifact_values={"top_json": [{"name": "busy_cpu", "samples": 70, "percent": 70.0}]},
        evidence_window={
            "trigger_event_id": "evt_001",
            "evidence_cohort_id": "cohort_001",
            "collection_mode": "triggered_group",
            "window_start": "2026-08-11T10:14:48Z",
            "window_end": "2026-08-11T10:15:18Z",
            "trigger_observed_at": "2026-08-11T10:15:03Z",
            "timing_relation": "same_window",
        },
    )
    delayed = structure_artifact_evidence(
        task_id="followup_task",
        artifacts=[{"artifact_type": "top_json", "filename": "top.json"}],
        artifact_values={"top_json": [{"name": "idle", "samples": 10, "percent": 10.0}]},
        evidence_window={
            "trigger_event_id": "evt_001",
            "evidence_cohort_id": "cohort_001",
            "collection_mode": "delayed_followup",
            "window_start": "2026-08-11T10:18:00Z",
            "window_end": "2026-08-11T10:18:15Z",
            "trigger_observed_at": "2026-08-11T10:15:03Z",
            "timing_relation": "delayed_followup",
        },
    )

    assert same_window.collection_mode == "triggered_group"
    assert same_window.timing_relation == "same_window"
    assert same_window.top_functions[0]["evidence_window"]["timing_relation"] == "same_window"
    assert same_window.evidence_index["evidence_window"]["evidence_cohort_id"] == "cohort_001"
    assert delayed.collection_mode == "delayed_followup"
    assert delayed.artifact_refs[0]["evidence_window"]["timing_relation"] == "delayed_followup"


def test_evidence_window_metadata_rejects_unknown_modes():
    with pytest.raises(ValidationError):
        structure_artifact_evidence(
            task_id="bad_window",
            artifacts=[],
            artifact_values={},
            evidence_window={"collection_mode": "root_cause_guess", "timing_relation": "same_window"},
        )


def test_svg_only_artifact_stays_reference_only_in_llm_input():
    structured = structure_artifact_evidence(
        task_id="pyspy_svg_only",
        artifacts=[{
            "artifact_type": "flamegraph_svg",
            "filename": "pyspy.svg",
            "local_path": "/tmp/mini-drop/pyspy_svg_only/pyspy.svg",
            "content_type": "image/svg+xml",
            "size_bytes": 1024 * 1024,
        }],
        artifact_values={},
    )
    inputs = rca_inputs_from_structured(structured)
    evidence = collect_evidence(
        task_id="pyspy_svg_only",
        task_record=_Task(),
        top_functions=inputs["top_functions"],
        evidence_index=inputs["evidence_index"],
    )

    payload = json.loads(evidence_to_json(evidence))

    assert structured.top_functions == []
    assert payload["evidence_index"]["artifact_refs"][0]["artifact_type"] == "flamegraph_svg"
    assert payload["evidence_index"]["confidence_inputs"]["parse_status"] == "insufficient_structured_signal"
    assert "<svg" not in json.dumps(payload, ensure_ascii=False)


def test_flamegraph_json_can_produce_compact_top_functions_when_top_json_missing():
    structured = structure_artifact_evidence(
        task_id="flame_task",
        artifacts=[{"artifact_type": "flamegraph_json", "filename": "flamegraph.json"}],
        artifact_values={
            "flamegraph_json": {
                "name": "root",
                "value": 100,
                "children": [
                    {"name": "gateway", "value": 100, "children": [{"name": "busy_cpu", "value": 72}]},
                    {"name": "idle", "value": 10},
                ],
            }
        },
    )

    assert structured.top_functions[0]["name"] == "gateway"
    assert structured.top_functions[0]["percent"] == 100.0
    assert structured.top_functions[1]["name"] == "busy_cpu"
    assert structured.stack_summary["dominant_hot_frame"] == "gateway"


def test_top_functions_reject_invalid_anchors_samples_and_percentages():
    structured = structure_artifact_evidence(
        task_id="invalid-profile",
        artifacts=[],
        artifact_values={
            "top_json": [
                {"name": "[unknown]", "samples": 10, "percent": 10},
                {"name": "0x7ffee", "samples": 10, "percent": 10},
                {"name": "negative", "samples": -1, "percent": 5},
                {"name": "overflow", "samples": 20, "percent": 1345},
                {
                    "name": "Rule.compile",
                    "file": "werkzeug/routing.py",
                    "line": 768,
                    "samples": 21,
                    "percent": 70,
                    "call_path": ["Map.__init__", "Rule.bind", "Rule.compile"],
                },
            ],
        },
    )

    assert len(structured.top_functions) == 1
    assert structured.top_functions[0]["name"] == "Rule.compile"
    assert structured.top_functions[0]["file"] == "werkzeug/routing.py"
    assert structured.top_functions[0]["line"] == 768
    assert structured.top_functions[0]["call_path"][-1] == "Rule.compile"


def test_flamegraph_svg_titles_can_be_structured_when_json_is_missing():
    structured = structure_artifact_evidence(
        task_id="svg_task",
        artifacts=[{"artifact_type": "flamegraph_svg", "filename": "pyspy.svg"}],
        artifact_values={
            "flamegraph_svg": """
                <svg>
                  <g><title>busy_cpu (138 samples, 72.4%)</title></g>
                  <g><title>idle (52 samples, 27.6%)</title></g>
                </svg>
            """,
        },
    )

    assert structured.top_functions[0]["name"] == "busy_cpu"
    assert structured.top_functions[0]["samples"] == 138
    assert structured.top_functions[0]["percent"] == 72.4


def test_call_path_hotspots_feed_rca_graph_without_raw_stack_payload():
    structured = structure_artifact_evidence(
        task_id="depth_task",
        artifacts=[
            {"artifact_type": "top_json", "filename": "top.json"},
            {"artifact_type": "depth_evidence_json", "filename": "depth_evidence.json"},
        ],
        artifact_values={
            "top_json": [{"name": "busy_cpu", "samples": 138, "percent": 72.4}],
            "depth_evidence_json": {
                "stack_samples": [{
                    "hot_frame": "busy_cpu",
                    "call_path": "gateway;order;busy_cpu",
                    "stack_fragment": ["gateway", "order", "busy_cpu"],
                    "sample_count": 138,
                    "percent": 72.4,
                    "context_id": "ctx-1",
                }],
                "context": {
                    "call_path": "gateway;order;busy_cpu",
                    "endpoint": "/api/order/create",
                    "service_id": "order-service",
                    "instance_id": "order-1",
                    "context_id": "ctx-1",
                },
            },
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 93.0, "avg_cpu_iowait_pct": 1.0}},
        },
    )
    inputs = rca_inputs_from_structured(structured)
    evidence = EvidenceInput(
        top_functions=inputs["top_functions"],
        sys_metrics=inputs["sys_metrics"],
        evidence_index=inputs["evidence_index"],
    )

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert structured.call_path_hotspots[0]["call_path"] == ["gateway", "order", "busy_cpu"]
    assert result.conclusion_boundary.max_supported_level == "call_path"
    assert any(item.relation == "owns_hotspot" for item in result.graph_links)
    assert any(item.entity_type == "endpoint" for item in result.graph_entities)


def test_industrial_adapters_become_structured_evidence_families():
    structured = structure_artifact_evidence(
        task_id="industrial_adapter_task",
        artifacts=[
            {"artifact_type": "log_window_json", "filename": "log_window.json"},
            {"artifact_type": "dependency_check_json", "filename": "dependency_check.json"},
            {"artifact_type": "redis_check_json", "filename": "redis_check.json"},
        ],
        artifact_values={
            "log_window_json": {
                "evidence_window": {
                    "trigger_event_id": "evt_1",
                    "evidence_cohort_id": "cohort_1",
                    "collection_mode": "triggered_group",
                    "start": 1720000001,
                    "end": 1720000002,
                    "timing_relation": "same_window",
                },
                "summary": {"matched_lines": 2, "error_cluster_count": 1},
                "error_clusters": [{"cluster_id": "log_cluster_1", "count": 2}],
                "evidence_index": {"dependencies": ["redis"]},
            },
            "dependency_check_json": {
                "summary": {"failed_dependencies": ["redis"]},
                "checks": [{"dependency_id": "redis", "success": False}],
            },
            "redis_check_json": {
                "connectivity": {"ping_ok": False, "error_type": "ConnectionRefusedError"},
                "slowlog_summary": {"entry_count": 2},
                "latency_summary": {"max_latency_ms": 30},
            },
        },
    )

    assert structured.confidence_inputs["has_log_signal"] is True
    assert structured.confidence_inputs["has_dependency_signal"] is True
    assert structured.confidence_inputs["has_redis_signal"] is True
    assert structured.confidence_inputs["failed_dependency_count"] == 1
    assert structured.confidence_inputs["log_error_cluster_count"] == 1
    assert structured.confidence_inputs["redis_slowlog_entry_count"] == 2
    assert structured.confidence_inputs["redis_max_latency_ms"] == 30
    assert structured.evidence_cohort_id == "cohort_1"
    assert structured.timing_relation == "same_window"
    assert structured.confidence_inputs["collector_families"] == [
        "dependency_check",
        "log_scan",
        "redis_check",
    ]
    assert structured.evidence_index["log_scan"]["summary"]["matched_lines"] == 2
    assert structured.evidence_index["dependency_check"]["checks"][0]["dependency_id"] == "redis"
    assert structured.evidence_index["redis_check"]["connectivity"]["ping_ok"] is False


def test_off_cpu_wait_json_becomes_structured_wait_evidence():
    structured = structure_artifact_evidence(
        task_id="off_cpu_task",
        artifacts=[{"artifact_type": "off_cpu_wait_json", "filename": "off_cpu_wait.json"}],
        artifact_values={
            "off_cpu_wait_json": {
                "summary": {
                    "sample_count": 2,
                    "blocked_thread_count": 1,
                    "total_wait_ms": 20.0,
                    "top_wait_reason": "interruptible_sleep_or_lock_wait",
                    "has_wait_reason": True,
                },
                "top_wait_stacks": [{
                    "wait_reason": "interruptible_sleep_or_lock_wait",
                    "stack": ["pthread_mutex_lock", "service.handle"],
                    "top_frame": "pthread_mutex_lock",
                    "samples": 2,
                    "wait_ms": 20.0,
                    "percent": 100.0,
                }],
                "thread_wait_summary": [{"tid": 1234, "wait_ms": 20.0}],
                "syscall_wait_summary": {"futex_or_lock": 2},
            }
        },
    )

    assert structured.top_functions[0]["name"] == "pthread_mutex_lock"
    assert structured.top_functions[0]["source"] == "off_cpu_wait"
    assert structured.stack_summary["has_wait_reason"] is True
    assert structured.stack_summary["top_wait_reason"] == "interruptible_sleep_or_lock_wait"
    assert structured.confidence_inputs["has_wait_or_io_signal"] is True
    assert "off_cpu_wait_profile" in structured.confidence_inputs["collector_families"]
    assert structured.evidence_index["off_cpu_wait"]["top_wait_stacks"][0]["top_frame"] == "pthread_mutex_lock"


def test_go_heap_profile_json_becomes_structured_hotspot_evidence():
    structured = structure_artifact_evidence(
        task_id="go_heap_task",
        artifacts=[{"artifact_type": "go_heap_profile_json", "filename": "go_heap_profile.json"}],
        artifact_values={
            "go_heap_profile_json": {
                "producer": "go pprof",
                "profile_kind": "heap",
                "heap_mode": "gc=1",
                "sample_type": "inuse_space",
                "hotspots": [{
                    "function": "cache.go",
                    "file": "/app/cache/cache.go",
                    "line": 42,
                    "flat_bytes": 8 * 1024 * 1024,
                    "cum_bytes": 10 * 1024 * 1024,
                    "flat_percent": 66.67,
                    "cum_percent": 83.33,
                }],
                "line_candidates": [{
                    "file": "/app/cache/cache.go",
                    "line": 42,
                    "symbol": "cache.go",
                }],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    assert structured.go_heap_profile_json is not None
    assert structured.top_functions[0]["name"] == "cache.go"
    assert structured.top_functions[0]["file"] == "/app/cache/cache.go"
    assert "go_heap_profile" in structured.confidence_inputs["collector_families"]
    assert structured.confidence_inputs["evidence_validity_by_family"]["go_heap_profile"] == "valid"
    assert structured.evidence_index["go_heap_profile"]["line_candidates"][0]["line"] == 42


def test_python_scenario_profiles_are_preserved_as_structured_evidence():
    structured = structure_artifact_evidence(
        task_id="python_scenario_task",
        artifacts=[
            {"artifact_type": "python_lock_wait_profile_json", "filename": "lock.json"},
            {"artifact_type": "python_exception_profile_json", "filename": "exception.json"},
            {"artifact_type": "python_queue_profile_json", "filename": "queue.json"},
            {"artifact_type": "python_cache_profile_json", "filename": "cache.json"},
            {"artifact_type": "python_input_profile_json", "filename": "input.json"},
        ],
        artifact_values={
            "python_lock_wait_profile_json": {
                "collector_family": "python_lock_wait_profile",
                "scenario_type": "python_lock_wait",
                "adapter": {"source_policy": "industrial_collectors_only", "source_count": 1},
                "wait_sites": [{"function": "reserve", "file": "/srv/app/orders.py", "line": 41}],
                "evidence_status": "valid",
                "missing_evidence": ["source_snapshot_verification", "scenario_root_cause_gate"],
                "conclusion_eligible": False,
                "evidence_validity": {"evidence_status": "valid"},
            },
            "python_exception_profile_json": {
                "collector_family": "python_exception_profile",
                "scenario_type": "python_exception_storm",
                "exception_clusters": [{"exception_type": "ValueError", "occurrence_count": 3}],
                "evidence_validity": {"evidence_status": "valid"},
            },
            "python_queue_profile_json": {
                "collector_family": "python_queue_profile",
                "scenario_type": "python_queue_backlog",
                "backlog": 27,
                "active_tasks": [{"task_name": "orders.tasks.checkout"}],
                "evidence_validity": {"evidence_status": "valid"},
            },
            "python_cache_profile_json": {
                "collector_family": "python_cache_profile",
                "scenario_type": "python_cache_growth",
                "adapter": {"source_policy": "industrial_collectors_only", "sources": [{"source_kind": "application_runtime_log"}]},
                "cache_growth": True,
                "cache_files": 71801,
                "evidence_validity": {"evidence_status": "valid"},
            },
            "python_input_profile_json": {
                "collector_family": "python_input_profile",
                "scenario_type": "python_input_slow_path",
                "adapter": {"source_policy": "industrial_collectors_only", "sources": [{"source_kind": "application_runtime_log"}]},
                "slow_path_detected": True,
                "rows": 50000,
                "categories": 5000,
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    assert structured.python_lock_wait_profile_json is not None
    assert structured.python_exception_profile_json is not None
    assert structured.python_queue_profile_json is not None
    assert structured.python_cache_profile_json is not None
    assert structured.python_input_profile_json is not None
    assert "python_lock_wait_profile" in structured.confidence_inputs["collector_families"]
    assert structured.confidence_inputs["evidence_validity_by_family"]["python_exception_profile"] == "valid"
    assert structured.confidence_inputs["python_scenario_statuses"]["python_queue_profile"] == "valid"
    assert structured.evidence_index["python_lock_wait_profile"]["wait_sites"][0]["line"] == 41
    assert structured.evidence_index["python_lock_wait_profile"]["conclusion_eligible"] is False
    assert "scenario_root_cause_gate" in structured.evidence_index["python_lock_wait_profile"]["missing_evidence"]
    assert structured.evidence_index["python_queue_profile"]["backlog"] == 27
    assert structured.evidence_index["python_cache_profile"]["cache_files"] == 71801
    assert structured.evidence_index["python_input_profile"]["categories"] == 5000
    gate = structured.confidence_inputs["python_scenario_gates"]["python_lock_wait_profile"]
    assert gate["source_policy"] == "industrial_collectors_only"
    assert gate["line_verified"] is False
    assert gate["max_supported_claim_type"] == "observation"
    assert "source_snapshot_verification" in gate["missing_evidence"]
    assert structured.confidence_inputs["python_scenario_gates"]["python_cache_profile"]["conclusion_eligible"] is False
    assert structured.confidence_inputs["python_scenario_gates"]["python_input_profile"]["conclusion_eligible"] is False


def test_python_scenario_line_gate_requires_matching_source_snapshot():
    structured = structure_artifact_evidence(
        task_id="python_exception_with_source",
        artifacts=[
            {"artifact_type": "python_exception_profile_json", "filename": "exception.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
        ],
        artifact_values={
            "python_exception_profile_json": {
                "collector_family": "python_exception_profile",
                "scenario_type": "python_exception_storm",
                "adapter": {
                    "source_policy": "industrial_collectors_only",
                    "sources": [{"kind": "log_window_json", "source_kind": "fluent_bit", "source_status": "loaded", "record_count": 3}],
                },
                "exception_clusters": [{
                    "exception_type": "ValueError",
                    "occurrence_count": 3,
                    "throw_site": {"file": "/srv/app/api.py", "line": 88, "function": "checkout"},
                    "evidence_ref": "python_exception_profile.exception_clusters[0]",
                }],
                "line_candidates": [{"file": "/srv/app/api.py", "line": 88, "function": "checkout"}],
                "evidence_validity": {"evidence_status": "valid"},
            },
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [{"file": "/srv/app/api.py", "focus_line": 88}],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    gate = structured.confidence_inputs["python_scenario_gates"]["python_exception_profile"]
    assert gate["line_verified"] is True
    assert gate["source_context_hash"] == "sha256:source"
    assert gate["max_supported_claim_type"] == "direct_failure_mechanism"
    assert gate["conclusion_eligible"] is False
    assert "scenario_root_cause_gate" in gate["missing_evidence"]


def test_python_exception_scenario_can_become_direct_root_with_impact_and_source():
    structured = structure_artifact_evidence(
        task_id="python_exception_root",
        artifacts=[
            {"artifact_type": "python_exception_profile_json", "filename": "exception.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
        ],
        artifact_values={
            "python_exception_profile_json": {
                "collector_family": "python_exception_profile",
                "scenario_type": "python_exception_storm",
                "adapter": {
                    "source_policy": "industrial_collectors_only",
                    "sources": [{"kind": "log_window_json", "source_kind": "fluent_bit", "source_status": "loaded", "record_count": 9}],
                },
                "exception_clusters": [{
                    "exception_type": "ValueError",
                    "occurrence_count": 9,
                    "throw_site": {"file": "/srv/app/api.py", "line": 88, "function": "checkout"},
                    "evidence_ref": "python_exception_profile.exception_clusters[0]",
                }],
                "line_candidates": [{"file": "/srv/app/api.py", "line": 88, "function": "checkout"}],
                "impact": {"error_rate": 0.42, "request_count": 120},
                "impact_evidence_refs": ["log_scan.error_clusters[0]"],
                "evidence_validity": {"evidence_status": "valid"},
            },
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [{"file": "/srv/app/api.py", "focus_line": 88}],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    gate = structured.confidence_inputs["python_scenario_gates"]["python_exception_profile"]
    assert gate["max_supported_claim_type"] == "direct_failure_mechanism"
    assert gate["conclusion_eligible"] is False
    assert "session_ai_candidate_qualification" in gate["missing_evidence"]


def test_builtin_python_cpu_gate_rejects_runtime_primitives_and_requires_source_snapshot():
    primitive = structure_artifact_evidence(
        task_id="cpu_primitive",
        artifacts=[{"artifact_type": "python_stack_samples_json", "filename": "stack.json"}],
        artifact_values={
            "python_stack_samples_json": {
                "sample_quality": {
                    "diagnostic_value": "low",
                    "primitive_frame_ratio": 0.9,
                    "framework_loop_ratio": 0.1,
                    "target_code_ratio": 0.0,
                },
                "evidence_validity": {"evidence_status": "valid"},
            },
            "top_json": [{"name": "poll", "samples": 50, "percent": 100.0}],
        },
    )

    primitive_gate = primitive.confidence_inputs["python_scenario_gates"]["python_cpu_hotspot"]
    assert primitive_gate["max_supported_claim_type"] == "observation"
    assert primitive_gate["gate_checks"]["runtime_primitive_rejected"] is True
    assert "stable_python_hotspot" in primitive_gate["missing_evidence"]

    with_source = structure_artifact_evidence(
        task_id="cpu_with_source",
        artifacts=[
            {"artifact_type": "python_stack_samples_json", "filename": "stack.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
        ],
        artifact_values={
            "python_stack_samples_json": {"evidence_validity": {"evidence_status": "valid"}},
            "top_json": [{"name": "orders.calculate", "file": "/srv/app/orders.py", "line": 41, "samples": 30, "percent": 72.0}],
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [{"file": "/srv/app/orders.py", "focus_line": 41}],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    source_gate = with_source.evidence_index["python_scenario_gates"]["python_cpu_hotspot"]
    assert source_gate["line_verified"] is True
    assert source_gate["max_supported_claim_type"] == "partial_localization"
    assert source_gate["gate_checks"]["baseline_or_impact"] is False
    assert source_gate["conclusion_eligible"] is False


def test_builtin_python_cpu_gate_can_become_direct_root_when_all_gates_pass():
    structured = structure_artifact_evidence(
        task_id="cpu_direct_root",
        artifacts=[
            {"artifact_type": "python_stack_samples_json", "filename": "stack.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
            {"artifact_type": "continuous_summary", "filename": "baseline.json"},
        ],
        artifact_values={
            "python_stack_samples_json": {"evidence_validity": {"evidence_status": "valid"}},
            "top_json": [{"name": "orders.calculate", "file": "/srv/app/orders.py", "line": 41, "samples": 30, "percent": 72.0}],
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [{"file": "/srv/app/orders.py", "focus_line": 41}],
                "evidence_validity": {"evidence_status": "valid"},
            },
            "continuous_summary": {
                "summary": {"baseline_shift": "cpu_regression"},
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    gate = structured.confidence_inputs["python_scenario_gates"]["python_cpu_hotspot"]
    assert gate["max_supported_claim_type"] == "direct_failure_mechanism"
    assert gate["conclusion_eligible"] is False
    assert "session_ai_candidate_qualification" in gate["missing_evidence"]


def test_builtin_python_endpoint_gate_keeps_dependency_counter_evidence_as_blocker():
    structured = structure_artifact_evidence(
        task_id="endpoint_latency",
        artifacts=[
            {"artifact_type": "trace_endpoint_profile_json", "filename": "trace.json"},
            {"artifact_type": "dependency_check_json", "filename": "dependency.json"},
        ],
        artifact_values={
            "trace_endpoint_profile_json": {
                "call_path_hotspots": [{
                    "function": "checkout",
                    "file": "/srv/app/api.py",
                    "line": 88,
                    "samples": 12,
                    "endpoint": "/checkout",
                    "call_path": ["api.checkout"],
                    "evidence_ref": "trace_endpoint_profile.call_path_hotspots[0]",
                }],
                "correlation_status": {"status": "completed", "max_supported_level": "call_path"},
                "trace_source": {"status": "completed"},
                "evidence_validity": {"evidence_status": "valid"},
            },
            "dependency_check_json": {
                "summary": {"failed_dependencies": ["redis-cart"]},
                "checks": [{"dependency_id": "redis-cart", "success": False}],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    gate = structured.confidence_inputs["python_scenario_gates"]["python_endpoint_latency"]
    assert gate["gate_checks"]["endpoint_call_path_correlation"] is True
    assert gate["gate_checks"]["counter_evidence_clear"] is False
    assert "dependency_or_broker_counter_evidence" in gate["missing_evidence"]
    assert gate["conclusion_eligible"] is False


def test_builtin_python_io_blocking_gate_uses_off_cpu_blocking_kind_without_root_promotion():
    structured = structure_artifact_evidence(
        task_id="io_blocking",
        artifacts=[
            {"artifact_type": "off_cpu_wait_json", "filename": "offcpu.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
        ],
        artifact_values={
            "off_cpu_wait_json": {
                "summary": {"sample_count": 4, "top_wait_reason": "socket_wait"},
                "top_wait_stacks": [{
                    "stack": ["socket.py:705:readinto", "/srv/app/client.py:22:fetch"],
                    "top_frame": "socket.recv",
                    "samples": 4,
                    "blocking_kind": "http_client",
                    "evidence_ref": "off_cpu_wait.top_wait_stacks[0]",
                }],
                "blocking_summary": {"by_kind": {"http_client": 4}},
                "evidence_validity": {"evidence_status": "valid"},
            },
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [{"file": "/srv/app/client.py", "focus_line": 22}],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    gate = structured.evidence_index["python_scenario_gates"]["python_io_blocking"]
    assert gate["gate_checks"]["blocking_kind_normalized"] is True
    assert gate["gate_checks"]["local_blocking_callsite"] is True
    assert gate["line_verified"] is True
    assert gate["max_supported_claim_type"] == "partial_localization"
    assert "local_io_behavior_evidence" in gate["missing_evidence"]


def test_retry_timeout_scenario_counter_evidence_blocks_direct_root_even_with_amplification():
    structured = structure_artifact_evidence(
        task_id="retry_counter_evidence",
        artifacts=[
            {"artifact_type": "python_retry_timeout_profile_json", "filename": "retry.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
            {"artifact_type": "dependency_check_json", "filename": "dependency.json"},
        ],
        artifact_values={
            "python_retry_timeout_profile_json": {
                "collector_family": "python_retry_timeout_profile",
                "scenario_type": "python_retry_timeout",
                "adapter": {
                    "source_policy": "industrial_collectors_only",
                    "sources": [{"kind": "trace_endpoint_profile_json", "source_kind": "otel_trace", "source_status": "loaded", "record_count": 6}],
                },
                "retry_clusters": [{"file": "/srv/app/client.py", "line": 22, "evidence_ref": "python_retry_timeout_profile.retry_clusters[0]"}],
                "timeout_sites": [{"file": "/srv/app/client.py", "line": 22, "evidence_ref": "python_retry_timeout_profile.retry_clusters[0]"}],
                "line_candidates": [{"file": "/srv/app/client.py", "line": 22, "function": "fetch"}],
                "attempt_count": 6,
                "nested_retry": True,
                "local_amplification": True,
                "impact": {"retry_rate": 0.8, "latency_ms": 1200},
                "evidence_validity": {"evidence_status": "valid"},
            },
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [{"file": "/srv/app/client.py", "focus_line": 22}],
                "evidence_validity": {"evidence_status": "valid"},
            },
            "dependency_check_json": {
                "summary": {"failed_dependencies": ["paymentservice"]},
                "checks": [{"dependency_id": "paymentservice", "success": False}],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    gate = structured.confidence_inputs["python_scenario_gates"]["python_retry_timeout_profile"]
    assert gate["gate_checks"]["retry_amplification"] is True
    assert gate["gate_checks"]["counter_evidence_clear"] is False
    assert gate["max_supported_claim_type"] == "partial_localization"
    assert gate["conclusion_eligible"] is False
    assert "dependency_or_broker_counter_evidence" in gate["missing_evidence"]


def test_lock_queue_pool_and_retry_scenario_gates_can_become_direct_roots():
    structured = structure_artifact_evidence(
        task_id="scenario_direct_roots",
        artifacts=[
            {"artifact_type": "python_lock_wait_profile_json", "filename": "lock.json"},
            {"artifact_type": "python_queue_profile_json", "filename": "queue.json"},
            {"artifact_type": "python_pool_profile_json", "filename": "pool.json"},
            {"artifact_type": "python_retry_timeout_profile_json", "filename": "retry.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
        ],
        artifact_values={
            "python_lock_wait_profile_json": {
                "collector_family": "python_lock_wait_profile",
                "scenario_type": "python_lock_wait",
                "adapter": {"source_policy": "industrial_collectors_only", "sources": [{"source_kind": "py-spy", "source_status": "loaded", "record_count": 8}]},
                "wait_sites": [{"file": "/srv/app/orders.py", "line": 41, "function": "reserve", "evidence_ref": "python_lock_wait.wait_sites[0]"}],
                "holder_candidates": [{"function": "hold_inventory", "evidence_ref": "python_lock_wait.holder_candidates[0]"}],
                "line_candidates": [{"file": "/srv/app/orders.py", "line": 41, "function": "reserve"}],
                "impact": {"blocked_ms": 3200, "request_count": 30},
                "evidence_validity": {"evidence_status": "valid"},
            },
            "python_queue_profile_json": {
                "collector_family": "python_queue_profile",
                "scenario_type": "python_queue_backlog",
                "adapter": {"source_policy": "industrial_collectors_only", "sources": [{"source_kind": "celery_inspect", "source_status": "loaded", "record_count": 4}]},
                "backlog": 42,
                "active_tasks": [{"task_name": "orders.tasks.checkout"}],
                "slow_task_candidates": [{"task_name": "orders.tasks.checkout"}],
                "line_candidates": [{"file": "/srv/app/tasks.py", "line": 17, "function": "checkout"}],
                "evidence_validity": {"evidence_status": "valid"},
            },
            "python_pool_profile_json": {
                "collector_family": "python_pool_profile",
                "scenario_type": "python_pool_exhaustion",
                "adapter": {"source_policy": "industrial_collectors_only", "sources": [{"source_kind": "fluent_bit", "source_status": "loaded", "record_count": 6}]},
                "pool_exhausted": True,
                "wait_sites": [{"file": "/srv/app/db.py", "line": 12, "function": "get_session"}],
                "acquire_sites": [{"file": "/srv/app/db.py", "line": 12, "function": "get_session"}],
                "line_candidates": [{"file": "/srv/app/db.py", "line": 12, "function": "get_session"}],
                "release_evidence": "missing",
                "impact": {"latency_ms": 1500, "request_count": 20},
                "evidence_validity": {"evidence_status": "valid"},
            },
            "python_retry_timeout_profile_json": {
                "collector_family": "python_retry_timeout_profile",
                "scenario_type": "python_retry_timeout",
                "adapter": {"source_policy": "industrial_collectors_only", "sources": [{"source_kind": "otel_trace", "source_status": "loaded", "record_count": 6}]},
                "retry_clusters": [{"file": "/srv/app/client.py", "line": 22, "evidence_ref": "python_retry_timeout_profile.retry_clusters[0]"}],
                "timeout_sites": [{"file": "/srv/app/client.py", "line": 22, "evidence_ref": "python_retry_timeout_profile.retry_clusters[0]"}],
                "line_candidates": [{"file": "/srv/app/client.py", "line": 22, "function": "fetch"}],
                "attempt_count": 6,
                "nested_retry": True,
                "local_amplification": True,
                "impact": {"retry_rate": 0.8, "latency_ms": 1200},
                "evidence_validity": {"evidence_status": "valid"},
            },
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [
                    {"file": "/srv/app/orders.py", "focus_line": 41},
                    {"file": "/srv/app/tasks.py", "focus_line": 17},
                    {"file": "/srv/app/db.py", "focus_line": 12},
                    {"file": "/srv/app/client.py", "focus_line": 22},
                ],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    gates = structured.confidence_inputs["python_scenario_gates"]
    for family in (
        "python_lock_wait_profile",
        "python_queue_profile",
        "python_pool_profile",
        "python_retry_timeout_profile",
    ):
        assert gates[family]["max_supported_claim_type"] == "direct_failure_mechanism"
        assert gates[family]["conclusion_eligible"] is False
        assert "session_ai_candidate_qualification" in gates[family]["missing_evidence"]


def test_python_scenario_gate_rejects_valid_payload_without_industrial_source_provenance():
    structured = structure_artifact_evidence(
        task_id="manual_scenario_payload",
        artifacts=[
            {"artifact_type": "python_queue_profile_json", "filename": "queue.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
        ],
        artifact_values={
            "python_queue_profile_json": {
                "collector_family": "python_queue_profile",
                "scenario_type": "python_queue_backlog",
                "backlog": 42,
                "active_tasks": [{"task_name": "orders.tasks.checkout"}],
                "slow_task_candidates": [{"task_name": "orders.tasks.checkout"}],
                "line_candidates": [{"file": "/srv/app/tasks.py", "line": 17, "function": "checkout"}],
                "evidence_validity": {"evidence_status": "valid"},
            },
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [{"file": "/srv/app/tasks.py", "focus_line": 17}],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )

    gate = structured.confidence_inputs["python_scenario_gates"]["python_queue_profile"]
    assert gate["gate_checks"]["industrial_source_provenance"] is False
    assert gate["gate_checks"]["industrial_observation"] is False
    assert gate["max_supported_claim_type"] == "observation"
    assert gate["conclusion_eligible"] is False
    assert "industrial_source_provenance" in gate["missing_evidence"]


def test_endpoint_and_io_builtin_gates_can_become_direct_roots_with_local_mechanism():
    endpoint = structure_artifact_evidence(
        task_id="endpoint_direct",
        artifacts=[
            {"artifact_type": "trace_endpoint_profile_json", "filename": "trace.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
        ],
        artifact_values={
            "trace_endpoint_profile_json": {
                "call_path_hotspots": [{
                    "function": "checkout",
                    "file": "/srv/app/api.py",
                    "line": 88,
                    "samples": 12,
                    "percent": 64.0,
                    "endpoint": "/checkout",
                    "call_path": ["api.checkout"],
                    "evidence_ref": "trace_endpoint_profile.call_path_hotspots[0]",
                }],
                "correlation_status": {"status": "completed", "max_supported_level": "call_path"},
                "trace_source": {"status": "completed"},
                "latency_summary": {"p95_ms": 1800, "request_count": 100},
                "evidence_validity": {"evidence_status": "valid"},
            },
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [{"file": "/srv/app/api.py", "focus_line": 88}],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )
    endpoint_gate = endpoint.confidence_inputs["python_scenario_gates"]["python_endpoint_latency"]
    assert endpoint_gate["max_supported_claim_type"] == "direct_failure_mechanism"
    assert endpoint_gate["conclusion_eligible"] is False

    io = structure_artifact_evidence(
        task_id="io_direct",
        artifacts=[
            {"artifact_type": "off_cpu_wait_json", "filename": "offcpu.json"},
            {"artifact_type": "source_snapshot_json", "filename": "source.json"},
        ],
        artifact_values={
            "off_cpu_wait_json": {
                "top_wait_stacks": [{
                    "stack": ["socket.py:705:readinto", "/srv/app/client.py:22:fetch"],
                    "blocking_kind": "http_client",
                    "missing_timeout": True,
                    "evidence_ref": "off_cpu_wait.top_wait_stacks[0]",
                }],
                "blocking_summary": {"by_kind": {"http_client": 4}, "missing_timeout": True},
                "evidence_validity": {"evidence_status": "valid"},
            },
            "source_snapshot_json": {
                "revision": "rev-1",
                "source_context_hash": "sha256:source",
                "snippets": [{"file": "/srv/app/client.py", "focus_line": 22}],
                "evidence_validity": {"evidence_status": "valid"},
            },
        },
    )
    io_gate = io.confidence_inputs["python_scenario_gates"]["python_io_blocking"]
    assert io_gate["max_supported_claim_type"] == "direct_failure_mechanism"
    assert io_gate["conclusion_eligible"] is False


def test_off_cpu_compact_evidence_keeps_cause_and_trace_correlation():
    structured = structure_artifact_evidence(
        task_id="off_cpu_correlated",
        artifacts=[{"artifact_type": "off_cpu_wait_json", "filename": "off_cpu_wait.json"}],
        artifact_values={
            "off_cpu_wait_json": {
                "collector_status": "completed",
                "parser_status": "ok",
                "event_summary": {"observed_wait_events": 3},
                "cause_summary": {"top_cause": "futex_or_lock"},
                "stack_quality": {"stack_unwind_status": "complete"},
                "summary": {"sample_count": 3, "top_wait_reason": "interruptible_sleep_or_lock_wait"},
                "top_wait_stacks": [{
                    "top_frame": "pthread_mutex_lock",
                    "samples": 3,
                    "wait_ms": 24.0,
                    "wait_reason": "interruptible_sleep_or_lock_wait",
                }],
                "trace_source": {"status": "completed", "records_in_window": 2},
                "correlation": {
                    "status": "confirmed",
                    "endpoint": "CartService/GetCart",
                    "call_path": ["gateway", "cartservice"],
                    "confidence": 0.86,
                },
                "endpoint_bindings": [{"endpoint": "CartService/GetCart"}],
                "call_path_hotspots": [{"function": "pthread_mutex_lock"}],
                "capability_check": {"missing_tools": []},
            },
        },
    )

    compact = structured.evidence_index["off_cpu_wait"]
    assert compact["cause_summary"]["top_cause"] == "futex_or_lock"
    assert compact["correlation"]["endpoint"] == "CartService/GetCart"
    assert compact["correlation"]["call_path"] == ["gateway", "cartservice"]
    assert compact["event_summary"]["observed_wait_events"] == 3


def test_structured_evidence_is_attached_to_final_report(monkeypatch):
    monkeypatch.setenv("MINI_DROP_AI_ENABLED", "none")
    structured = structure_artifact_evidence(
        task_id="report_task",
        artifacts=[{"artifact_type": "top_json", "filename": "top.json"}],
        artifact_values={
            "top_json": [{"name": "busy_cpu", "samples": 138, "percent": 72.4}],
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 93.0, "avg_cpu_iowait_pct": 1.0}},
        },
    )
    inputs = rca_inputs_from_structured(structured)

    outcome = run_diagnosis_context(
        task_id="report_task",
        task_record=_Task(),
        top_functions=inputs["top_functions"],
        sys_metrics=inputs["sys_metrics"],
        evidence_index=inputs["evidence_index"],
        structured_evidence=structured.model_dump(mode="json"),
        auto_execute_safe=False,
    )

    assert outcome.report.report.structured_evidence is not None
    assert outcome.report.report.structured_evidence["top_functions"][0]["name"] == "busy_cpu"
    assert outcome.report.evidence_snapshot["analysis_result"]["structured_evidence"]["confidence_inputs"]["token_safety"] == "compact_summary_only"
