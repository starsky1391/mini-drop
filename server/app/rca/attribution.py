"""Evidence-first RCA processing for structured attribution boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from server.app.rca.models import (
    AITreeBudgetSnapshot,
    AITreeCandidateNode,
    AITreeLayer,
    AITreeProbeEdge,
    AITreeProbeResult,
    AITreeSelfChallenge,
    AnalysisFact,
    AnalysisGraphEntity,
    AnalysisGraphLink,
    AnalysisLocalization,
    AnalysisSymptom,
    AnalysisTreeDecision,
    CandidateCause,
    ConclusionBoundary,
    ControlledAITree,
    EvidenceAttributionResult,
    EvidenceChallenge,
    EvidenceChallengeTest,
    EvidenceInput,
    GuardedAttribution,
)


_LEVEL_ORDER = {
    "resource": 0,
    "host": 1,
    "process": 2,
    "thread": 3,
    "syscall": 4,
    "dependency": 5,
    "service": 6,
    "endpoint": 7,
    "function": 8,
    "call_path": 9,
    "line": 10,
}


def analyze_evidence(
    evidence: EvidenceInput,
    candidates: Iterable[CandidateCause],
) -> EvidenceAttributionResult:
    """Build reportable attribution boundaries from existing RCA evidence."""
    default_window = _evidence_window(evidence)
    facts = _derive_facts(evidence)
    facts.extend(_derive_depth_facts(evidence))
    facts.extend(_derive_timed_window_facts(evidence))
    facts = _apply_default_window(facts, default_window)
    fact_map = {fact.fact_id: fact for fact in facts}
    symptoms = _derive_symptoms(facts)
    localizations = _derive_localizations(facts, symptoms)
    localizations.extend(_derive_depth_localizations(evidence, fact_map))
    attributions = [
        _guard_candidate(candidate, fact_map, symptoms, localizations)
        for candidate in candidates
    ]
    challenges = [
        _challenge(attribution, fact_map)
        for attribution in attributions
        if attribution.status == "supported"
    ]
    stable_candidates = {
        challenge.candidate_id
        for challenge in challenges
        if challenge.conclusion_stability == "stable"
    }
    allowed = [
        attribution.candidate_id
        for attribution in attributions
        if attribution.status == "supported" and attribution.candidate_id in stable_candidates
    ]
    max_level = _max_level(
        [
            *[item.level for item in localizations],
            *[
                attribution.max_supported_level
                for attribution in attributions
                if attribution.status in {"supported", "missing_evidence"}
            ],
        ]
    )
    primary_cause_id, primary_reason, stability_score = _select_primary_cause(
        attributions,
        challenges,
        localizations,
    )
    delayed_reproduction_status = _delayed_followup_reproduction_status(facts)
    conclusion_window = _conclusion_window(facts, attributions, allowed)
    timing_relation = _conclusion_timing_relation(facts, attributions, allowed)
    missing_evidence, blocked_upgrades, collection_gaps = _derive_collection_gaps(
        evidence,
        localizations,
        attributions,
        max_level,
        stability_score,
    )
    boundary = ConclusionBoundary(
        can_claim_root_cause=bool(allowed),
        max_supported_level=max_level,
        reason=_build_boundary_reason(
            can_claim_root_cause=bool(allowed),
            max_supported_level=max_level,
            missing_evidence=missing_evidence,
            blocked_upgrades=blocked_upgrades,
            timing_relation=timing_relation,
            delayed_followup_reproduction_status=delayed_reproduction_status,
        ),
        conclusion_window=conclusion_window,
        timing_relation=timing_relation,
        delayed_followup_reproduction_status=delayed_reproduction_status,
        non_refutable_evidence_boundaries=_non_refutable_evidence_boundaries(facts, attributions),
    )
    conflict_branch = _derive_conflict_branch(attributions, facts, primary_cause_id)
    ai_tree = _derive_ai_tree(
        evidence,
        facts,
        localizations,
        attributions,
        boundary,
        conflict_branch,
        stability_score,
        missing_evidence,
        blocked_upgrades,
        collection_gaps,
    )
    controlled_ai_tree = _derive_controlled_ai_tree(
        evidence,
        facts,
        localizations,
        attributions,
        boundary,
        ai_tree,
        stability_score,
        missing_evidence,
        blocked_upgrades,
        collection_gaps,
        conflict_branch,
        primary_cause_id,
    )
    graph_entities, graph_links = _derive_graph_context(evidence, localizations)
    graph_extension_points = _derive_graph_extension_points(graph_entities, graph_links)
    return EvidenceAttributionResult(
        facts=facts,
        symptoms=symptoms,
        localizations=localizations,
        ai_tree=ai_tree,
        controlled_ai_tree=controlled_ai_tree,
        graph_entities=graph_entities,
        graph_links=graph_links,
        attributions=attributions,
        evidence_challenges=challenges,
        missing_evidence=missing_evidence,
        blocked_upgrades=blocked_upgrades,
        collection_gaps=collection_gaps,
        graph_extension_points=graph_extension_points,
        allowed_cause_ids=allowed,
        primary_cause_id=primary_cause_id,
        stability_score=stability_score,
        primary_cause_reason=primary_reason,
        conclusion_boundary=boundary,
    )


def _derive_facts(evidence: EvidenceInput) -> list[AnalysisFact]:
    facts: list[AnalysisFact] = []
    summary = evidence.sys_metrics.get("summary", {}) if evidence.sys_metrics else {}

    user_cpu = _number(summary.get("avg_cpu_user_pct"))
    if user_cpu is not None:
        threshold = 70.0
        facts.append(AnalysisFact(
            fact_id="fact_cpu_user_high" if user_cpu >= threshold else "fact_cpu_user_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=user_cpu,
            status="observed" if user_cpu >= threshold else "normal",
            threshold_band=_threshold_band(user_cpu, threshold),
        ))

    iowait = _number(summary.get("avg_cpu_iowait_pct"))
    if iowait is not None:
        threshold = 10.0
        facts.append(AnalysisFact(
            fact_id="fact_iowait_high" if iowait >= threshold else "fact_iowait_low",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=iowait,
            status="observed" if iowait >= threshold else "normal",
            threshold_band=_threshold_band(iowait, threshold),
        ))

    sys_cpu = _number(summary.get("avg_cpu_sys_pct"))
    if sys_cpu is not None:
        threshold = 30.0
        facts.append(AnalysisFact(
            fact_id="fact_cpu_sys_high" if sys_cpu >= threshold else "fact_cpu_sys_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=sys_cpu,
            status="observed" if sys_cpu >= threshold else "normal",
            threshold_band=_threshold_band(sys_cpu, threshold),
        ))

    ctx_rate = _number(summary.get("ctx_nonvoluntary_rate"))
    if ctx_rate is not None:
        threshold = 1000.0
        facts.append(AnalysisFact(
            fact_id="fact_ctx_switch_high" if ctx_rate >= threshold else "fact_ctx_switch_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=ctx_rate,
            status="observed" if ctx_rate >= threshold else "normal",
            threshold_band=_threshold_band(ctx_rate, threshold),
        ))

    load1m = _number(summary.get("load1m"))
    if load1m is not None:
        threshold = 2.0
        facts.append(AnalysisFact(
            fact_id="fact_load_high" if load1m >= threshold else "fact_load_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=load1m,
            status="observed" if load1m >= threshold else "normal",
            threshold_band=_threshold_band(load1m, threshold),
        ))

    if evidence.top_functions:
        top = evidence.top_functions[0]
        percent = _number(top.get("percent")) or 0.0
        threshold = 30.0
        facts.append(AnalysisFact(
            fact_id="fact_top_function_0",
            source="top_functions",
            evidence_ref="top_functions[0]",
            value={
                "name": top.get("name", "unknown"),
                "percent": percent,
            },
            status="observed" if percent >= threshold else "normal",
            threshold_band=_threshold_band(percent, threshold),
        ))

    rss_mb = _number(summary.get("vmrss_mb"))
    if rss_mb is not None:
        threshold = 200.0
        facts.append(AnalysisFact(
            fact_id="fact_memory_rss_high" if rss_mb >= threshold else "fact_memory_rss_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=rss_mb,
            status="observed" if rss_mb >= threshold else "normal",
            threshold_band=_threshold_band(rss_mb, threshold),
        ))

    rss_peak_mb = _number(summary.get("vmrss_mb_max"))
    if rss_peak_mb is not None:
        threshold = 2048.0
        facts.append(AnalysisFact(
            fact_id="fact_memory_peak_high" if rss_peak_mb >= threshold else "fact_memory_peak_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=rss_peak_mb,
            status="observed" if rss_peak_mb >= threshold else "normal",
            threshold_band=_threshold_band(rss_peak_mb, threshold),
        ))

    fd_count = _number(summary.get("fd_count"))
    if fd_count is not None:
        threshold = 200.0
        facts.append(AnalysisFact(
            fact_id="fact_fd_high" if fd_count >= threshold else "fact_fd_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=fd_count,
            status="observed" if fd_count >= threshold else "normal",
            threshold_band=_threshold_band(fd_count, threshold),
        ))

    fd_trend = summary.get("fd_trend")
    if fd_trend:
        facts.append(AnalysisFact(
            fact_id="fact_fd_growth" if fd_trend == "increasing" else "fact_fd_stable",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=fd_trend,
            status="observed" if fd_trend == "increasing" else "normal",
            threshold_band="unknown",
        ))

    thread_count = _number(summary.get("thread_count"))
    if thread_count is not None:
        threshold = 50.0
        facts.append(AnalysisFact(
            fact_id="fact_thread_high" if thread_count >= threshold else "fact_thread_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=thread_count,
            status="observed" if thread_count >= threshold else "normal",
            threshold_band=_threshold_band(thread_count, threshold),
        ))

    thread_trend = summary.get("thread_trend")
    if thread_trend:
        facts.append(AnalysisFact(
            fact_id="fact_thread_growth" if thread_trend == "increasing" else "fact_thread_stable",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=thread_trend,
            status="observed" if thread_trend == "increasing" else "normal",
            threshold_band="unknown",
        ))

    net_rx = _number(summary.get("net_rx_kbps"))
    if net_rx is not None:
        threshold = 10000.0
        facts.append(AnalysisFact(
            fact_id="fact_network_rx_high" if net_rx >= threshold else "fact_network_rx_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=net_rx,
            status="observed" if net_rx >= threshold else "normal",
            threshold_band=_threshold_band(net_rx, threshold),
        ))

    net_tx = _number(summary.get("net_tx_kbps"))
    if net_tx is not None:
        threshold = 10000.0
        facts.append(AnalysisFact(
            fact_id="fact_network_tx_high" if net_tx >= threshold else "fact_network_tx_normal",
            source="sys_metrics",
            evidence_ref="sys_metrics.summary",
            value=net_tx,
            status="observed" if net_tx >= threshold else "normal",
            threshold_band=_threshold_band(net_tx, threshold),
        ))

    histogram = evidence.ebpf_metrics.get("io_latency_us", {}) if evidence.ebpf_metrics else {}
    if isinstance(histogram, dict) and histogram:
        facts.append(AnalysisFact(
            fact_id="fact_io_latency_present",
            source="ebpf_metrics",
            evidence_ref="ebpf_metrics.io_latency_us",
            value=sum(_number(value) or 0.0 for value in histogram.values()),
            threshold_band="unknown",
        ))

    return facts


def _derive_depth_facts(evidence: EvidenceInput) -> list[AnalysisFact]:
    index = evidence.evidence_index or {}
    if not isinstance(index, dict):
        return []

    facts: list[AnalysisFact] = []
    stack_summary = index.get("stack_summary", {})
    if isinstance(stack_summary, dict):
        dominant_frame = str(stack_summary.get("dominant_hot_frame") or "").strip()
        if dominant_frame:
            facts.append(AnalysisFact(
                fact_id="fact_depth_stack_summary",
                source="evidence_index",
                evidence_ref="evidence_index.stack_summary",
                value={
                    "dominant_hot_frame": dominant_frame,
                    "dominant_percent": float(stack_summary.get("dominant_percent", 0.0) or 0.0),
                    "sample_count": int(stack_summary.get("sample_count", 0) or 0),
                    "parse_status": str(stack_summary.get("parse_status") or ""),
                },
                threshold_band="unknown",
            ))

    stack_samples = index.get("stack_samples", [])
    if isinstance(stack_samples, list):
        for position, item in enumerate(stack_samples[:3], start=1):
            if not isinstance(item, dict):
                continue
            stack_fragment = item.get("stack_fragment") or []
            hot_frame = str(item.get("hot_frame") or "")
            call_path = str(item.get("call_path") or "")
            if not hot_frame and not call_path:
                continue
            facts.append(AnalysisFact(
                fact_id=f"fact_depth_stack_sample_{position}",
                source="evidence_index",
                evidence_ref=f"evidence_index.stack_samples[{position - 1}]",
                value={
                    "hot_frame": hot_frame,
                    "call_path": call_path,
                    "stack_fragment": stack_fragment,
                    "wait_reason": str(item.get("wait_reason") or ""),
                    "context_id": str(item.get("context_id") or ""),
                },
                threshold_band="unknown",
            ))

    call_path_hotspots = index.get("call_path_hotspots", [])
    if isinstance(call_path_hotspots, list):
        for position, item in enumerate(call_path_hotspots[:3], start=1):
            if not isinstance(item, dict):
                continue
            function = str(item.get("function") or "")
            call_path = item.get("call_path") if isinstance(item.get("call_path"), list) else []
            endpoint = str(item.get("endpoint") or "")
            if not function and not call_path and not endpoint:
                continue
            facts.append(AnalysisFact(
                fact_id=f"fact_depth_call_path_hotspot_{position}",
                source="evidence_index",
                evidence_ref=f"evidence_index.call_path_hotspots[{position - 1}]",
                value={
                    "function": function,
                    "call_path": call_path,
                    "endpoint": endpoint,
                    "service_id": str(item.get("service_id") or ""),
                    "instance_id": str(item.get("instance_id") or ""),
                    "percent": float(item.get("percent", 0.0) or 0.0),
                    "samples": int(item.get("samples", 0) or 0),
                    "context_id": str(item.get("context_id") or ""),
                },
                threshold_band="unknown",
            ))

    line_candidates = index.get("line_candidates", [])
    if isinstance(line_candidates, list):
        for position, item in enumerate(line_candidates[:3], start=1):
            if not isinstance(item, dict):
                continue
            file_path = str(item.get("file") or item.get("file_path") or "")
            line_number = int(item.get("line") or item.get("line_number") or 0)
            symbol = str(item.get("symbol") or item.get("function") or "")
            if not file_path and not line_number and not symbol:
                continue
            facts.append(AnalysisFact(
                fact_id=f"fact_depth_line_candidate_{position}",
                source="evidence_index",
                evidence_ref=f"evidence_index.line_candidates[{position - 1}]",
                value={
                    "file": file_path,
                    "line": line_number,
                    "symbol": symbol,
                    "confidence": float(item.get("confidence", 0.0) or 0.0),
                },
                threshold_band="unknown",
            ))

    context = index.get("context", {})
    if isinstance(context, dict):
        call_path = str(context.get("call_path") or "")
        endpoint = str(context.get("endpoint") or "")
        context_id = str(context.get("context_id") or "")
        if call_path or endpoint or context_id:
            facts.append(AnalysisFact(
                fact_id="fact_depth_context",
                source="evidence_index",
                evidence_ref="evidence_index.context",
                value={
                    "call_path": call_path,
                    "endpoint": endpoint,
                    "context_id": context_id,
                    "trace_id": str(context.get("trace_id") or ""),
                    "wait_reason": str(context.get("wait_reason") or ""),
                },
                threshold_band="unknown",
            ))

    return facts


def _derive_timed_window_facts(evidence: EvidenceInput) -> list[AnalysisFact]:
    index = evidence.evidence_index or {}
    if not isinstance(index, dict):
        return []
    timed_facts = index.get("timed_facts", [])
    if not isinstance(timed_facts, list):
        return []

    facts: list[AnalysisFact] = []
    for position, item in enumerate(timed_facts[:20], start=1):
        if not isinstance(item, dict):
            continue
        fact_id = str(item.get("fact_id") or f"fact_timed_{position}").strip()
        source = str(item.get("source") or "evidence_index.timed_facts").strip()
        evidence_ref = str(item.get("evidence_ref") or f"evidence_index.timed_facts[{position - 1}]").strip()
        status = "normal" if item.get("status") == "normal" else "observed"
        threshold_band = str(item.get("threshold_band") or "unknown")
        if threshold_band not in {"below", "near", "above", "unknown"}:
            threshold_band = "unknown"
        evidence_window = item.get("evidence_window") if isinstance(item.get("evidence_window"), dict) else {}
        facts.append(AnalysisFact(
            fact_id=fact_id,
            source=source,
            evidence_ref=evidence_ref,
            value=item.get("value"),
            status=status,
            threshold_band=threshold_band,
            evidence_window=evidence_window,
            timing_relation=_timing_relation_from_window(evidence_window),
        ))
    return facts


def _evidence_window(evidence: EvidenceInput) -> dict:
    index = evidence.evidence_index or {}
    if isinstance(index, dict) and isinstance(index.get("evidence_window"), dict):
        return dict(index["evidence_window"])
    for top in evidence.top_functions:
        if isinstance(top, dict) and isinstance(top.get("evidence_window"), dict):
            return dict(top["evidence_window"])
    return {}


def _apply_default_window(facts: list[AnalysisFact], default_window: dict) -> list[AnalysisFact]:
    if not default_window:
        return facts
    result: list[AnalysisFact] = []
    for fact in facts:
        if fact.evidence_window:
            result.append(fact)
            continue
        result.append(fact.model_copy(update={
            "evidence_window": default_window,
            "timing_relation": _timing_relation_from_window(default_window),
        }))
    return result


def _timing_relation_from_window(window: dict) -> str:
    relation = str(window.get("timing_relation") or "unknown")
    if relation in {
        "same_window",
        "pre_trigger_window",
        "post_trigger_window",
        "delayed_followup",
        "stale_window",
        "unknown",
    }:
        return relation
    return "unknown"


def _derive_symptoms(facts: list[AnalysisFact]) -> list[AnalysisSymptom]:
    by_id = {fact.fact_id: fact for fact in facts}
    symptoms: list[AnalysisSymptom] = []

    if "fact_cpu_user_high" in by_id:
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_cpu_utilization_high",
            symptom_type="cpu_utilization_high",
            severity="high",
            fact_ids=["fact_cpu_user_high"],
        ))
    if "fact_top_function_0" in by_id and by_id["fact_top_function_0"].status == "observed":
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_function_hotspot",
            symptom_type="function_hotspot",
            severity="high",
            fact_ids=["fact_top_function_0"],
        ))
    if "fact_iowait_high" in by_id:
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_io_wait_high",
            symptom_type="io_wait_high",
            severity="high",
            fact_ids=["fact_iowait_high"],
        ))
    if "fact_io_latency_present" in by_id:
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_io_latency_high",
            symptom_type="io_latency_high",
            severity="medium",
            fact_ids=["fact_io_latency_present"],
        ))
    if "fact_cpu_sys_high" in by_id:
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_kernel_cpu_high",
            symptom_type="kernel_cpu_high",
            severity="high",
            fact_ids=["fact_cpu_sys_high"],
        ))
    if "fact_ctx_switch_high" in by_id:
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_context_switch_storm",
            symptom_type="context_switch_storm",
            severity="high",
            fact_ids=["fact_ctx_switch_high"],
        ))
    if "fact_load_high" in by_id:
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_system_load_high",
            symptom_type="system_load_high",
            severity="medium",
            fact_ids=["fact_load_high"],
        ))
    if "fact_memory_rss_high" in by_id or "fact_memory_peak_high" in by_id:
        ids = [fact_id for fact_id in ("fact_memory_rss_high", "fact_memory_peak_high") if fact_id in by_id]
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_memory_pressure",
            symptom_type="memory_pressure",
            severity="medium",
            fact_ids=ids,
        ))
    if "fact_fd_high" in by_id or "fact_fd_growth" in by_id:
        ids = [fact_id for fact_id in ("fact_fd_high", "fact_fd_growth") if fact_id in by_id]
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_fd_exhaustion",
            symptom_type="fd_exhaustion",
            severity="high",
            fact_ids=ids,
        ))
    if "fact_thread_high" in by_id or "fact_thread_growth" in by_id:
        ids = [fact_id for fact_id in ("fact_thread_high", "fact_thread_growth") if fact_id in by_id]
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_thread_pressure",
            symptom_type="thread_pressure",
            severity="high",
            fact_ids=ids,
        ))
    if "fact_network_rx_high" in by_id or "fact_network_tx_high" in by_id:
        ids = [fact_id for fact_id in ("fact_network_rx_high", "fact_network_tx_high") if fact_id in by_id]
        symptoms.append(AnalysisSymptom(
            symptom_id="sym_network_pressure",
            symptom_type="network_pressure",
            severity="medium",
            fact_ids=ids,
        ))
    return symptoms


def _derive_localizations(
    facts: list[AnalysisFact],
    symptoms: list[AnalysisSymptom],
) -> list[AnalysisLocalization]:
    fact_by_id = {fact.fact_id: fact for fact in facts}
    symptom_types = {symptom.symptom_type for symptom in symptoms}
    localizations: list[AnalysisLocalization] = []

    if "cpu_utilization_high" in symptom_types:
        localizations.append(AnalysisLocalization(
            level="resource",
            target="cpu",
            fact_ids=["fact_cpu_user_high"],
        ))
    if "kernel_cpu_high" in symptom_types:
        ids = [fact_id for fact_id in ("fact_cpu_sys_high", "fact_ctx_switch_high") if fact_id in fact_by_id]
        localizations.append(AnalysisLocalization(
            level="syscall",
            target="kernel",
            fact_ids=ids,
        ))
    if "function_hotspot" in symptom_types:
        top = fact_by_id["fact_top_function_0"].value
        localizations.append(AnalysisLocalization(
            level="function",
            target=str(top.get("name", "unknown")),
            fact_ids=["fact_cpu_user_high", "fact_top_function_0"],
        ))
    if "fact_depth_stack_summary" in fact_by_id:
        summary = fact_by_id["fact_depth_stack_summary"].value
        localizations.append(AnalysisLocalization(
            level="function",
            target=str(summary.get("dominant_hot_frame", "unknown")),
            fact_ids=["fact_depth_stack_summary"],
            evidence_refs=["evidence_index.stack_summary"],
        ))
    if "fact_depth_call_path_hotspot_1" in fact_by_id:
        hotspot = fact_by_id["fact_depth_call_path_hotspot_1"].value
        localizations.append(AnalysisLocalization(
            level="call_path",
            target=";".join(hotspot.get("call_path", [])) or str(hotspot.get("function", "unknown")),
            fact_ids=["fact_depth_call_path_hotspot_1"],
            evidence_refs=["evidence_index.call_path_hotspots[0]"],
        ))
    if "io_wait_high" in symptom_types or "io_latency_high" in symptom_types:
        ids = [
            fact_id
            for fact_id in ("fact_iowait_high", "fact_io_latency_present")
            if fact_id in fact_by_id
        ]
        localizations.append(AnalysisLocalization(level="resource", target="io", fact_ids=ids))
    if "memory_pressure" in symptom_types:
        ids = [fact_id for fact_id in ("fact_memory_rss_high", "fact_memory_peak_high") if fact_id in fact_by_id]
        localizations.append(AnalysisLocalization(level="resource", target="memory", fact_ids=ids))
    if "fd_exhaustion" in symptom_types:
        ids = [fact_id for fact_id in ("fact_fd_high", "fact_fd_growth") if fact_id in fact_by_id]
        localizations.append(AnalysisLocalization(level="process", target="fd", fact_ids=ids))
    if "thread_pressure" in symptom_types:
        ids = [fact_id for fact_id in ("fact_thread_high", "fact_thread_growth") if fact_id in fact_by_id]
        localizations.append(AnalysisLocalization(level="thread", target="thread", fact_ids=ids))
    if "network_pressure" in symptom_types:
        ids = [fact_id for fact_id in ("fact_network_rx_high", "fact_network_tx_high") if fact_id in fact_by_id]
        localizations.append(AnalysisLocalization(level="resource", target="network", fact_ids=ids))
    if "system_load_high" in symptom_types:
        localizations.append(AnalysisLocalization(
            level="resource",
            target="system",
            fact_ids=["fact_load_high"],
        ))
    return localizations


def _derive_depth_localizations(
    evidence: EvidenceInput,
    fact_map: dict[str, AnalysisFact],
) -> list[AnalysisLocalization]:
    index = evidence.evidence_index or {}
    if not isinstance(index, dict):
        return []

    localizations: list[AnalysisLocalization] = []
    line_candidates = index.get("line_candidates", [])
    stack_samples = index.get("stack_samples", [])
    context = index.get("context", {})
    has_contextual_support = bool(stack_samples) or (
        isinstance(context, dict)
        and any(str(context.get(key) or "").strip() for key in ("call_path", "endpoint", "trace_id", "wait_reason"))
    )
    source_context = evidence.source_context if isinstance(evidence.source_context, dict) else {}
    has_source_context = _has_source_context(source_context)
    if isinstance(line_candidates, list) and has_source_context:
        for position, item in enumerate(line_candidates[:3], start=1):
            if not isinstance(item, dict):
                continue
            file_path = str(item.get("file") or item.get("file_path") or "").strip()
            line_number = _safe_int(item.get("line") or item.get("line_number") or 0)
            symbol = str(item.get("symbol") or item.get("function") or "").strip()
            confidence = float(item.get("confidence", 0.0) or 0.0)
            if not has_contextual_support or not file_path or line_number <= 0 or confidence < 0.6:
                continue
            evidence_refs = []
            ref = str(item.get("evidence_ref") or "").strip()
            if ref:
                evidence_refs.append(ref)
            supporting_fact_ids = []
            fact_id = f"fact_depth_line_candidate_{position}"
            if fact_id in fact_map:
                supporting_fact_ids.append(fact_id)
            localizations.append(AnalysisLocalization(
                level="line",
                target=symbol or file_path,
                fact_ids=supporting_fact_ids,
                file_path=file_path,
                line_number=line_number,
                evidence_refs=evidence_refs,
            ))

    if isinstance(context, dict) and not localizations:
        call_path = str(context.get("call_path") or "").strip()
        endpoint = str(context.get("endpoint") or "").strip()
        if call_path and endpoint:
            localizations.append(AnalysisLocalization(
                level="call_path",
                target=call_path,
                fact_ids=["fact_depth_context"] if "fact_depth_context" in fact_map else [],
                evidence_refs=["evidence_index.context"] if "fact_depth_context" in fact_map else [],
            ))
    if isinstance(index.get("call_path_hotspots"), list) and not any(item.level == "call_path" for item in localizations):
        hotspot = next((item for item in index.get("call_path_hotspots", []) if isinstance(item, dict) and item.get("call_path")), None)
        if isinstance(hotspot, dict):
            call_path = hotspot.get("call_path") if isinstance(hotspot.get("call_path"), list) else []
            target = ";".join(call_path) if call_path else str(hotspot.get("function") or "unknown")
            localizations.append(AnalysisLocalization(
                level="call_path",
                target=target,
                fact_ids=["fact_depth_call_path_hotspot_1"] if "fact_depth_call_path_hotspot_1" in fact_map else [],
                evidence_refs=["evidence_index.call_path_hotspots[0]"],
            ))
    return localizations


def _has_source_context(source_context: dict[str, object]) -> bool:
    if not isinstance(source_context, dict):
        return False
    source_paths = source_context.get("source_paths")
    symbol_map_paths = source_context.get("symbol_map_paths")
    return any([
        isinstance(source_paths, list) and any(str(item).strip() for item in source_paths),
        isinstance(symbol_map_paths, list) and any(str(item).strip() for item in symbol_map_paths),
        bool(str(source_context.get("repo_revision") or "").strip()),
        bool(str(source_context.get("build_id") or "").strip()),
    ])


def _guard_candidate(
    candidate: CandidateCause,
    fact_map: dict[str, AnalysisFact],
    symptoms: list[AnalysisSymptom],
    localizations: list[AnalysisLocalization],
) -> GuardedAttribution:
    domain = _candidate_domain(candidate.candidate_id)
    symptom_types = {symptom.symptom_type for symptom in symptoms}

    if candidate.candidate_id.startswith("cross_"):
        return _cross_attribution(candidate, fact_map, symptoms, localizations)
    if domain == "cpu":
        return _cpu_attribution(candidate, fact_map, symptom_types, localizations)
    if domain == "io":
        return _io_attribution(candidate, fact_map, symptom_types)
    if domain == "memory":
        return _memory_attribution(candidate, fact_map, symptom_types)
    if domain == "fd":
        return _fd_attribution(candidate, fact_map, symptom_types, localizations)
    if domain == "thread":
        return _thread_attribution(candidate, fact_map, symptom_types, localizations)
    if domain == "network":
        return _network_attribution(candidate, fact_map, symptom_types)

    available_refs = _candidate_fact_refs(candidate.evidence_refs, fact_map)
    if candidate.candidate_id == "insufficient_data":
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="forbidden",
            missing_evidence=candidate.missing_evidence,
        )
    if available_refs:
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="missing_evidence",
            supporting_fact_ids=available_refs,
            missing_evidence=[
                "当前版本尚未为该候选原因建立现象和定位规则。",
                *candidate.missing_evidence,
            ],
            max_supported_level=_max_level([item.level for item in localizations]),
        )
    return GuardedAttribution(
        candidate_id=candidate.candidate_id,
        status="forbidden",
        missing_evidence=["候选原因没有对应的可用事实。", *candidate.missing_evidence],
    )


def _cpu_attribution(
    candidate: CandidateCause,
    fact_map: dict[str, AnalysisFact],
    symptom_types: set[str],
    localizations: list[AnalysisLocalization],
) -> GuardedAttribution:
    fact_ids = set(fact_map)
    supporting_fact_ids = []
    max_supported_level = "resource"
    if "kernel_cpu_high" in symptom_types and "fact_cpu_sys_high" in fact_ids:
        supporting_fact_ids.append("fact_cpu_sys_high")
        if "fact_ctx_switch_high" in fact_ids:
            supporting_fact_ids.append("fact_ctx_switch_high")
        max_supported_level = "syscall"
    if "function_hotspot" in symptom_types:
        supporting_fact_ids.append("fact_top_function_0")
        max_supported_level = "function"
    if "line" in {item.level for item in localizations}:
        line_fact_ids = [
            fact_id
            for fact_id in fact_ids
            if fact_id.startswith("fact_depth_line_candidate_")
        ]
        if line_fact_ids:
            supporting_fact_ids.extend(line_fact_ids)
            max_supported_level = "line"
    if "fact_cpu_user_high" in fact_ids:
        supporting_fact_ids.append("fact_cpu_user_high")
    if "context_switch_storm" in symptom_types and "fact_ctx_switch_high" in fact_ids:
        supporting_fact_ids.append("fact_ctx_switch_high")
        max_supported_level = max(max_supported_level, "thread", key=lambda item: _LEVEL_ORDER[item])

    if supporting_fact_ids:
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="supported",
            supporting_fact_ids=sorted(supporting_fact_ids),
            max_supported_level=max_supported_level,
        )
    missing = []
    if "fact_cpu_user_high" not in fact_ids:
        missing.append("缺少 CPU 用户态高使用率事实")
    if "fact_top_function_0" not in fact_ids:
        missing.append("缺少函数热点事实")
    return GuardedAttribution(
        candidate_id=candidate.candidate_id,
        status="missing_evidence",
            supporting_fact_ids=_candidate_fact_refs(candidate.evidence_refs, fact_ids),
            missing_evidence=missing,
    )


def _io_attribution(
    candidate: CandidateCause,
    fact_map: dict[str, AnalysisFact],
    symptom_types: set[str],
) -> GuardedAttribution:
    fact_ids = set(fact_map)
    supporting_fact_ids = [
        fact_id
        for fact_id in ("fact_iowait_high", "fact_io_latency_present")
        if fact_id in fact_ids
    ]
    if supporting_fact_ids and (
        "io_wait_high" in symptom_types or "io_latency_high" in symptom_types
    ):
        opposing_fact_ids = []
        if "fact_iowait_low" in fact_ids and _can_fact_refute(
            supporting_fact_ids,
            "fact_iowait_low",
            fact_map,
        ):
            opposing_fact_ids.append("fact_iowait_low")
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="supported",
            supporting_fact_ids=supporting_fact_ids,
            opposing_fact_ids=opposing_fact_ids,
            max_supported_level="resource",
        )
    missing = []
    if "fact_iowait_high" not in fact_ids:
        missing.append("缺少 IO wait 高事实")
    if "fact_io_latency_present" not in fact_ids:
        missing.append("缺少 IO 延迟事实")
    return GuardedAttribution(
        candidate_id=candidate.candidate_id,
        status="missing_evidence",
        supporting_fact_ids=_candidate_fact_refs(candidate.evidence_refs, fact_ids),
        missing_evidence=missing,
    )


def _memory_attribution(
    candidate: CandidateCause,
    fact_map: dict[str, AnalysisFact],
    symptom_types: set[str],
) -> GuardedAttribution:
    fact_ids = set(fact_map)
    if candidate.candidate_id == "memory_swap_pressure":
        supporting_fact_ids = [
            fact_id
            for fact_id in ("fact_load_high", "fact_iowait_high")
            if fact_id in fact_ids
        ]
        if {"fact_load_high", "fact_iowait_high"}.issubset(fact_ids):
            return GuardedAttribution(
                candidate_id=candidate.candidate_id,
                status="supported",
                supporting_fact_ids=supporting_fact_ids,
                max_supported_level="resource",
            )
        missing = []
        if "fact_load_high" not in fact_ids:
            missing.append("缺少系统负载高事实")
        if "fact_iowait_high" not in fact_ids:
            missing.append("缺少 IO wait 高事实")
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="missing_evidence",
            supporting_fact_ids=_candidate_fact_refs(candidate.evidence_refs, fact_map),
            missing_evidence=missing,
        )

    supporting_fact_ids = [
        fact_id
        for fact_id in ("fact_memory_rss_high", "fact_memory_peak_high")
        if fact_id in fact_ids
    ]
    if "memory_pressure" in symptom_types and supporting_fact_ids:
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="supported",
            supporting_fact_ids=supporting_fact_ids,
            max_supported_level="resource",
        )
    missing = []
    if "fact_memory_rss_high" not in fact_ids:
        missing.append("缺少 RSS 增长或高水位事实")
    if "fact_memory_peak_high" not in fact_ids:
        missing.append("缺少 RSS 峰值事实")
    return GuardedAttribution(
        candidate_id=candidate.candidate_id,
        status="missing_evidence",
        supporting_fact_ids=_candidate_fact_refs(candidate.evidence_refs, fact_map),
        missing_evidence=missing,
    )


def _fd_attribution(
    candidate: CandidateCause,
    fact_map: dict[str, AnalysisFact],
    symptom_types: set[str],
    localizations: list[AnalysisLocalization],
) -> GuardedAttribution:
    fact_ids = set(fact_map)
    if candidate.candidate_id == "fd_high_watermark":
        supporting_fact_ids = [fact_id for fact_id in ("fact_fd_high",) if fact_id in fact_ids]
        if "fact_fd_high" in fact_ids:
            return GuardedAttribution(
                candidate_id=candidate.candidate_id,
                status="supported",
                supporting_fact_ids=supporting_fact_ids,
                max_supported_level="process",
            )
    if candidate.candidate_id == "fd_exhaustion_risk":
        supporting_fact_ids = [
            fact_id
            for fact_id in ("fact_fd_high", "fact_fd_growth")
            if fact_id in fact_ids
        ]
        if {"fact_fd_high", "fact_fd_growth"}.issubset(fact_ids):
            return GuardedAttribution(
                candidate_id=candidate.candidate_id,
                status="supported",
                supporting_fact_ids=supporting_fact_ids,
                max_supported_level="process",
            )
    supporting_fact_ids = [
        fact_id
        for fact_id in ("fact_fd_high", "fact_fd_growth")
        if fact_id in fact_ids
    ]
    if "fd_exhaustion" in symptom_types and supporting_fact_ids:
        level = "process" if len(supporting_fact_ids) >= 2 else "resource"
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="supported",
            supporting_fact_ids=supporting_fact_ids,
            max_supported_level=level,
        )
    missing = []
    if "fact_fd_high" not in fact_ids:
        missing.append("缺少 FD 高水位事实")
    if "fact_fd_growth" not in fact_ids:
        missing.append("缺少 FD 增长事实")
    return GuardedAttribution(
        candidate_id=candidate.candidate_id,
        status="missing_evidence",
        supporting_fact_ids=_candidate_fact_refs(candidate.evidence_refs, fact_map),
        missing_evidence=missing,
        max_supported_level=_max_level([item.level for item in localizations]) if localizations else "resource",
    )


def _thread_attribution(
    candidate: CandidateCause,
    fact_map: dict[str, AnalysisFact],
    symptom_types: set[str],
    localizations: list[AnalysisLocalization],
) -> GuardedAttribution:
    fact_ids = set(fact_map)
    if candidate.candidate_id == "context_switch_storm":
        supporting_fact_ids = [
            fact_id
            for fact_id in ("fact_ctx_switch_high", "fact_thread_high")
            if fact_id in fact_ids
        ]
        if "fact_ctx_switch_high" in fact_ids:
            return GuardedAttribution(
                candidate_id=candidate.candidate_id,
                status="supported",
                supporting_fact_ids=supporting_fact_ids,
                max_supported_level="syscall",
            )
    if candidate.candidate_id == "thread_pool_starvation":
        supporting_fact_ids = [
            fact_id
            for fact_id in ("fact_ctx_switch_high", "fact_thread_stable")
            if fact_id in fact_ids
        ]
        if {"fact_ctx_switch_high", "fact_thread_stable"}.issubset(fact_ids):
            return GuardedAttribution(
                candidate_id=candidate.candidate_id,
                status="supported",
                supporting_fact_ids=supporting_fact_ids,
                max_supported_level="process",
            )
    if candidate.candidate_id == "thread_count_excessive":
        supporting_fact_ids = [fact_id for fact_id in ("fact_thread_high",) if fact_id in fact_ids]
        if "fact_thread_high" in fact_ids:
            return GuardedAttribution(
                candidate_id=candidate.candidate_id,
                status="supported",
                supporting_fact_ids=supporting_fact_ids,
                max_supported_level="thread",
            )
    supporting_fact_ids = [
        fact_id
        for fact_id in ("fact_thread_high", "fact_thread_growth")
        if fact_id in fact_ids
    ]
    if "thread_pressure" in symptom_types and supporting_fact_ids:
        level = "thread" if len(supporting_fact_ids) >= 2 else "process"
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="supported",
            supporting_fact_ids=supporting_fact_ids,
            max_supported_level=level,
        )
    missing = []
    if "fact_thread_high" not in fact_ids:
        missing.append("缺少线程数高事实")
    if "fact_thread_growth" not in fact_ids:
        missing.append("缺少线程数增长事实")
    return GuardedAttribution(
        candidate_id=candidate.candidate_id,
        status="missing_evidence",
        supporting_fact_ids=_candidate_fact_refs(candidate.evidence_refs, fact_map),
        missing_evidence=missing,
        max_supported_level=_max_level([item.level for item in localizations]) if localizations else "resource",
    )


def _network_attribution(
    candidate: CandidateCause,
    fact_map: dict[str, AnalysisFact],
    symptom_types: set[str],
) -> GuardedAttribution:
    fact_ids = set(fact_map)
    if candidate.candidate_id == "network_bandwidth_saturation":
        supporting_fact_ids = [
            fact_id
            for fact_id in ("fact_network_rx_high", "fact_network_tx_high")
            if fact_id in fact_ids
        ]
        if supporting_fact_ids:
            return GuardedAttribution(
                candidate_id=candidate.candidate_id,
                status="supported",
                supporting_fact_ids=supporting_fact_ids,
                max_supported_level="resource",
            )
    if candidate.candidate_id == "network_io_correlation":
        supporting_fact_ids = [
            fact_id
            for fact_id in ("fact_network_rx_high", "fact_network_tx_high")
            if fact_id in fact_ids
        ]
        if {"fact_network_rx_high", "fact_network_tx_high"}.issubset(fact_ids):
            return GuardedAttribution(
                candidate_id=candidate.candidate_id,
                status="supported",
                supporting_fact_ids=supporting_fact_ids,
                max_supported_level="resource",
            )
    supporting_fact_ids = [
        fact_id
        for fact_id in ("fact_network_rx_high", "fact_network_tx_high")
        if fact_id in fact_ids
    ]
    if "network_pressure" in symptom_types and supporting_fact_ids:
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="supported",
            supporting_fact_ids=supporting_fact_ids,
            max_supported_level="resource",
        )
    missing = []
    if "fact_network_rx_high" not in fact_ids:
        missing.append("缺少网络接收高吞吐事实")
    if "fact_network_tx_high" not in fact_ids:
        missing.append("缺少网络发送高吞吐事实")
    return GuardedAttribution(
        candidate_id=candidate.candidate_id,
        status="missing_evidence",
        supporting_fact_ids=_candidate_fact_refs(candidate.evidence_refs, fact_map),
        missing_evidence=missing,
    )


def _cross_attribution(
    candidate: CandidateCause,
    fact_map: dict[str, AnalysisFact],
    symptoms: list[AnalysisSymptom],
    localizations: list[AnalysisLocalization],
) -> GuardedAttribution:
    supporting_fact_ids = _candidate_fact_refs(candidate.evidence_refs, fact_map)
    if len(supporting_fact_ids) < 2:
        return GuardedAttribution(
            candidate_id=candidate.candidate_id,
            status="missing_evidence",
            supporting_fact_ids=supporting_fact_ids,
            missing_evidence=["跨证据候选需要至少两条互补事实共同支持。"],
            max_supported_level=_max_level([item.level for item in localizations]) if localizations else "resource",
        )
    fact_levels = [_fact_level(fact_id) for fact_id in supporting_fact_ids]
    supported_level = _max_level(fact_levels)
    return GuardedAttribution(
        candidate_id=candidate.candidate_id,
        status="supported",
        supporting_fact_ids=supporting_fact_ids,
        max_supported_level=supported_level,
    )


def _challenge(
    attribution: GuardedAttribution,
    fact_map: dict[str, AnalysisFact],
) -> EvidenceChallenge:
    tests: list[EvidenceChallengeTest] = []
    for fact_id in attribution.supporting_fact_ids:
        if attribution.max_supported_level == "function" and fact_id == "fact_top_function_0":
            tests.append(EvidenceChallengeTest(
                removed_fact_id=fact_id,
                result="downgrade_level",
                meaning="缺少函数热点事实后，只能定位到资源层，不能定位到具体函数。",
            ))
        else:
            tests.append(EvidenceChallengeTest(
                removed_fact_id=fact_id,
                result="forbidden",
                meaning="缺少该关键事实后，当前归因结论不再成立。",
            ))
    evidence_window = _first_fact_window(attribution.supporting_fact_ids, fact_map)
    return EvidenceChallenge(
        candidate_id=attribution.candidate_id,
        critical_fact_ids=attribution.supporting_fact_ids,
        critical_fact_groups=[list(attribution.supporting_fact_ids)] if attribution.supporting_fact_ids else [],
        tests=tests,
        conclusion_stability=(
            "stable"
            if len(attribution.supporting_fact_ids) >= 2
            or (
                attribution.max_supported_level == "function"
                and "fact_top_function_0" in attribution.supporting_fact_ids
            )
            else "fragile"
        ),
        evidence_window=evidence_window,
        timing_relation=_timing_relation_from_window(evidence_window),
        delayed_followup_reproduction_status=_facts_delayed_followup_status(
            [fact_map[fact_id] for fact_id in attribution.supporting_fact_ids if fact_id in fact_map]
        ),
    )


def _select_primary_cause(
    attributions: list[GuardedAttribution],
    challenges: list[EvidenceChallenge],
    localizations: list[AnalysisLocalization],
) -> tuple[str | None, str, float]:
    stable_ids = {
        challenge.candidate_id
        for challenge in challenges
        if challenge.conclusion_stability == "stable"
    }
    allowed = [item for item in attributions if item.status == "supported" and item.candidate_id in stable_ids]
    if not allowed:
        return None, "", 0.0

    localization_level = _max_level([item.level for item in localizations]) if localizations else "resource"
    support_counts = {
        item.candidate_id: len(item.supporting_fact_ids)
        for item in allowed
    }
    opposing_counts = {
        item.candidate_id: len(item.opposing_fact_ids)
        for item in allowed
    }
    ranking = sorted(
        allowed,
        key=lambda item: (
            -_LEVEL_ORDER[item.max_supported_level],
            -support_counts[item.candidate_id],
            opposing_counts[item.candidate_id],
            item.candidate_id,
        ),
    )
    primary = ranking[0]
    support_count = support_counts[primary.candidate_id]
    opposing_count = opposing_counts[primary.candidate_id]
    level_bonus = {
        "resource": 0.05,
        "process": 0.10,
        "thread": 0.15,
        "syscall": 0.20,
        "function": 0.30,
        "call_path": 0.35,
    }.get(primary.max_supported_level, 0.05)
    stability_score = min(
        1.0,
        0.35
        + level_bonus
        + min(support_count * 0.12, 0.30)
        + (0.10 if not opposing_count else 0.0)
        + (0.10 if primary.max_supported_level == localization_level else 0.0)
    )
    reason_parts = [
        f"{primary.max_supported_level} 层级更深",
        f"支持事实 {support_count} 条",
    ]
    if opposing_count:
        reason_parts.append(f"存在 {opposing_count} 条反向证据")
    else:
        reason_parts.append("无明显反向证据")
    if primary.max_supported_level == localization_level:
        reason_parts.append("与定位层级一致")
    return primary.candidate_id, "、".join(reason_parts), round(stability_score, 3)


def _can_fact_refute(
    supporting_fact_ids: list[str],
    opposing_fact_id: str,
    fact_map: dict[str, AnalysisFact],
) -> bool:
    opposing = fact_map.get(opposing_fact_id)
    if opposing is None:
        return False
    if opposing.timing_relation in {"delayed_followup", "stale_window"}:
        for fact_id in supporting_fact_ids:
            supporting = fact_map.get(fact_id)
            if supporting is not None and supporting.timing_relation == "same_window":
                return False
    return True


def _first_fact_window(fact_ids: list[str], fact_map: dict[str, AnalysisFact]) -> dict:
    for fact_id in fact_ids:
        fact = fact_map.get(fact_id)
        if fact is not None and fact.evidence_window:
            return dict(fact.evidence_window)
    return {}


def _conclusion_window(
    facts: list[AnalysisFact],
    attributions: list[GuardedAttribution],
    allowed: list[str],
) -> dict:
    fact_map = {fact.fact_id: fact for fact in facts}
    allowed_set = set(allowed)
    for attribution in attributions:
        if attribution.candidate_id not in allowed_set:
            continue
        window = _first_fact_window(attribution.supporting_fact_ids, fact_map)
        if window:
            return window
    return _first_fact_window([fact.fact_id for fact in facts], fact_map)


def _conclusion_timing_relation(
    facts: list[AnalysisFact],
    attributions: list[GuardedAttribution],
    allowed: list[str],
) -> str:
    window = _conclusion_window(facts, attributions, allowed)
    return _timing_relation_from_window(window)


def _delayed_followup_reproduction_status(facts: list[AnalysisFact]) -> str:
    return _facts_delayed_followup_status(facts)


def _facts_delayed_followup_status(facts: list[AnalysisFact]) -> str:
    delayed = [fact for fact in facts if fact.timing_relation == "delayed_followup"]
    if not delayed:
        return "not_applicable"
    if any(fact.status == "observed" for fact in delayed):
        return "reproduced"
    if all(fact.status == "normal" for fact in delayed):
        return "not_reproduced"
    return "unknown"


def _non_refutable_evidence_boundaries(
    facts: list[AnalysisFact],
    attributions: list[GuardedAttribution],
) -> list[str]:
    fact_map = {fact.fact_id: fact for fact in facts}
    boundaries: list[str] = []
    for attribution in attributions:
        if attribution.status != "supported":
            continue
        for fact_id in attribution.supporting_fact_ids:
            fact = fact_map.get(fact_id)
            if fact is not None and fact.timing_relation == "same_window":
                text = f"{fact_id}:same_window evidence cannot be directly refuted by delayed_followup non-reproduction"
                if text not in boundaries:
                    boundaries.append(text)
    return boundaries


def _derive_collection_gaps(
    evidence: EvidenceInput,
    localizations: list[AnalysisLocalization],
    attributions: list[GuardedAttribution],
    max_supported_level: str,
    stability_score: float,
) -> tuple[list[str], list[str], list[str]]:
    missing_evidence: list[str] = []
    blocked_upgrades: list[str] = []
    collection_gaps: list[str] = []
    tool_names = _tool_result_names(evidence)
    collector_type = str(evidence.task_metadata.get("collector_type", "") or "")
    evidence_index = evidence.evidence_index or {}
    evidence_index = evidence_index if isinstance(evidence_index, dict) else {}
    localization_levels = {item.level for item in localizations}
    candidate_domains = {_candidate_domain(item.candidate_id) for item in attributions}
    candidate_text = " ".join(
        " ".join([str(item.candidate_id), *[str(missing) for missing in item.missing_evidence]])
        for item in attributions
    ).lower()

    def add_gap(capability: str, missing_text: str, blocked_text: str) -> None:
        if capability not in collection_gaps:
            collection_gaps.append(capability)
        if missing_text not in missing_evidence:
            missing_evidence.append(missing_text)
        if blocked_text not in blocked_upgrades:
            blocked_upgrades.append(blocked_text)

    if max_supported_level in {"function", "call_path"}:
        if not _has_collection_capability(tool_names, collector_type, "off_cpu_wait_profile"):
            add_gap(
                "off_cpu_wait_profile",
                "缺少 off_cpu_wait_profile 证据，无法判断函数热点到底是执行密集还是等待密集。",
                "function -> process 仍被 off_cpu_wait_profile 阻断。",
            )
        if not _has_collection_capability(tool_names, collector_type, "trace_endpoint_profile"):
            add_gap(
                "trace_endpoint_profile",
                "缺少 trace_endpoint_profile 证据，无法把函数热点回连到 endpoint 或调用路径。",
                "function -> call_path 仍被 trace_endpoint_profile 阻断。",
            )

    if max_supported_level in {"process", "thread", "syscall"} and not _has_collection_capability(tool_names, collector_type, "trace_endpoint_profile"):
        add_gap(
            "trace_endpoint_profile",
            f"当前证据只能稳定定位到 {max_supported_level} 层，缺少 trace_endpoint_profile 以继续回连更上层上下文。",
            f"{max_supported_level} -> call_path 仍被 trace_endpoint_profile 阻断。",
        )

    if stability_score < 0.75 and not _has_collection_capability(tool_names, collector_type, "baseline_window_profile"):
        add_gap(
            "baseline_window_profile",
            "当前结论稳定性偏低，缺少 baseline_window_profile 进行多窗口对齐和重复任务稳定化。",
            "低稳定性结果需要 baseline_window_profile 进行重复窗口验证。",
        )

    for attribution in attributions:
        for item in attribution.missing_evidence:
            if item not in missing_evidence:
                missing_evidence.append(item)

    has_log_scan = _has_collection_capability(tool_names, collector_type, "log_scan") or bool(evidence_index.get("log_scan"))
    has_dependency_check = (
        _has_collection_capability(tool_names, collector_type, "dependency_check")
        or bool(evidence_index.get("dependency_check"))
    )
    has_redis_check = _has_collection_capability(tool_names, collector_type, "redis_check") or bool(evidence_index.get("redis_check"))
    dependency_suspected = (
        "network" in candidate_domains
        or "network" in candidate_text
        or "net_" in candidate_text
        or "downstream" in candidate_text
        or "dependency" in candidate_text
        or "latency" in candidate_text
        or bool(evidence_index.get("log_scan", {}).get("evidence_index", {}).get("dependencies"))
    )
    redis_suspected = "redis" in candidate_text
    log_suspected = dependency_suspected or any(
        token in candidate_text
        for token in ("error", "exception", "timeout", "failed", "unavailable", "oom")
    )

    if dependency_suspected and not has_dependency_check:
        add_gap(
            "dependency_check",
            "缺少 dependency_check 证据，无法确认下游依赖的 DNS/TCP/HTTP/gRPC 可达性与延迟状态。",
            "downstream_dependency -> concrete_dependency 仍被 dependency_check 阻断。",
        )
    if log_suspected and not has_log_scan:
        add_gap(
            "log_scan",
            "缺少 log_scan 证据，无法提取异常日志簇、trace_id、endpoint 和依赖错误上下文。",
            "downstream_dependency -> evidence_cluster 仍被 log_scan 阻断。",
        )
    if redis_suspected and not has_redis_check:
        add_gap(
            "redis_check",
            "缺少 redis_check 证据，无法确认 Redis PING、INFO、SLOWLOG 和 LATENCY 状态。",
            "redis_dependency -> concrete_redis_signal 仍被 redis_check 阻断。",
        )

    if "function" in localization_levels and max_supported_level == "function":
        func_targets = [item.target for item in localizations if item.level == "function" and item.target]
        if func_targets and "trace_endpoint_profile" not in collection_gaps:
            add_gap(
                "trace_endpoint_profile",
                f"函数热点 {func_targets[0]} 还缺少 trace_endpoint_profile，无法继续上推到调用路径。",
                f"function -> call_path 仍被 trace_endpoint_profile 阻断。",
            )

    return missing_evidence, blocked_upgrades, collection_gaps


def _build_boundary_reason(
    can_claim_root_cause: bool,
    max_supported_level: str,
    missing_evidence: list[str],
    blocked_upgrades: list[str],
    timing_relation: str,
    delayed_followup_reproduction_status: str,
) -> str:
    timing_note = ""
    if timing_relation == "same_window" and delayed_followup_reproduction_status == "not_reproduced":
        timing_note = " 延迟补采未复现只能降低后续确认度，不能直接反证同窗证据。"
    elif timing_relation != "unknown":
        timing_note = f" 结论窗口关系：{timing_relation}。"

    if can_claim_root_cause:
        if max_supported_level == "function":
            return "当前证据已支持函数层结论，但仍缺少 off-CPU / trace 证据，无法继续上推到调用路径或等待机制。" + timing_note
        if max_supported_level in {"process", "thread", "syscall"}:
            return f"当前证据已稳定定位到 {max_supported_level} 层，但仍缺少更深层上下文采集，无法继续上推。" + timing_note
        return "存在由关键事实共同支持且通过证据反问校验的归因结论。" + timing_note

    if blocked_upgrades:
        return "；".join(blocked_upgrades[:2]) + timing_note
    if missing_evidence:
        return missing_evidence[0] + timing_note
    return "当前事实不足以稳定支持任何明确根因，只能保留现象或较粗定位层级。" + timing_note


def _derive_next_evidence_requests(
    localizations: list[AnalysisLocalization],
    boundary: ConclusionBoundary,
    stability_score: float,
    collection_gaps: list[str],
    missing_evidence: list[str],
    blocked_upgrades: list[str],
) -> list[str]:
    requests: list[str] = []
    gap_set = set(collection_gaps)
    localization_levels = {item.level for item in localizations}

    def add_request(request_id: str) -> None:
        if request_id not in requests:
            requests.append(request_id)

    if (
        boundary.max_supported_level in {"function", "call_path", "line"}
        or "function" in localization_levels
        or "call_path" in localization_levels
    ):
        if "off_cpu_wait_profile" in gap_set:
            add_request("off_cpu_wait_profile")
        if "trace_endpoint_profile" in gap_set:
            add_request("trace_endpoint_profile")
    else:
        if "off_cpu_wait_profile" in gap_set:
            add_request("off_cpu_wait_profile")
        if "trace_endpoint_profile" in gap_set:
            add_request("trace_endpoint_profile")

    if stability_score < 0.75 and "baseline_window_profile" in gap_set:
        add_request("baseline_window_profile")

    for request_id in ("dependency_check", "log_scan", "redis_check"):
        if request_id in gap_set:
            add_request(request_id)

    if not requests:
        for request_id in (
            "off_cpu_wait_profile",
            "trace_endpoint_profile",
            "baseline_window_profile",
            "dependency_check",
            "log_scan",
            "redis_check",
        ):
            if request_id in gap_set:
                add_request(request_id)

    if not requests and blocked_upgrades:
        if any("call_path" in item for item in blocked_upgrades):
            add_request("trace_endpoint_profile")
        if any("process" in item and "off_cpu_wait_profile" in item for item in blocked_upgrades):
            add_request("off_cpu_wait_profile")

    if not requests and missing_evidence:
        for item in missing_evidence:
            if "off_cpu_wait_profile" in item:
                add_request("off_cpu_wait_profile")
            elif "trace_endpoint_profile" in item:
                add_request("trace_endpoint_profile")
            elif "baseline_window_profile" in item:
                add_request("baseline_window_profile")
            elif "dependency_check" in item:
                add_request("dependency_check")
            elif "log_scan" in item:
                add_request("log_scan")
            elif "redis_check" in item:
                add_request("redis_check")

    return requests


def _derive_ai_tree(
    evidence: EvidenceInput,
    facts: list[AnalysisFact],
    localizations: list[AnalysisLocalization],
    attributions: list[GuardedAttribution],
    boundary: ConclusionBoundary,
    conflict_branch: dict | None,
    stability_score: float,
    missing_evidence: list[str],
    blocked_upgrades: list[str],
    collection_gaps: list[str],
) -> list[AnalysisTreeDecision]:
    tree: list[AnalysisTreeDecision] = []
    deepest = _deepest_localization(localizations)
    entry_level = boundary.max_supported_level
    entry_refs = [fact.evidence_ref for fact in facts[:3] if fact.evidence_ref]
    next_requests = _derive_next_evidence_requests(
        localizations,
        boundary,
        stability_score,
        collection_gaps,
        missing_evidence,
        blocked_upgrades,
    )
    conservative = bool(collection_gaps or missing_evidence or blocked_upgrades)

    if boundary.can_claim_root_cause and not conservative:
        entry_decision = "stop"
        leaf_status = "clear_leaf"
    elif boundary.can_claim_root_cause:
        entry_decision = "downgrade"
        leaf_status = "conservative_leaf"
    else:
        entry_decision = "stop"
        leaf_status = "unknown_leaf" if not localizations else "conservative_leaf"

    tree.append(AnalysisTreeDecision(
        node_id="tree_root",
        level=entry_level,
        branch_key=f"entry:{entry_level}",
        decision=entry_decision,
        leaf_status=leaf_status,
        next_evidence_requests=next_requests,
        evidence_refs=entry_refs,
        reason=_tree_reason(boundary, next_requests, conservative),
    ))

    if conflict_branch is not None:
        tree.append(AnalysisTreeDecision(
            node_id="tree_conflict",
            level=boundary.max_supported_level,
            branch_key=f"conflict:{conflict_branch['conflict_type']}",
            decision=conflict_branch["decision"],
            leaf_status=conflict_branch["leaf_status"],
            conflict_type=conflict_branch["conflict_type"],
            evidence_family=conflict_branch["evidence_family"],
            conflict_candidates=conflict_branch["conflict_candidates"],
            next_evidence_requests=next_requests,
            evidence_refs=conflict_branch["evidence_refs"],
            reason=conflict_branch["reason"],
        ))

    if deepest is not None:
        tree.append(AnalysisTreeDecision(
            node_id="tree_leaf",
            level=deepest.level,
            branch_key=f"{deepest.level}:{deepest.target or 'unknown'}",
            decision="stop" if deepest.level == boundary.max_supported_level else "continue",
            leaf_status=conflict_branch["leaf_status"] if conflict_branch is not None else leaf_status,
            next_evidence_requests=next_requests,
            evidence_refs=deepest.evidence_refs or [fact.evidence_ref for fact in facts if fact.evidence_ref][-2:],
            reason=_tree_leaf_reason(deepest, boundary, attributions, conservative, conflict_branch),
        ))
    return tree


def _derive_controlled_ai_tree(
    evidence: EvidenceInput,
    facts: list[AnalysisFact],
    localizations: list[AnalysisLocalization],
    attributions: list[GuardedAttribution],
    boundary: ConclusionBoundary,
    legacy_tree: list[AnalysisTreeDecision],
    stability_score: float,
    missing_evidence: list[str],
    blocked_upgrades: list[str],
    collection_gaps: list[str],
    conflict_branch: dict | None,
    primary_cause_id: str | None,
) -> ControlledAITree:
    next_requests = _unique_strings(
        request
        for node in legacy_tree
        for request in node.next_evidence_requests
    )
    source_context_hash = _source_context_hash(evidence.source_context)
    primary_id = primary_cause_id if boundary.can_claim_root_cause else None
    candidates = [
        _controlled_candidate(
            attribution,
            facts,
            primary_id,
            missing_evidence,
            blocked_upgrades,
            boundary.max_supported_level,
        )
        for attribution in sorted(attributions, key=lambda item: item.candidate_id)
    ]
    if not candidates:
        candidates.append(AITreeCandidateNode(
            candidate_id="unknown_evidence_gap",
            role="unknown",
            claim="当前结构化证据不足，不能形成可验证的根因候选。",
            supported_level=boundary.max_supported_level,
            confidence=0.0,
            status="unknown",
            self_challenge=AITreeSelfChallenge(
                missing_evidence=missing_evidence or ["缺少可区分候选原因的结构化证据。"],
                what_would_change_my_mind="补齐同窗栈、Trace、日志或依赖检查后重新评估。",
            ),
        ))

    layer0 = AITreeLayer(
        layer_id="layer_0_coarse_candidates",
        depth=0,
        generated_by="ai_guarded",
        summary="基于当前结构化证据形成粗候选集合，并按证据支持度分组。",
        primary_causes=[item for item in candidates if item.role == "primary"][:1],
        secondary_causes=[item for item in candidates if item.role == "secondary"][:3],
        rejected_causes=[item for item in candidates if item.role == "rejected"][:4],
        unknown_causes=[item for item in candidates if item.role == "unknown"][:4],
    )
    if not layer0.primary_causes and layer0.secondary_causes:
        promoted = layer0.secondary_causes[0].model_copy(update={"role": "primary"})
        layer0 = layer0.model_copy(update={
            "primary_causes": [promoted],
            "secondary_causes": layer0.secondary_causes[1:],
        })

    layers = [layer0]
    edges: list[AITreeProbeEdge] = []
    if next_requests:
        layer1 = AITreeLayer(
            layer_id="layer_1_requested_evidence_boundary",
            depth=1,
            generated_by="ai_guarded",
            summary="AI 树请求最小必要补证；回流前只记录缺口和停止边界，不强行升级结论。",
            primary_causes=[],
            secondary_causes=[],
            rejected_causes=[],
            unknown_causes=[
                AITreeCandidateNode(
                    candidate_id=f"gap_{gap}",
                    role="unknown",
                    claim=f"需要补充 {gap} 后才能继续收敛候选。",
                    supported_level=boundary.max_supported_level,
                    confidence=max(0.0, min(0.45, stability_score)),
                    status="missing_evidence",
                    self_challenge=AITreeSelfChallenge(
                        missing_evidence=[gap],
                        what_would_change_my_mind=f"{gap} 产生同窗结构化证据并引用到 evidence_refs。",
                    ),
                )
                for gap in next_requests[:3]
            ],
        )
        layers.append(layer1)
        edges.append(AITreeProbeEdge(
            edge_id="edge_layer_0_to_layer_1",
            from_layer_id=layer0.layer_id,
            to_layer_id=layer1.layer_id,
            probe_requests=next_requests[:3],
            probe_results=[
                AITreeProbeResult(
                    status="not_started",
                    evidence_refs=[],
                    blocked_reason="等待编排器按 fingerprint 复用或创建已注册采集任务。",
                )
            ],
            reuse_status="not_checked",
            effect="pending",
            reason="当前证据存在缺口，优先补最小必要证据而不是直接给更细结论。",
        ))

    if conflict_branch is not None:
        conflict_layer = AITreeLayer(
            layer_id="layer_conflict_review",
            depth=len(layers),
            generated_by="ai_guarded",
            summary=str(conflict_branch.get("reason") or "检测到候选冲突，当前保持保守边界。"),
            rejected_causes=[
                AITreeCandidateNode(
                    candidate_id=f"conflict_{candidate_id}",
                    role="rejected",
                    claim=f"候选 {candidate_id} 在当前冲突分枝中未被提升为主因。",
                    supported_level=boundary.max_supported_level,
                    confidence=0.2,
                    status="weakened",
                    evidence_refs=conflict_branch.get("evidence_refs", []),
                    self_challenge=AITreeSelfChallenge(
                        opposing_evidence_refs=conflict_branch.get("evidence_refs", []),
                        why_not_other_claims="同窗或跨证据族冲突存在，不能把所有候选同时提升为主因。",
                        what_would_change_my_mind="补齐冲突证据族并看到其中一个候选被同窗证据稳定支持。",
                    ),
                )
                for candidate_id in conflict_branch.get("conflict_candidates", [])
                if candidate_id != primary_id
            ],
        )
        layers.append(conflict_layer)

    final_primary = [item.candidate_id for item in layer0.primary_causes]
    final_secondary = [item.candidate_id for item in layer0.secondary_causes]
    final_rejected = _unique_strings(
        item.candidate_id
        for layer in layers
        for item in layer.rejected_causes
    )
    final_unknown = _unique_strings(
        item.candidate_id
        for layer in layers
        for item in layer.unknown_causes
    )
    return ControlledAITree(
        tree_id=f"controlled_ai_tree_{_stable_digest([fact.fact_id for fact in facts], final_primary, next_requests)}",
        source_context_hash=source_context_hash,
        final_supported_level=boundary.max_supported_level,
        stop_reason=boundary.reason,
        budget=AITreeBudgetSnapshot(
            used_ai_rounds=len(layers),
            used_probe_requests=len(next_requests),
        ),
        layers=layers,
        probe_edges=edges,
        final_primary_causes=final_primary,
        final_secondary_causes=final_secondary,
        final_rejected_causes=final_rejected,
        final_unknown_causes=final_unknown,
    )


def _controlled_candidate(
    attribution: GuardedAttribution,
    facts: list[AnalysisFact],
    primary_id: str | None,
    missing_evidence: list[str],
    blocked_upgrades: list[str],
    boundary_level: str,
) -> AITreeCandidateNode:
    fact_map = {fact.fact_id: fact for fact in facts}
    refs = _unique_strings(
        fact_map[fact_id].evidence_ref
        for fact_id in attribution.supporting_fact_ids
        if fact_id in fact_map and fact_map[fact_id].evidence_ref
    )
    opposing_refs = _unique_strings(
        fact_map[fact_id].evidence_ref
        for fact_id in attribution.opposing_fact_ids
        if fact_id in fact_map and fact_map[fact_id].evidence_ref
    )
    if attribution.status == "supported" and attribution.candidate_id == primary_id:
        role = "primary"
        confidence = 0.82
    elif attribution.status == "supported":
        role = "secondary"
        confidence = 0.64
    elif attribution.status == "forbidden":
        role = "rejected"
        confidence = 0.1
    else:
        role = "unknown"
        confidence = 0.35
    missing = _unique_strings([*attribution.missing_evidence, *missing_evidence[:3], *blocked_upgrades[:2]])
    return AITreeCandidateNode(
        candidate_id=attribution.candidate_id,
        role=role,
        claim=f"{attribution.candidate_id} 当前状态为 {attribution.status}，最高支持到 {attribution.max_supported_level or boundary_level} 层。",
        supported_level=attribution.max_supported_level or boundary_level,
        confidence=confidence,
        status=attribution.status,
        evidence_refs=refs,
        self_challenge=AITreeSelfChallenge(
            why_this_claim="该候选只使用 supporting_fact_ids 能引用到的事实作为支撑。",
            why_not_other_claims="其他候选需要更强同窗证据或存在反驳事实，不能无证据提升。",
            supporting_evidence_refs=refs,
            opposing_evidence_refs=opposing_refs,
            missing_evidence=missing,
            what_would_change_my_mind="新增同目标、同窗口、已结构化的反向证据，或补齐缺失探针后主证据不再成立。",
        ),
    )


def _source_context_hash(source_context: dict | None) -> str | None:
    if not isinstance(source_context, dict) or not _has_source_context(source_context):
        return None
    return f"sha256:{_stable_digest(source_context)}"


def _stable_digest(*items: object) -> str:
    payload = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _unique_strings(items: Iterable[object]) -> list[str]:
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _tree_reason(
    boundary: ConclusionBoundary,
    next_requests: list[str],
    conservative: bool,
) -> str:
    if boundary.can_claim_root_cause and not conservative:
        return f"当前证据已足够稳定下结论，停在 {boundary.max_supported_level} 层。"
    if next_requests:
        return f"当前树需继续补证，优先请求：{next_requests[0]}。"
    return f"当前证据只能保守停留在 {boundary.max_supported_level} 层。"


def _tree_leaf_reason(
    localization: AnalysisLocalization,
    boundary: ConclusionBoundary,
    attributions: list[GuardedAttribution],
    conservative: bool,
    conflict_branch: dict | None,
) -> str:
    if conflict_branch is not None:
        conflict_type = str(conflict_branch.get("conflict_type", "conflict"))
        candidates = conflict_branch.get("conflict_candidates", [])
        if candidates:
            return f"检测到 {conflict_type}，当前叶子保守停留，优先保留 {candidates[0]}。"
        return f"检测到 {conflict_type}，当前叶子保守停留。"
    if localization.level == boundary.max_supported_level and not conservative:
        return f"定位到 {localization.level} 层 {localization.target or 'unknown'}，可以稳定停止。"
    if conservative:
        return f"定位到 {localization.level} 层 {localization.target or 'unknown'}，但仍需保守停留。"
    supported = [item.candidate_id for item in attributions if item.status == "supported"]
    if supported:
        return f"当前叶子与受支持原因 {supported[0]} 对齐。"
    return f"当前叶子只代表 {localization.level} 层上下文，没有足够证据继续升级。"


def _derive_conflict_branch(
    attributions: list[GuardedAttribution],
    facts: list[AnalysisFact],
    primary_cause_id: str | None,
) -> dict | None:
    supported = [item for item in attributions if item.status == "supported"]
    opposing_supported = [item for item in attributions if item.opposing_fact_ids]
    if len(supported) < 2:
        if not _has_same_window_opposition(opposing_supported, facts):
            return None
        candidate_ids = sorted(item.candidate_id for item in opposing_supported)
        evidence_refs = _same_window_conflict_refs(opposing_supported, facts)
        return {
            "conflict_type": "same_window_collector_conflict",
            "evidence_family": sorted({_candidate_domain(item.candidate_id) for item in opposing_supported}),
            "conflict_candidates": candidate_ids,
            "decision": "downgrade",
            "leaf_status": "conservative_leaf",
            "evidence_refs": evidence_refs,
            "reason": (
                "检测到 same_window_collector_conflict，同一异常窗口内不同采集器证据相互矛盾，"
                "当前结论降级为保守叶子。"
            ),
        }

    families = sorted({
        _candidate_domain(item.candidate_id)
        for item in supported
    })
    if len([family for family in families if family != "other"]) <= 1 and not opposing_supported:
        return None

    if _has_same_window_opposition(opposing_supported, facts):
        conflict_type = "same_window_collector_conflict"
    elif len([family for family in families if family != "other"]) > 1:
        conflict_type = "cross_family_conflict"
    else:
        conflict_type = "opposing_evidence_conflict"
    candidate_ids = sorted(item.candidate_id for item in supported)
    evidence_refs = sorted({
        fact.evidence_ref
        for item in supported
        for fact in facts
        if fact.fact_id in item.supporting_fact_ids and fact.evidence_ref
    })
    reason = (
        f"检测到 {conflict_type}，优先保留 {primary_cause_id or candidate_ids[0]}，"
        f"其余支持方向降级为保守叶子。"
    )
    return {
        "conflict_type": conflict_type,
        "evidence_family": families,
        "conflict_candidates": candidate_ids,
        "decision": "downgrade",
        "leaf_status": "conservative_leaf",
        "evidence_refs": evidence_refs,
        "reason": reason,
    }


def _has_same_window_opposition(
    attributions: list[GuardedAttribution],
    facts: list[AnalysisFact],
) -> bool:
    fact_map = {fact.fact_id: fact for fact in facts}
    for attribution in attributions:
        has_same_window_support = any(
            (fact_map.get(fact_id) is not None and fact_map[fact_id].timing_relation == "same_window")
            for fact_id in attribution.supporting_fact_ids
        )
        has_same_window_opposition = any(
            (fact_map.get(fact_id) is not None and fact_map[fact_id].timing_relation == "same_window")
            for fact_id in attribution.opposing_fact_ids
        )
        if has_same_window_support and has_same_window_opposition:
            return True
    return False


def _same_window_conflict_refs(
    attributions: list[GuardedAttribution],
    facts: list[AnalysisFact],
) -> list[str]:
    fact_map = {fact.fact_id: fact for fact in facts}
    refs = []
    for attribution in attributions:
        for fact_id in [*attribution.supporting_fact_ids, *attribution.opposing_fact_ids]:
            fact = fact_map.get(fact_id)
            if fact is not None and fact.timing_relation == "same_window" and fact.evidence_ref not in refs:
                refs.append(fact.evidence_ref)
    return sorted(refs)


def _deepest_localization(localizations: list[AnalysisLocalization]) -> AnalysisLocalization | None:
    if not localizations:
        return None
    return sorted(
        localizations,
        key=lambda item: (
            _LEVEL_ORDER.get(item.level, -1),
            item.target or "",
            item.file_path or "",
            item.line_number or 0,
        ),
        reverse=True,
    )[0]


def _derive_graph_context(
    evidence: EvidenceInput,
    localizations: list[AnalysisLocalization],
) -> tuple[list[AnalysisGraphEntity], list[AnalysisGraphLink]]:
    index = evidence.evidence_index or {}
    if not isinstance(index, dict):
        return [], []

    entities: list[AnalysisGraphEntity] = []
    links: list[AnalysisGraphLink] = []
    seen_entities: set[str] = set()
    seen_links: set[tuple[str, str, str]] = set()
    context_id = None

    def add_entity(entity: AnalysisGraphEntity) -> None:
        if entity.entity_id in seen_entities:
            return
        seen_entities.add(entity.entity_id)
        entities.append(entity)

    def add_link(source_id: str, target_id: str, relation: str, evidence_ref: str) -> None:
        key = (source_id, target_id, relation)
        if key in seen_links:
            return
        seen_links.add(key)
        links.append(AnalysisGraphLink(
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            evidence_ref=evidence_ref,
            stable=True,
        ))

    context = index.get("context", {})
    if isinstance(context, dict):
        trace_id = str(context.get("trace_id") or "").strip()
        context_id = str(context.get("context_id") or "").strip()
        call_path = str(context.get("call_path") or "").strip()
        endpoint = str(context.get("endpoint") or "").strip()
        service = str(context.get("service") or "").strip()
        instance = str(context.get("instance") or "").strip()

        if trace_id:
            add_entity(AnalysisGraphEntity(
                entity_id=f"trace:{trace_id}",
                entity_type="trace",
                label=trace_id,
                evidence_ref="evidence_index.context.trace_id",
                context_id=context_id or None,
            ))
        if context_id:
            add_entity(AnalysisGraphEntity(
                entity_id=f"context:{context_id}",
                entity_type="context",
                label=context_id,
                evidence_ref="evidence_index.context.context_id",
                context_id=context_id,
            ))
        if call_path:
            add_entity(AnalysisGraphEntity(
                entity_id=f"call_path:{call_path}",
                entity_type="call_path",
                label=call_path,
                evidence_ref="evidence_index.context.call_path",
                context_id=context_id or None,
            ))
        if endpoint:
            add_entity(AnalysisGraphEntity(
                entity_id=f"endpoint:{endpoint}",
                entity_type="endpoint",
                label=endpoint,
                evidence_ref="evidence_index.context.endpoint",
                context_id=context_id or None,
            ))
        if service:
            add_entity(AnalysisGraphEntity(
                entity_id=f"service:{service}",
                entity_type="service",
                label=service,
                evidence_ref="evidence_index.context.service",
                context_id=context_id or None,
            ))
        if instance:
            add_entity(AnalysisGraphEntity(
                entity_id=f"instance:{instance}",
                entity_type="instance",
                label=instance,
                evidence_ref="evidence_index.context.instance",
                context_id=context_id or None,
            ))

        if trace_id and context_id:
            add_link(f"trace:{trace_id}", f"context:{context_id}", "contains", "evidence_index.context.trace_id")
        if context_id and call_path:
            add_link(f"context:{context_id}", f"call_path:{call_path}", "routes_to", "evidence_index.context.call_path")
        if call_path and endpoint:
            add_link(f"call_path:{call_path}", f"endpoint:{endpoint}", "invokes", "evidence_index.context.endpoint")
        if endpoint and service:
            add_link(f"endpoint:{endpoint}", f"service:{service}", "served_by", "evidence_index.context.service")
        if service and instance:
            add_link(f"service:{service}", f"instance:{instance}", "runs_on", "evidence_index.context.instance")

    hotspots = index.get("call_path_hotspots", [])
    if isinstance(hotspots, list):
        for item in hotspots[:3]:
            if not isinstance(item, dict):
                continue
            function = str(item.get("function") or "").strip()
            call_path = item.get("call_path") if isinstance(item.get("call_path"), list) else []
            endpoint = str(item.get("endpoint") or "").strip()
            service = str(item.get("service_id") or "").strip()
            instance = str(item.get("instance_id") or "").strip()
            hotspot_context_id = str(item.get("context_id") or "").strip()
            if function:
                add_entity(AnalysisGraphEntity(
                    entity_id=f"function:{function}",
                    entity_type="function",
                    label=function,
                    evidence_ref="evidence_index.call_path_hotspots",
                    context_id=hotspot_context_id or context_id or None,
                ))
            if call_path:
                path_text = ";".join(call_path)
                add_entity(AnalysisGraphEntity(
                    entity_id=f"call_path:{path_text}",
                    entity_type="call_path",
                    label=path_text,
                    evidence_ref="evidence_index.call_path_hotspots",
                    context_id=hotspot_context_id or context_id or None,
                ))
                if function:
                    add_link(f"call_path:{path_text}", f"function:{function}", "owns_hotspot", "evidence_index.call_path_hotspots")
                    add_link(f"call_path:{path_text}", f"function:{function}", "contains_hotspot", "evidence_index.call_path_hotspots")
            if endpoint:
                add_entity(AnalysisGraphEntity(
                    entity_id=f"endpoint:{endpoint}",
                    entity_type="endpoint",
                    label=endpoint,
                    evidence_ref="evidence_index.call_path_hotspots",
                    context_id=hotspot_context_id or context_id or None,
                ))
                if call_path:
                    add_link(f"call_path:{';'.join(call_path)}", f"endpoint:{endpoint}", "invokes", "evidence_index.call_path_hotspots")
            if service:
                add_entity(AnalysisGraphEntity(
                    entity_id=f"service:{service}",
                    entity_type="service",
                    label=service,
                    evidence_ref="evidence_index.call_path_hotspots",
                    context_id=hotspot_context_id or context_id or None,
                ))
                if endpoint:
                    add_link(f"endpoint:{endpoint}", f"service:{service}", "served_by", "evidence_index.call_path_hotspots")
            if instance:
                add_entity(AnalysisGraphEntity(
                    entity_id=f"instance:{instance}",
                    entity_type="instance",
                    label=instance,
                    evidence_ref="evidence_index.call_path_hotspots",
                    context_id=hotspot_context_id or context_id or None,
                ))
                if service:
                    add_link(f"service:{service}", f"instance:{instance}", "runs_on", "evidence_index.call_path_hotspots")

    for localization in localizations:
        if localization.level == "function" and localization.target:
            function_id = f"function:{localization.target}"
            add_entity(AnalysisGraphEntity(
                entity_id=function_id,
                entity_type="function",
                label=localization.target,
                evidence_ref=localization.evidence_refs[0] if localization.evidence_refs else "analysis.localization.function",
                context_id=context_id or None,
            ))
            call_path_entity = next((entity for entity in entities if entity.entity_type == "call_path"), None)
            if call_path_entity is not None:
                evidence_ref = localization.evidence_refs[0] if localization.evidence_refs else "analysis.localization.function"
                add_link(
                    call_path_entity.entity_id,
                    function_id,
                    "owns_hotspot",
                    evidence_ref,
                )
                add_link(
                    call_path_entity.entity_id,
                    function_id,
                    "contains_hotspot",
                    evidence_ref,
                )
        if localization.level == "line" and localization.file_path and localization.line_number is not None:
            line_label = f"{localization.file_path}:{localization.line_number}"
            line_id = f"line:{line_label}"
            add_entity(AnalysisGraphEntity(
                entity_id=line_id,
                entity_type="line",
                label=line_label,
                evidence_ref=localization.evidence_refs[0] if localization.evidence_refs else "analysis.localization.line",
                context_id=context_id or None,
            ))
            if localization.target:
                function_id = f"function:{localization.target}"
                evidence_ref = localization.evidence_refs[0] if localization.evidence_refs else "analysis.localization.line"
                add_link(function_id, line_id, "refines_hotspot", evidence_ref)
                add_link(function_id, line_id, "refines_to", evidence_ref)

    return entities, links


def _derive_graph_extension_points(
    graph_entities: list[AnalysisGraphEntity],
    graph_links: list[AnalysisGraphLink],
) -> list[str]:
    extension_points = ["graph_context_correlation", "graph_path_propagation", "graph_reasoning_upgrade"]
    if graph_entities:
        extension_points.append("graph_entity_replay")
    if graph_links:
        extension_points.append("graph_link_replay")
    return list(dict.fromkeys(extension_points))


def _tool_result_names(evidence: EvidenceInput) -> set[str]:
    names: set[str] = set()
    for item in evidence.tool_results:
        if isinstance(item, dict):
            tool_name = str(item.get("tool_name", "") or "")
            if tool_name:
                names.add(tool_name)
    return names


def _has_collection_capability(tool_names: set[str], collector_type: str, capability: str) -> bool:
    if capability in collector_type:
        return True
    return any(capability in name for name in tool_names)


def _candidate_domain(candidate_id: str) -> str:
    normalized = candidate_id.lower()
    if any(token in normalized for token in ("iowait", "io_", "disk", "io-wait")):
        return "io"
    if any(token in normalized for token in ("memory", "rss", "swap", "heap", "leak")):
        return "memory"
    if any(token in normalized for token in ("fd_", "fd", "descriptor")):
        return "fd"
    if any(token in normalized for token in ("thread", "ctx_switch", "context_switch", "ctx")):
        return "thread"
    if any(token in normalized for token in ("network", "net_")):
        return "network"
    if normalized.startswith("cross_"):
        return "cross"
    if any(token in normalized for token in ("cpu", "hotspot", "userland", "kernel")):
        return "cpu"
    return "other"


def _candidate_fact_refs(refs: Iterable[str], fact_map: dict[str, AnalysisFact]) -> list[str]:
    found = []
    for ref in refs:
        top = ref.split(".", 1)[0].split("[", 1)[0]
        if top == "top_functions":
            fact_id = "fact_top_function_0"
        elif top == "ebpf_metrics":
            fact_id = "fact_io_latency_present"
        elif "avg_cpu_user_pct" in ref:
            fact_id = "fact_cpu_user_high"
        elif "avg_cpu_sys_pct" in ref:
            fact_id = "fact_cpu_sys_high"
        elif "avg_cpu_iowait_pct" in ref:
            fact_id = "fact_iowait_high"
        elif "ctx_nonvoluntary_rate" in ref:
            fact_id = "fact_ctx_switch_high"
        elif "vmrss_mb_max" in ref:
            fact_id = "fact_memory_peak_high"
        elif "vmrss_mb" in ref:
            fact_id = "fact_memory_rss_high"
        elif "fd_trend" in ref:
            fact_id = "fact_fd_growth"
        elif "fd_count" in ref or "fd_max" in ref:
            fact_id = "fact_fd_high"
        elif "load1m" in ref:
            fact_id = "fact_load_high"
        elif "thread_trend" in ref:
            fact_id = "fact_thread_growth"
        elif "thread_count" in ref:
            fact_id = "fact_thread_high"
        elif "net_rx_kbps" in ref:
            fact_id = "fact_network_rx_high"
        elif "net_tx_kbps" in ref:
            fact_id = "fact_network_tx_high"
        else:
            fact_id = None
        if fact_id and fact_id in fact_map and fact_id not in found:
            found.append(fact_id)
    return found


def _fact_level(fact_id: str) -> str:
    if fact_id == "fact_top_function_0":
        return "function"
    if fact_id in {"fact_thread_growth"}:
        return "thread"
    if fact_id in {"fact_fd_growth"}:
        return "process"
    if fact_id in {"fact_cpu_sys_high", "fact_ctx_switch_high"}:
        return "syscall"
    return "resource"


def _max_level(levels: Iterable[str]) -> str:
    return max(levels, key=lambda item: _LEVEL_ORDER[item], default="resource")


def _threshold_band(value: float | None, threshold: float | None) -> str:
    if value is None or threshold is None:
        return "unknown"
    if threshold == 0:
        return "above" if value > 0 else "below"
    lower = threshold * 0.9
    upper = threshold * 1.1
    if value < lower:
        return "below"
    if value > upper:
        return "above"
    return "near"


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
