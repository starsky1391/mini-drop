"""智能归因编排入口。"""

from __future__ import annotations

import os

from server.app.rca.models import DiagnosisOutcome, FeedbackPrior, ValidatedReport
from server.app.rca.pipelines import normalize_pipeline_id
from server.app.rca.strategies import AnalysisContext, get_strategy


def run_diagnosis(
    task_id: str,
    task_record,
    top_functions: list[dict] | None = None,
    ebpf_metrics: dict | None = None,
    sys_metrics: dict | None = None,
    evidence_index: dict | None = None,
    suggestions: list[str] | None = None,
    failure_events: list[str] | None = None,
    baseline_diff: dict | None = None,
    agent_stats: dict | None = None,
    feedback_priors: dict[str, FeedbackPrior] | None = None,
    model_name: str | None = None,
    analysis_strategy: str | None = None,
    analysis_pipeline: str | None = None,
) -> ValidatedReport:
    """执行一次完整的智能归因。"""
    return run_diagnosis_context(
        task_id=task_id,
        task_record=task_record,
        top_functions=top_functions,
        ebpf_metrics=ebpf_metrics,
        sys_metrics=sys_metrics,
        evidence_index=evidence_index,
        suggestions=suggestions,
        failure_events=failure_events,
        baseline_diff=baseline_diff,
        agent_stats=agent_stats,
        feedback_priors=feedback_priors,
        model_name=model_name,
        analysis_strategy=analysis_strategy,
        analysis_pipeline=analysis_pipeline,
    ).report


def run_diagnosis_context(
    task_id: str,
    task_record,
    top_functions: list[dict] | None = None,
    ebpf_metrics: dict | None = None,
    sys_metrics: dict | None = None,
    evidence_index: dict | None = None,
    suggestions: list[str] | None = None,
    failure_events: list[str] | None = None,
    baseline_diff: dict | None = None,
    agent_stats: dict | None = None,
    feedback_priors: dict[str, FeedbackPrior] | None = None,
    model_name: str | None = None,
    task_events: list[dict] | None = None,
    agent_record=None,
    repo=None,
    auto_execute_safe: bool = True,
    analysis_strategy: str | None = None,
    analysis_pipeline: str | None = None,
) -> DiagnosisOutcome:
    """执行带工具证据和修复计划的完整诊断。"""
    strategy = get_strategy(analysis_strategy or os.getenv("MINI_DROP_RCA_STRATEGY", "linear"))
    pipeline = normalize_pipeline_id(
        analysis_pipeline or os.getenv("MINI_DROP_RCA_PIPELINE", "evidence_to_attribution")
    )
    return strategy.analyze(AnalysisContext(
        task_id=task_id,
        task_record=task_record,
        top_functions=top_functions,
        ebpf_metrics=ebpf_metrics,
        sys_metrics=sys_metrics,
        evidence_index=evidence_index,
        suggestions=suggestions,
        failure_events=failure_events,
        baseline_diff=baseline_diff,
        agent_stats=agent_stats,
        feedback_priors=feedback_priors,
        model_name=model_name,
        task_events=task_events,
        agent_record=agent_record,
        repo=repo,
        auto_execute_safe=auto_execute_safe,
        analysis_pipeline=pipeline,
    ))
