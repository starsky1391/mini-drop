#!/usr/bin/env python3
"""Replay a Celery run report without adding answer or Oracle information."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


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
        family = str(
            item.get("query_or_probe")
            or (item.get("observed_value") or {}).get("collector_type")
            or "unknown"
        )
        status = str(
            ((item.get("observed_value") or {}).get("summary") or {})
            .get("confidence_inputs", {})
            .get("evidence_validity_by_family", {})
            or (item.get("data_quality") or {}).get("completeness")
            or "unknown"
        )
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
        }
    ai_ids: list[str] = []
    fallback_ids: list[str] = []
    known_ids: set[str] = set()
    parent_refs: list[tuple[str, str]] = []
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
                for parent_id in node.get("parent_candidate_ids", []):
                    parent_refs.append((candidate_id, str(parent_id)))
    missing_parent_ids = sorted({
        parent_id
        for _, parent_id in parent_refs
        if parent_id and parent_id not in known_ids
    })
    return {
        "present": True,
        "tree_kind": tree.get("tree_kind"),
        "renderable": tree.get("renderable"),
        "ai_candidate_ids": sorted(set(ai_ids)),
        "fallback_or_observation_ids": sorted(set(fallback_ids)),
        "missing_parent_ids": missing_parent_ids,
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
    ai_candidate_ids = _tree_summary(tree)["ai_candidate_ids"]

    gate_failures = []
    if not ai_candidate_ids:
        gate_failures.append({
            "gate": "ai_candidate_generation",
            "status": "failed",
            "reason": "旧报告没有可用 AI candidate；candidate_review 只记录了失败状态，未保存当时的字段级诊断。",
            "observed": {
                "ai_review_status": candidate_review.get("ai_review_status"),
                "ai_review_error": candidate_review.get("ai_review_error"),
                "candidate_proposals": candidate_review.get("candidate_proposals", []),
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
        "ai_tree": _tree_summary(tree),
        "gate_failures": gate_failures,
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
