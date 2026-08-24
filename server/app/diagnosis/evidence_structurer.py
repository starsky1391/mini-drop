"""Collector artifact to compact evidence structuring.

This layer is intentionally non-AI: it normalizes artifact outputs into
referenceable summaries, but it does not decide root cause.
"""

from __future__ import annotations

import html
import re
from typing import Any, Literal

from pydantic import BaseModel, Field


TOP_LIMIT = 10
HOTSPOT_LIMIT = 10
PYTHON_SCENARIO_INDUSTRIAL_SOURCES = {
    "bcc_offcputime",
    "skywalking_rover",
    "otel_profile",
    "py-spy",
    "memray",
    "fluent_bit",
    "otel_filelog",
    "otel_trace",
    "prometheus",
    "celery_inspect",
    "redis_exporter",
    "statsd",
    "application_runtime_log",
}
CollectionMode = Literal[
    "manual_single",
    "manual_group",
    "triggered_group",
    "rolling_snapshot",
    "delayed_followup",
    "baseline_window",
]
TimingRelation = Literal[
    "same_window",
    "pre_trigger_window",
    "post_trigger_window",
    "delayed_followup",
    "stale_window",
    "unknown",
]


class EvidenceWindowMetadata(BaseModel):
    trigger_event_id: str | None = None
    evidence_cohort_id: str | None = None
    collection_mode: CollectionMode = "manual_single"
    window_start: str | None = None
    window_end: str | None = None
    trigger_observed_at: str | None = None
    timing_relation: TimingRelation = "unknown"


class StructuredEvidence(BaseModel):
    version: int = 1
    task_id: str
    trigger_event_id: str | None = None
    evidence_cohort_id: str | None = None
    collection_mode: CollectionMode = "manual_single"
    window_start: str | None = None
    window_end: str | None = None
    trigger_observed_at: str | None = None
    timing_relation: TimingRelation = "unknown"
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    top_functions: list[dict[str, Any]] = Field(default_factory=list)
    stack_summary: dict[str, Any] = Field(default_factory=dict)
    call_path_hotspots: list[dict[str, Any]] = Field(default_factory=list)
    evidence_index: dict[str, Any] = Field(default_factory=dict)
    confidence_inputs: dict[str, Any] = Field(default_factory=dict)
    sys_metrics: dict[str, Any] | None = None
    ebpf_metrics: dict[str, Any] | None = None
    memory_json: dict[str, Any] | None = None
    off_cpu_wait_json: dict[str, Any] | None = None
    log_window_json: dict[str, Any] | None = None
    dependency_check_json: dict[str, Any] | None = None
    redis_check_json: dict[str, Any] | None = None
    trace_endpoint_profile_json: dict[str, Any] | None = None
    runtime_control_event_json: dict[str, Any] | None = None
    pyspy_status_json: dict[str, Any] | None = None
    python_stack_samples_json: dict[str, Any] | None = None
    python_heap_profile_json: dict[str, Any] | None = None
    go_heap_profile_json: dict[str, Any] | None = None
    source_snapshot_json: dict[str, Any] | None = None
    python_lock_wait_profile_json: dict[str, Any] | None = None
    python_exception_profile_json: dict[str, Any] | None = None
    python_queue_profile_json: dict[str, Any] | None = None
    python_pool_profile_json: dict[str, Any] | None = None
    python_retry_timeout_profile_json: dict[str, Any] | None = None
    python_cache_profile_json: dict[str, Any] | None = None
    python_input_profile_json: dict[str, Any] | None = None


def structure_artifact_evidence(
    *,
    task_id: str,
    artifacts: list[dict[str, Any]],
    artifact_values: dict[str, Any] | None = None,
    evidence_window: dict[str, Any] | EvidenceWindowMetadata | None = None,
) -> StructuredEvidence:
    """Convert raw and semi-raw collector artifacts into compact evidence."""
    values = artifact_values or {}
    window = _normalize_evidence_window(evidence_window or values.get("evidence_window") or _artifact_value_window(values))
    window_json = window.model_dump(mode="json")
    runtime_profile = values.get("python_stack_samples_json") if isinstance(values.get("python_stack_samples_json"), dict) else values.get("pyspy_status_json")
    artifact_refs = _with_window_metadata(_build_artifact_refs(task_id, artifacts), window_json)
    top_functions = _normalize_top_functions(values.get("top_json"))
    if not top_functions:
        top_functions = _top_from_go_heap_profile(values.get("go_heap_profile_json"))
    if not top_functions:
        top_functions = _top_from_off_cpu_wait(values.get("off_cpu_wait_json"))
    if not top_functions:
        top_functions = _top_from_flamegraph_tree(values.get("flamegraph_json"))
    if not top_functions:
        top_functions = _top_from_flamegraph_svg(values.get("flamegraph_svg"))
    top_functions = _with_window_metadata(top_functions, window_json)

    depth = values.get("depth_evidence_json") if isinstance(values.get("depth_evidence_json"), dict) else {}
    stack_summary = {
        **_build_stack_summary(
            depth,
            top_functions,
            artifact_refs,
            off_cpu_wait=values.get("off_cpu_wait_json"),
            runtime_profile=runtime_profile,
        ),
        "evidence_window": window_json,
    }
    sample_quality = _runtime_profile_sample_quality(runtime_profile)
    if sample_quality:
        stack_summary["sample_quality"] = sample_quality
    call_path_hotspots = _with_window_metadata(_build_call_path_hotspots(depth, top_functions), window_json)
    trace_profile = values.get("trace_endpoint_profile_json")
    if isinstance(trace_profile, dict):
        profile_hotspots = _trace_profile_hotspots(trace_profile)
        if profile_hotspots:
            call_path_hotspots = _with_window_metadata(profile_hotspots, window_json)
    runtime_line_candidates = _runtime_line_candidates(
        depth=depth,
        runtime_profile=runtime_profile,
        top_functions=top_functions,
        call_path_hotspots=call_path_hotspots,
    )
    python_scenarios = _python_scenario_values(values)
    confidence_inputs = _build_confidence_inputs(
        top_functions=top_functions,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        artifact_refs=artifact_refs,
        sys_metrics=values.get("sys_metrics"),
        ebpf_metrics=values.get("ebpf_metrics"),
        off_cpu_wait=values.get("off_cpu_wait_json"),
        log_window=values.get("log_window_json"),
        dependency_check=values.get("dependency_check_json"),
        redis_check=values.get("redis_check_json"),
        trace_profile=trace_profile,
        runtime_control=values.get("runtime_control_event_json"),
        pyspy_status=values.get("pyspy_status_json"),
        go_heap_profile=values.get("go_heap_profile_json"),
        python_scenarios=python_scenarios,
        baseline=values.get("continuous_summary"),
    )
    confidence_inputs["python_scenario_gates"] = _python_scenario_gates(
        top_functions=top_functions,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        off_cpu_wait=values.get("off_cpu_wait_json"),
        python_scenarios=python_scenarios,
        source_snapshot=values.get("source_snapshot_json"),
        dependency_check=values.get("dependency_check_json"),
        redis_check=values.get("redis_check_json"),
        trace_profile=trace_profile,
        baseline=values.get("continuous_summary"),
        runtime_line_candidates=runtime_line_candidates,
    )
    confidence_inputs["runtime_line_candidates"] = runtime_line_candidates
    confidence_inputs["runtime_profile_quality"] = sample_quality.get("diagnostic_value") if sample_quality else "unknown"
    confidence_inputs["runtime_profile_non_idle_ratio"] = sample_quality.get("non_idle_ratio") if sample_quality else 0.0
    confidence_inputs["runtime_profile_primitive_frame_ratio"] = sample_quality.get("primitive_frame_ratio") if sample_quality else 0.0
    confidence_inputs["runtime_profile_framework_loop_ratio"] = sample_quality.get("framework_loop_ratio") if sample_quality else 0.0
    confidence_inputs["runtime_profile_target_code_ratio"] = sample_quality.get("target_code_ratio") if sample_quality else 0.0
    confidence_inputs["evidence_window"] = window_json
    evidence_index = _build_evidence_index(
        artifact_refs=artifact_refs,
        depth=depth,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        confidence_inputs=confidence_inputs,
        off_cpu_wait=values.get("off_cpu_wait_json"),
        log_window=values.get("log_window_json"),
        dependency_check=values.get("dependency_check_json"),
        redis_check=values.get("redis_check_json"),
        trace_profile=trace_profile,
        runtime_control=values.get("runtime_control_event_json"),
        go_heap_profile=values.get("go_heap_profile_json"),
        python_scenarios=python_scenarios,
    )
    if confidence_inputs.get("python_scenario_gates"):
        evidence_index["python_scenario_gates"] = confidence_inputs["python_scenario_gates"]
    evidence_index["evidence_window"] = window_json
    if sample_quality:
        evidence_index["runtime_profile_quality"] = sample_quality
    return StructuredEvidence(
        task_id=task_id,
        trigger_event_id=window.trigger_event_id,
        evidence_cohort_id=window.evidence_cohort_id,
        collection_mode=window.collection_mode,
        window_start=window.window_start,
        window_end=window.window_end,
        trigger_observed_at=window.trigger_observed_at,
        timing_relation=window.timing_relation,
        artifact_refs=artifact_refs,
        top_functions=top_functions,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        evidence_index=evidence_index,
        confidence_inputs=confidence_inputs,
        sys_metrics=values.get("sys_metrics") if isinstance(values.get("sys_metrics"), dict) else None,
        ebpf_metrics=values.get("ebpf_metrics") if isinstance(values.get("ebpf_metrics"), dict) else None,
        memory_json=values.get("memory_json") if isinstance(values.get("memory_json"), dict) else None,
        off_cpu_wait_json=values.get("off_cpu_wait_json") if isinstance(values.get("off_cpu_wait_json"), dict) else None,
        log_window_json=values.get("log_window_json") if isinstance(values.get("log_window_json"), dict) else None,
        dependency_check_json=values.get("dependency_check_json") if isinstance(values.get("dependency_check_json"), dict) else None,
        redis_check_json=values.get("redis_check_json") if isinstance(values.get("redis_check_json"), dict) else None,
        trace_endpoint_profile_json=trace_profile if isinstance(trace_profile, dict) else None,
        runtime_control_event_json=values.get("runtime_control_event_json") if isinstance(values.get("runtime_control_event_json"), dict) else None,
        pyspy_status_json=values.get("pyspy_status_json") if isinstance(values.get("pyspy_status_json"), dict) else None,
        python_stack_samples_json=values.get("python_stack_samples_json") if isinstance(values.get("python_stack_samples_json"), dict) else None,
        python_heap_profile_json=values.get("python_heap_profile_json") if isinstance(values.get("python_heap_profile_json"), dict) else None,
        go_heap_profile_json=values.get("go_heap_profile_json") if isinstance(values.get("go_heap_profile_json"), dict) else None,
        source_snapshot_json=values.get("source_snapshot_json") if isinstance(values.get("source_snapshot_json"), dict) else None,
        python_lock_wait_profile_json=values.get("python_lock_wait_profile_json") if isinstance(values.get("python_lock_wait_profile_json"), dict) else None,
        python_exception_profile_json=values.get("python_exception_profile_json") if isinstance(values.get("python_exception_profile_json"), dict) else None,
        python_queue_profile_json=values.get("python_queue_profile_json") if isinstance(values.get("python_queue_profile_json"), dict) else None,
        python_pool_profile_json=values.get("python_pool_profile_json") if isinstance(values.get("python_pool_profile_json"), dict) else None,
        python_retry_timeout_profile_json=values.get("python_retry_timeout_profile_json") if isinstance(values.get("python_retry_timeout_profile_json"), dict) else None,
        python_cache_profile_json=values.get("python_cache_profile_json") if isinstance(values.get("python_cache_profile_json"), dict) else None,
        python_input_profile_json=values.get("python_input_profile_json") if isinstance(values.get("python_input_profile_json"), dict) else None,
    )


def rca_inputs_from_structured(structured: StructuredEvidence) -> dict[str, Any]:
    """Map structured evidence back to existing RCA input fields."""
    return {
        "top_functions": structured.top_functions,
        "sys_metrics": structured.sys_metrics,
        "ebpf_metrics": structured.ebpf_metrics,
        "off_cpu_wait_json": structured.off_cpu_wait_json,
        "runtime_control_event_json": structured.runtime_control_event_json,
        "evidence_index": structured.evidence_index,
    }


def _normalize_evidence_window(value: Any) -> EvidenceWindowMetadata:
    if isinstance(value, EvidenceWindowMetadata):
        return value
    if isinstance(value, dict):
        return EvidenceWindowMetadata.model_validate(_normalize_window_keys(value))
    return EvidenceWindowMetadata()


def _artifact_value_window(values: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "log_window_json",
        "dependency_check_json",
        "redis_check_json",
        "off_cpu_wait_json",
        "depth_evidence_json",
        "continuous_summary",
        "runtime_control_event_json",
        "go_heap_profile_json",
        "python_lock_wait_profile_json",
        "python_exception_profile_json",
        "python_queue_profile_json",
        "python_pool_profile_json",
        "python_retry_timeout_profile_json",
        "python_cache_profile_json",
        "python_input_profile_json",
    ):
        value = values.get(key)
        if isinstance(value, dict) and isinstance(value.get("evidence_window"), dict):
            return value["evidence_window"]
    return {}


def _normalize_window_keys(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    if "window_start" not in normalized and "start" in normalized:
        normalized["window_start"] = str(normalized["start"]) if normalized["start"] is not None else None
    if "window_end" not in normalized and "end" in normalized:
        normalized["window_end"] = str(normalized["end"]) if normalized["end"] is not None else None
    return normalized


def _with_window_metadata(items: list[dict[str, Any]], window: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**item, "evidence_window": window} for item in items]


def _build_artifact_refs(task_id: str, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for artifact in sorted(artifacts, key=lambda item: (str(item.get("artifact_type", "")), str(item.get("filename", "")))):
        artifact_type = str(artifact.get("artifact_type") or "unknown")
        refs.append({
            "artifact_type": artifact_type,
            "filename": str(artifact.get("filename") or ""),
            "content_type": str(artifact.get("content_type") or ""),
            "size_bytes": int(artifact.get("size_bytes") or 0),
            "sha256": str(artifact.get("sha256") or ""),
            "storage_status": str(artifact.get("storage_status") or ""),
            "storage_error": str(artifact.get("storage_error") or ""),
            "planned_object_key": str(artifact.get("planned_object_key") or ""),
            "object_key": str(artifact.get("object_key") or ""),
            "local_path": str(artifact.get("local_path") or ""),
            "raw_payload_policy": str(artifact.get("raw_payload_policy") or "references_only"),
            "evidence_ref": f"task:{task_id}:artifact:{artifact_type}",
        })
    return refs


def _normalize_top_functions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("function") or item.get("symbol") or "").strip()
        if _invalid_function_anchor(name):
            continue
        raw_samples = item.get("samples", item.get("sample_count", item.get("value")))
        samples = 0 if raw_samples is None else _strict_nonnegative_int(raw_samples)
        percent = _safe_float(item.get("percent"))
        if samples is None or not 0.0 <= percent <= 100.0:
            continue
        call_path = item.get("call_path")
        if isinstance(call_path, str):
            call_path = [part for part in call_path.split(";") if part]
        if not isinstance(call_path, list):
            call_path = []
        items.append({
            "name": name,
            "samples": samples,
            "percent": round(percent, 2),
            "file": str(item.get("file") or ""),
            "line": max(0, _safe_int(item.get("line"))),
            "call_path": [str(part) for part in call_path if str(part).strip()],
        })
    items.sort(key=lambda item: (-float(item.get("percent") or 0.0), -int(item.get("samples") or 0), item["name"]))
    result = []
    for index, item in enumerate(items[:TOP_LIMIT]):
        result.append({**item, "evidence_ref": f"structured_evidence.top_functions[{index}]"})
    return result


def _top_from_off_cpu_wait(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    stacks = value.get("top_wait_stacks")
    if not isinstance(stacks, list):
        return []
    items: list[dict[str, Any]] = []
    for item in stacks:
        if not isinstance(item, dict):
            continue
        stack = item.get("stack")
        name = str(item.get("top_frame") or "").strip()
        if not name and isinstance(stack, list) and stack:
            name = str(stack[0]).strip()
        if _invalid_function_anchor(name):
            continue
        samples = 0 if item.get("samples") is None else _strict_nonnegative_int(item.get("samples"))
        percent = _safe_float(item.get("percent"))
        if samples is None or not 0.0 <= percent <= 100.0:
            continue
        items.append({
            "name": name,
            "samples": samples,
            "percent": round(percent, 2),
            "wait_reason": str(item.get("wait_reason") or ""),
            "source": "off_cpu_wait",
        })
    items.sort(key=lambda item: (-float(item.get("percent") or 0.0), -int(item.get("samples") or 0), item["name"]))
    return [
        {
            **item,
            "evidence_ref": f"structured_evidence.top_functions[{index}]",
        }
        for index, item in enumerate(items[:TOP_LIMIT])
    ]


def _top_from_flamegraph_tree(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    total = _safe_float(value.get("value"))
    counter: dict[str, int] = {}

    def walk(node: dict[str, Any], is_root: bool = False) -> None:
        name = str(node.get("name") or "").strip()
        samples = _safe_int(node.get("value"))
        if name and not is_root and not _invalid_function_anchor(name) and samples >= 0:
            counter[name] = max(counter.get(name, 0), samples)
        for child in node.get("children", []) if isinstance(node.get("children"), list) else []:
            if isinstance(child, dict):
                walk(child)

    walk(value, is_root=True)
    entries = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:TOP_LIMIT]
    return [
        {
            "name": name,
            "samples": samples,
            "percent": round((samples / total * 100.0), 2) if total else 0.0,
            "evidence_ref": f"structured_evidence.top_functions[{index}]",
        }
        for index, (name, samples) in enumerate(entries)
    ]


def _top_from_flamegraph_svg(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, str) or "<title" not in value:
        return []
    counter: dict[str, dict[str, float | int | str]] = {}
    for index, raw_title in enumerate(re.findall(r"<title[^>]*>(.*?)</title>", value, flags=re.IGNORECASE | re.DOTALL)):
        title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        if not title:
            continue
        name = _function_name_from_svg_title(title)
        if _invalid_function_anchor(name):
            continue
        percent = _percent_from_text(title)
        samples = _samples_from_text(title)
        if not 0.0 <= percent <= 100.0 or samples < 0 or (percent <= 0.0 and samples <= 0):
            continue
        current = counter.get(name)
        if current is None or percent > float(current["percent"]) or samples > int(current["samples"]):
            counter[name] = {
                "name": name,
                "samples": samples,
                "percent": round(percent, 2),
                "order": index,
            }
    entries = sorted(
        counter.values(),
        key=lambda item: (-float(item["percent"]), -int(item["samples"]), int(item["order"]), str(item["name"])),
    )[:TOP_LIMIT]
    return [
        {
            "name": str(item["name"]),
            "samples": int(item["samples"]),
            "percent": float(item["percent"]),
            "evidence_ref": f"structured_evidence.top_functions[{index}]",
        }
        for index, item in enumerate(entries)
    ]


def _top_from_go_heap_profile(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    hotspots = value.get("hotspots")
    if not isinstance(hotspots, list):
        return []
    items: list[dict[str, Any]] = []
    for item in hotspots:
        if not isinstance(item, dict):
            continue
        name = str(item.get("function") or "").strip()
        if _invalid_function_anchor(name):
            continue
        flat_bytes = _safe_int(item.get("flat_bytes"))
        cum_bytes = _safe_int(item.get("cum_bytes"))
        percent = _safe_float(item.get("flat_percent") or item.get("cum_percent"))
        if flat_bytes <= 0 and cum_bytes <= 0:
            continue
        items.append({
            "name": name,
            "samples": max(flat_bytes, cum_bytes),
            "percent": round(percent, 2),
            "file": str(item.get("file") or ""),
            "line": max(0, _safe_int(item.get("line"))),
            "call_path": [name],
            "source": "go_heap_profile",
            "flat_bytes": flat_bytes,
            "cum_bytes": cum_bytes,
        })
    items.sort(key=lambda item: (-int(item.get("samples") or 0), -float(item.get("percent") or 0.0), item["name"]))
    return [
        {
            **item,
            "evidence_ref": f"structured_evidence.top_functions[{index}]",
        }
        for index, item in enumerate(items[:TOP_LIMIT])
    ]


def _function_name_from_svg_title(title: str) -> str:
    first_line = title.splitlines()[0].strip()
    if "(" in first_line:
        first_line = first_line.split("(", 1)[0].strip()
    if " - " in first_line:
        first_line = first_line.split(" - ", 1)[0].strip()
    return first_line[:256]


def _percent_from_text(text: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return _safe_float(match.group(1)) if match else 0.0


def _samples_from_text(text: str) -> int:
    match = re.search(r"(\d[\d,]*)\s+(?:samples?|frames?)", text, flags=re.IGNORECASE)
    if not match:
        return 0
    return _safe_int(match.group(1).replace(",", ""))


def _invalid_function_anchor(name: str) -> bool:
    normalized = name.strip().lower()
    return (
        not normalized
        or normalized in {"[unknown]", "unknown", "all", "root"}
        or bool(re.fullmatch(r"(?:0x)?[0-9a-f]+", normalized))
    )


def _strict_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _build_stack_summary(
    depth: dict[str, Any],
    top_functions: list[dict[str, Any]],
    artifact_refs: list[dict[str, Any]],
    off_cpu_wait: Any = None,
    runtime_profile: Any = None,
) -> dict[str, Any]:
    stack_samples = depth.get("stack_samples", []) if isinstance(depth, dict) else []
    first_sample = stack_samples[0] if isinstance(stack_samples, list) and stack_samples and isinstance(stack_samples[0], dict) else {}
    first_top = top_functions[0] if top_functions else {}
    hot_frame = str(first_sample.get("hot_frame") or first_top.get("name") or "")
    call_path = str(first_sample.get("call_path") or "")
    sample_count = _safe_int(first_sample.get("sample_count") or first_top.get("samples"))
    percent = _safe_float(first_sample.get("percent") or first_top.get("percent"))
    off_cpu_summary = off_cpu_wait.get("summary") if isinstance(off_cpu_wait, dict) and isinstance(off_cpu_wait.get("summary"), dict) else {}
    off_cpu_stacks = off_cpu_wait.get("top_wait_stacks") if isinstance(off_cpu_wait, dict) and isinstance(off_cpu_wait.get("top_wait_stacks"), list) else []
    if off_cpu_stacks:
        first_wait = off_cpu_stacks[0] if isinstance(off_cpu_stacks[0], dict) else {}
        hot_frame = hot_frame or str(first_wait.get("top_frame") or "")
        sample_count = sample_count or _safe_int(off_cpu_summary.get("sample_count"))
        percent = percent or _safe_float(first_wait.get("percent"))
    parse_status = "ok" if hot_frame or stack_samples or top_functions else "insufficient_structured_signal"
    return {
        "dominant_hot_frame": hot_frame,
        "dominant_percent": round(percent, 2),
        "sample_count": sample_count,
        "stack_sample_count": (len(stack_samples) if isinstance(stack_samples, list) else 0) or len(off_cpu_stacks),
        "has_call_path": bool(call_path or depth.get("call_path") or (depth.get("context") or {}).get("call_path")),
        "has_wait_reason": bool(first_sample.get("wait_reason") or depth.get("wait_reason") or off_cpu_summary.get("has_wait_reason")),
        "top_wait_reason": str(off_cpu_summary.get("top_wait_reason") or first_sample.get("wait_reason") or depth.get("wait_reason") or ""),
        "total_wait_ms": round(_safe_float(off_cpu_summary.get("total_wait_ms")), 2),
        "parse_status": parse_status,
        "raw_payload_policy": "references_only",
        "artifact_ref_count": len(artifact_refs),
        "evidence_ref": "structured_evidence.stack_summary",
        "sample_quality": _runtime_profile_sample_quality(runtime_profile),
    }


def _build_call_path_hotspots(
    depth: dict[str, Any],
    top_functions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(depth, dict):
        return []
    context = depth.get("context", {}) if isinstance(depth.get("context"), dict) else {}
    stack_samples = depth.get("stack_samples", []) if isinstance(depth.get("stack_samples"), list) else []
    hotspots: list[dict[str, Any]] = []
    for index, item in enumerate(stack_samples[:HOTSPOT_LIMIT]):
        if not isinstance(item, dict):
            continue
        function = str(item.get("hot_frame") or (top_functions[0].get("name") if top_functions else "") or "")
        call_path_text = str(item.get("call_path") or context.get("call_path") or "")
        if not function and not call_path_text:
            continue
        hotspots.append({
            "function": function,
            "percent": round(_safe_float(item.get("percent")), 2),
            "samples": _safe_int(item.get("sample_count")),
            "call_path": [part for part in call_path_text.split(";") if part],
            "endpoint": str(item.get("endpoint") or context.get("endpoint") or ""),
            "service_id": str(item.get("service_id") or context.get("service_id") or context.get("service") or ""),
            "instance_id": str(item.get("instance_id") or context.get("instance_id") or context.get("instance") or ""),
            "context_id": str(item.get("context_id") or context.get("context_id") or ""),
            "file": str(item.get("file") or ""),
            "line": _line_from_hint(item.get("line_hint")) or _safe_int(item.get("line")),
            "evidence_ref": f"structured_evidence.call_path_hotspots[{index}]",
        })
    return hotspots


def _line_from_hint(value: Any) -> int:
    match = re.search(r":(\d+)$", str(value or ""))
    return int(match.group(1)) if match else 0


def _build_confidence_inputs(
    *,
    top_functions: list[dict[str, Any]],
    stack_summary: dict[str, Any],
    call_path_hotspots: list[dict[str, Any]],
    artifact_refs: list[dict[str, Any]],
    sys_metrics: Any,
    ebpf_metrics: Any,
    off_cpu_wait: Any,
    log_window: Any,
    dependency_check: Any,
    redis_check: Any,
    trace_profile: Any,
    runtime_control: Any,
    pyspy_status: Any,
    go_heap_profile: Any,
    python_scenarios: dict[str, Any],
    baseline: Any,
) -> dict[str, Any]:
    first_top = top_functions[0] if top_functions else {}
    context_completeness = "high" if call_path_hotspots else "medium" if stack_summary.get("has_call_path") else "low"
    artifact_types = [item["artifact_type"] for item in artifact_refs]
    validity_by_family = {
        family: _evidence_status(value)
        for family, value in (
            ("off_cpu_wait_profile", off_cpu_wait),
            ("trace_endpoint_profile", trace_profile),
            ("log_scan", log_window),
            ("dependency_check", dependency_check),
            ("redis_check", redis_check),
            ("runtime_control_history", runtime_control),
            ("python_runtime_profile", pyspy_status),
            ("go_heap_profile", go_heap_profile),
            *python_scenarios.items(),
            ("baseline_window_profile", baseline),
        )
        if isinstance(value, dict) and _evidence_status(value)
    }
    runtime_summary = runtime_control.get("summary") if isinstance(runtime_control, dict) and isinstance(runtime_control.get("summary"), dict) else {}
    return {
        "sample_count": _safe_int(stack_summary.get("sample_count") or first_top.get("samples")),
        "dominant_percent": round(_safe_float(stack_summary.get("dominant_percent") or first_top.get("percent")), 2),
        "context_completeness": context_completeness,
        "has_system_pressure": isinstance(sys_metrics, dict) and bool(sys_metrics),
        "has_wait_or_io_signal": _has_wait_or_io_signal(ebpf_metrics, off_cpu_wait),
        "has_log_signal": isinstance(log_window, dict) and bool(log_window.get("error_clusters")),
        "has_dependency_signal": isinstance(dependency_check, dict) and bool(dependency_check.get("checks")),
        "has_redis_signal": isinstance(redis_check, dict) and bool(redis_check),
        "failed_dependency_count": _failed_dependency_count(dependency_check),
        "log_error_cluster_count": _log_error_cluster_count(log_window),
        "redis_slowlog_entry_count": _redis_slowlog_entry_count(redis_check),
        "redis_max_latency_ms": _redis_max_latency_ms(redis_check),
        "parse_status": stack_summary.get("parse_status", "insufficient_structured_signal"),
        "artifact_types": artifact_types,
        "collector_families": _collector_families(artifact_types),
        "token_safety": "compact_summary_only",
        "trace_source_status": _trace_profile_status(trace_profile, "trace_source"),
        "stack_source_status": _trace_profile_status(trace_profile, "stack_source"),
        "trace_correlation_status": _trace_profile_status(trace_profile, "correlation_status"),
        "trace_max_supported_level": _trace_profile_status(trace_profile, "correlation_status", "max_supported_level"),
        "evidence_validity_by_family": validity_by_family,
        "runtime_control_evidence_status": validity_by_family.get("runtime_control_history", ""),
        "has_complete_control_chain": bool(runtime_summary.get("has_complete_control_chain")),
        "control_event_count": _safe_int(runtime_summary.get("event_count")),
        "python_scenario_statuses": {
            family: _evidence_status(value)
            for family, value in python_scenarios.items()
            if isinstance(value, dict) and _evidence_status(value)
        },
    }


def _build_evidence_index(
    *,
    artifact_refs: list[dict[str, Any]],
    depth: dict[str, Any],
    stack_summary: dict[str, Any],
    call_path_hotspots: list[dict[str, Any]],
    confidence_inputs: dict[str, Any],
    off_cpu_wait: Any,
    log_window: Any,
    dependency_check: Any,
    redis_check: Any,
    trace_profile: Any,
    runtime_control: Any,
    go_heap_profile: Any,
    python_scenarios: dict[str, Any],
) -> dict[str, Any]:
    index = dict(depth) if isinstance(depth, dict) else {}
    index["artifact_refs"] = artifact_refs
    index["stack_summary"] = stack_summary
    index["call_path_hotspots"] = call_path_hotspots
    index["confidence_inputs"] = confidence_inputs
    if isinstance(off_cpu_wait, dict):
        index["off_cpu_wait"] = _compact_off_cpu_wait(off_cpu_wait)
    if isinstance(log_window, dict):
        index["log_scan"] = _compact_log_window(log_window)
    if isinstance(dependency_check, dict):
        index["dependency_check"] = _compact_dependency_check(dependency_check)
    if isinstance(redis_check, dict):
        index["redis_check"] = _compact_redis_check(redis_check)
    if isinstance(trace_profile, dict):
        index["trace_endpoint_profile"] = _compact_trace_profile(trace_profile)
    if isinstance(runtime_control, dict):
        index["runtime_control"] = _compact_runtime_control(runtime_control)
    if isinstance(go_heap_profile, dict):
        index["go_heap_profile"] = _compact_go_heap_profile(go_heap_profile)
    for family, payload in python_scenarios.items():
        if isinstance(payload, dict):
            index[family] = _compact_python_scenario(payload)
    return index


def _trace_profile_hotspots(value: dict[str, Any]) -> list[dict[str, Any]]:
    hotspots = value.get("call_path_hotspots")
    if not isinstance(hotspots, list):
        return []
    result = []
    for index, item in enumerate(hotspots[:HOTSPOT_LIMIT]):
        if not isinstance(item, dict):
            continue
        function = str(item.get("function") or "")
        call_path = item.get("call_path") if isinstance(item.get("call_path"), list) else []
        if not function and not call_path:
            continue
        result.append({
            "function": function,
            "percent": round(_safe_float(item.get("percent")), 2),
            "samples": _safe_int(item.get("samples") or item.get("sample_count")),
            "call_path": [str(part) for part in call_path],
            "endpoint": str(item.get("endpoint") or ""),
            "service_id": str(item.get("service_id") or ""),
            "instance_id": str(item.get("instance_id") or ""),
            "context_id": str(item.get("context_id") or ""),
            "file": str(item.get("file") or ""),
            "line": _safe_int(item.get("line")),
            "trace_ids": [str(item.get("trace_id"))] if item.get("trace_id") else list(item.get("trace_ids") or []),
            "correlation_method": str(item.get("correlation_method") or ""),
            "confidence": round(_safe_float(item.get("confidence")), 3),
            "evidence_ref": str(item.get("evidence_ref") or f"trace_endpoint_profile.call_path_hotspots[{index}]"),
        })
    return result


def _trace_profile_status(value: Any, section: str, field: str = "status") -> str:
    if not isinstance(value, dict):
        return ""
    section_value = value.get(section)
    if not isinstance(section_value, dict):
        return ""
    return str(section_value.get(field) or "")


def _runtime_profile_sample_quality(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    quality = value.get("sample_quality")
    if not isinstance(quality, dict):
        return {}
    return {
        "diagnostic_value": str(quality.get("diagnostic_value") or "unknown"),
        "dominant_state": str(quality.get("dominant_state") or "unknown"),
        "non_idle_ratio": _safe_float(quality.get("non_idle_ratio")),
        "primitive_frame_ratio": _safe_float(quality.get("primitive_frame_ratio")),
        "framework_loop_ratio": _safe_float(quality.get("framework_loop_ratio")),
        "target_code_ratio": _safe_float(quality.get("target_code_ratio")),
        "sample_count": _safe_int(quality.get("sample_count")),
        "stable_across_samples": bool(quality.get("stable_across_samples")),
        "reason": str(quality.get("reason") or ""),
    }


def _compact_trace_profile(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "stack_source": value.get("stack_source", {}),
        "trace_source": {
            key: value.get("trace_source", {}).get(key)
            for key in ("kind", "status", "records_read", "records_in_window", "blocked_reason")
            if isinstance(value.get("trace_source"), dict) and key in value.get("trace_source", {})
        },
        "endpoint_bindings": (value.get("endpoint_bindings") or [])[:10],
        "call_path_hotspots": (value.get("call_path_hotspots") or [])[:10],
        "correlation_status": value.get("correlation_status", {}),
    }


def _collector_families(artifact_types: list[str]) -> list[str]:
    mapping = {
        "log_window_json": "log_scan",
        "dependency_check_json": "dependency_check",
        "redis_check_json": "redis_check",
        "top_json": "runtime_snapshot",
        "flamegraph_json": "runtime_snapshot",
        "depth_evidence_json": "runtime_snapshot",
        "continuous_top_json": "runtime_snapshot",
        "continuous_flamegraph_json": "runtime_snapshot",
        "off_cpu_wait_json": "off_cpu_wait_profile",
        "ebpf_metrics": "io_profile",
        "sys_metrics": "sys_metrics",
        "memory_json": "memory_profile",
        "trace_endpoint_profile_json": "trace_endpoint_profile",
        "runtime_control_event_json": "runtime_control_history",
        "pyspy_status_json": "python_runtime_profile",
        "python_stack_samples_json": "python_runtime_profile",
        "python_heap_profile_json": "python_heap_profile",
        "go_heap_profile_json": "go_heap_profile",
        "source_snapshot_json": "source_snapshot",
        "python_lock_wait_profile_json": "python_lock_wait_profile",
        "python_exception_profile_json": "python_exception_profile",
        "python_queue_profile_json": "python_queue_profile",
        "python_pool_profile_json": "python_pool_profile",
        "python_retry_timeout_profile_json": "python_retry_timeout_profile",
        "python_cache_profile_json": "python_cache_profile",
        "python_input_profile_json": "python_input_profile",
    }
    return list(dict.fromkeys(mapping.get(item, item) for item in artifact_types))


def _compact_log_window(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": value.get("summary", {}),
        "error_clusters": (value.get("error_clusters") or [])[:5],
        "evidence_index": value.get("evidence_index", {}),
    }


def _compact_dependency_check(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": value.get("summary", {}),
        "checks": (value.get("checks") or [])[:10],
    }


def _compact_redis_check(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "connectivity": value.get("connectivity", {}),
        "info_summary": value.get("info_summary", {}),
        "slowlog_summary": value.get("slowlog_summary", {}),
        "latency_summary": value.get("latency_summary", {}),
    }


def _compact_off_cpu_wait(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": value.get("schema_version"),
        "collector_family": value.get("collector_family"),
        "summary": value.get("summary", {}),
        "event_summary": value.get("event_summary", {}),
        "cause_summary": value.get("cause_summary", {}),
        "stack_quality": value.get("stack_quality", {}),
        "top_wait_stacks": (value.get("top_wait_stacks") or [])[:5],
        "kernel_stacks": (value.get("kernel_stacks") or [])[:5],
        "thread_wait_summary": (value.get("thread_wait_summary") or [])[:10],
        "syscall_wait_summary": value.get("syscall_wait_summary", {}),
        "blocking_summary": value.get("blocking_summary", {}),
        "collector_status": value.get("collector_status"),
        "parser_status": value.get("parser_status"),
        "trace_source": value.get("trace_source", {}),
        "correlation": value.get("correlation", {}),
        "endpoint_bindings": (value.get("endpoint_bindings") or [])[:5],
        "call_path_hotspots": (value.get("call_path_hotspots") or [])[:5],
        "capability_check": value.get("capability_check", {}),
        "evidence_validity": value.get("evidence_validity", {}),
    }


def _compact_runtime_control(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_pid": value.get("target_pid"),
        "evidence_window": value.get("evidence_window", {}),
        "summary": value.get("summary", {}),
        "events": (value.get("events") or [])[:20],
        "causal_edges": (value.get("causal_edges") or [])[:60],
        "evidence_validity": value.get("evidence_validity", {}),
    }


def _compact_go_heap_profile(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "producer": value.get("producer"),
        "profile_kind": value.get("profile_kind"),
        "heap_mode": value.get("heap_mode"),
        "sample_type": value.get("sample_type"),
        "summary": value.get("summary", {}),
        "hotspots": (value.get("hotspots") or [])[:10],
        "line_candidates": (value.get("line_candidates") or [])[:10],
        "raw_artifact_refs": value.get("raw_artifact_refs", []),
        "evidence_validity": value.get("evidence_validity", {}),
    }


def _python_scenario_values(values: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "python_lock_wait_profile": values.get("python_lock_wait_profile_json"),
        "python_exception_profile": values.get("python_exception_profile_json"),
        "python_queue_profile": values.get("python_queue_profile_json"),
        "python_pool_profile": values.get("python_pool_profile_json"),
        "python_retry_timeout_profile": values.get("python_retry_timeout_profile_json"),
        "python_cache_profile": values.get("python_cache_profile_json"),
        "python_input_profile": values.get("python_input_profile_json"),
    }
    return {family: payload for family, payload in mapping.items() if isinstance(payload, dict)}


def _compact_python_scenario(value: dict[str, Any]) -> dict[str, Any]:
    adapter = value.get("adapter") if isinstance(value.get("adapter"), dict) else {}
    return {
        "schema_version": value.get("schema_version"),
        "collector_family": value.get("collector_family"),
        "scenario_type": value.get("scenario_type"),
        "adapter": {
            "source_policy": adapter.get("source_policy"),
            "source_count": adapter.get("source_count"),
            "sources": (adapter.get("sources") or [])[:10],
        },
        "evidence_status": value.get("evidence_status"),
        "evidence_validity": value.get("evidence_validity", {}),
        "mechanism_evidence_refs": (value.get("mechanism_evidence_refs") or [])[:20],
        "counter_evidence_refs": (value.get("counter_evidence_refs") or [])[:20],
        "missing_evidence": (value.get("missing_evidence") or [])[:20],
        # Collector payloads may contain a legacy eligibility flag; the
        # structurer must never forward it as a formal conclusion.
        "conclusion_eligible": False,
        "formal_qualification": "session_ai_candidate_required",
        "eligibility_reason": value.get("eligibility_reason"),
        "wait_sites": (value.get("wait_sites") or [])[:10],
        "holder_candidates": (value.get("holder_candidates") or [])[:10],
        "exception_clusters": (value.get("exception_clusters") or [])[:10],
        "line_candidates": (value.get("line_candidates") or [])[:10],
        "backlog": value.get("backlog"),
        "active_tasks": (value.get("active_tasks") or [])[:10],
        "reserved_tasks": (value.get("reserved_tasks") or [])[:10],
        "slow_task_candidates": (value.get("slow_task_candidates") or [])[:10],
        "broker_evidence": value.get("broker_evidence", {}),
        "pool_type": value.get("pool_type"),
        "pool_exhausted": value.get("pool_exhausted"),
        "checked_out": value.get("checked_out"),
        "pool_size": value.get("pool_size"),
        "acquire_sites": (value.get("acquire_sites") or [])[:10],
        "retry_clusters": (value.get("retry_clusters") or [])[:10],
        "timeout_sites": (value.get("timeout_sites") or [])[:10],
        "attempt_count": value.get("attempt_count"),
        "backoff_detected": value.get("backoff_detected"),
        "dependency_context": value.get("dependency_context", {}),
        "cache_backend": value.get("cache_backend"),
        "cache_growth": value.get("cache_growth"),
        "cache_bytes": value.get("cache_bytes"),
        "cache_files": value.get("cache_files"),
        "unique_key_count": value.get("unique_key_count"),
        "cache_hits": value.get("cache_hits"),
        "cache_misses": value.get("cache_misses"),
        "key_sites": (value.get("key_sites") or [])[:10],
        "backend_sites": (value.get("backend_sites") or [])[:10],
        "input_operation": value.get("input_operation"),
        "slow_path_detected": value.get("slow_path_detected"),
        "rows": value.get("rows"),
        "categories": value.get("categories"),
        "cardinality": value.get("cardinality"),
        "elapsed_ms": value.get("elapsed_ms"),
        "slow_path_sites": (value.get("slow_path_sites") or [])[:10],
    }


def _python_scenario_gates(
    *,
    top_functions: list[dict[str, Any]],
    stack_summary: dict[str, Any],
    call_path_hotspots: list[dict[str, Any]],
    off_cpu_wait: Any,
    python_scenarios: dict[str, Any],
    source_snapshot: Any,
    dependency_check: Any,
    redis_check: Any,
    trace_profile: Any,
    baseline: Any,
    runtime_line_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    gates: dict[str, Any] = {}
    source_state = _source_snapshot_state(source_snapshot)
    counter_evidence = _python_scenario_counter_evidence(
        dependency_check=dependency_check,
        redis_check=redis_check,
    )
    builtin_gates = _builtin_python_scenario_gates(
        top_functions=top_functions,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        off_cpu_wait=off_cpu_wait,
        source_state=source_state,
        counter_evidence=counter_evidence,
        trace_profile=trace_profile,
        baseline=baseline,
        runtime_line_candidates=runtime_line_candidates or [],
    )
    gates.update(builtin_gates)
    for family, payload in python_scenarios.items():
        if not isinstance(payload, dict):
            continue
        gates[family] = _python_scenario_gate(
            family=family,
            payload=payload,
            source_state=source_state,
            counter_evidence=counter_evidence,
            trace_profile=trace_profile,
            baseline=baseline,
        )
    return gates


def _builtin_python_scenario_gates(
    *,
    top_functions: list[dict[str, Any]],
    stack_summary: dict[str, Any],
    call_path_hotspots: list[dict[str, Any]],
    off_cpu_wait: Any,
    source_state: dict[str, Any],
    counter_evidence: dict[str, Any],
    trace_profile: Any,
    baseline: Any,
    runtime_line_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    gates: dict[str, Any] = {}
    cpu_gate = _python_cpu_hotspot_gate(
        top_functions=top_functions,
        stack_summary=stack_summary,
        source_state=source_state,
        counter_evidence=counter_evidence,
        baseline=baseline,
        runtime_line_candidates=runtime_line_candidates,
    )
    if cpu_gate:
        gates["python_cpu_hotspot"] = cpu_gate
    endpoint_gate = _python_endpoint_latency_gate(
        call_path_hotspots=call_path_hotspots,
        trace_profile=trace_profile,
        source_state=source_state,
        counter_evidence=counter_evidence,
    )
    if endpoint_gate:
        gates["python_endpoint_latency"] = endpoint_gate
    io_gate = _python_io_blocking_gate(
        off_cpu_wait=off_cpu_wait,
        source_state=source_state,
        counter_evidence=counter_evidence,
    )
    if io_gate:
        gates["python_io_blocking"] = io_gate
    return gates


def _python_cpu_hotspot_gate(
    *,
    top_functions: list[dict[str, Any]],
    stack_summary: dict[str, Any],
    source_state: dict[str, Any],
    counter_evidence: dict[str, Any],
    baseline: Any,
    runtime_line_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    first = top_functions[0] if top_functions else {}
    if not first:
        return {}
    line_candidates = _merge_line_candidates(
        _line_candidates_from_items(top_functions),
        runtime_line_candidates,
    )
    line_verified = _line_candidates_match_source(line_candidates, source_state)
    sample_count = _safe_int(stack_summary.get("sample_count") or first.get("samples"))
    percent = _safe_float(stack_summary.get("dominant_percent") or first.get("percent"))
    quality = stack_summary.get("sample_quality") if isinstance(stack_summary.get("sample_quality"), dict) else {}
    primitive = _runtime_profile_is_primitive(first, quality)
    stable_hotspot = sample_count > 1 and percent > 0.0 and not primitive
    baseline_or_impact = _has_baseline_or_impact_signal(baseline)
    claim_type = "observation"
    reason = "CPU 热点证据尚未形成可验证源码定位。"
    if stable_hotspot and line_verified:
        claim_type = "partial_localization"
        reason = "Python CPU 热点已定位到 source_snapshot 验证的源码行，仍需 baseline/影响与反证门禁。"
    direct = stable_hotspot and line_verified and baseline_or_impact and not counter_evidence["has_failure"]
    if direct:
        claim_type = "direct_root_cause"
        reason = "Python CPU 热点、源码行、baseline/影响证据和反证门禁均已闭合。"
    if primitive:
        reason = "runtime primitive 或低诊断价值样本只能作为观察，不能升级为源码根因。"
    missing: list[str] = []
    if not stable_hotspot:
        missing.append("stable_python_hotspot")
    if not line_candidates:
        missing.append("verified_runtime_file_line_candidate")
    elif not line_verified:
        missing.append("source_snapshot_verification")
    if not baseline_or_impact:
        missing.append("baseline_or_same_window_impact")
    if counter_evidence["has_failure"]:
        missing.append("dependency_or_host_counter_evidence")
    if not direct:
        missing.append("scenario_root_cause_gate")
    missing.append("session_ai_candidate_qualification")
    return _scenario_gate_payload(
        family="python_cpu_hotspot",
        scenario_type="python_cpu_hotspot",
        evidence_status="valid" if stable_hotspot else "partial",
        source_policy="standard_runtime_profile",
        upstream_sources=[{"kind": "python_runtime_profile", "source_kind": "py-spy", "source_status": "loaded", "record_count": sample_count}],
        line_candidates=line_candidates,
        line_verified=line_verified,
        source_state=source_state,
        claim_type=claim_type,
        reason=reason,
        conclusion_eligible=direct,
        checks={
            "industrial_observation": True,
            "stable_hotspot": stable_hotspot,
            "runtime_line_candidate": bool(line_candidates),
            "source_snapshot_verified": line_verified,
            "baseline_or_impact": baseline_or_impact,
            "counter_evidence_clear": not counter_evidence["has_failure"],
            "runtime_primitive_rejected": primitive,
        },
        missing=missing,
        mechanism_refs=[str(first.get("evidence_ref") or "structured_evidence.top_functions[0]")],
        counter_refs=counter_evidence["evidence_refs"],
        trace_profile=None,
        baseline=baseline,
    )


def _python_endpoint_latency_gate(
    *,
    call_path_hotspots: list[dict[str, Any]],
    trace_profile: Any,
    source_state: dict[str, Any],
    counter_evidence: dict[str, Any],
) -> dict[str, Any]:
    trace_status = _trace_profile_status(trace_profile, "correlation_status")
    trace_level = _trace_profile_status(trace_profile, "correlation_status", "max_supported_level")
    if not call_path_hotspots and not trace_status:
        return {}
    line_candidates = _line_candidates_from_items(call_path_hotspots)
    line_verified = _line_candidates_match_source(line_candidates, source_state)
    correlated = bool(call_path_hotspots) and trace_status in {"completed", "partial"} and trace_level in {"endpoint", "call_path", "function", "line"}
    claim_type = "observation"
    reason = "Endpoint 慢证据尚未与 Python call path/source 同窗闭合。"
    if correlated:
        claim_type = "partial_localization"
        reason = "Trace 与 Python call path 已关联，仍需 source_snapshot 验证行和依赖反证。"
    if correlated and line_verified:
        claim_type = "partial_localization"
        reason = "Endpoint/call path 和源码行已闭合，仍需证明本地热点优于下游依赖解释。"
    has_endpoint_impact = _has_endpoint_impact_signal(call_path_hotspots, trace_profile)
    direct = correlated and line_verified and has_endpoint_impact and not counter_evidence["has_failure"]
    if direct:
        claim_type = "direct_root_cause"
        reason = "Endpoint 慢、Python call path、源码行和本地影响证据均已闭合，且无更强依赖反证。"
    missing: list[str] = []
    if not correlated:
        missing.append("endpoint_call_path_correlation")
    if line_candidates and not line_verified:
        missing.append("source_snapshot_verification")
    if not line_candidates:
        missing.append("verified_runtime_file_line_candidate")
    if counter_evidence["has_failure"]:
        missing.append("dependency_or_broker_counter_evidence")
    if not has_endpoint_impact:
        missing.append("endpoint_or_local_hotspot_impact")
    if not direct:
        missing.append("scenario_root_cause_gate")
    return _scenario_gate_payload(
        family="python_endpoint_latency",
        scenario_type="python_endpoint_latency",
        evidence_status="valid" if correlated else "partial",
        source_policy="trace_and_profile_correlation",
        upstream_sources=[{"kind": "trace_endpoint_profile", "source_kind": "otel_trace", "source_status": trace_status or "loaded", "record_count": len(call_path_hotspots)}],
        line_candidates=line_candidates,
        line_verified=line_verified,
        source_state=source_state,
        claim_type=claim_type,
        reason=reason,
        conclusion_eligible=direct,
        checks={
            "industrial_observation": bool(trace_status or call_path_hotspots),
            "endpoint_call_path_correlation": correlated,
            "runtime_line_candidate": bool(line_candidates),
            "source_snapshot_verified": line_verified,
            "endpoint_or_local_hotspot_impact": has_endpoint_impact,
            "counter_evidence_clear": not counter_evidence["has_failure"],
        },
        missing=missing,
        mechanism_refs=[str(item.get("evidence_ref")) for item in call_path_hotspots if item.get("evidence_ref")],
        counter_refs=counter_evidence["evidence_refs"],
        trace_profile=trace_profile,
        baseline=None,
    )


def _python_io_blocking_gate(
    *,
    off_cpu_wait: Any,
    source_state: dict[str, Any],
    counter_evidence: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(off_cpu_wait, dict):
        return {}
    validity = off_cpu_wait.get("evidence_validity") if isinstance(off_cpu_wait.get("evidence_validity"), dict) else {}
    status = str(validity.get("evidence_status") or "")
    stacks = [item for item in (off_cpu_wait.get("top_wait_stacks") or []) if isinstance(item, dict)]
    blocking_summary = off_cpu_wait.get("blocking_summary") if isinstance(off_cpu_wait.get("blocking_summary"), dict) else {}
    blocking_kinds = [
        str(item.get("blocking_kind") or "")
        for item in stacks
        if str(item.get("blocking_kind") or "")
    ]
    if isinstance(blocking_summary.get("by_kind"), dict):
        blocking_kinds.extend(str(kind) for kind in blocking_summary["by_kind"].keys())
    has_blocking = status in {"valid", "partial"} and bool(stacks or blocking_kinds)
    line_candidates = _line_candidates_from_items(stacks)
    line_verified = _line_candidates_match_source(line_candidates, source_state)
    local_kind = any(kind in {"file_io", "socket_io", "db_client", "redis_client", "http_client", "dns"} for kind in blocking_kinds)
    local_behavior = _has_local_io_behavior_evidence(off_cpu_wait)
    claim_type = "observation"
    reason = "I/O 阻塞证据尚未形成可验证 Python 调用点。"
    if has_blocking and line_verified:
        claim_type = "partial_localization"
        reason = "Python 阻塞调用点已通过 source_snapshot 验证，仍需证明本地代码行为而非外部依赖。"
    direct = has_blocking and line_verified and local_kind and local_behavior and not counter_evidence["has_failure"]
    if direct:
        claim_type = "direct_root_cause"
        reason = "Python I/O 阻塞调用点、源码行、本地阻塞行为证据和反证门禁均已闭合。"
    missing: list[str] = []
    if not has_blocking:
        missing.append("valid_off_cpu_blocking_observation")
    if not line_candidates:
        missing.append("verified_runtime_file_line_candidate")
    elif not line_verified:
        missing.append("source_snapshot_verification")
    if counter_evidence["has_failure"]:
        missing.append("dependency_or_broker_counter_evidence")
    if not local_behavior:
        missing.append("local_io_behavior_evidence")
    if not direct:
        missing.append("scenario_root_cause_gate")
    return _scenario_gate_payload(
        family="python_io_blocking",
        scenario_type="python_io_blocking",
        evidence_status=status or ("valid" if has_blocking else "empty_window"),
        source_policy="off_cpu_runtime_profile",
        upstream_sources=[{"kind": "off_cpu_wait_profile", "source_kind": "bcc_offcputime", "source_status": status or "unknown", "record_count": len(stacks)}],
        line_candidates=line_candidates,
        line_verified=line_verified,
        source_state=source_state,
        claim_type=claim_type,
        reason=reason,
        conclusion_eligible=direct,
        checks={
            "industrial_observation": has_blocking,
            "blocking_kind_normalized": bool(blocking_kinds),
            "local_blocking_callsite": local_kind,
            "local_io_behavior_evidence": local_behavior,
            "runtime_line_candidate": bool(line_candidates),
            "source_snapshot_verified": line_verified,
            "counter_evidence_clear": not counter_evidence["has_failure"],
        },
        missing=missing,
        mechanism_refs=[str(item.get("evidence_ref")) for item in stacks if item.get("evidence_ref")],
        counter_refs=counter_evidence["evidence_refs"],
        trace_profile=None,
        baseline=None,
    )


def _scenario_gate_payload(
    *,
    family: str,
    scenario_type: str,
    evidence_status: str,
    source_policy: str,
    upstream_sources: list[dict[str, Any]],
    line_candidates: list[dict[str, Any]],
    line_verified: bool,
    source_state: dict[str, Any],
    claim_type: str,
    reason: str,
    checks: dict[str, Any],
    missing: list[str],
    mechanism_refs: list[str],
    counter_refs: list[str],
    trace_profile: Any,
    baseline: Any,
    conclusion_eligible: bool = False,
) -> dict[str, Any]:
    # Scenario gates describe evidence quality and the highest defensible
    # localization boundary. Formal root-cause qualification belongs to the
    # session AI candidate gate.
    supported_claim_type = (
        "direct_failure_mechanism"
        if claim_type == "direct_root_cause"
        else claim_type
    )
    boundary_reason = reason
    if claim_type == "direct_root_cause":
        boundary_reason = (
            f"{reason} 当前仅表示证据支持的直接故障机制；"
            "Evidence Structurer 不直接产出正式根因，需由受控 AI candidate "
            "引用真实运行时/源码锚点后再执行统一资格门禁。"
        )
    return {
        "family": family,
        "scenario_type": scenario_type,
        "evidence_status": evidence_status,
        "source_policy": source_policy,
        "upstream_sources": upstream_sources[:10],
        "line_candidates": line_candidates[:10],
        "line_candidate_count": len(line_candidates),
        "source_snapshot_status": source_state["status"],
        "source_context_hash": source_state["source_context_hash"],
        "line_verified": line_verified,
        "max_supported_claim_type": supported_claim_type,
        "conclusion_eligible": False,
        "eligibility_reason": boundary_reason,
        "gate_checks": checks,
        "missing_evidence": list(dict.fromkeys(str(item) for item in missing if str(item or "").strip()))[:20],
        "mechanism_evidence_refs": [str(ref) for ref in mechanism_refs if str(ref or "").strip()][:20],
        "counter_evidence_refs": [str(ref) for ref in counter_refs if str(ref or "").strip()][:20],
        "trace_correlation_status": _trace_profile_status(trace_profile, "correlation_status"),
        "baseline_status": _evidence_status(baseline) if isinstance(baseline, dict) else "",
        "formal_qualification": "session_ai_candidate_required",
    }


def _python_scenario_gate(
    *,
    family: str,
    payload: dict[str, Any],
    source_state: dict[str, Any],
    counter_evidence: dict[str, Any],
    trace_profile: Any,
    baseline: Any,
) -> dict[str, Any]:
    status = _evidence_status(payload) or str(payload.get("evidence_status") or "")
    industrial_source = _python_scenario_has_industrial_source(payload)
    valid_observation = status in {"valid", "partial"} and industrial_source
    line_candidates = [item for item in (payload.get("line_candidates") or []) if isinstance(item, dict)]
    line_verified = valid_observation and _line_candidates_match_source(line_candidates, source_state)
    missing = list(payload.get("missing_evidence") or [])
    checks = {
        "industrial_observation": valid_observation,
        "industrial_source_provenance": industrial_source,
        "runtime_line_candidate": bool(line_candidates),
        "source_snapshot_verified": line_verified,
        "same_window_mechanism_evidence": False,
        "counter_evidence_clear": not counter_evidence["has_failure"],
    }
    claim_type = "observation"
    gate_reason = "场景证据尚未达到源码定位门禁。"

    if not industrial_source:
        gate_reason = "场景 producer 缺少工业采集器 source_kind/provenance，不能作为 valid 运行时证据。"
    elif not valid_observation:
        gate_reason = "场景 producer 未返回 valid/partial 工业证据。"
    elif family == "python_lock_wait_profile":
        checks["scenario_signal"] = bool(payload.get("wait_sites"))
        checks["same_window_mechanism_evidence"] = bool(payload.get("holder_candidates"))
        impact = _has_scenario_impact_signal(payload)
        checks["symptom_impact"] = impact
        if checks["scenario_signal"] and line_verified:
            claim_type = "partial_localization"
            gate_reason = "已定位 Python 等待点，但还缺持有者/影响闭合证据。"
        if claim_type == "partial_localization" and checks["same_window_mechanism_evidence"]:
            claim_type = "direct_failure_mechanism"
            gate_reason = "等待点和持有者候选已同窗出现，仍需症状影响和反证门禁才能成为源码根因。"
        if checks["scenario_signal"] and checks["same_window_mechanism_evidence"] and line_verified and impact and not counter_evidence["has_failure"]:
            claim_type = "direct_root_cause"
            gate_reason = "等待点、持有者候选、源码行和症状影响证据均已闭合。"
    elif family == "python_exception_profile":
        repeated = any(_safe_int(item.get("occurrence_count")) >= 2 for item in payload.get("exception_clusters") or [] if isinstance(item, dict))
        checks["scenario_signal"] = repeated
        checks["same_window_mechanism_evidence"] = repeated
        impact = _has_scenario_impact_signal(payload)
        checks["symptom_impact"] = impact
        if repeated and line_verified:
            claim_type = "direct_failure_mechanism"
            gate_reason = "异常簇和 throw/log 行已闭合，但仍需证明它解释错误率、延迟或 worker 失败。"
        if repeated and line_verified and impact and not counter_evidence["has_failure"]:
            claim_type = "direct_root_cause"
            gate_reason = "重复异常簇、源码 throw/log 行和同窗影响证据均已闭合。"
    elif family == "python_queue_profile":
        has_task = bool(payload.get("active_tasks") or payload.get("reserved_tasks") or payload.get("slow_task_candidates"))
        checks["scenario_signal"] = _safe_int(payload.get("backlog")) > 0 or has_task
        checks["task_identity"] = has_task
        checks["same_window_mechanism_evidence"] = _safe_int(payload.get("backlog")) > 0 and has_task
        task_source_hotspot = bool(payload.get("slow_task_candidates") or payload.get("worker_saturation") or payload.get("task_source_hotspot"))
        checks["task_source_hotspot"] = task_source_hotspot
        if checks["scenario_signal"] and has_task and line_verified:
            claim_type = "partial_localization"
            gate_reason = "队列/任务证据已定位到 Python 任务点，但 broker/worker 饱和原因仍需闭合。"
        if checks["same_window_mechanism_evidence"] and line_verified and task_source_hotspot and not counter_evidence["has_failure"]:
            claim_type = "direct_root_cause"
            gate_reason = "队列积压、任务身份、任务源码热点和反证门禁均已闭合。"
    elif family == "python_pool_profile":
        site_count = len(payload.get("wait_sites") or []) + len(payload.get("acquire_sites") or [])
        checks["scenario_signal"] = bool(payload.get("pool_exhausted")) or site_count > 0
        checks["same_window_mechanism_evidence"] = bool(payload.get("long_holder_candidates") or payload.get("release_evidence") == "missing")
        impact = _has_scenario_impact_signal(payload)
        checks["symptom_impact"] = impact
        if checks["scenario_signal"] and line_verified:
            claim_type = "partial_localization"
            gate_reason = "资源池等待/获取点已定位，仍需 acquire/release、长持有者或配置证据。"
        if checks["scenario_signal"] and checks["same_window_mechanism_evidence"] and line_verified and impact and not counter_evidence["has_failure"]:
            claim_type = "direct_root_cause"
            gate_reason = "资源池耗尽、源码获取点、持有/释放机制和症状影响证据均已闭合。"
    elif family == "python_retry_timeout_profile":
        attempts = _safe_int(payload.get("attempt_count"))
        checks["scenario_signal"] = attempts >= 2 or bool(payload.get("retry_clusters") or payload.get("timeout_sites"))
        checks["same_window_mechanism_evidence"] = attempts >= 2 and bool(payload.get("retry_clusters") or payload.get("timeout_sites"))
        amplification = _has_retry_amplification_signal(payload)
        checks["retry_amplification"] = amplification
        if checks["scenario_signal"] and line_verified:
            claim_type = "partial_localization"
            gate_reason = "重试/超时点已定位，若下游故障更强则只能作为贡献因素。"
        if checks["same_window_mechanism_evidence"] and line_verified and amplification and not counter_evidence["has_failure"]:
            claim_type = "direct_root_cause"
            gate_reason = "重试/超时源码点、本地放大证据和反证门禁均已闭合。"
    elif family == "python_cache_profile":
        checks["scenario_signal"] = bool(payload.get("cache_growth")) or _safe_float(payload.get("cache_bytes")) > 0 or _safe_int(payload.get("cache_files")) > 0
        checks["same_window_mechanism_evidence"] = bool(payload.get("key_sites") or payload.get("backend_sites"))
        impact = _has_scenario_impact_signal(payload) or _safe_float(payload.get("cache_bytes")) > 0 or _safe_int(payload.get("cache_files")) > 0
        checks["symptom_impact"] = impact
        if checks["scenario_signal"] and line_verified:
            claim_type = "partial_localization"
            gate_reason = "缓存增长已定位到 key/backend 候选点，仍需淘汰、容量或影响证据闭合。"
        if checks["scenario_signal"] and checks["same_window_mechanism_evidence"] and line_verified and impact and not counter_evidence["has_failure"]:
            claim_type = "direct_root_cause"
            gate_reason = "缓存增长、key/backend 源码点、同窗影响和反证门禁均已闭合。"
    elif family == "python_input_profile":
        checks["scenario_signal"] = bool(payload.get("slow_path_detected")) or _safe_int(payload.get("rows")) > 0 or _safe_int(payload.get("categories")) > 0
        checks["same_window_mechanism_evidence"] = bool(payload.get("slow_path_sites"))
        impact = _has_scenario_impact_signal(payload) or _safe_float(payload.get("elapsed_ms")) > 0
        checks["symptom_impact"] = impact
        if checks["scenario_signal"] and line_verified:
            claim_type = "partial_localization"
            gate_reason = "输入慢路径已定位到操作候选点，仍需输入分布和症状影响闭合。"
        if checks["scenario_signal"] and checks["same_window_mechanism_evidence"] and line_verified and impact and not counter_evidence["has_failure"]:
            claim_type = "direct_root_cause"
            gate_reason = "输入形态、慢路径源码点、同窗影响和反证门禁均已闭合。"
    else:
        checks["scenario_signal"] = valid_observation
        if valid_observation and line_verified:
            claim_type = "partial_localization"
            gate_reason = "场景证据已定位到源码行，但缺少场景专属根因门禁。"

    if valid_observation and not line_candidates:
        missing.append("verified_runtime_file_line_candidate")
    if valid_observation and line_candidates and not line_verified:
        missing.append("source_snapshot_verification")
    if status in {"valid", "partial"} and not industrial_source:
        missing.append("industrial_source_provenance")
    if counter_evidence["has_failure"]:
        missing.append("dependency_or_broker_counter_evidence")
    if claim_type != "direct_root_cause":
        missing.append("scenario_root_cause_gate")
    else:
        missing = [
            item for item in missing
            if item not in {
                "scenario_root_cause_gate",
                "source_snapshot_verification",
                "verified_runtime_file_line_candidate",
            }
        ]
    missing.append("session_ai_candidate_qualification")
    missing = list(dict.fromkeys(str(item) for item in missing if str(item or "").strip()))
    return {
        "family": family,
        "scenario_type": payload.get("scenario_type"),
        "evidence_status": status,
        "source_policy": (payload.get("adapter") or {}).get("source_policy") if isinstance(payload.get("adapter"), dict) else "",
        "upstream_sources": [
            {
                "kind": item.get("kind"),
                "source_kind": item.get("source_kind"),
                "source_status": item.get("source_status"),
                "record_count": item.get("record_count"),
            }
            for item in (
                (payload.get("adapter") if isinstance(payload.get("adapter"), dict) else {}).get("sources")
                or []
            )[:10]
            if isinstance(item, dict)
        ],
        "line_candidate_count": len(line_candidates),
        "source_snapshot_status": source_state["status"],
        "source_context_hash": source_state["source_context_hash"],
        "line_verified": line_verified,
        "max_supported_claim_type": (
            "direct_failure_mechanism"
            if claim_type == "direct_root_cause"
            else claim_type
        ),
        "conclusion_eligible": False,
        "eligibility_reason": (
            f"{gate_reason} 当前仅保留为证据支持的机制/定位边界；"
            "正式根因必须由受控 AI candidate 承接并通过统一资格门禁。"
        ),
        "gate_checks": checks,
        "missing_evidence": missing[:20],
        "mechanism_evidence_refs": (payload.get("mechanism_evidence_refs") or [])[:20],
        "counter_evidence_refs": counter_evidence["evidence_refs"][:20],
        "trace_correlation_status": _trace_profile_status(trace_profile, "correlation_status"),
        "baseline_status": _evidence_status(baseline) if isinstance(baseline, dict) else "",
        "formal_qualification": "session_ai_candidate_required",
    }


def _python_scenario_has_industrial_source(payload: dict[str, Any]) -> bool:
    adapter = payload.get("adapter") if isinstance(payload.get("adapter"), dict) else {}
    sources = adapter.get("sources") if isinstance(adapter.get("sources"), list) else []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if str(source.get("source_kind") or "") in PYTHON_SCENARIO_INDUSTRIAL_SOURCES:
            return True
    return False


def _source_snapshot_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "status": "missing",
            "source_context_hash": "",
            "revision": "",
            "snippets": [],
            "verified_source_lines": [],
            "source_syntax": [],
        }
    validity = value.get("evidence_validity") if isinstance(value.get("evidence_validity"), dict) else {}
    return {
        "status": str(validity.get("evidence_status") or ""),
        "source_context_hash": str(value.get("source_context_hash") or ""),
        "revision": str(value.get("revision") or ""),
        "snippets": [item for item in (value.get("snippets") or []) if isinstance(item, dict)],
        "verified_source_lines": [
            item for item in (value.get("verified_source_lines") or []) if isinstance(item, dict)
        ],
        "source_syntax": [
            item for item in (value.get("source_syntax") or []) if isinstance(item, dict)
        ],
    }


def _has_endpoint_impact_signal(call_path_hotspots: list[dict[str, Any]], trace_profile: Any) -> bool:
    if any(
        _safe_float(item.get("percent")) > 0
        or _safe_int(item.get("samples") or item.get("sample_count")) > 0
        or _safe_float(item.get("latency_ms") or item.get("duration_ms")) > 0
        for item in call_path_hotspots
        if isinstance(item, dict)
    ):
        return True
    if not isinstance(trace_profile, dict):
        return False
    for key in ("latency_summary", "endpoint_summary", "impact", "summary"):
        value = trace_profile.get(key)
        if isinstance(value, dict) and any(
            _safe_float(value.get(field)) > 0
            for field in ("p95_ms", "p99_ms", "latency_ms", "error_rate", "request_count", "slow_span_count")
        ):
            return True
    return False


def _has_local_io_behavior_evidence(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    summary = value.get("blocking_summary") if isinstance(value.get("blocking_summary"), dict) else {}
    if any(bool(summary.get(key)) for key in ("local_code_blocking", "missing_timeout", "unbounded_sync_io", "loop_internal_blocking")):
        return True
    for item in value.get("top_wait_stacks") or []:
        if not isinstance(item, dict):
            continue
        if any(bool(item.get(key)) for key in ("local_code_blocking", "missing_timeout", "unbounded_sync_io", "loop_internal_blocking")):
            return True
        text = " ".join(str(item.get(key) or "") for key in ("message", "reason", "diagnosis", "wait_reason")).lower()
        if any(token in text for token in ("missing timeout", "unbounded", "sync io", "blocking in loop", "large file")):
            return True
    return False


def _has_scenario_impact_signal(payload: dict[str, Any]) -> bool:
    impact = payload.get("impact") if isinstance(payload.get("impact"), dict) else {}
    if any(
        _safe_float(impact.get(field)) > 0
        for field in ("error_rate", "latency_ms", "p95_ms", "cpu_percent", "worker_failure_count", "blocked_ms", "request_count")
    ):
        return True
    if payload.get("symptom_impact") or payload.get("same_window_impact"):
        return True
    for key in ("impact_evidence_refs", "symptom_evidence_refs"):
        if isinstance(payload.get(key), list) and payload[key]:
            return True
    return False


def _has_retry_amplification_signal(payload: dict[str, Any]) -> bool:
    if payload.get("local_amplification") or payload.get("amplification_evidence"):
        return True
    if bool(payload.get("nested_retry")) and _safe_int(payload.get("attempt_count")) >= 3:
        return True
    impact = payload.get("impact") if isinstance(payload.get("impact"), dict) else {}
    return any(
        _safe_float(impact.get(field)) > 0
        for field in ("amplified_request_count", "retry_rate", "error_rate", "cpu_percent", "latency_ms")
    )


def _line_candidates_match_source(candidates: list[dict[str, Any]], source_state: dict[str, Any]) -> bool:
    if (
        not candidates
        or source_state.get("status") not in {"valid", "partial"}
        or not source_state.get("source_context_hash")
        or not source_state.get("revision")
    ):
        return False
    snippets = source_state.get("snippets") or []
    verified_lines = source_state.get("verified_source_lines") or []
    for candidate in candidates:
        candidate_file = str(candidate.get("file") or "").replace("\\", "/")
        candidate_line = _safe_int(candidate.get("line"))
        if not candidate_file or candidate_line <= 0:
            continue
        verified_items = list(verified_lines)
        for syntax_result in source_state.get("source_syntax") or []:
            if isinstance(syntax_result, dict):
                verified_items.extend(syntax_result.get("verified_source_lines") or [])
        if any(
            isinstance(item, dict)
            and _source_file_matches(candidate_file, str(item.get("file") or ""))
            and (
                _safe_int(item.get("verified_line") or item.get("line")) == candidate_line
                or (
                    _safe_int(
                        (item.get("source_span") or {}).get("start_line")
                        if isinstance(item.get("source_span"), dict)
                        else 0
                    )
                    <= candidate_line
                    <= _safe_int(
                        (item.get("source_span") or {}).get("end_line")
                        if isinstance(item.get("source_span"), dict)
                        else 0
                    )
                    and _safe_int(
                        (item.get("source_span") or {}).get("start_line")
                        if isinstance(item.get("source_span"), dict)
                        else 0
                    ) > 0
                )
            )
            for item in verified_items
        ):
            return True
        for snippet in snippets:
            snippet_file = str(snippet.get("file") or "").replace("\\", "/")
            snippet_line = _safe_int(snippet.get("focus_line"))
            if snippet_line == candidate_line and (
                snippet_file == candidate_file
                or snippet_file.endswith(f"/{candidate_file.lstrip('/')}")
                or candidate_file.endswith(f"/{snippet_file.lstrip('/')}")
            ):
                return True
    return False


def _source_file_matches(left: str, right: str) -> bool:
    left = str(left or "").replace("\\", "/").lstrip("/")
    right = str(right or "").replace("\\", "/").lstrip("/")
    return bool(left and right) and (
        left == right
        or left.endswith(f"/{right}")
        or right.endswith(f"/{left}")
    )


def _line_candidates_from_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        file = str(item.get("file") or "")
        line = _safe_int(item.get("line") or item.get("focus_line"))
        if not file and isinstance(item.get("stack"), list):
            for frame in reversed(item["stack"]):
                if _looks_like_runtime_frame(str(frame)):
                    continue
                candidate = _line_candidate_from_frame(str(frame))
                if candidate:
                    file = candidate["file"]
                    line = candidate["line"]
                    break
        if file and line > 0:
            candidates.append({
                "file": file,
                "line": line,
                "function": str(item.get("function") or item.get("name") or item.get("top_frame") or ""),
                "evidence_ref": item.get("evidence_ref"),
            })
    return candidates[:20]


def _runtime_line_candidates(
    *,
    depth: dict[str, Any],
    runtime_profile: Any,
    top_functions: list[dict[str, Any]],
    call_path_hotspots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    items.extend(top_functions)
    items.extend(call_path_hotspots)
    if isinstance(depth, dict):
        for key in ("line_candidates", "stack_samples", "call_path_hotspots"):
            value = depth.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
    if isinstance(runtime_profile, dict):
        for key in ("line_candidates", "stack_samples", "call_path_hotspots", "top_functions"):
            value = runtime_profile.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
    return _merge_line_candidates(_line_candidates_from_items(items))


def _merge_line_candidates(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            file = str(item.get("file") or "")
            line = _safe_int(item.get("line"))
            if not file or line <= 0:
                continue
            function = str(item.get("function") or item.get("symbol") or item.get("name") or "")
            key = (file.replace("\\", "/"), line, function)
            if key in seen:
                continue
            seen.add(key)
            merged.append({
                "file": file,
                "line": line,
                "function": function,
                "evidence_ref": item.get("evidence_ref"),
            })
    return merged[:20]


def _line_candidate_from_frame(frame: str) -> dict[str, Any] | None:
    traceback_match = re.search(r'File "([^"]+)", line (\d+), in ([^\s]+)', frame)
    if traceback_match:
        return {
            "file": traceback_match.group(1),
            "line": _safe_int(traceback_match.group(2)),
            "function": traceback_match.group(3),
        }
    colon_match = re.search(r"(.+):(\d+)(?::([^:]+))?$", frame)
    if colon_match:
        return {
            "file": colon_match.group(1),
            "line": _safe_int(colon_match.group(2)),
            "function": colon_match.group(3) or "",
        }
    return None


def _runtime_profile_is_primitive(top_function: dict[str, Any], quality: dict[str, Any]) -> bool:
    name = str(top_function.get("name") or top_function.get("function") or "").lower()
    if any(token in name for token in ("poll", "select", "epoll", "sleep", "futex", "pthread_cond_wait", "clock_nanosleep")):
        return True
    if str(quality.get("diagnostic_value") or "").lower() == "low":
        return True
    return (
        _safe_float(quality.get("primitive_frame_ratio")) >= 0.75
        or _safe_float(quality.get("framework_loop_ratio")) >= 0.75
    ) and _safe_float(quality.get("target_code_ratio")) < 0.2


def _looks_like_runtime_frame(frame: str) -> bool:
    text = frame.replace("\\", "/").lower()
    return any(
        token in text
        for token in (
            "/lib/python",
            "site-packages",
            "threading.py",
            "queue.py",
            "socket.py",
            "selectors.py",
            "asyncio/",
            "concurrent/futures",
        )
    )


def _has_baseline_or_impact_signal(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if _evidence_status(value) in {"valid", "partial"}:
        return True
    return bool(value.get("summary") or value.get("top_functions") or value.get("windows"))


def _python_scenario_counter_evidence(*, dependency_check: Any, redis_check: Any) -> dict[str, Any]:
    refs: list[str] = []
    has_failure = False
    if _failed_dependency_count(dependency_check) > 0:
        has_failure = True
        refs.append("evidence_index.dependency_check")
    if _redis_slowlog_entry_count(redis_check) > 0 or _redis_max_latency_ms(redis_check) > 0:
        has_failure = True
        refs.append("evidence_index.redis_check")
    return {"has_failure": has_failure, "evidence_refs": refs}


def _evidence_status(value: dict[str, Any]) -> str:
    validity = value.get("evidence_validity")
    if not isinstance(validity, dict):
        return ""
    return str(validity.get("evidence_status") or "")


def _has_wait_or_io_signal(ebpf_metrics: Any, off_cpu_wait: Any) -> bool:
    if isinstance(ebpf_metrics, dict) and bool(ebpf_metrics):
        return True
    if not isinstance(off_cpu_wait, dict):
        return False
    summary = off_cpu_wait.get("summary") if isinstance(off_cpu_wait.get("summary"), dict) else {}
    if _safe_int(summary.get("sample_count")) > 0:
        return True
    stacks = off_cpu_wait.get("top_wait_stacks")
    return isinstance(stacks, list) and bool(stacks)


def _failed_dependency_count(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    summary = value.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("failed_dependencies"), list):
        return len(summary["failed_dependencies"])
    checks = value.get("checks")
    if isinstance(checks, list):
        return sum(1 for item in checks if isinstance(item, dict) and item.get("success") is False)
    return 0


def _log_error_cluster_count(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    summary = value.get("summary")
    if isinstance(summary, dict):
        count = _safe_int(summary.get("error_cluster_count"))
        if count:
            return count
    clusters = value.get("error_clusters")
    return len(clusters) if isinstance(clusters, list) else 0


def _redis_slowlog_entry_count(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    slowlog = value.get("slowlog_summary")
    return _safe_int(slowlog.get("entry_count")) if isinstance(slowlog, dict) else 0


def _redis_max_latency_ms(value: Any) -> float:
    if not isinstance(value, dict):
        return 0.0
    latency = value.get("latency_summary")
    return _safe_float(latency.get("max_latency_ms")) if isinstance(latency, dict) else 0.0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
