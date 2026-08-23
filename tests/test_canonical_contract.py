import pytest
from pydantic import ValidationError

from server.app.diagnosis.canonical_claim_lineage import canonical_claim_fields
from server.app.rca.models import AITreeCandidateNode


def test_canonical_node_serializes_claim_lineage(canonical_node_factory):
    node = canonical_node_factory(
        "candidate",
        "Concrete mechanism",
        relation="alternative",
        **canonical_claim_fields(
            "Concrete mechanism",
            generated_by="ai",
            claim_origin="ai_proposal",
        ),
    )
    payload = node.model_dump(mode="json")
    assert payload["claim_origin"] == "ai_proposal"
    assert payload["claim_transform"] == "original"
    assert payload["claim_status"] == "active"
    assert payload["claim_hash"]


def test_origin_parent_must_be_listed_as_a_parent():
    with pytest.raises(ValidationError, match="origin_parent_candidate_id"):
        AITreeCandidateNode(
            candidate_id="child",
            role="unknown",
            claim="child",
            parent_candidate_ids=["other"],
            origin_parent_candidate_id="parent",
        )
