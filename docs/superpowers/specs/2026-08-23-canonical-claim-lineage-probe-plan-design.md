# Canonical Claim Lineage and Probe Plan Design

**Date:** 2026-08-23

**Status:** Proposed

## 1. Objective

收口诊断链路中的三类分散状态：

```text
canonical_candidate_state
canonical_claim_lineage
canonical_probe_plan
```

本设计解决以下问题：

- 区分 Analyzer、AI、历史恢复、fallback 和父节点继承产生的 claim。
- 禁止 refinement 子节点与父节点拥有完全相同的 claim。
- 将 fallback、retained conclusion 和 boundary 分离。
- 防止历史候选进入当前 `session_main`。
- 防止调查轮覆盖首轮探针请求。
- 防止 first-write-wins 丢失完整 `ai_generated_query` 和 `query_spec_hash`。

本轮采用一次性切换到新 canonical contract 的方式，不保留旧字段的模糊语义兼容层。

## 2. Design Principles

1. `generated_by` 不再承担 claim 来源和 AI 新生成语义。
2. 当前树、历史记录、探针历史和数据质量记录必须分离。
3. 所有候选和探针请求先归一化，再进入调度或报告序列化。
4. 子探针失败只能产生局部 boundary，不能反证来源父节点。
5. 只有同一 canonical eligibility result 可以同时决定正式结论、报告、树状态和 abstention。
6. 不通过数组顺序、layer 顺序、rank 或默认 coarse 节点推断父节点。

## 3. Canonical Claim Contract

节点使用以下字段：

```json
{
  "generated_by": "analyzer | ai | fallback | history | system",
  "claim_origin": "analyzer_rule | analyzer_diagnostic | analyzer_summary | ai_proposal | ai_update | history_restore | fallback_generated",
  "claim_transform": "original | refined | inherited | restored | boundary",
  "claim_status": "active | retained | inherited | boundary | rejected | duplicate",
  "claim": "...",
  "claim_hash": "...",
  "source_claim_hash": "...",
  "source_candidate_id": "...",
  "source_round": 1,
  "source_event_id": "..."
}
```

字段职责：

- `generated_by` 记录候选记录由哪个阶段产生。
- `claim_origin` 记录当前文本最初由谁提供。
- `claim_transform` 记录文本如何从来源演化。
- `claim_status` 记录当前是否正式、继承、边界或重复。
- `source_candidate_id` 和 claim hash 形成可审计的文本血缘。

AI guard 通过只表示 AI 审查成功，不自动把 Analyzer 节点改成 AI claim，也不自动重生成 claim。

## 4. Claim Lineage Pipeline

新增 `server/app/diagnosis/canonical_claim_lineage.py`，集中处理 claim 登记、更新、继承和边界生成。

入口包括：

- `server/app/rca/candidates.py`
- `server/app/diagnosis/orchestrator.py`
- `server/app/rca/llm_client.py`
- `server/app/diagnosis/session_conclusion.py`

来源映射：

```text
rules.json description        -> analyzer_rule
diagnostic_claim              -> analyzer_diagnostic
summary                       -> analyzer_summary
candidate_proposals.claim     -> ai_proposal
candidate_updates.claim       -> ai_update
parent retained claim         -> inherited
history restore               -> history_restore
boundary message              -> boundary
```

AI update 不包含 `claim` 时，必须保留原 claim lineage；只有包含新的 claim 文本时才创建新的 AI claim lineage。

## 5. Parent-Child Specificity Gate

新增 `validate_claim_refinement(parent, child)`。

claim 比较先执行标准化：Unicode NFKC、首尾空白清理、连续空白折叠、统一标点和大小写，然后比较 `claim_hash`。

`refinement` 子节点必须满足：

```text
父节点真实存在
origin_parent_candidate_id 合法
子节点 claim 非空
子节点 claim_hash 不等于父节点 claim_hash
至少增加 mechanism、target、定位层级、定位对象或有效 causal chain 步骤之一
```

以下情况不创建 refinement 子节点：

- 父子 claim 标准化后完全相同。
- 只增加 `evidence_refs`，没有增加机制、目标或定位信息。
- 只改变标点、语气或前缀。

处理规则：

```text
相同 claim + 新证据 -> 合并为父节点 evidence update
相同 claim + 新探针 -> 保留 probe edge，不创建子结论
相同 claim + refinement -> duplicate_claim 数据质量记录，不进入 session_main
父节点无法解析 -> orphan + data_quality，不自动补 coarse
```

## 6. Canonical Candidate State

新增 `server/app/diagnosis/canonical_candidate_state.py`，作为当前 session tree 的唯一 reducer。

候选归一化顺序固定为：

```text
Analyzer base candidates
  -> AI proposals
  -> AI updates
  -> probe result updates
  -> retained parent
  -> boundary generation
  -> final eligibility
```

候选状态至少包含：

```text
candidate_id
session_id
round
tree_kind
renderable
relation
parent_candidate_ids
origin_parent_candidate_id
claim_lineage
supported_level
mechanism
target
evidence_refs
decision
status
conclusion_eligible
```

`session_main` 只接受当前轮、父节点真实、claim lineage 合法且通过关系门禁的节点。
`child_snapshot`、`probe_history`、历史恢复节点和数据质量记录不能混入主树。

## 7. Fallback, Retained and Boundary

探针 `blocked`、`failed`、`timed_out`、`empty_window`、`unparseable`、`target_exit` 或 `partial` 时：

```text
来源父节点
  -> 保留原 claim、claim_origin、evidence_refs 和 causal_status
  -> 增加局部 boundary 子节点
  -> 记录 retained_conclusion 和 qualification_boundary
```

boundary 使用独立 `boundary_message`，不生成正式 root-cause claim。

没有有效来源父节点时，只输出 orphan、data quality 和 abstention；不得编造 generic fallback claim。

## 8. Session AI Participation

删除 layer 级别通过复制产生的 `generated_by=ai_guarded` 语义。

会话统一记录：

```json
{
  "ai_review_status": "succeeded | fallback | failed",
  "ai_review_scope": "candidate_round | investigation_round | session",
  "ai_review_attempts": 0,
  "ai_review_model": "...",
  "ai_review_error": "..."
}
```

只有结构化输出、候选关系、claim lineage、证据引用、探针清单和结论资格全部通过时，才允许 `ai_review_status=succeeded`。

## 9. Canonical Probe Plan

新增 `server/app/diagnosis/canonical_probe_plan.py`，负责首轮、调查轮和历史计划的统一归一化。

请求族使用 union 语义：

```text
canonical_families = initial_selected_families ∪ investigation_selected_families
```

每个请求以以下字段形成稳定键：

```text
evidence_family
candidate_id
origin_parent_candidate_id
query_spec_hash
target_scope_hash
evidence_window_hash
```

输入合并采用字段级规则：

- 空值不能覆盖非空值。
- 简略 provenance 不能覆盖完整 `ai_generated_query`。
- 完整 query spec 覆盖同字段的空值或简略值。
- `candidate_id` 和 `origin_parent_candidate_id` 冲突时不自动覆盖。
- 不同 `query_spec_hash` 必须保留冲突记录或创建新的 variant。

`source_mechanism_query` 最终必须保留 `query_spec_hash`、调查问题、候选 ID、来源父节点、期望关系和路径锚点。

## 10. Orchestrator Flow

`server/app/diagnosis/orchestrator.py` 的统一顺序：

```text
读取当前轮输入
  -> claim lineage 归一化
  -> canonical candidate state
  -> 父子关系和 specificity gate
  -> canonical probe plan union/merge
  -> probe input 完整性校验
  -> 调度 follow-up
  -> retained/boundary 生成
  -> session_main、报告和 localization chain 序列化
```

任何 probe、报告或前端 payload 都不能绕过 canonical state 直接读取某轮 LLM 原始结果。

## 11. Localization Chain

`localization_chain` 通过 `origin_parent_candidate_id` 追溯，并按 `claim_hash` 去重：

- 连续相同 claim 只展示一次。
- inherited 节点只展示“继承自父节点结论”。
- boundary 使用独立停止步骤，不作为 claim 展示。
- 每一步保留 candidate ID、claim status 和 evidence refs。

## 12. Frontend Contract

修改：

- `web/src/components/diagnosis/aiTreeGraphModel.js`
- `web/src/components/diagnosis/ControlledAITreeGraph.jsx`
- `web/src/pages/AIDiagnosis.jsx`

前端分为四个区域：

```text
session_main   当前主树
probe_history  探针尝试、回退和停止过程
data_quality   orphan、duplicate claim、父节点冲突和输入冲突
boundary       当前分支停止原因
```

前端只能消费 canonical 字段，不得根据 `generated_by`、数组顺序、颜色或 layer 顺序推断 claim 来源和资格。

## 13. Validation

后端测试覆盖：

- 所有 Analyzer、AI、历史、fallback claim origin 映射。
- AI update 无 claim 时保持原 lineage。
- retained parent 和 boundary 分离。
- 父子完全相同、仅改标点、只增加证据时均拒绝伪 refinement。
- mechanism、target 或 localization 增强时允许 refinement。
- 子探针失败不反证父节点。
- 历史候选不进入 `session_main`。

探针测试覆盖：

- 首轮多个 family 与调查轮 family 做 union。
- 完整 query 覆盖简略 provenance。
- `query_spec_hash` 不丢失。
- 相同请求只调度一次。
- 不同 query hash 产生 conflict 或 variant。
- 未知 family 产生显式 gate failure。

端到端验收必须确认：

```text
001 -> 004 不再出现相同 claim 子节点
002 -> 005 不再出现相同 claim 子节点
003 -> 006 不再出现相同 claim 子节点
localization_chain 不连续重复
fallback 不显示为 AI 新结论
历史候选不进入 session_main
首轮 probe 不被调查轮静默移除
source_mechanism_query 不再因 query_spec_hash 丢失而阻断
```

## 14. Implementation Order

```text
CG-1 模型与 claim lineage
CG-2 canonical candidate reducer
CG-3 Analyzer/AI/retained/fallback 接入
CG-4 parent-child specificity gate
CG-5 canonical probe plan union 和字段级 merge
CG-6 session conclusion、报告和 localization chain
CG-7 前端主树、历史树和 data quality
CG-8 单元、集成、回放测试
CG-9 pytest、compileall、前端构建、git diff --check
CG-10 使用最新真实报告回归重复 claim 和 probe 覆盖问题
```

## 15. Completion Criteria

- 每个节点都有明确的 claim origin 和 claim transform。
- `generated_by` 不再代表 AI 新生成语义。
- refinement 子节点不能与父节点拥有相同标准化 claim。
- fallback 只保留来源父结论并增加 boundary。
- 历史节点、probe history 和 data quality 不污染 `session_main`。
- 首轮和调查轮 probe request 使用 union 语义。
- 完整 query input 覆盖简略 provenance。
- 正式报告、前端和探针调度均来自 canonical state。
