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
from server.app.rca.controlled_tree import qualify_ai_candidate


ELIGIBLE_CAUSE_LEVELS = {"direct_root_cause", "complete_source_root_cause"}


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
    return clusters


def derive_root_cause_clusters_from_ai_tree(
    session_tree: dict[str, Any] | None,
    *,
    valid_evidence_refs: set[str] | None = None,
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
    clusters: list[RootCauseCluster] = []
    for node in nodes:
        if node.get("generated_by") not in {"ai_candidate", "ai_guarded"}:
            continue
        refs = _unique(node.get("evidence_refs", []))
        if valid_evidence_refs is not None and any(ref not in valid_evidence_refs for ref in refs):
            continue
        if not node.get("conclusion_eligible") or node.get("causal_status") != "supported" or node.get("decision") != "conclude":
            continue
        try:
            ai_node = AITreeCandidateNode.model_validate(node)
        except Exception:
            continue
        eligible, _ = qualify_ai_candidate(
            ai_node,
            valid_evidence_refs=valid_evidence_refs,
            known_candidate_ids=known_ids,
        )
        if not eligible:
            continue
        if any(parent_id not in known_ids for parent_id in ai_node.parent_candidate_ids):
            continue
        role = node.get("role")
        if role not in {"primary", "secondary"}:
            continue
        cluster_id = f"rc_cluster_ai_{node['candidate_id']}"
        clusters.append(RootCauseCluster(
            cluster_id=cluster_id,
            candidate_ids=[str(node["candidate_id"])],
            source_tree_candidate_ids=[str(node["candidate_id"])],
            role="primary" if role == "primary" else "contributing",
            causal_status="primary" if role == "primary" else "contributing",
            cause_level="complete_source_root_cause" if node.get("claim_type") == "complete_source_root_cause" else "direct_root_cause",
            mechanism=str(node.get("mechanism") or ""),
            target=str(node.get("target") or ""),
            claim=str(node.get("claim") or ""),
            why_it_happened=str((node.get("self_challenge") or {}).get("why_this_claim") or ""),
            causal_chain=[CausalExplanationStep(
                step_id=f"{cluster_id}_step_1",
                statement=str(node.get("claim") or ""),
                evidence_refs=refs,
            )],
            evidence_refs=refs,
            confidence=float(node.get("confidence") or 0.0),
            conclusion_eligible=True,
            qualification="confirmed_root_cause",
        ))
    return clusters


def collect_ai_gate_failures(
    session_tree: dict[str, Any] | None,
    *,
    valid_evidence_refs: set[str] | None = None,
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
        if node.get("generated_by") in {"ai_candidate", "ai_guarded"}
    ]
    known_ids = {str(node.get("candidate_id") or "") for node in all_nodes if node.get("candidate_id")}
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
        )
        missing_parents = [
            parent_id for parent_id in ai_node.parent_candidate_ids
            if parent_id not in known_ids
        ]
        if not eligible or missing_parents:
            evidence_refs = _unique(ai_node.evidence_refs)
            known_refs = set(valid_evidence_refs or set())
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
                "missing_initial_evidence_refs": sorted(
                    ref for ref in evidence_refs if ref not in known_refs
                )[:128],
                "parent_candidate_ids": list(ai_node.parent_candidate_ids),
                "missing_parent_candidate_ids": missing_parents,
            })
    return failures


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
    return RetainedConclusion(
        candidate_id=candidate_id,
        claim=claim,
        supported_level=level,
        status=status,
        qualification=qualification,
        evidence_refs=evidence_refs,
        confidence=max(0.0, min(1.0, confidence)),
        source_candidate_id=source_id,
        inherited=inherited,
        fallback_mode="inherit_parent" if inherited else "none",
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
) -> dict[str, Any]:
    eligible = [cluster for cluster in clusters if cluster.conclusion_eligible]
    primary = eligible[0] if eligible else None
    possible = next(
        (
            cluster for cluster in clusters
            if cluster.qualification in {"possible_root_cause", "partial_localization"}
            and str(cluster.cause_level or "") not in {"observation", "call_path"}
        ),
        None,
    )
    if primary:
        primary.role = "primary"
        primary.causal_status = "primary"
    for cluster in eligible[1:]:
        if cluster.causal_status == "contributing":
            cluster.role = "contributing"
            cluster.relation_to_primary = "目标调度受压证据表明该原因会放大主因造成的用户症状。"
        else:
            cluster.role = "independent"
            cluster.relation_to_primary = "该异常与主因同窗独立成立，但现有证据未证明它影响目标服务。"
    if primary:
        headline = primary.claim
        why = primary.why_it_happened
    retained = build_retained_conclusion(
        clusters,
        assessment,
        session_tree,
        previous_retained=previous_retained,
        inherited=True,
    )
    if primary:
        retained = retained or build_retained_conclusion(clusters, assessment, session_tree)
    # For observation-only assessments, the latest structured anchor is the
    # useful retained explanation. A stale tree candidate may still contain
    # the earlier generic process-level wording, so do not let it overwrite
    # the more specific assessment claim.
    assessment_claim = str(assessment.get("diagnostic_claim") or "").strip()
    if not primary and assessment.get("claim_type") == "observation_only" and assessment_claim:
        headline = assessment_claim
    else:
        headline = str(
            (retained or {}).get("claim")
            or assessment_claim
            or "当前没有可继承的证据支持结论。"
        )
    if primary:
        why = primary.why_it_happened
    elif possible:
        why = possible.why_it_happened
    else:
        why = str(assessment.get("eligibility_reason") or "当前只有观察事实，尚未建立可引用证据支持的因果机制。")
    residual = _unique(
        item
        for cluster in clusters
        for item in cluster.residual_unknowns
    )
    classification = classify_cluster_set(clusters)
    if classification == "insufficient_evidence" and assessment.get("classification"):
        classification = str(assessment["classification"])
    formal_chain = [step for cluster in eligible for step in cluster.causal_chain]
    localization_chain = _build_localization_chain(session_tree, retained)
    return {
        "headline": headline,
        "why_it_happened": why,
        "causal_chain": formal_chain,
        "localization_chain": localization_chain,
        "root_cause_clusters": clusters,
        "ruled_out_summary": [
            str(item.get("reason"))
            for item in assessment.get("ruled_out", [])
            if isinstance(item, dict) and item.get("reason")
        ],
        "residual_unknowns": residual,
        "recommendations": [recommendation for cluster in eligible for recommendation in cluster.recommendations],
        "classification": classification,
        "ai_review_status": status,
        "ai_review_scope": "session",
        "ai_review_attempts": attempts,
        "ai_review_model": model,
        "ai_review_error": error[:500],
        "confidence_level": "高" if primary else "中" if retained else "不可判断",
        "abstained": not bool(primary),
        "retained_conclusion": retained,
        "formal_root_cause": _formal_root_cause(clusters),
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
    return [
        CausalExplanationStep(
            step_id=f"localization_{node['candidate_id']}",
            statement=str(node.get("claim") or ""),
            evidence_refs=_unique(node.get("evidence_refs") or []),
        )
        for node in ordered
        if str(node.get("claim") or "").strip()
    ]


def apply_session_review(
    clusters: list[RootCauseCluster],
    review: SessionConclusionReview | dict[str, Any],
    *,
    attempts: int = 1,
    model: str = "",
    retained_conclusion: dict[str, Any] | None = None,
    qualification_boundary: dict[str, Any] | None = None,
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
) -> list[str]:
    issues: list[str] = []
    by_id = {cluster.cluster_id: cluster for cluster in clusters if cluster.conclusion_eligible}
    role_ids = set(review.cluster_roles)
    if review.primary_cluster_id not in by_id:
        issues.append("primary_cluster_id does not reference an eligible cluster")
    if role_ids - set(by_id):
        issues.append("cluster_roles contains unknown or ineligible cluster IDs")
    if review.cluster_roles.get(str(review.primary_cluster_id)) != "primary":
        issues.append("primary cluster must have role=primary")
    if sum(role == "primary" for role in review.cluster_roles.values()) != 1:
        issues.append("exactly one primary cluster is required")
    for cluster_id, role in review.cluster_roles.items():
        cluster = by_id.get(cluster_id)
        if cluster is None:
            continue
        if role == "contributing" and cluster.causal_status != "contributing":
            issues.append(f"cluster {cluster_id} lacks target-impact evidence required for role=contributing")
    selected_primary = by_id.get(str(review.primary_cluster_id))
    if selected_primary and selected_primary.causal_status == "independent" and any(
        cluster.causal_status != "independent" for cluster in by_id.values() if cluster.cluster_id != selected_primary.cluster_id
    ):
        issues.append("an independent anomaly cannot replace an evidence-backed causal primary cluster")
    allowed_refs = valid_evidence_refs | {ref for cluster in clusters for ref in cluster.evidence_refs}
    for step in review.causal_chain:
        if not step.evidence_refs:
            issues.append(f"causal step {step.step_id} has no evidence refs")
        elif set(step.evidence_refs) - allowed_refs:
            issues.append(f"causal step {step.step_id} contains unknown evidence refs")
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
