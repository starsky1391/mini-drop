# Python Source Localization Scenario Expansion Design

**Date:** 2026-08-23

**Status:** Proposed

## 1. Objective

在不扩展 Java、Go、C/C++ 等语言的前提下，将现有 Python 源码定位能力从
`python_memory_retention` 拓宽到更多 Python 运行时故障大场景。

本设计保留现有证据优先原则：

```text
症状证据
  -> Python 场景证据
  -> function / call_path / file:line 候选
  -> source_snapshot revision 和源码行校验
  -> 场景专属机制证据
  -> 结论资格门禁
```

源码行只回答“异常在哪里发生或被放大”。源码根因必须额外证明“为什么该
源码位置导致当前故障机制”。

## 2. Scope

本轮只设计 Python 语言内的场景拓宽。

包含：

- Python CPU 热点和自身代码耗时。
- Python 请求路径慢和 endpoint 慢。
- Python 锁竞争、线程等待和 runtime wait。
- Python 异常风暴和错误路径放大。
- Python worker / Celery 队列积压。
- Python I/O 阻塞调用点。
- Python 连接池或资源池耗尽。
- Python 重试风暴和超时配置问题。

不包含：

- Java、Go、C/C++ 或 native 语言源码机制。
- 自动修复、重启服务、修改配置或执行任意 shell。
- 使用 LLM 直接猜测源码行。
- 将下游依赖、宿主机资源或控制面动作强行归因为源码根因。

## 3. Existing Capability Baseline

已有能力可以作为场景拓宽的通用底座：

| Evidence family | Producer / module | Current role |
| --- | --- | --- |
| `python_heap_profile` | Memray | Python 分配和保留分配热点 |
| `python_runtime_profile` | py-spy | Python 栈、线程状态、函数和行候选 |
| `source_snapshot` | Git + universal-ctags + bounded Python AST hints | revision 校验、源码片段、符号上下文 |
| `source_mechanism_query` | CodeQL | 已验证行锚点后的源码调用链和数据流机制 |
| `python_heap_reference` | PyHeap | Python 对象入向引用链 |
| `trace_endpoint_profile` | Trace/profile adapter | endpoint、call path 和热点函数关联 |
| `off_cpu_wait_profile` | off-CPU profiler | 等待和阻塞栈 |
| `log_scan` | bounded log window | 错误、异常和日志模板 |
| `dependency_check` / `redis_check` | dependency probes | 下游依赖边界和反证 |

`python_memory_retention` 是当前最完整链路：

```text
RSS/smaps
  -> Memray retained allocation
  -> python_runtime_profile
  -> source_snapshot
  -> source_mechanism_query
  -> python_heap_reference
  -> direct_root_cause eligibility
```

其他 Python 场景复用这条工程模式，但不复用内存场景的结论资格。

## 4. Scenario Capability Matrix

| Scenario | Current state | Target state |
| --- | --- | --- |
| Python memory retention | 已具备完整闭环 | 保持为参考模板 |
| CPU hotspot / self code regression | 已有函数和行定位底座 | 增加 CPU 场景门禁和 baseline 退化判断 |
| Endpoint latency | 部分具备 Trace/call path/function 关联 | 增加 endpoint/profile/source 同窗闭合 |
| Lock contention / thread wait | 具备等待观察和栈定位 | 增加 wait site、holder candidate 和源码门禁 |
| Exception storm | 具备 log_scan 和 traceback 入口 | 增加 exception cluster 和 throw/log site |
| Worker / Celery backlog | 具备 worker 栈和部分真实 case | 增加 queue/task 证据契约 |
| I/O blocking call site | 具备 off-CPU/runtime 栈 | 增加 blocking kind 和 dependency 分流 |
| Pool exhaustion | 暂无完整场景契约 | 新增 pool evidence family |
| Retry storm / timeout config | 暂无完整场景契约 | 新增 retry/timeout evidence family |

## 5. Shared Contracts

所有场景输出统一使用以下语义：

```text
observation
  仅描述观察，例如等待原语、异常模板、热点函数

partial_localization
  定位到 function / call_path / file:line，但机制未闭合

direct_failure_mechanism
  证明某个机制直接导致症状，但未证明完整源码根因

direct_root_cause
  同窗证据、源码机制和反证门禁均通过
```

每个场景必须提供：

- `scenario_type`
- `evidence_status`
- `target`
- `time_window`
- `runtime_frames`
- `line_candidates`
- `source_context_hash`
- `mechanism_evidence_refs`
- `counter_evidence_refs`
- `missing_evidence`
- `conclusion_eligible`
- `eligibility_reason`

## 6. CPU Hotspot Scenario

### Tools

- Existing: `process_cpu_profile`
- Existing: `python_runtime_profile`
- Existing: `source_snapshot`
- Existing optional: `baseline_window_profile`
- Existing optional: `trace_endpoint_profile`

### Data flow

```text
cpu_saturation / self_code_regression
  -> cpu_profile or python_runtime_profile
  -> stable Python business hotspot
  -> file:line candidate
  -> source_snapshot
  -> baseline comparison
  -> CPU source localization
```

### Implementation

Add a `python_cpu_hotspot` assessment branch in the orchestrator. The branch
requests `source_snapshot` when CPU evidence contains a valid Python
`file:line` candidate and source context is available.

The structured evidence must reject:

- percentages outside `0-100`;
- negative sample counts;
- bare addresses;
- `[unknown]`;
- single-sample tail frames;
- runtime plumbing such as `poll`, `select`, `epoll`, `sleep`, `futex`.

### Eligibility

`partial_localization` requires:

```text
stable hotspot
+ valid function or file:line
+ source_snapshot verified when line is claimed
```

`direct_root_cause` requires:

```text
partial_localization
+ baseline or same-window impact evidence
+ no stronger dependency / host counter-evidence
+ hotspot explains the user symptom
```

## 7. Endpoint Latency Scenario

### Tools

- Existing: `trace_endpoint_profile`
- Existing: `python_runtime_profile`
- Existing: `process_cpu_profile`
- Existing: `source_snapshot`
- Existing: `dependency_check`
- Existing: `log_scan`

### Data flow

```text
latency_increase
  -> trace_endpoint_profile
  -> endpoint and call_path
  -> profile hotspot inside that call path
  -> source_snapshot
  -> dependency counter-evidence check
```

### Implementation

Introduce `python_endpoint_latency` as a scenario classification. The
orchestrator can promote endpoint evidence to source localization only when
Trace and profile share:

- target service or instance;
- evidence window;
- endpoint or context ID;
- call path or function identity.

If `dependency_check` or `redis_check` explains the symptom, the scenario must
stop at `dependency` and keep source evidence as context only.

### Eligibility

`partial_localization` requires endpoint and call path correlation.

`direct_root_cause` requires:

```text
endpoint latency regression
+ same-window Python hotspot in the endpoint path
+ verified source line
+ dependency evidence does not better explain the symptom
```

## 8. Lock Contention and Thread Wait

### Tools

- Existing: `python_runtime_profile`
- Existing: `off_cpu_wait_profile`
- Existing: `source_snapshot`
- New: `python_lock_wait_profile`

### New producer contract

`python_lock_wait_profile` normalizes Python wait evidence:

```json
{
  "scenario_type": "python_lock_wait",
  "primitive": "Lock | RLock | Condition | Queue | Future | Semaphore | unknown",
  "wait_sites": [],
  "holder_candidates": [],
  "runtime_frames": [],
  "line_candidates": [],
  "evidence_validity": {}
}
```

The first version may be derived from py-spy and off-CPU stacks. It does not
need to instrument application code.

### Implementation

The collector identifies business frames above Python waiting primitives. It
marks `holder_candidates` only when there is direct evidence for a long-running
holder stack or repeated blocked/holder pairing in the same window.

### Eligibility

Waiting primitives alone are `observation`.

`partial_localization` requires:

```text
stable wait stack
+ business wait site file:line
+ source_snapshot verified
```

`direct_root_cause` requires:

```text
partial_localization
+ holder candidate or long-hold evidence
+ symptom impact evidence
+ no stronger external dependency explanation
```

## 9. Exception Storm

### Tools

- Existing: `log_scan`
- Existing: `python_runtime_profile`
- Existing: `source_snapshot`
- Existing optional: `trace_endpoint_profile`
- New: `python_exception_profile`

### New producer contract

`python_exception_profile` normalizes traceback clusters:

```json
{
  "scenario_type": "python_exception_storm",
  "exception_clusters": [
    {
      "exception_type": "",
      "message_template": "",
      "occurrence_count": 0,
      "throw_site": {},
      "catch_or_log_site": {},
      "top_frames": [],
      "evidence_ref": ""
    }
  ],
  "line_candidates": [],
  "evidence_validity": {}
}
```

### Implementation

Start with structured logs and traceback parsing from `log_scan`. Later runtime
sampling can enrich top frames, but the first version should not require a
live attach.

### Eligibility

`partial_localization` requires repeated exception clusters and a verified
business throw, catch, or log site.

`direct_root_cause` requires the exception path to explain error rate, latency,
CPU amplification, or worker failure in the same window. Downstream errors must
remain downstream unless source evidence proves local mishandling or retry
amplification.

## 10. Worker and Celery Backlog

### Tools

- Existing: `python_runtime_profile`
- Existing: `log_scan`
- Existing: `source_snapshot`
- Existing optional: `redis_check`
- New: `python_queue_profile`

### New producer contract

`python_queue_profile` records task and backlog evidence:

```json
{
  "scenario_type": "python_queue_backlog",
  "queue_system": "celery | rq | asyncio | generic",
  "backlog": 0,
  "active_tasks": [],
  "reserved_tasks": [],
  "slow_task_candidates": [],
  "broker_evidence": {},
  "evidence_validity": {}
}
```

### Implementation

First target Celery because the repository already has real Celery case
coverage. Use broker metrics, worker logs, active/reserved task metadata and
runtime stack frames to map backlog to task function candidates.

### Eligibility

`partial_localization` requires:

```text
backlog or worker saturation
+ task identity
+ worker runtime stack or log evidence
+ verified task source line
```

If broker/Redis explains the symptom, root cause remains `dependency`.

## 11. I/O Blocking Call Site

### Tools

- Existing: `off_cpu_wait_profile`
- Existing: `python_runtime_profile`
- Existing: `trace_endpoint_profile`
- Existing: `dependency_check`
- Existing: `source_snapshot`

### Data flow

```text
io_degradation / latency_increase
  -> off_cpu_wait_profile
  -> blocking syscall or client wait
  -> nearest Python business frame
  -> dependency status
  -> source_snapshot
```

### Implementation

Add a normalized blocking kind:

```text
file_io
socket_io
db_client
redis_client
http_client
dns
unknown_external
```

The diagnosis must separate local call site from external target. A Python
source line can be the blocking call site without being the root cause.

### Eligibility

`partial_localization` requires a blocking Python call site and verified source
line.

`direct_root_cause` requires local code behavior such as unbounded synchronous
I/O, loop-internal blocking calls, missing timeout, or avoidable large file
access. External dependency slowness remains dependency-level.

## 12. Pool Exhaustion

### Tools

- New: `python_pool_profile`
- Existing: `log_scan`
- Existing: `python_runtime_profile`
- Existing: `source_snapshot`
- Existing optional: `source_mechanism_query`

### New producer contract

```json
{
  "scenario_type": "python_pool_exhaustion",
  "pool_type": "sqlalchemy | redis | urllib3 | aiohttp | generic",
  "pool_exhausted": false,
  "checked_out": null,
  "pool_size": null,
  "wait_sites": [],
  "acquire_sites": [],
  "release_evidence": "present | missing | unknown",
  "long_holder_candidates": [],
  "evidence_validity": {}
}
```

### Implementation

The first version should support common log patterns and stack signatures
instead of invasive instrumentation. Library-specific enrichments can be added
behind the same contract.

### Eligibility

`partial_localization` requires pool exhaustion plus verified acquire or wait
site.

`direct_root_cause` requires acquire/release asymmetry, long holder evidence,
or configuration evidence proving the pool behavior causes the symptom.

## 13. Retry Storm and Timeout Configuration

### Tools

- New: `python_retry_timeout_profile`
- Existing: `log_scan`
- Existing: `trace_endpoint_profile`
- Existing: `source_snapshot`
- Existing: `dependency_check`

### New producer contract

```json
{
  "scenario_type": "python_retry_timeout",
  "retry_clusters": [],
  "timeout_sites": [],
  "attempt_count": 0,
  "backoff_detected": false,
  "nested_retry": false,
  "dependency_context": {},
  "config_refs": [],
  "evidence_validity": {}
}
```

### Implementation

Start from logs and traces. The producer detects repeated attempts, retry
markers, timeout exceptions, and dependency target reuse in a bounded window.

### Eligibility

`partial_localization` requires repeated attempts and a verified retry or
timeout source site.

`direct_root_cause` requires proof that retry or timeout behavior amplified the
primary symptom. If a downstream outage is the primary cause, retry behavior is
`contributing` unless it independently explains the incident.

## 14. Orchestrator Changes

Add a scenario router before generic follow-up selection:

```text
python_memory_retention
python_cpu_hotspot
python_endpoint_latency
python_lock_wait
python_exception_storm
python_queue_backlog
python_io_blocking
python_pool_exhaustion
python_retry_timeout
```

Each scenario supplies:

- initial evidence families;
- follow-up evidence order;
- terminal evidence statuses;
- source localization gate;
- root cause eligibility gate;
- dependency or host counter-evidence rules.

The router must keep blocked probes local to the scenario. A failed deep probe
creates a boundary and preserves the parent claim; it must not produce a new
generic fallback root cause.

## 15. LLM Guardrails

The LLM may:

- choose registered evidence families from the Probe Manifest;
- propose falsifiable Python mechanism candidates;
- rank supported candidates;
- request `source_snapshot`, `source_mechanism_query`, or scenario producers
  when the manifest allows them.

The LLM may not:

- directly create `line` candidates;
- cite non-existent evidence refs;
- invent source files, line numbers, tasks, locks, pools or retry counts;
- choose unregistered probes;
- turn runtime primitives into root causes;
- treat dependency failure as source root cause without local amplification
  evidence.

## 16. Frontend and Audit

Diagnosis detail should show each scenario in the same shape:

```text
Scenario summary
  -> evidence families run
  -> source localization chain
  -> mechanism evidence
  -> counter-evidence
  -> conclusion eligibility
  -> residual unknowns
```

The AI tree must distinguish:

- `observation`
- `base_cause`
- `line_anchor`
- `mechanism_explanation`
- `stop_boundary`
- `rejected_candidate`

Audit bundle should include scenario type, producer outputs, source anchor
eligibility, mechanism gate checks and final eligibility reason.

## 17. Validation Plan

Unit tests:

- producer contract validation for each new evidence family;
- invalid percentage/sample/function filtering;
- source line cannot be produced without `source_snapshot`;
- runtime primitive observations cannot become root causes;
- blocked producer emits boundary only.

Orchestrator tests:

- correct follow-up order per scenario;
- dependency counter-evidence stops source root promotion;
- line anchor requires revision, file, line and source hash;
- one failed deep probe does not discard valid sibling candidates.

Session conclusion tests:

- formal conclusion fields come from one eligibility result;
- `partial_localization` never enters final root cause clusters;
- contributing retry or pool behavior is not promoted to primary without
  independent evidence.

Evaluation cases:

- CPU hotspot with verified source line.
- Endpoint latency explained by local Python hotspot.
- Endpoint latency explained by downstream dependency.
- Lock wait with only waiting primitive.
- Lock wait with verified holder candidate.
- Exception storm with verified throw site.
- Celery backlog with broker failure.
- Celery backlog with task source hotspot.
- Pool exhaustion with missing release evidence.
- Retry storm with downstream outage and local amplification.

## 18. Implementation Order

Phase 1:

```text
python_cpu_hotspot
  -> source localization gate
  -> baseline / impact eligibility
```

Phase 2:

```text
python_lock_wait_profile
python_exception_profile
  -> observation versus root cause separation
```

Phase 3:

```text
python_queue_profile
python_io_blocking normalization
  -> dependency counter-evidence rules
```

Phase 4:

```text
python_pool_profile
python_retry_timeout_profile
  -> mechanism-specific conclusion gates
```

## 19. Completion Gates

The expansion is complete when:

```text
each scenario has a structured evidence contract
each scenario has explicit follow-up order
line localization always requires source_snapshot verification
runtime primitive observations never become root causes
dependency and host evidence can block source-root promotion
failed probes create local boundaries only
final root cause eligibility is scenario-specific and auditable
frontend shows scenario evidence and missing gates without inferring from text
```

