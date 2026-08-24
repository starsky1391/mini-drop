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
    "process_python_heap_profile": ProbeDefinition(
        probe_id="process_python_heap_profile",
        name="Python Heap Profile",
        purpose="使用 Memray 官方 Producer 采集分配热点、保留分配和源码行栈",
        runner_task_kind="python_heap_profile",
        supported_platforms=["linux"],
        required_capabilities=["python_heap_profile"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=30,
        max_duration_seconds=180,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "2-10", "disk_mb": "20-500"},
        applicable_hypotheses=["MEMORY_LEAK", "HOST_MEMORY_PRESSURE", "SELF_CODE_REGRESSION"],
    ),
    "process_go_heap_profile": ProbeDefinition(
        probe_id="process_go_heap_profile",
        name="Go Heap pprof",
        purpose="使用 Go pprof heap 采集分配或 in-use 热点，并生成可验证的源码行候选",
        runner_task_kind="go_pprof",
        supported_platforms=["linux"],
        required_capabilities=["go_pprof"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "1-5", "disk_mb": "10-200"},
        applicable_hypotheses=["MEMORY_LEAK", "HOST_MEMORY_PRESSURE", "SELF_CODE_REGRESSION"],
    ),
    "process_source_snapshot": ProbeDefinition(
        probe_id="process_source_snapshot",
        name="源码上下文快照",
        purpose="验证 Git revision 并有界提取行候选附近源码和符号上下文",
        runner_task_kind="source_snapshot",
        supported_platforms=["linux"],
        required_capabilities=["source_snapshot"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=10,
        max_duration_seconds=30,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<10"},
        applicable_hypotheses=["MEMORY_LEAK", "SELF_CODE_REGRESSION", "LOCK_CONTENTION"],
    ),
    "process_source_mechanism_query": ProbeDefinition(
        probe_id="process_source_mechanism_query",
        name="源码机制查询",
        purpose="使用 CodeQL 在已验证 revision 和行锚点上查询跨函数调用与数据流机制",
        runner_task_kind="source_mechanism_query",
        supported_platforms=["linux"],
        required_capabilities=["source_mechanism_query"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=30,
        max_duration_seconds=180,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "5-40", "disk_mb": "100-5000"},
        applicable_hypotheses=["MEMORY_LEAK", "SELF_CODE_REGRESSION", "LOCK_CONTENTION"],
    ),
    "process_python_heap_reference": ProbeDefinition(
        probe_id="process_python_heap_reference",
        name="Python 运行时引用链",
        purpose="使用 PyHeap dump 验证保留对象的有限入向引用路径",
        runner_task_kind="python_heap_reference",
        supported_platforms=["linux"],
        required_capabilities=["python_heap_reference"],
        risk_level="R2",
        requires_approval=True,
        default_duration_seconds=30,
        max_duration_seconds=180,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "5-30", "disk_mb": "100-4096"},
        applicable_hypotheses=["MEMORY_LEAK"],
    ),
    "process_python_lock_wait_profile": ProbeDefinition(
        probe_id="process_python_lock_wait_profile",
        name="Python 锁等待画像",
        purpose="归一化 py-spy/off-CPU 等工业采集输出中的锁、队列和 Future 等等待站点",
        runner_task_kind="python_lock_wait_profile",
        supported_platforms=["linux"],
        required_capabilities=["python_lock_wait_profile"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<20"},
        applicable_hypotheses=["LOCK_CONTENTION"],
    ),
    "process_python_exception_profile": ProbeDefinition(
        probe_id="process_python_exception_profile",
        name="Python 异常风暴画像",
        purpose="归一化日志和 Trace 中的 traceback、异常类型、抛出点和日志点",
        runner_task_kind="python_exception_profile",
        supported_platforms=["linux"],
        required_capabilities=["python_exception_profile"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<20"},
        applicable_hypotheses=["SELF_CODE_REGRESSION", "DOWNSTREAM_LATENCY"],
    ),
    "process_python_queue_profile": ProbeDefinition(
        probe_id="process_python_queue_profile",
        name="Python 队列堆积画像",
        purpose="归一化 Celery/RQ/asyncio 队列、broker 指标、worker 日志和运行时栈证据",
        runner_task_kind="python_queue_profile",
        supported_platforms=["linux"],
        required_capabilities=["python_queue_profile"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<20"},
        applicable_hypotheses=["DOWNSTREAM_LATENCY", "SELF_CODE_REGRESSION"],
    ),
    "process_python_pool_profile": ProbeDefinition(
        probe_id="process_python_pool_profile",
        name="Python 连接池耗尽画像",
        purpose="归一化 SQLAlchemy、Redis、urllib3、aiohttp 等库日志和栈签名中的池耗尽证据",
        runner_task_kind="python_pool_profile",
        supported_platforms=["linux"],
        required_capabilities=["python_pool_profile"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<20"},
        applicable_hypotheses=["LOCK_CONTENTION", "DOWNSTREAM_LATENCY"],
    ),
    "process_python_retry_timeout_profile": ProbeDefinition(
        probe_id="process_python_retry_timeout_profile",
        name="Python 重试超时画像",
        purpose="归一化日志、Trace 和依赖检测中的重试、超时、退避和依赖上下文证据",
        runner_task_kind="python_retry_timeout_profile",
        supported_platforms=["linux"],
        required_capabilities=["python_retry_timeout_profile"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<20"},
        applicable_hypotheses=["DOWNSTREAM_LATENCY", "NETWORK_DEGRADATION"],
    ),
    "process_python_cache_profile": ProbeDefinition(
        probe_id="process_python_cache_profile",
        name="Python 缓存增长画像",
        purpose="归一化应用运行日志、缓存指标和 Trace 属性中的缓存体积、文件数、key 基数和后端线索",
        runner_task_kind="python_cache_profile",
        supported_platforms=["linux"],
        required_capabilities=["python_cache_profile"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<20"},
        applicable_hypotheses=["SELF_CODE_REGRESSION", "MEMORY_LEAK"],
    ),
    "process_python_input_profile": ProbeDefinition(
        probe_id="process_python_input_profile",
        name="Python 输入慢路径画像",
        purpose="归一化应用运行日志、Trace 和 Python 栈中的输入规模、基数、倾斜和慢路径操作线索",
        runner_task_kind="python_input_profile",
        supported_platforms=["linux"],
        required_capabilities=["python_input_profile"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=15,
        max_duration_seconds=60,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<2", "disk_mb": "<20"},
        applicable_hypotheses=["SELF_CODE_REGRESSION", "CPU_SATURATION"],
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
    "process_runtime_control_history": ProbeDefinition(
        probe_id="process_runtime_control_history",
        name="运行控制历史",
        purpose="查询同窗信号、systemd、容器运行时、cgroup、发布和 Kubernetes 控制事件",
        runner_task_kind="runtime_control_history",
        supported_platforms=["linux"],
        required_capabilities=["runtime_control_history"],
        risk_level="R1",
        requires_approval=False,
        default_duration_seconds=10,
        max_duration_seconds=30,
        default_sample_rate=1,
        estimated_overhead={"cpu_percent": "<1", "disk_mb": "<10"},
        applicable_hypotheses=["LOCK_CONTENTION", "SELF_CODE_REGRESSION"],
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
        "python_heap_profile": "process_python_heap_profile",
        "go_heap_profile": "process_go_heap_profile",
        "source_snapshot": "process_source_snapshot",
        "source_mechanism_query": "process_source_mechanism_query",
        "python_heap_reference": "process_python_heap_reference",
        "log_scan": "process_log_scan",
        "dependency_check": "process_dependency_check",
        "redis_check": "process_redis_check",
        "io_latency": "process_io_latency",
        "memory_map": "process_memory_map",
        "runtime_control_history": "process_runtime_control_history",
        "python_lock_wait_profile": "process_python_lock_wait_profile",
        "python_exception_profile": "process_python_exception_profile",
        "python_queue_profile": "process_python_queue_profile",
        "python_pool_profile": "process_python_pool_profile",
        "python_retry_timeout_profile": "process_python_retry_timeout_profile",
        "python_cache_profile": "process_python_cache_profile",
        "python_input_profile": "process_python_input_profile",
    }.get(evidence_gap)


def probe_id_to_evidence_gap(probe_id: str) -> str:
    return {
        "process_cpu_profile": "cpu_profile",
        "process_off_cpu_profile": "off_cpu_wait_profile",
        "process_trace_endpoint_profile": "trace_endpoint_profile",
        "process_baseline_window": "baseline_window_profile",
        "process_python_runtime_profile": "python_runtime_profile",
        "process_python_heap_profile": "python_heap_profile",
        "process_go_heap_profile": "go_heap_profile",
        "process_source_snapshot": "source_snapshot",
        "process_source_mechanism_query": "source_mechanism_query",
        "process_python_heap_reference": "python_heap_reference",
        "process_log_scan": "log_scan",
        "process_dependency_check": "dependency_check",
        "process_redis_check": "redis_check",
        "process_io_latency": "io_latency",
        "process_memory_map": "memory_map",
        "process_runtime_control_history": "runtime_control_history",
        "process_python_lock_wait_profile": "python_lock_wait_profile",
        "process_python_exception_profile": "python_exception_profile",
        "process_python_queue_profile": "python_queue_profile",
        "process_python_pool_profile": "python_pool_profile",
        "process_python_retry_timeout_profile": "python_retry_timeout_profile",
        "process_python_cache_profile": "python_cache_profile",
        "process_python_input_profile": "python_input_profile",
    }.get(probe_id, "")


def build_probe_manifest() -> dict:
    """Expose a safe tool catalog for AI tree probe selection."""
    probes = []
    for definition in sorted(_PROBES.values(), key=lambda item: item.probe_id):
        evidence_family = probe_id_to_evidence_gap(definition.probe_id)
        role, cannot_establish, produces, quality_gate, next_probe_hints = _manifest_semantics(
            definition.probe_id
        )
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
            "output_contract": _output_contract(definition.runner_task_kind),
            "capability_role": role,
            "cannot_establish": cannot_establish,
            "produces": produces,
            "input_requirements": _required_target_fields(definition.probe_id),
            "quality_gate": quality_gate,
            "next_probe_hints": next_probe_hints,
            "may_help_distinguish": definition.applicable_hypotheses,
            # Keep the old field for clients that still parse schema 1.0.
            "applicable_hypotheses": definition.applicable_hypotheses,
        })
    return {
        "schema_version": "2.0",
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


def _manifest_semantics(probe_id: str) -> tuple[str, list[str], list[str], list[str], list[str]]:
    """Describe probe boundaries without turning them into cause rules."""
    if probe_id in {"host_process_metrics", "process_memory_map", "process_log_scan", "process_dependency_check", "process_redis_check"}:
        return (
            "symptom",
            ["具体源码机制", "正式根因"],
            ["symptom_signal", "resource_metrics"],
            ["同窗窗口", "结构化输出", "目标范围匹配"],
            ["cpu_profile", "off_cpu_wait_profile", "source_snapshot"],
        )
    if probe_id in {
        "process_cpu_profile",
        "process_python_runtime_profile",
        "process_off_cpu_profile",
        "process_io_latency",
        "process_python_lock_wait_profile",
        "process_python_exception_profile",
        "process_python_queue_profile",
        "process_python_pool_profile",
        "process_python_retry_timeout_profile",
        "process_python_cache_profile",
        "process_python_input_profile",
        "process_baseline_window",
    }:
        return (
            "localization",
            ["完整触发链", "源码机制", "正式根因"],
            ["runtime_observation", "cost_center_candidate", "wait_or_task_context"],
            ["有效采样或结构化状态", "目标范围匹配", "同窗窗口"],
            ["trace_endpoint_profile", "source_snapshot", "source_mechanism_query"],
        )
    if probe_id == "process_source_snapshot":
        return (
            "source_relation",
            ["触发关系", "机制因果", "正式根因"],
            ["verified_source_context", "source_line_candidate", "source_relation_candidate"],
            ["revision verified", "candidate uniquely matched"],
            ["source_mechanism_query"],
        )
    if probe_id in {"process_source_mechanism_query", "process_python_heap_reference"}:
        return (
            "mechanism",
            ["运行时实际发生过该路径", "正式根因"],
            ["source_relation", "mechanism_path", "reference_path"],
            ["input provenance valid", "guarded query or bounded dump", "explicit path status"],
            ["runtime_profile", "source_snapshot"],
        )
    if probe_id == "process_trace_endpoint_profile":
        return (
            "context",
            ["源码机制", "正式根因"],
            ["endpoint_context", "call_path_context", "dependency_context"],
            ["trace/span target match", "same-window"],
            ["source_snapshot", "dependency_check"],
        )
    return (
        "context",
        ["具体源码机制", "正式根因"],
        ["structured_observation"],
        ["collector status valid"],
        ["source_snapshot"],
    )


def choose_probe_ids(symptom: str) -> list[str]:
    """确定性策略先查低风险指标，再选择一个可区分假设的深度探针。"""
    mapping = {
        "cpu_saturation": ["host_process_metrics", "process_cpu_profile", "process_off_cpu_profile", "process_trace_endpoint_profile"],
        "latency_increase": ["host_process_metrics", "process_dependency_check", "process_log_scan", "process_trace_endpoint_profile", "process_cpu_profile"],
        "io_degradation": ["host_process_metrics", "process_io_latency", "process_off_cpu_profile"],
        "noisy_neighbor": ["host_process_metrics", "process_io_latency"],
        "memory_pressure": ["process_memory_map", "process_log_scan", "process_baseline_window", "host_process_metrics"],
        "runtime_contention": ["host_process_metrics", "process_log_scan", "process_python_lock_wait_profile", "process_runtime_control_history"],
        "exception_storm": ["process_log_scan", "process_python_exception_profile", "process_source_snapshot"],
        "queue_backlog": ["process_log_scan", "process_redis_check", "process_python_queue_profile", "process_source_snapshot"],
        "pool_exhaustion": ["process_log_scan", "process_python_pool_profile", "process_source_snapshot"],
        "retry_timeout": ["process_log_scan", "process_trace_endpoint_profile", "process_python_retry_timeout_profile", "process_source_snapshot"],
        "cache_growth": ["process_log_scan", "process_python_cache_profile", "process_source_snapshot"],
        "input_slow_path": ["process_python_input_profile", "process_python_runtime_profile", "process_source_snapshot"],
    }
    return mapping.get(symptom, ["host_process_metrics", "process_cpu_profile", "process_off_cpu_profile"])


def _output_contract(runner_task_kind: str) -> str:
    return {
        "source_mechanism_query": "source_mechanism_json",
        "python_heap_reference": "python_heap_reference_json",
        "python_heap_profile": "python_heap_profile_json",
        "go_pprof": "go_heap_profile_json",
        "source_snapshot": "source_snapshot_json",
    }.get(runner_task_kind, f"{runner_task_kind}_json")


def _required_target_fields(probe_id: str) -> list[str]:
    base = ["agent_id", "host_id"]
    if probe_id.startswith("process_"):
        base.append("pid")
    if probe_id == "process_trace_endpoint_profile":
        base.extend(["service_id", "instance_id"])
    if probe_id in {"process_source_mechanism_query", "process_source_snapshot"}:
        base.extend(["source_root", "repo_revision"])
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
        "process_python_heap_profile": [
            "Memray 是否观测到持续分配或保留分配热点",
            "分配栈能否定位到 Python 函数和源码行",
        ],
        "process_go_heap_profile": [
            "Go pprof heap 是否观测到分配或 in-use 热点",
            "热点是否能产生可由 source_snapshot 验证的源码行候选",
        ],
        "process_source_snapshot": [
            "采样行是否属于已验证的源码 revision",
            "候选行附近源码能否支持或反驳当前机制假设",
        ],
        "process_source_mechanism_query": [
            "已验证源码行如何经跨函数调用、容器写入或代码生成形成故障机制",
            "当前机制候选被 CodeQL path 支持、反驳还是仍未知",
        ],
        "process_python_heap_reference": [
            "运行时对象由哪些 root、function、code object 或 container 实际持有",
            "源码机制候选是否与真实入向引用链一致",
        ],
        "process_python_lock_wait_profile": [
            "Python 等待栈是否稳定指向锁、队列、Future 或同步原语",
            "是否存在业务等待点、持有者候选或可继续 source_snapshot 验证的 file:line",
        ],
        "process_python_exception_profile": [
            "异常窗口内是否存在重复 traceback 或异常簇",
            "异常是否能定位到业务抛出点、捕获点或日志点",
        ],
        "process_python_queue_profile": [
            "Celery/RQ/asyncio 是否存在队列堆积、active/reserved task 或 broker 异常",
            "堆积是否能回连到任务函数和 worker 运行时栈",
        ],
        "process_python_pool_profile": [
            "是否存在连接池耗尽、checkout/acquire 等待或长持有线索",
            "池等待是否能定位到业务 acquire/wait 站点",
        ],
        "process_python_retry_timeout_profile": [
            "是否存在重复 attempt、retry、timeout 或 backoff 事件",
            "重试或超时是否与同一依赖、endpoint 和异常窗口相关",
        ],
        "process_python_cache_profile": [
            "是否存在缓存体积、文件数或 key 基数增长",
            "缓存增长是否能回连到 key 构造、后端或淘汰边界",
        ],
        "process_python_input_profile": [
            "是否存在输入规模、基数、倾斜或慢路径操作线索",
            "输入慢路径是否能回连到 Python 函数或源码行候选",
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
        "process_runtime_control_history": [
            "目标停止、终止、暂停或资源限制变化是否由同窗控制动作触发",
            "谁在何时通过信号、systemd、容器运行时、cgroup 或控制平面作用于目标",
        ],
    }.get(probe_id, ["补充该注册采集器对应的结构化证据"])
