from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.app.diagnosis.rolling_buffer import (
    RollingEvidenceBuffer,
    RollingSample,
    structure_snapshot_evidence,
)


def test_triggered_snapshot_freezes_pre_and_post_window_metadata():
    buffer = RollingEvidenceBuffer()
    trigger_at = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    target_key = "agent_1:4242"
    for offset in (-45, -20, 0, 15, 45):
        buffer.append(
            target_key,
            RollingSample(
                observed_at=trigger_at + timedelta(seconds=offset),
                family="metrics",
                payload={"avg_cpu_user_pct": 90 + offset},
            ),
        )

    snapshot = buffer.freeze_snapshot(
        target_key=target_key,
        trigger_event_id="evt_001",
        evidence_cohort_id="cohort_001",
        trigger_observed_at=trigger_at,
    )

    assert snapshot.metadata.trigger_event_id == "evt_001"
    assert snapshot.metadata.evidence_cohort_id == "cohort_001"
    assert snapshot.metadata.pre_window_start == trigger_at - timedelta(seconds=30)
    assert snapshot.metadata.post_window_end == trigger_at + timedelta(seconds=30)
    assert [sample.observed_at for sample in snapshot.samples] == [
        trigger_at + timedelta(seconds=-20),
        trigger_at,
        trigger_at + timedelta(seconds=15),
    ]


def test_frozen_snapshot_structures_as_rolling_snapshot_with_reference_artifacts():
    buffer = RollingEvidenceBuffer()
    trigger_at = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    target_key = "agent_1:4242"
    buffer.append(
        target_key,
        RollingSample(
            observed_at=trigger_at,
            family="metrics",
            payload={"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0},
        ),
    )
    buffer.append(
        target_key,
        RollingSample(
            observed_at=trigger_at + timedelta(seconds=5),
            family="stack",
            payload={
                "top_functions": [
                    {"name": "microservices_test.common.busy_cpu", "samples": 138, "percent": 72.4}
                ],
                "stack_samples": [
                    {
                        "hot_frame": "microservices_test.common.busy_cpu",
                        "sample_count": 138,
                        "percent": 72.4,
                        "call_path": "gateway;order;busy_cpu",
                    }
                ],
            },
        ),
    )

    snapshot = buffer.freeze_snapshot(
        target_key=target_key,
        trigger_event_id="evt_001",
        evidence_cohort_id="cohort_001",
        trigger_observed_at=trigger_at,
    )
    structured = structure_snapshot_evidence("task_snapshot", snapshot)

    assert structured.collection_mode == "rolling_snapshot"
    assert structured.trigger_event_id == "evt_001"
    assert structured.evidence_cohort_id == "cohort_001"
    assert structured.timing_relation == "same_window"
    assert structured.artifact_refs
    assert all(ref["raw_payload_policy"] == "references_only" for ref in structured.artifact_refs)
    assert structured.top_functions[0]["name"] == "microservices_test.common.busy_cpu"
    assert structured.stack_summary["raw_payload_policy"] == "references_only"
    assert structured.confidence_inputs["token_safety"] == "compact_summary_only"
