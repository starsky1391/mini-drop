"""Mini-Drop Analyzer 命令行入口。

从 perf.data 生成 d3-flame-graph 火焰图 JSON 树、TopN 热点函数、
规则建议和 fallback SVG。

用法:
  python -m analyzer.mini_drop_analyzer.hotmethod_analyzer \
    --task-id task_xxx --perf-data /tmp/mini-drop/task_xxx/perf.data \
    --config analyzer/config.example.toml

退出码: 0=成功, 1=失败
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Mini-Drop Analyzer")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--perf-data", required=True, help="perf.data 文件路径")
    parser.add_argument("--config", default="analyzer/config.example.toml")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    task_id = args.task_id
    perf_data = Path(args.perf_data)
    output_root = Path(args.output_dir or _load_output_dir(Path(args.config)))
    output_dir = output_root / task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    if not perf_data.is_file() or perf_data.stat().st_size == 0:
        _fail("perf.data 不存在或为空")

    # 1. perf script → 原始栈文本
    script_path = output_dir / "perf.script.txt"
    ok, err = _perf_script(perf_data, script_path)
    if not ok:
        _fail(f"perf script 失败: {err}")
    if script_path.stat().st_size == 0:
        _fail("perf script 未产出可解析栈文本")

    # 2. stackcollapse → 折叠栈
    collapsed_path = output_dir / "collapsed.txt"
    ok, err = _stackcollapse(script_path, collapsed_path)
    if not ok:
        _fail(f"stackcollapse 失败: {err}")
    if collapsed_path.stat().st_size == 0:
        _fail("stackcollapse 未产出可解析折叠栈")

    # 3. flamegraph.pl → fallback SVG
    svg_path = output_dir / "flamegraph.svg"
    _flamegraph_svg(collapsed_path, svg_path)

    # 4. 解析折叠栈 → TopN JSON + flamegraph JSON 树
    top_n = _parse_top(collapsed_path)
    if not top_n:
        _fail("折叠栈未解析出热点函数")
    top_path = output_dir / "top.json"
    top_path.write_text(json.dumps(top_n, indent=2, ensure_ascii=False))

    flame_tree = _build_flame_tree(collapsed_path)
    tree_path = output_dir / "flamegraph.json"
    tree_text = json.dumps(flame_tree, separators=(",", ":"), ensure_ascii=False)
    tree_path.write_text(tree_text)

    # 5. 规则引擎 → suggestions
    suggestions = _match_rules(top_n)
    sugg_path = output_dir / "suggestions.md"
    sugg_path.write_text(suggestions)

    summary = {
        "task_id": task_id,
        "status": "SUCCESS",
        "summary": suggestions.split("\n")[0] if suggestions else "分析完成",
        "top_functions": top_n[:5],
        "output_files": {
            "flamegraph_json": str(tree_path),
            "flamegraph_svg": str(svg_path),
            "top_json": str(top_path),
            "suggestions_md": str(sugg_path),
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def _fail(msg: str) -> None:
    print(json.dumps({"status": "FAILED", "error": msg}))
    raise SystemExit(1)


def _load_output_dir(config_path: Path) -> str:
    """读取 analyzer 配置中的 output_dir，缺失时返回默认值。

    当前只需要一个配置项，使用轻量解析避免为 Python 3.10 额外引入 tomli。
    """
    default = "/tmp/mini-drop-analyzer"
    if not config_path.is_file():
        return default

    in_analyzer = False
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_analyzer = line == "[analyzer]"
            continue
        if in_analyzer and line.startswith("output_dir"):
            _, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            return value or default
    return default


# ---------------------------------------------------------------------------
# 步骤 1: perf script
# ---------------------------------------------------------------------------


def _perf_script(perf_data: Path, output: Path) -> tuple[bool, str]:
    perf = shutil.which("perf")
    if perf is None:
        return False, "perf 命令不可用"
    try:
        subprocess.run(
            [perf, "script", "-i", str(perf_data)],
            stdout=output.open("w"),
            stderr=subprocess.PIPE,
            check=True,
            timeout=60,
        )
        return True, ""
    except subprocess.CalledProcessError as exc:
        return False, exc.stderr.decode("utf-8", errors="replace")[:200]
    except subprocess.TimeoutExpired:
        return False, "perf script 超时"


# ---------------------------------------------------------------------------
# 步骤 2: stackcollapse-perf.pl
# ---------------------------------------------------------------------------


def _stackcollapse(input_path: Path, output_path: Path) -> tuple[bool, str]:
    script = Path(__file__).resolve().parent.parent / "scripts" / "stackcollapse-perf.pl"
    if not script.is_file():
        return False, f"stackcollapse-perf.pl 未找到: {script}"
    try:
        subprocess.run(
            ["perl", str(script), str(input_path)],
            stdout=output_path.open("w"),
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        )
        return True, ""
    except subprocess.CalledProcessError as exc:
        return False, exc.stderr.decode("utf-8", errors="replace")[:200]
    except subprocess.TimeoutExpired:
        return False, "stackcollapse 超时"


# ---------------------------------------------------------------------------
# 步骤 3: flamegraph.pl → SVG
# ---------------------------------------------------------------------------


def _flamegraph_svg(collapsed: Path, output: Path) -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "flamegraph.pl"
    if not script.is_file():
        output.write_text(_fallback_svg("flamegraph.pl 未找到"))
        return
    try:
        subprocess.run(
            ["perl", str(script), str(collapsed)],
            stdout=output.open("w"),
            stderr=subprocess.PIPE,
            check=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        output.write_text(_fallback_svg("火焰图生成失败"))


def _fallback_svg(msg: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="120">'
        f'<text x="20" y="60" font-size="16" fill="#999">{msg}</text>'
        "</svg>\n"
    )


# ---------------------------------------------------------------------------
# 步骤 4: 解析折叠栈
# ---------------------------------------------------------------------------

MAX_TREE_DEPTH = 50


def _parse_top(collapsed: Path, limit: int = 20) -> list[dict]:
    """从折叠栈文本解析 TopN 热点函数。

    折叠栈格式（一行一个栈）：
      func1;func2;func3 1234
    """
    counter: dict[str, int] = {}
    total = 0
    with collapsed.open("r") as fh:
        for line in fh:
            if " " not in line:
                continue
            stack, _, count_str = line.rstrip().rpartition(" ")
            try:
                count = int(count_str)
            except ValueError:
                continue
            total += count
            for func in stack.split(";"):
                func = func.strip()
                if func:
                    counter[func] = counter.get(func, 0) + count

    entries = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [
        {"name": name, "samples": cnt, "percent": round(cnt / total * 100, 1) if total else 0}
        for name, cnt in entries
    ]


def _build_flame_tree(collapsed: Path) -> dict:
    """从折叠栈构建 d3-flame-graph JSON 树。

    返回格式：{"name":"root","value":total,"children":[...]}
    深度超过 MAX_TREE_DEPTH 时截断。
    """
    root: dict = {"name": "root", "value": 0, "children": []}
    node_map: dict[str, dict] = {"": root}

    with collapsed.open("r") as fh:
        for line in fh:
            if " " not in line:
                continue
            stack, _, count_str = line.rstrip().rpartition(" ")
            try:
                count = int(count_str)
            except ValueError:
                continue
            root["value"] += count

            funcs = [f.strip() for f in stack.split(";") if f.strip()]
            if not funcs:
                continue

            depth = min(len(funcs), MAX_TREE_DEPTH)
            for i in range(depth):
                func = funcs[i]
                prefix = ";".join(funcs[: i + 1])
                parent_key = ";".join(funcs[:i]) if i > 0 else ""
                parent = node_map.get(parent_key)
                if parent is None:
                    break

                if prefix not in node_map:
                    node: dict = {"name": func, "value": 0}
                    node.setdefault("children", [])
                    parent.setdefault("children", []).append(node)
                    node_map[prefix] = node
                node_map[prefix]["value"] += count

    return root


# ---------------------------------------------------------------------------
# 步骤 5: 规则引擎
# ---------------------------------------------------------------------------

_DEFAULT_RULES: list[dict[str, str]] = [
    {"regex": r"(?i)fib",         "advice": "检测到 Fibonacci 递归热点，建议改用迭代 + 记忆化或查表法替代"},
    {"regex": r"(?i)sort",        "advice": "排序开销较高，检查数据集大小，考虑原地排序或基数排序替代"},
    {"regex": r"(?i)json",        "advice": "JSON 编解码占用 CPU 显著，检查是否存在不必要的重复序列化"},
    {"regex": r"(?i)malloc",      "advice": "malloc 调用频繁，考虑使用内存池或 jemalloc 分配器"},
    {"regex": r"(?i)lock|mutex",  "advice": "锁竞争热点，检查临界区长度，考虑无锁结构或读写锁替代"},
    {"regex": r"(?i)strcmp|strncpy|strlen",
                                  "advice": "字符串操作密集，考虑使用 string_view 或预计算长度"},
    {"regex": r"(?i)io_submit|blk_mq|vfs_read|vfs_write",
                                  "advice": "内核 IO 路径出现热点，建议结合 eBPF 采集器确认 IO 延迟"},
]


def _match_rules(top_n: list[dict]) -> str:
    lines: list[str] = []
    for entry in top_n[:10]:
        for rule in _DEFAULT_RULES:
            if re.search(rule["regex"], entry["name"]):
                lines.append(f"- **{entry['name']}** ({entry['percent']}%): {rule['advice']}")
    if not lines:
        lines.append("- 未命中预置规则，建议结合 AI 归因深入分析")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
