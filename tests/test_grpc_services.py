"""gRPC 服务集成测试。

启动真实的 gRPC server（insecure port），通过各服务 stub 调 RPC，
验证服务实现与 Repository 交互的正确性。
"""

import json
import threading
import time
import socket
from collections import namedtuple
from datetime import timedelta
from unittest import mock

import grpc
import pytest

from server.app.generated import (
    control_pb2,
    control_pb2_grpc,
    healthcheck_pb2,
    healthcheck_pb2_grpc,
    hotmethod_pb2,
    hotmethod_pb2_grpc,
    init_pb2,
    init_pb2_grpc,
)
from server.app.grpc_server import serve
from server.app.repository import InMemoryRepository
from server.app.schemas import CreateTaskRequest
from server.app.state_machine import Actor, TaskStatus, now_utc


_ClientCallDetails = namedtuple(
    "_ClientCallDetails",
    ("method", "timeout", "metadata", "credentials", "wait_for_ready", "compression"),
)


def _free_port() -> int:
    """获取一个当前可用端口，避免整仓测试时固定端口冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


TEST_PORT = 50052


class GrpcFixture:
    """每个测试用例独立的 gRPC 环境和 stub 集合。"""

    def __init__(self, metadata=None):
        self.port = _free_port()
        self.repo = InMemoryRepository()
        self.server = serve(self.repo, port=self.port)
        channel = grpc.insecure_channel(f"localhost:{self.port}")
        self.channel = grpc.intercept_channel(channel, _MetadataClientInterceptor(metadata)) if metadata else channel
        self.init_stub = init_pb2_grpc.InitAgentStub(self.channel)
        self.hc_stub = healthcheck_pb2_grpc.HealthCheckStub(self.channel)
        self.hotmethod_stub = hotmethod_pb2_grpc.HotmethodStub(self.channel)
        self.control_stub = control_pb2_grpc.ControlStub(self.channel)

    def close(self):
        self.channel.close()
        self.server.stop(grace=None).wait(timeout=5)


class _MetadataClientInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, metadata):
        self._metadata = tuple(metadata or ())

    def intercept_unary_unary(self, continuation, client_call_details, request):
        details = _ClientCallDetails(
            method=client_call_details.method,
            timeout=client_call_details.timeout,
            metadata=tuple(client_call_details.metadata or ()) + self._metadata,
            credentials=client_call_details.credentials,
            wait_for_ready=client_call_details.wait_for_ready,
            compression=client_call_details.compression,
        )
        return continuation(details, request)


@pytest.fixture(name="grpc_fix")
def grpc_fixture():
    fix = GrpcFixture()
    yield fix
    fix.close()


class TestGrpcAuth:
    def test_auth_disabled_allows_plain_request(self, monkeypatch):
        monkeypatch.delenv("MINI_DROP_GRPC_AUTH_ENABLED", raising=False)
        fix = GrpcFixture()
        try:
            resp = fix.init_stub.FetchConfig(init_pb2.FetchConfigRequest(agent_id="agent_auth_open"))
            assert resp.cos_config.endpoint == "minio:9000"
        finally:
            fix.close()

    def test_auth_enabled_rejects_missing_token(self, monkeypatch):
        monkeypatch.setenv("MINI_DROP_GRPC_AUTH_ENABLED", "1")
        monkeypatch.setenv("MINI_DROP_GRPC_TOKEN", "secret")
        fix = GrpcFixture()
        try:
            with pytest.raises(grpc.RpcError) as exc_info:
                fix.init_stub.FetchConfig(init_pb2.FetchConfigRequest(agent_id="agent_auth_missing"))
            assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED
        finally:
            fix.close()

    def test_auth_enabled_accepts_matching_token(self, monkeypatch):
        monkeypatch.setenv("MINI_DROP_GRPC_AUTH_ENABLED", "1")
        monkeypatch.setenv("MINI_DROP_GRPC_TOKEN", "secret")
        fix = GrpcFixture(metadata=(("x-mini-drop-grpc-token", "secret"),))
        try:
            resp = fix.init_stub.FetchConfig(init_pb2.FetchConfigRequest(agent_id="agent_auth_ok"))
            assert resp.cos_config.endpoint == "minio:9000"
        finally:
            fix.close()


class TestInitAgent:
    """InitAgent 服务：Agent 注册与配置拉取。"""

    AGENT_ID = "test_agent_01"

    def test_register_creates_agent_record(self, grpc_fix: GrpcFixture):
        profile = '{"schema_version":"1.0","summary":{"available":1},"collectors":[]}'
        resp = grpc_fix.init_stub.RegisterAgent(
            init_pb2.RegisterAgentRequest(
                agent_id=self.AGENT_ID,
                hostname="test-host",
                ip_addr="10.0.0.1",
                version="0.1.0",
                os_info="Linux 5.15",
                capabilities=["perf_cpu", "ebpf_io"],
                collector_profile_json=profile,
            )
        )
        assert resp.heartbeat_interval_sec == 5
        agent = grpc_fix.repo.agents[self.AGENT_ID]
        assert agent.status == "ONLINE"
        assert agent.hostname == "test-host"
        assert "perf_cpu" in agent.capabilities
        assert grpc_fix.repo.agent_metrics[self.AGENT_ID]["collector_profile"]["summary"]["available"] == 1

    def test_recovery_from_offline_writes_online_audit(self, grpc_fix: GrpcFixture):
        # 首次注册
        grpc_fix.init_stub.RegisterAgent(
            init_pb2.RegisterAgentRequest(
                agent_id=self.AGENT_ID, hostname="h", ip_addr="10.0.0.1",
            )
        )
        # 标记离线
        grpc_fix.repo.agents[self.AGENT_ID].status = "OFFLINE"
        # 再次注册（恢复上线）
        grpc_fix.init_stub.RegisterAgent(
            init_pb2.RegisterAgentRequest(
                agent_id=self.AGENT_ID, hostname="h", ip_addr="10.0.0.1",
            )
        )
        online_events = [
            log for log in grpc_fix.repo.audit_logs
            if log.event_type == "AGENT_ONLINE" and log.agent_id == self.AGENT_ID
        ]
        assert len(online_events) == 1

    def test_fetch_config_returns_minio_settings(self, grpc_fix: GrpcFixture, monkeypatch):
        monkeypatch.setenv("MINIO_ENDPOINT", "minio.test:9000")
        monkeypatch.setenv("MINIO_BUCKET", "mini-drop-test")
        resp = grpc_fix.init_stub.FetchConfig(
            init_pb2.FetchConfigRequest(agent_id=self.AGENT_ID)
        )
        assert resp.cos_config.endpoint == "minio.test:9000"
        assert resp.cos_config.bucket == "mini-drop-test"

    def test_fetch_config_withholds_credentials_by_default(self, grpc_fix: GrpcFixture, monkeypatch):
        monkeypatch.setenv("MINIO_ACCESS_KEY", "worker-access")
        monkeypatch.setenv("MINIO_SECRET_KEY", "worker-secret")
        monkeypatch.delenv("MINI_DROP_GRPC_DISTRIBUTE_MINIO_CREDENTIALS", raising=False)

        resp = grpc_fix.init_stub.FetchConfig(init_pb2.FetchConfigRequest(agent_id=self.AGENT_ID))

        assert resp.cos_config.access_key == ""
        assert resp.cos_config.secret_key == ""

    def test_fetch_config_distributes_credentials_only_over_tls(self, grpc_fix: GrpcFixture, monkeypatch):
        monkeypatch.setenv("MINIO_ACCESS_KEY", "worker-access")
        monkeypatch.setenv("MINIO_SECRET_KEY", "worker-secret")
        monkeypatch.setenv("MINI_DROP_GRPC_DISTRIBUTE_MINIO_CREDENTIALS", "1")
        monkeypatch.setenv("MINI_DROP_GRPC_SECURE", "1")

        resp = grpc_fix.init_stub.FetchConfig(init_pb2.FetchConfigRequest(agent_id=self.AGENT_ID))

        assert resp.cos_config.access_key == "worker-access"
        assert resp.cos_config.secret_key == "worker-secret"


class TestHealthCheck:
    """HealthCheck 服务：心跳保活 + 任务下发。"""

    AGENT_ID = "test_agent_hc"
    IP = "10.0.0.10"

    def _register(self, fix: GrpcFixture):
        fix.init_stub.RegisterAgent(
            init_pb2.RegisterAgentRequest(
                agent_id=self.AGENT_ID, hostname="hc-host", ip_addr=self.IP,
            )
        )

    def test_heartbeat_no_task_returns_pending_false(self, grpc_fix: GrpcFixture):
        self._register(grpc_fix)
        resp = grpc_fix.hc_stub.Do(
            healthcheck_pb2.HealthCheckRequest(agent_id=self.AGENT_ID, ip_addr=self.IP)
        )
        assert resp.pending is False

    def test_heartbeat_pulls_task_and_transitions_to_running(self, grpc_fix: GrpcFixture):
        self._register(grpc_fix)
        task = grpc_fix.repo.create_task(
            CreateTaskRequest(
                name="hc-test", agent_id=self.AGENT_ID,
                target_pid=1234, collector_type="perf_cpu",
            )
        )
        assert task.status == TaskStatus.PENDING

        resp = grpc_fix.hc_stub.Do(
            healthcheck_pb2.HealthCheckRequest(agent_id=self.AGENT_ID, ip_addr=self.IP)
        )
        assert resp.pending is True
        assert resp.task_desc.task_id == task.id
        assert resp.task_desc.sample_argv.hz == 99

        refreshed = grpc_fix.repo.tasks[task.id]
        assert refreshed.status == TaskStatus.RUNNING

    def test_busy_heartbeat_does_not_pull_task(self, grpc_fix: GrpcFixture):
        self._register(grpc_fix)
        task = grpc_fix.repo.create_task(
            CreateTaskRequest(
                name="busy-test", agent_id=self.AGENT_ID,
                target_pid=1234, collector_type="perf_cpu",
            )
        )

        resp = grpc_fix.hc_stub.Do(
            healthcheck_pb2.HealthCheckRequest(
                agent_id=self.AGENT_ID,
                ip_addr=self.IP,
                busy=True,
            )
        )

        assert resp.pending is False
        assert grpc_fix.repo.tasks[task.id].status == TaskStatus.PENDING

    def test_heartbeat_updates_timestamp(self, grpc_fix: GrpcFixture):
        self._register(grpc_fix)
        before = grpc_fix.repo.agents[self.AGENT_ID].last_heartbeat_at
        time.sleep(0.01)
        grpc_fix.hc_stub.Do(
            healthcheck_pb2.HealthCheckRequest(agent_id=self.AGENT_ID, ip_addr=self.IP)
        )
        after = grpc_fix.repo.agents[self.AGENT_ID].last_heartbeat_at
        assert after > before

    def test_heartbeat_records_agent_metrics(self, grpc_fix: GrpcFixture):
        self._register(grpc_fix)
        grpc_fix.hc_stub.Do(
            healthcheck_pb2.HealthCheckRequest(
                agent_id=self.AGENT_ID,
                ip_addr=self.IP,
                self_pstats=healthcheck_pb2.common__pb2.PidStats(cpu_percent=1.5, rss_mb=32.0),
                children_pstats=healthcheck_pb2.common__pb2.PidStats(children_count=2),
            )
        )
        metrics = grpc_fix.repo.agent_metrics[self.AGENT_ID]
        assert metrics["self"]["cpu_percent"] == 1.5
        assert metrics["self"]["rss_mb"] == 32.0
        assert metrics["children"]["children_count"] == 2


class TestHotmethodNotifyResult:
    """Hotmethod 服务：采集结果上报。"""

    AGENT_ID = "test_agent_result"
    IP = "10.0.0.20"

    def _create_and_start_task(self, fix: GrpcFixture) -> str:
        fix.init_stub.RegisterAgent(
            init_pb2.RegisterAgentRequest(
                agent_id=self.AGENT_ID, hostname="h", ip_addr=self.IP,
            )
        )
        task = fix.repo.create_task(
            CreateTaskRequest(
                name="result-test", agent_id=self.AGENT_ID,
                target_pid=5678, collector_type="perf_cpu",
            )
        )
        fix.repo.transition_task(task.id, TaskStatus.RUNNING, "heartbeat", Actor.SERVER)
        return task.id

    def test_notify_success_transitions_to_analyzing(self, grpc_fix: GrpcFixture):
        task_id = self._create_and_start_task(grpc_fix)
        grpc_fix.hotmethod_stub.NotifyResult(
            hotmethod_pb2.TaskResult(
                task_id=task_id,
                error_message="",
                cos_key="tasks/test/perf.data",
                artifact_type="raw",
                artifact_metadata_json='[{"artifact_type":"raw","bucket":"mini-drop","object_key":"tasks/test/perf.data"}]',
            )
        )
        task = grpc_fix.repo.tasks[task_id]
        assert task.status == TaskStatus.ANALYZING
        assert len(grpc_fix.repo.artifacts.get(task_id, [])) == 1

    def test_notify_raw_perf_runs_server_analyzer_fallback(self, grpc_fix: GrpcFixture, tmp_path, monkeypatch):
        monkeypatch.setenv("MINI_DROP_ARTIFACT_ROOT", str(tmp_path))
        task_id = self._create_and_start_task(grpc_fix)
        perf_path = tmp_path / task_id / "perf.data"
        perf_path.parent.mkdir()
        perf_path.write_text("perf", encoding="utf-8")
        generated = [{"artifact_type": "top_json", "filename": "top.json", "local_path": str(tmp_path / task_id / "top.json")}]

        with mock.patch(
            "server.app.grpc_services.hotmethod_service.analyze_raw_perf_artifacts",
            return_value=generated,
        ):
            grpc_fix.hotmethod_stub.NotifyResult(
                hotmethod_pb2.TaskResult(
                    task_id=task_id,
                    error_message="",
                    artifact_metadata_json=json.dumps([{
                        "artifact_type": "raw",
                        "filename": "perf.data",
                        "local_path": str(perf_path),
                    }]),
                )
            )

        task = grpc_fix.repo.tasks[task_id]
        artifact_types = {item["artifact_type"] for item in grpc_fix.repo.artifacts.get(task_id, [])}
        assert task.status == TaskStatus.DONE
        assert {"raw", "top_json"} <= artifact_types

    def test_notify_sanitizes_artifact_metadata(self, grpc_fix: GrpcFixture):
        task_id = self._create_and_start_task(grpc_fix)
        artifacts = [
            {
                "artifact_type": "raw",
                "filename": f"artifact_{idx}.txt",
                "unexpected": "drop-me",
                "size_bytes": "-1",
                "metadata": {"window_index": idx, "nested": {"drop": True}},
            }
            for idx in range(40)
        ]
        grpc_fix.hotmethod_stub.NotifyResult(
            hotmethod_pb2.TaskResult(
                task_id=task_id,
                error_message="",
                artifact_metadata_json=json.dumps(artifacts),
            )
        )

        stored = grpc_fix.repo.artifacts.get(task_id, [])
        assert len(stored) == 32
        assert "unexpected" not in stored[0]
        assert stored[0]["size_bytes"] == 0
        assert stored[0]["metadata"] == {"window_index": 0}

    def test_notify_analysis_artifacts_transitions_to_done(self, grpc_fix: GrpcFixture):
        task_id = self._create_and_start_task(grpc_fix)
        grpc_fix.hotmethod_stub.NotifyResult(
            hotmethod_pb2.TaskResult(
                task_id=task_id,
                error_message="",
                artifact_metadata_json=(
                    '[{"artifact_type":"raw","filename":"perf.data"},'
                    '{"artifact_type":"flamegraph_json","filename":"flamegraph.json"},'
                    '{"artifact_type":"top_json","filename":"top.json"}]'
                ),
            )
        )
        task = grpc_fix.repo.tasks[task_id]
        assert task.status == TaskStatus.DONE
        events = [event.to_status for event in grpc_fix.repo.events if event.task_id == task_id]
        assert TaskStatus.ANALYZING in events
        assert TaskStatus.DONE in events

    def test_notify_ebpf_metrics_uses_ebpf_done_reason(self, grpc_fix: GrpcFixture):
        task_id = self._create_and_start_task(grpc_fix)
        grpc_fix.hotmethod_stub.NotifyResult(
            hotmethod_pb2.TaskResult(
                task_id=task_id,
                error_message="",
                artifact_metadata_json=(
                    '[{"artifact_type":"ebpf_metrics","filename":"ebpf_metrics.json"},'
                    '{"artifact_type":"ebpf_raw","filename":"io_latency.txt"}]'
                ),
            )
        )
        task = grpc_fix.repo.tasks[task_id]
        assert task.status == TaskStatus.DONE
        assert task.status_reason == "eBPF IO 延迟分布已生成"

    def test_notify_failure_transitions_to_failed(self, grpc_fix: GrpcFixture):
        task_id = self._create_and_start_task(grpc_fix)
        grpc_fix.hotmethod_stub.NotifyResult(
            hotmethod_pb2.TaskResult(
                task_id=task_id,
                error_message="目标 PID 不存在",
            )
        )
        task = grpc_fix.repo.tasks[task_id]
        assert task.status == TaskStatus.FAILED
        assert task.status_reason == "目标 PID 不存在"

    def test_notify_failure_sanitizes_error_message(self, grpc_fix: GrpcFixture):
        task_id = self._create_and_start_task(grpc_fix)
        grpc_fix.hotmethod_stub.NotifyResult(
            hotmethod_pb2.TaskResult(
                task_id=task_id,
                error_message="\x00" + ("x" * 2000),
            )
        )
        task = grpc_fix.repo.tasks[task_id]
        assert "\x00" not in task.status_reason
        assert len(task.status_reason) == 1024


class TestControlService:
    """Control 服务：Web → Server 任务创建与查询。"""

    AGENT_ID = "test_agent_ctl"
    IP = "10.0.0.30"

    def test_create_task_returns_pending(self, grpc_fix: GrpcFixture):
        grpc_fix.init_stub.RegisterAgent(
            init_pb2.RegisterAgentRequest(
                agent_id=self.AGENT_ID, hostname="c-host", ip_addr=self.IP,
            )
        )
        resp = grpc_fix.control_stub.CreateTask(
            control_pb2.CreateTaskRequest(
                target_ip=self.IP,
                task_desc=hotmethod_pb2.TaskDesc(
                    sample_argv=hotmethod_pb2.RecordArgv(hz=99, duration=15, pid=1234),
                ),
            )
        )
        assert resp.status == "PENDING"

        pull_resp = grpc_fix.hc_stub.Do(
            healthcheck_pb2.HealthCheckRequest(agent_id=self.AGENT_ID, ip_addr=self.IP)
        )
        assert pull_resp.pending is True
        assert pull_resp.task_desc.task_id == resp.task_id

    def test_create_task_rejects_unknown_target_ip(self, grpc_fix: GrpcFixture):
        with pytest.raises(grpc.RpcError) as exc_info:
            grpc_fix.control_stub.CreateTask(
                control_pb2.CreateTaskRequest(
                    target_ip="10.0.0.250",
                    task_desc=hotmethod_pb2.TaskDesc(
                        sample_argv=hotmethod_pb2.RecordArgv(hz=99, duration=15, pid=1234),
                    ),
                )
            )
        assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND

    def test_stat_agent_online(self, grpc_fix: GrpcFixture):
        grpc_fix.init_stub.RegisterAgent(
            init_pb2.RegisterAgentRequest(
                agent_id=self.AGENT_ID, hostname="s-host", ip_addr=self.IP,
            )
        )
        resp = grpc_fix.control_stub.StatAgent(
            control_pb2.StatAgentRequest(agent_id=self.AGENT_ID)
        )
        assert resp.agent_status == "ONLINE"

    def test_stat_agent_unknown(self, grpc_fix: GrpcFixture):
        resp = grpc_fix.control_stub.StatAgent(
            control_pb2.StatAgentRequest(agent_id="nonexistent")
        )
        assert resp.agent_status == "UNKNOWN"


class TestOfflineDetection:
    """Agent 离线判定：Repository 层逻辑（不走 gRPC，直接调 repo）。"""

    AGENT_ID = "agent_offline_test"
    IP = "10.0.0.99"

    def test_mark_offline_after_timeout(self, grpc_fix: GrpcFixture):
        grpc_fix.init_stub.RegisterAgent(
            init_pb2.RegisterAgentRequest(
                agent_id=self.AGENT_ID, hostname="off", ip_addr=self.IP,
            )
        )
        agent = grpc_fix.repo.agents[self.AGENT_ID]
        agent.last_heartbeat_at = now_utc() - timedelta(seconds=31)

        grpc_fix.repo.mark_offline_agents(timeout_sec=30)
        assert agent.status == "OFFLINE"

        offline_logs = [
            log for log in grpc_fix.repo.audit_logs
            if log.event_type == "AGENT_OFFLINE" and log.agent_id == self.AGENT_ID
        ]
        assert len(offline_logs) == 1
