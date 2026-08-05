"""Interactive probe RCA strategy placeholder."""

from __future__ import annotations

from server.app.rca.models import DiagnosisOutcome, DiagnosisReport, ValidatedReport
from server.app.rca.repair import build_repair_plan
from server.app.rca.strategies.base import AnalysisContext


class InteractiveProbeRCAAnalyzer:
    """Scheme C placeholder.

    This strategy is intentionally non-scheduling for now. It documents the
    future module boundary without allowing an AI loop to create follow-up tasks.
    """

    strategy_id = "interactive"

    def analyze(self, context: AnalysisContext) -> DiagnosisOutcome:
        report = ValidatedReport(
            task_id=context.task_id,
            model_name="interactive-probe-disabled",
            evidence_snapshot={"strategy": self.strategy_id},
            report=DiagnosisReport(
                summary="交互式探针策略尚未启用：当前版本不会由 AI 自动连续发布采集任务。",
                ranked_causes=[],
                facts=["方案 C 已预留策略模块，但需要额外的循环预算、审批和停止条件。"],
                not_enough_evidence=True,
            ),
            validated=True,
        )
        return DiagnosisOutcome(
            report=report,
            tool_results=[],
            repair_plan=build_repair_plan(context.task_id, report.report, _empty_evidence(context)),
        )


def _empty_evidence(context: AnalysisContext):
    from server.app.rca.models import EvidenceInput

    return EvidenceInput(task_metadata={
        "task_id": context.task_id,
        "collector_type": getattr(context.task_record, "collector_type", "unknown"),
        "agent_id": getattr(context.task_record, "agent_id", None),
        "target_pid": getattr(context.task_record, "target_pid", None),
    })
