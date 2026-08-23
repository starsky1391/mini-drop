"""AI Ops benchmark audit bundle export helpers."""

from __future__ import annotations

from typing import Any

from server.app.common_utils import json_safe


def build_audit_bundle(diagnosis_id: str, orchestrator, repo) -> dict[str, Any] | None:
    detail = orchestrator.store.get_detail(diagnosis_id)
    if detail is None:
        frozen = _frozen_watch_incident(repo, diagnosis_id)
        if frozen is not None:
            return frozen
        return None

    child_task_ids = list(detail.get("child_task_ids", []))
    tasks = [
        _task_to_dict(repo.tasks[task_id])
        for task_id in child_task_ids
        if task_id in repo.tasks
    ]
    artifacts = []
    for task_id in child_task_ids:
        for artifact in repo.artifacts.get(task_id, []):
            artifacts.append({"task_id": task_id, **artifact})

    evidence = detail.get("evidence", [])
    latest = detail.get("latest_conclusion") or {}
    probes = detail.get("probes", [])
    trace = _runtime_trace(detail)
    conclusion = _normalize_conclusion(latest)
    evidence_refs = _evidence_refs(evidence, latest)
    structured_evidence = _structured_evidence(evidence)
    readiness = build_readiness_gate(
        {
            "diagnosis_id": diagnosis_id,
            "run": _run_section(detail),
            "runtime_trace": trace,
            "probes": probes,
            "child_task_ids": child_task_ids,
            "tasks": tasks,
            "artifacts": artifacts,
            "evidence": evidence,
            "structured_evidence": structured_evidence,
            "evidence_refs": evidence_refs,
            "conclusion": conclusion,
            "latest_conclusion": latest,
        }
    )

    return json_safe({
        "schema_version": "1.0",
        "diagnosis_id": diagnosis_id,
        "run": _run_section(detail),
        "trace": trace,
        "runtime_trace": trace,
        "topology_snapshot": detail.get("topology_snapshot"),
        "probes": probes,
        "child_task_ids": child_task_ids,
        "tasks": tasks,
        "artifacts": artifacts,
        "evidence": evidence,
        "structured_evidence": structured_evidence,
        "evidence_refs": evidence_refs,
        "conclusion": conclusion,
        "latest_conclusion": latest,
        "safety": _safety_section(detail),
        "rollback": _rollback_section(detail),
        "readiness_gate": readiness,
    })


def _frozen_watch_incident(repo, diagnosis_id: str) -> dict[str, Any] | None:
    """Adapt a persisted frozen WatchIncident to the existing audit shape."""
    if not diagnosis_id.startswith("analysis_") or not hasattr(repo, "get_watch_incident"):
        return None
    incident = repo.get_watch_incident(diagnosis_id.removeprefix("analysis_"))
    if not incident or incident.get("analysis_session_id") != diagnosis_id:
        return None

    result = incident.get("analysis_result") or {}
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    structured = incident.get("structured_evidence") or result.get("structured_evidence") or {}
    reused_refs = list(dict.fromkeys(
        item
        for item in (
            result.get("reused_evidence_refs")
            or [
                ref.get("evidence_ref")
                for ref in incident.get("snapshot_refs", [])
                if isinstance(ref, dict) and ref.get("evidence_ref")
            ]
        )
        if item
    ))
    missing = list(dict.fromkeys(
        item
        for item in (result.get("missing_evidence") or report.get("missing_evidence") or [])
        if item
    ))
    boundary = (
        result.get("qualification_boundary")
        or result.get("conclusion_boundary")
        or report.get("conclusion_boundary")
    )
    evidence_refs = list(dict.fromkeys([
        *reused_refs,
        *[
            item.get("evidence_ref")
            for item in structured.get("artifact_refs", [])
            if isinstance(item, dict) and item.get("evidence_ref")
        ],
    ]))
    trace = [{
        "schema_version": "1.0",
        "diagnosis_id": diagnosis_id,
        "sequence": 1,
        "stage": "evidence",
        "component": "mini_drop.watch_runtime",
        "decision": "frozen_evidence_reused",
        "summary": "复用 WatchIncident 已保存的同窗冻结证据",
        "input_refs": evidence_refs,
        "output_refs": evidence_refs,
        "evidence_refs": evidence_refs,
        "alternatives": [],
        "details": {
            "incident_id": incident.get("incident_id"),
            "evidence_package_id": incident.get("evidence_cohort_id"),
            "evidence_cohort_id": incident.get("evidence_cohort_id"),
            "collection_mode": structured.get("collection_mode") or "rolling_snapshot",
            "timing_relation": structured.get("timing_relation") or "same_window",
            "probe_count": result.get("probe_count", 0),
            "missing_evidence": missing,
            "stop_reason": result.get("stop_reason", ""),
        },
        "recorded_at": result.get("analysis_completed_at") or incident.get("created_at"),
        "reconstructed": True,
    }]
    latest = report or result
    normalized = _normalize_conclusion(latest) if isinstance(latest, dict) else {}
    normalized.update({
        "diagnosis_mode": "frozen_evidence",
        "evidence_package_id": incident.get("evidence_cohort_id"),
        "evidence_cohort_id": incident.get("evidence_cohort_id"),
        "reused_evidence_refs": reused_refs,
        "missing_evidence": missing,
        "qualification_boundary": boundary,
        "stop_reason": result.get("stop_reason", ""),
        "probe_count": result.get("probe_count", 0),
    })
    bundle = {
        "schema_version": "1.0",
        "diagnosis_id": diagnosis_id,
        "diagnosis_mode": "frozen_evidence",
        "evidence_package_id": incident.get("evidence_cohort_id"),
        "evidence_cohort_id": incident.get("evidence_cohort_id"),
        "source_incident_id": incident.get("incident_id"),
        "run": {
            "status": incident.get("analysis_status"),
            "created_at": incident.get("created_at"),
            "updated_at": incident.get("created_at"),
            "model_version": result.get("model"),
            "planner_version": None,
            "policy_profile": None,
            "normalized_intent": {},
            "target_scope": {
                "diagnosis_mode": "frozen_evidence",
                "evidence_package_id": incident.get("evidence_cohort_id"),
                "source_evidence_cohort_id": incident.get("evidence_cohort_id"),
                "source_incident_id": incident.get("incident_id"),
            },
            "budget_used": {"probe_count": result.get("probe_count", 0)},
        },
        "trace": trace,
        "runtime_trace": trace,
        "topology_snapshot": None,
        "probes": [],
        "child_task_ids": [],
        "tasks": [],
        "artifacts": [],
        "evidence": [],
        "structured_evidence": structured,
        "evidence_refs": evidence_refs,
        "conclusion": normalized,
        "latest_conclusion": {
            **(report if isinstance(report, dict) else {}),
            **normalized,
        },
        "safety": {
            "forbidden_action_executed": False,
            "unsafe_action_count": 0,
            "commands": [],
        },
        "rollback": {"required": False, "attempted": False, "succeeded": None},
        "watch_incident": {
            "incident_id": incident.get("incident_id"),
            "watch_id": incident.get("watch_id"),
            "trigger_event_id": incident.get("trigger_event_id"),
            "snapshot_id": incident.get("snapshot_id"),
            "snapshot_refs": incident.get("snapshot_refs", []),
            "collector_tasks": incident.get("collector_tasks", []),
        },
        "reused_evidence_refs": reused_refs,
        "missing_evidence": missing,
        "qualification_boundary": boundary,
        "stop_reason": result.get("stop_reason", ""),
        "probe_count": result.get("probe_count", 0),
    }
    bundle["readiness_gate"] = build_readiness_gate(bundle)
    return json_safe(bundle)


def build_readiness_gate(bundle: dict[str, Any]) -> dict[str, Any]:
    if bundle.get("diagnosis_mode") == "frozen_evidence":
        return _build_frozen_readiness_gate(bundle)
    checks = [
        _check("audit_bundle_exists", True, "audit bundle was generated"),
        _check("runtime_trace_non_empty", bool(bundle.get("runtime_trace")), "runtime trace should not be empty"),
        _check("probes_non_empty", bool(bundle.get("probes")), "diagnosis should plan probes"),
        _check("child_task_ids_non_empty", bool(bundle.get("child_task_ids")), "diagnosis should create child tasks"),
        _check("artifact_count_non_zero", bool(bundle.get("artifacts")), "child tasks should upload artifacts"),
        _check("structured_evidence_non_empty", bool(bundle.get("structured_evidence")), "structured evidence should exist"),
        _check("evidence_refs_non_empty", bool(bundle.get("evidence_refs")), "conclusion should cite evidence refs"),
        _check(
            "controlled_ai_tree_present",
            bool(_bundle_controlled_ai_tree(bundle)),
            "AI Ops v2 scoring requires the controlled AI tree path, not only legacy task RCA",
        ),
        _check(
            "controlled_ai_tree_claim_lineage",
            _controlled_ai_tree_has_claim_lineage(bundle),
            "every claim-bearing session_main node should include canonical claim lineage",
        ),
        _check(
            "session_ai_review_succeeded",
            _session_ai_review_succeeded(bundle),
            "final readiness requires one valid session-scoped AI adjudication",
        ),
        _check(
            "compound_clusters_evidence_backed",
            _compound_clusters_are_ready(bundle),
            "compound incidents require at least two eligible clusters with independently attributable evidence",
        ),
        _check(
            "collector_type_not_fallback",
            _collector_mapping_is_explicit(bundle),
            "collector types should match planned probes without silent perf_cpu fallback",
        ),
        _check(
            "required_collector_family_has_structured_artifact",
            _required_collector_families_have_structured_artifacts(bundle),
            "planned evidence families should have matching structured artifacts before scoring",
        ),
        _check(
            "runtime_stack_quality_non_empty",
            _runtime_stack_quality_non_empty(bundle),
            "runtime/deep collectors should produce non-empty stack, hotspot, or wait evidence",
        ),
        _check(
            "completed_probe_evidence_valid",
            _completed_probes_have_valid_evidence(bundle),
            "completed probes must expose valid or partial structured evidence, not only a DONE task state",
        ),
    ]
    status = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    return {
        "status": status,
        "checks": checks,
        "summary": f"{sum(item['status'] == 'PASS' for item in checks)}/{len(checks)} checks passed",
    }


def _build_frozen_readiness_gate(bundle: dict[str, Any]) -> dict[str, Any]:
    structured = bundle.get("structured_evidence")
    checks = [
        _check("audit_bundle_exists", True, "audit bundle was generated"),
        _check(
            "frozen_mode_present",
            bundle.get("diagnosis_mode") == "frozen_evidence",
            "frozen analysis mode should be explicit",
        ),
        _check(
            "evidence_package_identity_present",
            bool(bundle.get("evidence_package_id") and bundle.get("evidence_cohort_id")),
            "frozen evidence package and cohort identity should be preserved",
        ),
        _check(
            "same_window_evidence_present",
            isinstance(structured, dict)
            and structured.get("collection_mode") == "rolling_snapshot"
            and structured.get("timing_relation") == "same_window",
            "frozen analysis should retain the same-window rolling snapshot",
        ),
        _check(
            "structured_evidence_non_empty",
            bool(structured),
            "saved structured evidence should exist",
        ),
        _check(
            "evidence_refs_recorded",
            bool(bundle.get("evidence_refs") or bundle.get("reused_evidence_refs")),
            "frozen analysis should preserve reusable evidence references",
        ),
        _check(
            "missing_evidence_recorded",
            "missing_evidence" in bundle,
            "package gaps should be explicit even when the package is complete",
        ),
        _check(
            "stop_reason_recorded",
            "stop_reason" in bundle,
            "frozen termination reason should be explicit",
        ),
        _check(
            "probe_count_zero",
            bundle.get("probe_count") == 0 and not bundle.get("probes"),
            "frozen analysis must not create diagnosis probes",
        ),
    ]
    status = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    return {
        "status": status,
        "checks": checks,
        "summary": f"{sum(item['status'] == 'PASS' for item in checks)}/{len(checks)} checks passed",
    }


def _check(name: str, passed: bool, message: str) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "message": message}


def _controlled_ai_tree_has_claim_lineage(bundle: dict[str, Any]) -> bool:
    latest = bundle.get("latest_conclusion") or bundle.get("conclusion") or {}
    if not isinstance(latest, dict):
        return False
    tree = _bundle_controlled_ai_tree(bundle)
    if not isinstance(tree, dict):
        return False
    nodes = [
        node
        for layer in tree.get("layers") or []
        if isinstance(layer, dict)
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes")
        for node in layer.get(group) or []
        if isinstance(node, dict) and (node.get("claim") or node.get("boundary_message"))
    ]
    return bool(nodes) and all(
        node.get("claim_origin")
        and node.get("claim_transform")
        and node.get("claim_status")
        and (node.get("claim_hash") or node.get("claim_status") == "boundary")
        for node in nodes
    )


def _session_ai_review_succeeded(bundle: dict[str, Any]) -> bool:
    latest = bundle.get("latest_conclusion") or bundle.get("conclusion") or {}
    return bool(
        isinstance(latest, dict)
        and latest.get("ai_review_status") == "succeeded"
        and latest.get("ai_review_scope") == "session"
        and int(latest.get("ai_review_attempts") or 0) > 0
    )


def _compound_clusters_are_ready(bundle: dict[str, Any]) -> bool:
    latest = bundle.get("latest_conclusion") or bundle.get("conclusion") or {}
    if not isinstance(latest, dict):
        return False
    if not _compound_evidence_present(bundle, latest):
        return True
    clusters = [
        item for item in latest.get("root_cause_clusters", [])
        if isinstance(item, dict) and item.get("conclusion_eligible")
    ]
    if len({(item.get("mechanism"), item.get("target")) for item in clusters}) < 2:
        return False
    ref_sets = [set(item.get("evidence_refs") or []) for item in clusters]
    if any(not refs for refs in ref_sets):
        return False
    return all(
        refs - set().union(*(other for other_index, other in enumerate(ref_sets) if other_index != index))
        for index, refs in enumerate(ref_sets)
    )


def _compound_evidence_present(bundle: dict[str, Any], latest: dict[str, Any]) -> bool:
    eligible = {
        (item.get("mechanism"), item.get("target"))
        for item in latest.get("root_cause_clusters", [])
        if isinstance(item, dict) and item.get("conclusion_eligible")
    }
    if len(eligible) >= 2:
        return True
    has_cpu_cluster = False
    has_dependency_failure = False
    for artifact in bundle.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        artifact_type = str(artifact.get("artifact_type") or "")
        if artifact_type == "sys_metrics":
            workload_cpu = float(metadata.get("avg_cpu_user_pct") or 0) + float(metadata.get("avg_cpu_sys_pct") or 0)
            host_busy = float(metadata.get("avg_host_cpu_busy_pct") or 0)
            has_cpu_cluster = has_cpu_cluster or (workload_cpu >= 70 and host_busy >= 85)
        if artifact_type in {"dependency_check_json", "redis_check_json"}:
            failed = metadata.get("failed_dependencies")
            has_dependency_failure = has_dependency_failure or bool(failed) or metadata.get("ping_ok") is False
    return has_cpu_cluster and has_dependency_failure


def _bundle_controlled_ai_tree(bundle: dict[str, Any]) -> dict[str, Any] | None:
    latest = bundle.get("latest_conclusion") or {}
    tree = latest.get("controlled_ai_tree") if isinstance(latest, dict) else None
    if isinstance(tree, dict):
        return tree
    conclusion = bundle.get("conclusion") or {}
    tree = conclusion.get("controlled_ai_tree") if isinstance(conclusion, dict) else None
    return tree if isinstance(tree, dict) else None


def _run_section(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": detail.get("status"),
        "created_at": detail.get("created_at"),
        "updated_at": detail.get("updated_at"),
        "model_version": detail.get("model_version"),
        "planner_version": detail.get("planner_version"),
        "policy_profile": detail.get("policy_profile"),
        "normalized_intent": detail.get("normalized_intent", {}),
        "target_scope": detail.get("target_scope", {}),
        "budget_used": detail.get("budget_used", {}),
    }


def _runtime_trace(detail: dict[str, Any]) -> list[dict[str, Any]]:
    events = detail.get("events", [])
    result = []
    for index, event in enumerate(events, 1):
        event_type = str(event.get("event_type", ""))
        result.append({
            "schema_version": "1.0",
            "diagnosis_id": detail.get("diagnosis_id"),
            "sequence": index,
            "stage": _stage_from_event(event_type),
            "component": "mini_drop.diagnosis_orchestrator",
            "decision": event_type,
            "summary": event_type.replace("_", " "),
            "input_refs": [],
            "output_refs": [],
            "evidence_refs": _payload_refs(event.get("payload", {})),
            "alternatives": [],
            "details": event.get("payload", {}),
            "recorded_at": event.get("created_at"),
            "reconstructed": False,
        })
    return result


def _stage_from_event(event_type: str) -> str:
    if "intent" in event_type:
        return "intent"
    if "scope" in event_type:
        return "scope"
    if "plan" in event_type or "probe" in event_type or "approval" in event_type:
        return "probe_plan"
    if "evidence" in event_type or "analysis" in event_type:
        return "evidence"
    if "conclusion" in event_type or "completed" in event_type:
        return "conclusion"
    return "workflow"


def _payload_refs(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    refs = []
    for key in ("evidence_refs", "step_ids", "child_task_ids"):
        value = payload.get(key)
        if isinstance(value, list):
            refs.extend(str(item) for item in value)
    return refs


def _normalize_conclusion(latest: dict[str, Any]) -> dict[str, Any]:
    assessment = latest.get("cluster_assessment") or {}
    candidates = latest.get("root_cause_candidates") or []
    possible = latest.get("possible_root_causes") or []
    primary = candidates[0] if candidates else {}
    abstained = bool(latest.get("abstained", not bool(candidates)))
    confidence_level = latest.get("confidence_level", "不可判断")
    if abstained and confidence_level == "高":
        confidence_level = "低" if possible else "不可判断"
    return {
        "summary": latest.get("summary", ""),
        "headline": latest.get("headline", ""),
        "why_it_happened": latest.get("why_it_happened", ""),
        "causal_chain": latest.get("causal_chain", []),
        "root_cause_clusters": latest.get("root_cause_clusters", []),
        "ruled_out_summary": latest.get("ruled_out_summary", []),
        "residual_unknowns": latest.get("residual_unknowns", []),
        "recommendations": latest.get("recommendations", []),
        "ai_review_status": latest.get("ai_review_status", "fallback"),
        "ai_review_scope": latest.get("ai_review_scope", "session"),
        "ai_review_attempts": latest.get("ai_review_attempts", 0),
        "ai_review_model": latest.get("ai_review_model", ""),
        "ai_review_error": latest.get("ai_review_error", ""),
        "candidate_review": latest.get("candidate_review"),
        "candidate_validation_diagnostics": latest.get("candidate_validation_diagnostics", []),
        "ai_gate_failures": latest.get("ai_gate_failures", []),
        "gate_failures": latest.get("gate_failures", latest.get("ai_gate_failures", [])),
        "candidate_generation_output": latest.get("candidate_generation_output", {}),
        "observations": latest.get("observations", []),
        "boundaries": latest.get("boundaries", []),
        "retained_parent_conclusions": latest.get("retained_parent_conclusions", []),
        "canonical_candidate_state": (latest.get("controlled_ai_tree") or {}).get("data_quality", {}),
        "canonical_probe_plan": (latest.get("controlled_ai_tree") or {}).get("canonical_probe_plan", []),
        "probe_conflicts": (latest.get("controlled_ai_tree") or {}).get("probe_conflicts", []),
        "candidate_sources": sorted({
            str(node.get("generated_by"))
            for layer in (latest.get("controlled_ai_tree") or {}).get("layers", [])
            if isinstance(layer, dict)
            for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes")
            for node in (layer.get(group) or [])
            if isinstance(node, dict) and node.get("generated_by")
        }),
        "confidence_level": confidence_level,
        "location_type": assessment.get("location_type") or primary.get("location_type"),
        "domain_type": assessment.get("domain_type") or primary.get("domain_type"),
        "classification": assessment.get("classification") or primary.get("classification"),
        "root_entity": assessment.get("root_entity") or primary.get("root_entity"),
        "abstained": abstained,
        "root_cause_candidates": candidates,
        "possible_root_causes": possible,
        "supporting_evidence_refs": assessment.get("evidence_refs") or primary.get("evidence_refs", []),
        "missing_evidence": latest.get("missing_evidence") or assessment.get("missing_evidence", []),
        "diagnostic_commands": latest.get("diagnostic_commands", []),
        "controlled_ai_tree": latest.get("controlled_ai_tree"),
    }


def _structured_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    items = _structured_evidence_items(evidence)
    if not items:
        return {}
    summaries = [
        item.get("observed_value", {}).get("summary", {})
        for item in items
        if isinstance(item.get("observed_value"), dict)
        and isinstance(item["observed_value"].get("summary"), dict)
    ]
    if not summaries:
        return {}

    artifact_refs = _unique_items(
        item
        for summary in summaries
        for item in _as_list(summary.get("artifact_refs"))
        if isinstance(item, dict)
    )
    top_functions = _unique_items(
        item
        for summary in summaries
        for item in _as_list(summary.get("top_functions"))
        if isinstance(item, dict)
    )
    call_path_hotspots = _unique_items(
        item
        for summary in summaries
        for item in _as_list(summary.get("call_path_hotspots"))
        if isinstance(item, dict)
    )
    stack_summaries = [
        summary.get("stack_summary")
        for summary in summaries
        if isinstance(summary.get("stack_summary"), dict)
    ]
    stack_summary = max(stack_summaries, key=_stack_signal_score, default={})

    confidence_inputs = _merge_confidence_inputs(summaries)
    evidence_index = {
        "artifact_refs": artifact_refs,
        "stack_summary": stack_summary,
        "call_path_hotspots": call_path_hotspots,
        "confidence_inputs": confidence_inputs,
        "task_evidence": [
            {
                "task_id": str(summary.get("task_id") or ""),
                "collector_families": list(
                    (summary.get("confidence_inputs") or {}).get("collector_families") or []
                ),
                "evidence_refs": [
                    str(item.get("evidence_ref") or "")
                    for item in _as_list(summary.get("artifact_refs"))
                    if isinstance(item, dict) and item.get("evidence_ref")
                ],
            }
            for summary in summaries
        ],
    }
    for field, index_key in (
        ("off_cpu_wait_json", "off_cpu_wait"),
        ("log_window_json", "log_scan"),
        ("dependency_check_json", "dependency_check"),
        ("redis_check_json", "redis_check"),
        ("trace_endpoint_profile_json", "trace_endpoint_profile"),
        ("python_lock_wait_profile_json", "python_lock_wait_profile"),
        ("python_exception_profile_json", "python_exception_profile"),
        ("python_queue_profile_json", "python_queue_profile"),
        ("python_pool_profile_json", "python_pool_profile"),
        ("python_retry_timeout_profile_json", "python_retry_timeout_profile"),
        ("python_cache_profile_json", "python_cache_profile"),
        ("python_input_profile_json", "python_input_profile"),
    ):
        selected = _strongest_signal_value(summaries, field)
        if selected:
            evidence_index[index_key] = selected
    scenario_gates = _merge_python_scenario_gates(confidence_inputs)
    if scenario_gates:
        evidence_index["python_scenario_gates"] = scenario_gates

    return {
        "version": 1,
        "scope": "diagnosis",
        "task_count": len(summaries),
        "artifact_refs": artifact_refs,
        "top_functions": top_functions,
        "stack_summary": stack_summary,
        "call_path_hotspots": call_path_hotspots,
        "confidence_inputs": confidence_inputs,
        "evidence_index": evidence_index,
        "sys_metrics": _strongest_signal_value(summaries, "sys_metrics"),
        "ebpf_metrics": _strongest_signal_value(summaries, "ebpf_metrics"),
        "memory_json": _strongest_signal_value(summaries, "memory_json"),
        "off_cpu_wait_json": _strongest_signal_value(summaries, "off_cpu_wait_json"),
        "log_window_json": _strongest_signal_value(summaries, "log_window_json"),
        "dependency_check_json": _strongest_signal_value(summaries, "dependency_check_json"),
        "redis_check_json": _strongest_signal_value(summaries, "redis_check_json"),
        "trace_endpoint_profile_json": _strongest_signal_value(
            summaries, "trace_endpoint_profile_json"
        ),
        "python_scenario_gates": scenario_gates,
        "python_lock_wait_profile_json": _strongest_signal_value(summaries, "python_lock_wait_profile_json"),
        "python_exception_profile_json": _strongest_signal_value(summaries, "python_exception_profile_json"),
        "python_queue_profile_json": _strongest_signal_value(summaries, "python_queue_profile_json"),
        "python_pool_profile_json": _strongest_signal_value(summaries, "python_pool_profile_json"),
        "python_retry_timeout_profile_json": _strongest_signal_value(summaries, "python_retry_timeout_profile_json"),
        "python_cache_profile_json": _strongest_signal_value(summaries, "python_cache_profile_json"),
        "python_input_profile_json": _strongest_signal_value(summaries, "python_input_profile_json"),
    }


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _unique_items(items) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        key = (
            str(item.get("evidence_ref") or ""),
            str(item.get("name") or item.get("function") or item.get("artifact_type") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _stack_signal_score(value: dict[str, Any]) -> tuple[int, int, float]:
    return (
        int(bool(value.get("has_wait_reason"))),
        _safe_int(value.get("stack_sample_count") or value.get("sample_count")),
        _safe_float(value.get("total_wait_ms")),
    )


def _merge_confidence_inputs(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    values = [
        summary.get("confidence_inputs")
        for summary in summaries
        if isinstance(summary.get("confidence_inputs"), dict)
    ]
    bool_fields = (
        "has_system_pressure",
        "has_wait_or_io_signal",
        "has_log_signal",
        "has_dependency_signal",
        "has_redis_signal",
        "has_complete_control_chain",
    )
    total_fields = (
        "failed_dependency_count",
        "log_error_cluster_count",
        "redis_slowlog_entry_count",
        "control_event_count",
    )
    result = {
        field: any(bool(item.get(field)) for item in values)
        for field in bool_fields
    }
    result.update({
        field: sum(_number(item.get(field)) for item in values)
        for field in total_fields
    })
    evidence_validity = _merge_evidence_validity(values)
    result.update({
        "sample_count": sum(_number(item.get("sample_count")) for item in values),
        "dominant_percent": max((_number(item.get("dominant_percent")) for item in values), default=0.0),
        "redis_max_latency_ms": max(
            (_number(item.get("redis_max_latency_ms")) for item in values),
            default=0.0,
        ),
        "artifact_types": _unique_strings(
            item
            for value in values
            for item in _as_list(value.get("artifact_types"))
        ),
        "collector_families": _unique_strings(
            item
            for value in values
            for item in _as_list(value.get("collector_families"))
        ),
        "token_safety": "compact_summary_only",
        "context_completeness": _best_context_completeness(values),
        "parse_status": _best_parse_status(values),
        "trace_source_status": _best_status(values, "trace_source_status"),
        "stack_source_status": _best_status(values, "stack_source_status"),
        "trace_correlation_status": _best_status(values, "trace_correlation_status"),
        "trace_max_supported_level": _best_localization_level(values),
        "runtime_control_evidence_status": (
            evidence_validity.get("runtime_control_history")
            or _best_status(values, "runtime_control_evidence_status")
        ),
        "evidence_validity_by_family": evidence_validity,
        "python_scenario_statuses": _merge_python_scenario_statuses(values),
        "python_scenario_gates": _merge_python_scenario_gates_from_values(values),
    })
    return result


def _merge_python_scenario_statuses(values: list[dict[str, Any]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    order = {"": 0, "blocked": 1, "unparseable": 1, "empty_window": 2, "partial": 3, "valid": 4}
    for value in values:
        statuses = value.get("python_scenario_statuses")
        if not isinstance(statuses, dict):
            continue
        for family, status in statuses.items():
            text = str(status or "")
            if order.get(text, 0) >= order.get(merged.get(str(family), ""), 0):
                merged[str(family)] = text
    return merged


def _merge_python_scenario_gates_from_values(values: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    order = {"": 0, "blocked": 1, "unparseable": 1, "empty_window": 2, "partial": 3, "valid": 4}
    for value in values:
        gates = value.get("python_scenario_gates")
        if not isinstance(gates, dict):
            continue
        for family, gate in gates.items():
            if not isinstance(gate, dict):
                continue
            current = merged.get(str(family))
            if not isinstance(current, dict) or order.get(str(gate.get("evidence_status") or ""), 0) >= order.get(str(current.get("evidence_status") or ""), 0):
                merged[str(family)] = gate
    return merged


def _merge_python_scenario_gates(confidence_inputs: dict[str, Any]) -> dict[str, Any]:
    gates = confidence_inputs.get("python_scenario_gates")
    return gates if isinstance(gates, dict) else {}


def _merge_evidence_validity(values: list[dict[str, Any]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    order = {"": 0, "blocked": 1, "unparseable": 1, "empty_window": 2, "partial": 3, "valid": 4}
    for value in values:
        current = value.get("evidence_validity_by_family")
        if not isinstance(current, dict):
            continue
        for family, status in current.items():
            text = str(status or "")
            if order.get(text, 0) >= order.get(merged.get(family, ""), 0):
                merged[str(family)] = text
    return merged


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _unique_strings(values) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def _best_context_completeness(values: list[dict[str, Any]]) -> str:
    order = {"low": 0, "medium": 1, "high": 2}
    return max(
        (str(value.get("context_completeness") or "low") for value in values),
        key=lambda item: order.get(item, 0),
        default="low",
    )


def _best_parse_status(values: list[dict[str, Any]]) -> str:
    statuses = [str(value.get("parse_status") or "") for value in values]
    return next(
        (status for status in statuses if status and status != "insufficient_structured_signal"),
        "insufficient_structured_signal",
    )


def _best_status(values: list[dict[str, Any]], field: str) -> str:
    order = {"completed": 4, "partial": 3, "empty_window": 2, "unavailable": 1, "blocked": 1}
    return max(
        (str(value.get(field) or "") for value in values),
        key=lambda item: order.get(item, 0),
        default="",
    )


def _best_localization_level(values: list[dict[str, Any]]) -> str:
    order = {"": 0, "host": 1, "service": 2, "process": 3, "function": 4, "call_path": 5, "line": 6}
    return max(
        (str(value.get("trace_max_supported_level") or "") for value in values),
        key=lambda item: order.get(item, 0),
        default="",
    )


def _strongest_signal_value(summaries: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    values = [
        summary.get(field)
        for summary in summaries
        if isinstance(summary.get(field), dict) and summary.get(field)
    ]
    if not values:
        return None
    return max(values, key=lambda value: len(str(value)))


def _structured_evidence_items(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in evidence
        if item.get("query_or_probe") == "structured_evidence_json"
        or str(item.get("raw_artifact_ref") or "").endswith(":structured_evidence_json")
    ]


def _evidence_refs(evidence: list[dict[str, Any]], latest: dict[str, Any]) -> list[str]:
    refs = [item.get("evidence_id") for item in evidence if item.get("evidence_id")]
    assessment = latest.get("cluster_assessment") or {}
    refs.extend(assessment.get("evidence_refs") or [])
    for candidate in latest.get("root_cause_candidates") or []:
        refs.extend(candidate.get("evidence_refs") or [])
    return list(dict.fromkeys(str(ref) for ref in refs if ref))


def _safety_section(detail: dict[str, Any]) -> dict[str, Any]:
    conclusions = detail.get("conclusion_versions", [])
    commands = []
    for conclusion in conclusions:
        commands.extend(conclusion.get("diagnostic_commands") or [])
    return {
        "forbidden_action_executed": False,
        "unsafe_action_count": 0,
        "commands": commands,
    }


def _rollback_section(_detail: dict[str, Any]) -> dict[str, Any]:
    return {"required": False, "attempted": False, "succeeded": None}


def _collector_mapping_is_explicit(bundle: dict[str, Any]) -> bool:
    expected_by_task = {}
    for probe in bundle.get("probes", []):
        task_id = probe.get("task_id")
        if not task_id:
            continue
        expected_by_task[task_id] = _probe_to_collector(probe.get("probe_id", ""))
    for task in bundle.get("tasks", []):
        expected = expected_by_task.get(task.get("id"))
        if expected and task.get("collector_type") != expected:
            return False
    return True


def _required_collector_families_have_structured_artifacts(bundle: dict[str, Any]) -> bool:
    required = {
        _probe_to_collector(probe.get("probe_id", ""))
        for probe in bundle.get("probes", [])
        if probe.get("task_id") and probe.get("status") == "COMPLETED"
    }
    artifact_families = {
        _artifact_family(artifact)
        for artifact in bundle.get("artifacts", [])
        if artifact.get("artifact_type")
    }
    structured_required = {
        "log_scan",
        "dependency_check",
        "redis_check",
        "trace_endpoint_profile",
        "runtime_control_history",
        "off_cpu_wait_profile",
        "baseline_window_profile",
        "sys_metrics",
        "memory_smaps",
        "ebpf_io",
        "perf_cpu",
    }
    missing = (required & structured_required) - artifact_families
    return not missing


def _runtime_stack_quality_non_empty(bundle: dict[str, Any]) -> bool:
    runtime_families = {
        "perf_cpu",
        "pyspy",
        "off_cpu_wait_profile",
        "trace_endpoint_profile",
        "baseline_window_profile",
    }
    executed = {
        _probe_to_collector(probe.get("probe_id", ""))
        for probe in bundle.get("probes", [])
        if probe.get("task_id") and probe.get("status") == "COMPLETED"
    }
    if not (executed & runtime_families):
        return True
    runtime_probes = [
        probe for probe in bundle.get("probes", [])
        if _probe_to_collector(probe.get("probe_id", "")) in runtime_families
        and probe.get("task_id")
        and probe.get("status") == "COMPLETED"
    ]
    if runtime_probes and all(probe.get("evidence_status") == "empty_window" for probe in runtime_probes):
        return True
    return any(
        _structured_runtime_signal_present(item.get("observed_value"))
        for item in _structured_evidence_items(bundle.get("evidence", []))
    )


def _structured_runtime_signal_present(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    summary = value.get("summary") if isinstance(value.get("summary"), dict) else value
    if not isinstance(summary, dict):
        return False
    top_functions = summary.get("top_functions")
    if isinstance(top_functions, list) and top_functions:
        return True
    call_path_hotspots = summary.get("call_path_hotspots")
    if isinstance(call_path_hotspots, list) and call_path_hotspots:
        return True
    stack_summary = summary.get("stack_summary")
    if isinstance(stack_summary, dict):
        if _safe_int(stack_summary.get("stack_sample_count") or stack_summary.get("sample_count")) > 0:
            return True
        if stack_summary.get("has_wait_reason"):
            return True
    evidence_index = summary.get("evidence_index")
    if isinstance(evidence_index, dict) and _off_cpu_wait_signal_present(evidence_index.get("off_cpu_wait")):
        return True
    confidence = summary.get("confidence_inputs")
    return isinstance(confidence, dict) and bool(confidence.get("has_wait_or_io_signal"))


def _completed_probes_have_valid_evidence(bundle: dict[str, Any]) -> bool:
    completed = [probe for probe in bundle.get("probes", []) if probe.get("status") == "COMPLETED"]
    if not completed:
        return True
    return all(probe.get("evidence_status") in {"valid", "partial", "empty_window"} for probe in completed)


def _off_cpu_wait_signal_present(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
    if _safe_int(summary.get("sample_count")) > 0:
        return True
    stacks = value.get("top_wait_stacks")
    return isinstance(stacks, list) and bool(stacks)


def _artifact_family(artifact: dict[str, Any]) -> str:
    if artifact.get("collector_family"):
        return str(artifact["collector_family"])
    mapping = {
        "log_window_json": "log_scan",
        "dependency_check_json": "dependency_check",
        "redis_check_json": "redis_check",
        "sys_metrics": "sys_metrics",
        "memory_json": "memory_smaps",
        "ebpf_metrics": "ebpf_io",
        "top_json": "perf_cpu",
        "flamegraph_json": "perf_cpu",
        "depth_evidence_json": "trace_endpoint_profile",
        "off_cpu_wait_json": "off_cpu_wait_profile",
        "continuous_top_json": "baseline_window_profile",
        "continuous_flamegraph_json": "baseline_window_profile",
        "continuous_summary": "baseline_window_profile",
        "trace_endpoint_profile_json": "trace_endpoint_profile",
        "runtime_control_event_json": "runtime_control_history",
        "pyspy_status_json": "pyspy",
        "go_heap_profile_json": "go_pprof",
    }
    return mapping.get(str(artifact.get("artifact_type")), str(artifact.get("artifact_type")))


def _probe_to_collector(probe_id: str) -> str:
    mapping = {
        "host_process_metrics": "sys_metrics",
        "process_cpu_profile": "perf_cpu",
        "process_trace_endpoint_profile": "trace_endpoint_profile",
        "process_off_cpu_profile": "off_cpu_wait_profile",
        "process_io_latency": "ebpf_io",
        "process_memory_map": "memory_smaps",
        "process_baseline_window": "baseline_window_profile",
        "process_python_runtime_profile": "pyspy",
        "process_python_heap_profile": "python_heap_profile",
        "process_go_heap_profile": "go_pprof",
        "process_source_snapshot": "source_snapshot",
        "process_log_scan": "log_scan",
        "process_dependency_check": "dependency_check",
        "process_redis_check": "redis_check",
        "process_runtime_control_history": "runtime_control_history",
    }
    return mapping.get(probe_id, probe_id)


def _task_to_dict(task) -> dict[str, Any]:
    return {
        "id": task.id,
        "name": task.name,
        "agent_id": task.agent_id,
        "target_pid": task.target_pid,
        "collector_type": task.collector_type,
        "sample_rate": task.sample_rate,
        "duration_sec": task.duration_sec,
        "status": task.status.value if hasattr(task.status, "value") else task.status,
        "status_reason": task.status_reason,
        "request_params": task.request_params,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }
