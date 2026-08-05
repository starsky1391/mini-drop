"""Selectable RCA analysis strategies."""

from __future__ import annotations

from server.app.rca.strategies.base import AnalysisContext, RCAAnalysisStrategy
from server.app.rca.strategies.graph import GraphCausalRCAAnalyzer
from server.app.rca.strategies.interactive import InteractiveProbeRCAAnalyzer
from server.app.rca.strategies.linear import LinearRCAAnalyzer


_ALIASES = {
    "default": "linear",
    "current": "linear",
    "rule_llm": "linear",
    "linear": "linear",
    "b": "graph",
    "graph": "graph",
    "causal": "graph",
    "graph_causal": "graph",
    "c": "interactive",
    "interactive": "interactive",
    "probe_loop": "interactive",
}


def normalize_strategy_id(value: str | None) -> str:
    key = (value or "linear").strip().lower().replace("-", "_")
    try:
        return _ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"未知 RCA 分析策略: {value}") from exc


def get_strategy(value: str | None) -> RCAAnalysisStrategy:
    strategy_id = normalize_strategy_id(value)
    if strategy_id == "graph":
        return GraphCausalRCAAnalyzer()
    if strategy_id == "interactive":
        return InteractiveProbeRCAAnalyzer()
    return LinearRCAAnalyzer()


__all__ = ["AnalysisContext", "RCAAnalysisStrategy", "get_strategy", "normalize_strategy_id"]
