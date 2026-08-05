"""新增深挖采集器的包装行为测试。"""

from unittest import mock

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.baseline import BaselineWindowCollector
from agent.mini_drop_agent.collectors.off_cpu import OffCPUCollector
from agent.mini_drop_agent.collectors.trace import TraceEndpointCollector


def _task(collector_type: str) -> CollectorTask:
    return CollectorTask(
        id=f"{collector_type}_test",
        collector_type=collector_type,
        target_pid=1234,
        sample_rate=11,
        duration_sec=15,
        options={},
    )


def test_off_cpu_collector_forces_off_cpu_event():
    collector = OffCPUCollector()
    with mock.patch.object(
        collector._perf,
        "collect",
        return_value=CollectorResult(ok=True, reason="perf ok", artifacts=[{"artifact_type": "raw"}]),
    ) as perf_collect:
        result = collector.collect(_task("off_cpu_wait_profile"))

    assert result.ok is True
    assert result.reason == "off-CPU 等待采集完成"
    assert perf_collect.call_args.args[0].options["event"] == "sched:sched_switch"
    assert perf_collect.call_args.args[0].options["all_user"] is False


def test_trace_endpoint_collector_keeps_stack_context_defaults():
    collector = TraceEndpointCollector()
    with mock.patch.object(
        collector._perf,
        "collect",
        return_value=CollectorResult(ok=True, reason="perf ok", artifacts=[{"artifact_type": "raw"}]),
    ) as perf_collect:
        result = collector.collect(_task("trace_endpoint_profile"))

    assert result.ok is True
    assert result.reason == "trace endpoint 上下文采集完成"
    assert perf_collect.call_args.args[0].options["callgraph"] == "fp"
    assert perf_collect.call_args.args[0].options["event"] == "cpu-cycles:u"


def test_baseline_window_collector_uses_continuous_sampling_defaults():
    collector = BaselineWindowCollector()
    with mock.patch.object(
        collector._continuous,
        "collect",
        return_value=CollectorResult(ok=True, reason="continuous ok", artifacts=[{"artifact_type": "continuous_summary"}]),
    ) as continuous_collect:
        result = collector.collect(_task("baseline_window_profile"))

    assert result.ok is True
    assert result.reason == "baseline 窗口采集完成"
    assert collector._continuous.WINDOW_DURATION_SEC == 5
    assert collector._continuous.WINDOW_INTERVAL_SEC == 15
    assert collector._continuous.WINDOW_SAMPLE_RATE == 11
    continuous_collect.assert_called_once()

