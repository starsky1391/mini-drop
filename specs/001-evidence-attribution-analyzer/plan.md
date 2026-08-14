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

**Project Type**: Python web service with React client; this feature changes the server-side RCA processing path only

**Performance Goals**: Analysis of a normal RCA evidence snapshot adds no external calls and completes within the existing diagnosis request path

**Constraints**: Reuse formatted collector data; do not schedule collection tasks; do not add historical-case dependencies; do not let the LLM introduce cause IDs or evidence outside the structured analysis result

**Scale/Scope**: One shared Analyzer module, two existing RCA strategy integration points, one existing LLM validation path, and focused unit/integration tests

## Scheme B Runtime Boundary and Kubernetes Migration

方案 B 当前以三节点 Docker VM 作为真实验收环境，负责验证工业采集器、结构化证据、AI 树补证和 Persistent Watch 的完整闭环。Kubernetes 不属于方案 B 的当前完成条件，而是后续的环境后端迁移方案。

当前 Docker VM 验收已覆盖真实 Redis case、手工窗口 Persistent Watch case 和 Agent 自动观察 Persistent Watch case。Watch 最新自动观察结果为 `needs_evidence` 有界终态，已验证 Agent 自采 baseline/trigger window、自动触发 `cpu_shift` incident、collector task 回灌、delayed follow-up 保留和测试 watch 自动停用；深度 CPU perf 和 endpoint/call_path 升级仍受 Worker 宿主机 `kernel.perf_event_paranoid=4` 与 Trace 源为空限制，不能计入已完成能力。

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

## Constitution Check

The current project constitution remains the unfilled Spec Kit template and defines no enforceable project-specific gates. The implementation follows the repository instructions: preserve existing worktree changes, keep the change scoped, write tests for new processing behavior, and avoid collector or task-scheduling changes.

Post-design check: pass. The design adds one cohesive RCA processing module and does not add external services, storage, or new API surface.

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
