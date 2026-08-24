from server.app.rca.controlled_tree import (
    classify_primitive,
    enforce_conclusion_eligibility,
    evidence_refs_with_anchors,
    qualify_ai_candidate,
)
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

    guarded = tree.layers[0].unknown_causes[0]
    assert classify_primitive("clock_nanosleep+90") == "wait_primitive"
    assert guarded.claim_type == "observation_only"
    assert guarded.conclusion_eligible is False
    assert tree.final_primary_causes == []


def test_mechanism_claim_with_target_and_evidence_passes_gate():
    node = AITreeCandidateNode(
        candidate_id="redis_timeout",
        generated_by="ai",
        claim_origin="ai_proposal",
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


def test_formal_ai_candidate_requires_a_real_runtime_or_source_anchor():
    node = AITreeCandidateNode(
        candidate_id="unanchored-cause",
        generated_by="ai",
        claim_origin="ai_proposal",
        relation="root",
        role="primary",
        claim="缓存策略导致请求变慢。",
        supported_level="service",
        status="supported",
        claim_type="direct_root_cause",
        causal_status="supported",
        decision="conclude",
        mechanism="cache_policy",
        target="checkoutservice",
        evidence_refs=["ev-summary"],
    )

    eligible, reason = qualify_ai_candidate(
        node,
        valid_evidence_refs={"ev-summary"},
        anchor_evidence_refs=set(),
    )

    assert eligible is False
    assert "真实运行时或源码锚点" in reason
    assert evidence_refs_with_anchors([
        {"evidence_id": "ev-summary", "observed_value": {"summary": {"message": "slow"}}},
        {"evidence_id": "ev-runtime", "observed_value": {"pid": 42, "function": "checkout"}},
    ]) == {"ev-runtime"}


def test_source_snapshot_alone_cannot_promote_formal_root_or_line():
    node = AITreeCandidateNode(
        candidate_id="source-only-line",
        generated_by="ai",
        claim_origin="ai_proposal",
        relation="root",
        role="primary",
        claim="worker.py:42 的逻辑导致请求变慢。",
        supported_level="line",
        status="supported",
        claim_type="direct_root_cause",
        causal_status="supported",
        decision="conclude",
        mechanism="slow_branch",
        target="worker.py:42",
        evidence_refs=["ev-source"],
    )

    eligible, reason = qualify_ai_candidate(
        node,
        valid_evidence_refs={"ev-source"},
        anchor_evidence_refs={"ev-source"},
        runtime_anchor_evidence_refs=set(),
        line_anchor_evidence_refs={"ev-source"},
    )

    assert eligible is False
    assert "运行时锚点" in reason


def test_runtime_stack_observation_cannot_be_promoted_to_primary_cause():
    node = AITreeCandidateNode(
        candidate_id="python_runtime_stack_hotspot",
        role="primary",
        claim="采样调用栈非空。",
        supported_level="function",
        confidence=0.82,
        status="supported",
        claim_type="likely_root_cause",
        causal_status="supported",
        decision="conclude",
        mechanism="python_runtime_stack_hotspot",
        target="worker",
        evidence_refs=["top_functions[0]"],
    )
    tree = ControlledAITree(
        tree_id="tree-observation-only",
        final_supported_level="function",
        layers=[AITreeLayer(layer_id="layer_0", depth=0, primary_causes=[node])],
    )

    guarded = enforce_conclusion_eligibility(tree)

    assert guarded.final_primary_causes == []
    assert guarded.final_unknown_causes == ["python_runtime_stack_hotspot"]
    guarded_node = guarded.layers[0].unknown_causes[0]
    assert guarded_node.claim_type == "observation_only"
    assert guarded_node.causal_status == "unproven"
    assert guarded_node.decision == "continue_probe"


def test_qualified_unknown_sibling_is_normalized_to_secondary_cause():
    node = AITreeCandidateNode(
        candidate_id="callback_lock_wait",
        generated_by="ai_candidate",
        claim_origin="ai_proposal",
        role="unknown",
        relation="root",
        claim="结果回调函数的锁等待降低了任务释放速度。",
        supported_level="function",
        status="supported",
        claim_type="direct_root_cause",
        causal_status="supported",
        mechanism="callback_lock_wait",
        target="backend.on_chord_part_return",
        evidence_refs=["ev-function"],
    )

    guarded = enforce_conclusion_eligibility(ControlledAITree(
        tree_id="qualified-unknown",
        layers=[AITreeLayer(layer_id="causes", depth=1, unknown_causes=[node])],
    ))

    assert guarded.layers[0].unknown_causes == []
    assert guarded.layers[0].secondary_causes[0].candidate_id == "callback_lock_wait"
    assert guarded.final_secondary_causes == ["callback_lock_wait"]


def test_final_cause_ids_keep_only_deepest_qualified_node_on_same_branch():
    parent = AITreeCandidateNode(
        candidate_id="function-parent",
        generated_by="ai_candidate",
        claim_origin="ai_proposal",
        role="primary",
        relation="root",
        claim="任务状态更新函数持续保留对象。",
        supported_level="function",
        status="supported",
        claim_type="direct_root_cause",
        causal_status="supported",
        mechanism="task_state_retention",
        target="update_state",
        evidence_refs=["ev-function"],
    )
    child = parent.model_copy(update={
        "candidate_id": "line-child",
        "parent_candidate_ids": ["function-parent"],
        "origin_parent_candidate_id": "function-parent",
        "relation": "refinement",
        "claim": "worker.py:42 的状态更新异常持续保留对象。",
        "supported_level": "line",
        "target": "worker.py:42",
        "evidence_refs": ["ev-line"],
    })

    guarded = enforce_conclusion_eligibility(ControlledAITree(
        tree_id="deepest-formal",
        layers=[
            AITreeLayer(layer_id="function", depth=1, primary_causes=[parent]),
            AITreeLayer(layer_id="line", depth=2, primary_causes=[child]),
        ],
    ))

    assert guarded.final_primary_causes == ["line-child"]
