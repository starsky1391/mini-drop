from __future__ import annotations

import json

import server.app.diagnosis.session_conclusion as session_conclusion
from server.app.diagnosis.session_conclusion import (
    apply_session_review,
    build_qualification_boundary,
    build_fallback_explanation,
    build_retained_conclusion,
    build_root_cause_clusters,
    build_scenario_retained_conclusion,
    build_session_qualification,
    collect_candidate_generation_gate_failures,
    collect_ai_gate_failures,
    derive_localization_frontier_from_ai_tree,
    derive_root_cause_clusters_from_ai_tree,
    classify_cluster_set,
    validate_session_review,
)
from server.app.rca.models import RootCauseCluster, SessionConclusionReview
from server.app.rca.llm_client import generate_session_conclusion_review
from server.app.rca.llm_client import _build_session_review_payload
from server.app.rca.llm_client import _compact_evidence_item


def test_ai_gate_failure_is_exposed_and_not_derived_as_formal_cluster():
    tree = {
        "layers": [{
            "layer_id": "layer-0",
            "depth": 0,
            "unknown_causes": [{
                "candidate_id": "coarse",
                "generated_by": "fallback_observation",
                "relation": "root",
                "node_type": "cluster_root",
                "role": "unknown",
                "claim": "粗定位",
                "supported_level": "resource",
                "status": "unknown",
                "claim_type": "partial_localization",
                "causal_status": "unproven",
                "decision": "continue_probe",
            }],
        }, {
            "layer_id": "layer-1",
            "depth": 1,
            "unknown_causes": [{
                "candidate_id": "ai_candidate_unproven",
                "generated_by": "ai_candidate",
                "parent_candidate_ids": ["coarse"],
                "origin_parent_candidate_id": "coarse",
                "relation": "refinement",
                "node_type": "base_cause",
                "role": "primary",
                "claim": "候选",
                "supported_level": "process",
                "confidence": 0.4,
                "status": "missing_evidence",
                "claim_type": "likely_root_cause",
                "causal_status": "unproven",
                "decision": "continue_probe",
                "mechanism": "memory_retention",
                "target": "worker",
                "depth_kind": "base",
                "conclusion_eligible": False,
                "evidence_refs": ["ev-rss"],
                "self_challenge": {},
            }],
        }],
    }
    failures = collect_ai_gate_failures(
        tree,
        valid_evidence_refs={"ev-rss"},
        evidence_catalog=[{
            "evidence_id": "ev-rss",
            "observed_value": {
                "evidence_window": {
                    "timing_relation": "same_window",
                    "window_start": "2026-08-22T01:00:00Z",
                    "window_end": "2026-08-22T01:00:10Z",
                    "evidence_cohort_id": "cohort-1",
                },
            },
        }],
    )
    assert failures[0]["candidate_id"] == "ai_candidate_unproven"
    assert failures[0]["failure_code"] == "eligibility_gate"
    assert failures[0]["gate_checks"]["source_is_ai"] is True
    assert failures[0]["gate_checks"]["evidence_refs_exist"] is True
    assert failures[0]["gate_checks"]["runtime_or_source_anchor"] is False
    assert "runtime_or_source_anchor" in failures[0]["failed_gates"]
    assert failures[0]["gate_checks"]["window"] is True
    assert failures[0]["gate_checks"]["causal_status"] is False
    assert "causal_status" in failures[0]["failed_gates"]
    assert derive_root_cause_clusters_from_ai_tree(tree, valid_evidence_refs={"ev-rss"}) == []


def test_verified_line_localization_reports_mechanism_and_source_relation_gaps():
    qualification = build_session_qualification(
        [],
        {
            "line_anchor_eligibility": {
                "status": "verified",
                "file": "requests/structures.py",
                "line": 83,
            },
            "layers": [{
                "unknown_causes": [{
                    "candidate_id": "coarse",
                    "generated_by": "analyzer_observation",
                    "relation": "root",
                    "node_type": "cluster_root",
                    "claim": "Python CPU 热点",
                    "supported_level": "resource",
                    "status": "unknown",
                }],
            }, {
                "unknown_causes": [{
                    "candidate_id": "ai-cpu#line",
                    "generated_by": "ai_candidate",
                    "relation": "refinement",
                    "node_type": "line_anchor",
                    "depth_kind": "base",
                    "parent_candidate_ids": ["ai-cpu"],
                    "origin_parent_candidate_id": "ai-cpu",
                    "claim": "requests/structures.py:83 的 CPU 热点",
                    "supported_level": "line",
                    "status": "missing_evidence",
                    "claim_type": "partial_localization",
                    "causal_status": "unproven",
                    "decision": "continue_probe",
                    "mechanism": "",
                    "target": "requests/structures.py:83",
                    "evidence_refs": ["ev-runtime", "ev-source"],
                }, {
                    "candidate_id": "ai-cpu",
                    "generated_by": "ai_candidate",
                    "relation": "refinement",
                    "node_type": "call_path_context",
                    "depth_kind": "base",
                    "parent_candidate_ids": ["coarse"],
                    "origin_parent_candidate_id": "coarse",
                    "claim": "Python 请求路径持续消耗 CPU",
                    "supported_level": "call_path",
                    "status": "missing_evidence",
                    "claim_type": "likely_root_cause",
                    "causal_status": "unproven",
                    "decision": "continue_probe",
                    "mechanism": "",
                    "target": "requests CPU call path",
                    "evidence_refs": ["ev-runtime"],
                }],
            }],
        },
        base={"evidence_refs": ["ev-runtime", "ev-source"]},
        retained_conclusion={"candidate_id": "ai-cpu", "claim": "Python 请求路径持续消耗 CPU"},
    )

    assert qualification["qualification"] == "partial_localization"
    assert qualification["supported_level"] == "line"
    assert {"mechanism", "verified_source_relation"} <= set(qualification["missing_evidence"])
    assert qualification["eligible_candidate_ids"] == []


def test_runtime_observed_line_remains_line_in_session_qualification():
    qualification = build_session_qualification(
        [],
        {
            "line_anchor_eligibility": {
                "status": "runtime_observed",
                "line_localization_status": "runtime_observed",
                "source_verification_status": "not_started",
                "file": "worker.py",
                "line": 42,
            },
            "layers": [{
                "unknown_causes": [{
                    "candidate_id": "coarse",
                    "generated_by": "analyzer_observation",
                    "relation": "root",
                    "node_type": "cluster_root",
                    "claim": "Python 运行时热点",
                    "supported_level": "process",
                    "status": "unknown",
                }],
            }, {
                "unknown_causes": [{
                    "candidate_id": "runtime-line",
                    "generated_by": "analyzer_observation",
                    "relation": "refinement",
                    "node_type": "line_anchor",
                    "depth_kind": "base",
                    "parent_candidate_ids": ["coarse"],
                    "origin_parent_candidate_id": "coarse",
                    "claim": "worker.py:42 已由工业采集器观察到。",
                    "supported_level": "line",
                    "status": "missing_evidence",
                    "claim_type": "partial_localization",
                    "causal_status": "unproven",
                    "decision": "continue_probe",
                    "evidence_refs": ["ev-runtime"],
                }],
            }],
        },
        base={"evidence_refs": ["ev-runtime"]},
        retained_conclusion={
            "candidate_id": "runtime-line",
            "claim": "worker.py:42 已由工业采集器观察到。",
            "supported_level": "line",
        },
    )

    assert qualification["qualification"] == "partial_localization"
    assert qualification["supported_level"] == "line"
    assert qualification["eligible_candidate_ids"] == []


def test_ai_gate_failure_marks_missing_window_and_parent_as_failed_gates():
    tree = {
        "layers": [{
            "layer_id": "layer-0",
            "depth": 0,
            "unknown_causes": [{
                "candidate_id": "ai_candidate_no_window",
                "generated_by": "ai_candidate",
                "relation": "refinement",
                "node_type": "base_cause",
                "role": "unknown",
                "claim": "候选",
                "supported_level": "process",
                "status": "missing_evidence",
                "claim_type": "likely_root_cause",
                "causal_status": "unproven",
                "decision": "continue_probe",
                "mechanism": "memory_retention",
                "target": "worker",
                "evidence_refs": ["ev-rss"],
                "self_challenge": {},
            }],
        }],
    }
    failures = collect_ai_gate_failures(
        tree,
        valid_evidence_refs={"ev-rss"},
        evidence_catalog=[{"evidence_id": "ev-rss", "observed_value": {}}],
    )
    assert failures[0]["gate_checks"]["window"] is False
    assert failures[0]["gate_checks"]["parent_exists"] is False
    assert {"window", "parent_exists"} <= set(failures[0]["failed_gates"])


def _ai_tree_node(
    candidate_id: str,
    *,
    parent: str,
    claim: str,
    level: str,
    mechanism: str,
    target: str,
    role: str = "unknown",
    eligible: bool = True,
    evidence_ref: str,
):
    return {
        "candidate_id": candidate_id,
        "generated_by": "ai_candidate",
        "parent_candidate_ids": [parent],
        "origin_parent_candidate_id": parent,
        "relation": "refinement",
        "node_type": "line_anchor" if level == "line" else "base_cause",
        "role": role,
        "claim": claim,
        "supported_level": level,
        "confidence": 0.82,
        "status": "supported" if eligible else "partial",
        "claim_type": "direct_root_cause" if eligible else "partial_localization",
        "causal_status": "supported" if eligible else "unproven",
        "decision": "conclude" if eligible else "continue_probe",
        "mechanism": mechanism,
        "target": target,
        "depth_kind": "base",
        "conclusion_eligible": eligible,
        "evidence_refs": [evidence_ref],
        "self_challenge": {"why_this_claim": claim},
    }


def _multi_branch_ai_tree(*nodes):
    return {
        "layers": [{
            "layer_id": "coarse",
            "depth": 0,
            "unknown_causes": [{
                "candidate_id": "coarse-root",
                "generated_by": "analyzer_observation",
                "relation": "root",
                "node_type": "cluster_root",
                "role": "unknown",
                "claim": "请求积压。",
                "supported_level": "resource",
                "status": "unknown",
            }],
        }, {
            "layer_id": "causes",
            "depth": 1,
            "unknown_causes": list(nodes),
        }],
    }


def test_ai_tree_keeps_distinct_sibling_causes_with_different_depths():
    tree = _multi_branch_ai_tree(
        _ai_tree_node(
            "line-cause",
            parent="coarse-root",
            claim="worker.py:42 的状态更新异常持续保留任务对象。",
            level="line",
            mechanism="task_state_retention",
            target="worker.py:42",
            role="primary",
            evidence_ref="ev-line",
        ),
        _ai_tree_node(
            "function-cause",
            parent="coarse-root",
            claim="结果回调函数的锁等待降低了任务释放速度。",
            level="function",
            mechanism="callback_lock_wait",
            target="backend.on_chord_part_return",
            evidence_ref="ev-function",
        ),
    )

    clusters = derive_root_cause_clusters_from_ai_tree(
        tree,
        valid_evidence_refs={"ev-line", "ev-function"},
    )

    assert len(clusters) == 2
    assert {cluster.supported_level for cluster in clusters} == {"line", "function"}
    assert [cluster.role for cluster in clusters] == ["primary", "contributing"]
    assert {cluster.source_tree_candidate_ids[0] for cluster in clusters} == {
        "line-cause",
        "function-cause",
    }


def test_ai_tree_collapses_formal_parent_when_same_branch_has_deeper_formal_child():
    parent = _ai_tree_node(
        "function-parent",
        parent="coarse-root",
        claim="任务状态更新函数持续保留对象。",
        level="function",
        mechanism="task_state_retention",
        target="update_state",
        role="primary",
        evidence_ref="ev-function",
    )
    child = _ai_tree_node(
        "line-child",
        parent="function-parent",
        claim="worker.py:42 的状态更新异常持续保留对象。",
        level="line",
        mechanism="task_state_retention",
        target="worker.py:42",
        role="primary",
        evidence_ref="ev-line",
    )
    tree = _multi_branch_ai_tree(parent)
    tree["layers"].append({"layer_id": "line", "depth": 2, "primary_causes": [child]})

    clusters = derive_root_cause_clusters_from_ai_tree(
        tree,
        valid_evidence_refs={"ev-function", "ev-line"},
    )

    assert [cluster.source_tree_candidate_ids for cluster in clusters] == [["line-child"]]
    assert clusters[0].supported_level == "line"


def test_ai_tree_exposes_deep_nonformal_sibling_as_localization_frontier():
    tree = _multi_branch_ai_tree(
        _ai_tree_node(
            "formal-line",
            parent="coarse-root",
            claim="worker.py:42 的状态更新异常持续保留任务对象。",
            level="line",
            mechanism="task_state_retention",
            target="worker.py:42",
            role="primary",
            evidence_ref="ev-line",
        ),
        _ai_tree_node(
            "partial-function",
            parent="coarse-root",
            claim="结果回调函数存在锁等待，但直接因果尚未闭合。",
            level="function",
            mechanism="callback_lock_wait",
            target="backend.on_chord_part_return",
            eligible=False,
            evidence_ref="ev-function",
        ),
    )

    frontier = derive_localization_frontier_from_ai_tree(
        tree,
        valid_evidence_refs={"ev-line", "ev-function"},
    )

    assert [(step.candidate_id, step.supported_level) for step in frontier] == [
        ("partial-function", "function"),
    ]


def test_session_review_requires_every_related_formal_branch_in_roles_and_chain():
    tree = _multi_branch_ai_tree(
        _ai_tree_node(
            "line-cause",
            parent="coarse-root",
            claim="worker.py:42 的状态更新异常持续保留任务对象。",
            level="line",
            mechanism="task_state_retention",
            target="worker.py:42",
            role="primary",
            evidence_ref="ev-line",
        ),
        _ai_tree_node(
            "function-cause",
            parent="coarse-root",
            claim="结果回调函数的锁等待降低了任务释放速度。",
            level="function",
            mechanism="callback_lock_wait",
            target="backend.on_chord_part_return",
            evidence_ref="ev-function",
        ),
    )
    clusters = derive_root_cause_clusters_from_ai_tree(
        tree,
        valid_evidence_refs={"ev-line", "ev-function"},
    )
    primary, contributing = clusters
    review = SessionConclusionReview.model_validate({
        "headline": "状态保留与回调锁等待共同造成任务积压。",
        "why_it_happened": "对象释放和结果回调同时受阻，使积压持续扩大。",
        "primary_cluster_id": primary.cluster_id,
        "cluster_roles": {primary.cluster_id: "primary"},
        "causal_chain": [{
            "step_id": "line-step",
            "candidate_id": "line-cause",
            "statement": primary.claim,
            "evidence_refs": ["ev-line"],
            "supported_level": "line",
        }],
    })

    issues = validate_session_review(review, clusters, {"ev-line", "ev-function"})

    assert "cluster_roles must cover every eligible cluster" in issues
    assert "causal_chain does not cover every primary/contributing cluster" not in issues

    review.cluster_roles[contributing.cluster_id] = "contributing"
    issues = validate_session_review(review, clusters, {"ev-line", "ev-function"})
    assert "causal_chain does not cover every primary/contributing cluster" in issues


def test_fallback_keeps_all_related_formal_branches():
    tree = _multi_branch_ai_tree(
        _ai_tree_node(
            "line-cause",
            parent="coarse-root",
            claim="worker.py:42 的状态更新异常持续保留任务对象。",
            level="line",
            mechanism="task_state_retention",
            target="worker.py:42",
            role="primary",
            evidence_ref="ev-line",
        ),
        _ai_tree_node(
            "function-cause",
            parent="coarse-root",
            claim="结果回调函数的锁等待降低了任务释放速度。",
            level="function",
            mechanism="callback_lock_wait",
            target="backend.on_chord_part_return",
            evidence_ref="ev-function",
        ),
    )
    clusters = derive_root_cause_clusters_from_ai_tree(
        tree,
        valid_evidence_refs={"ev-line", "ev-function"},
    )

    explanation = build_fallback_explanation(clusters, {}, session_tree=tree)

    assert explanation["root_cause_clusters"] == []
    assert explanation["abstained"] is True
    assert len(explanation["possible_root_causes"]) == 2
    assert "worker.py:42" in explanation["headline"]
    assert "结果回调函数" not in explanation["headline"]
    assert explanation["causal_chain"] == []


def test_candidate_generation_failure_is_exposed_with_initial_evidence():
    failures = collect_candidate_generation_gate_failures({
        "ai_review_status": "failed",
        "ai_review_error": "AI 未生成任何通过结构校验的候选",
        "candidate_generation_attempts": [{
            "attempt": 1,
            "status": "failed",
            "parsed_candidate_count": 1,
            "accepted_candidate_count": 0,
        }],
        "validation_diagnostics": [{
            "candidate_id": "ai_candidate_bad",
            "failure_code": "invalid_evidence_ref",
            "failure_path": "candidates[0].evidence_refs",
            "actual_value": ["ev-missing"],
            "candidate_evidence_refs": ["ev-missing"],
            "initial_evidence_refs": ["ev-rss"],
            "missing_initial_evidence_refs": ["ev-missing"],
        }],
        "initial_evidence_context": {
            "evidence_refs": ["ev-rss"],
            "evidence_snapshots": {"ev-rss": {"family": "memory_smaps", "status": "valid"}},
        },
    })

    assert failures[0]["failure_code"] == "invalid_evidence_ref"
    assert failures[0]["initial_evidence_refs"] == ["ev-rss"]
    assert failures[1]["failure_code"] == "no_usable_ai_candidate"
    assert failures[1]["attempts"][0]["accepted_candidate_count"] == 0


def test_compact_source_evidence_keeps_enclosing_source_text():
    compact = _compact_evidence_item({
        "evidence_id": "ev-source",
        "source_type": "derived_artifact",
        "observed_value": {
            "producer": "git+universal-ctags",
            "revision": "a220671d",
            "source_context_hash": "sha256:verified",
            "snippets": [],
            "enclosing_contexts": [{
                "file": "src/werkzeug/routing.py",
                "symbol": "BuilderCompiler",
                "kind": "class",
                "start_line": 828,
                "end_line": 1121,
                "lines": [
                    {"line": 829, "text": "JOIN_EMPTY = ''.join"},
                    {"line": 1109, "text": "tuple(self.consts)"},
                    {"line": 1119, "text": "co = types.CodeType(*code_args)"},
                ],
            }],
        },
    })

    source = compact["observed_value"]["enclosing_contexts"][0]["source"]
    assert "JOIN_EMPTY" in source
    assert "tuple(self.consts)" in source
    assert "types.CodeType" in source


def test_compact_python_ast_source_evidence_keeps_verified_lines():
    compact = _compact_evidence_item({
        "evidence_id": "ev-source-ast",
        "source_type": "derived_artifact",
        "observed_value": {
            "producer": "git+python.ast",
            "revision": "a220671d",
            "source_context_hash": "sha256:verified",
            "verified_source_lines": [{
                "file": "src/app.py",
                "verified_line": 42,
                "line_localization_status": "verified",
                "evidence_role": "verified_source_line",
                "enclosing_symbol": "Worker.process",
            }],
        },
    })

    observed = compact["observed_value"]
    assert observed["producer"] == "git+python.ast"
    assert observed["verified_source_lines"][0]["verified_line"] == 42
    assert observed["verified_source_lines"][0]["enclosing_symbol"] == "Worker.process"


def test_compact_source_evidence_keeps_reference_paths():
    compact = _compact_evidence_item({
        "evidence_id": "ev-source",
        "source_type": "derived_artifact",
        "observed_value": {
            "producer": "git+universal-ctags",
            "reference_paths": [{
                "source_expression": "callback",
                "source_kind": "partial_callable",
                "upstream_candidates": [],
                "stored_via": "self.remember(...)",
                "container": "self.values",
                "sink": "types.CodeType(... tuple(self.values) ...)",
                "runtime_slot": "CodeType.co_consts",
                "retained_by": "types.FunctionType(code, {})",
                "retention_chain": ["callback", "self.remember(...)" , "self.values", "CodeType.co_consts", "FunctionType"],
                "source_lines": [{"line": 10, "text": "self.remember(callback)"}],
            }],
        },
    })

    path = compact["observed_value"]["reference_paths"][0]
    assert path["source_expression"] == "callback"
    assert path["container"] == "self.values"
    assert path["runtime_slot"] == "CodeType.co_consts"
    assert "FunctionType" in path["retained_by"]


def _observation(
    *,
    service_id: str,
    instance_id: str,
    pid: int,
    refs: list[str],
    cpu: bool = False,
    target_scheduling_pressure: bool = False,
    failed_dependencies: list[str] | None = None,
):
    return {
        "task_id": f"task-{instance_id}",
        "collector_type": "sys_metrics" if failed_dependencies is None else "dependency_check",
        "target": {
            "service_id": service_id,
            "instance_id": instance_id,
            "host_id": "worker2",
            "pid": pid,
            "evidence_cohort_id": "cohort-1",
        },
        "specific_anchor": {
            "instance_id": instance_id,
            "service_id": service_id,
            "pid": pid,
            "avg_cpu_user_pct": 96.0 if cpu else 0.0,
            "supported_level": "process",
        },
        "summary": {
            "avg_host_cpu_busy_pct": 96.0 if cpu else 30.0,
            "host_cpu_saturated": cpu,
            "target_scheduling_pressure": target_scheduling_pressure,
            "avg_cgroup_throttled_pct": 4.0 if target_scheduling_pressure else 0.0,
            "cgroup_nr_throttled_delta": 2 if target_scheduling_pressure else 0,
        },
        "pressure": {
            "cpu": cpu,
            "io_wait": False,
            "memory": False,
            "fd": False,
            "thread": False,
            "load": cpu,
            "runtime_stall": False,
        },
        "dependency": {
            "has_signal": failed_dependencies is not None,
            "failed_count": len(failed_dependencies or []),
            "failed_dependencies": failed_dependencies or [],
        },
        "redis": {"has_signal": False, "failed": False},
        "runtime_control": {},
        "confidence_inputs": {"evidence_cohort_id": "cohort-1"},
        "evidence_refs": refs,
    }


def _assessment(**updates):
    value = {
        "classification": "downstream_dependency",
        "claim_type": "root_cause",
        "causal_status": "supported",
        "conclusion_eligible": True,
        "mechanism": "downstream_dependency_failure",
        "claim_target": "paymentservice",
        "diagnostic_claim": "paymentservice 不可达导致 checkoutservice 请求失败。",
        "evidence_refs": ["ev-dependency"],
        "confidence": 0.88,
        "ruled_out": [],
    }
    value.update(updates)
    return value


def _runtime_pause_observation(*, refs: list[str], cpu: bool = False):
    observation = _observation(
        service_id="paymentservice",
        instance_id="payment-1",
        pid=22,
        refs=refs,
        cpu=cpu,
    )
    observation["target"]["container_id"] = "payment-container-123"
    observation["runtime_control"] = {
        "summary": {
            "has_direct_control_chain": True,
            "has_complete_source_chain": False,
            "origin_unknown": True,
        },
        "events": [{
            "event_type": "container_runtime_control",
            "observed_at": "2026-08-19T06:21:31Z",
            "actor": {"kind": "docker_daemon"},
            "action": {"operation": "pause"},
            "target": {
                "container_id": "payment-container-123",
                "service_id": "paymentservice",
            },
            "qualification": {
                "direct_control_chain": True,
                "exact_container_match": True,
            },
        }],
    }
    return observation


def test_duplicate_dependency_observations_merge_into_one_cluster():
    observations = [
        _observation(service_id="checkoutservice", instance_id="checkout-1", pid=11, refs=["ev-1"], failed_dependencies=["paymentservice"]),
        _observation(service_id="checkoutservice", instance_id="checkout-1", pid=11, refs=["ev-2"], failed_dependencies=["paymentservice"]),
    ]

    clusters = build_root_cause_clusters(observations, _assessment(), {"target_scope": {"target_service": "checkoutservice"}})

    assert len(clusters) == 1
    assert clusters[0].target == "paymentservice"
    assert set(clusters[0].evidence_refs) == {"ev-1", "ev-2"}


def test_python_scenario_gate_never_becomes_formal_root_cause_cluster():
    observation = _observation(
        service_id="checkoutservice",
        instance_id="checkout-1",
        pid=11,
        refs=["ev-structured"],
    )
    observation["evidence_index"] = {
        "python_scenario_gates": {
            "python_exception_profile": {
                    "family": "python_exception_profile",
                    "scenario_type": "python_exception_storm",
                    "evidence_status": "valid",
                    "max_supported_claim_type": "direct_failure_mechanism",
                    "conclusion_eligible": False,
                "line_verified": True,
                "mechanism_evidence_refs": ["ev-exception-profile"],
                "counter_evidence_refs": [],
                "eligibility_reason": "重复异常簇、源码 throw/log 行和同窗影响证据均已闭合。",
                "missing_evidence": [],
            }
        }
    }

    clusters = build_root_cause_clusters(
        [observation],
        _assessment(classification="insufficient_evidence", conclusion_eligible=False),
        {"target_scope": {"target_service": "checkoutservice"}},
    )

    assert clusters == []
    retained = build_scenario_retained_conclusion(
        [observation],
        None,
        target_service="checkoutservice",
    )
    assert retained is not None
    assert retained["qualification"] == "partial_localization"
    assert retained["causal_status"] == "inconclusive"


def test_python_scenario_gate_with_counter_evidence_stays_out_of_clusters():
    observation = _observation(
        service_id="checkoutservice",
        instance_id="checkout-1",
        pid=11,
        refs=["ev-structured"],
    )
    observation["confidence_inputs"] = {
        "python_scenario_gates": {
            "python_retry_timeout_profile": {
                "family": "python_retry_timeout_profile",
                "scenario_type": "python_retry_timeout",
                "max_supported_claim_type": "direct_root_cause",
                "conclusion_eligible": True,
                "line_verified": True,
                "mechanism_evidence_refs": ["ev-retry"],
                "counter_evidence_refs": ["ev-dependency"],
                "eligibility_reason": "下游依赖反证仍存在。",
            }
        }
    }

    clusters = build_root_cause_clusters(
        [observation],
        _assessment(classification="insufficient_evidence", conclusion_eligible=False, evidence_refs=[]),
        {"target_scope": {"target_service": "checkoutservice"}},
    )

    assert clusters == []


def test_valid_python_scenario_gate_can_be_retained_without_formal_cluster():
    observation = _observation(
        service_id="celery-eta-queue-case",
        instance_id="celery-worker-1",
        pid=11,
        refs=["ev-structured"],
    )
    observation["evidence_index"] = {
        "python_scenario_gates": {
            "python_queue_profile": {
                "family": "python_queue_profile",
                "scenario_type": "python_queue_backlog",
                "evidence_status": "valid",
                "max_supported_claim_type": "observation",
                "conclusion_eligible": False,
                "line_verified": False,
                "mechanism_evidence_refs": ["python_queue_profile.tasks[0]"],
                "counter_evidence_refs": [],
                "eligibility_reason": "场景证据尚未达到源码定位门禁。",
                "gate_checks": {
                    "industrial_observation": True,
                    "scenario_signal": True,
                },
                "missing_evidence": ["source_snapshot_verification"],
            }
        }
    }
    tree = {
        "layers": [{
            "primary_causes": [],
            "secondary_causes": [],
            "rejected_causes": [],
            "unknown_causes": [{
                "candidate_id": "ai-queue",
                "generated_by": "ai_candidate",
                "claim_origin": "ai_proposal",
                "claim_transform": "original",
                "claim_status": "active",
                "parent_candidate_ids": [],
                "child_candidate_ids": [],
                "relation": "alternative",
                "node_type": "base_cause",
                "role": "unknown",
                "claim": "Celery worker 进程在异常窗口内存在队列堆积，任务处理延迟由队列 backlog 导致。",
                "mechanism": "python_queue_backlog",
                "supported_level": "process",
                "status": "partial",
                "claim_type": "partial_localization",
                "causal_status": "unproven",
                "decision": "continue_probe",
                "evidence_refs": ["python_queue_profile.tasks[0]"],
            }],
        }],
    }

    clusters = build_root_cause_clusters(
        [observation],
        _assessment(classification="insufficient_evidence", conclusion_eligible=False),
        {"target_scope": {"target_service": "celery-eta-queue-case"}},
    )
    scenario_retained = build_scenario_retained_conclusion(
        [observation],
        tree,
        target_service="celery-eta-queue-case",
    )
    retained = build_retained_conclusion(
        clusters,
        {
            "classification": "insufficient_evidence",
            "scenario_retained_conclusion": scenario_retained,
        },
        tree,
    )

    assert clusters == []
    assert retained["candidate_id"] == "ai-queue"
    assert retained["qualification"] == "partial_localization"
    assert retained["causal_status"] == "inconclusive"
    assert "队列堆积" in retained["claim"]

    explanation = build_fallback_explanation(
        clusters,
        {
            "classification": "insufficient_evidence",
            "scenario_retained_conclusion": scenario_retained,
        },
        session_tree=tree,
        qualification_boundary={
            "status": "inconclusive",
            "message": "缺少源码行和机制闭合证据。",
            "missing_evidence": ["source_snapshot_verification"],
        },
    )

    assert explanation["formal_root_cause"] is None
    assert explanation["abstained"] is True
    assert "当前证据支持场景级定位" in explanation["headline"]
    assert "未形成正式源码根因" in explanation["headline"]
    assert "队列堆积" in explanation["headline"]
    assert explanation["why_it_happened"] == "缺少源码行和机制闭合证据。"


def test_abstained_scenario_headline_does_not_repeat_formal_root_boundary():
    observation = _observation(
        service_id="requests-cpu-case",
        instance_id="requests-worker-1",
        pid=11,
        refs=["python_cpu_profile.top_functions[0]"],
    )
    observation["evidence_index"] = {
        "python_scenario_gates": {
            "python_cpu_hotspot": {
                "family": "python_cpu_hotspot",
                "scenario_type": "python_cpu_hotspot",
                "status": "partial",
                "evidence_status": "valid",
                "confidence": 0.45,
                "max_supported_claim_type": "observation",
                "conclusion_eligible": False,
                "line_verified": False,
                "eligibility_reason": "缺少源码行和机制闭合证据，不能升级为正式根因。",
                "missing_evidence": ["source_snapshot_verification"],
                "evidence_refs": ["python_cpu_profile.top_functions[0]"],
                "mechanism_evidence_refs": ["python_cpu_profile.top_functions[0]"],
                "counter_evidence_refs": [],
            },
        }
    }
    tree = {
        "layers": [{
            "layer": 0,
            "primary_causes": [{
                "candidate_id": "scenario-python-cpu",
                "generated_by": "scenario_gate",
                "claim_status": "active",
                "parent_candidate_ids": [],
                "child_candidate_ids": [],
                "claim": (
                    "Python 场景证据显示 requests-cpu-case 存在 python_cpu_hotspot，"
                    "当前可作为场景级局部定位，但缺少源码行和机制闭合证据，不能升级为正式根因。"
                ),
                "mechanism": "python_cpu_hotspot",
                "supported_level": "process",
                "status": "partial",
                "claim_type": "partial_localization",
                "causal_status": "unproven",
                "evidence_refs": ["python_cpu_profile.top_functions[0]"],
            }],
        }],
    }

    scenario_retained = build_scenario_retained_conclusion(
        [observation],
        tree,
        target_service="requests-cpu-case",
    )
    retained = build_retained_conclusion(
        [],
        {
            "classification": "insufficient_evidence",
            "scenario_retained_conclusion": scenario_retained,
        },
        tree,
    )

    explanation = build_fallback_explanation(
        [],
        {
            "classification": "insufficient_evidence",
            "scenario_retained_conclusion": scenario_retained,
        },
        session_tree=tree,
        qualification_boundary={"message": "仍缺少源码行和机制闭合证据。"},
    )

    assert retained["candidate_id"] == "scenario-python-cpu"
    assert explanation["abstained"] is True
    assert explanation["headline"].startswith("当前证据支持场景级定位：")
    assert explanation["headline"].count("正式根因") == 1
    assert "未形成正式源码根因" not in explanation["headline"]


def test_dependency_and_same_host_cpu_form_two_eligible_clusters():
    observations = [
        _observation(
            service_id="checkoutservice",
            instance_id="checkout-1",
            pid=11,
            refs=["ev-dependency"],
            failed_dependencies=["paymentservice"],
            target_scheduling_pressure=True,
        ),
        _observation(service_id="noise-generator", instance_id="noise-1", pid=22, refs=["ev-cpu"], cpu=True),
    ]
    session = {
        "target_scope": {
            "target_service": "checkoutservice",
            "same_host_instance_ids": ["noise-1"],
            "downstream_service_ids": ["paymentservice"],
        }
    }

    clusters = build_root_cause_clusters(observations, _assessment(), session)

    assert len(clusters) == 2
    assert all(cluster.conclusion_eligible for cluster in clusters)
    assert {cluster.mechanism for cluster in clusters} == {
        "downstream_dependency_failure",
        "same_host_cpu_contention",
    }
    assert classify_cluster_set(clusters) == "compound_incident"


def test_dependency_failure_and_same_window_pause_merge_into_one_cluster():
    observations = [
        _observation(
            service_id="checkoutservice",
            instance_id="checkout-1",
            pid=11,
            refs=["ev-dependency"],
            failed_dependencies=["paymentservice"],
        ),
        _runtime_pause_observation(refs=["ev-runtime-control"]),
    ]
    session = {
        "target_scope": {
            "target_service": "checkoutservice",
            "downstream_service_ids": ["paymentservice"],
        }
    }

    clusters = build_root_cause_clusters(observations, _assessment(), session)

    assert len(clusters) == 1
    assert clusters[0].mechanism == "process_suspended"
    assert clusters[0].target == "paymentservice"
    assert clusters[0].cause_level == "direct_root_cause"
    assert set(clusters[0].evidence_refs) == {"ev-dependency", "ev-runtime-control"}
    assert "暂停" in clusters[0].claim
    assert classify_cluster_set(clusters) == "runtime_stall"


def test_dependency_pause_and_noise_cpu_form_exactly_two_eligible_clusters():
    observations = [
        _observation(
            service_id="checkoutservice",
            instance_id="checkout-1",
            pid=11,
            refs=["ev-dependency"],
            failed_dependencies=["paymentservice"],
        ),
        _runtime_pause_observation(refs=["ev-runtime-control"]),
        _observation(
            service_id="noise-generator",
            instance_id="noise-1",
            pid=33,
            refs=["ev-noise-cpu"],
            cpu=True,
        ),
    ]
    session = {
        "target_scope": {
            "target_service": "checkoutservice",
            "same_host_instance_ids": ["noise-1"],
            "downstream_service_ids": ["paymentservice"],
        }
    }

    clusters = build_root_cause_clusters(observations, _assessment(), session)
    eligible = [cluster for cluster in clusters if cluster.conclusion_eligible]

    assert len(eligible) == 2
    assert {(cluster.mechanism, cluster.target) for cluster in eligible} == {
        ("process_suspended", "paymentservice"),
        ("same_host_cpu_contention", "noise-1"),
    }
    assert all(cluster.target != "payment-1" for cluster in eligible)
    assert classify_cluster_set(clusters) == "compound_incident"


def test_observation_only_cluster_does_not_create_compound_incident():
    observations = [
        _observation(service_id="checkoutservice", instance_id="checkout-1", pid=11, refs=["ev-dependency"], failed_dependencies=["paymentservice"]),
        _observation(service_id="noise-generator", instance_id="noise-1", pid=22, refs=[], cpu=True),
    ]
    session = {"target_scope": {"target_service": "checkoutservice", "same_host_instance_ids": ["noise-1"]}}

    clusters = build_root_cause_clusters(observations, _assessment(), session)

    assert len([item for item in clusters if item.conclusion_eligible]) == 1
    assert classify_cluster_set(clusters) == "downstream_dependency"


def test_apply_session_review_sets_primary_and_contributing_roles():
    observations = [
        _observation(
            service_id="checkoutservice",
            instance_id="checkout-1",
            pid=11,
            refs=["ev-dependency"],
            failed_dependencies=["paymentservice"],
            target_scheduling_pressure=True,
        ),
        _observation(service_id="noise-generator", instance_id="noise-1", pid=22, refs=["ev-cpu"], cpu=True),
    ]
    session = {"target_scope": {"target_service": "checkoutservice", "same_host_instance_ids": ["noise-1"]}}
    clusters = build_root_cause_clusters(observations, _assessment(), session)
    dependency = next(item for item in clusters if item.mechanism == "downstream_dependency_failure")
    cpu = next(item for item in clusters if item.mechanism == "same_host_cpu_contention")
    review = {
        "headline": "支付依赖暂停是主因，宿主机 CPU 噪声放大了延迟",
        "why_it_happened": "checkoutservice 无法完成 paymentservice 调用，同时 CPU 竞争减少了可用调度时间。",
        "primary_cluster_id": dependency.cluster_id,
        "cluster_roles": {dependency.cluster_id: "primary", cpu.cluster_id: "contributing"},
        "causal_chain": [{"step_id": "step-1", "statement": "paymentservice 不可达阻断支付调用。", "evidence_refs": ["ev-dependency"]}],
        "ruled_out_summary": [],
        "residual_unknowns": [],
        "recommendations": {},
    }

    explanation = apply_session_review(clusters, review)

    assert [item.role for item in explanation["root_cause_clusters"]] == ["primary", "contributing"]
    assert explanation["classification"] == "compound_incident"


def test_session_review_cannot_promote_independent_cpu_anomaly_to_contributing():
    observations = [
        _observation(service_id="checkoutservice", instance_id="checkout-1", pid=11, refs=["ev-dependency"], failed_dependencies=["paymentservice"]),
        _observation(service_id="noise-generator", instance_id="noise-1", pid=22, refs=["ev-cpu"], cpu=True),
    ]
    clusters = build_root_cause_clusters(
        observations,
        _assessment(),
        {"target_scope": {"target_service": "checkoutservice", "same_host_instance_ids": ["noise-1"]}},
    )
    dependency = next(item for item in clusters if item.mechanism == "downstream_dependency_failure")
    cpu = next(item for item in clusters if item.mechanism == "same_host_cpu_contention")
    review = SessionConclusionReview.model_validate({
        "headline": "支付依赖失败是主因",
        "why_it_happened": "依赖不可达阻断请求，CPU 异常是否影响目标仍未知。",
        "primary_cluster_id": dependency.cluster_id,
        "cluster_roles": {dependency.cluster_id: "primary", cpu.cluster_id: "contributing"},
        "causal_chain": [{"step_id": "step-1", "statement": "依赖失败。", "evidence_refs": ["ev-dependency"]}],
        "recommendations": {},
    })

    issues = validate_session_review(review, clusters, {"ev-dependency", "ev-cpu"})

    assert any("lacks target-impact evidence" in issue for issue in issues)


def test_fallback_explanation_is_explicit_and_keeps_unknowns():
    clusters = build_root_cause_clusters([], _assessment(
        classification="runtime_stall",
        claim_type="direct_root_cause",
        mechanism="process_suspended",
        claim_target="paymentservice:1234",
        diagnostic_claim="SIGSTOP 直接暂停了进程，但发起来源未知。",
        origin_unknown=True,
        evidence_refs=["ev-runtime", "ev-metrics"],
    ), {"target_scope": {"target_service": "paymentservice"}})

    explanation = build_fallback_explanation(clusters, _assessment(origin_unknown=True))

    assert explanation["ai_review_status"] == "fallback"
    assert "来源" in " ".join(explanation["residual_unknowns"])
    assert explanation["root_cause_clusters"] == []
    assert explanation["formal_root_cause"] is None
    assert explanation["abstained"] is True


def test_ineligible_memory_fallback_is_possible_cause_not_confirmed_root():
    assessment = _assessment(
        classification="python_memory_retention",
        claim_type="direct_failure_mechanism",
        mechanism="python_memory_retention",
        claim_target="src/werkzeug/routing.py:844",
        diagnostic_claim="路由构建期间形成的代码常量持有链可能造成对象持续保留。",
        conclusion_eligible=False,
        confidence=0.84,
        confidence_level="高",
        evidence_refs=["ev-memray", "ev-source"],
    )
    clusters = build_root_cause_clusters([], assessment, {"target_scope": {"target_service": "werkzeug-routing"}})

    explanation = build_fallback_explanation(clusters, assessment)

    assert explanation["root_cause_clusters"] == []
    assert explanation["possible_root_causes"][0]["qualification"] == "possible_root_cause"
    assert explanation["possible_root_causes"][0]["role"] == "independent"
    assert "未形成正式源码根因" in explanation["headline"]
    assert explanation["headline"] != assessment["diagnostic_claim"]
    assert explanation["retained_conclusion"]["claim"] == assessment["diagnostic_claim"]
    assert explanation["retained_conclusion"]["qualification"] == "possible_root_cause"
    assert explanation["formal_root_cause"] is None
    assert explanation["confidence_level"] == "低"
    assert explanation["abstained"] is True


def test_semiclosed_verified_line_is_formal_root_with_mechanism_boundary():
    cluster = RootCauseCluster(
        cluster_id="rc_cluster_ai_line",
        candidate_ids=["ai_candidate_line"],
        source_tree_candidate_ids=["ai_candidate_line"],
        role="primary",
        causal_status="primary",
        cause_level="direct_root_cause",
        supported_level="line",
        mechanism="python_failure_handling_cpu",
        target="celery/app/trace.py:651",
        claim="Celery failure handling is the verified CPU hotspot.",
        evidence_refs=["ev-runtime", "ev-source"],
        confidence=0.72,
        conclusion_eligible=True,
        qualification="confirmed_root_cause",
    )
    tree = {
        "semi_closed_root_cause_candidate_ids": ["ai_candidate_line"],
        "layers": [{
            "unknown_causes": [{
                "candidate_id": "ai_candidate_line",
                "generated_by": "ai_candidate",
                "node_type": "line_anchor",
                "depth_kind": "base",
                "role": "primary",
                "claim": "Celery failure handling is the verified CPU hotspot.",
                "supported_level": "line",
                "status": "supported",
                "claim_type": "direct_failure_mechanism",
                "causal_status": "supported",
                "decision": "conclude",
                "mechanism": "python_failure_handling_cpu",
                "target": "celery/app/trace.py:651",
                "evidence_refs": ["ev-runtime", "ev-source"],
            }],
        }],
    }

    qualification = build_session_qualification(
        [cluster],
        tree,
        base={"evidence_refs": ["ev-runtime", "ev-source"]},
        session_ai_review_status="budget_exhausted",
    )

    assert qualification["qualification"] == "formal_root_cause"
    assert qualification["decision"] == "conclude"
    assert qualification["eligible_candidate_ids"] == ["ai_candidate_line"]
    assert qualification["supported_level"] == "line"
    assert "source_mechanism_query" in qualification["missing_evidence"]


def test_fallback_retains_emitted_tree_candidate_instead_of_diagnostic_claim():
    assessment = _assessment(
        classification="python_memory_retention",
        diagnostic_claim="这段 Analyzer 文本不能直接成为正式根因。",
        conclusion_eligible=False,
        evidence_refs=["ev-rss"],
    )
    tree = {
        "retained_candidate_id": "fallback-memory",
        "layers": [{
            "layer_id": "base",
            "depth": 1,
            "unknown_causes": [
                {
                    "candidate_id": "fallback-memory",
                    "generated_by": "analyzer_fallback",
                    "node_type": "base_cause",
                    "depth_kind": "base",
                    "relation": "alternative",
                    "parent_candidate_ids": ["coarse"],
                    "origin_parent_candidate_id": "coarse",
                    "claim": "worker 异常任务压力导致 RSS 观察需要继续补证。",
                    "supported_level": "process",
                    "status": "missing_evidence",
                    "claim_type": "partial_localization",
                    "causal_status": "unproven",
                    "decision": "continue_probe",
                    "confidence": 0.3,
                    "evidence_refs": ["ev-rss"],
                },
                {
                    "candidate_id": "fallback-runtime",
                    "generated_by": "analyzer_fallback",
                    "node_type": "base_cause",
                    "depth_kind": "base",
                    "relation": "alternative",
                    "parent_candidate_ids": ["coarse"],
                    "origin_parent_candidate_id": "coarse",
                    "claim": "runtime 路径仍需补证。",
                    "supported_level": "process",
                    "status": "missing_evidence",
                    "claim_type": "partial_localization",
                    "causal_status": "unproven",
                    "decision": "continue_probe",
                    "confidence": 0.2,
                    "evidence_refs": ["ev-rss"],
                },
            ],
        }],
    }

    explanation = build_fallback_explanation(
        [],
        assessment,
        session_tree=tree,
    )

    assert explanation["retained_conclusion"]["candidate_id"] == "fallback-memory"
    assert explanation["retained_conclusion"]["claim"] == tree["layers"][0]["unknown_causes"][0]["claim"]
    assert "未形成正式源码根因" in explanation["headline"]
    assert explanation["headline"] != tree["layers"][0]["unknown_causes"][0]["claim"]
    assert explanation["formal_root_cause"] is None
    assert explanation["abstained"] is True


def test_blocked_deep_probe_retains_parent_claim_and_separates_boundary():
    tree = {
        "layers": [{
            "layer_id": "base",
            "depth": 1,
            "unknown_causes": [{
                "candidate_id": "worker-runtime-pressure",
                "node_type": "base_cause",
                "depth_kind": "base",
                "claim": "worker 异常处理路径与 RSS 增长同窗出现。",
                "supported_level": "function",
                "status": "supported",
                "confidence": 0.72,
                "evidence_refs": ["ev-stack", "ev-rss"],
            }, {
                "candidate_id": "heap-probe",
                "node_type": "stop_boundary",
                "depth_kind": "boundary",
                "parent_candidate_ids": ["worker-runtime-pressure"],
                "origin_parent_candidate_id": "worker-runtime-pressure",
                "claim": "heap 深探被阻断。",
                "supported_level": "function",
                "status": "blocked",
                "confidence": 0.1,
            }],
        }],
    }

    retained = build_retained_conclusion(
        [],
        {"classification": "python_memory_retention", "evidence_refs": ["ev-stack", "ev-rss"]},
        tree,
    )
    boundary = build_qualification_boundary(
        {},
        followup_requests=["python_heap_reference"],
        probes=[{"evidence_status": "blocked", "parameters": {"evidence_gap": "python_heap_reference"}}],
        origin_parent_candidate_id=retained["candidate_id"],
    )

    assert retained["candidate_id"] == "worker-runtime-pressure"
    assert retained["claim"] == "worker 异常处理路径与 RSS 增长同窗出现。"
    assert retained["qualification"] == "partial_localization"
    assert boundary["status"] == "blocked"
    assert boundary["origin_parent_candidate_id"] == "worker-runtime-pressure"
    assert "heap-probe" not in retained["claim"]


def test_source_line_boundary_explains_why_line_upgrade_did_not_start():
    boundary = build_qualification_boundary(
        {
            "supported_level": "function",
            "primary_anchor": {
                "supported_level": "function",
                "blocked_upgrade_reason": "缺少源码符号映射或行级采样证据。",
            },
        },
        origin_parent_candidate_id="worker-runtime-pressure",
    )

    assert boundary["status"] == "inconclusive"
    assert "source_context" in boundary["missing_evidence"]
    assert "line_level_profile" in boundary["missing_evidence"]
    assert "源码行探测未进入正式升级" in boundary["message"]


def test_localization_chain_deduplicates_claim_hash_and_renders_boundary_as_stop_step():
    session_tree = {
        "layers": [{
            "primary_causes": [],
            "secondary_causes": [],
            "rejected_causes": [],
            "unknown_causes": [
                {
                    "candidate_id": "parent",
                    "parent_candidate_ids": [],
                    "claim": "Worker CPU pressure",
                    "claim_status": "active",
                    "evidence_refs": ["ev-1"],
                },
                {
                    "candidate_id": "child",
                    "parent_candidate_ids": ["parent"],
                    "claim": "worker cpu pressure。",
                    "claim_status": "inherited",
                    "evidence_refs": ["ev-2"],
                },
                {
                    "candidate_id": "boundary",
                    "parent_candidate_ids": ["child"],
                    "claim": "",
                    "claim_status": "boundary",
                    "relation": "boundary",
                    "boundary_message": "深探失败，停止在父结论。",
                    "evidence_refs": [],
                },
            ],
        }],
    }

    chain = session_conclusion._build_localization_chain(
        session_tree,
        {"candidate_id": "boundary"},
    )

    assert [step.statement for step in chain] == [
        "Worker CPU pressure",
        "深探失败，停止在父结论。",
    ]
    assert chain[-1].step_kind == "boundary"


def test_empty_frozen_evidence_cannot_be_promoted_to_root_cause():
    assert classify_cluster_set([]) == "insufficient_evidence"
    assert build_retained_conclusion(
        [],
        {
            "classification": "insufficient_evidence",
            "evidence_refs": [],
            "conclusion_eligible": False,
        },
    ) is None


def test_child_contradiction_keeps_parent_as_retained_candidate():
    retained = build_retained_conclusion(
        [],
        {},
        {"layers": [{
            "layer_id": "base",
            "depth": 1,
            "unknown_causes": [{
                "candidate_id": "parent",
                "node_type": "base_cause",
                "depth_kind": "base",
                "claim": "父节点结论仍由同窗证据支持。",
                "supported_level": "process",
                "status": "supported",
                "confidence": 0.7,
            }, {
                "candidate_id": "child",
                "node_type": "call_path_context",
                "depth_kind": "base",
                "parent_candidate_ids": ["parent"],
                "origin_parent_candidate_id": "parent",
                "claim": "被反驳的子调用链。",
                "supported_level": "call_path",
                "status": "contradicted",
                "confidence": 0.2,
            }],
        }]},
    )

    assert retained["candidate_id"] == "parent"
    assert retained["claim"] == "父节点结论仍由同窗证据支持。"


def test_explicit_ai_tree_retained_candidate_precedes_analyzer_active_candidate():
    retained = build_retained_conclusion(
        [],
        {
            "active_retained_candidate_id": "analyzer-memory-hint",
            "evidence_refs": ["ev-ai"],
        },
        {
            "retained_candidate_id": "ai-runtime-direction",
            "layers": [{
                "layer_id": "ai",
                "depth": 1,
                "unknown_causes": [{
                    "candidate_id": "ai-runtime-direction",
                    "generated_by": "ai_candidate",
                    "node_type": "base_cause",
                    "depth_kind": "base",
                    "claim": "AI 选择的运行时方向仍需补证。",
                    "supported_level": "process",
                    "status": "missing_evidence",
                    "confidence": 0.45,
                    "evidence_refs": ["ev-ai"],
                    "parent_candidate_ids": ["coarse"],
                    "origin_parent_candidate_id": "coarse",
                }],
            }],
        },
    )

    assert retained["candidate_id"] == "ai-runtime-direction"
    assert retained["claim"] == "AI 选择的运行时方向仍需补证。"


def test_contradicted_previous_retained_claim_is_not_reused():
    retained = build_retained_conclusion(
        [],
        {},
        None,
        previous_retained={
            "candidate_id": "old-parent",
            "claim": "已经被直接反驳的旧结论。",
            "status": "contradicted",
        },
    )

    assert retained is None


def test_session_ai_review_rejects_unknown_cluster_and_evidence(monkeypatch):
    clusters = build_root_cause_clusters(
        [_observation(service_id="checkoutservice", instance_id="checkout-1", pid=11, refs=["ev-dependency"], failed_dependencies=["paymentservice"])],
        _assessment(),
        {"target_scope": {"target_service": "checkoutservice"}},
    )
    invalid = {
        "headline": "无效",
        "why_it_happened": "无效",
        "primary_cluster_id": "invented-cluster",
        "cluster_roles": {"invented-cluster": "primary"},
        "causal_chain": [{"step_id": "x", "statement": "无效", "evidence_refs": ["invented-ref"]}],
        "ruled_out_summary": [],
        "residual_unknowns": [],
        "recommendations": {},
    }
    monkeypatch.setattr("server.app.rca.llm_client._call_deepseek", lambda messages, model: json.dumps(invalid))
    monkeypatch.setattr("server.app.rca.llm_client.is_feature_enabled", lambda feature: True)

    result = generate_session_conclusion_review(
        diagnosis_id="diag-1",
        clusters=clusters,
        session_tree=None,
        evidence_catalog=[{"evidence_id": "ev-dependency"}],
        probe_manifest={"probes": []},
    )

    assert result["ai_review_status"] == "failed"
    assert result["review"] is None
    assert result["ai_review_attempts"] == 3


def test_session_ai_review_retry_includes_previous_rejected_output(monkeypatch):
    clusters = build_root_cause_clusters(
        [_observation(service_id="checkoutservice", instance_id="checkout-1", pid=11, refs=["ev-dependency"], failed_dependencies=["paymentservice"])],
        _assessment(),
        {"target_scope": {"target_service": "checkoutservice"}},
    )
    cluster = clusters[0]
    invalid = {"headline": "invalid"}
    valid = {
        "headline": "paymentservice dependency failure blocks checkoutservice",
        "why_it_happened": "checkoutservice waits for paymentservice, which was unreachable in the evidence window.",
        "primary_cluster_id": cluster.cluster_id,
        "cluster_roles": {cluster.cluster_id: "primary"},
        "causal_chain": [{"step_id": "step-1", "candidate_id": cluster.candidate_ids[0], "statement": "paymentservice failure propagated to checkoutservice", "evidence_refs": ["ev-dependency"]}],
        "ruled_out_summary": [],
        "residual_unknowns": [],
        "recommendations": {cluster.cluster_id: [
            {"recommendation_type": "investigation", "action": "Inspect paymentservice availability events.", "rationale": "Identify the direct failure source."},
            {"recommendation_type": "temporary_mitigation", "action": "Route checkout traffic away from the unavailable dependency.", "rationale": "Reduce current request failures."},
            {"recommendation_type": "permanent_fix", "action": "Add dependency health isolation and runtime control auditing.", "rationale": "Prevent recurrence and preserve provenance."},
        ]},
    }
    calls = []

    def fake_call(messages, model, **kwargs):
        calls.append(messages)
        return json.dumps(invalid if len(calls) == 1 else valid)

    monkeypatch.setattr("server.app.rca.llm_client._call_deepseek", fake_call)
    monkeypatch.setattr("server.app.rca.llm_client.is_feature_enabled", lambda feature: True)

    result = generate_session_conclusion_review(
        diagnosis_id="diag-repair",
        clusters=clusters,
        session_tree=None,
        evidence_catalog=[{"evidence_id": "ev-dependency"}],
        probe_manifest={"probes": []},
    )

    assert result["ai_review_status"] == "succeeded"
    assert result["ai_review_attempts"] == 2
    assert calls[1][-2] == {"role": "assistant", "content": json.dumps(invalid)}
    assert "未通过硬校验" in calls[1][-1]["content"]


def test_session_ai_review_normalizes_single_string_list_fields(monkeypatch):
    clusters = build_root_cause_clusters(
        [_observation(service_id="checkoutservice", instance_id="checkout-1", pid=11, refs=["ev-dependency"], failed_dependencies=["paymentservice"])],
        _assessment(),
        {"target_scope": {"target_service": "checkoutservice"}},
    )
    cluster = clusters[0]
    response = {
        "headline": "paymentservice failure blocks checkoutservice",
        "why_it_happened": "checkoutservice waits for paymentservice, which was unavailable in the evidence window.",
        "primary_cluster_id": cluster.cluster_id,
        "cluster_roles": {cluster.cluster_id: "primary"},
        "causal_chain": [{
            "step_id": "step-1",
            "candidate_id": cluster.candidate_ids[0],
            "statement": "paymentservice failure propagated to checkoutservice",
            "evidence_refs": ["ev-dependency"],
        }],
        "ruled_out_summary": "No stronger target-local cause was supported.",
        "residual_unknowns": "The internal source of the dependency failure remains unknown.",
        "recommendations": {cluster.cluster_id: [
            {"recommendation_type": "investigation", "action": "Inspect paymentservice runtime events.", "rationale": "Identify the failure source."},
            {"recommendation_type": "temporary_mitigation", "action": "Route traffic away from the unavailable instance.", "rationale": "Reduce request failures."},
            {"recommendation_type": "permanent_fix", "action": "Add dependency isolation and runtime auditing.", "rationale": "Prevent recurrence."},
        ]},
    }
    monkeypatch.setattr("server.app.rca.llm_client._call_deepseek", lambda messages, model, **kwargs: json.dumps(response))
    monkeypatch.setattr("server.app.rca.llm_client.is_feature_enabled", lambda feature: True)

    result = generate_session_conclusion_review(
        diagnosis_id="diag-normalize",
        clusters=clusters,
        session_tree=None,
        evidence_catalog=[{"evidence_id": "ev-dependency"}],
        probe_manifest={"probes": []},
    )

    assert result["ai_review_status"] == "succeeded"
    assert result["ai_review_attempts"] == 1
    assert result["review"].ruled_out_summary == ["No stronger target-local cause was supported."]
    assert result["review"].residual_unknowns == ["The internal source of the dependency failure remains unknown."]


def test_session_review_rejects_duplicate_explanation_and_generic_recommendations():
    clusters = build_root_cause_clusters(
        [_observation(service_id="checkoutservice", instance_id="checkout-1", pid=11, refs=["ev-dependency"], failed_dependencies=["paymentservice"])],
        _assessment(),
        {"target_scope": {"target_service": "checkoutservice"}},
    )
    cluster = clusters[0]
    repeated = "paymentservice 出现问题，需要人工确认。"
    review = SessionConclusionReview.model_validate({
        "headline": repeated,
        "why_it_happened": repeated,
        "primary_cluster_id": cluster.cluster_id,
        "cluster_roles": {cluster.cluster_id: "primary"},
        "causal_chain": [{"step_id": "step-1", "statement": repeated, "evidence_refs": ["ev-dependency"]}],
        "ruled_out_summary": [],
        "residual_unknowns": [],
        "recommendations": {
            cluster.cluster_id: [{
                "recommendation_type": "investigation",
                "action": "人工确认后处理。",
                "rationale": "继续观察。",
            }],
        },
    })

    issues = validate_session_review(review, clusters, {"ev-dependency"})

    assert any("must not duplicate" in issue for issue in issues)
    assert any("all three recommendation types" in issue for issue in issues)
    assert any("generic recommendation" in issue for issue in issues)


def test_session_review_payload_only_includes_bounded_referenced_evidence():
    clusters = build_root_cause_clusters(
        [_observation(
            service_id="checkoutservice",
            instance_id="checkout-1",
            pid=11,
            refs=["ev-dependency"],
            failed_dependencies=["paymentservice"],
        )],
        _assessment(),
        {"target_scope": {"target_service": "checkoutservice"}},
    )
    large_value = {
        "summary": {
            "detail": "x" * 10000,
            "nested": {"raw": "y" * 10000},
        },
        "records": [{"message": "z" * 10000} for _ in range(100)],
    }
    payload = _build_session_review_payload(
        diagnosis_id="diag-compact",
        eligible=clusters,
        tree_payload={"layers": []},
        evidence_catalog=[
            {"evidence_id": "ev-dependency", "source_type": "dependency_check", "observed_value": large_value},
            {"evidence_id": "ev-unreferenced", "source_type": "raw", "observed_value": large_value},
        ],
        probe_manifest={"probes": []},
    )

    serialized = json.dumps(payload, ensure_ascii=False)

    assert [item["evidence_id"] for item in payload["evidence_catalog"]] == ["ev-dependency"]
    assert "ev-unreferenced" not in serialized
    assert len(serialized) < 10000
