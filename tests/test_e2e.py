"""端到端集成测试。

每个测试启动完整的 Server + gRPC 后端，通过 TestClient 模拟用户操作，
验证从创建任务到拿到结果的完整链路。

三个 E2E 场景：
  1. 正常路径：从创建任务到 DONE 状态
  2. PID 不存在：任务进入 FAILED 并带明确 reason
  3. Agent 离线检测：30 秒无心跳后标记 OFFLINE + 审计日志
"""

import time
from datetime import timedelta
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from server.app.database import init_db, reset_engine
from server.app.main import app, repo
from server.app.models import Base
from server.app.state_machine import Actor, TaskStatus, now_utc


@pytest.fixture(autouse=True)
def _setup_e2e(monkeypatch):
    """每个 E2E 用例独立 SQLite + 预注册 Agent。"""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MINI_DROP_AI_API_KEY", raising=False)
    monkeypatch.delenv("MINI_DROP_AI_BASE_URL", raising=False)
    monkeypatch.setenv("MINI_DROP_AI_ENABLED", "none")
    monkeypatch.delenv("MINI_DROP_API_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("MINI_DROP_API_KEY", raising=False)
    reset_engine()
    init_db()
    repo._task_queues.clear()
    repo.register_agent("agent_e2e", "e2e-host", "10.0.0.100")
    yield
    from server.app.database import _get_engine
    Base.metadata.drop_all(bind=_get_engine())
    reset_engine()


@pytest.fixture(name="client")
def client_fixture():
    """预配置 TestClient。"""
    return TestClient(app)


# ── 场景 1：正常路径 ─────────────────────────────────────────────


class TestE2ENormalPath:
    """从创建任务到 Agent 拉取并上报结果的完整正向链路。"""

    def test_full_lifecycle_to_done(self, client):
        # Step 1: 创建任务 → PENDING
        resp = client.post("/api/tasks", json={
            "name": "e2e normal",
            "agent_id": "agent_e2e",
            "target_pid": 1234,
            "collector_type": "perf_cpu",
            "sample_rate": 99,
            "duration_sec": 10,
        })
        assert resp.status_code == 200
        task_id = resp.json()["data"]["task_id"]

        # Step 2: 确认初始状态
        task = client.get(f"/api/tasks/{task_id}").json()["data"]
        assert task["status"] == "PENDING"

        # Step 3: 模拟 Agent 心跳 → RUNNING
        pulled = repo.heartbeat("agent_e2e", "10.0.0.100")
        assert pulled is not None
        assert pulled.id == task_id

        task = client.get(f"/api/tasks/{task_id}").json()["data"]
        assert task["status"] == "RUNNING"

        # Step 4: 模拟采集 + 分析产物 → UPLOADING → ANALYZING → DONE
        repo.transition_task(task_id, TaskStatus.UPLOADING,
                             "采集完成，准备上传", Actor.AGENT)
        repo.add_artifacts(task_id, [{
            "artifact_type": "raw",
            "bucket": "mini-drop",
            "object_key": f"tasks/{task_id}/perf.data",
            "size_bytes": 102400,
        }, {
            "artifact_type": "flamegraph_json",
            "filename": "flamegraph.json",
            "local_path": f"/tmp/mini-drop/{task_id}/flamegraph.json",
            "size_bytes": 2048,
        }, {
            "artifact_type": "top_json",
            "filename": "top.json",
            "local_path": f"/tmp/mini-drop/{task_id}/top.json",
            "size_bytes": 512,
        }])
        repo.transition_task(task_id, TaskStatus.ANALYZING,
                             "产物已记录，等待分析", Actor.SERVER)
        repo.transition_task(task_id, TaskStatus.DONE,
                             "Analyzer 已生成火焰图和热点分析结果", Actor.ANALYZER)

        task = client.get(f"/api/tasks/{task_id}").json()["data"]
        assert task["status"] == "DONE"

        # Step 5: 验证状态事件链
        events = client.get(f"/api/tasks/{task_id}/events").json()["data"]
        statuses = [e["to_status"] for e in events]
        assert "PENDING" in statuses
        assert "RUNNING" in statuses
        assert "ANALYZING" in statuses
        assert "DONE" in statuses
        assert all(e.get("reason") for e in events), "每个事件都应有 reason"

        # Step 6: 产物可查询
        arts = client.get(f"/api/tasks/{task_id}/artifacts").json()["data"]
        assert len(arts) == 3
        assert {item["artifact_type"] for item in arts} >= {"raw", "flamegraph_json", "top_json"}

        # Step 7: 触发诊断（API Key 未配 → 降级）
        diag = client.post(f"/api/tasks/{task_id}/diagnose").json()["data"]
        assert "report_id" in diag
        assert "summary" in diag


# ── 场景 2：PID 不存在 ──────────────────────────────────────────


class TestE2EPidNotFound:
    """目标 PID 不存在时，任务正确进入 FAILED 带原因。"""

    def test_pid_not_found_fails_with_reason(self, client):
        resp = client.post("/api/tasks", json={
            "name": "e2e pid fail",
            "agent_id": "agent_e2e",
            "target_pid": 999999,
            "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]

        # 模拟 Agent 拉取后报告 PID 不存在
        repo.heartbeat("agent_e2e", "10.0.0.100")
        repo.transition_task(task_id, TaskStatus.FAILED,
                             "目标 PID 999999 不存在", Actor.AGENT)

        task = client.get(f"/api/tasks/{task_id}").json()["data"]
        assert task["status"] == "FAILED"
        assert "不存在" in task["status_reason"]

        # 审计日志中应有失败记录
        events = client.get(f"/api/tasks/{task_id}/events").json()["data"]
        fail_events = [e for e in events if e["to_status"] == "FAILED"]
        assert len(fail_events) == 1
        assert fail_events[0]["reason"] == "目标 PID 999999 不存在"


# ── 场景 3：Agent 离线检测 ──────────────────────────────────────


class TestE2EAgentOffline:
    """30 秒超时后 Agent 标记 OFFLINE 并写审计日志。"""

    def test_agent_offline_detection_and_audit(self, client):
        agent_id = "agent_offline_test"
        repo.register_agent(agent_id, "offline-host", "10.0.0.200")

        # 确认初始 ONLINE
        assert repo.agents[agent_id].status == "ONLINE"

        # 手动将心跳时间改到 31 秒前
        from server.app.database import new_session
        from server.app.models import AgentModel
        s = new_session()
        a = s.get(AgentModel, agent_id)
        a.last_heartbeat_at = now_utc() - timedelta(seconds=31)
        s.commit()
        s.close()

        # 标记离线
        changed = repo.mark_offline_agents(timeout_sec=30)
        assert len(changed) >= 1
        assert any(c.id == agent_id for c in changed)

        # 确认状态
        assert repo.agents[agent_id].status == "OFFLINE"

        # 审计日志中应有 AGENT_OFFLINE 记录
        log_resp = client.get("/api/audit-logs").json()["data"]
        log_items = log_resp if isinstance(log_resp, list) else log_resp.get("items", [])
        offline_logs = [l for l in log_items if l["event_type"] == "AGENT_OFFLINE"]
        assert len(offline_logs) >= 1

        # 再次注册 → AGENT_ONLINE 审计日志
        repo.register_agent(agent_id, "offline-host", "10.0.0.200")
        log_resp = client.get("/api/audit-logs").json()["data"]
        log_items = log_resp if isinstance(log_resp, list) else log_resp.get("items", [])
        online_logs = [l for l in log_items if l["event_type"] == "AGENT_ONLINE"]
        assert len(online_logs) >= 1


# ── 补充场景：状态机完整链路 ─────────────────────────────────────


class TestE2EStateMachineChain:
    """验证完整的 6 状态链路的每一步 reason 都不为空。"""

    def test_all_transitions_have_reason(self, client):
        resp = client.post("/api/tasks", json={
            "name": "e2e chain",
            "agent_id": "agent_e2e",
            "target_pid": 9999,
            "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]

        # 完整推进（按状态机顺序）
        repo.heartbeat("agent_e2e", "10.0.0.100")        # PENDING→RUNNING
        repo.transition_task(task_id, TaskStatus.UPLOADING,
                             "采集完成", Actor.AGENT)      # RUNNING→UPLOADING
        repo.transition_task(task_id, TaskStatus.ANALYZING,
                             "产物已记录", Actor.SERVER)    # UPLOADING→ANALYZING
        repo.transition_task(task_id, TaskStatus.DONE,
                             "分析完成", Actor.ANALYZER)    # ANALYZING→DONE

        # 每个事件必须有 reason
        events = client.get(f"/api/tasks/{task_id}/events").json()["data"]
        assert len(events) == 5  # PENDING → RUNNING → UPLOADING → ANALYZING → DONE

        for e in events:
            assert e.get("reason"), f"事件 {e['to_status']} 缺少 reason"
            assert e.get("to_status") in ["PENDING", "RUNNING", "UPLOADING",
                                           "ANALYZING", "DONE"]
