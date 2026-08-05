"""Off-CPU 采集器。

当前版本先复用 perf 采样骨架，以 `sched:sched_switch` 作为默认事件，
用于补齐阻塞等待类证据的任务链路。
"""

from __future__ import annotations

from dataclasses import replace

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.perf import PerfCollector


class OffCPUCollector:
    """Off-CPU 等待采集器。"""

    DEFAULT_EVENT = "sched:sched_switch"

    def __init__(self) -> None:
        self._perf = PerfCollector()

    def collect(self, task: CollectorTask) -> CollectorResult:
        derived_task = replace(
            task,
            options={
                **task.options,
                "event": task.options.get("event", self.DEFAULT_EVENT),
                "all_user": False,
            },
        )
        result = self._perf.collect(derived_task)
        if not result.ok:
            return result
        return CollectorResult(
            ok=True,
            reason="off-CPU 等待采集完成",
            artifacts=result.artifacts,
        )
