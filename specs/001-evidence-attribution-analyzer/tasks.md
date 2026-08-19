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

### Task Group AA - AI Ops v2 Audit Bundle Export

- [x] AA001 Add `/api/v1/diagnoses/{diagnosis_id}/audit-bundle` API.
- [x] AA002 Export run metadata, runtime trace, topology snapshot, probes, child tasks, artifacts, evidence, structured evidence, evidence refs, conclusion, safety, rollback, and readiness gate.
- [x] AA003 Return 404 for missing diagnosis sessions instead of emitting an empty benchmark bundle.
- [x] AA004 Add tests proving audit bundles include runtime trace and readiness gate checks.

**Outcome**: A diagnosis session can produce the audit artifact required by the AI Ops v2 evaluator.

### Task Group AB - Benchmark Scoring Module

- [x] AB001 Add `server.app.diagnosis.benchmark_score` with deterministic `score_audit_bundle`.
- [x] AB002 Add `aggregate_results` with case/run accuracy, Wilson interval, evidence citation rate, trace coverage, unsafe action count, and repeat consistency.
- [x] AB003 Support abstention oracle cases without forcing root-cause output.
- [x] AB004 Verify `docs/ai_ops_v2_test/评测脚本/evaluate_diagnosis_bundles.py` can import the scoring module from the script directory.

**Outcome**: The evaluator can score exported bundles offline without sending private oracle data to the diagnosis system.

### Task Group AC - Collector Mapping Gate

- [x] AC001 Map `trace_endpoint_profile`, `off_cpu_wait_profile`, and `baseline_window_profile` to explicit server task types.
- [x] AC002 Map agent task type `2` to `trace_endpoint_profile` so trace endpoint probes do not silently fall back to `perf_cpu`.
- [x] AC003 Map agent task types `8` and `9` to their intended collector routes.
- [x] AC004 Add tests proving extended runtime collectors use explicit task-type routes.

**Outcome**: Benchmark probe plans can verify that existing runtime collector execution is not a fake perf fallback.

### Task Group AD - Baseline Artifact Structuring

- [x] AD001 Accept `continuous_top_json`, `continuous_flamegraph_json`, and `continuous_summary` as structured artifact inputs.
- [x] AD002 Normalize continuous baseline outputs into `top_json`, `flamegraph_json`, and `depth_evidence_json.baseline_summary` for RCA consumption.
- [x] AD003 Apply the normalization in both diagnosis session analysis and ordinary task analysis.
- [x] AD004 Add tests proving baseline window artifacts become structured evidence visible to audit bundles.

**Outcome**: Baseline sampling outputs become usable evidence instead of orphaned collector artifacts.

### Task Group AE - Readiness Gate Script

- [x] AE001 Add `docs/ai_ops_v2_test/评测脚本/check_readiness_gate.py`.
- [x] AE002 Check audit bundle existence, runtime trace, probes, child tasks, artifacts, structured evidence, evidence refs, and collector fallback.
- [x] AE003 Write a machine-readable readiness report and return non-zero when any bundle fails.
- [x] AE004 Smoke test the script against existing sample bundles to prove it reports concrete gaps.

**Outcome**: Formal benchmark runs have a preflight gate that catches missing evidence chains before scores are interpreted.

### Task Group AF - AI Ops v2 Documentation Sync

- [x] AF001 Document Scheme 2 as an AI Ops v2 Benchmark Readiness Gate rather than a full benchmark platform.
- [x] AF002 Document the audit bundle schema, readiness checks, collector mapping, baseline structuring, and recommended test order.
- [x] AF003 Mark completed Scheme 2 items in `docs/evidence_to_attribution_analyzer.md`.
- [x] AF004 Mark completed Scheme 2 tasks in this task file.

**Outcome**: The implementation, benchmark readiness policy, and task tracking remain aligned.

### Task Group AG - 工业采集器适配层

**Purpose**: Replace self-built lightweight collectors with adapters around mature collectors. Mini-Drop only schedules, reads, structures, and indexes evidence; it does not reimplement log tailing, blackbox probing, or Redis protocol collection.

- [x] AG001 Define industrial collector adapter contracts for `log_scan`, `dependency_check`, and `redis_check`, including adapter mode, external endpoint/config path, target selection, timeout, and evidence window metadata.
- [x] AG002 Add `log_scan` adapter using Fluent Bit Tail / Multiline or OpenTelemetry Collector `filelogreceiver` output as the source of truth.
- [x] AG003 Convert industrial log output into structured `log_window_json` with `summary`, `error_clusters`, `trace_ids`, `endpoints`, `dependencies`, `first_seen`, `last_seen`, and stable `evidence_ref` fields.
- [x] AG004 Add `dependency_check` adapter using Prometheus Blackbox Exporter `/probe` as the source of truth for DNS, TCP, HTTP, HTTPS, and gRPC reachability.
- [x] AG005 Convert Blackbox Exporter probe metrics into structured `dependency_check_json` with `checks`, `summary`, per-target duration, success state, failure phase, error type, and stable `evidence_ref` fields.
- [x] AG006 Add `redis_check` adapter using Redis Exporter metrics as the source of truth, with an optional controlled Redis command snapshot only for evidence fields Redis Exporter cannot expose in the required window.
- [x] AG007 Convert Redis Exporter / Redis snapshot data into structured `redis_check_json` with `connectivity`, `info_summary`, `slowlog_summary`, `latency_summary`, and stable `evidence_ref` fields.
- [x] AG008 Ensure all three adapters output artifact metadata with `artifact_type`, `collector_family`, `evidence_window`, `trigger_event_id`, `evidence_cohort_id`, `collection_mode`, and `timing_relation`.
- [x] AG009 Add adapter tests using recorded Fluent Bit / OTel, Blackbox Exporter, and Redis Exporter fixture outputs; tests must prove raw collector output is not sent directly to AI.
- [x] AG010 Add readiness gate checks that fail when a required collector family is only planned or mapped but lacks the matching structured artifact.

**Outcome**: `log_scan`, `dependency_check`, and `redis_check` become industrial-collector-backed evidence families with deterministic structured outputs.

### Task Group AH - 最小必要采集器选择

**Purpose**: Make AI tree follow-up requests choose the smallest required industrial-backed evidence family based on case type, existing evidence families, and missing evidence families.

- [x] AH001 Define a deterministic evidence-family inventory from structured evidence: `log_scan`, `dependency_check`, `redis_check`, `runtime_snapshot`, `sys_metrics`, `trace_endpoint_profile`, `baseline_window_profile`, and `network_profile`.
- [x] AH002 Implement missing-family detection for downstream, Redis, Payment, DNS, TCP, HTTP, gRPC, log-error, and runtime-snapshot cases without letting these rules emit root-cause conclusions.
- [x] AH003 Map `downstream_dependency` candidates to minimal follow-up order: `dependency_check` first, then `log_scan`, then domain-specific collectors such as `redis_check`.
- [x] AH004 Map Redis-specific candidates to `redis_check` only when `dependency_check` or `log_scan` has produced Redis-related evidence or the topology explicitly marks a Redis dependency.
- [x] AH005 Prevent duplicate follow-up requests when the same `diagnosis_id + evidence_family` already has a same-window or active follow-up artifact.
- [x] AH006 Add tests proving identical input evidence produces identical `next_evidence_requests` order across repeated runs.
- [x] AH007 Add tests proving delayed follow-up evidence cannot directly refute stronger same-window industrial collector evidence.

**Outcome**: AI tree asks for the least necessary structured evidence instead of repeatedly launching broad or self-written collectors.

### Task Group AI - 结构化证据消费与审计

**Purpose**: Ensure industrial collector outputs enter the same Evidence-to-Attribution and audit bundle path as runtime snapshots.

- [x] AI001 Extend the evidence structuring layer to normalize `log_window_json`, `dependency_check_json`, and `redis_check_json` into compact `evidence_index` sections.
- [x] AI002 Add `confidence_inputs` fields for `has_log_signal`, `has_dependency_signal`, `has_redis_signal`, `failed_dependency_count`, `log_error_cluster_count`, and Redis slowlog/latency signals.
- [x] AI003 Ensure LLM input receives compact summaries and `evidence_ref` values only; raw log lines, raw Prometheus metric dumps, and large exporter payloads remain referenced artifacts.
- [x] AI004 Update audit bundle export so each structured artifact is visible under `artifacts`, `structured_evidence`, `evidence_refs`, and collector-family readiness checks.
- [x] AI005 Add tests proving required evidence families are scored only when the structured artifact exists, not when a probe was merely scheduled.

**Outcome**: Industrial collector data is usable by AI tree, benchmark gate, and final reports without token-heavy raw payloads.

### Suggested Execution Order For AG-AI

1. Finish AG first so each mature collector has a stable adapter and structured artifact contract.
2. Finish AI next so adapter outputs are visible to Evidence-to-Attribution, audit bundle, and readiness gate.
3. Finish AH last so next-evidence selection can reason over real structured evidence families instead of planned collector names.

### Task Group AJ - Managed Collector Profile

**Purpose**: Make industrial collectors practical by hiding per-task configuration behind Agent-managed defaults, capability discovery, and UI visibility.

- [x] AJ001 Add Agent-side `CollectorProfile` discovery for `log_scan`, `dependency_check`, `redis_check`, runtime tools, and degraded/unavailable reasons.
- [x] AJ002 Report `CollectorProfile` during Agent registration without breaking existing `capabilities` task routing.
- [x] AJ003 Keep `CollectorProfile` visible through `/api/agents` as both `collector_profile` and `latest_metrics.collector_profile`.
- [x] AJ004 Add managed worker sidecars for Fluent Bit and Blackbox Exporter, with Redis Exporter as an optional compose profile.
- [x] AJ005 Add default environment variables so Agent collectors can use managed sources without per-task options.
- [x] AJ006 Show collector availability summary on the dashboard and detailed collector status on the Agent detail page.
- [x] AJ007 Add tests for profile discovery, registration persistence, and API exposure.

**Outcome**: Users can start the worker stack and let Mini-Drop discover collector availability instead of configuring each collector per diagnosis task.

### Task Group AK - Target-Scoped Collector Invocation

**Purpose**: Prevent Agent-level collector availability from being confused with per-watch or per-diagnosis target configuration. Agent registration answers "can this worker collect"; each probe/task invocation answers "what exact target is collected this time".

- [x] AK001 Add a deterministic `collector_invocation` / `target_config` contract to probe parameters and task options.
- [x] AK002 Keep `CollectorProfile` limited to capability/status discovery and remove any assumption that global Redis config means a business Redis target is ready.
- [x] AK003 Make `dependency_check` tasks carry dependency targets from diagnosis/watch scope instead of relying on Agent registration-time config.
- [x] AK004 Make `redis_check` tasks carry one Redis target snapshot from diagnosis/watch scope and preserve it in the structured artifact.
- [x] AK005 Ensure industrial adapters output `collector_invocation` and per-target evidence refs so audit can prove which target was collected.
- [x] AK006 Add tests proving one Agent can schedule different dependency/Redis targets without target configuration leakage.

**Outcome**: Multiple watches, late-created tasks, and multiple Redis dependencies can share the same Agent-side industrial collectors without mixing target configuration.

### Task Group AL - Unified Watch Collector Invocation

**Purpose**: Make diagnosis-created tasks and watch-triggered tasks share one collector invocation contract, so Agent collectors never need to know whether a task came from manual diagnosis or persistent watch.

- [x] AL001 Add a shared `build_collector_invocation` helper used by diagnosis and persistent trigger paths.
- [x] AL002 Add `target_config` to watch subscriptions and watch leases.
- [x] AL003 Pass `WatchSubscription.target_config` into persistent trigger evaluation.
- [x] AL004 Write watch-scoped `collector_invocation` into triggered task options and incident collector task records.
- [x] AL005 Add API/runtime tests proving watch target config flows into triggered collector invocation.
- [x] AL006 Add frontend watch creation support for optional target config JSON.

**Outcome**: Both ordinary diagnosis and persistent watch produce task options shaped as `target_config -> collector_invocation -> Agent collector`, avoiding two parallel target-routing protocols.

### Task Group AT - Trace 复合采集与失败语义

**Purpose**: Upgrade `trace_endpoint_profile` from a perf alias into a composite evidence family while closing the industrial log adapter and permission failure gaps found by the real Redis case.

- [x] AT001 Unify `log_scan` target option resolution across `target_config.log_paths`, `target_config.source_paths`, invocation options, managed pipeline defaults, and target `container_id` Docker JSON logs.
- [x] AT002 Make `log_scan` emit `log_window_json` for readable empty windows and zero-error windows with `source_status`, `window_records`, `matched_records`, and `error_cluster_count`; reserve failure for unusable/corrupt inputs.
- [x] AT003 Extend `trace_endpoint_profile` target configuration with `stack_source`, `trace_source`, `trace_paths`, `service_id`, `instance_id`, `endpoint`, and evidence window metadata.
- [x] AT004 Define the `trace_endpoint_profile_json` contract with stack source, Trace source, endpoint bindings, call-path hotspots, correlation status, blocked reason, and maximum supported level.
- [x] AT005 Add deterministic OTel NDJSON and SkyWalking JSON/NDJSON input normalization without sending raw Trace payloads to the LLM.
- [x] AT006 Add eBPF profile capability detection while preserving perf as the fallback stack source; distinguish unavailable tool, permission denied, and target exit.
- [x] AT007 Emit structured permission/unavailable Trace artifacts instead of dropping all evidence when stack sampling is blocked.
- [x] AT008 Add focused tests for log source precedence, Docker log fallback, empty log windows, Trace source normalization, and permission-blocked Trace results.

**Outcome**: Industrial log collection no longer fails on a valid empty window, and Trace tasks always produce an honest structured result describing the evidence actually available.

### Task Group AM - 栈热点与 Trace/Span 关联

**Purpose**: Correlate stack samples with OTel/SkyWalking spans and produce stable endpoint/call-path evidence.

- [x] AM001 Implement explicit Trace/Span correlation using trace/span context, PID, process identity, and target context.
- [x] AM002 Implement PID/instance/service plus time-window overlap correlation with deterministic tie-breaking.
- [x] AM003 Implement low-confidence service/time overlap as a candidate only; do not upgrade it to a confirmed call path.
- [x] AM004 Generate stable `endpoint_bindings` and `call_path_hotspots` with `correlation_method`, `confidence`, and `evidence_ref`.
- [x] AM005 Preserve function-level stack evidence when Trace is missing or unmatched and set `max_supported_level` accordingly.
- [x] AM006 Add repeated-run stability tests and cross-window conflict tests.

**Outcome**: The same stack/Trace evidence repeatedly resolves to the same endpoint and call path, while insufficient correlation remains explicitly bounded.

### Task Group AN - 服务端、AI 树与前端接入

**Purpose**: Make the composite Trace artifact usable by structured evidence, RCA, AI-tree follow-up, audit bundles, and the diagnosis UI.

- [x] AN001 Normalize `trace_endpoint_profile_json` in `evidence_structurer.py` into compact Trace source, endpoint binding, and call-path sections.
- [x] AN002 Add Trace correlation state and supported localization level to `confidence_inputs`, `evidence_index`, and audit bundle output.
- [x] AN003 Update RCA localization and `next_evidence_requests` to distinguish missing stack source, missing Trace source, and unmatched Trace context.
- [x] AN004 Show Trace source, endpoint bindings, call-path hotspots, correlation method, confidence, and missing evidence in the frontend diagnosis report.
- [x] AN005 Update readiness gate so a Trace task is not considered fully ready from `perf.data` alone; require `trace_endpoint_profile_json`.
- [x] AN006 Add server/frontend tests for complete, partial, unmatched, and blocked Trace results.

**Outcome**: Reports name concrete functions/endpoints/call paths when evidence supports them and explain the exact missing layer otherwise.

### Task Group AO - Worker 权限、部署与真实验证

**Purpose**: Make the industrial stack collector configuration real in the deployed three-node environment and verify the whole diagnosis chain against the Redis case.

- [x] AO001 Add Worker trace source environment variables and read-only trace export volume configuration.
- [x] AO002 Add Agent CollectorProfile entries for `trace_endpoint_profile`, eBPF profile, Trace source status, and perf permission status.
- [x] AO003 Add a deployment/runtime check for `privileged`, `pid: host`, `PERFMON`, `SYS_PTRACE`, `SYS_ADMIN`, `BPF`, seccomp, and `kernel.perf_event_paranoid`.
- [x] AO004 Keep target-scoped invocation separate from Agent-global capability discovery so multiple targets cannot share the wrong Trace paths or Redis target.
- [x] AO005 Run local focused and full tests; preserve existing unrelated worktree changes.
- [x] AO006 Rebuild only required Worker/Control services and run `OB-SINGLE-REDIS-001`.
- [x] AO007 Verify `process_log_scan` and `process_trace_endpoint_profile` artifacts, readiness gate, AI-tree localization level, `next_evidence_requests`, and saved report output; retain the permission-blocked stack boundary.

**Outcome**: The deployed system can distinguish real collection success, valid empty evidence, missing Trace source, permission blocking, and successful endpoint/call-path correlation.

### Task Group AP - 方案 B：日志与 Trace 失败语义收口

**Purpose**: Close the two non-runtime gaps found by the real Redis case without losing structured evidence.

- [x] AP001 Distinguish `readable`, `empty_window`, `no_error`, `source_missing`, and `corrupt_input` in `log_window_json`.
- [x] AP002 Ensure empty and no-error log windows return a structured artifact and remain usable by Evidence Structurer.
- [x] AP003 Add Trace/perf/eBPF capability preflight fields for tools, `perf_event_paranoid`, capabilities, PID namespace, and seccomp/privileged state.
- [x] AP004 Emit structured permission and unavailable details with `repair_action`, `missing_capabilities`, `missing_tools`, and `max_supported_level`.
- [x] AP005 Add focused tests for empty log windows, no-error windows, permission blocking, target exit, and missing tools.

**Outcome**: Valid empty evidence is not mistaken for collector failure, and blocked stack collection remains actionable and auditable.

### Task Group AQ - Off-CPU Industrial Collector v2

**Purpose**: Upgrade Off-CPU from a single bpftrace script into a layered industrial evidence pipeline.

- [x] AQ001 Define the Off-CPU v2 artifact contract for event, cause, stack, correlation, capability, and evidence-window sections.
- [x] AQ002 Add event-layer collection for `sched_switch` and `sched_wakeup` with TID/PID, CPU, state, start/end timestamps, and wait duration.
- [x] AQ003 Add cause-layer adapters for futex/lock, block I/O, TCP/socket, syscall, and scheduler-delay signals.
- [x] AQ004 Preserve user and kernel stack quality separately, including `events_without_user_stack` and `stack_unwind_status`.
- [x] AQ005 Classify results as `completed`, `empty_window`, `partial`, `blocked`, or `target_exit`; never equate empty stack with no wait event.
- [x] AQ006 Add deterministic function/endpoint/Trace/call-path correlation when target context is available.
- [x] AQ007 Keep raw event output as references-only artifacts and expose compact summaries to the AI tree.
- [x] AQ008 Add unit tests for event-only, cause-only, stackless, permission-blocked, target-exit, and fully correlated results.
- [x] AQ009 Run a real Linux Off-CPU smoke test and record whether the target produced non-empty wait evidence; the deployed Redis case captured wait events but remained stackless because of host permissions.

**Outcome**: Off-CPU evidence can explain whether a target was waiting, why it was waiting, where it waited, and how complete that evidence is.

### Task Group AR - 180s 诊断预算与 Follow-up 保留

**Purpose**: Increase the default collection budget while protecting AI-tree follow-up probes from front-loaded exhaustion.

- [x] AR001 Change the default `max_total_probe_cpu_seconds` from `120` to `180`.
- [x] AR002 Define a follow-up reserve and expose initial/follow-up budget phase in session usage.
- [x] AR003 Prevent initial probes from consuming the reserved follow-up quota.
- [x] AR004 Allow approved or `all_registered` follow-up probes to consume the reserved quota with normal capability and risk checks.
- [x] AR005 Record budget block details including used, limit, reserved, requested, phase, and next action.
- [x] AR006 Add tests proving the initial phase cannot exhaust follow-up reserve and the follow-up phase can use it.
- [x] AR007 Re-run `OB-SINGLE-REDIS-001` with the 180s budget and verify follow-up probe execution.

**Outcome**: More evidence can be collected without losing the AI tree's ability to continue depth exploration.

### Task Group AS - 方案 B 集成验证

**Purpose**: Verify the complete chain after AP-AQ-AR changes.

- [x] AS001 Run focused collector, orchestrator, evidence structurer, audit bundle, and readiness gate tests.
- [x] AS002 Run the real Redis case and inspect all structured artifacts, not just terminal status.
- [x] AS003 Confirm the report distinguishes Redis service attribution from missing Off-CPU/Trace function evidence.
- [x] AS004 Save the timestamped report and update the progress record with real limitations.

**Outcome**: The four real-case failures are either fixed or represented with precise, actionable evidence boundaries.

### Task Group AU - WatchRuntime 持续监视闭环

**Purpose**: Make Persistent Watch operational in the deployed scheme B environment. Agent registration only answers "what this worker can collect"; WatchRuntime leases answer "which concrete target this worker should observe now".

- [x] AU001 Add `watch.proto` with `WatchRuntime.Sync`, `WatchLease`, `WatchObservation`, and structured metric samples.
- [x] AU002 Register WatchRuntime gRPC service on the server and keep it protected by the existing gRPC auth interceptor.
- [x] AU003 Convert Agent observations into existing `MetricWindow` objects and reuse `PersistentAgentRuntime.evaluate()`.
- [x] AU004 Add Agent-side independent Watch sync loop so ordinary collector tasks do not block watch observation.
- [x] AU005 Support multiple simultaneous watch leases with per-watch PID/window isolation.
- [x] AU006 Suppress duplicate incidents for one continuously active trigger while preserving manual multi-incident history.
- [x] AU007 Add focused tests for WatchRuntime service evaluation, target-exit ignore behavior, and repeated-trigger suppression.
- [x] AU008 Deploy the WatchRuntime-capable Control/Worker containers to all three VMs.
- [x] AU009 Test frontend watch creation, lease pickup, triggered incident creation, frozen snapshot, and watch-scoped collector tasks.

**Outcome**: Persistent Watch can capture suspicious windows before manual diagnosis, while still relying on the AI tree and structured evidence layer for attribution.

### Task Group AW - 方案 B 真实验收工具

**Purpose**: Keep remote deployment, Redis diagnosis, and Persistent Watch validation reproducible without mixing their failure signals.

- [x] AW001 Make the scheme B deployment script resolve `/home/<user>/mini-drop-active` before `/home/<user>/mini-drop`.
- [x] AW002 Normalize `proto/compile.sh` line endings and executable permissions after SFTP upload.
- [x] AW003 Make the AI Ops v2 runner use the same resolved remote repository root for fault injection and collector overrides.
- [x] AW004 Add a standalone Persistent Watch VM smoke script covering process selection, watch lease, trigger evaluation, frozen incident, and optional AI-tree analysis.
- [x] AW005 Execute the deployment script against all three VMs and rebuild the required services.
- [x] AW006 Execute `OB-SINGLE-REDIS-001` with the 180s budget and save the timestamped full report.
- [x] AW007 Execute the Persistent Watch smoke test and verify frontend-visible incident/snapshot/collector-task data.

**Outcome**: Scheme B has separate, repeatable commands for deployment, ordinary diagnosis, and persistent monitoring; Kubernetes remains outside this acceptance group.

### Task Group AV - Kubernetes 后续迁移路线

**Purpose**: Keep Kubernetes migration explicit without mixing it into the current Docker VM scheme B completion claim.

- [x] AV001 Document the current Docker VM scheme B boundary and declare Kubernetes out of current completion scope.
- [x] AV002 Document the future `Environment Backend -> Kubernetes Backend` migration path.
- [x] AV003 Document OTel Collector DaemonSet, industrial profile producer / SkyWalking Rover DaemonSet, CRI/containerd PID resolver, and CNI-aware dependency probing as follow-up upgrades.
- [x] AV004 Require same Case/Oracle dual-environment evaluation before Kubernetes migration is considered complete.

**Outcome**: Scheme B can be completed and tested now, while Kubernetes remains a clear future backend rather than an implied partial implementation.

### Task Group AX - 方案 B 分析终态与前端发布闭环

**Purpose**: Close the real-case gap where Watch collection completed but automatic AI analysis exceeded the test window or the result was not visible in the deployed frontend.

- [x] AX001 Add `analysis_failed` as a persisted terminal status for Watch incidents, preserving error type, retryability, elapsed budget, and frozen evidence refs.
- [x] AX002 Add per-analysis attempt identity and prevent late or duplicate background results from overwriting a newer terminal state.
- [x] AX003 Bound Watch automatic analysis to `150s` by default and bound each LLM request to `45s` by default, both configurable through Control environment variables.
- [x] AX004 Update the VM smoke runner to accept `analysis_failed` as a completed terminal observation instead of reporting a false timeout.
- [x] AX005 Show analysis failure status and retry message in the Persistent Watch frontend without hiding the frozen snapshot or structured evidence.
- [x] AX006 Include `web/src/pages/PersistentWatch.jsx` and the Control analysis timeout configuration in scheme B deployment synchronization.
- [x] AX007 Add API, collector, RCA, Python compile, and frontend build validation for the terminal-state contract.

**Outcome**: Watch incidents cannot remain indefinitely in `analyzing`; successful, evidence-insufficient, and failed analysis all have explicit persisted states, and the deployed frontend displays the same state as the API.

### Task Group AY - 完整版受控 AI 树核心

**Purpose**: Replace the flat legacy tree view with a controlled layered candidate convergence tree while keeping the legacy `ai_tree` follow-up contract compatible.

- [x] AY001 Add controlled AI tree models for layers, candidates, self-challenge, probe edges, budget snapshots, and final stop boundary.
- [x] AY002 Generate `controlled_ai_tree` from structured facts, candidates, localizations, evidence gaps, conflicts, and legacy next-evidence requests.
- [x] AY003 Keep old `ai_tree.next_evidence_requests` as the compatibility source for existing follow-up scheduling.
- [x] AY004 Add tests for layered candidates, self-challenge fields, rejected/unknown causes, and stable output across repeated runs.

**Outcome**: AI tree output represents coarse-to-fine evidence convergence instead of a few static root/leaf records.

### Task Group AZ - 源码上下文与行级定位契约

**Purpose**: Allow precise line localization only when source context is provided and evidence supports it.

- [x] AZ001 Add `source_context` to diagnosis context and service instance API models.
- [x] AZ002 Include source context in target scope, collector invocation, trace/off-CPU options, and AI tree source-context hash.
- [x] AZ003 Prevent line-level localization from being promoted when source context is absent.
- [x] AZ004 Add tests proving line candidates require source context and function/call-path diagnosis still works without it.

**Outcome**: Source code enables line-level diagnosis, but missing source never causes hallucinated file/line output.

### Task Group BA - 探针请求 Fingerprint 与复用

**Purpose**: Avoid repeated collection for the same target/window/options/source context, including stable blocked results.

- [x] BA001 Add deterministic collector request fingerprint generation to the shared collector invocation contract.
- [x] BA002 Store fingerprint in probe parameters and task options for initial and follow-up probes.
- [x] BA003 Reuse existing completed or stable-blocked probe/task results before creating a duplicate follow-up task.
- [x] BA004 Add tests for reuse hit, reuse miss, source-context mismatch, and permission-blocked result reuse.

**Outcome**: AI tree can ask for evidence without wasting budget on identical probes.

### Task Group BB - 默认少交互采集

**Purpose**: Make ordinary diagnosis automatically execute all registered collectors while keeping hard safety boundaries.

- [x] BB001 Change backend default `auto_execute_policy` to `all_registered`.
- [x] BB002 Remove R2 probe count as a blocking gate for `all_registered`; keep total duration, parallelism, registration, and target scope checks.
- [x] BB003 Keep arbitrary shell, sysctl changes, service mutations, and remediation actions as human-only suggestions.
- [x] BB004 Add tests proving default diagnoses schedule registered R2 probes without approval and still block unsupported or over-budget probes.

**Outcome**: Developer and controlled environments avoid approval dead-ends while unsafe actions remain manual.

### Task Group BC - 默认深采集 Worker 部署

**Purpose**: Make deployed Workers default to the permissions required by industrial stack/profile collection.

- [x] BC001 Remove `no-new-privileges:true` from Worker deployment and keep `seccomp:unconfined`.
- [x] BC002 Add host proc/sys/source mounts needed for profiler capability checks and source/symbol lookup.
- [x] BC003 Extend CollectorProfile with effective permission state and manual host sysctl warning fields.
- [x] BC004 Add tests or compile validation for CollectorProfile output shape.

**Outcome**: Container deployment no longer blocks deep collection by default, while host sysctl remains a visible manual boundary.

### Task Group BD - 前端受控 AI 树与自动采集状态

**Purpose**: Show the controlled AI tree and reduce routine interaction in the diagnosis UI.

- [x] BD001 Remove normal diagnosis execution-policy selection and stop sending `auto_execute_policy` from the create form.
- [x] BD002 Replace probe approval UI with automatic collection status and human-action-only notices.
- [x] BD003 Add optional source context inputs to the diagnosis creation form.
- [x] BD004 Add controlled AI tree visualization with layer cards, candidate status, evidence refs, probe edges, reuse state, and self-challenge detail.
- [x] BD005 Keep true natural-language readability rewrite as a documented follow-up, not a fake structural-only change.

**Outcome**: Users see how the AI tree narrows candidates and only act when the system genuinely cannot proceed automatically.

### Task Group BE - LLM 受控树生成与工具清单下探

**Purpose**: Make AI generate controlled tree node conclusions and choose follow-up probes from a bounded registry manifest instead of only decorating Analyzer-generated nodes.

- [x] BE001 Add `Probe Registry Manifest` serialization from registered probe definitions, including `probe_id`, `evidence_family`, purpose, answerable questions, target requirements, risk level, and output contract.
- [x] BE002 Add deterministic `evidence_family -> probe_id` mapping and reuse it in diagnosis follow-up scheduling.
- [x] BE003 Add LLM controlled-tree generation with Analyzer fallback, repair retry, and hard validation.
- [x] BE004 Allow LLM to rerank candidate roles and choose manifest probe requests while preventing new candidate IDs, fake evidence refs, unregistered probes, and localization-level upgrades.
- [x] BE005 Inject the probe manifest into RCA report analysis payloads and AI diagnosis-session analysis.
- [x] BE006 Add tests proving valid manifest probe selection is accepted and unregistered tool requests are rejected.
- [x] BE007 Add diagnosis-orchestrator test proving AI-selected evidence families create registered follow-up probe tasks.

**Outcome**: The complete controlled AI tree now lets AI produce node conclusions and select down-probing tools, while engineering controls execution boundaries and fallback behavior.

### Task Group BF - 候选回溯与结论资格门禁

**Purpose**: Prevent evidence details from being presented as root causes, preserve rejected hypotheses, and continue investigation through explicit backtracking.

- [x] BF001 Extend controlled-tree candidates with claim type, causal status, decision, primitive classification, and conclusion eligibility fields.
- [x] BF002 Make full and compact LLM reviews produce falsifiable mechanism claims, reject primitive-only roots, and request or reuse evidence before promotion.
- [x] BF003 Add deterministic conclusion eligibility validation and exclude rejected, contradicted, observation-only, or ineligible candidates from final causes.
- [x] BF004 Preserve rejected candidates in the session tree and add explicit rollback edges that continue to the next viable candidate.
- [x] BF005 Render rollback edges and rejected/contradicted nodes in the frontend while keeping hover and evidence details available.
- [x] BF006 Add backend tests and frontend build validation for primitive-only evidence, candidate rejection, backtracking, alternate-candidate continuation, and abstention.

**Outcome**: A failed hypothesis stays visible as a grey branch, the investigation returns to the nearest viable parent and evaluates another candidate, and only eligible mechanism claims can become final conclusions.
