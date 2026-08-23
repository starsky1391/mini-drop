from server.app.diagnosis.canonical_candidate_state import (
    reduce_candidate_state,
    validate_claim_refinement,
)
from server.app.rca.models import AITreeCandidateNode, AITreeLayer, ControlledAITree


def _node(candidate_id, claim, *, parent=None, mechanism="", target="", level="process", refs=None):
    return AITreeCandidateNode(
        candidate_id=candidate_id,
        generated_by="ai" if parent else "analyzer",
        claim_origin="ai_proposal" if parent else "analyzer_diagnostic",
        claim_transform="refined" if parent else "original",
        parent_candidate_ids=[parent] if parent else [],
        origin_parent_candidate_id=parent,
        relation="refinement" if parent else "root",
        role="unknown",
        claim=claim,
        supported_level=level,
        mechanism=mechanism,
        target=target,
        evidence_refs=refs or [],
    )


def test_specificity_rejects_exact_and_punctuation_only_duplicates():
    parent = _node("parent", "Worker CPU pressure")
    assert validate_claim_refinement(parent, _node("exact", "Worker CPU pressure", parent="parent")).code == "duplicate_claim"
    assert validate_claim_refinement(parent, _node("punctuation", "worker cpu pressure。", parent="parent")).code == "duplicate_claim"


def test_specificity_rejects_evidence_only_change():
    parent = _node("parent", "Worker pressure")
    child = _node("child", "Worker pressure remains likely", parent="parent", refs=["evidence[1]"])
    assert validate_claim_refinement(parent, child).code == "refinement_not_more_specific"


def test_specificity_accepts_mechanism_target_or_localization_refinement():
    parent = _node("parent", "Worker pressure")
    mechanism = _node("mechanism", "GIL contention in worker", parent="parent", mechanism="gil_contention")
    target = _node("target", "Pool worker-7 is saturated", parent="parent", target="worker-7")
    localized = _node("localized", "Worker call path is saturated", parent="parent", level="call_path")
    assert validate_claim_refinement(parent, mechanism).valid
    assert validate_claim_refinement(parent, target).valid
    assert validate_claim_refinement(parent, localized).valid


def test_reducer_excludes_duplicate_child_and_merges_its_evidence_into_parent():
    parent = _node("parent", "Worker CPU pressure", refs=["evidence[0]"])
    child = _node("child", "worker cpu pressure。", parent="parent", refs=["evidence[1]"])
    tree = ControlledAITree(
        tree_id="tree",
        layers=[
            AITreeLayer(layer_id="base", depth=0, generated_by="analyzer_observation", unknown_causes=[parent]),
            AITreeLayer(layer_id="child", depth=1, generated_by="ai_candidate", unknown_causes=[child]),
        ],
        final_unknown_causes=["parent", "child"],
    )
    reduced = reduce_candidate_state(tree)
    nodes = [node for layer in reduced.layers for node in layer.unknown_causes]
    assert [node.candidate_id for node in nodes] == ["parent"]
    assert nodes[0].evidence_refs == ["evidence[0]", "evidence[1]"]
    assert reduced.final_unknown_causes == ["parent"]
    assert reduced.data_quality["records"][-1]["status"] == "duplicate_claim"


def test_reducer_excludes_same_claim_child_even_when_legacy_relation_is_alternative():
    parent = _node("parent", "Worker CPU pressure")
    child = _node("child", "worker cpu pressure。", parent="parent")
    child = child.model_copy(update={"relation": "alternative"})
    tree = ControlledAITree(
        tree_id="tree",
        layers=[
            AITreeLayer(layer_id="base", depth=0, unknown_causes=[parent]),
            AITreeLayer(layer_id="child", depth=1, unknown_causes=[child]),
        ],
    )

    reduced = reduce_candidate_state(tree)

    assert reduced.layers[1].unknown_causes == []
    assert reduced.data_quality["records"][-1]["status"] == "duplicate_claim"


def test_reducer_excludes_history_from_session_main():
    restored = _node("history", "Old claim")
    restored = restored.model_copy(update={
        "generated_by": "history",
        "claim_origin": "history_restore",
        "claim_transform": "restored",
    })
    tree = ControlledAITree(
        tree_id="tree",
        layers=[AITreeLayer(layer_id="history", depth=0, unknown_causes=[restored])],
    )
    reduced = reduce_candidate_state(tree)
    assert reduced.layers[0].unknown_causes == []
    assert reduced.data_quality["records"][-1]["status"] == "history_excluded"


def test_reducer_excludes_nodes_from_another_round_and_orders_layers_deterministically():
    current = _node("current", "Current claim")
    current = current.model_copy(update={"source_round": 2})
    old = _node("old", "Old claim")
    old = old.model_copy(update={"source_round": 1})
    tree = ControlledAITree(
        tree_id="tree",
        layers=[
            AITreeLayer(layer_id="z", depth=1, unknown_causes=[old]),
            AITreeLayer(layer_id="a", depth=0, unknown_causes=[current]),
        ],
    )

    reduced = reduce_candidate_state(tree, current_round=2)

    assert [layer.layer_id for layer in reduced.layers] == ["a", "z"]
    assert reduced.layers[1].unknown_causes == []
    assert reduced.data_quality["records"][-1]["status"] == "history_excluded"
