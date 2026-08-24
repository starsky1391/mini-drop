from server.app.diagnosis.attribution_engine import (
    build_attribution_graph,
    build_scenario_facts,
    qualify_attribution,
)
from server.app.diagnosis.attribution_models import ProbeManifestEntry
from server.app.diagnosis.session_conclusion import (
    apply_session_qualification,
    build_session_qualification,
)
from server.app.rca.models import RootCauseCluster
from server.app.diagnosis.probe_registry import build_probe_manifest


def _closed_graph(project: str = "app"):
    source_file = f"{project}/worker.py"
    return build_attribution_graph(
        evidence={
            "evidence_window": {
                "timing_relation": "same_window",
                "evidence_cohort_id": f"{project}-cohort",
            },
            "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
            "top_functions": [{
                "name": "worker",
                "file": source_file,
                "line": 42,
                "percent": 80.0,
                "samples": 80,
                "evidence_ref": "ev:runtime",
            }],
            "input": {"cardinality": 4096},
            "configuration": {"mode": "custom"},
            "latency": {"p95_ms": 900},
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 80.0}},
            "evidence_index": {
                "evidence_validity_by_family": {
                    "python_runtime_profile": "valid",
                    "source_snapshot": "valid",
                    "source_mechanism_query": "valid",
                },
            },
            "source_snapshot_json": {
                "revision": f"{project}-rev",
                "source_context_hash": f"sha256:{project}",
                "verified_line_candidates": [{
                    "file": source_file,
                    "line": 42,
                    "symbol": "worker",
                    "evidence_ref": "ev:source",
                    "eligibility_status": "verified",
                }],
            },
        },
        target={"service_id": project, "pid": 123},
        source_mechanism={
            "revision": f"{project}-rev",
            "query": {"query_id": f"query:{project}"},
            "mechanism_paths": [
                {
                    "status": "supported",
                    "statement": "primary propagation path",
                    "relations": ["propagates_to"],
                    "evidence_ref": "ev:source",
                    "nodes": [
                        {"file": source_file, "line": 42, "symbol": "worker"},
                        {"file": source_file, "line": 43, "symbol": "compute"},
                    ],
                },
                {
                    "status": "supported",
                    "statement": "alternative mechanism path",
                    "relations": ["explains"],
                    "evidence_ref": "ev:source",
                    "nodes": [
                        {"file": source_file, "line": 42, "symbol": "worker"},
                        {"file": source_file, "line": 44, "symbol": "fallback"},
                    ],
                },
            ],
        },
    )


def test_manifest_entries_expose_capability_boundaries():
    manifest = build_probe_manifest()
    assert manifest["schema_version"] == "2.0"
    entries = [
        ProbeManifestEntry.model_validate(item)
        for item in manifest["available_probes"]
    ]
    source = next(item for item in entries if item.probe_id == "process_source_snapshot")
    assert source.capability_role == ["observe", "source_relation"]
    assert "正式根因" in source.cannot_establish
    assert "source_line_candidate" in source.produces
    assert source.quality_gate["same_target_required"] is True
    assert source.quality_gate["source_line_not_required"] is False
    assert all("applicable_hypotheses" not in item for item in manifest["available_probes"])

    input_profile = next(
        item for item in entries if item.probe_id == "process_python_input_profile"
    )
    assert input_profile.capability_role == ["observe", "trigger_analysis"]
    assert input_profile.quality_gate == {
        "same_target_required": True,
        "same_window_required": True,
        "empty_window_is_invalid": True,
        "source_line_not_required": True,
        "checks": [
            "有效采样或结构化状态",
            "目标范围匹配",
            "同窗窗口",
        ],
        "role": "localization",
    }


def test_runtime_primitive_is_cost_center_not_root_cause():
    facts = build_scenario_facts({
        "evidence_window": {
            "timing_relation": "same_window",
            "evidence_cohort_id": "cohort-1",
        },
        "top_functions": [{
            "name": "poll",
            "percent": 91.0,
            "samples": 91,
            "evidence_ref": "ev:profile",
        }],
        "sys_metrics": {"summary": {"avg_cpu_iowait_pct": 12.0}},
        "evidence_index": {
            "evidence_validity_by_family": {"python_runtime_profile": "valid"},
        },
    })
    assert facts.cost_centers[0].primitive is True
    assert not facts.trigger_candidates
    assert "trigger" in qualify_attribution(
        build_attribution_graph(evidence={
            "top_functions": [{
                "name": "poll",
                "percent": 91.0,
                "samples": 91,
                "evidence_ref": "ev:profile",
            }],
            "sys_metrics": {"summary": {"avg_cpu_iowait_pct": 12.0}},
            "evidence_index": {
                "evidence_validity_by_family": {"python_runtime_profile": "valid"},
                "evidence_window": {
                    "timing_relation": "same_window",
                    "evidence_cohort_id": "cohort-1",
                },
            },
        })
    ).missing_evidence


def test_source_line_and_mechanism_are_relations_until_all_gates_exist():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {
                "timing_relation": "same_window",
                "evidence_cohort_id": "cohort-1",
            },
            "top_functions": [{
                "name": "Rule.compile",
                "file": "werkzeug/routing.py",
                "line": 768,
                "percent": 72.0,
                "evidence_ref": "ev:runtime",
            }],
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 82.0}},
            "evidence_index": {
                "evidence_validity_by_family": {
                    "python_runtime_profile": "valid",
                    "source_snapshot": "valid",
                },
            },
            "source_snapshot_json": {
                "verified_line_candidates": [{
                    "file": "werkzeug/routing.py",
                    "line": 768,
                    "symbol": "Rule.compile",
                    "evidence_ref": "ev:source",
                    "eligibility_status": "verified",
                }],
            },
        },
        target={"service_id": "web", "pid": 123},
    )
    assert graph.source_relations[0].relation == "calls"
    assert graph.source_relations[0].status == "verified"
    qualification = qualify_attribution(graph)
    assert qualification.level in {"L1", "L2"}
    assert qualification.qualification != "formal_root_cause"
    assert qualification.decision == "continue_probe"


def test_codeql_path_is_source_relation_not_runtime_proof():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {"timing_relation": "same_window"},
            "evidence_refs": ["ev:structured"],
            "top_functions": [{
                "name": "Rule.compile",
                "percent": 80.0,
                "evidence_ref": "ev:runtime",
            }],
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 80.0}},
            "evidence_index": {
                "evidence_validity_by_family": {"source_mechanism_query": "valid"},
            },
        },
        source_mechanism={
            "mechanism_paths": [{
                "status": "supported",
                "statement": "container write path",
                "relations": ["retains"],
                "evidence_ref": "ev:codeql",
                "nodes": [
                    {"file": "werkzeug/routing.py", "line": 768, "symbol": "Rule.compile"},
                    {"file": "werkzeug/routing.py", "line": 770, "symbol": "Map._rules"},
                ],
            }],
        },
    )
    assert any(item.relation == "retains" for item in graph.source_relations)
    qualification = qualify_attribution(graph)
    assert qualification.level == "L2"
    assert qualification.causal_status == "unproven"


def test_source_relations_keep_query_revision_and_explicit_anchors():
    graph = _closed_graph("pandas")

    line_relation = next(
        item for item in graph.source_relations
        if item.relation_id == "source:calls:pandas/worker.py:42"
    )
    mechanism_relation = next(
        item for item in graph.source_relations
        if item.relation_id == "source:mechanism:0:0"
    )

    assert line_relation.relation_type == "calls"
    assert line_relation.query_id == "source_snapshot.runtime_line_mapping"
    assert line_relation.source_revision == "pandas-rev"
    assert line_relation.target_anchor["file"] == "pandas/worker.py"
    assert line_relation.target_anchor["line"] == 42
    assert mechanism_relation.query_id == "query:pandas"
    assert mechanism_relation.source_revision == "pandas-rev"
    assert mechanism_relation.source_anchor["line"] == 42
    assert mechanism_relation.target_anchor["line"] == 43


def test_one_cost_center_can_keep_multiple_open_mechanisms():
    graph = _closed_graph("pandas")
    candidates = [
        {
            "candidate_id": "ai_candidate_cardinality",
            "generated_by": "ai_guarded",
            "claim": "高基数输入沿第一条源码路径放大计算成本。",
            "mechanism": "input_cardinality_amplification",
            "target": "worker",
            "supported_level": "line",
            "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
            "causal_status": "supported",
            "decision": "conclude",
            "parent_candidate_ids": ["cost:line:0"],
            "cost_center_refs": ["cost:line:0"],
            "trigger_refs": ["trigger:input"],
            "mechanism_refs": ["source:mechanism:0:0"],
            "impact_refs": ["impact:latency"],
            "source_relation_refs": ["source:mechanism:0:0"],
        },
        {
            "candidate_id": "ai_candidate_fallback",
            "generated_by": "ai_guarded",
            "claim": "配置分支沿第二条源码路径解释同一热点。",
            "mechanism": "configuration_branch_amplification",
            "target": "worker",
            "supported_level": "line",
            "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
            "causal_status": "supported",
            "decision": "conclude",
            "parent_candidate_ids": ["cost:line:0"],
            "cost_center_refs": ["cost:line:0"],
            "trigger_refs": ["trigger:configuration"],
            "mechanism_refs": ["source:mechanism:1:0"],
            "impact_refs": ["impact:latency"],
            "source_relation_refs": ["source:mechanism:1:0"],
        },
    ]

    qualification = qualify_attribution(
        graph,
        ai_candidate_ids=[item["candidate_id"] for item in candidates],
        ai_candidates=candidates,
    )

    assert qualification.qualification == "formal_root_cause"
    assert qualification.eligible_candidate_ids == [
        "ai_candidate_cardinality",
        "ai_candidate_fallback",
    ]


def test_same_mechanism_name_is_not_bound_to_a_project():
    for project in ("requests", "urllib3", "aiohttp"):
        graph = _closed_graph(project)
        candidate_id = f"ai_candidate_{project}_retry"
        qualification = qualify_attribution(
            graph,
            ai_candidate_ids=[candidate_id],
            ai_candidates=[{
                "candidate_id": candidate_id,
                "generated_by": "ai_guarded",
                "claim": f"{project} 的重试路径需要同窗源码关系验证。",
                "mechanism": "retry_backoff_amplification",
                "target": "worker",
                "supported_level": "line",
                "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
                "causal_status": "supported",
                "decision": "conclude",
                "parent_candidate_ids": ["cost:line:0"],
                "cost_center_refs": ["cost:line:0"],
                "trigger_refs": ["trigger:input"],
                "mechanism_refs": ["source:mechanism:0:0"],
                "impact_refs": ["impact:latency"],
                "source_relation_refs": ["source:mechanism:0:0"],
            }],
        )

        assert qualification.qualification == "formal_root_cause"
        assert qualification.eligible_candidate_ids == [candidate_id]


def test_complete_graph_without_ai_candidate_stays_a_mechanism_hypothesis():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {"timing_relation": "same_window"},
            "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
            "top_functions": [{
                "name": "worker",
                "percent": 80.0,
                "samples": 80,
                "evidence_ref": "ev:runtime",
            }],
            "input": {"cardinality": 4096},
            "latency": {"p95_ms": 900},
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 80.0}},
            "evidence_index": {
                "evidence_validity_by_family": {
                    "python_runtime_profile": "valid",
                    "source_snapshot": "valid",
                    "source_mechanism_query": "valid",
                },
            },
            "source_snapshot_json": {
                "verified_line_candidates": [{
                    "file": "app.py",
                    "line": 42,
                    "symbol": "worker",
                    "evidence_ref": "ev:source",
                    "eligibility_status": "verified",
                }],
            },
        },
        source_mechanism={
            "mechanism_paths": [{
                "status": "supported",
                "statement": "input propagates into the hot path",
                "relations": ["propagates_to"],
                "evidence_ref": "ev:source",
                "nodes": [
                    {"file": "app.py", "line": 42, "symbol": "worker"},
                    {"file": "app.py", "line": 43, "symbol": "hot_path"},
                ],
            }],
        },
    )
    qualification = qualify_attribution(graph)
    assert qualification.level == "L2"
    assert qualification.qualification == "mechanism_hypothesis"
    assert qualification.eligible_candidate_ids == []
    assert "eligible_ai_candidate" in qualification.missing_evidence


def test_explicit_ai_candidate_can_promote_a_closed_graph():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {"timing_relation": "same_window"},
            "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
            "top_functions": [{
                "name": "worker",
                "percent": 80.0,
                "samples": 80,
                "evidence_ref": "ev:runtime",
            }],
            "input": {"cardinality": 4096},
            "latency": {"p95_ms": 900},
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 80.0}},
            "evidence_index": {
                "evidence_validity_by_family": {
                    "python_runtime_profile": "valid",
                    "source_snapshot": "valid",
                    "source_mechanism_query": "valid",
                },
            },
            "source_snapshot_json": {
                "verified_line_candidates": [{
                    "file": "app.py",
                    "line": 42,
                    "symbol": "worker",
                    "evidence_ref": "ev:source",
                    "eligibility_status": "verified",
                }],
            },
        },
        source_mechanism={
            "mechanism_paths": [{
                "status": "supported",
                "statement": "input propagates into the hot path",
                "relations": ["propagates_to"],
                "evidence_ref": "ev:source",
                "nodes": [
                    {"file": "app.py", "line": 42, "symbol": "worker"},
                    {"file": "app.py", "line": 43, "symbol": "hot_path"},
                ],
            }],
        },
    )
    qualification = qualify_attribution(
        graph,
        ai_candidate_ids=["ai_candidate_worker"],
        ai_candidates=[{
            "candidate_id": "ai_candidate_worker",
            "generated_by": "ai_guarded",
            "claim": "输入基数沿源码调用路径放大了热点计算。",
            "mechanism": "input_cardinality_amplification",
            "target": "worker",
            "supported_level": "line",
            "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
            "causal_status": "supported",
            "decision": "conclude",
            "parent_candidate_ids": ["cost:function:0"],
            "trigger_refs": ["trigger:input"],
            "mechanism_refs": ["source:mechanism:0:0"],
            "impact_refs": ["impact:latency"],
            "source_relation_refs": ["source:mechanism:0:0"],
        }],
    )
    assert qualification.level == "L3"
    assert qualification.qualification == "formal_root_cause"
    assert qualification.eligible_candidate_ids == ["ai_candidate_worker"]


def test_candidate_id_without_ai_record_cannot_promote_a_closed_graph():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {"timing_relation": "same_window"},
            "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
            "top_functions": [{
                "name": "worker",
                "file": "app.py",
                "line": 42,
                "percent": 80.0,
                "samples": 80,
                "evidence_ref": "ev:runtime",
            }],
            "input": {"cardinality": 4096},
            "latency": {"p95_ms": 900},
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 80.0}},
            "evidence_index": {
                "evidence_validity_by_family": {
                    "python_runtime_profile": "valid",
                    "source_snapshot": "valid",
                    "source_mechanism_query": "valid",
                },
            },
            "source_snapshot_json": {
                "verified_line_candidates": [{
                    "file": "app.py",
                    "line": 42,
                    "symbol": "worker",
                    "evidence_ref": "ev:source",
                    "eligibility_status": "verified",
                }],
            },
        },
        source_mechanism={
            "mechanism_paths": [{
                "status": "supported",
                "relations": ["propagates_to"],
                "evidence_ref": "ev:source",
                "nodes": [
                    {"file": "app.py", "line": 42, "symbol": "worker"},
                    {"file": "app.py", "line": 43, "symbol": "hot_path"},
                ],
            }],
        },
    )

    qualification = qualify_attribution(
        graph,
        ai_candidate_ids=["ai_candidate_worker"],
    )

    assert qualification.qualification == "mechanism_hypothesis"
    assert qualification.eligible_candidate_ids == []
    assert qualification.candidate_gate_failures == [{
        "candidate_id": "ai_candidate_worker",
        "reasons": ["candidate_record_missing"],
    }]


def test_ai_candidate_must_reference_each_causal_chain_segment():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {"timing_relation": "same_window"},
            "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
            "top_functions": [{
                "name": "worker",
                "file": "app.py",
                "line": 42,
                "percent": 80.0,
                "samples": 80,
                "evidence_ref": "ev:runtime",
            }],
            "input": {"cardinality": 4096},
            "latency": {"p95_ms": 900},
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 80.0}},
            "evidence_index": {
                "evidence_validity_by_family": {
                    "python_runtime_profile": "valid",
                    "source_snapshot": "valid",
                    "source_mechanism_query": "valid",
                },
            },
            "source_snapshot_json": {
                "verified_line_candidates": [{
                    "file": "app.py",
                    "line": 42,
                    "symbol": "worker",
                    "evidence_ref": "ev:source",
                    "eligibility_status": "verified",
                }],
            },
        },
        source_mechanism={
            "mechanism_paths": [{
                "status": "supported",
                "relations": ["propagates_to"],
                "evidence_ref": "ev:source",
                "nodes": [
                    {"file": "app.py", "line": 42, "symbol": "worker"},
                    {"file": "app.py", "line": 43, "symbol": "hot_path"},
                ],
            }],
        },
    )

    qualification = qualify_attribution(
        graph,
        ai_candidates=[{
            "candidate_id": "ai_candidate_incomplete_chain",
            "generated_by": "ai_guarded",
            "claim": "候选声称机制已闭合。",
            "mechanism": "open_mechanism",
            "target": "worker",
            "supported_level": "line",
            "evidence_refs": ["ev:runtime", "ev:source", "ev:impact"],
            "causal_status": "supported",
            "decision": "conclude",
            "parent_candidate_ids": ["cost:line:0"],
            "trigger_refs": ["trigger:input"],
            "mechanism_refs": ["source:mechanism:0:0"],
        }],
    )

    assert qualification.qualification == "mechanism_hypothesis"
    reasons = qualification.candidate_gate_failures[0]["reasons"]
    assert "impact_refs_missing" in reasons
    assert "source_relation_refs_missing" in reasons


def test_source_snapshot_line_without_runtime_location_is_not_a_source_relation():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {"timing_relation": "same_window"},
            "top_functions": [{
                "name": "_PyEval_EvalFrameDefault",
                "percent": 80.0,
                "samples": 80,
                "evidence_ref": "ev:runtime",
            }],
            "evidence_index": {
                "evidence_validity_by_family": {
                    "python_runtime_profile": "valid",
                    "source_snapshot": "valid",
                },
            },
            "source_snapshot_json": {
                "verified_line_candidates": [{
                    "file": "app.py",
                    "line": 42,
                    "symbol": "worker",
                    "evidence_ref": "ev:source",
                    "eligibility_status": "verified",
                }],
            },
        },
    )

    assert graph.source_relations == []


def test_runtime_call_path_is_exposed_as_observed_relation():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {"timing_relation": "same_window"},
            "top_functions": [{
                "name": "worker",
                "percent": 80.0,
                "samples": 80,
                "evidence_ref": "ev:runtime",
            }],
            "call_path_hotspots": [{
                "call_path": ["worker", "handle", "compute"],
                "evidence_ref": "ev:path",
            }],
            "evidence_index": {
                "evidence_validity_by_family": {
                    "python_runtime_profile": "valid",
                },
            },
        },
    )

    observed = {
        (item.source_ref, item.target_ref, item.relation)
        for item in graph.facts.observed_relations
    }
    assert ("worker", "handle", "calls") in observed
    assert ("handle", "compute", "calls") in observed


def test_unknown_structured_input_yields_open_facts_and_graph_domains():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {"timing_relation": "same_window"},
            "evidence_refs": ["ev:structured"],
            "top_functions": [{
                "name": "custom_native_symbol",
                "percent": 66.0,
                "evidence_ref": "ev:profile",
            }],
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 88.0}},
            "structured_values": {
                "input_shape": {"cardinality": 4096},
                "configuration": {"mode": "custom"},
                "latency": {"p95_ms": 810},
            },
            "evidence_index": {
                "evidence_validity_by_family": {"python_runtime_profile": "valid"},
            },
        },
        target={"service_id": "unknown-service"},
    )
    assert graph.facts.cost_centers[0].target == "custom_native_symbol"
    assert graph.facts.trigger_candidates
    assert graph.facts.impact_candidates
    assert graph.nodes
    assert not any("python_" in item.statement for item in graph.facts.trigger_candidates)
    assert qualify_attribution(graph).qualification != "formal_root_cause"


def test_session_qualification_abstains_without_eligible_ai_candidate():
    cluster = RootCauseCluster(
        cluster_id="engineering-only",
        candidate_ids=["analyzer-only"],
        mechanism="custom_mechanism",
        target="service-a",
        claim="Analyzer 观察到一个待验证机制。",
        evidence_refs=["ev-1"],
        qualification="possible_root_cause",
        conclusion_eligible=False,
    )
    qualification = build_session_qualification(
        [cluster],
        {
            "layers": [{
                "unknown_causes": [{
                    "candidate_id": "analyzer-only",
                    "generated_by": "analyzer_fallback",
                    "claim": "待验证",
                    "supported_level": "function",
                    "evidence_refs": ["ev-1"],
                }],
            }],
        },
        base={"evidence_refs": ["ev-1"]},
        retained_conclusion={"candidate_id": "analyzer-only", "claim": "待验证"},
    )
    explanation = apply_session_qualification(
        {
            "headline": "待验证",
            "why_it_happened": "待验证",
            "root_cause_clusters": [cluster],
            "causal_chain": [],
            "formal_root_cause": None,
            "retained_conclusion": {"candidate_id": "analyzer-only", "claim": "待验证"},
            "abstained": False,
        },
        qualification,
        all_clusters=[cluster],
    )
    assert qualification["qualification"] != "formal_root_cause"
    assert qualification["confidence_level"] == "低"
    assert explanation["formal_root_cause"] is None
    assert explanation["root_cause_clusters"] == []
    assert explanation["causal_chain"] == []
    assert explanation["abstained"] is True


def test_session_qualification_cannot_bypass_attribution_graph_gate():
    cluster = RootCauseCluster(
        cluster_id="ai-cluster",
        candidate_ids=["ai-candidate"],
        source_tree_candidate_ids=["ai-candidate"],
        mechanism="open_mechanism",
        target="service-a",
        claim="AI candidate claims a mechanism.",
        evidence_refs=["ev-1"],
        qualification="confirmed_root_cause",
        conclusion_eligible=True,
        cause_level="direct_root_cause",
        supported_level="line",
    )
    qualification = build_session_qualification(
        [cluster],
        {
            "layers": [{
                "primary_causes": [{
                    "candidate_id": "ai-candidate",
                    "generated_by": "ai_guarded",
                    "claim": "AI candidate claims a mechanism.",
                    "mechanism": "open_mechanism",
                    "target": "service-a",
                    "supported_level": "line",
                    "status": "supported",
                    "causal_status": "supported",
                    "decision": "conclude",
                    "evidence_refs": ["ev-1"],
                }],
            }],
        },
        attribution_qualification={
            "level": "L2",
            "qualification": "mechanism_hypothesis",
            "decision": "continue_probe",
            "causal_status": "unproven",
            "confidence": 0.6,
            "confidence_level": "中",
            "eligible_candidate_ids": [],
            "candidate_ids": ["ai-candidate"],
            "missing_evidence": ["verified_source_relation"],
        },
    )

    assert qualification["qualification"] == "mechanism_hypothesis"
    assert qualification["eligible_candidate_ids"] == []
    assert qualification["missing_evidence"] == ["verified_source_relation"]
