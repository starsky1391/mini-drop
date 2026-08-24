"""智能归因模块测试。

覆盖：证据采集 / 候选规则匹配 / 置信度校准 /
       Prompt 模板 / 输出校验 / 自修复重试 / 降级行为。
"""

import json
from unittest import mock

import pytest

from server.app.rca.calibrator import calibrate, interpret_confidence
from server.app.rca.controlled_tree import enforce_conclusion_eligibility
from server.app.rca.candidates import generate_candidates, load_rules
from server.app.rca.evidence import collect_evidence, evidence_to_json
from server.app.rca.attribution import analyze_evidence
from server.app.rca.llm_client import (
    _apply_compact_guard_review,
    _attach_analysis_result,
    _collect_evidence_paths,
    _extract_json,
    _filter_probe_request_specs,
    _normalize_probe_requests,
    _call_deepseek,
    _ref_exists,
    _validate_and_parse,
    generate_controlled_ai_tree,
    generate_session_candidate_review,
    generate_session_investigation_review,
)
from server.app.diagnosis.probe_registry import build_probe_manifest
from server.app.rca.models import (
    AITreeCandidateNode,
    AITreeLayer,
    CandidateCause,
    CauseEntry,
    ControlledAITree,
    DiagnosisReport,
    EvidenceInput,
    FeedbackPrior,
)
from server.app.rca.prompt import build_system_prompt, build_user_message
from server.app.rca.report import run_diagnosis, run_diagnosis_context


def test_forbidden_upgrade_boundary_is_inconclusive_not_counterevidence():
    tree = ControlledAITree(
        tree_id="forbidden-boundary",
        layers=[AITreeLayer(
            layer_id="layer-1",
            depth=1,
            unknown_causes=[AITreeCandidateNode(
                candidate_id="blocked_line_upgrade",
                role="unknown",
                claim="缺少行级证据，禁止升级。",
                supported_level="function",
                status="forbidden",
                evidence_refs=["ev-function"],
            )],
        )],
    )

    guarded = enforce_conclusion_eligibility(tree)
    node = guarded.layers[0].unknown_causes[0]

    assert node.causal_status == "inconclusive"
    assert node.decision == "backtrack"
    assert node.conclusion_eligible is False
    assert node.candidate_id in guarded.final_unknown_causes
    assert node.candidate_id not in guarded.final_rejected_causes


def test_structured_probe_request_keeps_valid_request_and_normalizes_observations():
    families, specs = _normalize_probe_requests([
        {
            "evidence_family": "python_heap_profile",
            "question": "对象保留是否集中在规则编译路径？",
            "why_needed": "区分热点与实际保留链。",
            "input_refs": ["ev-top"],
            "expected_observation": "同一对象类型在保留快照中持续增长",
            "disconfirming_observation": ["保留链不经过规则编译路径"],
        }
    ])

    accepted_families, accepted_specs, rejected = _filter_probe_request_specs(
        families,
        specs,
        {"ev-top"},
    )

    assert accepted_families == ["python_heap_profile"]
    assert accepted_specs[0]["expected_observation"] == ["同一对象类型在保留快照中持续增长"]
    assert accepted_specs[0]["disconfirming_observation"] == ["保留链不经过规则编译路径"]
    assert rejected == []


def test_structured_probe_request_rejects_missing_fields_and_unknown_refs():
    families, specs = _normalize_probe_requests([
        {
            "evidence_family": "python_heap_profile",
            "question": "是否存在对象保留？",
            "why_needed": "",
            "input_refs": ["ev-missing"],
            "expected_observation": ["对象数量上升"],
            "disconfirming_observation": ["对象数量稳定"],
        },
        {
            "evidence_family": "source_mechanism_query",
            "question": "是否存在对应源码机制？",
            "why_needed": "验证调用点与机制关系。",
            "input_refs": [],
            "expected_observation": ["命中源码关系"],
            "disconfirming_observation": ["未命中源码关系"],
        },
    ])

    accepted_families, accepted_specs, rejected = _filter_probe_request_specs(
        families,
        specs,
        {"ev-top"},
    )

    assert accepted_families == []
    assert accepted_specs == []
    assert [item["evidence_family"] for item in rejected] == [
        "python_heap_profile",
        "source_mechanism_query",
    ]
    assert rejected[0]["invalid_input_refs"] == ["ev-missing"]
    assert rejected[1]["invalid_input_refs"] == []
    assert "真实 evidence ref" in rejected[1]["reason"]


def test_structured_probe_request_rejection_does_not_drop_valid_sibling():
    families, specs = _normalize_probe_requests([
        {
            "evidence_family": "python_heap_profile",
            "question": "是否存在对象保留？",
            "why_needed": "验证保留链。",
            "input_refs": ["ev-invalid"],
            "expected_observation": ["对象数量上升"],
            "disconfirming_observation": ["对象数量稳定"],
        },
        {
            "evidence_family": "runtime_stack",
            "question": "当前运行栈是否指向业务函数？",
            "why_needed": "排除运行时等待栈。",
            "input_refs": ["ev-top"],
            "expected_observation": ["出现业务调用栈"],
            "disconfirming_observation": ["只有运行时等待栈"],
        },
    ])

    accepted_families, accepted_specs, rejected = _filter_probe_request_specs(
        families,
        specs,
        {"ev-top"},
    )

    assert accepted_families == ["runtime_stack"]
    assert accepted_specs[0]["evidence_family"] == "runtime_stack"
    assert [item["evidence_family"] for item in rejected] == ["python_heap_profile"]


def test_session_candidate_review_accepts_only_real_refs_and_canonical_parents():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    parent_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
    )
    response = {
        "probe_requests": [],
        "candidates": [{
            "candidate_id": "ai_candidate_rule_compile",
            "claim": "规则编译路径造成目标进程内存保留",
            "mechanism": "retained_allocation_during_rule_compilation",
            "target": "Rule.compile",
            "supported_level": "function",
            "decision": "needs_more_evidence",
            "causal_status": "needs_more_evidence",
            "evidence_refs": ["ev_top"],
            "parent_candidate_ids": [parent_id],
        }],
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-1",
            fact_context={"candidate_hints": [{"hint_id": "memory_hint"}]},
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{"evidence_id": "ev_top"}],
            probe_manifest=build_probe_manifest(),
        )
    assert result["ai_review_status"] == "succeeded"
    assert result["candidate_proposals"][0]["candidate_id"] == "ai_candidate_rule_compile"
    assert result["candidate_proposals"][0]["parent_candidate_ids"] == [parent_id]


def test_session_candidate_review_rejects_first_round_level_jump_and_line():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    parent_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
    )
    response = {
        "probe_requests": [],
        "candidates": [{
            "candidate_id": "ai_candidate_jump_to_line",
            "claim": "直接把首轮候选定位到源码行",
            "mechanism": "unverified_line_mechanism",
            "target": "Rule.compile",
            "supported_level": "line",
            "decision": "needs_more_evidence",
            "causal_status": "needs_more_evidence",
            "evidence_refs": ["ev_top"],
            "parent_candidate_ids": [parent_id],
        }],
    }
    with mock.patch.dict(
        "os.environ",
        {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"},
    ), mock.patch(
        "server.app.rca.llm_client._call_deepseek",
        return_value=json.dumps(response),
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-first-round-level-jump",
            fact_context={"localization_boundary": {"level": "function"}},
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{"evidence_id": "ev_top"}],
            probe_manifest=build_probe_manifest(),
        )

    assert result["ai_review_status"] == "failed"
    assert result["candidate_proposals"] == []
    assert result["validation_diagnostics"][0]["failure_code"] == "candidate_level_jump"
    assert result["validation_diagnostics"][0]["candidate_supported_level"] == "line"
    assert result["validation_diagnostics"][0]["candidate_entry_level"] == "function"


def test_session_candidate_review_keeps_valid_candidates_when_one_candidate_is_invalid():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    parent_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
    )
    response = {
        "probe_requests": ["python_heap_profile"],
        "candidates": [
            {
                "candidate_id": "ai_candidate_invalid",
                "claim": "invalid",
                "mechanism": "bad",
                "target": "worker",
                "supported_level": "process",
                "decision": "not-a-decision",
                "causal_status": "needs_more_evidence",
                "evidence_refs": ["ev-top"],
                "parent_candidate_ids": [parent_id],
            },
            {
                "candidate_id": "ai_candidate_valid",
                "claim": "规则编译路径仍需堆证据验证",
                "mechanism": "retained_allocation_during_rule_compilation",
                "target": "Rule.compile",
                "supported_level": "function",
                "decision": "needs_more_evidence",
                "causal_status": "needs_more_evidence",
                "evidence_refs": ["ev-top"],
                "parent_candidate_ids": [parent_id],
                "probe_requests": ["python_heap_profile"],
            },
        ],
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-partial-candidate",
            fact_context={},
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{"evidence_id": "ev-top"}],
            probe_manifest=build_probe_manifest(),
        )
    assert result["ai_review_status"] == "succeeded"
    assert [item["candidate_id"] for item in result["candidate_proposals"]] == ["ai_candidate_valid"]
    assert result["validation_diagnostics"][0]["failure_code"] == "invalid_decision"
    assert result["candidate_generation_attempts"][0]["accepted_candidate_count"] == 1
    assert result["candidate_generation_attempts"][0]["rejected_candidate_count"] == 1
    assert result["probe_inputs"]["python_heap_profile"] == {
        "candidate_id": "ai_candidate_valid",
        "origin_parent_candidate_id": parent_id,
    }


def test_session_candidate_review_normalizes_root_candidate_to_emitted_coarse_parent():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    coarse_ids = ["unknown_evidence_gap"]
    analysis_tree = analysis.controlled_ai_tree.model_copy(update={
        "emitted_coarse_ids": coarse_ids,
        "coarse_aliases": {
            "coarse_insufficient_evidence": coarse_ids[0],
            coarse_ids[0]: coarse_ids[0],
        },
    })
    assert len(coarse_ids) == 1
    response = {
        "probe_requests": [],
        "candidates": [{
            "candidate_id": "ai_candidate_root_alias",
            "claim": "候选仍需沿当前粗粒度方向补证。",
            "mechanism": "runtime_task_path",
            "target": "worker",
            "supported_level": "process",
            "decision": "needs_more_evidence",
            "causal_status": "needs_more_evidence",
            "evidence_refs": ["ev-top"],
            "relation": "root",
        }],
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-root-alias",
            fact_context={},
            session_tree=analysis_tree,
            evidence_catalog=[{"evidence_id": "ev-top"}],
            probe_manifest=build_probe_manifest(),
        )

    assert result["ai_review_status"] == "succeeded"
    candidate = result["candidate_proposals"][0]
    assert candidate["relation"] == "refinement"
    assert candidate["parent_candidate_ids"] == coarse_ids
    assert candidate["origin_parent_candidate_id"] == coarse_ids[0]


def test_session_candidate_review_treats_omitted_relation_as_root_before_coarse_normalization():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    coarse_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.unknown_causes,
            *layer.rejected_causes,
        ]
        if node.node_type in {"cluster_root", "coarse_candidate"}
    )
    response = {
        "candidates": [{
            "candidate_id": "ai_candidate_omitted_relation",
            "claim": "候选需要沿当前粗粒度方向继续补证。",
            "mechanism": "runtime_task_path",
            "target": "worker",
            "supported_level": "process",
            "decision": "needs_more_evidence",
            "causal_status": "needs_more_evidence",
            "evidence_refs": ["ev-top"],
        }],
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-omitted-relation",
            fact_context={},
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{"evidence_id": "ev-top"}],
            probe_manifest=build_probe_manifest(),
        )

    assert result["ai_review_status"] == "succeeded"
    candidate = result["candidate_proposals"][0]
    assert candidate["relation"] == "refinement"
    assert candidate["parent_candidate_ids"] == [coarse_id]
    assert candidate["origin_parent_candidate_id"] == coarse_id


def test_session_candidate_review_limits_active_investigation_to_three_deduplicated_candidates():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    parent_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
    )
    candidates = [
        {
            "candidate_id": f"ai_candidate_{index}",
            "claim": f"候选 {index}",
            "mechanism": mechanism,
            "target": "worker",
            "supported_level": "process",
            "decision": "needs_more_evidence",
            "causal_status": "needs_more_evidence",
            "evidence_refs": ["ev-top"],
            "parent_candidate_ids": [parent_id],
            "probe_requests": ["python_heap_profile"],
        }
        for index, mechanism in enumerate(
            ["exception_retention", "runtime_path", "broker_interaction", "low_quality_hotspot"],
            start=1,
        )
    ]
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek",
        return_value=json.dumps({"probe_requests": [], "candidates": candidates}),
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-active-candidates",
            fact_context={},
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{"evidence_id": "ev-top"}],
            probe_manifest=build_probe_manifest(),
        )
    assert result["ai_review_status"] == "succeeded"
    assert len(result["candidate_proposals"]) == 4
    assert len(result["active_candidate_ids"]) == 3
    assert len(result["deferred_candidate_ids"]) == 1
    assert len(result["candidate_selection_diagnostics"]) == 4
    assert {
        item["selection"]
        for item in result["candidate_selection_diagnostics"]
    } == {"active", "deferred"}


def test_session_candidate_review_keeps_candidate_when_only_probe_request_is_invalid():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    parent_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
    )
    response = {
        "probe_requests": [],
        "candidates": [{
            "candidate_id": "ai_candidate_probe_filtered",
            "claim": "候选本身仍需要调查",
            "mechanism": "runtime_task_path",
            "target": "worker",
            "supported_level": "process",
            "decision": "needs_more_evidence",
            "causal_status": "needs_more_evidence",
            "evidence_refs": ["ev-top"],
            "parent_candidate_ids": [parent_id],
            "probe_requests": ["not_registered_probe"],
        }],
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek",
        return_value=json.dumps(response),
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-probe-filter",
            fact_context={},
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{"evidence_id": "ev-top"}],
            probe_manifest=build_probe_manifest(),
        )

    assert result["ai_review_status"] == "succeeded"
    assert [item["candidate_id"] for item in result["candidate_proposals"]] == [
        "ai_candidate_probe_filtered"
    ]
    assert result["candidate_proposals"][0]["probe_requests"] == []
    assert result["validation_diagnostics"][0]["failure_code"] == "invalid_probe_request"


def test_session_candidate_review_does_not_schedule_unbound_top_level_probe():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    parent_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
    )
    response = {
        "probe_requests": ["python_heap_profile"],
        "candidates": [{
            "candidate_id": "ai_candidate_runtime_only",
            "claim": "运行时路径需要继续验证",
            "mechanism": "runtime_task_path",
            "target": "worker",
            "supported_level": "process",
            "decision": "needs_more_evidence",
            "causal_status": "needs_more_evidence",
            "evidence_refs": ["ev-top"],
            "parent_candidate_ids": [parent_id],
            "probe_requests": [],
        }],
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response),
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-unbound-probe",
            fact_context={},
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{"evidence_id": "ev-top"}],
            probe_manifest=build_probe_manifest(),
        )

    assert result["ai_review_status"] == "succeeded"
    assert result["selected_evidence_families"] == []
    assert result["probe_inputs"] == {}
    assert result["validation_diagnostics"][0]["failure_code"] == "unbound_probe_request"

def test_session_candidate_review_rejects_hint_id_and_unknown_evidence():
    response = {
        "probe_requests": [],
        "candidates": [{
            "candidate_id": "memory_hint",
            "claim": "hint",
            "mechanism": "unknown",
            "target": "worker",
            "evidence_refs": ["ev_missing"],
        }],
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-1",
            fact_context={"candidate_hints": [{"hint_id": "memory_hint"}]},
            session_tree=None,
            evidence_catalog=[{"evidence_id": "ev_real"}],
            probe_manifest=build_probe_manifest(),
        )
    assert result["ai_review_status"] == "failed"
    assert result["validation_diagnostics"][0]["failure_code"] in {
        "invalid_candidate_id",
        "invalid_evidence_ref",
    }
    assert result["validation_diagnostics"][0]["candidate_count"] == 1


def test_session_candidate_review_accepts_investigation_backtrack_alias():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    parent_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
    )
    response = {
        "probe_requests": [],
        "candidates": [{
            "candidate_id": "ai_candidate_aliases",
            "claim": "候选仍需补证",
            "mechanism": "runtime_alias",
            "target": "worker",
            "supported_level": "process",
            "decision": "backtrack",
            "causal_status": "inconclusive",
            "evidence_refs": ["ev_top"],
            "parent_candidate_ids": [parent_id],
        }],
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_candidate_review(
            diagnosis_id="diag-alias",
            fact_context={},
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{"evidence_id": "ev_top"}],
            probe_manifest=build_probe_manifest(),
        )
    assert result["ai_review_status"] == "succeeded"
    assert result["candidate_proposals"][0]["decision"] == "needs_more_evidence"
    assert result["candidate_proposals"][0]["causal_status"] == "needs_more_evidence"
    assert result["candidate_proposals"][0]["origin_parent_candidate_id"] == parent_id
    assert result["candidate_generation_attempts"][0]["status"] == "succeeded"
    assert result["initial_evidence_context"]["evidence_snapshots"]["ev_top"]["family"] == "unknown"


def test_session_investigation_review_selects_registered_probe_and_guarded_proposal():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(
        evidence,
        [CandidateCause(candidate_id="memory_candidate", description="memory", evidence_refs=["top_functions[0]"], rule_score=0.5)],
    )
    parent_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
    )
    response = {
        "selected_evidence_families": ["python_heap_profile"],
        "candidate_proposals": [{
            "candidate_id": "ai_proposal_retained_rule_builder",
            "parent_candidate_ids": [parent_id],
            "origin_parent_candidate_id": parent_id,
            "claim": "动态规则构建阶段保留分配对象，可能造成 Map 生命周期延长。",
            "mechanism": "retained_allocation_during_rule_compilation",
            "target": "Rule.compile",
            "supported_level": "host",
            "evidence_refs": ["ev_heap"],
            "opposing_evidence_refs": [],
            "missing_evidence": ["python_heap_profile"],
            "what_would_change_my_mind": "Memray 未显示该调用路径存在持续或保留分配。",
        }],
        "candidate_updates": {
            parent_id: {
                "status": "partial",
                "causal_status": "inconclusive",
                "decision": "backtrack",
                "missing_evidence": ["python_heap_profile"],
            },
        },
        "rollback_edges": [{
            "edge_id": "rollback-retained-parent",
            "from_candidate_ids": ["ai_proposal_retained_rule_builder"],
            "to_candidate_ids": [parent_id],
            "transition_type": "backtrack",
            "effect": "rollback",
            "status": "blocked",
            "reason": "补证未完成，保留来源父节点。",
        }],
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_investigation_review(
            diagnosis_id="diag-1",
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{"evidence_id": "ev_heap", "observed_value": {"summary": "memory grows"}}],
            probe_manifest=build_probe_manifest(),
            allowed_evidence_families=["python_heap_profile"],
        )
    assert result["ai_review_status"] == "succeeded"
    assert result["selected_evidence_families"] == ["python_heap_profile"]
    assert result["candidate_proposals"][0]["candidate_id"].startswith("ai_proposal_")
    assert result["candidate_updates"][parent_id]["status"] == "partial"
    assert result["rollback_edges"][0]["status"] == "blocked"


def test_session_investigation_review_rejects_refinement_level_jump():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    base_layer = analysis.controlled_ai_tree.layers[0]
    base_node = base_layer.unknown_causes[0].model_copy(update={"supported_level": "resource"})
    tree = analysis.controlled_ai_tree.model_copy(update={
        "final_supported_level": "line",
        "layers": [base_layer.model_copy(update={"unknown_causes": [base_node, *base_layer.unknown_causes[1:]]}), *analysis.controlled_ai_tree.layers[1:]],
    })
    parent_id = tree.layers[0].unknown_causes[0].candidate_id
    response = {
        "selected_evidence_families": ["python_heap_profile"],
        "candidate_proposals": [{
            "candidate_id": "ai_proposal_level_jump",
            "parent_candidate_ids": [parent_id],
            "origin_parent_candidate_id": parent_id,
            "claim": "候选直接跳到函数层。",
            "mechanism": "unverified_function_retention",
            "target": "Rule.compile",
            "supported_level": "function",
            "evidence_refs": ["ev_heap"],
            "missing_evidence": ["python_heap_profile"],
            "what_would_change_my_mind": "下一层证据不能支持该函数定位。",
        }],
    }
    with mock.patch.dict(
        "os.environ",
        {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"},
    ), mock.patch(
        "server.app.rca.llm_client._call_deepseek",
        return_value=json.dumps(response),
    ):
        result = generate_session_investigation_review(
            diagnosis_id="diag-level-jump",
            session_tree=tree,
            evidence_catalog=[{"evidence_id": "ev_heap"}],
            probe_manifest=build_probe_manifest(),
            allowed_evidence_families=["python_heap_profile"],
        )

    assert result["ai_review_status"] == "failed"
    assert result["validation_diagnostics"][0]["failure_code"] == "candidate_level_jump"


def test_session_investigation_review_rejects_unregistered_probe():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    response = {"selected_evidence_families": ["arbitrary_shell"], "candidate_proposals": []}
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_investigation_review(
            diagnosis_id="diag-1",
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[],
            probe_manifest=build_probe_manifest(),
            allowed_evidence_families=["python_heap_profile"],
        )
    assert result["ai_review_status"] == "failed"


def test_session_investigation_review_accepts_guarded_temporary_codeql_query():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    query = """/**
 * @name Mini-Drop mechanism path
 * @kind path-problem
 * @id mini-drop/mechanism-path
 */
import python
import semmle.python.dataflow.new.DataFlow
from DataFlow::PathNode source, DataFlow::PathNode sink
where source = sink
select sink.getNode(), source, sink, "same anchored node"
"""
    response = {
        "selected_evidence_families": ["source_mechanism_query"],
        "candidate_proposals": [],
        "probe_inputs": {
            "source_mechanism_query": {
                "ai_generated_query": {
                    "investigation_question": "converter.to_url 是否经常量容器进入生成函数的 code object？",
                    "candidate_id": next(
                        node.candidate_id
                        for layer in analysis.controlled_ai_tree.layers
                        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
                    ),
                    "origin_parent_candidate_id": next(
                        node.candidate_id
                        for layer in analysis.controlled_ai_tree.layers
                        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
                    ),
                    "expected_relation": "supports",
                    "source_anchor": {"file": "src/werkzeug/routing.py", "line": 1066},
                    "sink_anchor": {"file": "src/werkzeug/routing.py", "line": 1119},
                    "path_anchors": [
                        {"file": "src/werkzeug/routing.py", "line": 1066},
                        {"file": "src/werkzeug/routing.py", "line": 1119},
                    ],
                    "query": query,
                },
            },
        },
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_investigation_review(
            diagnosis_id="diag-codeql",
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{
                "query_or_probe": "source_snapshot",
                "observed_value": {
                    "producer": "git+universal-ctags",
                    "snippets": [],
                    "enclosing_contexts": [{
                        "file": "src/werkzeug/routing.py",
                        "symbol": "BuilderCompiler",
                        "reference_paths": [{
                            "upstream_candidates": [{"line": 1066, "expression": "converter.to_url"}],
                            "source_lines": [{"line": 1119, "text": "co = types.CodeType(*code_args)"}],
                        }],
                    }],
                },
            }],
            probe_manifest=build_probe_manifest(),
            allowed_evidence_families=["source_mechanism_query"],
        )

    assert result["ai_review_status"] == "succeeded"
    guarded = result["probe_inputs"]["source_mechanism_query"]["ai_generated_query"]
    assert guarded["origin"] == "ai_guarded_anchor_spec"
    assert guarded["query_spec_hash"].startswith("sha256:")
    assert guarded["raw_query_hash"].startswith("sha256:")
    assert guarded["source_anchor"]["line"] == 1066
    assert "query" not in guarded


def test_session_investigation_review_rejects_unverified_codeql_anchor():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    response = {
        "selected_evidence_families": ["source_mechanism_query"],
        "candidate_proposals": [],
        "probe_inputs": {
            "source_mechanism_query": {
                "ai_generated_query": {
                    "investigation_question": "检查机制",
                    "candidate_id": next(
                        node.candidate_id
                        for layer in analysis.controlled_ai_tree.layers
                        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
                    ),
                    "expected_relation": "supports",
                    "source_anchor": {"file": "src/routing.py", "line": 20},
                    "sink_anchor": {"file": "/tmp/arbitrary.py", "line": 99},
                },
            },
        },
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_investigation_review(
            diagnosis_id="diag-codeql-invalid",
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[{
                "query_or_probe": "source_snapshot",
                "observed_value": {
                    "producer": "git+universal-ctags",
                    "snippets": [
                        {"file": "src/routing.py", "focus_line": 20, "symbol": "compile"},
                        {"file": "src/routing.py", "focus_line": 21, "symbol": "compile"},
                    ],
                    "enclosing_contexts": [],
                },
            }],
            probe_manifest=build_probe_manifest(),
            allowed_evidence_families=["source_mechanism_query"],
        )

    assert result["ai_review_status"] == "failed"


def test_session_investigation_review_binds_pyheap_to_existing_candidate():
    evidence = EvidenceInput(top_functions=[{"name": "Rule.compile", "percent": 70.0}])
    analysis = analyze_evidence(evidence, [])
    candidate_id = next(
        node.candidate_id
        for layer in analysis.controlled_ai_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes, *layer.rejected_causes]
    )
    response = {
        "selected_evidence_families": ["python_heap_reference"],
        "candidate_proposals": [],
        "probe_inputs": {
            "python_heap_reference": {
                "candidate_id": candidate_id,
                "origin_parent_candidate_id": candidate_id,
                "object_type_hints": ["function", "code", "method", "Map"],
            },
        },
    }
    with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key", "MINI_DROP_AI_ENABLED": "1"}), mock.patch(
        "server.app.rca.llm_client._call_deepseek", return_value=json.dumps(response)
    ):
        result = generate_session_investigation_review(
            diagnosis_id="diag-pyheap",
            session_tree=analysis.controlled_ai_tree,
            evidence_catalog=[],
            probe_manifest=build_probe_manifest(),
            allowed_evidence_families=["python_heap_reference"],
        )

    assert result["ai_review_status"] == "succeeded"
    assert result["probe_inputs"]["python_heap_reference"]["candidate_id"] == candidate_id
from server.app.rca.tools import run_rca_tools


# ── 模拟 Task Record ──


class _StubTask:
    def __init__(self):
        self.id = "task_test"
        self.agent_id = "agent_test"
        self.collector_type = "perf_cpu"
        self.target_pid = 1234
        self.duration_sec = 15
        self.sample_rate = 99
        self.status = "DONE"
        self.status_reason = "analysis completed"


# ── 证据采集 ──


class TestEvidenceCollection:
    """证据采集层。"""

    def test_collects_all_fields(self):
        task = _StubTask()
        ev = collect_evidence(
            task_id=task.id, task_record=task,
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.0}],
            ebpf_metrics={"io_latency_us": {"[128,256)": 10}},
            suggestions=["检查递归"],
            failure_events=["permission denied"],
            baseline_diff={"cpu_percent_delta": 42.0},
            agent_stats={"max_cpu_percent": 3.1},
        )
        assert ev.task_metadata["collector_type"] == "perf_cpu"
        assert len(ev.top_functions) == 1
        assert ev.ebpf_metrics is not None
        assert len(ev.suggestions) == 1
        assert len(ev.failure_events) == 1
        assert ev.baseline_diff is not None

    def test_evidence_json_is_valid(self):
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task,
                              top_functions=[{"name": "f", "samples": 1, "percent": 100.0}])
        text = evidence_to_json(ev)
        data = json.loads(text)
        assert "task_metadata" in data
        assert "top_functions" in data

    def test_fields_ordered_by_recency(self):
        """top_functions 应出现在 ebpf_metrics 之前（证据按重要性排序，
        越重要越靠后 → 近因效应）。实际输出中重要字段放在后面。"""
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task,
                              top_functions=[{"name": "f", "samples": 1, "percent": 50.0}],
                              ebpf_metrics={"latency": {}})
        text = evidence_to_json(ev)
        # ebpf_metrics 应在 top_functions 之后出现（JSON 字符串位置）
        tf_pos = text.index("top_functions")
        ebpf_pos = text.index("ebpf_metrics")
        assert tf_pos < ebpf_pos

    def test_evidence_index_is_compacted_by_default(self):
        task = _StubTask()
        ev = collect_evidence(
            task_id="t1",
            task_record=task,
            evidence_index={
                "stack_samples": [
                    {
                        "hot_frame": f"frame_{index}",
                        "call_path": f"main;worker;frame_{index}",
                        "stack_fragment": ["main", "worker", f"frame_{index}"],
                    }
                    for index in range(8)
                ],
                "context": {
                    "call_path": "main;worker;frame_0",
                    "endpoint": "/api/order/create",
                },
            },
        )

        text = evidence_to_json(ev)
        data = json.loads(text)

        assert "evidence_index_raw" not in data
        assert len(data["evidence_index"]["stack_samples"]) == 5

    def test_evidence_index_can_hydrate_requested_refs(self):
        task = _StubTask()
        ev = collect_evidence(
            task_id="t1",
            task_record=task,
            evidence_index={
                "stack_samples": [
                    {
                        "hot_frame": "compute_hotspot",
                        "call_path": "main;worker;compute_hotspot",
                        "stack_fragment": ["main", "worker", "compute_hotspot"],
                        "wait_reason": "cpu_hotspot",
                    }
                ],
                "context": {
                    "call_path": "main;worker;compute_hotspot",
                    "endpoint": "/api/order/create",
                },
            },
        )

        text = evidence_to_json(ev, requested_refs=["evidence_index.stack_samples[0].call_path"])
        data = json.loads(text)

        assert data["evidence_index_raw"]["stack_samples"][0]["call_path"] == "main;worker;compute_hotspot"


# ── 候选规则 ──


class TestCandidateGeneration:
    """规则引擎。"""

    def test_rules_loaded_from_external_file(self):
        load_rules.cache_clear()
        rules = load_rules()
        assert any(rule["candidate_id"] == "cpu_hotspot_recursive" for rule in rules)
        assert all("match_type" in rule for rule in rules)

    def test_cpu_hotspot_matched(self):
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task,
                              top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}])
        candidates = generate_candidates(ev)
        ids = [c.candidate_id for c in candidates]
        assert "cpu_hotspot_recursive" in ids

    def test_io_wait_matched_from_ebpf(self):
        task = _StubTask()
        task.collector_type = "ebpf_io"
        ev = collect_evidence(task_id="t1", task_record=task,
                              ebpf_metrics={"io_latency_us": {"[128,256)": 10}})
        candidates = generate_candidates(ev)
        assert any(c.candidate_id == "io_wait_high" for c in candidates)

    def test_no_rules_fallback(self):
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task)
        candidates = generate_candidates(ev)
        assert len(candidates) == 1
        assert candidates[0].candidate_id == "insufficient_data"

    def test_target_pid_invalid_matched(self):
        task = _StubTask()
        task.status = "FAILED"
        ev = collect_evidence(task_id="t1", task_record=task,
                              failure_events=["目标 PID 不存在"])
        candidates = generate_candidates(ev)
        assert any(c.candidate_id == "target_pid_invalid" for c in candidates)

    def test_feedback_prior_adjusts_score(self):
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task,
                              top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}])
        candidates_no_prior = generate_candidates(ev)

        priors = {"cpu_hotspot_recursive": FeedbackPrior(
            candidate_id="cpu_hotspot_recursive", positive_count=2, negative_count=0, weight_delta=0.10)}
        candidates_with_prior = generate_candidates(ev, priors)

        cpu_no = next(c for c in candidates_no_prior if c.candidate_id == "cpu_hotspot_recursive")
        cpu_with = next(c for c in candidates_with_prior if c.candidate_id == "cpu_hotspot_recursive")
        assert cpu_with.rule_score > cpu_no.rule_score


# ── 置信度校准 ──


class TestCalibrator:
    """校准器。"""

    def test_calibration_outputs_confidence(self):
        ev = EvidenceInput(task_metadata={"duration_sec": 15},
                           top_functions=[{"name": "f", "samples": 100, "percent": 80.0}])
        candidates = [CandidateCause(
            candidate_id="cpu_hotspot_recursive",
            description="CPU hotspot",
            evidence_refs=["top_functions[0]"],
            rule_score=0.83,
        )]
        calibrated = calibrate(candidates, ev)
        assert len(calibrated) == 1
        assert 0.0 <= calibrated[0].final_confidence <= 1.0

    def test_confidence_interpretation(self):
        assert interpret_confidence(0.85) == "高置信"
        assert interpret_confidence(0.70) == "可能"
        assert interpret_confidence(0.50) == "待验证"
        assert interpret_confidence(0.30) == "证据不足"


class TestControlledAITreeMerge:
    def test_compact_review_moves_rejected_primary_and_promotes_next_candidate(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "cartservice.GetCart", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 91.0}},
        )
        original_primary = "lock_contention"
        next_candidate = "retry_backoff"
        tree = ControlledAITree(
            tree_id="compact_backtrack_tree",
            final_supported_level="function",
            layers=[AITreeLayer(
                layer_id="layer_0",
                depth=0,
                primary_causes=[AITreeCandidateNode(
                    candidate_id=original_primary,
                    role="primary",
                    claim="锁竞争候选。",
                    supported_level="function",
                    status="supported",
                    evidence_refs=["top_functions"],
                )],
                secondary_causes=[AITreeCandidateNode(
                    candidate_id=next_candidate,
                    role="secondary",
                    claim="重试退避候选。",
                    supported_level="function",
                    status="supported",
                    evidence_refs=["top_functions"],
                )],
            )],
            final_primary_causes=[original_primary],
            final_secondary_causes=[next_candidate],
        )
        candidate_ids = [original_primary, next_candidate]
        review = {
            "tree_id": tree.tree_id,
            "primary": [next_candidate],
            "secondary": [],
            "rejected": [original_primary],
            "unknown": [item for item in candidate_ids if item not in {original_primary, next_candidate}],
            "probe_requests": ["cpu_profile"],
            "candidate_updates": {
                next_candidate: {
                    "claim": "重试退避持续占用请求窗口并造成接口延迟。",
                    "claim_type": "likely_root_cause",
                    "causal_status": "supported",
                    "decision": "conclude",
                    "mechanism": "retry_backoff_loop",
                    "target": "cartservice.GetCart",
                },
                original_primary: {
                    "claim_type": "insufficient_for_root_cause",
                    "causal_status": "contradicted",
                    "decision": "reject_candidate",
                },
            },
            "self_challenges": {
                next_candidate: {
                    "supporting_evidence_refs": ["top_functions"],
                    "opposing_evidence_refs": [],
                },
            },
        }

        reviewed = _apply_compact_guard_review(
            raw=json.dumps(review),
            analyzer_tree=tree,
            evidence=evidence,
            probe_manifest=build_probe_manifest(),
        )

        assert reviewed.final_primary_causes == [next_candidate]
        assert original_primary in reviewed.final_rejected_causes
        assert any(
            edge.transition_type == "backtrack" and edge.probe_requests == ["cpu_profile"]
            for edge in reviewed.probe_edges
        )
        assert any(
            node.candidate_id == original_primary and node.causal_status == "contradicted"
            for layer in reviewed.layers
            for node in layer.rejected_causes
        )

    """受控 AI 树只允许 LLM 改写解释，不允许越界裁决。"""

    def test_llm_can_enrich_controlled_tree_explanations(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        evidence.analysis_result = analysis.model_dump(mode="json")
        proposed_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        proposed_primary = proposed_tree.layers[0].primary_causes[0]
        proposed_tree.layers[0].primary_causes[0] = proposed_primary.model_copy(update={
            "claim": "AI 提议：热点集中在 compute_hotspot，当前只支持函数层定位。",
            "self_challenge": proposed_primary.self_challenge.model_copy(update={
                "why_this_claim": "CPU 与 top function 证据同向支持。",
                "why_not_other_claims": "IO wait 证据没有同向升高。",
            }),
        })
        report = DiagnosisReport(
            summary="CPU hotspot",
            ranked_causes=[CauseEntry(
                cause_id="cpu_hotspot_recursive",
                confidence=0.8,
                claim="CPU hotspot",
                evidence_refs=["top_functions[0]"],
            )],
            facts=["compute_hotspot 72%"],
            controlled_ai_tree=proposed_tree,
        )

        attached = _attach_analysis_result(report, evidence)

        assert attached.controlled_ai_tree.layers[0].primary_causes[0].claim.startswith("AI 提议")
        assert attached.controlled_ai_tree.final_primary_causes == analysis.controlled_ai_tree.final_primary_causes
        assert attached.controlled_ai_tree.probe_edges == analysis.controlled_ai_tree.probe_edges

    def test_llm_controlled_tree_cannot_promote_level_or_add_refs(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        evidence.analysis_result = analysis.model_dump(mode="json")
        unsafe_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        unsafe_primary = unsafe_tree.layers[0].primary_causes[0]
        unsafe_tree.layers[0].primary_causes[0] = unsafe_primary.model_copy(update={
            "supported_level": "line",
            "claim": "越权提升到代码行。",
            "evidence_refs": ["evidence_index.fake_line[0]"],
        })
        report = DiagnosisReport(
            summary="CPU hotspot",
            ranked_causes=[CauseEntry(
                cause_id="cpu_hotspot_recursive",
                confidence=0.8,
                claim="CPU hotspot",
                evidence_refs=["top_functions[0]"],
            )],
            facts=["compute_hotspot 72%"],
            controlled_ai_tree=unsafe_tree,
        )

        attached = _attach_analysis_result(report, evidence)

        assert attached.controlled_ai_tree == analysis.controlled_ai_tree

    def test_llm_controlled_tree_can_select_manifest_probe(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        manifest = build_probe_manifest()
        evidence = evidence.model_copy(update={
            "analysis_result": {
                **analysis.model_dump(mode="json"),
                "probe_registry_manifest": manifest,
            }
        })
        proposed_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        proposed_tree.probe_edges[0] = proposed_tree.probe_edges[0].model_copy(update={
            "probe_requests": ["redis_check"],
            "reason": "AI 选择 Redis 专项检查来反证下游依赖方向。",
        })
        mock_resp = mock.MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps(proposed_tree.model_dump(mode="json"))}}]
        }

        with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", return_value=mock_resp):
                tree = generate_controlled_ai_tree(
                    task_id="t1",
                    evidence=evidence,
                    analyzer_result=analysis,
                    probe_manifest=manifest,
                )

        assert tree.probe_edges[0].probe_requests == ["redis_check"]

    def test_llm_call_retries_with_openai_compatible_payload(self):
        bad_resp = mock.MagicMock(status_code=502, text="proxy reset")
        good_resp = mock.MagicMock(status_code=200)
        good_resp.json.return_value = {
            "choices": [{"message": {"content": "{\"ok\": true}"}}]
        }
        payloads = []

        def fake_chat(payload, timeout):
            payloads.append(payload)
            return bad_resp if len(payloads) == 1 else good_resp

        with mock.patch("server.app.rca.llm_client.chat_completions", side_effect=fake_chat):
            assert _call_deepseek([{"role": "user", "content": "json"}], "model") == "{\"ok\": true}"

        assert "thinking" in payloads[0]
        assert "response_format" in payloads[0]
        assert "thinking" not in payloads[1]
        assert "response_format" not in payloads[1]

    def test_compact_guard_review_marks_tree_ai_guarded_after_full_tree_rejected(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        manifest = build_probe_manifest()
        bad_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        bad_tree.probe_edges[0] = bad_tree.probe_edges[0].model_copy(update={
            "probe_requests": ["arbitrary_shell"],
        })
        compact_review = {
            "tree_id": analysis.controlled_ai_tree.tree_id,
            "primary": analysis.controlled_ai_tree.final_primary_causes,
            "secondary": [],
            "rejected": [],
            "unknown": [],
            "supporting_evidence_refs": ["top_functions"],
            "opposing_evidence_refs": [],
            "probe_requests": ["cpu_profile"],
            "stop_reason": "AI review 确认当前只能停在函数热点候选，需要继续补 CPU profile。",
            "self_challenges": {
                analysis.controlled_ai_tree.final_primary_causes[0]: {
                    "why_this_claim": "top_functions 显示 compute_hotspot 占比最高。",
                    "why_not_other_claims": "没有同等强度的 IO 或依赖证据。",
                    "supporting_evidence_refs": ["top_functions"],
                    "opposing_evidence_refs": [],
                    "missing_evidence": ["cpu_profile"],
                    "what_would_change_my_mind": "CPU profile 不再指向该热点。",
                },
            },
        }
        responses = []
        for payload in [bad_tree.model_dump(mode="json")] * 3 + [compact_review]:
            resp = mock.MagicMock(status_code=200)
            resp.json.return_value = {
                "choices": [{"message": {"content": json.dumps(payload)}}]
            }
            responses.append(resp)

        with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", side_effect=responses):
                tree = generate_controlled_ai_tree(
                    task_id="t1",
                    evidence=evidence,
                    analyzer_result=analysis,
                    probe_manifest=manifest,
                )

        assert all(layer.generated_by != "ai_guarded" for layer in tree.layers)
        assert all(
            node.claim_origin != "ai_update"
            for layer in tree.layers
            for node in [
                *layer.primary_causes,
                *layer.secondary_causes,
                *layer.rejected_causes,
                *layer.unknown_causes,
            ]
        )
        assert tree.stop_reason == compact_review["stop_reason"]

    def test_compact_guard_review_accepts_unstructured_ai_feedback_without_changing_candidates(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        bad_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        bad_tree.probe_edges[0] = bad_tree.probe_edges[0].model_copy(update={
            "probe_requests": ["arbitrary_shell"],
        })
        responses = []
        for payload in [bad_tree.model_dump(mode="json")] * 3:
            resp = mock.MagicMock(status_code=200)
            resp.json.return_value = {
                "choices": [{"message": {"content": json.dumps(payload)}}]
            }
            responses.append(resp)
        text_resp = mock.MagicMock(status_code=200)
        text_resp.json.return_value = {
            "choices": [{"message": {"content": "我会保留现有 CPU hotspot 候选，但需要更多 CPU profile 证据。"}}]
        }
        responses.append(text_resp)

        with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", side_effect=responses):
                tree = generate_controlled_ai_tree(
                    task_id="t1",
                    evidence=evidence,
                    analyzer_result=analysis,
                    probe_manifest=build_probe_manifest(),
                )

        assert all(layer.generated_by != "ai_guarded" for layer in tree.layers)
        assert all(
            node.generated_by != "ai"
            for layer in tree.layers
            for node in [
                *layer.primary_causes,
                *layer.secondary_causes,
                *layer.rejected_causes,
                *layer.unknown_causes,
            ]
        )
        assert tree.final_primary_causes == analysis.controlled_ai_tree.final_primary_causes

    def test_llm_controlled_tree_rejects_unregistered_probe_request(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        manifest = build_probe_manifest()
        proposed_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        proposed_tree.probe_edges[0] = proposed_tree.probe_edges[0].model_copy(update={
            "probe_requests": ["arbitrary_shell"],
        })
        mock_resp = mock.MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps(proposed_tree.model_dump(mode="json"))}}]
        }

        with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", return_value=mock_resp):
                tree = generate_controlled_ai_tree(
                    task_id="t1",
                    evidence=evidence,
                    analyzer_result=analysis,
                    probe_manifest=manifest,
                )

        assert tree == analysis.controlled_ai_tree


# ── Prompt 模板 ──


class TestPromptTemplate:
    """System prompt 和 user message 生成。"""

    def test_prompt_contains_constraints(self):
        prompt = build_system_prompt()
        assert "硬性约束" in prompt
        assert "evidence_refs" in prompt
        assert "样例" in prompt

    def test_prompt_contains_few_shots(self):
        prompt = build_system_prompt()
        assert "fib_hotspot" in prompt  # shot 1
        assert "io_wait_high" in prompt  # shot 2
        assert "debuginfo" in prompt  # shot 3

    def test_prompt_duplicates_constraints(self):
        """核心约束应在 prompt 头尾各出现一次，对抗遗忘。"""
        prompt = build_system_prompt()
        count = prompt.count("硬性约束")
        assert count >= 2

    def test_user_message_puts_evidence_at_end(self):
        msg = build_user_message('{"top":[]}', '[{"id":"x"}]')
        assert "当前证据" in msg
        assert "候选原因" in msg
        assert msg.index("当前证据") < msg.index("候选原因")

    def test_model_tag_in_prompt(self):
        prompt = build_system_prompt("deepseek-chat")
        assert "DeepSeek Chat" in prompt

    def test_flash_model_tag(self):
        prompt = build_system_prompt("deepseek-4-flash")
        assert "DeepSeek V4 Flash" in prompt


# ── JSON 提取 ──


class TestJsonExtraction:
    """从 LLM 原始输出提取 JSON。"""

    def test_extracts_plain_json(self):
        assert _extract_json('{"a":1}') == '{"a":1}'

    def test_extracts_from_code_block(self):
        raw = '```json\n{"summary":"test"}\n```'
        assert _extract_json(raw) == '{"summary":"test"}'

    def test_extracts_from_non_json_block(self):
        raw = '```\n{"x":1}\n```'
        assert _extract_json(raw) == '{"x":1}'

    def test_extracts_nested_json(self):
        raw = 'Some text\n{"ranked_causes":[{"cause_id":"c1"}]}\nMore text'
        result = _extract_json(raw)
        assert result is not None
        assert "ranked_causes" in result

    def test_no_json_returns_none(self):
        assert _extract_json("just plain text no json") is None


# ── 校验与解析 ──


class TestValidationAndParsing:
    """LLM 输出校验 + 自修复。"""

    def test_valid_report_passes(self):
        evidence = EvidenceInput(
            task_metadata={"duration_sec": 15},
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}],
            baseline_diff={"cpu_percent_delta": 42.0},
        )
        raw = json.dumps({
            "summary": "CPU 热点在 fib",
            "ranked_causes": [{
                "cause_id": "cpu_hotspot_recursive",
                "confidence": 0.85,
                "claim": "fib 导致高 CPU",
                "evidence_refs": ["top_functions[0]", "baseline_diff"],
                "uncertainties": [],
                "verification_steps": ["加入缓存重测"],
            }],
            "facts": ["fib 占 68.5%"],
            "not_enough_evidence": False,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is not None
        assert issues == []
        assert report.summary == "CPU 热点在 fib"

    def test_rejects_cause_outside_analysis_boundary(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}],
            analysis_result={
                "allowed_cause_ids": ["cpu_hotspot_recursive"],
                "conclusion_boundary": {"can_claim_root_cause": True},
                "primary_cause_id": "cpu_hotspot_recursive",
                "stability_score": 0.91,
                "primary_cause_reason": "function 层级更深、支持事实更多",
                "symptoms": [],
                "localizations": [],
                "attributions": [],
                "evidence_challenges": [],
            },
        )
        raw = json.dumps({
            "summary": "错误归因",
            "ranked_causes": [{
                "cause_id": "io_wait_high",
                "confidence": 0.8,
                "claim": "IO 等待是根因",
                "evidence_refs": ["top_functions[0]"],
                "uncertainties": [],
                "verification_steps": [],
            }],
            "facts": ["fib 占 68.5%"],
            "not_enough_evidence": False,
        })

        report, issues = _validate_and_parse(raw, evidence)

        assert report is None
        assert any("不在结构化分析允许的原因集合" in issue for issue in issues)

    def test_analysis_result_is_attached_to_report(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}],
            analysis_result={
                "allowed_cause_ids": ["cpu_hotspot_recursive"],
                "conclusion_boundary": {"can_claim_root_cause": True},
                "primary_cause_id": "cpu_hotspot_recursive",
                "stability_score": 0.91,
                "primary_cause_reason": "function 层级更深、支持事实更多",
                "symptoms": [{"symptom_id": "sym_cpu_utilization_high", "symptom_type": "cpu_utilization_high", "severity": "high", "fact_ids": ["fact_cpu_user_high"]}],
                "localizations": [{"level": "function", "target": "fib", "fact_ids": ["fact_top_function_0", "fact_cpu_user_high"]}],
                "attributions": [{"candidate_id": "cpu_hotspot_recursive", "status": "supported", "supporting_fact_ids": ["fact_cpu_user_high", "fact_top_function_0"], "opposing_fact_ids": [], "missing_evidence": [], "max_supported_level": "function"}],
                "evidence_challenges": [{"candidate_id": "cpu_hotspot_recursive", "critical_fact_ids": ["fact_cpu_user_high", "fact_top_function_0"], "critical_fact_groups": [["fact_cpu_user_high", "fact_top_function_0"]], "tests": [], "conclusion_stability": "stable"}],
            },
        )
        raw = json.dumps({
            "summary": "CPU 热点",
            "ranked_causes": [{
                "cause_id": "cpu_hotspot_recursive",
                "confidence": 0.85,
                "claim": "fib 导致高 CPU",
                "evidence_refs": ["top_functions[0]"],
                "uncertainties": [],
                "verification_steps": [],
            }],
            "facts": ["fib 占 68.5%"],
            "not_enough_evidence": False,
        })

        report, issues = _validate_and_parse(raw, evidence)

        assert report is not None
        assert issues == []
        assert report.analysis_result is not None
        assert report.primary_cause_id == "cpu_hotspot_recursive"
        assert report.stability_score == 0.91
        assert report.conclusion_boundary is not None
        assert report.ai_tree == []
        assert report.graph_entities == []
        assert report.graph_links == []
        assert report.graph_extension_points == []

    def test_requires_insufficient_flag_when_analysis_forbids_root_cause(self):
        evidence = EvidenceInput(
            analysis_result={
                "allowed_cause_ids": [],
                "conclusion_boundary": {"can_claim_root_cause": False},
            },
        )
        raw = json.dumps({
            "summary": "仍然给出结论",
            "ranked_causes": [],
            "facts": [],
            "not_enough_evidence": False,
        })

        report, issues = _validate_and_parse(raw, evidence)

        assert report is None
        assert any("必须标记为证据不足" in issue for issue in issues)

    def test_bad_evidence_ref_rejected(self):
        evidence = EvidenceInput()
        raw = json.dumps({
            "summary": "test",
            "ranked_causes": [{
                "cause_id": "x",
                "confidence": 0.5,
                "claim": "bad",
                "evidence_refs": ["nonexistent_field"],
                "uncertainties": [],
                "verification_steps": [],
            }],
            "facts": [],
            "not_enough_evidence": False,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is None
        assert len(issues) > 0
        assert "evidence_refs" in issues[0]

    def test_missing_required_fields_rejected(self):
        evidence = EvidenceInput()
        raw = '{"summary":"x"}'
        report, issues = _validate_and_parse(raw, evidence)
        assert report is None
        assert len(issues) > 0

    def test_invalid_json_rejected(self):
        evidence = EvidenceInput()
        report, issues = _validate_and_parse("not json", evidence)
        assert report is None

    def test_not_enough_evidence_without_causes_is_ok(self):
        evidence = EvidenceInput()
        raw = json.dumps({
            "summary": "insufficient data",
            "ranked_causes": [],
            "facts": ["few samples"],
            "not_enough_evidence": True,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is not None
        assert issues == []

    def test_empty_causes_without_flag_fails(self):
        evidence = EvidenceInput()
        raw = json.dumps({
            "summary": "empty",
            "ranked_causes": [],
            "facts": [],
            "not_enough_evidence": False,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is None

    def test_ref_exists_in_evidence(self):
        paths = {"top_functions": {"name", "samples", "percent"}}
        assert _ref_exists("top_functions[0]", paths) is True
        assert _ref_exists("top_functions", paths) is True
        assert _ref_exists("nonexistent", paths) is False

    def test_tool_result_ref_is_valid(self):
        evidence = EvidenceInput(
            tool_results=[{
                "tool_name": "get_flamegraph_top",
                "status": "success",
                "evidence_ref": "tool_results.get_flamegraph_top",
                "output": {},
            }]
        )
        raw = json.dumps({
            "summary": "tool evidence",
            "ranked_causes": [{
                "cause_id": "cpu_hotspot_recursive",
                "confidence": 0.7,
                "claim": "hotspot",
                "evidence_refs": ["tool_results.get_flamegraph_top"],
                "uncertainties": [],
                "verification_steps": [],
            }],
            "facts": ["tool ok"],
            "not_enough_evidence": False,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is not None
        assert issues == []


# ── 工具证据与修复计划 ──


class _StubRepo:
    def __init__(self):
        self.created_payloads = []

    def create_task(self, payload):
        self.created_payloads.append(payload)

        class _Task:
            id = "task_followup"

        return _Task()


class TestToolAndRepairFlow:
    """工具证据链和 safe_auto 修复动作。"""

    def test_tool_results_are_structured_evidence(self):
        tools = run_rca_tools(
            task_record=_StubTask(),
            top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
        )
        flame_tool = next(item for item in tools if item.tool_name == "get_flamegraph_top")
        assert flame_tool.status == "success"
        assert flame_tool.evidence_ref == "tool_results.get_flamegraph_top"

    def test_context_builds_and_executes_safe_followup(self):
        stub_repo = _StubRepo()
        with mock.patch.dict("os.environ", {}, clear=True):
            outcome = run_diagnosis_context(
                task_id="task_test",
                task_record=_StubTask(),
                top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
                repo=stub_repo,
            )
        assert outcome.report.report.ranked_causes[0].cause_id == "cpu_hotspot_recursive"
        assert outcome.repair_plan is not None
        assert outcome.repair_plan.status == "safe_actions_executed"
        assert stub_repo.created_payloads[0].collector_type == "pyspy"


# ── 自修复重试 ──


class TestSelfRepair:
    """LLM 调用失败后的自修复重试。"""

    def test_retries_on_validation_failure(self):
        """首次返回 bad JSON → 重试返回 good JSON。"""
        evidence = EvidenceInput(
            task_metadata={"duration_sec": 15},
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}],
            baseline_diff={"cpu_percent_delta": 42.0},
        )
        good_json = json.dumps({
            "summary": "after repair",
            "ranked_causes": [{
                "cause_id": "cpu_hotspot_recursive",
                "confidence": 0.80,
                "claim": "fixed",
                "evidence_refs": ["top_functions[0]"],
                "uncertainties": [],
                "verification_steps": ["step"],
            }],
            "facts": ["f1"],
            "not_enough_evidence": False,
        })

        # mock: 第一次返回坏 JSON → 第二次返回好 JSON
        mock_resp_bad = mock.MagicMock(status_code=200)
        mock_resp_bad.json.return_value = {
            "choices": [{"message": {"content": '{"summary":"bad","ranked_causes":[]}'}}]
        }
        mock_resp_good = mock.MagicMock(status_code=200)
        mock_resp_good.json.return_value = {
            "choices": [{"message": {"content": good_json}}]
        }

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", side_effect=[mock_resp_bad, mock_resp_good]):
                from server.app.rca.llm_client import diagnose
                result = diagnose(
                    task_id="t1",
                    evidence=evidence,
                    candidates_json='[{"candidate_id":"cpu_hotspot_recursive"}]',
                )
                assert result.validated is True
                assert result.retry_count == 1
                assert result.report.summary == "after repair"

    def test_max_retries_exceeded_returns_failure(self):
        evidence = EvidenceInput()
        bad_json = '{"summary":"x","ranked_causes":[],"facts":[],"not_enough_evidence":false}'

        mock_resp = mock.MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": bad_json}}]
        }

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", return_value=mock_resp):
                from server.app.rca.llm_client import diagnose
                result = diagnose(
                    task_id="t1",
                    evidence=evidence,
                    candidates_json="[]",
                )
                assert result.validated is False
                assert result.retry_count >= 1


# ── 降级行为 ──


class TestFallback:
    """API Key 未配置时的降级。"""

    def test_no_api_key_returns_rule_only(self):
        evidence = EvidenceInput(task_metadata={"collector_type": "perf_cpu"})
        with mock.patch.dict("os.environ", {}, clear=True):
            from server.app.rca.llm_client import diagnose
            result = diagnose(task_id="t1", evidence=evidence, candidates_json="[]")
            assert result.model_name == "rule-engine-only"
            assert result.report.not_enough_evidence is True

    def test_run_diagnosis_with_no_key(self):
        task = _StubTask()
        with mock.patch.dict("os.environ", {}, clear=True):
            result = run_diagnosis(task_id="t1", task_record=task,
                                   top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}])
            assert result is not None
            assert result.model_name == "rule-engine-only"

    def test_run_diagnosis_does_not_promote_cpu_signal_without_hotspot(self):
        task = _StubTask()
        sys_metrics = {
            "sample_count": 10,
            "summary": {
                "avg_cpu_user_pct": 92.0,
                "avg_cpu_sys_pct": 5.0,
                "avg_cpu_iowait_pct": 1.0,
                "load1m": 8.0,
                "thread_count": 20,
                "thread_trend": "stable",
                "fd_count": 20,
                "fd_trend": "stable",
                "fd_max": 25,
                "vmrss_mb": 200,
                "vmrss_mb_max": 210,
                "ctx_nonvoluntary_rate": 10,
                "net_rx_kbps": 10,
                "net_tx_kbps": 10,
            },
        }
        with mock.patch.dict("os.environ", {}, clear=True):
            result = run_diagnosis(
                task_id="t1",
                task_record=task,
                sys_metrics=sys_metrics,
            )
        assert result.report.ranked_causes == []
        assert result.report.not_enough_evidence is True
