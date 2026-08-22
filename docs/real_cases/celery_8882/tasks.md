# Tasks: L4-CELERY-8882-EXCEPTION-MEMLEAK

**Input**: Design notes and case runtime files from `/docs/real_cases/celery_8882/`

**Prerequisites**: `run_case_vm.py`, `evaluate_case.py`, `celery_case_tasks.py`, `producer.py`, `worker_entrypoint.py`, `worker_monitor.py`

**Tests**: The case must be validated with focused collector, Analyzer, runner, and offline-oracle tests before the VM pair is accepted.

**Organization**: Tasks are grouped by user story so the case runner, runtime quality gate, Analyzer fallback, and offline oracle can be delivered and checked independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel when files do not overlap.
- **[Story]**: Which user story this task belongs to.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Add failing coverage for the Celery runtime-quality gate and the pair-run contract before changing behavior.

- [x] T001 [P] Add failing runtime-quality regression tests in `tests/test_pyspy_collector.py`, `tests/test_evidence_structurer.py`, and `tests/test_rca_attribution.py`
- [x] T002 [P] Add Celery pair-run regression tests in `tests/test_celery_real_case_runner.py` for vulnerable-only diagnosis, fixed no-diagnosis replay, and offline oracle consistency

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Add the shared runtime-quality metadata used by the Analyzer and the case runner.

- [x] T003 Implement `sample_quality`, `candidate_frames`, and true idle `filtered_idle_frames` in `agent/mini_drop_agent/collectors/pyspy.py`
- [x] T004 Thread py-spy quality metadata through `server/app/diagnosis/evidence_structurer.py` into `stack_summary`, `confidence_inputs`, and `evidence_index`
- [x] T005 Add runtime-profile quality extraction helpers in `server/app/rca/attribution.py`

**Checkpoint**: py-spy can now describe whether a runtime sample is diagnostic, framework-loop dominated, or idle-loop dominated.

---

## Phase 3: User Story 1 - 运行时质量门控与保守归因 (Priority: P1) 🎯 MVP

**Goal**: Low-quality runtime samples stay at observation/localization boundary; only medium/high-quality runtime evidence may promote function or line candidates.

**Independent Test**: An idle-loop heavy py-spy sample produces observation-only or process/call-path-level evidence, while a target-code-dominant sample can still promote to function or line when source context exists.

### Tests for User Story 1

- [x] T006 [P] [US1] Add Analyzer regression tests in `tests/test_rca_attribution.py` that keep low-quality runtime samples from promoting to function or line
- [x] T007 [P] [US1] Add collector/structurer assertions in `tests/test_pyspy_collector.py` and `tests/test_evidence_structurer.py` for low-quality sample classification and preserved metadata

### Implementation for User Story 1

- [x] T008 [US1] Gate top-function, context fallback, line, and call-path localization on runtime quality in `server/app/rca/attribution.py`
- [x] T009 [US1] Keep low-quality runtime samples as observations in `server/app/rca/attribution.py` without turning them into stronger root-cause candidates

**Checkpoint**: The Analyzer no longer upgrades idle-loop runtime noise into a final function or line conclusion.

---

## Phase 4: User Story 2 - Celery 真实 workload 与离线对照 (Priority: P2)

**Goal**: Keep the vulnerable stage as the only Analyzer diagnosis target, keep the fixed stage as a control replay only, and preserve a pair-wise workload comparison.

**Independent Test**: The runner emits one vulnerable diagnosis, the fixed stage has no diagnosis object, and the offline evaluator still verifies workload identity, runtime cleanliness, and rollback semantics.

### Tests for User Story 2

- [x] T010 [P] [US2] Extend `tests/test_celery_real_case_runner.py` so the pair contract covers runtime manifests, one diagnosis only, and fixed control replay
- [x] T011 [P] [US2] Extend `tests/test_celery_real_case_runner.py` or a nearby evaluator test to assert the offline oracle rejects fixed-stage diagnosis leakage

### Implementation for User Story 2

- [x] T012 [US2] Keep `docs/real_cases/celery_8882/run_case_vm.py` on the vulnerable-only diagnosis path and preserve the fixed control replay without Analyzer invocation
- [x] T013 [US2] Tighten `docs/real_cases/celery_8882/evaluate_case.py` so the fixed stage stays offline-only and the vulnerable/fixed pair is compared against the saved evidence bundle

**Checkpoint**: The Celery VM pair behaves as a real diagnosis/control pair, not as two independent Analyzer runs.

---

## Phase 5: Polish & Cross-Cutting Concerns

**Purpose**: Validate the complete Celery case end-to-end and record the accepted bundle.

- [x] T014 Run focused tests for `tests/test_pyspy_collector.py`, `tests/test_python_heap_collector.py`, `tests/test_evidence_structurer.py`, `tests/test_rca_attribution.py`, `tests/test_celery_real_case_runner.py`, and `tests/test_diagnosis_orchestrator.py`
- [ ] T015 Run the real Celery vulnerable-only 600s VM case and save the timestamped results under `reports/eval/real-open-source/celery-8882-*`
- [ ] T016 Optionally run `docs/real_cases/celery_8882/run_case_vm.py --with-fixed-control` and `evaluate_case.py` only when an explicit offline pair comparison is needed

---

## Dependencies & Execution Order

- Phase 1 starts first and should fail before the implementation.
- Phase 2 blocks all user-story work because the runtime-quality metadata must exist before Analyzer gating can use it.
- User Story 1 can start immediately after Phase 2 and is the MVP for the long-lived runtime-quality scheme.
- User Story 2 can start after Phase 2 and may be validated in parallel with User Story 1 once the shared metadata exists.
- Phase 5 depends on the implementation of both user stories and ends with the real VM acceptance record.

## Parallel Opportunities

- T001 and T002 can run in parallel.
- T003, T004, and T005 touch different layers and can be implemented in parallel once the tests are in place.
- T006 and T007 can run in parallel after the foundational metadata exists.
- T010 and T011 can run in parallel after the runner and evaluator contracts are stable.

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Add the failing tests for runtime quality.
2. Implement `sample_quality` in the py-spy collector.
3. Propagate the quality metadata into structured evidence.
4. Gate Analyzer upgrades on runtime quality so idle-loop samples stay conservative.

### Incremental Delivery

1. Land the runtime-quality gate first.
2. Lock the vulnerable/fixed Celery pair contract next.
3. Run the real Celery case and save the accepted bundle.
