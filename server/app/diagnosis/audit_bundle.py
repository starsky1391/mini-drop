"""AI Ops benchmark audit bundle export helpers."""

from __future__ import annotations

from typing import Any


def build_audit_bundle(diagnosis_id: str, orchestrator, repo) -> dict[str, Any] | None:
    detail = orchestrator.store.get_detail(diagnosis_id)
    if detail is None:
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
            "structured_evidence": _structured_evidence(evidence),
            "conclusion": conclusion,
        }
    )

    return {
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
        "structured_evidence": _structured_evidence(evidence),
        "evidence_refs": _evidence_refs(evidence, latest),
        "conclusion": conclusion,
        "latest_conclusion": latest,
        "safety": _safety_section(detail),
        "rollback": _rollback_section(detail),
        "readiness_gate": readiness,
    }


def build_readiness_gate(bundle: dict[str, Any]) -> dict[str, Any]:
    checks = [
        _check("audit_bundle_exists", True, "audit bundle was generated"),
        _check("runtime_trace_non_empty", bool(bundle.get("runtime_trace")), "runtime trace should not be empty"),
        _check("probes_non_empty", bool(bundle.get("probes")), "diagnosis should plan probes"),
        _check("child_task_ids_non_empty", bool(bundle.get("child_task_ids")), "diagnosis should create child tasks"),
        _check("artifact_count_non_zero", bool(bundle.get("artifacts")), "child tasks should upload artifacts"),
        _check("structured_evidence_non_empty", bool(bundle.get("structured_evidence")), "structured evidence should exist"),
        _check("evidence_refs_non_empty", bool(bundle.get("evidence_refs")), "conclusion should cite evidence refs"),
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
    ]
    status = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    return {
        "status": status,
        "checks": checks,
        "summary": f"{sum(item['status'] == 'PASS' for item in checks)}/{len(checks)} checks passed",
    }


def _check(name: str, passed: bool, message: str) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "message": message}


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
    primary = candidates[0] if candidates else {}
    return {
        "summary": latest.get("summary", ""),
        "confidence_level": latest.get("confidence_level", "不可判断"),
        "location_type": assessment.get("location_type") or primary.get("location_type"),
        "domain_type": assessment.get("domain_type") or primary.get("domain_type"),
        "classification": assessment.get("classification") or primary.get("classification"),
        "root_entity": assessment.get("root_entity") or primary.get("root_entity"),
        "abstained": not bool(candidates) or latest.get("confidence_level") == "不可判断",
        "root_cause_candidates": candidates,
        "supporting_evidence_refs": assessment.get("evidence_refs") or primary.get("evidence_refs", []),
        "missing_evidence": latest.get("missing_evidence") or assessment.get("missing_evidence", []),
        "diagnostic_commands": latest.get("diagnostic_commands", []),
    }


def _structured_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    items = [
        item for item in evidence
        if item.get("query_or_probe") == "structured_evidence_json"
        or str(item.get("raw_artifact_ref") or "").endswith(":structured_evidence_json")
    ]
    if not items:
        return {}
    return items[-1].get("observed_value", {})


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
        "off_cpu_wait_profile",
        "baseline_window_profile",
        "sys_metrics",
        "memory_smaps",
        "ebpf_io",
        "perf_cpu",
    }
    missing = (required & structured_required) - artifact_families
    return not missing


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
        "continuous_top_json": "baseline_window_profile",
        "continuous_flamegraph_json": "baseline_window_profile",
        "continuous_summary": "baseline_window_profile",
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
        "process_log_scan": "log_scan",
        "process_dependency_check": "dependency_check",
        "process_redis_check": "redis_check",
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
