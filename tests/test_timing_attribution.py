from __future__ import annotations

import json

from server.app.rca.attribution import analyze_evidence
from server.app.rca.llm_client import diagnose
from server.app.rca.models import CandidateCause, EvidenceInput


def _io_candidate() -> CandidateCause:
    return CandidateCause(
        candidate_id="io_wait_contention",
        description="IO wait contention",
        evidence_refs=["sys_metrics.summary.avg_cpu_iowait_pct", "ebpf_metrics.io_latency_us"],
        rule_score=0.8,
    )


def test_delayed_followup_non_reproduction_does_not_refute_same_window_evidence():
    same_window = {
        "trigger_event_id": "evt_001",
        "evidence_cohort_id": "cohort_001",
        "collection_mode": "triggered_group",
        "timing_relation": "same_window",
    }
    delayed_window = {
        "trigger_event_id": "evt_001",
        "evidence_cohort_id": "cohort_001",
        "collection_mode": "delayed_followup",
        "timing_relation": "delayed_followup",
    }
    evidence = EvidenceInput(
        evidence_index={
            "evidence_window": same_window,
            "timed_facts": [
                {
                    "fact_id": "fact_iowait_high",
                    "source": "sys_metrics",
                    "evidence_ref": "same_window.sys_metrics.summary.avg_cpu_iowait_pct",
                    "value": 18.0,
                    "status": "observed",
                    "evidence_window": same_window,
                },
                {
                    "fact_id": "fact_io_latency_present",
                    "source": "ebpf_metrics",
                    "evidence_ref": "same_window.ebpf_metrics.io_latency_us",
                    "value": 42.0,
                    "status": "observed",
                    "evidence_window": same_window,
                },
                {
                    "fact_id": "fact_iowait_low",
                    "source": "sys_metrics",
                    "evidence_ref": "delayed_followup.sys_metrics.summary.avg_cpu_iowait_pct",
                    "value": 1.0,
                    "status": "normal",
                    "evidence_window": delayed_window,
                },
            ],
        }
    )

    result = analyze_evidence(evidence, [_io_candidate()])
    attribution = result.attributions[0]

    assert attribution.status == "supported"
    assert attribution.opposing_fact_ids == []
    assert result.conclusion_boundary.timing_relation == "same_window"
    assert result.conclusion_boundary.delayed_followup_reproduction_status == "not_reproduced"
    assert result.conclusion_boundary.non_refutable_evidence_boundaries
    assert "不能直接反证同窗证据" in result.conclusion_boundary.reason


def test_same_window_opposing_evidence_creates_conflict_branch():
    same_window = {
        "trigger_event_id": "evt_001",
        "evidence_cohort_id": "cohort_001",
        "collection_mode": "triggered_group",
        "timing_relation": "same_window",
    }
    evidence = EvidenceInput(
        evidence_index={
            "evidence_window": same_window,
            "timed_facts": [
                {
                    "fact_id": "fact_iowait_high",
                    "source": "sys_metrics",
                    "evidence_ref": "same_window.sys_metrics.summary.avg_cpu_iowait_pct",
                    "value": 18.0,
                    "status": "observed",
                    "evidence_window": same_window,
                },
                {
                    "fact_id": "fact_io_latency_present",
                    "source": "ebpf_metrics",
                    "evidence_ref": "same_window.ebpf_metrics.io_latency_us",
                    "value": 42.0,
                    "status": "observed",
                    "evidence_window": same_window,
                },
                {
                    "fact_id": "fact_iowait_low",
                    "source": "sys_metrics",
                    "evidence_ref": "same_window.second_collector.avg_cpu_iowait_pct",
                    "value": 1.0,
                    "status": "normal",
                    "evidence_window": same_window,
                },
            ],
        }
    )

    result = analyze_evidence(evidence, [_io_candidate()])
    conflict = next(item for item in result.ai_tree if item.node_id == "tree_conflict")

    assert result.attributions[0].opposing_fact_ids == ["fact_iowait_low"]
    assert conflict.conflict_type == "same_window_collector_conflict"
    assert conflict.decision == "downgrade"
    assert conflict.leaf_status == "conservative_leaf"


def test_report_output_carries_timing_boundary_fields(monkeypatch):
    monkeypatch.setenv("MINI_DROP_AI_ENABLED", "none")
    same_window = {
        "trigger_event_id": "evt_001",
        "evidence_cohort_id": "cohort_001",
        "collection_mode": "triggered_group",
        "timing_relation": "same_window",
    }
    delayed_window = {
        "trigger_event_id": "evt_001",
        "evidence_cohort_id": "cohort_001",
        "collection_mode": "delayed_followup",
        "timing_relation": "delayed_followup",
    }
    evidence = EvidenceInput(
        evidence_index={
            "evidence_window": same_window,
            "timed_facts": [
                {
                    "fact_id": "fact_iowait_high",
                    "source": "sys_metrics",
                    "evidence_ref": "same_window.sys_metrics.summary.avg_cpu_iowait_pct",
                    "value": 18.0,
                    "status": "observed",
                    "evidence_window": same_window,
                },
                {
                    "fact_id": "fact_io_latency_present",
                    "source": "ebpf_metrics",
                    "evidence_ref": "same_window.ebpf_metrics.io_latency_us",
                    "value": 42.0,
                    "status": "observed",
                    "evidence_window": same_window,
                },
                {
                    "fact_id": "fact_iowait_low",
                    "source": "sys_metrics",
                    "evidence_ref": "delayed_followup.sys_metrics.summary.avg_cpu_iowait_pct",
                    "value": 1.0,
                    "status": "normal",
                    "evidence_window": delayed_window,
                },
            ],
        }
    )
    analysis = analyze_evidence(evidence, [_io_candidate()])
    evidence = evidence.model_copy(update={"analysis_result": analysis.model_dump()})

    report = diagnose(
        "task_timing",
        evidence,
        json.dumps([
            {
                "candidate_id": "io_wait_contention",
                "description": "IO wait contention",
                "final_confidence": 0.73,
                "evidence_refs": ["same_window.sys_metrics.summary.avg_cpu_iowait_pct"],
            }
        ]),
    ).report

    assert report.conclusion_boundary is not None
    assert report.conclusion_boundary.timing_relation == "same_window"
    assert report.conclusion_boundary.delayed_followup_reproduction_status == "not_reproduced"
    assert report.conclusion_boundary.non_refutable_evidence_boundaries
