"""CLI for the evidence-to-attribution experiment pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .pipeline import run_evidence_to_attribution


def _load_payload(path: str | None) -> dict[str, Any]:
    if not path:
        return json.load(__import__("sys").stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the evidence-to-attribution experiment pipeline.")
    parser.add_argument("--input", help="Path to a JSON file containing structured RCA evidence.")
    parser.add_argument("--output", help="Optional output file path. Defaults to stdout.")
    args = parser.parse_args(argv)

    payload = _load_payload(args.input)
    result = run_evidence_to_attribution(payload)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
