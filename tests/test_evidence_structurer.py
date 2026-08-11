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
