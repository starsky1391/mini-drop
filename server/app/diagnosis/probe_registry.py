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
        purpose="识别 CPU 热点、锁竞争和异常调用栈",
        runner_task_kind="perf_cpu",
        supported_platforms=["linux"],
        required_capabilities=["perf_cpu"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=49,
        estimated_overhead={"cpu_percent": "2-8", "disk_mb": "20-200"},
        applicable_hypotheses=["SELF_CODE_REGRESSION", "CPU_SATURATION", "LOCK_CONTENTION"],
    ),
    "process_off_cpu_profile": ProbeDefinition(
        probe_id="process_off_cpu_profile",
        name="进程 Off-CPU Profile",
        purpose="识别阻塞等待、锁竞争、调度延迟和 IO 等待",
        runner_task_kind="off_cpu_wait_profile",
        supported_platforms=["linux"],
        required_capabilities=["off_cpu_wait_profile"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=49,
        estimated_overhead={"cpu_percent": "2-8", "disk_mb": "20-200"},
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


def choose_probe_ids(symptom: str) -> list[str]:
    """确定性策略先查低风险指标，再选择一个可区分假设的深度探针。"""
    mapping = {
        "cpu_saturation": ["host_process_metrics", "process_cpu_profile", "process_off_cpu_profile", "process_trace_endpoint_profile"],
        "latency_increase": ["host_process_metrics", "process_trace_endpoint_profile", "process_cpu_profile"],
        "io_degradation": ["host_process_metrics", "process_io_latency", "process_off_cpu_profile"],
        "noisy_neighbor": ["host_process_metrics", "process_io_latency"],
        "memory_pressure": ["process_memory_map", "process_baseline_window", "host_process_metrics"],
    }
    return mapping.get(symptom, ["host_process_metrics", "process_cpu_profile", "process_off_cpu_profile"])
