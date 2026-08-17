# OB-SINGLE-REDIS-001 部署后真实验证报告

## 1. 运行信息

- Case：`OB-SINGLE-REDIS-001`
- Fixture：`redis_pause_v1`
- 代码提交：`684ed0f`
- 诊断 ID：`diag_session_20260813_161020_40b9211c`
- 预算配置：`development`
- 自动执行策略：`all_registered`
- 总采集预算：`180s`
- 初始采集预算：`120s`
- Follow-up 保留预算：`60s`
- 诊断耗时：`160.53s`
- 结果目录：`real-case-redis-industrial-180-deployed-20260814-000948`

## 2. 部署验证

三台 VM 均已完成：

1. 推送并拉取提交 `684ed0f`。
2. 原有未提交改动保存到 Git stash。
3. 本地 `deploy/env` 和证书目录备份后恢复。
4. Control 的 `server/web` 重新构建。
5. Worker1、Worker2 的 Agent 和采集 sidecar 重新构建。
6. 容器内核验到 `180s` 预算、bpftrace Off-CPU 和日志容器回退代码。

## 3. 总体结果

| 项目 | 结果 |
|---|---|
| 真实 VM 执行 | 通过 |
| Readiness Gate | 通过，10/10 |
| 子任务 | 10 个 |
| Artifact | 14 个 |
| Evidence refs | 29 个 |
| 初始采集 | 全部完成 |
| AI 树 Follow-up | 已触发并执行 |
| 故障回滚 | 通过 |
| 回滚后 Online Boutique | 健康，HTTP 200 |
| 诊断终态 | `PARTIAL_COMPLETED` |

Readiness Gate：

`readiness_gate.json`

审计包：

`bundles/OB-SINGLE-REDIS-001__r01.json`

## 4. 已验证的工业采集结果

### 4.1 Log Scan

`process_log_scan` 已完成并生成：

- `log_window_json`

真实结果：

- `total_records_read=10000`
- `window_records=0`
- `matched_records=0`
- `error_cluster_count=0`
- `source_status=readable`

这次可以明确区分：

```text
日志源可读，但当前 15 秒证据窗口内没有记录或错误簇。
```

不再是采集器失败，也不再是日志源缺失。

### 4.2 Off-CPU Industrial v2

`process_off_cpu_profile` 已完成并生成：

- `off_cpu_wait_json`
- `bpftrace_offcpu.txt`

能力检查：

- `bpftrace=true`
- `perf=true`
- `missing_capabilities=[]`
- `target_present=true`
- `perf_event_paranoid=4`

事件结果：

- `observed_wait_events=130`
- `events_without_user_stack=130`
- `events_without_kernel_stack=130`
- 主要等待线程总等待时间约 `28010.93ms`
- Top frame：`0x758465027fac`

因此工业事件层已经真实工作，但栈层仍未闭合。报告正确停留在未符号化地址，而不是伪造函数名。

### 4.3 Trace Endpoint Profile

AI 树 Follow-up 已真实创建并执行：

`process_trace_endpoint_profile`

生成：

- `ebpf_profile_raw`
- `trace_endpoint_profile_json`

Trace 栈采样结果：

- `stack_source.kind=ebpf`
- `stack_source.status=completed`
- `missing_capabilities=[]`
- `target_present=true`

但 Trace 关联结果为：

- `trace_source.status=unavailable`
- `trace_source.blocked_reason=trace_source_missing`
- `endpoint_bindings=[]`
- `call_path_hotspots=[]`
- `correlation_status.status=partial`
- `max_supported_level=function`

这说明 eBPF 栈采样本身已启动成功，但当前 Worker 没有可读取的 OTel/SkyWalking Trace 输出目录，因此不能把栈稳定回连到 Endpoint 或 Call Path。

### 4.4 Baseline Follow-up

`process_baseline_window` 已完成，生成两个连续窗口和摘要：

- 原始 `perf.data` 窗口：2 个
- 结构化窗口：0 个
- 原因：`perf script` 未产出可解析栈文本

这说明连续采集窗口存在，但当前目标进程的 perf 栈解析仍没有形成结构化函数证据。

## 5. 诊断结论

当前真实结论为：

```text
根因优先指向 Redis 下游依赖 redis-cart：
Redis ping/exporter 可达性失败。

同窗 Off-CPU 捕获到 cartservice PID 20061 的等待地址
0x758465027fac，等待原因是 interruptible_sleep_or_lock_wait，
样本数 56，总等待约 28010.93ms。
```

当前定位边界：

```text
redis-cart：service 级
cartservice：process 级
等待路径：未符号化地址级
Endpoint/Call Path：尚未回连
具体函数：尚未确认
```

这不是泛化结论，而是当前证据真正支持的最深层级。

## 6. 仍未闭合的问题

### 6.1 CPU Profile 被系统权限阻断

失败任务：

`task_20260813_161145_27b093`

失败原因：

```text
perf_event_paranoid 权限不足
```

虽然 Agent 容器具备：

- `privileged`
- `pid: host`
- `PERFMON`
- `BPF`
- `SYS_PTRACE`
- `SYS_ADMIN`

但 Worker 主机的：

```text
kernel.perf_event_paranoid=4
```

仍阻止普通 perf CPU profile。`all_registered` 不能绕过内核 capability 门禁，这是正确行为。

### 6.2 Trace 源没有接入

当前默认路径：

```text
/var/lib/mini-drop/traces
```

该路径没有可读 Trace 文件，因此：

- OTel/SkyWalking Trace 记录数为 `0`
- Endpoint 绑定数为 `0`
- Call Path 热点数为 `0`

### 6.3 用户态和内核态栈没有展开

Off-CPU 已有 `130` 个等待事件，但用户态和内核态栈均为空。需要继续排查：

- bpftrace `ustack/perf` 输出格式；
- 容器内目标进程符号和映射可见性；
- Go 二进制是否包含符号；
- bpftrace 对当前内核和用户态栈展开的兼容性；
- 是否需要使用 CO-RE libbpf 栈采集器替代 bpftrace 栈展开。

## 7. 结论

本次验证可以确认：

```text
方案 B 已从“代码实现”进入真实 VM 工业链路运行阶段。
```

已经真实跑通：

```text
git 同步
-> 三机容器重建
-> 故障注入
-> 结构化日志采集
-> Redis/依赖检查
-> bpftrace Off-CPU
-> eBPF Trace 栈采样
-> AI 树 Follow-up
-> 180s 预算
-> 审计包
-> readiness gate
-> 故障回滚
```

但方案 B 还不能标记为完全闭合，因为：

1. CPU Profile 仍被 Worker 主机 `perf_event_paranoid=4` 阻断。
2. OTel/SkyWalking Trace 源尚未接入 Worker。
3. Off-CPU 等待事件尚未完成用户态/内核态栈展开。
4. Trace 尚未回连到 Endpoint/Call Path。

因此当前 `PARTIAL_COMPLETED` 是准确结果，不是系统误报。

