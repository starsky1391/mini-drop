"""Persistent runtime-control event producers and bounded storage."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


DEFAULT_STORE_PATH = "/var/lib/mini-drop/runtime-control/events.ndjson"
SENSITIVE_KEY = re.compile(r"(authorization|password|secret|token|api[_-]?key)", re.IGNORECASE)
CONTROL_SIGNALS = {9: "SIGKILL", 15: "SIGTERM", 18: "SIGCONT", 19: "SIGSTOP", 20: "SIGTSTP"}


def runtime_target_snapshot(target_pid: int, target_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read current target state without turning it into a historical control event."""
    config = target_config if isinstance(target_config, dict) else {}
    process = _process_snapshot(target_pid)
    cgroup = _cgroup_snapshot(target_pid)
    container = _docker_container_snapshot(str(config.get("container_id") or ""))
    systemd = _systemd_unit_snapshot(str(config.get("systemd_unit") or ""))
    paused = bool(container.get("paused"))
    frozen = str(cgroup.get("cgroup.freeze") or "0") == "1"
    stopped = str(process.get("state") or "") in {"T", "t"}
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "process": process,
        "container": container,
        "cgroup": cgroup,
        "systemd": systemd,
        "direct_failure_mechanism_observed": paused or frozen or stopped,
        "state_flags": {
            "process_stopped": stopped,
            "container_paused": paused,
            "cgroup_frozen": frozen,
        },
    }


class RuntimeControlEventStore:
    def __init__(
        self,
        path: str | None = None,
        *,
        retention_seconds: int = 900,
        max_events: int = 20000,
    ) -> None:
        self.path = Path(path or os.getenv("MINI_DROP_RUNTIME_CONTROL_STORE", DEFAULT_STORE_PATH))
        self.retention_seconds = max(60, retention_seconds)
        self.max_events = max(100, max_events)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_runtime_control_event(event)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")) + "\n")
            if self.path.stat().st_size > 32 * 1024 * 1024:
                self._compact_locked()
        return normalized

    def query(
        self,
        *,
        target_pid: int | None = None,
        start: float | None = None,
        end: float | None = None,
        target_terms: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        terms = {str(item).lower() for item in target_terms if str(item).strip()}
        now = time.time()
        start = start if start is not None else now - self.retention_seconds
        end = end if end is not None else now
        events = self._read_tail()
        result = []
        for event in events:
            observed = _timestamp(event.get("observed_at"))
            if observed is None or observed < start or observed > end:
                continue
            target = event.get("target") if isinstance(event.get("target"), dict) else {}
            event_pid = _integer(target.get("pid"))
            haystack = " ".join(_flatten_strings(event)).lower()
            pid_matches = bool(target_pid and event_pid == target_pid)
            term_matches = bool(terms and any(term in haystack for term in terms))
            if (target_pid or terms) and not (pid_matches or term_matches):
                continue
            result.append(event)
        return result

    def _read_tail(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        lines: deque[str] = deque(maxlen=self.max_events)
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    lines.append(line)
        except (OSError, UnicodeDecodeError):
            return []
        result = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    def _compact_locked(self) -> None:
        cutoff = time.time() - self.retention_seconds
        events = [
            event for event in self._read_tail()
            if (_timestamp(event.get("observed_at")) or 0) >= cutoff
        ][-self.max_events :]
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        temporary.replace(self.path)


class RuntimeControlObserver:
    """Run independent producer streams; unavailable sources remain explicit."""

    def __init__(self, store: RuntimeControlEventStore | None = None) -> None:
        self.store = store or RuntimeControlEventStore()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._processes: list[subprocess.Popen[str]] = []
        self._targets: set[int] = set()
        self._target_lock = threading.Lock()
        self.source_status: dict[str, dict[str, str]] = {}
        self._status_path = self.store.path.with_name("source-status.json")

    def start(self) -> None:
        producers = [
            ("signal", self._run_signal_stream),
            ("systemd", self._run_journal_stream),
            ("container_runtime", self._run_docker_stream),
            ("containerd", self._run_containerd_stream),
            ("cgroup", self._run_cgroup_poll),
            ("release_change", lambda: self._run_file_stream("release_change", os.getenv("MINI_DROP_RELEASE_EVENT_PATH", "/var/lib/mini-drop/runtime-control/releases.ndjson"))),
            ("kubernetes_audit", lambda: self._run_file_stream("kubernetes_audit", os.getenv("MINI_DROP_K8S_AUDIT_PATH", "/var/lib/mini-drop/runtime-control/kubernetes-audit.ndjson"))),
        ]
        for name, runner in producers:
            thread = threading.Thread(target=self._guarded, args=(name, runner), name=f"runtime-control-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for process in list(self._processes):
            if process.poll() is None:
                process.terminate()
        for thread in self._threads:
            thread.join(timeout=3)

    def set_targets(self, pids: Iterable[int]) -> None:
        with self._target_lock:
            self._targets = {int(pid) for pid in pids if int(pid) > 0}

    def _guarded(self, name: str, runner) -> None:
        try:
            self._set_source_status(name, "starting", "")
            runner()
        except Exception as exc:
            self._set_source_status(name, "blocked", str(exc)[:300])

    def _set_source_status(self, source: str, status: str, reason: str) -> None:
        self.source_status[source] = {"status": status, "reason": reason}
        temporary = self._status_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.source_status, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._status_path)
        except OSError:
            pass

    def _run_signal_stream(self) -> None:
        bpftrace = shutil.which("bpftrace")
        if not bpftrace:
            self._set_source_status("signal", "unavailable", "bpftrace_not_installed")
            return
        script = (
            'tracepoint:signal:signal_generate /args.sig == 9 || args.sig == 15 || '
            'args.sig == 18 || args.sig == 19 || args.sig == 20/ '
            '{ printf("%llu|%d|%s|%d|%d|%d|%s\\n", nsecs, pid, comm, uid, args.sig, args.pid, args.comm); }'
        )
        self._set_source_status("signal", "available", "bpftrace signal_generate")
        self._stream_process("signal", [bpftrace, "-q", "-e", script], self._parse_signal_line)

    def _run_journal_stream(self) -> None:
        journalctl = shutil.which("journalctl")
        if not journalctl:
            self._set_source_status("systemd", "unavailable", "journalctl_not_installed")
            return
        self._set_source_status("systemd", "available", "journald follow")
        self._stream_process("systemd", [journalctl, "-f", "-n", "0", "-o", "json", "--no-pager"], self._parse_journal_line)

    def _run_docker_stream(self) -> None:
        socket_path = os.getenv("MINI_DROP_DOCKER_SOCKET", "/var/run/docker.sock")
        curl = shutil.which("curl")
        if not curl or not os.path.exists(socket_path):
            self._set_source_status("container_runtime", "unavailable", "docker_socket_or_curl_missing")
            return
        self._set_source_status("container_runtime", "available", "Docker Events API")
        self._stream_process(
            "container_runtime",
            [curl, "-sS", "--no-buffer", "--unix-socket", socket_path, "http://localhost/events"],
            self._parse_docker_line,
        )

    def _run_cgroup_poll(self) -> None:
        previous: dict[int, dict[str, str]] = {}
        self._set_source_status("cgroup", "available", "proc cgroup polling")
        while not self._stop.wait(1.0):
            with self._target_lock:
                targets = set(self._targets)
            for pid in targets:
                current = _cgroup_snapshot(pid)
                old = previous.get(pid)
                previous[pid] = current
                if old is None:
                    continue
                for key, value in current.items():
                    if old.get(key) == value:
                        continue
                    self.store.append({
                        "source": "cgroup",
                        "event_type": "cgroup_control_changed",
                        "actor": {"kind": "kernel_or_control_plane"},
                        "action": {"operation": key, "value_before": old.get(key), "value_after": value},
                        "target": {"pid": pid, "cgroup": current.get("path", "")},
                        "effect": {"control_changed": True},
                    })

    def _run_containerd_stream(self) -> None:
        ctr = shutil.which("ctr")
        socket_path = os.getenv("MINI_DROP_CONTAINERD_SOCKET", "/var/run/docker/containerd/containerd.sock")
        if not ctr or not os.path.exists(socket_path):
            self._set_source_status("containerd", "unavailable", "containerd_socket_or_ctr_missing")
            return
        self._set_source_status("containerd", "available", socket_path)
        self._stream_process(
            "containerd",
            [ctr, "--address", socket_path, "events", "--output", "json"],
            self._parse_containerd_line,
        )

    def _run_file_stream(self, source: str, path: str) -> None:
        candidate = Path(path)
        while not candidate.exists() and not self._stop.wait(1.0):
            self._set_source_status(source, "unavailable", f"source_missing:{path}")
        if self._stop.is_set():
            return
        self._set_source_status(source, "available", path)
        with candidate.open("r", encoding="utf-8") as fh:
            while not self._stop.is_set():
                line = fh.readline()
                if not line:
                    self._stop.wait(0.5)
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = normalize_kubernetes_audit_event(value) if source == "kubernetes_audit" else normalize_release_event(value)
                if event:
                    self.store.append(event)

    def _stream_process(self, source: str, command: list[str], parser) -> None:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self._processes.append(process)
        assert process.stdout is not None
        for line in process.stdout:
            if self._stop.is_set():
                break
            event = parser(line)
            if event:
                self.store.append(event)
        return_code = process.poll()
        if return_code is None:
            return_code = process.wait(timeout=1)
        if not self._stop.is_set():
            stderr = process.stderr.read() if process.stderr else ""
            status = "blocked" if return_code else "stopped"
            reason = stderr[-300:] if stderr else f"producer exited with code {return_code}"
            self._set_source_status(source, status, reason)

    @staticmethod
    def _parse_signal_line(line: str) -> dict[str, Any] | None:
        parts = line.strip().split("|", 6)
        if len(parts) == 6:
            _, actor_pid, actor_comm, signal_number, target_pid, target_comm = parts
            actor_uid = 0
        elif len(parts) == 7:
            _, actor_pid, actor_comm, actor_uid, signal_number, target_pid, target_comm = parts
        else:
            return None
        number = _integer(signal_number)
        return {
            "source": "ebpf_signal_generate",
            "event_type": "signal_sent",
            "actor": {"pid": _integer(actor_pid), "comm": actor_comm, "uid": _integer(actor_uid)},
            "action": {"operation": "signal", "signal": CONTROL_SIGNALS.get(number, str(number)), "signal_number": number},
            "target": {"pid": _integer(target_pid), "comm": target_comm},
            "effect": {"expected_state": "T" if number in {19, 20} else "resumed" if number == 18 else "terminated"},
        }

    @staticmethod
    def _parse_journal_line(line: str) -> dict[str, Any] | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        message = str(record.get("MESSAGE") or "")
        if not re.search(r"(Starting|Started|Stopping|Stopped|Killing|Killed|Main process exited|watchdog|RuntimeMaxSec)", message, re.IGNORECASE):
            return None
        unit = _systemd_unit(record, message)
        return {
            "source": "systemd_journal",
            "event_type": "systemd_unit_control",
            "observed_at": _journal_time(record),
            "actor": {"pid": _integer(record.get("_PID")), "comm": record.get("_COMM"), "uid": _integer(record.get("_UID"))},
            "action": {"operation": _systemd_operation(message), "message": message[:500]},
            "target": {"unit": unit, "pid": _integer(record.get("OBJECT_PID") or record.get("_PID"))},
            "effect": {"unit_state_changed": True},
        }

    @staticmethod
    def _parse_docker_line(line: str) -> dict[str, Any] | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        action = str(record.get("Action") or record.get("status") or "")
        if action not in {"pause", "unpause", "kill", "stop", "restart", "die", "oom", "update", "start"}:
            return None
        actor = record.get("Actor") if isinstance(record.get("Actor"), dict) else {}
        attrs = actor.get("Attributes") if isinstance(actor.get("Attributes"), dict) else {}
        return {
            "source": "docker_events",
            "event_type": "container_runtime_control",
            "observed_at": record.get("timeNano") or record.get("time"),
            "actor": {"kind": "docker_daemon"},
            "action": {"operation": action, "signal": attrs.get("signal")},
            "target": {"container_id": actor.get("ID") or record.get("id"), "container_name": attrs.get("name"), "service_id": attrs.get("com.docker.swarm.service.name")},
            "effect": {"container_state_changed": True},
        }

    @staticmethod
    def _parse_containerd_line(line: str) -> dict[str, Any] | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        topic = str(record.get("topic") or record.get("Topic") or "")
        operation = next(
            (name for name in ("kill", "exit", "oom", "pause", "resume", "update", "delete") if name in topic.lower()),
            "",
        )
        if not operation:
            return None
        event = record.get("event") if isinstance(record.get("event"), dict) else {}
        return {
            "source": "containerd_events",
            "event_type": "container_runtime_control",
            "observed_at": record.get("timestamp") or record.get("Timestamp"),
            "actor": {"kind": "containerd"},
            "action": {"operation": operation, "topic": topic},
            "target": {
                "container_id": event.get("container_id") or event.get("containerID") or event.get("id"),
                "namespace": record.get("namespace"),
            },
            "effect": {"container_state_changed": True},
        }


def normalize_runtime_control_event(event: dict[str, Any]) -> dict[str, Any]:
    observed = event.get("observed_at") or datetime.now(timezone.utc).isoformat()
    normalized = {
        "schema_version": "1.0",
        "source": str(event.get("source") or "unknown"),
        "event_type": str(event.get("event_type") or "runtime_control"),
        "observed_at": _iso_time(observed),
        "actor": _redact_mapping(event.get("actor")),
        "action": _redact_mapping(event.get("action")),
        "target": _redact_mapping(event.get("target")),
        "effect": _redact_mapping(event.get("effect")),
        "source_provenance": _redact_mapping(event.get("source_provenance")),
    }
    stable = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
    import hashlib
    normalized["event_id"] = "ctrl_evt_" + hashlib.sha256(stable.encode()).hexdigest()[:16]
    return normalized


def normalize_release_event(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    actor = record.get("actor")
    if not isinstance(actor, dict):
        actor = {"kind": actor or record.get("pipeline") or "deployment_system"}
    return {
        "source": "release_change",
        "event_type": "deployment_or_configuration_change",
        "observed_at": record.get("observed_at") or record.get("timestamp"),
        "actor": actor,
        "action": {"operation": record.get("operation") or record.get("action") or "deploy", "version": record.get("version"), "revision": record.get("revision")},
        "target": {"service_id": record.get("service_id") or record.get("service"), "environment": record.get("environment"), "namespace": record.get("namespace")},
        "effect": {"change_recorded": True},
    }


def normalize_kubernetes_audit_event(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict) or not record.get("verb"):
        return None
    obj = record.get("objectRef") if isinstance(record.get("objectRef"), dict) else {}
    user = record.get("user") if isinstance(record.get("user"), dict) else {}
    source_ips = record.get("sourceIPs") if isinstance(record.get("sourceIPs"), list) else []
    return {
        "source": "kubernetes_audit",
        "event_type": "kubernetes_control_action",
        "observed_at": record.get("stageTimestamp") or record.get("requestReceivedTimestamp"),
        "actor": {"username": user.get("username"), "groups": user.get("groups", [])[:10], "source_ip": source_ips[0] if source_ips else None},
        "action": {"operation": record.get("verb"), "request_uri": str(record.get("requestURI") or "").split("?", 1)[0]},
        "target": {"api_group": obj.get("apiGroup"), "resource": obj.get("resource"), "namespace": obj.get("namespace"), "name": obj.get("name"), "uid": obj.get("uid")},
        "effect": {"response_code": (record.get("responseStatus") or {}).get("code") if isinstance(record.get("responseStatus"), dict) else None},
    }


def _cgroup_snapshot(pid: int) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    path = lines[0].split(":", 2)[-1] if lines else ""
    result["path"] = path
    root = Path("/sys/fs/cgroup") / path.lstrip("/")
    for name in ("cgroup.freeze", "cpu.max", "memory.max", "memory.high"):
        try:
            result[name] = (root / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return result


def _process_snapshot(pid: int) -> dict[str, Any]:
    result: dict[str, Any] = {"pid": pid, "exists": Path(f"/proc/{pid}").is_dir()}
    if not result["exists"]:
        return result
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("Name:"):
                result["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("State:"):
                state = line.split(":", 1)[1].strip()
                result["state_name"] = state
                result["state"] = state[:1]
    except OSError:
        pass
    return result


def _docker_container_snapshot(container_id: str) -> dict[str, Any]:
    if not container_id:
        return {"configured": False}
    socket_path = os.getenv("MINI_DROP_DOCKER_SOCKET", "/var/run/docker.sock")
    curl = shutil.which("curl")
    if not curl or not os.path.exists(socket_path):
        return {"configured": True, "available": False, "reason": "docker_socket_or_curl_missing"}
    try:
        completed = subprocess.run(
            [
                curl,
                "-sS",
                "--max-time",
                "3",
                "--unix-socket",
                socket_path,
                f"http://localhost/containers/{quote(container_id, safe='')}/json",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"configured": True, "available": False, "reason": str(exc)[:200]}
    if completed.returncode != 0:
        return {"configured": True, "available": False, "reason": completed.stderr[-200:]}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"configured": True, "available": False, "reason": "docker_inspect_unparseable"}
    state = payload.get("State") if isinstance(payload.get("State"), dict) else {}
    return {
        "configured": True,
        "available": True,
        "container_id": str(payload.get("Id") or container_id),
        "name": str(payload.get("Name") or "").lstrip("/"),
        "status": state.get("Status"),
        "running": bool(state.get("Running")),
        "paused": bool(state.get("Paused")),
        "pid": _integer(state.get("Pid")),
    }


def _systemd_unit_snapshot(unit: str) -> dict[str, Any]:
    if not unit:
        return {"configured": False}
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"configured": True, "available": False, "reason": "systemctl_not_installed"}
    try:
        completed = subprocess.run(
            [systemctl, "show", unit, "--property=ActiveState,SubState,MainPID", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"configured": True, "available": False, "reason": str(exc)[:200]}
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return {
        "configured": True,
        "available": completed.returncode == 0,
        "unit": unit,
        "active_state": values.get("ActiveState"),
        "sub_state": values.get("SubState"),
        "main_pid": _integer(values.get("MainPID")),
        "reason": completed.stderr[-200:] if completed.returncode else "",
    }


def _redact_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if SENSITIVE_KEY.search(str(key)):
            result[str(key)] = "[REDACTED]"
        elif isinstance(item, str):
            result[str(key)] = item[:500]
        elif isinstance(item, list):
            result[str(key)] = item[:20]
        elif item is None or isinstance(item, (int, float, bool)):
            result[str(key)] = item
    return result


def _journal_time(record: dict[str, Any]) -> str:
    raw = _integer(record.get("__REALTIME_TIMESTAMP"))
    if raw:
        return datetime.fromtimestamp(raw / 1_000_000, timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _systemd_operation(message: str) -> str:
    lower = message.lower()
    operations = (
        ("killing", "kill"),
        ("killed", "killed"),
        ("stopping", "stop"),
        ("stopped", "stopped"),
        ("starting", "start"),
        ("started", "started"),
        ("watchdog", "watchdog"),
    )
    for marker, operation in operations:
        if marker in lower:
            return operation
    return "state_change"


def _systemd_unit(record: dict[str, Any], message: str) -> str:
    for pattern in (
        r"\b(?:Started|Starting|Stopped|Stopping|Killing|Killed)\s+([A-Za-z0-9_.@-]+\.service)\b",
        r"\b([A-Za-z0-9_.@-]+\.service)\b",
    ):
        match = re.search(pattern, message)
        if match:
            return match.group(1)
    return str(record.get("UNIT") or record.get("_SYSTEMD_UNIT") or "")


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_flatten_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_flatten_strings(item))
        return result
    if value is None:
        return []
    return [str(value)]


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1_000_000_000 if number > 10_000_000_000_000 else number
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _iso_time(value: Any) -> str:
    timestamp = _timestamp(value)
    if timestamp is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
