# 冻结证据包诊断设计

## 目标

为 Persistent Watch 保存的 `WatchIncident` 和外部证据结果包提供冻结证据诊断模式。该模式只消费已经保存的现场证据，不因 AI 树提出新的证据需求而创建实时采集任务。

## 范围

本次只支持两种诊断语义：

- `live_collection`：普通诊断，沿用当前初始探针和 follow-up 采集链路。
- `frozen_evidence`：冻结证据诊断，只分析证据包，缺失证据时停止在证据边界。

本次不支持 `hybrid`，不增加运行中模式切换，也不修改普通诊断的默认行为。

## 数据流

```text
WatchIncident / evidence package
  -> structured evidence
  -> Analyzer facts and candidates
  -> session AI tree
  -> resolve requested evidence family in package
       -> valid/partial: reuse and continue analysis
       -> missing/invalid: retain boundary and stop
```

冻结模式下，AI 树仍可以表达逻辑上的下探节点和缺失证据，但不能执行新的采集动作。

## 入口与编排

WatchIncident 的分析入口应接入已有的冻结现场分析路径，使用 incident 中的：

- `structured_evidence`
- `snapshot_refs`
- `evidence_cohort_id`
- `window_start` / `window_end`
- `timing_relation=same_window`

冻结模式不得进入普通诊断的初始探针计划，也不得进入 follow-up 探针计划。禁止点必须覆盖：

- `_plan_and_schedule()`
- `_plan_followup_requests()`
- 后台 `advance` 触发的延迟调度

普通 `live_collection` 路径保持现有实现。

## 证据复用规则

AI 树提出 `evidence_family` 后，系统按以下条件在证据包内解析：

- 证据族匹配；
- 目标匹配，包括 agent、PID、instance；
- `evidence_cohort_id` 匹配；
- 时间窗口和 `timing_relation` 可解释；
- 证据状态为 `valid` 或 `partial`。

命中时在树的证据边上记录复用结果；`partial` 证据可以继续分析，但必须保留定位和结论资格边界。冻结证据包不把后续窗口伪装成同窗证据。

## 缺失证据行为

冻结模式下，如果证据包没有满足请求的证据：

- 不调用 `evidence_gap_to_probe_id()` 创建新任务；
- 不创建 `delayed_followup`；
- 保留 `missing_evidence` 和 `qualification_boundary`；
- AI 树记录停止原因 `evidence_package_exhausted`；
- 结论只能停留在当前证据支持的层级，必要时输出 `insufficient_evidence` 或 `partial_completed`。

## 审计与验证

诊断结果和 audit bundle 需要能够区分：

- 实际诊断模式；
- 原始证据包和 cohort；
- 命中的复用证据；
- 缺失证据；
- 停止原因；
- 新建探针数量。

核心验收条件：

```text
frozen_evidence 的 probe_count == 0
frozen_evidence 的证据引用来自证据包
证据包已有证据可被 AI 树复用
证据包缺失证据时不创建任务
live_collection 的既有测试行为不变
```

## 非目标

- 不实现 hybrid 或用户确认后继续补采；
- 不重写 Analyzer、Probe Registry 或 AI 树模型；
- 不改变普通诊断的自动执行策略；
- 不把 delayed follow-up 作为冻结现场的替代品。
