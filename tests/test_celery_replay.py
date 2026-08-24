from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPLAY_PATH = ROOT / "docs" / "real_cases" / "celery_8882" / "replay_original_evidence.py"


def load_replay_module():
    spec = importlib.util.spec_from_file_location("celery_replay", REPLAY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evidence_index_accepts_scalar_summary_and_confidence_inputs():
    replay = load_replay_module()

    refs, families, statuses = replay._evidence_index([
        {
            "evidence_id": "ev-scalar-summary",
            "query_or_probe": "python_runtime_profile",
            "observed_value": {
                "summary": "runtime profile summary",
            },
            "data_quality": "partial",
        },
        {
            "evidence_id": "ev-scalar-confidence",
            "query_or_probe": "python_heap_profile",
            "observed_value": {
                "summary": {
                    "confidence_inputs": "valid",
                },
            },
        },
    ])

    assert refs == {"ev-scalar-summary", "ev-scalar-confidence"}
    assert families == {
        "ev-scalar-summary": "python_runtime_profile",
        "ev-scalar-confidence": "python_heap_profile",
    }
    assert statuses == {
        "ev-scalar-summary": "partial",
        "ev-scalar-confidence": "valid",
    }


def test_evidence_index_reads_family_status_from_structured_summary():
    replay = load_replay_module()

    _, _, statuses = replay._evidence_index([
        {
            "evidence_id": "ev-structured",
            "query_or_probe": "python_heap_profile",
            "observed_value": {
                "summary": {
                    "confidence_inputs": {
                        "evidence_validity_by_family": {
                            "python_heap_profile": "valid",
                        },
                    },
                },
            },
        },
    ])

    assert statuses == {"ev-structured": "valid"}
