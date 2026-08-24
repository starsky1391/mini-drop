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


def test_active_lease_blocks_same_owner_reentry(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    reset_engine()
    init_db()

    store = DiagnosisStore()
    store.create_session({
        "diagnosis_id": "diag_lease_reentry",
        "creator_id": "tester",
        "raw_query": "lease reentry",
        "status": "COLLECTING",
        "policy_profile": "default",
        "model_version": "v1",
        "planner_version": "v1",
    })

    assert store.acquire_lease("diag_lease_reentry", "orchestrator", ttl_seconds=30)
    assert not store.acquire_lease("diag_lease_reentry", "orchestrator", ttl_seconds=30)
    assert not store.renew_lease("diag_lease_reentry", "other", ttl_seconds=60)
    assert store.renew_lease("diag_lease_reentry", "orchestrator", ttl_seconds=60)
    store.release_lease("diag_lease_reentry", "orchestrator")
    assert store.acquire_lease("diag_lease_reentry", "orchestrator", ttl_seconds=30)


def test_runner_control_is_persisted_in_diagnosis_session(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    reset_engine()
    init_db()

    store = DiagnosisStore()
    store.create_session({
        "diagnosis_id": "diag_runner_control",
        "creator_id": "tester",
        "raw_query": "runner release",
        "status": "PARTIAL_COMPLETED",
        "policy_profile": "default",
        "runner_control": {
            "release_requested": True,
            "reason": "diagnosis_settled",
            "released_at": "2026-08-24T00:00:00+00:00",
            "outstanding_probes": [],
        },
        "model_version": "v1",
        "planner_version": "v1",
    })

    detail = store.get_session("diag_runner_control")

    assert detail["runner_control"]["release_requested"] is True
    assert detail["runner_control"]["reason"] == "diagnosis_settled"
