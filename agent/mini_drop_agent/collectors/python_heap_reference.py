"""Bounded runtime reference evidence derived from an official PyHeap dump."""

from __future__ import annotations

import importlib.util
import json
import mmap
import os
import re
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class PythonHeapReferenceCollector:
    OUTPUT_BASE = "/tmp/mini-drop"
    DEFAULT_MAX_DUMP_BYTES = 2 * 1024 * 1024 * 1024
    DEFAULT_MAX_OBJECTS = 2_000_000
    DEFAULT_MAX_DEPTH = 8
    DEFAULT_MAX_PATHS = 12
    DEFAULT_MAX_REPR = 160

    def collect(self, task: CollectorTask) -> CollectorResult:
        output_dir = Path(self.OUTPUT_BASE) / task.id
        output_dir.mkdir(parents=True, exist_ok=True)
        limits = self._limits(task.options)
        supplied = str(task.options.get("pyheap_dump_path") or "").strip()
        if supplied:
            dump_path = Path(supplied).resolve()
            if not self._allowed(dump_path):
                return self._blocked(output_dir, "dump_path_not_allowed", "PyHeap dump 不在受管理目录")
            if not dump_path.is_file() or dump_path.stat().st_size <= 0:
                return self._blocked(output_dir, "pyheap_dump_missing", "PyHeap dump 不存在或为空")
        else:
            capability_error = self._capture_capability_error(task.target_pid)
            if capability_error:
                return self._blocked(output_dir, capability_error[0], capability_error[1])
            dump_path = output_dir / "heap.pyheap"
            command = self._dump_command(task, dump_path)
            result = self._run(command, timeout=max(120, task.duration_sec + 90))
            if result.returncode != 0 or not dump_path.is_file() or dump_path.stat().st_size <= 0:
                return self._blocked(
                    output_dir,
                    "pyheap_attach_failed",
                    self._stderr(result) or "PyHeap GDB attach 未产出 dump",
                )
        if dump_path.stat().st_size > limits["max_dump_bytes"]:
            return self._blocked(
                output_dir,
                "dump_size_limit_exceeded",
                f"PyHeap dump 超过限制: {dump_path.stat().st_size}>{limits['max_dump_bytes']}",
            )
        try:
            analysis = self._analyze_dump(dump_path, task.options, limits)
        except ModuleNotFoundError as exc:
            return self._blocked(output_dir, "pyheap_analyzer_not_installed", str(exc))
        except (OSError, ValueError, RuntimeError) as exc:
            return self._blocked(output_dir, "pyheap_dump_unparseable", str(exc))

        status = "valid" if analysis["reference_paths"] else "partial" if analysis["top_retained_objects"] else "empty_window"
        payload = {
            "schema_version": "1.0",
            "producer": "pyheap",
            "target_pid": task.target_pid,
            "candidate_id": str(task.options.get("candidate_id") or ""),
            "top_retained_objects": analysis["top_retained_objects"],
            "reference_paths": [
                {**path, "candidate_id": str(task.options.get("candidate_id") or "")}
                for path in analysis["reference_paths"]
            ],
            "analysis_limits": limits,
            "raw_artifact_refs": ["artifact:pyheap_dump"],
            "evidence_validity": {
                "execution_status": "completed",
                "artifact_status": "produced",
                "evidence_status": status,
                "reason": (
                    "pyheap_inbound_reference_paths"
                    if status == "valid"
                    else "retained_objects_without_root_path" if status == "partial"
                    else "no_matching_retained_objects"
                ),
            },
        }
        structured = output_dir / "python_heap_reference.json"
        structured.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts = [
            self._artifact("pyheap_dump", dump_path, "application/octet-stream"),
            {
                **self._artifact("python_heap_reference_json", structured, "application/json"),
                "collector_family": "python_heap_reference",
                "metadata": {"data": payload},
            },
        ]
        return CollectorResult(ok=status in {"valid", "partial"}, reason="PyHeap 引用证据结构化完成", artifacts=artifacts)

    def _analyze_dump(
        self,
        dump_path: Path,
        options: dict[str, Any],
        limits: dict[str, int],
    ) -> dict[str, list[dict[str, Any]]]:
        from pyheap_ui.heap import (  # type: ignore[import-not-found]
            InboundReferences,
            objects_sorted_by_retained_heap,
            provide_retained_heap_with_caching,
        )
        from pyheap_ui.heap_reader import HeapReader  # type: ignore[import-not-found]

        with dump_path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as buffer:
            heap = HeapReader(buffer).read()
            if len(heap.objects) > limits["max_objects"]:
                raise RuntimeError(f"heap object count exceeds limit: {len(heap.objects)}>{limits['max_objects']}")
            inbound = InboundReferences(heap.objects)
            retained = provide_retained_heap_with_caching(str(dump_path), heap, inbound)
            ranked = objects_sorted_by_retained_heap(heap, retained)
            hints = {
                str(item).lower()
                for item in options.get("object_type_hints", [])
                if str(item).strip()
            }
            selected = []
            for item in ranked:
                obj = heap.objects.get(item.addr)
                if obj is None:
                    continue
                type_name = str(heap.types.get(obj.type) or "unknown")
                if hints and not any(hint in type_name.lower() for hint in hints):
                    continue
                selected.append((obj, int(item.retained_heap or 0)))
                if len(selected) >= min(20, limits["max_paths"] * 2):
                    break
            thread_roots = self._thread_roots(heap)
            top = [self._object_summary(heap, obj, retained_bytes, limits["max_repr_length"]) for obj, retained_bytes in selected]
            paths: list[dict[str, Any]] = []
            for obj, retained_bytes in selected:
                for addresses in self._inbound_paths(
                    target=obj.address,
                    inbound=inbound,
                    objects=heap.objects,
                    thread_roots=thread_roots,
                    max_depth=limits["max_depth"],
                    remaining=limits["max_paths"] - len(paths),
                ):
                    path_index = len(paths)
                    nodes = [
                        self._object_summary(
                            heap,
                            heap.objects[address],
                            int(retained.get_for_object(address) or 0),
                            limits["max_repr_length"],
                        )
                        for address in addresses
                    ]
                    paths.append({
                        "path_id": f"pyheap_path_{path_index + 1}",
                        "root_type": nodes[0]["type"],
                        "target_type": nodes[-1]["type"],
                        "nodes": nodes,
                        "edges": [
                            {"from": nodes[index]["node_id"], "to": nodes[index + 1]["node_id"], "type": "references"}
                            for index in range(len(nodes) - 1)
                        ],
                        "retained_bytes": retained_bytes,
                        "evidence_ref": f"python_heap_reference.reference_paths[{path_index}]",
                    })
                    if len(paths) >= limits["max_paths"]:
                        break
                if len(paths) >= limits["max_paths"]:
                    break
            return {"top_retained_objects": top, "reference_paths": paths}

    @staticmethod
    def _thread_roots(heap: Any) -> set[int]:
        roots: set[int] = set()
        for thread in heap.threads:
            roots.update(int(item) for item in thread.locals if int(item) in heap.objects)
        return roots

    @staticmethod
    def _inbound_paths(
        *,
        target: int,
        inbound: Any,
        objects: dict[int, Any],
        thread_roots: set[int],
        max_depth: int,
        remaining: int,
    ) -> list[list[int]]:
        queue = deque([(target, [target])])
        paths: list[list[int]] = []
        visited_depth = {target: 0}
        while queue and len(paths) < remaining:
            current, reversed_path = queue.popleft()
            depth = len(reversed_path) - 1
            try:
                parents = sorted(int(item) for item in inbound[current] if int(item) in objects)
            except KeyError:
                parents = []
            if current in thread_roots or not parents:
                if len(reversed_path) > 1:
                    paths.append(list(reversed(reversed_path)))
                continue
            if depth >= max_depth:
                continue
            for parent in parents:
                if parent in reversed_path:
                    continue
                next_depth = depth + 1
                if visited_depth.get(parent, next_depth + 1) < next_depth:
                    continue
                visited_depth[parent] = next_depth
                queue.append((parent, [*reversed_path, parent]))
        return paths

    @staticmethod
    def _object_summary(heap: Any, obj: Any, retained_bytes: int, max_repr: int) -> dict[str, Any]:
        try:
            representation = str(obj.str_repr or "")[:max_repr]
        except (AttributeError, KeyError, OSError, ValueError):
            representation = ""
        return {
            "node_id": f"object_{int(obj.address):x}",
            "address": f"0x{int(obj.address):x}",
            "type": str(heap.types.get(obj.type) or "unknown")[:200],
            "size_bytes": int(obj.size or 0),
            "retained_bytes": max(0, int(retained_bytes or 0)),
            "repr": representation,
        }

    @classmethod
    def _limits(cls, options: dict[str, Any]) -> dict[str, int]:
        return {
            "max_dump_bytes": cls._bounded(options.get("max_dump_bytes"), cls.DEFAULT_MAX_DUMP_BYTES, 1024, 4 * 1024**3),
            "max_objects": cls._bounded(options.get("max_objects"), cls.DEFAULT_MAX_OBJECTS, 100, 5_000_000),
            "max_depth": cls._bounded(options.get("max_depth"), cls.DEFAULT_MAX_DEPTH, 1, 16),
            "max_paths": cls._bounded(options.get("max_paths"), cls.DEFAULT_MAX_PATHS, 1, 32),
            "max_repr_length": cls._bounded(options.get("max_repr_length"), cls.DEFAULT_MAX_REPR, 0, 500),
        }

    @staticmethod
    def _bounded(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _capture_capability_error(target_pid: int) -> tuple[str, str] | None:
        if sys.platform != "linux":
            return "unsupported_platform", "PyHeap 运行时采集仅支持 Linux"
        if not shutil.which("gdb"):
            return "gdb_not_installed", "GDB 不可用"
        if importlib.util.find_spec("pyheap_ui") is None:
            return "pyheap_analyzer_not_installed", "PyHeap analyzer 不可用"
        if not os.path.exists(f"/proc/{target_pid}"):
            return "target_process_missing", "目标进程不存在"
        ptrace_scope = PythonHeapReferenceCollector._read_int("/proc/sys/kernel/yama/ptrace_scope")
        if ptrace_scope is not None and ptrace_scope > 0 and os.geteuid() != 0:
            return "ptrace_permission_blocked", f"ptrace_scope={ptrace_scope} 且 Agent 非 root"
        return None

    @staticmethod
    def _dump_command(task: CollectorTask, dump_path: Path) -> list[str]:
        dumper = os.getenv("MINI_DROP_PYHEAP_DUMPER", "pyheap_dump").strip() or "pyheap_dump"
        base = [sys.executable, dumper] if dumper.endswith(".py") else [dumper]
        container_id = str(task.options.get("container_id") or "").strip()
        if container_id and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", container_id):
            return [*base, "--docker-container", container_id, "--file", str(dump_path)]
        return [*base, "--pid", str(task.target_pid), "--file", str(dump_path)]

    @staticmethod
    def _run(command: list[str], timeout: int):
        try:
            return subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=str(exc).encode())

    @staticmethod
    def _stderr(result: subprocess.CompletedProcess) -> str:
        value = result.stderr
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return str(value or "").strip()[-500:]

    @staticmethod
    def _read_int(path: str) -> int | None:
        try:
            return int(Path(path).read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _allowed(path: Path) -> bool:
        roots = [
            Path(item.strip()).resolve()
            for item in os.getenv(
                "MINI_DROP_PYHEAP_ROOTS", "/var/lib/mini-drop/profiles,/tmp/mini-drop"
            ).split(",")
            if item.strip()
        ]
        return any(path == root or root in path.parents for root in roots)

    def _blocked(self, output_dir: Path, reason: str, detail: str) -> CollectorResult:
        payload = {
            "schema_version": "1.0",
            "producer": "pyheap",
            "top_retained_objects": [],
            "reference_paths": [],
            "raw_artifact_refs": [],
            "evidence_validity": {
                "execution_status": "failed",
                "artifact_status": "produced",
                "evidence_status": "blocked",
                "reason": reason,
                "detail": detail[:500],
            },
        }
        path = output_dir / "python_heap_reference.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifact = {
            **self._artifact("python_heap_reference_json", path, "application/json"),
            "collector_family": "python_heap_reference",
            "metadata": {"data": payload},
        }
        return CollectorResult(ok=False, reason=detail, artifacts=[artifact])

    @staticmethod
    def _artifact(artifact_type: str, path: Path, content_type: str) -> dict[str, Any]:
        return {
            "artifact_type": artifact_type,
            "filename": path.name,
            "local_path": str(path),
            "content_type": content_type,
            "size_bytes": path.stat().st_size,
        }
