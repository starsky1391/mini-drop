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

### Task Group K - 证据结构化处理层契约

- [x] K001 Add a non-AI evidence structuring module for artifact-to-evidence conversion.
- [x] K002 Define stable structured outputs for `artifact_refs`, `top_functions`, `stack_summary`, `call_path_hotspots`, `evidence_index`, and `confidence_inputs`.
- [x] K003 Add tests proving the same artifact inputs produce deterministic structured evidence.

**Outcome**: Raw and semi-raw collector artifacts become compact, referenceable evidence before RCA analysis.

### Task Group L - Artifact 入口接入

- [x] L001 Update task diagnosis artifact loading so `top_json`, `depth_evidence_json`, `flamegraph_json`, `flamegraph_svg`, `sys_metrics`, and `ebpf_metrics` are passed through the structuring layer.
- [x] L002 Update diagnosis session analysis so reusable structured artifacts are normalized before RCA evidence is collected.
- [x] L003 Ensure large raw artifacts and SVG payloads stay as references unless explicitly requested by `evidence_ref`.

**Outcome**: RCA no longer depends on whether a collector produced the exact final JSON contract up front.

### Task Group M - RCA 消费与验证

- [x] M001 Update RCA evidence collection to consume structured `top_functions` and `evidence_index` without introducing new attribution logic.
- [x] M002 Add tests proving pyspy-style SVG-only artifacts do not get sent to LLM as raw payloads but still leave audit references.
- [x] M003 Add tests proving depth evidence produces `stack_summary`, `call_path_hotspots`, and confidence inputs usable by AI tree and graph collation.
- [x] M004 Run focused RCA and diagnosis tests after the structuring layer is connected.

**Outcome**: AI 树和 Evidence-to-Attribution receive compact structured evidence, reducing hallucination and token waste.

### Suggested Execution Order For K-M

1. Finish K first to lock the non-AI contract.
2. Finish L next so HTTP diagnosis and diagnosis sessions share the same normalized evidence input.
3. Finish M last to prove RCA consumes the new structure without treating it as root-cause judgment.

### Task Group N - 证据请求受限映射

- [x] N001 Define the mapping from `next_evidence_requests` values to registered `ProbeDefinition` entries without adding a new nexttask protocol.
- [x] N002 Add a structured follow-up request / plan record carrying `diagnosis_id`, `parent_task_id`, `evidence_gap`, probe id, risk level, and execution policy.
- [x] N003 Add tests proving unknown AI evidence requests are ignored and registered requests map deterministically.

**Outcome**: AI tree requests become safe, auditable `ProbePlan` candidates.

### Task Group O - 诊断会话去重与预算

- [x] O001 Track generated and executed evidence gaps per `diagnosis_id`.
- [x] O002 Prevent the same `diagnosis_id + evidence_gap` from generating another follow-up task by default.
- [x] O003 Enforce existing diagnosis budget and terminal-state checks before creating follow-up tasks.
- [x] O004 Add tests proving repeated analysis does not create duplicate follow-up tasks.

**Outcome**: The evidence loop cannot spin indefinitely on one unresolved gap.

### Task Group P - 自动执行策略

- [x] P001 Add global default and per-diagnosis override for `safe_only`, `all_registered`, and `manual` follow-up execution.
- [x] P002 Auto-start eligible low-risk follow-up probes and route high-risk probes according to the selected policy.
- [x] P003 Preserve registered-probe, approval, platform, and budget constraints for all policies.
- [x] P004 Add tests for safe-only default, explicit high-risk automatic execution, and manual approval mode.

**Outcome**: Follow-up evidence is automatic by default within controlled policy boundaries.

### Task Group Q - 结果回灌与持续诊断

- [x] Q001 Re-enter the existing diagnosis progression after a follow-up child task reaches a terminal state.
- [x] Q002 Re-run structuring and AI tree analysis against the same `diagnosis_id` with the new evidence.
- [x] Q003 Stop when the tree reaches a terminal leaf, the session budget is exhausted, or no new request is executable.
- [x] Q004 Add end-to-end tests for collect -> analyze -> next_evidence_requests -> follow-up probe -> re-analyze.

**Outcome**: Task-driven diagnosis becomes an automatic evidence-questioning loop without implementing Persistent Agent yet.

### Task Group R - Persistent Agent 后续路线

- [x] R001 Document the future lightweight persistent trigger agent boundary and lifecycle.
- [x] R002 Document future ring-buffer / rolling-profile evidence retention and trigger snapshot behavior.
- [x] R003 Verify the current follow-up task and structured evidence contracts can be reused by a future triggered diagnosis session.

**Outcome**: Persistent monitoring remains an explicit upgrade path, not an unfinished partial implementation.

### Task Group S - 证据同窗协议

- [x] S001 Add evidence window metadata fields: `trigger_event_id`, `evidence_cohort_id`, `collection_mode`, `window_start`, `window_end`, and `timing_relation`.
- [x] S002 Extend structured evidence output so every artifact-derived evidence item can carry window metadata without changing root-cause logic.
- [x] S003 Add validation for allowed `collection_mode` and `timing_relation` values.
- [x] S004 Add tests proving same-window and delayed-follow-up evidence are represented distinctly.

**Outcome**: Analyzer can tell whether evidence belongs to the same abnormal window, a manual window, or a delayed follow-up.

### Task Group T - Analysis Session 普适入口

- [x] T001 Define an `analysis_session` creation path for ordinary completed tasks that do not already belong to a diagnosis session.
- [x] T002 Auto-structure completed task artifacts and attach them to the analysis session as `manual_single` or `manual_group` evidence.
- [x] T003 Expose the latest staged analysis result on the task detail or analysis-session detail API.
- [x] T004 Reuse existing Evidence-to-Attribution and AI tree logic instead of creating a second RCA path.
- [x] T005 Add tests proving a normal task can produce a staged conclusion without the user first creating a diagnosis.

**Outcome**: Conclusions can be published from ordinary collection tasks, while diagnosis sessions remain the active deep-diagnosis entry.

### Task Group U - Persistent Evidence Trigger 最小闭环

- [x] U001 Define the `trigger_event` schema with target, baseline window, trigger window, trigger signal, confidence, and action.
- [x] U002 Add a lightweight sliding-window evaluator for relative shifts in CPU, latency, thread count, iowait, memory, or error bursts.
- [x] U003 Map trigger types to registered collector groups without allowing trigger rules to emit root-cause conclusions.
- [x] U004 Create an `evidence_cohort_id` for each triggered collector group and pass it to all child tasks.
- [x] U005 Add tests proving trigger events create collector groups with shared cohort metadata and no attribution result.

**Outcome**: The system can capture suspicious evidence windows before the user manually starts diagnosis.

### Task Group V - Rolling Buffer / Triggered Snapshot

- [x] V001 Define rolling buffer retention settings for low-cost metrics, endpoint summaries, trace summaries, and lightweight stack summaries.
- [x] V002 Add snapshot metadata for `snapshot_id`, `trigger_event_id`, `evidence_cohort_id`, pre-window, post-window, and artifact refs.
- [x] V003 Freeze the pre-trigger and post-trigger window when a trigger event fires.
- [x] V004 Feed frozen snapshots through the existing evidence structuring layer as `rolling_snapshot` evidence.
- [x] V005 Add tests proving frozen snapshots keep stable artifact references and do not send raw large payloads to the AI path.

**Outcome**: Short-lived incidents can retain before-and-after evidence without always-on heavy profiling.

### Task Group W - AI 树同窗证据裁决

- [x] W001 Update Evidence-to-Attribution to include timing metadata in facts, evidence challenges, and conclusion boundaries.
- [x] W002 Prevent `delayed_followup` evidence from directly refuting stronger `same_window` evidence.
- [x] W003 Add a conflict branch for contradictory same-window evidence from different collectors.
- [x] W004 Update report output to state conclusion window, timing relation, delayed-follow-up reproduction status, and non-refutable evidence boundaries.
- [x] W005 Add tests proving delayed non-reproduction downgrades certainty only when no stronger same-window evidence exists.

**Outcome**: AI tree conclusions become stable across transient incidents and do not overinterpret late follow-up data.

### Task Group X - WatchSubscription 驱动的 Persistent Agent Runtime

- [x] X001 Document the watch-subscription control plane so Persistent Agent knows which target to observe without relying on diagnosis tasks.
- [x] X002 Add watch subscription, watch lease, registry, and runtime models.
- [x] X003 Expose API endpoints to create/list watches, list agent watch leases, disable watches, and evaluate watch windows.
- [x] X004 Reuse `evaluate_persistent_trigger` from watch runtime without allowing watch rules to emit root-cause conclusions.
- [x] X005 Add tests proving active watches create agent leases and triggered evaluations update `trigger_event_id` / `evidence_cohort_id`.
- [x] X006 Add a frontend Persistent Watch page for creating watches, viewing leases, and seeing recent trigger/cohort metadata.

**Outcome**: Persistent Agent has a visible control-plane answer for "who am I monitoring?" while diagnosis remains separate from watch observation.

### Task Group Y - WatchIncident / Frozen Evidence Inbox

- [x] Y001 Document the save-first trigger chain: freeze rolling snapshot, persist trigger/cohort, then run policy-controlled short probes.
- [x] Y002 Add `trigger_action` to watch subscriptions with `freeze_only`, R1-limited `freeze_and_safe_probe`, and `auto_all_registered` behavior.
- [x] Y003 Add `WatchIncident` so each abnormal window is appended instead of overwriting `last_trigger`.
- [x] Y004 Bind each incident to `trigger_event_id`, `evidence_cohort_id`, `snapshot_id`, `snapshot_refs`, and `collector_tasks`.
- [x] Y005 Add API support for listing incidents under a watch.
- [x] Y006 Update the Persistent Watch frontend into an expandable watch -> incident view.
- [x] Y007 Add tests proving multiple incidents are retained and `freeze_only` creates no collector tasks.

**Outcome**: Frozen abnormal windows remain available for later AI tree analysis even when the transient issue has already disappeared.

### Task Group Z - WatchIncident -> AI 树分析入口

- [x] Z001 Store same-window structured evidence on each `WatchIncident`.
- [x] Z002 Add API support for analyzing a frozen incident without creating a fake collector task.
- [x] Z003 Reuse the existing Evidence-to-Attribution / AI tree engine with a read-only incident task context.
- [x] Z004 Disable automatic repair execution for incident-originated analysis.
- [x] Z005 Write analysis status, analysis session id, and analysis result back to the incident.
- [x] Z006 Enable the frontend incident "AI 树分析" action and show the returned summary/status.
- [x] Z007 Add tests proving incident analysis uses the frozen same-window evidence cohort.

**Outcome**: A frozen abnormal window can be analyzed after the transient issue disappears, without pretending delayed probes can reconstruct lost runtime state.

### Suggested Execution Order For S-Z

1. Finish S first so every later feature can carry window and cohort metadata.
2. Finish T next so ordinary task results can publish staged conclusions through the same analyzer.
3. Finish U after S and T so persistent triggers reuse the same task and analysis contracts.
4. Finish V after U so rolling snapshots freeze around real trigger events.
5. Finish W last so AI tree timing rules operate on complete metadata from both manual and triggered evidence.
6. Finish X after W so watch subscriptions can reuse trigger/cohort contracts and expose them in the UI.
7. Finish Y after X so every watch can retain multiple frozen abnormal windows for later AI tree analysis.
8. Finish Z after Y so each frozen abnormal window can enter AI tree analysis as a first-class object.
