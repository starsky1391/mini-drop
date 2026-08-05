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
