"""Python scenario evidence adapters.

These collectors normalize outputs from industrial collectors and deployed
telemetry pipelines. They do not generate observations on their own; missing or
empty upstream input is reported as non-valid evidence.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.evidence_validity import evidence_state


OUTPUT_BASE = "/tmp/mini-drop"
MAX_RECORDS = 10000

INDUSTRIAL_SOURCES = {
    "bcc_offcputime",
    "skywalking_rover",
    "otel_profile",
    "py-spy",
    "memray",
    "fluent_bit",
    "otel_filelog",
    "otel_trace",
    "prometheus",
    "celery_inspect",
    "redis_exporter",
    "statsd",
    "application_runtime_log",
}

WAIT_PRIMITIVE_RE = re.compile(
    r"(futex|lock|rlock|condition|semaphore|queue\.get|queue\.put|future|wait|join|pthread_mutex)",
    re.IGNORECASE,
)
EXCEPTION_RE = re.compile(
    r"(?P<type>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Timeout))(?::\s*(?P<message>.*))?"
)
TRACEBACK_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in ([^\s]+)')
QUEUE_RE = re.compile(r"(celery|rq|asyncio|queue|backlog|reserved|active task|prefetch)", re.IGNORECASE)
POOL_RE = re.compile(
    r"(QueuePool limit|pool exhausted|connection pool|pool timeout|max overflow|checked out|acquire connection|"
    r"EmptyPoolError|FullPoolError|pool_request_error|_get_conn|_put_conn)",
    re.IGNORECASE,
)
RETRY_RE = re.compile(
    r"(retry|attempt\s+\d+|backoff|timeout|deadline exceeded|read timed out|MaxRetryError|retry_request_error)",
    re.IGNORECASE,
)
CACHE_RE = re.compile(r"(cache|cachedsession|cache key|cache_key|cache_bytes|cache_files|backend|filesystem)", re.IGNORECASE)
INPUT_RE = re.compile(r"(groupby|transform|categorical|cardinality|skew|rows|categories|input)", re.IGNORECASE)
NON_INPUT_SCENARIO_RE = re.compile(
    r"(retry|MaxRetryError|retry_request_error|retry_configuration|EmptyPoolError|FullPoolError|pool_request_error|"
    r"QueuePool|cache_batch|cache_|eta_submission|scheduled_count|submitted_eta|celery|backlog)",
    re.IGNORECASE,
)


class PythonLockWaitCollector:
    collector_type = "python_lock_wait_profile"
    artifact_type = "python_lock_wait_profile_json"
    filename = "python_lock_wait_profile.json"

    def collect(self, task: CollectorTask) -> CollectorResult:
        sources = _load_sources(task, [
            "python_stack_samples_json",
            "pyspy_status_json",
            "off_cpu_wait_json",
        ])
        wait_sites = _lock_wait_sites(sources)
        payload = _base_payload(task, self.collector_type, "python_lock_wait", sources)
        payload.update({
            "primitive": wait_sites[0].get("primitive", "unknown") if wait_sites else "unknown",
            "wait_sites": wait_sites[:20],
            "holder_candidates": _holder_candidates(sources)[:10],
            "runtime_frames": _runtime_frames(sources)[:30],
            "line_candidates": _line_candidates(wait_sites),
        })
        return _finish(task, self.collector_type, self.artifact_type, self.filename, payload, bool(wait_sites))


class PythonExceptionProfileCollector:
    collector_type = "python_exception_profile"
    artifact_type = "python_exception_profile_json"
    filename = "python_exception_profile.json"

    def collect(self, task: CollectorTask) -> CollectorResult:
        sources = _load_sources(task, ["log_window_json", "trace_endpoint_profile_json"])
        clusters = _exception_clusters(sources)
        payload = _base_payload(task, self.collector_type, "python_exception_storm", sources)
        payload.update({
            "exception_clusters": clusters[:20],
            "line_candidates": _line_candidates(clusters),
        })
        return _finish(task, self.collector_type, self.artifact_type, self.filename, payload, bool(clusters))


class PythonQueueProfileCollector:
    collector_type = "python_queue_profile"
    artifact_type = "python_queue_profile_json"
    filename = "python_queue_profile.json"

    def collect(self, task: CollectorTask) -> CollectorResult:
        sources = _load_sources(task, [
            "queue_metrics_json",
            "broker_metrics_json",
            "log_window_json",
            "python_stack_samples_json",
            "redis_check_json",
        ])
        queue = _queue_summary(sources)
        has_observation = queue["backlog"] > 0 or bool(queue["active_tasks"] or queue["reserved_tasks"] or queue["slow_task_candidates"])
        payload = _base_payload(task, self.collector_type, "python_queue_backlog", sources)
        payload.update(queue)
        payload["line_candidates"] = _line_candidates([
            *queue["slow_task_candidates"],
            *queue["active_tasks"],
            *queue["reserved_tasks"],
        ])
        return _finish(task, self.collector_type, self.artifact_type, self.filename, payload, has_observation)


class PythonPoolProfileCollector:
    collector_type = "python_pool_profile"
    artifact_type = "python_pool_profile_json"
    filename = "python_pool_profile.json"

    def collect(self, task: CollectorTask) -> CollectorResult:
        sources = _load_sources(task, ["log_window_json", "python_stack_samples_json", "trace_endpoint_profile_json"])
        pool = _pool_summary(sources)
        payload = _base_payload(task, self.collector_type, "python_pool_exhaustion", sources)
        payload.update(pool)
        payload["line_candidates"] = _line_candidates([*pool["wait_sites"], *pool["acquire_sites"]])
        return _finish(task, self.collector_type, self.artifact_type, self.filename, payload, pool["pool_exhausted"] or bool(pool["wait_sites"]))


class PythonRetryTimeoutProfileCollector:
    collector_type = "python_retry_timeout_profile"
    artifact_type = "python_retry_timeout_profile_json"
    filename = "python_retry_timeout_profile.json"

    def collect(self, task: CollectorTask) -> CollectorResult:
        sources = _load_sources(task, ["log_window_json", "trace_endpoint_profile_json", "dependency_check_json"])
        retry = _retry_summary(sources)
        payload = _base_payload(task, self.collector_type, "python_retry_timeout", sources)
        payload.update(retry)
        payload["line_candidates"] = _line_candidates([*retry["retry_clusters"], *retry["timeout_sites"]])
        return _finish(task, self.collector_type, self.artifact_type, self.filename, payload, retry["attempt_count"] > 0)


class PythonCacheProfileCollector:
    collector_type = "python_cache_profile"
    artifact_type = "python_cache_profile_json"
    filename = "python_cache_profile.json"

    def collect(self, task: CollectorTask) -> CollectorResult:
        sources = _load_sources(task, ["log_window_json", "trace_endpoint_profile_json"])
        cache = _cache_summary(sources)
        payload = _base_payload(task, self.collector_type, "python_cache_growth", sources)
        payload.update(cache)
        payload["line_candidates"] = _line_candidates([*cache["key_sites"], *cache["backend_sites"]])
        has_observation = cache["cache_growth"] or bool(cache["key_sites"] or cache["backend_sites"])
        return _finish(task, self.collector_type, self.artifact_type, self.filename, payload, has_observation)


class PythonInputProfileCollector:
    collector_type = "python_input_profile"
    artifact_type = "python_input_profile_json"
    filename = "python_input_profile.json"

    def collect(self, task: CollectorTask) -> CollectorResult:
        sources = _load_sources(task, ["log_window_json", "trace_endpoint_profile_json", "python_stack_samples_json"])
        input_profile = _input_summary(sources)
        payload = _base_payload(task, self.collector_type, "python_input_slow_path", sources)
        payload.update(input_profile)
        payload["line_candidates"] = _line_candidates(input_profile["slow_path_sites"])
        return _finish(task, self.collector_type, self.artifact_type, self.filename, payload, input_profile["slow_path_detected"])


def _load_sources(task: CollectorTask, source_keys: list[str]) -> list[dict[str, Any]]:
    sources = []
    for key in source_keys:
        value = task.options.get(key)
        paths = task.options.get(f"{key}_paths") or task.options.get(f"{key}_path")
        source = _source_from_value(key, value)
        if source is not None:
            sources.append(source)
        for path in _normalize_paths(paths):
            loaded = _source_from_path(key, path)
            if loaded is not None:
                sources.append(loaded)
    generic_paths = task.options.get("upstream_paths") or task.options.get("adapter_output_paths")
    for path in _normalize_paths(generic_paths):
        loaded = _source_from_path("upstream", path)
        if loaded is not None:
            sources.append(loaded)
    runtime_log_paths = task.options.get("application_runtime_log_paths") or task.options.get("runtime_log_paths")
    for path in _normalize_paths(runtime_log_paths):
        loaded = _source_from_path("application_runtime_log", path)
        if loaded is not None:
            sources.append(loaded)
    return sources[:32]


def _source_from_value(kind: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, (dict, list)):
        return None
    records = _flatten(value)
    return {
        "kind": kind,
        "source_status": "loaded",
        "source_kind": _source_kind(value, kind, allow_registered_fallback=False),
        "records": records[:MAX_RECORDS],
        "record_count": len(records),
        "readable_path": "",
    }


def _source_from_path(kind: str, raw_path: str) -> dict[str, Any] | None:
    path = Path(raw_path)
    if not path.exists():
        return None
    records: list[dict[str, Any]] = []
    paths = sorted(path.glob("*.json*")) if path.is_dir() else [path]
    for item in paths[:32]:
        try:
            text = item.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = _parse_json_text(text)
        source_kind = _source_kind(
            parsed,
            kind,
            allow_registered_fallback=kind == "application_runtime_log",
        )
        records.extend(_flatten(parsed))
        if len(records) >= MAX_RECORDS:
            break
    return {
        "kind": kind,
        "source_status": "loaded" if records else "empty",
        "source_kind": source_kind if records else "unknown",
        "records": records[:MAX_RECORDS],
        "record_count": len(records),
        "readable_path": str(path),
    }


def _parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        records = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.extend(_flatten(value))
        return records


def _flatten(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(_flatten(item))
        return result
    if not isinstance(value, dict):
        return []
    for key in (
        "records",
        "events",
        "samples",
        "error_clusters",
        "top_wait_stacks",
        "top_functions",
        "call_path_hotspots",
        "spans",
        "checks",
        "active_tasks",
        "reserved_tasks",
    ):
        if isinstance(value.get(key), list):
            return _flatten(value[key])
    return [value]


def _base_payload(task: CollectorTask, collector_type: str, scenario_type: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_window = _evidence_window(task)
    return {
        "schema_version": "1.0",
        "task_id": task.id,
        "collector_type": collector_type,
        "collector_family": collector_type,
        "scenario_type": scenario_type,
        "target_pid": task.target_pid,
        "target": {
            "pid": task.target_pid,
            "service_id": str(task.options.get("service_id") or (task.options.get("target_config") or {}).get("service_id") or "")
            if isinstance(task.options.get("target_config"), dict) else str(task.options.get("service_id") or ""),
            "instance_id": str(task.options.get("instance_id") or (task.options.get("target_config") or {}).get("instance_id") or "")
            if isinstance(task.options.get("target_config"), dict) else str(task.options.get("instance_id") or ""),
        },
        "time_window": evidence_window,
        "evidence_window": evidence_window,
        "runtime_frames": [],
        "line_candidates": [],
        "source_context_hash": str(task.options.get("source_context_hash") or ""),
        "mechanism_evidence_refs": [],
        "counter_evidence_refs": [],
        "missing_evidence": [],
        "conclusion_eligible": False,
        "eligibility_reason": "scenario adapter only normalizes upstream evidence; root-cause eligibility is decided by diagnosis gates",
        "adapter": {
            "source_policy": "industrial_collectors_only",
            "allowed_source_kinds": sorted(INDUSTRIAL_SOURCES),
            "source_count": len(sources),
            "sources": [
                {
                    "kind": item["kind"],
                    "source_kind": item["source_kind"],
                    "source_status": item["source_status"],
                    "record_count": item["record_count"],
                    "readable_path": item.get("readable_path", ""),
                }
                for item in sources
            ],
        },
    }


def _finish(
    task: CollectorTask,
    collector_type: str,
    artifact_type: str,
    filename: str,
    payload: dict[str, Any],
    has_observation: bool,
) -> CollectorResult:
    sources = payload["adapter"]["sources"]
    has_source = bool(sources)
    valid_source = any(item.get("source_kind") in INDUSTRIAL_SOURCES for item in sources)
    if not has_source:
        status = "blocked"
        reason = "source_missing"
    elif not valid_source:
        status = "blocked"
        reason = "unsupported_or_manual_source"
    elif has_observation:
        status = "valid"
        reason = "industrial upstream observation matched scenario contract"
    else:
        status = "empty_window"
        reason = "industrial upstream source had no matching scenario observation"
    payload["evidence_status"] = status
    payload["evidence_validity"] = evidence_state(status, reason=reason)
    payload["mechanism_evidence_refs"] = _evidence_refs(payload)
    payload["missing_evidence"] = _missing_evidence(payload, status)

    output_dir = os.path.join(os.getenv("MINI_DROP_OUTPUT_BASE", OUTPUT_BASE), task.id)
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, filename)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return CollectorResult(
        ok=status in {"valid", "partial", "empty_window"},
        reason=f"{collector_type}: {status} ({reason})",
        artifacts=[{
            "artifact_type": artifact_type,
            "filename": filename,
            "local_path": output_path,
            "content_type": "application/json",
            "size_bytes": os.path.getsize(output_path),
            "collector_family": collector_type,
            "evidence_window": payload["evidence_window"],
            "metadata": {"data": payload},
        }],
    )


def _evidence_refs(payload: dict[str, Any]) -> list[str]:
    refs = []
    for key in (
        "wait_sites",
        "holder_candidates",
        "exception_clusters",
        "active_tasks",
        "reserved_tasks",
        "slow_task_candidates",
        "wait_sites",
        "acquire_sites",
        "retry_clusters",
        "timeout_sites",
        "line_candidates",
    ):
        for item in payload.get(key) or []:
            if isinstance(item, dict) and item.get("evidence_ref"):
                refs.append(str(item["evidence_ref"]))
    return list(dict.fromkeys(refs))[:50]


def _missing_evidence(payload: dict[str, Any], status: str) -> list[str]:
    missing = []
    if status in {"blocked", "empty_window", "unparseable", "target_exit"}:
        missing.append("valid_industrial_upstream_observation")
    if not payload.get("line_candidates"):
        missing.append("verified_runtime_file_line_candidate")
    if not payload.get("source_context_hash"):
        missing.append("source_snapshot_verification")
    missing.append("scenario_root_cause_gate")
    return list(dict.fromkeys(missing))


def _lock_wait_sites(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sites = []
    for record in _records(sources):
        stack = _stack(record)
        text = " ".join(stack + [str(record.get("top_frame") or record.get("name") or record.get("function") or "")])
        if not WAIT_PRIMITIVE_RE.search(text):
            continue
        business = _business_frame(stack)
        sites.append({
            "primitive": _primitive(text),
            "function": business.get("function") or str(record.get("top_frame") or record.get("name") or ""),
            "file": business.get("file", ""),
            "line": business.get("line", 0),
            "samples": _int(record.get("samples") or record.get("sample_count") or 1),
            "wait_ms": _float(record.get("wait_ms")),
            "evidence_ref": f"python_lock_wait.wait_sites[{len(sites)}]",
        })
    return sites


def _holder_candidates(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for record in _records(sources):
        text = json.dumps(record, ensure_ascii=False).lower()
        if "holder" not in text and "long_hold" not in text:
            continue
        candidates.append({
            "function": str(record.get("holder") or record.get("function") or record.get("top_frame") or ""),
            "evidence_ref": f"python_lock_wait.holder_candidates[{len(candidates)}]",
        })
    return candidates


def _exception_clusters(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str, str, int, str]] = Counter()
    frames_by_key: dict[tuple[str, str, str, int, str], list[str]] = {}
    for record in _records(sources):
        text = _message(record)
        match = EXCEPTION_RE.search(text)
        if not match:
            continue
        frames = TRACEBACK_FRAME_RE.findall(text)
        file, line, func = (frames[-1] if frames else ("", "0", ""))
        template = _template(match.group("message") or text)
        key = (match.group("type"), template, file, _int(line), func)
        counter[key] += _int(record.get("count") or record.get("occurrence_count") or 1)
        frames_by_key[key] = [f"{item[0]}:{item[1]}:{item[2]}" for item in frames[-8:]]
    clusters = []
    for key, count in counter.most_common(20):
        exc_type, template, file, line, func = key
        clusters.append({
            "exception_type": exc_type,
            "message_template": template[:240],
            "occurrence_count": count,
            "throw_site": {"file": file, "line": line, "function": func},
            "catch_or_log_site": {},
            "top_frames": frames_by_key.get(key, []),
            "evidence_ref": f"python_exception_profile.exception_clusters[{len(clusters)}]",
        })
    return clusters


def _queue_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    active = []
    reserved = []
    slow = []
    backlog = 0
    system = "generic"
    for record in _records(sources):
        text = json.dumps(record, ensure_ascii=False)
        lower = text.lower()
        if "celery" in lower:
            system = "celery"
        for task_record in _nested_task_records(record, "active"):
            active.append(_task_item(task_record, "python_queue_profile.active_tasks", len(active)))
        for task_record in _nested_task_records(record, "reserved"):
            reserved.append(_task_item(task_record, "python_queue_profile.reserved_tasks", len(reserved)))
        metric_name = str(record.get("metric") or record.get("__name__") or record.get("name") or "")
        metric_value = _int(record.get("value") or record.get("sample_value"))
        if any(token in metric_name.lower() for token in ("queue", "backlog", "messages_ready", "celery")):
            backlog = max(backlog, metric_value)
        backlog = max(backlog, _int(
            record.get("backlog")
            or record.get("queue_length")
            or record.get("messages_ready")
            or record.get("messages")
            or record.get("ready")
            or record.get("scheduled_count")
            or record.get("eta_count")
            or record.get("submitted_eta")
        ))
        if any(key in record for key in ("scheduled_count", "eta_count", "submitted_eta", "submitted_immediate")):
            system = "celery" if system == "generic" else system
            task = _task_item(record, "python_queue_profile.tasks", len(active) + len(reserved) + len(slow))
            task["task_name"] = task["task_name"] or str(record.get("event") or "scheduled_task")
            reserved.append(task)
            continue
        if not QUEUE_RE.search(text):
            continue
        task = _task_item(record, "python_queue_profile.tasks", len(active) + len(reserved) + len(slow))
        state = str(record.get("state") or record.get("status") or "").lower()
        if not _has_task_identity(task):
            continue
        if "reserved" in state or "reserved" in text.lower():
            reserved.append(task)
        elif "slow" in state or _float(record.get("duration_ms")) > 1000:
            slow.append(task)
        else:
            active.append(task)
    return {
        "queue_system": system,
        "backlog": backlog,
        "active_tasks": active[:20],
        "reserved_tasks": reserved[:20],
        "slow_task_candidates": slow[:20],
        "broker_evidence": {"source_count": len(sources)},
    }


def _pool_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    wait_sites = []
    acquire_sites = []
    long_holders = []
    pool_type = "generic"
    checked_out = None
    pool_size = None
    release_evidence = "unknown"
    for record in _records(sources):
        text = json.dumps(record, ensure_ascii=False)
        lower = text.lower()
        if not POOL_RE.search(text) and not any(
            key in record for key in ("checked_out", "pool_size", "pool_checked_out", "pool_max_size")
        ):
            continue
        if "sqlalchemy" in lower or "queuepool" in lower:
            pool_type = "sqlalchemy"
        elif "redis" in lower:
            pool_type = "redis"
        elif "urllib3" in lower:
            pool_type = "urllib3"
        elif "aiohttp" in lower:
            pool_type = "aiohttp"
        elif "emptypoolerror" in lower or "fullpoolerror" in lower:
            pool_type = "urllib3"
        checked_out = checked_out if checked_out is not None else _optional_int(
            record.get("checked_out") or record.get("pool_checked_out") or record.get("in_use")
        )
        pool_size = pool_size if pool_size is not None else _optional_int(
            record.get("pool_size") or record.get("pool_max_size") or record.get("max_size") or record.get("size")
        )
        site = _site_from_record(record, "python_pool_profile.wait_sites", len(wait_sites))
        wait_sites.append(site)
        if "acquire" in lower or "checkout" in lower or "getconn" in lower:
            acquire_sites.append(site)
        if any(token in lower for token in ("not returned", "missing release", "connection leak", "leaked connection")):
            release_evidence = "missing"
        elif any(token in lower for token in ("released", "checked in", "return_conn")) and release_evidence != "missing":
            release_evidence = "present"
        if str(record.get("error_type") or "").lower() in {"emptypoolerror", "fullpoolerror"}:
            release_evidence = "unknown"
        if _float(record.get("hold_ms") or record.get("checkout_duration_ms") or record.get("duration_ms") or record.get("elapsed_ms")) >= 1000:
            long_holders.append({
                **site,
                "hold_ms": _float(record.get("hold_ms") or record.get("checkout_duration_ms") or record.get("duration_ms") or record.get("elapsed_ms")),
                "evidence_ref": f"python_pool_profile.long_holder_candidates[{len(long_holders)}]",
            })
    exhausted = bool(wait_sites) or (
        checked_out is not None
        and pool_size is not None
        and pool_size > 0
        and checked_out >= pool_size
    )
    return {
        "pool_type": pool_type,
        "pool_exhausted": exhausted,
        "checked_out": checked_out,
        "pool_size": pool_size,
        "wait_sites": wait_sites[:20],
        "acquire_sites": acquire_sites[:20],
        "release_evidence": release_evidence,
        "long_holder_candidates": long_holders[:20],
    }


def _retry_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    clusters = []
    timeout_sites = []
    attempts = 0
    backoff = False
    dependencies = Counter()
    for record in _records(sources):
        text = json.dumps(record, ensure_ascii=False)
        attributes = record.get("attributes") if isinstance(record.get("attributes"), dict) else {}
        if not RETRY_RE.search(text):
            continue
        attempts += max(1, _int(
            record.get("attempt_count")
            or record.get("attempt")
            or record.get("errors")
            or record.get("count")
            or attributes.get("retry.attempt")
            or attributes.get("http.retry_count")
            or _retry_total_from_text(text)
            or 1
        ))
        backoff = backoff or "backoff" in text.lower() or _float(record.get("elapsed_ms") or record.get("duration_ms")) >= 1000
        dependency = str(
            record.get("dependency")
            or record.get("peer.service")
            or record.get("net.peer.name")
            or attributes.get("peer.service")
            or attributes.get("net.peer.name")
            or attributes.get("server.address")
            or ""
        )
        if dependency:
            dependencies[dependency] += 1
        item = _site_from_record(record, "python_retry_timeout_profile.retry_clusters", len(clusters))
        clusters.append(item)
        if "timeout" in text.lower() or "deadline" in text.lower():
            timeout_sites.append(item)
    local_amplification = attempts >= 3 and (
        len(clusters) >= 2
        or backoff
        or any(item.get("file") for item in clusters)
    )
    return {
        "retry_clusters": clusters[:20],
        "timeout_sites": timeout_sites[:20],
        "attempt_count": attempts,
        "backoff_detected": backoff,
        "nested_retry": attempts >= 6,
        "local_amplification": local_amplification,
        "dependency_context": {"dependencies": [name for name, _ in dependencies.most_common(10)]},
        "config_refs": [],
    }


def _cache_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    key_sites = []
    backend_sites = []
    max_bytes = 0.0
    max_files = 0
    unique_keys = 0
    misses = 0
    hits = 0
    backend = "generic"
    for record in _records(sources):
        text = json.dumps(record, ensure_ascii=False)
        lower = text.lower()
        if not CACHE_RE.search(text) and not any(key in record for key in ("cache_bytes", "cache_files", "from_cache", "cache_key", "key")):
            continue
        if "filesystem" in lower or "file cache" in lower or _safe_has(record, "cache_files"):
            backend = "filesystem"
        elif "redis" in lower:
            backend = "redis"
        elif "sqlite" in lower:
            backend = "sqlite"
        max_bytes = max(max_bytes, _float(record.get("cache_bytes") or record.get("bytes")))
        max_files = max(max_files, _int(record.get("cache_files") or record.get("file_count")))
        unique_keys = max(unique_keys, _int(record.get("unique_keys") or record.get("key_count") or record.get("count")))
        if record.get("from_cache") is True or str(record.get("cache_status") or "").lower() == "hit":
            hits += 1
        if record.get("from_cache") is False or str(record.get("cache_status") or "").lower() == "miss":
            misses += 1
        site = _site_from_record(record, "python_cache_profile.key_sites", len(key_sites))
        if any(token in lower for token in ("key", "url", "vary", "request")) or record.get("key") or record.get("cache_key"):
            key_sites.append(site)
        else:
            backend_sites.append(site)
    growth = max_bytes > 0 or max_files > 0 or unique_keys > 0 or misses > hits
    return {
        "cache_backend": backend,
        "cache_growth": growth,
        "cache_bytes": max_bytes,
        "cache_files": max_files,
        "unique_key_count": unique_keys,
        "cache_hits": hits,
        "cache_misses": misses,
        "key_sites": key_sites[:20],
        "backend_sites": backend_sites[:20],
    }


def _input_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    slow_path_sites = []
    max_rows = 0
    max_categories = 0
    max_cardinality = 0
    max_elapsed_ms = 0.0
    operation = "generic"
    for record in _records(sources):
        text = json.dumps(record, ensure_ascii=False)
        lower = text.lower()
        if NON_INPUT_SCENARIO_RE.search(text):
            continue
        if not _has_input_signal(record, text):
            continue
        if "groupby" in lower:
            operation = "groupby"
        elif "transform" in lower:
            operation = "transform"
        max_rows = max(max_rows, _int(record.get("rows") or record.get("row_count")))
        max_categories = max(max_categories, _int(record.get("categories") or record.get("category_count")))
        max_cardinality = max(max_cardinality, _int(record.get("cardinality") or record.get("unique_values")))
        max_elapsed_ms = max(max_elapsed_ms, _float(record.get("elapsed_ms") or record.get("duration_ms") or record.get("latency_ms")))
        slow_path_sites.append(_site_from_record(record, "python_input_profile.slow_path_sites", len(slow_path_sites)))
    slow = bool(slow_path_sites) and (max_elapsed_ms > 0 or max_rows > 0 or max_categories > 0 or max_cardinality > 0)
    return {
        "input_operation": operation,
        "slow_path_detected": slow,
        "rows": max_rows,
        "categories": max_categories,
        "cardinality": max_cardinality,
        "elapsed_ms": max_elapsed_ms,
        "slow_path_sites": slow_path_sites[:20],
    }


def _has_input_signal(record: dict[str, Any], text: str) -> bool:
    return bool(
        INPUT_RE.search(text)
        or any(key in record for key in ("rows", "row_count", "categories", "category_count", "cardinality", "unique_values"))
    )


def _retry_total_from_text(text: str) -> int:
    match = re.search(r"Retry\([^)]*\btotal\s*=\s*(\d+)", text)
    if not match:
        return 0
    return _int(match.group(1))


def _safe_has(record: dict[str, Any], key: str) -> bool:
    return record.get(key) not in (None, "", [])


def _records(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for source in sources:
        records.extend(source.get("records") or [])
    return [item for item in records if isinstance(item, dict)]


def _nested_task_records(record: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = record.get(key) or record.get(f"{key}_tasks")
    if isinstance(value, dict):
        output = []
        for task_name, task_value in value.items():
            if isinstance(task_value, dict):
                output.append({"task_name": task_name, **task_value})
            else:
                output.append({"task_name": task_name, "value": task_value})
        return output
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _source_kind(value: Any, fallback: str, *, allow_registered_fallback: bool) -> str:
    if isinstance(value, dict):
        adapter = value.get("adapter") if isinstance(value.get("adapter"), dict) else {}
        for candidate in (
            value.get("source_kind"),
            value.get("producer"),
            value.get("collector_kind"),
            value.get("collector_type"),
            adapter.get("kind"),
            adapter.get("source"),
        ):
            text = str(candidate or "").lower()
            if "fluent" in text:
                return "fluent_bit"
            if "otel" in text and "trace" in fallback:
                return "otel_trace"
            if "otel" in text:
                return "otel_filelog"
            if "py-spy" in text or "pyspy" in text:
                return "py-spy"
            if "offcpu" in text or "off_cpu" in text or "off-cpu" in text or "rover" in text or "bcc" in text:
                return "bcc_offcputime"
            if "prometheus" in text:
                return "prometheus"
            if "statsd" in text:
                return "statsd"
            if "celery" in text:
                return "celery_inspect"
            if "redis_exporter" in text:
                return "redis_exporter"
            if "application_runtime_log" in text or "runtime_log" in text:
                return "application_runtime_log"
            if "log_scan" in text:
                return "fluent_bit"
            if "trace_endpoint_profile" in text:
                return "otel_trace"
    if isinstance(value, list):
        for item in value[:20]:
            if isinstance(item, dict):
                source_kind = _source_kind(item, fallback, allow_registered_fallback=False)
                if source_kind != "unsupported_manual_source":
                    return source_kind
    if not allow_registered_fallback:
        return "unsupported_manual_source"
    if fallback in {"log_window_json"}:
        return "fluent_bit"
    if fallback in {"trace_endpoint_profile_json"}:
        return "otel_trace"
    if fallback in {"python_stack_samples_json", "pyspy_status_json"}:
        return "py-spy"
    if fallback in {"off_cpu_wait_json"}:
        return "bcc_offcputime"
    if fallback in {"queue_metrics_json", "broker_metrics_json"}:
        return "celery_inspect"
    if fallback in {"redis_check_json"}:
        return "redis_exporter"
    if fallback in {"dependency_check_json"}:
        return "prometheus"
    if fallback in {"application_runtime_log", "application_runtime_log_paths", "runtime_log_paths"}:
        return "application_runtime_log"
    return "unsupported_manual_source"


def _message(record: dict[str, Any]) -> str:
    value = record.get("message") or record.get("sample") or record.get("body") or record.get("log") or ""
    return str(value)


def _template(text: str) -> str:
    return re.sub(r"\b\d+\b", "<num>", re.sub(r"\s+", " ", text)).strip()


def _stack(record: dict[str, Any]) -> list[str]:
    value = record.get("stack") or record.get("frames") or record.get("top_frames") or record.get("call_path") or []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[;\n]", value) if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _business_frame(stack: list[str]) -> dict[str, Any]:
    for frame in reversed(stack):
        if any(token in frame for token in ("site-packages", "threading.py", "queue.py", "concurrent/futures")):
            continue
        match = re.search(r"(.+):(\d+)(?::([^:]+))?$", frame)
        if match:
            return {"file": match.group(1), "line": _int(match.group(2)), "function": match.group(3) or frame}
        return {"file": "", "line": 0, "function": frame}
    return {"file": "", "line": 0, "function": ""}


def _primitive(text: str) -> str:
    lower = text.lower()
    if "rlock" in lower:
        return "RLock"
    if "condition" in lower:
        return "Condition"
    if "semaphore" in lower:
        return "Semaphore"
    if "queue" in lower:
        return "Queue"
    if "future" in lower:
        return "Future"
    if "lock" in lower or "futex" in lower:
        return "Lock"
    return "unknown"


def _runtime_frames(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frames = []
    for record in _records(sources):
        for frame in _stack(record)[:20]:
            frames.append({"frame": frame, "evidence_ref": f"python_runtime.frames[{len(frames)}]"})
    return frames


def _line_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in items:
        site = item.get("throw_site") if isinstance(item.get("throw_site"), dict) else item
        file = str(site.get("file") or "")
        line = _int(site.get("line"))
        if file and line > 0:
            result.append({
                "file": file,
                "line": line,
                "function": str(site.get("function") or ""),
                "evidence_ref": item.get("evidence_ref") or f"line_candidates[{len(result)}]",
            })
    return result[:20]


def _site_from_record(record: dict[str, Any], prefix: str, index: int) -> dict[str, Any]:
    stack = _stack(record)
    site = _business_frame(stack)
    return {
        "function": site.get("function") or str(record.get("function") or record.get("name") or ""),
        "file": site.get("file") or str(record.get("file") or ""),
        "line": site.get("line") or _int(record.get("line")),
        "message": _message(record)[:240],
        "evidence_ref": f"{prefix}[{index}]",
    }


def _task_item(record: dict[str, Any], prefix: str, index: int = 0) -> dict[str, Any]:
    site = _site_from_record(record, prefix, index)
    return {
        "task_name": str(record.get("task_name") or record.get("name") or record.get("function") or ""),
        "task_id": str(record.get("task_id") or record.get("id") or ""),
        "duration_ms": _float(record.get("duration_ms") or record.get("runtime_ms")),
        "file": site["file"],
        "line": site["line"],
        "function": site["function"],
        "evidence_ref": f"{prefix}[{index}]",
    }


def _has_task_identity(task: dict[str, Any]) -> bool:
    return bool(
        str(task.get("task_name") or "").strip()
        or str(task.get("task_id") or "").strip()
        or str(task.get("function") or "").strip()
        or str(task.get("file") or "").strip()
    )


def _normalize_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _evidence_window(task: CollectorTask) -> dict[str, Any]:
    end = task.options.get("window_end") or time.time()
    start = task.options.get("window_start") or (time.time() - task.duration_sec)
    return {
        "start": start,
        "end": end,
        "duration_sec": task.duration_sec,
        "trigger_event_id": task.options.get("trigger_event_id"),
        "evidence_cohort_id": task.options.get("evidence_cohort_id"),
        "collection_mode": task.options.get("collection_mode") or "manual_single",
        "timing_relation": task.options.get("timing_relation") or "unknown",
    }


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _int(value)


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
