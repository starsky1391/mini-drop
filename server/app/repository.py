"""
内存存储层：Agent 注册、任务管理、状态迁移和审计日志。

gRPC 服务和 HTTP API 共享同一个 Repository 实例。
当前阶段使用 Python dict 存储，后续引入 PostgreSQL 时替换实现即可。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from server.app.schemas import AgentRegistration, CreateTaskRequest
from server.app.prometheus_metrics import record_task_transition
from server.app.state_machine import (
    Actor,
    StatusEvent,
    TaskStatus,
    build_status_event,
    now_utc,
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class AgentRecord:
    """Agent 注册信息与在线状态。"""

    id: str
    hostname: str
    ip_addr: str
    version: str
    os_info: str
    capabilities: list[str]
    status: str  # "ONLINE" | "OFFLINE"
    last_heartbeat_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass
class TaskRecord:
    """任务主表记录。"""

    id: str
    name: str
    agent_id: str
    target_pid: int
    collector_type: str
    sample_rate: int
    duration_sec: int
    status: TaskStatus
    status_reason: str
    request_params: dict[str, Any]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class AuditLog:
    """审计事件。Agent 上下线和任务创建/失败时写入。"""

    event_type: str
    message: str
    agent_id: str | None = None
    task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=now_utc)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class InMemoryRepository:
    """线程安全的内存存储实现。"""

    def __init__(self) -> None:
        self.agents: dict[str, AgentRecord] = {}
        self.tasks: dict[str, TaskRecord] = {}
        self.events: list[StatusEvent] = []
        self.audit_logs: list[AuditLog] = []
        self.artifacts: dict[str, list[dict[str, Any]]] = {}
        self.agent_metrics: dict[str, dict[str, Any]] = {}

        # 每个 Agent IP 维护一个任务队列。
        # key = agent.ip_addr, value = deque of task_id
        self._task_queues: dict[str, deque[str]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------------

    def register_agent(
        self, agent_id: str, hostname: str, ip_addr: str,
        version: str = "0.1.0", os_info: str = "unknown",
        capabilities: list[str] | None = None,
    ) -> AgentRecord:
        """注册或更新 Agent 信息。从 OFFLINE 恢复时写入审计日志。"""
        with self._lock:
            caps = list(capabilities or [])
            existing = self.agents.get(agent_id)
            timestamp = now_utc()

            if existing is not None and existing.status == "OFFLINE":
                self._append_audit(
                    event_type="AGENT_ONLINE",
                    agent_id=agent_id,
                    message=f"{agent_id} 恢复在线",
                )

            record = AgentRecord(
                id=agent_id,
                hostname=hostname,
                ip_addr=ip_addr,
                version=version,
                os_info=os_info,
                capabilities=caps,
                status="ONLINE",
                last_heartbeat_at=timestamp,
                created_at=existing.created_at if existing else timestamp,
                updated_at=timestamp,
            )
            self.agents[agent_id] = record

            # 确保任务队列存在
            if ip_addr not in self._task_queues:
                self._task_queues[ip_addr] = deque()

            return record

    def heartbeat(self, agent_id: str, ip_addr: str) -> TaskRecord | None:
        """记录心跳并返回该 Agent IP 队列中的下一个待执行任务。

        如果队列中有 PENDING 任务，将其迁移到 RUNNING 后返回。
        """
        with self._lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                return None

            agent.status = "ONLINE"
            agent.last_heartbeat_at = now_utc()
            agent.updated_at = now_utc()

            # 从该 IP 的任务队列取下一个 PENDING 任务
            queue = self._task_queues.get(ip_addr)
            if not queue:
                return None

            while queue:
                task_id = queue[0]
                task = self.tasks.get(task_id)
                if task is not None and task.status == TaskStatus.PENDING:
                    queue.popleft()
                    self.transition_task(
                        task_id, TaskStatus.RUNNING,
                        "Agent 心跳拉取待执行任务", Actor.SERVER,
                    )
                    return task
                # 任务已被删除或状态不一致，跳过
                queue.popleft()

            return None

    def heartbeat_only(self, agent_id: str, ip_addr: str) -> None:
        """只记录心跳，不派发任务。用于 Agent 忙碌时保持在线。"""
        with self._lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                return
            agent.status = "ONLINE"
            agent.last_heartbeat_at = now_utc()
            agent.updated_at = now_utc()

    def mark_offline_agents(self, timeout_sec: int = 30) -> list[AgentRecord]:
        """将超时未心跳的 Agent 标记为 OFFLINE。"""
        with self._lock:
            cutoff = now_utc() - timedelta(seconds=timeout_sec)
            changed: list[AgentRecord] = []
            for agent in self.agents.values():
                if agent.status == "ONLINE" and agent.last_heartbeat_at < cutoff:
                    agent.status = "OFFLINE"
                    agent.updated_at = now_utc()
                    changed.append(agent)
                    self._append_audit(
                        event_type="AGENT_OFFLINE",
                        agent_id=agent.id,
                        message=f"{agent.id} 心跳超时 {timeout_sec}s，标记为离线",
                    )
            return changed

    def find_agent_by_ip(self, ip_addr: str) -> AgentRecord | None:
        """按 IP 查询 Agent。Control.CreateTask 通过 target_ip 定位任务队列。"""
        with self._lock:
            for agent in self.agents.values():
                if agent.ip_addr == ip_addr:
                    return agent
            return None

    # ------------------------------------------------------------------
    # Task
    # ------------------------------------------------------------------

    def create_task(self, payload: CreateTaskRequest) -> TaskRecord:
        """创建任务，写入 PENDING 状态，加入对应 Agent IP 的队列。"""
        with self._lock:
            timestamp = now_utc()
            hex_suffix = uuid4().hex[:6]
            task_id = f"task_{timestamp.strftime('%Y%m%d_%H%M%S')}_{hex_suffix}"

            task = TaskRecord(
                id=task_id,
                name=payload.name,
                agent_id=payload.agent_id,
                target_pid=payload.target_pid,
                collector_type=payload.collector_type,
                sample_rate=payload.sample_rate,
                duration_sec=payload.duration_sec,
                status=TaskStatus.PENDING,
                status_reason="Web 请求创建任务",
                request_params=payload.model_dump(),
                created_at=timestamp,
            )
            self.tasks[task_id] = task

            # 状态事件
            self.events.append(
                build_status_event(
                    task_id, None, TaskStatus.PENDING,
                    "Web 请求创建任务", Actor.WEB,
                    payload.model_dump(),
                )
            )
            record_task_transition("NONE", TaskStatus.PENDING.value)

            # 审计日志
            self._append_audit(
                event_type="TASK_CREATED",
                task_id=task_id,
                message=f"任务 {task_id} 已创建",
                metadata=payload.model_dump(),
            )

            # 加入目标 Agent IP 的任务队列
            agent = self.agents.get(payload.agent_id)
            if agent is not None:
                ip = agent.ip_addr
                if ip not in self._task_queues:
                    self._task_queues[ip] = deque()
                self._task_queues[ip].append(task_id)

            return task

    def transition_task(
        self, task_id: str, to_status: TaskStatus,
        reason: str, actor: Actor,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        """对指定任务执行一次状态迁移。

        校验由 build_status_event 内部完成，不合法时抛出 ValueError。
        """
        with self._lock:
            if task_id not in self.tasks:
                raise ValueError(f"任务不存在: {task_id}")
            task = self.tasks[task_id]
            event = build_status_event(
                task_id, task.status, to_status, reason, actor, metadata,
            )
            self.events.append(event)
            record_task_transition(task.status.value, to_status.value)
            task.status = to_status
            task.status_reason = reason
            if to_status == TaskStatus.RUNNING and task.started_at is None:
                task.started_at = now_utc()
            if to_status in (TaskStatus.DONE, TaskStatus.FAILED):
                task.finished_at = now_utc()
            return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        """按 ID 查询任务。"""
        return self.tasks.get(task_id)

    def get_tasks(self) -> list[TaskRecord]:
        """返回所有任务的列表。"""
        return list(self.tasks.values())

    def get_task_events(self, task_id: str) -> list[StatusEvent]:
        """返回指定任务的所有状态迁移事件。"""
        return [e for e in self.events if e.task_id == task_id]

    def record_agent_metrics(self, agent_id: str, metrics: dict[str, Any]) -> None:
        with self._lock:
            self.agent_metrics[agent_id] = {
                **self.agent_metrics.get(agent_id, {}),
                **dict(metrics),
            }

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def add_artifacts(self, task_id: str, artifacts: list[dict[str, Any]]) -> None:
        """追加采集产物元数据。"""
        with self._lock:
            self.artifacts.setdefault(task_id, []).extend(artifacts)

    def get_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        """查询产物列表。"""
        return self.artifacts.get(task_id, [])

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _append_audit(
        self, event_type: str, message: str,
        agent_id: str | None = None, task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """写入审计日志。非线程安全，调用方需持有 _lock。"""
        self.audit_logs.append(
            AuditLog(
                event_type=event_type,
                message=message,
                agent_id=agent_id,
                task_id=task_id,
                metadata=metadata or {},
            )
        )

    def get_audit_logs(self) -> list[AuditLog]:
        """返回审计日志列表。"""
        return list(self.audit_logs)

    # ------------------------------------------------------------------
    # 序列化辅助
    # ------------------------------------------------------------------

    def as_dict(self, value: Any) -> dict[str, Any]:
        """将数据类或枚举转换为纯 dict。"""
        if isinstance(value, (AgentRecord, TaskRecord, AuditLog, StatusEvent)):
            return asdict(value)
        return value
