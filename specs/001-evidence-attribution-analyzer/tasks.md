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

- The original T001-T014 MVP excludes collector, task, API, and front-end changes; later named task groups explicitly extend that scope without changing the completed MVP tasks.
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

### Task Group BQ - 工业 Python Heap、源码定位与逐轮 AI 裁决

**Purpose**: Close the Werkzeug #1521 real-case gaps with industrial producers and evidence-safe AI investigation rounds.

- [ ] BQ001 Document the Memray, py-spy raw, Git/tree-sitter source, evidence validity, and Werkzeug Oracle boundaries in plan/spec/tasks.
- [ ] BQ002 Add regression tests rejecting baseline percentages outside `0-100`, invalid sample counts, bare addresses, and `[unknown]` function anchors.
- [ ] BQ003 Switch py-spy structured output to official raw/collapsed stacks and preserve function, file, line, call path, samples, and percent; keep SVG optional and presentation-only.
- [ ] BQ004 Preserve consistent source context hash from diagnosis target scope through child trees and the session controlled tree; represent revision conflicts explicitly.
- [ ] BQ005 Add a `python_heap_profile` Probe Registry entry and Agent collector adapter backed only by Memray attach or instrumented official output.
- [ ] BQ006 Emit structured Memray capability, allocation hotspots, retained allocation hotspots, call paths, line candidates, raw refs, and evidence validity without hand-written GC object scanning.
- [ ] BQ007 Add a bounded `source_snapshot` Producer using Git revision verification and tree-sitter/universal-ctags symbol context; reject paths outside configured roots.
- [ ] BQ008 Make memory-leak investigation request `python_heap_profile`, `python_runtime_profile`, and `source_snapshot` before off-CPU/Trace unless independent wait/latency evidence exists.
- [ ] BQ009 Run session-level controlled AI adjudication before each follow-up round, count only actual model calls, allow evidence-guarded mechanism proposals, and retain explicit fallback/partial status when AI does not run.
- [ ] BQ010 Add focused Agent, evidence structurer, probe registry, orchestrator, source-context, AI guard, and audit-bundle tests.
- [ ] BQ011 Rebuild the Worker image and verify Memray/py-spy/Git/tree-sitter capability reporting without changing host sysctl.
- [ ] BQ012 Re-run Werkzeug #1521 with the unchanged vulnerable Oracle and require valid line evidence, correct probe selection, non-fallback AI review, and an evidence-bounded mechanism conclusion.

**Outcome**: Python memory cases use mature collection tools, invalid profiles cannot poison conclusions, source lines survive structuring, and the AI tree chooses and evaluates the evidence needed for a concrete mechanism instead of spending follow-up budget on unrelated probes.

### Task Group BG - 证据有效性状态契约

**Purpose**: Separate task completion, artifact production, and usable evidence so empty or broken deep probes cannot create false confidence.

- [ ] BG001 Add explicit execution, artifact, and evidence validity fields to structured collector results and audit bundles.
- [ ] BG002 Define minimum valid evidence checks for off-CPU, Trace, baseline/perf, py-spy, and log scan families.
- [ ] BG003 Remove zero-count default `top_cause` values and prevent empty Trace from advertising function-level support.
- [ ] BG004 Mark fully unparseable baseline windows and stopped-target py-spy results with precise non-valid evidence states.
- [ ] BG005 Make follow-up completion, readiness gate, and benchmark scoring require valid structured evidence instead of collector-name presence.
- [ ] BG006 Add regression tests for completed-empty, completed-unparseable, blocked, partial, and valid evidence results.

**Outcome**: A generated file remains auditable, but only evidence meeting its family quality contract can advance the AI tree or score as collected evidence.

### Task Group BH - 有界日志窗口与 systemd 回连

**Purpose**: Prevent large log files from timing out and make host/systemd targets discoverable without manual log-path configuration.

- [ ] BH001 Replace full-file `read_text()` parsing with bounded streaming/tail-window NDJSON processing.
- [ ] BH002 Add Fluent Bit systemd/journald input with PID, unit, timestamp, service, instance, and container metadata preservation.
- [ ] BH003 Resolve log sources from target PID, systemd unit, container ID, and managed pipeline output without mixing targets.
- [ ] BH004 Return structured `source_missing`, `empty_window`, `no_error`, `partial`, or valid log-cluster evidence within the probe deadline.
- [ ] BH005 Add large-file memory/time tests and systemd transient-unit correlation tests.

**Outcome**: Log collection remains bounded on growing industrial log output and can retrieve the target's same-window records for both containers and systemd services.

### Task Group BI - 3A 持久化信号控制溯源

**Purpose**: Capture control actions before task-driven diagnosis begins and preserve who sent which signal to the affected process.

- [ ] BI001 Define `runtime_control_event_json` and normalized actor/action/target/effect/evidence reference fields.
- [ ] BI002 Add a low-cost persistent eBPF signal observer for `signal_generate` with bounded Rolling Buffer storage.
- [ ] BI003 Filter and redact events before persistence, retaining only target-scope diagnostic fields.
- [ ] BI004 Freeze same-window events by PID and timestamp when a Watch incident or diagnosis requests runtime control history.
- [ ] BI005 Add the registered `runtime_control_history` evidence family and collector invocation/fingerprint contract.
- [ ] BI006 Convert signal events and process-state observations into control causality graph edges.
- [ ] BI007 Add Linux integration tests for running, `SIGSTOP`, `SIGCONT`, missing-history, buffer expiry, and multiple watched targets.

**Outcome**: Runtime-stall diagnosis can identify the signal sender and action when captured, while preserving an explicit unknown-actor boundary when history is unavailable.

### Task Group BJ - AI 树运行控制结论资格

**Purpose**: Prevent a stopped-state observation from being presented as a complete root cause and route follow-up to control provenance instead of useless profilers.

- [ ] BJ001 Add observation, direct-failure-mechanism, and complete-root-cause qualification levels.
- [ ] BJ002 Keep `/proc` `T/t` evidence at `process_suspended` direct mechanism unless a valid same-window control event proves the actor/action chain.
- [ ] BJ003 Skip py-spy, CPU, off-CPU, and endpoint profiling for an already group-stopped target and request runtime control history plus bounded logs.
- [ ] BJ004 Make top-level confidence, abstention, candidates, and controlled-tree final causes derive from one eligibility result.
- [ ] BJ005 Preserve rejected CPU, lock, dependency, and code candidates as grey branches with explicit control-state counterevidence and rollback edges.
- [ ] BJ006 Add tests preventing high-confidence conclusions when the tree has no eligible final primary cause.

**Outcome**: Reports state exactly whether the system found an observation, a direct failure mechanism, or the complete control-action root cause.

### Task Group BK - Runtime Stall 真实验收

**Purpose**: Re-run the real VM case against evidence-quality and control-provenance gates rather than the previous label-only score.

- [ ] BK001 Deploy BG-BJ changes to Control, Worker1, and Worker2 without overwriting unrelated VM configuration.
- [ ] BK002 Run a direct Linux `SIGSTOP/SIGCONT` collector smoke test and inspect the frozen runtime control artifact.
- [ ] BK003 Run `OB-SINGLE-RUNTIME-STALL-001` and save a timestamped audit bundle, readiness result, and score report.
- [ ] BK004 Verify log scan returns within deadline and py-spy/off-CPU/Trace/baseline empty results are not counted as valid deep evidence.
- [ ] BK005 Verify the report identifies signal actor/action/target when captured, otherwise explicitly stops at unknown-actor direct mechanism.
- [ ] BK006 Compare previous `96/100` with corrected scoring and document why invalid evidence no longer receives collector coverage credit.

**Outcome**: The runtime-stall case is accepted only when its conclusion semantics and evidence provenance are correct, not merely because four broad Oracle labels match.

### Task Group BL - 3B/3C 运行控制溯源

**Purpose**: Implement system, runtime, resource-control, deployment, and Kubernetes audit producers under the same runtime-control contract.

- [ ] BL001 3B: Add systemd control event producer and unit lifecycle correlation.
- [ ] BL002 3B: Add Docker/containerd event producer and pause/kill/restart/OOM correlation.
- [ ] BL003 3B: Add cgroup freezer, limit-change, and process-migration event producer.
- [ ] BL004 3C: Add release, configuration, image, and scaling change events.
- [ ] BL005 3C: Add Kubernetes Audit and workload-controller identity correlation after Kubernetes backend migration.
- [ ] BL006 Add source capability/status reporting and prevent unavailable producers from emitting synthetic events.
- [ ] BL007 Add Linux VM integration tests for systemd, Docker/containerd, cgroup, and release-change sources.
- [ ] BL008 Add Kubernetes Audit Event fixture integration tests and record that real-cluster acceptance remains environment-blocked.

**Outcome**: All 3A/3B/3C producers share one evidence and graph contract; VM-capable sources are genuinely tested and Kubernetes Audit is fixture-validated without overstating environment coverage.

### Task Group BM - 运行控制根因等级修正

**Purpose**: Separate a confirmed direct control cause from the still-unknown source that initiated it.

This group supersedes the old qualification wording in BJ001-BJ002; remaining implementation must use the four levels defined here.

- [x] BM001 Extend controlled-tree and conclusion schemas with `direct_root_cause` and `complete_source_root_cause`, retaining old `complete_root_cause` as read compatibility only.
- [x] BM002 Tighten runtime-control qualification with exact target matching, action/effect semantics, event ordering, and observed stopped-state correlation.
- [x] BM003 Add optional source-provenance fields for parent process, redacted command source, systemd/cgroup identity, release event, and audit actor.
- [x] BM004 Downgrade `bash -> SIGSTOP -> stopped` from complete root cause to direct root cause and expose the unresolved source boundary.
- [x] BM005 Add regression tests for observation, direct mechanism, direct root cause, complete source root cause, target mismatch, and invalid event ordering.

**Outcome**: A confirmed direct cause remains actionable without being mislabeled as a complete source explanation.

### Task Group BN - 会话级 AI 裁决真实性

**Purpose**: Make the final session conclusion genuinely AI-adjudicated and make fallback status observable.

- [x] BN001 Add persisted `ai_review_status`, `ai_review_scope`, model, attempt count, and sanitized failure reason to the diagnosis conclusion/audit contract.
- [x] BN002 Stop marking Analyzer fallback layers as `ai_guarded` after full or compact guard failure.
- [x] BN003 Build one compact session-level AI review after all currently valid evidence has returned; child tasks remain evidence producers rather than independent final-report generators.
- [x] BN004 Validate session AI output against candidate IDs, evidence refs, probe manifest, supported level, cause qualification, and token/time budget.
- [x] BN005 Make readiness require a successful session-level review instead of the presence of an `ai_guarded` label.
- [x] BN006 Add tests for valid review, malformed response, rejected evidence refs, retry exhaustion, truthful fallback, and readiness failure.

**Outcome**: `ai_guarded` means the session-level AI actually returned a valid controlled adjudication.

### Task Group BO - 多根因簇与复合事故裁决

**Purpose**: Represent multiple simultaneously valid causes without treating an ordinary secondary-candidate list as a compound diagnosis.

- [x] BO001 Add `RootCauseCluster` schemas with role, cause level, mechanism, target, explained symptoms, causal chain, relation to primary, evidence refs, confidence, and residual unknowns.
- [x] BO002 Group candidates by mechanism, target, evidence cohort, and propagation path while deduplicating semantically equivalent candidates and shared evidence.
- [x] BO003 Run conclusion eligibility independently for each cluster and exclude rejected, unknown, observation-only, and ineligible nodes.
- [x] BO004 Let the bounded session AI select one primary cluster and classify additional eligible clusters as contributing or independent.
- [x] BO005 Emit `compound_incident` only when at least two independent eligible clusters remain; otherwise keep a single-cause classification.
- [x] BO006 Preserve cluster-to-tree node links so rejected grey branches and rollback history remain visible.
- [x] BO007 Add deterministic tests for one cause, two independent causes, one contributing cause, duplicate candidates, shared evidence, and conflicting clusters.

**Outcome**: Multi-root output explains what each cause affected and how it relates to the primary cause.

### Task Group BP - 人话解释与按簇建议

**Purpose**: Turn validated causality into an understandable explanation rather than repeating evidence summaries.

- [x] BP001 Add `headline`, `why_it_happened`, ordered `causal_chain`, `ruled_out_summary`, `residual_unknowns`, and cluster-scoped recommendations to the conclusion contract.
- [x] BP002 Require the session AI to explain mechanism-to-symptom links and cite real evidence for every causal step.
- [x] BP003 Prevent one text value from populating summary, node claim, and `why_this_claim`; preserve deterministic fallback text with explicit non-AI status.
- [x] BP004 Generate investigation, temporary mitigation, and permanent-fix suggestions per eligible cluster without automatically executing remediation.
- [x] BP005 Add report validation tests for concrete mechanism language, unsupported claims, duplicate text, missing evidence refs, and generic recommendation rejection.

**Outcome**: The report answers what happened, why it caused the symptom, what remains unknown, and what to do next.

### Task Group BQ - 前端根因簇和 AI 状态展示

**Purpose**: Present the final explanation and multiple causes without hiding the controlled tree investigation history.

- [x] BQ001 Update the diagnosis conclusion card to show headline, mechanism explanation, cause level, residual unknowns, and AI review status.
- [x] BQ002 Add expandable root-cause cluster sections for primary, contributing, and independent causes with causal steps, evidence, and recommendations.
- [x] BQ003 Keep the controlled AI tree as the investigation view, with rejected nodes grey and links from final clusters to their source tree nodes.
- [x] BQ004 Show Analyzer fallback or failed AI review explicitly instead of presenting it as an AI-generated conclusion.
- [x] BQ005 Add frontend component tests where available and run the production build at desktop and mobile widths.

**Outcome**: Users can read the answer first, inspect each cause second, and audit the full tree when needed.

### Task Group BR - 门禁、评分与真实 Case 验收

**Purpose**: Prove the new semantics on one direct-control case and one real compound case.

- [x] BR001 Extend audit bundles and benchmark scoring with AI review status, root-cause clusters, cluster evidence independence, and symptom coverage.
- [x] BR002 Add a compound readiness check requiring at least two eligible evidence-backed clusters for `compound_incident`.
- [x] BR003 Run focused backend tests and the frontend production build.
- [ ] BR004 Deploy the approved changes to Control, Worker1, and Worker2 without deleting data volumes or changing host sysctl values.
- [ ] BR005 Re-run `OB-SINGLE-RUNTIME-STALL-001` and verify it stops at `direct_root_cause` with an explicit unknown source boundary.
- [ ] BR006 Run `OB-COMPOUND-NOISY-DOWNSTREAM-001` and verify `paymentservice paused` is primary while Worker2 CPU noise is a separately evidenced contributing cluster.
- [ ] BR007 Save timestamped audit bundles and a comparison report covering AI participation truthfulness, explanation quality, cluster count, evidence refs, score, and residual limitations.

**Outcome**: Acceptance depends on real session AI participation and evidence-backed causal explanations, not labels or collector presence.

### Suggested Execution Order For BM-BR

1. Complete BM first so every later explanation uses the correct cause-level semantics.
2. Complete BN before BO so multi-cause adjudication runs through a truthful session-level AI path.
3. Complete BO before BP so readable explanations are generated from validated clusters rather than raw candidates.
4. Complete BP and BQ together around one shared API contract.
5. Complete BR last, first validating Runtime Stall and then the compound noisy/downstream case.

### Task Group BS - Two-Hop 复合因果闭环

**Purpose**: Close the same-host CPU and downstream pause evidence chains without unbounded topology expansion or repeated model calls.

- [x] BS001 Set the diagnosis topology budget default to 2 hops and enforce bounded expansion, per-round probe limits, stop conditions, and delayed-followup boundaries.
- [x] BS002 Extend collector fingerprints with target scope, evidence window, source context, and effective parameters; reuse only completed artifacts with acceptable evidence validity.
- [x] BS003 Extend `sys_metrics` from one PID to a cgroup/process-tree workload scope and emit aggregate plus member-level CPU attribution.
- [x] BS004 Add target scheduling/throttling signals needed to distinguish contributing CPU contention from an unrelated same-host anomaly.
- [x] BS005 Backfill Docker paused/running state, cgroup freeze state, and process state into structured runtime-control evidence without fabricating historical actors.
- [x] BS006 Correlate dependency failure, runtime state, Trace/endpoint propagation, host saturation, and workload CPU into independently qualified root-cause clusters.
- [x] BS007 Make compound readiness independent of the predicted classification and align empty-window, collector coverage, and cluster-independence semantics with benchmark scoring.
- [x] BS008 Add deterministic tests for MainPID child CPU aggregation, cgroup membership changes, paused-state backfill, two-hop stopping, fingerprint reuse, contributing/independent/unknown CPU roles, and readiness/scorer agreement.
- [x] BS009 Run focused backend tests and the frontend production build; fix all deterministic failures before VM deployment.
- [x] BS010 Deploy once to Control, Worker1, and Worker2 without deleting volumes or changing host sysctl, then run one timestamped `OB-COMPOUND-NOISY-DOWNSTREAM-001` acceptance case.
- [x] BS011 Audit AI participation, two-hop behavior, probe reuse, primary/contributing clusters, evidence refs, readiness and score; only repeat once when the first result proves a concrete implementation defect.
- [x] BS012 Add the missing `scope` runtime-trace stage, strict compound multi-value scoring, downstream runtime location mapping, and one guard event per child task.
- [x] BS013 Count every session AI repair attempt against `max_model_calls`, include the previous rejected output in bounded retries, and stop with an explicit budget status.
- [x] BS014 Remove the static `perf_event_paranoid` pre-block, use the real `perf record` result, and include the sysctl value only in diagnostic context.
- [x] BS015 Add `perf.py` to the VM deployment contract and add a regression test for required runtime collector and session-AI files.
- [x] BS016 Normalize unambiguous single-string list fields in session AI JSON without changing claims, IDs, roles, or evidence refs; retain strict semantic validation.
- [x] BS017 Run the corrected real compound case, save the timestamped bundle, pass readiness, and score it with the private Oracle.

**Acceptance record**: `diag_session_20260819_175431_44599b8f` completed in `144.09s` with one successful session AI attempt, three `ai_guarded` layers, 8 child tasks, 24 evidence records, and 9 artifacts. Same-window runtime-control evidence proves `docker_daemon` paused downstream `paymentservice`; the upstream initiator remains unknown, so the primary cluster correctly stops at `direct_root_cause`. Worker2 noise-generator CPU is independently proven but not linked to checkoutservice scheduling pressure, so it remains `independent`. Readiness passed and the private Oracle scored the saved bundle `100/100`, with exact root match, compound cluster match, citation validity `1.0`, runtime trace coverage `1.0`, and zero unsafe actions.

**Outcome**: Bounded two-hop collection, same-window downstream task dispatch, causal separation of same-host CPU noise, truthful session AI participation, and direct pause-action provenance are implemented and verified. Only the actor above `docker_daemon` remains outside the accepted evidence boundary.

### Task Group BT - Persistent Watch Episode 与按影响自动诊断

**Purpose**: Preserve transient evidence while preventing a sustained anomaly or a normal heavy workload from repeatedly consuming collector and AI budgets.

- [x] BT001 Extend Watch persistence with the default-on auto-diagnosis switch and backward-compatible Episode fields.
- [x] BT002 Replace process-memory trigger suppression with persisted Watch Episode aggregation, occurrence/peak updates, recovery confirmation, and 120-second silence closure.
- [x] BT003 Emit all same-window anomaly signals, use median/MAD robust change detection, and collect process-state/cgroup-throttling context from the Agent.
- [x] BT004 Gate automatic diagnosis on hard-state or confirmed-impact evidence so an isolated CPU, memory, I/O, or thread shift is recorded without starting an AI tree.
- [x] BT005 Add an idempotent Episode diagnosis claim and reuse the existing WatchIncident-to-AI-cluster-diagnosis path.
- [x] BT006 Add one bounded anomaly-point explanation endpoint with strict output schema, no probe/root-cause authority, and persisted fingerprint cache.
- [x] BT007 Add the default-on frontend switch, Watch -> Episode -> anomaly-point view, lightweight analysis modal, and separate full AI diagnosis entry.
- [x] BT008 Add database migration compatibility, runtime/API/cache/recovery tests, and run the frontend production build.
- [x] BT009 Run the broader backend regression suite and inspect the final diff for interaction with the verified two-hop diagnosis path.

### Task Group BU - CodeQL 源码机制查询

- [x] BU001 Register `source_mechanism_query` as an optional R2 evidence family with source root, revision and line-anchor prerequisites.
- [x] BU002 Add a bounded CodeQL collector that verifies Git revision, reuses a revision/query-version database cache and runs either a managed query suite or a twice-validated AI-generated path query.
- [x] BU003 Normalize CodeQL SARIF/code-flow output into `source_mechanism_json` with bounded nodes, typed edges, candidate relations and stable evidence refs.
- [x] BU004 Preserve raw temporary QL, SARIF and database references while preventing source code, arbitrary query paths and unbounded code-flow payloads from reaching the model.
- [x] BU005 Report CodeQL availability, query-pack version, cache root and blocked reasons in CollectorProfile.
- [x] BU006 Add collector, revision mismatch, cache reuse, SARIF normalization, path-boundary and probe-registry tests.

### Task Group BV - PyHeap 运行时引用链验证

- [x] BV001 Register `python_heap_reference` as an optional R2 evidence family that is excluded from initial probes and Persistent Watch default collection.
- [x] BV002 Add a PyHeap collector that accepts a managed existing dump or performs a bounded GDB attach against the selected CPython PID.
- [x] BV003 Build an offline inbound-reference index and emit bounded retained objects and shortest reference paths as `python_heap_reference_json`.
- [x] BV004 Keep the raw heap dump outside the LLM payload and enforce object/path/depth/size limits plus structured permission and compatibility failures.
- [x] BV005 Report GDB, PyHeap analyzer, CPython compatibility and effective ptrace capability in CollectorProfile.
- [x] BV006 Add dump-ingestion, reference-path bounding, blocked capability, task routing and evidence-structure tests.

### Task Group BW - 按需机制分支与 Werkzeug 验收

- [x] BW001 Downgrade hand-written `source_snapshot.reference_paths` to partial localization so it cannot independently qualify a root cause.
- [x] BW002 Request `source_mechanism_query` only after a revision-matched line anchor exists and the mechanism-to-symptom causal chain remains unresolved.
- [x] BW003 Request `python_heap_reference` only for unresolved or conflicting Python retention candidates when the optional capability and budget are available.
- [x] BW004 Feed bounded source and heap mechanism evidence into the session AI review, require real evidence refs and retain rejected alternatives as grey rollback branches.
- [x] BW005 Extend artifact structuring, audit summaries, probe fingerprints and task routing for both new evidence families.
- [x] BW006 Add regression tests proving ordinary line diagnoses do not enter the branch, identical revision queries reuse cache, and blocked PyHeap does not fail the session.
- [x] BW007 Validate the Werkzeug fixture with a retained surface node, a rejected `defaults` branch and an evidence-bounded bound-method/code-constant mechanism branch.

**Outcome**: Persistent Watch freezes the first abnormal window, aggregates repeated signals into one durable Episode, starts at most one eligible AI diagnosis, and lets users understand individual anomaly points without invoking the controlled tree.

### Task Group BY - 父结论继承式回退与分层结论展示

**Purpose**: Keep the strongest evidence-backed parent conclusion visible when deep investigation is blocked, partial, inconclusive, or only disproves a child hypothesis; expose qualification boundaries separately from the current diagnosis.

- [x] BY001 Extend the conclusion contract in `server/app/rca/models.py` with retained conclusion, formal root cause, qualification boundary, and active retained candidate fields while preserving legacy JSON reads.
- [x] BY002 Add deterministic retained-conclusion and boundary builders in `server/app/diagnosis/session_conclusion.py` that select an existing candidate claim and never synthesize a generic fallback claim.
- [x] BY003 Update fallback explanation assembly in `server/app/diagnosis/session_conclusion.py` to inherit the origin parent claim, preserve level/evidence/confidence, and expose AI fallback only as metadata.
- [x] BY004 Carry retained conclusion and qualification boundary through `_analyze_tasks` and persisted conclusion versions in `server/app/diagnosis/orchestrator.py`.
- [x] BY005 Ensure deep-probe blocked/partial/inconclusive/empty-window/target-exit outcomes add boundary evidence without changing the active retained candidate or formal-root-cause eligibility in `server/app/diagnosis/orchestrator.py`.
- [x] BY006 Ensure child contradicted/rejected status does not invalidate its parent, while direct parent contradiction follows `origin_parent_candidate_id` in `server/app/diagnosis/orchestrator.py`.
- [x] BY007 Make active retained candidate selection exclude observation, mechanism, call-path context, and stop-boundary nodes in `server/app/diagnosis/orchestrator.py`.
- [x] BY008 Update `web/src/pages/AIDiagnosis.jsx` to display retained claim and supported level first, with formal root cause and qualification boundary as separate fields.
- [x] BY009 Update `web/src/components/diagnosis/ControlledAITreeGraph.jsx` and `web/src/components/diagnosis/aiTreeGraphModel.js` so only contradicted/rejected nodes are grey and boundary/observation/mechanism nodes remain explanatory.
- [x] BY010 Add backend regression tests for inherited claims, boundary-only failures, child-vs-parent contradiction, formal-root-cause nullability, and retained-candidate eligibility in `tests/test_session_conclusion.py` and `tests/test_diagnosis_orchestrator.py`.
- [x] BY011 Add frontend graph regressions for retained conclusion metadata, boundary styling, and non-primary observation/mechanism nodes in `web/src/components/diagnosis/aiTreeGraphModel.test.js`.
- [x] BY012 Run focused backend and frontend tests, mark this task group complete, and verify no Celery runner/fixed/oracle files changed for this feature.
- [x] BY013 Ensure line refinement nodes in `server/app/diagnosis/orchestrator.py` always point `parent_candidate_ids` at a real emitted coarse node in the current tree, never at an empty or conceptual placeholder.
- [x] BY014 Add regression coverage in `tests/test_diagnosis_orchestrator.py` for mapping conceptual `coarse_insufficient_evidence` parents onto the actual emitted coarse node id and keeping line parents stable after normalization.

**Execution order**: BY001 -> BY002-BY004 -> BY005-BY007 -> BY008-BY011 -> BY012. Backend contract and conclusion assembly must be complete before frontend assertions are updated.

**Outcome**: A failed deep probe preserves the actual parent diagnosis, displays the failed branch as a qualification boundary, and never turns analyzer status text into the latest root-cause conclusion.

### Task Group BX - 源码机制下探回退与结论一致性

**Purpose**: Prevent a blocked or inconclusive deep probe from erasing its still-plausible parent candidate, continue investigation with another evidence path, and ensure partial localization is never displayed as a high-confidence completed root cause.

- [x] BX001 Add a regression fixture from `diag_session_20260820_140856_578c5c5e` that preserves a Memray/source-supported parent candidate, a missing `ai_generated_query`, a blocked CodeQL result, no eligible final primary cause, and the contradictory high-confidence fallback state in `tests/fixtures/diagnosis/` and `tests/test_diagnosis_orchestrator.py`.
- [x] BX002 Preserve validated `investigation_review.probe_inputs` from session-level AI adjudication through follow-up planning, `ProbePlan.parameters`, `collector_invocation.target_config`, child task options and audit output in `server/app/diagnosis/orchestrator.py` and `server/app/diagnosis/collector_invocation.py`.
- [x] BX003 Prevent `source_mechanism_query` from silently falling back to an absent managed query suite when an AI-generated query was selected; keep the evidence request pending with a structured planning/contract error and do not dispatch an invalid child task in `server/app/diagnosis/orchestrator.py` and `agent/mini_drop_agent/collectors/source_mechanism.py`.
- [x] BX004 Introduce explicit deep-probe outcome semantics for `supported`, `contradicted`, `inconclusive`, and `blocked`, mapping execution failure, missing capability and partial segment coverage to non-refuting outcomes unless evidence explicitly disproves the candidate in `server/app/diagnosis/orchestrator.py`, `server/app/rca/models.py` and `server/app/rca/controlled_tree.py`.
- [x] BX005 Persist a parent-candidate checkpoint containing the strongest supported claim, localization level, evidence refs, unresolved causal edges and confidence before each deep probe; on `blocked` or `inconclusive`, restore that candidate as `needs_more_evidence` instead of replacing it with an Analyzer observation in `server/app/diagnosis/orchestrator.py` and `server/app/diagnosis/session_conclusion.py`.
- [x] BX006 Restrict grey `rejected/contradicted` nodes to explicit counterevidence; render blocked and partial child branches as unresolved, add rollback edges back to the preserved parent, and keep the failed branch query and reason auditable in `server/app/diagnosis/orchestrator.py`.
- [x] BX007 Continue the same candidate through another distinct CodeQL `query_spec_hash` or an independent `python_heap_reference` request when the previous path is blocked/inconclusive, while enforcing attempt, model-call, duration, fingerprint reuse and candidate-ID budgets in `server/app/diagnosis/orchestrator.py` and `server/app/rca/llm_client.py`.
- [x] BX008 Derive session status, `abstained`, `confidence_level`, root-cause candidates and `final_primary_causes` from one conclusion-eligibility result; use `PARTIAL_COMPLETED` for a preserved plausible mechanism with incomplete proof and `INSUFFICIENT_EVIDENCE` when only observations remain in `server/app/diagnosis/orchestrator.py`, `server/app/diagnosis/schemas.py` and `server/app/diagnosis/audit_bundle.py`.
- [x] BX009 Preserve the strongest evidence-bounded parent explanation in the final report as `possible_root_cause` with verified facts, unverified causal edges and the exact next evidence request; prohibit Analyzer fallback text from overwriting it or presenting allocation location as the cause in `server/app/diagnosis/session_conclusion.py` and `server/app/rca/llm_client.py`.
- [x] BX010 Update `web/src/pages/AIDiagnosis.jsx`, `web/src/components/diagnosis/RootCauseClusters.jsx` and `web/src/components/diagnosis/ControlledAITreeGraph.jsx` to distinguish confirmed root cause, possible root cause, partial localization and observation; show blocked child nodes separately from grey contradicted nodes and never display high confidence when no eligible final primary cause exists.
- [x] BX011 Add deterministic tests for guarded-query propagation, invalid-task non-dispatch, blocked-versus-refuted semantics, parent-candidate restoration, distinct-query retry, PyHeap alternate validation, terminal-status consistency and frontend graph mapping in `tests/test_diagnosis_orchestrator.py`, `tests/test_rca.py`, `tests/test_source_mechanism_collector.py` and `web/src/components/diagnosis/aiTreeGraphModel.test.js`.
- [x] BX012 Run the complete backend regression and frontend production build, then deploy without deleting volumes and execute one timestamped Werkzeug #1521 real case; accept only if the report either closes the CodeQL/PyHeap holding mechanism under one `candidate_id`, or truthfully ends as partial/insufficient without high confidence, false contradiction or loss of the strongest parent candidate.

**Execution order**: BX001 -> BX002-BX003 -> BX004-BX006 -> BX007-BX009 -> BX010-BX011 -> BX012. BX002 and BX004 may be implemented in parallel after the regression fixture exists; frontend work starts after the backend state contract is fixed.

**Acceptance record required**: Save the diagnosis detail, audit bundle, probe parameters, CodeQL query and segment coverage, PyHeap reference result when requested, final tree and frontend production build result under a timestamped `reports/eval/real-open-source/werkzeug-1521-*` directory. A probe failure is acceptable only when it remains `blocked/inconclusive`, preserves the parent candidate and produces a truthful non-final session status.

**Outcome**: Deep investigation refines or refutes an existing candidate instead of erasing it. Tool failure means “not verified”, explicit counterevidence means “refuted”, and every backend/API/frontend completion and confidence field agrees with the same final-cause eligibility decision.

### Task Group CA - DAG 证据节点与定位链修复

**Purpose**: 将会话级候选从隐式树和复制节点收口为稳定的 DAG，保留真实父节点解释，并在正式因果资格不足时输出独立的定位链。

- [x] CA001 为候选节点增加稳定的 `child_candidate_ids`，并为定位链和多父汇合补充模型契约。
- [x] CA002 在树归一化阶段建立 canonical candidate index，拒绝重复 ID，并将所有 layer 分类回填到唯一节点对象。
- [x] CA003 根据显式 `parent_candidate_ids` 重建双向父子边，支持多父、多子、共享节点，并拒绝缺失父子、自引用和环。
- [x] CA004 移除 line/function/resource 复制和 alias 改写，新增节点只挂载到当前树中真实存在的来源节点，保留父节点解释字段。
- [x] CA005 分离正式 `causal_chain` 与非正式 `localization_chain`，fallback 按实际 DAG 祖先拓扑回溯并去重。
- [x] CA006 增加多父汇合、反向边、非法引用和节点身份稳定性回归测试。
- [x] CA007 完成核心后端回归测试并更新旧测试至 DAG 契约。

**Outcome**: 同一候选只发出一次；一个节点可以同时作为上游子节点和下游父节点；line 子节点不再覆盖 resource/function 父节点解释；证据不足时正式因果链为空但定位链仍可追溯。

### Task Group BZ - 真实父节点闭环与低质量候选隔离

**Purpose**: 修复 `diag_session_20260822_032023_9241046c` 暴露的候选先错分类、line 错挂 coarse、低质量 call_path 被 fallback 继承以及 probe 完成状态冒充有效源码锚点的问题。

- [x] BZ001 在 `server/app/diagnosis/orchestrator.py` 增加统一 emitted candidate index 和父节点校验，要求所有 `parent_candidate_ids` 与 `origin_parent_candidate_id` 指向当前树中真实存在的 canonical candidate；缺失来源的 refinement、observation、mechanism、boundary 标记为 orphan，不使用 coarse、rank 或数组顺序猜父节点。
- [x] BZ002 调整 `_build_session_controlled_ai_tree()` 的处理顺序，在 coarse 兜底前先分类 observation、call_path_context、line_anchor、mechanism 和 boundary；仅允许明确独立的 alternative/rejected_alternative 进入 coarse parent 处理。
- [x] BZ003 修复 line anchor 归一化，使 `coarse_insufficient_evidence` 等概念父 ID 映射到当前 emitted coarse 或真实基础定位节点；line 节点必须挂到真实基础父节点，无法解析时生成 orphan/missing_provenance，不生成伪 line。
- [x] BZ004 修复 runtime profile 质量门禁和候选转换：low quality、idle loop、poll/select、单样本尾帧和无效超大样本只能生成 observation；不得生成 function/call_path/line 主因、source tree candidate 或 retained candidate。
- [x] BZ005 修复 source snapshot 语义，区分 probe execution completed 与 verified line anchor；缺少有效 revision、file、line 或 source hash 时只生成 source boundary，不能为低质量 runtime candidate 背书。
- [x] BZ006 修复 heap `FAILED`、`blocked`、`memray_attach_failed`、`target_exit` 和 timeout 的树映射，生成挂在真实来源父节点下的 heap boundary；不得生成 heap root cause，也不得把失败节点作为 fallback 父结论。
- [x] BZ007 收紧 `build_retained_conclusion()` 和 `build_fallback_explanation()`，禁止 observation、call_path_context、mechanism、boundary、orphan、无效 anchor 和无资格 cluster 成为 retained conclusion；深探失败只继承最近合法基础父节点，若不存在则返回 abstention。
- [x] BZ008 统一最终结论状态，确保 `formal_root_cause`、`root_cause_candidates`、`final_primary_causes`、`headline`、`abstained` 和 confidence 来自同一 eligibility 结果；无 eligible 主因时不得保留高置信 call_path 结论。
- [x] BZ009 修改 `web/src/components/diagnosis/aiTreeGraphModel.js`，主树仅使用 canonical 显性 lineage；probe/refine 总览边只作为注释，缺失父节点显示 orphan，不使用 index 0、layer 顺序或 coarse probe 边补骨架。
- [x] BZ010 [P] 在 `tests/test_diagnosis_orchestrator.py` 增加真实会话回归 fixture，覆盖 runtime observation 先分类、line 真实父节点、概念 coarse 映射、无效 source snapshot、heap failure boundary、call_path fallback 禁止和 orphan。
- [x] BZ011 [P] 在 `tests/test_session_conclusion.py` 增加 retained candidate eligibility、父结论继承、无合法父节点 abstention、child failure 不影响 parent、parent contradiction 才回退等测试。
- [x] BZ012 [P] 在 `web/src/components/diagnosis/aiTreeGraphModel.test.js` 增加显性父子边、probe 边隔离、orphan、boundary/observation 非反证、无 index 0 自动连接和多分支父节点测试。
- [x] BZ013 运行后端相关回归测试和前端生产构建，检查任务会话输出中所有 parent ID 均存在、没有重复 canonical ID，并将本任务组状态更新为完成。
- [x] BZ014 使用已有 `diag_session_20260822_032023_9241046c` 数据做离线契约检查，确认修复前的极长 call_path 不再成为 retained parent；仅在代码与回归测试通过后，再安排新的真实 case 验收。

**Execution order**: BZ001 -> BZ002-BZ003 -> BZ004-BZ006 -> BZ007-BZ008 -> BZ009 -> BZ010-BZ012 -> BZ013-BZ014. BZ010、BZ011、BZ012 可在对应契约稳定后并行。

**Acceptance criteria**:

```text
所有非 root 节点的 parent 都指向当前 emitted candidate
runtime observation 不再先被挂到 coarse 后再改类型
line parent 不使用概念 ID、root_entity 或 index 0
无效 source snapshot 不生成 verified line
heap failure 只生成 boundary
fallback 不继承 call_path/observation/mechanism/boundary
无合法基础父节点时明确 abstain/orphan
probe edge 不参与主树布局
最终结论字段与 eligibility 一致
```

### Task Group CB - AI 主导候选生成与事实层边界收口

**Purpose**: 将 Analyzer 从“提前生成根因”的裁决者收回到事实、观察和定位边界提供者；由 AI 首轮生成候选、按证据推进候选 DAG，再由工程门禁从合法 AI 节点派生正式根因结论。本任务组建立在 CA/BZ 的 canonical DAG 和父子关系修复之上，不重新引入节点复制或隐式树补边。

**Current code alignment**:

```text
orchestrator.py:1391-1429  当前先建树/建 clusters，再做会话级 AI 审核
orchestrator.py:4837       当前 _build_session_controlled_ai_tree() 可用 analyzer_fallback 包装候选
session_conclusion.py:21   当前 build_root_cause_clusters() 直接消费 Analyzer/cluster_assessment
llm_client.py:35           当前已有 investigation review 入口，但缺少首轮候选生成契约
models.py:218              当前 AITreeCandidateNode 缺少完整来源和候选状态边界
```

#### CB001 事实层输入契约与候选提示模型

- [x] 在 `server/app/rca/models.py` 定义或补齐 `AnalyzerFactContext`、`CandidateHint`、`EvidenceQuality` 和 `LocalizationBoundary`，字段覆盖 `facts`、`observations`、`evidence_refs`、证据质量、定位边界、缺失证据、可用探针和候选提示。
- [x] 明确 `CandidateHint` 只能表达 `unproven`/待调查方向，不能包含 `primary`、`secondary`、`root_cause_cluster`、`causal_chain` 或 `primary/contributing/independent` 裁决字段。
- [x] 为事实层字段定义向后兼容的 JSON 读写规则，确保已有诊断会话仍可读取，但新流程不会把旧 `cluster_assessment` 当成 AI 裁决结果。
- [x] 在 RCA 模型测试中验证候选提示可以保留真实 `evidence_refs` 和定位边界，且无法通过模型转换直接成为正式根因。

#### CB002 Analyzer 输出隔离与事实转换

- [ ] 修改 `server/app/diagnosis/orchestrator.py` 和 `server/app/diagnosis/session_conclusion.py`，将 Analyzer 的 `diagnostic_claim` 转换为 observation/localization，将 Analyzer 的 `mechanism` 转换为 `CandidateHint`，不得直接写入正式 root-cause candidate 或 cluster。
- [ ] 移除 `cluster_assessment.conclusion_eligible` 对最终根因资格的直接影响；该字段只能作为历史 Analyzer 信息或事实质量输入保存，不能单独提升 `conclusion_eligible`。
- [ ] 检查 `_root_cause_cluster_candidates()`、`build_root_cause_clusters()` 和 `_compound_location_fields()` 的调用方，阻止 Analyzer candidate、hint、observation 进入正式 cluster 输入。
- [ ] 保留 Analyzer 的证据引用、定位边界、缺证原因和可用探针，供 AI 首轮调用使用；禁止在此转换阶段生成 `causal_chain`。
- [ ] 在 `tests/test_diagnosis_orchestrator.py` 和 `tests/test_session_conclusion.py` 增加断言：同一 Analyzer 输出只能产生事实/观察/提示节点，不能产生 `primary` 或正式 cluster。

#### CB003 AI 首轮候选生成接口

- [x] 在 `server/app/rca/llm_client.py` 新增 `generate_session_candidate_review(...)`，输入 `AnalyzerFactContext`、probe manifest、现有合法 AI DAG 和证据摘要，输出结构化首轮候选审查结果。
- [ ] 规定首轮输出字段：`candidate_id`、`claim`、`mechanism`、`target`、`supported_level`、`decision`、`causal_status`、`evidence_refs`、`missing_evidence`、`parent_candidate_ids`、`probe_requests`；`decision` 至少支持 `needs_more_evidence`、`conclude`、`reject`，`causal_status` 至少支持 `supported`、`needs_more_evidence`、`contradicted`、`rejected`。
- [x] 在 prompt 和解析校验中限制 AI 只能引用输入中的 evidence ref，只能请求注册 probe，不能把 Analyzer hint 的 ID 冒充为 AI candidate ID。
- [x] 让 AI 首轮结果携带可审计的 review 状态、模型调用失败原因和原始结构化响应摘要；异常时不得合成根因候选。
- [x] 在 `tests/test_rca.py` 覆盖有效响应、非法 evidence ref、未知 probe、缺少机制/目标和解析失败。

#### CB004 调整首轮调用顺序与树来源

- [ ] 在 `server/app/diagnosis/orchestrator.py` 重排会话流程：先构造事实上下文，再调用 `generate_session_candidate_review()`，然后执行合法 probe 请求，之后调用 investigation review 更新 DAG，最后才派生 clusters 和最终解释。
- [ ] 将现有 `_build_session_controlled_ai_tree()` 拆分为 `_build_ai_tree_from_review()` 与 `_build_fallback_observation_tree()`；首轮 Analyzer candidate 不得通过 `analyzer_fallback` 包装为 AI 节点。
- [ ] AI 生成的节点使用 `generated_by="ai_candidate"`，通过工程门禁后的节点使用 `generated_by="ai_guarded"`；事实和 fallback 节点分别使用 `analyzer_observation` 与 `fallback_observation`。
- [ ] 保留上一轮已经通过门禁的 AI 节点及其 canonical ID、父子边和解释；本轮 Analyzer hint 只能作为新一轮 AI 输入，不能覆盖或复制既有节点。
- [ ] 增加调用顺序测试，确认 `build_root_cause_clusters()` 不会在首轮 AI 候选生成和门禁校验之前执行。

#### CB005 证据回流后的 AI DAG 更新

- [x] 扩展 `generate_session_investigation_review()` 的输入输出，使其接收当前 AnalyzerFactContext、已有 AI DAG、探针结果和证据缺口，返回候选状态更新、增量节点、`parent_candidate_ids`、`child_candidate_ids`、rollback 边和下一步 probe 请求。
- [x] 仅允许 AI review 创建或更新候选节点；Analyzer 新事实只能追加 evidence refs、observation 或 hint，不能直接改变候选的 role、causal_status 或结论资格。
- [x] 保留 `rejected`/`contradicted` 分支及其证据；探针失败、缺能力、超时和目标退出统一表示为 `blocked`/`inconclusive`，不得当作反证覆盖父候选。
- [x] 对多父节点要求明确 `relation`（`causal_convergence`、`shared_evidence` 或 `alternative`）和独立证据；没有关系类型或证据不足时拒绝该边。
- [x] 复用 CA/BZ 的 canonical index 和双向边校验，确保父节点已真实发出，已有 resource/function/line 节点解释不被子节点更新覆盖。
- [x] 在 `tests/test_diagnosis_orchestrator.py` 覆盖候选继续下钻、父节点继承、拒绝/回溯、探针失败不反证、多父汇合和父解释保持不变。

**CB005 真实回流状态（2026-08-22）**：
`reports/eval/real-open-source/celery-8882-vulnerable-600s-20260822-231915/run.json`
中的 `investigation_review` 已真实返回 `ai_review_status=succeeded`，选择的证据族为
`python_heap_profile`。本轮没有合法的 `candidate_updates` 或 `rollback_edges`，因此保留
`memory_leak_rss_growth` 父结论，写入 `qualification_boundary.status=inconclusive`，
并以 `origin_parent_candidate_id=memory_leak_rss_growth` 回退；没有把探针缺失误判为反证。
本轮最终 `abstained=true`、`root_cause_clusters=[]`、`causal_chain=[]`、
`formal_root_cause=null`。这表示 CB005 的状态回流契约已跑通，但真实 heap 证据仍未完成，
不能把本轮标记为完整根因闭环。

#### CB006 AI 资格门禁与根因簇派生

- [x] 在 `server/app/rca/controlled_tree.py` 增加明确的 `qualify_ai_candidate(...)` 或等价门禁入口，统一计算 `conclusion_eligible`，不读取 Analyzer 的同名字段作为授权结果。
- [ ] 门禁校验候选来源是 AI、candidate ID 合法、evidence refs 存在且属于当前会话、目标和时间窗口正确、机制具体、定位层级未越界、必要补证完成、`causal_status=supported`、`decision=conclude`、因果链闭合且 DAG 父节点真实存在。
- [x] 将 `build_root_cause_clusters()` 重构为从已通过门禁的 AI DAG 派生 clusters；禁止 `analyzer_observation`、`candidate_hint`、`fallback_observation`、`observation`、`mechanism`、`boundary`、`orphan`、`missing_evidence`、`rejected` 和 `contradicted` 节点进入正式 clusters。
- [ ] 正式 cluster 的 `role` 只能来自 AI 的 `primary`、`contributing` 或 `independent` 裁决，并保留对应 candidate ID、证据引用和完整因果链。
- [ ] 增加门禁失败原因的结构化记录，区分非法引用、证据不足、层级越界、关系非法和明确反证，供审计和前端展示。
- [ ] 在 `tests/test_session_conclusion.py` 和 `tests/test_rca.py` 覆盖 Analyzer eligible 欺骗、非法 evidence ref、未闭合因果链、合法 AI 主因和 AI 多父整合。

#### CB007 fallback 与最终结论字段收口

- [x] 修改 `server/app/diagnosis/session_conclusion.py` 和 `orchestrator.py`：AI 首轮或回流失败时只生成事实/观察/定位/证据缺口展示，设置 `abstained=true`、`root_cause_clusters=[]`、`causal_chain=[]`、`formal_root_cause=null`。
- [x] 允许 `localization_chain` 按 CA 的实际 DAG 祖先拓扑保留，但禁止使用 Analyzer hint、fallback 节点或 `line_id_aliases` 拼接正式因果链。
- [ ] 允许继承上一轮合法且仍有效的 AI 节点；禁止把本轮 Analyzer hint 升级为 AI 根因，禁止 fallback 文本覆盖合法父候选解释。
- [ ] 统一 `formal_root_cause`、`root_cause_candidates`、`final_primary_causes`、`headline`、`abstained`、confidence 和 `causal_chain` 的来源，全部由同一个 eligibility 结果派生。
- [ ] 在 `tests/test_session_conclusion.py` 增加 AI 失败、无合法 AI 节点、保留定位链、继承上一轮合法节点和无高置信根因的回归测试。

#### CB008 节点来源与兼容字段迁移

- [x] 在 `server/app/rca/models.py` 扩展 `AITreeCandidateNode.generated_by` 至 `analyzer_observation`、`ai_candidate`、`ai_guarded`、`fallback_observation`，并保留历史 `analyzer_fallback` 的读取兼容和迁移映射。
- [x] 同步扩展 `AITreeLayer.generated_by` 和相关持久化/API/audit bundle 序列化，不改变 CA 的 `parent_candidate_ids`/`child_candidate_ids` canonical 关系。
- [ ] 更新 `server/app/rca/controlled_tree.py`、`orchestrator.py` 和前端 graph model 的来源判断，避免旧值被当成正式 AI 节点或被错误渲染为已确认根因。
- [ ] 为历史会话读取增加兼容测试：旧 `analyzer_fallback` 只能迁移为 observation/fallback 展示，不能获得新的 `conclusion_eligible`。

#### CB009 结论审计和 API 契约

- [ ] 在 `server/app/diagnosis/audit_bundle.py` 及相关 API 输出中记录 AnalyzerFactContext、AI review 阶段、模型状态、candidate 来源、资格门禁结果、拒绝原因和 fallback/abstention 状态。
- [ ] 确保审计中可以区分“Analyzer 观察到什么”“AI 提出了什么”“门禁通过了什么”“最终输出了什么”，并保留每个 evidence ref 的来源。
- [ ] 保持前端已有 retained conclusion、qualification boundary、formal root cause 和 localization chain 字段兼容；只补充来源/状态字段，不让展示层推导根因资格。
- [ ] 增加 API/audit 序列化测试，确认 AI 失败时 clusters 和 causal chain 为空但 localization chain 可存在。

#### CB010 单元与集成回归

- [ ] 新增或更新 `tests/test_diagnosis_orchestrator.py`、`tests/test_session_conclusion.py`、`tests/test_rca.py` 和 `tests/test_llm_client.py`，覆盖 Analyzer 事实隔离、AI 首轮候选、证据回流、多父 DAG、父解释不覆盖、门禁和 fallback。
- [x] 更新前端 `web/src/components/diagnosis/aiTreeGraphModel.test.js`，覆盖来源标签、显式父子边、blocked/inconclusive 分支、orphan、fallback observation 和无正式 causal chain 展示。
- [x] 运行后端 RCA/diagnosis 回归和前端生产构建，确认 CA/BZ 已有父子关系、节点身份稳定性和 fallback 定位链测试仍通过：后端 `209 passed`，前端 `12 passed`，生产构建成功（仅保留既有 chunk/circular chunk warning）。

#### CB011 Celery VM 真实验收

- [x] 使用 `docs/real_cases/celery_8882/run_case_vm.py` 运行真实 case；确认 VM runtime、首轮 Analyzer 输入和 probe 输入均不包含离线 Oracle 或预期根因答案。
- [ ] AI 成功时验收日志能证明真实首轮候选生成、probe 选择、证据回流、候选更新、门禁结果和 causal chain 引用，而不是由 Analyzer candidate 直接包装生成。
- [x] AI 失败或 DeepSeek 不可用时验收结果必须为 fallback/abstention：正式 clusters 为空、`causal_chain=[]`、`formal_root_cause=null`，但事实、局部定位和证据缺口可保留。
- [ ] Oracle 只能由 `evaluate_case.py` 在诊断完成后离线读取；将诊断详情、audit bundle、最终 DAG、AI review 状态和测试结果写入 `reports/eval/real-open-source/celery-8882-*` 时间戳目录。
- [ ] 只有代码、单测、集成测试和 VM 契约验收全部通过后，才将 CB 任务组标记为完成；失败时记录具体阶段和阻断原因，不将部分定位标记为正式根因。

**CB011 vulnerable-only 真实验收记录（2026-08-22）**：
部署 revision 为 Mini-Drop `5a9bf1e`；Control 和 Worker1 已执行 `git pull` 并完成
容器 rebuild。真实报告为
`reports/eval/real-open-source/celery-8882-vulnerable-600s-20260822-231915/run.json`，
诊断会话为 `diag_session_20260822_151956_791e29ac`。本次只运行 vulnerable，
没有运行 fixed、`evaluate_case.py` 或 Oracle。VM 内使用完整 Celery revision，
真实 Redis、worker、monitor 和原生 `apply_async()` producer。

验收结果：`formal_root_cause=null`、`root_cause_clusters=[]`、
`causal_chain=[]`、`abstained=true`；`python_heap_profile` 仍是
`inconclusive` 缺失证据。最终树中 `memory_leak_rss_growth` 保留为基础候选，
局部 boundary 均显式回到该来源父节点，未发现缺失父 ID 或 `"None"` 伪父 ID。
producer 只完成第一批 1000 个失败任务，未在 runner 窗口内写入
`producer_complete`，所以本次真实验收结论为 **partial / 未形成正式根因**，
CB011 不能整体勾选完成。

**Execution order**: CB001 -> CB002 -> CB003 -> CB004 -> CB005 -> CB006 -> CB007-CB009 -> CB010 -> CB011. CB008/CB009 可在 CB006 的数据契约稳定后并行；CB010 必须等待后端契约和来源迁移完成。

**Acceptance criteria**:

```text
Analyzer 只产生 facts/observations/localization boundary/candidate hints
Analyzer hint 和 cluster_assessment.conclusion_eligible 不得直接产生正式 root cause
首轮 AI 真实生成 candidate，不能由 analyzer_fallback 伪造
AI 才能请求 probe、更新候选、建立 causal DAG 和裁决 role
正式 clusters 只能从通过统一门禁的 AI 节点派生
父子边、多父关系和父节点解释遵守 CA/BZ canonical DAG
探针失败/缺能力是 blocked 或 inconclusive，不是自动反证
AI 失败时 clusters/causal_chain/formal_root_cause 为空或 null，abstained=true
fallback 仍可保留事实、定位链和证据缺口
VM runtime 和首轮输入不含 Oracle，Oracle 仅离线评估
```

**Outcome**: Analyzer 只回答“观察到什么、定位到哪里、还缺什么”；AI 才回答“候选是什么、下一步查什么、哪些候选成立以及如何形成因果链”。在 AI 不可用时，系统诚实停在事实/定位层，不再把 Analyzer 候选或局部定位伪装成正式根因。

---

## Task Group CC - 主树渲染、候选门禁诊断与实时 Heap 降级

**Purpose**: 完成当前真实 case 暴露的三条断链：前端历史子树混入主图、AI 候选失败原因不可见、Memray attach 失败后诊断链停止。

- [x] CC001 为 `ControlledAITree` 增加 `tree_kind/renderable`，子任务树只作为审计与回放快照，禁止回填为 session canonical tree。
- [x] CC002 修复诊断详情切换的旧状态、响应 ID 校验和图组件 key，避免旧会话节点残留到新会话。
- [x] CC003 让主图只按显式 lineage 边布局；duplicate candidate ID、missing parent 和 child snapshot 以数据质量状态显式展示。
- [x] CC004 禁止无来源 `alternative/rejected_alternative/refinement` 节点自动挂 coarse；源码 line 节点只允许挂当前 emitted coarse 或真实基础父节点。
- [x] CC005 为 AI 首轮候选校验保存每次 retry 的失败阶段、字段路径、实际值、合法 evidence ref 数量、合法父节点清单、脱敏响应 hash/摘要。
- [x] CC006 为 AI 候选资格门禁输出 `ai_gate_failures`，并确保 Analyzer/fallback/observation/mechanism/boundary/orphan 节点不能派生正式 cluster。
- [x] CC007 将 heap `blocked/failed/memray_attach_failed/timeout/target_exit` 结构化记录 preflight、失败类型、stderr、重试状态，并在失败后继续 runtime/source follow-up。
- [x] CC008 增加 Worker1 容器内 managed Memray helper；允许同 PID namespace/UID 场景下实时 attach，不要求目标进程预加载。
- [x] CC009 增加受控 native allocator live helper 降级；该路径只输出 `native_allocation_observation` partial evidence，不生成 Python retention 或源码行根因。
- [x] CC010 补齐后端、前端、heap、AI 门禁和 sample-quality 回归；完成前端 production build。
- [ ] CC011 在 VM 部署后执行 Worker1 heap collector smoke，保存 helper/preflight/structured evidence 产物。
- [x] CC012 使用最新 Celery 原始证据离线回放，确认 AI 候选失败原因和 gate failure 与初始证据一致。
- [ ] CC013 仅运行 vulnerable-only Celery 600s 真实 case，确认 heap 失败可继续探测、主树无历史快照污染、无正式根因时 abstained 正确。

**CC012 离线回放记录（2026-08-22）**：
使用 `docs/real_cases/celery_8882/replay_original_evidence.py` 只读回放
`reports/eval/real-open-source/celery-8882-vulnerable-600s-20260822-231915/run.json`，
输出 `offline-replay.json`。回放未读取 issue、PR、修复 commit 或 Oracle。
结果确认：初始证据支持 RSS 增长和 function 层观察，但首轮没有真实
`ai_candidate_*`，`python_heap_profile` 为 `FAILED/memray_attach_failed`，
`primary_anchor` 没有 verified file/line，最终 `formal_root_cause=null`、
`root_cause_clusters=[]`、`abstained=true`；保留结论为
`memory_leak_rss_growth` 的 `inherit_parent`，没有生成新的 fallback claim。

**Acceptance criteria**:

```text
前端只渲染 session_main/renderable tree
child_snapshot 不进入主图
主树不存在 index 0、layer 顺序或 coarse probe 自动补边
line/refinement/observation/mechanism/boundary 的 parent 必须是真实 emitted 节点
AI 非法输出能指出实际字段和值，以及当时可用证据/父节点
AI 候选未过门禁能指出具体原因且不进入正式 cluster
heap attach 失败不会伪造 Python retention
helper 可以在目标运行期间触发 attach
native live 降级只声明 native allocation observation
heap failure 后仍会继续 runtime/source，最终进入明确终态
```

---

## Task Group CD - 多候选 AI 首轮与 Analyzer fallback 边界

**Purpose**: 明确 AI 首轮候选生成、调查排序和 Analyzer fallback 的职责边界，避免单个非法候选拖垮整轮，也避免 Analyzer 候补方向被误当成 AI 或正式根因。

- [x] CD001 首轮最多保留 4 个 AI 候选，逐个校验；单个候选非法时记录 `validation_diagnostics`，其余合法候选继续进入成功结果。
- [x] CD002 使用证据数量、可探测性和 AI 返回顺序选择最多 3 个 `active_candidate_ids`；选择只影响深探调度，不改变 `conclusion_eligible`。
- [x] CD003 其余合法候选保存在 `candidate_review` 审计结果中，标记 `deferred_candidate_ids`，不创建 active 深探。
- [x] CD004 只有全部候选无效、AI 未启用、调用失败、解析失败或重试耗尽时，才将 Analyzer 方向标记为 `analyzer_fallback`。
- [x] CD005 Analyzer fallback 节点固定为 `role=unknown`、`status=missing_evidence`、`claim_type=partial_localization`、`causal_status=unproven`、`conclusion_eligible=false`。
- [x] CD006 AI 成功生成候选后，正式门禁仍只读取 `qualify_ai_candidate()` 和 `enforce_conclusion_eligibility()`，active 选择不授予正式根因资格。
- [x] CD007 前端显示候选无效的实际字段、初始证据引用、缺失证据和缺失父节点；回归测试覆盖部分成功、全部失败和 active 限制。

**Acceptance criteria**:

```text
一个候选非法不会使其他合法候选丢失
AI 成功时不切换到 Analyzer fallback
AI 完全失败时才出现 analyzer_fallback
active_candidate_ids 最多 3 个
deferred_candidate_ids 仍保留在 candidate_review
active 选择不改变 conclusion_eligible
Analyzer fallback 不进入正式 cluster
门禁失败能关联最初证据和实际缺失项
```

---

## Task Group CE - 完整闭环：主树渲染、Line 探测、实时 Heap 与候选失败解释

**Purpose**: 收口最新 Celery vulnerable-only case 暴露的剩余问题：前端把
orphan/历史子树混入可视树、`source_snapshot` 完成后没有形成可验证 line
锚点、heap attach 失败会中断深探链路，以及 AI 候选/门禁失败无法用最初证据
解释。该任务组不改变普通真实 case 的运行模型，不增加 fixed、Oracle 或第二套
runner。

### CE001 统一会话输出域与兼容契约

- [x] 在 `server/app/rca/models.py` 或现有诊断 schema 中补齐
  `tree_kind`、`renderable`、`line_anchor_eligibility`、
  `candidate_diagnostics`、`ai_gate_failures`、`heap_probe_outcome` 和
  `data_quality` 的读写契约。
- [x] 约束 `tree_kind` 至少支持 `session_main`、`child_snapshot`、
  `probe_history`、`data_quality`；只有 `session_main` 默认
  `renderable=true`。
- [x] 保持旧会话字段兼容：旧 `analyzer_fallback` 只能迁移为观察/回退展示，
  不得因此获得正式根因资格。
- [x] 增加模型序列化测试，确保新增字段缺失时旧报告仍可读，新报告不会把
  历史子树当成主树。

### CE002 当前 emitted candidate 的唯一父节点解析

- [x] 在 `_build_session_controlled_ai_tree()` 入口先建立 emitted candidate
  index，并把 `coarse_insufficient_evidence` 等概念父 ID 映射到实际 emitted
  coarse ID。
- [x] 所有 `refinement`、`line_anchor`、`observation`、`mechanism`、
  `boundary` 节点必须在写入 layer 前解析到当前真实父节点；解析失败标记
  `missing_provenance/orphan`，不得补 coarse。
- [x] `alternative/rejected_alternative` 只有在明确表示独立候选且 relation
  合法时才允许挂真实 coarse；没有来源的备选分支保持 orphan。
- [x] 删除或封锁任何按 `primary_nodes[0]`、rank、layer 顺序、全局最具体候选
  或 root entity 猜父的路径。
- [x] 对 line 节点增加硬断言：其 parent 必须存在于当前树，且不能是概念 ID。
- [x] 增加回归：line 挂真实 emitted coarse、概念 coarse 映射、缺失父节点
  orphan、不同 branch 不共享父节点。

### CE003 Fallback 与局部边界回退

- [x] 深探 `blocked/failed/partial/inconclusive/target_exit` 统一生成 boundary
  子节点，`parent_candidate_ids` 和 `origin_parent_candidate_id` 必须指向
  实际来源父节点。
- [x] 父节点的 claim、mechanism、evidence refs 和结论状态在子探针失败时保持
  不变；boundary 只能说明停止原因，不能生成新的 claim。
- [x] 父节点本身被直接反驳时，才沿其 `origin_parent_candidate_id` 回退；
  子节点失败不得反证父节点。
- [x] 来源父节点缺失时只输出 data-quality orphan 和 `abstained=true`，不能
  创建新的“当前证据不足”根因节点。
- [x] 增加 `retained_conclusion`、`qualification_boundary`、
  `localization_chain` 和 formal conclusion 一致性测试。

### CE004 AI 候选生成与门禁失败的证据化输出

- [x] 为每次首轮/重试/调查轮保存阶段、attempt、field path、脱敏
  `actual_value`、响应摘要 hash、候选数、接受/拒绝数、失败 code 和重试状态。
- [x] 将当时的 `initial_evidence_context`、有效 evidence refs、证据族、证据
  状态、缺失 evidence refs 和 emitted parent IDs 固定写入诊断记录。
- [x] 为每个 `ai_gate_failure` 输出结构化 `gate_checks`，至少区分：
  `source_is_ai`、`candidate_id`、`evidence_refs`、`target/window`、
  `supported_level`、`mechanism`、`causal_status`、`decision`、
  `required_probe`、`parent_exists`、`origin_parent`、`causal_chain`。
- [x] 明确“模型返回了什么”和“为什么不能进入正式结论”两层内容；不能只
  输出一个总的 `no_usable_ai_candidate`。
- [x] 单个候选失败时保留其他合法候选；只有所有候选和重试都不可用时才写
  `analyzer_fallback`。
- [x] 增加 API/audit 序列化测试，确保不泄漏 issue、PR、fix revision、Oracle
  或密钥。

### CE005 Probe family 映射与 Line 探测闭环

- [x] 建立 AI evidence family 到注册 probe ID 的单一映射，至少覆盖
  `cpu_profile -> process_cpu_profile`、`python_runtime_profile ->
  process_python_runtime_profile`、`python_heap_profile ->
  process_python_heap_profile` 和 `source_snapshot ->
  process_source_snapshot`。
- [x] 对未知 family 直接生成 `unknown_probe_family` 诊断，不创建一个不会执行
  的任务；执行结果必须回绑定 `candidate_id` 和
  `origin_parent_candidate_id`。
- [x] 定义 `line_anchor_eligibility` 检查：revision、file、positive line、
  runtime/source 匹配、source hash、真实父节点、同窗关系。
- [x] `source_snapshot=valid` 仅验证源码上下文；只有通过
  `line_anchor_eligibility` 才创建 `line_anchor` 节点。
- [x] 未通过时生成挂在来源父节点下的 source/line boundary，并输出具体失败
  检查；不把 `source_snapshot` 成功伪装成 line 层完成。
- [x] 只有 `line_anchor` 通过后才允许 source mechanism、对象调用链或机制解释
  继续下钻；机制节点不允许挂在 service/function 旁支。
- [x] 增加 Celery frame 优先级测试，确保异常处理真实源码帧优先于通用 exception
  helper，并验证 line 节点父 ID 是当前 emitted 节点。

### CE006 实时 Heap 采集器与结构化降级

- [x] 固化 Memray live attach 为 Python heap 主路径；目标进程不预加载
  Memray，Agent 通过容器内 managed helper 在诊断窗口内触发 attach。
- [x] 完整记录 attach preflight：PID/mount namespace、UID、ptrace 权限、
  helper 可用性、目标稳定性、runtime 版本、输出路径和 blocked reason。
- [x] attach 失败允许一次有界 retry；保存 stdout/stderr、exit code、failure
  type、retry outcome 和产物缺失原因，禁止只返回一个布尔失败。
- [x] 成功时保留官方 `memray.bin`、stats/leaks JSON/CSV 和结构化热点；
  失败时继续 `python_runtime_profile`、RSS/smaps 和 `source_snapshot`，不让
  heap 失败终止诊断会话。
- [x] native live helper 只能输出 `native_allocation_observation` partial；
  不得生成 Python retention、对象引用链或源码 line 根因。
- [x] 不引入 `gc.get_referrers`、objgraph、Pympler 或自研对象图作为 Memray
  替代；py-spy 负责运行时栈，smaps/RSS 负责进程内存，工具语义保持分离。
- [x] 增加 collector 测试：未预加载 attach 成功、namespace blocked、permission
  failure、retry、target exit、native fallback、产物不完整和继续 follow-up。

### CE007 主树、历史子树与数据质量前端分离

- [x] `buildControlledAITreeGraph()` 只接受 `session_main/renderable=true`
  作为主图输入；`child_snapshot` 和 `probe_history` 不进入 ELK 主布局。
- [x] 主树只使用 explicit lineage edge；coarse summary/probe edge 降级为注释，
  rollback/boundary 作为非布局边；不再用任何 index 0 或 layer 顺序补边。
- [x] `orphan`、重复 candidate ID、missing parent 和 invalid relation 独立进入
  `dataQuality` 面板，不作为主树节点，也不伪装成真实层级。
- [x] Observation、mechanism、STOP、boundary 的样式和语义分开；blocked/partial/
  inconclusive 不得显示成灰色 rejected。
- [x] 历史子树提供审计/回放入口，明确展示“尝试过什么、回退到谁、为什么停止”，
  但不能改变当前 session main tree。
- [x] 增加前端测试：orphan 不进主布局、历史子树不污染当前图、显式 line parent、
  mechanism 只能在线下、多个 probe 不混成同一层、STOP 不是全局终点。

### CE008 候选失败与门禁失败前端诊断视图

- [x] 在诊断详情页增加候选生成诊断区，按 attempt 展示模型阶段、候选输出摘要、
  实际失败字段、初始证据、缺失证据、父节点解析和重试结果。
- [x] 增加门禁失败区，展示每个候选的逐项 gate check、失败原因和“保留到哪个
  父结论/边界”的结果。
- [x] 增加 line eligibility 和 heap outcome 摘要，明确区分“采集成功”“证据有效”
  和“允许升级到 line/root cause”。
- [x] 没有 formal root cause 时，页面必须显示明确 abstention/retained conclusion，
  不使用一个新造的模糊 fallback headline。
- [x] 增加前端 API fixture 测试，确保失败诊断可读且不把观察/边界显示成正式主因。

### CE009 审计与 API 回流闭环

- [x] 在 `audit_bundle.py` 记录 Analyzer facts、AI attempts、candidate diagnostics、
  gate failures、probe outcomes、line eligibility、tree kind、orphan/data quality、
  retained conclusion 和 final eligibility。
- [x] 每个深探结果绑定 `diagnosis_id`、`parent_task_id`、`candidate_id`、
  `origin_parent_candidate_id`、evidence family、probe ID 和 fingerprint。
- [x] 确保旧报告读取兼容，新报告能区分 Analyzer 观察、AI 候选、门禁通过、
  fallback 保留和最终输出；不让前端自行推导结论资格。
- [x] 增加 audit/API round-trip 测试，验证 AI 失败时 formal clusters/causal chain
  为空但 localization/diagnostics 可用。

### CE010 本地回归与离线真实数据回放

- [x] 新增/更新后端测试：`test_diagnosis_orchestrator.py`、
  `test_session_conclusion.py`、`test_rca.py`、`test_llm_client.py`、
  `test_python_heap_collector.py`。
- [x] 新增/更新前端 `aiTreeGraphModel.test.js` 和诊断详情 fixture 测试。
- [x] 使用最新 Celery `run.json` 只读回放，输出：
  `initial_evidence_context`、候选失败/成功、gate failures、line eligibility、
  heap outcome、orphan/data-quality 和 retained conclusion。
- [x] 离线回放不读取 issue、PR、fix commit、Oracle 或测试答案；回放只验证当前
  证据能否解释为什么通过/不通过门禁。
- [x] 通过 `compileall`、`git diff --check`、focused pytest 和前端 production build。

### CE011 提交、部署与 Worker1 Heap Smoke

- [x] 代码和测试通过后提交并推送 mini-drop 仓库，不提交本地报告、密钥、临时
  回放文件或 VM 私有配置。
- [x] Control 执行 `git pull` 并 rebuild mini-drop；Worker1 执行 `git pull` 并
  rebuild Agent/采集器相关容器；保留并核对 worker 原有未提交 helper 改动。
- [x] Worker1 使用未预加载 Memray 的受控长寿命 Python 目标执行 heap smoke，
  保存 preflight、helper、产物和结构化 evidence；确认目标运行期间完成 attach。
- [x] Heap smoke 失败时只记录具体 capability/namespace/permission 原因，不能
  把失败写成 heap retention 成功。

### CE012 Vulnerable-only Celery 600s 真实验收

- [x] VM 内使用完整 Celery checkout、真实 Redis、Celery worker 和原生
  `apply_async()` producer；runner 只运行 vulnerable workload，持续 600 秒覆盖
  诊断窗口。
- [x] 不运行 fixed replay、Oracle 或 `evaluate_case.py`；普通跑测不重复设置这三
  个阶段。
- [x] 验收主树：`session_main` 可渲染；line 若通过则挂到真实 emitted parent；
  observation/机制/STOP 只挂到各自来源；orphan 和历史子树不进入主布局。
- [x] 验收候选诊断：能看到 AI 实际返回/失败字段、初始证据、合法候选数、门禁失败
  清单和 fallback 保留父节点；不能把 Analyzer fallback 伪装成 AI root cause。
- [x] 验收 heap：attach 成功时有真实 Memray 产物；失败时有结构化边界且
  runtime/source follow-up 仍完成。
- [x] 无 eligible AI candidate 时必须为
  `root_cause_clusters=[]`、`causal_chain=[]`、`formal_root_cause=null`、
  `abstained=true`；保留结论只能是实际来源父节点的原结论。
- [x] 报告保存 runner manifest、producer barrier、diagnosis terminal state、
  probe outcomes、AI review、tree/data-quality 和最终结论字段，作为本次 vulnerable-only
  跑测记录。

**CE011/CE012 实际执行记录（2026-08-22）**：

- 提交并推送：`e799d76`。
- Control 和 Worker1 均已 `git pull --ff-only origin try1` 并 rebuild；
  Worker1 agent 使用新镜像启动。
- Worker1 heap smoke 使用未预加载 Memray 的 Python 目标，由 managed helper
  现场 attach，产出 `memray.bin`、stats 和 `python_heap_profile`，证据状态为
  `valid`。
- vulnerable-only 报告：
  `reports/eval/real-open-source/celery-8882-vulnerable-600s-20260823-041730/run.json`。
- 诊断会话：`diag_session_20260822_201819_2ebdfe94`。
- AI 首轮候选 3 个，均通过结构校验并带显式父节点；`source_snapshot` 形成
  已验证的 `/opt/celery-src/celery/app/trace.py:651` line 锚点。
- 运行时树为 `session_main`；orphan 和 child snapshot 被记录为数据质量/历史
  信息，不进入主布局；正式根因仍为空，`formal_root_cause=null`、
  `abstained=true`，保留结论沿实际来源父节点回退。
- producer 在等待窗口内未写入 `producer_complete`，因此本次运行结果是
  **partial / 未形成正式根因**；这是真实运行状态，不把任务不完整伪装成验收成功。

**Execution order**:

```text
CE001 -> CE002 -> CE003
CE004 -> CE005
CE006
CE007 -> CE008 -> CE009
CE010 -> CE011 -> CE012
```

CE004、CE006 可在 CE002 的候选契约稳定后并行；CE007/CE008 依赖后端输出字段；
CE011 必须在本地回归通过后执行；CE012 必须在部署和 Worker1 heap smoke 通过后执行。

**Completion gate**:

```text
主树只包含 session_main canonical lineage
历史子树、probe edge、orphan 不参与主布局
line 只有通过真实 file:line eligibility 才生成
机制链只能挂在 verified line 下
深探失败只生成 boundary 并继承来源父结论
AI 失败/门禁失败能用初始证据解释
heap live attach 不要求预加载，失败结构化降级并继续诊断
普通真实 case 只跑 vulnerable-only 600s
未形成正式根因时 abstention 字段一致
```

## Task Group CF - Unified Closure: Tree, Heap and Candidate Diagnostics

**Purpose**: 按当前真实 case 暴露的问题重新收口。CF 将代码契约、离线
证据回放和 VM vulnerable-only 跑测分开，避免把旧的“本地测试通过”记录
误当成真实 VM 验收完成。

### CF001 - Canonical lineage and emitted-parent validation

- [ ] 建立当前 `session_main` 的 emitted candidate index。
- [ ] 将 `coarse_insufficient_evidence` 等概念 ID 映射到真实 emitted coarse ID。
- [ ] 强制 `refinement`、`line_anchor`、`observation`、`mechanism`、
  `boundary` 只能引用当前树中存在的父节点。
- [ ] 无法解析来源时写入 `data_quality.missing_provenance/orphan`，不补 coarse。
- [ ] line 节点增加真实父节点硬门禁，禁止使用 rank、layer 顺序或
  `primary_nodes[0]`。

### CF002 - Fallback and conclusion inheritance

- [ ] 深探 `blocked/failed/partial/inconclusive/target_exit` 统一生成局部
  boundary 子节点。
- [ ] boundary 继承真实 origin parent 的 claim、evidence 和结论状态。
- [ ] 子探针失败不得反证父节点；只有父节点被直接反证时才回退。
- [ ] 来源父节点缺失时只输出 data-quality orphan 和 abstention。
- [ ] 统一 `formal_root_cause`、`root_cause_clusters`、`causal_chain`、
  `headline`、`confidence`、`abstained` 的资格来源。

### CF003 - AI candidate output and gate diagnostics

- [ ] 保存每次首轮、重试和调查轮的阶段、attempt、响应摘要 hash、候选数、
  接受/拒绝/延后数量和失败状态。
- [ ] 固定保存该次尝试看到的 initial evidence context、有效 refs、缺失 refs、
  evidence status 和 emitted parent IDs。
- [ ] 输出逐字段 gate checks，至少覆盖 source、candidate、evidence、target/window、
  supported level、mechanism、causal status、decision、required probe 和 parent。
- [ ] 单个候选失败不影响其他合法候选；只有全部候选失败才生成
  `analyzer_fallback`。
- [ ] Analyzer fallback 保持 unknown/partial_localization/unproven，
  不得进入正式 root-cause cluster。
- [ ] 前端和 audit 均展示“模型实际返回什么、为什么未过门禁、保留哪个父结论”。

### CF004 - Runtime quality and line-probe closure

- [ ] 为 `python_runtime_profile` 增加 sample-quality 字段和 idle/framework
  状态分类。
- [ ] 低质量 poll/select/epoll/sleep/event-loop 样本只能进入 observation。
- [ ] 统一 evidence-family 到 registered probe 的映射，未知 family 生成结构化
  失败而不创建虚假任务。
- [ ] 实现 line-anchor eligibility：revision、file、line、runtime/source match、
  source hash、window 和真实 parent。
- [ ] `source_snapshot=valid` 仅证明源码可读；不通过 line 门禁时生成来源 boundary。
- [ ] source mechanism 和 object-call-chain 只允许从 verified line 下钻。

### CF005 - Live Memray and bounded heap fallback

- [ ] 目标不预加载 Memray，Agent 使用 managed helper 在诊断窗口现场 attach。
- [ ] 保存 PID/namespace/UID/ptrace/helper/runtime/output preflight。
- [ ] 处理目标 Python runtime mismatch：优先进入目标 runtime，否则输出
  `blocked/incompatible_python_runtime`。
- [ ] 允许一次有界 retry，保存 stdout/stderr、exit code、failure type 和产物缺失原因。
- [ ] 成功保存官方 `memray.bin`、stats/leaks 和结构化热点。
- [ ] 失败继续 runtime、RSS/smaps 和 source follow-up，不生成 Python retention。
- [ ] native live fallback 只能输出 `native_allocation_observation` partial。
- [ ] 禁止 gc referrer、objgraph、Pympler 或自研对象图替代 Memray。

### CF006 - Frontend main tree and diagnostic views

- [ ] 主图只接受 `session_main/renderable=true`，只用显式 lineage edge 布局。
- [ ] child snapshot、probe history、coarse summary edge 不进入主树骨架。
- [ ] orphan、duplicate ID、invalid relation 进入 data-quality 区，不静默挂 coarse。
- [ ] observation、line、mechanism、STOP、boundary 使用不同节点语义。
- [ ] blocked/partial/inconclusive 不显示为 rejected；只有 contradicted/rejected 灰化。
- [ ] 历史子树单独展示尝试、回退和停止原因，不改变当前主树。
- [ ] 候选诊断区展示 attempt、实际输出、初始证据、失败字段、门禁和保留父结论。

### CF007 - Verification, deployment and vulnerable-only run

- [ ] 补齐后端、前端、heap、line eligibility、candidate diagnostics 和 audit round-trip
  focused tests。
- [ ] 运行 `compileall`、`git diff --check`、focused pytest 和前端 production build。
- [ ] 提交并推送代码，不提交本地报告、临时回放、密钥或 VM 私有配置。
- [ ] Control 和 Worker1 拉取同一 commit 并 rebuild 对应 mini-drop/Agent 容器。
- [ ] Worker1 完成长寿命、未预加载 Memray 的 heap smoke，并保存真实产物或结构化边界。
- [ ] 用最新 Celery `run.json` 做只读离线回放，不读取 issue、PR、fixed、Oracle 或答案。
- [ ] 只运行 vulnerable-only Celery 600 秒真实 case；不运行 fixed、Oracle 或
  `evaluate_case.py`。
- [ ] 检查 producer barrier、诊断终态、主树父血缘、候选失败输出、line eligibility、
  heap outcome、fallback retained parent 和 abstention 一致性。

### CF Completion Criteria

```text
session_main 是唯一主布局输入
line/refinement/observation/mechanism/boundary 的父节点真实存在
概念 coarse ID 已归一化为 emitted coarse ID
无来源节点显示 orphan，不伪造树边
深探失败只产生局部 boundary 并继承父结论
AI 部分成功不会切换 Analyzer fallback
AI 全部失败时才生成 fallback，且不进入正式根因
门禁失败能关联初始证据和实际返回
line 探测失败有具体 eligibility 原因
Heap attach 不要求目标预加载，失败后诊断继续
前端主树、历史子树和数据质量区互不污染
vulnerable-only 600s case 可形成完整运行终态
```

## Task Group CG - Final Integrated Closure (Authoritative)

CG 是当前执行任务的唯一来源，覆盖前端树渲染、实时 Heap、AI 候选
失败诊断、Line 探测、父结论回退和 vulnerable-only 真实跑测。CE/CF
中的重复条目保留为历史记录；CG 完成前不得把方案标记为完成。

### CG001 - Lock the evidence, lineage and conclusion contracts

- [ ] CG001 [P] Update `server/app/rca/models.py` with the canonical fields and validation for `tree_kind`, `renderable`, `relation`, `parent_candidate_ids`, `origin_parent_candidate_id`, `line_anchor_eligibility`, `candidate_diagnostics`, `ai_gate_failures`, `heap_probe_outcome`, `retained_conclusion` and `data_quality`.
- [ ] CG002 [P] Update `server/app/rca/controlled_tree.py` and `server/app/diagnosis/session_conclusion.py` so one eligibility result drives formal root cause, clusters, causal chain, headline, confidence and abstention.
- [ ] CG003 [P] Add compatibility tests in `tests/test_rca.py`, `tests/test_session_conclusion.py` and `tests/test_diagnosis_orchestrator.py` for old reports containing `analyzer_fallback` and missing new fields.

### CG002 - Restore canonical backend parent lineage

- [ ] CG004 Build the emitted candidate index at the start of `_build_session_controlled_ai_tree()` in `server/app/diagnosis/orchestrator.py`.
- [ ] CG005 Map `coarse_insufficient_evidence` and other conceptual IDs to the current emitted coarse candidate before persisting any child node in `server/app/diagnosis/orchestrator.py`.
- [ ] CG006 Enforce real-parent resolution for `refinement`, `line_anchor`, `observation`, `mechanism`, `boundary`, `STOP` and object-call-chain nodes in `server/app/diagnosis/orchestrator.py`.
- [ ] CG007 Remove parent inference from `primary_nodes[0]`, index order, rank, global most-specific candidate and `root_entity` in `server/app/diagnosis/orchestrator.py`; unresolved nodes must become `missing_provenance/orphan` data-quality records.
- [ ] CG008 Allow `alternative/rejected_alternative` to attach to coarse only when the node explicitly declares an independent branch with a real emitted parent in `server/app/diagnosis/orchestrator.py`.
- [ ] CG009 Add regression cases in `tests/test_diagnosis_orchestrator.py` for real emitted coarse parents, conceptual coarse mapping, cross-branch isolation, duplicate IDs, orphan nodes and line parent hard failures.

### CG003 - Implement inherited fallback and local boundary semantics

- [ ] CG010 Update fallback construction in `server/app/diagnosis/orchestrator.py` and `server/app/diagnosis/session_conclusion.py` so blocked, failed, partial, inconclusive, target-exit and timeout probes create only a local boundary under `origin_parent_candidate_id`.
- [ ] CG011 Preserve the origin parent's claim, evidence refs, mechanism state and causal status in `retained_conclusion` in `server/app/diagnosis/session_conclusion.py`; do not create a new generic fallback claim.
- [ ] CG012 Allow rollback to move upward only when the parent itself is directly contradicted in `server/app/diagnosis/orchestrator.py` and `server/app/diagnosis/session_conclusion.py`; a failed child probe must not invalidate its parent.
- [ ] CG013 Make missing origin parent produce orphan plus abstention in `server/app/diagnosis/session_conclusion.py`, and keep `formal_root_cause`, clusters and causal chain empty.
- [ ] CG014 Add tests in `tests/test_session_conclusion.py` for child failure, parent contradiction, retained parent identity, boundary rendering and no-eligible-candidate output.

### CG004 - Make AI candidate failure explainable from initial evidence

- [ ] CG015 Persist every candidate-generation and investigation attempt in `server/app/rca/llm_client.py` with phase, attempt, bounded actual output, response hash, candidate counts, accepted/rejected/deferred/active IDs and retry state.
- [ ] CG016 Persist the exact initial evidence context, valid and missing evidence refs, evidence statuses and emitted parent IDs used by each attempt in `server/app/rca/llm_client.py`.
- [ ] CG017 Emit field-level `gate_checks` for source, candidate ID, evidence refs, target/window, supported level, mechanism, causal status, decision, required probe, parent existence, origin parent and causal chain in `server/app/diagnosis/session_conclusion.py`.
- [ ] CG018 Preserve valid sibling candidates when one candidate fails in `server/app/rca/llm_client.py`; cap active candidates only for probe scheduling; create `analyzer_fallback` only after all AI candidates and retries are unusable.
- [ ] CG019 Keep fallback as `unknown + partial_localization + unproven + ineligible` and prevent it from entering formal clusters in `server/app/diagnosis/orchestrator.py`.
- [ ] CG020 Add backend tests in `tests/test_diagnosis_orchestrator.py`, `tests/test_session_conclusion.py` and the existing LLM test module covering valid sibling retention, malformed candidate fields, retry exhaustion and evidence-based gate explanations.

### CG005 - Close runtime quality and Line probe promotion

- [ ] CG021 Add `sample_quality` production and normalization in `agent/mini_drop_agent/collectors/pyspy.py` and the runtime-profile consumer; classify idle loop, primitive frame, framework loop and target-code ratios.
- [ ] CG022 Prevent low-quality poll/select/epoll/sleep/futex and single-tail samples from creating function, call-path or line candidates in `agent/mini_drop_agent/collectors/pyspy.py` and `server/app/diagnosis/orchestrator.py`; retain them as observations with a reason.
- [ ] CG023 Make evidence-family mapping in `server/app/diagnosis/orchestrator.py` reject unknown probe families with structured diagnostics instead of creating non-executable tasks.
- [ ] CG024 Implement and persist line-anchor eligibility for revision, file, positive line, runtime/source match, source hash, time window and real parent in `server/app/diagnosis/orchestrator.py`.
- [ ] CG025 Emit a source/line boundary under the actual origin parent when `source_snapshot` is valid but line eligibility fails in `server/app/diagnosis/orchestrator.py`; do not treat source readability as line proof.
- [ ] CG026 Permit source mechanism and object-call-chain probes only below a verified line anchor in `server/app/diagnosis/orchestrator.py` and `server/app/rca/llm_client.py`, and bind every result to the same candidate and origin parent.
- [ ] CG027 Add focused line and runtime-quality tests in `tests/test_diagnosis_orchestrator.py` and `tests/test_session_conclusion.py`.

### CG006 - Make Heap collection genuinely live and diagnosable

- [ ] CG028 Keep Memray live attach as the Python heap primary path and ensure the target process does not preload Memray in `agent/mini_drop_agent/collectors/python_heap.py`.
- [ ] CG029 Split `deploy/collectors/memray_live/memray_attach_helper.py` into observable preflight, runtime staging, attach-control and artifact-finalization phases.
- [ ] CG030 Record PID/namespace/UID/ptrace/runtime/helper/output preflight, staged module paths, attach method, bounded stdout/stderr, exit code, process-group timeout and artifact state in `deploy/collectors/memray_live/memray_attach_helper.py` and `agent/mini_drop_agent/collectors/python_heap.py`.
- [ ] CG031 Resolve target Python runtime and Memray native module compatibility before attach in `deploy/collectors/memray_live/memray_attach_helper.py`; classify mismatch, namespace, permission, target-exit, control-channel timeout and missing-artifact failures separately.
- [ ] CG032 Keep one bounded retry, kill the entire helper process group on timeout, and never report Heap success without a valid official `memray.bin` or parseable official stats/leaks in `agent/mini_drop_agent/collectors/python_heap.py`.
- [ ] CG033 Preserve RSS/smaps/runtime/source follow-up after Heap failure in `server/app/diagnosis/orchestrator.py`; native live fallback may only emit `native_allocation_observation` partial and must not claim Python retention or a line root cause.
- [ ] CG034 Keep PyHeap as an explicit optional post-mortem/deep probe only in `agent/mini_drop_agent/collectors/python_heap_reference.py`; do not use tracemalloc, objgraph, `gc.get_referrers`, Pympler or a custom object graph as the default live replacement.
- [ ] CG035 Extend `tests/test_python_heap_collector.py` and add helper-stage tests for no-preload attach, runtime mismatch, namespace/permission block, control timeout, retry, target exit, missing artifact and native partial fallback.

### CG007 - Separate frontend main tree, history and diagnostics

- [ ] CG036 Update `web/src/components/diagnosis/aiTreeGraphModel.js` so only `session_main/renderable=true` enters the primary layout and only explicit canonical lineage edges are layout edges.
- [ ] CG037 Treat coarse summary/probe edges as annotations in `web/src/components/diagnosis/aiTreeGraphModel.js`, keep rollback/boundary edges out of the main hierarchy, and remove any index-0 or layer-order parent inference.
- [ ] CG038 Keep `child_snapshot`, `probe_history`, orphan, duplicate ID and invalid-relation records outside the main tree in `web/src/components/diagnosis/ControlledAITreeGraph.jsx` while exposing them in dedicated audit/data-quality sections.
- [ ] CG039 Render `line_anchor`, `observation`, `mechanism_explanation`, `stop_boundary`, `rejected_candidate` and orphan with distinct semantics in `web/src/components/diagnosis/ControlledAITreeGraph.jsx`; only contradicted/rejected use grey rejected styling.
- [ ] CG040 Add the candidate diagnostics view in `web/src/pages/AIDiagnosis.jsx` for actual AI output, initial evidence, field failures, gate checks, line eligibility, heap outcome, retained parent and abstention.
- [ ] CG041 Add/extend `web/src/components/diagnosis/aiTreeGraphModel.test.js` and diagnosis-page fixtures for cross-branch parents, history isolation, coarse summary edges, orphan visibility, local STOP and candidate failure explanations.

### CG008 - Persist audit data and provide offline replay

- [ ] CG042 Update `server/app/diagnosis/audit_bundle.py` and API serialization to preserve Analyzer facts, AI attempts, actual output summary, initial evidence, gate failures, probe outcomes, parent validation, line eligibility, Heap outcome, tree kind, data quality, retained conclusion and final eligibility.
- [ ] CG043 Bind every follow-up result to diagnosis ID, parent task ID, candidate ID, origin parent, evidence family, probe ID and fingerprint in `server/app/diagnosis/audit_bundle.py` and `server/app/diagnosis/orchestrator.py`.
- [ ] CG044 Update `docs/real_cases/celery_8882/replay_original_evidence.py` to read only the latest vulnerable `run.json` and explain why each candidate/gate/line/Heap step passed or failed from the saved initial evidence.
- [ ] CG045 Ensure replay in `docs/real_cases/celery_8882/replay_original_evidence.py` never reads issue, PR, fixed revision, Oracle, evaluator answer or hidden test labels, and never mutates the original report.

### CG009 - Validate, deploy and run the ordinary real case

- [ ] CG046 Run focused backend, frontend, Heap, line-eligibility, candidate-diagnostic and audit round-trip tests from `tests/` and `web/`, plus `compileall`, `git diff --check` and frontend production build.
- [ ] CG047 Review the repository diff at the repository root and commit only source, tests and required plan artifacts; exclude `reports/`, temporary replay files, credentials and VM-private configuration.
- [ ] CG048 Push one commit, then have Control and Worker1 pull the exact commit and rebuild the corresponding mini-drop/Agent containers using the VM deployment scripts under `deploy/`.
- [ ] CG049 Run Worker1's long-lived non-preloaded Python target Heap smoke using the real Worker1 Agent container and the deployment configuration; record official Memray artifacts or a structured capability boundary before the case run.
- [ ] CG050 Run the ordinary Celery case through `docs/real_cases/celery_8882/run_case_vm.py` for vulnerable-only 600 seconds with real Redis, worker and native `apply_async()` producer; do not run fixed, Oracle or `evaluate_case.py`.
- [ ] CG051 Verify the saved report under `reports/eval/real-open-source/` contains producer barrier, diagnosis terminal state, main-tree lineage, candidate diagnostics, line eligibility, Heap outcome, retained parent, history/data-quality records and consistent abstention fields.

### CG Completion Criteria

```text
session_main is the only primary layout input
every refinement/line/observation/mechanism/boundary parent is a real emitted node
conceptual coarse IDs are normalized before persistence
unresolved provenance is visible as orphan/data quality
deep probe failure creates a local boundary and retains the source parent claim
AI partial success keeps valid siblings; full failure alone creates fallback
AI output and initial evidence explain every gate failure
line promotion requires verified file:line eligibility
mechanism and object-call-chain nodes are below verified line
Memray live attach does not require target preload
Heap failure is structured and does not stop runtime/source follow-up
main tree, history, probe edges and data quality do not contaminate each other
formal conclusion fields share one eligibility result
ordinary real-case validation is vulnerable-only 600 seconds
```

## Task Group CH - Frozen Evidence Gap Closure (Reuse Existing Implementation)

CH 不是重写冻结证据链路。当前工作区已经有一部分实现，后续任务必须先复用
并验证现有代码；只有测试证明存在缺口时才补最小改动。已确认的现有实现包括：

- `server/app/diagnosis/schemas.py` 已有 `live_collection|frozen_evidence`、
  `evidence_package_id`、`evidence_cohort_id` 和 `source_incident_id` 字段；
- `server/app/main.py` 已有 WatchIncident 冻结现场分析入口和
  `_run_watch_incident_analysis()`，直接消费 `structured_evidence`；
- `server/app/diagnosis/orchestrator.py` 已在初始计划、单探针调度、延迟调度、
  follow-up 计划和 schedulable 判断处加入冻结模式拦截；
- `server/app/diagnosis/watch_runtime.py` 已阻止冻结分析后的 Watch follow-up；
- 冻结分析结果已记录 `diagnosis_mode`、`evidence_package_id`、
  `evidence_cohort_id`、`same_window`、`rolling_snapshot`、`probe_count=0` 和
  `evidence_package_exhausted`；
- `docs/superpowers/specs/2026-08-23-frozen-evidence-diagnosis-design.md`
  已记录本次设计边界。

以下已存在项标记为完成，仅代表“代码已存在”，不代表测试和审计闭环已经完成。

### CH001 - Define the two-mode diagnosis contract

- [x] CH001 [P] Existing mode contract in `server/app/diagnosis/schemas.py` is limited to `live_collection|frozen_evidence`; do not add `hybrid` or a second mode enum.
- [x] CH002 Existing `server/app/diagnosis/orchestrator.py` persists the mode and source package/cohort references in `target_scope`; retain this contract instead of adding a parallel session model.
- [x] CH003 Add request and backward-compatibility tests in `tests/test_diagnosis_orchestrator.py` and `tests/test_server_api.py` proving old requests remain `live_collection`, `hybrid` is rejected, and existing mode fields survive serialization.

### CH002 - Route WatchIncident analysis through frozen evidence

- [x] CH004 Existing `server/app/main.py` routes `POST /api/v1/watch-incidents/{incident_id}/analyze` to the frozen analysis executor; do not route it back through `DiagnosisOrchestrator.create()`.
- [x] CH005 Existing `_run_watch_incident_analysis()` in `server/app/main.py` consumes the saved `structured_evidence` and preserves `rolling_snapshot` plus `same_window`; do not create `frozen_evidence.py` unless a real shared caller is found.
- [x] CH006 Add API regression coverage in `tests/test_server_api.py` proving a `freeze_only` incident can be analyzed after the window ends, retains the original evidence cohort, reports `diagnosis_mode=frozen_evidence`, and creates no live collector task.

### CH003 - Resolve AI evidence requests inside the package

- [x] CH007 First verify the existing `StructuredEvidence`/Analyzer path in `server/app/main.py`, `server/app/diagnosis/evidence_structurer.py`, and `server/app/rca/attribution.py` already consumes all evidence present in a WatchIncident package; do not introduce a new resolver or duplicate evidence model unless a failing test demonstrates that an in-package family is ignored.
- [x] CH008 If CH007 identifies a real gap, add only the smallest mapping or audit metadata needed to distinguish package evidence that is `valid`/`partial` from package evidence that is missing; reuse existing `evidence_ref`, `evidence_status`, `reuse_status`, and controlled-tree probe-result contracts.
- [x] CH009 Add focused tests in the existing Watch/diagnosis test modules, or a new `tests/test_frozen_evidence.py` only if no existing module is suitable, covering package-hit, partial, wrong target/window, invalid status, duplicate record, and deterministic missing-request behavior.

### CH004 - Stop frozen analysis when the package is exhausted

- [x] CH010 Existing frozen Watch analysis in `server/app/main.py` records `missing_evidence`, `qualification_boundary`, and `stop_reason=evidence_package_exhausted` without scheduling a collector.
- [x] CH011 Verify the existing AI-tree boundary and conclusion qualification in `server/app/main.py`, `server/app/rca/models.py`, and `server/app/diagnosis/session_conclusion.py`; patch only a failing promotion or parent-retention case, and do not create a second stop-reason pipeline.
- [x] CH012 Add end-to-end tests in `tests/test_server_api.py`, `tests/test_diagnosis_orchestrator.py`, and `tests/test_session_conclusion.py` proving package exhaustion ends with `insufficient_evidence` or `partial_completed`, no root-cause upgrade, and stable missing-evidence output.

### CH005 - Enforce the no-new-probe gate

- [x] CH013 Existing initial scheduling guards in `server/app/diagnosis/orchestrator.py` cover `_plan_and_schedule()`, `_schedule_probe()`, and deferred scheduling; do not add duplicate guards.
- [x] CH014 Existing follow-up guards in `server/app/diagnosis/orchestrator.py` and `server/app/diagnosis/watch_runtime.py` cover `_plan_followup_requests()`, `_has_schedulable_followup_work()`, and `schedule_followup_tasks()`.
- [x] CH015 Add regression assertions in `tests/test_diagnosis_orchestrator.py` and `tests/test_watch_runtime.py` proving the existing guards prevent initial probes, delayed follow-ups, approval items, and background rescheduling in frozen mode.

### CH006 - Preserve auditability and test-package output

- [x] CH016 Existing WatchIncident serialization in `server/app/main.py` already records mode, package/cohort identity, reused refs, missing gaps, stop reason, and probe count; do not duplicate those fields there.
- [x] CH017 Extend only the existing audit export path in `server/app/diagnosis/audit_bundle.py` or add a thin WatchIncident-to-audit adapter, so frozen results are exportable without rerunning analysis or pretending the synthetic rolling-snapshot task is a live collector task.
- [x] CH018 Add audit-bundle tests in `tests/test_server_api.py`, `tests/test_sql_repository.py`, and the relevant Watch tests proving frozen results retain same-window evidence refs, package identity, missing gaps, stop reason, and `probe_count=0`.
- [x] CH019 Update `docs/ai_ops_v2_test/AI_OPS_V2_FULL_TEST_RUNBOOK_CN.md` and the relevant test-package README under `docs/运维Agent多赛道评测最终交付-20260822/` only where the current instructions do not already describe the existing frozen Watch flow; do not document a new collector or a hybrid mode.

### CH007 - Validate the focused increment

- [x] CH020 Run focused frozen-evidence, Watch, diagnosis-orchestrator, session-conclusion, audit-bundle, and evidence-structurer tests, then run `python -m compileall server tests`.
- [x] CH021 Run `git diff --check` and verify ordinary `live_collection` diagnosis behavior is unchanged, including initial probe selection, `auto_execute_policy`, and existing delayed follow-up tests.

### CH Completion Criteria

```text
existing ordinary diagnosis requests default to live_collection
hybrid is rejected and is not added as a second mode contract
existing WatchIncident analysis uses the saved rolling snapshot as same-window evidence
existing guards create zero initial/follow-up probes for frozen analysis
package-hit and package-miss semantics are proven by tests before any resolver code is added
missing package evidence produces a bounded stop without recollection
the existing frozen result is exported through the current audit contract or a thin adapter
live_collection behavior and delayed follow-up tests remain unchanged
```

## Dependencies for Task Group CH

- Existing CH001-CH002, CH004-CH005, CH010, CH013-CH014 are already implemented and must not be reimplemented.
- CH003 and CH006 validate the existing contracts before further changes.
- CH007-CH009 determine whether package resolution needs any code at all.
- CH011-CH012 verify bounded conclusions and only patch a demonstrated defect.
- CH015 verifies the existing no-new-probe guards.
- CH017-CH019 close the actual audit/export gap and documentation gap.
- CH020-CH021 are final validation and regression gates.

## Parallel Opportunities for Task Group CH

- CH003, CH006, and CH015 can be prepared in parallel because they validate separate existing paths.
- CH007 and CH009 can proceed in parallel after the current structured-evidence contract is reviewed.
- CH017 and CH019 can proceed in parallel after the existing result shape is confirmed.

## Implementation Strategy for Task Group CH

### MVP

1. Treat existing CH001-CH002, CH004-CH005, CH010, CH013-CH014 as already implemented.
2. Complete CH003, CH006, and CH015 to prove those paths without rewriting them.
3. Complete CH007-CH012 to distinguish package hits from package misses and preserve bounded conclusions.
4. Complete CH017-CH019 so the existing frozen result can enter the evaluation audit package.

### Incremental Delivery

1. Keep `live_collection` as the compatibility default.
2. Reuse the existing `frozen_evidence` WatchIncident path.
3. Test package-hit and package-miss behavior before adding code.
4. Add only the missing audit adapter or boundary fix.
5. Run CH020-CH021 before considering the first frozen-evidence evaluation ready.

---

## Task Group CI - Canonical Claim Lineage and Probe Plan Closure (Authoritative)

本任务组对应
`docs/superpowers/specs/2026-08-23-canonical-claim-lineage-probe-plan-design.md`。
它是本次“结论语义问题、父子重复 claim、探针调度覆盖和完整 probe input 丢失”范围的唯一执行口径。

本任务组不删除历史 CG/CH 记录。与本任务组目标重复的未完成 CG 任务以 CI 的 canonical contract、状态 reducer 和测试结果为准；已存在实现先复用，只有失败测试证明存在缺口时才修改。

### CI001 - Lock canonical claim and candidate contracts

- [X] CI001 [P] Add `generated_by`, `claim_origin`, `claim_transform`, `claim_status`, `claim_hash`, `source_claim_hash`, `source_candidate_id`, `source_round` and `source_event_id` to the candidate models in `server/app/rca/models.py`.
- [X] CI002 [P] Extend the existing tree/session schemas only with missing claim-lineage and probe-plan fields; reuse existing `tree_kind`, `renderable`, boundary and data-quality contracts in `server/app/diagnosis/schemas.py`.
- [X] CI003 [P] Add canonical lineage and candidate state fixture builders in `tests/conftest.py` or `tests/fixtures/canonical_lineage.py` without relying on report-specific IDs.
- [X] CI004 [P] Add contract tests for canonical node serialization and invalid parent/origin combinations in `tests/test_canonical_contract.py`.

### CI002 - Implement canonical claim lineage

- [X] CI005 Implement claim normalization, deterministic hashing and source mapping in `server/app/diagnosis/canonical_claim_lineage.py`.
- [X] CI006 Implement Analyzer claim registration for `rules.json` descriptions, `diagnostic_claim` and `summary` in `server/app/diagnosis/canonical_claim_lineage.py` and `server/app/rca/candidates.py`.
- [X] CI007 Implement AI proposal and AI update claim registration in `server/app/diagnosis/canonical_claim_lineage.py` and `server/app/rca/llm_client.py`; an update without `claim` must preserve the existing lineage.
- [X] CI008 Extend the existing retained-parent and qualification-boundary builders with canonical claim lineage and boundary-message metadata in `server/app/diagnosis/canonical_claim_lineage.py` and `server/app/diagnosis/session_conclusion.py`.
- [X] CI009 Add lineage unit tests in `tests/test_canonical_claim_lineage.py` covering Analyzer, AI proposal, AI update, retained, fallback, boundary and history-restore origins.

### CI003 - Implement canonical candidate state reducer

- [X] CI010 Extract the existing candidate normalization, DAG validation and conclusion-eligibility paths into the ordered reducer in `server/app/diagnosis/canonical_candidate_state.py` without duplicating orchestrator-only parent logic.
- [X] CI011 Consolidate existing coarse aliases, emitted-parent checks, orphan handling and data-quality records in `server/app/diagnosis/canonical_candidate_state.py`; add only missing canonical reducer outputs.
- [X] CI012 Reuse existing `tree_kind`, `renderable`, child-snapshot and frontend main-tree filters while making the canonical reducer the single producer of `session_main`, `probe_history` and `data_quality` outputs.
- [X] CI013 Add reducer tests in `tests/test_canonical_candidate_state.py` for current-round filtering, history isolation, real parent resolution, orphan handling and deterministic output ordering.

### CI004 - Enforce parent-child claim specificity

- [X] CI014 Implement `validate_claim_refinement(parent, child)` with normalized claim equality, mechanism, target, supported-level, localization-object and causal-chain specificity checks in `server/app/diagnosis/canonical_candidate_state.py`.
- [X] CI015 Convert same-claim child updates into parent evidence updates or probe annotations, and emit `duplicate_claim` or `refinement_not_more_specific` data-quality records instead of adding them to `session_main` in `server/app/diagnosis/canonical_candidate_state.py`.
- [X] CI016 Add focused specificity tests in `tests/test_canonical_candidate_state.py` for exact duplicates, punctuation-only changes, evidence-only changes, mechanism refinement, target refinement and localization refinement.
- [X] CI017 Add regression fixtures and assertions in `tests/test_diagnosis_orchestrator.py` proving emitted pairs `001 -> 004`, `002 -> 005` and `003 -> 006` cannot contain identical normalized claims.

### CI005 - Integrate canonical state into AI and orchestration

- [X] CI018 Preserve the already implemented session-level AI review fields and remove only the misleading layer/node `generated_by=ai_guarded` relabeling in `server/app/rca/llm_client.py` and `server/app/diagnosis/orchestrator.py`.
- [X] CI019 Route the existing Analyzer candidate, AI proposal and candidate-update paths through the canonical claim lineage and candidate reducer in `server/app/diagnosis/orchestrator.py`.
- [X] CI020 Replace the existing distributed `session_main` assembly with canonical candidate-state output while preserving current child-snapshot, renderable and data-quality behavior in `server/app/diagnosis/orchestrator.py`.
- [X] CI021 Extend the existing retained-conclusion and qualification-boundary paths in `server/app/diagnosis/session_conclusion.py` so retained claims carry canonical lineage and boundaries never become formal claims.
- [X] CI022 Add orchestration integration tests in `tests/test_diagnosis_orchestrator.py` for AI guard success without new claim text, AI proposal with new claim text, valid sibling retention, history exclusion and child-probe failure retaining the parent.

### CI006 - Rebuild canonical probe planning and input merge

- [X] CI023 Extend the existing `query_spec_hash` validation and duplicate-query suppression into canonical probe request normalization in `server/app/diagnosis/canonical_probe_plan.py`, adding target/window dimensions only where absent.
- [X] CI024 Fix the existing investigation-review replacement at `server/app/diagnosis/orchestrator.py` so initial and investigation evidence families use union semantics; preserve the already existing initial-review union path.
- [X] CI025 Implement field-level probe input merge where non-empty complete query data wins over abbreviated provenance and conflicting candidate/origin values produce explicit conflict records in `server/app/diagnosis/canonical_probe_plan.py`.
- [X] CI026 Reuse the existing `source_mechanism_query` readiness and `query_spec_hash` validation, then make canonical probe-plan normalization preserve the complete investigation query before scheduling in `server/app/diagnosis/canonical_probe_plan.py`.
- [X] CI027 Replace first-write-wins `_merge_probe_input_maps()` behavior in `server/app/diagnosis/orchestrator.py` with canonical probe-plan normalization and conflict diagnostics.
- [X] CI028 Add probe-plan tests in `tests/test_canonical_probe_plan.py` for family union, complete-query replacement, query-hash retention, identical request deduplication, conflicting query variants and unknown-family rejection.
- [X] CI029 Add orchestrator regression tests in `tests/test_diagnosis_orchestrator.py` proving `source_mechanism_query` no longer fails with `followup_probe_input_missing` when the investigation review supplies the complete query.

### CI007 - Separate retained conclusions, boundaries and localization output

- [X] CI030 Extend the existing retained/fallback construction in `server/app/diagnosis/session_conclusion.py` so inherited claims retain source candidate identity, canonical claim origin, evidence refs and causal status while boundary nodes use only `boundary_message`.
- [X] CI031 Extend the existing explicit-parent localization-chain construction in `server/app/diagnosis/orchestrator.py` with claim-hash deduplication, inherited-claim suppression and boundary stop steps.
- [X] CI032 Add session-conclusion tests in `tests/test_session_conclusion.py` for retained-parent identity, local boundary rendering, no-eligible-candidate abstention and non-contradiction after child probe failure.
- [X] CI033 Add localization-chain regression tests in `tests/test_diagnosis_orchestrator.py` proving parent-child duplicate claims are displayed once and boundary messages do not become root-cause claims.

### CI008 - Update frontend and audit contracts

- [X] CI034 Verify and extend the existing `session_main/renderable=true` and explicit-parent graph filtering in `web/src/components/diagnosis/aiTreeGraphModel.js` to consume canonical lineage fields and keep history/probe/data-quality records outside the main layout.
- [X] CI035 Extend the existing boundary, rejected, orphan and history rendering in `web/src/components/diagnosis/ControlledAITreeGraph.jsx` with active/inherited/duplicate claim semantics; remove remaining source inference from `generated_by` and node color.
- [X] CI036 Extend the existing AI review, retained-conclusion and gate-diagnostic views in `web/src/pages/AIDiagnosis.jsx` with claim origin, claim transform, duplicate-claim diagnostics and probe conflicts.
- [X] CI037 Extend `web/src/components/diagnosis/aiTreeGraphModel.test.js` and diagnosis fixtures for duplicate-claim exclusion, history isolation, orphan visibility, boundary rendering and explicit parent edges.
- [X] CI038 Extend the existing audit/API serialization of AI review, gate failures, retained conclusions and boundaries in `server/app/diagnosis/audit_bundle.py` with canonical claim lineage, candidate state, probe plan and probe conflicts.
- [X] CI039 Add audit/API round-trip tests in `tests/test_server_api.py`, `tests/test_sql_repository.py` and `tests/test_diagnosis_orchestrator.py` for canonical fields and data-quality records.

### CI009 - End-to-end validation and real-report acceptance

- [X] CI040 Add an end-to-end canonical-tree fixture test in `tests/test_canonical_lineage_e2e.py` covering Analyzer input, AI proposal, investigation review, probe merge, failed child probe, retained parent, boundary and final serialization.
- [X] CI041 Extend the existing offline replay in `docs/real_cases/celery_8882/replay_original_evidence.py` to assert no duplicate parent-child claims, no historical nodes in `session_main` and no lost `query_spec_hash`.
- [X] CI042 Run focused backend tests for canonical contract, lineage, candidate state, probe plan, orchestrator and session conclusion in `tests/`.
- [X] CI043 Run frontend tests, production build, `python -m compileall server tests` and `git diff --check` after the canonical integration is complete.
- [X] CI044 Inspect the latest real diagnosis report and record the status of `001 -> 004`, `002 -> 005`, `003 -> 006`, localization-chain deduplication and probe-family union in the task completion evidence.
- [X] CI045 Update the canonical design and feature task cross-references only if implementation names or validation commands changed, without reintroducing legacy claim semantics.

### CI Completion Evidence - 2026-08-23

```text
focused backend: 346 passed
full backend: 800 passed
frontend graph model: 22 passed
frontend production build: passed
python compileall: passed
git diff --check: passed

latest inspected report:
reports/eval/real-open-source/celery-8882-vulnerable-600s-20260823/run.json
source revision: a83070e5ec748c32325332db422756cfdd709aae
historical report input: 001->004, 002->005, 003->006 still contain duplicate claim text
current canonical reducer replay: 004/005/006 are excluded from emitted session_main and recorded as duplicate_claim
history_in_session_main: none
localization-chain canonical fixture: consecutive duplicate claim suppressed and boundary rendered as a stop step
probe-family union/query hash: covered by canonical unit, orchestrator and end-to-end tests
real report probe plan: absent because the inspected report predates canonical_probe_plan; no live diagnosis was rerun
```

### CI Completion Criteria

```text
every current node has claim_origin, claim_transform and claim_status
generated_by no longer implies AI-generated claim text
refinement children cannot equal the normalized parent claim
same-claim updates merge into the parent or remain probe annotations
fallback retains the actual source parent and creates only a local boundary
history, probe_history and data_quality never enter session_main
localization_chain has no consecutive duplicate claim text
initial and investigation probe families use union semantics
complete ai_generated_query and query_spec_hash survive merge
formal report, frontend tree and probe scheduling consume canonical state
real report confirms 001->004, 002->005 and 003->006 are resolved
```

## CI Multi-Branch Conclusion Synthesis

- [X] CI046 Add regression fixtures for distinct sibling causes with mixed `line` and `function` support levels.
- [X] CI047 Add one canonical DAG frontier selector that collapses selected ancestors while preserving distinct sibling endpoints.
- [X] CI048 Reuse the existing formal eligibility guard when deriving all qualified sibling root-cause clusters, including qualified nodes previously left with `role=unknown`.
- [X] CI049 Preserve each cluster and explanation step's own `supported_level` instead of replacing it with one session-wide level.
- [X] CI050 Reuse `SessionConclusionReview.localization_chain` for deepest evidence-backed non-formal branch findings.
- [X] CI051 Require session review roles and causal steps to cover every formal primary/contributing branch with matching candidate IDs, evidence and support levels.
- [X] CI052 Keep all qualified branches in deterministic fallback output when session-level AI review is unavailable.
- [X] CI053 Prefer the existing session review `headline` as the frontend summary and render related branch details without replacing them with one retained claim.
- [X] CI054 Extend the existing root-cause cluster detail view with per-branch localization depth and tree linkage.
- [X] CI055 Run focused backend, orchestrator and frontend model regressions for multi-branch convergence.
- [X] CI056 Run full backend tests, frontend production build, compileall and diff validation.

### Multi-Branch Completion Evidence - 2026-08-23

```text
multi-branch/session/orchestrator focused backend: 191 passed
full backend: 807 passed
frontend graph model: 22 passed
frontend production build: passed
python compileall: passed
git diff --check: passed

covered behavior:
- distinct sibling causes survive with independent line/function support levels
- a deeper formal child replaces only its same-branch formal ancestor
- qualified role=unknown siblings normalize to secondary instead of disappearing
- session review roles cover every eligible cluster
- causal steps retain matching candidate IDs, evidence and support levels
- non-formal deep siblings remain visible through localization_chain
- fallback keeps every qualified formal branch when session AI is unavailable
- frontend summary prefers the integrated session headline
```

## CI Dependencies and Execution Order

```text
CI001-CI004 contract and fixtures
  -> CI005-CI009 claim lineage
  -> CI010-CI013 candidate reducer
  -> CI014-CI017 specificity gate
  -> CI018-CI022 AI/orchestrator integration
  -> CI023-CI029 canonical probe plan
  -> CI030-CI033 retained/boundary/localization
  -> CI034-CI039 frontend and audit
  -> CI040-CI045 end-to-end validation and acceptance
```

The first independently usable increment is:

```text
CI001-CI017
```

It proves canonical claim origins, real parent lineage and duplicate-claim rejection before changing live probe scheduling.

## CI Parallel Opportunities

- CI001-CI004 can run in parallel because they touch separate contract and fixture files.
- CI005-CI009 can be split between lineage implementation and lineage tests after CI001.
- CI010-CI013 can proceed alongside CI005-CI009 after the model contract is fixed.
- CI023-CI026 and CI028 can proceed in parallel after the canonical probe request shape is agreed.
- CI034-CI037 can proceed in parallel with CI038-CI039 after backend payload fields are stable.
- CI042 and CI043 can run in parallel once CI040-CI041 are complete.

## CI Implementation Strategy

### MVP

1. Complete CI001-CI004 to lock the contract.
2. Complete CI005-CI017 to normalize claim lineage, parents and duplicate refinement behavior.
3. Stop and validate the three known duplicate pairs before changing probe scheduling.

### Incremental Delivery

1. Complete CI018-CI022 and remove layer-level AI guard relabeling.
2. Complete CI023-CI029 and validate initial/investigation probe union and complete query preservation.
3. Complete CI030-CI033 and unify retained, boundary and localization semantics.
4. Complete CI034-CI039 and expose the canonical state in frontend and audit payloads.
5. Complete CI040-CI045 with focused tests, build checks and latest real-report acceptance.

## CI Implementation Status Review - 2026-08-23

本次审查基于当前工作区代码、现有测试和最近提交完成。结论不是把现有逻辑直接标记为 CI 已完成，而是区分“可复用基础”和“本次仍需补齐的 canonical 缺口”。

### Already Implemented and Must Be Reused

- Session-level AI review metadata 已存在于 `server/app/rca/llm_client.py`、`server/app/diagnosis/orchestrator.py` 和 `server/app/diagnosis/session_conclusion.py`，包括 status、scope、attempts、model 和 error；对应测试已存在于 `tests/test_rca.py`、`tests/test_diagnosis_orchestrator.py` 和 `tests/test_session_conclusion.py`。CI018 只需移除错误的 layer/node relabeling，不要再创建第二套 AI review 状态。
- `ControlledAITree` 已有 `tree_kind`、`renderable`、`data_quality`、`retained_candidate_id`、`localization_chain` 和 child-snapshot 语义，位置在 `server/app/rca/models.py` 和 `server/app/diagnosis/orchestrator.py`。CI002、CI012、CI020 应扩展并集中生产这些字段，不应重复定义。
- emitted parent、概念 coarse alias、orphan、missing provenance、duplicate candidate ID 和显式 `origin_parent_candidate_id` 已在 `server/app/diagnosis/orchestrator.py`、`server/app/diagnosis/session_conclusion.py` 和 `web/src/components/diagnosis/aiTreeGraphModel.js` 中存在。CI010-CI013、CI034-CI035 应提取或复用这些路径。
- retained conclusion、qualification boundary、child probe failure 后回退父节点和 retained pointer 同步已经实现，相关测试包括 `tests/test_session_conclusion.py` 和 `tests/test_diagnosis_orchestrator.py`。CI008、CI021、CI030、CI032 只补 canonical claim lineage 和重复展示语义。
- AI 候选生成失败后的 fallback、合法兄弟候选保留、候选门禁诊断和 session-level AI readiness 已有实现。CI022、CI038、CI039 应围绕现有输出补字段和测试，不应重写 fallback 流程。
- `query_spec_hash` 的读取、source mechanism 输入 readiness 和相同 guarded query 去重已经存在于 `server/app/diagnosis/orchestrator.py`。CI023、CI026、CI028 应扩展既有逻辑，而不是重新实现 query hash 机制。
- 前端已经过滤非 `session_main` 或 `renderable=false` 的树，并使用显式父节点建立主树边；已有测试覆盖 child snapshot、orphan、duplicate candidate ID 和跨分支 parent。CI034-CI037 是 canonical claim 字段和展示语义的补充。
- audit bundle 已保留 AI review、candidate diagnostics、gate failures、retained conclusions、boundaries 和 controlled tree 摘要。CI038-CI039 只需补 canonical 字段和 probe-plan 冲突记录。
- `docs/real_cases/celery_8882/replay_original_evidence.py` 已存在并能读取候选、父节点、探针、line 和 heap 摘要。CI041 应扩展现有 replay，不新建第二个回放脚本。

### Partially Implemented and Still Requires Changes

- `generated_by=ai_guarded` 仍会在 `server/app/rca/llm_client.py` 和 `server/app/diagnosis/orchestrator.py` 对 layer 或 AI candidate 做提升。这解决了旧 readiness 语义，但仍无法表示 claim 是否由本轮 AI 新生成；CI018 是本次真实修复点。
- 候选已有 claim、mechanism、target、supported level 和 parent 校验，但没有 `claim_origin`、`claim_transform`、`claim_status`、claim hash，也没有父子 claim 文本相等门禁；CI001、CI005-CI017 仍是主要新增范围。
- 初始候选 review 已使用 union 合并 selected evidence families，但调查轮在 `server/app/diagnosis/orchestrator.py` 中仍用调查轮列表替换 `followup_requests`；CI024 是一个明确的现有 bug 修复。
- `_merge_probe_input_maps()` 已验证 candidate/origin provenance，但仍使用 first-write-wins；CI025、CI027、CI029 是另一个明确的现有 bug 修复。
- localization chain 已按真实 parent 追溯，但直接输出每个节点的 `claim`，尚未按 claim hash 去重，也没有 inherited/boundary 的独立展示语义；CI031-CI033 仍需要实现。

### Not Found in Current Implementation

- `canonical_claim_lineage.py`、`canonical_candidate_state.py` 和 `canonical_probe_plan.py` 当前不存在。
- claim 来源字段和 claim 变换字段当前不存在。
- `duplicate_claim`、`refinement_not_more_specific` 和 probe input conflict 的后端 canonical 记录当前不存在。
- 父子 claim 是否增加机制、目标或定位信息的统一 specificity gate 当前不存在。
- 首轮与调查轮合并后的 canonical probe plan 当前不存在。
- 针对三组已确认重复节点和完整 query 丢失的独立 canonical E2E fixture 当前不存在。

### Review Decision

CI 任务组保留，但执行时遵循以下调整：

```text
先复用现有 AI review、parent、fallback、boundary、session_main、audit 和 replay 逻辑
再新增 claim lineage、specificity gate 和 canonical probe plan
最后删除 layer-level ai_guarded relabeling，并将现有调用接入 canonical reducer
```

本审查没有将已有代码直接标记为 `[x]`，因为现有实现尚未满足新的 canonical contract；只将对应任务从“新增实现”修订为“复用并补齐”。
