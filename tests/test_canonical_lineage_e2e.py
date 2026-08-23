from server.app.diagnosis.canonical_candidate_state import reduce_candidate_state
from server.app.diagnosis.canonical_probe_plan import merge_probe_input_maps, normalize_probe_plan, union_probe_families
from server.app.diagnosis.session_conclusion import _build_localization_chain
from server.app.rca.models import AITreeCandidateNode, AITreeLayer, ControlledAITree
import json
from pathlib import Path


def test_canonical_tree_probe_and_boundary_flow_is_consistent_end_to_end():
    parent = AITreeCandidateNode(
        candidate_id="analyzer-parent",
        generated_by="analyzer",
        claim_origin="analyzer_diagnostic",
        relation="root",
        role="unknown",
        claim="Worker memory pressure",
        supported_level="process",
        evidence_refs=["ev-rss"],
    )
    duplicate = AITreeCandidateNode(
        candidate_id="ai-duplicate",
        generated_by="ai",
        claim_origin="ai_proposal",
        claim_transform="refined",
        parent_candidate_ids=[parent.candidate_id],
        origin_parent_candidate_id=parent.candidate_id,
        relation="refinement",
        role="unknown",
        claim="worker memory pressure。",
        supported_level="process",
        evidence_refs=["ev-runtime"],
    )
    boundary = AITreeCandidateNode(
        candidate_id="probe-boundary",
        generated_by="system",
        claim_origin="fallback_generated",
        parent_candidate_ids=[parent.candidate_id],
        origin_parent_candidate_id=parent.candidate_id,
        relation="boundary",
        node_type="stop_boundary",
        depth_kind="boundary",
        role="unknown",
        claim="Source mechanism probe failed; retain parent.",
        supported_level="process",
        status="blocked",
    )
    tree = ControlledAITree(
        tree_id="e2e",
        layers=[
            AITreeLayer(layer_id="base", depth=0, unknown_causes=[parent]),
            AITreeLayer(layer_id="followup", depth=1, unknown_causes=[duplicate, boundary]),
        ],
        retained_candidate_id=parent.candidate_id,
        final_unknown_causes=[parent.candidate_id, duplicate.candidate_id],
    )

    families = union_probe_families(
        ["source_mechanism_query", "cpu_profile"],
        ["source_mechanism_query"],
    )
    merged = merge_probe_input_maps(
        {"source_mechanism_query": {
            "candidate_id": duplicate.candidate_id,
            "origin_parent_candidate_id": parent.candidate_id,
        }},
        {"source_mechanism_query": {
            "candidate_id": duplicate.candidate_id,
            "origin_parent_candidate_id": parent.candidate_id,
            "ai_generated_query": {"query_spec_hash": "query-hash"},
        }},
    )
    tree = tree.model_copy(update={
        "canonical_probe_plan": normalize_probe_plan(families, merged.inputs),
        "probe_conflicts": merged.conflicts,
    })
    reduced = reduce_candidate_state(tree)
    payload = reduced.model_dump(mode="json")
    chain = _build_localization_chain(payload, {"candidate_id": parent.candidate_id})

    emitted_ids = {
        node.candidate_id
        for layer in reduced.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
    }
    assert duplicate.candidate_id not in emitted_ids
    assert reduced.retained_candidate_id == parent.candidate_id
    assert reduced.layers[1].unknown_causes[0].boundary_message
    assert reduced.layers[1].unknown_causes[0].claim == ""
    assert [item["evidence_family"] for item in reduced.canonical_probe_plan] == [
        "source_mechanism_query", "cpu_profile",
    ]
    assert reduced.canonical_probe_plan[0]["probe_input"]["ai_generated_query"]["query_spec_hash"] == "query-hash"
    assert [step.statement for step in chain] == ["Worker memory pressure"]


def test_latest_celery_report_duplicate_pairs_are_rejected_by_current_reducer():
    path = Path("reports/eval/real-open-source/celery-8882-vulnerable-600s-20260823/run.json")
    if not path.exists():
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    payload = report["vulnerable"]["diagnosis"]["detail"]["latest_conclusion"]["controlled_ai_tree"]

    reduced = reduce_candidate_state(ControlledAITree.model_validate(payload))
    emitted = {
        node.candidate_id
        for layer in reduced.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
    }
    records = reduced.data_quality.get("records", [])

    assert not ({"ai_candidate_004", "ai_candidate_005", "ai_candidate_006"} & emitted)
    assert {
        record.get("candidate_id")
        for record in records
        if record.get("status") == "duplicate_claim"
    } >= {"ai_candidate_004", "ai_candidate_005", "ai_candidate_006"}
