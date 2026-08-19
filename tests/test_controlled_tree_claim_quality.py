from server.app.rca.controlled_tree import classify_primitive, enforce_conclusion_eligibility
from server.app.rca.models import (
    AITreeCandidateNode,
    AITreeLayer,
    AITreeSelfChallenge,
    ControlledAITree,
)


def test_wait_primitives_are_not_eligible_root_functions():
    node = AITreeCandidateNode(
        candidate_id="sleep_wait",
        role="primary",
        claim="clock_nanosleep 导致请求变慢。",
        supported_level="function",
        status="supported",
        claim_type="root_cause",
        causal_status="supported",
        decision="conclude",
        mechanism="thread_wait",
        target="clock_nanosleep+90",
        evidence_refs=["off_cpu_wait_json.top_wait_stacks[0]"],
        self_challenge=AITreeSelfChallenge(
            supporting_evidence_refs=["off_cpu_wait_json.top_wait_stacks[0]"],
        ),
    )
    tree = enforce_conclusion_eligibility(ControlledAITree(
        tree_id="primitive_tree",
        final_supported_level="function",
        layers=[AITreeLayer(layer_id="layer_0", depth=0, primary_causes=[node])],
    ))

    guarded = tree.layers[0].primary_causes[0]
    assert classify_primitive("clock_nanosleep+90") == "wait_primitive"
    assert guarded.claim_type == "observation_only"
    assert guarded.conclusion_eligible is False
    assert tree.final_primary_causes == []


def test_mechanism_claim_with_target_and_evidence_passes_gate():
    node = AITreeCandidateNode(
        candidate_id="redis_timeout",
        role="primary",
        claim="Redis 请求超时导致购物车接口阻塞。",
        supported_level="service",
        status="supported",
        claim_type="root_cause",
        causal_status="supported",
        mechanism="downstream_dependency_timeout",
        target="redis-cart",
        evidence_refs=["redis_check_json.connectivity"],
    )
    tree = enforce_conclusion_eligibility(ControlledAITree(
        tree_id="dependency_tree",
        final_supported_level="service",
        layers=[AITreeLayer(layer_id="layer_0", depth=0, primary_causes=[node])],
    ))

    assert tree.layers[0].primary_causes[0].conclusion_eligible is True
    assert tree.final_primary_causes == ["redis_timeout"]
