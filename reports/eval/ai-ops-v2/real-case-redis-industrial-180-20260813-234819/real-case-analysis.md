# OB-SINGLE-REDIS-001 真实 Case 验证报告

## 1. 运行信息

- Case：`OB-SINGLE-REDIS-001`
- Fixture：`redis_pause_v1`
- 运行时间：`2026-08-13 23:48:19`（本地命名）
- 诊断 ID：`diag_session_20260813_154841_fc164ef2`
- 预算配置：`development`
- 自动执行策略：`all_registered`
- 总采集预算：`180s`
- 初始采集预算：`120s`
- Follow-up 保留预算：`60s`
- 诊断耗时：`91.25s`
- 测试脚本耗时：`106.81s`

## 2. 总体结果

| 项目 | 结果 |
|---|---|
| 真实 VM 执行 | 通过 |
| 诊断任务创建 | 通过 |
| 子任务创建 | 通过，8 个 |
| Artifact 上传 | 通过，8 个 |
| 结构化证据 | 通过 |
| Evidence refs | 通过，22 个 |
| Readiness gate | 通过，10/10 |
| 诊断终态 | `PARTIAL_COMPLETED` |
| 故障回滚 | 通过 |
| 回滚后 Online Boutique | 健康，HTTP 200 |

Readiness gate 输出位于：

`readiness_gate.json`

## 3. 已验证的真实证据

### 3.1 Redis 依赖证据

真实生成：

- `dependency_check_json`
- `redis_check_json`

Redis 诊断结论明确指向：

```text
根因优先指向 Redis 下游依赖 redis-cart：
Redis ping/exporter 可达性失败。
```

该结论不是由固定文本直接生成，而是基于真实 Redis 专项 artifact、依赖探测和同窗诊断证据形成。

### 3.2 Off-CPU 工业采集

真实生成：

- `off_cpu_wait_json`
- `bpftrace_offcpu.txt`

采集到：

- 目标进程：`cartservice` PID `20061`
- 等待样本：`58`
- 总等待时间：约 `29008.35ms`
- 等待原因：`interruptible_sleep_or_lock_wait`
- 等待地址：`0x758465027fac`

当前报告已经具体定位到：

```text
cartservice-worker2 PID 20061 的等待路径
```

但因为等待栈顶部仍然是未符号化地址，暂时不能升级到函数名。

## 4. AI 树和预算验证

本次 Follow-up 确实被 AI 树触发并执行：

```text
trace_endpoint_profile
```

这证明：

1. 初始采集没有直接终止整个诊断；
2. AI 树可以继续提出深度证据请求；
3. `all_registered` 能自动跳过逐项审批；
4. Follow-up 预算机制已经进入实际运行链路；
5. 诊断没有出现 `BUDGET_EXHAUSTED`。

本次结果仍为 `PARTIAL_COMPLETED`，原因不是预算耗尽，而是部分深度采集任务失败，报告保留了：

```text
trace_endpoint_profile
baseline_window_profile
log_scan
cpu_profile
```

作为后续证据请求。

## 5. 本次失败项

### 5.1 `log_scan`

任务：

`task_20260813_154841_7ff4b6`

状态：`FAILED`

真实 bundle 中该任务的 `target_config` 只有：

```json
{
  "log_paths": []
}
```

缺少当前工作区代码已经支持的目标上下文和日志回退信息，例如：

- `container_id`
- `source_paths`
- `service_id`
- `instance_id`

因此本次不能判断为“日志窗口内没有错误”，只能判断为部署侧没有把完整日志目标配置传入采集器。

### 5.2 `trace_endpoint_profile`

任务：

`task_20260813_155002_f5fe48`

状态：`FAILED`

真实 bundle 中 Follow-up 的 `target_config` 为空：

```json
{}
```

这与当前工作区的预期不一致。当前代码应传入：

- `pid`
- `service_id`
- `instance_id`
- `host_id`
- `endpoint`
- `trace_paths`
- `container_id`

因此本次不能直接把失败归因于 `perf_event_paranoid` 或 eBPF capability。首先需要排除 control/server 和 worker agent 没有部署当前版本的问题。

## 6. 部署一致性判断

本次真实运行使用的是：

```text
本机测试脚本
  -> 远端 Control API
  -> 远端 Worker Agent
```

测试脚本本身会连接远端环境，但不会自动把当前工作区的 server/agent 代码部署到 VM。

从真实 bundle 的字段看，远端运行版本至少存在以下疑似落后：

1. Follow-up `trace_endpoint_profile` 没有完整 `target_config`；
2. `log_scan` 没有收到 `container_id` 和 source path fallback 配置；
3. 深度采集失败结果没有体现当前版本应输出的完整结构化阻断证据。

所以本次结果应解释为：

```text
当前远端部署链路已经能够真实执行方案 B 的主流程，
但尚未证明远端已经部署了本地工作区的最新 Trace/Log 配置补丁。
```

## 7. 结论

### 已确认

- 真实 Redis 故障可以被采集器触发并复现；
- `dependency_check` 和 `redis_check` 可以产出结构化证据；
- 工业 Off-CPU eBPF/bpftrace 可以产出真实等待事件；
- AI 树可以根据证据继续触发 Follow-up；
- `180s` 总预算和 `60s` Follow-up 保留没有阻断诊断；
- 审计包和 readiness gate 链路通过；
- 故障注入后的回滚成功。

### 尚未确认

- 最新 `log_scan` 目标配置是否已部署到远端；
- 最新 `trace_endpoint_profile` 的 eBPF/perf/OTel/SkyWalking 复合链路是否已部署到远端；
- 真实 Trace 是否能回连到 endpoint/call_path；
- 真实 Off-CPU 用户态栈是否能完成符号化。

## 8. 下一次真实验证前置动作

1. 将当前工作区的 server、agent 和 web 重新构建并部署到三台 VM。
2. 重启远端 server/agent，使新版本生效。
3. 检查远端任务的 `collector_invocation.target_config`：
   - `log_scan` 必须包含 `container_id` 或有效 `source_paths`；
   - `trace_endpoint_profile` 必须包含 `pid`、实例上下文和 trace 路径。
4. 重新运行同一个 `OB-SINGLE-REDIS-001`。
5. 只有在新 bundle 同时包含以下结果后，才关闭方案 B 的真实验证任务：
   - `log_window_json`
   - `trace_endpoint_profile_json`
   - `off_cpu_wait_json`
   - 具体 endpoint/call_path 或明确的 capability 阻断证据。

