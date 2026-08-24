"""Session-level root-cause clustering and bounded explanation assembly."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from server.app.rca.models import (
    AITreeCandidateNode,
    CausalExplanationStep,
    QualificationBoundary,
    RetainedConclusion,
    RootCauseCluster,
    RootCauseRecommendation,
    SessionConclusionReview,
)
from server.app.rca.controlled_tree import (
    evidence_refs_with_anchors,
    evidence_refs_with_line_anchors,
    evidence_refs_with_runtime_anchors,
    qualify_ai_candidate,
    select_terminal_candidate_ids,
)
from server.app.diagnosis.canonical_claim_lineage import ensure_claim_lineage, hash_claim
from server.app.diagnosis.attribution_models import QualificationResult


ELIGIBLE_CAUSE_LEVELS = {"direct_root_cause", "complete_source_root_cause"}


def build_session_qualification(
    clusters: list[RootCauseCluster],
    session_tree: dict[str, Any] | None,
    *,
    base: dict[str, Any] | None = None,
    retained_conclusion: dict[str, Any] | None = None,
    attribution_qualification: dict[str, Any] | QualificationResult | None = None,
    session_ai_review_status: str | None = None,
) -> dict[str, Any]:
    """Derive one session qualification from the emitted AI tree and its clusters."""
    base = base if isinstance(base, dict) else {}
    nodes = _all_tree_nodes(session_tree)
    candidate_ids = _unique(
        str(node.get("candidate_id") or "")
        for node in nodes
        if node.get("candidate_id")
    )
    ai_nodes = [
        node for node in nodes
        if str(node.get("generated_by") or "") in {"ai", "ai_candidate", "ai_guarded"}
    ]
    ai_candidate_ids = {
        str(node.get("candidate_id") or "")
        for node in ai_nodes
        if node.get("candidate_id")
    }
    eligible_ids = _unique(
        str(cluster.candidate_ids[0] if cluster.candidate_ids else "")
        for cluster in clusters
        if cluster.conclusion_eligible and cluster.candidate_ids
    )
    eligible_ids = [
        candidate_id for candidate_id in eligible_ids
        if candidate_id in ai_candidate_ids
    ]
    if session_ai_review_status is not None and session_ai_review_status != "succeeded":
        # A candidate can be technically guardable while the session-level
        # adjudication is unavailable. Keep it as investigation state, never
        # let the qualification reducer turn it into a formal conclusion.
        eligible_ids = []
    graph_result = None
    if attribution_qualification is not None:
        try:
            graph_result = (
                attribution_qualification
                if isinstance(attribution_qualification, QualificationResult)
                else QualificationResult.model_validate(attribution_qualification)
            )
        except Exception:
            graph_result = None
    if graph_result is not None:
        graph_eligible_ids = set(graph_result.eligible_candidate_ids)
        eligible_ids = [
            candidate_id for candidate_id in eligible_ids
            if candidate_id in graph_eligible_ids
        ]
    evidence_refs = _unique([
        *(base.get("evidence_refs") or []),
        *(ref for cluster in clusters for ref in cluster.evidence_refs),
        *(ref for node in nodes for ref in (node.get("evidence_refs") or [])),
    ])
    missing = _unique([
        *(base.get("missing_evidence") or []),
        *(item for cluster in clusters for item in cluster.residual_unknowns),
    ])
    line_eligibility = (
        session_tree.get("line_anchor_eligibility")
        if isinstance(session_tree, dict)
        and isinstance(session_tree.get("line_anchor_eligibility"), dict)
        else {}
    )
    if str(line_eligibility.get("status") or "") == "verified" and ai_nodes:
        ai_line_nodes = [
            node for node in ai_nodes
            if str(node.get("supported_level") or "") == "line"
            and str(node.get("node_type") or "") == "line_anchor"
        ]
        line_candidates = ai_line_nodes or ai_nodes
        if not any(str(node.get("mechanism") or "").strip() for node in line_candidates):
            missing.append("mechanism")
        if not any(node.get("source_relation_refs") for node in line_candidates):
            missing.append("verified_source_relation")
    if graph_result is not None:
        missing = _unique([*missing, *graph_result.missing_evidence])
    supported_level = _deepest_level([
        str(cluster.supported_level)
        for cluster in clusters
        if cluster.evidence_refs
    ] + [
        str(node.get("supported_level") or "resource")
        for node in nodes
        if node.get("claim") and node.get("status") not in {"rejected", "contradicted"}
    ])
    if graph_result is not None:
        graph_result.candidate_ids = _unique([
            *candidate_ids,
            *graph_result.candidate_ids,
        ])
        graph_result.eligible_candidate_ids = eligible_ids
        if graph_result.qualification == "formal_root_cause" and eligible_ids:
            return graph_result.model_dump(mode="json")
        if graph_result.qualification == "formal_root_cause":
            graph_result.level = "L2"
            graph_result.qualification = "mechanism_hypothesis"
            graph_result.decision = "continue_probe"
            graph_result.causal_status = "unproven"
            graph_result.confidence = min(graph_result.confidence, 0.6)
            graph_result.confidence_level = "中" if graph_result.confidence >= 0.5 else "低"
            graph_result.missing_evidence = _unique([
                *graph_result.missing_evidence,
                *missing,
                "session_ai_candidate_cluster",
            ])
            graph_result.reason = (
                "统一归因图已闭合，但当前 session tree 没有可承接正式根因的合格 AI 候选。"
            )
        return graph_result.model_dump(mode="json")
    if eligible_ids:
        confidence = max(
            (cluster.confidence for cluster in clusters if cluster.conclusion_eligible),
            default=0.0,
        )
        result = QualificationResult(
            level="L3",
            qualification="formal_root_cause",
            decision="conclude",
            causal_status="supported",
            confidence=confidence,
            confidence_level="高" if confidence >= 0.75 else "中",
            target=base.get("target") if isinstance(base.get("target"), dict) else {},
            window=base.get("window") if isinstance(base.get("window"), dict) else {},
            evidence_refs=evidence_refs,
            candidate_ids=candidate_ids,
            eligible_candidate_ids=eligible_ids,
            supported_level=supported_level,
            reason="受控 AI 候选通过候选、证据引用、因果状态和父节点门禁。",
        )
    elif ai_nodes or clusters or retained_conclusion:
        level = "L2" if any(
            node.get("mechanism") and node.get("evidence_refs")
            for node in ai_nodes
        ) else "L1"
        confidence = max(
            (cluster.confidence for cluster in clusters if cluster.evidence_refs),
            default=0.35 if level == "L1" else 0.5,
        )
        result = QualificationResult(
            level=level,
            qualification="mechanism_hypothesis" if level == "L2" else "partial_localization",
            decision="continue_probe" if base.get("next_evidence_requests") else "abstain",
            causal_status="unproven",
            confidence=min(confidence, 0.6),
            confidence_level="低" if confidence < 0.5 else "中",
            target=base.get("target") if isinstance(base.get("target"), dict) else {},
            window=base.get("window") if isinstance(base.get("window"), dict) else {},
            evidence_refs=evidence_refs,
            candidate_ids=candidate_ids,
            supported_level=supported_level,
            missing_evidence=missing or ["eligible_ai_candidate"],
            reason="存在事实、定位或机制方向，但没有受控 AI 候选通过正式根因资格门禁。",
        )
    else:
        result = QualificationResult(
            level="L0",
            qualification="observation",
            decision="abstain",
            causal_status="inconclusive",
            confidence=0.0,
            confidence_level="不可判断",
            target=base.get("target") if isinstance(base.get("target"), dict) else {},
            window=base.get("window") if isinstance(base.get("window"), dict) else {},
            evidence_refs=evidence_refs,
            candidate_ids=candidate_ids,
            supported_level=supported_level,
            missing_evidence=missing or ["eligible_ai_candidate"],
            reason="当前没有可用于正式归因的 AI 候选或足够的定位事实。",
        )
    return result.model_dump(mode="json")


def apply_session_qualification(
    explanation: dict[str, Any],
    qualification: dict[str, Any],
    *,
    all_clusters: list[RootCauseCluster],
) -> dict[str, Any]:
    """Make every formal conclusion field derive from the same qualification."""
    result = dict(explanation)
    eligible_ids = {
        str(item)
        for item in qualification.get("eligible_candidate_ids", [])
        if item
    }
    formal_clusters = [
        cluster for cluster in all_clusters
        if cluster.conclusion_eligible
        and eligible_ids.intersection({
            *[str(item) for item in cluster.candidate_ids],
            *[str(item) for item in cluster.source_tree_candidate_ids],
        })
    ]
    if qualification.get("qualification") == "formal_root_cause" and formal_clusters:
        result["root_cause_clusters"] = formal_clusters
        result["causal_chain"] = [
            step for cluster in formal_clusters for step in cluster.causal_chain
        ]
        result["formal_root_cause"] = _formal_root_cause(formal_clusters)
        result["abstained"] = False
        result["confidence_level"] = qualification.get("confidence_level") or "中"
        result["classification"] = classify_cluster_set(formal_clusters)
        return result

    retained = result.get("retained_conclusion") if isinstance(result.get("retained_conclusion"), dict) else {}
    retained_claim = str(retained.get("claim") or "").strip()
    result["root_cause_clusters"] = []
    result["causal_chain"] = []
    result["formal_root_cause"] = None
    result["abstained"] = True
    result["confidence_level"] = qualification.get("confidence_level") or "不可判断"
    result["classification"] = "insufficient_evidence"
    result["headline"] = (
        _abstained_retained_headline(retained_claim)
        if retained_claim
        else "未形成正式根因；当前证据只支持观察、局部定位或机制假设。"
    )
    result["why_it_happened"] = str(
        qualification.get("reason")
        or result.get("why_it_happened")
        or "没有候选通过统一正式根因资格门禁。"
    )
    result["possible_root_causes"] = [
        cluster.model_dump(mode="json")
        for cluster in all_clusters
        if cluster.qualification in {"possible_root_cause", "partial_localization"}
    ]
    return result


def build_root_cause_clusters(
    observations: list[dict[str, Any]],
    assessment: dict[str, Any],
    session: dict[str, Any],
    session_tree: dict[str, Any] | None = None,
) -> list[RootCauseCluster]:
    """Build and deduplicate evidence-backed causal templates before LLM review."""
    scope = session.get("target_scope") if isinstance(session.get("target_scope"), dict) else {}
    target_service = str(scope.get("target_service") or "目标服务")
    same_host_ids = {str(item) for item in scope.get("same_host_instance_ids", [])}
    source_ids = _tree_candidate_ids_by_mechanism(session_tree)
    candidates: list[dict[str, Any]] = []

    failed_dependency_seen = False
    for observation in observations:
        dependency = observation.get("dependency") if isinstance(observation.get("dependency"), dict) else {}
        failed_dependencies = dependency.get("failed_dependencies") if isinstance(dependency.get("failed_dependencies"), list) else []
        for dependency_id in failed_dependencies:
            failed_dependency_seen = True
            target = str(dependency_id or assessment.get("claim_target") or "下游依赖")
            refs = _unique(observation.get("evidence_refs", []))
            if not refs:
                refs = _unique(assessment.get("evidence_refs", []))
            control = _direct_runtime_control_for_target(observations, target)
            if control:
                refs = _unique([*refs, *control["evidence_refs"]])
                actor_text = control["actor"]
                action_text = control["action"]
                candidates.append(_cluster_template(
                    mechanism="process_suspended",
                    target=target,
                    cause_level="complete_source_root_cause" if control["complete_source_chain"] else "direct_root_cause",
                    claim=(
                        f"{actor_text} 在异常同窗对 {target} 执行 {action_text}，使该下游服务停止处理请求，"
                        f"并直接阻断 {target_service} 的依赖调用。"
                    ),
                    why=(
                        f"{action_text} 使 {target} 无法继续处理业务；{target_service} 必须等待该依赖返回，"
                        "因此同窗出现调用失败或超时。"
                    ),
                    symptoms=[f"{target_service} 下游请求失败或延迟"],
                    refs=refs,
                    confidence=max(0.94, _number(assessment.get("confidence"))),
                    cohort=_cohort(observation),
                    propagation_path=f"control_action->{target}->{target_service}",
                    source_ids=[
                        *source_ids.get("process_suspended", []),
                        *source_ids.get("downstream_dependency_failure", []),
                    ],
                    eligible=bool(refs),
                    unknowns=(
                        ["已确认容器运行时直接执行暂停动作，但触发该动作的上游用户或自动化来源仍未知。"]
                        if control["origin_unknown"] else []
                    ),
                ))
                continue
            candidates.append(_cluster_template(
                mechanism="downstream_dependency_failure",
                target=target,
                cause_level="direct_root_cause",
                claim=f"{target} 在异常同窗内不可达或请求失败，直接阻断 {target_service} 的下游调用。",
                why=f"{target_service} 必须等待 {target} 返回；依赖检查直接观测到失败，因此请求会阻塞、超时或返回错误。",
                symptoms=[f"{target_service} 下游请求失败或延迟"],
                refs=refs,
                confidence=max(0.75, _number(assessment.get("confidence"))),
                cohort=_cohort(observation),
                propagation_path=f"{target_service}->{target}",
                source_ids=source_ids.get("downstream_dependency_failure", []),
                eligible=bool(refs),
                unknowns=[f"尚未证明 {target} 内部为何不可达；需运行控制、容器事件或服务日志补充来源。"],
            ))

    if assessment.get("classification") == "downstream_dependency" and not failed_dependency_seen:
        target = str(assessment.get("claim_target") or "下游依赖")
        refs = _unique(assessment.get("evidence_refs", []))
        candidates.append(_cluster_template(
            mechanism="downstream_dependency_failure",
            target=target,
            cause_level=_cause_level(assessment),
            claim=str(assessment.get("diagnostic_claim") or assessment.get("summary") or "下游依赖异常。"),
            why=f"{target_service} 的业务路径依赖 {target}；同窗依赖证据显示该调用无法正常完成。",
            symptoms=[f"{target_service} 下游请求失败或延迟"],
            refs=refs,
            confidence=_number(assessment.get("confidence")),
            cohort="unknown",
            propagation_path=f"{target_service}->{target}",
            source_ids=source_ids.get("downstream_dependency_failure", []),
            eligible=bool(assessment.get("conclusion_eligible") and refs),
            unknowns=[f"尚未证明 {target} 内部的故障来源。"],
        ))

    if assessment.get("classification") == "runtime_stall":
        refs = _unique(assessment.get("evidence_refs", []))
        origin_unknown = bool(assessment.get("origin_unknown", True))
        candidates.append(_cluster_template(
            mechanism="process_suspended",
            target=str(assessment.get("claim_target") or target_service),
            cause_level=_cause_level(assessment),
            claim=str(assessment.get("diagnostic_claim") or assessment.get("summary") or "目标进程被暂停。"),
            why="暂停控制使目标进程不再获得正常执行机会，因此进程仍存在但业务工作停止推进。",
            symptoms=["进程存在但业务无进展"],
            refs=refs,
            confidence=_number(assessment.get("confidence")),
            cohort="same_window",
            propagation_path="control_action->target_process->request_stall",
            source_ids=source_ids.get("process_suspended", []),
            eligible=bool(assessment.get("conclusion_eligible") and refs),
            unknowns=["已确认直接执行控制动作的进程，但上游发起者或自动化来源仍未知。"] if origin_unknown else [],
        ))
    elif assessment.get("classification") not in {
        "downstream_dependency",
        "same_host_noisy_neighbor",
        "insufficient_evidence",
    }:
        refs = _unique(assessment.get("evidence_refs", []))
        mechanism = str(assessment.get("mechanism") or assessment.get("classification") or "unknown_mechanism")
        if refs and mechanism:
            candidates.append(_cluster_template(
                mechanism=mechanism,
                target=str(assessment.get("claim_target") or target_service),
                cause_level=_cause_level(assessment),
                claim=str(assessment.get("diagnostic_claim") or assessment.get("summary") or mechanism),
                why=str(assessment.get("eligibility_reason") or "当前证据只支持该候选，因果机制仍需补证。"),
                symptoms=[str(assessment.get("classification") or "性能异常")],
                refs=refs,
                confidence=_number(assessment.get("confidence")),
                cohort="same_window",
                propagation_path=mechanism,
                source_ids=source_ids.get(mechanism, []),
                eligible=bool(assessment.get("conclusion_eligible")),
                unknowns=_unique(assessment.get("missing_evidence", [])),
            ))

    for observation in observations:
        target = observation.get("target") if isinstance(observation.get("target"), dict) else {}
        pressure = observation.get("pressure") if isinstance(observation.get("pressure"), dict) else {}
        instance_id = str(target.get("instance_id") or "")
        service_id = str(target.get("service_id") or "")
        if not pressure.get("cpu") or service_id == target_service:
            continue
        if instance_id not in same_host_ids:
            continue
        refs = _unique(observation.get("evidence_refs", []))
        anchor = observation.get("specific_anchor") if isinstance(observation.get("specific_anchor"), dict) else {}
        summary = observation.get("summary") if isinstance(observation.get("summary"), dict) else {}
        cpu_pct = _number(anchor.get("avg_cpu_user_pct")) + _number(anchor.get("avg_cpu_sys_pct"))
        host_saturated = bool(summary.get("host_cpu_saturated")) or _number(summary.get("avg_host_cpu_busy_pct")) >= 85
        target_impacted = any(
            isinstance(item.get("target"), dict)
            and item["target"].get("service_id") == target_service
            and item["target"].get("host_id") == target.get("host_id")
            and isinstance(item.get("summary"), dict)
            and (
                bool(item["summary"].get("target_scheduling_pressure"))
                or _number(item["summary"].get("avg_cgroup_throttled_pct")) > 0
                or _number(item["summary"].get("cgroup_nr_throttled_delta")) > 0
            )
            for item in observations
        )
        concrete_target = instance_id or service_id or f"pid={target.get('pid')}"
        candidates.append(_cluster_template(
            mechanism="same_host_cpu_contention",
            target=concrete_target,
            cause_level="direct_root_cause" if refs and cpu_pct >= 70 and host_saturated else "observation",
            claim=(
                f"同宿主工作负载 {concrete_target}（含其 cgroup/子进程）持续占用约 {cpu_pct:.1f}% CPU，"
                f"宿主忙碌度达到 {_number(summary.get('avg_host_cpu_busy_pct')):.1f}%。"
            ),
            why=(
                f"{concrete_target} 与 {target_service} 共享宿主 CPU，且目标服务出现 throttling/调度受压，"
                "因此该负载会放大主故障造成的请求延迟。"
                if target_impacted
                else f"{concrete_target} 的高 CPU 与宿主饱和同窗成立，但尚未观察到 {target_service} 的 throttling 或调度受压，"
                "所以它只能作为独立异常，不能声称已拖慢目标服务。"
            ),
            symptoms=[f"{target_service} 调度延迟被放大"] if target_impacted else [f"{concrete_target} 自身 CPU 异常"],
            refs=refs,
            confidence=0.78 if cpu_pct >= 90 else 0.68,
            cohort=_cohort(observation),
            propagation_path=f"{concrete_target}->shared_host_cpu->{target_service}",
            source_ids=source_ids.get("same_host_noisy_neighbor", []) + source_ids.get("same_host_cpu_contention", []),
            eligible=bool(refs and cpu_pct >= 70 and host_saturated),
            unknowns=([] if target_impacted else [f"缺少 {target_service} 的 cgroup throttling 或调度受压证据，不能把该异常提升为贡献根因。"]),
            causal_status="contributing" if target_impacted else "independent" if host_saturated else "unknown",
        ))

    clusters = _deduplicate(candidates)
    primary_assigned = False
    for cluster in clusters:
        cluster.conclusion_eligible = bool(
            cluster.conclusion_eligible
            and cluster.cause_level in ELIGIBLE_CAUSE_LEVELS
            and cluster.evidence_refs
            and cluster.mechanism
            and cluster.target
        )
        if not cluster.conclusion_eligible:
            cluster.role = "independent"
            cluster.causal_status = "unknown"
        elif not primary_assigned and cluster.causal_status != "independent":
            cluster.role = "primary"
            cluster.causal_status = "primary"
            primary_assigned = True
        elif cluster.causal_status == "contributing":
            cluster.role = "contributing"
        else:
            cluster.role = "independent"
        cluster.qualification = (
            "confirmed_root_cause"
            if cluster.conclusion_eligible
            else "possible_root_cause"
            if cluster.cause_level == "direct_failure_mechanism" and cluster.mechanism and cluster.evidence_refs
            else "partial_localization"
            if cluster.target and cluster.evidence_refs
            else "observation"
        )
        if not cluster.conclusion_eligible:
            cluster.confidence = min(cluster.confidence, 0.49)
    if not primary_assigned:
        first_eligible = next((cluster for cluster in clusters if cluster.conclusion_eligible), None)
        if first_eligible:
            first_eligible.role = "primary"
            first_eligible.causal_status = "primary"
    if isinstance(session_tree, dict):
        # This helper still supplies bounded engineering observations for
        # legacy callers, but once a canonical session tree exists its
        # Analyzer-derived output is never a formal cluster.  Formal clusters
        # are derived exclusively from the AI DAG below.
        for cluster in clusters:
            cluster.conclusion_eligible = False
            cluster.role = "independent"
            cluster.causal_status = "unknown"
            if cluster.cause_level in {"direct_root_cause", "complete_source_root_cause"}:
                cluster.cause_level = "direct_failure_mechanism"
            cluster.qualification = (
                "possible_root_cause"
                if cluster.mechanism and cluster.evidence_refs
                else "observation"
            )
            cluster.confidence = min(cluster.confidence, 0.49)
    return clusters


def derive_root_cause_clusters_from_ai_tree(
    session_tree: dict[str, Any] | None,
    *,
    valid_evidence_refs: set[str] | None = None,
    anchor_evidence_refs: set[str] | None = None,
    runtime_anchor_evidence_refs: set[str] | None = None,
    line_anchor_evidence_refs: set[str] | None = None,
) -> list[RootCauseCluster]:
    """Derive formal clusters only from qualified AI nodes in the DAG."""
    if not isinstance(session_tree, dict):
        return []
    nodes = []
    for layer in session_tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for key in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            nodes.extend(item for item in layer.get(key, []) if isinstance(item, dict))
    known_ids = {
        str(node.get("candidate_id") or "")
        for node in nodes
        if node.get("candidate_id")
    }
    qualified_nodes: list[dict[str, Any]] = []
    parsed_nodes: list[AITreeCandidateNode] = []
    for node in nodes:
        try:
            ai_node = AITreeCandidateNode.model_validate(node)
        except Exception:
            continue
        parsed_nodes.append(ai_node)
        eligible, _ = qualify_ai_candidate(
            ai_node,
            valid_evidence_refs=valid_evidence_refs,
            known_candidate_ids=known_ids,
            anchor_evidence_refs=anchor_evidence_refs,
            runtime_anchor_evidence_refs=runtime_anchor_evidence_refs,
            line_anchor_evidence_refs=line_anchor_evidence_refs,
        )
        if not eligible:
            continue
        if any(parent_id not in known_ids for parent_id in ai_node.parent_candidate_ids):
            continue
        qualified_nodes.append(node)

    frontier_ids = set(select_terminal_candidate_ids(
        parsed_nodes,
        {str(node["candidate_id"]) for node in qualified_nodes},
    ))
    frontier = [node for node in qualified_nodes if str(node["candidate_id"]) in frontier_ids]
    primary_index = next(
        (index for index, node in enumerate(frontier) if node.get("role") == "primary"),
        0,
    )
    clusters: list[RootCauseCluster] = []
    for index, node in enumerate(frontier):
        refs = _unique(node.get("evidence_refs", []))
        is_primary = index == primary_index
        cluster_id = f"rc_cluster_ai_{node['candidate_id']}"
        clusters.append(RootCauseCluster(
            cluster_id=cluster_id,
            candidate_ids=[str(node["candidate_id"])],
            source_tree_candidate_ids=[str(node["candidate_id"])],
            role="primary" if is_primary else "contributing",
            causal_status="primary" if is_primary else "contributing",
            cause_level="complete_source_root_cause" if node.get("claim_type") == "complete_source_root_cause" else "direct_root_cause",
            supported_level=str(node.get("supported_level") or "resource"),
            mechanism=str(node.get("mechanism") or ""),
            target=str(node.get("target") or ""),
            claim=str(node.get("claim") or ""),
            why_it_happened=str((node.get("self_challenge") or {}).get("why_this_claim") or ""),
            cost_center_refs=_unique(node.get("cost_center_refs") or []),
            trigger_refs=_unique(node.get("trigger_refs") or []),
            mechanism_refs=_unique(node.get("mechanism_refs") or []),
            impact_refs=_unique(node.get("impact_refs") or []),
            source_relation_refs=_unique(node.get("source_relation_refs") or []),
            causal_chain=[CausalExplanationStep(
                step_id=f"{cluster_id}_step_1",
                candidate_id=str(node["candidate_id"]),
                statement=str(node.get("claim") or ""),
                evidence_refs=refs,
                supported_level=str(node.get("supported_level") or "resource"),
            )],
            evidence_refs=refs,
            confidence=float(node.get("confidence") or 0.0),
            conclusion_eligible=True,
            qualification="confirmed_root_cause",
        ))
    return clusters


def derive_localization_frontier_from_ai_tree(
    session_tree: dict[str, Any] | None,
    *,
    valid_evidence_refs: set[str] | None = None,
    anchor_evidence_refs: set[str] | None = None,
    runtime_anchor_evidence_refs: set[str] | None = None,
    line_anchor_evidence_refs: set[str] | None = None,
) -> list[CausalExplanationStep]:
    """Return deepest evidence-backed, non-formal findings from each DAG branch."""
    if not isinstance(session_tree, dict):
        return []
    nodes: list[dict[str, Any]] = []
    for layer in session_tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for key in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            nodes.extend(item for item in layer.get(key, []) if isinstance(item, dict))
    parsed_nodes: list[AITreeCandidateNode] = []
    reportable_ids: set[str] = set()
    formal_ids: set[str] = set()
    known_ids = {str(node.get("candidate_id") or "") for node in nodes if node.get("candidate_id")}
    for node in nodes:
        try:
            parsed = AITreeCandidateNode.model_validate(node)
        except Exception:
            continue
        parsed_nodes.append(parsed)
        refs = _unique(parsed.evidence_refs)
        formal, _ = qualify_ai_candidate(
            parsed,
            valid_evidence_refs=valid_evidence_refs,
            known_candidate_ids=known_ids,
            anchor_evidence_refs=anchor_evidence_refs,
            runtime_anchor_evidence_refs=runtime_anchor_evidence_refs,
            line_anchor_evidence_refs=line_anchor_evidence_refs,
        )
        if formal:
            formal_ids.add(parsed.candidate_id)
        if (
            parsed.claim.strip()
            and refs
            and parsed.generated_by in {"ai", "ai_candidate", "ai_guarded", "analyzer", "analyzer_observation"}
            and parsed.claim_transform not in {"inherited", "restored", "boundary"}
            and parsed.supported_level != "resource"
            and parsed.depth_kind == "base"
            and parsed.role != "rejected"
            and parsed.status in {"supported", "partial", "missing_evidence", "blocked"}
            and parsed.causal_status in {"supported", "unproven", "inconclusive"}
            and parsed.decision not in {"reject_candidate", "backtrack", "abstain"}
            and parsed.claim_status not in {"boundary", "duplicate", "rejected"}
            and (valid_evidence_refs is None or not (set(refs) - valid_evidence_refs))
        ):
            reportable_ids.add(parsed.candidate_id)
    frontier_ids = set(select_terminal_candidate_ids(parsed_nodes, reportable_ids)) - formal_ids
    by_id = {node.candidate_id: node for node in parsed_nodes}
    return [
        CausalExplanationStep(
            step_id=f"localization_{candidate_id}",
            candidate_id=candidate_id,
            statement=by_id[candidate_id].claim,
            evidence_refs=_unique(by_id[candidate_id].evidence_refs),
            claim_status=by_id[candidate_id].claim_status,
            step_kind="claim",
            supported_level=by_id[candidate_id].supported_level,
        )
        for candidate_id in select_terminal_candidate_ids(parsed_nodes, frontier_ids)
        if candidate_id in by_id
    ]


def collect_ai_gate_failures(
    session_tree: dict[str, Any] | None,
    *,
    valid_evidence_refs: set[str] | None = None,
    evidence_catalog: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Expose why AI-originated nodes did not enter formal conclusions."""
    if not isinstance(session_tree, dict):
        return []
    all_nodes = [
        node
        for layer in session_tree.get("layers", [])
        if isinstance(layer, dict)
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes")
        for node in layer.get(group, [])
        if isinstance(node, dict)
    ]
    nodes = [
        node
        for node in all_nodes
        if node.get("generated_by") in {"ai", "ai_candidate", "ai_guarded"}
    ]
    known_ids = {str(node.get("candidate_id") or "") for node in all_nodes if node.get("candidate_id")}
    anchor_refs = evidence_refs_with_anchors(evidence_catalog or [])
    runtime_anchor_refs = evidence_refs_with_runtime_anchors(evidence_catalog or [])
    line_anchor_refs = evidence_refs_with_line_anchors(evidence_catalog or [])
    failures: list[dict[str, Any]] = []
    for node in nodes:
        try:
            ai_node = AITreeCandidateNode.model_validate(node)
        except Exception as exc:
            failures.append({
                "candidate_id": str(node.get("candidate_id") or ""),
                "failure_code": "invalid_candidate_shape",
                "reason": str(exc)[:240],
                "evidence_refs": _unique(node.get("evidence_refs", [])),
            })
            continue
        eligible, reason = qualify_ai_candidate(
            ai_node,
            valid_evidence_refs=valid_evidence_refs,
            known_candidate_ids=known_ids,
            anchor_evidence_refs=anchor_refs,
            runtime_anchor_evidence_refs=runtime_anchor_refs,
            line_anchor_evidence_refs=line_anchor_refs,
        )
        missing_parents = [
            parent_id for parent_id in ai_node.parent_candidate_ids
            if parent_id not in known_ids
        ]
        known_refs = set(valid_evidence_refs or set())
        evidence_windows = _evidence_windows_for_refs(
            ai_node.evidence_refs,
            evidence_catalog or [],
        )
        has_verified_window = any(
            str(window.get("timing_relation") or "unknown") != "unknown"
            and (
                window.get("window_start") is not None
                or window.get("window_end") is not None
                or window.get("evidence_cohort_id")
            )
            for window in evidence_windows.values()
        )
        parent_is_declared = ai_node.relation == "root" or bool(ai_node.parent_candidate_ids)
        gate_checks = {
            "source_is_ai": ai_node.generated_by in {"ai", "ai_candidate", "ai_guarded"},
            "candidate_id": bool(ai_node.candidate_id),
            "evidence_refs": bool(ai_node.evidence_refs),
            "evidence_refs_exist": not (
                valid_evidence_refs is not None
                and any(ref not in known_refs for ref in ai_node.evidence_refs)
            ),
            "runtime_or_source_anchor": (
                bool(set(ai_node.evidence_refs) & anchor_refs)
            ),
            "runtime_anchor": bool(set(ai_node.evidence_refs) & runtime_anchor_refs),
            "line_anchor": (
                ai_node.supported_level != "line"
                or bool(set(ai_node.evidence_refs) & line_anchor_refs)
            ),
            "target": bool(ai_node.target.strip()),
            "window": has_verified_window,
            "supported_level": bool(ai_node.supported_level),
            "mechanism": bool(ai_node.mechanism.strip()),
            "causal_status": ai_node.causal_status == "supported",
            "decision": ai_node.decision == "conclude",
            "required_probe": bool(
                ai_node.self_challenge.missing_evidence
                or ai_node.self_challenge.what_would_change_my_mind
                or ai_node.evidence_refs
            ),
            "parent_exists": parent_is_declared and not missing_parents,
            "origin_parent": bool(
                ai_node.relation == "root"
                or (
                    ai_node.origin_parent_candidate_id
                    and ai_node.origin_parent_candidate_id in ai_node.parent_candidate_ids
                    and ai_node.origin_parent_candidate_id in known_ids
                )
            ),
            "causal_chain": bool(
                ai_node.self_challenge.why_this_claim.strip()
                and ai_node.evidence_refs
            ),
            "trigger_relation_refs": bool(ai_node.trigger_refs),
            "mechanism_relation_refs": bool(ai_node.mechanism_refs),
            "impact_relation_refs": bool(ai_node.impact_refs),
            "source_relation_refs": bool(ai_node.source_relation_refs),
        }
        if not eligible or missing_parents:
            evidence_refs = _unique(ai_node.evidence_refs)
            failed_gates = [
                key for key, passed in gate_checks.items()
                if not passed
            ]
            failures.append({
                "candidate_id": ai_node.candidate_id,
                "failure_code": "missing_parent" if missing_parents else "eligibility_gate",
                "reason": "父节点不存在: " + ", ".join(missing_parents) if missing_parents else reason,
                "status": ai_node.status,
                "causal_status": ai_node.causal_status,
                "decision": ai_node.decision,
                "supported_level": ai_node.supported_level,
                "evidence_refs": evidence_refs,
                "valid_initial_evidence_refs": sorted(known_refs)[:256],
                "initial_evidence_context": _initial_evidence_context_for_refs(
                    ai_node.evidence_refs,
                    evidence_catalog or [],
                ),
                "missing_initial_evidence_refs": sorted(
                    ref for ref in evidence_refs if ref not in known_refs
                )[:128],
                "evidence_windows": evidence_windows,
                "parent_candidate_ids": list(ai_node.parent_candidate_ids),
                "missing_parent_candidate_ids": missing_parents,
                "gate_checks": gate_checks,
                "failed_gates": failed_gates,
                "required_probe": list(ai_node.self_challenge.missing_evidence),
                "target": ai_node.target,
                "mechanism": ai_node.mechanism,
                "origin_parent_candidate_id": ai_node.origin_parent_candidate_id,
            })
    return failures


def _evidence_windows_for_refs(
    refs: Iterable[str],
    evidence_catalog: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    catalog_by_ref: dict[str, dict[str, Any]] = {}
    for item in evidence_catalog:
        if not isinstance(item, dict):
            continue
        for key in ("evidence_id", "evidence_ref", "raw_artifact_ref", "derived_artifact_ref"):
            value = str(item.get(key) or "").strip()
            if value:
                catalog_by_ref[value] = item

    result: dict[str, dict[str, Any]] = {}
    for ref in _unique(refs):
        item = catalog_by_ref.get(str(ref))
        if item is None:
            continue
        observed = item.get("observed_value")
        observed = observed if isinstance(observed, dict) else {}
        summary = observed.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        candidates = (
            item.get("evidence_window"),
            observed.get("evidence_window"),
            summary.get("evidence_window"),
            observed.get("evidence_index"),
            summary.get("evidence_index"),
        )
        window = next(
            (value for value in candidates if isinstance(value, dict)),
            {},
        )
        if window:
            result[str(ref)] = {
                key: window[key]
                for key in (
                    "collection_mode",
                    "timing_relation",
                    "window_start",
                    "window_end",
                    "evidence_cohort_id",
                )
                if key in window
            }
    return result


def _initial_evidence_context_for_refs(
    refs: Iterable[str],
    evidence_catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    windows = _evidence_windows_for_refs(refs, evidence_catalog)
    return {
        "evidence_refs": _unique(refs),
        "evidence_windows": windows,
        "evidence_statuses": {
            str(ref): str(
                (
                    (
                        next(
                            (
                                item for item in evidence_catalog
                                if isinstance(item, dict)
                                and str(item.get("evidence_id") or item.get("evidence_ref") or "") == str(ref)
                            ),
                            {},
                        ).get("data_quality")
                        or {}
                    ).get("completeness")
                    or "unknown"
                )
            )
            for ref in _unique(refs)
        },
    }


def collect_candidate_generation_gate_failures(
    candidate_review: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Expose why the first AI candidate round could not produce usable candidates."""
    if not isinstance(candidate_review, dict):
        return []
    diagnostics = candidate_review.get("validation_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, list) else []
    ingestion_diagnostics = candidate_review.get("tree_ingestion_diagnostics")
    ingestion_diagnostics = ingestion_diagnostics if isinstance(ingestion_diagnostics, list) else []
    initial_context = (
        candidate_review.get("initial_evidence_context")
        if isinstance(candidate_review.get("initial_evidence_context"), dict)
        else {}
    )
    failures: list[dict[str, Any]] = []
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        failures.append({
            "stage": "candidate_generation",
            "candidate_id": str(diagnostic.get("candidate_id") or ""),
            "failure_code": str(diagnostic.get("failure_code") or "validation_error"),
            "failure_path": str(diagnostic.get("failure_path") or ""),
            "reason": str(
                diagnostic.get("reason")
                or diagnostic.get("actual_value")
                or "候选没有通过首轮结构校验。"
            )[:500],
            "actual_value": diagnostic.get("actual_value"),
            "expected_values": diagnostic.get("expected_values") or [],
            "candidate_evidence_refs": list(diagnostic.get("candidate_evidence_refs") or []),
            "initial_evidence_refs": list(
                diagnostic.get("initial_evidence_refs")
                or initial_context.get("evidence_refs")
                or []
            ),
            "initial_evidence_context": initial_context,
            "missing_initial_evidence_refs": list(
                diagnostic.get("missing_initial_evidence_refs") or []
            ),
            "parent_candidate_ids": list(diagnostic.get("candidate_parent_candidate_ids") or []),
            "missing_parent_candidate_ids": list(
                diagnostic.get("missing_parent_candidate_ids") or []
            ),
        })
    for diagnostic in ingestion_diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        failures.append({
            "stage": "candidate_tree_ingestion",
            "candidate_id": str(diagnostic.get("candidate_id") or ""),
            "failure_code": str(diagnostic.get("failure_code") or "tree_ingestion_error"),
            "failure_path": "session_main.parent_candidate_ids",
            "reason": str(diagnostic.get("reason") or "AI 候选没有进入当前主树。")[:500],
            "actual_value": diagnostic.get("parent_candidate_ids") or diagnostic.get("selection"),
            "expected_values": ["当前 emitted candidate_id"],
            "candidate_parent_candidate_ids": list(diagnostic.get("parent_candidate_ids") or []),
            "missing_parent_candidate_ids": list(diagnostic.get("missing_parent_candidate_ids") or []),
            "initial_evidence_context": initial_context,
            "initial_evidence_refs": list(
                initial_context.get("evidence_refs") or []
            ),
        })
    proposals = candidate_review.get("candidate_proposals")
    proposals = proposals if isinstance(proposals, list) else []
    status = str(candidate_review.get("ai_review_status") or "")
    if not proposals and status in {"failed", "fallback"}:
        failures.append({
            "stage": "candidate_generation",
            "candidate_id": "",
            "failure_code": "no_usable_ai_candidate",
            "failure_path": "candidates",
            "reason": str(
                candidate_review.get("ai_review_error")
                or "首轮 AI 没有形成任何通过结构校验的候选，已退回 Analyzer fallback 调查方向。"
            )[:500],
            "attempts": candidate_review.get("candidate_generation_attempts") or [],
            "initial_evidence_context": initial_context,
            "initial_evidence_refs": list(initial_context.get("evidence_refs") or []),
        })
    return failures


def classify_cluster_set(clusters: Iterable[RootCauseCluster]) -> str:
    eligible = [cluster for cluster in clusters if cluster.conclusion_eligible]
    independent_keys = {(cluster.mechanism, cluster.target) for cluster in eligible}
    if len(independent_keys) >= 2:
        return "compound_incident"
    if not eligible:
        return "insufficient_evidence"
    mechanism = eligible[0].mechanism
    return {
        "downstream_dependency_failure": "downstream_dependency",
        "process_suspended": "runtime_stall",
        "same_host_cpu_contention": "same_host_noisy_neighbor",
    }.get(mechanism, mechanism)


def build_retained_conclusion(
    clusters: list[RootCauseCluster],
    assessment: dict[str, Any],
    session_tree: dict[str, Any] | None = None,
    *,
    previous_retained: dict[str, Any] | None = None,
    inherited: bool = False,
) -> dict[str, Any] | None:
    """Select an existing evidence-backed claim; never invent a fallback claim."""
    scenario_retained = (
        assessment.get("scenario_retained_conclusion")
        if isinstance(assessment.get("scenario_retained_conclusion"), dict)
        else None
    )
    has_eligible_cluster = any(cluster.conclusion_eligible for cluster in clusters)
    if scenario_retained and not has_eligible_cluster:
        return RetainedConclusion.model_validate(scenario_retained).model_dump(mode="json")

    tree_nodes = _tree_base_nodes(session_tree)
    tree_retained_id = (
        str(session_tree.get("retained_candidate_id") or "").strip()
        if isinstance(session_tree, dict)
        else ""
    )
    # Once AI has produced usable candidates, the current session tree owns
    # the active investigation direction. The Analyzer assessment is only the
    # fallback source when the tree has no explicit retained candidate.
    active_id = str(
        tree_retained_id
        or assessment.get("active_retained_candidate_id")
        or ""
    ).strip()
    selected_node = next(
        (node for node in tree_nodes if node.get("candidate_id") == active_id),
        None,
    ) if active_id else None
    if selected_node is None and not active_id:
        final_ids: list[str] = []
        if isinstance(session_tree, dict):
            final_ids = _unique([
                *(session_tree.get("final_primary_causes") or []),
                *(session_tree.get("final_secondary_causes") or []),
            ])
        selected_node = next(
            (node for node in tree_nodes if node.get("candidate_id") in final_ids),
            None,
        )
    if selected_node is None and len(tree_nodes) == 1:
        selected_node = tree_nodes[0]

    ordered_clusters = sorted(
        clusters,
        key=lambda item: (
            0 if item.conclusion_eligible else 1,
            0 if item.qualification == "possible_root_cause" else 1,
            -item.confidence,
        ),
    )
    selected_cluster = ordered_clusters[0] if ordered_clusters else None
    if selected_cluster is not None and (
        not selected_cluster.conclusion_eligible
        and str(selected_cluster.cause_level or "") in {"observation", "call_path"}
    ):
        selected_cluster = None
    if selected_node is None and selected_cluster is not None:
        cluster_ids = _unique([
            *selected_cluster.source_tree_candidate_ids,
            *selected_cluster.candidate_ids,
        ])
        selected_node = next(
            (node for node in tree_nodes if node.get("candidate_id") in cluster_ids),
            None,
        )

    previous = previous_retained if isinstance(previous_retained, dict) else None
    if (
        selected_node is None
        and selected_cluster is None
        and previous
        and previous.get("claim")
        and previous.get("status") not in {"contradicted", "rejected"}
    ):
        retained = dict(previous)
        retained["status"] = {
            "missing_evidence": "partial",
            "unknown": "observation",
            "weakened": "partial",
        }.get(str(retained.get("status") or ""), retained.get("status") or "observation")
        retained.setdefault("qualification", "partial_localization")
        retained["inherited"] = True
        retained["fallback_mode"] = "inherit_parent"
        retained["claim_transform"] = "inherited"
        retained["claim_status"] = "inherited"
        retained["source_claim_hash"] = retained.get("claim_hash") or hash_claim(retained.get("claim"))
        retained["claim_hash"] = hash_claim(retained.get("claim"))
        return RetainedConclusion.model_validate(retained).model_dump(mode="json")

    claim = str(
        (selected_node or {}).get("claim")
        or (selected_cluster.claim if selected_cluster else "")
        or assessment.get("diagnostic_claim")
        or ""
    ).strip()
    if not claim:
        return None
    candidate_id = str(
        (selected_node or {}).get("candidate_id")
        or (
            selected_cluster.source_tree_candidate_ids[0]
            if selected_cluster and selected_cluster.source_tree_candidate_ids
            else selected_cluster.candidate_ids[0]
            if selected_cluster and selected_cluster.candidate_ids
            else assessment.get("active_retained_candidate_id")
            or assessment.get("root_entity")
            or "assessment"
        )
    ).strip()
    evidence_refs = _unique([
        *((selected_node or {}).get("evidence_refs") or []),
        *(selected_cluster.evidence_refs if selected_cluster else []),
        *(assessment.get("evidence_refs") or []),
    ])
    eligible = bool(selected_cluster and selected_cluster.conclusion_eligible)
    possible = bool(
        selected_cluster
        and selected_cluster.qualification in {"possible_root_cause", "partial_localization"}
    )
    level = str(
        (selected_node or {}).get("supported_level")
        or assessment.get("supported_level")
        or assessment.get("max_supported_level")
        or "resource"
    )
    qualification = "formal_root_cause" if eligible else "possible_root_cause" if possible else "partial_localization" if evidence_refs else "observation"
    status = str((selected_node or {}).get("status") or "")
    if status not in {"supported", "partial", "blocked", "inconclusive", "observation"}:
        status = "supported" if eligible else "partial" if evidence_refs else "observation"
    confidence = _number(
        (selected_node or {}).get("confidence")
        if selected_node is not None
        else selected_cluster.confidence
        if selected_cluster is not None
        else assessment.get("confidence")
    )
    source_id = str(
        (selected_node or {}).get("origin_parent_candidate_id")
        or (selected_node or {}).get("source_candidate_id")
        or candidate_id
    ).strip()
    lineage = ensure_claim_lineage(selected_node or {
        "claim": claim,
        "diagnostic_claim": assessment.get("diagnostic_claim"),
        "summary": assessment.get("summary"),
    })
    retained_causal_status = str(
        (selected_node or {}).get("causal_status")
        or assessment.get("causal_status")
        or ""
    )
    if retained_causal_status not in {"supported", "unproven", "contradicted", "inconclusive"}:
        retained_causal_status = "supported" if eligible else "inconclusive"
    return RetainedConclusion(
        candidate_id=candidate_id,
        claim=claim,
        supported_level=level,
        status=status,
        qualification=qualification,
        evidence_refs=evidence_refs,
        confidence=max(0.0, min(1.0, confidence)),
        causal_status=retained_causal_status,
        source_candidate_id=source_id,
        inherited=inherited,
        fallback_mode="inherit_parent" if inherited else "none",
        generated_by=lineage["generated_by"],
        claim_origin=lineage["claim_origin"],
        claim_transform="inherited" if inherited else lineage["claim_transform"],
        claim_status="inherited" if inherited else "retained",
        claim_hash=hash_claim(claim),
        source_claim_hash=lineage["claim_hash"],
        source_round=lineage["source_round"],
        source_event_id=lineage["source_event_id"],
    ).model_dump(mode="json")


def build_scenario_retained_conclusion(
    observations: list[dict[str, Any]],
    session_tree: dict[str, Any] | None,
    *,
    target_service: str,
) -> dict[str, Any] | None:
    """Keep valid Python scenario observations visible without promoting them to roots."""
    priority = {
        "python_queue_profile": 0,
        "python_pool_profile": 1,
        "python_retry_timeout_profile": 2,
        "python_cache_profile": 3,
        "python_input_profile": 4,
        "python_exception_profile": 5,
        "python_lock_wait_profile": 6,
    }
    candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for observation in observations:
        for gate in _scenario_gates_from_observation(observation):
            family = str(gate.get("family") or "").strip()
            checks = gate.get("gate_checks") if isinstance(gate.get("gate_checks"), dict) else {}
            if str(gate.get("evidence_status") or "").lower() not in {"valid", "partial"}:
                continue
            if gate.get("conclusion_eligible") is True:
                continue
            if checks and checks.get("scenario_signal") is False:
                continue
            if not _unique(gate.get("mechanism_evidence_refs") or observation.get("evidence_refs") or []):
                continue
            candidates.append((priority.get(family, 99), gate, observation))
    if not candidates:
        return None

    _, gate, observation = sorted(candidates, key=lambda item: item[0])[0]
    family = str(gate.get("family") or "").strip() or "python_scenario"
    scenario_type = str(gate.get("scenario_type") or family).strip()
    refs = _unique([
        *(gate.get("mechanism_evidence_refs") or []),
        *(observation.get("evidence_refs") or []),
    ])
    nodes = _tree_base_nodes(session_tree)
    selected_node = next(
        (
            node for node in nodes
            if str(node.get("mechanism") or "") in {scenario_type, family}
            and str(node.get("status") or "") not in {"rejected", "contradicted", "forbidden"}
        ),
        None,
    )
    target = _scenario_observation_target(observation, target_service)
    claim = str((selected_node or {}).get("claim") or "").strip()
    if not claim:
        claim = (
            f"Python 场景证据显示 {target} 存在 {scenario_type}，"
            "当前可作为场景级局部定位，但缺少源码行和机制闭合证据，不能升级为正式根因。"
        )
    candidate_id = str((selected_node or {}).get("candidate_id") or f"scenario_{family}").strip()
    lineage = ensure_claim_lineage(selected_node or {"claim": claim})
    return RetainedConclusion(
        candidate_id=candidate_id,
        claim=claim,
        supported_level=str((selected_node or {}).get("supported_level") or "process"),
        status="partial",
        qualification="partial_localization",
        evidence_refs=refs,
        confidence=max(0.45, _number((selected_node or {}).get("confidence") or 0.0)),
        causal_status="inconclusive",
        source_candidate_id=str(
            (selected_node or {}).get("origin_parent_candidate_id")
            or (selected_node or {}).get("source_candidate_id")
            or candidate_id
        ),
        inherited=False,
        fallback_mode="none",
        generated_by="ai" if selected_node and str(selected_node.get("generated_by") or "").startswith("ai") else "analyzer",
        claim_origin=str(lineage["claim_origin"]),
        claim_transform=str(lineage["claim_transform"]),
        claim_status="retained",
        claim_hash=hash_claim(claim),
        source_claim_hash=lineage["claim_hash"],
        source_round=lineage["source_round"],
        source_event_id=lineage["source_event_id"],
    ).model_dump(mode="json")


def build_qualification_boundary(
    assessment: dict[str, Any],
    *,
    followup_requests: Iterable[str] = (),
    probes: Iterable[dict[str, Any]] = (),
    origin_parent_candidate_id: str | None = None,
) -> dict[str, Any]:
    """Describe an evidence boundary without replacing the retained claim."""
    explicit = assessment.get("qualification_boundary")
    if isinstance(explicit, dict):
        boundary = dict(explicit)
        boundary.setdefault("origin_parent_candidate_id", origin_parent_candidate_id)
        return QualificationBoundary.model_validate(boundary).model_dump(mode="json")
    missing = _unique([
        *(assessment.get("missing_evidence") or []),
        *followup_requests,
    ])
    anchor = assessment.get("primary_anchor")
    if isinstance(anchor, dict):
        anchor_reason = str(anchor.get("blocked_upgrade_reason") or "").strip()
        anchor_level = str(anchor.get("supported_level") or assessment.get("supported_level") or "")
        if anchor_reason and anchor_level != "line":
            missing = _unique([
                *missing,
                "source_context",
                "line_level_profile",
            ])
    statuses = {
        str(probe.get("evidence_status") or probe.get("status") or "").lower()
        for probe in probes
        if isinstance(probe, dict)
    }
    if statuses & {"blocked", "failed", "unavailable", "memray_attach_failed", "target_exit"}:
        status = "blocked"
        message = "深探未能完成，当前保留来源父结论；暂不能升级到更细定位。"
    elif statuses & {"partial", "empty_window", "unparseable"}:
        status = "partial"
        message = "深探只返回部分或空窗口证据，当前保留来源父结论；暂不能升级到更细定位。"
    elif missing:
        status = "inconclusive"
        message = "当前仍缺少必要补证，来源父结论继续有效；暂不能升级到更细定位。"
        if isinstance(anchor, dict) and anchor.get("blocked_upgrade_reason"):
            message += f" 源码行探测未进入正式升级：{str(anchor['blocked_upgrade_reason'])[:240]}"
    else:
        status = "none"
        message = ""
    return QualificationBoundary(
        status=status,
        message=message,
        missing_evidence=missing,
        origin_parent_candidate_id=origin_parent_candidate_id,
    ).model_dump(mode="json")


def _formal_root_cause(clusters: list[RootCauseCluster]) -> dict[str, Any] | None:
    primary = next(
        (cluster for cluster in clusters if cluster.conclusion_eligible and cluster.role == "primary"),
        next((cluster for cluster in clusters if cluster.conclusion_eligible), None),
    )
    return primary.model_dump(mode="json") if primary else None


def _tree_base_nodes(tree: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(tree, dict):
        return []
    result: list[dict[str, Any]] = []
    for layer in tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for group in ("primary_causes", "secondary_causes", "unknown_causes"):
            for node in layer.get(group, []):
                if not isinstance(node, dict):
                    continue
                if node.get("node_type") in {"observation", "mechanism_explanation", "stop_boundary"}:
                    continue
                if node.get("depth_kind") in {"mechanism", "boundary"}:
                    continue
                if node.get("status") in {"contradicted", "rejected", "forbidden"}:
                    continue
                if (
                    node.get("node_type") in {"cluster_root", "coarse_candidate", "call_path_context", "orphan"}
                    or node.get("supported_level") == "call_path"
                    or not node.get("candidate_id")
                ):
                    continue
                result.append(node)
    return result


def build_fallback_explanation(
    clusters: list[RootCauseCluster],
    assessment: dict[str, Any],
    *,
    status: str = "fallback",
    attempts: int = 0,
    model: str = "",
    error: str = "",
    session_tree: dict[str, Any] | None = None,
    previous_retained: dict[str, Any] | None = None,
    qualification_boundary: dict[str, Any] | None = None,
    localization_frontier: list[CausalExplanationStep] | None = None,
) -> dict[str, Any]:
    # A session-level AI failure cannot be converted into a formal conclusion
    # by reusing an already-shaped cluster. Preserve the direction as a
    # retained/localization result and explicitly demote formal fields.
    fallback_clusters = [
        cluster.model_copy(update={
            "conclusion_eligible": False,
            "role": "independent",
            "causal_status": "unknown",
            "cause_level": (
                "direct_failure_mechanism"
                if cluster.cause_level in {"direct_root_cause", "complete_source_root_cause"}
                else cluster.cause_level
            ),
            "qualification": (
                "possible_root_cause"
                if cluster.mechanism and cluster.evidence_refs
                else "observation"
            ),
            "confidence": min(cluster.confidence, 0.49),
        })
        for cluster in clusters
    ]
    eligible: list[RootCauseCluster] = []
    primary = None
    possible = next(
        (
            cluster for cluster in fallback_clusters
            if cluster.qualification in {"possible_root_cause", "partial_localization"}
            and str(cluster.cause_level or "") not in {"observation", "call_path"}
        ),
        None,
    )
    retained = build_retained_conclusion(
        fallback_clusters,
        assessment,
        session_tree,
        previous_retained=previous_retained,
        inherited=True,
    )
    retained_level = str((retained or {}).get("supported_level") or "").strip()
    level_label = {
        "resource": "资源",
        "host": "主机",
        "process": "进程",
        "thread": "线程",
        "syscall": "系统调用",
        "dependency": "依赖",
        "service": "服务",
        "endpoint": "端点",
        "function": "函数",
        "call_path": "调用路径",
        "line": "源码行",
    }.get(retained_level, "观察/定位")
    retained_claim = str((retained or {}).get("claim") or "").strip()
    headline = (
        _abstained_retained_headline(retained_claim)
        if retained_claim
        else (
            f"未形成正式根因；当前证据只支持停在{level_label}级观察/定位层，"
            "尚未闭合可验证的因果链。"
        )
    )
    boundary = qualification_boundary if isinstance(qualification_boundary, dict) else {}
    why = str(
        boundary.get("message")
        or (possible.why_it_happened if possible else "")
        or ((retained or {}).get("qualification") if retained else "")
        or assessment.get("eligibility_reason")
        or "当前只有观察事实，尚未建立可引用证据支持的因果机制。"
    )
    residual = _unique(
        item
        for cluster in fallback_clusters
        for item in cluster.residual_unknowns
    )
    classification = "insufficient_evidence"
    if classification == "insufficient_evidence" and assessment.get("classification"):
        classification = str(assessment["classification"])
    localization_chain = _merge_explanation_steps(
        localization_frontier or [],
        _build_localization_chain(session_tree, retained),
    )
    return {
        "headline": headline,
        "why_it_happened": why,
        "causal_chain": [],
        "localization_chain": localization_chain,
        "root_cause_clusters": [],
        "possible_root_causes": [
            cluster.model_dump(mode="json")
            for cluster in fallback_clusters
            if cluster.qualification in {"possible_root_cause", "partial_localization"}
        ],
        "ruled_out_summary": [
            str(item.get("reason"))
            for item in assessment.get("ruled_out", [])
            if isinstance(item, dict) and item.get("reason")
        ],
        "residual_unknowns": residual,
        "recommendations": [],
        "classification": classification,
        "ai_review_status": status,
        "ai_review_scope": "session",
        "ai_review_attempts": attempts,
        "ai_review_model": model,
        "ai_review_error": error[:500],
        "confidence_level": "低" if retained else "不可判断",
        "abstained": True,
        "retained_conclusion": retained,
        "formal_root_cause": None,
        "qualification_boundary": qualification_boundary or build_qualification_boundary(
            assessment,
            origin_parent_candidate_id=(retained or {}).get("candidate_id"),
        ),
        "active_retained_candidate_id": (retained or {}).get("candidate_id"),
    }


def _build_localization_chain(
    session_tree: dict[str, Any] | None,
    retained: dict[str, Any] | None,
) -> list[CausalExplanationStep]:
    """Return the retained node's real DAG lineage without claiming causality."""
    if not isinstance(session_tree, dict) or not isinstance(retained, dict):
        return []
    nodes: dict[str, dict[str, Any]] = {}
    for layer in session_tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            for node in layer.get(group, []):
                if isinstance(node, dict) and node.get("candidate_id"):
                    nodes.setdefault(str(node["candidate_id"]), node)
    retained_id = str(retained.get("candidate_id") or "")
    if retained_id not in nodes:
        return []
    ancestors: set[str] = set()
    stack = [retained_id]
    while stack:
        current_id = stack.pop()
        if current_id in ancestors or current_id not in nodes:
            continue
        ancestors.add(current_id)
        stack.extend(
            str(parent_id)
            for parent_id in nodes[current_id].get("parent_candidate_ids", [])
            if str(parent_id) in nodes
        )
    ordered: list[dict[str, Any]] = []
    emitted: set[str] = set()
    while len(emitted) < len(ancestors):
        ready = sorted(
            candidate_id
            for candidate_id in ancestors - emitted
            if all(
                str(parent_id) not in ancestors or str(parent_id) in emitted
                for parent_id in nodes[candidate_id].get("parent_candidate_ids", [])
            )
        )
        if not ready:
            break
        for candidate_id in ready:
            emitted.add(candidate_id)
            ordered.append(nodes[candidate_id])
    children_by_parent: dict[str, list[str]] = {}
    for node in nodes.values():
        origin = str(node.get("origin_parent_candidate_id") or "").strip()
        if origin:
            children_by_parent.setdefault(origin, []).append(str(node["candidate_id"]))
    line_path: list[dict[str, Any]] | None = None
    if ordered:
        queue: list[tuple[str, list[dict[str, Any]]]] = [
            (str(ordered[-1].get("candidate_id") or ""), [ordered[-1]])
        ]
        visited_descendants: set[str] = set()
        while queue:
            parent_id, path = queue.pop(0)
            for child_id in sorted(children_by_parent.get(parent_id, [])):
                if child_id in visited_descendants or child_id not in nodes:
                    continue
                visited_descendants.add(child_id)
                child = nodes[child_id]
                child_path = [*path, child]
                if (
                    str(child.get("generated_by") or "")
                    in {"ai", "ai_candidate", "ai_guarded"}
                    and str(child.get("node_type") or "") == "line_anchor"
                    and str(child.get("supported_level") or "") == "line"
                    and str(child.get("claim_transform") or "") == "refined"
                ):
                    line_path = child_path
                queue.append((child_id, child_path))
        if line_path:
            ordered.extend(line_path[1:])
    result: list[CausalExplanationStep] = []
    emitted_hashes: set[str] = set()
    for node in ordered:
        candidate_id = str(node.get("candidate_id") or "")
        claim_status = str(node.get("claim_status") or "active")
        boundary_message = str(node.get("boundary_message") or "").strip()
        if claim_status == "boundary" or node.get("relation") == "boundary" or node.get("depth_kind") == "boundary":
            statement = boundary_message or str(node.get("claim") or "").strip()
            if statement:
                result.append(CausalExplanationStep(
                    step_id=f"localization_{candidate_id}",
                    candidate_id=candidate_id,
                    claim_status="boundary",
                    step_kind="boundary",
                    statement=statement,
                    evidence_refs=_unique(node.get("evidence_refs") or []),
                ))
            continue
        claim = str(node.get("claim") or "").strip()
        claim_hash = str(node.get("claim_hash") or hash_claim(claim))
        if not claim or not claim_hash or claim_hash in emitted_hashes:
            continue
        emitted_hashes.add(claim_hash)
        result.append(CausalExplanationStep(
            step_id=f"localization_{candidate_id}",
            candidate_id=candidate_id,
            claim_status=claim_status,
            step_kind="inherited" if claim_status == "inherited" else "claim",
            statement="继承自父节点结论" if claim_status == "inherited" else claim,
            evidence_refs=_unique(node.get("evidence_refs") or []),
            supported_level=str(node.get("supported_level") or "resource"),
        ))
    return result


def _abstained_retained_headline(claim: str) -> str:
    normalized = " ".join(str(claim or "").split())
    if not normalized:
        return ""
    if normalized.startswith(("未形成正式根因", "当前证据支持")):
        return normalized
    if "不能升级为正式根因" in normalized or "未形成正式源码根因" in normalized:
        return f"当前证据支持场景级定位：{normalized}"
    return f"当前证据支持场景级定位：{normalized} 但未形成正式源码根因。"


def apply_session_review(
    clusters: list[RootCauseCluster],
    review: SessionConclusionReview | dict[str, Any],
    *,
    attempts: int = 1,
    model: str = "",
    retained_conclusion: dict[str, Any] | None = None,
    qualification_boundary: dict[str, Any] | None = None,
    localization_frontier: list[CausalExplanationStep] | None = None,
) -> dict[str, Any]:
    parsed = review if isinstance(review, SessionConclusionReview) else SessionConclusionReview.model_validate(review)
    by_id = {cluster.cluster_id: cluster for cluster in clusters}
    for cluster_id, role in parsed.cluster_roles.items():
        by_id[cluster_id].role = role
        if role == "contributing":
            by_id[cluster_id].relation_to_primary = "AI 会话裁决认为该原因有目标受压证据，并会放大主因造成的症状。"
        elif role == "independent":
            by_id[cluster_id].relation_to_primary = "AI 会话裁决认为该异常同窗独立成立，但现有证据未证明它影响目标服务。"
    for cluster_id, recommendations in parsed.recommendations.items():
        by_id[cluster_id].recommendations = recommendations
    ordered = sorted(
        clusters,
        key=lambda item: ({"primary": 0, "contributing": 1, "independent": 2}.get(item.role, 3), item.cluster_id),
    )
    return {
        "headline": parsed.headline,
        "why_it_happened": parsed.why_it_happened,
        "causal_chain": parsed.causal_chain,
        "localization_chain": _merge_explanation_steps(
            parsed.localization_chain,
            localization_frontier or [],
        ),
        "root_cause_clusters": ordered,
        "ruled_out_summary": parsed.ruled_out_summary,
        "residual_unknowns": parsed.residual_unknowns,
        "recommendations": [recommendation for item in ordered for recommendation in item.recommendations],
        "classification": classify_cluster_set(ordered),
        "ai_review_status": "succeeded",
        "ai_review_scope": "session",
        "ai_review_attempts": attempts,
        "ai_review_model": model,
        "ai_review_error": "",
        "retained_conclusion": retained_conclusion,
        "formal_root_cause": _formal_root_cause(ordered),
        "qualification_boundary": qualification_boundary or {"status": "none", "message": "", "missing_evidence": [], "origin_parent_candidate_id": (retained_conclusion or {}).get("candidate_id")},
        "active_retained_candidate_id": (retained_conclusion or {}).get("candidate_id"),
    }


def validate_session_review(
    review: SessionConclusionReview,
    clusters: list[RootCauseCluster],
    valid_evidence_refs: set[str],
    localization_frontier: list[CausalExplanationStep] | None = None,
) -> list[str]:
    issues: list[str] = []
    by_id = {cluster.cluster_id: cluster for cluster in clusters if cluster.conclusion_eligible}
    role_ids = set(review.cluster_roles)
    if review.primary_cluster_id not in by_id:
        issues.append("primary_cluster_id does not reference an eligible cluster")
    if role_ids - set(by_id):
        issues.append("cluster_roles contains unknown or ineligible cluster IDs")
    if role_ids != set(by_id):
        issues.append("cluster_roles must cover every eligible cluster")
    if review.cluster_roles.get(str(review.primary_cluster_id)) != "primary":
        issues.append("primary cluster must have role=primary")
    if sum(role == "primary" for role in review.cluster_roles.values()) != 1:
        issues.append("exactly one primary cluster is required")
    for cluster_id, role in review.cluster_roles.items():
        cluster = by_id.get(cluster_id)
        if cluster is None:
            continue
        if role == "contributing" and cluster.causal_status not in {"primary", "contributing"}:
            issues.append(f"cluster {cluster_id} lacks target-impact evidence required for role=contributing")
    selected_primary = by_id.get(str(review.primary_cluster_id))
    if selected_primary and selected_primary.causal_status == "independent" and any(
        cluster.causal_status != "independent" for cluster in by_id.values() if cluster.cluster_id != selected_primary.cluster_id
    ):
        issues.append("an independent anomaly cannot replace an evidence-backed causal primary cluster")
    allowed_refs = (
        valid_evidence_refs
        | {ref for cluster in clusters for ref in cluster.evidence_refs}
        | {ref for step in (localization_frontier or []) for ref in step.evidence_refs}
    )
    for step in review.causal_chain:
        if not step.evidence_refs:
            issues.append(f"causal step {step.step_id} has no evidence refs")
        elif set(step.evidence_refs) - allowed_refs:
            issues.append(f"causal step {step.step_id} contains unknown evidence refs")
    formal_candidate_ids = {
        candidate_id
        for cluster in clusters
        for candidate_id in [*cluster.source_tree_candidate_ids, *cluster.candidate_ids]
    }
    localization_by_id = {
        step.candidate_id: step
        for step in (localization_frontier or [])
        if step.candidate_id
    }
    covered_cluster_ids = {
        cluster.cluster_id
        for cluster in by_id.values()
        if any(
            step.candidate_id in set([*cluster.source_tree_candidate_ids, *cluster.candidate_ids])
            for step in review.causal_chain
        )
    }
    required_cluster_ids = {
        cluster_id
        for cluster_id, role in review.cluster_roles.items()
        if role in {"primary", "contributing"}
    }
    if required_cluster_ids - covered_cluster_ids:
        issues.append("causal_chain does not cover every primary/contributing cluster")
    for step in review.causal_chain:
        if not step.candidate_id:
            issues.append(f"explanation step {step.step_id} has no candidate_id")
        elif step.candidate_id not in formal_candidate_ids:
            issues.append(f"explanation step {step.step_id} contains unknown candidate_id")
        if set(step.evidence_refs) - allowed_refs:
            issues.append(f"explanation step {step.step_id} contains unknown evidence refs")
        matching_cluster = next(
            (
                cluster
                for cluster in by_id.values()
                if step.candidate_id in set([*cluster.source_tree_candidate_ids, *cluster.candidate_ids])
            ),
            None,
        )
        if matching_cluster is not None:
            if set(step.evidence_refs) - set(matching_cluster.evidence_refs):
                issues.append(f"causal step {step.step_id} contains evidence outside its cluster")
            if step.supported_level != matching_cluster.supported_level:
                issues.append(f"causal step {step.step_id} changes supported_level")
    localization_ids = {step.candidate_id for step in review.localization_chain if step.candidate_id}
    if localization_ids != set(localization_by_id):
        issues.append("localization_chain must cover exactly the provided localization frontier")
    for step in review.localization_chain:
        source = localization_by_id.get(step.candidate_id)
        if source is None:
            continue
        if set(step.evidence_refs) - set(source.evidence_refs):
            issues.append(f"localization step {step.step_id} contains evidence outside its candidate")
        if step.supported_level != source.supported_level:
            issues.append(f"localization step {step.step_id} changes supported_level")
    if not review.headline.strip() or not review.why_it_happened.strip():
        issues.append("headline and why_it_happened are required")
    if review.headline.strip() == review.why_it_happened.strip():
        issues.append("headline and why_it_happened must not duplicate the same text")
    for cluster_id in review.recommendations:
        if cluster_id not in by_id:
            issues.append("recommendations contains unknown or ineligible cluster ID")
    required_types = {"investigation", "temporary_mitigation", "permanent_fix"}
    for cluster_id in by_id:
        recommendations = review.recommendations.get(cluster_id, [])
        if {item.recommendation_type for item in recommendations} != required_types:
            issues.append(f"cluster {cluster_id} must include all three recommendation types")
        for item in recommendations:
            combined = f"{item.action} {item.rationale}".lower()
            if any(token in combined for token in ("继续观察", "人工确认后处理", "根据情况处理", "进一步排查")):
                issues.append(f"cluster {cluster_id} contains a generic recommendation")
            if any(token in combined for token in ("sudo ", "sysctl ", "systemctl ", "docker restart", "rm -")):
                issues.append(f"cluster {cluster_id} recommendation contains an executable system command")
    return issues


def _merge_explanation_steps(
    preferred: Iterable[CausalExplanationStep],
    fallback: Iterable[CausalExplanationStep],
) -> list[CausalExplanationStep]:
    result: list[CausalExplanationStep] = []
    seen: set[str] = set()
    for step in [*preferred, *fallback]:
        key = step.candidate_id or hash_claim(step.statement)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(step)
    return result


def _cluster_template(
    *,
    mechanism: str,
    target: str,
    cause_level: str,
    claim: str,
    why: str,
    symptoms: list[str],
    refs: list[str],
    confidence: float,
    cohort: str,
    propagation_path: str,
    source_ids: list[str],
    eligible: bool,
    unknowns: list[str],
    causal_status: str = "unknown",
    supported_level: str = "resource",
) -> dict[str, Any]:
    key = f"{mechanism}|{target}|{cohort}|{propagation_path}"
    cluster_id = "rc_cluster_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    causal_chain = [CausalExplanationStep(
        step_id=f"{cluster_id}_step_1",
        statement=why,
        evidence_refs=refs,
    )] if refs else []
    return {
        "cluster_id": cluster_id,
        "candidate_ids": [_candidate_id(mechanism, target)],
        "source_tree_candidate_ids": _unique(source_ids),
        "role": "primary",
        "causal_status": causal_status if causal_status in {"primary", "contributing", "independent", "unknown"} else "unknown",
        "cause_level": cause_level if cause_level in {
            "observation", "direct_failure_mechanism", "direct_root_cause", "complete_source_root_cause",
        } else "observation",
        "supported_level": supported_level if supported_level in {
            "resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line",
        } else "resource",
        "mechanism": mechanism,
        "target": target,
        "claim": claim,
        "why_it_happened": why,
        "explained_symptoms": symptoms,
        "causal_chain": causal_chain,
        "relation_to_primary": "",
        "evidence_refs": refs,
        "confidence": max(0.0, min(1.0, confidence)),
        "residual_unknowns": unknowns,
        "recommendations": _recommendations(mechanism, target),
        "conclusion_eligible": eligible,
        "_key": key,
    }


def _scenario_gate_cluster_candidates(
    observations: list[dict[str, Any]],
    target_service: str,
    source_ids: dict[str, list[str]],
) -> list[dict[str, Any]]:
    # Kept as a compatibility seam for callers and old audit data. Scenario
    # gates are evidence/localization inputs only; they cannot manufacture a
    # formal root-cause cluster without a qualified session AI candidate.
    return []


def _scenario_gates_from_observation(observation: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for key in ("evidence_index", "confidence_inputs"):
        value = observation.get(key) if isinstance(observation.get(key), dict) else {}
        gates = value.get("python_scenario_gates")
        if isinstance(gates, dict):
            candidates.append(gates)
    observed = observation.get("observed_value") if isinstance(observation.get("observed_value"), dict) else {}
    summary = observed.get("summary") if isinstance(observed.get("summary"), dict) else {}
    for container in (observed, summary):
        index = container.get("evidence_index") if isinstance(container.get("evidence_index"), dict) else {}
        gates = index.get("python_scenario_gates")
        if isinstance(gates, dict):
            candidates.append(gates)
        confidence = index.get("confidence_inputs") if isinstance(index.get("confidence_inputs"), dict) else {}
        gates = confidence.get("python_scenario_gates")
        if isinstance(gates, dict):
            candidates.append(gates)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for gates in candidates:
        for family, gate in gates.items():
            if not isinstance(gate, dict):
                continue
            enriched = dict(gate)
            enriched.setdefault("family", family)
            key = f"{family}|{enriched.get('source_context_hash')}|{enriched.get('eligibility_reason')}"
            if key in seen:
                continue
            seen.add(key)
            result.append(enriched)
    return result


def _scenario_gate_supports_direct_root_cause(gate: dict[str, Any]) -> bool:
    # Legacy compatibility helper. The Evidence Structurer never grants
    # formal eligibility; only a session AI candidate can do so.
    return False


def _scenario_observation_target(observation: dict[str, Any], fallback: str) -> str:
    for candidate in (
        observation.get("target") if isinstance(observation.get("target"), dict) else {},
        observation.get("observed_value", {}).get("target")
        if isinstance(observation.get("observed_value"), dict)
        and isinstance(observation.get("observed_value", {}).get("target"), dict)
        else {},
    ):
        for key in ("service_id", "instance_id", "endpoint", "pid"):
            value = candidate.get(key)
            if value:
                return str(value)
    return fallback


def _deduplicate(candidates: list[dict[str, Any]]) -> list[RootCauseCluster]:
    merged: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = candidate.pop("_key")
        current = merged.get(key)
        if current is None:
            merged[key] = candidate
            continue
        current["candidate_ids"] = _unique([*current["candidate_ids"], *candidate["candidate_ids"]])
        current["source_tree_candidate_ids"] = _unique([*current["source_tree_candidate_ids"], *candidate["source_tree_candidate_ids"]])
        current["evidence_refs"] = _unique([*current["evidence_refs"], *candidate["evidence_refs"]])
        current["confidence"] = max(current["confidence"], candidate["confidence"])
        current["conclusion_eligible"] = current["conclusion_eligible"] or candidate["conclusion_eligible"]
        current["causal_chain"][0] = current["causal_chain"][0].model_copy(update={"evidence_refs": current["evidence_refs"]})
    return [RootCauseCluster.model_validate(item) for item in merged.values()]


def _recommendations(mechanism: str, target: str) -> list[RootCauseRecommendation]:
    if mechanism == "downstream_dependency_failure":
        return [
            RootCauseRecommendation(recommendation_type="investigation", action=f"核对 {target} 同窗容器事件、运行控制记录和服务日志。", rationale="区分暂停、崩溃、网络不可达和服务内部失败。"),
            RootCauseRecommendation(recommendation_type="temporary_mitigation", action=f"在人工确认后恢复或隔离 {target}，并验证调用成功率。", rationale="先恢复被阻断的下游路径。"),
            RootCauseRecommendation(recommendation_type="permanent_fix", action=f"为 {target} 增加状态变更审计和依赖失败保护。", rationale="保留故障来源并降低单点依赖影响。"),
        ]
    if mechanism == "same_host_cpu_contention":
        return [
            RootCauseRecommendation(recommendation_type="investigation", action=f"核对 {target} 的启动来源、CPU 配额和同窗调度指标。", rationale="确认高占用是否为预期负载。"),
            RootCauseRecommendation(recommendation_type="temporary_mitigation", action=f"在人工确认后限制或迁移 {target} 的 CPU 负载。", rationale="减少对目标服务调度时间的竞争。"),
            RootCauseRecommendation(recommendation_type="permanent_fix", action=f"为 {target} 配置资源配额和持续 CPU 基线。", rationale="防止同类噪声再次放大业务延迟。"),
        ]
    if mechanism == "process_suspended":
        return [
            RootCauseRecommendation(recommendation_type="investigation", action="追查直接控制进程的父进程、控制器、systemd/cgroup、发布和审计来源。", rationale="补齐谁发起暂停动作的来源边界。"),
            RootCauseRecommendation(recommendation_type="temporary_mitigation", action=f"人工确认业务状态后恢复 {target} 并观察请求是否恢复。", rationale="验证暂停动作与业务停滞的可逆关系。"),
            RootCauseRecommendation(recommendation_type="permanent_fix", action="保留运行控制审计并约束生产暂停权限。", rationale="避免无法追责的控制动作再次发生。"),
        ]
    return [RootCauseRecommendation(recommendation_type="investigation", action=f"围绕 {target} 补充同窗因果证据。", rationale="确认机制与用户症状之间的传播路径。")]


def _cause_level(assessment: dict[str, Any]) -> str:
    claim_type = str(assessment.get("claim_type") or "")
    if claim_type == "complete_root_cause":
        return "direct_root_cause"
    if claim_type in {"direct_root_cause", "complete_source_root_cause", "direct_failure_mechanism"}:
        return claim_type
    if claim_type in {"root_cause", "likely_root_cause"}:
        return "direct_root_cause"
    return "observation"


def _candidate_id(mechanism: str, target: str) -> str:
    if mechanism == "process_suspended":
        return "runtime_control_process_suspended"
    normalized = "".join(character if character.isalnum() else "_" for character in target).strip("_").lower()
    return f"{mechanism}_{normalized or 'target'}"


def _direct_runtime_control_for_target(
    observations: list[dict[str, Any]],
    service_id: str,
) -> dict[str, Any] | None:
    for observation in observations:
        target = observation.get("target") if isinstance(observation.get("target"), dict) else {}
        if str(target.get("service_id") or "") != service_id:
            continue
        payload = observation.get("runtime_control") if isinstance(observation.get("runtime_control"), dict) else {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        if not summary.get("has_direct_control_chain"):
            continue
        for event in payload.get("events", []):
            if not isinstance(event, dict):
                continue
            qualification = event.get("qualification") if isinstance(event.get("qualification"), dict) else {}
            if not qualification.get("direct_control_chain"):
                continue
            actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
            action = event.get("action") if isinstance(event.get("action"), dict) else {}
            provenance = event.get("source_provenance") if isinstance(event.get("source_provenance"), dict) else {}
            return {
                "actor": str(actor.get("comm") or actor.get("kind") or actor.get("pid") or "运行控制面"),
                "action": _runtime_action_label(action),
                "evidence_refs": _unique(observation.get("evidence_refs", [])),
                "complete_source_chain": bool(summary.get("has_complete_source_chain")),
                "origin_unknown": bool(summary.get("origin_unknown", True)),
                "source_provenance": provenance,
            }
    return None


def _runtime_action_label(action: dict[str, Any]) -> str:
    raw = str(action.get("signal") or action.get("operation") or "pause")
    return {
        "pause": "暂停",
        "freeze": "冻结",
        "cgroup.freeze": "cgroup 冻结",
        "SIGSTOP": "SIGSTOP 暂停信号",
        "SIGTSTP": "SIGTSTP 暂停信号",
    }.get(raw, raw)


def _cohort(observation: dict[str, Any]) -> str:
    target = observation.get("target") if isinstance(observation.get("target"), dict) else {}
    confidence = observation.get("confidence_inputs") if isinstance(observation.get("confidence_inputs"), dict) else {}
    return str(target.get("evidence_cohort_id") or confidence.get("evidence_cohort_id") or "unknown")


def _tree_candidate_ids_by_mechanism(tree: dict[str, Any] | None) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    if not isinstance(tree, dict):
        return output
    for layer in tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            for node in layer.get(group, []):
                if not isinstance(node, dict):
                    continue
                mechanism = str(node.get("mechanism") or "")
                candidate_id = str(node.get("candidate_id") or "")
                if mechanism and candidate_id:
                    output.setdefault(mechanism, []).append(candidate_id)
    return output


def _all_tree_nodes(tree: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(tree, dict):
        return []
    result = []
    for layer in tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            result.extend(
                item for item in layer.get(group, [])
                if isinstance(item, dict) and item.get("candidate_id")
            )
    return result


def _deepest_level(values: Iterable[str]) -> str:
    order = {
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
    return max(
        (str(value) for value in values if str(value) in order),
        key=lambda value: order[value],
        default="resource",
    )


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
