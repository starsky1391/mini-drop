#!/usr/bin/env python3
"""Replay a Celery run report without adding answer or Oracle information."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any


def _claim_hash(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"[\s,，.。:：;；!！?？]+$", "", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected an object: {path}")
    return payload


def _evidence_index(evidence: list[dict[str, Any]]) -> tuple[set[str], dict[str, str], dict[str, str]]:
    refs: set[str] = set()
    families: dict[str, str] = {}
    statuses: dict[str, str] = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        candidates = [
            item.get("evidence_id"),
            item.get("evidence_ref"),
            item.get("raw_artifact_ref"),
            item.get("derived_artifact_ref"),
        ]
        item_refs = {str(value) for value in candidates if value}
        refs.update(item_refs)
        observed_value = item.get("observed_value")
        observed_value = observed_value if isinstance(observed_value, dict) else {}
        family = str(
            item.get("query_or_probe")
            or observed_value.get("collector_type")
            or "unknown"
        )
        summary = observed_value.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        confidence_inputs = summary.get("confidence_inputs")
        confidence_inputs = (
            confidence_inputs
            if isinstance(confidence_inputs, dict)
            else {}
        )
        raw_confidence_inputs = summary.get("confidence_inputs")
        if isinstance(raw_confidence_inputs, dict):
            family_statuses = confidence_inputs.get("evidence_validity_by_family")
            if isinstance(family_statuses, dict):
                status_value = (
                    family_statuses.get(family)
                    or family_statuses.get("status")
                    or family_statuses
                )
            else:
                status_value = family_statuses
        else:
            status_value = raw_confidence_inputs
        data_quality = item.get("data_quality")
        if isinstance(data_quality, dict):
            completeness = data_quality.get("completeness")
        else:
            completeness = data_quality
        status = str(status_value or completeness or "unknown")
        for ref in item_refs:
            families[ref] = family
            statuses[ref] = status
    return refs, families, statuses


def _probe_summary(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        result.append({
            "probe_id": probe.get("probe_id"),
            "status": probe.get("status"),
            "evidence_status": probe.get("evidence_status"),
            "evidence_reason": probe.get("evidence_reason"),
            "reason": probe.get("reason"),
            "updated_at": probe.get("updated_at"),
        })
    return result


def _tree_summary(tree: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(tree, dict):
        return {
            "present": False,
            "ai_candidate_ids": [],
            "fallback_or_observation_ids": [],
            "missing_parent_ids": [],
            "orphan_node_ids": [],
            "line_node_ids": [],
            "nodes": [],
        }
    ai_ids: list[str] = []
    fallback_ids: list[str] = []
    known_ids: set[str] = set()
    parent_refs: list[tuple[str, str]] = []
    node_summaries: list[dict[str, Any]] = []
    orphan_nodes: list[str] = []
    line_nodes: list[str] = []
    claim_hashes: dict[str, str] = {}
    history_in_session_main: list[str] = []
    for layer in tree.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            for node in layer.get(group, []):
                if not isinstance(node, dict):
                    continue
                candidate_id = str(node.get("candidate_id") or "")
                if not candidate_id:
                    continue
                known_ids.add(candidate_id)
                claim_hashes[candidate_id] = str(node.get("claim_hash") or _claim_hash(node.get("claim")))
                if (
                    tree.get("tree_kind") in {None, "session_main"}
                    and (node.get("generated_by") == "history" or node.get("claim_transform") == "restored")
                ):
                    history_in_session_main.append(candidate_id)
                if (
                    node.get("generated_by") in {"ai_candidate", "ai_guarded"}
                    and candidate_id.startswith("ai_candidate_")
                ):
                    ai_ids.append(candidate_id)
                if node.get("generated_by") in {
                    "analyzer_fallback",
                    "analyzer_observation",
                    "fallback_observation",
                }:
                    fallback_ids.append(candidate_id)
                parents = [str(value) for value in node.get("parent_candidate_ids", []) if str(value)]
                origin = str(node.get("origin_parent_candidate_id") or "")
                node_summaries.append({
                    "candidate_id": candidate_id,
                    "generated_by": node.get("generated_by"),
                    "relation": node.get("relation"),
                    "node_type": node.get("node_type"),
                    "supported_level": node.get("supported_level"),
                    "status": node.get("status"),
                    "causal_status": node.get("causal_status"),
                    "parent_candidate_ids": parents,
                    "origin_parent_candidate_id": origin,
                    "conclusion_eligible": bool(node.get("conclusion_eligible")),
                })
                if node.get("node_type") == "orphan" or (
                    node.get("relation") not in {None, "root"} and not parents
                ):
                    orphan_nodes.append(candidate_id)
                if (
                    node.get("node_type") == "line_anchor"
                    or (
                        node.get("supported_level") == "line"
                        and node.get("relation") == "refinement"
                        and node.get("node_type") not in {
                            "observation",
                            "mechanism_explanation",
                            "stop_boundary",
                            "orphan",
                        }
                    )
                ):
                    line_nodes.append(candidate_id)
                for parent_id in node.get("parent_candidate_ids", []):
                    parent_refs.append((candidate_id, str(parent_id)))
    missing_parent_ids = sorted({
        parent_id
        for _, parent_id in parent_refs
        if parent_id and parent_id not in known_ids
    })
    duplicate_parent_child_claims = sorted({
        f"{parent_id}->{candidate_id}"
        for candidate_id, parent_id in parent_refs
        if parent_id in claim_hashes
        and claim_hashes.get(candidate_id)
        and claim_hashes[candidate_id] == claim_hashes[parent_id]
    })
    return {
        "present": True,
        "tree_kind": tree.get("tree_kind"),
        "renderable": tree.get("renderable"),
        "ai_candidate_ids": sorted(set(ai_ids)),
        "fallback_or_observation_ids": sorted(set(fallback_ids)),
        "missing_parent_ids": missing_parent_ids,
        "orphan_node_ids": sorted(set(orphan_nodes)),
        "line_node_ids": sorted(set(line_nodes)),
        "duplicate_parent_child_claims": duplicate_parent_child_claims,
        "history_in_session_main": sorted(set(history_in_session_main)),
        "nodes": node_summaries[:256],
    }


def replay_report(report: dict[str, Any]) -> dict[str, Any]:
    vulnerable = report.get("vulnerable") if isinstance(report.get("vulnerable"), dict) else {}
    diagnosis = vulnerable.get("diagnosis") if isinstance(vulnerable.get("diagnosis"), dict) else {}
    detail = diagnosis.get("detail") if isinstance(diagnosis.get("detail"), dict) else {}
    evidence = detail.get("evidence") if isinstance(detail.get("evidence"), list) else []
    refs, families, statuses = _evidence_index(evidence)
    latest = detail.get("latest_conclusion") if isinstance(detail.get("latest_conclusion"), dict) else {}
    retained = latest.get("retained_conclusion") if isinstance(latest.get("retained_conclusion"), dict) else {}
    assessment = latest.get("cluster_assessment") if isinstance(latest.get("cluster_assessment"), dict) else {}
    anchor = assessment.get("primary_anchor") if isinstance(assessment.get("primary_anchor"), dict) else {}
    candidate_review = latest.get("candidate_review") if isinstance(latest.get("candidate_review"), dict) else {}
    candidate_generation_output = (
        latest.get("candidate_generation_output")
        if isinstance(latest.get("candidate_generation_output"), dict)
        else {}
    )
    candidate_validation = (
        latest.get("candidate_validation_diagnostics")
        if isinstance(latest.get("candidate_validation_diagnostics"), list)
        else candidate_review.get("validation_diagnostics", [])
        if isinstance(candidate_review.get("validation_diagnostics"), list)
        else []
    )
    tree = latest.get("controlled_ai_tree") if isinstance(latest.get("controlled_ai_tree"), dict) else None
    probes = detail.get("probes") if isinstance(detail.get("probes"), list) else []

    heap_probe = [
        item for item in probes
        if isinstance(item, dict)
        and (
            item.get("probe_id") == "python_heap_profile"
            or str(item.get("probe_id") or "").endswith("_python_heap_profile")
        )
    ]
    heap_statuses = sorted({
        str(item.get("evidence_status") or item.get("status") or "unknown")
        for item in heap_probe
    })
    heap_available = any(status in {"valid", "partial"} for status in heap_statuses)
    line_anchor_present = bool(anchor.get("file")) and int(anchor.get("line") or 0) > 0
    tree_summary = _tree_summary(tree)
    ai_candidate_ids = tree_summary["ai_candidate_ids"]
    canonical_probe_plan = tree.get("canonical_probe_plan", []) if isinstance(tree, dict) else []
    source_query_entries = [
        item
        for item in canonical_probe_plan
        if isinstance(item, dict) and item.get("evidence_family") == "source_mechanism_query"
    ]
    source_query_hashes = [
        str(((item.get("probe_input") or {}).get("ai_generated_query") or {}).get("query_spec_hash") or "")
        for item in source_query_entries
    ]

    gate_failures = [
        item for item in (latest.get("gate_failures") or [])
        if isinstance(item, dict)
    ]
    if not ai_candidate_ids:
        gate_failures.append({
            "gate": "ai_candidate_generation",
            "status": "failed",
            "reason": "旧报告没有可用 AI candidate；以下保留报告中可见的候选生成状态、输出摘要和字段级门禁信息。",
            "observed": {
                "ai_review_status": candidate_review.get("ai_review_status"),
                "ai_review_error": candidate_review.get("ai_review_error"),
                "candidate_proposals": candidate_review.get("candidate_proposals", []),
                "candidate_generation_attempts": candidate_review.get("candidate_generation_attempts", []),
                "validation_diagnostics": candidate_validation,
                "initial_evidence_context": candidate_review.get("initial_evidence_context", {}),
            },
        })
    if not heap_available:
        gate_failures.append({
            "gate": "python_heap_profile",
            "status": "missing_evidence",
            "reason": "没有 valid/partial 的 Python heap 证据，RSS 增长不能升级为 Python retention 机制。",
            "observed": {
                "probe_count": len(heap_probe),
                "statuses": heap_statuses,
                "next_evidence_requests": latest.get("next_evidence_requests", []),
            },
        })
    if not line_anchor_present:
        gate_failures.append({
            "gate": "verified_source_line",
            "status": "not_reached",
            "reason": "初始证据只支持进程/函数观察，primary_anchor 没有可验证文件和行号。",
            "observed": {
                "supported_level": assessment.get("supported_level"),
                "anchor": anchor,
                "blocked_upgrade_reason": anchor.get("blocked_upgrade_reason"),
            },
        })
    if not latest.get("root_cause_clusters"):
        gate_failures.append({
            "gate": "formal_root_cause",
            "status": "abstained",
            "reason": "没有候选同时满足机制、目标、同窗证据、证据引用和因果链闭合。",
            "observed": {
                "formal_root_cause": latest.get("formal_root_cause"),
                "root_cause_candidates": latest.get("root_cause_candidates", []),
                "causal_chain": latest.get("causal_chain", []),
            },
        })

    deduped_gate_failures = []
    seen_gate_failures = set()
    for item in gate_failures:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("gate") or ""),
            str(item.get("failure_code") or item.get("status") or ""),
            str(item.get("candidate_id") or ""),
            str(item.get("reason") or ""),
        )
        if key in seen_gate_failures:
            continue
        seen_gate_failures.add(key)
        deduped_gate_failures.append(item)
    gate_failures = deduped_gate_failures

    return {
        "replay_scope": "existing_report_only",
        "answer_sources_used": [],
        "source_report_stage": vulnerable.get("stage_role"),
        "source_revision": (vulnerable.get("runtime_manifest") or {})
        .get("source_context", {})
        .get("repo_revision"),
        "initial_evidence": {
            "count": len(evidence),
            "refs": sorted(refs),
            "families": dict(sorted(families.items())),
            "statuses": dict(sorted(statuses.items())),
            "rss_observation": {
                "claim": retained.get("claim"),
                "evidence_refs": retained.get("evidence_refs", []),
                "supported_level": retained.get("supported_level"),
            },
        },
        "probe_summary": _probe_summary(probes),
        "ai_tree": tree_summary,
        "canonical_acceptance": {
            "no_duplicate_parent_child_claims": not tree_summary["duplicate_parent_child_claims"],
            "no_history_in_session_main": not tree_summary["history_in_session_main"],
            "source_mechanism_query_hash_preserved": (
                not source_query_entries or all(source_query_hashes)
            ),
            "source_mechanism_query_hashes": source_query_hashes,
            "probe_conflicts": tree.get("probe_conflicts", []) if isinstance(tree, dict) else [],
        },
        "candidate_generation": {
            "status": candidate_review.get("ai_review_status"),
            "attempts": candidate_review.get("candidate_generation_attempts", []),
            "validation_diagnostics": candidate_validation,
            "initial_evidence_context": candidate_review.get("initial_evidence_context", {}),
            "output": candidate_generation_output,
            "candidate_proposals": candidate_review.get("candidate_proposals", []),
        },
        "investigation_review": {
            "status": (latest.get("investigation_review") or {}).get("ai_review_status"),
            "selected_evidence_families": (latest.get("investigation_review") or {}).get("selected_evidence_families", []),
            "probe_inputs": (latest.get("investigation_review") or {}).get("probe_inputs", {}),
        },
        "line_probe": [
            {
                "probe_id": item.get("probe_id"),
                "status": item.get("status"),
                "evidence_status": item.get("evidence_status"),
                "reason": item.get("reason"),
                "evidence_reason": item.get("evidence_reason"),
                "parameters": {
                    key: item.get("parameters", {}).get(key)
                    for key in (
                        "candidate_id",
                        "origin_parent_candidate_id",
                        "evidence_gap",
                        "source_root",
                        "repo_revision",
                    )
                    if isinstance(item.get("parameters"), dict) and item.get("parameters", {}).get(key)
                },
            }
            for item in probes
            if isinstance(item, dict)
            and (
                str(item.get("probe_id") or "").lower() in {
                    "source_snapshot",
                    "source_mechanism_query",
                    "line_level_profile",
                }
                or "codeql" in str(item.get("probe_id") or "").lower()
            )
        ],
        "gate_failures": gate_failures,
        "line_anchor_eligibility": (
            tree.get("line_anchor_eligibility", {})
            if isinstance(tree, dict)
            else {}
        ),
        "heap_probe_outcome": (
            tree.get("heap_probe_outcome", {})
            if isinstance(tree, dict)
            else {}
        ),
        "retained_parent_conclusions": latest.get("retained_parent_conclusions", []),
        "final_state": {
            "diagnosis_status": detail.get("status"),
            "runner_status": diagnosis.get("runner_status"),
            "terminal": diagnosis.get("terminal"),
            "abstained": latest.get("abstained"),
            "formal_root_cause": latest.get("formal_root_cause"),
            "root_cause_clusters": latest.get("root_cause_clusters", []),
            "retained_conclusion": retained,
            "qualification_boundary": latest.get("qualification_boundary"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = replay_report(_read_json(args.report))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "gate_failure_count": len(result["gate_failures"]),
        "abstained": result["final_state"]["abstained"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
