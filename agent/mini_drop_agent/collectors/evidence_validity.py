"""Evidence validity is independent from collector process completion."""

from __future__ import annotations

from typing import Any


VALID_EVIDENCE_STATUSES = {"valid", "partial"}


def evidence_state(
    status: str,
    *,
    execution_status: str = "completed",
    artifact_status: str = "produced",
    reason: str = "",
) -> dict[str, str]:
    return {
        "execution_status": execution_status,
        "artifact_status": artifact_status,
        "evidence_status": status,
        "reason": reason,
    }


def off_cpu_evidence_state(payload: dict[str, Any]) -> dict[str, str]:
    status = str(payload.get("collector_status") or "")
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    events = payload.get("event_summary") if isinstance(payload.get("event_summary"), dict) else {}
    sample_count = _integer(summary.get("sample_count"))
    event_count = _integer(events.get("observed_wait_events"))
    if status == "blocked":
        return evidence_state("blocked", reason=str(payload.get("parser_status") or "collector blocked"))
    if status == "target_exit":
        return evidence_state("target_exit", reason="target exited before collection")
    if sample_count > 0:
        return evidence_state("valid")
    if event_count > 0:
        return evidence_state("partial", reason="wait events were observed without usable stacks")
    return evidence_state("empty_window", reason="no wait events or stacks were observed")


def trace_evidence_state(payload: dict[str, Any]) -> dict[str, str]:
    correlation = payload.get("correlation_status") if isinstance(payload.get("correlation_status"), dict) else {}
    if str(correlation.get("status") or "") == "blocked":
        return evidence_state("blocked", reason=str(correlation.get("blocked_reason") or "trace collection blocked"))
    has_trace = bool(payload.get("endpoint_bindings") or payload.get("call_path_hotspots"))
    has_stack = bool(payload.get("top_functions"))
    if has_trace and has_stack:
        return evidence_state("valid")
    if has_trace or has_stack:
        return evidence_state("partial", reason="only stack or trace context was available")
    return evidence_state("empty_window", reason="no stack, endpoint, span, or call path was observed")


def baseline_evidence_state(payload: dict[str, Any]) -> dict[str, str]:
    structured = _integer(payload.get("structured_window_count"))
    raw = _integer(payload.get("raw_window_count"))
    if structured > 0:
        return evidence_state("valid")
    if raw > 0:
        return evidence_state("unparseable", reason="raw windows exist but none produced a structured stack")
    return evidence_state("empty_window", reason="no baseline windows produced evidence")


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
