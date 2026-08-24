"""DeepSeek API 客户端。

工程校验层：LLM JSON 响应的格式校验 → 证据引用完整性 → 自修复重试。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
from typing import Any

from server.app.ai_provider import chat_completions, get_ai_settings, is_feature_enabled
from server.app.diagnosis.codeql_query_guard import validate_ai_generated_codeql_query
from server.app.logging_utils import log_event
from server.app.rca.models import (
    AITreeProbeEdge,
    CauseEntry,
    ControlledAITree,
    DiagnosisReport,
    EvidenceAttributionResult,
    EvidenceInput,
    RootCauseCluster,
    SessionConclusionReview,
    ValidatedReport,
)
from server.app.diagnosis.canonical_claim_lineage import apply_ai_claim_update
from server.app.rca.controlled_tree import enforce_conclusion_eligibility
from server.app.rca.prompt import build_system_prompt, build_user_message


# 最大自修复重试次数
MAX_RETRIES = 2


def _redact_excerpt(value: str, limit: int = 600) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    return text[:limit]


def _validation_diagnostic(
    *,
    stage: str,
    attempt: int,
    error: str,
    raw: str,
    candidate_id: str = "",
    candidate_count: int,
    valid_evidence_ref_count: int,
    known_candidate_ids: set[str] | None = None,
    initial_evidence_refs: set[str] | None = None,
    initial_evidence_families: dict[str, str] | None = None,
    initial_evidence_statuses: dict[str, str] | None = None,
    candidate_evidence_refs: list[str] | None = None,
    candidate_parent_candidate_ids: list[str] | None = None,
    candidate_origin_parent_candidate_id: str = "",
    candidate_supported_level: str = "",
    candidate_entry_level: str = "",
    candidate_parent_supported_level: str = "",
) -> dict:
    """Return a bounded explanation of a rejected model response."""
    message = str(error or "")
    failure_code = "validation_error"
    failure_path = ""
    patterns = (
        ("candidate decision 非法", "invalid_decision", "candidates[].decision"),
        ("candidate causal_status 非法", "invalid_causal_status", "candidates[].causal_status"),
        ("candidate supported_level 非法", "invalid_supported_level", "candidates[].supported_level"),
        ("candidate 层级跃迁非法", "candidate_level_jump", "candidates[].supported_level"),
        ("candidate role 非法", "invalid_role", "candidates[].role"),
        ("candidate relation 非法", "invalid_relation", "candidates[].relation"),
        ("candidate_id 非法或重复", "invalid_candidate_id", "candidates[].candidate_id"),
        ("candidate evidence_refs 不真实", "invalid_evidence_ref", "candidates[].evidence_refs"),
        ("candidate parent_candidate_ids 不真实", "invalid_parent", "candidates[].parent_candidate_ids"),
        ("candidate parent_candidate_ids 缺少显式来源父节点", "missing_parent_provenance", "candidates[].parent_candidate_ids"),
        ("probe_requests 包含未注册证据族", "invalid_probe_request", "probe_requests"),
        ("probe_requests 未绑定 active candidate", "unbound_probe_request", "probe_requests"),
        ("candidates 必须是最多四个候选的数组", "invalid_candidates", "candidates"),
    )
    for marker, code, path in patterns:
        if marker in message:
            failure_code = code
            failure_path = path
            break
    raw_text = _redact_excerpt(raw)
    actual_value = message.split(":", 1)[1].strip() if ":" in message else message
    initial_refs = set(initial_evidence_refs or set())
    candidate_refs = sorted(set(candidate_evidence_refs or []))
    candidate_parents = sorted(set(candidate_parent_candidate_ids or []))
    return {
        "stage": stage,
        "attempt": attempt,
        "candidate_id": candidate_id,
        "failure_code": failure_code,
        "failure_path": failure_path,
        "expected_values": {
            "decision": [
                "needs_more_evidence",
                "continue_probe",
                "conclude",
                "reject",
                "reject_candidate",
                "backtrack",
                "abstain",
            ],
            "causal_status": [
                "supported",
                "needs_more_evidence",
                "unproven",
                "inconclusive",
                "contradicted",
                "rejected",
            ],
        } if failure_code in {"invalid_decision", "invalid_causal_status"} else [],
        "actual_value": _redact_excerpt(actual_value, 240),
        "candidate_count": candidate_count,
        "valid_evidence_ref_count": valid_evidence_ref_count,
        "known_candidate_ids": sorted(known_candidate_ids or set())[:32],
        "initial_evidence_refs": sorted(initial_refs)[:256],
        "initial_evidence_families": dict(sorted((initial_evidence_families or {}).items())[:128]),
        "initial_evidence_statuses": dict(sorted((initial_evidence_statuses or {}).items())[:128]),
        "candidate_evidence_refs": candidate_refs[:128],
        "missing_initial_evidence_refs": [
            ref for ref in candidate_refs if ref not in initial_refs
        ][:128],
        "candidate_parent_candidate_ids": candidate_parents[:64],
        "candidate_origin_parent_candidate_id": str(candidate_origin_parent_candidate_id or ""),
        "candidate_supported_level": str(candidate_supported_level or ""),
        "candidate_entry_level": str(candidate_entry_level or ""),
        "candidate_parent_supported_level": str(candidate_parent_supported_level or ""),
        "missing_parent_candidate_ids": [
            parent_id
            for parent_id in candidate_parents
            if parent_id not in (known_candidate_ids or set())
        ][:64],
        "raw_response_hash": hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest() if raw_text else "",
        "raw_response_excerpt": raw_text,
    }


def _initial_evidence_context(evidence_catalog: list[dict], valid_refs: set[str]) -> dict:
    """Keep a bounded, answer-free snapshot of the evidence available to AI."""
    families: dict[str, str] = {}
    statuses: dict[str, str] = {}
    snapshots: dict[str, dict[str, str]] = {}
    windows: dict[str, dict[str, str]] = {}
    for item in evidence_catalog:
        if not isinstance(item, dict):
            continue
        ref = next(
            (
                str(item.get(key) or "")
                for key in ("evidence_id", "evidence_ref", "raw_artifact_ref", "derived_artifact_ref")
                if item.get(key)
            ),
            "",
        )
        if not ref:
            continue
        observed = item.get("observed_value") if isinstance(item.get("observed_value"), dict) else {}
        summary = observed.get("summary") if isinstance(observed.get("summary"), dict) else {}
        families[ref] = str(
            item.get("query_or_probe")
            or observed.get("collector_type")
            or summary.get("collector_family")
            or "unknown"
        )
        validity = summary.get("confidence_inputs")
        validity = validity if isinstance(validity, dict) else {}
        family_statuses = validity.get("evidence_validity_by_family")
        if isinstance(family_statuses, dict) and family_statuses:
            statuses[ref] = ",".join(
                f"{key}:{value}"
                for key, value in sorted(family_statuses.items())
                if value
            )
        else:
            quality = item.get("data_quality")
            statuses[ref] = str(
                (quality or {}).get("completeness")
                if isinstance(quality, dict)
                else ""
            ) or "unknown"
        observed_excerpt = _redact_excerpt(
            json.dumps(item.get("observed_value") or {}, ensure_ascii=False, sort_keys=True, default=str),
            500,
        )
        observed_window = observed.get("evidence_window")
        if not isinstance(observed_window, dict):
            observed_window = summary.get("evidence_window")
        if isinstance(observed_window, dict):
            windows[ref] = {
                key: str(observed_window[key])
                for key in (
                    "collection_mode",
                    "timing_relation",
                    "window_start",
                    "window_end",
                    "evidence_cohort_id",
                )
                if observed_window.get(key) is not None
            }
        snapshots[ref] = {
            "family": families[ref],
            "status": statuses[ref],
            "observed_excerpt": observed_excerpt,
        }
    return {
        "evidence_refs": sorted(valid_refs)[:256],
        "evidence_families": dict(sorted(families.items())[:128]),
        "evidence_statuses": dict(sorted(statuses.items())[:128]),
        "evidence_snapshots": dict(sorted(snapshots.items())[:128]),
        "evidence_windows": dict(sorted(windows.items())[:128]),
    }


def _candidate_generation_attempt_record(
    *,
    attempt: int,
    raw: str,
    parsed_candidate_count: int,
    accepted_candidate_count: int,
    validation_diagnostic_count: int,
    status: str,
) -> dict:
    redacted = _redact_excerpt(raw, 1200)
    return {
        "attempt": attempt,
        "status": status,
        "response_hash": hashlib.sha256(
            redacted.encode("utf-8", errors="replace")
        ).hexdigest() if redacted else "",
        "response_excerpt": redacted,
        "parsed_candidate_count": parsed_candidate_count,
        "accepted_candidate_count": accepted_candidate_count,
        "rejected_candidate_count": max(0, parsed_candidate_count - accepted_candidate_count),
        "validation_diagnostic_count": validation_diagnostic_count,
    }


def _candidate_selection_diagnostics(
    proposals: list[dict],
    active_candidate_ids: list[str],
    deferred_candidate_ids: list[str],
) -> list[dict]:
    """Explain investigation scheduling without changing conclusion eligibility."""
    active = {str(value) for value in active_candidate_ids if str(value)}
    deferred = {str(value) for value in deferred_candidate_ids if str(value)}
    result: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(proposals, start=1):
        candidate_id = str(item.get("candidate_id") or "")
        if not candidate_id:
            continue
        key = (
            str(item.get("mechanism") or "").strip(),
            str(item.get("target") or "").strip(),
        )
        duplicate = key in seen_keys
        seen_keys.add(key)
        if candidate_id in active:
            selection = "active"
            reason = "按首轮返回顺序、证据数量、可探测性和机制/目标去重后进入深探。"
        elif candidate_id in deferred:
            selection = "deferred"
            reason = "保留为 AI 调查方向，但本轮深探预算只选择少数候选。"
        else:
            selection = "not_selected"
            reason = "候选没有进入本轮 active 调查集合。"
        if duplicate:
            reason = "机制和目标与更早候选重复，本轮不重复调度。"
            selection = "deferred"
        result.append({
            "candidate_id": candidate_id,
            "source_order": index,
            "selection": selection,
            "reason": reason,
            "probeable": bool(item.get("probe_requests") or item.get("missing_evidence")),
            "evidence_ref_count": len(item.get("evidence_refs") or []),
            "mechanism": str(item.get("mechanism") or "")[:240],
            "target": str(item.get("target") or "")[:240],
        })
    return result


def _active_candidate_ids(proposals: list[dict], max_active: int = 3) -> tuple[list[str], list[str]]:
    """Choose investigation directions without granting conclusion eligibility."""
    ranked: list[tuple[tuple[int, int, int], str, dict]] = []
    seen_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(proposals):
        candidate_id = str(item.get("candidate_id") or "")
        if not candidate_id:
            continue
        if str(item.get("decision") or "") in {"reject", "reject_candidate", "abstain"} or str(item.get("causal_status") or "") in {"rejected", "contradicted"}:
            continue
        key = (str(item.get("mechanism") or "").strip(), str(item.get("target") or "").strip())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        probeable = bool(item.get("probe_requests") or item.get("missing_evidence"))
        score = (-len(item.get("evidence_refs") or []), 0 if probeable else 1, index)
        ranked.append((score, candidate_id, item))
    ranked.sort(key=lambda value: value[0])
    active = [candidate_id for _, candidate_id, _ in ranked[:max_active]]
    active_set = set(active)
    deferred = [
        str(item.get("candidate_id"))
        for item in proposals
        if str(item.get("candidate_id") or "") and str(item.get("candidate_id")) not in active_set
    ]
    return active, deferred


def _candidate_probe_inputs(
    proposals: list[dict],
    active_candidate_ids: list[str],
) -> dict[str, dict[str, str]]:
    """Bind each selected evidence family to the candidate that requested it."""
    active = {str(value) for value in active_candidate_ids if str(value)}
    by_family: dict[str, dict[str, str]] = {}
    for item in proposals:
        candidate_id = str(item.get("candidate_id") or "").strip()
        if not candidate_id or candidate_id not in active:
            continue
        origin = str(item.get("origin_parent_candidate_id") or "").strip()
        if not origin:
            parents = [str(value) for value in item.get("parent_candidate_ids", []) if str(value)]
            origin = _single_explicit_parent(parents)
        if not origin:
            continue
        specs = {
            str(spec.get("evidence_family") or ""): spec
            for spec in item.get("probe_request_specs", [])
            if isinstance(spec, dict) and spec.get("evidence_family")
        }
        for family in item.get("probe_requests", []):
            family = str(family or "").strip()
            if family and family not in by_family:
                spec = specs.get(family, {})
                by_family[family] = {
                    "candidate_id": candidate_id,
                    "origin_parent_candidate_id": origin,
                    **{
                        key: spec[key]
                        for key in (
                            "question",
                            "why_needed",
                            "input_refs",
                            "expected_observation",
                            "disconfirming_observation",
                        )
                        if spec.get(key)
                    },
                }
    return by_family


def _normalize_probe_requests(value: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Normalize legacy strings and the new structured probe request contract."""
    if not isinstance(value, list):
        return [], []
    families: list[str] = []
    specs: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            family = item.strip()
            spec = {
                "evidence_family": family,
                "_structured": False,
                "question": "",
                "why_needed": "",
                "input_refs": [],
                "expected_observation": [],
                "disconfirming_observation": [],
            }
        elif isinstance(item, dict):
            family = str(item.get("evidence_family") or "").strip()
            spec = {
                "evidence_family": family,
                "_structured": True,
                "question": str(item.get("question") or "").strip()[:500],
                "why_needed": str(item.get("why_needed") or "").strip()[:500],
                "input_refs": [
                    str(ref) for ref in item.get("input_refs", [])
                    if str(ref)
                ][:32],
                "expected_observation": _normalize_probe_observations(item.get("expected_observation")),
                "disconfirming_observation": _normalize_probe_observations(item.get("disconfirming_observation")),
            }
        else:
            continue
        if not family or family in families:
            continue
        families.append(family)
        specs.append(spec)
    return families, specs


def _filter_probe_request_specs(
    families: list[str],
    specs: list[dict[str, Any]],
    valid_refs: set[str],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop only invalid probe requests; keep valid sibling requests/candidates."""
    accepted_families: list[str] = []
    accepted_specs: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    specs_by_family = {str(item.get("evidence_family") or ""): item for item in specs}
    for family in families:
        spec = specs_by_family.get(family) or {
            "evidence_family": family,
            "_structured": False,
            "input_refs": [],
        }
        refs = [str(ref) for ref in spec.get("input_refs", []) if str(ref)]
        invalid_refs = [ref for ref in refs if ref not in valid_refs]
        missing_fields = [
            field
            for field in ("question", "why_needed", "expected_observation", "disconfirming_observation")
            if not spec.get(field)
        ]
        if spec.get("_structured") and (not refs or invalid_refs or missing_fields):
            rejected.append({
                "evidence_family": family,
                "invalid_input_refs": invalid_refs,
                "missing_fields": missing_fields,
                "reason": (
                    "结构化 probe request 必须引用当前会话真实 evidence ref。"
                    if not refs else
                    "结构化 probe request 引用了当前会话不存在的 evidence ref。"
                    if invalid_refs else
                    f"结构化 probe request 缺少字段: {', '.join(missing_fields)}。"
                ),
            })
            continue
        accepted_families.append(family)
        accepted_specs.append(spec)
    return accepted_families, accepted_specs, rejected


def _normalize_probe_observations(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [str(item).strip()[:300] for item in values if str(item).strip()][:8]


def _normalize_initial_candidate_decision(value: Any) -> str:
    """Normalize common model vocabulary without granting conclusion eligibility."""
    decision = str(value or "").strip().lower()
    if decision in {"needs_more_evidence", "continue_probe", "backtrack", "abstain", "unknown"}:
        return "needs_more_evidence"
    if decision in {"reject", "reject_candidate", "rejected"}:
        return "reject"
    if decision in {"conclude", "supported", "approve"}:
        return "conclude"
    return ""


def _normalize_initial_candidate_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"needs_more_evidence", "unproven", "inconclusive", "unknown"}:
        return "needs_more_evidence"
    if status in {"supported", "conclude"}:
        return "supported"
    if status in {"contradicted", "rejected"}:
        return "rejected"
    return ""


def _single_explicit_parent(parent_ids: list[str]) -> str:
    unique = list(dict.fromkeys(str(value) for value in parent_ids if str(value)))
    return unique[0] if len(unique) == 1 else ""


def _initial_entry_boundary(
    fact_context: dict,
    session_tree: ControlledAITree | None,
) -> str:
    boundary = fact_context.get("localization_boundary")
    if isinstance(boundary, dict) and boundary.get("level"):
        return str(boundary["level"]).strip()
    return str(session_tree.final_supported_level if session_tree else "resource")


def generate_session_candidate_review(
    *,
    diagnosis_id: str,
    fact_context: dict,
    session_tree: ControlledAITree | None,
    evidence_catalog: list[dict],
    probe_manifest: dict,
    model_name: str | None = None,
    max_attempts: int = 1,
) -> dict:
    """Ask AI to create initial, falsifiable candidates from Analyzer facts."""
    settings = get_ai_settings()
    model_name = model_name or settings.model
    base = {
        "ai_review_scope": "candidate_generation",
        "ai_review_model": model_name,
        "candidate_proposals": [],
        "selected_evidence_families": [],
        "probe_inputs": {},
        "validation_diagnostics": [],
        "candidate_generation_attempts": [],
    }
    valid_refs = {
        str(item.get(key) or "")
        for item in evidence_catalog
        if isinstance(item, dict)
        for key in ("evidence_id", "evidence_ref", "raw_artifact_ref", "derived_artifact_ref")
        if item.get(key)
    }
    initial_evidence_context = _initial_evidence_context(evidence_catalog, valid_refs)
    base["initial_evidence_context"] = initial_evidence_context
    if not is_feature_enabled("rca"):
        return {
            **base,
            "ai_review_status": "fallback",
            "ai_review_attempts": 0,
            "ai_review_error": "AI RCA is disabled or no API key is configured",
        }
    known_candidate_ids = {
        node.candidate_id
        for layer in session_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
        if node.node_type not in {
            "orphan",
            "observation",
            "mechanism_explanation",
            "stop_boundary",
            "evidence_gap",
        }
    } if session_tree else set()
    emitted_coarse_ids = (
        list(dict.fromkeys(
            str(value)
            for value in (session_tree.emitted_coarse_ids if session_tree else [])
            if str(value)
        ))
        if session_tree
        else []
    )
    if not emitted_coarse_ids and session_tree:
        emitted_coarse_ids = [
            node.candidate_id
            for layer in session_tree.layers
            for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
            if node.node_type in {"cluster_root", "coarse_candidate"}
        ]
    payload = {
        "diagnosis_id": diagnosis_id,
        "fact_context": fact_context,
        "current_ai_tree": session_tree.model_dump(mode="json") if session_tree else None,
        "allowed_parent_candidate_ids": sorted(known_candidate_ids),
        "valid_evidence_refs": sorted(valid_refs),
        "probe_manifest": probe_manifest,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Mini-Drop 首轮候选生成器，只输出 JSON。Analyzer 只提供事实、观察、定位边界和未证实提示；"
                "你必须生成可证伪的 AI 候选，不能把 Analyzer hint 直接当成根因。"
                "候选字段为 candidate_id、claim、mechanism、target、supported_level、decision、causal_status、"
                "evidence_refs、missing_evidence、parent_candidate_ids、origin_parent_candidate_id、probe_requests、role。"
                "candidate_id 必须以 ai_candidate_ 开头；evidence_refs 只能使用 valid_evidence_refs；"
                "parent_candidate_ids 只能逐字选择 allowed_parent_candidate_ids 中的 canonical 基础节点；"
                "orphan、observation、mechanism、boundary 节点不能作为首轮候选父节点；"
                "origin_parent_candidate_id 必须是其中唯一来源父节点（只有一个父节点时可直接使用该节点）；"
                "首轮候选 supported_level 不得超过 Analyzer 当前 localization_boundary.level；"
                "首轮候选不得直接生成 line，line 必须由 runtime file:line 和 source_snapshot 验证后产生；"
                "probe_requests 可以是旧字符串或结构化对象；结构化对象必须包含 evidence_family、question、why_needed、input_refs、expected_observation、disconfirming_observation。"
                "evidence_family 只能使用 probe_manifest 中注册的 evidence_family；role 只能是 primary、secondary、unknown 或 rejected。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)},
    ]
    attempt_limit = max(1, min(int(max_attempts), MAX_RETRIES + 1))
    last_error = ""
    validation_diagnostics = []
    candidate_generation_attempts = []
    for attempt in range(1, attempt_limit + 1):
        raw = ""
        current_item: dict | None = None
        diagnostic_count_before = len(validation_diagnostics)
        try:
            raw = _call_deepseek(messages, model_name)
            data = json.loads(_extract_json(raw) or "{}")
            candidates = data.get("candidates")
            if not isinstance(candidates, list) or len(candidates) > 4:
                raise ValueError("candidates 必须是最多四个候选的数组")
            normalized = []
            seen = set()
            for item in candidates:
                current_item = item if isinstance(item, dict) else None
                try:
                    if not isinstance(item, dict):
                        raise ValueError("candidate 不是对象")
                    candidate_id = str(item.get("candidate_id") or "")
                    if not re.fullmatch(r"ai_candidate_[a-zA-Z0-9_-]{1,80}", candidate_id) or candidate_id in seen:
                        raise ValueError(f"candidate_id 非法或重复: {candidate_id}")
                    refs = [str(ref) for ref in item.get("evidence_refs", []) if str(ref)]
                    if any(ref not in valid_refs for ref in refs):
                        raise ValueError("candidate evidence_refs 不真实")
                    parent_ids = [str(value) for value in item.get("parent_candidate_ids", []) if str(value)]
                    if any(parent_id not in known_candidate_ids for parent_id in parent_ids):
                        raise ValueError("candidate parent_candidate_ids 不真实")
                    relation = str(item.get("relation") or "").strip()
                    if not parent_ids and relation not in {"", "root"}:
                        raise ValueError("candidate parent_candidate_ids 缺少显式来源父节点")
                    if not parent_ids and relation in {"", "root"}:
                        if len(emitted_coarse_ids) == 1:
                            parent_ids = [emitted_coarse_ids[0]]
                            relation = "refinement"
                        elif emitted_coarse_ids:
                            raise ValueError("candidate root 父节点对应多个 emitted coarse，无法确定来源")
                        else:
                            raise ValueError("candidate root 没有可用的 emitted coarse 父节点")
                    decision = _normalize_initial_candidate_decision(item.get("decision"))
                    causal_status = _normalize_initial_candidate_status(item.get("causal_status"))
                    if not decision:
                        raise ValueError(f"candidate decision 非法: {item.get('decision')}")
                    if not causal_status:
                        raise ValueError(f"candidate causal_status 非法: {item.get('causal_status')}")
                    supported_level = str(item.get("supported_level") or "resource")
                    if supported_level not in {"resource", "host", "process", "thread", "syscall", "dependency", "service", "endpoint", "function", "call_path", "line"}:
                        raise ValueError(f"candidate supported_level 非法: {supported_level}")
                    entry_boundary = _initial_entry_boundary(fact_context, session_tree)
                    if supported_level == "line" or _level_order(supported_level) > _level_order(entry_boundary):
                        raise ValueError(
                            "candidate 层级跃迁非法: "
                            f"首轮候选 {supported_level} 超过 Analyzer 入口边界 {entry_boundary}"
                        )
                    role = str(item.get("role") or "unknown")
                    if role not in {"primary", "secondary", "unknown", "rejected"}:
                        raise ValueError(f"candidate role 非法: {role}")
                    if relation and relation not in {"root", "alternative", "refinement", "causal_convergence", "shared_evidence"}:
                        raise ValueError(f"candidate relation 非法: {relation}")
                    origin_parent = str(item.get("origin_parent_candidate_id") or "").strip()
                    if origin_parent and origin_parent not in parent_ids:
                        raise ValueError("candidate origin_parent_candidate_id 不属于 parent_candidate_ids")
                    if not origin_parent:
                        origin_parent = _single_explicit_parent(parent_ids)
                    if len(parent_ids) > 1 and not origin_parent:
                        raise ValueError("candidate origin_parent_candidate_id 缺少显式来源父节点")
                    for field in ("claim", "mechanism", "target"):
                        if not str(item.get(field) or "").strip():
                            raise ValueError(f"candidate 缺少 {field}")
                    probe_requests, probe_request_specs = _normalize_probe_requests(item.get("probe_requests"))
                    probe_requests, probe_request_specs, rejected_probe_specs = _filter_probe_request_specs(
                        probe_requests,
                        probe_request_specs,
                        valid_refs,
                    )
                    for rejected_spec in rejected_probe_specs:
                        validation_diagnostics.append(_validation_diagnostic(
                            stage="candidate_generation",
                            attempt=attempt,
                            error=str(rejected_spec.get("reason") or "probe request evidence ref 无效"),
                            raw=raw,
                            candidate_id=candidate_id,
                            candidate_count=len(candidates),
                            valid_evidence_ref_count=len(valid_refs),
                            known_candidate_ids=known_candidate_ids,
                            initial_evidence_refs=valid_refs,
                            initial_evidence_families=initial_evidence_context["evidence_families"],
                            initial_evidence_statuses=initial_evidence_context["evidence_statuses"],
                            candidate_evidence_refs=refs,
                            candidate_parent_candidate_ids=parent_ids,
                        ))
                    normalized.append({
                        "candidate_id": candidate_id,
                        "claim": str(item["claim"]).strip(),
                        "mechanism": str(item["mechanism"]).strip(),
                        "target": str(item["target"]).strip(),
                        "role": role,
                        "relation": relation or ("root" if not parent_ids else "causal_convergence" if len(parent_ids) > 1 else "refinement"),
                        "supported_level": supported_level,
                        "decision": decision,
                        "causal_status": causal_status,
                        "evidence_refs": refs,
                        "missing_evidence": [str(value) for value in item.get("missing_evidence", []) if str(value)],
                        "parent_candidate_ids": parent_ids,
                        "origin_parent_candidate_id": origin_parent or None,
                        "probe_requests": probe_requests,
                        "probe_request_specs": probe_request_specs,
                    })
                    seen.add(candidate_id)
                except Exception as exc:
                    validation_diagnostics.append(_validation_diagnostic(
                        stage="candidate_generation",
                        attempt=attempt,
                        error=str(exc),
                        raw=raw,
                        candidate_id=str(current_item.get("candidate_id") or "") if current_item else "",
                        candidate_count=len(candidates),
                        valid_evidence_ref_count=len(valid_refs),
                        known_candidate_ids=known_candidate_ids,
                        initial_evidence_refs=valid_refs,
                        initial_evidence_families=initial_evidence_context["evidence_families"],
                        initial_evidence_statuses=initial_evidence_context["evidence_statuses"],
                        candidate_evidence_refs=(
                            [str(ref) for ref in current_item.get("evidence_refs", []) if str(ref)]
                            if current_item
                            else []
                        ),
                        candidate_parent_candidate_ids=(
                            [str(value) for value in current_item.get("parent_candidate_ids", []) if str(value)]
                            if current_item
                            else []
                        ),
                        candidate_origin_parent_candidate_id=(
                            str(current_item.get("origin_parent_candidate_id") or "")
                            if current_item
                            else ""
                        ),
                        candidate_supported_level=(
                            str(current_item.get("supported_level") or "")
                            if current_item
                            else ""
                        ),
                        candidate_entry_level=_initial_entry_boundary(fact_context, session_tree),
                    ))
                    continue
            registered = {
                str(item.get("evidence_family") or "")
                for item in probe_manifest.get("available_probes", [])
                if isinstance(item, dict)
            }
            selected = [str(value) for value in data.get("probe_requests", []) if str(value)]
            invalid_selected = [value for value in selected if value not in registered]
            if invalid_selected:
                validation_diagnostics.append(_validation_diagnostic(
                    stage="candidate_generation",
                    attempt=attempt,
                    error=f"probe_requests 包含未注册证据族: {', '.join(invalid_selected)}",
                    raw=raw,
                    candidate_count=len(candidates),
                    valid_evidence_ref_count=len(valid_refs),
                    known_candidate_ids=known_candidate_ids,
                        initial_evidence_refs=valid_refs,
                        initial_evidence_families=initial_evidence_context["evidence_families"],
                        initial_evidence_statuses=initial_evidence_context["evidence_statuses"],
                ))
                selected = [value for value in selected if value in registered]
            candidate_probe_requests = [
                value
                for item in normalized
                for value in item["probe_requests"]
            ]
            invalid_candidate_probes = {
                value for value in candidate_probe_requests if value not in registered
            }
            if invalid_candidate_probes:
                kept = []
                for item in normalized:
                    invalid = [
                        value for value in item["probe_requests"]
                        if value in invalid_candidate_probes
                    ]
                    if invalid:
                        validation_diagnostics.append(_validation_diagnostic(
                            stage="candidate_generation",
                            attempt=attempt,
                            error=f"probe_requests 包含未注册证据族: {', '.join(invalid)}",
                            raw=raw,
                            candidate_id=str(item.get("candidate_id") or ""),
                            candidate_count=len(candidates),
                            valid_evidence_ref_count=len(valid_refs),
                            known_candidate_ids=known_candidate_ids,
                    initial_evidence_refs=valid_refs,
                    initial_evidence_families=initial_evidence_context["evidence_families"],
                    initial_evidence_statuses=initial_evidence_context["evidence_statuses"],
                            candidate_evidence_refs=item["evidence_refs"],
                            candidate_parent_candidate_ids=item["parent_candidate_ids"],
                        ))
                        # The candidate remains a valid investigation
                        # direction. Only unsupported probe requests are
                        # removed; selection is scheduling, not a root-cause
                        # eligibility gate.
                        item = {
                            **item,
                            "probe_requests": [
                                value
                                for value in item["probe_requests"]
                                if value not in invalid_candidate_probes
                            ],
                            "probe_request_specs": [
                                spec
                                for spec in item.get("probe_request_specs", [])
                                if str(spec.get("evidence_family") or "") not in invalid_candidate_probes
                            ],
                        }
                    kept.append(item)
                normalized = kept
            if not normalized:
                raise ValueError("AI 未生成任何通过结构校验的候选")
            active_ids, deferred_ids = _active_candidate_ids(normalized)
            active_set = set(active_ids)
            active_probe_families = {
                str(value)
                for item in normalized
                if item["candidate_id"] in active_set
                for value in item["probe_requests"]
                if str(value)
            }
            unbound_selected = [
                value for value in selected
                if value not in active_probe_families
            ]
            if unbound_selected:
                validation_diagnostics.append(_validation_diagnostic(
                    stage="candidate_generation",
                    attempt=attempt,
                    error=(
                        "probe_requests 未绑定 active candidate: "
                        + ", ".join(unbound_selected)
                    ),
                    raw=raw,
                    candidate_count=len(candidates),
                    valid_evidence_ref_count=len(valid_refs),
                    known_candidate_ids=known_candidate_ids,
                    initial_evidence_refs=valid_refs,
                    initial_evidence_families=initial_evidence_context["evidence_families"],
                    initial_evidence_statuses=initial_evidence_context["evidence_statuses"],
                ))
            selected = list(dict.fromkeys([
                *active_probe_families,
            ]))
            probe_inputs = _candidate_probe_inputs(normalized, active_ids)
            selection_diagnostics = _candidate_selection_diagnostics(
                normalized,
                active_ids,
                deferred_ids,
            )
            candidate_generation_attempts.append(_candidate_generation_attempt_record(
                attempt=attempt,
                raw=raw,
                parsed_candidate_count=len(candidates),
                accepted_candidate_count=len(normalized),
                validation_diagnostic_count=len(validation_diagnostics) - diagnostic_count_before,
                status="succeeded",
            ))
            return {
                **base,
                "ai_review_status": "succeeded",
                "ai_review_attempts": attempt,
                "ai_review_error": "",
                "candidate_proposals": normalized,
                "selected_evidence_families": selected,
                "probe_inputs": probe_inputs,
                "active_candidate_ids": active_ids,
                "deferred_candidate_ids": deferred_ids,
                "candidate_selection_diagnostics": selection_diagnostics,
                "validation_diagnostics": validation_diagnostics,
                "candidate_generation_attempts": candidate_generation_attempts,
            }
        except Exception as exc:
            last_error = str(exc)
            validation_diagnostics.append(_validation_diagnostic(
                stage="candidate_generation",
                attempt=attempt,
                error=last_error,
                raw=raw,
                candidate_count=len(candidates) if isinstance(locals().get("candidates"), list) else 0,
                valid_evidence_ref_count=len(valid_refs),
                known_candidate_ids=known_candidate_ids,
                            initial_evidence_refs=valid_refs,
                            initial_evidence_families=initial_evidence_context["evidence_families"],
                            initial_evidence_statuses=initial_evidence_context["evidence_statuses"],
                candidate_evidence_refs=(
                    [str(ref) for ref in current_item.get("evidence_refs", []) if str(ref)]
                    if current_item
                    else []
                ),
                candidate_parent_candidate_ids=(
                    [str(value) for value in current_item.get("parent_candidate_ids", []) if str(value)]
                    if current_item
                    else []
                ),
                candidate_origin_parent_candidate_id=(
                    str(current_item.get("origin_parent_candidate_id") or "")
                    if current_item
                    else ""
                ),
                candidate_supported_level=(
                    str(current_item.get("supported_level") or "")
                    if current_item
                    else ""
                ),
                candidate_entry_level=_initial_entry_boundary(fact_context, session_tree),
            ))
            candidate_generation_attempts.append(_candidate_generation_attempt_record(
                attempt=attempt,
                raw=raw,
                parsed_candidate_count=len(candidates) if isinstance(locals().get("candidates"), list) else 0,
                accepted_candidate_count=0,
                validation_diagnostic_count=len(validation_diagnostics) - diagnostic_count_before,
                status="failed",
            ))
            if attempt < attempt_limit:
                messages.extend([
                    {"role": "assistant", "content": raw[:1000]},
                    {"role": "user", "content": f"上一输出未通过硬校验：{last_error[:300]}。请只修正 JSON。"},
                ])
    return {
        **base,
        "ai_review_status": "failed",
        "ai_review_attempts": attempt_limit,
        "ai_review_error": last_error[:500],
        "candidate_selection_diagnostics": [],
        "validation_diagnostics": validation_diagnostics,
        "candidate_generation_attempts": candidate_generation_attempts,
    }


def generate_session_investigation_review(
    *,
    diagnosis_id: str,
    session_tree: ControlledAITree,
    evidence_catalog: list[dict],
    probe_manifest: dict,
    allowed_evidence_families: list[str],
    model_name: str | None = None,
    max_attempts: int = 1,
) -> dict:
    """Let AI choose the next bounded evidence action before a follow-up round."""
    settings = get_ai_settings()
    model_name = model_name or settings.model
    if not is_feature_enabled("rca"):
        return {
            "ai_review_status": "fallback",
            "ai_review_scope": "investigation_round",
            "ai_review_attempts": 0,
            "ai_review_model": model_name,
            "ai_review_error": "AI RCA is disabled or no API key is configured",
            "selected_evidence_families": [],
            "candidate_proposals": [],
            "candidate_updates": {},
            "rollback_edges": [],
            "probe_inputs": {},
        }
    attempt_limit = max(1, min(int(max_attempts), 1 + MAX_RETRIES))
    allowed = set(allowed_evidence_families)
    manifest_families = {
        str(item.get("evidence_family") or "")
        for item in probe_manifest.get("available_probes", [])
        if isinstance(item, dict)
    }
    allowed &= manifest_families
    candidate_ids = {
        node.candidate_id
        for layer in session_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
    }
    rejected_candidate_ids = {
        node.candidate_id
        for layer in session_tree.layers
        for node in [*layer.rejected_causes, *layer.primary_causes, *layer.secondary_causes, *layer.unknown_causes]
        if node.role == "rejected" or node.status in {"rejected", "contradicted", "forbidden"}
    }
    valid_refs = {
        str(item.get(key) or "")
        for item in evidence_catalog
        if isinstance(item, dict)
        for key in ("evidence_id", "raw_artifact_ref", "derived_artifact_ref")
        if item.get(key)
    }
    valid_refs.update(
        ref
        for layer in session_tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
        for ref in [*node.evidence_refs, *node.self_challenge.supporting_evidence_refs, *node.self_challenge.opposing_evidence_refs]
        if ref
    )
    source_anchor_catalog = _source_anchor_catalog(evidence_catalog)
    candidate_catalog = [
        {
            "candidate_id": node.candidate_id,
            "parent_candidate_ids": list(node.parent_candidate_ids),
            "origin_parent_candidate_id": node.origin_parent_candidate_id,
            "depth_kind": node.depth_kind,
            "supported_level": node.supported_level,
            "role": node.role,
            "status": node.status,
        }
        for layer in session_tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    ]
    eligible_probe_candidates = [
        item["candidate_id"]
        for item in candidate_catalog
        if item["candidate_id"] in (candidate_ids - rejected_candidate_ids)
    ]
    candidate_levels = {
        item["candidate_id"]: item["supported_level"]
        for item in candidate_catalog
    }
    payload = {
        "diagnosis_id": diagnosis_id,
        "current_tree": session_tree.model_dump(mode="json"),
        "candidate_catalog": candidate_catalog,
        "eligible_probe_candidates": eligible_probe_candidates,
        "valid_evidence_refs": sorted(valid_refs),
        "evidence_catalog": [
            {
                key: item.get(key)
                for key in ("evidence_id", "query_or_probe", "raw_artifact_ref", "derived_artifact_ref", "observed_value", "data_quality")
                if key in item
            }
            for item in evidence_catalog[-30:]
            if isinstance(item, dict)
        ],
        "probe_manifest": probe_manifest,
        "eligible_this_round": sorted(allowed),
        "source_anchor_catalog": source_anchor_catalog,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Mini-Drop 会话级调查树裁决器，只输出 JSON。选择最小必要补证并形成可证伪机制候选。"
                "probe_requests 可以是结构化对象，必须包含 evidence_family、question、why_needed、input_refs、expected_observation、disconfirming_observation；"
                "系统会从合法 probe_requests 推导 selected_evidence_families。旧 selected_evidence_families 字符串仅作兼容输入。"
                "不能输出命令、修复动作或未注册工具。"
                "当选择 source_mechanism_query 时，必须输出 probe_inputs.source_mechanism_query.ai_generated_query，"
                "其中包含 investigation_question、candidate_id、origin_parent_candidate_id、expected_relation(supports|refutes) 和 2-6 个有序 path_anchors；"
                "锚点必须逐字选择 source_anchor_catalog 中不同的 file/line，并按预期机制传播顺序排列；"
                "candidate_id 必须绑定 current_tree 或本轮 candidate_proposals；"
                "candidate_catalog 是唯一可用的候选 ID 和父子关系清单；candidate_proposals 的 parent_candidate_ids 只能逐字选择其中的 candidate_id，"
                "source_mechanism_query 的 origin_parent_candidate_id 必须选择其中 depth_kind=base、supported_level=line 的节点；"
                "valid_evidence_refs 是唯一可引用的 evidence_refs 清单，不能自行改写、拼接或引用不存在的 ref；"
                "若 current_tree 已把候选标为 rejected/contradicted，禁止再次绑定该候选，也禁止重复其原 path_anchors；"
                "此时必须回退到父层并提出不同机制的新候选。分配热点行只是表层位置，源码机制查询应优先验证"
                "reference_origin 到 reference_step 的完整传播链；"
                "不要编写 CodeQL 语法，系统会从锚点生成版本锁定的查询；不得引用目录外路径、shell 或修复动作。"
                "当选择 python_heap_reference 时，必须输出 probe_inputs.python_heap_reference，包含 current_tree 或本轮候选的"
                "candidate_id，以及 1-8 个 object_type_hints，用于限制 PyHeap 运行时引用验证目标。"
                "candidate_proposals 可为空；新增 candidate_id 必须以 ai_proposal_ 开头，parent_candidate_ids 必须引用 current_tree，"
                "并且每个候选必须给出唯一 origin_parent_candidate_id，且该值必须属于 parent_candidate_ids；"
                "evidence_refs 必须真实存在，supported_level 不得超过 current_tree.final_supported_level；"
                "refinement 候选最多比 origin_parent_candidate_id 深一层；不得直接生成 line 候补，"
                "line 只能由 runtime file:line 和 source_snapshot 验证后形成。"
                "每个候选必须包含 claim、mechanism、target、支持/反驳/缺失证据和 what_would_change_my_mind。"
                "证据回流时可返回 candidate_updates（只能更新 current_tree 中已有候选）和 rollback_edges；"
                "candidate_updates 只能改变该候选的解释、状态、角色和显式血缘，不能删除候选或伪造父节点；"
                "探针失败、阻断、超时只能使用 blocked/inconclusive 语义，不能写成 contradicted。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)},
    ]
    last_error = ""
    validation_diagnostics = []
    for attempt in range(1, attempt_limit + 1):
        raw = ""
        try:
            raw = _call_deepseek(messages, model_name)
            data = json.loads(_extract_json(raw) or "{}")
            raw_probe_requests = data.get("probe_requests")
            if raw_probe_requests is None:
                raw_probe_requests = data.get("selected_evidence_families", [])
            requested_families, probe_request_specs = _normalize_probe_requests(raw_probe_requests)
            requested_families, probe_request_specs, rejected_probe_specs = _filter_probe_request_specs(
                requested_families,
                probe_request_specs,
                valid_refs,
            )
            for rejected_spec in rejected_probe_specs:
                validation_diagnostics.append({
                    "stage": "investigation_round",
                    "attempt": attempt,
                    "failure_code": "invalid_probe_input_ref",
                    "evidence_family": rejected_spec.get("evidence_family"),
                    "invalid_input_refs": rejected_spec.get("invalid_input_refs", []),
                    "reason": rejected_spec.get("reason"),
                })
            selected = [item for item in requested_families if item in allowed]
            if not selected or len(selected) > 3:
                raise ValueError("probe_requests 越界或为空")
            invalid_probe_requests = [
                item for item in requested_families if item not in allowed
            ]
            if invalid_probe_requests:
                validation_diagnostics.append({
                    "stage": "investigation_round",
                    "attempt": attempt,
                    "failure_code": "invalid_probe_request",
                    "invalid_evidence_families": invalid_probe_requests[:16],
                    "reason": "AI 请求的 evidence_family 不在本轮编排器允许集合中。",
                })
            proposals = _validate_investigation_proposals(
                data.get("candidate_proposals"),
                candidate_ids,
                valid_refs,
                session_tree.final_supported_level,
                candidate_levels=candidate_levels,
            )
            all_candidate_ids = candidate_ids | {
                str(item.get("candidate_id") or "")
                for item in proposals
            }
            candidate_updates = _validate_investigation_updates(
                data.get("candidate_updates"),
                candidate_ids=all_candidate_ids,
                existing_candidate_ids=candidate_ids,
                valid_refs=valid_refs,
            )
            rollback_edges = _validate_investigation_rollback_edges(
                data.get("rollback_edges"),
                candidate_ids=all_candidate_ids,
                valid_refs=valid_refs,
            )
            probe_inputs = _validate_investigation_probe_inputs(
                data.get("probe_inputs"),
                selected,
                (candidate_ids - rejected_candidate_ids)
                | {str(item.get("candidate_id") or "") for item in proposals},
                source_anchor_catalog,
                candidate_parent_ids={
                    node.candidate_id: set(node.parent_candidate_ids)
                    for layer in session_tree.layers
                    for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
                }
                | {
                    str(item.get("candidate_id")): set(str(parent) for parent in item.get("parent_candidate_ids", []))
                    for item in proposals
                },
            )
            for spec in probe_request_specs:
                family = str(spec.get("evidence_family") or "")
                if family not in selected or family in probe_inputs:
                    continue
                probe_inputs[family] = {
                    **spec,
                    "evidence_family": family,
                }
            return {
                "ai_review_status": "succeeded",
                "ai_review_scope": "investigation_round",
                "ai_review_attempts": attempt,
                "ai_review_model": model_name,
                "ai_review_error": "",
                "selected_evidence_families": list(dict.fromkeys(selected)),
                "probe_requests": [
                    spec for spec in probe_request_specs
                    if str(spec.get("evidence_family") or "") in selected
                ],
                "candidate_proposals": proposals,
                "candidate_updates": candidate_updates,
                "rollback_edges": rollback_edges,
                "probe_inputs": probe_inputs,
            }
        except Exception as exc:
            last_error = str(exc)
            validation_diagnostics.append(_validation_diagnostic(
                stage="investigation_round",
                attempt=attempt,
                error=last_error,
                raw=raw,
                candidate_count=len(proposals) if isinstance(locals().get("proposals"), list) else 0,
                valid_evidence_ref_count=len(valid_refs),
                known_candidate_ids=candidate_ids,
            ))
            if attempt < attempt_limit:
                messages.extend([
                    {"role": "assistant", "content": raw[:1000] if "raw" in locals() else "{}"},
                    {"role": "user", "content": f"上一输出未通过硬校验：{last_error[:300]}。请只修正 JSON。"},
                ])
    return {
        "ai_review_status": "failed",
        "ai_review_scope": "investigation_round",
        "ai_review_attempts": attempt_limit,
        "ai_review_model": model_name,
        "ai_review_error": last_error[:500],
        "validation_diagnostics": validation_diagnostics,
        "selected_evidence_families": [],
        "candidate_proposals": [],
        "candidate_updates": {},
        "rollback_edges": [],
        "probe_inputs": {},
    }


def _validate_investigation_probe_inputs(
    value,
    selected: list[str],
    allowed_candidate_ids: set[str],
    allowed_source_anchors: list[dict] | None = None,
    candidate_parent_ids: dict[str, set[str]] | None = None,
) -> dict[str, dict]:
    value = value if isinstance(value, dict) else {}
    allowed_keys = {"source_mechanism_query", "python_heap_reference"} & set(selected)
    if any(str(key) not in allowed_keys for key in value):
        raise ValueError("probe_inputs 包含未选择或不允许的采集器参数")
    result = {}
    if "source_mechanism_query" in selected:
        item = value.get("source_mechanism_query")
        if not isinstance(item, dict):
            raise ValueError("source_mechanism_query 缺少受控临时查询")
        guarded = validate_ai_generated_codeql_query(
            item.get("ai_generated_query"),
            allowed_candidate_ids=allowed_candidate_ids,
            allowed_anchors=allowed_source_anchors,
        )
        raw_generated = item.get("ai_generated_query") if isinstance(item.get("ai_generated_query"), dict) else {}
        origin_parent = str(
            item.get("origin_parent_candidate_id")
            or raw_generated.get("origin_parent_candidate_id")
            or guarded.get("origin_parent_candidate_id")
            or ""
        ).strip()
        if not origin_parent or origin_parent not in allowed_candidate_ids:
            raise ValueError("source_mechanism_query 缺少合法 origin_parent_candidate_id")
        known_parents = (candidate_parent_ids or {}).get(guarded["candidate_id"], set())
        if known_parents and origin_parent not in known_parents:
            raise ValueError("source_mechanism_query 的来源父节点不属于候选的 parent_candidate_ids")
        guarded["origin_parent_candidate_id"] = origin_parent
        result["source_mechanism_query"] = {
            "candidate_id": guarded["candidate_id"],
            "origin_parent_candidate_id": origin_parent,
            "ai_generated_query": guarded,
        }
    if "python_heap_reference" in selected:
        item = value.get("python_heap_reference")
        if not isinstance(item, dict):
            raise ValueError("python_heap_reference 缺少候选绑定参数")
        candidate_id = str(item.get("candidate_id") or "")
        hints = [str(hint).strip() for hint in item.get("object_type_hints", []) if str(hint).strip()]
        if candidate_id not in allowed_candidate_ids:
            raise ValueError("python_heap_reference 绑定了越界候选")
        origin_parent = str(item.get("origin_parent_candidate_id") or "").strip()
        if not origin_parent or origin_parent not in allowed_candidate_ids:
            raise ValueError("python_heap_reference 缺少合法 origin_parent_candidate_id")
        known_parents = (candidate_parent_ids or {}).get(candidate_id, set())
        if known_parents and origin_parent not in known_parents:
            raise ValueError("python_heap_reference 的来源父节点不属于候选的 parent_candidate_ids")
        if not 1 <= len(hints) <= 8 or any(not re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", hint) for hint in hints):
            raise ValueError("python_heap_reference object_type_hints 非法")
        result["python_heap_reference"] = {
            "candidate_id": candidate_id,
            "origin_parent_candidate_id": origin_parent,
            "object_type_hints": list(dict.fromkeys(hints)),
        }
    return result


def _source_anchor_catalog(evidence_catalog: list[dict]) -> list[dict]:
    """Build a compact, evidence-backed anchor menu for the investigation model."""
    result: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def add(file_name, line, symbol="", text="", semantic_role="observed_line"):
        file_text = str(file_name or "").strip().replace("\\", "/")
        try:
            line_number = int(line or 0)
        except (TypeError, ValueError):
            return
        if not file_text or line_number <= 0:
            return
        key = (file_text.lstrip("/"), line_number)
        if key in seen:
            return
        seen.add(key)
        result.append({
            "file": file_text,
            "line": line_number,
            "symbol": str(symbol or "")[:160],
            "text": str(text or "")[:300],
            "semantic_role": str(semantic_role or "observed_line")[:80],
        })

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("producer") == "git+universal-ctags":
            for snippet in value.get("snippets", []):
                if isinstance(snippet, dict):
                    add(
                        snippet.get("file"),
                        snippet.get("focus_line"),
                        snippet.get("symbol"),
                        semantic_role="runtime_focus",
                    )
            for context in value.get("enclosing_contexts", []):
                if not isinstance(context, dict):
                    continue
                file_name = context.get("file")
                symbol = context.get("symbol")
                for path in context.get("reference_paths", []):
                    if not isinstance(path, dict):
                        continue
                    for upstream in path.get("upstream_candidates", []):
                        if isinstance(upstream, dict):
                            add(
                                file_name,
                                upstream.get("line"),
                                symbol,
                                upstream.get("expression"),
                                "reference_origin",
                            )
                    for source_line in path.get("source_lines", []):
                        if isinstance(source_line, dict):
                            add(
                                file_name,
                                source_line.get("line"),
                                symbol,
                                source_line.get("text"),
                                "reference_step",
                            )
            default_file = next((
                snippet.get("file")
                for snippet in value.get("snippets", [])
                if isinstance(snippet, dict) and snippet.get("file")
            ), "")
            for path in value.get("reference_paths", []):
                if not isinstance(path, dict):
                    continue
                for upstream in path.get("upstream_candidates", []):
                    if isinstance(upstream, dict):
                        add(
                            default_file,
                            upstream.get("line"),
                            text=upstream.get("expression"),
                            semantic_role="reference_origin",
                        )
                for source_line in path.get("source_lines", []):
                    if isinstance(source_line, dict):
                        add(
                            default_file,
                            source_line.get("line"),
                            text=source_line.get("text"),
                            semantic_role="reference_step",
                        )
            return
        for child in value.values():
            visit(child)

    for evidence in evidence_catalog:
        if isinstance(evidence, dict) and evidence.get("query_or_probe") == "source_snapshot":
            visit(evidence.get("observed_value"))
    return result[:40]


def _validate_investigation_proposals(
    value,
    parent_ids: set[str],
    valid_refs: set[str],
    max_level: str,
    *,
    candidate_levels: dict[str, str] | None = None,
) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 2:
        raise ValueError("candidate_proposals 必须是最多两个候选的数组")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("candidate_proposal 不是对象")
        candidate_id = str(item.get("candidate_id") or "")
        parents = [str(parent) for parent in item.get("parent_candidate_ids", [])]
        origin_parent = str(item.get("origin_parent_candidate_id") or "")
        refs = [str(ref) for ref in item.get("evidence_refs", [])]
        level = str(item.get("supported_level") or "resource")
        if not re.fullmatch(r"ai_proposal_[a-zA-Z0-9_\-]{1,80}", candidate_id) or candidate_id in seen:
            raise ValueError("candidate_id 非法或重复")
        if not parents or any(parent not in parent_ids for parent in parents):
            raise ValueError("candidate parent 越界")
        if not origin_parent or origin_parent not in parents:
            raise ValueError("candidate 必须提供属于 parent_candidate_ids 的唯一 origin_parent_candidate_id")
        if not refs or any(ref not in valid_refs for ref in refs):
            raise ValueError("candidate evidence_refs 不真实")
        if _level_order(level) > _level_order(max_level):
            raise ValueError("candidate supported_level 越界")
        relation = str(item.get("relation") or ("causal_convergence" if len(parents) > 1 else "refinement"))
        if relation not in {"refinement", "causal_convergence", "shared_evidence", "alternative"}:
            raise ValueError("candidate relation 非法")
        if level == "line":
            raise ValueError("candidate 层级跃迁非法: line 只能由 runtime/source_snapshot 验证产生")
        parent_level = str((candidate_levels or {}).get(origin_parent) or "").strip()
        if (
            relation == "refinement"
            and parent_level
            and _level_order(level) > _level_order(parent_level) + 1
        ):
            raise ValueError(
                "candidate 层级跃迁非法: "
                f"refinement {parent_level} -> {level} 超过 origin parent 的下一层"
            )
        child_ids = [str(child) for child in item.get("child_candidate_ids", []) if str(child)]
        if any(child == candidate_id for child in child_ids):
            raise ValueError("candidate child_candidate_ids 不真实")
        for field in ("claim", "mechanism", "target", "what_would_change_my_mind"):
            if not str(item.get(field) or "").strip():
                raise ValueError(f"candidate 缺少 {field}")
        result.append({
            "candidate_id": candidate_id,
            "parent_candidate_ids": parents,
            "origin_parent_candidate_id": origin_parent,
            "child_candidate_ids": list(dict.fromkeys(child_ids)),
            "relation": relation,
            "claim": str(item["claim"]).strip(),
            "mechanism": str(item["mechanism"]).strip(),
            "target": str(item["target"]).strip(),
            "supported_level": level,
            "evidence_refs": refs,
            "opposing_evidence_refs": [str(ref) for ref in item.get("opposing_evidence_refs", []) if str(ref) in valid_refs],
            "missing_evidence": [str(gap) for gap in item.get("missing_evidence", [])],
            "what_would_change_my_mind": str(item["what_would_change_my_mind"]).strip(),
        })
        seen.add(candidate_id)
    proposed_ids = {item["candidate_id"] for item in result}
    for item in result:
        if any(
            child not in parent_ids and child not in proposed_ids
            for child in item.get("child_candidate_ids", [])
        ):
            raise ValueError("candidate child_candidate_ids 不真实")
    return result


def _validate_investigation_updates(
    value,
    *,
    candidate_ids: set[str],
    existing_candidate_ids: set[str],
    valid_refs: set[str],
) -> dict[str, dict]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("candidate_updates 必须是对象")
    allowed_roles = {"primary", "secondary", "rejected", "unknown"}
    allowed_statuses = {
        "supported", "weakened", "missing_evidence", "forbidden",
        "contradicted", "rejected", "unknown", "blocked", "partial",
    }
    allowed_causal = {"supported", "unproven", "contradicted", "inconclusive"}
    allowed_decisions = {"continue_probe", "reject_candidate", "conclude", "abstain", "backtrack"}
    allowed_relations = {
        "alternative", "refinement", "causal_convergence", "shared_evidence",
        "rejected_alternative",
    }
    normalized: dict[str, dict] = {}
    for candidate_id, raw in value.items():
        candidate_id = str(candidate_id)
        if candidate_id not in existing_candidate_ids or candidate_id not in candidate_ids:
            raise ValueError("candidate_updates 只能更新当前树中已有候选")
        if not isinstance(raw, dict):
            raise ValueError("candidate_update 不是对象")
        update: dict[str, object] = {}
        for key in ("claim", "mechanism", "target", "eligibility_reason", "stop_reason", "blocked_probe"):
            if key in raw:
                text = str(raw.get(key) or "").strip()
                if text:
                    update[key] = text
        if "role" in raw and str(raw.get("role")) in allowed_roles:
            update["role"] = str(raw["role"])
        if "status" in raw and str(raw.get("status")) in allowed_statuses:
            update["status"] = str(raw["status"])
        if "causal_status" in raw and str(raw.get("causal_status")) in allowed_causal:
            update["causal_status"] = str(raw["causal_status"])
        if "decision" in raw and str(raw.get("decision")) in allowed_decisions:
            update["decision"] = str(raw["decision"])
        if "relation" in raw:
            relation = str(raw.get("relation") or "")
            if relation not in allowed_relations:
                raise ValueError("candidate_update relation 非法")
            update["relation"] = relation
        if "parent_candidate_ids" in raw:
            parents = [str(parent) for parent in raw.get("parent_candidate_ids", []) if str(parent)]
            if not parents or any(parent not in candidate_ids for parent in parents):
                raise ValueError("candidate_update parent_candidate_ids 不真实")
            origin = str(raw.get("origin_parent_candidate_id") or "")
            if not origin or origin not in parents:
                raise ValueError("candidate_update 缺少合法 origin_parent_candidate_id")
            update["parent_candidate_ids"] = list(dict.fromkeys(parents))
            update["origin_parent_candidate_id"] = origin
        if "child_candidate_ids" in raw:
            children = [str(child) for child in raw.get("child_candidate_ids", []) if str(child)]
            if any(child == candidate_id or child not in candidate_ids for child in children):
                raise ValueError("candidate_update child_candidate_ids 不真实")
            update["child_candidate_ids"] = list(dict.fromkeys(children))
        for key in ("evidence_refs", "opposing_evidence_refs"):
            if key in raw:
                refs = [str(ref) for ref in raw.get(key, []) if str(ref)]
                if any(ref not in valid_refs for ref in refs):
                    raise ValueError(f"candidate_update {key} 不真实")
                update[key] = list(dict.fromkeys(refs))
        if "missing_evidence" in raw:
            update["missing_evidence"] = list(dict.fromkeys(
                str(item) for item in raw.get("missing_evidence", []) if str(item)
            ))
        if update:
            normalized[candidate_id] = update
    return normalized


def _validate_investigation_rollback_edges(
    value,
    *,
    candidate_ids: set[str],
    valid_refs: set[str],
) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("rollback_edges 必须是最多四条边的数组")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("rollback_edge 不是对象")
        from_ids = [str(candidate_id) for candidate_id in item.get("from_candidate_ids", []) if str(candidate_id)]
        to_ids = [str(candidate_id) for candidate_id in item.get("to_candidate_ids", []) if str(candidate_id)]
        if not from_ids or not to_ids or any(candidate_id not in candidate_ids for candidate_id in [*from_ids, *to_ids]):
            raise ValueError("rollback_edge 候选不存在")
        transition_type = str(item.get("transition_type") or "backtrack")
        effect = str(item.get("effect") or "rollback")
        if transition_type not in {"backtrack", "boundary"} or effect not in {"rollback", "no_change"}:
            raise ValueError("rollback_edge 类型非法")
        status = str(item.get("status") or "inconclusive")
        if status not in {"completed", "inconclusive", "blocked", "failed", "reused", "not_started", "unknown"}:
            raise ValueError("rollback_edge status 非法")
        refs = [str(ref) for ref in item.get("evidence_refs", []) if str(ref)]
        if any(ref not in valid_refs for ref in refs):
            raise ValueError("rollback_edge evidence_refs 不真实")
        result.append({
            "edge_id": str(item.get("edge_id") or f"ai_rollback_{len(result)}"),
            "from_candidate_ids": list(dict.fromkeys(from_ids)),
            "to_candidate_ids": list(dict.fromkeys(to_ids)),
            "transition_type": transition_type,
            "effect": effect,
            "status": status,
            "reason": str(item.get("reason") or "AI 依据证据回流保留来源父节点。"),
            "probe_requests": [str(value) for value in item.get("probe_requests", []) if str(value)],
            "evidence_refs": list(dict.fromkeys(refs)),
        })
    return result


def generate_session_conclusion_review(
    *,
    diagnosis_id: str,
    clusters: list[RootCauseCluster],
    session_tree: ControlledAITree | dict | None,
    evidence_catalog: list[dict],
    probe_manifest: dict,
    localization_frontier: list | None = None,
    model_name: str | None = None,
    max_attempts: int | None = None,
) -> dict:
    """Run one bounded LLM adjudication over all eligible session clusters."""
    settings = get_ai_settings()
    model_name = model_name or settings.model
    if not is_feature_enabled("rca"):
        return {
            "ai_review_status": "fallback",
            "ai_review_scope": "session",
            "ai_review_attempts": 0,
            "ai_review_model": model_name,
            "ai_review_error": "AI RCA is disabled or no API key is configured",
            "review": None,
        }
    eligible = [cluster for cluster in clusters if cluster.conclusion_eligible]
    if not eligible:
        return {
            "ai_review_status": "fallback",
            "ai_review_scope": "session",
            "ai_review_attempts": 0,
            "ai_review_model": model_name,
            "ai_review_error": "no eligible root-cause clusters",
            "review": None,
        }

    from server.app.diagnosis.session_conclusion import validate_session_review

    tree_payload = session_tree.model_dump(mode="json") if isinstance(session_tree, ControlledAITree) else session_tree
    valid_refs = {
        str(item.get(key) or "")
        for item in evidence_catalog
        if isinstance(item, dict)
        for key in ("evidence_id", "evidence_ref", "raw_artifact_ref", "derived_artifact_ref")
        if item.get(key)
    }
    payload = _build_session_review_payload(
        diagnosis_id=diagnosis_id,
        eligible=eligible,
        tree_payload=tree_payload,
        evidence_catalog=evidence_catalog,
        probe_manifest=probe_manifest,
        localization_frontier=localization_frontier or [],
    )
    base_messages = [
        {"role": "system", "content": _session_review_system_prompt()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]
    messages = list(base_messages)
    attempt_limit = max(1, min(int(max_attempts or (MAX_RETRIES + 1)), MAX_RETRIES + 1))
    last_error = ""
    for attempt in range(1, attempt_limit + 1):
        raw = ""
        try:
            raw = _call_deepseek(
                messages,
                model_name,
                max_tokens=int(os.getenv("MINI_DROP_SESSION_REVIEW_MAX_TOKENS", "4096")),
            )
            data = json.loads(_extract_json(raw) or "{}")
            data = _normalize_session_review_shape(data)
            review = SessionConclusionReview.model_validate(data)
            issues = validate_session_review(
                review,
                eligible,
                valid_refs,
                localization_frontier=localization_frontier or [],
            )
            if not issues:
                log_event(
                    "info",
                    "session_conclusion_ai_review_succeeded",
                    diagnosis_id=diagnosis_id,
                    attempt=attempt,
                    model=model_name,
                    cluster_count=len(eligible),
                )
                return {
                    "ai_review_status": "succeeded",
                    "ai_review_scope": "session",
                    "ai_review_attempts": attempt,
                    "ai_review_model": model_name,
                    "ai_review_error": "",
                    "review": review,
                }
            last_error = "; ".join(issues)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        log_event(
            "warning",
            "session_conclusion_ai_review_rejected",
            diagnosis_id=diagnosis_id,
            attempt=attempt,
            model=model_name,
            error=last_error[:500],
        )
        if attempt < attempt_limit:
            messages = [
                *base_messages,
                {"role": "assistant", "content": raw},
                {
                "role": "user",
                "content": (
                    f"上一份会话裁决未通过硬校验：{last_error[:500]}。"
                    "只能使用输入中的 cluster_id 和 evidence_refs 修正 JSON。"
                    "注意 ruled_out_summary、residual_unknowns、causal_chain 都必须是 JSON 数组，"
                    "cluster_roles 和 recommendations 必须是 JSON 对象。"
                ),
                },
            ]
    return {
        "ai_review_status": "failed",
        "ai_review_scope": "session",
        "ai_review_attempts": attempt_limit,
        "ai_review_model": model_name,
        "ai_review_error": last_error[:500],
        "review": None,
    }


def generate_controlled_ai_tree(
    *,
    task_id: str,
    evidence: EvidenceInput,
    analyzer_result: EvidenceAttributionResult,
    probe_manifest: dict,
    model_name: str | None = None,
) -> ControlledAITree | None:
    """Ask the LLM to generate the controlled AI tree, then enforce hard boundaries."""
    fallback_tree = analyzer_result.controlled_ai_tree
    if fallback_tree is None:
        log_event(
            "warning",
            "controlled_ai_tree_llm_skipped",
            task_id=task_id,
            reason="missing_analyzer_tree",
        )
        return None
    if not is_feature_enabled("rca"):
        settings = get_ai_settings()
        log_event(
            "warning",
            "controlled_ai_tree_llm_skipped",
            task_id=task_id,
            reason="rca_feature_disabled",
            enabled=settings.enabled,
            source=settings.source,
            has_key=bool(settings.api_key),
            rca_enabled=settings.rca_enabled,
        )
        return fallback_tree

    model_name = model_name or get_ai_settings().model
    messages = [
        {"role": "system", "content": _build_controlled_tree_system_prompt()},
        {"role": "user", "content": _build_controlled_tree_user_message(
            evidence=evidence,
            analyzer_result=analyzer_result,
            probe_manifest=probe_manifest,
        )},
    ]
    last_error = ""
    for attempt in range(1 + MAX_RETRIES):
        try:
            raw = _call_deepseek(messages, model_name)
            proposed = ControlledAITree.model_validate(json.loads(_extract_json(raw) or "{}"))
            merged = _merge_llm_controlled_tree(
                llm_tree=proposed,
                analyzer_tree=fallback_tree,
                evidence=evidence,
                probe_manifest=probe_manifest,
            )
            if merged != fallback_tree or _llm_tree_shape_is_safe(proposed, fallback_tree, evidence, probe_manifest):
                log_event(
                    "info",
                    "controlled_ai_tree_llm_guarded",
                    task_id=task_id,
                    attempt=attempt,
                    model=model_name,
                    layer_count=len(merged.layers),
                )
                return merged
            last_error = "controlled_ai_tree 越过 Analyzer 边界或引用了非法证据/探针"
        except Exception as exc:
            last_error = str(exc)
        log_event(
            "warning",
            "controlled_ai_tree_llm_rejected",
            task_id=task_id,
            attempt=attempt,
            model=model_name,
            error=last_error[:500],
        )
        if attempt < MAX_RETRIES:
            messages.append({"role": "user", "content": f"上一次 controlled_ai_tree 无效：{last_error}。请只基于模板候选和 Probe Manifest 修正 JSON。"})
    log_event(
        "warning",
        "controlled_ai_tree_llm_fallback",
        task_id=task_id,
        model=model_name,
        error=last_error[:500],
    )
    compact_tree = _generate_compact_guard_review(
        task_id=task_id,
        evidence=evidence,
        analyzer_tree=fallback_tree,
        probe_manifest=probe_manifest,
        model_name=model_name,
    )
    if compact_tree is not None:
        return compact_tree
    return fallback_tree

def diagnose(
    task_id: str,
    evidence: EvidenceInput,
    candidates_json: str,
    model_name: str | None = None,
) -> ValidatedReport:
    """执行智能归因：LLM 推理 + 校验 + 自修复。

    Args:
        task_id: 任务 ID。
        evidence: 结构化证据。
        candidates_json: 校准后候选原因列表的 JSON 字符串。
        model_name: DeepSeek 模型名。

    Returns:
        ValidatedReport，包含校验通过的 DiagnosisReport。
    """
    model_name = model_name or get_ai_settings().model
    evidence_json = _serialize_evidence(evidence)
    system_prompt = build_system_prompt(model_name)
    user_message = build_user_message(evidence_json, candidates_json)

    if not is_feature_enabled("rca"):
        return _fallback_report(task_id, evidence, candidates_json)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    last_error = ""
    for attempt in range(1 + MAX_RETRIES):
        try:
            raw = _call_deepseek(messages, model_name)
            report, issues = _validate_and_parse(raw, evidence)
            if not issues:
                report = _attach_analysis_result(report, evidence)
                return ValidatedReport(
                    task_id=task_id,
                    model_name=model_name,
                    evidence_snapshot=json.loads(evidence_json),
                    report=report,
                    validated=True,
                    retry_count=attempt,
                )

            # 校验失败 → 构造修复提示追加到 messages
            last_error = "; ".join(issues)
            repair_prompt = (
                f"上一次你的输出校验失败：{last_error}\n"
                "请修正后重新输出 JSON。"
            )
            messages.append({"role": "assistant", "content": raw[:200]})
            messages.append({"role": "user", "content": repair_prompt})

        except Exception as exc:
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                time.sleep(1 * (attempt + 1))  # 指数退避
                continue

    # 全部重试失败
    return ValidatedReport(
        task_id=task_id,
        model_name=model_name,
        evidence_snapshot=json.loads(evidence_json) if evidence_json else {},
        report=_attach_analysis_result(DiagnosisReport(
            summary=f"归因失败（已重试 {MAX_RETRIES} 次）: {last_error}",
            ranked_causes=[],
            facts=[],
            not_enough_evidence=True,
        ), evidence),
        validated=False,
        validation_issues=[last_error],
        retry_count=MAX_RETRIES,
    )


def generate_compact_guarded_tree(
    *,
    task_id: str,
    evidence: EvidenceInput,
    analyzer_tree: ControlledAITree | None,
    probe_manifest: dict | None,
    model_name: str | None = None,
) -> ControlledAITree | None:
    if analyzer_tree is None:
        return None
    return _generate_compact_guard_review(
        task_id=task_id,
        evidence=evidence,
        analyzer_tree=analyzer_tree,
        probe_manifest=probe_manifest,
        model_name=model_name or get_ai_settings().model,
    )


# ── 内部 ──


def _serialize_evidence(evidence: EvidenceInput) -> str:
    """将证据序列化为 JSON，字段按近因效应排序——越重要的越靠后。"""
    from server.app.rca.evidence import evidence_to_json
    return evidence_to_json(evidence)


def _call_deepseek(messages: list[dict], model: str, *, max_tokens: int | None = None) -> str:
    """调用 DeepSeek Chat API。

    Args:
        messages: 对话历史（system + user）。
        model: 模型名称。

    Returns:
        LLM 原始响应文本。

    Raises:
        RuntimeError: API 返回非 200。
    """
    payload = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "disabled"},
        "temperature": 0.1,  # 低温：归因需要确定性而非创意
        "max_tokens": max_tokens or int(os.getenv("MINI_DROP_RCA_MAX_TOKENS", "8192")),
        "response_format": {"type": "json_object"},
    }
    timeout = max(10, int(os.getenv("MINI_DROP_RCA_LLM_TIMEOUT_SEC", "90")))
    resp = chat_completions(payload, timeout=timeout)
    if resp.status_code in (400, 422) or resp.status_code >= 500:
        compatible_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"thinking", "response_format"}
        }
        resp = chat_completions(compatible_payload, timeout=timeout)

    if resp.status_code != 200:
        raise RuntimeError(f"DeepSeek API 返回 {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    message = body["choices"][0]["message"]
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content

    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content

    raise RuntimeError("DeepSeek API 返回的消息缺少可解析内容")


def _session_review_system_prompt() -> str:
    return """你是 Mini-Drop 的会话级受控归因裁决器。你只能在输入给出的 eligible clusters 内裁决。
必须输出一个 JSON 对象，字段严格为：
headline, why_it_happened, primary_cluster_id, cluster_roles, causal_chain,
localization_chain, ruled_out_summary, residual_unknowns, recommendations。
约束：
1. 只能使用输入中已有的 cluster_id，不得新增、合并或改写 ID。
2. 只能引用输入中已有的 candidate_id 和 evidence_refs；每个 causal_chain 和 localization_chain step 必须包含真实 candidate_id，并至少引用一条真实证据。
3. 必须且只能选一个 primary；其余可标 contributing 或 independent。
4. 不得提高 cause_level，不得新增探针、候选、定位层级或源代码行。
5. why_it_happened 必须解释机制为何导致用户症状，不能只复述指标、栈或采集结果。
6. recommendations 以 cluster_id 为键，每项包含 recommendation_type、action、rationale；只能给建议，不得声称已执行修复。
7. 信息不足时写入 residual_unknowns，不得补造事实。
8. 对内存保留问题，retained allocation 只能证明分配来源，不能单独证明长期持有者。必须按 reference_paths 中真实存在的 source_expression/upstream_candidates -> container -> runtime_slot -> retained_by 解释引用链；禁止把没有进入该路径的分配对象编造成被 co_consts 持有。
9. causal_chain 必须覆盖每个 primary 或 contributing cluster，不能只解释 primary；不同方向可以分别停在 line、function、call_path 或更粗层级。
10. localization_chain 只能改写输入中的 localization_frontier；这些步骤必须使用“定位到、观察到、尚未证明”等有限语义，不得写成已确认根因。
字段类型必须符合下列 JSON 形状，不得把数组字段输出成字符串：
{
  "headline": "string",
  "why_it_happened": "string",
  "primary_cluster_id": "existing cluster_id",
  "cluster_roles": {"existing cluster_id": "primary|contributing|independent"},
  "causal_chain": [{"step_id": "string", "candidate_id": "existing candidate_id", "statement": "string", "evidence_refs": ["existing evidence_ref"], "supported_level": "line|function|call_path|endpoint|service|dependency|syscall|thread|process|host|resource"}],
  "localization_chain": [{"step_id": "string", "candidate_id": "existing candidate_id", "statement": "string", "evidence_refs": ["existing evidence_ref"], "supported_level": "line|function|call_path|endpoint|service|dependency|syscall|thread|process|host|resource"}],
  "ruled_out_summary": ["string"],
  "residual_unknowns": ["string"],
  "recommendations": {
    "existing cluster_id": [
      {"recommendation_type": "investigation|temporary_mitigation|permanent_fix", "action": "string", "rationale": "string"}
    ]
  }
}"""


def _normalize_session_review_shape(data: object) -> object:
    """Normalize unambiguous container mistakes without changing diagnostic semantics."""
    if not isinstance(data, dict):
        return data
    normalized = dict(data)
    for field in ("ruled_out_summary", "residual_unknowns"):
        value = normalized.get(field)
        if isinstance(value, str):
            normalized[field] = [value] if value.strip() else []
    return normalized


def _compact_session_tree(tree: dict | None) -> dict:
    if not isinstance(tree, dict):
        return {}
    layers = []
    for layer in tree.get("layers", [])[-4:]:
        if not isinstance(layer, dict):
            continue
        candidates = []
        for key in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            for node in layer.get(key, [])[:6]:
                if isinstance(node, dict):
                    candidates.append({
                        field: node.get(field)
                        for field in (
                            "candidate_id", "role", "claim", "claim_type", "causal_status",
                            "mechanism", "target", "supported_level", "status", "decision",
                            "parent_candidate_ids", "conclusion_eligible", "evidence_refs",
                        )
                    })
        layers.append({"layer_id": layer.get("layer_id"), "depth": layer.get("depth"), "candidates": candidates})
    return {
        "final_supported_level": tree.get("final_supported_level"),
        "stop_reason": tree.get("stop_reason"),
        "layers": layers,
    }


def _build_session_review_payload(
    *,
    diagnosis_id: str,
    eligible: list[RootCauseCluster],
    tree_payload: dict | None,
    evidence_catalog: list[dict],
    probe_manifest: dict,
    localization_frontier: list | None = None,
) -> dict:
    referenced_ids = {
        ref
        for cluster in eligible
        for ref in cluster.evidence_refs
    }
    referenced_ids.update(
        ref
        for step in (localization_frontier or [])
        for ref in (
            step.evidence_refs
            if hasattr(step, "evidence_refs")
            else step.get("evidence_refs", [])
        )
    )
    referenced_evidence = []
    for item in evidence_catalog:
        if not isinstance(item, dict):
            continue
        item_refs = {
            str(item.get(key) or "")
            for key in ("evidence_id", "evidence_ref", "raw_artifact_ref", "derived_artifact_ref")
            if item.get(key)
        }
        if not (item_refs & referenced_ids):
            continue
        referenced_evidence.append(_compact_evidence_item(item))
        if len(referenced_evidence) >= 24:
            break
    return {
        "diagnosis_id": diagnosis_id,
        "clusters": [cluster.model_dump(mode="json") for cluster in eligible],
        "localization_frontier": [
            step.model_dump(mode="json") if hasattr(step, "model_dump") else step
            for step in (localization_frontier or [])
        ],
        "session_tree": _compact_session_tree(tree_payload),
        "evidence_catalog": referenced_evidence,
        "probe_manifest": {"probe_ids": sorted(_probe_ids(probe_manifest))},
    }


def _compact_evidence_item(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    observed = item.get("observed_value")
    if isinstance(observed, dict) and observed.get("producer") == "git+universal-ctags":
        observed = _compact_source_snapshot(observed)
    elif isinstance(observed, dict) and observed.get("producer") == "memray":
        observed = {
            key: observed.get(key)
            for key in ("producer", "mode", "summary", "retained_allocation_hotspots", "evidence_validity")
        }
    else:
        observed = _compact_json_value(observed, depth=0)
    return {
        "evidence_id": item.get("evidence_id") or item.get("evidence_ref"),
        "source_type": item.get("source_type") or item.get("query_or_probe"),
        "observed_value": observed,
    }


def _compact_source_snapshot(observed: dict) -> dict:
    contexts = []
    for context in (observed.get("enclosing_contexts") or [])[:2]:
        if not isinstance(context, dict):
            continue
        source = "\n".join(
            f"{line.get('line')}: {line.get('text') or ''}"
            for line in (context.get("lines") or [])
            if isinstance(line, dict)
        )
        contexts.append({
            "file": context.get("file"),
            "symbol": context.get("symbol"),
            "kind": context.get("kind"),
            "start_line": context.get("start_line"),
            "end_line": context.get("end_line"),
            "source": source[:30000],
        })
    return {
        "producer": observed.get("producer"),
        "revision": observed.get("revision"),
        "source_context_hash": observed.get("source_context_hash"),
        "enclosing_contexts": contexts,
        "reference_paths": [
            {
                "source_expression": path.get("source_expression"),
                "source_kind": path.get("source_kind"),
                "upstream_candidates": (path.get("upstream_candidates") or [])[:8],
                "stored_via": path.get("stored_via"),
                "container": path.get("container"),
                "sink": path.get("sink"),
                "runtime_slot": path.get("runtime_slot"),
                "retained_by": path.get("retained_by"),
                "retention_chain": (path.get("retention_chain") or [])[:8],
                "source_lines": (path.get("source_lines") or [])[:8],
            }
            for path in (observed.get("reference_paths") or [])[:12]
            if isinstance(path, dict)
        ],
        "snippets": [
            {
                "file": snippet.get("file"),
                "focus_line": snippet.get("focus_line"),
                "symbol": snippet.get("symbol"),
            }
            for snippet in (observed.get("snippets") or [])[:8]
            if isinstance(snippet, dict)
        ],
        "evidence_validity": observed.get("evidence_validity") or {},
    }


def _compact_json_value(value, *, depth: int):
    if depth >= 3:
        return "[nested evidence retained by reference]"
    if isinstance(value, dict):
        compact = {}
        for key in list(value)[:8]:
            compact[str(key)] = _compact_json_value(value.get(key), depth=depth + 1)
        return compact
    if isinstance(value, list):
        return [_compact_json_value(item, depth=depth + 1) for item in value[:5]]
    if isinstance(value, str):
        return value[:240]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:240]


def _probe_ids(manifest: dict) -> set[str]:
    if not isinstance(manifest, dict):
        return set()
    values = manifest.get("probes") or manifest.get("items")
    if values is None:
        values = list(manifest.values())
    if isinstance(values, dict):
        values = list(values.values())
    result = set()
    for item in values if isinstance(values, (list, tuple, set)) else []:
        if isinstance(item, dict) and item.get("probe_id"):
            result.add(str(item["probe_id"]))
    return result



def _validate_and_parse(raw: str, evidence: EvidenceInput) -> tuple[DiagnosisReport | None, list[str]]:
    """校验 LLM 输出并解析为 DiagnosisReport。

    校验规则：
      1. JSON 可解析
      2. 所有字段类型正确（通过 Pydantic 校验）
      3. 每条 cause 的 evidence_refs 必须引用 evidence 中存在的字段
      4. confidence 在 [0, 1]
      5. ranked_causes 不空（除非 not_enough_evidence=True）
    """
    issues: list[str] = []

    # 步骤 1：提取 JSON
    json_text = _extract_json(raw)
    if not json_text:
        return None, ["无法从 LLM 输出中提取 JSON"]

    # 步骤 2：Pydantic 解析
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        return None, [f"JSON 解析失败: {exc}"]

    try:
        report = DiagnosisReport(**data)
    except Exception as exc:
        return None, [f"Schema 校验失败: {exc}"]

    # 步骤 3：证据引用完整性——每条 cause 的 evidence_refs 必须在 evidence 中可找到
    valid_paths = _collect_evidence_paths(evidence)
    for i, cause in enumerate(report.ranked_causes):
        for ref in cause.evidence_refs:
            if not _ref_exists(ref, valid_paths):
                issues.append(f"ranked_causes[{i}].evidence_refs 中的 '{ref}' 不在证据路径中")

    # 步骤 4：边界校验
    if report.not_enough_evidence and not report.ranked_causes:
        pass  # 证据不足 + 无候选 = 合理
    elif not report.ranked_causes and not report.not_enough_evidence:
        issues.append("ranked_causes 为空但 not_enough_evidence=false")

    analysis_result = evidence.analysis_result or {}
    if analysis_result:
        allowed_cause_ids = set(analysis_result.get("allowed_cause_ids", []))
        boundary = analysis_result.get("conclusion_boundary", {})
        can_claim_root_cause = bool(boundary.get("can_claim_root_cause", False))
        primary_cause_id = analysis_result.get("primary_cause_id")
        stability_score = float(analysis_result.get("stability_score", 0.0) or 0.0)

        for i, cause in enumerate(report.ranked_causes):
            if cause.cause_id not in allowed_cause_ids:
                issues.append(
                    f"ranked_causes[{i}].cause_id '{cause.cause_id}' 不在结构化分析允许的原因集合中"
                )

        if primary_cause_id and report.ranked_causes and report.ranked_causes[0].cause_id != primary_cause_id:
            issues.append(
                f"ranked_causes[0].cause_id '{report.ranked_causes[0].cause_id}' 必须优先与结构化主因 '{primary_cause_id}' 一致"
            )

        boundary_reason = str(boundary.get("reason", "") or "")
        if boundary_reason and any(
            token in boundary_reason
            for token in ("off_cpu_wait_profile", "trace_endpoint_profile", "baseline_window_profile")
        ):
            if not (
                report.missing_evidence
                or report.blocked_upgrades
                or report.collection_gaps
            ):
                issues.append("结构化边界已经说明缺少采集能力，报告也必须同步输出 missing_evidence / blocked_upgrades / collection_gaps")

        if not can_claim_root_cause:
            if not report.not_enough_evidence:
                issues.append("结构化分析禁止根因结论时，报告必须标记为证据不足")
            for i, cause in enumerate(report.ranked_causes):
                if cause.confidence >= 0.4:
                    issues.append(
                        f"ranked_causes[{i}] 在证据不足时 confidence 必须低于 0.4"
                    )
        elif primary_cause_id and stability_score >= 0.7 and report.ranked_causes:
            if report.ranked_causes[0].confidence < report.ranked_causes[-1].confidence:
                issues.append("高稳定性场景下 ranked_causes 应保持主因优先且置信度不低于后续候选")

    if issues:
        return None, issues

    return _attach_analysis_result(report, evidence), []


def _attach_analysis_result(report: DiagnosisReport, evidence: EvidenceInput) -> DiagnosisReport:
    """把结构化分析结果挂回最终报告对象，保证最终输出形态完整。"""
    analysis_result = evidence.analysis_result or {}
    if not analysis_result:
        return report

    normalized_result = dict(analysis_result)
    boundary = dict(normalized_result.get("conclusion_boundary") or {})
    boundary.setdefault("reason", normalized_result.get("primary_cause_reason", ""))
    boundary.setdefault("max_supported_level", "resource")
    normalized_result["conclusion_boundary"] = boundary

    analysis_result_model = EvidenceAttributionResult.model_validate(normalized_result)
    controlled_tree = _merge_llm_controlled_tree(
        llm_tree=report.controlled_ai_tree,
        analyzer_tree=analysis_result_model.controlled_ai_tree,
        evidence=evidence,
        probe_manifest=analysis_result.get("probe_registry_manifest") if isinstance(analysis_result, dict) else None,
    )
    return report.model_copy(update={
        "analysis_result": analysis_result_model,
        "symptoms": analysis_result_model.symptoms,
        "localizations": analysis_result_model.localizations,
        "ai_tree": analysis_result_model.ai_tree,
        "controlled_ai_tree": controlled_tree,
        "graph_entities": analysis_result_model.graph_entities,
        "graph_links": analysis_result_model.graph_links,
        "attributions": analysis_result_model.attributions,
        "evidence_challenges": analysis_result_model.evidence_challenges,
        "missing_evidence": analysis_result_model.missing_evidence,
        "blocked_upgrades": analysis_result_model.blocked_upgrades,
        "collection_gaps": analysis_result_model.collection_gaps,
        "graph_extension_points": analysis_result_model.graph_extension_points,
        "structured_evidence": evidence.analysis_result.get("structured_evidence") if evidence.analysis_result else None,
        "primary_cause_id": analysis_result_model.primary_cause_id,
        "stability_score": analysis_result_model.stability_score,
        "primary_cause_reason": analysis_result_model.primary_cause_reason,
        "secondary_causes": analysis_result.get("secondary_causes", []),
        "correlated_symptoms": analysis_result.get("correlated_symptoms", []),
        "unsupported_causes": analysis_result.get("unsupported_causes", []),
        "conclusion_boundary": analysis_result_model.conclusion_boundary,
    })


def _merge_llm_controlled_tree(
    *,
    llm_tree: ControlledAITree | None,
    analyzer_tree: ControlledAITree | None,
    evidence: EvidenceInput,
    probe_manifest: dict | None = None,
) -> ControlledAITree | None:
    """Adopt LLM tree decisions only inside Analyzer and probe-registry boundaries."""
    if analyzer_tree is None or llm_tree is None:
        return analyzer_tree

    if not _llm_tree_shape_is_safe(llm_tree, analyzer_tree, evidence, probe_manifest):
        return analyzer_tree

    merged = analyzer_tree.model_copy(update={
        "layers": llm_tree.layers,
        "probe_edges": llm_tree.probe_edges,
        "final_supported_level": analyzer_tree.final_supported_level,
        "final_primary_causes": _group_candidate_ids(llm_tree.layers, "primary"),
        "final_secondary_causes": _group_candidate_ids(llm_tree.layers, "secondary"),
        "final_rejected_causes": _group_candidate_ids(llm_tree.layers, "rejected"),
        "final_unknown_causes": _group_candidate_ids(llm_tree.layers, "unknown"),
        "stop_reason": llm_tree.stop_reason or analyzer_tree.stop_reason,
    })
    return enforce_conclusion_eligibility(merged)


def _generate_compact_guard_review(
    *,
    task_id: str,
    evidence: EvidenceInput,
    analyzer_tree: ControlledAITree,
    probe_manifest: dict | None,
    model_name: str,
) -> ControlledAITree | None:
    """Use a compact LLM review when full-tree JSON is too large or brittle."""
    messages = [
        {"role": "system", "content": _build_compact_guard_system_prompt()},
        {"role": "user", "content": _build_compact_guard_user_message(
            evidence=evidence,
            analyzer_tree=analyzer_tree,
            probe_manifest=probe_manifest,
        )},
    ]
    last_error = ""
    for attempt in range(1 + MAX_RETRIES):
        try:
            raw = _call_deepseek(messages, model_name)
            tree = _apply_compact_guard_review(
                raw=raw,
                analyzer_tree=analyzer_tree,
                evidence=evidence,
                probe_manifest=probe_manifest,
            )
            if tree is not None:
                log_event(
                    "info",
                    "controlled_ai_tree_llm_guarded_compact",
                    task_id=task_id,
                    attempt=attempt,
                    model=model_name,
                    layer_count=len(tree.layers),
                )
                return tree
            last_error = "compact guard review did not pass boundary checks"
        except Exception as exc:
            last_error = str(exc)
        log_event(
            "warning",
            "controlled_ai_tree_llm_compact_rejected",
            task_id=task_id,
            attempt=attempt,
            model=model_name,
            error=last_error[:500],
        )
        if attempt < MAX_RETRIES:
            messages.append({"role": "user", "content": f"上一次 compact guard review 无效：{last_error}。请只输出符合 schema 的 JSON。"})
    return None


def _apply_compact_guard_review(
    *,
    raw: str,
    analyzer_tree: ControlledAITree,
    evidence: EvidenceInput,
    probe_manifest: dict | None,
) -> ControlledAITree | None:
    json_text = _extract_json(raw)
    if not json_text and raw.strip():
        return _mark_tree_ai_guarded(
            analyzer_tree,
            "AI compact review returned unstructured feedback; Mini-Drop kept Analyzer candidates unchanged and only recorded AI guarded participation.",
        )
    data = json.loads(json_text or "{}")
    if not isinstance(data, dict):
        return None
    if "layers" in data or "probe_edges" in data:
        return None
    if data.get("tree_id") not in {None, analyzer_tree.tree_id}:
        return None

    analyzer_candidates = {
        item.candidate_id: item
        for layer in analyzer_tree.layers
        for item in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    valid_paths = _collect_evidence_paths(evidence)
    allowed_requests = _manifest_allowed_probe_requests(probe_manifest, analyzer_tree)
    for key in ("primary", "secondary", "rejected", "unknown"):
        values = data.get(key, [])
        if values is None:
            continue
        if not isinstance(values, list):
            data[key] = []
        else:
            data[key] = [str(item) for item in values if str(item) in analyzer_candidates]
    data["supporting_evidence_refs"] = [
        str(ref) for ref in data.get("supporting_evidence_refs", []) or []
        if _ref_exists(str(ref), valid_paths)
    ]
    data["opposing_evidence_refs"] = [
        str(ref) for ref in data.get("opposing_evidence_refs", []) or []
        if _ref_exists(str(ref), valid_paths)
    ]
    data["probe_requests"] = [
        str(request) for request in data.get("probe_requests", []) or []
        if str(request) in allowed_requests
    ]

    challenges = data.get("self_challenges", {}) or {}
    if not isinstance(challenges, dict):
        challenges = {}
    sanitized_challenges = {}
    for candidate_id, challenge in challenges.items():
        if str(candidate_id) not in analyzer_candidates or not isinstance(challenge, dict):
            continue
        challenge = dict(challenge)
        challenge["supporting_evidence_refs"] = [
            str(ref) for ref in challenge.get("supporting_evidence_refs", []) or []
            if _ref_exists(str(ref), valid_paths)
        ]
        challenge["opposing_evidence_refs"] = [
            str(ref) for ref in challenge.get("opposing_evidence_refs", []) or []
            if _ref_exists(str(ref), valid_paths)
        ]
        sanitized_challenges[str(candidate_id)] = challenge
    challenges = sanitized_challenges

    candidate_updates = data.get("candidate_updates", {}) or {}
    if not isinstance(candidate_updates, dict):
        candidate_updates = {}
    candidate_updates = {
        str(candidate_id): update
        for candidate_id, update in candidate_updates.items()
        if str(candidate_id) in analyzer_candidates and isinstance(update, dict)
    }

    role_updates: dict[str, str] = {}
    for role in ("primary", "secondary", "rejected", "unknown"):
        for candidate_id in data.get(role, []) or []:
            role_updates[str(candidate_id)] = role

    layers = []
    for layer in analyzer_tree.layers:
        grouped = {"primary": [], "secondary": [], "rejected": [], "unknown": []}
        nodes = [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
        for node in nodes:
            role = role_updates.get(node.candidate_id, node.role)
            guarded = _guarded_node(
                node,
                role,
                challenges.get(node.candidate_id),
                candidate_updates.get(node.candidate_id),
            )
            grouped[role].append(guarded)
        layers.append(layer.model_copy(update={
            "summary": str(
                (data.get("layer_summaries") or {}).get(layer.layer_id)
                if isinstance(data.get("layer_summaries"), dict)
                else layer.summary
            ) or layer.summary,
            "primary_causes": grouped["primary"],
            "secondary_causes": grouped["secondary"],
            "rejected_causes": grouped["rejected"],
            "unknown_causes": grouped["unknown"],
        }))

    reviewed_edges = list(analyzer_tree.probe_edges)
    rejected_ids = data.get("rejected", []) or []
    next_ids = [*(data.get("primary", []) or []), *(data.get("secondary", []) or []), *(data.get("unknown", []) or [])]
    if rejected_ids and next_ids:
        from_layer_id = _candidate_layer_id(analyzer_tree, rejected_ids[0])
        to_layer_id = _candidate_layer_id(analyzer_tree, next_ids[0])
        if from_layer_id and to_layer_id:
            reviewed_edges.append(AITreeProbeEdge(
                edge_id=f"compact_backtrack_{rejected_ids[0]}_to_{next_ids[0]}",
                from_layer_id=from_layer_id,
                to_layer_id=to_layer_id,
                from_candidate_ids=[rejected_ids[0]],
                to_candidate_ids=[next_ids[0]],
                probe_requests=data["probe_requests"][:3],
                status="not_started" if data["probe_requests"] else "completed",
                effect="rollback",
                transition_type="backtrack",
                reason=f"{rejected_ids[0]} 被反证，回退并转查 {next_ids[0]}。",
            ))
    reviewed = analyzer_tree.model_copy(update={
        "layers": layers,
        "probe_edges": reviewed_edges,
        "stop_reason": str(data.get("stop_reason") or analyzer_tree.stop_reason),
    })
    return enforce_conclusion_eligibility(reviewed)


def _mark_tree_ai_guarded(analyzer_tree: ControlledAITree, stop_reason: str) -> ControlledAITree:
    return analyzer_tree.model_copy(update={"stop_reason": stop_reason or analyzer_tree.stop_reason})


def _candidate_layer_id(tree: ControlledAITree, candidate_id: str) -> str:
    for layer in tree.layers:
        if any(
            node.candidate_id == candidate_id
            for node in [
                *layer.primary_causes,
                *layer.secondary_causes,
                *layer.rejected_causes,
                *layer.unknown_causes,
            ]
        ):
            return layer.layer_id
    return ""


def _guarded_node(node, suggested_role: str, challenge: dict | None, candidate_update: dict | None):
    if isinstance(challenge, dict):
        self_challenge = node.self_challenge.model_copy(update={
            "why_this_claim": str(challenge.get("why_this_claim") or node.self_challenge.why_this_claim),
            "why_not_other_claims": str(challenge.get("why_not_other_claims") or node.self_challenge.why_not_other_claims),
            "supporting_evidence_refs": [str(item) for item in challenge.get("supporting_evidence_refs", node.self_challenge.supporting_evidence_refs)],
            "opposing_evidence_refs": [str(item) for item in challenge.get("opposing_evidence_refs", node.self_challenge.opposing_evidence_refs)],
            "missing_evidence": [str(item) for item in challenge.get("missing_evidence", node.self_challenge.missing_evidence)],
            "what_would_change_my_mind": str(challenge.get("what_would_change_my_mind") or node.self_challenge.what_would_change_my_mind),
        })
    else:
        self_challenge = node.self_challenge
    status = node.status
    if suggested_role == "rejected" and node.status == "supported":
        status = "contradicted"
    elif suggested_role == "unknown" and node.status in {"supported", "weakened"}:
        status = "missing_evidence"
    update = {
        "role": suggested_role,
        "status": status,
        "self_challenge": self_challenge,
    }
    if isinstance(candidate_update, dict):
        enum_fields = {
            "claim_type": {
                "root_cause", "complete_root_cause", "direct_root_cause", "complete_source_root_cause",
                "direct_failure_mechanism", "likely_root_cause", "partial_localization",
                "observation_only", "insufficient_for_root_cause", "abstention",
            },
            "causal_status": {"supported", "unproven", "contradicted", "inconclusive"},
            "decision": {"continue_probe", "reject_candidate", "conclude", "abstain", "backtrack"},
            "primitive_kind": {
                "wait_primitive", "scheduler_primitive", "syscall_primitive", "runtime_primitive",
            },
        }
        for field in ("claim", "mechanism", "target", "eligibility_reason"):
            if isinstance(candidate_update.get(field), str):
                update[field] = candidate_update[field].strip()
        for field, allowed in enum_fields.items():
            value = candidate_update.get(field)
            if value in allowed:
                update[field] = value
        lineage = apply_ai_claim_update(node.model_dump(mode="python"), candidate_update)
        update["claim"] = lineage["claim"]
        for field in (
            "generated_by", "claim_origin", "claim_transform", "claim_status",
            "claim_hash", "source_claim_hash", "source_candidate_id", "source_round",
            "source_event_id",
        ):
            update[field] = lineage[field]
    return node.model_copy(update=update)


def _group_candidate_ids(layers, role: str) -> list[str]:
    ids = []
    for layer in layers:
        for item in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]:
            if item.role == role and item.candidate_id not in ids:
                ids.append(item.candidate_id)
    return ids


def _llm_tree_shape_is_safe(
    llm_tree: ControlledAITree,
    analyzer_tree: ControlledAITree,
    evidence: EvidenceInput,
    probe_manifest: dict | None = None,
) -> bool:
    if llm_tree.tree_id != analyzer_tree.tree_id:
        return False
    if llm_tree.final_supported_level != analyzer_tree.final_supported_level:
        return False
    if [layer.layer_id for layer in llm_tree.layers] != [layer.layer_id for layer in analyzer_tree.layers]:
        return False

    analyzer_candidates = {
        item.candidate_id: item
        for layer in analyzer_tree.layers
        for item in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    analyzer_candidate_ids = set(analyzer_candidates)
    valid_paths = _collect_evidence_paths(evidence)
    max_level_order = _level_order(analyzer_tree.final_supported_level)
    llm_candidate_ids = set()

    for layer in llm_tree.layers:
        for item in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]:
            base = analyzer_candidates.get(item.candidate_id)
            if base is None:
                return False
            llm_candidate_ids.add(item.candidate_id)
            if any(parent_id not in analyzer_candidate_ids for parent_id in item.parent_candidate_ids):
                return False
            if item.origin_parent_candidate_id:
                if item.origin_parent_candidate_id not in item.parent_candidate_ids:
                    return False
                if base.origin_parent_candidate_id and item.origin_parent_candidate_id != base.origin_parent_candidate_id:
                    return False
            elif base.origin_parent_candidate_id and item.depth_kind != "base":
                return False
            if not _candidate_status_is_safe(base.status, item.status):
                return False
            if _level_order(item.supported_level) > max_level_order:
                return False
            if _level_order(item.supported_level) > _level_order(base.supported_level):
                return False
            refs = [
                *item.evidence_refs,
                *item.self_challenge.supporting_evidence_refs,
                *item.self_challenge.opposing_evidence_refs,
            ]
            if any(not _ref_exists(ref, valid_paths) for ref in refs):
                return False

    if llm_candidate_ids != analyzer_candidate_ids:
        return False

    analyzer_layer_ids = {layer.layer_id for layer in analyzer_tree.layers}
    allowed_requests = _manifest_allowed_probe_requests(probe_manifest, analyzer_tree)
    for edge in llm_tree.probe_edges:
        if edge.from_layer_id not in analyzer_layer_ids:
            return False
        if edge.to_layer_id is not None and edge.to_layer_id not in analyzer_layer_ids:
            return False
        if any(candidate_id not in analyzer_candidate_ids for candidate_id in edge.from_candidate_ids):
            return False
        if any(candidate_id not in analyzer_candidate_ids for candidate_id in edge.to_candidate_ids):
            return False
        if any(request not in allowed_requests for request in edge.probe_requests):
            return False
    return True


def _candidate_status_is_safe(base_status: str, proposed_status: str) -> bool:
    if proposed_status == base_status:
        return True
    allowed_downgrades = {
        "supported": {"weakened", "missing_evidence", "contradicted", "rejected", "unknown"},
        "weakened": {"missing_evidence", "contradicted", "rejected", "unknown"},
        "missing_evidence": {"rejected", "unknown"},
        "forbidden": set(),
        "contradicted": {"rejected"},
        "rejected": set(),
        "unknown": set(),
    }
    return proposed_status in allowed_downgrades.get(base_status, set())


def _manifest_allowed_probe_requests(probe_manifest: dict | None, analyzer_tree: ControlledAITree) -> set[str]:
    allowed = {
        request
        for edge in analyzer_tree.probe_edges
        for request in edge.probe_requests
    }
    if isinstance(probe_manifest, dict):
        for item in probe_manifest.get("available_probes", []):
            if isinstance(item, dict) and item.get("evidence_family"):
                allowed.add(str(item["evidence_family"]))
    return {item for item in allowed if item}


def _level_order(level: str) -> int:
    order = {
        "resource": 0,
        "host": 1,
        "process": 2,
        "thread": 3,
        "syscall": 4,
        "dependency": 5,
        "service": 6,
        "endpoint": 7,
        "function": 8,
        "call_path": 9,
        "line": 10,
    }
    return order.get(level, 0)


def _build_controlled_tree_system_prompt() -> str:
    return """你是 Mini-Drop 的受控 AI 树生成器。

你必须输出一个 ControlledAITree JSON 对象，不能输出 markdown。

硬性规则：
1. tree_id、schema_version、source_context_hash、final_supported_level 必须沿用 Analyzer 模板。
2. layer_id 必须沿用 Analyzer 模板，不得新增或删除层。
3. candidate_id 必须来自 Analyzer 模板，不得新增候选；模板里的所有候选必须保留且只能出现一次。
4. 你可以在已有候选内重新分配 primary / secondary / rejected / unknown，并填写 parent_candidate_ids / probe_edges 的 candidate 血缘，但不得引用不存在的 candidate；正常父子边只使用 parent_candidate_ids，origin_parent_candidate_id 必须沿用 Analyzer 的唯一来源。
5. supported_level 不得超过 Analyzer 给出的 final_supported_level，也不得超过候选原始 supported_level。
6. evidence_refs 只能引用当前证据中真实存在的路径。
7. probe_edges[].probe_requests 只能选择 Probe Manifest 中的 evidence_family，不能写 probe_id，不能写任意 shell、sysctl、修复动作。
8. 你必须为每个候选填写 self_challenge：为什么是它、为什么不是其他、支持证据、反驳证据、缺失证据、什么会改变结论。
9. 证据不足时要停在当前证据支持层级，并通过 probe_edges 请求最小必要补证，而不是强行给更细结论。
10. 节点必须表达可证伪的机制判断，不得把函数名、采样占比、系统调用或等待点原样改写成根因结论。
11. clock_nanosleep、futex、epoll_wait、poll、select、pthread_cond_wait、runtime.futex 等原语只能标记为 observation_only；缺少上层业务栈时不得作为 function 根因。
12. 候选被反证时必须保留为 rejected/contradicted 节点，并用 effect=rollback、transition_type=backtrack 的边转向下一候选；不能删除失败候选后直接停止。
13. 只有具备 mechanism、target、真实支持证据且 causal_status=supported 的 root_cause/likely_root_cause 才能设置 decision=conclude。
"""


def _build_controlled_tree_user_message(
    *,
    evidence: EvidenceInput,
    analyzer_result: EvidenceAttributionResult,
    probe_manifest: dict,
) -> str:
    payload = {
        "current_evidence": json.loads(_serialize_evidence(evidence)),
        "analyzer_boundaries": {
            "facts": [item.model_dump(mode="json") for item in analyzer_result.facts],
            "localizations": [item.model_dump(mode="json") for item in analyzer_result.localizations],
            "attributions": [item.model_dump(mode="json") for item in analyzer_result.attributions],
            "missing_evidence": analyzer_result.missing_evidence,
            "blocked_upgrades": analyzer_result.blocked_upgrades,
            "collection_gaps": analyzer_result.collection_gaps,
            "allowed_cause_ids": analyzer_result.allowed_cause_ids,
            "primary_cause_id": analyzer_result.primary_cause_id,
            "conclusion_boundary": analyzer_result.conclusion_boundary.model_dump(mode="json"),
        },
        "controlled_tree_template": analyzer_result.controlled_ai_tree.model_dump(mode="json") if analyzer_result.controlled_ai_tree else None,
        "probe_registry_manifest": probe_manifest,
        "output": "只输出 ControlledAITree JSON 对象。",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _build_compact_guard_system_prompt() -> str:
    return """你是 Mini-Drop 的受控 AI 树裁决器。

完整树 JSON 过大时，你只输出 compact guard review JSON，不输出 markdown。
你不能新增候选、不能新增证据、不能新增探针，只能在系统给出的候选和 Probe Manifest 内选择。

JSON schema:
{
  "tree_id": "必须等于模板 tree_id",
  "primary": ["保留为主因的 candidate_id"],
  "secondary": ["次因 candidate_id"],
  "rejected": ["被反证压低的 candidate_id"],
  "unknown": ["证据不足不能裁决的 candidate_id"],
  "supporting_evidence_refs": ["只能引用 current_evidence 中存在的顶层证据路径"],
  "opposing_evidence_refs": ["只能引用 current_evidence 中存在的顶层证据路径"],
  "probe_requests": ["只能引用 Probe Manifest 的 evidence_family"],
  "stop_reason": "为什么停在当前层级",
  "self_challenges": {
    "candidate_id": {
      "why_this_claim": "支持/保留该候选的原因",
      "why_not_other_claims": "为什么压过或不能压过其他候选",
      "supporting_evidence_refs": ["真实证据路径"],
      "opposing_evidence_refs": ["真实证据路径"],
      "missing_evidence": ["缺失证据族"],
      "what_would_change_my_mind": "什么证据会改变结论"
    }
  },
  "candidate_updates": {
    "candidate_id": {
      "claim": "可证伪的机制结论，不是证据摘要",
      "claim_type": "root_cause | direct_root_cause | complete_source_root_cause | direct_failure_mechanism | likely_root_cause | partial_localization | observation_only | insufficient_for_root_cause | abstention",
      "causal_status": "supported | unproven | contradicted | inconclusive",
      "decision": "continue_probe | reject_candidate | conclude | abstain | backtrack",
      "mechanism": "导致症状的具体机制",
      "target": "机制作用的具体服务/进程/函数/调用路径",
      "primitive_kind": "仅当目标是等待/调度/syscall/runtime 原语时填写"
    }
  },
  "layer_summaries": {"layer_id": "该层的人话摘要"}
}

如果主候选不成立，把它放入 rejected，保留反证理由；随后从剩余候选中选择下一项继续补证。原语采样点只能作为 observation_only，不能作为根因函数。
"""


def _build_compact_guard_user_message(
    *,
    evidence: EvidenceInput,
    analyzer_tree: ControlledAITree,
    probe_manifest: dict | None,
) -> str:
    candidates = []
    for layer in analyzer_tree.layers:
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]:
            candidates.append({
                "layer_id": layer.layer_id,
                "candidate_id": node.candidate_id,
                "role": node.role,
                "status": node.status,
                "supported_level": node.supported_level,
                "claim": node.claim,
                "evidence_refs": node.evidence_refs,
                "missing_evidence": node.self_challenge.missing_evidence,
            })
    payload = {
        "current_evidence": _compact_evidence_snapshot(evidence),
        "tree_id": analyzer_tree.tree_id,
        "final_supported_level": analyzer_tree.final_supported_level,
        "candidate_template": candidates,
        "existing_probe_edges": [edge.model_dump(mode="json") for edge in analyzer_tree.probe_edges],
        "probe_registry_manifest": probe_manifest,
        "output": "只输出 compact guard review JSON 对象。",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _compact_evidence_snapshot(evidence: EvidenceInput) -> dict:
    snapshot: dict[str, object] = {}
    if evidence.task_metadata:
        snapshot["task_metadata"] = {
            key: evidence.task_metadata.get(key)
            for key in ("task_id", "collector_type", "target_pid", "status")
            if key in evidence.task_metadata
        }
    if evidence.top_functions:
        snapshot["top_functions"] = evidence.top_functions[:5]
    if evidence.sys_metrics:
        summary = evidence.sys_metrics.get("summary") if isinstance(evidence.sys_metrics, dict) else None
        snapshot["sys_metrics"] = {"summary": summary} if summary else evidence.sys_metrics
    if evidence.ebpf_metrics:
        snapshot["ebpf_metrics"] = evidence.ebpf_metrics
    if evidence.off_cpu_wait_json:
        snapshot["off_cpu_wait_json"] = evidence.off_cpu_wait_json
    if evidence.baseline_diff:
        snapshot["baseline_diff"] = evidence.baseline_diff
    if evidence.tool_results:
        snapshot["tool_results"] = [
            {
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
                "evidence_ref": item.get("evidence_ref"),
                "error_message": item.get("error_message"),
            }
            for item in evidence.tool_results[:6]
            if isinstance(item, dict)
        ]
    if evidence.failure_events:
        snapshot["failure_events"] = evidence.failure_events[-5:]
    if evidence.source_context:
        snapshot["source_context"] = {
            key: evidence.source_context.get(key)
            for key in ("service_id", "instance_id", "endpoint", "call_path")
            if key in evidence.source_context
        }
    return snapshot


def _extract_json(raw: str | None) -> str | None:
    """从 LLM 原始输出中提取 JSON。

    处理以下情况：
      - 纯 JSON
      - ```json ... ``` 包裹
      - ``` ... ``` 包裹
    """
    if not raw:
        return None
    text = raw.strip()

    # 尝试匹配 ```json ... ``` 或 ``` ... ```
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 尝试找到第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]

    return None


def _collect_evidence_paths(evidence: EvidenceInput) -> dict[str, set[str]]:
    """收集 evidence 中所有可引用的字段路径。

    Returns:
        {"top_functions": {"name", "samples", "percent"}, ...}
    """
    paths: dict[str, set[str]] = {}

    if evidence.top_functions:
        paths["top_functions"] = set()
        for item in evidence.top_functions[:3]:
            paths["top_functions"].update(item.keys())

    if evidence.ebpf_metrics:
        paths["ebpf_metrics"] = set(evidence.ebpf_metrics.keys())

    if evidence.baseline_diff:
        paths["baseline_diff"] = set(evidence.baseline_diff.keys())

    if evidence.agent_stats:
        paths["agent_stats"] = set(evidence.agent_stats.keys())

    if evidence.evidence_index:
        paths["evidence_index"] = _collect_index_paths(evidence.evidence_index)

    if evidence.task_metadata:
        paths["task_metadata"] = set(evidence.task_metadata.keys())

    if evidence.tool_results:
        # tool_results can be referenced by tool_name and also by generic keys
        tool_paths: set[str] = set()
        for item in evidence.tool_results:
            tn = item.get("tool_name", "")
            if tn:
                tool_paths.add(tn)
            # collect common tool result top-level fields
            for field in ("status", "evidence_ref", "tool_name"):
                val = item.get(field)
                if val:
                    tool_paths.add(f"{tn}.{field}" if tn else field)
            # also collect sub-keys of tool output so LLM can reference them
            out = item.get("output", {}) if isinstance(item.get("output"), dict) else {}
            for k in out.keys():
                tool_paths.add(f"{tn}.output.{k}" if tn else k)
        paths["tool_results"] = tool_paths

    # Top-level scalar fields on EvidenceInput — LLM can reference them directly
    if evidence.failure_events:
        paths["failure_events"] = set()  # list field — any value is valid
    if evidence.suggestions:
        paths["suggestions"] = set()  # list field
    if evidence.sys_metrics:
        paths["sys_metrics"] = set(evidence.sys_metrics.keys()) if isinstance(evidence.sys_metrics, dict) else set()

    if evidence.analysis_result:
        paths["analysis_result"] = set(evidence.analysis_result.keys())

    return paths


def _collect_index_paths(value, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            next_prefix = f"{prefix}.{key_text}" if prefix else key_text
            paths.add(next_prefix)
            paths.update(_collect_index_paths(item, next_prefix))
        return paths
    if isinstance(value, list):
        for item in value:
            paths.update(_collect_index_paths(item, prefix))
        return paths
    if prefix:
        paths.add(prefix)
    return paths


def _ref_exists(ref: str, valid_paths: dict[str, set[str]]) -> bool:
    """检查 evidence_ref 是否在有效路径集合中。

    支持格式：
      - "top_functions[0]" → 索引被去掉，检查顶层 key 存在
      - "task_metadata.status" → 检查嵌套路径
      - "tool_results[3].output.failure_reasons" → LLM 用索引引用 tool_results，
        校验时去掉索引 + tool_name 前缀做 lenient 匹配
    """
    # 去掉索引后缀: "top_functions[0]" → "top_functions"
    base = re.sub(r"\[\d+\]", "", ref)
    # 取顶层 key
    top = base.split(".")[0]

    if top not in valid_paths:
        return False

    # 没有子路径 → 顶层 key 存在即通过
    if "." not in base:
        return True

    sub = base.split(".", 1)[1]
    sub_paths = valid_paths[top]

    # 精确匹配
    if sub in sub_paths:
        return True

    # Lenient: tool_results 的 LLM 可能用索引或省略 tool_name 前缀
    #   e.g. ref="tool_results.output.failure_reasons"
    #   而 valid path 是 "inspect_task_events.output.failure_reasons"
    #   检查是否有任何 valid path 末尾段匹配
    if top == "tool_results":
        for valid_sub in sub_paths:
            if valid_sub.endswith("." + sub.split(".", 1)[-1] if "." in sub else sub):
                return True
            # Also check if ref's output.{key} matches valid's output.{key}
            if sub.startswith("output."):
                out_key = sub.split("output.", 1)[-1]
                if valid_sub.endswith(f".output.{out_key}"):
                    return True

    return False


def _fallback_report(task_id: str, evidence: EvidenceInput, candidates_json: str = "[]") -> ValidatedReport:
    """API Key 未配置时的降级报告（纯规则引擎输出）。"""
    ranked: list[CauseEntry] = []
    try:
        candidates = json.loads(candidates_json)
    except json.JSONDecodeError:
        candidates = []

    for item in candidates[:3]:
        confidence = float(item.get("final_confidence", 0.0))
        if item.get("candidate_id") == "insufficient_data":
            continue
        ranked.append(CauseEntry(
            cause_id=item.get("candidate_id", "unknown"),
            confidence=confidence,
            claim=item.get("description", "规则引擎候选归因"),
            evidence_refs=item.get("evidence_refs", []),
            uncertainties=item.get("missing_evidence", []),
            verification_steps=["补充采集或对比 baseline 后复核该结论"],
        ))

    not_enough = len(ranked) == 0
    report = DiagnosisReport(
        summary="未配置 DEEPSEEK_API_KEY，归因引擎使用规则候选与工具证据生成降级报告。",
        ranked_causes=ranked,
        facts=evidence.suggestions if evidence.suggestions else ["无规则命中"],
        not_enough_evidence=not_enough,
    )
    return ValidatedReport(
        task_id=task_id,
        model_name="rule-engine-only",
        evidence_snapshot=evidence.model_dump() if isinstance(evidence, EvidenceInput) else {},
        report=_attach_analysis_result(report, evidence),
        validated=True,
    )
