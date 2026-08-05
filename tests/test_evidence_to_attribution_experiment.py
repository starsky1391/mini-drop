"""Tests for the standalone evidence-to-attribution experiment pipeline."""

from __future__ import annotations

from experiments.evidence_to_attribution.pipeline import run_evidence_to_attribution


def test_pipeline_returns_structured_analysis():
    result = run_evidence_to_attribution(
        {
            "top_functions": [{"name": "fib_hotspot", "percent": 68.5}],
            "sys_metrics": {
                "summary": {
                    "avg_cpu_user_pct": 91.0,
                    "avg_cpu_iowait_pct": 1.0,
                }
            },
        }
    )

    assert result["pipeline"] == "evidence_to_attribution"
    assert result["analysis_result"]["allowed_cause_ids"] == ["cpu_hotspot_recursive"]
    assert result["analysis_result"]["conclusion_boundary"]["can_claim_root_cause"] is True
    assert result["selected_causes"]


def test_pipeline_stays_conservative_without_hotspot():
    result = run_evidence_to_attribution(
        {
            "sys_metrics": {
                "summary": {
                    "avg_cpu_user_pct": 91.0,
                }
            },
        }
    )

    assert result["analysis_result"]["allowed_cause_ids"] == []
    assert result["analysis_result"]["conclusion_boundary"]["can_claim_root_cause"] is False
    assert result["selected_causes"] == []
