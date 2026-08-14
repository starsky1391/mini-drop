"""Rolling buffer and triggered snapshot support for transient evidence windows."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import os
from typing import Any
from uuid import uuid4

from pydantic import Field

from server.app.diagnosis.evidence_structurer import StructuredEvidence, structure_artifact_evidence
from server.app.diagnosis.schemas import StrictModel


class RollingBufferSettings(StrictModel):
    low_cost_metrics_retention_seconds: int = Field(default=300, ge=30, le=3600)
    endpoint_summary_retention_seconds: int = Field(default=300, ge=30, le=3600)
    trace_summary_retention_seconds: int = Field(default=300, ge=30, le=3600)
    stack_summary_retention_seconds: int = Field(default=120, ge=15, le=600)
    pre_window_seconds: int = Field(default=30, ge=1, le=300)
    post_window_seconds: int = Field(default=30, ge=1, le=300)


class RollingSample(StrictModel):
    observed_at: datetime
    family: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class SnapshotMetadata(StrictModel):
    snapshot_id: str
    trigger_event_id: str
    evidence_cohort_id: str
    target_key: str
    trigger_observed_at: datetime
    pre_window_start: datetime
    post_window_end: datetime
    window_start: datetime
    window_end: datetime
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)


class FrozenSnapshot(StrictModel):
    metadata: SnapshotMetadata
    samples: list[RollingSample] = Field(default_factory=list)


class RollingEvidenceBuffer:
    """In-memory rolling buffer for low-cost evidence families."""

    def __init__(self, settings: RollingBufferSettings | None = None) -> None:
        self.settings = settings or RollingBufferSettings()
        self._samples: dict[str, list[RollingSample]] = defaultdict(list)

    def append(self, target_key: str, sample: RollingSample) -> None:
        self._samples[target_key].append(sample)
        self._prune(target_key, sample.observed_at)

    def freeze_snapshot(
        self,
        *,
        target_key: str,
        trigger_event_id: str,
        evidence_cohort_id: str,
        trigger_observed_at: datetime,
    ) -> FrozenSnapshot:
        pre_start = trigger_observed_at - timedelta(seconds=self.settings.pre_window_seconds)
        post_end = trigger_observed_at + timedelta(seconds=self.settings.post_window_seconds)
        samples = [
            sample
            for sample in self._samples.get(target_key, [])
            if pre_start <= sample.observed_at <= post_end
        ]
        samples.sort(key=lambda item: (item.observed_at, item.family))
        snapshot_id = f"snap_{uuid4().hex[:12]}"
        metadata = SnapshotMetadata(
            snapshot_id=snapshot_id,
            trigger_event_id=trigger_event_id,
            evidence_cohort_id=evidence_cohort_id,
            target_key=target_key,
            trigger_observed_at=trigger_observed_at,
            pre_window_start=pre_start,
            post_window_end=post_end,
            window_start=samples[0].observed_at if samples else pre_start,
            window_end=samples[-1].observed_at if samples else post_end,
            artifact_refs=_artifact_refs(snapshot_id, samples),
        )
        return FrozenSnapshot(metadata=metadata, samples=samples)

    def _prune(self, target_key: str, now: datetime) -> None:
        retention = max(
            self.settings.low_cost_metrics_retention_seconds,
            self.settings.endpoint_summary_retention_seconds,
            self.settings.trace_summary_retention_seconds,
            self.settings.stack_summary_retention_seconds,
        )
        cutoff = now - timedelta(seconds=retention)
        self._samples[target_key] = [
            sample for sample in self._samples[target_key]
            if sample.observed_at >= cutoff
        ]


def structure_snapshot_evidence(task_id: str, snapshot: FrozenSnapshot) -> StructuredEvidence:
    """Convert a frozen rolling snapshot into the existing structured evidence contract."""
    values = _artifact_values(snapshot)
    return structure_artifact_evidence(
        task_id=task_id,
        artifacts=snapshot.metadata.artifact_refs,
        artifact_values=values,
        evidence_window={
            "trigger_event_id": snapshot.metadata.trigger_event_id,
            "evidence_cohort_id": snapshot.metadata.evidence_cohort_id,
            "collection_mode": "rolling_snapshot",
            "window_start": snapshot.metadata.window_start.isoformat(),
            "window_end": snapshot.metadata.window_end.isoformat(),
            "trigger_observed_at": snapshot.metadata.trigger_observed_at.isoformat(),
            "timing_relation": "same_window",
        },
    )


def _artifact_refs(snapshot_id: str, samples: list[RollingSample]) -> list[dict[str, Any]]:
    return [reference for reference, _ in snapshot_artifact_payloads(snapshot_id, samples)]


def snapshot_artifact_payloads(
    snapshot_id: str,
    samples: list[RollingSample],
) -> list[tuple[dict[str, Any], bytes]]:
    """Build durable per-family snapshot artifacts before object-store upload."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[sample.family].append(sample.model_dump(mode="json"))

    artifacts: list[tuple[dict[str, Any], bytes]] = []
    for family in sorted(grouped):
        payload = json.dumps(
            {
                "snapshot_id": snapshot_id,
                "family": family,
                "samples": grouped[family],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        filename = f"{snapshot_id}_{family}.json"
        artifacts.append(
            (
                {
                    "artifact_type": f"rolling_{family}_summary",
                    "filename": filename,
                    "content_type": "application/json",
                    "size_bytes": len(payload),
                    "bucket": os.getenv("MINIO_BUCKET", "mini-drop"),
                    "object_key": f"watch_snapshots/{snapshot_id}/{filename}",
                    "local_path": "",
                    "sha256": f"sha256:{digest}",
                    "storage_status": "pending",
                    "evidence_ref": f"rolling_snapshot:{snapshot_id}:{family}",
                    "raw_payload_policy": "references_only",
                },
                payload,
            )
        )
    return artifacts


def _artifact_values(snapshot: FrozenSnapshot) -> dict[str, Any]:
    latest_by_family: dict[str, dict[str, Any]] = {}
    for sample in snapshot.samples:
        latest_by_family[sample.family] = sample.payload

    values: dict[str, Any] = {
        "evidence_index": {
            "snapshot_id": snapshot.metadata.snapshot_id,
            "artifact_refs": snapshot.metadata.artifact_refs,
        }
    }
    if "metrics" in latest_by_family:
        values["sys_metrics"] = {"summary": latest_by_family["metrics"]}
    if "stack" in latest_by_family:
        stack_payload = latest_by_family["stack"]
        values["depth_evidence_json"] = stack_payload
        top_functions = stack_payload.get("top_functions")
        if isinstance(top_functions, list):
            values["top_json"] = top_functions
    if "trace" in latest_by_family:
        trace_payload = latest_by_family["trace"]
        values.setdefault("depth_evidence_json", {})
        if isinstance(values["depth_evidence_json"], dict):
            values["depth_evidence_json"]["context"] = trace_payload
    return values
