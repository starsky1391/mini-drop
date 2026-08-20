"""Mini-Drop Agent：gRPC 客户端，心跳拉取任务并执行采集。

启动流程：
  config ← 环境变量
  → InitAgent.RegisterAgent（注册元数据）
  → loop:
      → HealthCheck.Do（心跳 + 拉取任务）
      → 如有任务 → 执行采集 → Hotmethod.NotifyResult（上报结果）
      → sleep 5s
"""

from __future__ import annotations

import server.app._env  # noqa: F401 — 自动加载 .env

import json
import os
import queue
import signal
import socket
import threading
import time
from dataclasses import replace
from typing import Any

import grpc

from agent.mini_drop_agent.watch_observer import (
    WatchObservationState,
    ProcessDeltaSampler,
    dict_to_sample,
    observe_watch_lease,
    sample_to_dict,
)
from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.baseline import BaselineWindowCollector
from agent.mini_drop_agent.collectors.continuous import ContinuousCollector
from agent.mini_drop_agent.collectors.dependency import DependencyCheckCollector
from agent.mini_drop_agent.collectors.ebpf import EBPFCollector
from agent.mini_drop_agent.collectors.off_cpu import OffCPUCollector
from agent.mini_drop_agent.collectors.trace import TraceEndpointCollector
from agent.mini_drop_agent.collectors.java_async import JavaAsyncProfilerCollector
from agent.mini_drop_agent.collectors.log_scan import LogScanCollector
from agent.mini_drop_agent.collectors.memory import MemoryCollector
from agent.mini_drop_agent.collectors.perf import PerfCollector
from agent.mini_drop_agent.collectors.process_inventory import ProcessInventoryCollector
from agent.mini_drop_agent.collectors.pprof import PprofCollector
from agent.mini_drop_agent.collectors.pyspy import PySpyCollector
from agent.mini_drop_agent.collectors.python_heap import PythonHeapCollector
from agent.mini_drop_agent.collectors.python_heap_reference import PythonHeapReferenceCollector
from agent.mini_drop_agent.collectors.redis_check import RedisCheckCollector
from agent.mini_drop_agent.collectors.runtime_control import RuntimeControlCollector
from agent.mini_drop_agent.collectors.source_snapshot import SourceSnapshotCollector
from agent.mini_drop_agent.collectors.source_mechanism import SourceMechanismCollector
from agent.mini_drop_agent.collectors.sys_metrics import SysMetricsCollector
from agent.mini_drop_agent.runtime_control import RuntimeControlObserver
from agent.mini_drop_agent.artifact_upload import maybe_upload_artifacts
from agent.mini_drop_agent.collector_profile import collector_profile_json
from agent.mini_drop_agent.connection import GrpcConnection
from agent.mini_drop_agent.config import AgentConfig, load_config
from agent.mini_drop_agent.logging_utils import log_event
from agent.mini_drop_agent.metrics import ProcessStatsSampler
from server.app.generated import (
    healthcheck_pb2,
    healthcheck_pb2_grpc,
    hotmethod_pb2,
    hotmethod_pb2_grpc,
    init_pb2,
    init_pb2_grpc,
    watch_pb2,
    watch_pb2_grpc,
)

# ── 采集器注册 ────────────────────────────────────────────────────

COLLECTORS = {
    "perf_cpu": PerfCollector(),
    "ebpf_io": EBPFCollector(),
    "pyspy": PySpyCollector(),
    "python_heap_profile": PythonHeapCollector(),
    "python_heap_reference": PythonHeapReferenceCollector(),
    "source_snapshot": SourceSnapshotCollector(),
    "source_mechanism_query": SourceMechanismCollector(),
    "continuous_perf": ContinuousCollector(),
    "java_async": JavaAsyncProfilerCollector(),
    "go_pprof": PprofCollector(),
    "memory_smaps": MemoryCollector(),
    "sys_metrics": SysMetricsCollector(),
    "off_cpu_wait_profile": OffCPUCollector(),
    "trace_endpoint_profile": TraceEndpointCollector(),
    "baseline_window_profile": BaselineWindowCollector(),
    "log_scan": LogScanCollector(),
    "dependency_check": DependencyCheckCollector(),
    "redis_check": RedisCheckCollector(),
    "process_inventory": ProcessInventoryCollector(),
    "runtime_control_history": RuntimeControlCollector(),
}

CAPABILITIES = sorted(COLLECTORS.keys())


# ── 任务执行 ───────────────────────────────────────────────────────


def _run_collector(task_payload: dict[str, Any], config: AgentConfig | None = None) -> tuple[bool, str, list[dict[str, Any]]]:
    """执行采集任务：构造 CollectorTask 后分发到注册的采集器。

    如果 collector_type 不在 COLLECTORS 中，明确上报失败。
    输入值经过安全裁剪防止资源耗尽。
    """
    collector_type = task_payload.get("collector_type", "perf_cpu")
    collector = COLLECTORS.get(collector_type)
    if collector is None:
        return False, f"collector {collector_type} 未在此 Agent 构建中注册", []

    # 安全裁剪：防止服务器下发恶意参数
    target_pid = task_payload.get("target_pid", 0)
    if not isinstance(target_pid, int) or target_pid <= 0:
        return False, f"无效的 target_pid: {target_pid}", []
    if target_pid == os.getpid():
        return False, "拒绝自剖析请求 (target_pid 与 Agent 自身 PID 相同)", []

    sample_rate = max(1, min(task_payload.get("sample_rate", 99), 10000))
    duration_sec = max(1, min(task_payload.get("duration_sec", 15), 600))

    collector_task = CollectorTask(
        id=task_payload.get("id", ""),
        collector_type=collector_type,
        target_pid=target_pid,
        sample_rate=sample_rate,
        duration_sec=duration_sec,
        options=task_payload.get("request_params", {}).get("options", {}),
    )
    result = collector.collect(collector_task)
    artifacts = result.artifacts
    if result.ok and config is not None:
        try:
            artifacts = maybe_upload_artifacts(task_payload["id"], result.artifacts, config)
        except Exception as exc:
            return False, f"artifact upload failed: {exc}", result.artifacts
    return result.ok, result.reason, artifacts


# ── gRPC 客户端 ───────────────────────────────────────────────────


def _register(stub: init_pb2_grpc.InitAgentStub, config: AgentConfig) -> None:
    """通过 gRPC InitAgent.RegisterAgent 注册自身元数据。"""
    stub.RegisterAgent(
        init_pb2.RegisterAgentRequest(
            agent_id=config.agent_id,
            hostname=socket.gethostname(),
            ip_addr=config.agent_ip_addr,
            version="0.1.0",
            os_info=_os_info(),
            capabilities=CAPABILITIES,
            collector_profile_json=collector_profile_json(CAPABILITIES),
        ),
        timeout=5,
    )


def _fetch_config(stub: init_pb2_grpc.InitAgentStub, config: AgentConfig) -> AgentConfig:
    resp = stub.FetchConfig(init_pb2.FetchConfigRequest(agent_id=config.agent_id), timeout=5)
    return _apply_cos_config(config, resp.cos_config)


def _apply_cos_config(config: AgentConfig, cos_config) -> AgentConfig:
    if not getattr(cos_config, "endpoint", ""):
        return config
    return replace(
        config,
        minio_endpoint=cos_config.endpoint,
        minio_access_key=cos_config.access_key or config.minio_access_key,
        minio_secret_key=cos_config.secret_key or config.minio_secret_key,
        minio_bucket=cos_config.bucket or config.minio_bucket,
    )


def _heartbeat(
    stub: healthcheck_pb2_grpc.HealthCheckStub,
    config: AgentConfig,
    sampler: ProcessStatsSampler | None = None,
    busy: bool = False,
) -> dict[str, Any] | None:
    """通过 gRPC HealthCheck.Do 发送心跳，返回待执行任务或 None。"""
    request = healthcheck_pb2.HealthCheckRequest(
        agent_id=config.agent_id,
        hostname=socket.gethostname(),
        ip_addr=config.agent_ip_addr,
        agent_version="0.1.0",
        busy=busy,
    )
    if sampler is not None:
        _fill_pid_stats(request.self_pstats, sampler.sample_self())
        _fill_pid_stats(request.children_pstats, sampler.sample_children())
    resp = stub.Do(
        request,
        timeout=5,
    )
    if resp.pending and resp.task_desc.task_id:
        task_type = resp.task_desc.task_type
        # task_type 优先路由（如 MemCheck → memory_smaps）
        if task_type in _TASK_TYPE_COLLECTOR:
            collector_type = _TASK_TYPE_COLLECTOR[task_type]
        else:
            collector_type = _profiler_to_collector(resp.task_desc.profiler_type)
        options = {
            "callgraph": resp.task_desc.sample_argv.callgraph,
            "event": resp.task_desc.sample_argv.event,
        }
        try:
            extra = json.loads(resp.task_desc.script_content or "{}")
        except json.JSONDecodeError:
            extra = {}
        if isinstance(extra, dict) and isinstance(extra.get("options"), dict):
            options.update(extra["options"])
        return {
            "id": resp.task_desc.task_id,
            "collector_type": collector_type,
            "target_pid": resp.task_desc.sample_argv.pid,
            "sample_rate": resp.task_desc.sample_argv.hz,
            "duration_sec": resp.task_desc.sample_argv.duration,
            "request_params": {
                "options": options,
            },
        }
    return None


def _collector_worker(work_queue, result_queue, config: AgentConfig) -> None:
    """Run collectors away from the heartbeat loop."""
    while True:
        task = work_queue.get()
        try:
            if task is None:
                return
            ok, reason, artifacts = _run_collector(task, config)
            result_queue.put((task, ok, reason, artifacts))
        except Exception as exc:
            if task is not None:
                result_queue.put((task, False, f"collector worker crashed: {exc}", []))
        finally:
            work_queue.task_done()


def _watch_sync_loop(
    conn: GrpcConnection,
    config: AgentConfig,
    runtime_control_observer: RuntimeControlObserver | None = None,
) -> None:
    """Keep all assigned watch leases alive without blocking task collection."""
    states: dict[str, WatchObservationState] = {}
    samplers: dict[str, ProcessDeltaSampler] = {}
    lease_pids: dict[str, int] = {}
    next_poll: dict[str, float] = {}
    leases: dict[str, watch_pb2.WatchLease] = {}

    while not _should_exit:
        now = time.monotonic()
        observations: list[watch_pb2.WatchObservation] = []
        for watch_id, lease in list(leases.items()):
            interval = max(1, int(lease.poll_interval_seconds or config.watch_sync_interval_sec))
            if now < next_poll.get(watch_id, 0.0):
                continue
            next_poll[watch_id] = now + interval
            if lease_pids.get(watch_id) != lease.target_pid:
                states[watch_id] = WatchObservationState()
                samplers[watch_id] = ProcessDeltaSampler()
                lease_pids[watch_id] = lease.target_pid

            state = states.setdefault(watch_id, WatchObservationState())
            sampler = samplers.setdefault(watch_id, ProcessDeltaSampler())
            sample, target_exists, status = observe_watch_lease(lease, sampler)
            if target_exists:
                state.append(sample_to_dict(sample))
            baseline, trigger = state.split()
            observation = watch_pb2.WatchObservation(
                watch_id=watch_id,
                target_exists=target_exists,
                observation_status=status,
            )
            observation.baseline_samples.extend(dict_to_sample(item) for item in baseline)
            observation.trigger_samples.extend(dict_to_sample(item) for item in trigger)
            if baseline and trigger:
                observations.append(observation)

        try:
            response = conn.call_with_retry(
                lambda: watch_pb2_grpc.WatchRuntimeStub(conn.channel).Sync(
                    watch_pb2.WatchSyncRequest(
                        agent_id=config.agent_id,
                        hostname=socket.gethostname(),
                        ip_addr=config.agent_ip_addr,
                        observations=observations,
                    ),
                    timeout=5,
                )
            )
            returned = {lease.watch_id: lease for lease in response.lease}
            leases = returned
            if runtime_control_observer is not None:
                runtime_control_observer.set_targets(lease.target_pid for lease in returned.values())
            active_ids = set(returned)
            for watch_id in set(states) - active_ids:
                states.pop(watch_id, None)
                samplers.pop(watch_id, None)
                lease_pids.pop(watch_id, None)
                next_poll.pop(watch_id, None)
        except grpc.RpcError as exc:
            log_event("warning", "watch_sync_failed", code=exc.code(), details=exc.details())

        time.sleep(max(1, config.watch_sync_interval_sec))


def _notify_result(
    stub: hotmethod_pb2_grpc.HotmethodStub,
    task_id: str,
    ok: bool,
    reason: str,
    artifacts: list[dict],
) -> None:
    """通过 gRPC Hotmethod.NotifyResult 上报采集结果。"""
    if ok or artifacts:
        stub.NotifyResult(
            hotmethod_pb2.TaskResult(
                task_id=task_id,
                error_message="" if ok else reason,
                artifact_type="raw",
                artifact_metadata_json=json.dumps(artifacts),
            ),
            timeout=10,
        )
    else:
        stub.NotifyResult(
            hotmethod_pb2.TaskResult(
                task_id=task_id,
                error_message=reason,
            ),
            timeout=10,
        )


# ── 主循环 ─────────────────────────────────────────────────────────

_should_exit = False
_signal_count = 0  # 信号计数器：第一次优雅退出，第二次强制终止


def _on_signal(signum, frame):
    global _should_exit, _signal_count
    _signal_count += 1
    if _signal_count >= 2:
        # 第二次信号：强制退出（采集器子进程可能残留，但操作系统会回收）
        log_event("warning", "agent_force_exit", signal=_signal_count)
        os._exit(1)
    _should_exit = True
    log_event("info", "agent_graceful_shutdown", signal=_signal_count,
              hint="再次发送 SIGTERM 强制退出")


def _init_register_with_retry(conn, config: AgentConfig, max_retries: int = 5, backoff_sec: float = 2.0) -> AgentConfig:
    """注册 Agent 并拉取配置，支持指数退避重试。

    生产环境中 Server 可能尚未就绪，重试避免 Agent 启动即崩溃。
    """
    last_exc = None
    delay = backoff_sec
    for attempt in range(max_retries + 1):
        try:
            init_stub = init_pb2_grpc.InitAgentStub(conn.channel)
            _register(init_stub, config)
            config = _fetch_config(init_stub, config)
            log_event("info", "agent_registered", agent_id=config.agent_id, ip_addr=config.agent_ip_addr)
            return config
        except grpc.RpcError as exc:
            last_exc = exc
            if attempt >= max_retries:
                raise
            log_event(
                "warning",
                "agent_init_retry",
                attempt=attempt + 1,
                max_retries=max_retries,
                code=exc.code(),
                delay=delay,
            )
            time.sleep(delay)
            delay *= 2
    raise last_exc


def main() -> None:
    global _should_exit
    config = load_config()
    conn = GrpcConnection(config.server_grpc_addr, auth_token=config.grpc_auth_token)
    sampler = ProcessStatsSampler()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # 初始化注册 + 拉取配置（带重试）
    config = _init_register_with_retry(conn, config)
    runtime_control_observer = RuntimeControlObserver()
    runtime_control_observer.start()

    work_queue = queue.Queue(maxsize=1)
    result_queue = queue.Queue()
    worker = threading.Thread(
        target=_collector_worker,
        args=(work_queue, result_queue, config),
        name="collector-worker",
        daemon=True,
    )
    worker.start()
    watch_thread = threading.Thread(
        target=_watch_sync_loop,
        args=(conn, config, runtime_control_observer),
        name="watch-sync",
        daemon=True,
    )
    watch_thread.start()
    active_task: dict[str, Any] | None = None

    while not _should_exit:
        try:
            finished_task, ok, reason, artifacts = result_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            try:
                conn.call_with_retry(
                    lambda: _notify_result(
                        hotmethod_pb2_grpc.HotmethodStub(conn.channel),
                        finished_task["id"],
                        ok,
                        reason,
                        artifacts,
                    )
                )
                if ok:
                    log_event("info", "task_completed", task_id=finished_task["id"], artifact_count=len(artifacts))
                else:
                    log_event("error", "task_failed", task_id=finished_task["id"], reason=reason)
            except grpc.RpcError as exc:
                log_event(
                    "error",
                    "notify_result_failed",
                    task_id=finished_task["id"],
                    code=exc.code(),
                    details=exc.details(),
                )
            finally:
                if active_task and active_task.get("id") == finished_task.get("id"):
                    active_task = None
                result_queue.task_done()

        try:
            task = conn.call_with_retry(
                lambda: _heartbeat(
                    healthcheck_pb2_grpc.HealthCheckStub(conn.channel),
                    config,
                    sampler,
                    busy=active_task is not None,
                )
            )
        except grpc.RpcError as exc:
            log_event("error", "heartbeat_failed", code=exc.code(), details=exc.details())
            time.sleep(config.heartbeat_interval_sec)
            continue

        if task is None:
            time.sleep(config.heartbeat_interval_sec)
            continue

        if active_task is not None:
            log_event(
                "warning",
                "task_received_while_busy",
                active_task_id=active_task.get("id"),
                dropped_task_id=task.get("id"),
            )
            time.sleep(config.heartbeat_interval_sec)
            continue

        log_event(
            "info",
            "task_pulled",
            task_id=task["id"],
            collector=task["collector_type"],
            pid=task["target_pid"],
        )
        active_task = task
        work_queue.put(task)

        time.sleep(config.heartbeat_interval_sec)

    work_queue.put(None)
    worker.join(timeout=5)
    watch_thread.join(timeout=5)
    runtime_control_observer.stop()
    conn.close()


# ── 辅助 ───────────────────────────────────────────────────────────


# profiler_type → collector_type 映射（与 proto hotmethod.proto + healthcheck_service.py 对齐）
_PROFILER_TO_COLLECTOR: dict[int, str] = {
    0: "perf_cpu",        # perf
    1: "java_async",      # async-profiler (Java)
    2: "go_pprof",         # pprof (Go)
    3: "pyspy",            # py-spy (Python)
    4: "ebpf_io",          # bpftrace (eBPF)
    5: "memory_smaps",     # memory smaps
    6: "sys_metrics",      # system multi-metrics
    7: "continuous_perf",  # continuous perf
}

# task_type → collector_type 映射（MemCheck 等需要特殊路由的场景）
_TASK_TYPE_COLLECTOR: dict[int, str] = {
    2: "trace_endpoint_profile",
    4: "memory_smaps",     # MemCheck
    8: "off_cpu_wait_profile",
    9: "baseline_window_profile",
    10: "log_scan",
    11: "dependency_check",
    12: "redis_check",
    13: "pyspy",
    14: "process_inventory",
    15: "runtime_control_history",
    16: "python_heap_profile",
    17: "source_snapshot",
    18: "source_mechanism_query",
    19: "python_heap_reference",
}


def _profiler_to_collector(profiler_type: int) -> str:
    """根据 profiler_type 获取 collector_type 字符串。"""
    return _PROFILER_TO_COLLECTOR.get(profiler_type, "perf_cpu")


def _fill_pid_stats(message, stats: dict[str, Any]) -> None:
    message.cpu_percent = float(stats.get("cpu_percent", 0.0) or 0.0)
    message.rss_mb = float(stats.get("rss_mb", 0.0) or 0.0)
    message.read_kb_s = float(stats.get("read_kb_s", 0.0) or 0.0)
    message.write_kb_s = float(stats.get("write_kb_s", 0.0) or 0.0)
    message.children_count = int(stats.get("children_count", 0) or 0)


def _os_info() -> str:
    try:
        with open("/proc/version", "r") as fh:
            return fh.readline().strip()
    except FileNotFoundError:
        return "unknown"


if __name__ == "__main__":
    main()
