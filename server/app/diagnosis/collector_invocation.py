"""Shared collector invocation contract for diagnosis and watch-triggered tasks."""

from __future__ import annotations

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
