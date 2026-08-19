"""固定探针注册表：模型只能选择这里声明的能力。"""

from __future__ import annotations

from server.app.diagnosis.schemas import ProbeDefinition


_PROBES = {
    "host_process_metrics": ProbeDefinition(
        probe_id="host_process_metrics",
        name="主机与进程系统指标",
        purpose="低开销确认 CPU、内存、线程、FD、网络和 I/O 等待趋势",
        runner_task_kind="sys_metrics",
        supported_platforms=["linux"],
        required_capabilities=["sys_metrics"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=30,
        default_sample_rate=11,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<10"},
        applicable_hypotheses=[
            "CPU_SATURATION", "HOST_MEMORY_PRESSURE", "HOST_DISK_CONTENTION",
            "SAME_HOST_NOISY_NEIGHBOR", "NETWORK_DEGRADATION",
        ],
    ),
    "process_cpu_profile": ProbeDefinition(
        probe_id="process_cpu_profile",
        name="进程 CPU Profile",
        purpose="识别 on-CPU 热点和异常运行态调用栈",
        runner_task_kind="perf_cpu",
        supported_platforms=["linux"],
        required_capabilities=["perf_cpu"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=49,
        estimated_overhead={"cpu_percent": "2-8", "disk_mb": "20-200"},
        applicable_hypotheses=["SELF_CODE_REGRESSION", "CPU_SATURATION"],
    ),
    "process_python_runtime_profile": ProbeDefinition(
        probe_id="process_python_runtime_profile",
        name="Python 运行时栈采样",
        purpose="识别 Python 线程阻塞、锁等待和用户态调用栈热点",
        runner_task_kind="pyspy",
        supported_platforms=["linux"],
        required_capabilities=["pyspy"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=49,
        estimated_overhead={"cpu_percent": "1-5", "disk_mb": "10-100"},
        applicable_hypotheses=["LOCK_CONTENTION", "SELF_CODE_REGRESSION"],
    ),
    "process_off_cpu_profile": ProbeDefinition(
        probe_id="process_off_cpu_profile",
        name="进程 Off-CPU Wait Profile",
        purpose="使用 bpftrace/eBPF 采集等待栈，识别阻塞等待、锁竞争、调度延迟和 IO 等待",
        runner_task_kind="off_cpu_wait_profile",
        supported_platforms=["linux"],
        required_capabilities=["off_cpu_wait_profile"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=49,
        estimated_overhead={"cpu_percent": "2-8", "disk_mb": "10-100"},
        applicable_hypotheses=["LOCK_CONTENTION", "HOST_DISK_CONTENTION", "SELF_CODE_REGRESSION"],
    ),
    "process_trace_endpoint_profile": ProbeDefinition(
        probe_id="process_trace_endpoint_profile",
        name="进程调用链上下文采样",
        purpose="将函数热点回连到 endpoint 和调用路径上下文",
        runner_task_kind="trace_endpoint_profile",
        supported_platforms=["linux"],
        required_capabilities=["trace_endpoint_profile"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=11,
        estimated_overhead={"cpu_percent": "1-5", "disk_mb": "<100"},
        applicable_hypotheses=["SELF_CODE_REGRESSION", "DOWNSTREAM_LATENCY", "CPU_SATURATION"],
    ),
    "process_baseline_window": ProbeDefinition(
        probe_id="process_baseline_window",
        name="进程基线窗口采样",
        purpose="对比连续窗口中的热点变化，降低重复任务抖动",
        runner_task_kind="baseline_window_profile",
        supported_platforms=["linux"],
        required_capabilities=["baseline_window_profile"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=30,
        max_duration_seconds=60,
        default_sample_rate=11,
        estimated_overhead={"cpu_percent": "1-5", "disk_mb": "<100"},
        applicable_hypotheses=["CPU_SATURATION", "SELF_CODE_REGRESSION", "MEMORY_LEAK"],
    ),
    "process_log_scan": ProbeDefinition(
        probe_id="process_log_scan",
        name="日志窗口扫描",
        purpose="扫描异常窗口内日志，提取错误簇、trace_id、endpoint 和 dependency 线索",
        runner_task_kind="log_scan",
        supported_platforms=["linux"],
        required_capabilities=["log_scan"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<50"},
        applicable_hypotheses=["DOWNSTREAM_LATENCY", "NETWORK_DEGRADATION", "MEMORY_LEAK", "LOCK_CONTENTION"],
    ),
    "process_dependency_check": ProbeDefinition(
        probe_id="process_dependency_check",
        name="下游依赖可达性检测",
        purpose="检测 DNS、TCP、HTTP、gRPC 和关键下游服务的可达性与延迟",
        runner_task_kind="dependency_check",
        supported_platforms=["linux"],
        required_capabilities=["dependency_check"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=10,
        max_duration_seconds=30,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<10"},
        applicable_hypotheses=["DOWNSTREAM_LATENCY", "NETWORK_DEGRADATION"],
    ),
    "process_redis_check": ProbeDefinition(
        probe_id="process_redis_check",
        name="Redis 专项检查",
        purpose="采集 Redis PING、INFO、SLOWLOG 和 LATENCY 证据",
        runner_task_kind="redis_check",
        supported_platforms=["linux"],
        required_capabilities=["redis_check"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=10,
        max_duration_seconds=30,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<10"},
        applicable_hypotheses=["DOWNSTREAM_LATENCY", "MEMORY_LEAK", "NETWORK_DEGRADATION"],
    ),
    "process_io_latency": ProbeDefinition(
        probe_id="process_io_latency",
        name="块设备 I/O 延迟",
        purpose="确认宿主机块设备延迟和 I/O 争抢",
        runner_task_kind="ebpf_io",
        supported_platforms=["linux"],
        required_capabilities=["ebpf_io"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=11,
        estimated_overhead={"cpu_percent": "1-5", "disk_mb": "<50"},
        applicable_hypotheses=["HOST_DISK_CONTENTION", "SAME_HOST_NOISY_NEIGHBOR"],
    ),
    "process_memory_map": ProbeDefinition(
        probe_id="process_memory_map",
        name="进程内存映射摘要",
        purpose="确认 RSS/PSS/Swap 趋势和内存压力",
        runner_task_kind="memory_smaps",
        supported_platforms=["linux"],
        required_capabilities=["memory_smaps"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=30,
        default_sample_rate=11,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<20"},
        applicable_hypotheses=["HOST_MEMORY_PRESSURE", "MEMORY_LEAK"],
    ),
}


def get_probe(probe_id: str) -> ProbeDefinition:
    try:
        return _PROBES[probe_id]
    except KeyError as exc:
        raise ValueError(f"未注册探针: {probe_id}") from exc


def list_probes() -> list[ProbeDefinition]:
    return list(_PROBES.values())


def evidence_gap_to_probe_id(evidence_gap: str) -> str | None:
    return {
        "cpu_profile": "process_cpu_profile",
        "off_cpu_wait_profile": "process_off_cpu_profile",
        "trace_endpoint_profile": "process_trace_endpoint_profile",
        "baseline_window_profile": "process_baseline_window",
        "python_runtime_profile": "process_python_runtime_profile",
        "log_scan": "process_log_scan",
        "dependency_check": "process_dependency_check",
        "redis_check": "process_redis_check",
        "io_latency": "process_io_latency",
        "memory_map": "process_memory_map",
    }.get(evidence_gap)


def probe_id_to_evidence_gap(probe_id: str) -> str:
    return {
        "process_cpu_profile": "cpu_profile",
        "process_off_cpu_profile": "off_cpu_wait_profile",
        "process_trace_endpoint_profile": "trace_endpoint_profile",
        "process_baseline_window": "baseline_window_profile",
        "process_python_runtime_profile": "python_runtime_profile",
        "process_log_scan": "log_scan",
        "process_dependency_check": "dependency_check",
        "process_redis_check": "redis_check",
        "process_io_latency": "io_latency",
        "process_memory_map": "memory_map",
    }.get(probe_id, "")


def build_probe_manifest() -> dict:
    """Expose a safe tool catalog for AI tree probe selection."""
    probes = []
    for definition in sorted(_PROBES.values(), key=lambda item: item.probe_id):
        evidence_family = probe_id_to_evidence_gap(definition.probe_id)
        probes.append({
            "probe_id": definition.probe_id,
            "evidence_family": evidence_family,
            "name": definition.name,
            "purpose": definition.purpose,
            "can_answer": _probe_questions(definition.probe_id),
            "required_capabilities": definition.required_capabilities,
            "required_target_fields": _required_target_fields(definition.probe_id),
            "risk_level": definition.risk_level,
            "auto_executable_when_policy_all_registered": definition.risk_level in {"R0", "R1", "R2"},
            "max_duration_seconds": definition.max_duration_seconds,
            "output_contract": f"{definition.runner_task_kind}_json",
        })
    return {
        "schema_version": "1.0",
        "selection_field": "evidence_family",
        "available_probes": probes,
        "hard_forbidden": [
            "arbitrary_shell",
            "host_sysctl_write",
            "service_restart",
            "configuration_mutation",
            "remediation_action_execution",
        ],
    }


def choose_probe_ids(symptom: str) -> list[str]:
    """确定性策略先查低风险指标，再选择一个可区分假设的深度探针。"""
    mapping = {
        "cpu_saturation": ["host_process_metrics", "process_cpu_profile", "process_off_cpu_profile", "process_trace_endpoint_profile"],
        "latency_increase": ["host_process_metrics", "process_dependency_check", "process_log_scan", "process_trace_endpoint_profile", "process_cpu_profile"],
        "io_degradation": ["host_process_metrics", "process_io_latency", "process_off_cpu_profile"],
        "noisy_neighbor": ["host_process_metrics", "process_io_latency"],
        "memory_pressure": ["process_memory_map", "process_log_scan", "process_baseline_window", "host_process_metrics"],
        "runtime_contention": ["host_process_metrics", "process_log_scan", "process_off_cpu_profile", "process_python_runtime_profile", "process_trace_endpoint_profile"],
    }
    return mapping.get(symptom, ["host_process_metrics", "process_cpu_profile", "process_off_cpu_profile"])


def _required_target_fields(probe_id: str) -> list[str]:
    base = ["agent_id", "host_id"]
    if probe_id.startswith("process_"):
        base.append("pid")
    if probe_id == "process_trace_endpoint_profile":
        base.extend(["service_id", "instance_id"])
    if probe_id in {"process_dependency_check", "process_redis_check"}:
        base.append("dependency_targets")
    return list(dict.fromkeys(base))


def _probe_questions(probe_id: str) -> list[str]:
    return {
        "host_process_metrics": [
            "目标进程或宿主机是否存在 CPU、内存、线程、FD、网络或 I/O 指标偏移",
            "当前窗口是否值得继续深采集",
        ],
        "process_cpu_profile": [
            "是否存在 on-CPU 热点函数",
            "热点是否足以支持 function 层定位",
        ],
        "process_python_runtime_profile": [
            "Python 线程是否存在用户态热点、锁等待或运行时阻塞",
            "Python 栈是否能解释当前热点",
        ],
        "process_off_cpu_profile": [
            "是否存在 futex、锁、I/O、socket、syscall 或调度等待",
            "等待是否能定位到具体等待函数或调用栈",
        ],
        "process_trace_endpoint_profile": [
            "热点属于哪个 endpoint 或 call_path",
            "Trace/span 是否与目标进程和采集窗口匹配",
        ],
        "process_baseline_window": [
            "当前热点是否相对最近基线发生偏移",
            "重复任务结论是否稳定",
        ],
        "process_log_scan": [
            "异常窗口内是否存在错误日志簇",
            "日志是否出现 trace_id、endpoint 或下游依赖线索",
        ],
        "process_dependency_check": [
            "DNS、TCP、HTTP 或 gRPC 依赖是否可达",
            "下游依赖是否存在超时、拒绝连接或错误响应",
        ],
        "process_redis_check": [
            "Redis PING、INFO、SLOWLOG 或 LATENCY 是否异常",
            "Redis 是否支持或反驳下游依赖候选",
        ],
        "process_io_latency": [
            "宿主机块设备 I/O 延迟是否异常",
            "是否存在 I/O 争抢或磁盘瓶颈",
        ],
        "process_memory_map": [
            "目标进程 RSS/PSS/Swap 是否异常",
            "内存压力是否支持 memory 类候选",
        ],
    }.get(probe_id, ["补充该注册采集器对应的结构化证据"])
