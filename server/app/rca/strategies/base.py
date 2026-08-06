"""RCA analysis strategy interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from server.app.rca.models import DiagnosisOutcome, FeedbackPrior


@dataclass(frozen=True)
class AnalysisContext:
    """All inputs needed by an RCA strategy."""

    task_id: str
    task_record: Any
    top_functions: list[dict] | None = None
    ebpf_metrics: dict | None = None
    sys_metrics: dict | None = None
    suggestions: list[str] | None = None
    failure_events: list[str] | None = None
    baseline_diff: dict | None = None
    agent_stats: dict | None = None
    evidence_index: dict | None = None
    feedback_priors: dict[str, FeedbackPrior] | None = None
    model_name: str | None = None
    task_events: list[dict] | None = None
    agent_record: Any | None = None
    repo: Any | None = None
    auto_execute_safe: bool = True
    analysis_pipeline: str = "evidence_to_attribution"
    structured_evidence: dict | None = None


class RCAAnalysisStrategy(Protocol):
    """Pluggable RCA strategy."""

    strategy_id: str

    def analyze(self, context: AnalysisContext) -> DiagnosisOutcome:
        """Run RCA and return a full diagnosis outcome."""
        ...
