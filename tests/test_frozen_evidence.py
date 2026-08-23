from server.app.diagnosis.audit_bundle import _structured_evidence
from server.app.diagnosis.evidence_structurer import (
    StructuredEvidence,
    rca_inputs_from_structured,
)
from server.app.rca.attribution import analyze_evidence
from server.app.rca.models import CandidateCause, EvidenceInput


def _structured(**updates) -> StructuredEvidence:
    value = {
        "task_id": "watch:incident:snapshot",
        "evidence_cohort_id": "cohort-1",
        "collection_mode": "rolling_snapshot",
        "timing_relation": "same_window",
        "sys_metrics": {"summary": {"avg_cpu_user_pct": 92.0}},
        "evidence_index": {
            "evidence_window": {
                "evidence_cohort_id": "cohort-1",
                "timing_relation": "same_window",
            },
        },
    }
    value.update(updates)
    return StructuredEvidence.model_validate(value)


def test_frozen_package_hit_reuses_structured_family_and_window():
    structured = _structured()
    inputs = rca_inputs_from_structured(structured)

    assert inputs["sys_metrics"]["summary"]["avg_cpu_user_pct"] == 92.0
    assert inputs["evidence_index"]["evidence_window"] == {
        "evidence_cohort_id": "cohort-1",
        "timing_relation": "same_window",
    }

    result = analyze_evidence(
        EvidenceInput(**inputs),
        [CandidateCause(
            candidate_id="cpu_pressure",
            description="目标进程存在 CPU 压力",
            evidence_refs=["sys_metrics.summary"],
        )],
    )

    assert any(fact.evidence_ref == "sys_metrics.summary" for fact in result.facts)
    assert result.conclusion_boundary.timing_relation == "same_window"


def test_frozen_package_miss_stays_bounded_without_promoting_dependency_cause():
    result = analyze_evidence(
        EvidenceInput(**rca_inputs_from_structured(_structured())),
        [CandidateCause(
            candidate_id="downstream_dependency",
            description="下游依赖不可达",
            evidence_refs=["evidence_index.dependency_check"],
        )],
    )

    assert result.allowed_cause_ids == []
    assert result.conclusion_boundary.can_claim_root_cause is False
    assert any("dependency_check" in item for item in result.missing_evidence)


def test_partial_or_invalid_package_family_remains_a_gap():
    structured = _structured(
        confidence_inputs={
            "evidence_validity_by_family": {
                "dependency_check": "partial",
            },
        },
        evidence_index={
            "evidence_window": {
                "evidence_cohort_id": "cohort-1",
                "timing_relation": "same_window",
            },
            "dependency_check": {
                "evidence_validity": {
                    "evidence_status": "unparseable",
                },
            },
        },
    )
    result = analyze_evidence(
        EvidenceInput(**rca_inputs_from_structured(structured)),
        [CandidateCause(
            candidate_id="downstream_dependency",
            description="下游依赖不可达",
            evidence_refs=["evidence_index.dependency_check"],
        )],
    )

    assert structured.confidence_inputs["evidence_validity_by_family"]["dependency_check"] == "partial"
    assert result.allowed_cause_ids == []
    assert result.conclusion_boundary.can_claim_root_cause is False


def test_stale_package_window_does_not_upgrade_supported_candidate():
    structured = _structured(
        timing_relation="stale_window",
        evidence_index={
            "evidence_window": {
                "evidence_cohort_id": "cohort-1",
                "timing_relation": "stale_window",
            },
        },
    )

    result = analyze_evidence(
        EvidenceInput(**rca_inputs_from_structured(structured)),
        [CandidateCause(
            candidate_id="cpu_pressure",
            description="目标进程存在 CPU 压力",
            evidence_refs=["sys_metrics.summary"],
        )],
    )

    assert result.conclusion_boundary.timing_relation == "stale_window"
    assert result.allowed_cause_ids == []


def test_audit_structured_evidence_deduplicates_duplicate_package_records():
    duplicate = {
        "query_or_probe": "structured_evidence_json",
        "observed_value": {
            "summary": {
                "artifact_refs": [{
                    "evidence_ref": "package:cohort-1:sys_metrics",
                    "artifact_type": "sys_metrics",
                }],
            },
        },
    }

    merged = _structured_evidence([duplicate, duplicate])

    assert len(merged["artifact_refs"]) == 1
    assert merged["artifact_refs"][0]["evidence_ref"] == "package:cohort-1:sys_metrics"
