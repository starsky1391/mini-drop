# Evidence-to-Attribution Analyzer 后续升级路线图

> 本文是 `docs/evidence_to_attribution_analyzer.md` 的后续升级副本。
> 它只展示“已有方案未来如何升级”，不重复已完成方案，也不把当前缺口补齐误写成升级。

## 1. 定义边界

这里先把两个概念分开：

| 概念 | 含义 | 示例 |
|---|---|---|
| 补齐 | 当前能力还没有，需要先接入工业成熟采集来源，并转换成结构化证据 | `log_scan`、`dependency_check`、`redis_check` |
| 升级 | 某个能力已经有工业适配版本，再往更强、更稳定、更自动化方向演进 | 从 Fluent Bit/OTel 日志适配升级到 rolling log buffer；从 Blackbox 主动探测升级到 SkyWalking Rover 风格网络画像 |

所以：

```text
补齐 log_scan / dependency_check / redis_check
```

不算本文的“后续升级”，它们是进入后续升级前的前置补齐项。

本文真正关注的是：

```text
当基础能力存在以后，每个部件还能往哪里升级？
```

## 2. 竖型树形图

这张图按“部件”拆分，不把不同部件强行连成一个阶段链。每条分支内部才表示该部件自己的升级路径。

```mermaid
flowchart TB
    A["后续升级路线图<br/>已有方案完成后的演进方向"]

    A --> P["前置补齐项<br/>不是升级，但会阻塞后续升级"]
    P --> P1["log_scan adapter<br/>Fluent Bit / OTel Collector 日志输出 -> log_window_json"]
    P --> P2["dependency_check adapter<br/>Blackbox Exporter /probe -> dependency_check_json"]
    P --> P3["redis_check adapter<br/>Redis Exporter / 受控快照 -> redis_check_json"]

    A --> C["采集器升级线"]
    C --> C1["任务驱动采集器标准化<br/>所有 collector 输出统一 artifact + evidence_ref"]
    C1 --> C2["Rolling Buffer 采集<br/>低成本持续缓存日志、依赖、指标、轻量栈"]
    C2 --> C3["Triggered Snapshot<br/>异常触发时冻结前后窗口"]
    C3 --> C4["SkyWalking Rover 风格采集<br/>eBPF 网络边、L7 摘要、服务拓扑证据"]

    A --> S["证据结构化升级线"]
    S --> S1["Collector Family 标准化<br/>统一 log_scan / dependency_check / redis_check / runtime_snapshot 证据族"]
    S1 --> S2["Evidence Index v2<br/>支持跨 artifact 引用、局部取证、证据回看"]
    S2 --> S3["Evidence Lake / Snapshot Store<br/>同窗证据、历史证据、延迟补证分层保存"]

    A --> T["AI 树升级线"]
    T --> T1["证据请求闭环增强<br/>根据缺失证据族提出下一步采证"]
    T1 --> T2["冲突裁决增强<br/>处理日志、依赖、栈、指标之间互相矛盾"]
    T2 --> T3["疑难杂症保守诊断<br/>支持多候选、弱结论、不可判断、待复现"]

    A --> G["图层升级线"]
    G --> G1["调用链回连增强<br/>endpoint / trace / service / instance / hotspot 更稳定归属"]
    G1 --> G2["跨窗口证据图<br/>区分 same_window / delayed_followup / stale_window"]
    G2 --> G3["因果图推理<br/>基于拓扑方向、时间先后、证据强度排序候选传播路径"]

    A --> R["Runtime Snapshot 升级线"]
    R --> R1["Runtime Adapter<br/>统一 java_async / go_pprof / pyspy / off_cpu 输出"]
    R1 --> R2["锁竞争与停顿摘要<br/>blocked thread、goroutine wait、GIL、futex、monitor lock"]
    R2 --> R3["代码行级证据增强<br/>在栈、符号、源码映射足够时再升级到 line"]

    A --> W["Persistent Agent 升级线"]
    W --> W1["Watch Lease 持久化<br/>agent 稳定知道自己监视谁"]
    W1 --> W2["多对象低成本观察<br/>同一 agent 管理多个 watch 的预算、并发、冷却"]
    W2 --> W3["自动同窗采证<br/>trigger_event 触发 collector group 并冻结现场"]

    A --> B["Benchmark 升级线"]
    B --> B1["Readiness Gate v2<br/>按 case 需要的证据族检查真实 artifact"]
    B1 --> B2["Case 维度差距分析<br/>区分 AI 误判、采集缺失、结构化缺失、证据引用缺失"]
    B2 --> B3["版本对比评测<br/>稳定性、准确率、证据召回、成本、时间配对比较"]

    A --> F["前端可视化升级线"]
    F --> F1["Readiness Gate 展示<br/>诊断结果是否具备评测资格"]
    F1 --> F2["证据树浏览器<br/>按 evidence_ref 展开日志簇、依赖检查、栈摘要"]
    F2 --> F3["拓扑与传播视图<br/>展示服务链路、异常窗口、候选传播路径"]
```

## 3. 前置补齐项

这些不是升级，但它们会影响后续升级是否有意义。

### 3.1 `log_scan`

作用：

```text
把工业日志采集器输出变成 AI 树能理解的结构化证据。
```

推荐采集来源：

```text
Fluent Bit Tail + Multiline
或 OpenTelemetry Collector filelogreceiver
```

Mini-Drop 不自研日志 tail、多行合并、offset、文件轮转和容器日志兼容能力，只做适配、窗口裁剪、字段归一和证据结构化。

它要产出：

- 日志窗口摘要。
- 错误日志簇。
- 异常关键字和错误类型。
- trace_id / span_id / endpoint / dependency。
- 稳定 `evidence_ref`。

它解决的问题：

```text
如果没有日志证据，Payment、Redis、OOM、Disk、Network、Runtime 类 case 容易只能靠指标猜。
```

### 3.2 `dependency_check`

作用：

```text
把工业探测器结果转换成下游依赖是否可达、慢、拒绝连接、DNS 失败或 gRPC NOT_SERVING 的结构化证据。
```

推荐采集来源：

```text
Prometheus Blackbox Exporter /probe
```

Mini-Drop 不自研 DNS/TCP/HTTP/HTTPS/gRPC 探测器，只负责编排目标、读取 probe metrics、转换成证据 JSON。

它要覆盖：

- DNS resolve。
- TCP connect。
- HTTP status / latency。
- gRPC health check。
- Payment / Checkout / Redis 等关键依赖地址。

它解决的问题：

```text
没有依赖探测时，AI 树很难区分“本服务慢”和“下游不可达”。
```

### 3.3 `redis_check`

作用：

```text
把 Redis Exporter 指标或受控 Redis 快照转换成可诊断对象。
```

推荐采集来源：

```text
Redis Exporter metrics
必要时受控执行 Redis INFO / SLOWLOG GET / LATENCY LATEST 快照
```

Mini-Drop 不自研 Redis RESP 协议采集器。Redis 专项信息必须来自成熟 exporter 或受控命令快照。

它要采：

- `PING`。
- `INFO`。
- `SLOWLOG GET`。
- `LATENCY LATEST`。
- 可选 `LATENCY DOCTOR`。

它解决的问题：

```text
Redis pause、慢命令、连接数异常、内存压力、错误回复增加，不能只靠 TCP connect 判断。
```

## 4. 采集器升级线

这一条线的核心是从“任务触发采一下”升级到“持续低成本观察，并在异常窗口冻结证据”。

### 4.1 任务驱动采集器标准化

这是采集器升级的起点。所有 collector 都应该输出：

- 标准 artifact type。
- 标准 metadata。
- 标准 evidence window。
- 标准 evidence family。
- 标准 evidence_ref。

这样后续无论是日志、依赖、Redis、栈还是网络画像，都能进入同一套证据结构化层。

### 4.2 Rolling Buffer 采集

升级方向：

```text
从“发现问题后再采”
升级为
“低成本缓存最近 N 秒，发现问题时可以回看前后窗口”
```

适合进入 rolling buffer 的数据：

- metrics summary。
- log error summary。
- endpoint latency summary。
- dependency probe summary。
- lightweight stack summary。

### 4.3 Triggered Snapshot

升级方向：

```text
trigger_event 出现时，冻结同一窗口内的多类证据。
```

它解决的是瞬时异常：

```text
异常发生时已经保存现场，后续 AI 树不需要假装 delayed follow-up 能恢复现场。
```

### 4.4 SkyWalking Rover 风格采集

这是采集器升级线的长期目标。

升级内容：

- eBPF 采集 L4 网络边。
- 采 TCP 连接失败、RTT、重传、连接状态。
- 在可控范围内识别 HTTP / gRPC / Redis 协议摘要。
- 将网络边归属到 service / instance / process。

它解决的问题：

```text
主动 dependency_check 只能告诉你探测时是否可达。
Rover 风格网络画像能告诉你异常窗口内真实流量发生了什么。
```

## 5. 证据结构化升级线

这一条线的核心是让更多采集器输出进入统一证据世界，而不是各写各的 JSON。

### 5.1 Collector Family 标准化

需要统一的证据族：

| family | 包含 |
|---|---|
| `log_scan` | `log_window_json`、错误簇、日志 trace 关联 |
| `dependency_check` | DNS / TCP / HTTP / gRPC 探测 |
| `redis_check` | Redis INFO / SLOWLOG / LATENCY |
| `runtime_snapshot` | Java / Go / Python / off-CPU runtime 证据 |
| `network_profile` | eBPF 网络边和协议摘要 |

### 5.2 Evidence Index v2

升级目标：

```text
不仅能引用一整个 artifact，还能引用 artifact 内的具体证据单元。
```

例如：

- `log_scan.error_clusters[0]`
- `dependency_check.checks[2]`
- `redis_check.slowlog_summary.top_commands[0]`
- `runtime_snapshot.lock_signals[1]`

### 5.3 Evidence Lake / Snapshot Store

升级目标：

```text
把 same_window、rolling_snapshot、delayed_followup、stale_window 分层保存。
```

这样 AI 树在裁决时不会把不同时间窗口的证据互相错误反证。

## 6. AI 树升级线

这一条线的核心不是让 AI 更自由，而是让 AI 树在证据更复杂时仍然稳定。

### 6.1 证据请求闭环增强

当前 AI 树已经能输出补证请求。后续要增强的是：

```text
根据 case 类型、现有证据族、缺失证据族，选择最小必要采集器。
```

例如：

```text
出现 downstream_dependency 候选
  -> 缺 log_scan
  -> 缺 dependency_check
  -> 先请求 dependency_check，再请求 log_scan
```

### 6.2 冲突裁决增强

新增日志、依赖、Redis、网络画像后，证据之间可能冲突：

```text
日志显示 Redis timeout
dependency_check 显示 Redis 当前可达
same_window metrics 显示网络抖动
delayed_followup 显示恢复正常
```

AI 树要能判断：

- 哪些是 same_window。
- 哪些是 delayed_followup。
- 哪些只能降低置信度。
- 哪些不能直接反证异常窗口。

### 6.3 疑难杂症保守诊断

升级目标：

```text
面对复合故障、多个弱信号、证据冲突时，不强行输出单一根因。
```

允许输出：

- 多候选。
- 主次根因。
- 并列故障域。
- 当前不可判断。
- 待复现。
- 缺证据清单。

## 7. 图层升级线

这一条线的核心是从“热点属于哪条链路”升级到“异常如何沿链路传播”。

### 7.1 调用链回连增强

升级目标：

```text
让函数热点、日志错误、依赖探测结果稳定回到同一个 endpoint / trace / service / instance。
```

这一步仍然不是最终归因，只是归属。

### 7.2 跨窗口证据图

升级目标：

```text
把 same_window、delayed_followup、stale_window 放进图结构。
```

它解决：

```text
后续补采没复现，不能直接推翻异常窗口已经冻结的证据。
```

### 7.3 因果图推理

升级目标：

```text
在复杂微服务拓扑里，基于时间先后、拓扑方向、证据强度排序候选传播路径。
```

注意：这不是规则匹配根因，而是候选排序与证据解释。最终结论仍受 AI 树边界控制。

## 8. Runtime Snapshot 升级线

这一条线专门解决 Java / Go / Python / off-CPU 证据口径不一致。

### 8.1 Runtime Adapter

目标输出：

```json
{
  "collector_family": "runtime_snapshot",
  "runtime": "java|go|python|native|unknown",
  "top_wait_reasons": [],
  "blocked_threads": [],
  "hot_stacks": [],
  "lock_signals": [],
  "call_path_refs": []
}
```

### 8.2 锁竞争与停顿摘要

统一提取：

- Java blocked thread / monitor lock。
- Go goroutine wait / mutex / block profile。
- Python GIL / thread wait / lock。
- Native futex / sched_switch / off-CPU wait。

### 8.3 代码行级证据增强

只有在证据足够时才升级到代码行：

```text
有符号信息
有源码映射
有稳定栈样本
有调用路径上下文
有足够样本数
```

否则仍停在 function 或 call_path。

## 9. Persistent Agent 升级线

这一条线解决“任务下发时异常已经消失”的问题。

### 9.1 Watch Lease 持久化

升级目标：

```text
agent 重启后仍能知道自己负责哪些 watch。
```

当前内存态 watch registry 后续要升级成持久化表。

### 9.2 多对象低成本观察

升级目标：

```text
同一个 agent 可以监视多个 service / pid / endpoint，同时控制预算、并发和冷却时间。
```

### 9.3 自动同窗采证

升级目标：

```text
trigger_event 出现后，自动冻结 snapshot，并按策略启动 logs / dependency / stack / metrics collector group。
```

Persistent Agent 仍然不输出根因，只输出时间锚点和证据窗口。

## 10. Benchmark 升级线

这一条线让测试结果更容易解释。

### 10.1 Readiness Gate v2

升级方向：

```text
从检查通用字段
升级为
按 case 所需证据族检查真实 artifact。
```

例如：

| case 需要 | gate 应检查 |
|---|---|
| `log_scan` | 是否有 `log_window_json` 或 log_scan family |
| `dependency_check` | 是否有 `dependency_check_json` |
| `redis_check` | 是否有 `redis_check_json` |
| `runtime_snapshot` | 是否有 runtime_snapshot family |

### 10.2 Case 维度差距分析

升级目标：

```text
把未命中原因拆开。
```

失败类型：

- AI 判断错。
- 采集器没采到。
- artifact 没结构化。
- evidence_ref 缺失。
- 同窗证据不足。
- oracle 所需证据族缺失。

### 10.3 版本对比评测

升级目标：

```text
比较不同版本在准确率、稳定性、证据召回、成本、耗时上的变化。
```

这能避免只看一个平均分。

## 11. 前端可视化升级线

这一条线不决定后端能力是否成立，但决定用户能不能看懂。

### 11.1 Readiness Gate 展示

展示：

- 当前诊断是否可评测。
- 哪些门禁项失败。
- 缺哪类证据。
- 是否应该先补采。

### 11.2 证据树浏览器

展示：

- evidence_ref。
- artifact。
- 日志簇。
- 依赖检查。
- Redis 摘要。
- runtime snapshot。

### 11.3 拓扑与传播视图

展示：

- 服务调用链。
- 异常窗口。
- 证据时间关系。
- 候选传播路径。
- 多根因 / 复合故障结构。

## 12. 推荐推进方式

先完成前置补齐：

```text
log_scan adapter -> log_window_json
dependency_check adapter -> dependency_check_json
redis_check adapter -> redis_check_json
```

然后按部件独立升级，不强行串成一个总阶段：

| 优先级 | 升级线 | 原因 |
|---|---|---|
| 1 | 证据结构化升级线 | 工业采集器适配产物必须被 AI 树和 gate 识别 |
| 2 | Benchmark 升级线 | 需要判断新证据是否真的改善测试 |
| 3 | Runtime Snapshot 升级线 | 改善 Java / Go / Python lock 和 stall case |
| 4 | Persistent Agent 升级线 | 改善瞬时异常捕捉 |
| 5 | 采集器升级到 Rover 风格 | 改善复杂网络传播和下游问题 |
| 6 | 图层升级线 | 在证据更完整后再做复杂推理 |
| 7 | 前端可视化升级线 | 让用户看懂证据链和评测差距 |

核心原则：

```text
先补证据，再升级推理。
先证明采集真实，再优化 AI 判断。
先接工业采集器，再考虑是否自研补充。
先按部件升级，不把不同部件硬串成一条阶段链。
```
