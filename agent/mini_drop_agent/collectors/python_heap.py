"""Industrial Python heap evidence produced by Memray."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class PythonHeapCollector:
    OUTPUT_BASE = "/tmp/mini-drop"
    MAX_LEAK_ROWS = 2_000_000

    def collect(self, task: CollectorTask) -> CollectorResult:
        output_dir = Path(self.OUTPUT_BASE) / task.id
        output_dir.mkdir(parents=True, exist_ok=True)
        supplied = str(task.options.get("instrumented_result") or task.options.get("memray_result_path") or "").strip()
        supplied_stats = str(task.options.get("memray_stats_path") or "").strip()
        supplied_leaks = str(task.options.get("memray_leaks_path") or "").strip()
        supplied_reports = bool(supplied_stats or supplied_leaks)
        memray = shutil.which("memray")
        if not memray and not supplied_reports:
            return self._blocked(output_dir, task, "memray_not_installed", "Memray 命令不可用")

        mode = "official_reports" if supplied_reports else "instrumented_result" if supplied else "attach"
        capture: Path | None = None
        if supplied:
            capture = Path(supplied).resolve()
            if not self._is_allowed(capture, "MINI_DROP_MEMRAY_ROOTS", "/var/lib/mini-drop/profiles,/tmp/mini-drop"):
                return self._blocked(output_dir, task, "result_path_not_allowed", "产物不在允许的 Memray 目录")
            if not capture.is_file() or capture.stat().st_size <= 0:
                return self._blocked(output_dir, task, "instrumented_result_missing", "Memray 官方产物不存在或为空")
        elif not supplied_reports:
            capture = output_dir / "memray.bin"
            attach = [
                memray,
                "attach",
                "--output",
                str(capture),
                "--duration",
                str(task.duration_sec),
                str(task.target_pid),
            ]
            result = self._run(attach, task.duration_sec + 45)
            if result.returncode != 0 or not capture.is_file() or capture.stat().st_size <= 0:
                reason = result.stderr.decode("utf-8", errors="replace").strip()[:300]
                return self._blocked(output_dir, task, "memray_attach_failed", reason or "Memray attach 未产出数据")

        stats_path: Path | None = None
        if supplied_stats:
            stats_path = Path(supplied_stats).resolve()
            if not self._is_allowed(stats_path, "MINI_DROP_MEMRAY_ROOTS", "/var/lib/mini-drop/profiles,/tmp/mini-drop"):
                return self._blocked(output_dir, task, "stats_path_not_allowed", "Memray stats 不在允许目录")
            if not stats_path.is_file() or stats_path.stat().st_size <= 0:
                return self._blocked(output_dir, task, "memray_stats_missing", "Memray 官方 stats JSON 不存在或为空")
        elif not supplied_leaks:
            stats_path = output_dir / "memray-stats.json"
            stats = self._run(
                [str(memray), "stats", "--json", "--force", "-o", str(stats_path), str(capture)],
                max(60, task.duration_sec + 30),
            )
            if stats.returncode != 0 or not stats_path.is_file():
                reason = stats.stderr.decode("utf-8", errors="replace").strip()[:300]
                return self._blocked(output_dir, task, "memray_stats_failed", reason or "Memray stats 未产出 JSON")
        official: dict[str, Any] = {}
        if stats_path is not None:
            try:
                official = json.loads(stats_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return self._blocked(output_dir, task, "memray_stats_invalid", str(exc))

        leaks_path: Path | None = None
        retained: list[dict[str, Any]] = []
        if supplied_leaks:
            leaks_path = Path(supplied_leaks).resolve()
            if not self._is_allowed(leaks_path, "MINI_DROP_MEMRAY_ROOTS", "/var/lib/mini-drop/profiles,/tmp/mini-drop"):
                return self._blocked(output_dir, task, "leaks_path_not_allowed", "Memray leaks CSV 不在允许目录")
            try:
                retained = self._retained_hotspots(leaks_path)
            except (OSError, csv.Error, ValueError) as exc:
                return self._blocked(output_dir, task, "memray_leaks_invalid", str(exc))

        payload = self._normalize_stats(
            official,
            mode,
            retained,
            has_capture=capture is not None,
            has_stats=stats_path is not None,
            has_leaks=leaks_path is not None,
        )
        structured_path = output_dir / "python_heap_profile.json"
        structured_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts = []
        if capture is not None:
            artifacts.append(self._artifact("memray_capture", capture, "application/octet-stream"))
        if stats_path is not None:
            artifacts.append(self._artifact("memray_stats_json", stats_path, "application/json"))
        if leaks_path is not None:
            artifacts.append(self._artifact("memray_leaks_csv", leaks_path, "text/csv"))
        artifacts.append({
                **self._artifact("python_heap_profile_json", structured_path, "application/json"),
                "collector_family": "python_heap_profile",
                "metadata": {"data": payload},
            })
        return CollectorResult(ok=payload["evidence_validity"]["evidence_status"] == "valid", reason="Memray Heap 证据结构化完成", artifacts=artifacts)

    @staticmethod
    def _run(command: list[str], timeout: int):
        try:
            return subprocess.run(command, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=str(exc).encode())

    @staticmethod
    def _normalize_stats(
        value: dict[str, Any],
        mode: str,
        retained: list[dict[str, Any]] | None = None,
        *,
        has_capture: bool = True,
        has_stats: bool = True,
        has_leaks: bool = False,
    ) -> dict[str, Any]:
        allocations = PythonHeapCollector._hotspots(
            value.get("top_allocations_by_size") or value.get("largest_allocations") or []
        )
        retained = retained or PythonHeapCollector._hotspots(
            value.get("retained_allocations") or value.get("top_retained_allocations") or []
        )
        count_hotspots = PythonHeapCollector._hotspots(value.get("top_allocations_by_count") or [])
        merged = allocations + [item for item in count_hotspots if item not in allocations]
        line_candidates = []
        for family, values in (("retained_allocation_hotspots", retained), ("allocation_hotspots", merged)):
            for index, item in enumerate(values):
                candidate = {
                    "symbol": item["function"],
                    "file": item["file"],
                    "line": item["line"],
                    "evidence_ref": f"python_heap_profile.{family}[{index}]",
                }
                if item["file"] and item["line"] > 0 and candidate not in line_candidates:
                    line_candidates.append(candidate)
        valid = bool(merged or retained)
        return {
            "schema_version": "1.0",
            "producer": "memray",
            "mode": mode,
            "summary": {
                "total_num_allocations": int(value.get("total_num_allocations") or 0),
                "total_memory_allocated": int(value.get("total_memory_allocated") or value.get("total_bytes_allocated") or 0),
                "peak_memory_allocated": int(value.get("peak_memory_allocated") or (value.get("metadata") or {}).get("peak_memory") or 0),
            },
            "allocation_hotspots": merged[:20],
            "retained_allocation_hotspots": retained[:20],
            "call_path_hotspots": [item for item in [*retained, *merged] if item.get("call_path")][:20],
            "line_candidates": line_candidates[:20],
            "raw_artifact_refs": [
                ref for ref, present in (
                    ("artifact:memray_capture", has_capture),
                    ("artifact:memray_stats_json", has_stats),
                    ("artifact:memray_leaks_csv", has_leaks),
                ) if present
            ],
            "evidence_validity": {
                "execution_status": "completed",
                "artifact_status": "produced",
                "evidence_status": "valid" if valid else "insufficient",
                "reason": "memray_retained_allocation_stacks" if retained else "memray_allocation_stacks" if valid else "no_allocation_hotspots",
            },
        }

    @classmethod
    def _retained_hotspots(cls, path: Path) -> list[dict[str, Any]]:
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("Memray leaks CSV 不存在或为空")
        aggregated: dict[tuple[str, str, int, tuple[str, ...]], dict[str, Any]] = {}
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"num_allocations", "size", "stack_trace"}
            if not required.issubset(set(reader.fieldnames or [])):
                raise ValueError("Memray leaks CSV 缺少必要字段")
            for row_index, row in enumerate(reader):
                if row_index >= cls.MAX_LEAK_ROWS:
                    break
                frames = cls._csv_frames(str(row.get("stack_trace") or ""))
                if not frames:
                    continue
                leaf = frames[0]
                call_path = tuple(frame["function"] for frame in frames[:32])
                key = (leaf["function"], leaf["file"], leaf["line"], call_path)
                item = aggregated.setdefault(key, {
                    "function": leaf["function"],
                    "file": leaf["file"],
                    "line": leaf["line"],
                    "size_bytes": 0,
                    "allocation_count": 0,
                    "call_path": list(call_path),
                })
                item["size_bytes"] += max(0, int(row.get("size") or 0))
                item["allocation_count"] += max(0, int(row.get("num_allocations") or 0))
        return sorted(aggregated.values(), key=lambda item: (item["size_bytes"], item["allocation_count"]), reverse=True)[:20]

    @staticmethod
    def _csv_frames(stack_trace: str) -> list[dict[str, Any]]:
        frames = []
        for raw in stack_trace.split("|"):
            parts = raw.rsplit(";", 2)
            if len(parts) != 3:
                continue
            function, file_name, line_text = (part.strip() for part in parts)
            if not function or function.lower() in {"unknown", "[unknown]"}:
                continue
            try:
                line = int(line_text)
            except ValueError:
                line = 0
            frames.append({"function": function, "file": file_name, "line": line})
        return frames

    @staticmethod
    def _hotspots(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            return []
        result = []
        for value in values:
            if not isinstance(value, dict):
                continue
            function, file_name, line = PythonHeapCollector._location(value)
            if not function or function.lower() in {"unknown", "[unknown]"}:
                continue
            call_path = value.get("call_path") or value.get("stack") or []
            if isinstance(call_path, str):
                call_path = [part for part in call_path.split(";") if part]
            result.append({
                "function": function,
                "file": file_name,
                "line": line,
                "size_bytes": max(0, int(value.get("size") or value.get("size_bytes") or 0)),
                "allocation_count": max(0, int(value.get("count") or value.get("allocation_count") or 0)),
                "call_path": call_path if isinstance(call_path, list) else [],
            })
        return result

    @staticmethod
    def _location(value: dict[str, Any]) -> tuple[str, str, int]:
        function = str(value.get("function") or value.get("name") or "").strip()
        file_name = str(value.get("file") or value.get("filename") or "").strip()
        line = int(value.get("line") or value.get("lineno") or 0)
        location = str(value.get("location") or "").strip()
        match = re.match(r"^(.*?):(.+):(\d+)$", location)
        if match:
            function = function or match.group(1).strip()
            file_name = file_name or match.group(2).strip()
            line = line or int(match.group(3))
        return function, file_name, line

    def _blocked(self, output_dir: Path, task: CollectorTask, reason: str, detail: str) -> CollectorResult:
        payload = {
            "schema_version": "1.0",
            "producer": "memray",
            "mode": "blocked",
            "target_pid": task.target_pid,
            "allocation_hotspots": [],
            "retained_allocation_hotspots": [],
            "call_path_hotspots": [],
            "line_candidates": [],
            "raw_artifact_refs": [],
            "evidence_validity": {
                "execution_status": "failed",
                "artifact_status": "produced",
                "evidence_status": "blocked",
                "reason": reason,
                "detail": detail,
            },
        }
        path = output_dir / "python_heap_profile.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifact = {**self._artifact("python_heap_profile_json", path, "application/json"), "collector_family": "python_heap_profile", "metadata": {"data": payload}}
        return CollectorResult(ok=False, reason=detail, artifacts=[artifact])

    @staticmethod
    def _is_allowed(path: Path, env_name: str, default: str) -> bool:
        roots = [Path(item.strip()).resolve() for item in os.getenv(env_name, default).split(",") if item.strip()]
        return any(path == root or root in path.parents for root in roots)

    @staticmethod
    def _artifact(artifact_type: str, path: Path, content_type: str) -> dict[str, Any]:
        return {
            "artifact_type": artifact_type,
            "filename": path.name,
            "local_path": str(path),
            "content_type": content_type,
            "size_bytes": path.stat().st_size,
        }
