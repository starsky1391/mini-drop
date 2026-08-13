"""Process inventory collector.

Reads /proc and emits a structured process list for target selection.
It does not diagnose root cause; it only turns OS process facts into
searchable evidence for watch subscriptions and manual diagnosis setup.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask

try:  # pragma: no cover - Windows test environments do not provide pwd.
    import pwd
except ImportError:  # pragma: no cover
    pwd = None


class ProcessInventoryCollector:
    OUTPUT_BASE = "/tmp/mini-drop"
    MAX_PROCESSES = 2000

    def collect(self, task: CollectorTask) -> CollectorResult:
        if not os.path.isdir("/proc"):
            return CollectorResult(ok=False, reason="/proc 不可用，无法读取进程清单")

        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)
        processes = self._list_processes()
        output = {
            "task_id": task.id,
            "collector_type": "process_inventory",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "processes": processes,
            "summary": {
                "process_count": len(processes),
                "max_processes": self.MAX_PROCESSES,
            },
        }

        output_path = os.path.join(output_dir, "process_inventory.json")
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, indent=2, ensure_ascii=False)

        return CollectorResult(
            ok=True,
            reason=f"进程清单采集完成: {len(processes)} 个进程",
            artifacts=[{
                "artifact_type": "process_inventory_json",
                "filename": "process_inventory.json",
                "local_path": output_path,
                "content_type": "application/json",
                "size_bytes": os.path.getsize(output_path),
                "metadata": output["summary"],
            }],
        )

    def _list_processes(self) -> list[dict]:
        boot_time = self._boot_time()
        clock_ticks = self._sysconf("SC_CLK_TCK", 100)
        page_size = self._sysconf("SC_PAGE_SIZE", 4096)
        processes: list[dict] = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            item = self._read_process(pid, boot_time, clock_ticks, page_size)
            if item:
                processes.append(item)
            if len(processes) >= self.MAX_PROCESSES:
                break
        return processes

    def _read_process(self, pid: int, boot_time: float, clock_ticks: int, page_size: int) -> dict | None:
        proc_dir = f"/proc/{pid}"
        try:
            stat_text = self._read_text(f"{proc_dir}/stat")
            status = self._read_status(f"{proc_dir}/status")
            cmdline = self._read_cmdline(f"{proc_dir}/cmdline")
            comm = self._read_text(f"{proc_dir}/comm").strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            return None

        fields = stat_text.split()
        if len(fields) < 24:
            return None
        try:
            utime_ticks = int(fields[13])
            stime_ticks = int(fields[14])
            start_ticks = int(fields[21])
            rss_pages = int(fields[23])
        except (ValueError, IndexError):
            return None

        cpu_seconds = (utime_ticks + stime_ticks) / max(clock_ticks, 1)
        started_at = boot_time + (start_ticks / max(clock_ticks, 1))
        age_seconds = max(time.time() - started_at, 0.001)
        cpu_percent = min(100.0, cpu_seconds / age_seconds * 100.0)
        uid = status.get("Uid", "").split()[0] if status.get("Uid") else ""

        return {
            "pid": pid,
            "comm": comm,
            "cmdline": cmdline or comm,
            "user": self._user_name(uid),
            "cpu_percent": round(cpu_percent, 2),
            "rss_mb": round((rss_pages * page_size) / 1024 / 1024, 1),
            "thread_count": int(status.get("Threads") or 0),
            "started_at": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
            "service_guess": self._guess_service(cmdline or comm),
            "instance_guess": f"{os.uname().nodename}:{pid}" if hasattr(os, "uname") else str(pid),
        }

    @staticmethod
    def _read_text(path: str) -> str:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    @staticmethod
    def _read_cmdline(path: str) -> str:
        raw = ProcessInventoryCollector._read_text(path)
        return " ".join(part for part in raw.split("\x00") if part).strip()

    @staticmethod
    def _read_status(path: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in ProcessInventoryCollector._read_text(path).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                result[key] = value.strip()
        return result

    @staticmethod
    def _boot_time() -> float:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        return float(line.split()[1])
        except (FileNotFoundError, PermissionError, ValueError):
            pass
        return time.time()

    @staticmethod
    def _user_name(uid: str) -> str:
        if pwd is None:
            return uid
        try:
            return pwd.getpwuid(int(uid)).pw_name
        except (KeyError, ValueError):
            return uid

    @staticmethod
    def _guess_service(text: str) -> str:
        tokens = [token for token in text.replace("=", " ").replace("/", " ").split() if token]
        for marker in ("--service", "service", "--name", "name"):
            for index, token in enumerate(tokens):
                if token == marker and index + 1 < len(tokens):
                    return tokens[index + 1][:128]
                if token.startswith(f"{marker}-") and len(token) > len(marker) + 1:
                    return token[len(marker) + 1:][:128]
        return ""

    @staticmethod
    def _sysconf(name: str, fallback: int) -> int:
        try:
            return int(os.sysconf(name))
        except (AttributeError, ValueError, OSError):
            return fallback
