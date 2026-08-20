"""Query the persistent runtime-control buffer for one diagnosis target."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.runtime_control import RuntimeControlEventStore, runtime_target_snapshot


class RuntimeControlCollector:
    OUTPUT_BASE = "/tmp/mini-drop"

    def collect(self, task: CollectorTask) -> CollectorResult:
        config = task.options.get("target_config") if isinstance(task.options.get("target_config"), dict) else {}
        end = _number(task.options.get("window_end")) or time.time()
        explicit_start = _number(task.options.get("window_start"))
        start = explicit_start if explicit_start is not None else end - max(task.duration_sec, 180)
        terms = [
            config.get("service_id"),
            config.get("instance_id"),
            config.get("systemd_unit"),
            config.get("container_id"),
        ]
        store = RuntimeControlEventStore(path=task.options.get("runtime_control_store"))
        events = store.query(target_pid=task.target_pid, start=start, end=end, target_terms=terms)
        source_status = _read_source_status(store.path.with_name("source-status.json"))
        current_state = runtime_target_snapshot(task.target_pid, config)
        qualified_events = [
            (event, _qualify_control_event(event, task, config, start, end))
            for event in events
        ]
        direct_events = [event for event, result in qualified_events if result["direct_control_chain"]]
        complete_source_events = [event for event, result in qualified_events if result["complete_source_chain"]]
        causal_edges = _causal_edges(direct_events, task, config)
        state_mechanism = bool(current_state.get("direct_failure_mechanism_observed"))
        evidence_status = "valid" if direct_events and causal_edges else "partial" if state_mechanism else "empty_window"
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "task_id": task.id,
            "collector_type": "runtime_control_history",
            "collector_family": "runtime_control_history",
            "target_pid": task.target_pid,
            "evidence_window": {"start": start, "end": end, "duration_sec": end - start},
            "events": [
                {
                    **event,
                    "qualification": _qualify_control_event(event, task, config, start, end),
                    "evidence_ref": f"runtime_control.events[{index}]",
                }
                for index, event in enumerate(events[:100])
            ],
            "causal_edges": causal_edges,
            "source_status": source_status,
            "current_state": current_state,
            "summary": {
                "event_count": len(events),
                "source_count": len({event.get("source") for event in events}),
                "has_control_actor": any(bool(event.get("actor")) for event in direct_events),
                "has_direct_control_chain": bool(direct_events and causal_edges),
                "has_complete_source_chain": bool(complete_source_events),
                "has_complete_control_chain": bool(complete_source_events),
                "origin_unknown": bool(direct_events) and not bool(complete_source_events),
                "has_current_failure_mechanism": state_mechanism,
                "current_state_flags": current_state.get("state_flags", {}),
            },
            "evidence_validity": {
                "execution_status": "completed",
                "artifact_status": "produced",
                "evidence_status": evidence_status,
                "reason": (
                    "matching runtime control events found"
                    if direct_events and causal_edges
                    else "current paused, frozen, or stopped state observed without historical actor/action"
                    if state_mechanism
                    else "no matching runtime control events or stopped state in retained window"
                ),
            },
        }
        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "runtime_control_events.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return CollectorResult(
            ok=True,
            reason=f"运行控制历史查询完成: {len(events)} 个事件",
            artifacts=[{
                "artifact_type": "runtime_control_event_json",
                "filename": "runtime_control_events.json",
                "local_path": path,
                "content_type": "application/json",
                "size_bytes": os.path.getsize(path),
                "collector_family": "runtime_control_history",
                "metadata": {"data": payload},
            }],
        )


def _causal_edges(events: list[dict[str, Any]], task: CollectorTask, config: dict[str, Any]) -> list[dict[str, Any]]:
    edges = []
    for index, event in enumerate(events[:100]):
        target = event.get("target") if isinstance(event.get("target"), dict) else {}
        action = event.get("action") if isinstance(event.get("action"), dict) else {}
        actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
        event_ref = f"runtime_control.events[{index}]"
        actor_id = str(actor.get("comm") or actor.get("username") or actor.get("kind") or actor.get("pid") or "unknown_actor")
        action_id = str(action.get("signal") or action.get("operation") or event.get("event_type"))
        target_id = str(target.get("service_id") or target.get("unit") or target.get("container_name") or target.get("pid") or task.target_pid)
        provenance = event.get("source_provenance") if isinstance(event.get("source_provenance"), dict) else {}
        source_id = str(
            provenance.get("audit_actor")
            or provenance.get("controller")
            or provenance.get("parent_process")
            or provenance.get("systemd_unit")
            or provenance.get("release_event_id")
            or ""
        ).strip()
        if source_id:
            edges.append({"source": source_id, "relation": "INITIATED", "target": actor_id, "evidence_ref": event_ref})
        edges.extend([
            {"source": actor_id, "relation": "ISSUED", "target": action_id, "evidence_ref": event_ref},
            {"source": action_id, "relation": "TARGETED", "target": target_id, "evidence_ref": event_ref},
            {"source": target_id, "relation": "IMPACTED", "target": str(config.get("service_id") or target_id), "evidence_ref": event_ref},
        ])
    return edges


def _qualify_control_event(
    event: dict[str, Any],
    task: CollectorTask,
    config: dict[str, Any],
    window_start: float,
    window_end: float,
) -> dict[str, Any]:
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    actor_identity = actor.get("pid") or actor.get("comm") or actor.get("username") or actor.get("controller") or actor.get("kind")
    if event.get("event_type") == "cgroup_control_changed" and actor_identity == "kernel_or_control_plane":
        actor_identity = None
    signal_name = str(action.get("signal") or "").upper()
    operation = str(action.get("operation") or "").lower()
    effect = event.get("effect") if isinstance(event.get("effect"), dict) else {}
    action_effect_matches = bool(
        (signal_name in {"SIGSTOP", "SIGTSTP"} and str(effect.get("expected_state") or "").upper() == "T")
        or operation in {"pause", "freeze", "cgroup.freeze"}
    )
    target_pid = _integer(target.get("pid"))
    exact_pid_match = bool(task.target_pid and target_pid == int(task.target_pid))
    configured_container_id = str(config.get("container_id") or "").strip()
    event_container_id = str(target.get("container_id") or "").strip()
    exact_container_match = bool(
        configured_container_id
        and event_container_id
        and configured_container_id == event_container_id
    )
    configured_terms = {
        str(value).strip()
        for value in (
            config.get("service_id"),
            config.get("instance_id"),
            config.get("systemd_unit"),
            config.get("container_id"),
        )
        if str(value or "").strip()
    }
    event_terms = {
        str(value).strip()
        for value in (
            target.get("service_id"),
            target.get("instance_id"),
            target.get("unit"),
            target.get("container_id"),
        )
        if str(value or "").strip()
    }
    if event.get("event_type") == "container_runtime_control":
        target_matches = exact_container_match
    else:
        target_matches = exact_pid_match if task.target_pid else bool(configured_terms & event_terms)
    observed_epoch = _timestamp(event.get("observed_at"))
    event_in_window = observed_epoch is not None and window_start <= observed_epoch <= window_end
    direct = bool(actor_identity and action_effect_matches and target_matches and event_in_window)
    provenance = event.get("source_provenance") if isinstance(event.get("source_provenance"), dict) else {}
    provenance_identity = any(
        provenance.get(key)
        for key in (
            "parent_process", "redacted_command_source", "systemd_unit", "cgroup_identity",
            "release_event_id", "audit_actor", "controller",
        )
    )
    initiated_epoch = _timestamp(provenance.get("initiated_at"))
    provenance_ordered = initiated_epoch is not None and observed_epoch is not None and initiated_epoch <= observed_epoch
    return {
        "direct_control_chain": direct,
        "complete_source_chain": bool(direct and provenance_identity and provenance_ordered),
        "exact_target_match": target_matches,
        "exact_pid_match": exact_pid_match,
        "exact_container_match": exact_container_match,
        "action_effect_matches": action_effect_matches,
        "event_in_window": event_in_window,
        "source_provenance_present": bool(provenance_identity),
        "source_precedes_action": provenance_ordered,
    }


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return _timestamp(value)


def _read_source_status(path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
