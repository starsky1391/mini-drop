"""HTTP API 测试。

通过 FastAPI TestClient 验证各 REST 端点，
测试使用独立 repo 实例避免与 gRPC 测试共享状态。

注意：TestClient 会触发 FastAPI startup 事件尝试启动 gRPC server。
50051 端口被占用时 gRPC 启动失败不影响 HTTP 端点功能，
测试在 setUp 中直接清理 repo 状态。
"""

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from server.app import storage as store
from server.app.database import init_db, reset_engine
from server.app.main import _ensure_minio_bucket_with_retry, app, repo, watch_registry
from server.app.models import Base
from server.app.prometheus_metrics import REGISTRY
from server.app.state_machine import Actor, TaskStatus


@pytest.fixture(autouse=True)
def _reset_repo(monkeypatch):
    """每个测试使用独立 SQLite 内存库，确保用例间无状态交叉。"""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MINI_DROP_AI_API_KEY", raising=False)
    monkeypatch.delenv("MINI_DROP_AI_BASE_URL", raising=False)
    monkeypatch.setenv("MINI_DROP_AI_ENABLED", "none")
    monkeypatch.delenv("MINI_DROP_API_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("MINI_DROP_API_KEY", raising=False)
    REGISTRY.clear()
    reset_engine()
    init_db()
    repo._task_queues.clear()
    repo.agent_metrics.clear()
    watch_registry.clear()
    repo.register_agent("agent_local_demo", "demo-host", "10.0.0.10")
    repo.register_agent("a1", "agent-one", "10.0.0.11")
    repo.register_agent("a2", "agent-two", "10.0.0.12")
    repo.register_agent("a3", "agent-three", "10.0.0.13")
    yield
    from server.app.database import _get_engine
    Base.metadata.drop_all(bind=_get_engine())
    reset_engine()


@pytest.fixture(name="client")
def client_fixture():
    """提供预配置的 TestClient 实例。"""
    return TestClient(app)


class TestHealthz:
    """健康与用户信息端点。"""

    def test_healthz_returns_service_info(self, client: TestClient):
        resp = client.get("/api/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["service"] == "mini-drop-server"

    def test_me_returns_demo_user(self, client: TestClient):
        resp = client.get("/api/me")
        assert resp.status_code == 200
        assert resp.json()["data"]["user_id"] == "demo_user"

    def test_ai_config_never_returns_key(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("MINI_DROP_AI_API_KEY", "secret-must-not-leak")
        monkeypatch.setenv("MINI_DROP_AI_MODEL", "deepseek-v4-flash")
        resp = client.get("/api/ai-config")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["has_api_key"] is True
        assert data["model"] == "deepseek-v4-flash"
        assert "secret-must-not-leak" not in resp.text

    def test_ai_validation_endpoint(self, client: TestClient):
        result = {
            "run_id": "ai_validation_test",
            "status": "PASSED",
            "passed_count": 8,
            "failed_count": 0,
            "total_count": 8,
            "checks": [],
        }
        with mock.patch("server.app.main.run_ai_validation_suite", return_value=result):
            resp = client.post("/api/ai-validation/runs")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "PASSED"


class TestStartupMinio:
    def test_bucket_init_retries_transient_failure(self, monkeypatch):
        calls = {"count": 0}

        def flaky_ensure(bucket: str) -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("not ready")
            assert bucket == "mini-drop"

        monkeypatch.setenv("MINI_DROP_MINIO_READY_RETRIES", "2")
        monkeypatch.setenv("MINI_DROP_MINIO_READY_DELAY_SEC", "0")
        monkeypatch.setattr(store, "ensure_bucket", flaky_ensure)

        _ensure_minio_bucket_with_retry("mini-drop")

        assert calls["count"] == 2

    def test_bucket_init_raises_after_retry_exhausted(self, monkeypatch):
        monkeypatch.setenv("MINI_DROP_MINIO_READY_RETRIES", "2")
        monkeypatch.setenv("MINI_DROP_MINIO_READY_DELAY_SEC", "0")
        monkeypatch.setattr(store, "ensure_bucket", lambda bucket: (_ for _ in ()).throw(RuntimeError("down")))

        with pytest.raises(RuntimeError, match="down"):
            _ensure_minio_bucket_with_retry("mini-drop")


class TestApiAuth:
    def test_auth_disabled_by_default(self, client: TestClient):
        resp = client.get("/api/tasks")
        assert resp.status_code == 200

    def test_auth_enabled_rejects_missing_token(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("MINI_DROP_API_AUTH_ENABLED", "1")
        monkeypatch.setenv("MINI_DROP_API_KEY", "secret-token")
        resp = client.get("/api/tasks")
        assert resp.status_code == 401

    def test_auth_enabled_accepts_bearer_token(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("MINI_DROP_API_AUTH_ENABLED", "1")
        monkeypatch.setenv("MINI_DROP_API_KEY", "secret-token")
        resp = client.get("/api/tasks", headers={"Authorization": "Bearer secret-token"})
        assert resp.status_code == 200

    def test_auth_enabled_accepts_x_api_key(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("MINI_DROP_API_AUTH_ENABLED", "1")
        monkeypatch.setenv("MINI_DROP_API_KEY", "secret-token")
        resp = client.get("/api/tasks", headers={"X-API-Key": "secret-token"})
        assert resp.status_code == 200

    def test_healthz_stays_public_when_auth_enabled(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("MINI_DROP_API_AUTH_ENABLED", "1")
        monkeypatch.setenv("MINI_DROP_API_KEY", "secret-token")
        resp = client.get("/api/healthz")
        assert resp.status_code == 200

    def test_metrics_stays_public_when_auth_enabled(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("MINI_DROP_API_AUTH_ENABLED", "1")
        monkeypatch.setenv("MINI_DROP_API_KEY", "secret-token")
        resp = client.get("/api/metrics")
        assert resp.status_code == 200
        assert "mini_drop" in resp.text or resp.text.strip() == ""


class TestAgents:
    def test_list_agents_includes_latest_metrics(self, client: TestClient):
        repo.record_agent_metrics("a1", {
            "self": {"cpu_percent": 1.5, "rss_mb": 32.0, "read_kb_s": 0.1, "write_kb_s": 0.2},
            "children": {"children_count": 2},
            "collector_profile": {
                "schema_version": "1.0",
                "summary": {"available": 2, "degraded": 1, "unavailable": 0},
                "collectors": [],
            },
        })
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        data = resp.json()["data"]
        agents = data if isinstance(data, list) else data.get("items", [])
        agent = next(item for item in agents if item["id"] == "a1")
        assert agent["latest_metrics"]["self"]["cpu_percent"] == 1.5
        assert agent["collector_profile"]["summary"]["degraded"] == 1


class TestWatchSubscriptions:
    def test_agent_process_inventory_refresh_and_query(self, client: TestClient, tmp_path, monkeypatch):
        monkeypatch.setenv("MINI_DROP_ARTIFACT_ROOT", str(tmp_path))
        repo.register_agent(
            "a1",
            "agent-one",
            "10.0.0.11",
            capabilities=["process_inventory"],
        )

        refresh = client.post("/api/v1/agents/a1/process-inventory/refresh")

        assert refresh.status_code == 200
        task_id = refresh.json()["data"]["task_id"]
        task = repo.tasks[task_id]
        assert task.collector_type == "process_inventory"
        assert task.request_params["options"]["source"] == "watch_target_picker"

        inventory_path = tmp_path / "process_inventory.json"
        inventory_path.write_text(
            '{"collected_at":"2026-08-13T00:00:00Z","processes":['
            '{"pid":4242,"comm":"python","cmdline":"python app.py --service order-service",'
            '"user":"app","cpu_percent":3.2,"rss_mb":128.0,'
            '"service_guess":"order-service","instance_guess":"agent-one:4242"},'
            '{"pid":5252,"comm":"redis-server","cmdline":"redis-server *:6379",'
            '"user":"redis","cpu_percent":1.0,"rss_mb":64.0,'
            '"service_guess":"redis","instance_guess":"agent-one:5252"}'
            '],"summary":{"process_count":2}}',
            encoding="utf-8",
        )
        repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
        repo.transition_task(task_id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
        repo.add_artifacts(task_id, [{
            "artifact_type": "process_inventory_json",
            "filename": "process_inventory.json",
            "local_path": str(inventory_path),
            "content_type": "application/json",
            "size_bytes": inventory_path.stat().st_size,
            "metadata": {"process_count": 2},
        }])
        repo.transition_task(task_id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
        repo.transition_task(task_id, TaskStatus.DONE, "done", Actor.ANALYZER)

        query = client.get("/api/v1/agents/a1/processes", params={"query": "order", "limit": 10})

        assert query.status_code == 200
        data = query.json()["data"]
        assert data["inventory_status"] == "ready"
        assert data["total"] == 1
        assert data["items"][0]["pid"] == 4242
        assert data["items"][0]["service_guess"] == "order-service"

    def test_create_watch_and_list_agent_lease(self, client: TestClient):
        resp = client.post("/api/v1/watches", json={
            "name": "order service watch",
            "target": {
                "agent_id": "a1",
                "target_pid": 4242,
                "service_id": "order-service",
                "instance_id": "order-1",
                "endpoint": "/orders",
            },
            "target_config": {
                "redis_target": {
                    "host": "order-redis.local",
                    "port": 6379,
                    "url": "redis://order-redis.local:6379",
                },
            },
            "watch_profile": "low_cost_default",
            "enabled_collectors": ["sys_metrics", "light_stack"],
            "retention_seconds": 120,
        })

        assert resp.status_code == 200
        watch = resp.json()["data"]
        assert watch["watch_id"].startswith("watch_")
        assert watch["status"] == "active"
        assert watch["target_config"]["redis_target"]["host"] == "order-redis.local"

        leases = client.get("/api/v1/agents/a1/watch-leases").json()["data"]["items"]
        assert len(leases) == 1
        assert leases[0]["watch_id"] == watch["watch_id"]
        assert leases[0]["target"]["target_pid"] == 4242
        assert leases[0]["target_config"]["redis_target"]["url"] == "redis://order-redis.local:6379"

    def test_watch_evaluate_creates_triggered_collector_tasks(self, client: TestClient):
        repo.register_agent(
            "a1",
            "agent-one",
            "10.0.0.11",
            capabilities=["sys_metrics", "perf_cpu"],
        )
        watch = client.post("/api/v1/watches", json={
            "name": "cpu shift watch",
            "target": {
                "agent_id": "a1",
                "target_pid": 4242,
                "service_id": "order-service",
            },
        }).json()["data"]

        payload = {
            "baseline_window": {
                "start": "2026-08-11T10:00:00Z",
                "end": "2026-08-11T10:00:30Z",
                "samples": [{"cpu_percent": 20.0}, {"cpu_percent": 20.0}],
            },
            "trigger_window": {
                "start": "2026-08-11T10:05:00Z",
                "end": "2026-08-11T10:05:30Z",
                "samples": [{"cpu_percent": 55.0}, {"cpu_percent": 55.0}],
            },
        }
        resp = client.post(f"/api/v1/watches/{watch['watch_id']}/evaluate", json=payload)

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["trigger"]["trigger_event"]["trigger_type"] == "cpu_shift"
        assert data["incident"]["incident_id"].startswith("inc_")
        assert data["incident"]["snapshot_id"].startswith("snap_")
        assert data["incident"]["status"] == "collecting"
        assert data["watch"]["last_trigger_event_id"] == data["trigger"]["trigger_event"]["trigger_event_id"]
        assert data["trigger"]["evidence_cohort_id"] is not None
        assert [task["probe_id"] for task in data["trigger"]["collector_tasks"]] == ["host_process_metrics"]
        assert data["trigger"]["skipped_probe_ids"] == [
            "process_baseline_window",
            "process_off_cpu_profile",
            "process_trace_endpoint_profile",
            "process_cpu_profile",
        ]

        incidents = client.get(f"/api/v1/watches/{watch['watch_id']}/incidents").json()["data"]["items"]
        assert len(incidents) == 1
        assert incidents[0]["trigger_event_id"] == data["trigger"]["trigger_event"]["trigger_event_id"]
        assert incidents[0]["evidence_cohort_id"] == data["trigger"]["evidence_cohort_id"]

    def test_freeze_only_watch_does_not_auto_create_collector_tasks(self, client: TestClient):
        repo.register_agent(
            "a1",
            "agent-one",
            "10.0.0.11",
            capabilities=["sys_metrics", "perf_cpu"],
        )
        watch = client.post("/api/v1/watches", json={
            "name": "freeze first watch",
            "target": {
                "agent_id": "a1",
                "target_pid": 4242,
            },
            "trigger_action": "freeze_only",
        }).json()["data"]

        resp = client.post(f"/api/v1/watches/{watch['watch_id']}/evaluate", json={
            "baseline_window": {
                "start": "2026-08-11T10:00:00Z",
                "end": "2026-08-11T10:00:30Z",
                "samples": [{"cpu_percent": 20.0}, {"cpu_percent": 20.0}],
            },
            "trigger_window": {
                "start": "2026-08-11T10:05:00Z",
                "end": "2026-08-11T10:05:30Z",
                "samples": [{"cpu_percent": 55.0}, {"cpu_percent": 55.0}],
            },
        })

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["incident"]["status"] == "frozen"
        assert data["trigger"]["collector_tasks"] == []

    def test_watch_incident_can_start_ai_cluster_diagnosis_from_frozen_snapshot(self, client: TestClient):
        repo.register_agent(
            "a1",
            "agent-one",
            "10.0.0.11",
            capabilities=["sys_metrics", "perf_cpu"],
        )
        watch = client.post("/api/v1/watches", json={
            "name": "incident analysis watch",
            "target": {
                "agent_id": "a1",
                "target_pid": 4242,
                "service_id": "order-service",
            },
            "trigger_action": "freeze_only",
        }).json()["data"]
        triggered = client.post(f"/api/v1/watches/{watch['watch_id']}/evaluate", json={
            "baseline_window": {
                "start": "2026-08-11T10:00:00Z",
                "end": "2026-08-11T10:00:30Z",
                "samples": [{"cpu_percent": 20.0}, {"cpu_percent": 20.0}],
            },
            "trigger_window": {
                "start": "2026-08-11T10:05:00Z",
                "end": "2026-08-11T10:05:30Z",
                "samples": [{"cpu_percent": 55.0}, {"cpu_percent": 55.0}],
            },
        }).json()["data"]
        incident_id = triggered["incident"]["incident_id"]

        resp = client.post(f"/api/v1/watch-incidents/{incident_id}/analyze")

        assert resp.status_code == 200
        data = resp.json()["data"]
        diagnosis_id = data["diagnosis_id"]
        incident = data["incident"]
        assert diagnosis_id.startswith("diag_session_")
        assert data["mode"] == "ai_cluster_diagnosis"
        assert incident["analysis_status"] in {"analyzing", "analyzed", "needs_evidence"}
        assert incident["analysis_session_id"] == diagnosis_id
        assert incident["analysis_result"]["mode"] == "ai_cluster_diagnosis"
        assert incident["analysis_result"]["diagnosis_id"] == diagnosis_id
        assert incident["analysis_result"]["incident_id"] == incident_id
        assert incident["analysis_result"]["evidence_cohort_id"] == triggered["trigger"]["evidence_cohort_id"]
        assert incident["analysis_result"]["timing_relation"] == "same_window"

        session = client.get(f"/api/v1/diagnoses/{diagnosis_id}")
        assert session.status_code == 200
        assert session.json()["data"]["diagnosis_id"] == diagnosis_id

    def test_watch_incident_diagnosis_creation_failure_is_terminal_and_retryable(
        self,
        client: TestClient,
        monkeypatch,
    ):
        repo.register_agent(
            "a1",
            "agent-one",
            "10.0.0.11",
            capabilities=["sys_metrics", "perf_cpu"],
        )
        watch = client.post("/api/v1/watches", json={
            "name": "analysis failure watch",
            "target": {
                "agent_id": "a1",
                "target_pid": 4242,
                "service_id": "order-service",
            },
            "trigger_action": "freeze_only",
        }).json()["data"]
        triggered = client.post(f"/api/v1/watches/{watch['watch_id']}/evaluate", json={
            "baseline_window": {
                "start": "2026-08-11T10:00:00Z",
                "end": "2026-08-11T10:00:30Z",
                "samples": [{"cpu_percent": 20.0}, {"cpu_percent": 20.0}],
            },
            "trigger_window": {
                "start": "2026-08-11T10:05:00Z",
                "end": "2026-08-11T10:05:30Z",
                "samples": [{"cpu_percent": 55.0}, {"cpu_percent": 55.0}],
            },
        }).json()["data"]
        incident_id = triggered["incident"]["incident_id"]

        monkeypatch.setattr(
            "server.app.main.diagnosis_orchestrator.create",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
        )
        response = client.post(f"/api/v1/watch-incidents/{incident_id}/analyze")

        assert response.status_code == 200
        data = response.json()["data"]
        incident = data["incident"]
        assert incident["analysis_status"] == "analysis_failed"
        assert incident["status"] == "analysis_failed"
        assert incident["analysis_result"]["mode"] == "ai_cluster_diagnosis"
        assert incident["analysis_result"]["auto_analysis_error"] == "RuntimeError"
        assert incident["analysis_result"]["retryable"] is True
        assert incident["analysis_result"]["preserved_evidence_refs"]


class TestCreateTask:
    """任务创建端点。"""

    def test_create_task_records_pending_status(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "demo cpu profile",
            "agent_id": "agent_local_demo",
            "target_pid": 1234,
            "collector_type": "perf_cpu",
            "sample_rate": 99,
            "duration_sec": 10,
        })
        assert resp.status_code == 200
        body = resp.json()
        task_id = body["data"]["task_id"]
        assert body["data"]["status"] == "PENDING"

        # 通过详情端点确认
        detail = client.get(f"/api/tasks/{task_id}")
        assert detail.json()["data"]["status"] == "PENDING"

    def test_create_task_writes_status_event(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "test", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        events = client.get(f"/api/tasks/{task_id}/events").json()["data"]
        assert len(events) >= 1
        assert events[0]["to_status"] == "PENDING"
        assert events[0]["reason"] == "Web 请求创建任务"

    def test_create_task_writes_audit_log(self, client: TestClient):
        client.post("/api/tasks", json={
            "name": "test", "agent_id": "a2",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        log_data = client.get("/api/audit-logs").json()["data"]
        logs = log_data if isinstance(log_data, list) else log_data.get("items", [])
        assert any(log["event_type"] == "TASK_CREATED" for log in logs)

    def test_rejects_zero_duration(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "bad", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
            "duration_sec": 0,
        })
        assert resp.status_code == 400

    def test_rejects_negative_sample_rate(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "bad", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
            "sample_rate": -1,
        })
        assert resp.status_code == 400

    def test_rejects_too_long_duration(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "bad", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
            "duration_sec": 121,
        })
        assert resp.status_code == 400

    def test_rejects_too_high_sample_rate(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "bad", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
            "sample_rate": 1000,
        })
        assert resp.status_code == 400

    def test_rejects_unknown_agent(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "bad-agent", "agent_id": "missing_agent",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        assert resp.status_code == 404


class TestTaskListAndDetail:
    """任务列表与详情端点。"""

    def test_list_returns_empty_initially(self, client: TestClient):
        resp = client.get("/api/tasks")
        assert resp.json()["data"]["total"] == 0

    def test_list_returns_created_tasks(self, client: TestClient):
        client.post("/api/tasks", json={
            "name": "task1", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        client.post("/api/tasks", json={
            "name": "task2", "agent_id": "a1",
            "target_pid": 2, "collector_type": "ebpf_io",
        })
        resp = client.get("/api/tasks")
        assert resp.json()["data"]["total"] == 2

    def test_detail_returns_full_fields(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "detail-test", "agent_id": "a3",
            "target_pid": 9999, "collector_type": "pyspy",
            "sample_rate": 11, "duration_sec": 5,
        })
        task_id = resp.json()["data"]["task_id"]
        detail = client.get(f"/api/tasks/{task_id}").json()["data"]
        assert detail["name"] == "detail-test"
        assert detail["target_pid"] == 9999
        assert detail["collector_type"] == "pyspy"
        assert detail["sample_rate"] == 11
        assert detail["duration_sec"] == 5

    def test_nonexistent_task_returns_404(self, client: TestClient):
        resp = client.get("/api/tasks/nonexistent")
        assert resp.status_code == 404


class TestTaskEvents:
    """状态迁移事件端点。"""

    def test_events_are_returned_in_order(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "events-test", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]

        # 手动推进两步
        repo.transition_task(task_id, TaskStatus.RUNNING, "heartbeat", Actor.SERVER)
        repo.transition_task(task_id, TaskStatus.UPLOADING, "done collecting", Actor.AGENT)

        events = client.get(f"/api/tasks/{task_id}/events").json()["data"]
        statuses = [e["to_status"] for e in events]
        assert statuses == ["PENDING", "RUNNING", "UPLOADING"]

        metrics = client.get("/api/metrics").text
        assert 'mini_drop_task_transitions_total{from="NONE",to="PENDING"}' in metrics
        assert 'mini_drop_task_transitions_total{from="PENDING",to="RUNNING"}' in metrics
        assert 'mini_drop_task_transitions_total{from="RUNNING",to="UPLOADING"}' in metrics

    def test_events_404_for_nonexistent_task(self, client: TestClient):
        resp = client.get("/api/tasks/does-not-exist/events")
        assert resp.status_code == 404


class TestTaskArtifacts:
    """产物查询端点。"""

    def test_empty_artifacts_for_new_task(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "art-test", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        arts = client.get(f"/api/tasks/{task_id}/artifacts").json()["data"]
        assert arts == []

    def test_artifacts_after_result_report(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "art2", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        repo.add_artifacts(task_id, [{"artifact_type": "raw", "bucket": "mini-drop", "object_key": "tasks/x/perf.data"}])
        arts = client.get(f"/api/tasks/{task_id}/artifacts").json()["data"]
        assert len(arts) == 1
        assert arts[0]["artifact_type"] == "raw"

    def test_artifact_content_reads_local_json(self, client: TestClient, tmp_path, monkeypatch):
        monkeypatch.setenv("MINI_DROP_ARTIFACT_ROOT", str(tmp_path))
        top_path = tmp_path / "top.json"
        top_path.write_text('[{"name":"fib_hotspot","samples":10,"percent":80.0}]', encoding="utf-8")
        resp = client.post("/api/tasks", json={
            "name": "art-content", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        repo.add_artifacts(task_id, [{
            "artifact_type": "top_json",
            "filename": "top.json",
            "local_path": str(top_path),
            "content_type": "application/json",
        }])

        content = client.get(f"/api/tasks/{task_id}/artifacts/top_json/content")
        assert content.status_code == 200
        assert content.json()["data"][0]["name"] == "fib_hotspot"

    def test_artifact_content_rejects_path_outside_root(self, client: TestClient, tmp_path, monkeypatch):
        root = tmp_path / "artifacts"
        outside = tmp_path / "outside.json"
        root.mkdir()
        outside.write_text('{"secret": true}', encoding="utf-8")
        monkeypatch.setenv("MINI_DROP_ARTIFACT_ROOT", str(root))
        resp = client.post("/api/tasks", json={
            "name": "art-content-forbidden", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        repo.add_artifacts(task_id, [{
            "artifact_type": "top_json",
            "filename": "top.json",
            "local_path": str(outside),
            "content_type": "application/json",
        }])

        content = client.get(f"/api/tasks/{task_id}/artifacts/top_json/content")
        assert content.status_code == 403

    def test_artifact_content_reads_minio_object(self, client: TestClient, monkeypatch):
        resp = client.post("/api/tasks", json={
            "name": "art-object", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        repo.add_artifacts(task_id, [{
            "artifact_type": "top_json",
            "bucket": "mini-drop",
            "object_key": f"tasks/{task_id}/top.json",
            "content_type": "application/json",
        }])
        monkeypatch.setattr(store, "read_object_bytes", lambda bucket, key: b'[{"name":"fib","samples":1}]')

        content = client.get(f"/api/tasks/{task_id}/artifacts/top_json/content")

        assert content.status_code == 200
        assert content.json()["data"][0]["name"] == "fib"

    def test_artifact_content_falls_back_to_minio_when_local_path_missing(
        self,
        client: TestClient,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setenv("MINI_DROP_ARTIFACT_ROOT", str(tmp_path))
        resp = client.post("/api/tasks", json={
            "name": "art-object-fallback", "agent_id": "a1",
            "target_pid": 1, "collector_type": "pyspy",
        })
        task_id = resp.json()["data"]["task_id"]
        repo.add_artifacts(task_id, [{
            "artifact_type": "flamegraph_svg",
            "bucket": "mini-drop",
            "object_key": f"tasks/{task_id}/pyspy.svg",
            "local_path": str(tmp_path / task_id / "pyspy.svg"),
            "content_type": "image/svg+xml",
        }])
        monkeypatch.setattr(store, "read_object_bytes", lambda bucket, key: b"<svg></svg>")

        content = client.get(f"/api/tasks/{task_id}/artifacts/flamegraph_svg/content")

        assert content.status_code == 200
        assert content.json()["data"]["text"] == "<svg></svg>"

    def test_artifact_download_streams_minio_through_server(self, client: TestClient, monkeypatch):
        resp = client.post("/api/tasks", json={
            "name": "art-download", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        repo.add_artifacts(task_id, [{
            "artifact_type": "raw",
            "filename": "perf data.bin",
            "bucket": "mini-drop",
            "object_key": f"tasks/{task_id}/perf.data",
            "content_type": "application/octet-stream",
        }])
        monkeypatch.setattr(store, "stream_object", lambda bucket, key: iter([b"part-1", b"part-2"]))

        download = client.get(f"/api/tasks/{task_id}/artifacts/raw/download")

        assert download.status_code == 200
        assert download.content == b"part-1part-2"
        assert "perf%20data.bin" in download.headers["content-disposition"]

    def test_artifact_download_reads_local_file(self, client: TestClient, tmp_path, monkeypatch):
        monkeypatch.setenv("MINI_DROP_ARTIFACT_ROOT", str(tmp_path))
        artifact_path = tmp_path / "report.txt"
        artifact_path.write_bytes(b"local-report")
        resp = client.post("/api/tasks", json={
            "name": "local-download", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        repo.add_artifacts(task_id, [{
            "artifact_type": "raw",
            "filename": "report.txt",
            "local_path": str(artifact_path),
            "content_type": "text/plain",
        }])

        download = client.get(f"/api/tasks/{task_id}/artifacts/raw/download")

        assert download.status_code == 200
        assert download.content == b"local-report"


class TestStoragePresign:
    """对象存储预签名 URL 端点。"""

    def test_presign_returns_url(self, client: TestClient, monkeypatch):
        monkeypatch.setattr(
            store,
            "presigned_get_url",
            lambda bucket, key, expires: "http://minio:9000/mini-drop/artifact.svg",
        )
        resp = client.get("/api/storage/presign", params={
            "bucket": "mini-drop",
            "key": "tasks/demo/flamegraph.svg",
            "expires": 600,
        })
        assert resp.status_code == 200
        assert resp.json()["data"]["url"].startswith("http://minio:9000")

    def test_presign_rejects_empty_key(self, client: TestClient):
        resp = client.get("/api/storage/presign", params={"bucket": "mini-drop"})
        assert resp.status_code == 400

    def test_presign_rejects_unallowed_bucket(self, client: TestClient):
        resp = client.get("/api/storage/presign", params={
            "bucket": "other-bucket",
            "key": "tasks/demo/flamegraph.svg",
        })
        assert resp.status_code == 403

    def test_presign_rejects_path_traversal_key(self, client: TestClient):
        resp = client.get("/api/storage/presign", params={
            "bucket": "mini-drop",
            "key": "tasks/../secret.txt",
        })
        assert resp.status_code == 400

    def test_presign_rejects_key_outside_task_artifacts(self, client: TestClient):
        resp = client.get("/api/storage/presign", params={
            "bucket": "mini-drop",
            "key": "public/demo.svg",
        })
        assert resp.status_code == 403

    def test_presign_rejects_invalid_expires(self, client: TestClient):
        resp = client.get("/api/storage/presign", params={
            "bucket": "mini-drop",
            "key": "tasks/demo/flamegraph.svg",
            "expires": 0,
        })
        assert resp.status_code == 400


class TestDiagnose:
    """诊断触发端点。"""

    def test_done_task_auto_creates_analysis_session_on_task_detail(self, client: TestClient, monkeypatch):
        resp = client.post("/api/tasks", json={
            "name": "auto-analysis", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
        repo.transition_task(task_id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
        repo.transition_task(task_id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
        repo.add_artifacts(task_id, [{
            "artifact_type": "top_json",
            "bucket": "mini-drop",
            "object_key": f"tasks/{task_id}/top.json",
            "content_type": "application/json",
        }])
        monkeypatch.setattr(
            store,
            "read_object_bytes",
            lambda bucket, key: b'[{"name":"fib_hotspot","samples":100,"percent":68.5}]',
        )
        repo.transition_task(task_id, TaskStatus.DONE, "analysis complete", Actor.ANALYZER)

        detail = client.get(f"/api/tasks/{task_id}").json()["data"]
        latest = detail["latest_analysis"]
        assert latest["analysis_session_id"].startswith("diag_")
        assert latest["task_id"] == task_id
        assert latest["analysis_pipeline"] == "evidence_to_attribution"
        assert latest["report"]["structured_evidence"]["collection_mode"] == "manual_single"

        second = client.get(f"/api/tasks/{task_id}/analysis-session").json()["data"]
        history = client.get(f"/api/tasks/{task_id}/diagnoses").json()["data"]
        assert second["analysis_session_id"] == latest["analysis_session_id"]
        assert len(history) == 1

    def test_diagnose_enqueues_report(self, client: TestClient, monkeypatch):
        resp = client.post("/api/tasks", json={
            "name": "diag", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]
        repo.add_artifacts(task_id, [{
            "artifact_type": "top_json",
            "bucket": "mini-drop",
            "object_key": f"tasks/{task_id}/top.json",
            "content_type": "application/json",
        }])
        monkeypatch.setattr(
            store,
            "read_object_bytes",
            lambda bucket, key: b'[{"name":"fib_hotspot","samples":100,"percent":68.5}]',
        )
        diag = client.post(f"/api/tasks/{task_id}/diagnose").json()["data"]
        assert diag["diagnosis_id"].startswith("diag_")
        assert diag["report_id"].startswith("report_")
        assert diag["task_id"] == task_id
        assert "summary" in diag
        assert "ranked_causes" in diag
        assert "model" in diag
        assert diag["analysis_strategy"] == "linear"
        assert diag["analysis_pipeline"] == "evidence_to_attribution"
        assert len(diag["tool_results"]) >= 1
        assert diag["repair_plan"]["plan_id"].startswith("repair_")
        assert "report" in diag
        assert len(diag["ranked_causes"]) > 0
        assert diag["report"]["primary_cause_id"] == diag["ranked_causes"][0]["cause_id"]
        assert diag["report"]["analysis_result"] is not None
        assert "stability_score" in diag["report"]

        detail = client.get(f"/api/diagnoses/{diag['diagnosis_id']}").json()["data"]
        assert detail["run"]["task_id"] == task_id
        assert len(detail["tool_results"]) >= 1
        assert detail["report"]["report"]["primary_cause_id"] == diag["report"]["primary_cause_id"]
        history = client.get(f"/api/tasks/{task_id}/diagnoses").json()["data"]
        assert history[0]["id"] == diag["diagnosis_id"]

        metrics = client.get("/api/metrics").text
        assert 'mini_drop_diagnosis_total{status="' in metrics

        feedback = client.post(
            f"/api/diagnoses/{diag['diagnosis_id']}/feedback",
            json={
                "predicted_cause_id": "insufficient_data",
                "feedback_label": "partial",
                "feedback_note": "需要更多证据",
            },
        )
        assert feedback.status_code == 200
        assert feedback.json()["data"]["feedback_saved"] is True

    def test_diagnose_404_for_nonexistent(self, client: TestClient):
        resp = client.post("/api/tasks/nope/diagnose")
        assert resp.status_code == 404

    def test_diagnose_accepts_legacy_pipeline(self, client: TestClient):
        resp = client.post("/api/tasks", json={
            "name": "legacy-diag", "agent_id": "a1",
            "target_pid": 1, "collector_type": "perf_cpu",
        })
        task_id = resp.json()["data"]["task_id"]

        diag = client.post(
            f"/api/tasks/{task_id}/diagnose",
            params={"analysis_pipeline": "legacy"},
        ).json()["data"]

        assert diag["analysis_pipeline"] == "legacy"

    def test_diagnosis_detail_404_for_nonexistent(self, client: TestClient):
        resp = client.get("/api/diagnoses/diag_missing")
        assert resp.status_code == 404
