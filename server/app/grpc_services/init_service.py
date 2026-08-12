"""InitAgent gRPC 服务：Agent 注册与配置拉取。"""

import os
import json
from typing import Any

from server.app.generated import init_pb2, init_pb2_grpc


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(int(default))).strip().lower() in {"1", "true", "yes", "on"}


class InitAgentService(init_pb2_grpc.InitAgentServicer):
    """Agent 启动时调用的初始化服务。"""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    def RegisterAgent(self, request: init_pb2.RegisterAgentRequest, context) -> init_pb2.RegisterAgentResponse:
        agent = self._repo.register_agent(
            agent_id=request.agent_id,
            hostname=request.hostname,
            ip_addr=request.ip_addr,
            version=request.version,
            os_info=request.os_info,
            capabilities=list(request.capabilities),
        )
        _ = agent  # register_agent 副作用已完成（包括 AGENT_ONLINE 审计日志）
        if request.collector_profile_json and hasattr(self._repo, "record_agent_metrics"):
            self._repo.record_agent_metrics(
                request.agent_id,
                {"collector_profile": _safe_json(request.collector_profile_json)},
            )
        return init_pb2.RegisterAgentResponse(heartbeat_interval_sec=5)

    def FetchConfig(self, request: init_pb2.FetchConfigRequest, context) -> init_pb2.FetchConfigResponse:
        # 默认只下发 Worker 可访问的 MinIO 地址和 bucket，凭据由 Worker 环境注入。
        # 仅当管理员显式允许且 gRPC 已启用 TLS 时才下发凭据，避免明文泄露。
        distribute_credentials = (
            _env_bool("MINI_DROP_GRPC_DISTRIBUTE_MINIO_CREDENTIALS")
            and _env_bool("MINI_DROP_GRPC_SECURE")
        )
        return init_pb2.FetchConfigResponse(
            cos_config=init_pb2.common__pb2.CosConfig(
                endpoint=os.getenv("MINIO_PUBLIC_ENDPOINT", os.getenv("MINIO_ENDPOINT", "minio:9000")),
                access_key=os.getenv("MINIO_ACCESS_KEY", "") if distribute_credentials else "",
                secret_key=os.getenv("MINIO_SECRET_KEY", "") if distribute_credentials else "",
                bucket=os.getenv("MINIO_BUCKET", "mini-drop"),
                region=os.getenv("MINIO_REGION", ""),
            )
        )


def _safe_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"schema_version": "1.0", "error": "invalid collector_profile_json"}
    return parsed if isinstance(parsed, dict) else {"schema_version": "1.0", "error": "collector_profile_json is not an object"}
