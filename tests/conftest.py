import pytest

from server.app.rca.models import AITreeCandidateNode


@pytest.fixture
def canonical_node_factory():
    def build(candidate_id: str, claim: str, **overrides):
        values = {
            "generated_by": "analyzer",
            "claim_origin": "analyzer_diagnostic",
            "claim_transform": "original",
            "claim_status": "active",
            "relation": "root",
            "role": "unknown",
        }
        values.update(overrides)
        return AITreeCandidateNode(candidate_id=candidate_id, claim=claim, **values)

    return build
