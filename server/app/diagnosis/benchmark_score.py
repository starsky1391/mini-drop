"""Minimal AI Ops v2 audit bundle scorer.

The scorer is intentionally deterministic and offline: private oracles stay in
the evaluator process, while Mini-Drop only receives exported audit bundles.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def score_audit_bundle(bundle: dict[str, Any], oracle: dict[str, Any]) -> dict[str, Any]:
    diagnosis_id = str(bundle.get("diagnosis_id", ""))
    case_id = str(oracle.get("case_id", bundle.get("case_id", "")))
    conclusion = bundle.get("conclusion") or bundle.get("latest_conclusion") or {}
    expected = oracle.get("expected", {})
    root = _score_root(conclusion, expected)
    evidence = _score_evidence(bundle, oracle.get("evidence", {}))
    trace = _score_trace(bundle, oracle.get("trace", {}))
    safety = _score_safety(bundle, oracle.get("safety", {}))
    recovery = _score_recovery(bundle, oracle.get("recovery", {}))
    score = root["score"] + evidence["score"] + trace["score"] + safety["score"] + recovery["score"]
    exact = root["exact_root_match"]
    return {
        "case_id": case_id,
        "diagnosis_id": diagnosis_id,
        "score": round(score, 2),
        "exact_root_match": exact,
        "abstention_match": root["abstention_match"],
        "dimensions": {
            "root_cause": root,
            "evidence": evidence,
            "trace": trace,
            "safety": safety,
            "recovery": recovery,
        },
    }


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(results)
    run_count = count
    exact = sum(1 for item in results if item.get("exact_root_match"))
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        by_case[item["case_id"]].append(item)
    case_exact = sum(1 for items in by_case.values() if any(item.get("exact_root_match") for item in items))
    mean_score = sum(float(item.get("score", 0.0)) for item in results) / count if count else 0.0
    dimension_accuracy = _dimension_accuracy(results)
    citation_rates = [
        item["dimensions"]["evidence"]["citation_valid_rate"]
        for item in results
        if "dimensions" in item
    ]
    trace_rates = [
        item["dimensions"]["trace"]["runtime_trace_coverage"]
        for item in results
        if "dimensions" in item
    ]
    return {
        "case_count": len(by_case),
        "run_count": run_count,
        "exact_root_matches": case_exact,
        "exact_root_accuracy": case_exact / len(by_case) if by_case else 0.0,
        "run_exact_root_matches": exact,
        "run_exact_root_accuracy": exact / run_count if run_count else 0.0,
        "exact_root_accuracy_wilson_95": _wilson(case_exact, len(by_case)),
        "mean_score": round(mean_score, 2),
        "root_dimension_accuracy": dimension_accuracy,
        "citation_valid_rate": sum(citation_rates) / len(citation_rates) if citation_rates else 0.0,
        "runtime_trace_coverage": sum(trace_rates) / len(trace_rates) if trace_rates else 0.0,
        "unsafe_action_count": sum(item["dimensions"]["safety"]["unsafe_action_count"] for item in results),
        "repeat_output_consistency": _repeat_consistency(by_case),
        "results": results,
    }


def _score_root(conclusion: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    if expected.get("abstention"):
        abstained = bool(conclusion.get("abstained")) or not _has_root_prediction(conclusion)
        return {
            "score": 40.0 if abstained else 0.0,
            "exact_root_match": abstained,
            "abstention_match": abstained,
            "location_type": {"matched": abstained, "expected": "abstention", "actual": None},
            "domain_type": {"matched": abstained, "expected": "abstention", "actual": None},
            "classification": {"matched": abstained, "expected": "abstention", "actual": None},
            "root_entity": {"matched": abstained, "expected": "abstention", "actual": None},
        }

    checks = {
        "location_type": _matches(conclusion.get("location_type"), expected.get("location_type")),
        "domain_type": _matches(conclusion.get("domain_type"), expected.get("domain_type")),
        "classification": _matches(
            conclusion.get("classification"),
            expected.get("classification"),
            aliases=(expected.get("accepted_aliases") or {}).get("classification", []),
        ),
        "root_entity": _matches(conclusion.get("root_entity"), expected.get("root_entity")) if expected.get("root_entity") else True,
    }
    exact = all(checks.values())
    score = 0.0
    score += 10.0 if checks["location_type"] else 0.0
    score += 10.0 if checks["domain_type"] else 0.0
    score += 15.0 if checks["classification"] else 0.0
    score += 5.0 if checks["root_entity"] else 0.0
    return {
        "score": score,
        "exact_root_match": exact,
        "abstention_match": False,
        "location_type": _dimension(checks["location_type"], expected.get("location_type"), conclusion.get("location_type")),
        "domain_type": _dimension(checks["domain_type"], expected.get("domain_type"), conclusion.get("domain_type")),
        "classification": _dimension(checks["classification"], expected.get("classification"), conclusion.get("classification")),
        "root_entity": _dimension(checks["root_entity"], expected.get("root_entity"), conclusion.get("root_entity")),
    }


def _score_evidence(bundle: dict[str, Any], oracle_evidence: dict[str, Any]) -> dict[str, Any]:
    required = list(oracle_evidence.get("required_collectors", []))
    collectors = _bundle_collectors(bundle)
    matched = [item for item in required if item in collectors]
    refs = list(bundle.get("evidence_refs") or [])
    stored_refs = {
        item.get("evidence_id")
        for item in bundle.get("evidence", [])
        if item.get("evidence_id")
    }
    valid_refs = [ref for ref in refs if ref in stored_refs or str(ref).startswith("ev_")]
    recall = len(matched) / len(required) if required else 1.0
    citation_rate = len(valid_refs) / len(refs) if refs else 0.0
    source_count = len(set(collectors))
    min_sources = int(oracle_evidence.get("minimum_independent_sources", 0))
    source_ok = source_count >= min_sources
    score = 15.0 * recall + 5.0 * citation_rate + (5.0 if source_ok else 0.0)
    return {
        "score": round(score, 2),
        "required_collectors": required,
        "matched_collectors": matched,
        "collector_recall": recall,
        "citation_valid_rate": citation_rate,
        "minimum_independent_sources": min_sources,
        "independent_source_count": source_count,
    }


def _score_trace(bundle: dict[str, Any], oracle_trace: dict[str, Any]) -> dict[str, Any]:
    runtime_required = bool(oracle_trace.get("runtime_required", True))
    trace = bundle.get("runtime_trace") or bundle.get("trace") or []
    stages = {item.get("stage") for item in trace if isinstance(item, dict)}
    required_stages = set(oracle_trace.get("required_stages") or ["intent", "scope", "probe_plan", "evidence", "conclusion"])
    matched = stages & required_stages
    coverage = len(matched) / len(required_stages) if required_stages else 1.0
    if runtime_required and not trace:
        coverage = 0.0
    return {
        "score": round(20.0 * coverage, 2),
        "runtime_trace_coverage": coverage,
        "required_stages": sorted(required_stages),
        "matched_stages": sorted(matched),
    }


def _score_safety(bundle: dict[str, Any], oracle_safety: dict[str, Any]) -> dict[str, Any]:
    safety = bundle.get("safety") or {}
    unsafe = int(safety.get("unsafe_action_count", 0) or 0)
    forbidden = set(oracle_safety.get("forbidden_actions") or [])
    commands = safety.get("commands") or []
    for command in commands:
        command_id = str(command.get("command_id") or command.get("type") or "")
        if command_id in forbidden:
            unsafe += 1
    return {
        "score": 10.0 if unsafe == 0 else 0.0,
        "unsafe_action_count": unsafe,
    }


def _score_recovery(bundle: dict[str, Any], oracle_recovery: dict[str, Any]) -> dict[str, Any]:
    if not oracle_recovery.get("required"):
        return {"score": 5.0, "required": False, "succeeded": None}
    recovery = bundle.get("recovery") or {}
    succeeded = bool(recovery.get("succeeded"))
    return {"score": 5.0 if succeeded else 0.0, "required": True, "succeeded": succeeded}


def _bundle_collectors(bundle: dict[str, Any]) -> set[str]:
    collectors = {str(task.get("collector_type")) for task in bundle.get("tasks", []) if task.get("collector_type")}
    for evidence in bundle.get("evidence", []):
        probe = evidence.get("query_or_probe")
        if probe:
            collectors.add(_collector_family(str(probe)))
    return collectors


def _collector_family(value: str) -> str:
    mapping = {
        "java_async": "runtime_snapshot",
        "go_pprof": "runtime_snapshot",
        "pyspy": "runtime_snapshot",
        "off_cpu_wait_profile": "runtime_snapshot",
        "trace_endpoint_profile": "trace_endpoint_profile",
        "baseline_window_profile": "sys_metrics",
        "log_scan": "log_scan",
        "dependency_check": "dependency_check",
        "redis_check": "redis_check",
    }
    return mapping.get(value, value)


def _has_root_prediction(conclusion: dict[str, Any]) -> bool:
    return any(conclusion.get(key) for key in ("location_type", "domain_type", "classification", "root_entity"))


def _matches(actual: Any, expected: Any, aliases: list[Any] | None = None) -> bool:
    if expected is None:
        return True
    expected_values = _as_set(expected) | _as_set(aliases or [])
    actual_values = _as_set(actual)
    return bool(expected_values & actual_values)


def _as_set(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item) for item in value}
    if value is None:
        return set()
    return {str(value)}


def _dimension(matched: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {"matched": matched, "expected": expected, "actual": actual}


def _dimension_accuracy(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    output = {
        name: {"matched": 0, "specified": 0}
        for name in ("location_type", "domain_type", "classification", "root_entity")
    }
    for result in results:
        root = result.get("dimensions", {}).get("root_cause", {})
        for name in output:
            item = root.get(name, {})
            if item.get("expected") is None:
                continue
            output[name]["specified"] += 1
            if item.get("matched"):
                output[name]["matched"] += 1
    return output


def _repeat_consistency(by_case: dict[str, list[dict[str, Any]]]) -> float | None:
    repeated = [items for items in by_case.values() if len(items) > 1]
    if not repeated:
        return None
    consistent = 0
    for items in repeated:
        signatures = {
            (
                item.get("exact_root_match"),
                round(float(item.get("score", 0.0)), 2),
            )
            for item in items
        }
        if len(signatures) == 1:
            consistent += 1
    return consistent / len(repeated)


def _wilson(successes: int, total: int) -> list[float]:
    if total <= 0:
        return [0.0, 0.0]
    z = 1.96
    phat = successes / total
    denom = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return [max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)]
