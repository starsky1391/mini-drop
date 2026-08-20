"""Industrial off-CPU collector with layered eBPF evidence."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from collections import defaultdict
from datetime import datetime
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.evidence_validity import off_cpu_evidence_state
from agent.mini_drop_agent.collectors.trace import (
    _correlate as _correlate_trace,
    _load_trace_source,
)


class OffCPUCollector:
    """Collect wait stacks from industrial profile spool, with explicit fallback."""

    OUTPUT_BASE = "/tmp/mini-drop"
    DEFAULT_MIN_WAIT_MS = 1.0
    MAX_THREAD_FILTERS = 256
    STACK_DEPTH = 32

    def collect(self, task: CollectorTask) -> CollectorResult:
        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "off_cpu_wait.json")
        raw_path = os.path.join(output_dir, "bpftrace_offcpu.txt")
        script_path = os.path.join(output_dir, "offcpu.bt")
        evidence_window = _evidence_window(task)

        industrial = _load_industrial_offcpu_spool(task)
        if industrial["records"]:
            parsed = _parse_industrial_offcpu_records(industrial["records"])
            trace_source = _load_trace_source(task, _target_config(task))
            correlation = _correlate_wait_evidence(task, parsed, trace_source)
            payload = self._payload_from_parsed(
                task,
                evidence_window,
                thread_ids=_thread_ids(task.target_pid),
                parsed=parsed,
                returncode=0,
                stderr="",
                capability_check=_capability_check(task),
                trace_source=trace_source,
                correlation=correlation,
                adapter_kind="industrial_profile_spool",
                source_status="industrial_spool",
                source_metadata={
                    "profile_paths": industrial["readable_paths"],
                    "records_read": industrial["records_read"],
                    "records_in_window": industrial["records_in_window"],
                    "supported_producers": ["bcc_offcputime", "skywalking_rover", "otel_profile", "mini_drop_profile_bridge"],
                },
            )
            _write_json(output_path, payload)
            return CollectorResult(
                ok=True,
                reason=(
                    "工业 Off-CPU profile spool 已结构化: "
                    f"{payload['summary']['sample_count']} 个等待样本, "
                    f"Top wait={payload['summary']['top_wait_reason']}"
                ),
                artifacts=[self._artifact(task, output_path, evidence_window, payload)],
            )

        preflight_status = self._preflight(task)
        if preflight_status:
            payload = self._empty_payload(
                task,
                evidence_window,
                collector_status="blocked",
                parser_status=preflight_status,
                capability_check=_capability_check(task),
            )
            _write_json(output_path, payload)
            return CollectorResult(
                ok=False,
                reason=f"off-CPU 工业采集器被阻断: {preflight_status}",
                artifacts=[self._artifact(task, output_path, evidence_window, payload)],
            )

        thread_ids = _thread_ids(task.target_pid)
        if not thread_ids:
            payload = self._empty_payload(
                task,
                evidence_window,
                collector_status="target_exit",
                parser_status="target_threads_unavailable",
                capability_check=_capability_check(task),
            )
            _write_json(output_path, payload)
            return CollectorResult(
                ok=False,
                reason=f"目标 PID {task.target_pid} 不存在或没有可采集线程",
                artifacts=[self._artifact(task, output_path, evidence_window, payload)],
            )

        capability_check = _capability_check(task)
        script = _build_bpftrace_script(
            thread_ids[: self.MAX_THREAD_FILTERS],
            task.options,
            capability_check=capability_check,
        )
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)

        bpftrace_path = shutil.which("bpftrace") or "bpftrace"
        cmd = [bpftrace_path, script_path]
        timeout = task.duration_sec + 20
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=hasattr(os, "setsid"),
            )
            try:
                stdout, stderr = proc.communicate(timeout=task.duration_sec)
            except subprocess.TimeoutExpired:
                _terminate_process_group(proc)
                stdout, stderr = proc.communicate(timeout=5)
        except Exception as exc:
            payload = self._empty_payload(
                task,
                evidence_window,
                collector_status="blocked",
                parser_status=f"bpftrace_launch_failed:{exc}",
                capability_check=capability_check,
            )
            _write_json(output_path, payload)
            return CollectorResult(
                ok=False,
                reason=f"bpftrace off-CPU 启动失败: {exc}",
                artifacts=[self._artifact(task, output_path, evidence_window, payload)],
            )

        with open(raw_path, "w", encoding="utf-8") as fh:
            fh.write(stdout or "")
            if stderr:
                fh.write("\n--- STDERR ---\n")
                fh.write(stderr)

        parsed = _parse_bpftrace_output(stdout or "")
        trace_source = _load_trace_source(task, _target_config(task))
        correlation = _correlate_wait_evidence(task, parsed, trace_source)
        payload = self._payload_from_parsed(
            task,
            evidence_window,
            thread_ids,
            parsed,
            proc.returncode,
            stderr or "",
            capability_check,
            trace_source=trace_source,
            correlation=correlation,
        )
        _write_json(output_path, payload)

        artifacts = [
            {
                "artifact_type": "raw",
                "filename": "bpftrace_offcpu.txt",
                "local_path": raw_path,
                "content_type": "text/plain",
                "size_bytes": os.path.getsize(raw_path),
                "collector_family": "off_cpu_wait_profile",
                "evidence_window": evidence_window,
                **_cohort_fields(task),
            },
            self._artifact(task, output_path, evidence_window, payload),
        ]
        status = payload["collector_status"]
        if status == "blocked":
            return CollectorResult(
                ok=False,
                reason=f"bpftrace off-CPU 采集被阻断: {payload['parser_status']}",
                artifacts=artifacts,
            )
        return CollectorResult(
            ok=True,
            reason=(
                "bpftrace off-CPU 等待栈采集完成: "
                f"状态 {status}, "
                f"{payload['summary']['sample_count']} 个等待样本, "
                f"Top wait={payload['summary']['top_wait_reason']}"
            ),
            artifacts=artifacts,
        )

    def _preflight(self, task: CollectorTask) -> str:
        if shutil.which("bpftrace") is None:
            return "bpftrace_not_installed"
        if not os.path.isdir(f"/proc/{task.target_pid}"):
            return "target_pid_not_found"
        if os.name != "posix":
            return "linux_required"
        return ""

    def _empty_payload(
        self,
        task: CollectorTask,
        evidence_window: dict[str, Any],
        *,
        collector_status: str,
        parser_status: str,
        capability_check: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": "1.0",
            "task_id": task.id,
            "collector_type": "off_cpu_wait_profile",
            "collector_family": "off_cpu_wait_profile",
            **_cohort_fields(task),
            "collector_invocation": _collector_invocation(task),
            "target_pid": task.target_pid,
            "target_thread_ids": [],
            "evidence_window": evidence_window,
            "collector_status": collector_status,
            "parser_status": parser_status,
            "capability_check": capability_check or _capability_check(task),
            "strategy": {
                "kind": "industrial_offcpu_v2",
                "adapter_kind": "unavailable",
                "source_status": "blocked",
                "fallback_used": False,
                "layers": ["event", "cause", "stack", "correlation"],
                "stack_sources": ["user", "kernel"],
            },
            "event_summary": {
                "observed_wait_events": 0,
                "events_without_user_stack": 0,
                "events_without_kernel_stack": 0,
                "sample_events": [],
            },
            "cause_summary": {
                "cause_counts": {},
                "top_cause": "",
                "source": "none",
            },
            "stack_quality": {
                "user_stack_samples": 0,
                "kernel_stack_samples": 0,
                "stack_unwind_status": "not_observed",
            },
            "correlation": _correlation_summary(task),
            "summary": {
                "sample_count": 0,
                "blocked_thread_count": 0,
                "total_wait_ms": 0.0,
                "top_wait_reason": "",
                "has_wait_reason": False,
            },
            "top_wait_stacks": [],
            "thread_wait_summary": [],
            "syscall_wait_summary": {},
        }
        payload["evidence_validity"] = off_cpu_evidence_state(payload)
        return payload

    def _payload_from_parsed(
        self,
        task: CollectorTask,
        evidence_window: dict[str, Any],
        thread_ids: list[int],
        parsed: dict[str, Any],
        returncode: int | None,
        stderr: str,
        capability_check: dict[str, Any],
        *,
        trace_source: dict[str, Any] | None = None,
        correlation: dict[str, Any] | None = None,
        adapter_kind: str = "bpftrace_fallback",
        source_status: str = "fallback_bpftrace",
        source_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        top_wait_stacks = parsed["top_wait_stacks"][:10]
        thread_wait_summary = parsed["thread_wait_summary"][:20]
        wait_reason_counts: dict[str, int] = defaultdict(int)
        for item in top_wait_stacks:
            wait_reason_counts[str(item.get("wait_reason") or "unknown")] += int(item.get("samples") or 0)
        top_wait_reason = max(wait_reason_counts.items(), key=lambda item: item[1])[0] if wait_reason_counts else ""
        sample_count = sum(int(item.get("samples") or 0) for item in top_wait_stacks)
        total_wait_ms = round(sum(float(item.get("wait_ms") or 0.0) for item in top_wait_stacks), 2)
        parser_status = parsed["parser_status"]
        target_present = os.path.isdir(f"/proc/{task.target_pid}")
        if returncode not in (0, -signal.SIGTERM, -signal.SIGINT, None) and not top_wait_stacks:
            parser_status = f"bpftrace_failed:{_compact_error(stderr)}"
        if returncode not in (0, -signal.SIGTERM, -signal.SIGINT, None):
            collector_status = "blocked"
        elif not target_present and parsed["observed_wait_events"] <= 0:
            collector_status = "target_exit"
        elif parsed["observed_wait_events"] <= 0:
            collector_status = "empty_window"
        elif parsed["stackless_event_count"] > 0 and not top_wait_stacks:
            collector_status = "partial"
        elif parsed["stackless_event_count"] > 0:
            collector_status = "partial"
        else:
            collector_status = "completed"
        payload = {
            "schema_version": "1.0",
            "task_id": task.id,
            "collector_type": "off_cpu_wait_profile",
            "collector_family": "off_cpu_wait_profile",
            **_cohort_fields(task),
            "collector_invocation": _collector_invocation(task),
            "target_pid": task.target_pid,
            "target_thread_ids": thread_ids[: self.MAX_THREAD_FILTERS],
            "evidence_window": evidence_window,
            "collector_status": collector_status,
            "parser_status": parser_status,
            "capability_check": capability_check,
            "strategy": {
                "kind": "industrial_offcpu_v2",
                "adapter_kind": adapter_kind,
                "source_status": source_status,
                "source_metadata": source_metadata or {},
                "fallback_used": adapter_kind.endswith("_fallback"),
                "stack_depth": self.STACK_DEPTH,
                "min_wait_ms": _safe_float(task.options.get("min_wait_ms"), self.DEFAULT_MIN_WAIT_MS),
                "layers": ["event", "cause", "stack", "correlation"],
                "stack_sources": ["user", "kernel"],
                "cause_probes": parsed["cause_probes"],
            },
            "event_summary": {
                "observed_wait_events": parsed["observed_wait_events"],
                "events_without_user_stack": parsed["stackless_event_count"],
                "events_without_kernel_stack": parsed["events_without_kernel_stack"],
                "sample_events": parsed["sample_events"],
            },
            "cause_summary": {
                "cause_counts": parsed["cause_counts"],
                "top_cause": parsed["top_cause"] if parsed["observed_wait_events"] > 0 else "",
                "source": parsed["cause_source"],
            },
            "stack_quality": {
                "user_stack_samples": sum(int(item.get("samples") or 0) for item in top_wait_stacks),
                "kernel_stack_samples": parsed["kernel_stack_samples"],
                "stack_unwind_status": (
                    "complete" if top_wait_stacks and parsed["stackless_event_count"] == 0
                    else "partial" if top_wait_stacks else "unavailable"
                ),
            },
            "trace_source": _compact_trace_source(trace_source or {}),
            "correlation": correlation or _correlation_summary(task),
            "endpoint_bindings": (correlation or {}).get("endpoint_bindings", []),
            "call_path_hotspots": (correlation or {}).get("call_path_hotspots", []),
            "kernel_stacks": parsed.get("kernel_stacks", [])[:10],
            "summary": {
                "sample_count": sample_count,
                "blocked_thread_count": len(thread_wait_summary),
                "total_wait_ms": total_wait_ms,
                "top_wait_reason": top_wait_reason,
                "has_wait_reason": bool(top_wait_reason),
            },
            "top_wait_stacks": top_wait_stacks,
            "thread_wait_summary": thread_wait_summary,
            "syscall_wait_summary": parsed["syscall_wait_summary"],
        }
        payload["evidence_validity"] = off_cpu_evidence_state(payload)
        return payload

    @staticmethod
    def _artifact(
        task: CollectorTask,
        output_path: str,
        evidence_window: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "artifact_type": "off_cpu_wait_json",
            "filename": "off_cpu_wait.json",
            "local_path": output_path,
            "content_type": "application/json",
            "size_bytes": os.path.getsize(output_path),
            "collector_family": "off_cpu_wait_profile",
            "evidence_window": evidence_window,
            **_cohort_fields(task),
            "metadata": {"data": payload},
        }


def _build_bpftrace_script(
    thread_ids: list[int],
    options: dict[str, Any],
    *,
    capability_check: dict[str, Any] | None = None,
) -> str:
    predicate = " || ".join(f"args->prev_pid == {tid}" for tid in thread_ids) or "0"
    next_predicate = " || ".join(f"args->next_pid == {tid}" for tid in thread_ids) or "0"
    wakeup_predicate = " || ".join(f"args->pid == {tid}" for tid in thread_ids) or "0"
    min_wait_ns = int(_safe_float(options.get("min_wait_ms"), OffCPUCollector.DEFAULT_MIN_WAIT_MS) * 1_000_000)
    depth = int(_safe_float(options.get("stack_depth"), OffCPUCollector.STACK_DEPTH))
    depth = max(1, min(depth, 127))
    cause_lines = _cause_probe_lines(capability_check or {})
    cause_prints = _cause_print_lines(capability_check or {})
    return f"""
BEGIN
{{
  printf("mini-drop offcpu industrial v2 start\\n");
}}

tracepoint:sched:sched_switch /{predicate}/
{{
  @start[args->prev_pid] = nsecs;
  @state[args->prev_pid] = args->prev_state;
  @ustack[args->prev_pid] = ustack({depth});
  @kstack[args->prev_pid] = kstack({depth});
}}

tracepoint:sched:sched_switch /({next_predicate}) && @start[args->next_pid]/
{{
  $delta = nsecs - @start[args->next_pid];
    $start_ns = @start[args->next_pid];
  if ($delta >= {min_wait_ns}) {{
    printf("{{\\"event\\":\\"offcpu\\",\\"pid\\":%d,\\"tid\\":%d,\\"wait_ns\\":%llu,\\"start_ns\\":%llu,\\"end_ns\\":%llu,\\"state\\":%d,\\"cpu\\":%d}}\\n",
      pid, args->next_pid, $delta, $start_ns, nsecs, @state[args->next_pid], cpu);
    @wait_ns[@ustack[args->next_pid], @state[args->next_pid]] = sum($delta);
    @wait_count[@ustack[args->next_pid], @state[args->next_pid]] = count();
    @wait_k_ns[@kstack[args->next_pid], @state[args->next_pid]] = sum($delta);
    @thread_wait_ns[args->next_pid] = sum($delta);
  }}
  delete(@start[args->next_pid]);
  delete(@state[args->next_pid]);
  delete(@ustack[args->next_pid]);
  delete(@kstack[args->next_pid]);
}}

tracepoint:sched:sched_wakeup /{wakeup_predicate}/
{{
  @wakeups[args->pid] = count();
}}

{cause_lines}
END
{{
  print(@wait_ns);
  print(@wait_count);
  print(@wait_k_ns);
  print(@thread_wait_ns);
  {cause_prints}
}}
    """.strip()


def _capability_check(task: CollectorTask) -> dict[str, Any]:
    tools = {
        "bpftrace": bool(shutil.which("bpftrace")),
        "perf": bool(shutil.which("perf")),
    }
    paranoid = None
    try:
        paranoid = int(open("/proc/sys/kernel/perf_event_paranoid", encoding="utf-8").read().strip())
    except (OSError, ValueError):
        pass

    cap_eff = ""
    try:
        for line in open("/proc/self/status", encoding="utf-8", errors="replace"):
            if line.startswith("CapEff:"):
                cap_eff = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass

    capability_bits = {
        "SYS_PTRACE": 19,
        "SYS_ADMIN": 21,
        "SYS_RESOURCE": 24,
        "PERFMON": 38,
        "BPF": 39,
    }
    missing_capabilities = []
    if cap_eff:
        try:
            effective = int(cap_eff, 16)
            missing_capabilities = [
                name for name, bit in capability_bits.items()
                if not (effective & (1 << bit))
            ]
        except ValueError:
            missing_capabilities = list(capability_bits)

    tracepoints = {
        name: _tracepoint_exists(group, event)
        for name, group, event in (
            ("sched_switch", "sched", "sched_switch"),
            ("sched_wakeup", "sched", "sched_wakeup"),
            ("futex", "syscalls", "sys_enter_futex"),
            ("block_io", "block", "block_rq_issue"),
            ("tcp_recv", "syscalls", "sys_enter_recvfrom"),
            ("poll", "syscalls", "sys_enter_epoll_wait"),
        )
    }
    missing = []
    if not tools["bpftrace"]:
        missing.append("bpftrace")
    if paranoid is not None and paranoid > 2:
        missing.append("perf_event_paranoid")
    return {
        "status": "available" if tools["bpftrace"] and not missing_capabilities else "degraded",
        "tools": tools,
        "missing_tools": [name for name in missing if name == "bpftrace"],
        "missing_capabilities": missing_capabilities,
        "perf_event_paranoid": paranoid,
        "tracepoints": tracepoints,
        "target_pid": task.target_pid,
        "target_present": os.path.isdir(f"/proc/{task.target_pid}"),
        "repair_action": (
            "以 root/privileged Agent 运行，并启用 pid: host、PERFMON、BPF、"
            "SYS_PTRACE、SYS_ADMIN；同时检查 kernel.perf_event_paranoid。"
        ),
    }


def _tracepoint_exists(group: str, event: str) -> bool:
    roots = (
        "/sys/kernel/tracing/events",
        "/sys/kernel/debug/tracing/events",
    )
    return any(os.path.exists(os.path.join(root, group, event)) for root in roots)


def _cause_probe_lines(capability_check: dict[str, Any]) -> str:
    points = capability_check.get("tracepoints") or {}
    lines = []
    if points.get("futex"):
        lines.append(
            "tracepoint:syscalls:sys_enter_futex /pid == %d/ { @cause_futex = count(); }"
            % int(capability_check.get("target_pid") or 0)
        )
    if points.get("block_io"):
        lines.append(
            "tracepoint:block:block_rq_issue /pid == %d/ { @cause_block_io = count(); }"
            % int(capability_check.get("target_pid") or 0)
        )
    if points.get("tcp_recv"):
        lines.append(
            "tracepoint:syscalls:sys_enter_recvfrom /pid == %d/ { @cause_tcp_recv = count(); }"
            % int(capability_check.get("target_pid") or 0)
        )
    if points.get("poll"):
        lines.append(
            "tracepoint:syscalls:sys_enter_epoll_wait /pid == %d/ { @cause_poll = count(); }"
            % int(capability_check.get("target_pid") or 0)
        )
    return "\n".join(lines)


def _cause_print_lines(capability_check: dict[str, Any]) -> str:
    points = capability_check.get("tracepoints") or {}
    names = []
    if points.get("futex"):
        names.append("print(@cause_futex);")
    if points.get("block_io"):
        names.append("print(@cause_block_io);")
    if points.get("tcp_recv"):
        names.append("print(@cause_tcp_recv);")
    if points.get("poll"):
        names.append("print(@cause_poll);")
    return "\n  ".join(names)


def _cause_kind(stack: tuple[str, ...] | list[str], reason: str, explicit: Any = None) -> str:
    if explicit:
        return str(explicit)
    family = _syscall_family(list(stack))
    if family == "futex_or_lock":
        return "futex_or_lock"
    if family == "io_or_socket":
        return "socket_or_io"
    if family == "poll_wait":
        return "poll_wait"
    if reason == "uninterruptible_io_or_kernel_wait":
        return "kernel_or_block_io"
    if reason == "runnable_preempted":
        return "scheduler_delay"
    if reason:
        return reason
    return "unknown"


def _correlation_summary(task: CollectorTask) -> dict[str, Any]:
    options = task.options
    target_config = options.get("target_config")
    if not isinstance(target_config, dict):
        target_config = {}
    service_id = target_config.get("service_id") or options.get("service_id")
    instance_id = target_config.get("instance_id") or options.get("instance_id")
    endpoint = target_config.get("endpoint") or options.get("endpoint")
    trace_id = options.get("trace_id") or target_config.get("trace_id")
    span_id = options.get("span_id") or target_config.get("span_id")
    call_path = target_config.get("call_path") or options.get("call_path") or []
    if not isinstance(call_path, list):
        call_path = []
    if trace_id and span_id and endpoint and call_path:
        status, method, confidence = "confirmed", "explicit_trace_context", 0.98
    elif endpoint and service_id:
        status, method, confidence = "candidate", "target_context_overlap", 0.62
    else:
        status, method, confidence = "unmatched", "not_available", 0.0
    return {
        "status": status,
        "service_id": service_id,
        "instance_id": instance_id,
        "endpoint": endpoint,
        "trace_id": trace_id,
        "span_id": span_id,
        "call_path": [str(item) for item in call_path],
        "correlation_method": method,
        "confidence": confidence,
        "evidence_ref": "off_cpu_wait.correlation",
    }


def _target_config(task: CollectorTask) -> dict[str, Any]:
    value = task.options.get("target_config")
    return value if isinstance(value, dict) else {}


def _compact_trace_source(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": str(value.get("kind") or "auto"),
        "status": str(value.get("status") or "unavailable"),
        "paths": list(value.get("paths") or [])[:20],
        "readable_paths": list(value.get("readable_paths") or [])[:20],
        "records_read": _safe_int(value.get("records_read")),
        "records_in_window": _safe_int(value.get("records_in_window")),
        "blocked_reason": str(value.get("blocked_reason") or ""),
    }


def _correlate_wait_evidence(
    task: CollectorTask,
    parsed: dict[str, Any],
    trace_source: dict[str, Any],
) -> dict[str, Any]:
    wait_stacks = [
        {
            "hot_frame": item.get("top_frame"),
            "sample_count": item.get("samples"),
            "percent": item.get("percent"),
        }
        for item in parsed.get("top_wait_stacks", [])
        if item.get("top_frame")
    ]
    config = _target_config(task)
    bindings, hotspots = _correlate_trace(
        top_functions=[],
        stack_samples=wait_stacks,
        trace_records=trace_source.get("records", []),
        config=config,
        window=_trace_window(task),
    )
    if hotspots:
        primary = hotspots[0]
        status = "confirmed" if primary.get("call_path") else "candidate"
        method = primary.get("correlation_method") or "trace_match"
        return {
            "status": status,
            "service_id": primary.get("service_id") or config.get("service_id"),
            "instance_id": primary.get("instance_id") or config.get("instance_id"),
            "endpoint": primary.get("endpoint") or config.get("endpoint"),
            "trace_id": (primary.get("trace_ids") or [None])[0],
            "span_id": (primary.get("span_ids") or [None])[0],
            "call_path": primary.get("call_path") or [],
            "correlation_method": method,
            "confidence": _safe_float(primary.get("confidence")),
            "evidence_ref": primary.get("evidence_ref") or "off_cpu_wait.correlation",
            "endpoint_bindings": bindings,
            "call_path_hotspots": hotspots,
        }
    return {
        **_correlation_summary(task),
        "status": "unmatched" if trace_source.get("status") == "completed" else "unavailable",
        "correlation_method": "trace_source_unmatched" if trace_source.get("status") == "completed" else "trace_source_unavailable",
        "endpoint_bindings": [],
        "call_path_hotspots": [],
    }


def _trace_window(task: CollectorTask) -> dict[str, Any]:
    end = task.options.get("window_end")
    start = task.options.get("window_start")
    return {
        "start": start,
        "end": end,
    }


def _parse_bpftrace_output(text: str) -> dict[str, Any]:
    stack_wait_ns: dict[tuple[tuple[str, ...], str], float] = defaultdict(float)
    stack_counts: dict[tuple[tuple[str, ...], str], int] = defaultdict(int)
    thread_wait_ns: dict[int, float] = defaultdict(float)
    kernel_stack_wait_ns: dict[tuple[tuple[str, ...], str], float] = defaultdict(float)
    observed_wait_events = 0
    stackless_event_count = 0
    events_without_kernel_stack = 0
    kernel_stack_samples = 0
    cause_counts: dict[str, int] = defaultdict(int)
    for event in _parse_json_events(text):
        observed_wait_events += 1
        stack = tuple(event.get("stack") or ())
        kernel_stack = event.get("kernel_stack") or []
        if not stack:
            stackless_event_count += 1
        if not kernel_stack:
            events_without_kernel_stack += 1
        else:
            kernel_stack_samples += 1
        reason = _wait_reason(event.get("state"))
        cause = _cause_kind(stack, reason, event.get("cause"))
        cause_counts[cause] += 1
        if stack:
            key = (stack, reason)
            stack_wait_ns[key] += float(event.get("wait_ns") or 0.0)
            stack_counts[key] += 1
        if event.get("tid"):
            thread_wait_ns[int(event["tid"])] += float(event.get("wait_ns") or 0.0)
    if not stack_wait_ns:
        maps = _parse_printed_maps(text)
        stack_wait_ns.update(maps["stack_wait_ns"])
        stack_counts.update(maps["stack_counts"])
        thread_wait_ns.update(maps["thread_wait_ns"])
        kernel_stack_wait_ns.update(maps["kernel_stack_wait_ns"])
        if not observed_wait_events:
            observed_wait_events = sum(stack_counts.values())
        for cause, count in maps["cause_counts"].items():
            cause_counts[cause] += count

    total_wait_ns = sum(stack_wait_ns.values())
    top_wait_stacks = []
    for index, ((stack, reason), wait_ns) in enumerate(
        sorted(stack_wait_ns.items(), key=lambda item: (-item[1], item[0][1], item[0][0]))[:10]
    ):
        samples = stack_counts.get((stack, reason), 0)
        wait_ms = wait_ns / 1_000_000.0
        top_wait_stacks.append({
            "wait_reason": reason,
            "stack": list(stack),
            "top_frame": stack[0] if stack else "",
            "samples": samples,
            "wait_ms": round(wait_ms, 2),
            "percent": round((wait_ns / total_wait_ns * 100.0), 2) if total_wait_ns else 0.0,
            "evidence_ref": f"off_cpu_wait.top_wait_stacks[{index}]",
        })
    thread_wait_summary = [
        {
            "tid": tid,
            "wait_ms": round(wait_ns / 1_000_000.0, 2),
            "evidence_ref": f"off_cpu_wait.thread_wait_summary[{index}]",
        }
        for index, (tid, wait_ns) in enumerate(sorted(thread_wait_ns.items(), key=lambda item: -item[1])[:20])
    ]
    kernel_stacks = [
        {
            "wait_reason": reason,
            "stack": list(stack),
            "top_frame": stack[0] if stack else "",
            "wait_ms": round(wait_ns / 1_000_000.0, 2),
            "evidence_ref": f"off_cpu_wait.kernel_stacks[{index}]",
        }
        for index, ((stack, reason), wait_ns) in enumerate(
            sorted(kernel_stack_wait_ns.items(), key=lambda item: (-item[1], item[0][1], item[0][0]))[:10]
        )
    ]
    syscall_wait_summary: dict[str, int] = defaultdict(int)
    for item in top_wait_stacks:
        family = _syscall_family(item["stack"])
        if family:
            syscall_wait_summary[family] += int(item["samples"] or 0)
    return {
        "top_wait_stacks": top_wait_stacks,
        "thread_wait_summary": thread_wait_summary,
        "syscall_wait_summary": dict(syscall_wait_summary),
        "parser_status": (
            "ok" if top_wait_stacks
            else "events_without_stack" if observed_wait_events
            else "empty_bpftrace_output"
        ),
        "observed_wait_events": observed_wait_events,
        "stackless_event_count": stackless_event_count,
        "events_without_kernel_stack": events_without_kernel_stack,
        "kernel_stack_samples": kernel_stack_samples,
        "kernel_stacks": kernel_stacks,
        "sample_events": [
            {
                "pid": event["pid"],
                "tid": event["tid"],
                "cpu": event["cpu"],
                "state": event["state"],
                "start_ns": event["start_ns"],
                "end_ns": event["end_ns"],
                "wait_ns": event["wait_ns"],
                "wait_reason": _wait_reason(event["state"]),
                "evidence_ref": f"off_cpu_wait.events[{index}]",
            }
            for index, event in enumerate(_parse_json_events(text)[:20])
        ],
        "cause_counts": dict(cause_counts),
        "top_cause": (
            max(cause_counts.items(), key=lambda item: (item[1], item[0]))[0]
            if cause_counts else ""
        ),
        "cause_source": "eBPF event and stack inference" if cause_counts else "none",
        "cause_probes": sorted(cause_counts),
    }


def _load_industrial_offcpu_spool(task: CollectorTask) -> dict[str, Any]:
    paths = _offcpu_spool_paths(task)
    records: list[dict[str, Any]] = []
    readable_paths: list[str] = []
    for path in paths:
        if not os.path.isfile(path):
            continue
        readable_paths.append(path)
        for record in _read_profile_records(path):
            normalized = _normalize_industrial_offcpu_record(record)
            if normalized and _offcpu_record_in_window(normalized, task):
                records.append(normalized)
    return {
        "paths": paths,
        "readable_paths": readable_paths,
        "records_read": len(records),
        "records_in_window": len(records),
        "records": records,
    }


def _offcpu_spool_paths(task: CollectorTask) -> list[str]:
    target_config = _target_config(task)
    value = (
        target_config.get("offcpu_profile_paths")
        or task.options.get("offcpu_profile_paths")
        or os.getenv("MINI_DROP_OFFCPU_PROFILE_PATHS", "")
    )
    raw_items = value if isinstance(value, list) else str(value).split(",")
    result = []
    for raw in raw_items:
        path = str(raw).strip()
        if not path:
            continue
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.endswith((".json", ".jsonl", ".ndjson")):
                    result.append(os.path.join(path, name))
        else:
            result.append(path)
    return result[:32]


def _read_profile_records(path: str) -> list[dict[str, Any]]:
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if payload is not None:
        return _flatten_profile_payload(payload)
    records = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.extend(_flatten_profile_payload(item))
    return records


def _flatten_profile_payload(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_flatten_profile_payload(item))
        return result
    if not isinstance(value, dict):
        return []
    for key in ("events", "samples", "records", "profiles"):
        if isinstance(value.get(key), list):
            return _flatten_profile_payload(value[key])
    return [value]


def _normalize_industrial_offcpu_record(record: dict[str, Any]) -> dict[str, Any] | None:
    pid = _safe_int(record.get("pid") or record.get("process_id"))
    tid = _safe_int(record.get("tid") or record.get("thread_id") or pid)
    wait_ns = _duration_to_ns(
        record.get("wait_ns"),
        record.get("wait_us"),
        record.get("wait_ms"),
        record.get("duration_ns"),
        record.get("duration_us"),
        record.get("duration_ms"),
    )
    stack = _stack_list(record.get("user_stack") or record.get("stack") or record.get("ustack"))
    kernel_stack = _stack_list(record.get("kernel_stack") or record.get("kstack"))
    if wait_ns <= 0 and not stack and not kernel_stack:
        return None
    return {
        "event": "industrial_offcpu",
        "pid": pid,
        "tid": tid,
        "cpu": _safe_int(record.get("cpu")),
        "wait_ns": wait_ns,
        "start_ns": _safe_int(record.get("start_ns") or record.get("start_time_unix_nano")),
        "end_ns": _safe_int(record.get("end_ns") or record.get("end_time_unix_nano")),
        "state": record.get("state", 1),
        "stack": stack,
        "kernel_stack": kernel_stack,
        "cause": record.get("cause_kind") or record.get("wait_reason") or record.get("category"),
    }


def _parse_industrial_offcpu_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [
        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for record in records
    ]
    return _parse_bpftrace_output("\n".join(lines))


def _duration_to_ns(*values: Any) -> float:
    names = ("ns", "us", "ms", "ns", "us", "ms")
    for name, value in zip(names, values):
        if value is None:
            continue
        number = _safe_float(value)
        if name == "ms":
            return number * 1_000_000
        if name == "us":
            return number * 1_000
        return number
    return 0.0


def _stack_list(value: Any) -> list[str]:
    if isinstance(value, str):
        separators = ";" if ";" in value else "\n"
        return [part.strip()[:256] for part in value.split(separators) if part.strip()]
    if isinstance(value, list):
        return [str(item).strip()[:256] for item in value if str(item).strip()]
    return []


def _offcpu_record_in_window(record: dict[str, Any], task: CollectorTask) -> bool:
    start = _window_epoch(task.options.get("window_start"))
    end = _window_epoch(task.options.get("window_end"))
    if start <= 0 or end <= 0:
        return True
    record_start = _safe_int(record.get("start_ns"), 0)
    record_end = _safe_int(record.get("end_ns"), record_start)
    if record_start > 10_000_000_000_000:
        start *= 1_000_000_000
        end *= 1_000_000_000
    return (record_end or record_start) >= start and record_start <= end


def _window_epoch(value: Any) -> int:
    if isinstance(value, str) and value.strip():
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return _safe_int(value, 0)


def _parse_json_events(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw.startswith("{"):
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack = item.get("stack")
        if isinstance(stack, str):
            stack = [part for part in stack.split(";") if part]
        if not isinstance(stack, list):
            stack = []
        events.append({
            "event": item.get("event"),
            "pid": _safe_int(item.get("pid"), 0),
            "tid": _safe_int(item.get("tid"), 0),
            "cpu": _safe_int(item.get("cpu"), 0),
            "wait_ns": _safe_float(item.get("wait_ns"), 0.0),
            "start_ns": _safe_int(item.get("start_ns"), 0),
            "end_ns": _safe_int(item.get("end_ns"), 0),
            "state": item.get("state"),
            "stack": [str(frame).strip() for frame in stack if str(frame).strip()],
            "kernel_stack": [
                str(frame).strip()
                for frame in (item.get("kernel_stack") or item.get("kstack") or [])
                if str(frame).strip()
            ],
        })
    return events


def _parse_printed_maps(text: str) -> dict[str, Any]:
    stack_wait_ns: dict[tuple[tuple[str, ...], str], float] = defaultdict(float)
    stack_counts: dict[tuple[tuple[str, ...], str], int] = defaultdict(int)
    thread_wait_ns: dict[int, float] = defaultdict(float)
    kernel_stack_wait_ns: dict[tuple[tuple[str, ...], str], float] = defaultdict(float)
    cause_counts: dict[str, int] = defaultdict(int)
    for line in text.splitlines():
        match = re.match(r"@thread_wait_ns\[(\d+)\]:\s*(\d+(?:\.\d+)?)", line.strip())
        if match:
            thread_wait_ns[int(match.group(1))] += float(match.group(2))
    for map_name in ("wait_ns", "wait_count"):
        for stack, state, value in _iter_stack_map_entries(text, map_name):
            key = (tuple(stack), _wait_reason(state))
            if map_name == "wait_ns":
                stack_wait_ns[key] += value
            else:
                stack_counts[key] += int(value)
    for stack, state, value in _iter_stack_map_entries(text, "wait_k_ns"):
        kernel_stack_wait_ns[(tuple(stack), _wait_reason(state))] += value
    for cause, value in re.findall(r"@cause_([a-z_]+):\s*(\d+(?:\.\d+)?)", text):
        cause_counts[cause] += int(float(value))
    return {
        "stack_wait_ns": stack_wait_ns,
        "stack_counts": stack_counts,
        "thread_wait_ns": thread_wait_ns,
        "kernel_stack_wait_ns": kernel_stack_wait_ns,
        "cause_counts": cause_counts,
    }


def _iter_stack_map_entries(text: str, map_name: str):
    pattern = re.compile(rf"@{map_name}\[(.*?)\]:\s*(\d+(?:\.\d+)?)", re.DOTALL)
    for match in pattern.finditer(text):
        raw_key = match.group(1)
        stack, state = _split_stack_key(raw_key)
        if stack:
            yield stack, state, float(match.group(2))


def _split_stack_key(raw_key: str) -> tuple[list[str], int | str]:
    state: int | str = ""
    key = raw_key.strip()
    state_match = re.search(r",\s*(-?\d+)\s*$", key)
    if state_match:
        state = int(state_match.group(1))
        key = key[: state_match.start()]
    stack = []
    for raw_line in key.splitlines():
        frame = raw_line.strip()
        if not frame or frame in {"user stack", "kernel stack", "ustack"}:
            continue
        if frame.startswith("@") or frame.endswith("["):
            continue
        stack.append(frame[:256])
    return stack, state


def _wait_reason(state: Any) -> str:
    value = _safe_int(state, 0)
    if value == 0:
        return "runnable_preempted"
    if value & 2:
        return "uninterruptible_io_or_kernel_wait"
    if value & 1:
        return "interruptible_sleep_or_lock_wait"
    if value & 4:
        return "stopped"
    if value & 8:
        return "traced"
    if value & 16:
        return "dead"
    return f"sched_state_{value}"


def _syscall_family(stack: list[str]) -> str:
    text = " ".join(stack).lower()
    if "futex" in text or "pthread_mutex" in text or "lock" in text:
        return "futex_or_lock"
    if "epoll" in text or "poll" in text or "select" in text:
        return "poll_wait"
    if "nanosleep" in text or "sleep" in text:
        return "sleep"
    if "read" in text or "write" in text or "recv" in text or "send" in text:
        return "io_or_socket"
    return ""


def _thread_ids(pid: int) -> list[int]:
    task_dir = f"/proc/{pid}/task"
    try:
        return sorted(int(item) for item in os.listdir(task_dir) if item.isdigit())
    except (FileNotFoundError, PermissionError, ValueError):
        return []


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def _write_json(path: str, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _collector_invocation(task: CollectorTask) -> dict[str, Any]:
    invocation = task.options.get("collector_invocation")
    if isinstance(invocation, dict):
        return invocation
    return {
        "schema_version": "1.0",
        "scope_source": "task_options",
        "collector_family": "off_cpu_wait_profile",
        "target_config": {"pid": task.target_pid},
    }


def _cohort_fields(task: CollectorTask) -> dict[str, Any]:
    return {
        "trigger_event_id": task.options.get("trigger_event_id"),
        "evidence_cohort_id": task.options.get("evidence_cohort_id"),
        "collection_mode": task.options.get("collection_mode") or "manual_single",
        "timing_relation": task.options.get("timing_relation") or "unknown",
    }


def _evidence_window(task: CollectorTask) -> dict[str, Any]:
    return {
        "window_start": task.options.get("window_start"),
        "window_end": task.options.get("window_end"),
        "duration_sec": task.duration_sec,
        **_cohort_fields(task),
    }


def _compact_error(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:200]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
