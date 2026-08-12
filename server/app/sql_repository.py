"""SQLAlchemy 持久化 Repository。

接口与 InMemoryRepository 保持一致，替换时 gRPC 服务和 HTTP handler
无需修改调用代码。通过 DATABASE_URL 切换 PostgreSQL / SQLite 后端。
"""

from __future__ import annotations

import json
import threading
import time

from server.app.event_bus import notify_task_changed, notify_agent_status
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session as OrmSession

from server.app.database import new_session
from server.app.models import (
    AgentMetricSnapshotModel,
    AgentModel,
    ArtifactModel,
    AuditLogModel,
    DiagnosisReportModel,
    DiagnosisRunModel,
    DiagnosisToolResultModel,
    RCAFeedbackModel,
    RCAFeedbackWeightModel,
    RepairPlanModel,
    StatusEventModel,
    TaskModel,
)
from server.app.prometheus_metrics import record_task_transition
from server.app.rca.models import FeedbackPrior
from server.app.schemas import CreateTaskRequest
from server.app.state_machine import (
    Actor,
    StatusEvent,
    TaskStatus,
    build_status_event,
    now_utc,
)


class SqlRepository:
    """SQLAlchemy 持久化 Repository。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Compatibility shim for older tests; dispatch now reads PENDING tasks from DB.
        self._task_queues: dict[str, deque[str]] = {}
        self.agent_metrics: dict[str, dict[str, Any]] = {}
        # TTL 缓存：key → (expires_at, value)
        self._cache: dict[str, tuple[float, Any]] = {}

    def _cached(self, key: str, ttl_sec: float, factory):
        """带 TTL 的简单缓存。

        如果 key 未过期则返回缓存值，否则调用 factory() 重新计算并缓存。
        """
        now = time.monotonic()
        if key in self._cache:
            expires_at, value = self._cache[key]
            if now < expires_at:
                return value
        value = factory()
        self._cache[key] = (now + ttl_sec, value)
        return value

    @contextmanager
    def _write_session(self):
        """写事务 context manager：加锁 → 建 session → 提交/回滚 → 关闭 → 清缓存。

        用于 register_agent / create_task / transition_task 等写操作。
        自动处理 lock → new_session → commit → close → cache_invalidation。
        """
        with self._lock:
            session = new_session()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
            # 写入后清除所有 TTL 缓存，确保下次读取拿到最新数据
            self._cache.clear()

    @contextmanager
    def _read_session(self):
        """只读 session context manager：建 session → 查询 → 关闭。

        用于 agents / tasks / events / artifacts 等只读查询。
        不加锁，不提交事务。
        """
        session = new_session()
        try:
            yield session
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------------

    def register_agent(
        self, agent_id: str, hostname: str, ip_addr: str,
        version: str = "0.1.0", os_info: str = "unknown",
        capabilities: list[str] | None = None,
    ) -> AgentModel:
        caps = list(capabilities or [])
        ts = now_utc()

        with self._write_session() as session:
            existing = session.get(AgentModel, agent_id)
            if existing is not None and existing.status == "OFFLINE":
                self._write_audit(
                    session, "AGENT_ONLINE", agent_id,
                    f"{agent_id} 恢复在线",
                )

            if existing is not None:
                existing.hostname = hostname
                existing.ip_addr = ip_addr
                existing.version = version
                existing.os_info = os_info
                existing.capabilities = caps
                existing.status = "ONLINE"
                existing.last_heartbeat_at = ts
                existing.updated_at = ts
                agent = existing
            else:
                agent = AgentModel(
                    id=agent_id, hostname=hostname, ip_addr=ip_addr,
                    version=version, os_info=os_info, capabilities=caps,
                    status="ONLINE", last_heartbeat_at=ts,
                    created_at=ts, updated_at=ts,
                )
                session.add(agent)

            # 保持与 InMemoryRepository 接口一致（SqlRepository.heartbeat 直接查 DB，不使用此队列）
            if ip_addr not in self._task_queues:
                self._task_queues[ip_addr] = deque()

            notify_agent_status(agent_id, "ONLINE", ip_addr)
            return agent

    def heartbeat(self, agent_id: str, ip_addr: str) -> TaskModel | None:
        with self._write_session() as session:
            agent = session.get(AgentModel, agent_id)
            if agent is None:
                return None

            agent.status = "ONLINE"
            agent.last_heartbeat_at = now_utc()
            agent.updated_at = now_utc()

            task = (
                session.query(TaskModel)
                .filter(
                    TaskModel.agent_id == agent_id,
                    TaskModel.status == TaskStatus.PENDING.value,
                )
                .order_by(TaskModel.created_at.asc())
                .first()
            )
            if task is None:
                return None

            self._transition_task_in_session(
                session, task.id, TaskStatus.RUNNING,
                "Agent 心跳拉取待执行任务", Actor.SERVER,
            )
            task.status = TaskStatus.RUNNING.value
            return task

    def heartbeat_only(self, agent_id: str, ip_addr: str) -> None:
        """Update heartbeat timestamp without dispatching a new task."""
        with self._write_session() as session:
            agent = session.get(AgentModel, agent_id)
            if agent is None:
                return
            agent.ip_addr = ip_addr or agent.ip_addr
            agent.status = "ONLINE"
            agent.last_heartbeat_at = now_utc()
            agent.updated_at = now_utc()

    def mark_offline_agents(self, timeout_sec: int = 30) -> list[AgentModel]:
        with self._write_session() as session:
            cutoff = now_utc() - timedelta(seconds=timeout_sec)
            changed = (
                session.query(AgentModel)
                .filter(
                    AgentModel.status == "ONLINE",
                    AgentModel.last_heartbeat_at < cutoff,
                )
                .all()
            )
            for agent in changed:
                agent.status = "OFFLINE"
                agent.updated_at = now_utc()
                self._write_audit(
                    session, "AGENT_OFFLINE", agent.id,
                    f"{agent.id} 心跳超时 {timeout_sec}s，标记为离线",
                )
                notify_agent_status(agent.id, "OFFLINE", agent.ip_addr)
            return changed

    @property
    def agents(self) -> dict[str, AgentModel]:
        """返回 {agent_id: AgentModel} 字典（兼容旧接口的 dict 访问）。

        2 秒 TTL 缓存，避免高频场景下每请求查全表。
        """
        return self._cached("agents", 2.0, lambda: self._query_all_agents())

    def _query_all_agents(self) -> dict[str, AgentModel]:
        s = new_session()
        try:
            return {a.id: a for a in s.query(AgentModel).all()}
        finally:
            s.close()

    def find_agent_by_ip(self, ip_addr: str) -> AgentModel | None:
        with self._read_session() as session:
            return session.query(AgentModel).filter(AgentModel.ip_addr == ip_addr).first()

    def record_agent_metrics(self, agent_id: str, metrics: dict[str, Any]) -> None:
        with self._lock:
            self.agent_metrics[agent_id] = {
                **self.agent_metrics.get(agent_id, {}),
                **dict(metrics),
            }

    def persist_agent_metric_snapshots(self) -> int:
        """将内存中的 agent metrics 批量写入数据库快照表。

        每次调用对所有在线 agent 生成一条快照记录，用于趋势分析。
        返回写入的快照数量。
        """
        with self._write_session() as session:
            ts = now_utc()
            count = 0
            for agent_id, metrics in self.agent_metrics.items():
                self_data = metrics.get("self", {})
                session.add(AgentMetricSnapshotModel(
                    agent_id=agent_id,
                    cpu_percent=int(self_data.get("cpu_percent", 0) or 0),
                    rss_mb=int(self_data.get("rss_mb", 0) or 0),
                    read_kb_s=int(self_data.get("read_kb_s", 0) or 0),
                    write_kb_s=int(self_data.get("write_kb_s", 0) or 0),
                    children_count=int(self_data.get("children_count", 0) or 0),
                    created_at=ts,
                ))
                count += 1
            return count

    def get_agent_metric_history(self, agent_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """查询指定 Agent 的历史指标快照。"""
        with self._read_session() as session:
            rows = (
                session.query(AgentMetricSnapshotModel)
                .filter(AgentMetricSnapshotModel.agent_id == agent_id)
                .order_by(AgentMetricSnapshotModel.created_at.desc())
                .limit(limit)
                .all()
            )
            return [row.to_dict() for row in rows]

    # ------------------------------------------------------------------
    # Task
    # ------------------------------------------------------------------

    def delete_task(self, task_id: str) -> bool:
        """删除任务及其关联数据（事件、产物、诊断结果）。返回是否实际删除。"""
        with self._write_session() as session:
            task = session.get(TaskModel, task_id)
            if task is None:
                return False
            # 级联删除关联数据
            session.query(StatusEventModel).filter(
                StatusEventModel.task_id == task_id
            ).delete()
            session.query(ArtifactModel).filter(
                ArtifactModel.task_id == task_id
            ).delete()
            session.query(DiagnosisRunModel).filter(
                DiagnosisRunModel.task_id == task_id
            ).delete()
            session.delete(task)
            # 插入审计日志
            self._write_audit(
                session,
                event_type="task_deleted",
                task_id=task_id,
                message=f"任务 {task.name or task_id} 已删除",
            )
            # 清除缓存，下次读取时重新查询
            self._cache.pop("tasks", None)
            self._cache.pop("events", None)
            return True

    def create_task(self, payload: CreateTaskRequest) -> TaskModel:
        with self._write_session() as session:
            ts = now_utc()
            hex_suffix = uuid4().hex[:6]
            task_id = f"task_{ts.strftime('%Y%m%d_%H%M%S')}_{hex_suffix}"
            agent = session.get(AgentModel, payload.agent_id)
            if agent is None:
                raise ValueError(f"Agent {payload.agent_id} 不存在")

            task = TaskModel(
                id=task_id,
                name=payload.name,
                agent_id=payload.agent_id,
                target_pid=payload.target_pid,
                collector_type=payload.collector_type,
                sample_rate=payload.sample_rate,
                duration_sec=payload.duration_sec,
                status=TaskStatus.PENDING.value,
                status_reason="Web 请求创建任务",
                request_params=payload.model_dump(),
                created_at=ts,
            )
            session.add(task)
            session.flush()

            # 状态事件
            self._write_event(session, task_id, None, TaskStatus.PENDING,
                              "Web 请求创建任务", Actor.WEB, payload.model_dump())
            record_task_transition("NONE", TaskStatus.PENDING.value)

            # 审计日志
            self._write_audit(session, "TASK_CREATED", task_id=task_id,
                              message=f"任务 {task_id} 已创建",
                              metadata=payload.model_dump())

            return task

    def transition_task(
        self, task_id: str, to_status: TaskStatus,
        reason: str, actor: Actor,
        metadata: dict[str, Any] | None = None,
    ) -> TaskModel:
        with self._write_session() as session:
            task = session.get(TaskModel, task_id)
            if task is None:
                raise ValueError(f"任务 {task_id} 不存在")

            _ = build_status_event(
                task_id, TaskStatus(task.status), to_status,
                reason, actor, metadata or {},
            )

            self._transition_task_in_session(
                session, task_id, to_status, reason, actor, metadata,
            )
            task.status = to_status.value
            return task

    @property
    def tasks(self) -> dict[str, TaskModel]:
        return self._cached("tasks", 2.0, self._query_all_tasks)

    def _query_all_tasks(self) -> dict[str, TaskModel]:
        s = new_session()
        try:
            return {t.id: t for t in s.query(TaskModel).all()}
        finally:
            s.close()

    @property
    def events(self) -> list[StatusEvent]:
        """返回所有状态事件，兼容原有 list[StatusEvent] 接口。"""
        return self._cached("events", 2.0, self._query_all_events)

    def _query_all_events(self) -> list[StatusEvent]:
        s = new_session()
        try:
            models = s.query(StatusEventModel).all()
            result: list[StatusEvent] = []
            for m in models:
                result.append(StatusEvent(
                    task_id=m.task_id if m.task_id else "",
                    from_status=TaskStatus(m.from_status) if m.from_status else None,
                    to_status=TaskStatus(m.to_status),
                    reason=m.reason if m.reason else "",
                    actor=Actor(m.actor) if m.actor else Actor.SERVER,
                    metadata=m.meta_json if isinstance(m.meta_json, dict) else {},
                    created_at=m.created_at if m.created_at else now_utc(),
                ))
            return result
        finally:
            s.close()

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def add_artifacts(self, task_id: str, artifacts: list[dict[str, Any]]) -> None:
        with self._write_session() as session:
            ts = now_utc()
            for art in artifacts:
                session.add(ArtifactModel(
                    task_id=task_id,
                    artifact_type=art.get("artifact_type", "raw"),
                    bucket=art.get("bucket", "mini-drop"),
                    object_key=art.get("object_key", ""),
                    filename=art.get("filename"),
                    local_path=art.get("local_path"),
                    content_type=art.get("content_type", "application/octet-stream"),
                    size_bytes=art.get("size_bytes", 0),
                    meta_json=art.get("metadata", {}),
                    created_at=ts,
                ))

    @property
    def artifacts(self) -> dict[str, list[dict[str, Any]]]:
        return self._cached("artifacts", 2.0, self._query_all_artifacts)

    def _query_all_artifacts(self) -> dict[str, list[dict[str, Any]]]:
        s = new_session()
        try:
            result: dict[str, list[dict[str, Any]]] = {}
            for art in s.query(ArtifactModel).all():
                tid = art.task_id if art.task_id else ""
                result.setdefault(tid, []).append(art.to_dict())
            return result
        finally:
            s.close()

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _write_audit(
        self, session: OrmSession, event_type: str, agent_id: str | None = None,
        task_id: str | None = None, message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session.add(AuditLogModel(
            event_type=event_type,
            message=message,
            agent_id=agent_id,
            task_id=task_id,
            meta_json=metadata or {},
            created_at=now_utc(),
        ))

    @property
    def audit_logs(self) -> list[AuditLogModel]:
        return self._cached("audit_logs", 5.0, self._query_all_audit_logs)

    def _query_all_audit_logs(self) -> list[AuditLogModel]:
        s = new_session()
        try:
            return s.query(AuditLogModel).all()
        finally:
            s.close()

    # ------------------------------------------------------------------
    # RCA
    # ------------------------------------------------------------------

    def create_diagnosis_run(self, task_id: str, model_name: str) -> str:
        with self._write_session() as session:
            ts = now_utc()
            diagnosis_id = f"diag_{ts.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
            session.add(DiagnosisRunModel(
                id=diagnosis_id,
                task_id=task_id,
                status="RUNNING",
                model_name=model_name,
                created_at=ts,
            ))
            return diagnosis_id

    def finish_diagnosis_run(
        self, diagnosis_id: str, status: str, summary: str,
        validated: bool, retry_count: int,
    ) -> None:
        with self._write_session() as session:
            run = session.get(DiagnosisRunModel, diagnosis_id)
            if run is None:
                raise ValueError(f"诊断 {diagnosis_id} 不存在")
            run.status = status
            run.summary = summary
            run.validated = 1 if validated else 0
            run.retry_count = retry_count
            run.finished_at = now_utc()

    def add_diagnosis_tool_result(
        self, diagnosis_id: str, tool_name: str, status: str,
        evidence_ref: str, input_json: dict[str, Any],
        output_json: dict[str, Any], error_message: str | None = None,
    ) -> None:
        with self._write_session() as session:
            session.add(DiagnosisToolResultModel(
                diagnosis_id=diagnosis_id,
                tool_name=tool_name,
                status=status,
                evidence_ref=evidence_ref,
                input_json=_json_safe(input_json),
                output_json=_json_safe(output_json),
                error_message=error_message,
                created_at=now_utc(),
            ))

    def add_diagnosis_report(
        self, diagnosis_id: str, report_json: dict[str, Any],
        ranked_causes: list[dict[str, Any]], confidence: float,
        not_enough_evidence: bool,
    ) -> str:
        with self._write_session() as session:
            report_id = f"report_{uuid4().hex[:10]}"
            session.add(DiagnosisReportModel(
                id=report_id,
                diagnosis_id=diagnosis_id,
                report_json=_json_safe(report_json),
                ranked_causes_json=_json_safe(ranked_causes),
                confidence=int(max(0.0, min(confidence, 1.0)) * 1000),
                not_enough_evidence=1 if not_enough_evidence else 0,
                created_at=now_utc(),
            ))
            return report_id

    def add_repair_plan(
        self, diagnosis_id: str, plan_id: str, cause_id: str,
        risk_level: str, actions: list[dict[str, Any]],
        executed_actions: list[dict[str, Any]],
        requires_user_confirm: bool, status: str,
    ) -> None:
        with self._write_session() as session:
            session.add(RepairPlanModel(
                id=plan_id,
                diagnosis_id=diagnosis_id,
                cause_id=cause_id,
                risk_level=risk_level,
                actions_json=_json_safe(actions),
                executed_actions_json=_json_safe(executed_actions),
                requires_user_confirm=1 if requires_user_confirm else 0,
                status=status,
                created_at=now_utc(),
            ))

    def get_diagnosis(self, diagnosis_id: str) -> dict[str, Any] | None:
        with self._read_session() as session:
            run = session.get(DiagnosisRunModel, diagnosis_id)
            if run is None:
                return None
            report = (
                session.query(DiagnosisReportModel)
                .filter(DiagnosisReportModel.diagnosis_id == diagnosis_id)
                .order_by(DiagnosisReportModel.created_at.desc())
                .first()
            )
            plan = (
                session.query(RepairPlanModel)
                .filter(RepairPlanModel.diagnosis_id == diagnosis_id)
                .order_by(RepairPlanModel.created_at.desc())
                .first()
            )
            tools = (
                session.query(DiagnosisToolResultModel)
                .filter(DiagnosisToolResultModel.diagnosis_id == diagnosis_id)
                .order_by(DiagnosisToolResultModel.id.asc())
                .all()
            )
            return {
                "run": run.to_dict(),
                "report": report.to_dict() if report else None,
                "repair_plan": plan.to_dict() if plan else None,
                "tool_results": [tool.to_dict() for tool in tools],
            }

    def list_diagnoses_for_task(self, task_id: str) -> list[dict[str, Any]]:
        with self._read_session() as session:
            runs = (
                session.query(DiagnosisRunModel)
                .filter(DiagnosisRunModel.task_id == task_id)
                .order_by(DiagnosisRunModel.created_at.desc())
                .all()
            )
            return [run.to_dict() for run in runs]

    def get_feedback_priors(self) -> dict[str, FeedbackPrior]:
        with self._read_session() as session:
            priors: dict[str, FeedbackPrior] = {}
            for row in session.query(RCAFeedbackWeightModel).all():
                priors[row.candidate_id] = FeedbackPrior(
                    candidate_id=row.candidate_id,
                    positive_count=row.positive_count or 0,
                    negative_count=row.negative_count or 0,
                    weight_delta=(row.weight_delta or 0) / 1000,
                )
            return priors

    def record_rca_feedback(
        self, diagnosis_id: str, task_id: str, predicted_cause_id: str,
        feedback_label: str, corrected_cause_id: str | None = None,
        feedback_note: str | None = None,
    ) -> None:
        with self._write_session() as session:
            ts = now_utc()
            session.add(RCAFeedbackModel(
                diagnosis_id=diagnosis_id,
                task_id=task_id,
                predicted_cause_id=predicted_cause_id,
                feedback_label=feedback_label,
                corrected_cause_id=corrected_cause_id,
                feedback_note=feedback_note,
                created_at=ts,
            ))

            candidate_id = corrected_cause_id if feedback_label == "wrong" and corrected_cause_id else predicted_cause_id
            weight = session.get(RCAFeedbackWeightModel, candidate_id)
            if weight is None:
                weight = RCAFeedbackWeightModel(
                    candidate_id=candidate_id,
                    positive_count=0,
                    negative_count=0,
                    partial_count=0,
                    weight_delta=0,
                    updated_at=ts,
                )
                session.add(weight)

            if feedback_label == "correct":
                weight.positive_count += 1
            elif feedback_label == "partial":
                weight.partial_count += 1
            elif feedback_label == "wrong":
                weight.negative_count += 1

            weight.weight_delta = _feedback_delta(
                weight.positive_count, weight.partial_count, weight.negative_count,
            )
            weight.updated_at = ts

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _write_event(
        self, session: OrmSession, task_id: str,
        from_status, to_status: TaskStatus,
        reason: str, actor: Actor,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session.add(StatusEventModel(
            task_id=task_id,
            from_status=from_status.value if from_status else None,
            to_status=to_status.value,
            reason=reason,
            actor=actor.value,
            meta_json=metadata or {},
            created_at=now_utc(),
        ))

    def _transition_task_in_session(
        self, session: OrmSession, task_id: str,
        to_status: TaskStatus, reason: str, actor: Actor,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        task = session.get(TaskModel, task_id)
        # 事件：from 用旧 status value
        from_status = task.status
        session.add(StatusEventModel(
            task_id=task_id,
            from_status=from_status,
            to_status=to_status.value,
            reason=reason,
            actor=actor.value,
            meta_json=metadata or {},
            created_at=now_utc(),
        ))
        record_task_transition(from_status, to_status.value)
        task.status = to_status.value
        task.status_reason = reason
        if to_status == TaskStatus.RUNNING and task.started_at is None:
            task.started_at = now_utc()
        if to_status in (TaskStatus.DONE, TaskStatus.FAILED):
            task.finished_at = now_utc()

        # 发布 SSE 事件
        notify_task_changed(task_id, from_status, to_status.value, reason)

    def as_dict(self, value: Any) -> dict[str, Any]:
        if isinstance(value, StatusEvent):
            data = asdict(value)
            data["from_status"] = value.from_status.value if value.from_status else None
            data["to_status"] = value.to_status.value
            data["actor"] = value.actor.value
            return data
        if isinstance(value, (
            AgentModel, TaskModel, StatusEventModel, AuditLogModel, ArtifactModel,
            DiagnosisRunModel, DiagnosisToolResultModel, DiagnosisReportModel,
            RepairPlanModel,
        )):
            return value.to_dict()
        return json.loads(json.dumps(value, default=str))


def _feedback_delta(positive: int, partial: int, negative: int) -> int:
    raw = positive * 60 + partial * 25 - negative * 80
    return max(-200, min(200, raw))


def _json_safe(value: Any):
    return json.loads(json.dumps(value, default=str))
