# 数据字段说明

## sys_metrics.json

Schema：`sys_metrics.v2`

| 字段 | 含义 |
| --- | --- |
| `task_id` | Mini-Drop 任务 ID |
| `mode` | 采集模式 |
| `duration_sec` | 采集持续时间 |
| `sample_count` | 样本数量 |
| `host` | 主机静态信息 |
| `process` | 目标进程静态信息 |
| `container` | 容器或 Cgroup 信息，没有时为空 |
| `summary` | Analyzer 生成的窗口摘要 |
| `samples` | 按时间排列的原始采样点 |

`summary` 中的重点字段：

| 字段 | 单位 | 含义 |
| --- | --- | --- |
| `avg_cpu_user_pct` | % | 主机用户态 CPU 平均值 |
| `avg_cpu_sys_pct` | % | 主机内核态 CPU 平均值 |
| `avg_cpu_iowait_pct` | % | 主机 I/O wait 平均值 |
| `process_cpu_core_usage` | CPU core | 目标进程使用的 CPU 核数 |
| `load1m` | 无 | 采集结束时 1 分钟负载 |
| `vmrss_mb` | MiB | 目标进程 RSS |
| `vmrss_slope_bytes_per_second` | byte/s | RSS 线性变化斜率 |
| `process_write_bytes_per_second` | byte/s | 目标进程写入速率 |
| `thread_count` | 个 | 目标进程线程数 |
| `fd_count` | 个 | 目标进程文件描述符数 |

## memory_profile.json

| 字段 | 含义 |
| --- | --- |
| `pid` | 目标进程 PID |
| `sample_count` | smaps 采样数量 |
| `first_rss_mb` | 窗口首个 RSS |
| `last_rss_mb` | 窗口末尾 RSS |
| `peak_rss_mb` | 窗口峰值 RSS |
| `trend` | 当前窗口增长、下降或稳定 |
| `samples` | RSS、PSS、Swap 等时间序列 |

不同采集窗口可能得到不同趋势。趋势只描述本文件覆盖的窗口，不能脱离时间范围外推。

## pyspy.svg

标准 SVG 火焰图，由 py-spy 对 Python 进程采样生成。

- 横向宽度：调用栈采样占比；
- 纵向层级：调用深度；
- `<title>`：函数名、文件、行号、样本数和占比；
- 本数据中的主要函数为 `cpu_hotspot`。

## ebpf_metrics.json

| 字段 | 含义 |
| --- | --- |
| `io_latency_us` | 以微秒为单位的延迟桶和样本数量 |
| `total_samples` | 总 I/O 延迟样本数 |

例如：

```json
{
  "io_latency_us": {
    "[128, 256)": 192
  },
  "total_samples": 438
}
```

表示有 192 个样本位于 128–256 微秒区间。

## io_latency.txt

bpftrace 原始输出。它保留原采集器产生的数据，便于：

- 验证 Analyzer 是否正确归一化；
- 使用其他解析器重新处理；
- 比较不同内核或脚本输出差异。

## task.json

记录任务的请求参数和最终状态。重点字段：

- `id`；
- `name`；
- `agent_id`；
- `target_pid`；
- `collector_type`；
- `sample_rate`；
- `duration_sec`；
- `status`；
- `status_reason`；
- `created_at`、`started_at`、`finished_at`。

## events.json

记录任务从创建到结束的状态转换，可用于分析：

- 排队时间；
- Worker 拉取时间；
- 采集持续时间；
- 失败或取消原因。

## artifacts.json

记录服务端 Artifact 元数据：

- `artifact_type`；
- `bucket`；
- `object_key`；
- `filename`；
- `content_type`；
- `size_bytes`；
- `sha256`；
- `availability`；
- `retention_state`。

数据包中的实际 Artifact 已再次计算 SHA-256，并与这里的登记值比较。

## diagnosis_detail.json

包含单任务归因的：

- `run`；
- `tool_results`；
- `report`；
- `repair_plans`；
- `feedback`。

报告中的 `evidence_refs` 是逻辑证据路径，不等同于本地文件路径。要实现跨任务诊断，需要先将各 Task Artifact 归一化为统一 Evidence，再让报告引用对应 Evidence ID。
