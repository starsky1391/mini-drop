from server.app.diagnosis.canonical_claim_lineage import ensure_claim_lineage
from server.app.diagnosis.orchestrator import _build_session_controlled_ai_tree
from server.app.diagnosis.session_conclusion import (
    build_fallback_explanation,
    build_root_cause_clusters,
    build_session_qualification,
    derive_root_cause_clusters_from_ai_tree,
)
from server.app.rca.controlled_tree import enforce_conclusion_eligibility
from server.app.rca.models import AITreeCandidateNode, AITreeLayer, ControlledAITree


def test_analyzer_observation_cannot_become_formal_candidate():
    node = AITreeCandidateNode(
        candidate_id="analyzer-hotspot",
        generated_by="analyzer_observation",
        role="primary",
        claim="热点函数持续占用 CPU。",
        supported_level="function",
        status="supported",
        claim_type="direct_root_cause",
        causal_status="supported",
        decision="conclude",
        mechanism="self_code_execution",
        target="worker",
        evidence_refs=["ev-runtime"],
    )
    tree = enforce_conclusion_eligibility(ControlledAITree(
        tree_id="analyzer-only",
        layers=[AITreeLayer(layer_id="base", depth=0, primary_causes=[node])],
    ))

    guarded = tree.layers[0].unknown_causes[0]
    assert guarded.generated_by == "analyzer_observation"
    assert guarded.conclusion_eligible is False
    assert tree.final_primary_causes == []


def test_analyzer_tree_contains_observation_not_root_cause_claim():
    tree = _build_session_controlled_ai_tree(
        diagnosis_id="open-contract",
        cluster_assessment={
            "classification": "python_memory_retention",
            "summary": "RSS 在异常窗口持续增长。",
            "diagnostic_claim": "内存持续增长。",
            "mechanism": "python_retention",
            "claim_type": "direct_root_cause",
            "conclusion_eligible": True,
            "supported_level": "process",
            "evidence_refs": ["ev-rss"],
        },
        candidates=[{
            "candidate_id": "memory-rss",
            "rank": 1,
            "description": "目标进程 RSS 持续增长。",
            "evidence_refs": ["ev-rss"],
            "max_supported_level": "process",
        }],
        followup_requests=["python_heap_profile"],
        probes=[],
        child_trees=[],
    )

    nodes = [
        node
        for layer in tree.layers
        for node in (
            layer.primary_causes
            + layer.secondary_causes
            + layer.rejected_causes
            + layer.unknown_causes
        )
    ]
    analyzer_nodes = [node for node in nodes if node.candidate_id == "memory-rss"]
    assert len(analyzer_nodes) == 1
    assert analyzer_nodes[0].generated_by == "analyzer_observation"
    assert analyzer_nodes[0].claim_type == "abstention"
    assert analyzer_nodes[0].causal_status == "unproven"
    assert analyzer_nodes[0].conclusion_eligible is False


def test_session_tree_demotes_legacy_analyzer_clusters_to_nonformal_hints():
    clusters = build_root_cause_clusters(
        [{
            "dependency": {"failed_dependencies": ["redis"]},
            "evidence_refs": ["ev-dependency"],
            "target": {"service_id": "checkout"},
        }],
        {
            "classification": "downstream_dependency",
            "claim_target": "redis",
            "diagnostic_claim": "Redis 不可达。",
            "evidence_refs": ["ev-dependency"],
            "conclusion_eligible": True,
        },
        {"target_scope": {"target_service": "checkout"}},
        {"layers": []},
    )

    assert clusters
    assert all(cluster.conclusion_eligible is False for cluster in clusters)
    assert all(cluster.qualification != "confirmed_root_cause" for cluster in clusters)


def test_lineage_preserves_new_ai_source_values():
    candidate = ensure_claim_lineage({
        "candidate_id": "ai_candidate_1",
        "generated_by": "ai_candidate",
        "claim": "候选机制",
    })
    guarded = ensure_claim_lineage({
        "candidate_id": "ai_candidate_1",
        "generated_by": "ai_guarded",
        "claim": "已通过门禁的候选机制",
    })

    assert candidate["generated_by"] == "ai_candidate"
    assert candidate["claim_origin"] == "ai_proposal"
    assert guarded["generated_by"] == "ai_guarded"
    assert guarded["claim_origin"] == "ai_update"


def test_ai_candidate_formal_cluster_requires_ai_origin():
    tree = {
        "layers": [{
            "layer_id": "base",
            "depth": 0,
            "unknown_causes": [{
                "candidate_id": "coarse",
                "generated_by": "analyzer_observation",
                "relation": "root",
                "node_type": "cluster_root",
                "role": "unknown",
                "claim": "存在性能异常。",
                "supported_level": "resource",
                "status": "unknown",
                "claim_type": "partial_localization",
                "causal_status": "unproven",
                "decision": "continue_probe",
            }],
        }, {
            "layer_id": "candidate",
            "depth": 1,
            "unknown_causes": [{
                "candidate_id": "ai_candidate_1",
                "generated_by": "ai_candidate",
                "relation": "refinement",
                "parent_candidate_ids": ["coarse"],
                "origin_parent_candidate_id": "coarse",
                "role": "unknown",
                "claim": "输入触发慢路径。",
                "supported_level": "function",
                "status": "supported",
                "claim_type": "direct_root_cause",
                "causal_status": "supported",
                "decision": "conclude",
                "mechanism": "input_slow_path",
                "target": "worker",
                "evidence_refs": ["ev-runtime"],
            }],
        }],
    }

    clusters = derive_root_cause_clusters_from_ai_tree(
        tree,
        valid_evidence_refs={"ev-runtime"},
        anchor_evidence_refs={"ev-runtime"},
        runtime_anchor_evidence_refs={"ev-runtime"},
    )
    assert clusters
    assert clusters[0].source_tree_candidate_ids == ["ai_candidate_1"]


def test_session_ai_failure_abstains_even_when_candidate_was_prequalified():
    node = AITreeCandidateNode(
        candidate_id="ai_guarded_1",
        generated_by="ai_guarded",
        role="primary",
        claim="输入路径放大了计算成本。",
        supported_level="function",
        status="supported",
        claim_type="direct_root_cause",
        causal_status="supported",
        decision="conclude",
        mechanism="input_amplification",
        target="worker",
        evidence_refs=["ev-runtime"],
    )
    cluster = derive_root_cause_clusters_from_ai_tree(
        {
            "layers": [{
                "layer_id": "base",
                "depth": 0,
                "unknown_causes": [{
                    **node.model_dump(mode="json"),
                    "relation": "root",
                    "parent_candidate_ids": [],
                }],
            }],
        },
        valid_evidence_refs={"ev-runtime"},
        anchor_evidence_refs={"ev-runtime"},
        runtime_anchor_evidence_refs={"ev-runtime"},
    )
    result = build_fallback_explanation(
        cluster,
        {},
        status="failed",
        error="AI unavailable",
    )

    assert result["root_cause_clusters"] == []
    assert result["causal_chain"] == []
    assert result["formal_root_cause"] is None
    assert result["abstained"] is True


def test_session_qualification_does_not_promote_when_session_review_failed():
    node = AITreeCandidateNode(
        candidate_id="ai_guarded_2",
        generated_by="ai_guarded",
        relation="root",
        role="primary",
        claim="源码关系支持输入放大。",
        supported_level="function",
        status="supported",
        claim_type="direct_root_cause",
        causal_status="supported",
        decision="conclude",
        mechanism="input_amplification",
        target="worker",
        evidence_refs=["ev-runtime"],
    )
    tree = {
        "layers": [{
            "layer_id": "base",
            "depth": 0,
            "primary_causes": [node.model_dump(mode="json")],
        }],
    }
    cluster = derive_root_cause_clusters_from_ai_tree(
        tree,
        valid_evidence_refs={"ev-runtime"},
        anchor_evidence_refs={"ev-runtime"},
        runtime_anchor_evidence_refs={"ev-runtime"},
    )
    qualification = build_session_qualification(
        cluster,
        tree,
        session_ai_review_status="failed",
        attribution_qualification={
            "level": "L3",
            "qualification": "formal_root_cause",
            "decision": "conclude",
            "causal_status": "supported",
            "eligible_candidate_ids": ["ai_guarded_2"],
        },
    )

    assert qualification["qualification"] != "formal_root_cause"
    assert qualification["eligible_candidate_ids"] == []
