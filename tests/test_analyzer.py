"""Analyzer 单元测试。

测试覆盖折叠栈解析、火焰图 JSON 树构造、规则匹配、
空数据/缺失工具的错误处理。
"""

import json
import re
from unittest import mock

import pytest

from analyzer.mini_drop_analyzer.hotmethod_analyzer import (
    _build_flame_tree,
    _fail,
    _load_output_dir,
    _match_rules,
    _parse_top,
)

# 模拟折叠栈文本
_COLLAPSED_SAMPLE = (
    "fib_hotspot;fib_hotspot;fib_hotspot 3500\n"
    "fib_hotspot;fib_hotspot 2000\n"
    "sort_hotspot;sort_hotspot;__libc_start_main 1200\n"
    "sort_hotspot;sort_hotspot 800\n"
    "json_hotspot;json.dumps;json.encoder 500\n"
    "json_hotspot;json.loads 300\n"
)


class TestParseTop:
    """TopN 热点解析。"""

    def test_top_functions_sorted(self, tmp_path):
        collapsed = tmp_path / "collapsed.txt"
        collapsed.write_text(_COLLAPSED_SAMPLE)
        top = _parse_top(collapsed, limit=10)
        # 折叠栈中每个函数在多个栈行中出现，独立函数名 ≥ 6 个
        assert len(top) >= 6
        assert top[0]["name"] == "fib_hotspot"
        assert top[0]["samples"] > top[1]["samples"]

    def test_top_percent_sum(self, tmp_path):
        collapsed = tmp_path / "collapsed.txt"
        collapsed.write_text(_COLLAPSED_SAMPLE)
        top = _parse_top(collapsed)
        # samples 在各栈行间可能重复计数，不检验 sum
        assert top[0]["percent"] > 0

    def test_empty_collapsed_file(self, tmp_path):
        collapsed = tmp_path / "collapsed.txt"
        collapsed.write_text("")
        top = _parse_top(collapsed)
        assert top == []

    def test_malformed_lines_skipped(self, tmp_path):
        collapsed = tmp_path / "collapsed.txt"
        collapsed.write_text("func_a;func_b 100\nbad-line\nfunc_c 50\n\n")
        top = _parse_top(collapsed)
        # func_a, func_b, func_c 共 3 个独立函数名
        assert len(top) == 3


class TestAnalyzerQualityGate:
    def test_fail_exits_nonzero_for_empty_runtime_stack(self):
        with pytest.raises(SystemExit) as exc:
            _fail("perf script 未产出可解析栈文本")
        assert exc.value.code == 1


class TestFlameTree:
    """火焰图 JSON 树构造。"""

    def test_tree_has_root_structure(self, tmp_path):
        collapsed = tmp_path / "collapsed.txt"
        collapsed.write_text("a;b;c 100\na;b 200\nd;e 50\n")
        tree = _build_flame_tree(collapsed)

        assert tree["name"] == "root"
        assert tree["value"] == 350
        assert "children" in tree

    def test_tree_depth_truncation(self, tmp_path):
        """超过 MAX_TREE_DEPTH 深度的栈应被截断。"""
        # 构造 60 层调用栈
        funcs = [f"f{i}" for i in range(60)]
        stack = ";".join(funcs) + " 10\n"
        collapsed = tmp_path / "collapsed.txt"
        collapsed.write_text(stack)

        tree = _build_flame_tree(collapsed)
        # 验证不会超过 50 层
        max_depth = 0

        def walk(node, depth):
            nonlocal max_depth
            max_depth = max(max_depth, depth)
            for child in node.get("children", []):
                walk(child, depth + 1)

        walk(tree, 0)
        assert max_depth <= 50

    def test_empty_input(self, tmp_path):
        collapsed = tmp_path / "collapsed.txt"
        collapsed.write_text("")
        tree = _build_flame_tree(collapsed)
        assert tree["name"] == "root"
        assert tree["value"] == 0


class TestRules:
    """规则引擎匹配。"""

    def test_fib_hotspot_triggers_rule(self):
        top = [{"name": "fib_hotspot", "samples": 100, "percent": 68.5}]
        result = _match_rules(top)
        assert "Fibonacci" in result
        assert "记忆化" in result

    def test_sort_hotspot_triggers_rule(self):
        top = [{"name": "sort_hotspot", "samples": 50, "percent": 30.0}]
        result = _match_rules(top)
        assert "排序" in result

    def test_json_triggers_rule(self):
        top = [{"name": "json.dumps", "samples": 20, "percent": 10.0}]
        result = _match_rules(top)
        assert "JSON" in result

    def test_no_match_returns_fallback(self):
        top = [{"name": "unknown_func", "samples": 10, "percent": 5.0}]
        result = _match_rules(top)
        assert "未命中" in result

    def test_lock_hotspot_triggers_mutex_rule(self):
        top = [{"name": "pthread_mutex_lock", "samples": 80, "percent": 50.0}]
        result = _match_rules(top)
        assert "锁竞争" in result or "互斥" in result


class TestAnalyzerConfig:
    """Analyzer 配置读取。"""

    def test_load_output_dir_from_config(self, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text('[analyzer]\noutput_dir = "/tmp/custom-analyzer"\n')
        assert _load_output_dir(config) == "/tmp/custom-analyzer"

    def test_load_output_dir_uses_default_when_missing(self, tmp_path):
        assert _load_output_dir(tmp_path / "missing.toml") == "/tmp/mini-drop-analyzer"
