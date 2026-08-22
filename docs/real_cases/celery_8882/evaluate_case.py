#!/usr/bin/env python3
"""Evaluate the Celery vulnerable/fixed VM pair offline.

This evaluator is intentionally outside the diagnosis request path. It reads
the private oracle and the two VM result/evidence directories only after both
stages have finished.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from datetime import datetime
from statistics import mean


ROOT = Path(__file__).resolve().parent
VULNERABLE_REVISION = "a83070e5ec748c32325332db422756cfdd709aae"
FIXED_REVISION = "ca2d22204a47d7c7d553ae1408a099ab10caf055"
MIN_RSS_DELTA = 16 * 1024 * 1024
MIN_BATCH_RSS_DELTA = 4 * 1024 * 1024
MIN_BATCH_RELATIVE_MULTIPLIER = 4
RUNTIME_FORBIDDEN_PATTERNS = {
    "issue_8882": re.compile(r"(?:celery[-_]?8882|issue[-_# ]?8882|#8882)"),
    "pr_9799": re.compile(r"(?:pr[-_# ]?9799|pull[-_ ]?9799|#9799)"),
    "vulnerable_label": re.compile(r"\bvulnerable\b"),
    "fixed_label": re.compile(r"\bfixed\b"),
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_ndjson(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def rss_summary(evidence_dir: Path) -> dict:
    rows = [row for row in read_ndjson(evidence_dir / "worker_observations.ndjson") if row.get("rss_bytes")]
    values = [int(row["rss_bytes"]) for row in rows]
    if not values:
        return {"sample_count": 0, "initial_bytes": None, "final_bytes": None, "delta_bytes": None}
    head = values[: max(1, min(5, len(values)))]
    tail = values[-max(1, min(5, len(values))):]
    initial = mean(head)
    final = mean(tail)
    return {
        "sample_count": len(values),
        "initial_bytes": round(initial),
        "final_bytes": round(final),
        "delta_bytes": round(final - initial),
        "minimum_bytes": min(values),
        "maximum_bytes": max(values),
    }


def rss_batch_summary(evidence_dir: Path) -> dict:
    task_rows = read_ndjson(evidence_dir / "task_observations.ndjson")
    barriers = [row for row in task_rows if row.get("event") == "failure_batch_barrier" and row.get("rss_bytes")]
    if barriers:
        return {
            str(row["batch"]): {
                "boundary_rss_bytes": int(row["rss_bytes"]),
                "boundary_tracemalloc_current_bytes": row.get("tracemalloc_current_bytes"),
                "boundary_gc_collected": row.get("gc_collected"),
                "boundary_observed_at": row.get("observed_at"),
                "source": "worker_task_barrier",
            }
            for row in barriers
        }
    worker_rows = [
        row for row in read_ndjson(evidence_dir / "worker_observations.ndjson")
        if row.get("rss_bytes")
    ]
    producer_rows = read_ndjson(evidence_dir / "producer_observations.ndjson")
    boundaries = [
        row for row in producer_rows
        if row.get("event") == "failure_batch_complete"
    ]
    result = {}
    for boundary in boundaries:
        try:
            boundary_time = datetime.fromisoformat(str(boundary["observed_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        before = [
            int(row["rss_bytes"])
            for row in worker_rows
            if datetime.fromisoformat(str(row["observed_at"])) <= boundary_time
        ]
        if before:
            result[str(boundary.get("batch"))] = {
                "boundary_rss_bytes": before[-1],
                "sample_count_before_boundary": len(before),
                "source": "monitor_before_producer_event",
            }
    return result


def producer_summary(evidence_dir: Path) -> dict:
    rows = read_ndjson(evidence_dir / "producer_observations.ndjson")
    complete = next((row for row in reversed(rows) if row.get("event") == "producer_complete"), {})
    return {
        "sample_count": len(rows),
        "submitted_failures": int(complete.get("submitted_failures") or 0),
        "submitted_controls": int(complete.get("submitted_controls") or 0),
        "completed": bool(complete),
    }


def runtime_visibility_summary(stage_result: dict, evidence_dir: Path) -> dict:
    visible_values = [stage_result.get("runtime_manifest") or {}]
    for name in (
        "producer_observations.ndjson",
        "task_observations.ndjson",
        "worker_observations.ndjson",
        "worker.log",
    ):
        path = evidence_dir / name
        if path.is_file():
            visible_values.append(path.read_text(encoding="utf-8", errors="replace"))
    text = json.dumps(visible_values, ensure_ascii=False).lower()
    hits = [name for name, pattern in RUNTIME_FORBIDDEN_PATTERNS.items() if pattern.search(text)]
    return {"clean": not hits, "forbidden_marker_hits": hits}


def latest_tree(stage_result: dict) -> dict:
    detail = ((stage_result.get("diagnosis") or {}).get("detail") or {})
    versions = detail.get("conclusion_versions") or []
    if not versions:
        return {}
    tree = versions[-1].get("controlled_ai_tree")
    return tree if isinstance(tree, dict) else {}


def all_tree_nodes(tree: dict) -> list[dict]:
    result = []
    for layer in tree.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        for group in ("primary_causes", "secondary_causes", "rejected_causes", "unknown_causes"):
            result.extend(node for node in layer.get(group) or [] if isinstance(node, dict))
    return result


def evaluate_tree_contract(tree: dict, stage_result: dict) -> dict:
    nodes = all_tree_nodes(tree)
    node_by_id = {
        str(node.get("candidate_id")): node
        for node in nodes
        if node.get("candidate_id")
    }
    deep_nodes = [
        node for node in nodes
        if node.get("depth_kind") in {"mechanism", "boundary"}
    ]
    explicit_parent_links = all(
        node.get("parent_candidate_ids")
        and node.get("origin_parent_candidate_id") in node.get("parent_candidate_ids", [])
        and all(parent in node_by_id for parent in node.get("parent_candidate_ids", []))
        for node in deep_nodes
    )
    observation_nodes = [node for node in nodes if node.get("node_type") == "observation"]
    observations_under_base = all(
        node.get("parent_candidate_ids")
        and any(
            node_by_id.get(parent, {}).get("depth_kind") == "base"
            for parent in node.get("parent_candidate_ids", [])
        )
        for node in observation_nodes
    )
    detail = ((stage_result.get("diagnosis") or {}).get("detail") or {})
    probes = detail.get("probes") or []
    heap_failures = [
        probe for probe in probes
        if str((probe.get("parameters") or {}).get("evidence_gap") or "") == "python_heap_profile"
        and (
            str(probe.get("status") or "").lower() in {"failed", "blocked", "timed_out", "timeout"}
            or str(probe.get("evidence_status") or "").lower() in {
                "failed", "blocked", "partial", "empty_window", "unparseable", "target_exit",
            }
        )
    ]
    final_primary = set(tree.get("final_primary_causes") or [])
    heap_primary_free = all(
        str((probe.get("parameters") or {}).get("candidate_id") or "") not in final_primary
        for probe in heap_failures
    )
    headline = str(detail.get("headline") or detail.get("summary") or "")
    abstain_truthful = bool(final_primary) or (
        bool(detail.get("abstained"))
        and "未形成正式根因" in headline
    )
    return {
        "explicit_parent_links": explicit_parent_links,
        "observations_under_base": observations_under_base,
        "heap_failure_not_formal_primary": heap_primary_free,
        "abstain_truthful": abstain_truthful,
    }


def evaluate_stage(stage: str, stage_result: dict, evidence_dir: Path, oracle: dict) -> dict:
    manifest = stage_result.get("runtime_manifest") or {}
    revision = ((manifest.get("source_context") or {}).get("repo_revision") or "")
    expected_revision = (
        oracle.get("expected_vulnerable_revision")
        if stage == "vulnerable"
        else oracle.get("expected_fix_revision")
    )
    rss = rss_summary(evidence_dir)
    batch_rss = rss_batch_summary(evidence_dir)
    producer = producer_summary(evidence_dir)
    runtime_visibility = runtime_visibility_summary(stage_result, evidence_dir)
    tree = latest_tree(stage_result)
    nodes = all_tree_nodes(tree)
    source_hits = [
        node for node in nodes
        if "celery/app/trace.py" in json.dumps(node, ensure_ascii=False)
    ]
    mechanism_nodes = [node for node in nodes if node.get("depth_kind") == "mechanism"]
    final_primary = set(tree.get("final_primary_causes") or [])
    mechanism_not_primary = all(node.get("candidate_id") not in final_primary for node in mechanism_nodes)
    tree_contract = evaluate_tree_contract(tree, stage_result) if stage == "vulnerable" else {
        "explicit_parent_links": True,
        "observations_under_base": True,
        "heap_failure_not_formal_primary": True,
        "abstain_truthful": True,
    }
    return {
        "revision_matches_oracle": revision == expected_revision,
        "revision": revision,
        "expected_revision": expected_revision,
        "runtime_target_present": bool(manifest.get("worker_pid") and manifest.get("container_id")),
        "full_project_source_present": bool((manifest.get("source_context") or {}).get("source_paths")),
        "rss": rss,
        "batch_rss": batch_rss,
        "producer": producer,
        "runtime_visibility": runtime_visibility,
        "diagnosis_present": bool(stage_result.get("diagnosis")),
        "stage_role": stage_result.get("stage_role"),
        "diagnosis_mode": stage_result.get("diagnosis_mode"),
        "source_range_detected": bool(source_hits),
        "source_hit_count": len(source_hits),
        "mechanism_node_count": len(mechanism_nodes),
        "mechanism_not_formal_primary": mechanism_not_primary,
        "tree_contract": tree_contract,
        "tree": tree,
    }


def evaluate_fallback(vulnerable: dict) -> dict:
    tree = vulnerable.get("tree") or {}
    failed_probe_origins = {}
    diagnosis = vulnerable.get("_stage_result") or {}
    detail = ((diagnosis.get("diagnosis") or {}).get("detail") or {})
    for probe in detail.get("probes") or []:
        parameters = probe.get("parameters") or {}
        status = str(probe.get("status") or "").lower()
        evidence_status = str(probe.get("evidence_status") or "").lower()
        if parameters.get("candidate_id") and parameters.get("origin_parent_candidate_id") and (
            status in {"failed", "blocked", "timed_out", "timeout"}
            or evidence_status in {"partial", "blocked", "failed", "empty_window", "unparseable", "target_exit"}
        ):
            failed_probe_origins[str(parameters["candidate_id"])] = str(parameters["origin_parent_candidate_id"])
    rollback_pairs = {
        (str(edge.get("from_candidate_ids", [""])[0]), str(edge.get("to_candidate_ids", [""])[0]))
        for edge in tree.get("probe_edges") or []
        if edge.get("effect") == "rollback"
        and edge.get("from_candidate_ids")
        and edge.get("to_candidate_ids")
    }
    matched = any(
        (candidate_id, origin) in rollback_pairs
        or any(candidate_id in pair[0] and origin == pair[1] for pair in rollback_pairs)
        for candidate_id, origin in failed_probe_origins.items()
    )
    return {
        "failed_deep_probe_count": len(failed_probe_origins),
        "failed_deep_probe_rolls_back_to_recorded_origin": matched if failed_probe_origins else None,
    }


def evaluate_run(run: dict, base: Path, oracle: dict) -> dict:
    vulnerable_result = run.get("vulnerable") or {}
    fixed_result = run.get("fixed") or {}
    vulnerable = evaluate_stage("vulnerable", vulnerable_result, base / "evidence" / "vulnerable", oracle)
    fixed = evaluate_stage("fixed", fixed_result, base / "evidence" / "fixed", oracle)
    vulnerable["_stage_result"] = vulnerable_result
    fixed_delta = fixed["rss"].get("delta_bytes")
    vulnerable_delta = vulnerable["rss"].get("delta_bytes")
    comparison = {
        "same_workload": (
            (vulnerable_result.get("runtime_manifest") or {}).get("workload")
            == (fixed_result.get("runtime_manifest") or {}).get("workload")
        ),
        "producer_counts_close": (
            vulnerable["producer"]["submitted_failures"] > 0
            and fixed["producer"]["submitted_failures"] > 0
            and abs(vulnerable["producer"]["submitted_failures"] - fixed["producer"]["submitted_failures"])
            <= max(100, vulnerable["producer"]["submitted_failures"] * 0.25)
        ),
        "vulnerable_rss_anomaly": bool(
            vulnerable_delta is not None and vulnerable_delta >= MIN_RSS_DELTA
        ),
        "fixed_does_not_reproduce_same_rss_anomaly": bool(
            fixed_delta is not None
            and vulnerable_delta is not None
            and fixed_delta < MIN_RSS_DELTA
            and fixed_delta < vulnerable_delta * 0.5
        ),
        "fixed_delta_bytes": fixed_delta,
        "vulnerable_delta_bytes": vulnerable_delta,
    }
    vulnerable_batches = vulnerable.get("batch_rss") or {}
    fixed_batches = fixed.get("batch_rss") or {}
    if "1" in vulnerable_batches and "2" in vulnerable_batches and "1" in fixed_batches and "2" in fixed_batches:
        vulnerable_second_delta = vulnerable_batches["2"]["boundary_rss_bytes"] - vulnerable_batches["1"]["boundary_rss_bytes"]
        fixed_second_delta = fixed_batches["2"]["boundary_rss_bytes"] - fixed_batches["1"]["boundary_rss_bytes"]
        vulnerable_second_anomaly = (
            vulnerable_second_delta >= MIN_BATCH_RSS_DELTA
            and vulnerable_second_delta >= fixed_second_delta * MIN_BATCH_RELATIVE_MULTIPLIER
        )
        comparison.update({
            "vulnerable_second_batch_delta_bytes": vulnerable_second_delta,
            "fixed_second_batch_delta_bytes": fixed_second_delta,
            "vulnerable_second_batch_anomaly": vulnerable_second_anomaly,
            "fixed_second_batch_does_not_match_vulnerable": fixed_second_delta < max(MIN_RSS_DELTA, vulnerable_second_delta * 0.5),
        })
    else:
        comparison["vulnerable_second_batch_anomaly"] = False
        comparison["fixed_second_batch_does_not_match_vulnerable"] = False
    if comparison.get("vulnerable_second_batch_anomaly") is not None:
        comparison["vulnerable_rss_anomaly"] = comparison["vulnerable_second_batch_anomaly"]
    fallback = evaluate_fallback(vulnerable)
    checks = {
        "vulnerable_revision": vulnerable["revision_matches_oracle"],
        "fixed_revision": fixed["revision_matches_oracle"],
        "real_runtime_targets": vulnerable["runtime_target_present"] and fixed["runtime_target_present"],
        "full_project_source": vulnerable["full_project_source_present"] and fixed["full_project_source_present"],
        "vulnerable_diagnosis_present": (
            vulnerable["diagnosis_present"]
            and vulnerable["stage_role"] == "diagnosis_target"
            and vulnerable["diagnosis_mode"] == "full"
        ),
        "fixed_diagnosis_absent": (
            not fixed["diagnosis_present"]
            and fixed["stage_role"] == "regression_control"
            and fixed["diagnosis_mode"] == "none"
        ),
        "runtime_visibility_clean": (
            vulnerable["runtime_visibility"]["clean"]
            and fixed["runtime_visibility"]["clean"]
        ),
        "worker_barriers_complete": (
            vulnerable["producer"]["completed"]
            and fixed["producer"]["completed"]
            and set(vulnerable["batch_rss"]) >= {"1", "2"}
            and set(fixed["batch_rss"]) >= {"1", "2"}
            and all(
                row.get("source") == "worker_task_barrier"
                for row in [
                    vulnerable["batch_rss"]["1"],
                    vulnerable["batch_rss"]["2"],
                    fixed["batch_rss"]["1"],
                    fixed["batch_rss"]["2"],
                ]
            )
        ),
        "vulnerable_anomaly": comparison["vulnerable_second_batch_anomaly"],
        "fixed_regression_control": comparison["fixed_second_batch_does_not_match_vulnerable"],
        "same_workload": comparison["same_workload"] and comparison["producer_counts_close"],
        "real_source_range": vulnerable["source_range_detected"],
        "mechanism_is_not_primary": vulnerable["mechanism_not_formal_primary"],
        "tree_parent_links_explicit": vulnerable["tree_contract"]["explicit_parent_links"],
        "observations_under_base": vulnerable["tree_contract"]["observations_under_base"],
        "heap_failure_not_formal_primary": vulnerable["tree_contract"]["heap_failure_not_formal_primary"],
        "abstain_truthful": vulnerable["tree_contract"]["abstain_truthful"],
        "deep_probe_fallback": fallback["failed_deep_probe_rolls_back_to_recorded_origin"] is not False,
    }
    result = {
        "case_id": run.get("case_id"),
        "oracle": "offline_only",
        "checks": checks,
        "passed": all(checks.values()),
        "vulnerable": {key: value for key, value in vulnerable.items() if key != "_stage_result"},
        "fixed": {key: value for key, value in fixed.items() if key != "_stage_result"},
        "comparison": comparison,
        "fallback": fallback,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", required=True, type=Path)
    parser.add_argument("--oracle", default=str(ROOT / "oracle.json"), type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    args = parser.parse_args()

    result = evaluate_run(load_json(args.run_json), args.run_json.parent, load_json(args.oracle))
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
