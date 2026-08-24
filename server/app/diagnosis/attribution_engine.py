"""从工业采集器结构化结果构造统一事实、关系和资格结果。"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from server.app.diagnosis.attribution_models import (
    AttributionEdge,
    AttributionGraph,
    AttributionLevel,
    AttributionNode,
    CostCenterCandidate,
    EvidenceQualityEntry,
    EvidenceSignal,
    ImpactCandidate,
    ObservedRelation,
    QualificationResult,
    RepairCluster,
    ScenarioFacts,
    SourceRelation,
    TriggerCandidate,
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

_PRIMITIVE_NAMES = {
    "poll",
    "select",
    "epoll_wait",
    "futex",
    "pthread_cond_wait",
    "clock_nanosleep",
    "sleep",
    "sched_yield",
}


def build_attribution_graph(
    *,
    evidence: dict[str, Any],
    target: dict[str, Any] | None = None,
    source_snapshot: dict[str, Any] | None = None,
    source_mechanism: dict[str, Any] | None = None,
) -> AttributionGraph:
    facts = build_scenario_facts(evidence)
    source_relations = build_source_relations(
        evidence=evidence,
        source_snapshot=source_snapshot,
        source_mechanism=source_mechanism,
    )
    graph_id = "attribution:" + hashlib.sha256(
        str(sorted((target or {}).items())).encode("utf-8")
        + str(facts.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()[:16]
    nodes, runtime_relations, causal_edges, repair_clusters = _build_graph_context(
        facts=facts,
        source_relations=source_relations,
        evidence=evidence,
    )
    entities = [
        {
            "entity_id": item.candidate_id,
            "entity_type": item.level,
            "label": item.target,
            "evidence_refs": item.evidence_refs,
        }
        for item in facts.cost_centers
    ]
    return AttributionGraph(
        graph_id=graph_id,
        target=target or {},
        facts=facts,
        nodes=nodes,
        runtime_relations=runtime_relations,
        source_relations=source_relations,
        causal_edges=causal_edges,
        repair_clusters=repair_clusters,
        boundaries=list(facts.missing_evidence),
        entities=entities,
        graph_relations=facts.observed_relations,
    )


def build_scenario_facts(evidence: dict[str, Any]) -> ScenarioFacts:
    evidence = evidence if isinstance(evidence, dict) else {}
    window = _evidence_window(evidence)
    refs = _collect_refs(evidence)
    signals: list[EvidenceSignal] = []
    costs: list[CostCenterCandidate] = []
    impacts: list[ImpactCandidate] = []
    triggers: list[TriggerCandidate] = []
    relations: list[ObservedRelation] = []
    missing: list[str] = []
    quality = _quality_entries(evidence, window)

    sys_metrics = evidence.get("sys_metrics")
    summary = sys_metrics.get("summary", {}) if isinstance(sys_metrics, dict) else {}
    for key, signal_type in (
        ("avg_cpu_user_pct", "cpu_pressure"),
        ("avg_cpu_iowait_pct", "io_wait"),
        ("vmrss_mb", "memory_pressure"),
        ("fd_count", "fd_pressure"),
        ("thread_count", "thread_pressure"),
    ):
        value = _number(summary.get(key))
        if value is None:
            continue
        signal_id = f"signal:{signal_type}"
        signals.append(EvidenceSignal(
            signal_id=signal_id,
            signal_type=signal_type,
            value=value,
            evidence_refs=["sys_metrics.summary"],
            window=window,
            status="observed",
        ))
        impacts.append(ImpactCandidate(
            candidate_id=f"impact:{signal_type}",
            statement=f"采集窗口观测到 {signal_type} 指标值 {value:g}",
            evidence_refs=["sys_metrics.summary"],
            status="observed",
        ))

    top_functions = evidence.get("top_functions")
    if isinstance(top_functions, list):
        for index, item in enumerate(top_functions[:10]):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("function") or "").strip()
            if not name:
                continue
            ref = str(item.get("evidence_ref") or f"top_functions[{index}]")
            file_path = str(item.get("file") or "")
            line = _positive_int(item.get("line"))
            level: AttributionLevel = "line" if file_path and line else "function"
            candidate_id = f"cost:{level}:{index}"
            costs.append(CostCenterCandidate(
                candidate_id=candidate_id,
                level=level,
                target=f"{file_path}:{line}" if level == "line" else name,
                evidence_refs=[ref],
                source=str(item.get("source") or "industrial_profile"),
                primitive=name.rsplit(":", 1)[-1] in _PRIMITIVE_NAMES,
                confidence=min(1.0, max(0.0, _number(item.get("percent")) or 0.0) / 100.0),
            ))
            if index == 0:
                signals.append(EvidenceSignal(
                    signal_id="signal:top_cost_center",
                    signal_type="cost_center_observed",
                    value={"name": name, "percent": item.get("percent")},
                    evidence_refs=[ref],
                    window=window,
                    status="observed",
                ))

    _append_generic_trigger_candidates(evidence, window, triggers, relations)
    _append_generic_impact_candidates(evidence, window, impacts, signals)

    for item in _line_candidates(evidence):
        relation_id = f"relation:runtime-calls:{item['file']}:{item['line']}"
        relations.append(ObservedRelation(
            relation_id=relation_id,
            relation="calls",
            source_ref=str(item.get("runtime_ref") or item.get("symbol") or "runtime"),
            target_ref=f"{item['file']}:{item['line']}",
            evidence_refs=[str(item.get("evidence_ref") or "source_snapshot")],
            status="observed",
            statement="工业运行时栈与已验证源码位置建立调用映射。",
        ))
    _append_runtime_call_path_relations(evidence, relations)

    if not signals:
        missing.append("symptom_signal")
    if not costs:
        missing.append("cost_center")
    if not window:
        missing.append("evidence_window")
    if not any(item.status in {"valid", "partial"} for item in quality):
        missing.append("valid_evidence_quality")

    return ScenarioFacts(
        symptom_signals=signals,
        cost_centers=costs,
        trigger_candidates=triggers,
        impact_candidates=impacts,
        observed_relations=relations,
        missing_evidence=_unique(missing),
        evidence_quality=quality,
        evidence_refs=refs,
        evidence_window=window,
    )


def build_source_relations(
    *,
    evidence: dict[str, Any],
    source_snapshot: dict[str, Any] | None = None,
    source_mechanism: dict[str, Any] | None = None,
) -> list[SourceRelation]:
    result: list[SourceRelation] = []
    for item in _line_candidates({**evidence, "source_snapshot_json": source_snapshot or evidence.get("source_snapshot_json")}):
        file_path = str(item.get("file") or "")
        line = _positive_int(item.get("line"))
        if not file_path or not line:
            continue
        result.append(SourceRelation(
            relation_id=f"source:calls:{file_path}:{line}",
            relation="calls",
            source_ref=str(item.get("runtime_ref") or item.get("symbol") or "runtime"),
            target_ref=f"{file_path}:{line}",
            evidence_refs=[str(item.get("evidence_ref") or "source_snapshot")],
            file_path=file_path,
            line_number=line,
            status="verified" if item.get("verified", True) else "unproven",
            statement="运行时调用栈已通过 source snapshot 映射到指定 revision 的源码位置。",
        ))

    mechanism = source_mechanism or evidence.get("source_mechanism_json")
    paths = mechanism.get("mechanism_paths", []) if isinstance(mechanism, dict) else []
    for path_index, path in enumerate(paths if isinstance(paths, list) else []):
        if not isinstance(path, dict):
            continue
        path_ref = str(path.get("evidence_ref") or f"source_mechanism.mechanism_paths[{path_index}]")
        nodes = path.get("nodes") if isinstance(path.get("nodes"), list) else []
        for index in range(len(nodes) - 1):
            left = _node_ref(nodes[index])
            right = _node_ref(nodes[index + 1])
            if not left or not right:
                continue
            result.append(SourceRelation(
                relation_id=f"source:mechanism:{path_index}:{index}",
                relation=_relation_from_path(path, index),
                source_ref=left,
                target_ref=right,
                evidence_refs=[path_ref],
                file_path=str(nodes[index].get("file") or ""),
                line_number=_positive_int(nodes[index].get("line")),
                status="supported" if path.get("status") in {"supported", "verified"} else "unproven",
                statement=str(path.get("statement") or path.get("mechanism") or "源码查询返回跨函数关系路径。"),
            ))
    return _dedupe_relations(result)


def qualify_attribution(
    graph: AttributionGraph,
    *,
    trigger_refs: list[str] | None = None,
    mechanism_refs: list[str] | None = None,
    impact_refs: list[str] | None = None,
    disconfirming_evidence_refs: list[str] | None = None,
    ai_candidate_ids: list[str] | None = None,
    ai_candidates: list[dict[str, Any]] | None = None,
) -> QualificationResult:
    facts = graph.facts
    cost_refs = [item.candidate_id for item in facts.cost_centers]
    evidence_refs = _unique([
        *facts.evidence_refs,
        *(ref for item in graph.source_relations for ref in item.evidence_refs),
    ])
    triggers = _unique(trigger_refs or [item.candidate_id for item in facts.trigger_candidates if item.status == "observed"])
    mechanisms = _unique(mechanism_refs or [
        item.relation_id
        for item in graph.source_relations
        if item.relation in {"retains", "explains", "acquires", "holds", "releases", "amplifies", "propagates_to"}
        and item.status in {"verified", "supported"}
    ])
    impacts = _unique(impact_refs or [item.candidate_id for item in facts.impact_candidates if item.status == "observed"])
    source_refs = [
        item.relation_id
        for item in graph.source_relations
        if item.status in {"verified", "supported"}
    ]
    candidate_ids = _unique([
        *cost_refs,
        *triggers,
        *mechanisms,
        *impacts,
        *source_refs,
    ])
    supported_level = _max_supported_level(graph)
    if not facts.symptom_signals:
        return QualificationResult(
            level="L0",
            qualification="observation",
            decision="abstain",
            confidence=0.0,
            confidence_level="不可判断",
            target=graph.target,
            window=facts.evidence_window,
            evidence_refs=evidence_refs,
            candidate_ids=candidate_ids,
            supported_level=supported_level,
            missing_evidence=_unique([*facts.missing_evidence, "symptom_signal"]),
            reason="没有有效的同窗症状信号，不能进行归因。",
        )
    if not facts.cost_centers:
        return QualificationResult(
            level="L0",
            qualification="observation",
            decision="continue_probe",
            confidence=0.2,
            confidence_level="低",
            target=graph.target,
            window=facts.evidence_window,
            symptom_refs=[item.signal_id for item in facts.symptom_signals],
            evidence_refs=evidence_refs,
            candidate_ids=candidate_ids,
            supported_level=supported_level,
            missing_evidence=_unique([*facts.missing_evidence, "cost_center"]),
            reason="只能确认症状，尚未定位到运行时成本中心。",
        )
    if not triggers or not mechanisms or not impacts or not source_refs:
        missing = []
        if not triggers:
            missing.append("trigger")
        if not mechanisms:
            missing.append("mechanism")
        if not impacts:
            missing.append("impact")
        if not source_refs:
            missing.append("verified_source_relation")
        level = "L1" if not mechanisms else "L2"
        return QualificationResult(
            level=level,
            qualification="partial_localization" if level == "L1" else "mechanism_hypothesis",
            decision="continue_probe",
            causal_status="unproven",
            confidence=0.45 if level == "L1" else 0.6,
            confidence_level="低" if level == "L1" else "中",
            target=graph.target,
            window=facts.evidence_window,
            symptom_refs=[item.signal_id for item in facts.symptom_signals],
            cost_center_refs=cost_refs,
            trigger_refs=triggers,
            mechanism_refs=mechanisms,
            impact_refs=impacts,
            source_relation_refs=source_refs,
            evidence_refs=evidence_refs,
            candidate_ids=candidate_ids,
            supported_level=supported_level,
            missing_evidence=missing,
            disconfirming_evidence_refs=_unique(disconfirming_evidence_refs or []),
            reason="已定位到成本中心或源码关系，但触发、机制、影响和证据闭环尚未全部满足。",
        )
    if disconfirming_evidence_refs:
        return QualificationResult(
            level="L2",
            qualification="mechanism_hypothesis",
            decision="abstain",
            causal_status="contradicted",
            confidence=0.2,
            confidence_level="低",
            target=graph.target,
            window=facts.evidence_window,
            symptom_refs=[item.signal_id for item in facts.symptom_signals],
            cost_center_refs=cost_refs,
            trigger_refs=triggers,
            mechanism_refs=mechanisms,
            impact_refs=impacts,
            source_relation_refs=source_refs,
            evidence_refs=evidence_refs,
            disconfirming_evidence_refs=_unique(disconfirming_evidence_refs),
            candidate_ids=candidate_ids,
            supported_level=supported_level,
            reason="存在同窗反证，不能宣布正式根因。",
        )
    candidate_gate_failures = _validate_ai_candidates(
        graph,
        ai_candidate_ids=ai_candidate_ids or [],
        ai_candidates=ai_candidates or [],
        available_evidence_refs=set(evidence_refs),
        supported_level=supported_level,
        trigger_refs=triggers,
        mechanism_refs=mechanisms,
        impact_refs=impacts,
        source_relation_refs=source_refs,
    )
    failed_ids = {
        str(item.get("candidate_id") or "")
        for item in candidate_gate_failures
        if str(item.get("candidate_id") or "")
    }
    eligible_candidate_ids = _unique([
        str(item.get("candidate_id") or "")
        for item in (ai_candidates or [])
        if isinstance(item, dict)
        and str(item.get("candidate_id") or "") not in failed_ids
    ])
    if not eligible_candidate_ids:
        return QualificationResult(
            level="L2",
            qualification="mechanism_hypothesis",
            decision="continue_probe",
            causal_status="supported",
            confidence=0.7,
            confidence_level="中",
            target=graph.target,
            window=facts.evidence_window,
            symptom_refs=[item.signal_id for item in facts.symptom_signals],
            cost_center_refs=cost_refs,
            trigger_refs=triggers,
            mechanism_refs=mechanisms,
            impact_refs=impacts,
            source_relation_refs=source_refs,
            evidence_refs=evidence_refs,
            candidate_ids=candidate_ids,
            supported_level=supported_level,
            missing_evidence=["eligible_ai_candidate"],
            candidate_gate_failures=candidate_gate_failures,
            reason="事实、影响和源码关系已经闭合为机制假设，但正式根因必须由受控 AI 候选承接。",
        )
    return QualificationResult(
        level="L3",
        qualification="formal_root_cause",
        decision="conclude",
        causal_status="supported",
        confidence=0.85,
        confidence_level="高",
        target=graph.target,
        window=facts.evidence_window,
        symptom_refs=[item.signal_id for item in facts.symptom_signals],
        cost_center_refs=cost_refs,
        trigger_refs=triggers,
        mechanism_refs=mechanisms,
        impact_refs=impacts,
        source_relation_refs=source_refs,
        evidence_refs=evidence_refs,
        candidate_ids=candidate_ids,
        eligible_candidate_ids=eligible_candidate_ids,
        candidate_gate_failures=candidate_gate_failures,
        supported_level=supported_level,
        reason="同窗症状、成本中心、触发、机制、影响和已验证源码关系均有真实证据引用。",
    )


def _validate_ai_candidates(
    graph: AttributionGraph,
    *,
    ai_candidate_ids: list[str],
    ai_candidates: list[dict[str, Any]],
    available_evidence_refs: set[str],
    supported_level: AttributionLevel,
    trigger_refs: list[str],
    mechanism_refs: list[str],
    impact_refs: list[str],
    source_relation_refs: list[str],
) -> list[dict[str, Any]]:
    """Require an actual guarded AI record before allowing L3 promotion."""
    records = [
        item
        for item in ai_candidates
        if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
    ]
    requested_ids = _unique([
        *ai_candidate_ids,
        *(str(item.get("candidate_id") or "") for item in records),
    ])
    failed: list[dict[str, Any]] = []
    level_order = {key: value for key, value in _LEVEL_ORDER.items()}
    for candidate_id in requested_ids:
        record = next(
            (
                item
                for item in records
                if str(item.get("candidate_id") or "") == candidate_id
            ),
            None,
        )
        reasons: list[str] = []
        if record is None:
            reasons.append("candidate_record_missing")
        else:
            generated_by = str(
                record.get("generated_by")
                or record.get("source")
                or ""
            ).lower()
            if generated_by not in {"ai", "ai_candidate", "ai_guarded", "llm"}:
                reasons.append("candidate_not_ai_generated")
            for field in ("claim", "mechanism", "target"):
                if not str(record.get(field) or "").strip():
                    reasons.append(f"{field}_missing")
            refs = _unique([
                str(ref)
                for ref in record.get("evidence_refs", [])
                if str(ref)
            ])
            if not refs:
                reasons.append("candidate_evidence_refs_missing")
            elif any(ref not in available_evidence_refs for ref in refs):
                reasons.append("candidate_evidence_ref_invalid")
            if str(record.get("causal_status") or "") != "supported":
                reasons.append("causal_status_not_supported")
            if str(record.get("decision") or "") != "conclude":
                reasons.append("decision_not_conclude")
            level = str(record.get("supported_level") or "resource")
            if level not in level_order:
                reasons.append("supported_level_invalid")
            elif level_order[level] > level_order.get(supported_level, 0):
                reasons.append("supported_level_exceeds_graph")
            if not (
                str(record.get("origin_parent_candidate_id") or "").strip()
                or record.get("parent_candidate_ids")
            ):
                reasons.append("parent_provenance_missing")
            relation_refs = {
                "trigger": record.get("trigger_refs") or [],
                "mechanism": record.get("mechanism_refs") or [],
                "impact": record.get("impact_refs") or [],
                "source_relation": record.get("source_relation_refs") or [],
            }
            if not any(relation_refs.values()):
                reasons.append("causal_chain_refs_missing")
            if relation_refs["trigger"] and not set(relation_refs["trigger"]).intersection(trigger_refs):
                reasons.append("trigger_relation_missing")
            if relation_refs["mechanism"] and not set(relation_refs["mechanism"]).intersection(mechanism_refs):
                reasons.append("mechanism_relation_missing")
            if relation_refs["impact"] and not set(relation_refs["impact"]).intersection(impact_refs):
                reasons.append("impact_relation_missing")
            if relation_refs["source_relation"] and not set(relation_refs["source_relation"]).intersection(source_relation_refs):
                reasons.append("source_relation_missing")
        if reasons:
            failed.append({
                "candidate_id": candidate_id,
                "reasons": list(dict.fromkeys(reasons)),
            })
    return failed


def _build_graph_context(
    *,
    facts: ScenarioFacts,
    source_relations: list[SourceRelation],
    evidence: dict[str, Any],
) -> tuple[list[AttributionNode], list[AttributionEdge], list[AttributionEdge], list[RepairCluster]]:
    nodes: list[AttributionNode] = []
    runtime_edges: list[AttributionEdge] = []
    causal_edges: list[AttributionEdge] = []
    for item in facts.cost_centers:
        file_path, line = _split_location(item.target)
        nodes.append(AttributionNode(
            node_id=item.candidate_id,
            role="cost_center",
            symbol=item.target if not file_path else "",
            file=file_path,
            line=line,
            supported_level=item.level,
            evidence_refs=item.evidence_refs,
            source_status="supported" if item.evidence_refs else "unproven",
            window=facts.evidence_window,
            label=item.target,
        ))
    for item in facts.trigger_candidates:
        nodes.append(AttributionNode(
            node_id=item.candidate_id,
            role="trigger",
            supported_level="process",
            evidence_refs=item.evidence_refs,
            source_status="supported" if item.status == "observed" else "unproven",
            window=facts.evidence_window,
            label=item.statement,
        ))
    for item in facts.impact_candidates:
        nodes.append(AttributionNode(
            node_id=item.candidate_id,
            role="impact",
            supported_level="process",
            evidence_refs=item.evidence_refs,
            source_status="supported" if item.status == "observed" else "unproven",
            window=facts.evidence_window,
            label=item.statement,
        ))
    for item in source_relations:
        source_id = f"source:{item.source_ref}"
        target_id = f"source:{item.target_ref}"
        file_path, line = _split_location(item.target_ref)
        nodes.extend([
            AttributionNode(
                node_id=source_id,
                role="source",
                symbol=item.source_ref if not _split_location(item.source_ref)[0] else "",
                supported_level="function",
                evidence_refs=item.evidence_refs,
                source_status=item.status,
                window=facts.evidence_window,
                label=item.source_ref,
            ),
            AttributionNode(
                node_id=target_id,
                role="source",
                symbol=item.target_ref if not file_path else "",
                file=file_path,
                line=line,
                supported_level="line" if line else "function",
                evidence_refs=item.evidence_refs,
                source_status=item.status,
                window=facts.evidence_window,
                label=item.target_ref,
            ),
        ])
        edge = AttributionEdge(
            edge_id=item.relation_id,
            from_node=source_id,
            relation=item.relation,
            to_node=target_id,
            evidence_refs=item.evidence_refs,
            same_window=_same_window(facts.evidence_window),
            confidence=1.0 if item.status == "verified" else 0.65,
            status=item.status,
        )
        runtime_edges.append(edge)
        causal_edges.append(edge)

    for item in facts.observed_relations:
        edge = AttributionEdge(
            edge_id=item.relation_id,
            from_node=item.source_ref,
            relation=item.relation,
            to_node=item.target_ref,
            evidence_refs=item.evidence_refs,
            same_window=_same_window(facts.evidence_window),
            confidence=0.8 if item.status == "observed" else 0.35,
            status=item.status,
        )
        runtime_edges.append(edge)
        causal_edges.append(edge)

    node_ids = {item.node_id for item in nodes}
    repairs = []
    if source_relations:
        repairs.append(RepairCluster(
            cluster_id="repair:source-relations",
            node_refs=[item.node_id for item in nodes if item.role in {"source", "cost_center"}],
            source_relation_refs=[item.relation_id for item in source_relations],
            evidence_refs=_unique(ref for item in source_relations for ref in item.evidence_refs),
            status="supported" if any(item.status == "verified" for item in source_relations) else "candidate",
            reason="仅由源码关系聚合候选修复位置，不能单独证明需要修改。",
        ))
    return _dedupe_nodes(nodes), _dedupe_edges(runtime_edges), _dedupe_edges(causal_edges), repairs


def _append_generic_trigger_candidates(
    evidence: dict[str, Any],
    window: dict[str, Any],
    triggers: list[TriggerCandidate],
    relations: list[ObservedRelation],
) -> None:
    for key, value in _candidate_fields(evidence, {
        "input", "input_shape", "input_profile", "request", "task", "queue",
        "config", "configuration", "schedule", "trigger", "producer", "workload",
    }):
        refs = _generic_evidence_refs(evidence)
        if not refs:
            continue
        statement = _summarize_value(value)
        if not statement:
            continue
        candidate_id = f"trigger:{key}"
        if any(item.candidate_id == candidate_id for item in triggers):
            continue
        triggers.append(TriggerCandidate(
            candidate_id=candidate_id,
            statement=f"结构化证据记录了 {key}：{statement}",
            evidence_refs=refs,
            status="observed",
        ))
        if len(triggers) > 12:
            break


def _append_generic_impact_candidates(
    evidence: dict[str, Any],
    window: dict[str, Any],
    impacts: list[ImpactCandidate],
    signals: list[EvidenceSignal],
) -> None:
    for key, value in _candidate_fields(evidence, {
        "error", "errors", "latency", "duration", "backlog", "queue_depth",
        "timeout", "retries", "cache", "retention", "rss", "memory", "status",
    }):
        refs = _generic_evidence_refs(evidence)
        if not refs:
            continue
        statement = _summarize_value(value)
        if not statement:
            continue
        candidate_id = f"impact:{key}"
        if any(item.candidate_id == candidate_id for item in impacts):
            continue
        impacts.append(ImpactCandidate(
            candidate_id=candidate_id,
            statement=f"结构化证据记录了 {key}：{statement}",
            evidence_refs=refs,
            status="observed",
        ))
        signals.append(EvidenceSignal(
            signal_id=f"signal:{key}",
            signal_type=key,
            value=value,
            evidence_refs=refs,
            window=window,
            status="observed",
        ))
        if len(impacts) > 16:
            break


def _candidate_fields(value: Any, keys: set[str], prefix: str = "") -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).lower() in keys and child not in (None, "", [], {}):
                result.append((path, child))
            if isinstance(child, (dict, list)):
                result.extend(_candidate_fields(child, keys, path))
    elif isinstance(value, list):
        for index, child in enumerate(value[:20]):
            result.extend(_candidate_fields(child, keys, f"{prefix}[{index}]"))
    return result


def _summarize_value(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)):
        return str(value)[:180]
    if isinstance(value, dict):
        keys = list(value)[:8]
        return ", ".join(f"{key}={str(value[key])[:40]}" for key in keys)
    if isinstance(value, list):
        return f"{len(value)} items"
    return ""


def _generic_evidence_refs(evidence: dict[str, Any]) -> list[str]:
    refs = _collect_refs(evidence)
    if refs:
        return refs[:8]
    index = evidence.get("evidence_index")
    if isinstance(index, dict):
        refs = _collect_refs(index)
    return refs[:8]


def _split_location(value: Any) -> tuple[str, int | None]:
    text = str(value or "")
    match = re.match(r"^(.*):(\d+)$", text)
    if not match:
        return "", None
    return match.group(1), _positive_int(match.group(2))


def _same_window(window: dict[str, Any]) -> bool | None:
    relation = str(window.get("timing_relation") or "")
    if not relation:
        return None
    return relation == "same_window"


def _max_supported_level(graph: AttributionGraph) -> AttributionLevel:
    levels = [
        item.level for item in graph.facts.cost_centers
    ] + [
        "line" if item.line_number else "function"
        for item in graph.source_relations
        if item.status in {"verified", "supported"}
    ]
    return max(levels, key=lambda item: _LEVEL_ORDER.get(item, 0), default="resource")


def _dedupe_nodes(values: list[AttributionNode]) -> list[AttributionNode]:
    result = []
    seen = set()
    for item in values:
        if item.node_id in seen:
            continue
        seen.add(item.node_id)
        result.append(item)
    return result


def _dedupe_edges(values: list[AttributionEdge]) -> list[AttributionEdge]:
    result = []
    seen = set()
    for item in values:
        key = (item.from_node, item.relation, item.to_node, tuple(item.evidence_refs))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _evidence_window(evidence: dict[str, Any]) -> dict[str, Any]:
    for value in (
        evidence.get("evidence_window"),
        evidence.get("confidence_inputs", {}).get("evidence_window")
        if isinstance(evidence.get("confidence_inputs"), dict)
        else None,
    ):
        if isinstance(value, dict):
            return dict(value)
    return {}


def _quality_entries(evidence: dict[str, Any], window: dict[str, Any]) -> list[EvidenceQualityEntry]:
    result = []
    index = evidence.get("evidence_index")
    values = index.get("evidence_validity_by_family", {}) if isinstance(index, dict) else {}
    if isinstance(values, dict):
        for family, status in values.items():
            normalized = str(status or "unknown")
            if normalized not in {"valid", "partial", "empty", "blocked", "stale", "unknown"}:
                normalized = "unknown"
            result.append(EvidenceQualityEntry(
                evidence_ref=f"evidence_family:{family}",
                family=str(family),
                status=normalized,
                reason="结构化采集器质量摘要",
                window=window,
            ))
    if not result and evidence:
        result.append(EvidenceQualityEntry(
            evidence_ref="structured_evidence",
            status="valid" if evidence.get("top_functions") or evidence.get("sys_metrics") else "partial",
            reason="存在结构化工业采集输出" if evidence.get("top_functions") or evidence.get("sys_metrics") else "结构化输出为空或不足",
            window=window,
        ))
    return result


def _line_candidates(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    runtime_locations = _runtime_location_keys(evidence)
    source = evidence.get("source_snapshot_json")
    if isinstance(source, dict):
        for key in ("verified_line_candidates", "line_candidates", "candidates"):
            values = source.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict):
                    file_path = str(item.get("file") or item.get("file_path") or "")
                    line = _positive_int(item.get("line") or item.get("focus_line"))
                    explicit_runtime = bool(
                        item.get("runtime_verified")
                        or item.get("runtime_file")
                        or item.get("runtime_line")
                    )
                    runtime_match = (
                        explicit_runtime
                        or any(
                            _source_paths_match(file_path, runtime_file)
                            and line == runtime_line
                            for runtime_file, runtime_line in runtime_locations
                        )
                    )
                    if not runtime_match:
                        continue
                    result.append({
                        **item,
                        "verified": (
                            item.get("eligibility_status") in {None, "", "verified"}
                            or item.get("verified") is True
                        ),
                        "evidence_ref": item.get("evidence_ref") or f"source_snapshot.{key}",
                    })
            if result:
                break
    index = evidence.get("evidence_index")
    if isinstance(index, dict) and isinstance(index.get("line_candidates"), list):
        result.extend(item for item in index["line_candidates"] if isinstance(item, dict))
    return result


def _runtime_location_keys(evidence: dict[str, Any]) -> set[tuple[str, int]]:
    result: set[tuple[str, int]] = set()
    containers: list[Any] = [
        evidence.get("top_functions"),
        evidence.get("call_path_hotspots"),
        evidence.get("line_candidates"),
    ]
    for key in ("depth_evidence_json", "python_stack_samples_json", "go_heap_profile_json"):
        payload = evidence.get(key)
        if not isinstance(payload, dict):
            continue
        containers.extend(
            payload.get(list_key)
            for list_key in (
                "line_candidates",
                "top_functions",
                "call_path_hotspots",
                "hotspots",
                "stack_samples",
            )
        )
    for container in containers:
        if not isinstance(container, list):
            continue
        for item in container:
            if not isinstance(item, dict):
                continue
            file_path = str(item.get("file") or item.get("file_path") or "")
            line = _positive_int(item.get("line") or item.get("focus_line"))
            if file_path and line:
                result.add((file_path.replace("\\", "/"), line))
    return result


def _source_paths_match(left: str, right: str) -> bool:
    left_text = str(left or "").replace("\\", "/").lstrip("./")
    right_text = str(right or "").replace("\\", "/").lstrip("./")
    if not left_text or not right_text:
        return False
    return left_text == right_text or left_text.endswith("/" + right_text) or right_text.endswith("/" + left_text)


def _append_runtime_call_path_relations(
    evidence: dict[str, Any],
    relations: list[ObservedRelation],
) -> None:
    containers: list[Any] = [evidence.get("call_path_hotspots")]
    for key in ("depth_evidence_json", "python_stack_samples_json", "trace_endpoint_profile_json"):
        payload = evidence.get(key)
        if isinstance(payload, dict):
            containers.append(payload.get("call_path_hotspots"))
    seen = {
        (item.relation, item.source_ref, item.target_ref, tuple(item.evidence_refs))
        for item in relations
    }
    for container in containers:
        if not isinstance(container, list):
            continue
        for index, item in enumerate(container):
            if not isinstance(item, dict):
                continue
            path = item.get("call_path")
            if isinstance(path, str):
                path = [part.strip() for part in path.split(";") if part.strip()]
            if not isinstance(path, list):
                continue
            path = [str(part).strip() for part in path if str(part).strip()]
            if len(path) < 2:
                continue
            evidence_ref = str(
                item.get("evidence_ref")
                or f"evidence_index.call_path_hotspots[{index}]"
            )
            for left, right in zip(path, path[1:]):
                relation = ObservedRelation(
                    relation_id=f"relation:runtime-calls:{left}:{right}:{evidence_ref}",
                    relation="calls",
                    source_ref=left,
                    target_ref=right,
                    evidence_refs=[evidence_ref],
                    status="observed",
                    statement=f"工业采集器在同窗调用路径中观察到 {left} -> {right}。",
                )
                key = (
                    relation.relation,
                    relation.source_ref,
                    relation.target_ref,
                    tuple(relation.evidence_refs),
                )
                if key not in seen:
                    relations.append(relation)
                    seen.add(key)


def _node_ref(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    file_path = str(node.get("file") or node.get("file_path") or "")
    line = _positive_int(node.get("line") or node.get("line_number"))
    symbol = str(node.get("symbol") or node.get("function") or node.get("name") or "")
    if file_path and line:
        return f"{file_path}:{line}"
    return symbol


def _relation_from_path(path: dict[str, Any], index: int) -> str:
    relations = path.get("relations")
    if isinstance(relations, list) and index < len(relations):
        value = str(relations[index])
        if value in {"calls", "triggers", "activates", "propagates_to", "amplifies", "waits_on", "retains", "explains", "acquires", "holds", "releases"}:
            return value
    return "propagates_to"


def _collect_refs(evidence: dict[str, Any]) -> list[str]:
    refs = []
    for key in ("evidence_refs", "artifact_refs"):
        value = evidence.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    refs.append(item)
                elif isinstance(item, dict):
                    refs.extend(str(item.get(key2) or "") for key2 in ("evidence_ref", "evidence_id", "object_key"))
    return _unique(refs)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _dedupe_relations(values: list[SourceRelation]) -> list[SourceRelation]:
    result = []
    seen = set()
    for value in values:
        key = (value.relation, value.source_ref, value.target_ref, tuple(value.evidence_refs))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
