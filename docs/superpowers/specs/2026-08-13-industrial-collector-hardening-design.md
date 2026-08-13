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
