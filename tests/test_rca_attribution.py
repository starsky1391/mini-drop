"""Tests for evidence-to-attribution RCA processing."""

from __future__ import annotations

import json

from server.app.rca.calibrator import calibrate, format_for_llm
from server.app.rca.attribution import analyze_evidence
from server.app.rca.models import CandidateCause, EvidenceInput


def _candidate(candidate_id: str, refs: list[str]) -> CandidateCause:
    return CandidateCause(
        candidate_id=candidate_id,
        description=candidate_id,
        evidence_refs=refs,
        rule_score=0.8,
    )


def test_cpu_hotspot_requires_cpu_and_function_evidence():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 62.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 91.0,
                "avg_cpu_iowait_pct": 1.0,
            },
        },
    )

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert result.conclusion_boundary.can_claim_root_cause is True
    assert result.conclusion_boundary.max_supported_level == "function"
    assert result.allowed_cause_ids == ["cpu_hotspot_recursive"]
    assert result.primary_cause_id == "cpu_hotspot_recursive"
    assert "off_cpu_wait_profile" in result.collection_gaps
    assert any("off_cpu_wait_profile" in item for item in result.missing_evidence)
    assert any("trace_endpoint_profile" in item for item in result.blocked_upgrades)
    assert len(result.ai_tree) >= 1
    assert result.ai_tree[0].leaf_status in {"clear_leaf", "conservative_leaf", "unknown_leaf"}
    challenge = result.evidence_challenges[0]
    assert set(challenge.critical_fact_ids) == {"fact_cpu_user_high", "fact_top_function_0"}
    assert {item.result for item in challenge.tests} == {"forbidden", "downgrade_level"}


def test_io_wait_requires_iowait_and_latency_evidence():
    evidence = EvidenceInput(
        ebpf_metrics={"io_latency_us": {"[128,256)": 50}},
        sys_metrics={"summary": {"avg_cpu_iowait_pct": 18.0}},
    )

    result = analyze_evidence(evidence, [_candidate("io_wait_high", ["ebpf_metrics.io_latency_us"])])

    assert result.conclusion_boundary.can_claim_root_cause is True
    assert result.conclusion_boundary.max_supported_level == "resource"
    assert result.allowed_cause_ids == ["io_wait_high"]
    assert result.attributions[0].status == "supported"


def test_non_hot_function_does_not_create_cpu_root_cause():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 20.0}],
    )

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert result.conclusion_boundary.can_claim_root_cause is False
    assert result.allowed_cause_ids == []
    assert result.primary_cause_id is None
    assert result.attributions[0].status == "missing_evidence"


def test_cpu_resource_signal_without_hotspot_is_fragile():
    evidence = EvidenceInput(
        sys_metrics={"summary": {"avg_cpu_user_pct": 91.0}},
    )

    result = analyze_evidence(evidence, [_candidate("cpu_userland_hotspot", ["sys_metrics.summary"])])

    assert result.attributions[0].status == "supported"
    assert result.evidence_challenges[0].conclusion_stability == "fragile"
    assert result.allowed_cause_ids == []
    assert result.conclusion_boundary.can_claim_root_cause is False


def test_function_level_result_reports_missing_collection_capabilities():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 68.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 93.0,
                "avg_cpu_iowait_pct": 1.0,
            }
        },
        tool_results=[],
    )

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert result.conclusion_boundary.max_supported_level == "function"
    assert result.conclusion_boundary.can_claim_root_cause is True
    assert "off_cpu_wait_profile" in result.collection_gaps
    assert "trace_endpoint_profile" in result.collection_gaps
    assert any("无法判断函数热点到底是执行密集还是等待密集" in item for item in result.missing_evidence)
    assert any("无法把函数热点回连到 endpoint 或调用路径" in item for item in result.missing_evidence)
    assert any("function -> process" in item for item in result.blocked_upgrades)


def test_depth_evidence_can_promote_to_line_level():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 62.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 91.0,
                "avg_cpu_iowait_pct": 1.0,
            },
        },
        evidence_index={
            "stack_samples": [{
                "hot_frame": "compute_hotspot",
                "call_path": "main;worker;compute_hotspot",
                "stack_fragment": ["main", "worker", "compute_hotspot"],
                "wait_reason": "cpu_hotspot",
                "context_id": "ctx-1",
            }],
            "line_candidates": [{
                "file": "src/app/service.py",
                "line": 128,
                "symbol": "compute_hotspot",
                "confidence": 0.92,
                "evidence_ref": "evidence_index.line_candidates[0]",
            }],
            "context": {
                "call_path": "main;worker;compute_hotspot",
                "endpoint": "/api/order/create",
                "context_id": "ctx-1",
                "trace_id": "trace-1",
            },
        },
    )

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert result.conclusion_boundary.max_supported_level == "line"
    assert result.conclusion_boundary.can_claim_root_cause is True
    assert any(item.level == "line" for item in result.localizations)
    assert any(item.max_supported_level == "line" for item in result.attributions)
    assert any(item.leaf_status == "clear_leaf" for item in result.ai_tree)
    assert any(item.entity_type == "call_path" for item in result.graph_entities)
    assert any(item.entity_type == "function" for item in result.graph_entities)
    assert any(item.relation == "contains_hotspot" for item in result.graph_links)
    assert any(item.relation == "owns_hotspot" for item in result.graph_links)
    assert any(item.relation == "refines_hotspot" for item in result.graph_links)


def test_hotspot_ownership_edges_are_stable_across_repeated_runs():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 62.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 91.0,
                "avg_cpu_iowait_pct": 1.0,
            },
        },
        evidence_index={
            "context": {
                "call_path": "main;worker;compute_hotspot",
                "endpoint": "/api/order/create",
                "service": "order-service",
                "instance": "order-1",
                "context_id": "ctx-1",
                "trace_id": "trace-1",
            },
            "line_candidates": [{
                "file": "src/app/service.py",
                "line": 128,
                "symbol": "compute_hotspot",
                "confidence": 0.92,
                "evidence_ref": "evidence_index.line_candidates[0]",
            }],
        },
    )

    result_a = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])
    result_b = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert result_a.graph_links == result_b.graph_links
    ownership_links = [item for item in result_a.graph_links if item.relation == "owns_hotspot"]
    assert ownership_links
    assert ownership_links[0].source_id == "call_path:main;worker;compute_hotspot"
    assert ownership_links[0].target_id == "function:compute_hotspot"


def test_hotspot_ownership_edges_do_not_become_root_cause_assertions():
    evidence = EvidenceInput(
        evidence_index={
            "context": {
                "call_path": "main;worker;compute_hotspot",
                "endpoint": "/api/order/create",
                "service": "order-service",
                "instance": "order-1",
                "context_id": "ctx-1",
                "trace_id": "trace-1",
            }
        },
    )

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert any(item.relation == "routes_to" for item in result.graph_links)
    assert result.allowed_cause_ids == []
    assert result.primary_cause_id is None
    assert result.conclusion_boundary.can_claim_root_cause is False


def test_depth_evidence_without_line_candidate_stays_conservative():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 62.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 91.0,
                "avg_cpu_iowait_pct": 1.0,
            },
        },
        evidence_index={
            "stack_samples": [{
                "hot_frame": "compute_hotspot",
                "call_path": "main;worker;compute_hotspot",
                "stack_fragment": ["main", "worker", "compute_hotspot"],
                "wait_reason": "cpu_hotspot",
                "context_id": "ctx-1",
            }],
            "context": {
                "call_path": "main;worker;compute_hotspot",
                "endpoint": "/api/order/create",
                "context_id": "ctx-1",
                "trace_id": "trace-1",
            },
        },
    )

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert result.conclusion_boundary.max_supported_level == "call_path"
    assert all(item.level != "line" for item in result.localizations)
    assert any(item.max_supported_level == "function" for item in result.attributions)
    assert any(item.leaf_status == "conservative_leaf" for item in result.ai_tree)
    assert "graph_reasoning_upgrade" in result.graph_extension_points
    assert any(item.entity_type == "call_path" for item in result.graph_entities)


def test_ai_tree_and_graph_outputs_are_stable_across_repeated_runs():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 62.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 91.0,
                "avg_cpu_iowait_pct": 1.0,
            },
        },
        evidence_index={
            "stack_samples": [{
                "hot_frame": "compute_hotspot",
                "call_path": "main;worker;compute_hotspot",
                "stack_fragment": ["main", "worker", "compute_hotspot"],
                "wait_reason": "cpu_hotspot",
                "context_id": "ctx-1",
            }],
            "context": {
                "call_path": "main;worker;compute_hotspot",
                "endpoint": "/api/order/create",
                "service": "order-service",
                "instance": "order-1",
                "context_id": "ctx-1",
                "trace_id": "trace-1",
            },
        },
    )

    result_a = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])
    result_b = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert result_a.ai_tree == result_b.ai_tree
    assert result_a.graph_entities == result_b.graph_entities
    assert result_a.graph_links == result_b.graph_links
    assert result_a.ai_tree[0].leaf_status == result_b.ai_tree[0].leaf_status


def test_graph_collation_does_not_overreach_into_root_cause():
    evidence = EvidenceInput(
        evidence_index={
            "context": {
                "call_path": "main;worker;compute_hotspot",
                "endpoint": "/api/order/create",
                "service": "order-service",
                "instance": "order-1",
                "context_id": "ctx-1",
                "trace_id": "trace-1",
            }
        },
    )

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert result.graph_entities
    assert result.graph_links
    assert result.allowed_cause_ids == []
    assert result.primary_cause_id is None
    assert result.conclusion_boundary.can_claim_root_cause is False


def test_missing_depth_evidence_results_in_unknown_leaf():
    evidence = EvidenceInput()

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    assert result.conclusion_boundary.can_claim_root_cause is False
    assert result.ai_tree
    assert any(item.leaf_status == "unknown_leaf" for item in result.ai_tree)
    assert result.graph_entities == []
    assert result.graph_links == []


def test_next_evidence_requests_follow_missing_evidence_family():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 62.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 91.0,
                "avg_cpu_iowait_pct": 1.0,
            },
        },
    )

    result = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    root = next(item for item in result.ai_tree if item.node_id == "tree_root")
    assert root.next_evidence_requests == ["off_cpu_wait_profile", "trace_endpoint_profile"]
    assert result.collection_gaps[:2] == ["off_cpu_wait_profile", "trace_endpoint_profile"]


def test_next_evidence_requests_are_stable_for_same_gap():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 62.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 91.0,
                "avg_cpu_iowait_pct": 1.0,
            },
        },
    )

    result_a = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])
    result_b = analyze_evidence(evidence, [_candidate("cpu_hotspot_recursive", ["top_functions[0]"])])

    root_a = next(item for item in result_a.ai_tree if item.node_id == "tree_root")
    root_b = next(item for item in result_b.ai_tree if item.node_id == "tree_root")

    assert root_a.next_evidence_requests == root_b.next_evidence_requests
    assert root_a.next_evidence_requests == ["off_cpu_wait_profile", "trace_endpoint_profile"]


def test_primary_cause_is_stable_in_llm_candidate_order():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 62.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 91.0,
                "avg_cpu_iowait_pct": 1.0,
            },
        },
    )
    candidates = [
        _candidate("cpu_hotspot_recursive", ["top_functions[0]"]),
        _candidate("cpu_userland_hotspot", ["sys_metrics.summary"]),
    ]

    result = analyze_evidence(evidence, candidates)
    calibrated = calibrate(candidates, evidence)
    formatted = format_for_llm(calibrated, result.primary_cause_id)
    payload = json.loads(formatted)

    assert result.primary_cause_id == "cpu_hotspot_recursive"
    assert payload[0]["candidate_id"] == "cpu_hotspot_recursive"


def test_memory_fd_thread_network_and_cross_candidates_are_supported():
    evidence = EvidenceInput(
        sys_metrics={
            "summary": {
                "load1m": 3.5,
                "avg_cpu_iowait_pct": 14.0,
                "vmrss_mb": 512.0,
                "vmrss_mb_max": 2600.0,
                "fd_count": 240,
                "fd_trend": "increasing",
                "thread_count": 120,
                "thread_trend": "increasing",
                "net_rx_kbps": 12000,
                "net_tx_kbps": 15000,
                "ctx_nonvoluntary_rate": 12000,
            }
        },
        top_functions=[{"name": "network_hotspot", "percent": 82.0}],
        ebpf_metrics={"io_latency_us": {"[128,256)": 40}},
    )
    candidates = [
        _candidate("memory_swap_pressure", ["sys_metrics.summary.load1m", "sys_metrics.summary.avg_cpu_iowait_pct"]),
        _candidate("fd_exhaustion_risk", ["sys_metrics.summary.fd_count", "sys_metrics.summary.fd_trend"]),
        _candidate("thread_pool_starvation", ["sys_metrics.summary.ctx_nonvoluntary_rate", "sys_metrics.summary.thread_trend"]),
        _candidate("network_io_correlation", ["sys_metrics.summary.net_rx_kbps", "sys_metrics.summary.net_tx_kbps"]),
        _candidate("cross_io_plus_cpu_wait", ["ebpf_metrics.io_latency_us", "sys_metrics.summary.avg_cpu_iowait_pct"]),
    ]

    result = analyze_evidence(evidence, candidates)
    supported = {item.candidate_id for item in result.attributions if item.status == "supported"}

    assert {"memory_swap_pressure", "fd_exhaustion_risk", "thread_pool_starvation", "network_io_correlation", "cross_io_plus_cpu_wait"}.issubset(supported)
    assert result.conclusion_boundary.can_claim_root_cause is True
    assert result.primary_cause_id is not None
    assert result.stability_score > 0


def test_conflict_branch_is_stable_for_competing_candidates():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 92.0,
                "avg_cpu_iowait_pct": 12.0,
                "load1m": 3.6,
            },
        },
        ebpf_metrics={"io_latency_us": {"[128,256)": 35}},
    )
    candidates = [
        _candidate("cpu_hotspot_recursive", ["top_functions[0]", "sys_metrics.summary.avg_cpu_user_pct"]),
        _candidate("io_wait_high", ["ebpf_metrics.io_latency_us", "sys_metrics.summary.avg_cpu_iowait_pct"]),
    ]

    result_a = analyze_evidence(evidence, candidates)
    result_b = analyze_evidence(evidence, candidates)

    assert result_a.ai_tree == result_b.ai_tree
    conflict_nodes = [item for item in result_a.ai_tree if item.node_id == "tree_conflict"]
    assert conflict_nodes
    assert conflict_nodes[0].conflict_type in {"cross_family_conflict", "opposing_evidence_conflict"}
    assert conflict_nodes[0].leaf_status == "conservative_leaf"
    assert "cpu_hotspot_recursive" in conflict_nodes[0].conflict_candidates
    assert "io_wait_high" in conflict_nodes[0].conflict_candidates


def test_weakly_supported_candidate_is_not_promoted_when_conflict_exists():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 92.0,
                "avg_cpu_iowait_pct": 12.0,
            },
        },
        ebpf_metrics={"io_latency_us": {"[128,256)": 35}},
    )
    candidates = [
        _candidate("cpu_hotspot_recursive", ["top_functions[0]", "sys_metrics.summary.avg_cpu_user_pct"]),
        _candidate("io_wait_high", ["ebpf_metrics.io_latency_us", "sys_metrics.summary.avg_cpu_iowait_pct"]),
    ]

    result = analyze_evidence(evidence, candidates)

    assert result.ai_tree
    assert any(item.node_id == "tree_conflict" for item in result.ai_tree)
    assert result.conclusion_boundary.can_claim_root_cause is True
    assert result.primary_cause_id == "cpu_hotspot_recursive"
    conflict = next(item for item in result.ai_tree if item.node_id == "tree_conflict")
    assert conflict.decision == "downgrade"
    assert conflict.leaf_status == "conservative_leaf"


def test_primary_cause_is_independent_of_candidate_input_order():
    evidence = EvidenceInput(
        top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
        sys_metrics={
            "summary": {
                "avg_cpu_user_pct": 92.0,
                "avg_cpu_iowait_pct": 1.0,
            },
        },
    )
    ordered = [
        _candidate("cpu_userland_hotspot", ["sys_metrics.summary"]),
        _candidate("cpu_hotspot_recursive", ["top_functions[0]"]),
    ]
    reversed_candidates = list(reversed(ordered))

    result_a = analyze_evidence(evidence, ordered)
    result_b = analyze_evidence(evidence, reversed_candidates)

    assert result_a.primary_cause_id == result_b.primary_cause_id
    assert result_a.primary_cause_id == "cpu_hotspot_recursive"


def test_candidate_missing_evidence_is_deduplicated():
    evidence = EvidenceInput()
    candidates = [
        CandidateCause(
            candidate_id="cpu_hotspot_recursive",
            description="cpu",
            evidence_refs=["top_functions[0]", "top_functions[0]"],
            rule_score=0.8,
            missing_evidence=["缺少 TopN 热点函数数据", "缺少 TopN 热点函数数据"],
        )
    ]

    calibrated = calibrate(candidates, evidence)

    assert calibrated[0].missing_evidence == ["缺少 TopN 热点函数数据"]
