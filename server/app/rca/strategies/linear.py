"""Default linear RCA pipeline."""

from __future__ import annotations

from server.app.ai_provider import get_ai_settings
from server.app.rca.attribution import analyze_evidence
from server.app.rca.calibrator import calibrate, format_for_llm
from server.app.rca.candidates import generate_candidates
from server.app.rca.evidence import collect_evidence
from server.app.rca.llm_client import diagnose
from server.app.rca.models import DiagnosisOutcome
from server.app.rca.repair import build_repair_plan, execute_safe_actions
from server.app.rca.strategies.base import AnalysisContext
from server.app.rca.tools import run_rca_tools, tool_results_to_evidence


class LinearRCAAnalyzer:
    """Current evidence -> rules -> confidence -> LLM -> repair flow."""

    strategy_id = "linear"

    def analyze(self, context: AnalysisContext) -> DiagnosisOutcome:
        model = context.model_name or get_ai_settings().model

        tool_results = run_rca_tools(
            task_record=context.task_record,
            top_functions=context.top_functions,
            ebpf_metrics=context.ebpf_metrics,
            baseline_diff=context.baseline_diff,
            task_events=context.task_events,
            agent_record=context.agent_record,
        )

        evidence = collect_evidence(
            task_id=context.task_id,
            task_record=context.task_record,
            top_functions=context.top_functions,
            ebpf_metrics=context.ebpf_metrics,
            sys_metrics=context.sys_metrics,
            evidence_index=context.evidence_index,
            suggestions=context.suggestions,
            failure_events=context.failure_events,
            baseline_diff=context.baseline_diff,
            agent_stats=context.agent_stats,
            tool_results=tool_results_to_evidence(tool_results),
        )

        candidates = generate_candidates(evidence, context.feedback_priors)
        calibrated = calibrate(candidates, evidence, context.feedback_priors)
        primary_cause_id = None
        if context.analysis_pipeline == "evidence_to_attribution":
            analysis_result = analyze_evidence(evidence, candidates)
            evidence = evidence.model_copy(update={
                "analysis_result": analysis_result.model_dump(),
            })
            allowed_cause_ids = set(analysis_result.allowed_cause_ids)
            calibrated = [
                item for item in calibrated
                if item.candidate_id in allowed_cause_ids
            ]
            primary_cause_id = analysis_result.primary_cause_id
        result = diagnose(
            context.task_id,
            evidence,
            format_for_llm(calibrated, primary_cause_id),
            model_name=model,
        )

        repair_plan = build_repair_plan(context.task_id, result.report, evidence)
        if context.auto_execute_safe and context.repo is not None:
            repair_plan = execute_safe_actions(repair_plan, context.repo)

        return DiagnosisOutcome(
            report=result,
            tool_results=tool_results,
            repair_plan=repair_plan,
        )
