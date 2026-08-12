#!/usr/bin/env python3
"""Compare two paired diagnosis evaluation reports case by case."""

from __future__ import annotations

import argparse
import json
from math import comb
from pathlib import Path


def _load_results(path: Path) -> dict[tuple[str, int], dict]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return {
        (item["case_id"], int(item.get("repetition", 1))): item
        for item in value["aggregate"]["results"]
    }


def _mcnemar_exact(left_only: int, right_only: int) -> float:
    discordant = left_only + right_only
    if discordant == 0:
        return 1.0
    tail = sum(comb(discordant, index) for index in range(0, min(left_only, right_only) + 1))
    return min(1.0, 2.0 * tail / (2 ** discordant))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--left-name", default="left")
    parser.add_argument("--right-name", default="right")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    left = _load_results(args.left)
    right = _load_results(args.right)
    shared = sorted(set(left) & set(right))
    rows = []
    left_only = right_only = 0
    for case_key in shared:
        left_item, right_item = left[case_key], right[case_key]
        left_exact = bool(left_item["exact_root_match"])
        right_exact = bool(right_item["exact_root_match"])
        left_only += left_exact and not right_exact
        right_only += right_exact and not left_exact
        rows.append({
            "case_id": case_key[0],
            "repetition": case_key[1],
            "left_exact": left_exact,
            "right_exact": right_exact,
            "left_score": left_item["score"],
            "right_score": right_item["score"],
            "score_delta_right_minus_left": round(right_item["score"] - left_item["score"], 2),
        })
    result = {
        "schema_version": "1.0",
        "left_name": args.left_name,
        "right_name": args.right_name,
        "paired_case_count": len(shared),
        "left_exact": sum(item["left_exact"] for item in rows),
        "right_exact": sum(item["right_exact"] for item in rows),
        "left_only_correct": left_only,
        "right_only_correct": right_only,
        "mcnemar_exact_p_value": round(_mcnemar_exact(left_only, right_only), 6),
        "interpretation": (
            "A lower p-value indicates stronger evidence that paired root-cause accuracy differs; "
            "do not rank systems from score alone when the paired sample is small."
        ),
        "cases": rows,
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
