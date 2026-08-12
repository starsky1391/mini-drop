#!/usr/bin/env python3
"""Score an executed Online Boutique VM validation report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _all_faults_recovered(report: dict[str, Any]) -> bool:
    cases = report.get("fault_cases", [])
    return len(cases) >= 8 and all(
        item.get("repetitions", 0) >= 2
        and item.get("fault_observed") is True
        and item.get("rollback_verified") is True
        for item in cases
    )


def score(report: dict[str, Any]) -> dict[str, Any]:
    deployment = report.get("deployment", {})
    deployment_passed = (
        deployment.get("worker_nodes") == 2
        and deployment.get("running_services") == 12
        and deployment.get("offline_bundle_sha256_verified") is True
    )

    fault_passed = _all_faults_recovered(report)
    destructive = report.get("destructive_tests", {})
    destructive_passed = all(
        destructive.get(name, {}).get("recovered") is True
        for name in ("network_partition", "disk_enospc", "control_restart")
    )

    diagnoses = report.get("diagnoses", [])
    diagnosis_passed = (
        any(
            item.get("multi_host") is True
            and item.get("score_pct") == 100
            and item.get("status") == "COMPLETED"
            for item in diagnoses
        )
        and report.get("artifact_downloads", {}).get("all_hashes_match") is True
    )

    soak = report.get("stability", {})
    stability_passed = (
        soak.get("duration_sec", 0) >= 1800
        and soak.get("availability_pct", 0) >= 99.9
        and soak.get("agent_offline_samples", 1) == 0
        and soak.get("replica_failure_samples", 1) == 0
    )

    dimensions = {
        "deployment": {"score": 20 if deployment_passed else 0, "maximum": 20},
        "fault_reproducibility": {"score": 30 if fault_passed else 0, "maximum": 30},
        "destructive_recovery": {"score": 20 if destructive_passed else 0, "maximum": 20},
        "diagnosis_and_artifacts": {"score": 20 if diagnosis_passed else 0, "maximum": 20},
        "stability": {"score": 10 if stability_passed else 0, "maximum": 10},
    }
    total = sum(item["score"] for item in dimensions.values())
    mandatory = all(
        (deployment_passed, fault_passed, destructive_passed, diagnosis_passed, stability_passed)
    )
    return {
        "score": total,
        "maximum": 100,
        "tier": "verified_vm" if total >= 90 and mandatory else "vm_candidate",
        "mandatory_gates_passed": mandatory,
        "dimensions": dimensions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = score(report)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["tier"] == "verified_vm" else 1


if __name__ == "__main__":
    raise SystemExit(main())
