# Feature Specification: Evidence-to-Attribution Analyzer

**Feature Branch**: `[001-evidence-attribution-analyzer]`

**Created**: 2026-07-26

**Status**: Draft

**Input**: User description: "实现 Analyzer 的 Evidence-to-Attribution 处理链路：从已格式化采集数据中抽取事实、推导现象与定位层级；对候选归因执行证据边界与证据反问校验；仅允许 LLM 基于结构化分析结果生成报告。"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 获取受证据约束的归因结论 (Priority: P1)

作为诊断结果使用者，我希望 Analyzer 先把已有诊断数据整理为事实、异常现象和可定位层级，再输出归因结论，以便结论能够对应到输入数据而不是由模型直接猜测。

**Why this priority**: 这是降低错误归因的最小闭环；没有这一能力，后续的报告约束和稳定性校验都没有可靠输入。

**Independent Test**: 使用 CPU 热点、IO 等待和证据不足的固定输入执行 RCA，验证结果包含事实、现象、定位层级、归因状态和结论边界。

**Acceptance Scenarios**:

1. **Given** 存在 CPU 使用率高和函数热点的诊断数据，**When** 执行 RCA，**Then** 结果将 CPU 压力识别为现象，并将定位结论限制到有对应证据的函数层级。
2. **Given** 存在 IO 等待和块设备延迟的诊断数据，**When** 执行 RCA，**Then** 结果将 IO 等待归类为支持的归因，并将无热点证据的 CPU 原因标记为不支持或削弱。
3. **Given** 只有轻度资源异常且没有明确热点或等待证据，**When** 执行 RCA，**Then** 结果不得输出明确根因。

---

### User Story 2 - 检查归因结论对关键证据的依赖 (Priority: P2)

作为诊断结果使用者，我希望每个归因结论都经过关键证据反问校验，以便知道“缺少某条数据后结论是否仍成立”。

**Why this priority**: 单条指标或重复证据容易被过度解释；稳定性校验能明确结论应降级到什么层级。

**Independent Test**: 对函数热点归因移除函数热点证据，验证函数级结论降级；移除 CPU 高证据，验证 CPU 瓶颈结论被禁止。

**Acceptance Scenarios**:

1. **Given** 一个由 CPU 高和函数热点共同支持的函数级归因，**When** Analyzer 执行证据反问校验，**Then** 结果标记两条数据为关键证据，并说明移除函数热点数据后只能定位到资源层。
2. **Given** 一个只有单条弱证据支持的归因，**When** Analyzer 执行证据反问校验，**Then** 结果将该归因标记为不稳定，且不允许作为明确根因输出。

---

### User Story 3 - 生成边界明确的 RCA 报告 (Priority: P3)

作为诊断结果使用者，我希望最终报告清楚分开事实、推断、关键证据、反向证据和结论边界，以便理解结论可信范围。

**Why this priority**: 报告是用户直接看到的结果；它必须反映结构化分析的边界，而不能重新引入模型自由归因。

**Independent Test**: 将包含明确根因和证据不足两类分析结果交给报告生成流程，验证报告只能引用存在的证据，并在证据不足时使用非确定性结论。

**Acceptance Scenarios**:

1. **Given** 有稳定归因及关键证据的结构化分析结果，**When** 生成报告，**Then** 报告说明主因、关键证据、反向证据和定位边界。
2. **Given** 结论边界禁止声明根因的结构化分析结果，**When** 生成报告，**Then** 报告不得使用“根因是”等确定性措辞。

---

### User Story 4 - 溯源运行控制行为 (Priority: P1)

作为诊断结果使用者，我希望系统在进程被暂停、终止、重启或冻结时，不仅报告当前进程状态，还能回查异常发生前后的运行控制事件，以便区分直接故障机制和真正触发该机制的控制行为。

**Why this priority**: 仅观察到进程处于 `T` 状态只能解释业务为何停止推进，不能回答谁通过什么动作暂停了进程；即使捕获直接控制动作，也不能在缺少上游来源时误报为完整来源根因。

**Independent Test**: 对运行中的测试进程发送 `SIGSTOP`，验证常驻信号观察器保存发送者、目标、信号和时间，诊断将控制事件与后续 `T` 状态关联并输出直接根因；删除历史控制事件后，验证结论降级为直接故障机制；只有补齐动作来源时才允许输出完整来源根因。

**Acceptance Scenarios**:

1. **Given** 持久化观察器在异常窗口内记录了 `SIGSTOP` 发送事件，**When** 目标进程随后持续进入 `T` 状态，**Then** 结论必须指出发送者、控制动作、目标和状态变化，并允许将其标记为 `direct_root_cause`；只有补齐动作来源链后才允许标记为 `complete_source_root_cause`。
2. **Given** 目标进程持续处于 `T` 状态但没有同窗控制事件，**When** 生成结论，**Then** 系统只能输出 `process_suspended` 直接故障机制，并明确暂停发起者未知。
3. **Given** py-spy、off-CPU、Trace 或 baseline 探针只产出空窗口或不可解析产物，**When** 执行 AI 树资格校验，**Then** 这些探针不得被计为有效深度证据，也不得升级定位层级或置信度。
4. **Given** 日志流水线文件持续增长，**When** 执行日志窗口扫描，**Then** 采集器必须有界读取目标时间窗口，不得将整个日志文件加载到内存。

---

### User Story 5 - 获得真实 AI 参与的多根因解释 (Priority: P1)

作为诊断结果使用者，我希望最终结论由会话级受控 AI 基于全部有效证据裁决，并在多个原因同时成立时区分主因、贡献原因和独立原因，以便理解每个原因分别解释了什么症状，而不是看到由规则模板拼接的证据摘要。

**Why this priority**: 当前系统可以保存多个候选和次因字段，但会话归因仍只选择一个分类；AI 裁决失败后还可能被重新标记为 `ai_guarded`，导致门禁通过但最终解释并非 AI 生成。

**Independent Test**: 让会话级 AI 分别处理单一 `SIGSTOP` 因果链和 CPU 噪声加下游暂停的复合证据，验证 AI 参与状态真实、根因等级正确，并为复合场景输出两个独立通过门禁的根因簇。

**Acceptance Scenarios**:

1. **Given** 会话级 LLM 返回合法受控裁决，**When** 保存终态结论，**Then** 系统标记 `ai_review_status=succeeded`，并允许终态树使用 `generated_by=ai_guarded`。
2. **Given** LLM 返回非结构化内容、越过证据边界或重试失败，**When** 回退到 Analyzer，**Then** 系统必须标记 `fallback` 或 `failed`，且 readiness gate 不得把该结果视为 AI 裁决通过。
3. **Given** `bash` 同窗发送 `SIGSTOP` 且目标随后进入停止状态，**When** 缺少父进程、命令来源或控制面身份，**Then** 结论必须停在 `direct_root_cause`，不得声明 `complete_source_root_cause`。
4. **Given** 两个原因分别拥有独立有效证据并解释不同症状，**When** 会话级 AI 完成裁决，**Then** 系统输出 `compound_incident` 和至少两个根因簇，并明确主因与贡献或独立关系。
5. **Given** 多个候选只是同一机制的不同表述或复用同一证据，**When** 形成根因簇，**Then** 系统必须将其合并或降级，不能虚增根因数量。
6. **Given** 用户打开诊断详情，**When** 查看终态结论和 AI 树，**Then** 页面分别展示原因、因果链、排除项、未知边界和按根因簇组织的建议处理。

---

### User Story 6 - 聚合持续异常并按影响自动诊断 (Priority: P1)

作为持续监视使用者，我希望同一个持续异常只冻结一次现场并只创建一次完整 AI 诊断，同时仍能查看该 Episode 内所有异常点，以免重复触发浪费采集和模型预算。

**Why this priority**: 任务驱动采集会错过瞬时现场，而每次偏移都创建诊断又会在持续异常和正常重任务下产生大量重复消耗。

**Independent Test**: 连续提交同一目标的多次 CPU 偏移、带延迟的资源偏移、进程暂停和恢复窗口，验证 Episode 聚合、影响门禁、唯一诊断、恢复关闭和异常点轻量解释缓存。

**Acceptance Scenarios**:

1. **Given** 同一 Watch 在 120 秒内连续出现相关偏移，**When** Agent 重复上报观察窗口，**Then** 系统只保留一个活动 Episode 和一组首次同窗采集，并累计异常点次数和峰值。
2. **Given** 只有 CPU 上升且没有延迟、错误、throttling 或队列影响，**When** 自动诊断已开启，**Then** 系统保存异常点和现场但不自动创建 AI 树。
3. **Given** 目标进程暂停或资源偏移与用户影响同窗出现，**When** Episode 完成聚合和同窗采集，**Then** 系统幂等创建一个 AI 集群诊断会话。
4. **Given** Episode 连续三个观察周期恢复正常或静默超过 120 秒，**When** 后续再次发生异常，**Then** 系统关闭旧 Episode 并创建新 Episode。
5. **Given** 用户点击某个异常点的“分析”，**When** 轻量 AI 返回解释，**Then** 页面展示触发原因、变化和数据质量，不发探针、不创建 AI 树、不输出根因；重复点击复用 fingerprint 缓存。

---

### Edge Cases

- 同一原始指标被多个规则重复引用时，系统不得把它当作多份独立证据提高置信度。
- 资源层证据存在但函数、线程或调用链数据不存在时，系统只能停留在资源层。
- 支持某候选原因的证据与削弱该原因的证据同时存在时，系统必须输出冲突或降级结果。
- LLM 不可用或报告生成失败时，结构化分析结果仍应保留，且不得丢失结论边界。
- 进程已处于 group-stopped 状态且运行时 profiler 无法附加时，系统不得重复请求同类 profiler；应转查信号、systemd、容器运行时或 cgroup 控制事件。
- 采集任务成功退出但结构化结果为空、损坏或不支持目标层级时，系统必须保留产物并将证据状态标记为无效或空窗口，不能等同于有效证据。
- 控制事件包含命令行、环境变量或用户信息时，系统必须先脱敏再保存结构化摘要。
- 会话级 AI 失败后，系统不得通过修改 `generated_by` 或复制模板文案伪装成 AI 已参与。
- 两个候选引用相同目标、机制、同窗证据和传播路径时，系统必须合并为一个根因簇。
- 已确认直接控制动作但未知上游来源时，系统必须保留可执行的直接根因结论，同时明确来源未知，不能强制拒答或过度升级。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 系统必须从已有 RCA 证据中生成带有稳定标识、来源和观察状态的事实项。
- **FR-002**: 系统必须从事实项推导异常现象，且每个现象必须保留其支持事实。
- **FR-003**: 系统必须根据已有证据确定最大可支持定位层级，且不得输出超过该层级的归因。
- **FR-004**: 系统必须将候选归因标记为支持、削弱、证据缺失或禁止，并记录支持与反向事实。
- **FR-005**: 系统必须对每个可输出归因执行关键证据反问校验，并记录移除关键事实后的结论变化。
- **FR-006**: 系统必须将稳定性校验结果用于主因、次因、伴随现象和不支持原因的最终组合。
- **FR-007**: 系统必须向报告生成流程提供结构化分析结果，且报告不得引用结构中不存在的事实或超过结论边界。
- **FR-008**: 当结构化分析无法支持明确根因时，系统必须输出可定位现象和证据不足原因，而不是选出最低分候选作为根因。
- **FR-009**: 本功能不得创建采集任务、调用采集器或请求用户补充数据。
- **FR-010**: 系统必须区分采集执行状态、产物生成状态和证据有效状态；只有满足对应证据族最小质量要求的结果才能完成 AI 树补证请求。
- **FR-011**: 系统必须将 `empty_window`、`blocked`、`target_exit`、`unparseable` 和 `partial` 保留为可审计状态，且不得将空栈、空 Trace 或不可解析 perf 结果标记为有效深度证据。
- **FR-012**: 日志采集器必须采用有界流式或尾部窗口读取，并支持按 PID、systemd unit、容器和时间窗口选择日志来源。
- **FR-013**: 持久化 Agent 必须低成本记录目标范围内的信号控制事件，至少包括信号类型、发送者 PID/comm/UID、目标 PID/comm 和事件时间。
- **FR-014**: 信号事件必须进入有界 Rolling Buffer，并在 trigger event 或诊断创建时按目标和时间窗口冻结为结构化 `runtime_control_event` 证据。
- **FR-015**: 系统必须将运行控制事件、进程状态变化及业务影响组织为可引用的因果边；同目标、正确时序且可解释状态变化的控制事件只能升级为直接根因，补齐动作来源链后才能升级为完整来源根因。
- **FR-016**: 当只观测到持续停止状态而缺少直接控制行为时，系统必须输出直接故障机制并保持部分归因，不得声明直接根因或完整来源根因。
- **FR-017**: 评分和 readiness gate 必须按有效结构化证据评分，失败任务、空窗口和仅出现过采集器名称不得满足必需证据族。
- **FR-018**: systemd、Docker/containerd、cgroup、发布变更和 Kubernetes Audit 控制溯源必须在本轮以独立 producer 接入同一 `runtime_control_event` 契约；来源不可用时必须报告能力边界，不能伪造事件。
- **FR-019**: systemd、Docker/containerd、cgroup 和发布变更 producer 必须在 Linux VM 上进行真实来源测试；Kubernetes Audit producer 在无集群环境下必须使用真实格式 fixture 验证，并明确保留集群级验收边界。
- **FR-020**: 系统必须区分 `observation`、`direct_failure_mechanism`、`direct_root_cause` 和 `complete_source_root_cause`；新诊断不得生成语义含混的 `complete_root_cause`。
- **FR-021**: `direct_root_cause` 必须具备同目标、正确时序的 actor/action/target/effect 证据；`complete_source_root_cause` 还必须具备父进程、命令来源、systemd/cgroup、发布记录或审计身份中的至少一种可引用来源链。
- **FR-022**: 系统必须记录会话级 `ai_review_status`、模型、尝试次数和失败原因；只有通过全部工程校验的会话级裁决才能标记为 `succeeded` 和 `ai_guarded`。
- **FR-023**: 系统必须在所有当前有效证据回流后执行会话级受控 AI 裁决，并使用该结果生成终态根因簇、解释、反证摘要和建议处理。
- **FR-024**: 系统必须按机制、目标、证据同窗和传播路径聚合根因簇，并为每个簇保存角色、原因等级、解释症状、因果链、证据引用、与主因关系和残余未知。
- **FR-025**: 只有至少两个独立通过结论资格门禁的根因簇同时成立时，系统才能输出 `classification=compound_incident`。
- **FR-026**: 根因簇不得包含 rejected、unknown、observation-only 或未通过结论资格门禁的候选，也不得将同一证据重复计为多个独立来源。
- **FR-027**: 最终结论必须分别提供一句话结论、症状机制解释、因果链、排除项、残余未知和按根因簇组织的建议处理，且不得用同一段文本重复填充所有解释字段。
- **FR-028**: 前端必须分别展示终态人话结论、根因簇和逐层受控 AI 树，并在根因簇中区分主因、贡献原因和独立原因。
- **FR-029**: readiness gate 必须根据会话级 AI 裁决成功状态验收 AI 参与；复合场景评分必须验证合格根因簇数量、独立证据引用和症状覆盖。
- **FR-030**: 持续监视必须将 120 秒内的连续异常聚合到持久化 Episode，并保存状态、首次/末次时间、发生次数、恢复计数和异常点列表。
- **FR-031**: 每个 Episode 必须最多创建一个正式 AI 集群诊断会话；并发回灌、重复轮询和服务重启不得产生重复诊断。
- **FR-032**: 第一次异常必须立即冻结 rolling snapshot 并执行所选同窗采集；后续同 Episode 触发不得重复启动同一组采集器。
- **FR-033**: 统计型资源偏移必须存在延迟、错误、throttling、队列或等价影响证据后才具备自动诊断资格；硬状态可以立即具备资格。
- **FR-034**: Watch 必须支持默认开启的 `auto_diagnosis_enabled`，关闭后继续观察、冻结和保存，但不得自动创建诊断。
- **FR-035**: 单异常点解释必须是一次有界结构化 AI 调用，禁止输出根因、请求探针、创建 AI 树或执行修复，并按 anomaly fingerprint 缓存。
- **FR-036**: 前端必须按 Watch -> Episode -> anomaly point 展示持续监视结果，并区分异常点“分析”和 Episode“AI 集群诊断”。
- **FR-037**: Episode 必须在连续三个正常观察周期或 120 秒静默后关闭，恢复期间复发必须复用原 Episode。
- **FR-038**: Python 内存深采集必须使用 Memray 官方 Producer；Mini-Drop 只能执行 capability preflight、受控调用、官方产物留存和结构化适配，不得用手写 GC 引用图或轻量对象扫描器冒充工业 Heap Profiler。
- **FR-039**: py-spy 必须以官方 raw/collapsed 栈作为结构化主输入并保留函数、文件、行号、调用路径、样本数和百分比；SVG 只能作为展示产物。
- **FR-040**: baseline 和 Profile 结构化数据必须拒绝超出 `0-100` 的百分比、负样本、不可解释的超大样本数、裸地址或 `[unknown]` 函数升级定位层级。
- **FR-041**: 源码上下文必须通过 Git revision 校验和有界源码片段 Producer 获取；系统不得上传整个仓库或执行源码内容。
- **FR-042**: memory leak 候选必须优先请求 `python_heap_profile`、`python_runtime_profile` 和 `source_snapshot`，只有存在独立等待、延迟或调用链信号时才允许请求 off-CPU/Trace。
- **FR-043**: 会话级 AI 必须在每轮补证前裁决候选并从 Probe Manifest 选择最小必要工业 Producer；未发生真实模型调用时不得增加模型调用计数或标记 AI 裁决成功。
- **FR-044**: AI 可以提出新的机制候选，但候选必须具有父候选、真实 evidence refs、证据支持的定位边界和 Manifest 内补证请求，且必须通过反问和工程资格门禁。

### Key Entities *(include if feature involves data)*

- **Fact**: 从已有 RCA 证据中抽取的原子观察，包含来源、指标、值、时间窗口和状态。
- **Symptom**: 由一个或多个事实支持的异常现象，例如 CPU 压力、IO 等待或函数热点。
- **Localization**: 当前事实最多支持的资源、进程、线程、系统调用、函数或调用路径层级。
- **Attribution**: 对候选原因的受约束判断，包含支持事实、反向事实、缺失事实和结论边界。
- **Evidence Challenge**: 对归因移除单条关键事实后的结果，用于表示结论稳定性和关键证据。
- **Analysis Result**: 供 RCA 报告使用的完整结构化分析结果，包含主因、次因、伴随现象、不支持原因和结论边界。
- **Runtime Control Event**: 运行控制动作的结构化事件，包含 actor、action、target、observed_at、effect 和 evidence reference。
- **Evidence Validity**: 独立于任务终态的证据质量状态，用于表示结果是否为空、受阻、部分有效、不可解析或满足对应证据族门槛。
- **Control Causality Edge**: 从控制行为到运行状态变化再到服务影响的有向关系，包含时序、目标一致性和支持证据。
- **Root Cause Cluster**: 对同一机制、目标、证据同窗和传播路径的候选归并结果，包含主因/贡献/独立角色、原因等级、解释症状、因果链、证据引用和残余未知。
- **AI Review Status**: 会话级受控 AI 裁决的真实执行状态，用于区分成功裁决、Analyzer 回退和调用失败。
- **Watch Episode**: 同一监视目标上一段连续异常的持久化聚合单元，包含冻结证据、异常点、采集任务、恢复状态和唯一正式诊断。
- **Watch Anomaly Point**: 检测层产生的结构化异常观察，包含稳定 fingerprint、指标变化、窗口、次数、影响状态和可缓存轻量解释，不表示根因。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 对覆盖 CPU 热点、IO 等待和证据不足的基准诊断输入，100% 生成事实、现象、定位层级和结论边界。
- **SC-002**: 在缺少函数或调用链证据的测试中，100% 不输出函数或调用路径级归因。
- **SC-003**: 在移除结论关键证据的测试中，100% 将结论降级或禁止，而不是保留原有确定性结论。
- **SC-004**: 在证据不足的测试中，100% 不输出明确 root cause。
- **SC-005**: 报告生成输入中的每个归因结论均包含至少一条可追溯事实或被标记为证据不足。
- **SC-006**: 对 `SIGSTOP` 真实 case，控制事件存在时 100% 输出发送者、信号、目标和状态变化；控制事件缺失时 100% 停止在直接故障机制。
- **SC-007**: 对空 off-CPU、空 Trace、失败 py-spy 和不可解析 baseline 输入，100% 不计为有效深度证据。
- **SC-008**: 对至少 1 GB 的增长中 NDJSON 日志，日志窗口扫描保持有界内存并在探针执行窗口内返回结构化状态。
- **SC-009**: 诊断输出不得同时出现高置信直接/完整来源根因、`abstained=true`、空 root-cause candidate 和无资格 AI 树主因。
- **SC-010**: 会话级 AI 调用失败或被边界校验拒绝时，100% 不标记为 `ai_guarded`，readiness gate 100% 不通过 AI 参与检查。
- **SC-011**: 对只有 `bash -> SIGSTOP -> stopped` 的真实运行控制 case，100% 输出 `direct_root_cause` 并明确来源未知，不输出 `complete_source_root_cause`。
- **SC-012**: 对 `OB-COMPOUND-NOISY-DOWNSTREAM-001`，系统输出至少两个拥有独立证据引用的合格根因簇，并分别解释依赖失败与延迟放大。
- **SC-013**: 对重复候选或共享同一证据的输入，根因簇数量在重复运行中保持一致，且不会将重复候选计为独立根因。
- **SC-014**: 最终结论、根因簇详情和 AI 树节点不再使用同一段证据摘要填充“结论”和“为什么是它”；每个主因簇均提供机制解释和至少一项具体建议处理。
- **SC-015**: 同一 Episode 连续触发 10 次时，正式诊断会话和首次同窗 collector group 数量均保持为 1。
- **SC-016**: 仅有 CPU 或内存资源偏移的测试中，100% 保存 anomaly point 且 0 次自动创建 AI 树；加入影响信号后同一 Episode 最多创建 1 次。
- **SC-017**: 单异常点解释重复请求时模型调用次数保持为 1，且输出 100% 不包含 root cause、probe request 或 remediation execution。
- **SC-018**: Werkzeug #1521 复测中所有结构化百分比均处于 `0-100`，不得出现 `[unknown]` 或裸地址作为函数层最终锚点。
- **SC-019**: Werkzeug #1521 复测中 py-spy 结构化证据至少保留一个 `werkzeug/routing.py` 行级候选，且会话树保留一致的 source context hash。
- **SC-020**: Werkzeug #1521 的 memory 分支至少执行一次有效 Memray Producer，并优先于无独立等待信号的 off-CPU/Trace 补证。
- **SC-021**: Werkzeug #1521 终态必须明确区分 Memray 直接观测的持续未释放分配与 AI 基于源码形成的引用机制推断；无法支持完整机制时必须降级而不是伪造引用链。

## Assumptions

- 已有 RCA evidence、候选原因和评分输入可以继续复用，不修改采集端契约。
- 第一阶段只覆盖现有项目可表达的 CPU、IO、内存、函数热点和系统调用相关数据。
- 既有 LLM 报告与修复建议能力继续保留，但其输入增加受约束的分析结果。
- 原始 Analyzer 策略选择机制保持兼容；本轮扩展复用现有诊断会话自动补证调度，不新增第二套任务协议。
- 3A、3B、3C 在同一轮实现；当前三节点 VM 可真实验收 Linux、systemd、Docker、cgroup 和发布变更来源，但不具备真实 Kubernetes 集群验收条件。
