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

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| None | No constitution gate or unnecessary abstraction is introduced. | N/A |
