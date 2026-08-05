"""Trace endpoint 采集器。

当前版本先复用 perf 采样骨架，用于保留调用栈上下文并回连到
endpoint / service 级别的后续分析入口。
"""

from __future__ import annotations

from dataclasses import replace

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.perf import PerfCollector


class TraceEndpointCollector:
    """调用链上下文采集器。"""

    def __init__(self) -> None:
        self._perf = PerfCollector()

    def collect(self, task: CollectorTask) -> CollectorResult:
        derived_task = replace(
            task,
            options={
                **task.options,
                "callgraph": task.options.get("callgraph", "fp"),
                "event": task.options.get("event", "cpu-cycles:u"),
            },
        )
        result = self._perf.collect(derived_task)
        if not result.ok:
            return result
        return CollectorResult(
            ok=True,
            reason="trace endpoint 上下文采集完成",
            artifacts=result.artifacts,
        )
