from server.app.diagnosis.canonical_claim_lineage import (
    apply_ai_claim_update,
    boundary_metadata,
    canonical_claim_fields,
    hash_claim,
    inherit_claim,
    normalize_claim,
)


def test_claim_normalization_is_stable_for_case_spacing_and_punctuation():
    assert normalize_claim("  CPU  Pressure。 ") == "cpu pressure"
    assert hash_claim("CPU Pressure!") == hash_claim("cpu pressure")


def test_ai_update_without_claim_preserves_existing_lineage():
    existing = {
        "candidate_id": "parent",
        "claim": "Analyzer claim",
        **canonical_claim_fields(
            "Analyzer claim",
            generated_by="analyzer",
            claim_origin="analyzer_diagnostic",
        ),
    }
    updated = apply_ai_claim_update(existing, {"status": "partial"})
    assert updated["claim"] == existing["claim"]
    assert updated["claim_origin"] == "analyzer_diagnostic"
    assert updated["claim_hash"] == existing["claim_hash"]


def test_ai_update_with_claim_creates_refined_lineage():
    existing = {"candidate_id": "parent", "claim": "Process pressure"}
    updated = apply_ai_claim_update(existing, {"claim": "Worker process CPU pressure"})
    assert updated["generated_by"] == "ai"
    assert updated["claim_origin"] == "ai_update"
    assert updated["claim_transform"] == "refined"
    assert updated["source_claim_hash"] == hash_claim(existing["claim"])


def test_inherited_and_boundary_metadata_have_distinct_semantics():
    inherited = inherit_claim({"candidate_id": "parent", "claim": "Retained claim"})
    boundary = boundary_metadata("probe failed")
    assert inherited["claim_status"] == "inherited"
    assert inherited["source_candidate_id"] == "parent"
    assert boundary == {
        "boundary_message": "probe failed",
        "claim_transform": "boundary",
        "claim_status": "boundary",
    }
