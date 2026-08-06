"""Collector artifact to compact evidence structuring.

This layer is intentionally non-AI: it normalizes artifact outputs into
referenceable summaries, but it does not decide root cause.
"""

from __future__ import annotations

import html
import re
from typing import Any

from pydantic import BaseModel, Field


TOP_LIMIT = 10
HOTSPOT_LIMIT = 10


class StructuredEvidence(BaseModel):
    version: int = 1
    task_id: str
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    top_functions: list[dict[str, Any]] = Field(default_factory=list)
    stack_summary: dict[str, Any] = Field(default_factory=dict)
    call_path_hotspots: list[dict[str, Any]] = Field(default_factory=list)
    evidence_index: dict[str, Any] = Field(default_factory=dict)
    confidence_inputs: dict[str, Any] = Field(default_factory=dict)
    sys_metrics: dict[str, Any] | None = None
    ebpf_metrics: dict[str, Any] | None = None
    memory_json: dict[str, Any] | None = None


def structure_artifact_evidence(
    *,
    task_id: str,
    artifacts: list[dict[str, Any]],
    artifact_values: dict[str, Any] | None = None,
) -> StructuredEvidence:
    """Convert raw and semi-raw collector artifacts into compact evidence."""
    values = artifact_values or {}
    artifact_refs = _build_artifact_refs(task_id, artifacts)
    top_functions = _normalize_top_functions(values.get("top_json"))
    if not top_functions:
        top_functions = _top_from_flamegraph_tree(values.get("flamegraph_json"))
    if not top_functions:
        top_functions = _top_from_flamegraph_svg(values.get("flamegraph_svg"))

    depth = values.get("depth_evidence_json") if isinstance(values.get("depth_evidence_json"), dict) else {}
    stack_summary = _build_stack_summary(depth, top_functions, artifact_refs)
    call_path_hotspots = _build_call_path_hotspots(depth, top_functions)
    confidence_inputs = _build_confidence_inputs(
        top_functions=top_functions,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        artifact_refs=artifact_refs,
        sys_metrics=values.get("sys_metrics"),
        ebpf_metrics=values.get("ebpf_metrics"),
    )
    evidence_index = _build_evidence_index(
        artifact_refs=artifact_refs,
        depth=depth,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        confidence_inputs=confidence_inputs,
    )
    return StructuredEvidence(
        task_id=task_id,
        artifact_refs=artifact_refs,
        top_functions=top_functions,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        evidence_index=evidence_index,
        confidence_inputs=confidence_inputs,
        sys_metrics=values.get("sys_metrics") if isinstance(values.get("sys_metrics"), dict) else None,
        ebpf_metrics=values.get("ebpf_metrics") if isinstance(values.get("ebpf_metrics"), dict) else None,
        memory_json=values.get("memory_json") if isinstance(values.get("memory_json"), dict) else None,
    )


def rca_inputs_from_structured(structured: StructuredEvidence) -> dict[str, Any]:
    """Map structured evidence back to existing RCA input fields."""
    return {
        "top_functions": structured.top_functions,
        "sys_metrics": structured.sys_metrics,
        "ebpf_metrics": structured.ebpf_metrics,
        "evidence_index": structured.evidence_index,
    }


def _build_artifact_refs(task_id: str, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for artifact in sorted(artifacts, key=lambda item: (str(item.get("artifact_type", "")), str(item.get("filename", "")))):
        artifact_type = str(artifact.get("artifact_type") or "unknown")
        refs.append({
            "artifact_type": artifact_type,
            "filename": str(artifact.get("filename") or ""),
            "content_type": str(artifact.get("content_type") or ""),
            "size_bytes": int(artifact.get("size_bytes") or 0),
            "object_key": str(artifact.get("object_key") or ""),
            "local_path": str(artifact.get("local_path") or ""),
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
        if not name:
            continue
        samples = _safe_int(item.get("samples") or item.get("sample_count") or item.get("value"))
        percent = _safe_float(item.get("percent"))
        items.append({
            "name": name,
            "samples": samples,
            "percent": round(percent, 2),
        })
    items.sort(key=lambda item: (-float(item.get("percent") or 0.0), -int(item.get("samples") or 0), item["name"]))
    result = []
    for index, item in enumerate(items[:TOP_LIMIT]):
        result.append({**item, "evidence_ref": f"structured_evidence.top_functions[{index}]"})
    return result


def _top_from_flamegraph_tree(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    total = _safe_float(value.get("value"))
    counter: dict[str, int] = {}

    def walk(node: dict[str, Any], is_root: bool = False) -> None:
        name = str(node.get("name") or "").strip()
        samples = _safe_int(node.get("value"))
        if name and not is_root:
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
        if not name or name.lower() in {"all", "root"}:
            continue
        percent = _percent_from_text(title)
        samples = _samples_from_text(title)
        if percent <= 0.0 and samples <= 0:
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


def _build_stack_summary(
    depth: dict[str, Any],
    top_functions: list[dict[str, Any]],
    artifact_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    stack_samples = depth.get("stack_samples", []) if isinstance(depth, dict) else []
    first_sample = stack_samples[0] if isinstance(stack_samples, list) and stack_samples and isinstance(stack_samples[0], dict) else {}
    first_top = top_functions[0] if top_functions else {}
    hot_frame = str(first_sample.get("hot_frame") or first_top.get("name") or "")
    call_path = str(first_sample.get("call_path") or "")
    sample_count = _safe_int(first_sample.get("sample_count") or first_top.get("samples"))
    percent = _safe_float(first_sample.get("percent") or first_top.get("percent"))
    parse_status = "ok" if hot_frame or stack_samples or top_functions else "insufficient_structured_signal"
    return {
        "dominant_hot_frame": hot_frame,
        "dominant_percent": round(percent, 2),
        "sample_count": sample_count,
        "stack_sample_count": len(stack_samples) if isinstance(stack_samples, list) else 0,
        "has_call_path": bool(call_path or depth.get("call_path") or (depth.get("context") or {}).get("call_path")),
        "has_wait_reason": bool(first_sample.get("wait_reason") or depth.get("wait_reason")),
        "parse_status": parse_status,
        "raw_payload_policy": "references_only",
        "artifact_ref_count": len(artifact_refs),
        "evidence_ref": "structured_evidence.stack_summary",
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
            "evidence_ref": f"structured_evidence.call_path_hotspots[{index}]",
        })
    return hotspots


def _build_confidence_inputs(
    *,
    top_functions: list[dict[str, Any]],
    stack_summary: dict[str, Any],
    call_path_hotspots: list[dict[str, Any]],
    artifact_refs: list[dict[str, Any]],
    sys_metrics: Any,
    ebpf_metrics: Any,
) -> dict[str, Any]:
    first_top = top_functions[0] if top_functions else {}
    context_completeness = "high" if call_path_hotspots else "medium" if stack_summary.get("has_call_path") else "low"
    artifact_types = [item["artifact_type"] for item in artifact_refs]
    return {
        "sample_count": _safe_int(stack_summary.get("sample_count") or first_top.get("samples")),
        "dominant_percent": round(_safe_float(stack_summary.get("dominant_percent") or first_top.get("percent")), 2),
        "context_completeness": context_completeness,
        "has_system_pressure": isinstance(sys_metrics, dict) and bool(sys_metrics),
        "has_wait_or_io_signal": isinstance(ebpf_metrics, dict) and bool(ebpf_metrics),
        "parse_status": stack_summary.get("parse_status", "insufficient_structured_signal"),
        "artifact_types": artifact_types,
        "token_safety": "compact_summary_only",
    }


def _build_evidence_index(
    *,
    artifact_refs: list[dict[str, Any]],
    depth: dict[str, Any],
    stack_summary: dict[str, Any],
    call_path_hotspots: list[dict[str, Any]],
    confidence_inputs: dict[str, Any],
) -> dict[str, Any]:
    index = dict(depth) if isinstance(depth, dict) else {}
    index["artifact_refs"] = artifact_refs
    index["stack_summary"] = stack_summary
    index["call_path_hotspots"] = call_path_hotspots
    index["confidence_inputs"] = confidence_inputs
    return index


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
