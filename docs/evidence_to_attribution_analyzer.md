# Evidence-to-Attribution Analyzer 方案

## 1. 背景与问题

当前系统已经具备采集端能力，且采集结果已经结构化。Analyzer 当前需要解决的问题不是“如何采集更多数据”，而是：

- 如何基于已有结构化数据完成智能分析。
- 如何从现象定位到资源、进程、线程、函数或调用路径。
- 如何区分主因、次因、伴随现象和不支持结论。
- 如何避免 AI 在证据不足时强行归因。
- 如何让最终 RCA 报告中的每个判断都能对应到具体证据。

因此，本方案聚焦 Analyzer 的数据处理、归因和定位链路，不涉及采集端改造。

## 2. 当前链路问题

当前分析链路可以概括为：

```text
结构化数据
  -> 生成候选原因
  -> 候选原因打分
  -> LLM 输出 RCA 报告
```

这个链路的问题不在于“假设验证”本身完全错误，而在于候选原因出现得太早。

一旦系统先生成多个候选原因，后续分析就容易变成：

```text
假设 A：CPU hotspot
假设 B：IO bottleneck
假设 C：memory pressure

然后从数据里找最匹配的假设。
```

这种方式存在几个风险：

- 容易让 AI 围绕候选原因讲故事。
- 容易在证据不足时也输出一个最像的 root cause。
- 容易把伴随现象误判成主因。
- 容易把“当前采样窗口内观察到的热点”扩大解释成“业务逻辑缺陷”。
- 结论边界不清晰，用户难以判断哪些是事实，哪些是推断。

## 3. 核心设计思想

本方案将 Analyzer 从 Hypothesis Validation 调整为 Evidence-to-Attribution，并将它设计为独立于 RCA 策略的分析链路。

核心区别是：

```text
原链路：先有嫌疑人，再找证据。
新链路：先看证据能证明到哪，再决定有没有嫌疑人。
```

新链路的核心问题不是：

```text
哪个候选 root cause 分数最高？
```

而是：

```text
当前数据最多允许 Analyzer 输出什么级别的定位结论？
```

因此 Analyzer 的主流程应从“候选原因竞争”变成“证据状态推进”。

这里需要区分两个维度：

| 维度 | 参数 | 可选值 | 作用 |
|---|---|---|---|
| 候选组织策略 | `analysis_strategy` | `linear` / `graph` | 决定候选原因如何生成、排序或重排 |
| 分析处理链路 | `analysis_pipeline` | `legacy` / `evidence_to_attribution` | 决定是否启用事实、现象、定位边界和证据反问 |

也就是说：

```text
linear / graph 不是新旧链路切换。
legacy / evidence_to_attribution 才是新旧 Analyzer 处理链路切换。
```

```text
Raw Facts
  -> Evidence State
  -> Symptom State
  -> Localization State
  -> Attribution State
  -> Evidence Challenge
  -> Explanation
```

外部方法论上，这个方向参考了三个原则：

- Brendan Gregg 的 USE Method：性能分析应围绕资源的 Utilization、Saturation、Errors 展开，而不是无方向地看指标。
- Google SRE 中 symptom 与 cause 的区分：现象和原因必须分层，不应混写。
- LLM hallucination 的通用控制原则：模型必须被约束在已有证据范围内，允许输出“不足以判断”。

参考资料：

- [The USE Method](https://www.brendangregg.com/usemethod.html)
- [Performance Analysis Methodology](https://www.brendangregg.com/methodology.html)
- [Google SRE - Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/)
- [Why language models hallucinate](https://openai.com/index/why-language-models-hallucinate/)

## 4. 新旧链路区别

| 维度 | 原假设验证链路 | Evidence-to-Attribution 链路 |
|---|---|---|
| 分析起点 | 候选原因 | 数据事实 |
| 推理方向 | 原因假设 -> 数据匹配 | 数据事实 -> 现象 -> 定位 -> 归因 |
| 核心问题 | 哪个假设最像 | 当前证据能证明到哪一层 |
| 证据作用 | 给候选原因加分 | 决定结论是否允许出现 |
| 输出倾向 | 选出一个 root cause | 输出主因、次因、伴随现象、无法归因 |
| AI 角色 | 参与归因和解释 | 主要负责解释受约束的分析结构 |
| 失败结果 | 低置信度原因 | 明确输出证据不足或只能定位到某层 |

当前实现中，新旧链路通过 `analysis_pipeline` 切换：

| `analysis_pipeline` | 实际链路 | 行为 |
|---|---|---|
| `legacy` | `evidence -> candidates -> calibrate -> LLM` | 保留旧版假设验证，不生成 `analysis_result` |
| `evidence_to_attribution` | `evidence -> analysis_result -> candidates -> calibrate -> constrained LLM` | 启用事实、现象、定位边界、证据反问和原因过滤 |

默认值为：

```text
analysis_pipeline=evidence_to_attribution
```

示例：

```text
输入事实：
- CPU user utilization 高
- perf top 中函数 foo 占比 38%
- iowait 低
- memory pressure 不明显

原链路可能输出：
- 根因是 foo 函数 CPU hotspot。

新链路应输出：
- 已确认现象：CPU user utilization 高。
- 已定位层级：function。
- 支持证据：perf top 中 foo 函数采样占比最高。
- 反向证据：iowait 低，不支持 IO wait bottleneck。
- 结论边界：可以判断主要瓶颈在 CPU 执行路径，但不能证明 foo 一定存在业务逻辑缺陷。
```

## 5. 总体架构

推荐总体链路如下：

```text
已格式化采集数据
  -> Fact Normalization
  -> Symptom Derivation
  -> Localization Derivation
  -> Attribution Guard
  -> Evidence Challenge
  -> Cause Composition
  -> Report Composition
```

各阶段职责如下：

| 阶段 | 输入 | 输出 | 主要职责 |
|---|---|---|---|
| Fact Normalization | 已格式化采集数据 | 标准事实项 | 把不同来源的数据转成统一事实 |
| Symptom Derivation | 标准事实项 | 异常现象 | 判断发生了什么性能现象 |
| Localization Derivation | 事实和现象 | 定位层级 | 判断最多能定位到哪一层 |
| Attribution Guard | 事实、现象、定位层级 | 可允许归因范围 | 控制哪些结论能出现 |
| Evidence Challenge | 可允许归因范围 | 证据必要性校验结果 | 判断缺少某条关键证据时结论是否仍成立 |
| Cause Composition | 归因范围和证据校验结果 | 主因、次因、伴随现象 | 组合最终归因结构 |
| Report Composition | 归因结构 | RCA 报告 | 用 AI 生成可读解释 |

## 6. 子模块设计

### 6.1 Fact Normalization

职责：

- 接收已有结构化采集结果。
- 不改变采集格式，只在 Analyzer 内部转换成统一事实模型。
- 为每条事实分配稳定的 `fact_id`。
- 标记事实来源、指标名、值、阈值、时间窗口和可信度。
- 对接近阈值的事实增加稳定性标记，避免轻微波动直接翻转结论。
- 将同类指标归一到统一事实表达，减少不同采集批次仅因字段顺序或命名差异导致的漂移。

示例事实：

```json
{
  "fact_id": "fact_cpu_user_high",
  "source": "sys_metrics",
  "metric": "cpu.user",
  "value": 91.2,
  "threshold": 80.0,
  "operator": ">=",
  "time_window": "task_window",
  "threshold_band": "above",
  "status": "observed"
}
```

该模块不做归因，只负责回答：

```text
现在有哪些事实成立？
```

### 6.2 Symptom Derivation

职责：

- 从事实项推导异常现象。
- 现象不是 root cause，只描述系统表现。
- 一个现象必须由一个或多个事实支持。

典型现象：

| 现象类型 | 示例 |
|---|---|
| CPU utilization high | CPU user/system 使用率高 |
| CPU saturation high | load average 或 run queue 高 |
| IO wait high | iowait 高、block IO latency 高 |
| memory pressure | page fault、swap、reclaim 明显 |
| function hotspot | perf top 或火焰图中某函数占比高 |
| syscall blocking | eBPF syscall 延迟高 |
| process concentration | 单进程占用异常集中 |

示例：

```json
{
  "symptom_id": "sym_cpu_util_high",
  "type": "cpu_utilization_high",
  "severity": "high",
  "supporting_facts": ["fact_cpu_user_high"]
}
```

该模块回答：

```text
这些事实说明发生了什么现象？
```

### 6.3 Localization Derivation

职责：

- 判断当前证据最多能定位到哪一层。
- 不急于下 root cause。
- 如果只能定位到资源层，就不应输出函数级根因。

定位层级：

| 层级 | 含义 | 示例 |
|---|---|---|
| resource | 资源层 | CPU、memory、disk、network |
| process | 进程层 | 某进程占用高 |
| thread | 线程层 | 某线程或 worker 异常 |
| syscall | 系统调用层 | read/write/futex 等阻塞 |
| function | 函数层 | 某函数采样占比高 |
| call_path | 调用路径层 | 某条调用链聚集明显 |

示例：

```json
{
  "localization_id": "loc_function_foo",
  "level": "function",
  "target": "foo",
  "supporting_symptoms": ["sym_function_hotspot"],
  "boundary": "localized_to_runtime_hotspot_not_business_defect"
}
```

该模块回答：

```text
当前数据最多能定位到哪一层？
```

### 6.4 Attribution Guard

职责：

- 控制哪些归因结论允许出现。
- 明确支持、削弱、缺失和禁止输出的归因。
- 防止 AI 根据弱证据扩大解释。

归因判断分为：

| 类型 | 含义 |
|---|---|
| supported | 有足够事实和现象支持 |
| weakened | 存在反向证据 |
| missing_evidence | 关键证据缺失 |
| forbidden | 当前证据不允许输出 |

示例：

```json
{
  "attribution_id": "attr_cpu_hotspot_supported",
  "cause_type": "cpu_hotspot",
  "status": "supported",
  "supporting_facts": ["fact_cpu_user_high", "fact_perf_foo_top"],
  "opposing_facts": ["fact_iowait_low"],
  "boundary": "can_claim_cpu_execution_bottleneck_only"
}
```

该模块回答：

```text
哪些归因可以说，哪些不能说？
```

### 6.5 Evidence Challenge

职责：

- 对每个允许输出的归因结论做证据必要性校验。
- 检查“如果移除某条证据，这个结论是否仍成立”。
- 进一步按证据族做剥离，而不只按单条事实做剥离，避免同一信号的不同表述重复放大置信度。
- 识别关键证据、辅助证据和冗余证据。
- 防止单条弱证据支撑过强结论。
- 防止多个证据其实都在表达同一件事，导致置信度被重复放大。

这个模块不是反事实分析，也不是重新构造另一个世界。它只在已有证据集合内做剥离校验：

```text
结论 C 是否成立？
  -> 去掉证据 E1 后是否仍成立？
  -> 去掉证据 E2 后是否仍成立？
  -> 只保留核心证据后是否仍成立？
```

校验结果分为：

| 类型 | 含义 |
|---|---|
| necessary | 去掉该证据后，结论必须降级或不能成立 |
| supportive | 去掉该证据后，结论仍成立，但置信度下降 |
| redundant | 去掉该证据后，结论基本不变 |
| misleading | 该证据容易导致错误放大，需要降低权重 |

示例：

```json
{
  "challenge_id": "challenge_cpu_hotspot_foo",
  "attribution_id": "attr_cpu_hotspot_supported",
  "tests": [
    {
      "removed_fact": "fact_perf_foo_top",
      "result": "downgrade_to_resource_level",
      "meaning": "缺少函数热点证据后，只能判断 CPU 压力，不能判断 foo 是热点"
    },
    {
      "removed_fact": "fact_cpu_user_high",
      "result": "forbid_cpu_bottleneck_claim",
      "meaning": "缺少 CPU 高这一事实后，不能输出 CPU 瓶颈结论"
    },
    {
      "removed_fact": "fact_load_high",
      "result": "confidence_down",
      "meaning": "缺少 load 高后，仍可判断 CPU 执行热点，但饱和程度证据变弱"
    }
  ],
  "critical_facts": ["fact_cpu_user_high", "fact_perf_foo_top"],
  "conclusion_stability": "stable_with_critical_facts_only"
}
```

该模块回答：

```text
这个结论到底依赖哪些关键证据？
如果某条证据不存在，结论需要降级到哪一层？
```

### 6.6 Cause Composition

职责：

- 只基于 Attribution Guard 允许的范围生成结论结构。
- 结合 Evidence Challenge 的结果判断结论是否稳定。
- 区分主因、次因、伴随现象和不支持原因。
- 不把所有异常都压成一个 root cause。
- 主因选择不依赖候选列表顺序，而依赖固定裁决顺序：更深定位层级优先、关键事实更多优先、反证更少优先，最后才按稳定的 `candidate_id` 兜底。
- 输出稳定性评分，标记该结论在重复任务中的收敛程度。

输出结构：

```json
{
  "primary_cause": {
    "cause_type": "cpu_hotspot",
    "target": "foo",
    "confidence": "high_possible",
    "stability_score": 0.92,
    "selection_reason": "function 层级更深且关键事实更多",
    "evidence": ["fact_cpu_user_high", "fact_perf_foo_top"],
    "critical_evidence": ["fact_cpu_user_high", "fact_perf_foo_top"]
  },
  "secondary_causes": [],
  "correlated_symptoms": ["sym_cpu_saturation_high"],
  "unsupported_causes": ["io_wait_bottleneck", "memory_pressure"]
}
```

该模块回答：

```text
最终应该如何组织主因、次因和伴随现象？
同类候选在重复任务中如何稳定选出同一个主因？
这个主因在重复任务中的收敛程度有多高？
```

### 6.7 Report Composition

职责：

- 使用 LLM 生成最终报告。
- LLM 在受约束归因范围内组织自然语言。
- LLM 不允许新增事实、不允许新增未出现的证据、不允许扩大归因范围。
- LLM 应优先沿用结构化分析给出的主因排序与稳定性评分，不得将次序重排为新的主因集合。
- 报告展开顺序固定为事实、现象、定位、证据链、关键证据反问、受约束归因、不支持的判断、结论边界、建议动作，避免同一结论因叙述顺序变化而显得不稳定。

报告应明确区分：

- 已观察事实。
- 已确认现象。
- 定位层级。
- 支持证据。
- 关键证据。
- 反向证据。
- 归因结论。
- 结论边界。
- 不支持的判断。

## 7. 数据结构设计

### 7.1 Fact

```json
{
  "fact_id": "string",
  "source": "string",
  "metric": "string",
  "value": "number|string|object",
  "threshold": "number|string|null",
  "operator": "string|null",
  "time_window": "string|null",
  "threshold_band": "below|near|above|unknown",
  "status": "observed|missing|invalid",
  "note": "string|null"
}
```

### 7.2 Symptom

```json
{
  "symptom_id": "string",
  "type": "string",
  "severity": "low|medium|high",
  "supporting_facts": ["fact_id"],
  "description": "string"
}
```

### 7.3 Localization

```json
{
  "localization_id": "string",
  "level": "resource|process|thread|syscall|function|call_path|line",
  "target": "string",
  "supporting_symptoms": ["symptom_id"],
  "supporting_facts": ["fact_id"],
  "boundary": "string"
}
```

### 7.4 Attribution

```json
{
  "attribution_id": "string",
  "cause_type": "string",
  "status": "supported|weakened|missing_evidence|forbidden",
  "supporting_facts": ["fact_id"],
  "opposing_facts": ["fact_id"],
  "missing_facts": ["string"],
  "confidence": "low|medium|high_possible",
  "boundary": "string"
}
```

### 7.5 Evidence Challenge

```json
{
  "challenge_id": "string",
  "attribution_id": "string",
  "tests": [
    {
      "removed_fact": "fact_id",
      "result": "unchanged|confidence_down|downgrade_level|forbidden",
      "meaning": "string"
    }
  ],
  "critical_facts": ["fact_id"],
  "critical_fact_groups": [["fact_id", "fact_id"]],
  "supportive_facts": ["fact_id"],
  "redundant_facts": ["fact_id"],
  "conclusion_stability": "stable|fragile|unsupported_without_key_fact"
}
```

### 7.6 Final Analysis

```json
{
  "facts": [],
  "symptoms": [],
  "localizations": [],
  "attributions": [],
  "evidence_challenges": [],
  "primary_cause": null,
  "stability_score": 0.0,
  "primary_cause_reason": "",
  "secondary_causes": [],
  "correlated_symptoms": [],
  "unsupported_causes": [],
  "conclusion_boundary": {
    "can_claim_root_cause": false,
    "max_supported_level": "resource|process|thread|syscall|function|call_path|line",
    "reason": "string"
  }
}
```

其中 `stability_score` 表示整份分析结果的整体稳定性，`primary_cause_reason` 是主因选择原因的摘要字段，应与 `primary_cause.selection_reason` 保持一致。

## 8. 分析流程

### 8.1 CPU 执行热点示例

输入：

```text
- CPU user 高
- load 高
- perf top 中 foo 占比最高
- iowait 低
- memory pressure 不明显
```

处理：

```text
Fact:
- CPU user utilization high
- load average high
- function foo hotspot observed
- IO wait low
- memory pressure not observed

Symptom:
- CPU utilization high
- CPU saturation high
- function hotspot

Localization:
- function: foo

Attribution:
- supported: cpu_hotspot
- weakened: io_wait_bottleneck
- weakened: memory_pressure

Evidence Challenge:
- 去掉 perf top 中 foo 占比最高这一事实后，结论降级为 CPU 资源压力，不能定位到 foo
- 去掉 CPU user 高这一事实后，不能输出 CPU 瓶颈结论
- 去掉 load 高这一事实后，仍可判断 CPU 执行热点，但饱和程度证据变弱

Conclusion:
- 主因：CPU 执行热点集中在 foo
- 关键证据：CPU user 高、perf top 中 foo 占比最高
- 边界：不能证明 foo 是业务逻辑缺陷，只能证明当前窗口内 CPU 消耗集中
```

### 8.2 IO 等待示例

输入：

```text
- CPU iowait 高
- block IO latency 高
- perf top 没有明显 CPU 热点
- 业务延迟升高
```

处理：

```text
Fact:
- IO wait high
- block IO latency high
- no dominant CPU hotspot

Symptom:
- IO wait high
- syscall or block layer delay

Localization:
- resource: disk/io
- syscall: read/write, if available

Attribution:
- supported: io_wait_bottleneck
- weakened: cpu_hotspot

Evidence Challenge:
- 去掉 block IO latency 高这一事实后，结论降级为 IO wait 现象，不能定位到 block layer 延迟
- 去掉 iowait 高这一事实后，不能输出 IO wait bottleneck
- perf top 无明显热点只能削弱 CPU hotspot，不能单独证明 IO 是主因

Conclusion:
- 主因：IO 等待导致任务延迟升高
- 关键证据：iowait 高、block IO latency 高
- 边界：如果没有具体 syscall 或 block target，只能定位到资源层
```

### 8.3 证据不足示例

输入：

```text
- CPU 稍高
- perf top 无明显热点
- IO 和内存指标正常
- 采样窗口较短
```

处理：

```text
Fact:
- CPU utilization medium
- no dominant hotspot
- IO normal
- memory normal

Symptom:
- weak CPU pressure

Localization:
- resource: CPU

Attribution:
- missing_evidence: cpu_hotspot
- weakened: io_wait_bottleneck
- weakened: memory_pressure

Evidence Challenge:
- 去掉 CPU 稍高这一事实后，剩余证据不能支持任何明确异常
- perf top 无明显热点不能作为 CPU hotspot 的支持证据
- 当前证据集合无法稳定支撑 root cause

Conclusion:
- 当前只能说明 CPU 存在轻度压力
- 不允许输出明确 root cause
```

## 9. AI 职责边界

AI 不是纯解释器，也不是自由归因器，而是“受证据边界约束的归因表达器”。

AI 可以做：

- 在 `allowed_cause_ids` 中选择最合适的主因表达。
- 组织主因、次因、伴随现象和结论边界的自然语言。
- 解释支持证据、反向证据和为什么只能定位到某一层。
- 在允许范围内补充排查建议，但不能突破已确认边界。

AI 不应该做：

- 新增输入数据中不存在的事实。
- 编造不存在的函数、进程、指标或调用链。
- 在 Attribution Guard 禁止时输出未被允许的归因。
- 把“可能”写成“确定”。
- 把“当前采样窗口内的现象”扩大成“长期系统缺陷”。
- 自动发起新的采集任务。

推荐约束：

```text
LLM 只能引用 facts、symptoms、localizations、attributions、evidence_challenges 中已有的 id。
LLM 可以在 allowed_cause_ids 内做主因选择，但不得新增候选或提升定位层级。
如果 conclusion_boundary.can_claim_root_cause=false，报告中不得出现超出边界的确定性根因表述。
如果需要表达额外判断，必须放入 assumptions 或 limitations。
```

## 10. 结论输出规范

最终报告建议采用固定结构：

```text
1. 结论摘要
2. 已确认现象
3. 定位结果
4. 证据链
5. 关键证据反问
6. 受约束归因
7. 不支持的判断
8. 结论边界
9. 建议动作
```

其中“受约束归因”不是自由猜测，而是在 `allowed_cause_ids` 范围内选择最合适的主因，并用证据链解释为什么它成立。

结论措辞建议：

| 证据状态 | 允许措辞 | 禁止措辞 |
|---|---|---|
| supported | 当前证据支持、主要瓶颈集中在 | 绝对是、唯一根因 |
| high_possible | 可以判断、在当前边界内最合理的是 | 已完全证明 |
| missing_evidence | 当前不足以判断、只能定位到更粗层级 | 根因是 |
| weakened | 当前证据不支持该更强归因 | 仍然强行归因 |
| forbidden | 不应输出该结论 | 任何确定性归因 |

示例结论：

```text
当前证据支持 CPU 执行路径是主要瓶颈，热点集中在 foo 函数。
该判断基于 CPU user utilization 高、perf top 中 foo 采样占比最高，以及证据反问后结论仍稳定。
当前证据不支持 IO wait 或 memory pressure 是主因。
需要注意，该结论只能说明当前采样窗口内 CPU 消耗集中，不能直接证明 foo 存在业务逻辑缺陷。
```

## 11. 与现有代码的落地关系

当前 RCA 相关代码主要集中在：

- `server/app/rca/report.py`
- `server/app/rca/evidence.py`
- `server/app/rca/candidates.py`
- `server/app/rca/calibrator.py`
- `server/app/rca/llm_client.py`
- `server/app/rca/repair.py`

推荐落地方式：

| 新模块 | 建议文件 | 作用 |
|---|---|---|
| Fact Normalization | `server/app/rca/facts.py` | 从现有 evidence 输入中生成标准事实 |
| Symptom Derivation | `server/app/rca/symptoms.py` | 从事实推导现象 |
| Localization Derivation | `server/app/rca/localization.py` | 判断定位层级 |
| Attribution Guard | `server/app/rca/attribution.py` | 控制归因边界 |
| Evidence Challenge | `server/app/rca/challenge.py` | 校验关键证据缺失时结论是否仍成立 |
| Cause Composition | `server/app/rca/composer.py` | 生成主因、次因、伴随现象 |
| Report Constraint | `server/app/rca/llm_client.py` | 限制 LLM 只解释结构化结果 |

与当前链路的关系：

```text
原有 evidence.py 仍然负责收集分析所需输入。
原有 candidates.py 可以保留，但不再作为分析起点。
原有 calibrator.py 可以保留，但分数只作为辅助信息。
原有 llm_client.py 继续生成报告，但输入应改为 Evidence-to-Attribution 结构。
```

建议集成点：

```text
report.py
  -> collect_evidence()
  -> normalize_facts()
  -> derive_symptoms()
  -> derive_localization()
  -> guard_attributions()
  -> challenge_evidence()
  -> compose_causes()
  -> generate_llm_report()
```

## 12. 分阶段实施计划

### 阶段一：最小闭环

目标：

- 不推翻现有 RCA。
- 先增加证据状态结构。
- 让报告能明确输出“证据不足”和“结论边界”。

工作：

- 新增 `facts.py`。
- 新增 `symptoms.py`。
- 新增 `attribution.py`。
- 新增 `challenge.py` 的最小版本，用于标记关键证据。
- 在 LLM 输入中加入 facts、symptoms、attributions。
- 修改 prompt，禁止 LLM 引用未出现证据。
- 增加测试：证据不足时不得输出明确 root cause。
- 增加测试：缺少关键证据时结论必须降级。

### 阶段二：主因/次因分层

目标：

- 不再只输出单一 root cause。
- 把性能问题拆成主因、次因和伴随现象。

工作：

- 新增 `composer.py`。
- 定义 primary、secondary、correlated、unsupported。
- 增加 CPU、IO、memory、function hotspot 的基础规则。
- 增加测试：伴随现象不能被误判为主因。

### 阶段三：证据反问校验

目标：

- 判断每个结论依赖哪些关键证据。
- 防止单条弱证据支撑过强结论。

工作：

- 完善 `challenge.py`。
- 定义 necessary、supportive、redundant、misleading。
- 输出 critical facts 和 conclusion stability。
- 增加测试：去掉 perf/function 证据后，函数级结论必须降级。

### 阶段四：定位层级控制

目标：

- 控制 Analyzer 结论能定位到哪一层。
- 防止资源层证据直接跳到函数级结论。

工作：

- 新增 `localization.py`。
- 定义 resource、process、thread、syscall、function、call_path。
- 在 conclusion boundary 中输出 `max_supported_level`。
- 增加测试：没有 perf/function 证据时不得输出函数级归因。

### 阶段五：冲突裁决

目标：

- 处理多个现象方向不一致的问题。

工作：

- 增加冲突规则。
- 典型冲突包括：
  - CPU 高但 perf 无热点。
  - IO wait 高但 CPU 热点也明显。
  - memory pressure 弱但存在分配热点。
  - load 高但 CPU utilization 不高。
- 输出 opposing facts 和 weakened causes。

### 阶段六：报告质量收敛

目标：

- 让最终 RCA 报告更稳定、更可解释。

工作：

- 固定报告结构。
- 固定结论措辞。
- 强制 evidence id 引用。
- 如果 root cause 不成立，报告必须说明为什么不能下结论。

## 13. 风险与限制

### 13.1 方案收益

- 不依赖历史案例。
- 不依赖训练集。
- 不改采集端。
- 能降低 AI 幻觉。
- 能让结论和证据对应。
- 能明确表达“当前只能定位到某层”。
- 能兼容已有候选原因和评分逻辑。

### 13.2 方案限制

- 初始规则覆盖面有限，需要逐步补充。
- 结论会比原来更保守。
- 当输入数据本身无法支持细粒度定位时，Analyzer 会停在较粗层级。
- 需要重写一部分 LLM prompt 和报告结构。
- 需要新增针对边界输出的测试。

### 13.3 设计边界

本方案不处理：

- 采集器调度。
- 新采集任务发布。
- 历史案例库。
- 模型训练。
- 自动化反事实实验。
- 用户无确认情况下的交互式探测。

这些能力可以作为未来扩展，但不属于当前 Analyzer 数据处理链路的核心问题。

### 13.4 稳定性优化点

为提升重复任务下的结论一致性，本方案额外约束以下优化点：

- 事实归一要吸收阈值带宽，避免临界值轻微波动导致结论翻转。
- 主因选择要脱离候选列表顺序，改为固定裁决顺序。
- 证据反问要按证据族剥离，避免同一信号的重复表达放大置信度。
- `calibrator` 负责排序，`attribution` 负责边界，两者职责拆分后不重复抬高结论等级。
- 报告输出顺序固定，LLM 只在已锁定的证据边界内组织自然语言。

## 14. 总结

Evidence-to-Attribution Analyzer 的目标不是让 AI 更大胆地猜 root cause，而是让 Analyzer 更严格地控制归因边界。

它的核心变化是：

```text
从“候选原因竞争”改为“证据状态推进”。
```

最终 Analyzer 应该做到：

- 先整理事实。
- 再识别现象。
- 再判断定位层级。
- 再决定允许哪些归因。
- 最后让 AI 解释结构化结果。

这样可以在不改采集端、不依赖历史案例、不训练模型的前提下，修复当前链路中“证据不足仍强行归因”的问题。

## 15. 采集层与 AI 定位层增强方案

当前 Analyzer 已经能够基于现有结构化采集结果完成事实归一、现象识别、函数级定位和边界归因，但能力仍主要停留在“函数层热点可见”，对“为什么热”“热与哪条调用链相关”“是执行还是等待”“是否为锁、网络、调度或运行时问题”这些更深层机制缺乏足够证据。

因此，下一步不应只继续强化证据边缘判定，而应同步补齐采集层的深挖工具，并让 AI 在更完整的证据链上做受约束归因。

### 15.1 需要补齐的采集能力

推荐优先补齐以下工具能力：

- `off_cpu_wait_profile`：识别线程或进程未运行时的阻塞原因，区分锁等待、IO 阻塞、调度等待和 page fault 等
- `trace_endpoint_profile`：将函数热点回连到 endpoint、service、instance 和调用链上下文
- `network_profile`：识别网络侧延迟、重试、连接复用和下游 RTT 问题
- `runtime_block_mutex_alloc_profile`：补齐 block、mutex、alloc、heap、goroutine 和 GC 相关证据
- `baseline_window_profile`：保存多窗口证据并与历史基线对齐，提升重复任务下结论稳定性

这些能力的目标不是替换现有 `perf_cpu`、`sys_metrics`、`ebpf_io`、`memory_smaps`，而是在其基础上补足“机制解释”的证据。

### 15.2 采集触发策略

采集层应采用门控式分层流程，而不是全量持续重采：

1. 先执行轻量观测，采集 `sys_metrics`、`perf_cpu`、`ebpf_io`、`memory_smaps`
2. 再根据异常现象触发深挖采集
   - CPU 异常触发 `off_cpu_wait_profile` 和 `trace_endpoint_profile`
   - IO 异常触发 `network_profile` 和 `off_cpu_wait_profile`
   - 内存异常触发 `runtime_block_mutex_alloc_profile`
   - 结果波动明显时触发 `baseline_window_profile`
3. 只有在证据仍不足时，才继续增加采样时长或采样粒度

这样做可以避免把深度采集变成默认开销，同时保持异常场景下的定位能力。

### 15.3 AI 定位层增强

AI 的职责需要从“自由总结根因”调整为“证据边界内的归因裁决器”。

AI 不再只回答“哪个假设最像”，而是回答：

- 当前证据最多允许定位到哪一层
- 结论缺少哪些证据
- 哪些采集项补齐后可以继续下钻
- 主因、次因、伴随现象和不支持结论分别是什么

建议新增以下结构化输出：

```json
{
  "missing_evidence": [],
  "blocked_upgrades": [],
  "conclusion_boundary": {},
  "primary_cause": {},
  "secondary_causes": [],
  "correlated_symptoms": [],
  "unsupported_causes": []
}
```

其中 `missing_evidence` 用于标记当前采集层缺少的证据类型，`blocked_upgrades` 用于标记当前无法推进到更深定位层级的原因，`conclusion_boundary` 用于约束报告生成只能停留在证据允许的范围内。

### 15.4 与 SkyWalking 思路的对应关系

本方案参考 SkyWalking 的分层采集与异常触发深挖思路，但不直接照搬其实现：

| SkyWalking 思路 | 这里的落地方向 |
|---|---|
| on-CPU / off-CPU profiling | `perf_cpu` + `off_cpu_wait_profile` |
| trace profiling | `trace_endpoint_profile` |
| network profiling | `network_profile` |
| continuous profiling | `baseline_window_profile` 和门控式窗口采集 |
| topology / correlation | service、instance、endpoint 与调用链回连 |

核心差异是：SkyWalking 更偏分布式可观测性，这里的目标更偏受证据约束的 RCA 归因，所以采集能力要服务于“结论边界”，而不是单纯追求更多热点。

### 15.5 预期效果

补齐上述采集能力后，系统应从当前的“函数热点可见”推进到：

- 能判断热点是执行密集还是等待密集
- 能把函数热点回连到请求路径和服务上下文
- 能识别网络、锁、IO、GC、分配等更深层机制
- 能明确输出当前缺少哪些证据，以及下一步应该补什么采集项
- 能让重复任务下的结论更稳定，不再受单窗口波动影响

这部分能力是后续从函数层继续往机制层推进的前置条件。

### 15.6 已完成项

- [x] 已在 Analyzer 结构化结果中加入 `missing_evidence`、`blocked_upgrades`、`collection_gaps`
- [x] 已让报告能明确说明当前结论卡在哪个定位层级
- [x] 已让报告能明确指出缺少的采集能力，如 `off_cpu_wait_profile`、`trace_endpoint_profile`、`baseline_window_profile`
- [x] 已收紧候选排序与重复证据处理，降低重复任务中的结论抖动

### 15.7 原始证据先存后取

为避免把大量栈、trace 和 off-CPU 原始数据一次性送入 LLM，本方案采用“先存证、后取证”的策略：

1. 原始证据先完整保存，保留审计和回放能力。
2. Analyzer 先生成摘要和边界，只把压缩后的证据摘要给 AI。
3. 当 AI 需要继续下钻时，再按 `evidence_ref` 按需拉取局部原文。

对应的任务目标是：

- [ ] 原始证据可以先落库，不直接占满 LLM 上下文
- [ ] AI 默认只消费摘要证据
- [ ] AI 需要时才按需拉取局部栈和 trace 片段

## 16. 方案 2：AI 树 + 轻量图处理层

在前述证据约束链路之上，当前更适合落地的方案不是“规则匹配树”，而是“AI 树 + 轻量图处理层”。

这里的 AI 树不是让模型自己随意发散出根因，而是把它约束成一个稳定的证据推进器：

- 树的分支只决定下一步要补什么证据、是否继续下钻、是否必须降级结论。
- 叶子节点不直接代表最终真相，只代表“当前证据允许到达的最深结论”。
- 若证据不足，叶子可以停在 `function`、`call_path` 或 `unknown`，不强行升级到根因。
- 多次重复任务中，树的分支必须尽量保持一致，因此分支依据优先使用稳定事实、稳定阈值和反问后仍成立的关键证据。

轻量图处理层的职责也不是做最终裁决，而是把“同一热点属于哪条链路”这件事先稳定下来：

- 将 `trace / endpoint / service / instance / context` 串成可回连的最小图。
- 只做归属和聚合，不做强归因。

### 16.1 方案 2 的核心收益

- 比纯规则匹配更稳，因为树节点决定的是证据动作，不是硬编码根因。
- 比直接让 LLM 自由归因更可控，因为每一步都受证据门控。
- 比单纯函数热点分析更完整，因为图层补上了链路归属。
- 比全量图推理更轻，因为图层只处理必要的回连，不做重计算。
- 更适合疑难杂症场景，因为叶子可以保守停留，不必强制给出伪确定结论。

### 16.2 方案 2 的处理流程

```text
事实归一
  -> AI 树节点门控
  -> 轻量图回连
  -> 证据反问
  -> 受约束归因
  -> 报告生成
```

树层负责回答：

- 现在应不应该继续下钻？
- 还缺哪一类证据？
- 该停在 function、call_path 还是更粗层级？

图层负责回答：

- 这个热点属于哪个 endpoint？
- 这个 endpoint 属于哪条 service / instance 链路？
- 这组证据是不是同一个上下文里的同一个热点？

```mermaid
flowchart TD
    A["事实归一"] --> B{"证据完整性"}
    B -->|不足| C["unknown_leaf"]
    B -->|足够| D{"机制分枝"}

    D -->|执行密集| E["function / call_path"]
    D -->|等待密集| F["off-CPU / blocking"]
    D -->|链路回连| G["endpoint / service"]
    D -->|冲突明显| H{"冲突裁决"}

    H -->|反证不足| I["conservative_leaf"]
    H -->|证据稳定| J["clear_leaf"]

    E --> K["输出下一步补证请求"]
    F --> K
    G --> K
    I --> K
    J --> L["停止下钻，进入报告输出"]
```

### 16.3 叶子结论约束

为解决疑难杂症下结论不稳定的问题，叶子必须允许三种输出：

| 叶子类型 | 含义 |
|---|---|
| 明确叶子 | 证据足够，当前层级可以稳定归因 |
| 保守叶子 | 证据只允许到达某一层，不能继续升级 |
| 未知叶子 | 关键证据缺失，不能稳定归因 |

这样做的目标是避免“树一定要给出一个答案”的错误倾向。

### 16.4 方案 3 的后续升级口

方案 2 只做轻量图回连，不把图推理做重。后续如果要升级到方案 3，可以在不破坏当前接口的前提下，逐步把图层增强为更强的图推理层。

升级方向建议如下：

- 从轻量回连升级为更强的链路传播分析。
- 从静态上下文归属升级为跨窗口、跨任务的证据图。
- 从人工定义的门控条件升级为可学习的图裁决策略。
- 从单次分析升级为多轮证据补齐和反问闭环。

但方案 3 必须保持当前边界不变：

- `facts / symptoms / localizations / attributions / evidence_challenges` 的结构不变。
- `analysis_result` 和报告边界字段不变。
- 现有证据先存后取策略不变。
- 当前 AI 树的保守叶子能力不退化。

### 16.5 已完成项

- [x] 已明确 AI 树不是规则匹配树，而是受证据门控的稳定推进器
- [x] 已明确轻量图层只负责调用链回连，不直接做最终归因
- [x] 已明确叶子结论允许保守停留，避免疑难杂症被强行归因
- [x] 已预留后续升级到更强图推理的接口边界

### 16.6 AI 树分枝表

AI 树的目标不是增加分叉数量，而是把每一次分枝都绑定到明确的证据动作上。推荐把树的分枝固定为以下四类：

| 分枝类型 | 触发条件 | 目标动作 | 允许的叶子状态 |
|---|---|---|---|
| 证据完整性分枝 | 事实、定位层级、关键证据是否齐全 | 判断是否可以进入下一层分析 | `unknown_leaf` / `conservative_leaf` |
| 机制分枝 | 已确认的现象可以区分执行、等待、链路回连等机制 | 决定下一步是下钻 `function`、`call_path` 还是等待机制 | `clear_leaf` / `conservative_leaf` |
| 冲突裁决分枝 | 多个候选方向都部分成立 | 使用反问证据、证据族去重和稳定性评分进行裁决 | `conservative_leaf` / `clear_leaf` |
| 停止分枝 | 证据已足够稳定，且继续下钻不会增加确定性 | 停止继续追问，进入报告输出 | `clear_leaf` |

每个树节点建议至少包含以下语义：

- `branch_key`：当前分枝依据，必须是可重复的稳定条件。
- `decision`：`continue`、`downgrade` 或 `stop`。
- `leaf_status`：`clear_leaf`、`conservative_leaf` 或 `unknown_leaf`。
- `next_evidence_requests`：下一步建议补的证据类型。
- `evidence_refs`：当前分枝实际依赖的证据引用。
- `reason`：一句话说明为什么进入这个分枝。
- `request_stability_key`：请求列表的稳定依据，保证同一证据缺口下输出一致。

推荐的分枝顺序如下：

```text
证据完整性
  -> 机制分枝
  -> 冲突裁决
  -> 停止或保守叶子
```

这样做的好处是：

- 先解决“能不能说”
- 再解决“说到哪一层”
- 最后解决“要不要继续问”

### 16.6.1 证据请求闭环

当树停在保守叶子时，不应只输出“证据不足”，而应输出下一步最值得补的证据类型。当前闭环约定如下：

| 缺口场景 | 优先请求 | 目的 |
|---|---|---|
| 函数热点已见，但机制不清 | `off_cpu_wait_profile` | 区分执行密集和等待密集 |
| 已有函数热点，但链路未回连 | `trace_endpoint_profile` | 把热点回连到 endpoint / service / instance |
| 结论稳定性偏低 | `baseline_window_profile` | 做多窗口对齐，减少重复任务波动 |

补证请求必须满足两个约束：

- 同一证据缺口下，请求列表必须稳定。
- 请求列表只负责指出下一步补什么，不负责直接升级根因结论。

### 16.7 轻量图字段定义

轻量图的核心不是建一个复杂图数据库，而是把链路回连为稳定、可解释的最小图。建议图层只保留两个对象集：实体和边。

#### 实体 `graph_entities`

| 字段 | 含义 | 示例 |
|---|---|---|
| `entity_id` | 实体唯一标识 | `trace:trace-1` |
| `entity_type` | 实体类型 | `trace` / `endpoint` / `service` / `instance` / `context` / `call_path` / `function` / `line` |
| `label` | 可读名称 | `/api/order/create` |
| `evidence_ref` | 支持该实体的证据路径 | `evidence_index.context.endpoint` |
| `context_id` | 归属上下文标识 | `ctx-1` |

#### 边 `graph_links`

| 字段 | 含义 | 示例 |
|---|---|---|
| `source_id` | 起点实体 | `call_path:main;worker;compute_hotspot` |
| `target_id` | 终点实体 | `function:compute_hotspot` |
| `relation` | 关系类型 | `contains_hotspot` / `owns_hotspot` / `refines_hotspot` / `invokes` / `served_by` / `runs_on` |
| `evidence_ref` | 支持该关系的证据路径 | `evidence_index.context.call_path` |
| `stable` | 是否为稳定回连关系 | `true` |

```mermaid
flowchart LR
    T["trace"] --> C["context"]
    C --> P["call_path"]
    P --> E["endpoint"]
    E --> S["service"]
    S --> I["instance"]

    P --> F["function hotspot"]
    F --> L["line hotspot"]

    F -.-> R["归属，不裁决"]
    L -.-> R
```

推荐只保留两类边语义：

- 归属边：说明链路如何串起来
- 热点边：说明热点属于哪条链路

当前方案 2 已补齐的热点边包括：

- `call_path -> function` 的 `owns_hotspot`
- `function -> line` 的 `refines_hotspot`

这两类边都只表示“归属 / 下钻”，不表示“根因成立”。

不建议在方案 2 阶段引入更复杂的图关系，例如传播权重、图卷积状态或跨任务全局社区发现。那些应留给方案 3。

### 16.8 与现有结构的映射

方案 2 的所有新增内容都应挂回现有结构，而不是另起一套新协议。推荐映射如下：

| 新结构 | 现有承载字段 | 作用 |
|---|---|---|
| AI 树分枝 | `analysis_result.ai_tree` | 记录当前分析在哪个分枝停住、为什么停住 |
| 轻量图实体 | `analysis_result.graph_entities` | 记录热点属于哪条链路、哪个上下文 |
| 轻量图边 | `analysis_result.graph_links` | 记录链路回连关系 |
| 图升级口 | `analysis_result.graph_extension_points` | 预留方案 3 的增强接口 |
| 保守叶子 | `analysis_result.conclusion_boundary` + `ai_tree.leaf_status` | 约束是否允许继续升级 |
| 补证请求 | `analysis_result.ai_tree.next_evidence_requests` | 记录当前缺口下下一步该补什么证据 |

最终报告中应明确区分四层含义：

1. `facts` 表示事实
2. `localizations` 表示定位层级
3. `ai_tree` 表示下钻过程
4. `graph_links` 表示链路归属

如果报告包含 `next_evidence_requests`，它表示下一步补证方向，不表示新的归因结论。

这样即使后续升级到方案 3，现有报告结构也不会被推翻，只是在 `graph_extension_points` 上逐步接入更强的图推理能力。

### 16.9 已完成项

- [x] 已补齐 AI 树的冲突裁决分枝
- [x] 已补齐图层的热点归属边
- [x] 已补齐证据请求闭环
- [x] 已将补证请求与缺失证据族绑定
- [x] 已保证同一证据缺口下的请求列表稳定

## 17. 证据结构化处理层

当前采集链路仍存在一个硬缺口：采集器已经能够产出 `pyspy.svg`、`perf.data`、`flamegraph_json`、`top_json`、`sys_metrics`、`depth_evidence_json` 等产物，但 RCA / AI 树真正能稳定理解的是结构化证据，而不是原始图像、二进制采样文件或体积较大的半原始数据。

因此需要在采集器产物和证据归因之间新增一层：

```text
采集器
  -> 原始/半原始产物
  -> Evidence Structuring
  -> 结构化证据
  -> Evidence-to-Attribution / AI 树
```

这一层的定位必须保持清晰：

- 它不是 AI。
- 它不做 root cause 判断。
- 它不生成候选原因。
- 它只把采集结果转换为可检索、可引用、可压缩、可反问的证据对象。

### 17.1 输入与输出

输入包括：

- `flamegraph_svg`，例如 `pyspy.svg`
- `flamegraph_json`
- `top_json`
- `depth_evidence_json`
- `sys_metrics`
- `ebpf_metrics`
- `memory_json`
- `raw` / `perf.data` 的元数据引用

输出统一为 `structured_evidence`：

```json
{
  "version": 1,
  "task_id": "task-1",
  "artifact_refs": [],
  "top_functions": [],
  "stack_summary": {},
  "call_path_hotspots": [],
  "evidence_index": {},
  "confidence_inputs": {}
}
```

其中各字段职责如下：

| 字段 | 作用 |
|---|---|
| `artifact_refs` | 保留原始产物引用，支持审计和按需取证 |
| `top_functions` | 标准 TopN 函数热点，供事实归一直接消费 |
| `stack_summary` | 对栈样本、热点帧、等待原因、上下文的压缩摘要 |
| `call_path_hotspots` | 将热点函数与调用路径、endpoint、service、instance 回连 |
| `evidence_index` | RCA 默认消费的紧凑索引，支持按 `evidence_ref` 局部取证 |
| `confidence_inputs` | 给稳定性和置信判断使用的非归因输入，例如样本数、覆盖率、上下文完整度 |

### 17.2 处理规则

推荐转换规则如下：

| 原始/半原始产物 | 结构化结果 | 说明 |
|---|---|---|
| `top_json` | `top_functions` | 直接规范字段、排序、截断 |
| `depth_evidence_json` | `stack_summary`、`evidence_index`、`call_path_hotspots` | 复用深采集标准摘要 |
| `flamegraph_json` | `stack_summary`、`top_functions` | 当 `top_json` 缺失时从火焰图树提取热点摘要 |
| `flamegraph_svg` | `artifact_refs` | SVG 默认只存引用，不送入 AI；只有缺少结构化热点时才尝试轻量提取 title 文本 |
| `sys_metrics` | `confidence_inputs.system_pressure` | 提供系统压力背景，不直接代表根因 |
| `ebpf_metrics` | `confidence_inputs.wait_or_io_signal` | 提供等待或 IO 证据背景 |
| `raw` / `perf.data` | `artifact_refs.raw` | 只保留引用，不进入 LLM 上下文 |

默认策略是：

```text
能消费 JSON 摘要就不消费 SVG。
能消费结构化索引就不消费原始栈。
能引用 raw artifact 就不把 raw 内容送入 AI。
```

### 17.3 pyspy 示例

`pyspy` 当前可能只产出：

```text
flamegraph_svg
```

结构化处理层需要至少补出：

```text
artifact_refs.flamegraph_svg
top_functions
stack_summary
evidence_index
confidence_inputs
```

示例：

```json
{
  "top_functions": [
    {
      "name": "microservices_test.common.busy_cpu",
      "percent": 72.4,
      "samples": 138,
      "evidence_ref": "structured_evidence.top_functions[0]"
    }
  ],
  "stack_summary": {
    "dominant_hot_frame": "microservices_test.common.busy_cpu",
    "dominant_percent": 72.4,
    "sample_count": 138,
    "has_call_path": true,
    "has_wait_reason": false,
    "evidence_ref": "structured_evidence.stack_summary"
  },
  "call_path_hotspots": [
    {
      "function": "microservices_test.common.busy_cpu",
      "percent": 72.4,
      "samples": 138,
      "call_path": ["gateway", "order", "busy_cpu"],
      "endpoint": "/api/order/create",
      "service_id": "order-service",
      "instance_id": "order-1",
      "evidence_ref": "structured_evidence.call_path_hotspots[0]"
    }
  ],
  "confidence_inputs": {
    "sample_count": 138,
    "dominant_percent": 72.4,
    "context_completeness": "medium",
    "token_safety": "compact_summary_only"
  }
}
```

### 17.4 与现有 RCA 的接入方式

接入时不新增另一套归因协议，而是把结构化输出映射回现有字段：

| 结构化层输出 | 现有消费字段 |
|---|---|
| `top_functions` | `EvidenceInput.top_functions` |
| `evidence_index` | `EvidenceInput.evidence_index` |
| `confidence_inputs` | `EvidenceInput.evidence_index.confidence_inputs` |
| `call_path_hotspots` | `EvidenceInput.evidence_index.call_path_hotspots` |
| `artifact_refs` | `EvidenceInput.evidence_index.artifact_refs` |

这样 Analyzer 原有的 `facts / symptoms / localizations / attributions / evidence_challenges / ai_tree / graph_links` 不需要换协议，只是拿到更完整、更稳定、更节省 token 的证据输入。

### 17.5 稳定性要求

结构化处理层必须满足：

- 同一组 artifacts 多次处理，输出字段顺序和热点排序稳定。
- TopN 只按明确数值排序，数值相同再按函数名排序。
- 原始 SVG、raw perf 等大产物默认只作为引用，不进入 LLM。
- `evidence_ref` 必须稳定，不能依赖随机 id。
- 当无法解析出热点时，必须输出空结构和 `confidence_inputs.parse_status=insufficient_structured_signal`，而不是猜函数名。

### 17.6 已完成项

- [x] 已新增证据结构化处理层方案
- [x] 已新增结构化层任务分组
- [x] 已实现 `top_json / depth_evidence_json / flamegraph_json / flamegraph_svg / sys_metrics` 的最小结构化闭环
- [x] 已让 RCA 入口优先消费结构化证据
- [x] 已补充结构化层稳定性与 token 安全测试

## 18. 任务驱动诊断闭环与 Persistent Agent 路线

当前系统以任务驱动为主：用户创建诊断会话后，系统采集一批证据并自动进入 Analyzer。为了让 AI 树真正参与后续探测，本方案不新增 `nexttask` 接口，而是复用现有的：

```text
analysis_result.ai_tree[].next_evidence_requests
```

完整闭环如下：

```text
首批采集任务
  -> 结构化证据
  -> Evidence-to-Attribution / AI 树
  -> next_evidence_requests
  -> 受限证据请求映射
  -> ProbePlan / 子任务
  -> 采集结果回灌同一 diagnosis_id
  -> 再次结构化与诊断
```

### 18.1 证据请求与任务计划的职责边界

| 对象 | 职责 |
|---|---|
| `next_evidence_requests` | AI 树表达下一步需要补充的证据类型，不生成命令、不直接创建任务 |
| `ProbeDefinition` | 系统白名单，定义证据类型对应的探针、风险、平台和预算成本 |
| `ProbePlan` | 系统将证据请求映射成的可执行计划 |
| 子任务 | 既有任务生命周期中的实际采集任务 |
| `diagnosis_id` | 将首批任务、补证子任务和多轮分析关联为同一个诊断会话 |
| `evidence_gap` | 说明本次请求解决哪个证据缺口，用于去重和审计 |

AI 只能提出稳定的证据类型，例如：

```text
off_cpu_wait_profile
trace_endpoint_profile
baseline_window_profile
```

系统只允许将这些请求映射到已注册的 `ProbeDefinition`，不允许 AI 生成任意 shell 命令、未知采集器或未注册参数。

### 18.2 自动执行策略

默认使用全局安全策略，单次诊断可覆盖：

```text
auto_execute_policy = safe_only       # 默认：仅自动执行低风险探针
auto_execute_policy = all_registered  # 显式允许注册探针自动执行，包括高风险

auto_execute_policy = manual          # 所有补证任务等待人工审批
```

即使使用 `all_registered`，仍必须满足：

- 只能执行注册探针。
- 只能执行 AI 树输出的证据类型。
- 必须通过诊断预算、平台能力和风险校验。
- 记录 `parent_task_id`、`diagnosis_id`、`evidence_gap` 和风险等级。
- 失败或已执行的请求不能无限重试。

### 18.3 同一缺口默认去重

同一诊断会话中，以下组合默认只允许生成一次补证任务：

```text
diagnosis_id + evidence_gap
```

这样可以避免 AI 树在证据未改善时不断重复生成同一探针。后续如需处理突发问题的有限复核，可增加显式的复核次数预算，但不作为第一阶段默认行为。

### 18.4 诊断会话终止条件

任务驱动闭环不是无限监控。会话在以下情况之一成立时结束：

- AI 树进入 `clear_leaf`，证据足够稳定。
- AI 树进入 `conservative_leaf` 或 `unknown_leaf`，继续补证收益不足或无法继续。
- 进入 `INSUFFICIENT_EVIDENCE`、`BUDGET_EXHAUSTED`、`FAILED` 或 `USER_CANCELED`。
- 所有已生成子任务和结构化证据均完成处理。

这里结束的是当前诊断会话，不代表后续不能升级为持久化监测。

### 18.5 Persistent Agent 后续路线

突发 Bug 可能在用户创建任务时已经消失，任务驱动闭环无法补回已经错过的现场。因此 Persistent Agent 保留为后续演进方向，但不与第一阶段任务闭环混实现。

推荐路线：

```text
阶段 1：任务驱动自动诊断闭环
  采集 -> AI 树 -> next_evidence_requests -> ProbePlan -> 子任务 -> 再诊断

阶段 2：轻量 Persistent Trigger Agent
  常驻低成本指标 -> 保存短期窗口 -> 记录 trigger_event

阶段 3：本地 ring buffer / rolling profile
  持续保留最近窗口 -> 异常触发时冻结前后证据

阶段 4：触发式诊断会话
  冻结证据 -> 自动创建 diagnosis_id -> 复用同一结构化证据和 AI 树
```

Persistent Agent 的职责是保存现场和提供时间锚点，不直接判断 root cause。后续应复用当前 `ProbePlan`、子任务、`structured_evidence` 和 RCA 协议，而不是新建另一套诊断链路。

### 18.6 本阶段完成项

- [x] 已将 `next_evidence_requests` 解析为受限 ProbePlan
- [x] 已自动创建补证子任务并回灌同一诊断会话
- [x] 已实现同一 `diagnosis_id + evidence_gap` 默认去重
- [x] 已实现 `safe_only / all_registered / manual` 执行策略
- [x] 已补充任务驱动闭环测试
- [x] 已记录 Persistent Agent、ring buffer 和触发式诊断后续路线

## 19. 证据同窗性与 Persistent Evidence Trigger 方案

任务驱动闭环已经解决了“证据不足后如何继续补证”的问题，但它仍然只能观察任务执行窗口内发生的事情。对于瞬时异常，用户创建任务或 AI 树请求补证时，异常现场可能已经消失。

因此下一阶段不应把 Agent 升级成“自动诊断器”，而应增加一个轻量的证据触发与窗口管理层：

```text
Persistent Evidence Trigger
  -> trigger_event
  -> collector_group / rolling_snapshot
  -> evidence_cohort
  -> evidence_structuring
  -> Evidence-to-Attribution / AI 树
  -> 阶段性结论 / 补证请求 / 最终结论
```

该方案的核心不是“自动判断根因”，而是：

- 用轻量偏移检测捕捉值得采证的时间窗口。
- 用 `evidence_cohort_id` 保证多采集器证据同窗。
- 用 `timing_relation` 防止延迟补采被错误当作反证。
- 复用现有结构化证据层、AI 树、`ProbeDefinition` 和任务生命周期。

### 19.1 当前已具备与仍缺失的能力

前一阶段已经具备诊断会话内的自动分析能力：

```text
diagnosis 会话内
  -> 首批采集任务
  -> 结构化证据
  -> AI 树分析
  -> next_evidence_requests
  -> 补证 ProbePlan
  -> 回灌同一 diagnosis_id
```

下一阶段缺的不是重新实现这条闭环，而是补齐三个入口与协议：

| 能力 | 当前状态 | 下一阶段目标 |
|---|---|---|
| 诊断会话内结构化分析 | 已具备 | 继续复用 |
| 诊断会话内补证闭环 | 已具备 | 继续复用 |
| 普通采集任务完成后自动分析 | 未形成统一入口 | 自动创建或绑定 `analysis_session` |
| 异常发生前后的现场保存 | 未形成协议 | 引入 `trigger_event` 与 `evidence_cohort` |
| 不同时间窗口证据裁决 | 未显式建模 | 引入 `timing_relation` |

### 19.2 核心对象

#### `trigger_event`

`trigger_event` 是时间锚点，只表示“这个窗口值得采证”，不表示根因成立。

最小字段：

```json
{
  "trigger_event_id": "evt_001",
  "trigger_type": "cpu_shift",
  "target": {
    "service_id": "service-a",
    "instance_id": "service-a-1",
    "agent_id": "a1",
    "pid": 1234
  },
  "observed_at": "2026-08-11T10:15:03Z",
  "baseline_window": {
    "start": "2026-08-11T10:10:03Z",
    "end": "2026-08-11T10:15:03Z"
  },
  "trigger_window": {
    "start": "2026-08-11T10:14:48Z",
    "end": "2026-08-11T10:15:03Z"
  },
  "trigger_signal": {
    "metric": "cpu_user_pct",
    "baseline": 22.4,
    "current": 81.7,
    "shift_score": 3.8
  },
  "confidence": "suspected",
  "action": "start_collector_group"
}
```

#### `evidence_cohort`

`evidence_cohort` 表示同一窗口内的一组证据。它将 metrics、trace、profile、off-CPU、日志窗口等结果绑定在一起，避免 Analyzer 把不同时间的证据混用。

最小字段：

```json
{
  "evidence_cohort_id": "cohort_001",
  "trigger_event_id": "evt_001",
  "target_scope": ["service-a-1"],
  "collection_mode": "triggered_group",
  "window_start": "2026-08-11T10:14:48Z",
  "window_end": "2026-08-11T10:15:18Z",
  "artifact_refs": ["sys_metrics", "perf_cpu", "trace_endpoint_profile"]
}
```

#### `timing_relation`

`timing_relation` 表示单份证据与触发窗口的时间关系。

允许值建议为：

```text
same_window
pre_trigger_window
post_trigger_window
delayed_followup
stale_window
unknown
```

AI 树必须遵守：

```text
delayed_followup 没复现，不能直接反证 same_window 的异常证据。
```

### 19.3 采集模式

`collection_mode` 用于说明证据是如何产生的：

| 模式 | 含义 |
|---|---|
| `manual_single` | 用户或系统主动创建的单个普通采集任务 |
| `manual_group` | 用户或系统主动创建的一组同窗采集任务 |
| `triggered_group` | Persistent Trigger 触发的一组采集任务 |
| `rolling_snapshot` | 从本地 rolling buffer 冻结出的异常前后窗口 |
| `delayed_followup` | AI 树在后续轮次请求的补证任务 |
| `baseline_window` | 用于稳定性对比的基线窗口证据 |

普通任务不应被排除在 Analyzer 之外。没有 `diagnosis_id` 时，系统应自动创建或绑定一个 `analysis_session`，并将该任务标记为 `manual_single` 或 `manual_group`。

### 19.4 偏移触发，不做根因判断

Persistent Trigger Agent 不使用固定阈值直接判断异常，更不输出根因。它只判断当前窗口相对最近基线是否出现明显偏移。

示例信号：

- 当前 CPU 明显高于最近 5 分钟均值或分位数。
- 当前 P99 明显高于最近窗口。
- 当前 thread count 或 fd count 增长速度异常。
- 当前 iowait 出现短时突刺。
- 当前 endpoint timeout 或 error 出现短时集中。

这里允许少量规则，但规则只负责选择采集器：

| 触发类型 | 建议采集组 |
|---|---|
| `cpu_shift` | `sys_metrics` + `perf_cpu` + `stack_summary` |
| `latency_shift` | `trace_endpoint_profile` + `sys_metrics` |
| `io_wait_shift` | `ebpf_io` + `off_cpu_wait_profile` + `sys_metrics` |
| `thread_growth_shift` | `off_cpu_wait_profile` + `memory_smaps` |
| `memory_growth_shift` | `memory_smaps` + `baseline_window_profile` |
| `error_burst` | `logs_error_window` + `trace_endpoint_profile` |

这些规则不产生 root cause，只产生“该采什么证据”的计划。

### 19.5 Rolling Buffer / Triggered Snapshot

Rolling Buffer 用于保存最近一段时间的低成本证据，触发时冻结异常前后窗口。

对于瞬时异常，后续补深度任务不能被当成“恢复现场”。如果异常已经消失，延迟补证只能说明后续窗口是否复现，不能替代异常发生当下的同窗证据。

因此触发后的推荐链路必须是保存现场优先：

```text
Persistent Agent 低成本常驻观察
  -> 发现相对偏移
  -> 立即冻结 rolling snapshot
  -> 保存 trigger_event / evidence_cohort
  -> 保存轻量栈 / trace summary / endpoint summary / metrics window
  -> 同时按策略启动短窗口深采集
  -> AI 树优先分析 same-window snapshot
  -> 如果证据不足，再请求 delayed follow-up
```

关键原则：

- `rolling_snapshot` 是瞬时异常的主现场证据。
- `collector_tasks` 是触发后的补充证据，不是唯一证据来源。
- `delayed_followup` 不是时光机，不能恢复未保存的异常现场。
- AI 树应优先消费 `same_window` snapshot，再解释后续补证是否复现。
- 默认策略建议为 `freeze_and_safe_probe`：先冻结现场，再自动启动 `R1` 低风险短窗口补采；`R2` 深采集需要 `auto_all_registered` 或后续审批策略。

第一版建议：

```text
pre_window = 30s
post_window = 30s
max_retention = 5m
```

先保留低成本数据：

- metrics ring buffer
- endpoint latency summary
- trace summary buffer
- lightweight stack summary

触发后生成 snapshot：

```json
{
  "snapshot_id": "snap_001",
  "trigger_event_id": "evt_001",
  "evidence_cohort_id": "cohort_001",
  "snapshot_type": "rolling_snapshot",
  "pre_window_seconds": 30,
  "post_window_seconds": 30,
  "artifact_refs": [
    "metrics_window_json",
    "trace_summary_json",
    "stack_summary_json"
  ]
}
```

更重的 continuous profiling 作为后续升级，不在第一版直接开启。

### 19.6 AI 树同窗裁决要求

AI 树需要理解同窗关系：

- `same_window` 证据优先用于支持或挑战异常窗口内结论。
- `delayed_followup` 只能说明后续窗口状态，不能直接推翻异常窗口。
- `stale_window` 只能作为历史参考，不能作为主证据。
- 多个 `same_window` 证据冲突时，进入冲突裁决分枝。
- 只有 `trigger_event` 但缺少结构化证据时，输出“发现疑似窗口，但根因证据不足”。

报告中应明确显示：

```text
结论适用窗口
证据同窗关系
延迟补采是否复现
哪些证据不能互相反证
```

### 19.7 分阶段落地路线

#### 阶段 1：证据同窗协议

补齐 `trigger_event_id`、`evidence_cohort_id`、`collection_mode`、`window_start`、`window_end`、`timing_relation` 字段，让所有证据都能表达时间来源。

#### 阶段 2：Analysis Session 普适入口

诊断会话内自动分析已经具备。这里要补的是普通采集任务完成后的统一分析入口：

```text
普通 task DONE
  -> 自动结构化证据
  -> 自动创建或绑定 analysis_session
  -> 生成阶段性结论
```

#### 阶段 3：Persistent Evidence Trigger

新增轻量常驻触发器，维护滑动窗口，发现相对基线偏移后生成 `trigger_event`，并触发同窗 collector group。

#### 阶段 4：Rolling Buffer / Triggered Snapshot

在 Agent 本地保留最近 N 秒低成本证据，触发后冻结异常前后窗口，形成 `rolling_snapshot`。

#### 阶段 5：AI 树同窗证据裁决

让 Analyzer 使用 `timing_relation` 约束结论边界，避免延迟补采误反证异常现场证据。

#### 阶段 6：WatchSubscription 驱动的 Persistent Agent Runtime

补齐“agent 怎么知道自己监视谁”的控制面。诊断任务不直接驱动常驻监视，用户或系统先创建 `WatchSubscription`，Agent 通过 `WatchLease` 获得自己负责的监视对象。

```text
WatchSubscription
  -> WatchLease
  -> PersistentAgentRuntime
  -> evaluate_persistent_trigger
  -> trigger_event / evidence_cohort / collector_tasks
```

这一阶段仍然只解决监视目标、低成本窗口和触发交接，不做根因判断。

#### 阶段 7：WatchIncident / Frozen Evidence Inbox

补齐“一个监视对象可能多次异常”的产品与数据模型。`WatchSubscription` 不再只保存最近一次触发，而是追加多个 `WatchIncident`，每个 incident 表示一次被冻结的异常现场。

```text
WatchSubscription
  -> WatchIncident[]
      -> trigger_event
      -> evidence_cohort
      -> rolling_snapshot
      -> collector_tasks
      -> analysis_session
```

前端以可展开列表展示：

```text
监视对象
  -> 异常窗口列表
      -> 冻结证据
      -> 关联采集任务
      -> AI 树分析状态
```

这使“已经冷冻现场但还没 AI 树分析”的异常不会丢失，也不会被覆盖为“最近一次触发”。

#### 阶段 8：WatchIncident -> AI 树分析入口

补齐“冷冻现场已经保存，但还没分析”的最后一段链路。AI 树分析对象不是整个 watch，而是某一次 `WatchIncident`。

```text
WatchIncident
  -> structured_evidence
  -> rca_inputs
  -> Evidence-to-Attribution / AI tree
  -> analysis_result
  -> 回写 analysis_status
```

约束：

- 分析优先使用 incident 内的 `same_window` frozen snapshot。
- 不为了分析 incident 伪造采集任务。
- 不把 delayed follow-up 当作恢复现场。
- 从 incident 发起分析时不自动执行修复动作。
- 第一版将分析结果挂回 incident；后续再升级为持久化 analysis session。

### 19.8 本方案边界

本阶段不做：

- 不实现完整监控系统。
- 不让 Persistent Trigger Agent 输出根因。
- 不默认开启高成本 continuous profiling。
- 不绕过现有 `ProbeDefinition`、审批和预算机制。
- 不把 `WatchSubscription` 第一版直接落成数据库持久化表；当前先完成控制面协议、API、前端入口和运行时闭环。
- 不把 `WatchIncident` 第一版直接等同为告警或 RCA；它只是被冻结的异常现场。

本阶段要保证：

- 任务驱动和触发式证据进入同一套结构化证据层。
- 所有证据都能追溯到窗口、触发事件和采集模式。
- AI 树发表结论时明确证据时间边界。
- 前端能看到 agent 当前被要求监视哪些对象、最近是否触发过证据窗口。
- 前端能展开监视对象，查看该对象下所有待分析或已分析的异常窗口。
- 前端能从某个异常窗口发起 AI 树分析，并看到分析状态和摘要。

### 19.9 分阶段完成项

- [x] 阶段 1：已补齐证据同窗协议字段与结构化输出携带能力
- [x] 阶段 2：Analysis Session 普适入口
- [x] 阶段 3：Persistent Evidence Trigger 最小闭环
- [x] 阶段 4：Rolling Buffer / Triggered Snapshot
- [x] 阶段 5：AI 树同窗证据裁决
- [x] 阶段 6：WatchSubscription 驱动的 Persistent Agent Runtime 与前端入口
- [x] 阶段 7：WatchIncident / Frozen Evidence Inbox 可展开异常窗口
- [x] 阶段 8：WatchIncident -> AI 树分析入口

### 19.10 Persistent Agent 后续路线与复用验证

未来的 Persistent Agent 仍然只做证据时间锚点和窗口管理，不进入根因判断。它的生命周期建议保持为：

```text
agent_start
  -> register low-cost observers
  -> maintain rolling buffers
  -> emit trigger_event on relative shift
  -> freeze rolling_snapshot
  -> create or attach evidence_cohort
  -> hand over to existing diagnosis / analysis session
```

边界要求：

- Persistent Agent 只输出 `trigger_event`、`rolling_snapshot` 和 `evidence_cohort_id`。
- Persistent Agent 不输出 `root_cause`、`ranked_causes`、`repair_plan`。
- 触发规则只决定“是否值得采证”和“该启动哪组 collector”，不决定原因。
- 高风险或中风险采集仍必须经过现有 `ProbeDefinition`、审批策略、预算和 capability 校验。

Rolling Buffer 保留策略：

| 证据族 | 默认保留 | 触发后行为 | 进入 AI 路径方式 |
|---|---:|---|---|
| low-cost metrics | 5 分钟 | 冻结 pre/post window | `sys_metrics.summary` 摘要 |
| endpoint summary | 5 分钟 | 冻结 endpoint 聚合 | `evidence_index.context` 摘要 |
| trace summary | 5 分钟 | 冻结 trace 上下文 | `call_path_hotspots` / context 摘要 |
| lightweight stack summary | 2 分钟 | 冻结轻量栈摘要 | `top_functions` / `stack_summary` 摘要 |

当前合同复用验证：

- `trigger_event_id` 和 `evidence_cohort_id` 已能从触发器传入子采集任务。
- `collection_mode=triggered_group` 和 `collection_mode=rolling_snapshot` 已进入结构化证据层。
- `timing_relation` 已进入 facts、evidence challenges 和 conclusion boundary。
- `next_evidence_requests` 已能通过现有 `ProbeDefinition` 映射为 follow-up probe。
- follow-up probe 完成后会回到同一个 `diagnosis_id` 重新分析并生成新 conclusion version。

因此未来升级成真正常驻进程时，不需要新建第二套归因链路，只要把 Persistent Agent 的输出接到现有：

```text
trigger_event / rolling_snapshot
  -> structured_evidence
  -> Evidence-to-Attribution / AI tree
  -> diagnosis session / analysis session
```

### 19.11 WatchSubscription 驱动的 Persistent Agent 方案

当前 `Persistent Trigger` 已能在给定 `target + baseline_window + trigger_window` 后生成 `trigger_event`、`evidence_cohort` 和同窗 collector group，但它本身不知道要监视哪个目标。因此需要增加独立控制面：

```text
用户 / 系统
  -> 创建 WatchSubscription
  -> Agent 查询 WatchLease
  -> Agent 维护对应 target 的低成本窗口
  -> Runtime 调用 Persistent Trigger
  -> 生成 trigger_event / evidence_cohort / collector_tasks
```

核心原则：

- `WatchSubscription` 表示“希望系统持续低成本观察哪个目标”。
- `WatchLease` 表示“某个 agent 当前实际承担哪个观察对象”。
- `PersistentAgentRuntime` 只把 watch 窗口送入触发器，不输出 RCA。
- `DiagnosisTask` 只消费已有 `evidence_cohort` 或创建主动采集任务，不直接决定 agent 常驻观察面。

最小对象：

```json
{
  "watch_id": "watch_001",
  "name": "order service watch",
  "target": {
    "agent_id": "agent_1",
    "target_pid": 4242,
    "service_id": "order-service",
    "instance_id": "order-1",
    "endpoint": "/orders"
  },
  "watch_profile": "low_cost_default",
  "enabled_collectors": ["sys_metrics", "light_stack", "trace_window"],
  "retention_seconds": 120,
  "trigger_policy": "relative_shift_only",
  "status": "active"
}
```

前端要求：

- 提供“持续监视”页面。
- 能创建 watch subscription。
- 能查看当前 active watch。
- 能查看某个 agent 领取到的 watch lease。
- 能看到最近触发的 `trigger_event_id`、`evidence_cohort_id` 和 `trigger_type`。
- 页面文案必须明确：watch 只表示证据观察，不代表根因结论。

第一版完成项：

- [x] 已实现 `WatchSubscription`、`WatchLease`、`WatchRegistry` 和 `PersistentAgentRuntime`。
- [x] 已提供 `/api/v1/watches`、`/api/v1/agents/{agent_id}/watch-leases`、`/api/v1/watches/{watch_id}/evaluate` API。
- [x] 已验证 watch 能复用 `evaluate_persistent_trigger` 生成同窗 collector group。
- [x] 已提供前端“持续监视”入口，用于创建 watch、查看 lease 和最近触发窗口。

后续升级：

- 将 `WatchRegistry` 从内存态升级为 SQL 持久化表。
- 增加 agent 侧自动拉取 watch lease 的后台循环。
- 增加每个 target 的 ring buffer 自动维护。
- 增加 watch 预算、并发上限、冷却时间和资源保护策略。
- 支持诊断任务自动复用最近相关 `evidence_cohort`。

### 19.12 WatchIncident 与 Frozen Evidence Inbox

`WatchIncident` 是一次异常窗口，不是一次根因结论。它用于承接“异常已经冻结，但还没有进入 AI 树分析”的中间状态。

状态建议：

| 状态 | 含义 |
|---|---|
| `frozen` | 已冻结现场，但没有自动补采任务 |
| `collecting` | 已冻结现场，同时启动了低风险短窗口补采 |
| `ready_for_analysis` | 证据已满足进入 AI 树的最低要求 |
| `analyzing` | AI 树正在分析 |
| `analyzed` | 已生成阶段性或最终结论 |
| `needs_evidence` | AI 树认为证据不足，需要补证 |
| `stale` | 历史窗口，只作为参考 |

第一版最小字段：

```json
{
  "incident_id": "inc_001",
  "watch_id": "watch_001",
  "trigger_event_id": "evt_001",
  "evidence_cohort_id": "cohort_001",
  "trigger_type": "cpu_shift",
  "window_start": "2026-08-11T10:14:48Z",
  "window_end": "2026-08-11T10:15:18Z",
  "status": "collecting",
  "analysis_status": "not_started",
  "snapshot_id": "snap_001",
  "snapshot_refs": ["rolling_metrics_summary", "rolling_stack_summary"],
  "collector_tasks": ["task_001", "task_002"]
}
```

第一版完成项：

- [x] 触发时追加 `WatchIncident`，不覆盖历史异常窗口。
- [x] 每个 incident 绑定 `trigger_event_id`、`evidence_cohort_id`、`snapshot_id`、`snapshot_refs` 和 `collector_tasks`。
- [x] `freeze_only` 策略只冻结现场，不自动创建采集任务。
- [x] `freeze_and_safe_probe` 策略先冻结现场，再创建 `R1` 低风险短窗口补采任务。
- [x] 前端 `WatchSubscription` 父列表支持展开查看异常窗口。
- [x] 已验证同一 watch 多次触发会产生多个 incident。

后续升级：

- 将 snapshot refs 接入证据查看器。
- 将 collector task 状态实时同步到 incident。
- 支持按 `ready_for_analysis / needs_evidence / analyzed` 筛选异常窗口。

### 19.13 WatchIncident -> AI 树分析入口

每个 `WatchIncident` 都保存同窗 `structured_evidence`，因此可以在异常已经结束后，基于冻结现场进入 AI 树分析。

第一版链路：

```text
POST /api/v1/watch-incidents/{incident_id}/analyze
  -> 读取 incident.structured_evidence
  -> 转成 rca_inputs
  -> 构造只读 synthetic task context
  -> run_diagnosis_context(auto_execute_safe=False)
  -> 回写 incident.analysis_status / analysis_session_id / analysis_result
```

这里的 synthetic task context 只用于兼容现有 RCA 引擎读取 `agent_id / target_pid / collector_type` 等元数据，不会写入任务表，也不会下发采集任务。

第一版完成项：

- [x] `WatchIncident` 已保存 `structured_evidence`。
- [x] 已提供 `/api/v1/watch-incidents/{incident_id}/analyze`。
- [x] 分析结果回写 `analysis_status`、`analysis_session_id` 和 `analysis_result`。
- [x] 前端异常窗口行已启用“AI 树分析”按钮。
- [x] 分析使用 `same_window` frozen snapshot，并关闭自动修复执行。

后续升级：

- 将 incident analysis 升级为可持久化 `analysis_session`。
- 支持从 `analysis_result.next_evidence_requests` 继续生成 delayed follow-up probe。
- 支持从 analyzed incident 跳转到完整报告页。
- 支持把 collector task 完成后的新证据合并回同一个 incident/cohort 再分析。

## 20. AI Ops v2 Benchmark Readiness Gate

本小节对应“方案 2”：先补齐评测前置门禁，再进入正式 90 轮 benchmark。它不替代 AI 树，也不直接提高根因准确率；它只回答一个更基础的问题：

```text
这轮诊断结果是否具备被 benchmark 公平评分的证据链条件？
```

如果门禁不通过，后续分数不能直接解释为“AI 归因能力差”，因为失败可能来自审计包缺失、collector fallback、artifact 未结构化、证据引用不可追溯等平台链路问题。

### 20.1 目标

方案 2 的目标是让正式评测前先具备以下能力：

- 诊断会话可以导出完整 `/api/v1/diagnoses/{diagnosis_id}/audit-bundle`。
- 审计包包含 runtime trace、probe plan、child task、artifact、evidence、structured evidence、conclusion 和 safety 信息。
- 评测脚本可以离线导入 `server.app.diagnosis.benchmark_score` 并完成 deterministic scoring。
- gRPC 下发时 `off_cpu_wait_profile`、`trace_endpoint_profile`、`baseline_window_profile` 不再静默退回 `perf_cpu`。
- `continuous_top_json`、`continuous_flamegraph_json`、`continuous_summary` 可以进入结构化证据层。
- readiness gate 能在正式跑 90 轮前暴露“证据链未闭合”的具体缺口。

### 20.2 新增审计包结构

审计包最小结构：

```json
{
  "schema_version": "1.0",
  "diagnosis_id": "diag_session_xxx",
  "run": {},
  "runtime_trace": [],
  "topology_snapshot": {},
  "probes": [],
  "child_task_ids": [],
  "tasks": [],
  "artifacts": [],
  "evidence": [],
  "structured_evidence": {},
  "evidence_refs": [],
  "conclusion": {},
  "latest_conclusion": {},
  "safety": {},
  "rollback": {},
  "readiness_gate": {}
}
```

其中 `runtime_trace` 来自诊断会话事件流，用于还原 intent、scope、probe plan、evidence、conclusion 等关键阶段。`structured_evidence` 来自 `structured_evidence_json`，用于证明 AI 树看到的是可检索、可引用的证据，而不是只看到 SVG 或半原始文本。

### 20.3 Readiness Gate 检查项

第一版门禁检查项：

| 检查项 | 含义 | 不通过时的解释 |
|---|---|---|
| `audit_bundle_exists` | 审计包成功生成 | API 或导出流程不可用 |
| `runtime_trace_non_empty` | 运行时审计轨迹非空 | 评分无法判断诊断过程是否真实发生 |
| `probes_non_empty` | 诊断规划过探针 | 没有进入采证链路 |
| `child_task_ids_non_empty` | 创建过子采集任务 | 诊断停在计划层，未进入任务执行 |
| `artifact_count_non_zero` | 子任务上传了 artifact | 采集或上传链路未闭合 |
| `structured_evidence_non_empty` | artifact 被结构化 | AI 树没有可消费证据 |
| `evidence_refs_non_empty` | 结论引用了证据 | 结论不可追溯 |
| `collector_type_not_fallback` | probe 与 task collector 匹配 | 存在静默 fallback 或假采集 |

门禁的设计原则是“宁可提前失败，也不要假通过”。例如旧版审计包可能存在 `trace` 和 `evidence_manifest`，但如果没有新结构里的 `structured_evidence`、`tasks`、`artifacts`，仍然不应被当作新门禁通过。

### 20.4 评分模块边界

`benchmark_score` 是离线 deterministic scorer，不参与诊断过程，也不会把私有 oracle 传给系统被测侧。

评分维度：

| 维度 | 权重 | 说明 |
|---|---:|---|
| root cause | 40 | 位置、故障域、分类、根因实体 |
| evidence | 25 | 必需采集器召回、证据引用有效率、独立证据源数量 |
| trace | 20 | runtime trace 阶段覆盖 |
| safety | 10 | 是否执行禁止动作 |
| recovery | 5 | 需要恢复验证的 case 是否成功 |

聚合输出包含：

- case 级严格根因准确率。
- run 级严格根因准确率。
- 95% Wilson 区间。
- 平均综合分。
- 证据引用有效率。
- runtime trace 覆盖率。
- 重复运行输出一致率。

### 20.5 Collector 映射门禁

正式评测前必须保证 server 下发的 collector 类型与 agent 执行的 collector 类型一致。

已明确的映射：

| collector | server task_type | profiler_type | agent collector |
|---|---:|---:|---|
| `trace_endpoint_profile` | 2 | 0 | `trace_endpoint_profile` |
| `off_cpu_wait_profile` | 8 | 4 | `off_cpu_wait_profile` |
| `baseline_window_profile` | 9 | 7 | `baseline_window_profile` |
| `log_scan` | 10 | 0 | `log_scan` |
| `dependency_check` | 11 | 0 | `dependency_check` |
| `redis_check` | 12 | 0 | `redis_check` |

这里 `log_scan`、`dependency_check`、`redis_check` 已不再复用 trace/endpoint 作为占位，而是进入 Agent 专用 collector 路由。它们的实现边界是“工业采集器适配器”：Mini-Drop 不自研日志 tail、协议探测或 Redis RESP 采集，只读取 Fluent Bit / OTel、Blackbox Exporter、Redis Exporter 等成熟组件的输出，并转换为结构化 artifact。

### 20.6 Baseline Artifact 结构化

`baseline_window_profile` 可能输出 continuous 系列 artifact：

```text
continuous_top_json
continuous_flamegraph_json
continuous_summary
```

为避免“采到了但 AI 树看不到”，结构化入口会做兼容映射：

```text
continuous_top_json -> top_json
continuous_flamegraph_json -> flamegraph_json
continuous_summary -> depth_evidence_json.baseline_summary
```

该映射只改变 artifact 进入结构化层的方式，不改变根因判断逻辑。

### 20.7 推荐测试顺序

正式 90 轮前建议按三步走：

1. 先跑 readiness gate，确认新审计包满足证据链门槛。
2. 再跑 6 到 8 个代表 case 的 smoke，每个 case 先 1 次，看采集与结构化是否真实有效。
3. 最后跑 30 个 case、每个 3 次的正式 benchmark。

旧目录 `docs/ai_ops_v2_test/审计包` 中的历史包可用于评分回放和问题对照，但不代表新 readiness gate 已通过。新评测应使用当前代码重新导出的审计包。

### 20.8 已完成项

- [x] 已补齐 `/api/v1/diagnoses/{diagnosis_id}/audit-bundle` 导出接口。
- [x] 已补齐 `server.app.diagnosis.benchmark_score` 离线评分模块。
- [x] 已补齐 readiness gate 脚本 `docs/ai_ops_v2_test/评测脚本/check_readiness_gate.py`。
- [x] 已修正评测脚本仓库根路径，避免从中文脚本目录执行时 import 失败。
- [x] 已补齐 `trace_endpoint_profile`、`off_cpu_wait_profile`、`baseline_window_profile` 的 server/agent 显式映射。
- [x] 已补齐 continuous baseline artifact 进入结构化证据层的兼容映射。
- [x] 已增加 focused tests 覆盖审计包导出、评分模块、collector 映射和 baseline 结构化。
- [x] 已补齐 `log_scan`、`dependency_check`、`redis_check` 的工业采集器适配器，分别输出 `log_window_json`、`dependency_check_json`、`redis_check_json`。
- [x] 已补齐工业适配器 artifact metadata：`collector_family`、`evidence_window`、`trigger_event_id`、`evidence_cohort_id`、`collection_mode`、`timing_relation`。
- [x] 已补齐 readiness gate：完成的 probe 如果缺少对应结构化 artifact，门禁失败。

### 20.9 工业采集器适配层

当前不再自研轻量 `log_scan`、`dependency_check`、`redis_check` 采集器。原因是日志 tail、多行合并、offset、文件轮转、DNS/TCP/HTTP/gRPC 探测、Redis ACL、INFO、SLOWLOG、LATENCY 等能力都有大量边界，长期维护会把 Mini-Drop 推向“自写一套不成熟观测采集器”。

已落地的方向是工业采集器适配层：

| 证据族 | 工业采集来源 | Mini-Drop 责任 | 结构化产物 |
|---|---|---|---|
| `log_scan` | Fluent Bit Tail/Multiline 或 OpenTelemetry Collector `filelogreceiver` | 读取采集输出、裁剪窗口、聚类、生成证据引用 | `log_window_json` |
| `dependency_check` | Prometheus Blackbox Exporter `/probe` | 下发目标、读取 probe metrics、转换失败阶段和耗时 | `dependency_check_json` |
| `redis_check` | Redis Exporter metrics，必要时受控 Redis 快照 | 汇总 Redis 连接、内存、慢命令、延迟事件 | `redis_check_json` |

工业采集器负责：

```text
稳定采集、日志轮转、多行解析、协议细节、TLS/gRPC/Redis 边界、超时控制
```

Mini-Drop 负责：

```text
任务编排、证据窗口、artifact 保存、evidence_ref、结构化 evidence_index、AI 树补证请求
```

每个适配器的输出必须是结构化证据，而不是把原始日志或 exporter metrics 直接交给 AI：

```text
raw collector output
  -> adapter normalization
  -> structured artifact
  -> evidence_index
  -> AI tree / Evidence-to-Attribution
```

结构化产物最低要求：

```text
artifact_type
collector_family
evidence_window
trigger_event_id
evidence_cohort_id
collection_mode
timing_relation
summary
evidence_ref
```

后续仍需：

- 将 readiness gate 结果接入前端诊断详情页，让用户看到“这份结论是否可评测 / 可审计”。
- 为 runtime snapshot 建立统一证据族标记，使 `java_async`、`go_pprof`、`pyspy`、`off_cpu_wait_profile` 在 benchmark 里稳定归入 `runtime_snapshot`。
- 基于结构化证据族实现最小必要采集器选择：例如 `downstream_dependency` 缺 `dependency_check` 时先请求 Blackbox 适配证据，缺 `log_scan` 时再请求日志适配证据，Redis 相关证据不足时再请求 Redis Exporter 适配证据。

### 20.10 Managed Collector Profile

上一节解决的是“不要自研轻量采集器”，但如果仍要求用户每次诊断手动配置 Fluent Bit、Blackbox Exporter、Redis Exporter，那么工业采集器会变成新的配置负担。因此本轮补齐的是托管采集器配置层：

```text
worker compose
  -> 默认启动 Fluent Bit / Blackbox Exporter sidecar
  -> Redis Exporter 作为可选 profile
  -> Agent 启动时自动探测可用性
  -> RegisterAgent 上报 CollectorProfile
  -> Server 保存到 latest_metrics
  -> /api/agents 暴露
  -> Dashboard / AgentDetail 展示采集能力
```

`CollectorProfile` 不替代原有 `capabilities`：

- `capabilities` 仍用于任务路由，表示 Agent 构建中注册了哪些 collector。
- `collector_profile` 用于产品可用性，表示这些 collector 当前是否真的具备默认来源、sidecar、命令或 exporter endpoint。

已完成项：

- [x] Agent 侧新增 `CollectorProfile` 自动发现，覆盖 `log_scan`、`dependency_check`、`redis_check` 和 runtime 工具。
- [x] `RegisterAgentRequest` 增加 `collector_profile_json`，Agent 注册时上报托管采集器状态。
- [x] Server 将 profile 保存到 `latest_metrics.collector_profile`，并在 `/api/agents` 同步暴露 `collector_profile`。
- [x] Worker compose 默认托管 Fluent Bit 和 Blackbox Exporter，分别提供日志管道输出和 DNS/TCP/HTTP 依赖探测能力。
- [x] Redis Exporter 作为 `redis` compose profile 保留，未配置时前端显示为不可用，不伪装成已就绪。
- [x] 前端 Dashboard 增加采集能力摘要，Agent 详情页增加采集能力表格。
- [x] 已补充测试覆盖 profile 发现、注册持久化、API 暴露，并通过 worker compose 配置校验。

这个层的意义不是让 Mini-Drop 自动猜出所有业务依赖，而是把“工业采集器是否可用”变成系统可见状态。用户不需要在每个诊断任务里重复填写采集器参数；系统可以先使用 worker 默认来源，缺失时在前端明确显示原因。

默认行为：

| 证据族 | 默认来源 | 用户是否必须每次配置 | 未就绪时行为 |
|---|---|---:|---|
| `log_scan` | Fluent Bit 输出 `/var/lib/mini-drop/logs/logs.ndjson` | 否 | profile 标记 `degraded` 或 `unavailable` |
| `dependency_check` | `http://blackbox-exporter:9115` | 否 | profile 标记 `degraded` |
| `redis_check` | Redis Exporter `redis` profile | 否，只有需要 Redis 专项证据时配置一次 | profile 标记 `unavailable` |

后续升级方向：

- 将 `CollectorProfile` 从注册时快照升级为周期性心跳刷新，避免 sidecar 后续异常但 UI 仍显示旧状态。
- 在诊断创建页展示目标 Agent 的采集能力缺口，提前提示“这次结论最多能定位到哪一层”。
- 将最小必要采集器选择接入 profile，可用时自动请求，缺失时输出明确的 `missing_evidence_family`。
- Redis profile 可以进一步升级为 watch/topology 驱动：当 topology 中声明 Redis 依赖时，自动提示启用 Redis Exporter，而不是让用户自己猜。

### 20.11 Target-Scoped Collector Invocation

`CollectorProfile` 只能解决“Agent 有无能力”，不能解决“这次任务要采谁”。如果把 Redis 地址、依赖目标或日志选择器放在 Agent 注册配置里，会出现三个问题：

- Agent 注册早于任务创建，注册时不知道后续要诊断哪个业务目标。
- 同一个 Agent 可能同时监视多个服务、多个 watch、多个 Redis 依赖。
- 全局 Redis 配置会让后续任务误用旧目标，造成证据串线。

因此采集配置必须拆成三层：

```text
Agent CollectorProfile
  -> 这个 worker 有没有 log_scan / dependency_check / redis_check 能力

WatchSubscription / Diagnosis target_config
  -> 这次要监视哪个服务、进程、endpoint、依赖、Redis、日志选择器

CollectorTask collector_invocation
  -> 本次实际采集的目标快照，绑定 task_id / diagnosis_step_id / evidence_cohort_id
```

正确链路：

```text
用户 / 系统创建诊断或 watch
  -> context.dependencies / watch.target_config 声明目标
  -> ProbePlan.parameters 写入 target_config
  -> CreateTaskRequest.options 写入 collector_invocation
  -> Agent collector 只读取本次 task options
  -> structured artifact 原样保存 collector_invocation
```

已完成项：

- [x] 已定义 `collector_invocation`，包含 `collector_family`、`scope_source`、`target_config`、`agent_id`、`service_id`、`instance_id`、`diagnosis_step_id`。
- [x] `dependency_check` 使用本次任务的 `targets`，适配 Blackbox Exporter 多目标 `/probe?target=...&module=...`。
- [x] `redis_check` 使用本次任务的 Redis target，适配 Redis Exporter 多目标 `/scrape?target=...` 或 fixture metrics。
- [x] 结构化 artifact 输出 `collector_invocation`，让审计包能证明“这份证据采的是哪个目标”。
- [x] 测试覆盖同一 Agent 两个诊断 / 两个 Redis 目标不会互相串配置。

边界：

- `CollectorProfile.redis_check = available` 只表示 Redis Exporter 工具链可用，不表示当前业务 Redis 已配置。
- 没有 `target_config.redis_target` 时，`redis_check` 不能使用全局 Redis 地址冒充业务目标。
- 不同 Redis ACL / 密码 / TLS 配置后续应通过 `secret_ref` 或 credential group 表达，不进入 LLM 输入，也不写入普通日志。

### 20.12 统一 Watch 与诊断采集契约

`Target-Scoped Collector Invocation` 不能只服务普通诊断，否则系统会形成两套目标路由协议：

```text
普通诊断：DiagnosisContext -> collector_invocation -> task options
持续监视：WatchSubscription -> trigger collector_tasks
```

统一后的协议是：

```text
任意采集入口
  -> target_config
  -> collector_invocation
  -> task options
  -> Agent collector
```

其中：

- 普通诊断的 `scope_source = diagnosis_target_scope`。
- 持续监视的 `scope_source = watch_subscription`。
- Agent collector 不关心任务来源，只读取 `collector_invocation.target_config`。

已完成项：

- [x] 新增共享 `build_collector_invocation` 构造器。
- [x] 普通诊断编排和 Persistent Trigger 统一使用该构造器。
- [x] `WatchSubscription` 与 `WatchLease` 增加 `target_config`。
- [x] Watch 触发采集时将 `target_config` 写入 triggered task options。
- [x] `WatchIncident.collector_tasks` 保存同一份 `collector_invocation`，便于前端和审计追溯。
- [x] 前端持续监视创建页增加可选 `target_config` JSON 输入。

示例：

```json
{
  "scope_source": "watch_subscription",
  "watch_id": "watch_order",
  "collector_family": "sys_metrics",
  "probe_id": "host_process_metrics",
  "target_config": {
    "redis_target": {
      "host": "order-redis",
      "port": 6379,
      "url": "redis://order-redis:6379"
    }
  },
  "target_context": {
    "agent_id": "agent-1",
    "service_id": "order-service",
    "instance_id": "order-1",
    "pid": 1234
  }
}
```

这样 watch 触发采集时也不会使用 Agent 全局 Redis 配置；每个 watch、incident、collector task 都有自己的目标快照。
