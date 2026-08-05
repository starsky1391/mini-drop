# Mini-Drop 复合异常采集数据

采集日期：2026-07-30  
采集节点：`linux-worker-1`（Ubuntu Linux，4 核）  
目标 PID：`1533`  
场景：CPU 热点、缓存增长、同步日志写入同时发生

## 1. 数据用途

这组数据用于研究以下链路：

```text
受控异常负载
→ Agent 采集
→ 原始 Artifact
→ Analyzer 结构化结果
→ 单任务 AI 归因
```

数据包不依赖 Mini-Drop 前端即可阅读。JSON、SVG 和 TXT 文件都是普通文件，可以用文本编辑器、浏览器或脚本直接处理。

## 2. 异常场景

`source/complex_anomaly_workload.py` 模拟了一个存在三类问题的 Python 服务：

1. 持续创建、排序和 JSON 序列化请求数据，并计算 SHA-256，形成单核 CPU 热点；
2. 每秒向缓存增加约 1 MiB 数据，最大约 96 MiB；
3. 持续写入审计日志并调用 `fsync`，文件达到 64 MiB 后轮转。

进程限定运行 240 秒，结束时自动删除临时 I/O 文件。该脚本不修改系统配置，也不操作 Mini-Drop 服务进程。

## 3. 实际观察结果

系统指标任务捕捉到：

- 进程 CPU 使用约 `1.003 cores`；
- 4 核主机平均用户态 CPU 约 `24.9%`；
- 进程 RSS 约 `120.4 MiB`；
- RSS 变化趋势为 `increasing`；
- RSS 斜率约 `327,999 bytes/s`；
- 进程写入约 `1,508,596 bytes/s`；
- 平均 I/O wait 约 `1.0%`；
- 线程数为 `4`。

Python Profile 捕捉到：

- `cpu_hotspot()` 是主要调用路径；
- 热点覆盖列表构造、排序、JSON 编码和哈希计算；
- 结果为可直接用浏览器打开的 SVG 火焰图。

eBPF I/O 任务捕捉到 `438` 个块设备延迟样本：

| 延迟区间（微秒） | 样本数 |
| --- | ---: |
| 64–128 | 44 |
| 128–256 | 192 |
| 256–512 | 148 |
| 512–1000 | 31 |
| 1000–2000 | 16 |
| 2000–4000 | 5 |
| 4000–8000 | 2 |

内存任务执行时缓存已经接近上限，因此该 30 秒窗口内 RSS 为稳定状态。这与系统指标在更早窗口检测到的增长趋势并不冲突，说明分析时必须考虑采集时间窗。

## 4. 目录结构

```text
.
├── README.md
├── DATA_DICTIONARY.md
├── collection_summary.json
├── manifest.json
├── SHA256SUMS.txt
├── source
│   ├── complex_anomaly_workload.py
│   └── workload.log
├── tasks
│   ├── 01_sys_metrics
│   │   ├── task.json
│   │   ├── events.json
│   │   ├── artifacts.json
│   │   └── sys_metrics.json
│   ├── 02_memory_smaps
│   │   ├── task.json
│   │   ├── events.json
│   │   ├── artifacts.json
│   │   └── memory_profile.json
│   ├── 03_python_profile
│   │   ├── task.json
│   │   ├── events.json
│   │   ├── artifacts.json
│   │   └── pyspy.svg
│   └── 04_ebpf_io
│       ├── task.json
│       ├── events.json
│       ├── artifacts.json
│       ├── ebpf_metrics.json
│       └── io_latency.txt
└── analysis
    ├── diagnose_response.json
    └── diagnosis_detail.json
```

每个任务目录中的文件含义：

- `task.json`：任务参数、状态、采集器、PID 和时间；
- `events.json`：任务状态变化历史；
- `artifacts.json`：服务端登记的 Artifact 元数据、对象键、大小和 SHA-256；
- 其余文件：Agent 实际上传的采集产物。

## 5. 任务 ID

| 采集器 | Task ID | 状态 |
| --- | --- | --- |
| `sys_metrics` | `task_20260730_035620_603d19` | DONE |
| `memory_smaps` | `task_20260730_035620_13b667` | DONE |
| `pyspy` | `task_20260730_035620_b06fb0` | DONE |
| `ebpf_io` | `task_20260730_035620_7073b7` | DONE |

曾创建一个原生 `perf_cpu` 任务，但为了保证同一 PID 在有限负载窗口内完成关键采集，该任务在开始前被取消，没有把空任务作为有效数据放入数据包。

## 6. 单任务 AI 分析结果

`analysis/diagnosis_detail.json` 是对 `sys_metrics` 任务执行单任务智能归因后的完整结果。

AI 识别到：

- 进程约占用一个 CPU 核；
- RSS 存在增长；
- 写入约 1.5 MB/s。

最终结论仍是 `insufficient_data`，置信分数为 `0.33`。这是合理结果，不是数据采集失败：

- 单任务归因只读取 `sys_metrics` 任务及其 Artifact；
- 它不会自动合并另外三个独立 Task 的 py-spy、eBPF 和内存 Artifact；
- 因此报告明确指出缺少火焰图、eBPF 延迟和基线对比。

这说明“采集数据格式是否通用”和“当前 AI 链路是否会自动关联这些数据”是两个问题。文件格式可以复用，但跨任务证据需要额外的聚合层、统一时间窗和 Evidence 引用。

## 7. 如何查看

### JSON

可以直接使用：

```bash
python -m json.tool tasks/01_sys_metrics/sys_metrics.json
python -m json.tool tasks/04_ebpf_io/ebpf_metrics.json
```

### 火焰图

用 Chrome、Edge 或 Firefox 打开：

```text
tasks/03_python_profile/pyspy.svg
```

火焰图中的方块宽度代表采样占比，点击方块可放大调用路径。

### eBPF 原始输出

`tasks/04_ebpf_io/io_latency.txt` 是 bpftrace 原始文本，`ebpf_metrics.json` 是 Mini-Drop Analyzer 归一化后的直方图。

### 完整性校验

Linux：

```bash
sha256sum -c SHA256SUMS.txt
```

Windows PowerShell 可使用：

```powershell
Get-FileHash .\tasks\01_sys_metrics\sys_metrics.json -Algorithm SHA256
```

## 8. 数据能否通用

可以通用的部分：

- JSON、SVG 和 TXT 的文件格式；
- CPU、RSS、I/O、调用栈和延迟直方图的分析思想；
- Task、Event、Artifact、Evidence 的链路设计；
- SHA-256 完整性校验方法。

需要适配的部分：

- `sys_metrics.v2` 等字段命名属于 Mini-Drop Schema；
- 其他系统需要编写 Adapter 将自己的字段映射到统一 Evidence；
- eBPF 原始输出受内核版本、tracepoint 和 bpftrace 脚本影响；
- py-spy、perf、pprof 的原始格式不同，不能只靠修改文件扩展名互换；
- PID、主机、Boot ID 和采集时间窗属于当时环境，不能作为另一个环境的现场证据；
- 不同 Task 的 Artifact 不会天然成为同一个诊断上下文。

## 9. 安全说明

数据包不包含：

- Mini-Drop API Key；
- AI Provider Key；
- SSH 密码；
- gRPC Token；
- MinIO 访问密钥。

数据仅包含实验主机标识、任务 ID、PID、采集时间和受控异常产生的性能数据。
