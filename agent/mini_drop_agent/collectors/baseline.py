"""基线窗口采集器。

当前版本复用连续 perf 采样，用于提供重复任务下的窗口对比能力。
"""

from __future__ import annotations

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.continuous import ContinuousCollector


class BaselineWindowCollector:
    """基线窗口采集器。"""

    def __init__(self) -> None:
        self._continuous = ContinuousCollector()
        self._continuous.WINDOW_DURATION_SEC = 5
        self._continuous.WINDOW_INTERVAL_SEC = 15
        self._continuous.WINDOW_SAMPLE_RATE = 11

    def collect(self, task: CollectorTask) -> CollectorResult:
        result = self._continuous.collect(task)
        if not result.ok:
            return result
        return CollectorResult(
            ok=True,
            reason="baseline 窗口采集完成",
            artifacts=result.artifacts,
        )
