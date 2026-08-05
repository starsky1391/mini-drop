"""Standalone evidence-to-attribution experiment pipeline."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from server.app.rca.attribution import analyze_evidence
from server.app.rca.calibrator import calibrate
from server.app.rca.candidates import generate_candidates
from server.app.rca.models import EvidenceInput, FeedbackPrior


def run_evidence_to_attribution(
    payload: dict[str, Any],
    feedback_priors: dict[str, FeedbackPrior] | None = None,
) -> dict[str, Any]:
    """Run the standalone evidence-first analysis pipeline on raw structured data."""
    evidence = EvidenceInput(**payload)
    candidates = generate_candidates(evidence, feedback_priors)
    analysis_result = analyze_evidence(evidence, candidates)
    evidence_with_analysis = evidence.model_copy(update={
        "analysis_result": analysis_result.model_dump(),
    })
    calibrated = calibrate(candidates, evidence_with_analysis, feedback_priors)
    allowed_cause_ids = set(analysis_result.allowed_cause_ids)
    selected = [
        asdict(item)
        for item in calibrated
        if item.candidate_id in allowed_cause_ids
    ]

    return {
        "pipeline": "evidence_to_attribution",
        "input": evidence.model_dump(),
        "analysis_result": analysis_result.model_dump(),
        "selected_causes": selected,
        "candidate_count": len(candidates),
    }
