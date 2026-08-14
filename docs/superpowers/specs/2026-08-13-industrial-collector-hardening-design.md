# 工业采集链路加固设计

**日期**：2026-08-13  
**方案**：B：分层收口 + 工业化 Off-CPU 升级  
**范围**：`log_scan`、`trace/perf/eBPF`、`off_cpu_wait_profile`、诊断采集预算

## 1. 目标

修复真实 `OB-SINGLE-REDIS-001` 暴露的四个问题：

1. 日志窗口为空时仍然能够生成可引用的 `log_window_json`。
2. Trace/栈采样权限不足时能够准确区分工具缺失、权限阻断、目标退出和 Trace 源缺失。
3. 将 Off-CPU 从单一 `bpftrace sched_switch` 脚本升级为分层工业化采集链路。
4. 将总采集时长预算提高到 `180s`，同时为 AI 树 follow-up 保留预算。

本次不把失败结果伪装成成功，也不把“没有采到样本”解释为“没有异常”。

## 2. 总体链路

```text
诊断目标 / Watch target_config
  -> collector_invocation
  -> Agent capability preflight
  -> industrial collector
  -> structured artifact
  -> Evidence Structurer
  -> AI tree / Evidence-to-Attribution
  -> audit bundle / frontend
```

预算逻辑：

```text
总预算 180s
  = 前置采集预算
  + follow-up 保留预算
```

`all_registered` 只表示已注册探针可以免逐项审批，不绕过总时长、并发、风险和 capability 门禁。

## 3. Log Scan

### 3.1 来源优先级

```text
target_config.log_paths/source_paths
  -> invocation options
  -> Agent managed pipeline output
  -> container Docker JSON log fallback
```

### 3.2 结果语义

`log_window_json` 必须始终包含：

- `source_status`
- `window_start`
- `window_end`
- `window_records`
- `matched_records`
- `error_cluster_count`
- `readable_paths`
- `corrupt_paths`
- `fallback_source`

状态定义：

| 状态 | 含义 | 任务结果 |
|---|---|---|
| `readable` | 读取到日志记录 | 成功 |
| `empty_window` | 日志源可读，但窗口内没有记录 | 成功 |
| `no_error` | 窗口内有记录，但没有错误簇 | 成功 |
| `source_missing` | 没有可用日志源 | 结构化不可用 |
| `corrupt_input` | 输入存在但无法解析 | 失败 |

## 4. Trace / perf / eBPF

### 4.1 能力检查

Agent 在 CollectorProfile 和任务执行前检查：

- `perf`、eBPF profile 工具是否安装；
- `perf_event_paranoid`；
- `CAP_PERFMON`、`CAP_BPF`、`CAP_SYS_PTRACE`、`CAP_SYS_ADMIN`；
- `pid: host`；
- seccomp/privileged 配置；
- 目标 PID 是否仍存在；
- OTel/SkyWalking Trace 源是否可读。

### 4.2 结构化阻断

`trace_endpoint_profile_json` 即使采样被阻断也必须生成：

```json
{
  "correlation_status": {
    "status": "blocked",
    "max_supported_level": "function",
    "blocked_reason": "permission_denied"
  },
  "capability_check": {
    "perf_event_paranoid": 3,
    "missing_capabilities": ["CAP_PERFMON"],
    "missing_tools": [],
    "repair_action": "..."
  }
}
```

## 5. Industrial Off-CPU v2

### 5.1 四层采集

#### 事件层

使用 eBPF/bpftrace 采集：

- `sched_switch`
- `sched_wakeup`
- TID/PID
- CPU
- 离开 CPU 时间
- 恢复运行时间
- 线程状态

事件层只回答“线程等待了多久”，不直接声称业务原因。

#### 原因层

根据可用能力选择专项探针：

- `futex` / mutex：锁竞争；
- block I/O：磁盘或块设备等待；
- TCP/网络 syscall：网络读写、连接和下游等待；
- syscall：read/write/recv/send/poll/epoll；
- 调度事件：调度延迟。

原因层输出 `cause_kind`、`cause_source` 和 `cause_confidence`。

#### 栈层

同时保留：

- 用户态栈；
- 内核态栈；
- 原始事件；
- 符号化状态；
- 栈展开失败原因。

“有等待事件但无用户态栈”不能再被归类为“无等待事件”。

#### 回连层

将等待事件按时间、PID/TID、实例、endpoint、Trace/Span 进行回连，输出：

- `service_id`
- `instance_id`
- `function`
- `endpoint`
- `trace_id`
- `call_path`
- `correlation_method`
- `confidence`

### 5.2 采集策略

```text
工业 eBPF 能力可用
  -> 事件层 + 原因层 + 用户/内核栈
能力部分可用
  -> 保留事件层，标记缺失层
bpftrace 不可用但已有滚动快照
  -> 使用快照结构化证据
全部不可用
  -> 生成 blocked/unavailable artifact
```

不使用 `perf record` 冒充 Off-CPU。`perf` 只作为 Trace 复合采集的 on-CPU 栈来源。

### 5.3 结果状态

| 状态 | 含义 |
|---|---|
| `completed` | 有等待事件、原因或栈证据 |
| `empty_window` | 探针正常运行，但窗口内没有满足条件的等待事件 |
| `partial` | 有事件或原因，但缺少完整栈/回连 |
| `blocked` | 权限、工具或内核能力阻断 |
| `target_exit` | 目标进程在采集期间退出 |

### 5.4 结构化产物

主产物为 `off_cpu_wait_json`，包含：

- `event_summary`
- `cause_summary`
- `top_wait_stacks`
- `kernel_stacks`
- `thread_wait_summary`
- `syscall_wait_summary`
- `correlation`
- `capability_check`
- `evidence_window`
- `parser_status`

## 6. 预算

### 6.1 总预算

默认 `max_total_probe_cpu_seconds=180`。

### 6.2 Follow-up 保留

总预算中保留固定 follow-up 额度。前置任务只能消耗：

```text
180s - follow_up_reserve_seconds
```

当 AI 树请求 `trace_endpoint_profile`、`off_cpu_wait_profile`、`cpu_profile` 或 `baseline_window_profile` 时，优先使用保留额度。

预算拒绝事件必须记录：

- `budget_phase`：`initial` 或 `followup`
- `used_seconds`
- `limit_seconds`
- `reserved_seconds`
- `requested_seconds`
- `next_action`

## 7. 验收标准

1. 空日志窗口可生成 `log_window_json`，不再因空输出直接失败。
2. Trace 权限不足时，报告包含 `perf_event_paranoid`、缺失能力和修复动作。
3. Off-CPU 真实任务至少能区分 `empty_window`、`partial`、`blocked`、`target_exit`。
4. Off-CPU 结果包含事件、原因、栈和回连层的结构化状态。
5. 任何有等待事件但栈为空的结果都不会被错误标记为“无等待”。
6. 默认总采集预算为 `180s`，follow-up 仍有可用保留额度。
7. 本地测试、readiness gate 和真实 `OB-SINGLE-REDIS-001` 均可验证上述行为。

## 8. 方案 B 当前部署边界

当前方案 B 的验收面是三台 Docker VM：

```text
control  -> Mini-Drop Server / Web / MinIO / Postgres
worker1  -> Agent + Fluent Bit + Blackbox Exporter + optional Redis Exporter
worker2  -> Agent + Fluent Bit + Blackbox Exporter + optional Redis Exporter
```

本轮要求完成的是：

- 工业采集器适配层真实运行；
- 所有采集结果进入结构化 artifact；
- AI 树能消费结构化证据并输出具体定位边界；
- 持续监视 Agent 能通过 WatchRuntime 领取多个 watch；
- 真实 Redis case 和 Persistent Watch 都能在 Docker VM 环境中闭环。

本轮不把 Kubernetes、SkyWalking Rover DaemonSet 或 CRI/containerd 迁移标记为已完成。

### 8.1 VM 部署与测试命令

方案 B 的真实验收必须先把当前工作树同步到三台 VM，再重建必要容器：

```powershell
$env:MINI_DROP_VM_PASSWORD='<本机设置，不写入仓库>'
python docs\ai_ops_v2_test\scripts\deploy_scheme_b_vm.py
```

部署脚本要求：

- 优先使用远端 `/home/<user>/mini-drop-active`，不存在时才回退到 `/home/<user>/mini-drop`；
- 上传后规范化 `proto/compile.sh` 的换行和执行权限；
- 只同步方案 B 相关文件，不依赖远端 `git pull` 合并状态；
- Control 仅重建 `server/web`，Worker 仅重建 `agent/fluent-bit/blackbox-exporter/otel-collector/redis-exporter`。

Redis case 真实验收命令：

```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$env:MINI_DROP_VM_PASSWORD='<本机设置，不写入仓库>'

python docs\ai_ops_v2_test\scripts\run_ai_ops_v2_vm.py `
  --cases OB-SINGLE-REDIS-001 `
  --repetitions 1 `
  --seed 20260811 `
  --budget-profile development `
  --auto-execute-policy all_registered `
  --output-dir "reports\eval\ai-ops-v2\scheme-b-redis-$ts"
```

Persistent Watch 真实验收命令：

```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$env:MINI_DROP_API_KEY='<本机设置，不写入仓库>'

python docs\ai_ops_v2_test\scripts\test_persistent_watch_vm.py `
  --agent-id linux-worker-2 `
  --process-query cartservice `
  --service-id cartservice `
  --analyze `
  --output "reports\eval\ai-ops-v2\watch-smoke-$ts.json"
```

上述 Watch smoke 只证明 Watch API、lease、冻结窗口、incident、结构化 snapshot 和 AI 树入口闭合；Agent 自动发现真实偏移还需要部署后观察持续运行日志和自动 incident。

### 8.2 自动分析终态

Watch 的自动分析不能依赖调用方无限等待。一次分析必须在有界时间内落入以下终态之一：

```text
analyzed
  -> 已生成 AI 树结果

needs_evidence
  -> 已生成当前边界和 next evidence 请求

analysis_failed
  -> 分析超时或 Provider/解析失败，但冻结证据仍保留，可人工重试
```

默认配置为：

```text
MINI_DROP_WATCH_ANALYSIS_TIMEOUT_SEC=150
MINI_DROP_RCA_LLM_TIMEOUT_SEC=45
```

每次分析带有独立 `analysis_attempt_id`。迟到的后台结果不能覆盖已经持久化的 `analysis_failed` 或更新一轮的结果；自动回灌不会重复启动已经失败的 incident，人工点击分析才是重试入口。

## 9. Persistent Watch 闭环

WatchRuntime 是方案 B 的持续观察入口，不替代 AI 树，也不做根因判断。

```text
用户/系统创建 WatchSubscription
  -> Server 生成 WatchLease
  -> Agent 通过 WatchRuntime.Sync 拉取多个 lease
  -> Agent 按每个 lease 的 target_pid 低成本采样
  -> Agent 回传 baseline/trigger metric windows
  -> Server 复用 Persistent Trigger
  -> 冻结 rolling snapshot
  -> 生成 trigger_event / evidence_cohort / collector_tasks
```

设计约束：

- Watch 同步与普通任务心跳分离；
- Agent 执行深度采集时仍然继续 watch sync；
- 同一 Agent 可同时监视多个 PID；
- PID 由 lease 下发，不使用 Agent 注册期的静态配置；
- 同一持续偏移窗口不重复刷出相同 incident；
- 手动评估仍允许多次异常保留历史 incident；
- Watch 触发的采集任务复用 `collector_invocation`，避免多个目标串配置。
- Watch delayed follow-up 回灌按 `task_id` 保留多条独立证据，不允许后续补证覆盖已有补证。
- 最新真实 Watch case：`reports/eval/ai-ops-v2/watch-scheme-b-20260814-113744.json`，结果为 `needs_evidence` 有界终态，已验证自动分析持久化和 3 条 delayed follow-up 证据保留。
- 当前 `needs_evidence` 的原因是 Worker 宿主机 `kernel.perf_event_paranoid=4` 阻断 CPU perf，且 OTel/SkyWalking Trace 窗口为空；AI 树必须保留 function 边界，不能升级成 endpoint/call_path 或代码行级结论。
- Agent 自动观察已通过真实 VM 验证：`reports/eval/ai-ops-v2/watch-agent-observe-20260814-121400.json` 使用 `watch_cpu_shift_v1` 延迟异常 fixture，让 Agent 自己采样、触发 `cpu_shift`、生成同窗 collector tasks、执行 AI 树，并在分析后自动停用测试 watch。
- 同一持续异常窗口必须 suppress 重复 trigger；实现上同类 trigger 持续存在时保留 active trigger 状态，不再每轮创建新 incident，指标恢复后才允许下一次同类 trigger。
- Watch smoke 脚本必须清理自身创建的 fixture 和 watch，防止测试遗留订阅继续生成 incident、delayed follow-up 和任务风暴。

## 10. Kubernetes 后续迁移路线

Kubernetes 是方案 B 之后的独立升级分支，不阻塞当前 Docker VM 验收。

```text
当前 Docker VM 方案 B
  -> Environment Backend 抽象
  -> Kubernetes Backend
  -> OTel Collector DaemonSet
  -> 工业 Profile Producer / SkyWalking Rover DaemonSet
  -> CRI/containerd PID resolver
  -> CNI-aware dependency probing
  -> Kubernetes fault injection
  -> 同一 Case/Oracle 双环境评测
```

### 10.1 Environment Backend

把当前测试脚本中的 Docker/Swarm 操作抽象为环境接口：

- 部署 workload；
- 注入故障；
- 查询目标实例；
- 解析 service/instance/pid/container；
- 回滚环境；
- 拉取采集 artifact。

这样同一测试集可以在 Docker VM 与 Kubernetes 中复用 case/oracle，而不是把测试逻辑写死在某个运行环境里。

迁移时必须先把当前 Docker/Swarm runner 的能力抽成接口，至少包含：

- `resolve_repo_root`
- `deploy_workload`
- `inject_fault`
- `resolve_scope`
- `prepare_collectors`
- `cleanup_environment`
- `collect_audit_bundle`

当前 `deploy_scheme_b_vm.py` 和 `run_ai_ops_v2_vm.py` 仍属于 Docker VM backend，不是 Kubernetes backend 的部分实现。

### 10.2 OTel Collector DaemonSet

Kubernetes 环境中，每个节点部署 OTel Collector：

- 接收 OTLP trace/log/profile；
- 写入本地或对象存储 spool；
- Mini-Drop Agent 只读取结构化前的本地 spool；
- Evidence Structurer 再转换为 `trace_endpoint_profile_json`、`log_window_json` 和证据索引。

### 10.3 SkyWalking Rover DaemonSet

Rover 作为后续升级目标，用于补强：

- eBPF 网络边；
- 服务拓扑；
- L7 协议摘要；
- continuous profiling；
- 进程到服务实例归属。

当前 Docker VM 方案 B 不声称已经具备 Rover 能力，只保留可对接的结构化证据契约。

### 10.4 CRI/containerd 与 CNI 适配

Kubernetes 迁移后不能再依赖 Docker JSON 日志路径或 Docker container ID 解析。

需要新增：

- CRI/containerd PID resolver；
- Pod/Container/Node/Service/Endpoint 映射；
- CNI-aware DNS/TCP/HTTP/gRPC dependency probing；
- NetworkPolicy、Service VIP、Headless Service 和 Pod IP 区分。

### 10.5 双环境评测

迁移完成后，同一 case 应在两个环境中运行：

| 环境 | 验收点 |
|---|---|
| Docker VM | 当前方案 B 的工业采集、结构化证据、AI 树闭环 |
| Kubernetes | DaemonSet 采集、CRI 目标解析、CNI 依赖探测、同一 oracle 对比 |

只有两个环境都能产出同一套 evidence family，才认为迁移完成。
