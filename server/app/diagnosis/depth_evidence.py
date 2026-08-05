"""深采集证据的标准化契约与摘要构建。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DepthStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DepthEvidenceContext(DepthStrictModel):
    task_id: str
    collector_kind: str
    target_pid: int = 0
    agent_id: str = ""
    service_id: str = ""
    instance_id: str = ""
    host_id: str = ""
    endpoint: str = ""
    trace_id: str = ""
    window_id: str = ""
    wait_reason: str = ""
    call_path: str = ""
    line_hint: str = ""
    context_id: str = ""
    collection_mode: Literal["stack_sampling", "trace_context", "baseline_window"] = "stack_sampling"


class DepthStackSample(DepthStrictModel):
    stack_id: str
    sample_count: int = Field(ge=0)
    percent: float = Field(ge=0.0, le=100.0)
    root_frame: str = ""
    hot_frame: str = ""
    call_path: str = ""
    stack_fragment: list[str] = Field(default_factory=list)
    wait_reason: str = ""
    context_id: str = ""
    endpoint: str = ""
    service_id: str = ""
    instance_id: str = ""
    line_hint: str = ""
    evidence_ref: str = ""


class DepthLineCandidate(DepthStrictModel):
    file: str = ""
    line: int = Field(default=0, ge=0)
    symbol: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_ref: str = ""


class DepthEvidenceSummary(DepthStrictModel):
    version: int = 1
    task_id: str
    collector_kind: str
    wait_reason: str = ""
    context_id: str = ""
    endpoint: str = ""
    service_id: str = ""
    instance_id: str = ""
    host_id: str = ""
    trace_id: str = ""
    call_path: str = ""
    localization_boundary: Literal["resource", "process", "thread", "syscall", "function", "call_path", "line"] = "function"
    stack_samples: list[DepthStackSample] = Field(default_factory=list)
    line_candidates: list[DepthLineCandidate] = Field(default_factory=list)
    top_frames: list[str] = Field(default_factory=list)
    raw_stack_ref: str = ""
    derived_artifact_ref: str = ""
    summary: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    context: DepthEvidenceContext


def build_depth_evidence_summary(
    *,
    task_id: str,
    collector_kind: str,
    collapsed_path: str | Path,
    top_functions: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    raw_stack_ref: str = "",
    derived_artifact_ref: str = "",
) -> dict[str, Any]:
    """从折叠栈生成标准化深采集摘要。"""
    collapsed = Path(collapsed_path)
    context_model = _normalize_context(task_id, collector_kind, context or {}, top_functions or [])
    entries = _read_collapsed_entries(collapsed)
    stack_samples = _build_stack_samples(entries, context_model)
    line_candidates = _build_line_candidates(stack_samples, context or {}, top_functions or [])
    top_frames = _build_top_frames(top_functions or [], stack_samples)
    boundary = _infer_boundary(context_model, stack_samples, line_candidates)
    summary = _build_summary(context_model, top_functions or [], stack_samples, boundary)
    evidence_refs = [sample.evidence_ref for sample in stack_samples if sample.evidence_ref]
    if raw_stack_ref:
        evidence_refs.append(raw_stack_ref)
    if derived_artifact_ref:
        evidence_refs.append(derived_artifact_ref)
    return DepthEvidenceSummary(
        task_id=task_id,
        collector_kind=collector_kind,
        wait_reason=context_model.wait_reason,
        context_id=context_model.context_id,
        endpoint=context_model.endpoint,
        service_id=context_model.service_id,
        instance_id=context_model.instance_id,
        host_id=context_model.host_id,
        trace_id=context_model.trace_id,
        call_path=context_model.call_path,
        localization_boundary=boundary,
        stack_samples=stack_samples,
        line_candidates=line_candidates,
        top_frames=top_frames,
        raw_stack_ref=raw_stack_ref,
        derived_artifact_ref=derived_artifact_ref,
        summary=summary,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
        context=context_model,
    ).model_dump(mode="json")


def _normalize_context(
    task_id: str,
    collector_kind: str,
    context: dict[str, Any],
    top_functions: list[dict[str, Any]],
) -> DepthEvidenceContext:
    endpoint = str(context.get("endpoint") or context.get("target_endpoint") or "")
    service_id = str(context.get("service_id") or context.get("target_service") or "")
    instance_id = str(context.get("instance_id") or context.get("target_instance_id") or "")
    host_id = str(context.get("host_id") or "")
    trace_id = str(context.get("trace_id") or context.get("span_id") or "")
    wait_reason = str(context.get("wait_reason") or _default_wait_reason(collector_kind))
    call_path = str(context.get("call_path") or "")
    line_hint = str(context.get("line_hint") or "")
    target_pid = int(context.get("target_pid") or 0)
    agent_id = str(context.get("agent_id") or "")
    window_id = str(context.get("window_id") or context.get("window_index") or "")
    context_id = str(context.get("context_id") or _build_context_id(
        task_id=task_id,
        collector_kind=collector_kind,
        endpoint=endpoint,
        service_id=service_id,
        instance_id=instance_id,
        target_pid=target_pid,
        window_id=window_id,
        trace_id=trace_id,
    ))
    if not call_path and top_functions:
        call_path = str(top_functions[0].get("call_path") or top_functions[0].get("stack") or "")
    collection_mode = "baseline_window" if "baseline" in collector_kind else "trace_context" if "trace" in collector_kind else "stack_sampling"
    return DepthEvidenceContext(
        task_id=task_id,
        collector_kind=collector_kind,
        target_pid=target_pid,
        agent_id=agent_id,
        service_id=service_id,
        instance_id=instance_id,
        host_id=host_id,
        endpoint=endpoint,
        trace_id=trace_id,
        window_id=window_id,
        wait_reason=wait_reason,
        call_path=call_path,
        line_hint=line_hint,
        context_id=context_id,
        collection_mode=collection_mode,
    )


def _read_collapsed_entries(collapsed: Path) -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    if not collapsed.is_file():
        return entries
    with collapsed.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if " " not in line:
                continue
            stack, _, count_str = line.rstrip().rpartition(" ")
            try:
                count = int(count_str)
            except ValueError:
                continue
            stack = stack.strip()
            if stack:
                entries.append((stack, count))
    entries.sort(key=lambda item: item[1], reverse=True)
    return entries


def _build_stack_samples(entries: list[tuple[str, int]], context: DepthEvidenceContext) -> list[DepthStackSample]:
    total = sum(count for _, count in entries) or 0
    samples: list[DepthStackSample] = []
    for index, (stack, count) in enumerate(entries[:10], start=1):
        frames = [item.strip() for item in stack.split(";") if item.strip()]
        if not frames:
            continue
        stack_id = hashlib.sha256(f"{context.context_id}:{stack}:{count}".encode("utf-8")).hexdigest()[:16]
        samples.append(DepthStackSample(
            stack_id=stack_id,
            sample_count=count,
            percent=round((count / total * 100.0), 2) if total else 0.0,
            root_frame=frames[0],
            hot_frame=frames[-1],
            call_path=";".join(frames),
            stack_fragment=_fragment_frames(frames),
            wait_reason=context.wait_reason,
            context_id=context.context_id,
            endpoint=context.endpoint,
            service_id=context.service_id,
            instance_id=context.instance_id,
            line_hint=context.line_hint,
            evidence_ref=f"depth:{context.task_id}:stack:{index}",
        ))
    return samples


def _build_line_candidates(
    stack_samples: list[DepthStackSample],
    context: dict[str, Any],
    top_functions: list[dict[str, Any]],
) -> list[DepthLineCandidate]:
    candidates: list[DepthLineCandidate] = []
    explicit = context.get("line_candidates")
    if isinstance(explicit, list):
        for index, item in enumerate(explicit[:10], start=1):
            if not isinstance(item, dict):
                continue
            candidates.append(DepthLineCandidate(
                file=str(item.get("file", "")),
                line=int(item.get("line", 0) or 0),
                symbol=str(item.get("symbol") or item.get("function") or ""),
                confidence=float(item.get("confidence", 0.0) or 0.0),
                evidence_ref=str(item.get("evidence_ref") or f"depth:line:{index}"),
            ))
    if candidates:
        return candidates

    top = top_functions[0] if top_functions else {}
    file_name = str(top.get("file") or top.get("source_file") or "")
    line = int(top.get("line") or top.get("source_line") or 0)
    if file_name and line > 0:
        candidates.append(DepthLineCandidate(
            file=file_name,
            line=line,
            symbol=str(top.get("name") or ""),
            confidence=0.8,
            evidence_ref=f"top_functions[0].{file_name}:{line}",
        ))
    elif stack_samples and context.get("line_hint"):
        candidates.append(DepthLineCandidate(
            file=str(context.get("line_hint")),
            line=0,
            symbol=stack_samples[0].hot_frame,
            confidence=0.45,
            evidence_ref=stack_samples[0].evidence_ref,
        ))
    return candidates


def _build_top_frames(top_functions: list[dict[str, Any]], stack_samples: list[DepthStackSample]) -> list[str]:
    frames: list[str] = []
    for item in top_functions[:5]:
        name = str(item.get("name") or "").strip()
        if name and name not in frames:
            frames.append(name)
    for sample in stack_samples[:5]:
        if sample.hot_frame and sample.hot_frame not in frames:
            frames.append(sample.hot_frame)
    return frames


def _infer_boundary(
    context: DepthEvidenceContext,
    stack_samples: list[DepthStackSample],
    line_candidates: list[DepthLineCandidate],
) -> Literal["resource", "process", "thread", "syscall", "function", "call_path", "line"]:
    if line_candidates:
        return "line"
    if context.call_path or any(sample.call_path for sample in stack_samples):
        return "call_path"
    if context.wait_reason:
        return "function"
    return "process"


def _build_summary(
    context: DepthEvidenceContext,
    top_functions: list[dict[str, Any]],
    stack_samples: list[DepthStackSample],
    boundary: str,
) -> str:
    top_name = str((top_functions[0] or {}).get("name", "unknown")) if top_functions else "unknown"
    if boundary == "line" and stack_samples:
        line_target = context.line_hint or (stack_samples[0].hot_frame or "unknown")
        return f"{context.collector_kind} 已采到可回连到代码行的深栈证据，当前可下钻到 {line_target}。"
    if boundary == "call_path" and stack_samples:
        return f"{context.collector_kind} 已采到栈片段与调用路径上下文，热点主函数是 {top_name}。"
    return f"{context.collector_kind} 已采到深采样证据，热点主函数是 {top_name}，等待原因是 {context.wait_reason or 'unknown'}。"


def _default_wait_reason(collector_kind: str) -> str:
    if "off_cpu" in collector_kind:
        return "off_cpu_wait"
    if "trace" in collector_kind:
        return "trace_endpoint_context"
    if "baseline" in collector_kind:
        return "baseline_window"
    return "stack_sampling"


def _build_context_id(
    *,
    task_id: str,
    collector_kind: str,
    endpoint: str,
    service_id: str,
    instance_id: str,
    target_pid: int,
    window_id: str,
    trace_id: str,
) -> str:
    parts = [
        task_id,
        collector_kind,
        endpoint or service_id or instance_id or str(target_pid or ""),
        window_id or trace_id or "single",
    ]
    return ":".join(part for part in parts if part)


def _fragment_frames(frames: list[str], limit: int = 6) -> list[str]:
    if len(frames) <= limit:
        return frames
    head = max(2, limit // 2)
    tail = max(1, limit - head - 1)
    return [*frames[:head], "...", *frames[-tail:]]

