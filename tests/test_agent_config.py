"""Agent 配置加载单元测试。"""

from dataclasses import FrozenInstanceError

import pytest

from agent.mini_drop_agent.config import load_config
from agent.mini_drop_agent.collector_profile import build_collector_profile
from agent.mini_drop_agent.main import CAPABILITIES, COLLECTORS, _apply_cos_config, _run_collector
from server.app.generated import common_pb2


class TestAgentConfig:
    """配置文件从环境变量读取。"""

    def _clean_env(self, monkeypatch):
        for key in (
            "AGENT_ID",
            "AGENT_GRPC_ADDR",
            "AGENT_IP_ADDR",
            "AGENT_HEARTBEAT_INTERVAL_SEC",
            "MINI_DROP_GRPC_TOKEN",
            "MINI_DROP_API_KEY",
            "AGENT_UPLOAD_ARTIFACTS",
            "MINIO_ENDPOINT",
            "MINIO_ACCESS_KEY",
            "MINIO_SECRET_KEY",
            "MINIO_BUCKET",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_default_config(self, monkeypatch):
        self._clean_env(monkeypatch)
        cfg = load_config()
        assert cfg.agent_id == "agent_local_demo"
        assert cfg.server_grpc_addr == "localhost:50051"
        assert cfg.agent_ip_addr
        assert cfg.heartbeat_interval_sec == 5

    def test_custom_agent_id(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("AGENT_ID", "agent_ubuntu_01")
        cfg = load_config()
        assert cfg.agent_id == "agent_ubuntu_01"

    def test_custom_grpc_addr(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("AGENT_GRPC_ADDR", "192.168.1.100:50051")
        cfg = load_config()
        assert cfg.server_grpc_addr == "192.168.1.100:50051"

    def test_custom_agent_ip(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("AGENT_IP_ADDR", "10.0.0.20")
        cfg = load_config()
        assert cfg.agent_ip_addr == "10.0.0.20"

    def test_custom_heartbeat_interval(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("AGENT_HEARTBEAT_INTERVAL_SEC", "10")
        cfg = load_config()
        assert cfg.heartbeat_interval_sec == 10

    def test_grpc_token_prefers_dedicated_env(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("MINI_DROP_API_KEY", "api-token")
        monkeypatch.setenv("MINI_DROP_GRPC_TOKEN", "grpc-token")
        cfg = load_config()
        assert cfg.grpc_auth_token == "grpc-token"

    def test_grpc_token_falls_back_to_api_key(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("MINI_DROP_API_KEY", "api-token")
        cfg = load_config()
        assert cfg.grpc_auth_token == "api-token"

    def test_minio_upload_config(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("AGENT_UPLOAD_ARTIFACTS", "1")
        monkeypatch.setenv("MINIO_ENDPOINT", "minio.internal:9000")
        monkeypatch.setenv("MINIO_BUCKET", "artifact-bucket")
        cfg = load_config()
        assert cfg.upload_artifacts is True
        assert cfg.minio_endpoint == "minio.internal:9000"
        assert cfg.minio_bucket == "artifact-bucket"

    def test_config_is_frozen(self, monkeypatch):
        self._clean_env(monkeypatch)
        cfg = load_config()
        with pytest.raises(FrozenInstanceError):
            cfg.agent_id = "hacked"

    def test_apply_cos_config_updates_minio_fields(self, monkeypatch):
        self._clean_env(monkeypatch)
        cfg = load_config()
        updated = _apply_cos_config(
            cfg,
            common_pb2.CosConfig(
                endpoint="server-minio:9000",
                access_key="server-ak",
                secret_key="server-sk",
                bucket="server-bucket",
            ),
        )
        assert updated.minio_endpoint == "server-minio:9000"
        assert updated.minio_access_key == "server-ak"
        assert updated.minio_secret_key == "server-sk"
        assert updated.minio_bucket == "server-bucket"

    def test_collector_profile_reports_managed_adapter_defaults(self, monkeypatch, tmp_path):
        log_output = tmp_path / "logs.ndjson"
        log_output.write_text("", encoding="utf-8")
        monkeypatch.setenv("MINI_DROP_LOG_PIPELINE_OUTPUT", str(log_output))
        monkeypatch.setenv("MINI_DROP_BLACKBOX_URL", "http://blackbox-exporter:9115")
        monkeypatch.delenv("MINI_DROP_REDIS_EXPORTER_URL", raising=False)

        profile = build_collector_profile(["log_scan", "dependency_check", "redis_check"])
        collectors = {item["collector_type"]: item for item in profile["collectors"]}

        assert profile["managed"] is True
        assert collectors["log_scan"]["status"] == "available"
        assert collectors["dependency_check"]["status"] in {"available", "degraded"}
        assert collectors["dependency_check"]["default_options"]["blackbox_url"] == "http://blackbox-exporter:9115"
        assert collectors["redis_check"]["status"] == "unavailable"


class TestAgentCollectorDispatch:
    """Agent 任务执行入口。"""

    def test_capabilities_match_registered_collectors(self):
        assert CAPABILITIES == sorted(COLLECTORS.keys())
        assert CAPABILITIES == sorted(COLLECTORS.keys())  # capacity list auto-expands with registered collectors
        assert set(CAPABILITIES) >= {"continuous_perf", "ebpf_io", "go_pprof", "java_async", "memory_smaps", "perf_cpu", "pyspy"}

    def test_unregistered_collector_reports_failure_without_artifact(self):
        ok, reason, artifacts = _run_collector({
            "id": "task_001",
            "collector_type": "nonexistent_collector",
        })
        assert ok is False
        assert "未在此 Agent 构建中注册" in reason
        assert artifacts == []
