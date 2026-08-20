"""HealthCheck gRPC 服务：1 Hz 心跳 + 任务下发。"""

import json
from typing import Any

from server.app.generated import healthcheck_pb2, healthcheck_pb2_grpc, hotmethod_pb2


class HealthCheckService(healthcheck_pb2_grpc.HealthCheckServicer):
    """Agent 心跳服务，一次 RPC 同时完成保活和任务拉取。"""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    def Do(self, request: healthcheck_pb2.HealthCheckRequest, context) -> healthcheck_pb2.HealthCheckResponse:
        # 记录心跳，检查有无待执行任务
        if hasattr(self._repo, "record_agent_metrics"):
            self._repo.record_agent_metrics(request.agent_id, _metrics_from_request(request))
        response = healthcheck_pb2.HealthCheckResponse()
        response.status = healthcheck_pb2.HealthCheckResponse.SERVING

        if getattr(request, "busy", False):
            if hasattr(self._repo, "heartbeat_only"):
                self._repo.heartbeat_only(request.agent_id, request.ip_addr)
            response.pending = False
            return response

        task = self._repo.heartbeat(request.agent_id, request.ip_addr)

        if task is None:
            response.pending = False
            return response

        # 构造 TaskDesc 并嵌入响应
        response.pending = True
        task_desc = response.task_desc
        task_desc.task_id = task.id
        task_desc.task_type = self._task_type(task.collector_type)
        task_desc.profiler_type = self._profiler_type(task.collector_type)
        task_desc.timeout_sec = task.duration_sec + 30  # 留 30 秒余量
        task_desc.sample_argv.hz = task.sample_rate
        task_desc.sample_argv.duration = task.duration_sec
        task_desc.sample_argv.pid = task.target_pid
        task_desc.sample_argv.callgraph = task.request_params.get("options", {}).get("callgraph", "fp")
        task_desc.sample_argv.event = task.request_params.get("options", {}).get("event", "cpu-cycles")
        task_desc.sample_argv.subprocess = task.request_params.get("options", {}).get("subprocess", False)
        task_desc.script_content = json.dumps(
            {"options": task.request_params.get("options", {})},
            ensure_ascii=False,
        )
        return response

    @staticmethod
    def _profiler_type(collector_type: str) -> int:
        """将 collector_type 字符串映射为 protobuf profiler_type 值。

        Proto 定义（hotmethod.proto）:
          0=perf, 1=async-profiler(Java), 2=pprof(Go), 3=py-spy, 4=bpftrace
          5=memory_smaps, 6=sys_metrics, 7=continuous_perf
        """
        mapping: dict[str, int] = {
            "perf_cpu": 0,
            "java_async": 1,
            "go_pprof": 2,
            "pyspy": 3,
            "ebpf_io": 4,
            "memory_smaps": 5,
            "sys_metrics": 6,
            "continuous_perf": 7,
            "baseline_window_profile": 7,
            "off_cpu_wait_profile": 4,
            "trace_endpoint_profile": 0,
            "pyspy": 3,
        }
        return mapping.get(collector_type, 0)

    @staticmethod
    def _task_type(collector_type: str) -> int:
        mapping: dict[str, int] = {
            "trace_endpoint_profile": 2,
            "off_cpu_wait_profile": 8,
            "baseline_window_profile": 9,
            "log_scan": 10,
            "dependency_check": 11,
            "redis_check": 12,
            "pyspy": 13,
            "process_inventory": 14,
            "runtime_control_history": 15,
            "python_heap_profile": 16,
            "source_snapshot": 17,
            "source_mechanism_query": 18,
            "python_heap_reference": 19,
        }
        return mapping.get(collector_type, 0)


def _pid_stats_to_dict(stats) -> dict:
    return {
        "cpu_percent": round(float(stats.cpu_percent), 3),
        "rss_mb": round(float(stats.rss_mb), 3),
        "read_kb_s": round(float(stats.read_kb_s), 3),
        "write_kb_s": round(float(stats.write_kb_s), 3),
        "children_count": int(stats.children_count),
    }


def _metrics_from_request(request: healthcheck_pb2.HealthCheckRequest) -> dict:
    return {
        "self": _pid_stats_to_dict(request.self_pstats),
        "children": _pid_stats_to_dict(request.children_pstats),
    }
