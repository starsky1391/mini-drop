"""Persistent Watch gRPC service.

Watch synchronization is deliberately independent from task heartbeat.  The
Agent can keep several leases alive while a normal collector task is running.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from server.app.diagnosis.persistent_trigger import MetricSample, MetricWindow
from server.app.diagnosis.watch_runtime import (
    PersistentAgentRuntime,
    WatchEvaluationRequest,
)
from server.app.generated import watch_pb2, watch_pb2_grpc


class WatchRuntimeService(watch_pb2_grpc.WatchRuntimeServicer):
    """Pull leases and evaluate the observation windows reported by an Agent."""

    def __init__(self, runtime: PersistentAgentRuntime) -> None:
        self._runtime = runtime

    def Sync(self, request: watch_pb2.WatchSyncRequest, context) -> watch_pb2.WatchSyncResponse:
        repo = getattr(self._runtime, "repo", None)
        if repo is not None and hasattr(repo, "heartbeat_only"):
            repo.heartbeat_only(request.agent_id, request.ip_addr)
        valid_leases = {
            lease.watch_id: lease
            for lease in self._runtime.list_leases(request.agent_id)
        }

        for observation in request.observations:
            lease = valid_leases.get(observation.watch_id)
            if lease is None:
                continue
            if not observation.target_exists:
                continue

            baseline = _window_from_samples(observation.baseline_samples)
            trigger = _window_from_samples(observation.trigger_samples)
            if baseline is None or trigger is None:
                continue

            try:
                self._runtime.evaluate(
                    observation.watch_id,
                    WatchEvaluationRequest(
                        baseline_window=baseline,
                        trigger_window=trigger,
                    ),
                    suppress_repeated_trigger=True,
                )
            except (KeyError, ValueError):
                # A lease can be disabled between the lease snapshot and the
                # evaluation. The next sync will return the current lease set.
                continue

        response = watch_pb2.WatchSyncResponse()
        for lease in valid_leases.values():
            item = response.lease.add()
            item.watch_id = lease.watch_id
            item.agent_id = lease.agent_id
            item.target_pid = lease.target.target_pid
            item.service_id = lease.target.service_id or ""
            item.instance_id = lease.target.instance_id or ""
            item.endpoint = lease.target.endpoint or ""
            item.target_config_json = json.dumps(
                lease.target_config,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            item.watch_profile = lease.watch_profile
            item.retention_seconds = lease.retention_seconds
            item.poll_interval_seconds = lease.poll_interval_seconds
        return response


def _window_from_samples(samples) -> MetricWindow | None:
    if not samples:
        return None
    parsed = [_metric_sample_from_proto(sample) for sample in samples]
    observed = [
        sample.observed_at
        for sample in parsed
        if sample.observed_at is not None
    ]
    if not observed:
        return None
    return MetricWindow(
        start=min(observed),
        end=max(observed),
        samples=parsed,
    )


def _metric_sample_from_proto(sample: watch_pb2.WatchMetricSample) -> MetricSample:
    observed_at = _parse_datetime(sample.observed_at)
    return MetricSample(
        observed_at=observed_at,
        cpu_percent=sample.cpu_percent if sample.has_cpu_percent else None,
        latency_ms_p99=sample.latency_ms_p99 if sample.has_latency_ms_p99 else None,
        thread_count=sample.thread_count if sample.has_thread_count else None,
        iowait_percent=sample.iowait_percent if sample.has_iowait_percent else None,
        rss_mb=sample.rss_mb if sample.has_rss_mb else None,
        error_count=sample.error_count if sample.has_error_count else None,
    )


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
