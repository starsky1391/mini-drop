"""Shared collector invocation contract for diagnosis and watch-triggered tasks."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def build_collector_invocation(
    *,
    scope_source: str,
    collector_family: str,
    probe_id: str,
    target_config: dict[str, Any] | None,
    target_context: dict[str, Any],
    diagnosis_id: str | None = None,
    diagnosis_step_id: str | None = None,
    watch_id: str | None = None,
    trigger_event_id: str | None = None,
    evidence_cohort_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_source": scope_source,
        "collector_family": collector_family,
        "probe_id": probe_id,
        "target_config": target_config if isinstance(target_config, dict) else {},
        "target_context": target_context,
    }
    optional = {
        "diagnosis_id": diagnosis_id,
        "diagnosis_step_id": diagnosis_step_id,
        "watch_id": watch_id,
        "trigger_event_id": trigger_event_id,
        "evidence_cohort_id": evidence_cohort_id,
    }
    payload.update({key: value for key, value in optional.items() if value})
    return payload


def collector_request_fingerprint(invocation: dict[str, Any], options: dict[str, Any] | None = None) -> str:
    """Create a stable identity for one target-scoped collector request."""
    payload = {
        "collector_family": invocation.get("collector_family"),
        "probe_id": invocation.get("probe_id"),
        "target_config": invocation.get("target_config") or {},
        "target_context": invocation.get("target_context") or {},
        "evidence_window": _window_from_options(options or {}),
        "source_context": _source_context_from_invocation(invocation),
        "investigation_scope": _investigation_scope(options or {}),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def collector_capability_fingerprint(invocation: dict[str, Any]) -> str:
    """Identify a stable target/capability boundary without diagnosis window identity."""
    payload = {
        "collector_family": invocation.get("collector_family"),
        "probe_id": invocation.get("probe_id"),
        "target_config": invocation.get("target_config") or {},
        "target_context": invocation.get("target_context") or {},
        "source_context": _source_context_from_invocation(invocation),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _source_context_from_invocation(invocation: dict[str, Any]) -> dict[str, Any]:
    target_config = invocation.get("target_config")
    if not isinstance(target_config, dict):
        return {}
    context = target_config.get("source_context")
    return context if isinstance(context, dict) else {}


def _window_from_options(options: dict[str, Any]) -> dict[str, Any]:
    return {
        key: options.get(key)
        for key in (
            "trigger_event_id",
            "evidence_cohort_id",
            "collection_mode",
            "window_start",
            "window_end",
            "timing_relation",
            "duration_sec",
            "sample_rate",
        )
        if options.get(key) is not None
    }


def _investigation_scope(options: dict[str, Any]) -> dict[str, Any]:
    generated = options.get("ai_generated_query") if isinstance(options.get("ai_generated_query"), dict) else {}
    return {
        key: value
        for key, value in {
            "source_revision": options.get("source_revision") or options.get("repo_revision"),
            "line_candidates": options.get("line_candidates"),
            "codeql_query_hash": generated.get("query_spec_hash") or generated.get("query_hash"),
            "investigation_question": generated.get("investigation_question"),
            "object_type_hints": options.get("object_type_hints"),
            "pyheap_dump_path": options.get("pyheap_dump_path"),
            "codeql_sarif_path": options.get("codeql_sarif_path"),
        }.items()
        if value not in (None, "", [])
    }
