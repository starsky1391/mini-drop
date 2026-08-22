"""Agent-side collector capability profile discovery."""

from __future__ import annotations

import json
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


def build_collector_profile(capabilities: list[str]) -> dict[str, Any]:
    checks = [
        _tool_profile("perf_cpu", "perf", "linux perf CPU sampling"),
        _tool_profile("ebpf_io", "bpftrace", "bpftrace eBPF IO latency"),
        _always_available("sys_metrics", "procfs system and process metrics"),
        _tool_profile("pyspy", "py-spy", "Python stack sampling"),
        _tool_profile("python_heap_profile", "memray", "Memray Python allocation and leak profiling"),
        _native_heap_live_profile(),
        _source_snapshot_profile(),
        _source_mechanism_profile(),
        _python_heap_reference_profile(),
        _offcpu_profile(),
        _trace_endpoint_profile(),
        _log_scan_profile(),
        _blackbox_profile(),
        _redis_exporter_profile(),
    ]
    by_family = {item["collector_type"]: item for item in checks}
    for capability in capabilities:
        by_family.setdefault(capability, _always_available(capability, "registered collector"))
    items = [by_family[key] for key in sorted(by_family)]
    return {
        "schema_version": "1.0",
        "managed": _env_bool("MINI_DROP_MANAGED_COLLECTORS", True),
        "summary": {
            "available": sum(1 for item in items if item["status"] == "available"),
            "degraded": sum(1 for item in items if item["status"] == "degraded"),
            "unavailable": sum(1 for item in items if item["status"] == "unavailable"),
        },
        "collectors": items,
    }


def collector_profile_json(capabilities: list[str]) -> str:
    return json.dumps(build_collector_profile(capabilities), ensure_ascii=False, sort_keys=True)


def _always_available(collector_type: str, source: str) -> dict[str, Any]:
    return {
        "collector_type": collector_type,
        "status": "available",
        "source": source,
        "reason": "registered",
        "default_options": {},
    }


def _tool_profile(collector_type: str, command: str, source: str) -> dict[str, Any]:
    path = shutil.which(command)
    return {
        "collector_type": collector_type,
        "status": "available" if path else "unavailable",
        "source": source,
        "reason": f"{command} found" if path else f"{command} command not found",
        "default_options": {},
    }


def _native_heap_live_profile() -> dict[str, Any]:
    helper = os.getenv("MINI_DROP_NATIVE_HEAP_LIVE_HELPER", "").strip()
    available = bool(helper and Path(helper).is_file() and os.access(helper, os.X_OK))
    return {
        "collector_type": "native_heap_live_profile",
        "status": "available" if available else "unavailable",
        "source": "managed eBPF/BCC native allocator helper",
        "reason": "managed helper found" if available else "managed native heap live helper not configured",
        "default_options": {
            "semantics": "native_allocation_observation_only",
            "cannot_prove": ["python_object_retention", "python_source_line_root_cause"],
        },
    }


def _log_scan_profile() -> dict[str, Any]:
    source = os.getenv("MINI_DROP_LOG_PIPELINE_OUTPUT", "/var/lib/mini-drop/logs/logs.ndjson")
    paths = [item.strip() for item in source.split(",") if item.strip()]
    existing = [path for path in paths if Path(path).exists()]
    if existing:
        status = "available"
        reason = "industrial log pipeline output found"
    elif _env_bool("MINI_DROP_LOG_AUTO_DISCOVER", True) and any(Path(path).exists() for path in ("/var/log", "/var/lib/docker/containers")):
        status = "degraded"
        reason = "log input exists but normalized pipeline output is not ready yet"
    else:
        status = "unavailable"
        reason = "no log pipeline output or readable default log path"
    return {
        "collector_type": "log_scan",
        "status": status,
        "source": "Fluent Bit or OpenTelemetry filelog output",
        "reason": reason,
        "default_options": {"source_paths": paths},
    }


def _source_snapshot_profile() -> dict[str, Any]:
    git_path = shutil.which("git")
    ctags_path = shutil.which("ctags")
    roots = [item.strip() for item in os.getenv("MINI_DROP_SOURCE_ROOTS", "/host/home,/usr/src").split(",") if item.strip()]
    available_roots = [item for item in roots if Path(item).is_dir()]
    available = bool(git_path and ctags_path and available_roots)
    return {
        "collector_type": "source_snapshot",
        "status": "available" if available else "degraded" if git_path and ctags_path else "unavailable",
        "source": "Git revision verification and universal-ctags",
        "reason": "source tools and read-only roots found" if available else "Git, universal-ctags, or source roots are unavailable",
        "default_options": {"source_roots": roots, "available_source_roots": available_roots},
    }


def _source_mechanism_profile() -> dict[str, Any]:
    codeql = shutil.which("codeql")
    suite = os.getenv("MINI_DROP_CODEQL_QUERY_SUITE", "").strip()
    suite_ready = bool(suite and Path(suite).is_file())
    cache_root = os.getenv("MINI_DROP_CODEQL_CACHE_ROOT", "/var/lib/mini-drop/codeql")
    version = os.getenv("MINI_DROP_CODEQL_QUERY_PACK_VERSION", "unversioned")
    roots = [item.strip() for item in os.getenv("MINI_DROP_SOURCE_ROOTS", "/host/home,/usr/src").split(",") if item.strip()]
    available_roots = [item for item in roots if Path(item).is_dir()]
    available = bool(codeql and available_roots)
    reason = (
        "CodeQL CLI ready for guarded AI query; managed query suite found"
        if available and suite_ready
        else "CodeQL CLI ready for guarded AI query; managed query suite is optional"
        if available
        else "codeql command not found" if not codeql else "configured source roots are unavailable"
    )
    return {
        "collector_type": "source_mechanism_query",
        "status": "available" if available else "unavailable",
        "source": "CodeQL CLI guarded AI query or managed query suite",
        "reason": reason,
        "default_options": {
            "query_pack_version": version,
            "cache_root": cache_root,
            "managed_query_suite": suite if suite_ready else "",
            "available_source_roots": available_roots,
            "guarded_ai_query": True,
        },
    }


def _python_heap_reference_profile() -> dict[str, Any]:
    gdb = shutil.which("gdb")
    dumper = os.getenv("MINI_DROP_PYHEAP_DUMPER", "pyheap_dump").strip() or "pyheap_dump"
    dumper_ready = Path(dumper).is_file() if os.path.sep in dumper else bool(shutil.which(dumper))
    analyzer_ready = importlib.util.find_spec("pyheap_ui") is not None
    linux = sys.platform == "linux"
    ptrace_scope = _read_int("/proc/sys/kernel/yama/ptrace_scope")
    ptrace_ready = linux and (ptrace_scope in {None, 0} or (hasattr(os, "geteuid") and os.geteuid() == 0))
    available = bool(linux and gdb and dumper_ready and analyzer_ready and ptrace_ready)
    missing = []
    if not linux:
        missing.append("Linux")
    if not gdb:
        missing.append("gdb")
    if not dumper_ready:
        missing.append("PyHeap dumper")
    if not analyzer_ready:
        missing.append("PyHeap analyzer")
    if not ptrace_ready:
        missing.append("ptrace permission")
    return {
        "collector_type": "python_heap_reference",
        "status": "available" if available else "unavailable",
        "source": "PyHeap v0.7 compatible dumper and analyzer",
        "reason": "PyHeap runtime reference analysis ready" if available else f"missing: {', '.join(missing)}",
        "default_options": {
            "dumper": dumper if dumper_ready else "",
            "analyzer_available": analyzer_ready,
            "gdb_available": bool(gdb),
            "ptrace_scope": ptrace_scope,
            "cpython_versions": "3.8-3.12",
        },
    }


def _trace_endpoint_profile() -> dict[str, Any]:
    perf_path = shutil.which("perf")
    bpftrace_path = shutil.which("bpftrace")
    trace_path = os.getenv("MINI_DROP_TRACE_PATHS", "/var/lib/mini-drop/traces")
    paranoid = _read_perf_paranoid()
    if not perf_path and not bpftrace_path:
        status = "unavailable"
        reason = "perf and bpftrace are not installed"
    elif paranoid is not None and paranoid > 1 and not bpftrace_path:
        status = "degraded"
        reason = f"perf_event_paranoid={paranoid}; perf stack sampling may be blocked"
    elif not Path(trace_path).exists():
        status = "degraded"
        reason = "stack source available but Trace export path is missing"
    else:
        status = "available"
        reason = "stack source and Trace export path found"
    return {
        "collector_type": "trace_endpoint_profile",
        "status": status,
        "source": "perf/eBPF plus OTel/SkyWalking Trace",
        "reason": reason,
        "default_options": {
            "stack_source": "auto",
            "trace_source": "auto",
            "trace_paths": [trace_path],
            "perf_event_paranoid": paranoid,
            "perf_installed": bool(perf_path),
            "ebpf_profile_installed": bool(bpftrace_path),
        },
        "effective_permissions": _deep_collection_permissions(paranoid),
    }


def _offcpu_profile() -> dict[str, Any]:
    spool = os.getenv("MINI_DROP_OFFCPU_PROFILE_PATHS", "/var/lib/mini-drop/profiles/offcpu")
    paths = [item.strip() for item in spool.split(",") if item.strip()]
    existing = [path for path in paths if Path(path).exists()]
    bpftrace_path = shutil.which("bpftrace")
    if existing:
        status = "available"
        reason = "industrial off-CPU profile spool found"
    elif bpftrace_path:
        status = "degraded"
        reason = "industrial off-CPU profile spool missing; bpftrace fallback available"
    else:
        status = "unavailable"
        reason = "industrial off-CPU profile spool missing and bpftrace command not found"
    return {
        "collector_type": "off_cpu_wait_profile",
        "status": status,
        "source": "industrial profile spool with bpftrace fallback",
        "reason": reason,
        "default_options": {
            "offcpu_profile_paths": paths,
            "fallback": "bpftrace" if bpftrace_path else "",
        },
        "effective_permissions": _deep_collection_permissions(_read_perf_paranoid()),
    }


def _blackbox_profile() -> dict[str, Any]:
    url = os.getenv("MINI_DROP_BLACKBOX_URL", "http://blackbox-exporter:9115")
    status, reason = _http_ready(f"{url.rstrip('/')}/-/healthy")
    return {
        "collector_type": "dependency_check",
        "status": status,
        "source": "Prometheus Blackbox Exporter",
        "reason": reason,
        "default_options": {"blackbox_url": url},
    }


def _redis_exporter_profile() -> dict[str, Any]:
    url = os.getenv("MINI_DROP_REDIS_EXPORTER_URL", "")
    if not url:
        return {
            "collector_type": "redis_check",
            "status": "unavailable",
            "source": "Redis Exporter",
            "reason": "MINI_DROP_REDIS_EXPORTER_URL is not set",
            "default_options": {},
        }
    status, reason = _http_ready(url)
    return {
        "collector_type": "redis_check",
        "status": status,
        "source": "Redis Exporter",
        "reason": reason,
        "default_options": {"exporter_url": url},
    }


def _http_ready(url: str) -> tuple[str, str]:
    try:
        request = Request(url, headers={"Accept": "text/plain"})
        with urlopen(request, timeout=1.0) as response:  # noqa: S310 - URL is local/operator-configured.
            if 200 <= response.status < 500:
                return "available", f"endpoint reachable: {url}"
            return "degraded", f"endpoint returned HTTP {response.status}: {url}"
    except OSError:
        return "degraded", f"endpoint not reachable yet: {url}"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_perf_paranoid() -> int | None:
    try:
        return int(Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip())
    except (OSError, ValueError):
        return None


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _deep_collection_permissions(perf_event_paranoid: int | None) -> dict[str, Any]:
    cap_eff = _read_status_value("/proc/self/status", "CapEff")
    no_new_privs = _read_status_value("/proc/self/status", "NoNewPrivs")
    seccomp = _read_status_value("/proc/self/status", "Seccomp")
    debugfs_ready = Path("/sys/kernel/debug").exists()
    host_proc_ready = Path("/host/proc").exists() or Path("/proc").exists()
    host_sys_ready = Path("/host/sys").exists() or Path("/sys").exists()
    sysctl_blocks_perf = perf_event_paranoid is not None and perf_event_paranoid >= 3
    return {
        "container_permission": {
            "cap_eff": cap_eff,
            "no_new_privileges": no_new_privs == "1",
            "seccomp_mode": seccomp,
            "debugfs_ready": debugfs_ready,
            "host_proc_ready": host_proc_ready,
            "host_sys_ready": host_sys_ready,
        },
        "host_perf_event": {
            "perf_event_paranoid": perf_event_paranoid,
            "blocks_unprivileged_perf": sysctl_blocks_perf,
            "manual_action": (
                "需要运维在可信 Worker 宿主机上调整 kernel.perf_event_paranoid，Mini-Drop 不会自动修改。"
                if sysctl_blocks_perf
                else ""
            ),
        },
        "effective_status": "blocked_by_host_sysctl" if sysctl_blocks_perf else "ready_or_container_limited",
    }


def _read_status_value(path: str, key: str) -> str | None:
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(f"{key}:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None
