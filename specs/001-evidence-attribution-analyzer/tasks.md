# Tasks: Evidence-to-Attribution Analyzer

**Input**: Design documents from `/specs/001-evidence-attribution-analyzer/`

**Prerequisites**: plan.md and spec.md

**Tests**: New processing behavior is test-first because each functional requirement is an analysis-result invariant.

**Organization**: Tasks are grouped by user story and preserve existing RCA behavior outside the new processing stage.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel when files do not overlap.
- **[Story]**: User story traceability label.

## Path Conventions

- Server RCA code: `server/app/rca/`
- Tests: `tests/`

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Prepare the focused test target and confirm the existing RCA baseline.

- [x] T001 Add failing evidence-to-attribution behavior tests in `tests/test_rca_attribution.py`
- [x] T002 Run the existing RCA strategy regression tests in `tests/test_rca_strategies.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Add the shared structured models and analysis transformation used by all strategies.

- [x] T003 Add structured analysis models to `server/app/rca/models.py`
- [x] T004 Implement fact, symptom, localization, attribution, and evidence challenge processing in `server/app/rca/attribution.py`
- [x] T005 Add structured analysis serialization support in `server/app/rca/evidence.py`

**Checkpoint**: Evidence-to-attribution results can be created and validated independently.

---

## Phase 3: User Story 1 - 获取受证据约束的归因结论 (Priority: P1) 🎯 MVP

**Goal**: Run the evidence-first analysis stage in the existing RCA strategies before report generation.

**Independent Test**: CPU hotspot, IO wait, and evidence-insufficient inputs produce supported, weakened, or forbidden conclusions with a localization boundary.

- [x] T006 [US1] Integrate structured analysis into `server/app/rca/strategies/linear.py`
- [x] T007 [US1] Integrate structured analysis into `server/app/rca/strategies/graph.py`
- [x] T008 [US1] Extend strategy integration assertions in `tests/test_rca_strategies.py`

**Checkpoint**: The default and graph strategies produce the same protected analysis artifact before LLM reporting.

---

## Phase 4: User Story 2 - 检查归因结论对关键证据的依赖 (Priority: P2)

**Goal**: Enforce evidence challenge results when composing allowed conclusions.

**Independent Test**: Removing a critical CPU or hotspot fact downgrades the related conclusion; unsupported conclusions cannot be promoted to a root cause.

- [x] T009 [US2] Extend evidence challenge cases in `tests/test_rca_attribution.py`
- [x] T010 [US2] Use evidence challenge stability when composing final analysis in `server/app/rca/attribution.py`

**Checkpoint**: Key evidence and conclusion downgrade behavior are covered without new collector calls.

---

## Phase 5: User Story 3 - 生成边界明确的 RCA 报告 (Priority: P3)

**Goal**: Bind the existing LLM report to structured analysis boundaries.

**Independent Test**: Reports cannot cite a cause outside the allowed set and must mark insufficient evidence when root-cause claims are forbidden.

- [x] T011 [US3] Add analysis-bound prompt constraints and validation in `server/app/rca/prompt.py` and `server/app/rca/llm_client.py`
- [x] T012 [US3] Add report boundary tests in `tests/test_rca.py`

**Checkpoint**: The LLM report path preserves the Analyzer's fact and conclusion boundaries.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Validate the complete change and update the tracked task status.

- [x] T013 Run focused RCA tests: `tests/test_rca.py`, `tests/test_rca_enhanced.py`, `tests/test_rca_strategies.py`, and `tests/test_rca_attribution.py`
- [x] T014 Verify the implementation matches `specs/001-evidence-attribution-analyzer/spec.md` and mark completed tasks in `specs/001-evidence-attribution-analyzer/tasks.md`

---

## Dependencies & Execution Order

- T001 and T002 begin first.
- T003 through T005 are required before strategy integration.
- T006 through T008 deliver the P1 processing flow.
- T009 and T010 add the evidence challenge behavior to that flow.
- T011 and T012 bind the report path after structured analysis exists.
- T013 and T014 complete validation and task tracking.

## Parallel Opportunities

- T001 and T002 can run in parallel.
- T006 and T007 can run in parallel after T003 through T005, but both require review before T008.
- T009 can be prepared before T010, then run first.
- T011 and T012 are sequential because tests depend on the report validation contract.

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Write focused analysis tests.
2. Add the models and one cohesive processing module.
3. Integrate it into the linear strategy.
4. Validate CPU, IO, and insufficient-evidence behavior.

### Incremental Delivery

1. Add the default processing path.
2. Apply the same artifact to the graph strategy.
3. Add evidence challenge stability checks.
4. Enforce the artifact in LLM reporting.
5. Run the focused RCA regression suite.

## Notes

- The scope excludes collector changes, task creation, automatic probing, API changes, and front-end changes.
- Existing uncommitted changes remain untouched unless directly required by these tasks.

---

## Extended Plan: Collection-First Depth Upgrade

**Purpose**: Extend the existing evidence-to-attribution chain so it can detect what the collector is missing, trigger deeper profiling when needed, and keep repeated-task conclusions stable.

### Task Group A - 补齐采集层深挖工具

- [x] A001 Add `off_cpu_wait_profile` as a probe definition in `server/app/diagnosis/probe_registry.py`.
- [x] A002 Add `trace_endpoint_profile` as a probe definition in `server/app/diagnosis/probe_registry.py`.
- [x] A003 Add `baseline_window_profile` as a probe definition in `server/app/diagnosis/probe_registry.py`.
- [x] A004 Update probe selection logic in `server/app/diagnosis/probe_registry.py` so CPU, IO, and unstable-window symptoms can trigger the new probes.
- [x] A005 Add tests for new probe registration and symptom-to-probe mapping in `tests/test_diagnosis_probe_registry.py`.
- [x] A006 Add a minimal artifact contract for off-CPU, trace, and baseline outputs in `server/app/diagnosis/schemas.py` or adjacent structured schemas.

**Outcome**: The collector can request deeper evidence instead of stopping at on-CPU function hotspots.

### Task Group B - 补齐 AI 定位层的缺证识别

- [x] B001 Extend `server/app/rca/models.py` or the structured analysis model layer with `missing_evidence`, `blocked_upgrades`, and `collection_gaps` fields.
- [x] B002 Update `server/app/rca/attribution.py` so the analysis result records which collector capabilities are missing for deeper localization.
- [x] B003 Update `server/app/rca/report.py` and `server/app/rca/llm_client.py` so reports can explain the current localization boundary and the missing collection capabilities.
- [x] B004 Add tests that verify the analysis result can say "cannot promote beyond function level because off-CPU / trace evidence is missing".
- [x] B005 Add tests that verify reports do not upgrade beyond the boundary reported by the structured analysis result.

**Outcome**: The Analyzer can explain not only what it knows, but also what collector evidence is missing.

### Task Group C - 收紧重复任务稳定性

- [x] C001 Add or extend evidence challenge tests to cover repeated-window and baseline-sensitive scenarios.
- [x] C002 Update `server/app/rca/calibrator.py` or the cause composition path so primary cause selection uses fixed tie-break rules instead of candidate order.
- [x] C003 Add deduplication rules for repeated or semantically equivalent evidence so the same signal is not counted multiple times.
- [x] C004 Add tests for conclusion stability across repeated runs with the same dataset and small threshold fluctuations.
- [x] C005 Verify the new task groups still preserve the existing CPU hotspot, IO wait, and evidence-insufficient behaviors.

**Outcome**: Repeated executions produce similar conclusions, and small evidence drift does not flip the main attribution.

### Task Group D - 原始证据先存后取

- [x] D001 Add a cached raw evidence index so stack / trace / off-CPU artifacts can be stored once and queried by `evidence_ref` on demand.
- [x] D002 Update the Analyzer evidence loading path so LLM input only receives summarized evidence first, with raw evidence fetched lazily when needed.
- [x] D003 Add tests proving large stack / trace payloads are not eagerly sent to LLM, but can still be retrieved when analysis requests a deeper check.

**Outcome**: The analyzer keeps full raw evidence for audit and replay, while AI only consumes compact summaries and targeted slices when needed.

### Task Group E - AI 树门控与保守叶子

- [x] E001 Define AI tree node inputs and outputs in `server/app/rca/models.py` or adjacent structured models, including branch decision, next evidence request, and leaf status.
- [x] E002 Implement tree gating logic in `server/app/rca/attribution.py` so branches decide whether to continue, downgrade, or stop at a conservative leaf.
- [x] E003 Add tests proving the same evidence set produces the same tree branch decisions across repeated runs.
- [x] E004 Add tests proving insufficient evidence results in `conservative_leaf` or `unknown_leaf` instead of forced root-cause promotion.

**Outcome**: The Analyzer uses a stable AI tree that can stop safely when evidence is incomplete.

### Task Group F - 轻量图回连层

- [x] F001 Define minimal graph entities for `trace / endpoint / service / instance / context` linkage in `server/app/rca/models.py` or a dedicated graph module.
- [x] F002 Implement lightweight graph collation in `server/app/rca/attribution.py` so the same hotspot can be tied back to one request path and one context.
- [x] F003 Add tests proving one hotspot maps to a stable endpoint and service/instance chain when the input evidence is unchanged.
- [x] F004 Add tests proving graph collation does not overreach into final root-cause attribution.

**Outcome**: The Analyzer can explain which call chain a hotspot belongs to without turning graph collation into final judgment.

### Task Group G - 方案 3 升级预留

- [x] G001 Add explicit extension points in the structured analysis result for a future stronger graph reasoning layer.
- [x] G002 Keep current `facts / symptoms / localizations / attributions / evidence_challenges` contracts stable while reserving upgrade fields for future graph inference.
- [x] G003 Add tests proving current outputs remain backward compatible when future upgrade fields are absent.

**Outcome**: The current AI tree and lightweight graph layer can evolve into a stronger future scheme without breaking existing boundaries.

### Task Group H - AI 树冲突裁决分枝

- [x] H001 Extend `server/app/rca/models.py` or adjacent structured models with explicit conflict-branch metadata, including conflict type, evidence family, and branch decision.
- [x] H002 Implement conflict-resolution branching in `server/app/rca/attribution.py` so the tree can prefer stability, downgrade on opposing evidence, or stop with a conservative leaf.
- [x] H003 Add tests proving that competing but partially supported causes resolve to the same branch decision across repeated runs.
- [x] H004 Add tests proving weakly supported candidates are downgraded instead of promoted when conflict evidence is present.

**Outcome**: The AI tree becomes more stable when multiple candidate directions are plausible.

### Task Group I - 图层热点归属边

- [x] I001 Extend the lightweight graph layer with explicit hotspot-ownership edges for `call_path -> function` and `function -> line` refinements.
- [x] I002 Update graph collation in `server/app/rca/attribution.py` so the same hotspot resolves to the same request path and context when input evidence is unchanged.
- [x] I003 Add tests proving hotspot ownership edges remain stable across repeated runs with identical evidence.
- [x] I004 Add tests proving ownership edges do not become root-cause assertions.

**Outcome**: Hotspots can be tied back to a stable chain without turning the graph into final judgment.

### Task Group J - 证据请求闭环

- [x] J001 Extend tree leaf outputs with explicit next-evidence requests tied to missing evidence families.
- [x] J002 Implement a feedback loop in `server/app/rca/attribution.py` so conservative leaves can request the next best evidence type instead of stopping silently.
- [x] J003 Add tests proving insufficient evidence emits a deterministic next-evidence request list.
- [x] J004 Add tests proving the request list is stable for the same evidence gap and does not change across repeated runs.

**Outcome**: The tree can explain what evidence to collect next, completing the evidence-challenge loop.

### Suggested Execution Order

1. Finish Task Group A first so the collector can expose deeper evidence.
2. Finish Task Group B next so the Analyzer can report missing evidence and boundary limits.
3. Finish Task Group C last so repeated-task stability is improved after the evidence model is complete.
4. Finish Task Group D after the evidence model is stable so evidence retrieval stays token-efficient.
5. Finish Task Group E before Task Group F so tree gating is defined before graph collation.
6. Finish Task Group F before Task Group G so the future upgrade points are based on a stable current graph layer.
7. Finish Task Group H before Task Group I so conflict resolution is settled before hotspot ownership is refined.
8. Finish Task Group I before Task Group J so ownership edges exist before the next-evidence feedback loop is tuned.
