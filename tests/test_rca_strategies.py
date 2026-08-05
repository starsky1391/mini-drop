"""RCA strategy selection tests."""

from __future__ import annotations

from unittest import mock

from server.app.rca.pipelines import normalize_pipeline_id
from server.app.rca.report import run_diagnosis_context
from server.app.rca.strategies import normalize_strategy_id


class _Task:
    id = "task_strategy"
    agent_id = "agent_strategy"
    collector_type = "perf_cpu"
    target_pid = 1234
    duration_sec = 15
    sample_rate = 99
    status = "DONE"
    status_reason = "analysis completed"


class _Repo:
    def __init__(self):
        self.created_payloads = []

    def create_task(self, payload):
        self.created_payloads.append(payload)

        class _Created:
            id = "task_created"

        return _Created()


def test_strategy_aliases():
    assert normalize_strategy_id("linear") == "linear"
    assert normalize_strategy_id("B") == "graph"
    assert normalize_strategy_id("graph-causal") == "graph"
    assert normalize_strategy_id("C") == "interactive"


def test_pipeline_aliases():
    assert normalize_pipeline_id("legacy") == "legacy"
    assert normalize_pipeline_id("hypothesis") == "legacy"
    assert normalize_pipeline_id("evidence") == "evidence_to_attribution"
    assert normalize_pipeline_id("eta") == "evidence_to_attribution"


def test_linear_strategy_is_default_without_graph_tool():
    with mock.patch.dict("os.environ", {}, clear=True):
        outcome = run_diagnosis_context(
            task_id="task_strategy",
            task_record=_Task(),
            top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
            sys_metrics={
                "summary": {
                    "avg_cpu_user_pct": 88.0,
                    "avg_cpu_iowait_pct": 1.0,
                },
            },
            auto_execute_safe=False,
        )

    tool_names = [item.tool_name for item in outcome.tool_results]
    assert "build_causal_graph" not in tool_names
    assert outcome.report.model_name == "rule-engine-only"
    analysis_result = outcome.report.evidence_snapshot["analysis_result"]
    assert analysis_result["allowed_cause_ids"] == ["cpu_hotspot_recursive"]
    assert analysis_result["conclusion_boundary"]["max_supported_level"] == "function"


def test_legacy_pipeline_skips_evidence_to_attribution():
    with mock.patch.dict("os.environ", {}, clear=True):
        outcome = run_diagnosis_context(
            task_id="task_strategy",
            task_record=_Task(),
            top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
            analysis_pipeline="legacy",
            auto_execute_safe=False,
        )

    assert outcome.report.evidence_snapshot["analysis_result"] is None
    assert outcome.report.report.ranked_causes[0].cause_id == "cpu_hotspot_recursive"


def test_graph_strategy_adds_causal_graph_evidence():
    with mock.patch.dict("os.environ", {}, clear=True):
        outcome = run_diagnosis_context(
            task_id="task_strategy",
            task_record=_Task(),
            top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
            sys_metrics={
                "sample_count": 10,
                "summary": {
                    "avg_cpu_user_pct": 88.0,
                    "avg_cpu_sys_pct": 5.0,
                    "avg_cpu_iowait_pct": 1.0,
                    "thread_count": 20,
                    "thread_trend": "stable",
                    "fd_count": 20,
                    "fd_trend": "stable",
                    "vmrss_mb": 200,
                },
            },
            analysis_strategy="graph",
            auto_execute_safe=False,
        )

    graph_tool = next(item for item in outcome.tool_results if item.tool_name == "build_causal_graph")
    assert graph_tool.status == "success"
    assert graph_tool.output["strategy"] == "graph"
    assert graph_tool.output["ranked_causes"]
    assert outcome.report.report.ranked_causes[0].evidence_refs
    assert "analysis_result" in outcome.report.evidence_snapshot


def test_run_diagnosis_context_keeps_depth_evidence_snapshot():
    evidence_index = {
        "stack_samples": [{
            "hot_frame": "compute_hotspot",
            "call_path": "main;worker;compute_hotspot",
            "stack_fragment": ["main", "worker", "compute_hotspot"],
            "wait_reason": "cpu_hotspot",
            "context_id": "ctx-1",
        }],
        "context": {
            "call_path": "main;worker;compute_hotspot",
            "endpoint": "/api/order/create",
            "context_id": "ctx-1",
            "trace_id": "trace-1",
        },
    }

    with mock.patch.dict("os.environ", {}, clear=True):
        outcome = run_diagnosis_context(
            task_id="task_strategy",
            task_record=_Task(),
            top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
            sys_metrics={
                "summary": {
                    "avg_cpu_user_pct": 88.0,
                    "avg_cpu_iowait_pct": 1.0,
                },
            },
            evidence_index=evidence_index,
            auto_execute_safe=False,
        )

    assert outcome.report.evidence_snapshot["evidence_index"]["context"]["call_path"] == "main;worker;compute_hotspot"
    assert outcome.report.evidence_snapshot["evidence_index"]["stack_samples"][0]["hot_frame"] == "compute_hotspot"


def test_evidence_to_attribution_pipeline_is_stable_against_candidate_order():
    with mock.patch.dict("os.environ", {}, clear=True):
        outcome_a = run_diagnosis_context(
            task_id="task_strategy",
            task_record=_Task(),
            top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
            sys_metrics={
                "summary": {
                    "avg_cpu_user_pct": 88.0,
                    "avg_cpu_iowait_pct": 1.0,
                },
            },
            auto_execute_safe=False,
        )
        outcome_b = run_diagnosis_context(
            task_id="task_strategy",
            task_record=_Task(),
            top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
            sys_metrics={
                "summary": {
                    "avg_cpu_user_pct": 88.0,
                    "avg_cpu_iowait_pct": 1.0,
                },
            },
            auto_execute_safe=False,
        )

    assert outcome_a.report.report.ranked_causes[0].cause_id == outcome_b.report.report.ranked_causes[0].cause_id
    assert outcome_a.report.report.primary_cause_id == outcome_b.report.report.primary_cause_id
    assert outcome_a.report.report.conclusion_boundary.max_supported_level == "function"


def test_interactive_strategy_placeholder_does_not_schedule_tasks():
    repo = _Repo()
    outcome = run_diagnosis_context(
        task_id="task_strategy",
        task_record=_Task(),
        repo=repo,
        analysis_strategy="interactive",
    )

    assert repo.created_payloads == []
    assert outcome.report.model_name == "interactive-probe-disabled"
    assert outcome.report.report.not_enough_evidence is True
