"""候选归因规则引擎。

规则定义外部化到 rules.json。配置文件只声明 match_type 和参数，
具体执行由本模块中的白名单 matcher 完成，避免把规则配置变成任意代码入口。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from server.app.rca.models import CandidateCause, EvidenceInput, FeedbackPrior

logger = logging.getLogger(__name__)

RULES_PATH = Path(__file__).with_name("rules.json")


def generate_candidates(
    evidence: EvidenceInput,
    feedback_priors: dict[str, FeedbackPrior] | None = None,
) -> list[CandidateCause]:
    """规则引擎生成候选归因列表。"""
    candidates: list[CandidateCause] = []
    priors = feedback_priors or {}

    for rule in load_rules():
        try:
            matched = _match_rule(rule, evidence)
        except Exception as exc:
            matched = False
            logger.warning("rule match failed for %s: %s", rule.get("candidate_id", "?"), exc)

        if not matched:
            continue

        candidate_id = rule["candidate_id"]
        score = float(rule.get("rule_score", 0.0))
        if candidate_id in priors:
            score += priors[candidate_id].weight_delta

        candidates.append(CandidateCause(
            candidate_id=candidate_id,
            description=rule["description"],
            evidence_refs=_filter_available_refs(rule.get("evidence_refs", []), evidence),
            rule_score=min(max(score, 0.0), 1.0),
            missing_evidence=_detect_missing_evidence(candidate_id, evidence),
        ))

    candidates.sort(key=lambda item: item.rule_score, reverse=True)
    if not candidates:
        candidates.append(CandidateCause(
            candidate_id="insufficient_data",
            description="当前证据量不足以触发任何预置规则，建议补充采集",
            evidence_refs=[],
            rule_score=0.10,
            missing_evidence=["更长的采样时长", "多采集器交叉验证", "历史基线对比"],
        ))
    return candidates


@lru_cache(maxsize=1)
def load_rules(path: str | None = None) -> list[dict[str, Any]]:
    """加载外部规则配置。测试可传入 path 或 clear cache 后重载。"""
    rules_path = Path(path) if path else RULES_PATH
    try:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("RCA rules.json 格式错误: %s", exc)
        raise ValueError(f"RCA 规则文件 JSON 解析失败: {rules_path}") from exc
    except FileNotFoundError:
        logger.warning("RCA rules.json 未找到: %s，使用内置默认规则", rules_path)
        return _builtin_rules()
    if not isinstance(data, list):
        raise ValueError("RCA 规则文件必须是数组")
    # 过滤掉以 _comment 开头仅用于分组的条目
    rules = [item for item in data if "candidate_id" in item]
    for item in rules:
        _validate_rule(item)
    if not rules:
        logger.warning("RCA rules.json 没有有效规则，使用内置默认规则")
        return _builtin_rules()
    return rules


def _builtin_rules() -> list[dict[str, Any]]:
    """内置默认规则（当 rules.json 缺失或损坏时使用）。"""
    return [
        {
            "candidate_id": "cpu_hotspot_default",
            "description": "CPU 热点函数占比过高（默认规则）",
            "match_type": "top_function_keyword",
            "params": {"min_percent": 40, "keywords": ["hotspot", "fib", "recursive", "compute"]},
            "rule_score": 0.70,
            "evidence_refs": ["top_functions[0]"],
        },
        {
            "candidate_id": "io_wait_default",
            "description": "IO 延迟异常（默认规则）",
            "match_type": "ebpf_latency_present",
            "params": {},
            "rule_score": 0.65,
            "evidence_refs": ["ebpf_metrics"],
        },
    ]


def _validate_rule(rule: dict[str, Any]) -> None:
    required = {"candidate_id", "description", "match_type", "rule_score"}
    missing = sorted(required - set(rule))
    if missing:
        raise ValueError(f"RCA 规则缺少字段: {', '.join(missing)}")
    if rule["match_type"] not in _MATCHERS:
        raise ValueError(f"未知 RCA match_type: {rule['match_type']}")


def _match_rule(rule: dict[str, Any], evidence: EvidenceInput) -> bool:
    matcher = _MATCHERS[rule["match_type"]]
    return matcher(evidence, rule.get("params", {}))


def _match_top_function_keyword(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    if not evidence.top_functions:
        return False
    top = evidence.top_functions[0]
    if float(top.get("percent", 0)) < float(params.get("min_percent", 40)):
        return False
    name = top.get("name", "").lower()
    return any(keyword.lower() in name for keyword in params.get("keywords", []))


def _match_ebpf_latency_present(evidence: EvidenceInput, _params: dict[str, Any]) -> bool:
    histogram = evidence.ebpf_metrics.get("io_latency_us", {}) if evidence.ebpf_metrics else {}
    return isinstance(histogram, dict) and len(histogram) > 0


def _match_collector_or_suggestion(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    collector_type = params.get("collector_type", "")
    if evidence.task_metadata.get("collector_type") == collector_type:
        return True
    keyword = params.get("suggestion_keyword", "").lower()
    return bool(keyword) and any(keyword in item.lower() for item in evidence.suggestions)


def _match_agent_cpu_overhead(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    threshold = float(params.get("min_cpu_percent", 10))
    return float(evidence.agent_stats.get("max_cpu_percent", 0)) > threshold


def _match_failure_contains(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    status = params.get("status")
    if status and evidence.task_metadata.get("status") != status:
        return False
    joined = "\n".join(evidence.failure_events).lower()
    return any(keyword.lower() in joined for keyword in params.get("keywords", []))


# ── 多维指标匹配器 ──────────────────────────────────────────


def _match_sys_metric_threshold(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    """通过 sys_metrics 多维度阈值触发规则。

    params 支持的字段（任意组合，全部满足才触发）:
      metric_path: 期字段路径，如 "summary.thread_count", "summary.fd_trend"
      op: 比较操作符 "gt"/"lt"/"gte"/"lte"/"eq"/"contains"
      value: 比较值
      min_samples: 最少样本数
    """
    if not evidence.sys_metrics:
        return False
    sm = evidence.sys_metrics
    path = params.get("metric_path", "")
    op = params.get("op", "gt")
    expected = params.get("value")
    min_samples = params.get("min_samples", 1)

    # 检查样本数
    if sm.get("sample_count", 0) < min_samples:
        return False

    # 解析路径获取值
    val = _resolve_path(sm, path)
    if val is None:
        return False

    if op == "contains":
        return str(expected or "").lower() in str(val).lower()
    if op == "gt":
        return float(val) > float(expected or 0)
    if op == "lt":
        return float(val) < float(expected or 0)
    if op == "gte":
        return float(val) >= float(expected or 0)
    if op == "lte":
        return float(val) <= float(expected or 0)
    if op == "eq":
        return str(val) == str(expected)
    return False


def _match_multi_metric(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    """多指标组合匹配——所有子条件都满足才触发。

    params.conditions:
      [{"metric_path": "...", "op": "gt", "value": N}, ...]
    """
    conditions = params.get("conditions", [])
    if not conditions:
        return False
    for cond in conditions:
        if not _match_sys_metric_threshold(evidence, cond):
            return False
    return True


def _match_fd_trend(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    """FD 趋势检测——专门检查文件描述符是否持续增长。"""
    if not evidence.sys_metrics:
        return False
    summary = evidence.sys_metrics.get("summary", {})
    min_count = params.get("min_fd_count", 0)
    if summary.get("fd_count", 0) < min_count:
        return False
    trend = summary.get("fd_trend", "")
    return trend == "increasing"


def _match_thread_trend(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    """线程数趋势检测。"""
    if not evidence.sys_metrics:
        return False
    summary = evidence.sys_metrics.get("summary", {})
    min_count = params.get("min_threads", 50)
    if summary.get("thread_count", 0) < min_count:
        return False
    trend = summary.get("thread_trend", "")
    return trend == "increasing"


def _match_cross_evidence(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    """跨证据交叉验证：多个采集器或指标维度同时异常时触发。

    params.signals: list[str] — 信号名称列表
      支持的信号: cpu_hotspot, io_high, fd_growth, thread_growth,
                 memory_growth, net_high, sys_cpu_high, iowait_high
    所有 signals 都触发才算匹配。
    """
    signals = params.get("signals", [])
    if not signals:
        return False

    for sig in signals:
        if not _check_signal(sig, evidence):
            return False
    return True


def _match_dependency_failure(evidence: EvidenceInput, _params: dict[str, Any]) -> bool:
    for item in evidence.tool_results:
        if item.get("tool_name") != "dependency_check":
            continue
        if int(item.get("failed_dependency_count") or 0) > 0:
            return True
        summary = item.get("summary")
        if isinstance(summary, dict) and summary.get("failed_dependencies"):
            return True
    index = evidence.evidence_index or {}
    dependency = index.get("dependency_check")
    if not isinstance(dependency, dict):
        return False
    summary = dependency.get("summary")
    if isinstance(summary, dict) and summary.get("failed_dependencies"):
        return True
    return any(
        isinstance(item, dict) and item.get("success") is False
        for item in dependency.get("checks") or []
    )


def _match_redis_failure(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    min_latency_ms = float(params.get("min_latency_ms", 1000))
    for item in evidence.tool_results:
        if item.get("tool_name") != "redis_check":
            continue
        if item.get("ping_ok") is False or item.get("exporter_up") is False:
            return True
        if float(item.get("max_latency_ms") or 0) >= min_latency_ms:
            return True
        if int(item.get("slowlog_entry_count") or 0) > 0:
            return True
    index = evidence.evidence_index or {}
    redis = index.get("redis_check")
    if not isinstance(redis, dict):
        return False
    connectivity = redis.get("connectivity") if isinstance(redis.get("connectivity"), dict) else {}
    latency = redis.get("latency_summary") if isinstance(redis.get("latency_summary"), dict) else {}
    slowlog = redis.get("slowlog_summary") if isinstance(redis.get("slowlog_summary"), dict) else {}
    return (
        connectivity.get("ping_ok") is False
        or connectivity.get("exporter_up") is False
        or float(latency.get("max_latency_ms") or 0) >= min_latency_ms
        or int(slowlog.get("entry_count") or 0) > 0
    )


def _match_runtime_top_function_present(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    if not evidence.top_functions:
        return False
    collector_type = str(evidence.task_metadata.get("collector_type") or "")
    if collector_type not in {"pyspy", "process_python_runtime_profile"}:
        return False
    min_percent = float(params.get("min_percent", 1))
    top = evidence.top_functions[0]
    return float(top.get("percent") or 0) >= min_percent or int(top.get("samples") or 0) > 0


def _match_off_cpu_wait_hotspot(evidence: EvidenceInput, params: dict[str, Any]) -> bool:
    index = evidence.evidence_index or {}
    off_cpu = index.get("off_cpu_wait")
    if not isinstance(off_cpu, dict):
        off_cpu = getattr(evidence, "off_cpu_wait_json", None)
    if not isinstance(off_cpu, dict):
        return False
    summary = off_cpu.get("summary") if isinstance(off_cpu.get("summary"), dict) else {}
    min_samples = int(params.get("min_samples", 1))
    if int(summary.get("sample_count") or 0) >= min_samples:
        return True
    stacks = off_cpu.get("top_wait_stacks")
    return isinstance(stacks, list) and len(stacks) >= min_samples


_SIGNAL_CHECKERS: dict[str, Any] = {}


def _check_signal(signal: str, evidence: EvidenceInput) -> bool:
    """检查单个信号是否触发。"""
    if signal == "cpu_hotspot":
        return bool(evidence.top_functions) and float(
            evidence.top_functions[0].get("percent", 0)) > 30 if evidence.top_functions else False
    if signal == "io_high":
        ebpf = evidence.ebpf_metrics or {}
        lat = ebpf.get("io_latency_us", {}) if isinstance(ebpf, dict) else {}
        return len(lat) > 0
    if signal == "fd_growth":
        return (evidence.sys_metrics or {}).get("summary", {}).get("fd_trend") == "increasing"
    if signal == "thread_growth":
        return (evidence.sys_metrics or {}).get("summary", {}).get("thread_trend") == "increasing"
    if signal == "memory_growth":
        return (evidence.sys_metrics or {}).get("summary", {}).get("vmrss_mb", 0) > 100
    if signal == "net_high":
        net_rx = (evidence.sys_metrics or {}).get("summary", {}).get("net_rx_kbps", 0)
        net_tx = (evidence.sys_metrics or {}).get("summary", {}).get("net_tx_kbps", 0)
        return float(net_rx) > 5000 or float(net_tx) > 5000
    if signal == "sys_cpu_high":
        return (evidence.sys_metrics or {}).get("summary", {}).get("avg_cpu_sys_pct", 0) > 30
    if signal == "iowait_high":
        return (evidence.sys_metrics or {}).get("summary", {}).get("avg_cpu_iowait_pct", 0) > 10
    if signal == "ctx_high":
        return (evidence.sys_metrics or {}).get("summary", {}).get("ctx_nonvoluntary_rate", 0) > 1000
    return False


def _resolve_path(data: dict, path: str) -> Any:
    """按 '.' 分隔的路径解析嵌套字典，如 summary.fd_trend。"""
    parts = path.split(".")
    cur: Any = data
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


_MATCHERS = {
    "top_function_keyword": _match_top_function_keyword,
    "ebpf_latency_present": _match_ebpf_latency_present,
    "collector_or_suggestion": _match_collector_or_suggestion,
    "agent_cpu_overhead": _match_agent_cpu_overhead,
    "failure_contains": _match_failure_contains,
    "sys_metric_threshold": _match_sys_metric_threshold,
    "multi_metric": _match_multi_metric,
    "fd_trend": _match_fd_trend,
    "thread_trend": _match_thread_trend,
    "cross_evidence": _match_cross_evidence,
    "dependency_failure": _match_dependency_failure,
    "redis_failure": _match_redis_failure,
    "runtime_top_function_present": _match_runtime_top_function_present,
    "off_cpu_wait_hotspot": _match_off_cpu_wait_hotspot,
}


def _filter_available_refs(refs: list[str], evidence: EvidenceInput) -> list[str]:
    return [ref for ref in refs if _ref_available(ref, evidence)]


def _ref_available(ref: str, evidence: EvidenceInput) -> bool:
    top = ref.split(".", 1)[0].split("[", 1)[0]
    if top == "top_functions":
        return bool(evidence.top_functions)
    if top == "ebpf_metrics":
        return bool(evidence.ebpf_metrics)
    if top == "sys_metrics":
        return bool(evidence.sys_metrics)
    if top == "baseline_diff":
        return bool(evidence.baseline_diff)
    if top == "agent_stats":
        return bool(evidence.agent_stats)
    if top == "suggestions":
        return bool(evidence.suggestions)
    if top == "failure_events":
        return bool(evidence.failure_events)
    if top == "task_metadata":
        sub = ref.split(".", 1)[1] if "." in ref else ""
        return bool(evidence.task_metadata.get(sub)) if sub else bool(evidence.task_metadata)
    if top == "tool_results":
        tool_name = ref.split(".", 1)[1] if "." in ref else ""
        return any(item.get("tool_name") == tool_name for item in evidence.tool_results)
    if top == "evidence_index":
        sub = ref.split(".", 1)[1] if "." in ref else ""
        return bool(_resolve_path(evidence.evidence_index or {}, sub)) if sub else bool(evidence.evidence_index)
    return False


def _detect_missing_evidence(candidate_id: str, evidence: EvidenceInput) -> list[str]:
    missing: list[str] = []
    if not evidence.top_functions and candidate_id in ("cpu_hotspot_recursive", "python_userland_hotspot"):
        missing.append("缺少 TopN 热点函数数据")
    if not evidence.ebpf_metrics and candidate_id in ("io_wait_high",):
        missing.append("缺少 eBPF IO 延迟分布数据")
    if not evidence.sys_metrics and candidate_id.startswith(("fd_", "thread_", "memory_", "sys_cpu_", "network_", "multi_", "cross_")):
        missing.append("缺少系统多维指标数据 (sys_metrics)")
    if not evidence.baseline_diff and candidate_id in ("cpu_hotspot_recursive", "io_wait_high"):
        missing.append("缺少历史基线对比数据")
    return missing
