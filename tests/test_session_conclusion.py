from __future__ import annotations

import json

from server.app.diagnosis.session_conclusion import (
    apply_session_review,
    build_qualification_boundary,
    build_fallback_explanation,
    build_retained_conclusion,
    build_root_cause_clusters,
    collect_candidate_generation_gate_failures,
    collect_ai_gate_failures,
    derive_root_cause_clusters_from_ai_tree,
    classify_cluster_set,
    validate_session_review,
)
from server.app.rca.models import SessionConclusionReview
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
    failures = collect_ai_gate_failures(tree, valid_evidence_refs={"ev-rss"})
    assert failures[0]["candidate_id"] == "ai_candidate_unproven"
    assert failures[0]["failure_code"] == "eligibility_gate"
    assert derive_root_cause_clusters_from_ai_tree(tree, valid_evidence_refs={"ev-rss"}) == []


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
    assert explanation["root_cause_clusters"][0].cause_level == "direct_root_cause"


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

    assert explanation["root_cause_clusters"][0].qualification == "possible_root_cause"
    assert explanation["root_cause_clusters"][0].role == "independent"
    assert explanation["headline"] == assessment["diagnostic_claim"]
    assert explanation["retained_conclusion"]["claim"] == assessment["diagnostic_claim"]
    assert explanation["retained_conclusion"]["qualification"] == "possible_root_cause"
    assert explanation["formal_root_cause"] is None
    assert explanation["confidence_level"] == "中"
    assert explanation["abstained"] is True


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
    assert explanation["headline"] == tree["layers"][0]["unknown_causes"][0]["claim"]
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
        "causal_chain": [{"step_id": "step-1", "statement": "paymentservice failure propagated to checkoutservice", "evidence_refs": ["ev-dependency"]}],
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
