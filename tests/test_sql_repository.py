"""SQLAlchemy Repository 测试。

使用 SQLite :memory: 后端，验证与 InMemoryRepository 的接口一致性。
"""

from datetime import datetime, timedelta, timezone

import pytest

from server.app.database import init_db, reset_engine
from server.app.schemas import CreateTaskRequest
from server.app.sql_repository import SqlRepository
from server.app.state_machine import Actor, TaskStatus, now_utc


@pytest.fixture(autouse=True)
def _patch_db_url(monkeypatch):
    """测试统一使用 SQLite :memory:。"""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    reset_engine()
    init_db()
    yield
    from server.app.models import Base
    from server.app.database import _get_engine
    Base.metadata.drop_all(bind=_get_engine())
    reset_engine()


@pytest.fixture(name="repo")
def repo_fixture() -> SqlRepository:
    """每次测试用全新的 repo + 空库。"""
    return SqlRepository()


class TestAgentPersistence:
    """Agent 注册与心跳持久化。"""

    AGENT_ID = "agent_pg_01"
    IP = "10.0.1.1"

    def test_register_agent_persists(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "pg-host", self.IP)
        agents = repo.agents
        assert self.AGENT_ID in agents
        assert agents[self.AGENT_ID].status == "ONLINE"

    def test_registered_agent_survives_repo_reload(self, repo: SqlRepository):
        """新 repo 实例能从 DB 读到上一个实例写入的数据。"""
        repo.register_agent(self.AGENT_ID, "pg-host", self.IP)

        # 新 repo 实例（模拟重启）
        repo2 = SqlRepository()
        agents = repo2.agents
        assert self.AGENT_ID in agents
        assert agents[self.AGENT_ID].hostname == "pg-host"

    def test_offline_recovery_writes_audit(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        # 手动标记离线
        from server.app.database import new_session
        from server.app.models import AgentModel
        session = new_session()
        agent = session.get(AgentModel, self.AGENT_ID)
        if agent:
            agent.status = "OFFLINE"
            session.commit()
        session.close()

        # 重新注册
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        logs = repo.audit_logs
        online_logs = [l for l in logs if l.event_type == "AGENT_ONLINE"]
        assert len(online_logs) == 1

    def test_heartbeat_updates_timestamp(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        agent = repo.agents[self.AGENT_ID]
        before = agent.last_heartbeat_at

        import time
        time.sleep(0.01)
        repo.heartbeat(self.AGENT_ID, self.IP)

        agent2 = repo.agents[self.AGENT_ID]
        assert agent2.last_heartbeat_at > before

    def test_mark_offline_after_timeout(self, repo: SqlRepository):
        repo.register_agent("off_agent", "off-host", "10.0.99.1")
        # 将心跳时间改到 31 秒前
        from server.app.database import new_session
        from server.app.models import AgentModel
        session = new_session()
        agent = session.get(AgentModel, "off_agent")
        agent.last_heartbeat_at = now_utc() - timedelta(seconds=31)
        session.commit()
        session.close()

        changed = repo.mark_offline_agents(timeout_sec=30)
        assert len(changed) == 1
        assert changed[0].status == "OFFLINE"

        audit = repo.audit_logs
        offline_logs = [l for l in audit if l.event_type == "AGENT_OFFLINE"]
        assert len(offline_logs) == 1

    def test_find_agent_by_ip(self, repo: SqlRepository):
        repo.register_agent("ip_agent", "ip-host", "10.0.5.5")
        found = repo.find_agent_by_ip("10.0.5.5")
        assert found is not None
        assert found.id == "ip_agent"


class TestTaskPersistence:
    """任务 CRUD 与状态迁移持久化。"""

    AGENT_ID = "agent_task"
    IP = "10.0.2.1"

    def test_create_task_persists(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        task = repo.create_task(CreateTaskRequest(
            name="pg-task", agent_id=self.AGENT_ID,
            target_pid=100, collector_type="perf_cpu",
        ))
        assert task.status == TaskStatus.PENDING.value
        assert task.id

    def test_create_task_flushes_task_before_status_event(self, repo: SqlRepository, monkeypatch):
        repo.register_agent(self.AGENT_ID, "h", self.IP)

        from sqlalchemy.orm import Session

        original_flush = Session.flush
        flushed = False

        def spy_flush(self, *args, **kwargs):
            nonlocal flushed
            flushed = True
            return original_flush(self, *args, **kwargs)

        original_write_event = repo._write_event

        def assert_task_flushed_before_event(*args, **kwargs):
            assert flushed
            return original_write_event(*args, **kwargs)

        monkeypatch.setattr(Session, "flush", spy_flush)
        monkeypatch.setattr(repo, "_write_event", assert_task_flushed_before_event)

        task = repo.create_task(CreateTaskRequest(
            name="pg-fk-order", agent_id=self.AGENT_ID,
            target_pid=101, collector_type="perf_cpu",
        ))

        assert task.status == TaskStatus.PENDING.value

    def test_create_task_requires_registered_agent(self, repo: SqlRepository):
        with pytest.raises(ValueError, match="Agent missing_agent 不存在"):
            repo.create_task(CreateTaskRequest(
                name="missing-agent", agent_id="missing_agent",
                target_pid=100, collector_type="perf_cpu",
            ))

    def test_task_survives_repo_reload(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        task = repo.create_task(CreateTaskRequest(
            name="survive", agent_id=self.AGENT_ID,
            target_pid=200, collector_type="perf_cpu",
        ))
        task_id = task.id

        repo2 = SqlRepository()
        tasks = repo2.tasks
        assert task_id in tasks

    def test_transition_task_persists_event(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        task = repo.create_task(CreateTaskRequest(
            name="event-test", agent_id=self.AGENT_ID,
            target_pid=300, collector_type="perf_cpu",
        ))

        repo.transition_task(task.id, TaskStatus.RUNNING, "心跳拉取", Actor.SERVER)
        events = repo.events
        assert len(events) == 2  # PENDING + RUNNING

    def test_transition_rejects_illegal_status(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        task = repo.create_task(CreateTaskRequest(
            name="bad-transition", agent_id=self.AGENT_ID,
            target_pid=1, collector_type="perf_cpu",
        ))
        with pytest.raises(ValueError, match="非法的状态迁移"):
            repo.transition_task(task.id, TaskStatus.DONE, "直接跳过", Actor.SERVER)

    def test_heartbeat_returns_pending_task(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        task = repo.create_task(CreateTaskRequest(
            name="heartbeat-test", agent_id=self.AGENT_ID,
            target_pid=400, collector_type="perf_cpu",
        ))

        pulled = repo.heartbeat(self.AGENT_ID, self.IP)
        assert pulled is not None
        assert pulled.id == task.id
        assert pulled.status == TaskStatus.RUNNING.value

    def test_pending_task_survives_repo_reload_before_heartbeat(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        task = repo.create_task(CreateTaskRequest(
            name="queued-before-reload", agent_id=self.AGENT_ID,
            target_pid=401, collector_type="perf_cpu",
        ))

        repo2 = SqlRepository()
        pulled = repo2.heartbeat(self.AGENT_ID, self.IP)

        assert pulled is not None
        assert pulled.id == task.id
        assert pulled.status == TaskStatus.RUNNING.value


class TestArtifactPersistence:
    """产物存储持久化。"""

    AGENT_ID = "agent_art"
    IP = "10.0.3.1"

    def test_add_and_query_artifacts(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        task = repo.create_task(CreateTaskRequest(
            name="art-pg", agent_id=self.AGENT_ID,
            target_pid=1, collector_type="perf_cpu",
        ))
        repo.add_artifacts(task.id, [
            {"artifact_type": "raw", "bucket": "mini-drop",
             "object_key": "tasks/x/perf.data"}
        ])
        arts = repo.artifacts.get(task.id, [])
        assert len(arts) == 1
        assert arts[0]["artifact_type"] == "raw"

    def test_artifacts_survive_repo_reload(self, repo: SqlRepository):
        repo.register_agent(self.AGENT_ID, "h", self.IP)
        task = repo.create_task(CreateTaskRequest(
            name="art-survive", agent_id=self.AGENT_ID,
            target_pid=1, collector_type="perf_cpu",
        ))
        repo.add_artifacts(task.id, [{"artifact_type": "raw", "bucket": "m", "object_key": "k"}])

        repo2 = SqlRepository()
        arts = repo2.artifacts.get(task.id, [])
        assert len(arts) == 1


class TestAuditPersistence:
    """审计日志持久化。"""

    def test_audit_logs_survive_repo_reload(self, repo: SqlRepository):
        repo.register_agent("audit_agent", "h", "10.0.4.1")
        repo.create_task(CreateTaskRequest(
            name="audit-task", agent_id="audit_agent",
            target_pid=1, collector_type="perf_cpu",
        ))

        repo2 = SqlRepository()
        logs = repo2.audit_logs
        assert len(logs) >= 1
        assert any(l.event_type == "TASK_CREATED" for l in logs)


class TestWatchIncidentPersistence:
    def test_frozen_watch_analysis_roundtrip_preserves_audit_fields(self, repo: SqlRepository):
        created_at = datetime(2026, 8, 11, 10, 5, tzinfo=timezone.utc)
        repo.persist_watch_incident({
            "incident_id": "inc-frozen-1",
            "watch_id": "watch-1",
            "trigger_event_id": "trigger-1",
            "evidence_cohort_id": "cohort-1",
            "trigger_type": "cpu_shift",
            "window_start": "2026-08-11T10:00:00Z",
            "window_end": "2026-08-11T10:05:00Z",
            "trigger_observed_at": "2026-08-11T10:05:00Z",
            "status": "analyzed",
            "analysis_status": "analyzed",
            "snapshot_id": "snapshot-1",
            "snapshot_refs": [{"evidence_ref": "snapshot:cohort-1:metrics"}],
            "structured_evidence": {
                "collection_mode": "rolling_snapshot",
                "timing_relation": "same_window",
                "evidence_cohort_id": "cohort-1",
            },
            "collector_tasks": [],
            "analysis_session_id": "analysis_inc-frozen-1",
            "analysis_result": {
                "diagnosis_mode": "frozen_evidence",
                "evidence_package_id": "cohort-1",
                "missing_evidence": ["dependency_check"],
                "stop_reason": "evidence_package_exhausted",
                "probe_count": 0,
            },
            "created_at": created_at.isoformat(),
            "episode_status": "ACTIVE",
            "diagnosis_eligible": True,
        })

        persisted = repo.get_watch_incident("inc-frozen-1")

        assert persisted is not None
        assert persisted["analysis_session_id"] == "analysis_inc-frozen-1"
        assert persisted["structured_evidence"]["timing_relation"] == "same_window"
        assert persisted["analysis_result"]["diagnosis_mode"] == "frozen_evidence"
        assert persisted["analysis_result"]["missing_evidence"] == ["dependency_check"]
        assert persisted["analysis_result"]["stop_reason"] == "evidence_package_exhausted"
        assert persisted["analysis_result"]["probe_count"] == 0


class TestRCAPersistence:
    """智能归因结果、工具证据和反馈权重持久化。"""

    def test_diagnosis_roundtrip(self, repo: SqlRepository):
        repo.register_agent("rca_agent", "h", "10.0.6.1")
        task = repo.create_task(CreateTaskRequest(
            name="rca-task", agent_id="rca_agent",
            target_pid=1, collector_type="perf_cpu",
        ))
        diagnosis_id = repo.create_diagnosis_run(task.id, "rule-engine-only")
        repo.add_diagnosis_tool_result(
            diagnosis_id=diagnosis_id,
            tool_name="inspect_task_events",
            status="success",
            evidence_ref="tool_results.inspect_task_events",
            input_json={},
            output_json={"events": [{"created_at": now_utc()}]},
        )
        report_id = repo.add_diagnosis_report(
            diagnosis_id=diagnosis_id,
            report_json={"summary": "ok"},
            ranked_causes=[{"cause_id": "cpu_hotspot_recursive", "confidence": 0.7}],
            confidence=0.7,
            not_enough_evidence=False,
        )
        repo.add_repair_plan(
            diagnosis_id=diagnosis_id,
            plan_id="repair_test",
            cause_id="cpu_hotspot_recursive",
            risk_level="manual_only",
            actions=[{"action_id": "a1"}],
            executed_actions=[],
            requires_user_confirm=True,
            status="planned",
        )
        repo.finish_diagnosis_run(diagnosis_id, "DONE", "ok", True, 0)

        item = repo.get_diagnosis(diagnosis_id)
        assert item is not None
        assert item["run"]["status"] == "DONE"
        assert item["report"]["id"] == report_id
        assert item["tool_results"][0]["tool_name"] == "inspect_task_events"
        assert item["repair_plan"]["id"] == "repair_test"

    def test_feedback_updates_prior(self, repo: SqlRepository):
        repo.register_agent("feedback_agent", "h", "10.0.6.2")
        task = repo.create_task(CreateTaskRequest(
            name="feedback-task", agent_id="feedback_agent",
            target_pid=1, collector_type="perf_cpu",
        ))
        diagnosis_id = repo.create_diagnosis_run(task.id, "rule-engine-only")
        repo.record_rca_feedback(
            diagnosis_id=diagnosis_id,
            task_id=task.id,
            predicted_cause_id="cpu_hotspot_recursive",
            feedback_label="correct",
        )
        priors = repo.get_feedback_priors()
        assert priors["cpu_hotspot_recursive"].positive_count == 1
        assert priors["cpu_hotspot_recursive"].weight_delta > 0
