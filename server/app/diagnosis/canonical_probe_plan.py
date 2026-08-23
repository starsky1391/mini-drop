"""Canonical follow-up probe family and input merging."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ProbeInputMerge:
    inputs: dict[str, dict[str, Any]]
    conflicts: list[dict[str, Any]]


def union_probe_families(*families: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    for values in families:
        for value in values or ():
            family = str(value or "").strip()
            if family and family not in result:
                result.append(family)
    return result


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _merge_mapping(
    existing: dict[str, Any],
    incoming: Mapping[str, Any],
    *,
    family: str,
    path: str,
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(existing)
    protected = {"candidate_id", "origin_parent_candidate_id"}
    for key, incoming_value in incoming.items():
        field_path = f"{path}.{key}" if path else str(key)
        existing_value = result.get(key)
        if isinstance(existing_value, dict) and isinstance(incoming_value, Mapping):
            result[key] = _merge_mapping(
                existing_value,
                incoming_value,
                family=family,
                path=field_path,
                conflicts=conflicts,
            )
            continue
        if not _non_empty(incoming_value):
            continue
        if not _non_empty(existing_value) or existing_value == incoming_value:
            result[key] = deepcopy(incoming_value)
            continue
        if key in protected or key == "query_spec_hash":
            conflicts.append({
                "evidence_family": family,
                "field": field_path,
                "existing": existing_value,
                "incoming": incoming_value,
                "status": "probe_input_conflict",
            })
            continue
        result[key] = deepcopy(incoming_value)
    return result


def merge_probe_input_maps(
    *maps: Mapping[str, Any] | None,
    allowed_families: Iterable[str] | None = None,
) -> ProbeInputMerge:
    allowed = {str(value) for value in allowed_families or () if str(value)}
    merged: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for source in maps:
        if not isinstance(source, Mapping):
            continue
        for raw_family, value in source.items():
            family = str(raw_family or "").strip()
            if not family or not isinstance(value, Mapping):
                continue
            if allowed and family not in allowed:
                conflicts.append({
                    "evidence_family": family,
                    "status": "unknown_probe_family",
                })
                continue
            if family not in merged:
                merged[family] = deepcopy(dict(value))
                continue
            merged[family] = _merge_mapping(
                merged[family],
                value,
                family=family,
                path="",
                conflicts=conflicts,
            )
    return ProbeInputMerge(inputs=merged, conflicts=conflicts)


def normalize_probe_plan(
    families: Iterable[str],
    inputs: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for family in union_probe_families(families):
        probe_input = dict(inputs.get(family) or {})
        query = probe_input.get("ai_generated_query") if isinstance(probe_input.get("ai_generated_query"), dict) else {}
        target_scope_hash = str(probe_input.get("target_scope_hash") or _stable_hash(
            probe_input.get("target_scope") or probe_input.get("target")
        ))
        evidence_window_hash = str(probe_input.get("evidence_window_hash") or _stable_hash(
            probe_input.get("evidence_window") or probe_input.get("window")
        ))
        key = (
            family,
            str(probe_input.get("candidate_id") or query.get("candidate_id") or ""),
            str(probe_input.get("origin_parent_candidate_id") or query.get("origin_parent_candidate_id") or ""),
            str(query.get("query_spec_hash") or ""),
            target_scope_hash,
            evidence_window_hash,
        )
        if key in seen:
            continue
        seen.add(key)
        plan.append({
            "evidence_family": family,
            "candidate_id": key[1],
            "origin_parent_candidate_id": key[2],
            "query_spec_hash": key[3],
            "target_scope_hash": target_scope_hash,
            "evidence_window_hash": evidence_window_hash,
            "probe_input": probe_input,
        })
    return plan


def _stable_hash(value: Any) -> str:
    if not _non_empty(value):
        return ""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
