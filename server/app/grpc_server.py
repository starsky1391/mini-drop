"""gRPC 服务器启动模块。

在后台线程中运行 gRPC server（端口 50051），
与 FastAPI HTTP server（端口 8191）共存于同一进程。
两者共享同一个 Repository 实例。

TLS 支持：
  设置 MINI_DROP_GRPC_SECURE=1 启用 TLS。
  设置 MINI_DROP_GRPC_CERT_FILE / MINI_DROP_GRPC_KEY_FILE 指定证书路径。
  未设置时使用 insecure 模式（仅适用于开发/演示环境）。
"""

from __future__ import annotations

import os
from concurrent import futures
from typing import Any

import grpc

from server.app.grpc_auth import GrpcAuthInterceptor
from server.app.generated import (
    control_pb2_grpc,
    healthcheck_pb2_grpc,
    hotmethod_pb2_grpc,
    init_pb2_grpc,
    watch_pb2_grpc,
)
from server.app.grpc_services.control_service import ControlService
from server.app.grpc_services.healthcheck_service import HealthCheckService
from server.app.grpc_services.hotmethod_service import HotmethodService
from server.app.grpc_services.init_service import InitAgentService
from server.app.grpc_services.watch_runtime_service import WatchRuntimeService


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(int(default))).strip().lower() in {"1", "true", "yes", "on"}


def _add_port(server: grpc.Server, address: str) -> int:
    """Add a gRPC port, optionally secured with TLS."""
    if _env_bool("MINI_DROP_GRPC_SECURE", default=False):
        cert_file = os.getenv("MINI_DROP_GRPC_CERT_FILE", "").strip()
        key_file = os.getenv("MINI_DROP_GRPC_KEY_FILE", "").strip()
        if not cert_file or not key_file:
            raise RuntimeError(
                "MINI_DROP_GRPC_SECURE=1 requires MINI_DROP_GRPC_CERT_FILE and MINI_DROP_GRPC_KEY_FILE"
            )
        with open(key_file, "rb") as fh:
            private_key = fh.read()
        with open(cert_file, "rb") as fh:
            certificate_chain = fh.read()
        server_credentials = grpc.ssl_server_credentials([(private_key, certificate_chain)])
        bound_port = server.add_secure_port(address, server_credentials)
    else:
        bound_port = server.add_insecure_port(address)
    if bound_port == 0:
        raise RuntimeError(f"failed to bind gRPC address: {address}")
    return bound_port


def _build_server() -> grpc.Server:
    return grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=[GrpcAuthInterceptor()],
    )


def serve(
    repo: Any,
    port: int = 50051,
    watch_runtime: Any | None = None,
    on_task_terminal: Any | None = None,
) -> grpc.Server:
    """创建并启动 gRPC server。

    Returns:
        grpc.Server 实例，调用方负责在进程退出时调用 server.stop()。
    """
    server = _build_server()

    init_pb2_grpc.add_InitAgentServicer_to_server(InitAgentService(repo), server)
    healthcheck_pb2_grpc.add_HealthCheckServicer_to_server(HealthCheckService(repo), server)
    hotmethod_pb2_grpc.add_HotmethodServicer_to_server(
        HotmethodService(repo, on_task_terminal=on_task_terminal),
        server,
    )
    control_pb2_grpc.add_ControlServicer_to_server(ControlService(repo), server)
    if watch_runtime is not None:
        watch_pb2_grpc.add_WatchRuntimeServicer_to_server(
            WatchRuntimeService(watch_runtime),
            server,
        )

    _add_port(server, f"0.0.0.0:{port}")
    server.start()
    return server


def serve_in_background(
    repo: Any,
    port: int = 50051,
    watch_runtime: Any | None = None,
    on_task_terminal: Any | None = None,
) -> grpc.Server:
    """在后台守护线程启动 gRPC server，主线程继续执行 HTTP server。"""

    grpc_server = _build_server()

    init_pb2_grpc.add_InitAgentServicer_to_server(InitAgentService(repo), grpc_server)
    healthcheck_pb2_grpc.add_HealthCheckServicer_to_server(HealthCheckService(repo), grpc_server)
    hotmethod_pb2_grpc.add_HotmethodServicer_to_server(
        HotmethodService(repo, on_task_terminal=on_task_terminal),
        grpc_server,
    )
    control_pb2_grpc.add_ControlServicer_to_server(ControlService(repo), grpc_server)
    if watch_runtime is not None:
        watch_pb2_grpc.add_WatchRuntimeServicer_to_server(
            WatchRuntimeService(watch_runtime),
            grpc_server,
        )

    _add_port(grpc_server, f"0.0.0.0:{port}")
    grpc_server.start()

    return grpc_server
