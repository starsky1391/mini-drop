"""置信度校准层。

组合加权公式对每个候选原因进行多维度打分，输出校准后的置信度。
"""

from __future__ import annotations

from server.app.rca.models import CalibratedCause, CandidateCause, EvidenceInput, FeedbackPrior


# 权重分配
_W_RULE = 0.35
_W_EVIDENCE_QUALITY = 0.25
_W_BASELINE = 0.15
_W_CROSS_COLLECTOR = 0.15
_W_FEEDBACK = 0.10


def calibrate(
    candidates: list[CandidateCause],
    evidence: EvidenceInput,
    feedback_priors: dict[str, FeedbackPrior] | None = None,
) -> list[CalibratedCause]:
    """对候选原因列表进行多维度置信度校准。

    维度：
      1. 规则分 (0.35) —— 规则引擎的原始命中强度
      2. 证据质量 (0.25) —— 证据完整性、样本量是否充足
      3. 基线支持 (0.15) —— 与历史基线的偏离程度
      4. 交叉验证 (0.15) —— 多采集器之间是否互相支持
      5. 反馈先验 (0.10) —— 历史人工标注的修正
    """
    priors = feedback_priors or {}

    calibrated: list[CalibratedCause] = []
    for c in candidates:
        rule_score = c.rule_score
        evidence_quality = _score_evidence_quality(c, evidence)
        baseline_support = _score_baseline_support(c, evidence)
        cross_collector = _score_cross_collector_agreement(c, evidence)
        feedback_prior = _score_feedback_prior(c, priors)

        final = (
            rule_score * _W_RULE
            + evidence_quality * _W_EVIDENCE_QUALITY
            + baseline_support * _W_BASELINE
            + cross_collector * _W_CROSS_COLLECTOR
            + feedback_prior * _W_FEEDBACK
        )

        calibrated.append(CalibratedCause(
            candidate_id=c.candidate_id,
            description=c.description,
            evidence_refs=c.evidence_refs,
            final_confidence=min(max(final, 0.0), 1.0),
            rule_score=rule_score,
            evidence_quality=evidence_quality,
            baseline_support=baseline_support,
            cross_collector_agreement=cross_collector,
            feedback_prior=feedback_prior,
            missing_evidence=_unique_preserve_order(c.missing_evidence),
        ))

    calibrated.sort(key=lambda c: (-c.final_confidence, c.candidate_id))
    return calibrated


def format_for_llm(
    calibrated: list[CalibratedCause],
    primary_cause_id: str | None = None,
) -> str:
    """将校准后的候选原因列表格式化为 LLM 输入。"""
    import json
    if primary_cause_id:
        calibrated = sorted(
            calibrated,
            key=lambda item: (
                item.candidate_id != primary_cause_id,
                -item.final_confidence,
                item.candidate_id,
            ),
        )
    else:
        calibrated = sorted(
            calibrated,
            key=lambda item: (-item.final_confidence, item.candidate_id),
        )
    items = []
    for c in calibrated:
        items.append({
            "candidate_id": c.candidate_id,
            "description": c.description,
            "final_confidence": round(c.final_confidence, 3),
            "evidence_refs": _unique_preserve_order(c.evidence_refs),
            "missing_evidence": _unique_preserve_order(c.missing_evidence),
        })
    return json.dumps(items, indent=2, ensure_ascii=False)


def interpret_confidence(confidence: float) -> str:
    """解释置信度水平。"""
    if confidence >= 0.80:
        return "高置信"
    if confidence >= 0.60:
        return "可能"
    if confidence >= 0.40:
        return "待验证"
    return "证据不足"


# ── 评分函数 ──


def _score_evidence_quality(c: CandidateCause, ev: EvidenceInput) -> float:
    """证据质量评分：数据越完整，分越高。"""
    score = 0.5  # 基础分
    if ev.top_functions:
        score += 0.15
        if len(ev.top_functions) >= 3:
            score += 0.05
        if ev.top_functions[0].get("percent", 0) > 30:
            score += 0.05
    if ev.task_metadata.get("duration_sec", 0) >= 15:
        score += 0.05
    if ev.task_metadata.get("duration_sec", 0) >= 30:
        score += 0.05
    if ev.ebpf_metrics:
        score += 0.10
    if ev.baseline_diff:
        score += 0.05
    return min(score, 1.0)


def _score_baseline_support(c: CandidateCause, ev: EvidenceInput) -> float:
    """基线支持评分：是否有历史对比数据。"""
    if ev.baseline_diff is None:
        return 0.3  # 无基线，中性
    if ev.baseline_diff.get("top_function_changed"):
        return 0.8
    if ev.baseline_diff.get("cpu_percent_delta", 0) > 20:
        return 0.7
    if ev.baseline_diff.get("io_latency_p95_increased"):
        return 0.8
    return 0.5


def _score_cross_collector_agreement(c: CandidateCause, ev: EvidenceInput) -> float:
    """交叉验证评分：不同采集器之间是否互相支持。"""
    has_perf = ev.top_functions is not None and len(ev.top_functions) > 0
    has_ebpf = ev.ebpf_metrics is not None
    has_suggestions = len(ev.suggestions) > 0
    successful_tools = {
        item.get("tool_name") for item in ev.tool_results
        if item.get("status") == "success"
    }
    has_tool_support = bool(successful_tools)

    if not has_perf and not has_tool_support:
        return 0.3

    evidence_count = sum([has_perf, has_ebpf, has_suggestions, has_tool_support])
    if evidence_count >= 3:
        return 0.85
    if evidence_count >= 2:
        return 0.60
    return 0.35


def _score_feedback_prior(c: CandidateCause, priors: dict[str, FeedbackPrior]) -> float:
    """反馈先验：历史用户标注对此候选的支持度。"""
    if c.candidate_id not in priors:
        return 0.50  # 中性，无先验
    prior = priors[c.candidate_id]
    total = prior.positive_count + prior.negative_count
    if total == 0:
        return 0.50
    return prior.positive_count / total


def _unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique
