#!/usr/bin/env python3
"""Score diagnosis audit bundles against evaluator-only private oracles."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from pathlib import Path
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.diagnosis.benchmark_score import aggregate_results, score_audit_bundle  # noqa: E402


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _with_defaults(defaults: dict, value: dict) -> dict:
    merged = dict(defaults)
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **item}
        else:
            merged[key] = item
    return merged


def _fetch_bundle(server: str, api_key: str, diagnosis_id: str) -> dict:
    url = f"{server.rstrip('/')}/api/v1/diagnoses/{diagnosis_id}/audit-bundle"
    request = Request(url, headers={"X-API-Key": api_key})
    context = ssl._create_unverified_context() if url.startswith("https://") else None
    with urlopen(request, timeout=30, context=context) as response:  # noqa: S310 - explicit lab URL
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("data", payload)


def render_markdown(report: dict) -> str:
    aggregate = report["aggregate"]
    lines = [
        "# AI 运维诊断基线评测",
        "",
        f"- 数据集：`{report['dataset']}` v{report['dataset_version']}",
        f"- 已评测：{report['evaluated_case_count']} / {report['dataset_case_count']} 个案例，"
        f"共 {report['evaluated_run_count']} 次运行",
        f"- 严格根因准确率：{aggregate['exact_root_matches']}/{aggregate['case_count']} "
        f"({aggregate['exact_root_accuracy'] * 100:.1f}%)",
        f"- 运行级严格命中：{aggregate['run_exact_root_matches']}/{aggregate['run_count']} "
        f"({aggregate['run_exact_root_accuracy'] * 100:.1f}%)",
        f"- 95% Wilson 区间：{aggregate['exact_root_accuracy_wilson_95'][0] * 100:.1f}%～"
        f"{aggregate['exact_root_accuracy_wilson_95'][1] * 100:.1f}%",
        f"- 平均综合得分：{aggregate['mean_score']:.2f}/100",
        f"- 位置命中：{aggregate['root_dimension_accuracy']['location_type']['matched']}/"
        f"{aggregate['root_dimension_accuracy']['location_type']['specified']}",
        f"- 故障域命中：{aggregate['root_dimension_accuracy']['domain_type']['matched']}/"
        f"{aggregate['root_dimension_accuracy']['domain_type']['specified']}",
        f"- 分类命中：{aggregate['root_dimension_accuracy']['classification']['matched']}/"
        f"{aggregate['root_dimension_accuracy']['classification']['specified']}",
        f"- 根因实体命中：{aggregate['root_dimension_accuracy']['root_entity']['matched']}/"
        f"{aggregate['root_dimension_accuracy']['root_entity']['specified']}",
        f"- 有效证据引用率：{aggregate['citation_valid_rate'] * 100:.1f}%",
        f"- 运行时审计轨迹覆盖率：{aggregate['runtime_trace_coverage'] * 100:.1f}%",
        f"- 不安全动作：{aggregate['unsafe_action_count']}",
        f"- 重复案例输出一致率："
        f"{aggregate['repeat_output_consistency'] * 100:.1f}%"
        if aggregate['repeat_output_consistency'] is not None else "- 重复案例输出一致率：尚无重复运行",
        "",
        "综合得分用于定位差距，不替代严格根因准确率。旧会话重建出的轨迹不计为运行时审计轨迹。",
        "",
        "## 逐案例",
        "",
        "| Case | 次数 | 诊断 ID | 根因 | 总分 | 根因 | 证据 | 轨迹 | 安全 |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for item in aggregate["results"]:
        dimensions = item["dimensions"]
        lines.append(
            f"| {item['case_id']} | {item.get('repetition', 1)} | `{item['diagnosis_id']}` | "
            f"{'命中' if item['exact_root_match'] else '未命中'} | {item['score']:.2f} | "
            f"{dimensions['root_cause']['score']:.1f}/40 | "
            f"{dimensions['evidence']['score']:.1f}/25 | "
            f"{dimensions['trace']['score']:.1f}/20 | "
            f"{dimensions['safety']['score']:.1f}/10 |"
        )
    if report["missing_cases"]:
        lines.extend([
            "", "## 尚未运行", "",
            ", ".join(f"`{case_id}`" for case_id in report["missing_cases"]),
        ])
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--diagnosis-map", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--server")
    parser.add_argument("--api-key-env", default="MINI_DROP_API_KEY")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = _load(args.dataset / "manifest.json")
    private = _load(args.dataset / "private" / "oracles.json")
    oracles = {
        item["case_id"]: _with_defaults(private.get("defaults") or {}, item)
        for item in private["cases"]
    }
    diagnosis_map = _load(args.diagnosis_map)
    mapping = diagnosis_map.get("cases", diagnosis_map)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = args.bundle_dir or args.output_dir / "bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    api_key = os.getenv(args.api_key_env, "")
    results = []
    missing = []
    evaluated_case_ids = set()
    for case in manifest["cases"]:
        case_id = case["case_id"]
        diagnosis_value = mapping.get(case_id)
        if not diagnosis_value:
            missing.append(case_id)
            continue
        diagnosis_ids = diagnosis_value if isinstance(diagnosis_value, list) else [diagnosis_value]
        case_results = 0
        for repetition, diagnosis_id in enumerate(diagnosis_ids, 1):
            result_repetition = repetition
            filename = f"{case_id}__r{repetition:02d}.json" if len(diagnosis_ids) > 1 else f"{case_id}.json"
            bundle_path = bundle_dir / filename
            repeated_name = bundle_dir / f"{case_id}__r{repetition:02d}.json"
            if not bundle_path.is_file() and repeated_name.is_file():
                bundle_path = repeated_name
            if not bundle_path.is_file():
                # Partial randomized runs may only contain r02/r03. Resolve
                # by diagnosis id so the original repetition label survives
                # and paired comparisons do not silently shift the run.
                for candidate in sorted(bundle_dir.glob(f"{case_id}__r*.json")):
                    candidate_bundle = _load(candidate)
                    if candidate_bundle.get("diagnosis_id") != diagnosis_id:
                        continue
                    bundle_path = candidate
                    try:
                        result_repetition = int(candidate.stem.rsplit("__r", 1)[1])
                    except (IndexError, ValueError):
                        result_repetition = repetition
                    break
            if bundle_path.is_file():
                bundle = _load(bundle_path)
            elif args.server:
                if not api_key:
                    raise SystemExit(f"{args.api_key_env} is required when --server is used")
                bundle = _fetch_bundle(args.server, api_key, diagnosis_id)
                bundle_path.write_text(
                    json.dumps(bundle, ensure_ascii=False, indent=2, default=str) + "\n",
                    encoding="utf-8",
                )
            else:
                continue
            result = score_audit_bundle(bundle, oracles[case_id])
            result["repetition"] = result_repetition
            results.append(result)
            case_results += 1
        if case_results:
            evaluated_case_ids.add(case_id)
        else:
            missing.append(case_id)
    aggregate = aggregate_results(results)
    report = {
        "schema_version": "1.0",
        "dataset": manifest["dataset"],
        "dataset_version": manifest["version"],
        "dataset_case_count": len(manifest["cases"]),
        "evaluated_case_count": len(evaluated_case_ids),
        "evaluated_run_count": len(results),
        "missing_cases": missing,
        "aggregate": aggregate,
    }
    (args.output_dir / "evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (args.output_dir / "evaluation.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if results else 2


if __name__ == "__main__":
    raise SystemExit(main())
