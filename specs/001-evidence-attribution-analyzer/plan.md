# Implementation Plan: Evidence-to-Attribution Analyzer

**Branch**: `[001-evidence-attribution-analyzer]` | **Date**: 2026-07-26 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-evidence-attribution-analyzer/spec.md`

**Note**: This plan uses the Spec Kit plan template and limits implementation to the existing RCA processing path.

## Summary

Add an evidence-first analysis stage between candidate calibration and LLM report generation. The stage converts existing RCA evidence into facts, symptoms, localization boundaries, guarded attributions, and evidence challenge results. Both current RCA strategies reuse this stage; the LLM receives the structured result and is validated against its permitted conclusions.

## Technical Context

**Language/Version**: Python 3.11

**Primary Dependencies**: FastAPI, Pydantic, existing RCA rule engine and AI provider

**Storage**: Existing task repository and RCA report snapshot; no schema or persistence change required for the first version

**Testing**: pytest

**Target Platform**: Linux performance diagnosis service, developed and tested on Windows

**Project Type**: Python web service with React client; the original MVP changed the server-side RCA path, while the current extension also changes the diagnosis API contract and React diagnosis detail view

**Performance Goals**: Analysis of a normal RCA evidence snapshot adds no external calls and completes within the existing diagnosis request path

**Constraints**: Reuse formatted collector data; do not schedule collection tasks; do not add historical-case dependencies; do not let the LLM introduce cause IDs or evidence outside the structured analysis result

**Scale/Scope**: One shared Analyzer module, two existing RCA strategy integration points, one existing LLM validation path, and focused unit/integration tests

## Scheme B Runtime Boundary and Kubernetes Migration

方案 B 当前以三节点 Docker VM 作为真实验收环境，负责验证工业采集器、结构化证据、AI 树补证和 Persistent Watch 的完整闭环。Kubernetes 不属于方案 B 的当前完成条件，而是后续的环境后端迁移方案。

当前 Docker VM 验收已覆盖真实 Redis case、手工窗口 Persistent Watch case、Agent 自动观察 Persistent Watch case 和复合下游故障 case。Watch 最新自动观察结果为 `needs_evidence` 有界终态，已验证 Agent 自采 baseline/trigger window、自动触发 `cpu_shift` incident、collector task 回灌、delayed follow-up 保留和测试 watch 自动停用。Worker 容器内即使读取到 `kernel.perf_event_paranoid=4`，在既有高权限能力下仍已实际通过 `perf stat`；采集器因此以真实 `perf` 执行结果为准，只把 sysctl 值作为诊断上下文，不再用静态阈值提前阻断。Trace 源为空时仍只能输出 `empty_window`，不能计作 endpoint/call_path 深度证据。

迁移必须保持现有证据契约不变，先抽象环境控制接口，再替换运行环境：

```text
Docker VM Environment Backend
  -> Environment Backend Contract
  -> Kubernetes Environment Backend
```

Kubernetes 后续实现重点包括：

- 使用 OTel Collector DaemonSet 接收日志、Trace 和指标，并继续输出现有结构化证据族。
- 使用工业 Profile Producer 或 SkyWalking Rover DaemonSet 采集 eBPF 网络边、连接状态、RTT、重传和协议摘要。
- 通过 CRI/containerd 元数据和 PID resolver 将 Pod、容器、进程、service、instance 统一回连。
- 通过 CNI-aware dependency probing 识别 Service、Pod、Namespace 和跨节点网络路径。
- 将 Kubernetes Fault Injection 接入现有 Case runner，但复用同一 Case/Oracle 评测协议。

迁移完成前不得把 DaemonSet、Rover、CRI/PID resolver 或 CNI 探测能力标记为当前已完成能力。只有 Docker VM 与 Kubernetes 能执行同一测试集、产出同一证据族并通过同一 Oracle 门禁时，才允许宣称 Kubernetes 后端迁移完成。

## Full Controlled AI Tree Scope

本轮直接实现完整版受控 AI 树，而不是前部升级或仅前端展示。受控 AI 树的定义是：Analyzer 先提供 facts、候选池、定位边界、证据引用和 fallback 树；LLM 在预算、已注册采集器、安全边界和证据引用校验约束下生成分层候选结论，并从工具清单中选择层间下探请求。采集结果回流后，LLM 可以对候选进行重排、细化、反证、新增提案或回退，但正式候选和定位层级必须通过工程校验。

核心链路：

```text
当前结构化证据
  -> layer[0] 粗候选集合
  -> Probe Registry Manifest: 已注册、可执行、可结构化输出的下探工具清单
  -> LLM 生成节点结论，并从 Manifest 选择最小必要补证工具
  -> 复用同目标/同窗口/同参数/同源码上下文的既有探针结果
  -> layer[n] 更细候选集合
  -> 节点内自我反问: 支持/反驳/缺失/改变结论条件
  -> 证据不足则停在证据支持的最细层级
```

受控 AI 树不强制得到代码行结论。最终定位层级只能是证据支持的最大层级：

```text
resource / host / process / thread / syscall / dependency / service / endpoint / call_path / function / line
```

只有当源码上下文、符号/栈、Trace 或 call path 证据同时满足时，才允许升级到 `line`。如果用户没有提供源码上下文，诊断仍然成立，但必须停留在 `function`、`call_path` 或更粗层级。

本轮交互策略同步收口：

- 普通诊断默认自动执行全部已注册采集器，不再把 R2 深采集审批作为常规路径。
- 仍保留预算、并发、已注册采集器、安全边界和目标范围校验。
- 不自动执行任意 shell、宿主机 `sysctl` 修改、服务重启、配置变更、修复动作。
- 前端只突出“自动采集状态”和“需要人工处理的事项”，不再让用户处理常规采集审批。

LLM 只能从 `Probe Registry Manifest.available_probes[].evidence_family` 中选择下探请求。编排器再把该 evidence family 映射到注册探针 ID，并执行预算、能力、目标范围和 fingerprint 复用校验。Manifest 明确禁止任意 shell、宿主机 sysctl 写入、服务变更和修复动作执行。

Worker 部署默认按深采集可信节点配置：`privileged: true`、`pid: host`、`PERFMON/BPF/SYS_PTRACE/SYS_ADMIN`、`seccomp:unconfined`。宿主机 `kernel.perf_event_paranoid` 只检测和提示，不自动修改。

前一阶段只实现结构化树、证据引用、候选状态、探针边和前端可视化承载。本轮 `Session-Level AI Explanation and Root-Cause Clusters` 扩展正式接管自然语言可读性、机制解释和按根因簇组织的建议处理，不再把它们保留为未实现的后续升级。

### Candidate Backtracking and Conclusion Eligibility

受控 AI 树必须区分“证据观察”和“诊断结论”。采样点、系统调用、占比、样本数等原始细节只能作为候选结论的支持或反驳证据，不能直接作为根因结论。`clock_nanosleep`、`futex`、`epoll_wait`、`poll`、`select`、`pthread_cond_wait` 等等待、调度、系统调用或运行时原语，在缺少上层业务栈、调用路径或源码映射时不得升级为函数级根因。

候选树按以下生命周期推进：

```text
AI 生成可证伪候选
  -> 检查支持、反驳、缺失和时序关系
  -> supported: 通过结论资格门禁后进入最终主因/次因
  -> needs_more_evidence: 复用或请求最小必要探针
  -> contradicted/rejected: 保留灰色节点和淘汰理由
  -> backtrack: 回到父层并继续验证下一候选
  -> 无候选通过门禁: 输出 observation/partial_localization/abstention
```

每个候选节点必须声明 `claim_type`、`decision` 和 `causal_status`。最终根因必须同时具备具体机制、具体目标、同窗支持证据、可引用证据链和候选间比较；否则只能停留在观察、局部定位或证据不足。回溯通过显式 `rollback` 探针边表示，被淘汰节点不得从树中删除，也不得继续进入 `final_primary_causes`。前端将 `rejected/contradicted` 节点保持可查询但置灰，并用回溯边展示系统转查的下一候选。

## Runtime Control Provenance and Evidence Validity

本轮一次实现方案 `3A + 3B + 3C`，同时先修复所有 producer 共同依赖的证据有效性语义。所有来源统一输出 `runtime_control_event`，但按当前环境分别验收：3A/3B 必须通过 Linux VM 真实事件测试，3C 的发布变更必须通过 VM 真实文件事件测试；Kubernetes Audit 在当前无 Kubernetes 集群的条件下通过真实格式 fixture 与适配器集成测试，后续迁移 Kubernetes 时再补集群级验收，不能把 fixture 测试表述成真实 K8s 集群验证。

### Conclusion Levels

诊断结论必须分为三个不可混用的层级：

```text
observation
  -> 目标 PID 在窗口内持续处于 T 状态
direct_failure_mechanism
  -> process_suspended 直接导致进程无法继续调度
direct_root_cause
  -> actor 通过具体控制动作暂停目标，并由时序和状态变化证明直接因果链
complete_source_root_cause
  -> 用户、控制器、脚本、systemd unit 或发布动作与直接控制链完成来源回连
```

仅有 `/proc/<pid>/status` 的 `T/t` 状态时，系统最多输出 `direct_failure_mechanism`。`runtime_control_event` 同时包含发送者、动作、目标、时间，且目标随后进入停止状态时，只允许输出 `direct_root_cause`；继续补齐动作来源后才能输出 `complete_source_root_cause`。顶层 conclusion、AI 树 final causes、`abstained` 和 confidence 必须来自同一资格结果，禁止出现树内 `unproven/ineligible`、顶层却为高置信根因的矛盾状态。

### Evidence Validity Contract

任务、产物和证据使用三个独立状态：

```text
execution_status: completed | failed | timed_out
artifact_status: produced | missing | corrupt
evidence_status: valid | partial | empty_window | blocked | target_exit | unparseable
```

`completed + produced` 不代表 `valid`。各证据族必须通过最小质量门槛：

- off-CPU 至少存在等待事件、等待栈或明确的 event-only 信号；全零原因不得产生 `top_cause`。
- Trace 至少存在有效 span/endpoint/call_path；空 Trace 不得声明 function 或更细层级。
- baseline/perf 至少一个窗口产出可解析结构化栈；全部解析失败时证据为 `unparseable`。
- py-spy 对 group-stopped 目标失败时标记 `blocked_by_target_state`，不重复请求同类运行时 profiler。
- log scan 必须返回可界定来源和窗口的结构化结果；读取超时或无来源不能满足日志证据族。

readiness gate、AI 树补证完成判断和 benchmark scorer 都使用 `evidence_status=valid|partial` 及对应最小质量字段，不再根据任务列表中的 collector type 判断证据覆盖。

### 3A Persistent Signal Observer

Worker Agent 增加低成本常驻信号观察器，优先使用 eBPF `signal:signal_generate`，并以受控 producer adapter 保留未来替换为 SkyWalking Rover/BCC/OTel Profile Producer 的边界。观察器不做归因，只把最近 N 分钟的控制事件写入有界 Rolling Buffer：

```json
{
  "event_type": "signal_sent",
  "observed_at": "2026-08-19T06:21:31Z",
  "actor": {"pid": 194820, "comm": "bash", "uid": 0},
  "action": {"operation": "kill", "signal": "SIGSTOP"},
  "target": {"pid": 195689, "comm": "python3", "service_id": "paymentservice"},
  "effect": {"process_state_before": "S", "process_state_after": "T"},
  "evidence_ref": "runtime_control.events[0]"
}
```

诊断创建或 Persistent Trigger 冻结现场时，按 `target_pid + time window` 查询并保存 `runtime_control_event_json`。结构化层生成：

```text
actor -> ISSUED -> signal/control action
action -> TARGETED -> process
process -> ENTERED -> runtime state
runtime state -> IMPACTED -> service
```

AI 树首先验证 `process_suspended`，随后请求 `runtime_control_history`。命中同窗控制事件则升级主因；没有历史事件则保留“发起者未知”的边界，不回退到无关的 CPU、锁、Trace 或代码热点探针。

### Log and Profiler Corrections

`log_scan` 改为有界流式/尾部读取，不再对增长中的 NDJSON 使用整文件 `read_text()`。Fluent Bit 增加 systemd/journald 输入，并保留 `_PID`、`_SYSTEMD_UNIT`、container ID、service/instance 关联字段，使 systemd transient unit 与容器日志都能按目标和窗口回连。

对已经 `T/t` 的目标，编排器跳过无法产生有效证据的 py-spy、CPU profile、off-CPU 和 endpoint profile，并改为请求 `runtime_control_history` 与目标日志窗口。空结果仍保存用于审计，但不得增加定位层级或置信度。

### 3B and 3C Producers

`3B` 在同一 `runtime_control_event` 契约上增加：

- systemd unit 创建、stop/restart/kill、watchdog 和 RuntimeMaxSec 事件。
- Docker/containerd pause、unpause、kill、restart、OOM 和健康状态事件。
- cgroup.freeze、CPU/memory 限制变更和进程 cgroup 迁移。

`3C` 继续增加：

- 发布、配置、镜像和扩缩容变更事件。
- Kubernetes Audit、Pod/Deployment/ReplicaSet 事件、Operator/Reconciler 动作。
- 从用户、流水线或控制器到运行控制动作的身份和变更链路。

这些 producer 只新增来源适配和图边映射，不修改 AI 树使用的结构化证据契约。来源不可用时必须显式输出 capability/source 状态，不能阻断其他已可用 producer。

### Validation

本轮必须通过以下验证：

1. 单元测试覆盖大文件日志有界读取、空探针证据状态、停止进程的 profiler 阻断、信号事件结构化和结论资格一致性。
2. Linux 集成测试对真实进程执行 `SIGSTOP/SIGCONT`，对真实 systemd unit、Docker event、cgroup 和发布变更源执行可用性与结构化验证。
3. Kubernetes Audit producer 使用符合 Kubernetes Audit Event 结构的 fixture 验证过滤、脱敏、目标回连和时间窗口；当前 VM 环境不宣称真实集群验证。
4. 重新运行 `OB-SINGLE-RUNTIME-STALL-001`，要求结论明确区分直接故障机制、直接根因与完整来源根因，且失败/空探针不再满足 readiness 和 scorer 的有效证据门槛。
5. 保留被反证候选和 rollback 边，但不得用预置 Oracle 标签或测试 fixture 信息生成诊断结论。

## Session-Level AI Explanation and Root-Cause Clusters

本轮修复终态解释链路和多根因归因。子任务继续负责采集与结构化证据，但不再以子任务 AI 树是否出现过 `ai_guarded` 代表会话级 AI 已完成裁决。所有已完成证据回流后，编排器先形成受控候选池，再执行一次会话级 AI 裁决；最终结论、根因簇、反证说明和建议处理均来自该会话级裁决，并通过工程门禁校验。

### Root-Cause Qualification Levels

运行控制和其他因果结论统一使用四级语义：

```text
observation
  -> 只描述可重复观测的异常事实
direct_failure_mechanism
  -> 已证明直接导致症状的机制，但未知触发动作
direct_root_cause
  -> 已证明 actor/action/target/effect 的同窗直接控制链
complete_source_root_cause
  -> 已继续证明用户、控制器、脚本、systemd unit 或发布动作到直接控制链的来源
```

`bash -> SIGSTOP -> target process -> T(stopped)` 只能升级为 `direct_root_cause`。只有存在可引用的父进程、命令来源、systemd/cgroup 身份、发布记录或审计身份，并且这些来源与直接控制动作在目标和时序上闭合，才允许升级为 `complete_source_root_cause`。旧 `complete_root_cause` 只作为读取兼容值，不再由新诊断生成。

### Truthful AI Participation

每次会话级裁决必须记录：

```text
ai_review_status: succeeded | fallback | failed
ai_review_scope: session
ai_review_attempts
ai_review_model
ai_review_error
```

只有通过 JSON 解析、候选边界、证据引用、探针清单、定位层级和结论资格校验的结果才能标记为 `succeeded`，对应树层才能使用 `generated_by=ai_guarded`。LLM 返回非结构化文本、重试耗尽或被工程门禁拒绝时，系统保留 `analyzer_fallback`，不得通过重新标记满足 readiness gate。

会话级 AI 输入仅包含结构化候选、紧凑证据摘要、可引用 `evidence_refs`、当前树状态、已注册 Probe Manifest 和证据缺口；原始大文件继续按引用惰性读取。AI 可以在已有候选内执行主次排序、聚簇、反证、回退和最小补证选择，但不能伪造候选、证据、探针或定位层级。

### Root-Cause Cluster Contract

多根因不是把多个候选机械放进 `secondary_causes`。系统必须按 `mechanism + target + evidence cohort + propagation path` 将表达同一因果机制的候选合并为根因簇，并对每个簇独立执行结论资格门禁：

```json
{
  "cluster_id": "paymentservice_paused",
  "role": "primary",
  "cause_level": "direct_root_cause",
  "mechanism": "container_paused",
  "target": "paymentservice",
  "explained_symptoms": ["checkout dependency failure"],
  "causal_chain": [
    "Docker pause",
    "paymentservice stopped",
    "checkout RPC unavailable"
  ],
  "relation_to_primary": "primary",
  "evidence_refs": ["ev_runtime_control", "ev_dependency_check"],
  "origin_unknown": true
}
```

簇角色为 `primary | contributing | independent`。有根因结论时只能有一个主因；贡献原因必须说明它放大或促成了主因解释的哪个症状；独立原因必须解释不同目标或不同症状。多个簇不能仅因引用同一证据而被计为独立根因。至少两个独立通过资格门禁的簇同时成立时，顶层 `classification` 才能使用 `compound_incident`。

AI 树继续保留粗候选、探针边、反证灰色节点和回溯边。根因簇是终态层对已支持节点的归并，不替代树，也不得把 rejected、unknown 或 observation-only 节点纳入最终簇。

### Human-Readable Conclusion Contract

最终结论拆为稳定结构，而不是让同一句证据摘要重复充当结论、节点 claim 和 `why_this_claim`：

```text
headline              一句话说明主因及影响对象
why_it_happened       解释机制如何导致用户症状
causal_chain          按时间顺序列出来源、动作、状态和影响
root_cause_clusters   主因、贡献原因和独立原因
ruled_out_summary     说明关键候选为何被压低
residual_unknowns     明确尚未追到的来源或边界
recommendations       按根因簇给出调查、临时缓解和长期修复建议
```

每段因果说明都必须引用实际证据。建议可以包含人工执行步骤，但不得自动执行任意 shell、sysctl、服务变更或修复动作。LLM 不可用时，fallback 只能输出结构化事实摘要和明确边界，不能伪装成人工可读的 AI 结论。

### Frontend and Evaluation

诊断详情页顶部显示 `headline` 和 `why_it_happened`，随后按根因簇展示主因、贡献原因或独立原因。每个簇可展开查看因果链、证据引用、来源完整度、残余未知和建议处理。受控 AI 树继续单独显示逐层下探过程，被反证节点保持灰色。

readiness gate 必须验证会话级 `ai_review_status=succeeded`，不能只检查某层是否写有 `ai_guarded`。复合 case 的评分除现有 `location_type/domain_type/classification` 外，还要验证至少两个独立合格根因簇、各自证据引用和症状覆盖。

真实验收分两步：

1. 重跑 `OB-SINGLE-RUNTIME-STALL-001`，确认 `SIGSTOP` 链只输出 `direct_root_cause`，并明确动作来源仍未知。
2. 运行 `OB-COMPOUND-NOISY-DOWNSTREAM-001`，预期 `paymentservice paused` 为主因簇；Worker2 CPU noise 必须依据目标服务是否出现 throttling/调度受压，分别裁为贡献、独立或未知簇，不能预设为贡献原因。

## Two-Hop Evidence Expansion and Compound Causality Closure

本阶段修复真实 `OB-COMPOUND-NOISY-DOWNSTREAM-001` 暴露的证据缺口。采用“主因 + 经因果补证成立的贡献原因”语义，不把同窗出现的两个异常机械判为复合根因。

### Bounded Two-Hop Expansion

受控 AI 树最多沿服务拓扑扩展 `2 hops`。第一轮只检查目标服务、自身实例、同宿主候选和一跳下游；只有第一轮证据无法解释症状，或一跳下游仍明确指向另一依赖时，才扩展到第二跳。达到任一条件后停止扩展：

- 已形成一个直接根因，且剩余分支无法解释额外症状或放大效应。
- 同一候选连续补证仍无有效同窗证据。
- 达到 2 hops、3 个 AI 轮次、单轮 3 个补证请求或 180 秒总时限。
- 后续只能产生 delayed follow-up，无法反证异常同窗结论。

最终真实复合 Case `diag_session_20260819_175431_44599b8f` 已验证会话级 AI 裁决、两个独立证据簇和 Worker2 CPU `independent` 分类。same-window runtime-control 证据确认 `docker_daemon` 暂停 `paymentservice`，并与依赖失败建立传播链；发起该 Docker 动作的上游用户或自动化来源仍未知，因此结论停在 `direct_root_cause`。该次运行的 readiness 为 `PASS`，会话 AI 一次通过，离线 Oracle 得分 `100/100`，Trace 五阶段覆盖率和证据引用有效率均为 `1.0`。

相互独立的补证任务并行发布。同一 `target + evidence family + time window + source context + parameters` 生成稳定 fingerprint；命中已完成且证据状态合格的任务时复用原 artifact，不再次采集。被反证分支保留灰色节点和回退边，不继续沿该分支扩散。

### Same-Host CPU Attribution

systemd transient unit、容器和普通 PID 统一使用“工作负载范围”采集，不再只统计 `MainPID`。Agent 必须解析目标的 cgroup 或进程树，聚合范围内所有存活进程的 CPU ticks、RSS、线程、FD 和调度信号，并同时保留主 PID 与成员 PID 明细。

Worker2 CPU noise 只有同时满足以下证据时才成为 `contributing` 根因簇：

```text
noise-generator 工作负载范围 CPU 显著占用
  AND Worker2 同窗 CPU 饱和
  AND checkoutservice 同窗出现调度受压、throttling 或可归属延迟
```

只证明噪声进程高 CPU 时，输出 `independent` 异常；只证明宿主 CPU 高但无法归属来源时，保留 `unknown`；不得用“同宿主且同时发生”替代因果关系。

### Runtime State Backfill

运行控制历史继续优先使用持久化 Docker/containerd/systemd/eBPF 事件，但诊断不能依赖“采集任务启动后才出现事件”。查询历史时还要读取当前 Docker inspect、cgroup freezer 和 `/proc` 状态，生成带来源标记的 `runtime_state_snapshot`：

```text
historical event -> 能证明 actor/action/time 时升级 direct_root_cause
current paused/frozen state -> 能证明 direct_failure_mechanism
state + dependency failure same-window -> 能证明下游传播链
```

当前状态快照不能伪造成历史 actor 事件。若已确认容器处于 paused，但历史事件不在保留窗口内，结论必须说明“暂停状态已确认，发起者和准确开始时间未知”。

### Compound Classification and Gates

复合门禁不能根据系统自己输出的 `classification` 决定是否检查。门禁从会话目标范围、AI 树中的并行候选和最终根因簇判断是否存在复合候选：存在多个目标或多个故障注入范围时，必须验证每个已声明主因/贡献原因拥有独立证据和症状关系。

`empty_window` 是结构化负证据，不等同采集失败；只有当来源覆盖窗口完整、目标匹配并且 artifact 可解析时，才允许作为有效负证据。已知目标当前处于 paused，而运行控制采集返回无事件且没有状态快照时，必须判为覆盖缺口，不能通过 completed-probe 门禁。

### Focused Acceptance

实现期间先使用单元和集成测试覆盖所有分支，集中修复后再部署。真实 VM 默认只运行一次 `OB-COMPOUND-NOISY-DOWNSTREAM-001`，验收要求：

1. AI 会话裁决成功，受控树最多 2 hops，重复探针命中 fingerprint 复用。
2. `paymentservice` 暂停/不可达形成主因簇，并说明暂停状态、传播路径和来源边界。
3. Worker2 CPU noise 由工作负载范围指标归属，并根据目标受压证据归类为 contributing、independent 或 unknown，不允许无证据强行命中。
4. Readiness gate 与 benchmark scorer 对根因簇数量、证据有效性和 collector coverage 给出一致判断。
5. 只有真实结果暴露明确代码缺陷时才集中修复并追加纠错复测，不进行无代码变更的试探性多轮真实调用。

### Final Acceptance Record

最终验收目录为 `reports/eval/ai-ops-v2/two-hop-verified-20260820-015345`，诊断 ID 为 `diag_session_20260819_175431_44599b8f`：

- 诊断 `COMPLETED`，耗时 `144.09s`，8 个子任务、24 条证据、9 个 artifact。
- 目标拓扑为 `checkoutservice hop=0 -> paymentservice hop=1`，预算上限保持 2 hops，未发生越界扩展。
- AI 会话裁决 `succeeded`，`ai_review_attempts=1`，最终三层均为 `ai_guarded`。
- 主因簇为 `paymentservice process_suspended`，角色 `primary`，等级 `direct_root_cause`。
- Worker2 `noise-generator` 工作负载约 `398.2% CPU`、宿主忙碌度约 `93.3%`；由于缺少目标 throttling/调度受压证据，角色为 `independent`。
- Readiness `PASS`；Oracle `100/100`，根因精确命中、复合簇匹配、引用有效率和五阶段 Trace 覆盖均通过。
- Worker1/Worker2 容器内已核验新版 `PerfCollector` 和真实 `perf stat`；部署清单测试保证 `perf.py` 不再漏同步。

## Persistent Watch Episode Aggregation and Auto Diagnosis

持续监视不再把每次相对偏移直接转换成独立诊断。`WatchIncident` 保持旧 API 和冻结证据兼容，同时作为持久化 Episode 容器保存 `episode_id`、状态、异常点、发生次数、恢复计数、自动诊断资格和唯一诊断会话。

```text
低成本持续观察
  -> 多源 anomaly point
  -> 首次异常立即冻结 rolling snapshot 并执行同窗采集
  -> Episode 聚合 30 秒
  -> 硬状态或影响已确认时创建一次 AI 集群诊断
  -> 后续同类或关联异常只更新 Episode
  -> 连续 3 个正常观察周期或 120 秒静默后关闭
```

Episode 状态为 `AGGREGATING -> DIAGNOSING -> ACTIVE -> RECOVERING -> CLOSED`。同一 Watch 在 120 秒内的连续异常合并到活动 Episode；重复异常按稳定 fingerprint 累计 occurrence、latest、peak 和时间范围，不重复冻结、采集或创建 AI 会话。超过静默期或完成恢复确认后出现的新异常创建新 Episode。诊断认领通过持久状态和运行时互斥保证幂等，每个 Episode 最多一个正式诊断会话。

检测层只判断“值得保存的异常点”，不输出根因。当前信号包括：

- 硬状态：目标进程处于 `T/t` 暂停状态，立即具备自动诊断资格。
- 稳健变化：指标使用 median 和 MAD 变化评分，并保留最小绝对变化量与相对变化门槛。
- 资源与运行影响：CPU、内存、I/O、线程、cgroup throttling、队列、延迟和错误可在同窗形成多个 anomaly points。

统计型资源偏移只有出现用户影响或执行影响证据时才允许自动诊断。单独 CPU、内存、I/O 或线程偏移只保存 anomaly point 和冻结现场，不自动消耗 AI 树预算；延迟、错误、throttling、队列积压或硬状态可提升 Episode 的 `diagnosis_eligible`。用户仍可手动对任意 Episode 启动正式诊断。

每个异常点提供独立轻量 AI 解释接口。该调用只接收异常点、目标、窗口、同窗信号和数据质量，输出触发原因、变化幅度和分类；禁止生成根因、请求探针、创建 AI 树或给出修复动作。解释按 anomaly fingerprint 持久缓存，重复查看不再次调用模型。

前端创建和 Watch 列表均提供单列开关 `异常后自动诊断`，默认开启。关闭后 Agent 仍持续观察、冻结和保存 Episode，但不会自动创建正式诊断。Watch 展开后先展示 Episode，再展示其中所有异常点；异常点“分析”打开轻量解释，Episode 的“AI 集群诊断”继续进入完整受控 AI 树。

数据库迁移以幂等增列兼容旧 Watch 数据；旧 incident 缺少 Episode 字段时按默认值加载。Agent gRPC 指标契约补充 process state、cgroup throttling 和 queue depth，Docker 构建期间继续从 `proto/watch.proto` 生成客户端与服务端 stub。

## Optional Source Mechanism Investigation Branch

### 本轮完成状态

- [x] CodeQL 工业源码机制采集器、revision/database cache、受管理 suite 和受控 AI 临时 path query 已接入。
- [x] PyHeap 工业运行时引用验证器、GDB attach、原始 dump 留存和有界入向引用路径已接入。
- [x] AI 树已按候选绑定 CodeQL/PyHeap，支持未成立候选灰化、回退和同候选证据闭合。
- [x] 旧 `source_snapshot.reference_paths` 保留原功能，并降级为不具备根因资格的 `source_reference_hints`。
- [x] Werkzeug #1521 fixture 已覆盖表层行、`defaults` 反证和 bound-method/code-constant 最终机制链。

行级定位只回答“异常或保留分配出现在哪里”，不能自动回答“该位置为什么形成最终故障机制”。当受控 AI 树已经获得 revision 匹配的 `line` 锚点，但候选仍停留在 allocation hotspot、等待点、回调注册点、动态代码生成点或其他表层位置时，允许按需进入机制下钻分支。该分支不是所有诊断的必经层，不改变已有 `resource -> line` 定位层级。

```text
已验证的 file:line 表层锚点
  -> AI 判断机制因果链未闭合
  -> source_mechanism_query: CodeQL 源码调用链和跨函数数据流
  -> 形成多个可证伪机制候选
  -> 已有 Memray 证据验证分配和保留事实
  -> 必要时 python_heap_reference: PyHeap 运行时入向引用子图
  -> 支持候选升级；反证候选保留灰色并回退下一候选
```

### Source Mechanism Query

`source_mechanism_query` 使用 CodeQL CLI，不执行目标仓库代码，也不允许任务参数传入任意本机 query 路径或 shell。它支持部署方固定的受管理 query suite，也支持受控 AI 树按节点生成临时 QL `path-problem` 查询：AI 必须同时给出有界 investigation question；服务端限制长度、import、输出类型和已选择的 probe，计算 `query_hash` 后才随 follow-up 下发；Collector 再次校验并只在自身任务目录落盘。输入还必须包含允许目录内的 `source_root`、准确 `repo_revision` 和已有 `file:line/symbol` 锚点。CodeQL database 按 `repository identity + revision + language + query pack version` 缓存；同一 revision 后续复用 database，但不同 `query_hash` 不复用查询结果。缓存目录不属于 AI 输入。

输出 `source_mechanism_json`，仅包含有界 `mechanism_paths`、源码节点、`call/data_flow/container_write/code_generation` 边、候选支持或反驳关系、revision/hash 和 `evidence_refs`。SARIF、CodeQL database 和完整源码继续作为原始产物按引用保存。CodeQL 不可用、revision 不匹配、查询失败或没有有效 path 时必须输出结构化 `blocked/empty_window/unparseable`，不得退回自研 AST 后伪装成工业结果。

现有 `source_snapshot.reference_paths` 保留为源码快照的局部提示，证据等级降为 `partial_localization`。它可以帮助 AI 选择 CodeQL 查询锚点，但不能单独把 `python_memory_retention` 升级为 `direct_root_cause`。

### Python Heap Reference Verification

`python_heap_reference` 使用 PyHeap 对 CPython 目标生成堆快照并离线构建入向引用索引。采集器只允许目标 PID、目标容器 ID、对象类型提示、最大深度、最大路径数和最大对象数等受控参数；需要 Linux、GDB 和 `SYS_PTRACE`。完整 `.pyheap` 文件作为原始产物保存，模型只接收 `python_heap_reference_json` 中的 top retained objects、指定类型的最短入向引用路径、对象类型/有限表示、retained bytes 和稳定 `evidence_refs`。

PyHeap 是可选深度验证器。其缺失或权限阻断不使普通诊断失败；已有 CodeQL + Memray 足以支持源码机制时，AI 可以停在机制候选或已支持根因。堆快照可能造成短暂停顿和较大磁盘占用，因此只有开发预算、用户显式深度诊断或机制候选冲突时才允许请求，不进入持续监视默认采集组。

### AI Tree Eligibility

机制分支仅在以下条件同时成立时可自动请求 `source_mechanism_query`：

- 目标已绑定 source root 和准确 repo revision。
- 当前存在 revision 匹配的 `line` 锚点。
- 当前 claim 仍是 observation、partial localization 或 direct failure mechanism。
- “表层位置如何导致症状”的因果步骤为空、冲突或只来自 LLM 文本。
- 预算和 Agent capability 允许执行已注册工具。

只有命中既有 `file:line` 锚点的 CodeQL 结构化路径和已有运行时证据共同支持候选时，才允许升级源码机制。CodeQL 临时查询、机制路径和 PyHeap 验证任务必须绑定同一个 `candidate_id`；不同候选的运行时路径不得交叉证明。需要证明对象实际持有关系的内存问题，在缺少 `python_heap_reference` 时必须明确该运行时边界；PyHeap 返回的引用链与源码路径一致时才能声明完整持有链。错误候选不得删除，必须以 `contradicted/rejected` 灰色节点和 rollback 边保留。

Werkzeug #1521 验收要求保留 Memray 的分配行作为表层节点，并由机制分支区分 `defaults`、普通循环引用和 bound method/code constant retention 等候选。只有真实源码路径支持 `converter.to_url -> get_const -> self.consts -> CodeType.co_consts`，且运行时引用证据支持 `function -> code -> co_consts -> bound method -> converter -> Map` 时，才允许把后者升级为最终持有机制。

## Constitution Check

## Industrial Python Memory and Source Localization

Werkzeug #1521 真实 case 证明当前链路能观测 RSS 增长和原始 py-spy 源码帧，但 baseline 聚合会产生超范围百分比，SVG 反向解析会丢失 `file:line`，memory 分支还会错误请求 off-CPU/Trace。修复采用工业 Producer，而不是在 Agent 内手写 Python Heap 或引用图扫描器。

```text
memory_smaps
  -> 证明同窗 RSS/PSS 增长
Memray attach / instrumented run
  -> 官方 allocation/leak profile
py-spy raw
  -> Python 调用栈、函数、文件和行号
Git + tree-sitter / universal-ctags
  -> revision 校验和命中位置附近的有界源码片段
受控 AI 树
  -> 基于真实 evidence_refs 形成、反问和回退机制候选
```

Mini-Drop 的 `python_heap_profile` 采集器只负责 Memray capability preflight、受控 attach/record、官方产物留存、结构化转换和证据有效性判断。它不得使用 `gc.get_objects()`、`gc.get_referrers()`、objgraph 或 Pympler 替代 Memray。Memray 无法证明完整对象引用链时，只允许输出 allocation/leak stack 和 retained allocation hotspot；完整引用机制必须标记为 AI 基于分配证据、运行时栈和源码片段形成的可证伪推断。

py-spy 使用官方 raw/collapsed 输出作为结构化主输入，SVG 仅作为展示产物。结构化层必须保留函数、文件、行号、完整调用路径、样本数和百分比，并拒绝 `percent < 0`、`percent > 100`、负样本、裸地址或 `[unknown]` 满足函数/行级门禁。baseline 多窗口不累加百分比，使用逐窗口有效样本和中位数/最大值表达稳定性。

源码上下文 Producer 只处理用户明确提供的 source roots、目标容器内源码或上传 source bundle。它先用 Git 校验 `repo_revision`，再围绕已有 line candidate 或 symbol 读取有限行数，并使用 tree-sitter 或 universal-ctags 提供符号边界。它不上传整个仓库，不执行源码中的任何命令。会话级 AI 树必须继承 child tree 的一致 `source_context_hash`；多个目标 revision 冲突时保持 conflict 状态并禁止行级合并。

内存候选的最小补证顺序为 `python_heap_profile -> python_runtime_profile -> source_snapshot`。只有同窗等待、延迟或请求路径证据存在时，memory 分支才请求 off-CPU 或 Trace。会话级 AI 在每轮补证前进行一次受控裁决，从 Probe Manifest 选择工具；工程层继续校验注册、能力、范围、预算、fingerprint 和 evidence refs。没有真实模型调用时不得预增 `model_calls`，AI 未执行或被校验拒绝时必须保留 fallback/partial 状态，不能把 Analyzer fallback 计作 AI 成功。

Werkzeug #1521 的验收 Oracle 保持独立：漏洞版用于证明测试样本成立，修复版只作为离线对照，不提供给诊断模型。正式诊断必须在不知道 PR 答案的前提下，至少定位 `Rule.compile/_compile_builder` 相关源码范围，说明持续未释放分配与动态 builder 路径的关系，并明确区分 Memray 直接观测事实和 AI 推导的引用机制。

The current project constitution remains the unfilled Spec Kit template and defines no enforceable project-specific gates. The implementation follows the repository instructions: preserve existing worktree changes, keep the change scoped, write tests for new processing behavior, and avoid collector or task-scheduling changes.

Post-design check: pass. The design reuses existing diagnosis/conclusion JSON persistence and registered collector contract. CodeQL and PyHeap are optional managed tools executed through the Worker Agent; unavailable tools produce bounded blocked evidence and do not become mandatory external services.

## Project Structure

### Documentation (this feature)

```text
specs/001-evidence-attribution-analyzer/
├── plan.md
├── spec.md
├── tasks.md
└── checklists/
    └── requirements.md
```

### Source Code (repository root)

```text
server/app/rca/
├── models.py
├── attribution.py
├── evidence.py
├── llm_client.py
├── strategies/
│   ├── linear.py
│   └── graph.py
└── report.py

tests/
├── test_rca_attribution.py
├── test_rca_strategies.py
└── test_rca.py
```

**Structure Decision**: Keep evidence-to-attribution processing in one `server/app/rca/attribution.py` module. It is a single cohesive transformation over the existing `EvidenceInput` and candidate models, avoiding unnecessary submodules. Existing strategies only orchestrate it; LLM validation remains in `llm_client.py`.

## Follow-up Closure Plan

This follow-up closes the remaining gaps exposed by the vulnerable-only Celery
run. A normal real case runs the vulnerable workload only, keeps the target
alive for the diagnosis window, and does not run fixed replay or the offline
Oracle.

### Output Domains

The result is split into four domains:

```text
session_main
  canonical candidate tree used by the primary frontend layout
probe_history
  attempted probes, retries, rollback and local boundary transitions
data_quality
  orphan, duplicate ID, missing provenance and invalid relation records
candidate_diagnostics
  AI output, validation failures, gate checks and initial evidence
```

Only `session_main` can create main-tree layout edges. The other three domains
may be displayed beside the tree and retained for audit/replay, but cannot
create or replace a tree parent.

### Canonical Lineage

Every refinement, line, observation, mechanism and boundary node resolves its
parent against the current emitted candidate index before persistence.
`coarse_insufficient_evidence` is an alias only and must be normalized to the
actual emitted coarse ID. The resolver never uses layer order, rank,
`primary_nodes[0]`, `next_candidates[0]`, a global most-specific node or a root
entity to guess a parent.

An explicit refinement with no resolvable parent becomes
`missing_provenance/orphan`. Only an explicitly independent
`alternative/rejected_alternative` may attach to the emitted coarse root.
Line nodes must have a real emitted parent; a conceptual ID is invalid.

### Main Tree and History

`tree_kind=session_main` and `renderable=true` identify the only tree sent to
the primary layout. Child probe snapshots use
`tree_kind=child_snapshot`, `renderable=false`; probe history remains available
for audit and replay but is never merged into the session tree.

The main layout uses explicit lineage only. Coarse summary edges and ordinary
probe/refine edges are annotations. Rollback and local boundary edges may be
shown as non-layout edges. Orphan and duplicate-ID records are shown in a
data-quality area rather than as tree nodes.

### Line Eligibility

`source_snapshot=valid` only proves that source context was read. A
`line_anchor` is emitted only when revision, file, positive line number,
runtime/source match, stable source hash and real parent checks all pass.
Otherwise the system emits a source/line boundary under the origin parent and
records the exact failed checks. A completed source snapshot is never treated
as a line conclusion.

Mechanism and object-call-chain nodes are legal only below a verified line
anchor. They explain the line; they cannot replace the line or base conclusion.

### Candidate Failure and Gate Diagnostics

Every candidate-generation and eligibility attempt persists the phase, attempt,
field path, sanitized actual value, response summary hash, accepted/rejected
counts, initial evidence context, valid and missing evidence refs, emitted
parent IDs, missing parent IDs and per-check gate results.

The UI and audit output must answer:

```text
AI returned what
which field failed
what initial evidence existed
what evidence or parent was missing
which gate blocked promotion
which parent conclusion was retained
```

One invalid candidate does not invalidate other valid candidates. Analyzer
fallback is created only when every candidate and retry is unusable. Analyzer
facts/hints never become formal AI candidates or root-cause clusters.

### Live Heap Collection

Memray remains the Python heap producer. The target process must not preload
Memray; the Agent invokes a managed in-container helper to attach during the
diagnosis window, records namespace/UID/ptrace/runtime preflight, allows one
bounded retry, and retains official Memray artifacts and stdout/stderr.

```text
valid
  -> allocation/retained-allocation evidence may be used
blocked/failed/attach_failed/timeout/target_exit
  -> boundary only, then continue runtime and source follow-up
native_allocation_observation
  -> optional live fallback, never Python retention or source-line root cause
```

`py-spy` remains the runtime-stack producer and RSS/smaps remains process-memory
evidence. No GC referrer scan, objgraph, Pympler or hand-written object graph
may masquerade as Memray heap evidence.

### Fallback and Final Conclusion

Deep probe failure creates a boundary child whose parent is the actual
`origin_parent_candidate_id`. The parent claim, evidence and conclusion state
remain unchanged. A child failure cannot contradict its parent; only direct
parent evidence can trigger a parent-level backtrack. If the origin parent is
missing, the result is data-quality orphan plus abstention.

`formal_root_cause`, `root_cause_clusters`, `final_primary_causes`, `headline`,
`confidence`, `causal_chain` and `abstained` must be derived from one
eligibility result. With no eligible AI candidate:

```text
root_cause_clusters=[]
causal_chain=[]
formal_root_cause=null
abstained=true
```

Localization, observations, boundaries, retained parent conclusion and failure
diagnostics remain available.

### Validation and Deployment

Local checks are focused Python diagnosis/RCA/heap tests, frontend graph-model
tests, frontend production build, `compileall`, and `git diff --check`.
After commit/push, Control and Worker1 pull and rebuild. Worker1 then runs a
non-preloaded managed-Memray smoke against a long-lived target.

The final real case is vulnerable-only Celery for 400 seconds with a full
Celery checkout, real Redis, real worker and native `apply_async()` producer.
It does not run fixed replay, Oracle or `evaluate_case.py`. The report must
include tree kind, parent validation, candidate diagnostics, line eligibility,
heap outcome, probe history, data-quality records, retained conclusion and
final abstention state.

## Unified Closure Plan: Tree Lineage, Candidate Diagnostics and Live Heap

### Objective

Close the remaining diagnosis-chain failures as one contract. The system must
keep a real canonical tree, explain why AI candidates did or did not pass the
formal gates, and attempt Python heap collection against a live target without
requiring the target to preload Memray.

The ordinary real-case runner remains vulnerable-only. Fixed replay, Oracle and
`evaluate_case.py` are separate evaluation tools and are not part of a normal
case run.

### Shared semantics

```text
session_main
  -> only canonical lineage nodes and explicit parent edges
child_snapshot / probe_history
  -> audit and replay only; never part of the main layout
data_quality
  -> orphan, duplicate ID, invalid relation and missing provenance records
formal conclusion
  -> only eligible AI candidates; Analyzer facts and fallback hints are not
     formal root causes
```

The backend, frontend, audit output and replay must share these rules:

1. `parent_candidate_ids` declares possible parents; the stable visual parent is
   `origin_parent_candidate_id`.
2. Every `refinement`, `line_anchor`, `observation`, `mechanism` and `boundary`
   node resolves its parent against the current emitted `session_main` index.
3. `coarse_insufficient_evidence` and other conceptual IDs are aliases only and
   must resolve to the actual emitted coarse candidate ID.
4. No resolver may use rank, layer order, `primary_nodes[0]`, a global
   most-specific candidate, or root entity to guess a parent.
5. Only an explicitly independent `alternative` may attach to the emitted
   coarse root. Other unresolved alternatives remain orphan/data-quality nodes.
6. Observation and runtime-stack nodes are evidence context. They do not become
   parallel root candidates.
7. Mechanism and object-call-chain nodes are legal only below a verified line
   anchor.
8. A blocked, failed, partial or inconclusive deep probe creates a local
   boundary under the actual origin parent and retains that parent's claim.
9. A child probe failure never contradicts its parent. Parent-level backtrack
   requires direct evidence against the parent.
10. With no eligible AI candidate, the final fields are
    `formal_root_cause=null`, `root_cause_clusters=[]`, `causal_chain=[]` and
    `abstained=true`; localization, boundary and retained-parent details remain.

### Backend scope

#### Candidate lineage and line eligibility

Build an emitted-candidate index before constructing layers. Normalize
conceptual coarse IDs, validate real parents, preserve origin parent through
retries/source-line normalization/follow-up persistence, and record
`missing_provenance`, `orphan`, duplicate IDs and invalid relations in
`data_quality`.

A line anchor is valid only when revision, file, positive line, runtime/source
match, stable source hash, evidence-window relation and real emitted parent all
pass. `source_snapshot=valid` proves only that source context was read; it does
not itself create a line conclusion. Failed checks are persisted in
`line_anchor_eligibility` and produce a source/line boundary under the known
origin parent.

#### Candidate failure diagnostics

Every AI attempt and retry persists a bounded record containing phase, attempt,
response summary/hash, actual candidate count, accepted/rejected/deferred
counts, claim/relation/parents, initial evidence context, valid and missing
evidence refs, required probe, field-level gate checks, and retained parent.
One invalid candidate must not discard valid siblings. `analyzer_fallback` is
created only when all candidates/retries are unusable or AI is unavailable, and
is always `unknown + partial_localization + unproven + ineligible`.

The output must explain:

```text
AI actually returned what
which field or gate failed
what initial evidence existed
what evidence or parent was missing
which parent conclusion was retained
why line or formal-root-cause promotion was blocked
```

#### Runtime quality and live Heap

`python_runtime_profile` emits sample quality including diagnostic value,
dominant state, idle/primitive/framework/target ratios, sample count,
stability and reason. Poll/select/epoll/sleep/event-loop-only samples remain
observations and cannot become function or line roots.

Memray remains the Python heap producer. The target must not preload it. The
managed Agent helper resolves target PID and namespaces, UID/ptrace capability,
target executable/runtime, helper availability and output paths, then attaches
during the diagnosis window. It retains official `memray.bin`,
stats/leaks output and bounded stdout/stderr, with one bounded retry.

Runtime mismatch is a structured capability boundary. Heap outcomes are:

```text
valid
  -> allocation/retained-allocation evidence may be used
blocked / failed / memray_attach_failed / timeout / target_exit
  -> boundary only; continue runtime, RSS/smaps and source follow-up
native_allocation_observation
  -> partial observation only; never Python retention, object chain or line root cause
```

No `gc.get_referrers`, objgraph, Pympler or custom object graph may replace
Memray.

#### Fallback and final conclusion

The same qualification result drives `headline`, `confidence`,
`formal_root_cause`, `root_cause_clusters`, `causal_chain`, `abstained` and
frontend eligibility. A retained conclusion is the actual origin parent's
claim, never a newly invented generic fallback claim.

### Frontend scope

`buildControlledAITreeGraph()` accepts only `session_main/renderable=true` for
the primary layout. Explicit lineage edges are the only layout edges. Coarse
summary/probe edges are annotations; rollback and local boundary edges are
history edges. `child_snapshot`, `probe_history`, orphan, duplicate ID and
invalid relation records never create main-tree layout edges.

Use distinct node semantics for `cluster_root`, `base_cause`, `line_anchor`,
`observation`, `mechanism_explanation`, `stop_boundary`, `rejected_candidate`
and `orphan`. `blocked`, `partial` and `inconclusive` are boundary states;
only `contradicted` and `rejected` use the grey rejected style.

The diagnosis page reads backend diagnostics and displays each AI attempt,
actual returned candidates, accepted/rejected/active/deferred candidates,
initial evidence, parent resolution, gate failures, line eligibility, heap
preflight/retry/outcome, retained parent and formal abstention. It must not
infer eligibility from node color or text.

### Validation and deployment

Local validation covers focused backend/heap/line/candidate/audit tests,
frontend graph/detail tests, production build, `compileall` and
`git diff --check`.

Offline replay reads only the latest Celery `run.json` and reports initial
evidence, AI output/failures, gate checks, line eligibility, heap outcome,
parent validation, data quality, retained parent and final abstention. It does
not read issue, PR, fixed revision, Oracle or answer data, and does not rewrite
the original report.

After commit/push, Control and Worker1 pull the exact commit and rebuild.
Worker1 first runs a long-lived, non-preloaded Memray smoke and saves either
official artifacts or a structured capability boundary. Only then does the
ordinary runner execute one vulnerable-only Celery case for 400 seconds using
the complete checkout, real Redis, real worker and native `apply_async()`.
Fixed replay, Oracle and `evaluate_case.py` are not run in this ordinary case.

The case report must include runner manifest, producer barrier, diagnosis
terminal state, candidate diagnostics, line eligibility, heap outcome, tree
kind, parent validation, history/data-quality records, retained conclusion and
final abstention.

### Completion gates

```text
main layout contains session_main lineage only
history/probe/orphan records do not create main-tree edges
line nodes point to real emitted parents or become explicit boundaries
mechanism/object-call-chain nodes are below verified line anchors
deep probe failure retains the actual parent claim
one invalid AI candidate does not discard valid siblings
Analyzer fallback appears only after complete AI failure
gate failures explain actual output and initial evidence
runtime idle samples cannot become root causes
live heap attach does not require preload
heap failure is structured and does not stop later probes
formal conclusion and abstention fields are consistent
normal Celery validation is vulnerable-only 400s
```

## Final Integrated Closure Plan (Authoritative)

本节是当前“树渲染、候选失败解释、Line 探测、实时 Heap 和真实
Celery 跑测”闭环的唯一执行口径。前文 CE/CF 保留为历史设计记录；若
与本节冲突，以本节为准。

### 1. Scope and non-goals

本次闭环只修复 Analyzer/诊断输出/前端展示/Agent 采集和真实跑测流程。
不从 issue、PR、修复 commit、Oracle 或测试答案向 Analyzer 回流答案。
不把 fixed replay、Oracle 或 `evaluate_case.py` 放入普通真实 case 的
跑测路径。普通 Celery 跑测固定为 vulnerable-only，默认持续 400 秒；
runner 不要求每次手工重新设置 fixed/Oracle 参数。

不新增 Celery 特供证据，不由 runner 生成深层源码、heap 或调用链结论。
runner 只负责启动完整项目、真实 Redis、worker、原生 producer，维持
异常 workload 和诊断窗口，并保存运行清单。

### 2. Canonical evidence-to-conclusion pipeline

```text
initial evidence
  -> Analyzer facts / observations / localization boundary
  -> AI candidate review
  -> candidate field-level gate
  -> selected follow-up probes
  -> evidence回流与AI DAG更新
  -> line-anchor eligibility
  -> optional mechanism/object-call-chain explanation
  -> one shared conclusion eligibility result
  -> session_main + diagnostics + audit bundle
```

三类内容必须隔离：

```text
facts/observation
  只能描述已观测事实和定位边界

AI candidate
  只能来自真实 AI 输出，并通过工程门禁后才可成为正式候选

formal root cause
  只能来自通过同一资格结果的 AI candidate
```

Analyzer fallback 只能作为 `fallback_observation`/调查方向。它固定为
`unknown + partial_localization + unproven + conclusion_eligible=false`，
不得进入 `root_cause_clusters`、`causal_chain` 或 `formal_root_cause`。

### 3. Backend lineage and emitted-parent contract

在构建 `session_main` 前先建立当前 emitted candidate index。所有概念
父 ID 必须先归一化：

```text
coarse_insufficient_evidence
  -> 当前本次真正 emitted 的 coarse candidate_id
```

`line`、`refinement`、`observation`、`mechanism`、`boundary`、
`STOP` 和对象调用链节点必须满足：

```text
parent_candidate_ids
  -> 当前 session_main 中真实存在的 candidate_id

origin_parent_candidate_id
  -> 唯一视觉主父节点，且属于 parent_candidate_ids
```

禁止使用 `index 0`、`primary_nodes[0]`、rank、layer 顺序、全局“最具体”
候选或 `root_entity` 猜测父节点。父节点解析失败时输出
`missing_provenance/orphan` 数据质量记录，不补 coarse、不伪造 line。

`alternative/rejected_alternative` 只有明确表示独立候选且父节点真实存在
时才可以挂 coarse；没有来源的备选保持 orphan。子节点探针失败不能反证
父节点，只有父节点本身被直接反驳时才能沿 `origin_parent_candidate_id`
回退。

### 4. Fallback and retained conclusion

深探返回 `blocked`、`failed`、`partial`、`inconclusive`、`target_exit`
或超时时：

```text
来源父节点
  -> 保留原 claim/evidence/causal status
  -> 增加局部 boundary 子节点
  -> boundary.parent = origin_parent_candidate_id
  -> 记录 retained_conclusion 和 qualification_boundary
```

boundary 只说明“为什么停止”，不能产生新的根因 claim；不能把
“当前证据不足”“unknown downstream dependency”或探针失败文本升格为
新的正式 fallback 结论。来源父节点不存在时只能生成 orphan 和
`abstained=true`。

最终字段必须全部由同一 eligibility result 派生：

```text
formal_root_cause
root_cause_candidates
root_cause_clusters
final_primary_causes
causal_chain
headline
confidence
abstained
```

无 eligible AI candidate 时固定输出：

```json
{
  "formal_root_cause": null,
  "root_cause_clusters": [],
  "causal_chain": [],
  "abstained": true
}
```

### 5. Runtime quality and Line probe

`python_runtime_profile` 必须输出 `sample_quality`，至少包含：

```text
diagnostic_value
dominant_state
non_idle_ratio
primitive_frame_ratio
framework_loop_ratio
target_code_ratio
sample_count
stable_across_samples
reason
```

纯 `poll/select/epoll/sleep/futex`、事件循环尾帧、单样本尾帧和低占比
runtime plumbing 只能作为 observation，不能升级为 function/call_path/line
主因。高占比、跨窗口稳定、能解释症状并有 RSS/smaps/日志/heap/source
交叉证据的框架瓶颈可以保留为候选。

Line 探测流程固定为：

```text
runtime/source frame
  -> process_source_snapshot
  -> revision/file/positive line/source hash 校验
  -> runtime/source/window/parent 交叉校验
  -> verified line_anchor
```

`source_snapshot=valid` 只说明源码存在和 revision 可读，不能单独制造
line 主因。Line eligibility 任一项失败时，生成挂在真实来源父节点下的
source/line boundary，并持久化逐项失败原因。只有 verified line 后，才允许
机制节点、对象调用链或源码机制解释继续下钻；这些节点只能解释 line，
不能替代 line 或基础结论。

### 6. Live Heap tool decision and failure handling

工具职责固定如下：

```text
Memray live attach
  -> Python heap 主路径；目标不预加载 Memray

RSS/smaps
  -> 进程级内存增长和映射观察；不是 Python retention 证明

python_runtime_profile / py-spy
  -> 运行时栈和执行状态；不是 heap retention 证明

PyHeap
  -> 显式、可选的后置深探；需要暂停/attach 条件时不得作为实时默认路径

tracemalloc/objgraph/gc.get_referrers/Pympler/自研对象图
  -> 不作为当前默认 live heap 方案
```

Memray helper 必须分阶段记录：

```text
preflight
  PID、namespace、UID、ptrace、目标 runtime、helper、输出目录、目标稳定性

staging
  目标 Python runtime 和 Memray purelib/native module 是否可用

attach
  method、注入脚本、控制通道、duration、stdout/stderr、exit code

artifact
  memray.bin、stats/leaks、解析状态、产物缺失原因
```

managed attach 允许一次有界 retry，但 timeout 后必须杀掉整个 helper
进程组并保存阶段状态，不能只输出一个总 timeout。目标 runtime 不匹配、
namespace 不可达、权限不足、目标退出、产物缺失和 attach 控制通道无响应
必须使用不同的结构化 failure type。

结果语义：

```text
valid
  -> 可提供 allocation/retained-allocation evidence

blocked/failed/memray_attach_failed/timeout/target_exit
  -> heap boundary；继续 runtime、RSS/smaps、source follow-up

native_allocation_observation
  -> partial；不能声明 Python retention、对象引用链或源码 line 根因
```

Heap 失败不能终止诊断，也不能改变来源父节点结论。

### 7. AI candidate failure and gate diagnostics

每次首轮、重试和 investigation review 都要保存：

```text
phase
attempt
response_summary_hash
bounded_actual_output
candidate_count
accepted/rejected/deferred/active candidate IDs
initial_evidence_context
valid/missing evidence refs
emitted parent IDs
retry state
```

每个候选保存逐字段 `gate_checks`：

```text
source_is_ai
candidate_id
evidence_refs
target/window
supported_level
mechanism
causal_status
decision
required_probe
parent_exists
origin_parent
causal_chain
```

页面和 audit 必须同时回答：

```text
AI 实际返回了什么
哪一个字段/门禁失败
失败时初始证据是什么
缺失或不一致的证据是什么
为什么不能升级到 line 或 formal root cause
最后保留了哪个来源父结论
```

单个候选失败不能丢弃合法兄弟；active candidate 最多只限制深探
调度，不授予正式资格。只有全部候选、重试和可用 AI 路径都失败时才
创建 Analyzer fallback。

### 8. Frontend tree and history rendering

前端把返回内容分成四个域：

```text
session_main
  当前 canonical tree，唯一进入主布局

child_snapshot
  某次子探针快照，只用于审计/回放

probe_history
  尝试、回退、停止和 rollback 边，只用于解释过程

data_quality
  orphan、duplicate ID、invalid relation、missing parent
```

主图只按显性 `origin_parent_candidate_id` 生成主树边；普通 probe/refine
边、`session_edge_coarse_to_supported_causes` 等总览边只能作为注释，
不能参与布局。rollback/boundary 可以显示为非布局说明边。

节点语义必须分开：

```text
cluster_root/base_cause/line_anchor
observation/mechanism_explanation/stop_boundary
rejected_candidate/orphan
```

`blocked/partial/inconclusive` 显示为证据边界，不得套用 rejected 灰色；
只有 `contradicted/rejected` 才进入反证态。orphan 必须可见但不进入主树，
不能静默接 coarse。历史子树必须有独立入口，不能与当前主树混合成一排
“探针节点”。

诊断详情页新增候选失败诊断区，读取后端结构化字段，不从节点颜色、
headline 文本或数组顺序推断资格。

### 9. Runner and real-case contract

普通真实 case 固定执行：

```text
完整项目 checkout
真实 Redis broker
真实 Celery worker
Celery 原生 apply_async() producer
持续 400 秒的异常 workload
唯一 vulnerable diagnosis
```

runner 不采集深层证据、不生成源码 line/heap/调用链答案，只提供目标
定位信息、维持 workload、保存 runner manifest 和诊断输出。固定不执行：

```text
fixed replay
Oracle
evaluate_case.py
```

只有用户明确要求回归对照时，才另行执行 fixed/Oracle 流程；这不属于
普通“跑测 case”的默认配置。

### 10. Validation and deployment order

实施顺序固定为：

```text
CG001 契约与 canonical index
  -> CG002 fallback/结论资格
  -> CG003 AI 候选和门禁诊断
  -> CG004 runtime quality/Line probe
  -> CG005 live Heap helper
  -> CG006 前端主树/历史树/诊断视图
  -> CG007 audit/API/离线回放
  -> CG008 本地测试和构建
  -> CG009 commit/push
  -> CG010 Control/Worker1 git pull + rebuild
  -> CG011 Worker1 未预加载 Memray smoke
  -> CG012 vulnerable-only Celery 400 秒真实 case
```

部署前不把本地测试结果称为 VM 验收。Heap smoke 先于 Celery case；
smoke 失败时仍可以跑 case，但必须把 Heap 结果记录为结构化 boundary，
不能写成 Heap 成功。

### 11. Completion gate

完整方案只有同时满足以下条件才算完成：

```text
所有细化节点父节点真实存在，概念 coarse 已映射
无来源节点进入 data-quality/orphan，不伪造父边
深探失败只产生局部 boundary 并保留来源父结论
line 只有通过真实 file:line eligibility 才生成
机制/对象调用链只能挂 verified line
runtime idle 样本不能升级为主因
Heap 不要求预加载，失败后继续其他证据
AI 部分失败保留合法候选，全部失败才 fallback
AI 实际输出和初始证据可解释每个门禁失败
主树、历史子树、probe edge、orphan 不互相污染
正式结论字段来自同一资格结果
普通真实 case 只运行 vulnerable-only 400 秒
```

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| None | No constitution gate or unnecessary abstraction is introduced. | N/A |
