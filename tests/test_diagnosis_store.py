"""DiagnosisStore 证据回连测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from server.app.diagnosis.store import DiagnosisStore
from server.app.database import init_db, reset_engine


def test_resolve_evidence_ref_matches_nested_evidence_index(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    reset_engine()
    init_db()

    store = DiagnosisStore()
    store.create_session({
        "diagnosis_id": "diag_nested_ref",
        "creator_id": "tester",
        "raw_query": "nested ref",
        "status": "OPEN",
        "policy_profile": "default",
        "model_version": "v1",
        "planner_version": "v1",
    })
    store.add_evidence({
        "evidence_id": "ev_nested",
        "diagnosis_id": "diag_nested_ref",
        "source_type": "task_artifact",
        "source_system": "collector",
        "target": {},
        "event_time_range": {},
        "ingestion_time": datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc),
        "query_or_probe": "depth_evidence_json",
        "derivation_version": "v1",
        "evidence_index": {
            "stack_samples": [
                {
                    "hot_frame": "compute_hotspot",
                    "call_path": "main;worker;compute_hotspot",
                    "stack_fragment": ["main", "worker", "compute_hotspot"],
                    "wait_reason": "cpu_hotspot",
                }
            ],
            "context": {
                "call_path": "main;worker;compute_hotspot",
                "endpoint": "/api/order/create",
            },
        },
        "integrity_hash": "hash-1",
    })

    matched = store.resolve_evidence_ref("diag_nested_ref", "evidence_index.stack_samples[0].call_path")

    assert matched is not None
    assert matched["evidence_id"] == "ev_nested"
