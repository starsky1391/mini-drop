#!/usr/bin/env python3
"""Check Mini-Drop AI Ops v2 audit bundle readiness before formal scoring."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.diagnosis.audit_bundle import build_readiness_gate  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results = []
    for path in sorted(args.bundle_dir.glob("*.json")):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        gate = build_readiness_gate(bundle)
        results.append({
            "file": path.name,
            "diagnosis_id": bundle.get("diagnosis_id"),
            "status": gate["status"],
            "summary": gate["summary"],
            "checks": gate["checks"],
        })
    report = {
        "schema_version": "1.0",
        "bundle_dir": str(args.bundle_dir),
        "bundle_count": len(results),
        "passed_count": sum(item["status"] == "PASS" for item in results),
        "failed_count": sum(item["status"] != "PASS" for item in results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["failed_count"] == 0 and report["bundle_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
