"""Graph-first causal RCA strategy."""

from __future__ import annotations

from typing import Any

from server.app.ai_provider import get_ai_settings
from server.app.rca.attribution import analyze_evidence
from server.app.rca.calibrator import calibrate, format_for_llm
from server.app.rca.candidates import generate_candidates
from server.app.rca.evidence import collect_evidence
from server.app.rca.llm_client import diagnose
from server.app.rca.models import CandidateCause, DiagnosisOutcome, ToolResult
from server.app.rca.repair import build_repair_plan, execute_safe_actions
from server.app.rca.strategies.base import AnalysisContext
from server.app.rca.tools import run_rca_tools, tool_results_to_evidence


GRAPH_EVIDENCE_REF = "tool_results.build_causal_graph"


class GraphCausalRCAAnalyzer:
    """Scheme B: infer over an evidence/signal/cause graph before LLM reasoning."""

    strategy_id = "graph"

    def analyze(self, context: AnalysisContext) -> DiagnosisOutcome:
        model = context.model_name or get_ai_settings().model

        base_tools = run_rca_tools(
            task_record=context.task_record,
            top_functions=context.top_functions,
            ebpf_metrics=context.ebpf_metrics,
            baseline_diff=context.baseline_diff,
            task_events=context.task_events,
            agent_record=context.agent_record,
        )
        base_evidence = collect_evidence(
            task_id=context.task_id,
            task_record=context.task_record,
            top_functions=context.top_functions,
            ebpf_metrics=context.ebpf_metrics,
            sys_metrics=context.sys_metrics,
            evidence_index=context.evidence_index,
            suggestions=context.suggestions,
            failure_events=context.failure_events,
            baseline_diff=context.baseline_diff,
            agent_stats=context.agent_stats,
            tool_results=tool_results_to_evidence(base_tools),
        )

        candidates = generate_candidates(base_evidence, context.feedback_priors)
        graph = build_causal_graph(base_evidence, candidates)
        graph_tool = ToolResult(
            tool_name="build_causal_graph",
            status="success" if graph["signals"] else "missing",
            evidence_ref=GRAPH_EVIDENCE_REF,
            input={"strategy": self.strategy_id},
            output=graph,
            error_message=None if graph["signals"] else "没有足够异常信号构建因果图",
        )
        tool_results = [*base_tools, graph_tool]

        evidence = collect_evidence(
            task_id=context.task_id,
            task_record=context.task_record,
            top_functions=context.top_functions,
            ebpf_metrics=context.ebpf_metrics,
            sys_metrics=context.sys_metrics,
            evidence_index=context.evidence_index,
            suggestions=context.suggestions,
            failure_events=context.failure_events,
            baseline_diff=context.baseline_diff,
            agent_stats=context.agent_stats,
            tool_results=tool_results_to_evidence(tool_results),
        )

        ranked = rerank_candidates_with_graph(candidates, graph)
        calibrated = calibrate(ranked, evidence, context.feedback_priors)
        if context.analysis_pipeline == "evidence_to_attribution":
            analysis_result = analyze_evidence(base_evidence, candidates)
            evidence = evidence.model_copy(update={
                "analysis_result": analysis_result.model_dump(),
            })
            allowed_cause_ids = set(analysis_result.allowed_cause_ids)
            calibrated = [
                item for item in calibrated
                if item.candidate_id in allowed_cause_ids
            ]
            primary_cause_id = analysis_result.primary_cause_id
        else:
            primary_cause_id = None
        result = diagnose(
            context.task_id,
            evidence,
            format_for_llm(calibrated, primary_cause_id),
            model_name=model,
        )

        repair_plan = build_repair_plan(context.task_id, result.report, evidence)
        if context.auto_execute_safe and context.repo is not None:
            repair_plan = execute_safe_actions(repair_plan, context.repo)

        return DiagnosisOutcome(
            report=result,
            tool_results=tool_results,
            repair_plan=repair_plan,
        )


def build_causal_graph(evidence, candidates: list[CandidateCause]) -> dict[str, Any]:
    """Build a compact graph from current evidence and rule candidates."""
    signals = _extract_signals(evidence)
    candidate_scores = {
        candidate.candidate_id: _candidate_graph_score(candidate.candidate_id, signals)
        for candidate in candidates
    }
    nodes = (
        [{"id": f"signal:{item['id']}", "type": "signal", **item} for item in signals]
        + [
            {
                "id": f"cause:{candidate.candidate_id}",
                "type": "cause",
                "label": candidate.description,
                "rule_score": candidate.rule_score,
                "graph_score": candidate_scores[candidate.candidate_id],
            }
            for candidate in candidates
        ]
    )
    edges = []
    for candidate in candidates:
        for signal in signals:
            weight = _edge_weight(candidate.candidate_id, signal["id"])
            if weight <= 0:
                continue
            edges.append({
                "source": f"signal:{signal['id']}",
                "target": f"cause:{candidate.candidate_id}",
                "weight": weight,
                "evidence_ref": signal["evidence_ref"],
            })
    return {
        "strategy": "graph",
        "signals": signals,
        "nodes": nodes,
        "edges": edges,
        "ranked_causes": sorted(
            [
                {"candidate_id": key, "graph_score": round(value, 3)}
                for key, value in candidate_scores.items()
            ],
            key=lambda item: item["graph_score"],
            reverse=True,
        ),
    }


def rerank_candidates_with_graph(
    candidates: list[CandidateCause],
    graph: dict[str, Any],
) -> list[CandidateCause]:
    """Blend rule score with graph support while preserving the old candidate model."""
    scores = {
        item["candidate_id"]: float(item.get("graph_score", 0.0))
        for item in graph.get("ranked_causes", [])
    }
    ranked = []
    for candidate in candidates:
        graph_score = scores.get(candidate.candidate_id, 0.0)
        blended = min(1.0, candidate.rule_score * 0.70 + graph_score * 0.30)
        refs = list(dict.fromkeys([*candidate.evidence_refs, GRAPH_EVIDENCE_REF]))
        ranked.append(candidate.model_copy(update={
            "rule_score": blended,
            "evidence_refs": refs,
        }))
    ranked.sort(key=lambda item: item.rule_score, reverse=True)
    return ranked


def _extract_signals(evidence) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    if evidence.top_functions:
        top = evidence.top_functions[0]
        percent = float(top.get("percent", 0) or 0)
        if percent >= 30:
            signals.append({
                "id": "cpu_hotspot",
                "strength": min(percent / 100, 1.0),
                "label": f"Top function {top.get('name', 'unknown')} at {percent}%",
                "evidence_ref": "top_functions[0]",
            })

    summary = evidence.sys_metrics.get("summary", {}) if evidence.sys_metrics else {}
    _add_signal(signals, summary.get("avg_cpu_iowait_pct", 0), 10, "iowait_high", "sys_metrics.summary")
    _add_signal(signals, summary.get("avg_cpu_sys_pct", 0), 30, "kernel_cpu_high", "sys_metrics.summary")
    _add_signal(signals, summary.get("avg_cpu_user_pct", 0), 70, "user_cpu_high", "sys_metrics.summary")
    if summary.get("fd_trend") == "increasing":
        _append_unit_signal(signals, "fd_growth", "sys_metrics.summary")
    if summary.get("thread_trend") == "increasing":
        _append_unit_signal(signals, "thread_growth", "sys_metrics.summary")
    if float(summary.get("vmrss_mb", 0) or 0) >= 512:
        _append_unit_signal(signals, "memory_pressure", "sys_metrics.summary")

    histogram = evidence.ebpf_metrics.get("io_latency_us", {}) if evidence.ebpf_metrics else {}
    if isinstance(histogram, dict) and histogram:
        total = sum(int(value) for value in histogram.values())
        if total > 0:
            signals.append({
                "id": "io_latency",
                "strength": min(total / 1000, 1.0),
                "label": f"eBPF latency samples: {total}",
                "evidence_ref": "ebpf_metrics",
            })
    return signals


def _add_signal(signals: list[dict[str, Any]], value: Any, threshold: float, signal_id: str, evidence_ref: str) -> None:
    numeric = float(value or 0)
    if numeric >= threshold:
        signals.append({
            "id": signal_id,
            "strength": min(numeric / max(threshold * 2, 1), 1.0),
            "label": f"{signal_id}={numeric}",
            "evidence_ref": evidence_ref,
        })


def _append_unit_signal(signals: list[dict[str, Any]], signal_id: str, evidence_ref: str) -> None:
    signals.append({
        "id": signal_id,
        "strength": 0.8,
        "label": signal_id,
        "evidence_ref": evidence_ref,
    })


def _candidate_graph_score(candidate_id: str, signals: list[dict[str, Any]]) -> float:
    if not signals:
        return 0.0
    total = 0.0
    weight_total = 0.0
    for signal in signals:
        weight = _edge_weight(candidate_id, signal["id"])
        if weight <= 0:
            continue
        total += weight * float(signal["strength"])
        weight_total += weight
    if weight_total == 0:
        return 0.0
    return min(total / weight_total, 1.0)


def _edge_weight(candidate_id: str, signal_id: str) -> float:
    candidate = candidate_id.lower()
    if signal_id in {"cpu_hotspot", "user_cpu_high"} and any(token in candidate for token in ("cpu", "hotspot", "userland")):
        return 1.0
    if signal_id in {"iowait_high", "io_latency"} and any(token in candidate for token in ("io", "iowait", "disk", "wait")):
        return 1.0
    if signal_id == "kernel_cpu_high" and any(token in candidate for token in ("sys_cpu", "kernel")):
        return 1.0
    if signal_id == "fd_growth" and "fd" in candidate:
        return 1.0
    if signal_id == "thread_growth" and "thread" in candidate:
        return 1.0
    if signal_id == "memory_pressure" and "memory" in candidate:
        return 1.0
    if candidate.startswith("cross_"):
        return 0.5
    return 0.0
