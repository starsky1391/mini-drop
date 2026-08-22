"""Industrial Python heap evidence produced by Memray."""

from __future__ import annotations

import csv
import json
import os
import re
import signal
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
        native_live_tool = self._native_live_tool()
        managed_helper = self._helper_command(task, output_dir / "memray.bin")
        if not memray and not supplied_reports and not native_live_tool and not managed_helper:
            return self._blocked(output_dir, task, "memray_not_installed", "Memray 命令不可用")

        mode = "official_reports" if supplied_reports else "instrumented_result" if supplied else "attach"
        capture: Path | None = None
        attach_preflight: dict[str, Any] = {}
        if supplied:
            capture = Path(supplied).resolve()
            if not self._is_allowed(capture, "MINI_DROP_MEMRAY_ROOTS", "/var/lib/mini-drop/profiles,/tmp/mini-drop"):
                return self._blocked(output_dir, task, "result_path_not_allowed", "产物不在允许的 Memray 目录")
            if not capture.is_file() or capture.stat().st_size <= 0:
                return self._blocked(output_dir, task, "instrumented_result_missing", "Memray 官方产物不存在或为空")
        elif not supplied_reports:
            if not task.target_pid or not self._pid_exists(task.target_pid):
                return self._blocked(output_dir, task, "missing_target_pid", "目标 PID 不存在或不可访问")
            preflight = self._attach_preflight(task, helper_available=bool(managed_helper))
            attach_preflight = preflight
            if preflight["blocked_reason"]:
                return self._blocked(
                    output_dir,
                    task,
                    preflight["blocked_reason"],
                    preflight["detail"],
                    preflight=preflight,
                )
            if not memray and native_live_tool:
                return self._collect_native_live(
                    output_dir,
                    task,
                    native_live_tool,
                    preflight=preflight,
                    attach_failure=subprocess.CompletedProcess(
                        [],
                        127,
                        stdout=b"",
                        stderr=b"memray command not found",
                    ),
                    retry_failure=subprocess.CompletedProcess(
                        [],
                        127,
                        stdout=b"",
                        stderr=b"memray command not found",
                    ),
                )
            else:
                capture = output_dir / "memray.bin"
                # Prefer the managed helper when it is available. A plain CLI
                # attach can leave a ptrace session behind, causing the helper
                # to fail later with "already traced" even when the target is
                # healthy.
                if managed_helper:
                    result = self._run(
                        managed_helper,
                        task.duration_sec + 45,
                        process_group=True,
                    )
                    if result.returncode == 0 and capture.is_file() and capture.stat().st_size > 0:
                        attach_preflight = {
                            **preflight,
                            "attach_strategy": "managed_helper",
                        }
                        retry = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
                    else:
                        retry_capture = output_dir / "memray-retry.bin"
                        helper_failure_type = self._failure_type(result, capture)
                        retry_skipped_reason = self._retry_skip_reason(
                            result,
                            helper_failure_type,
                        )
                        retry_was_attempted = not bool(retry_skipped_reason)
                        retry = (
                            subprocess.CompletedProcess(
                                managed_helper,
                                125,
                                stdout=b"",
                                stderr=retry_skipped_reason.encode(),
                            )
                            if retry_skipped_reason
                            else self._run(
                                self._attach_command(memray, retry_capture, task, min(task.duration_sec, 5)),
                                max(30, min(task.duration_sec, 5) + 20),
                                process_group=True,
                            )
                        ) if memray else subprocess.CompletedProcess(
                            [],
                            127,
                            stdout=b"",
                            stderr=b"memray command not found",
                        )
                        if retry.returncode == 0 and retry_capture.is_file() and retry_capture.stat().st_size > 0:
                            capture = retry_capture
                            result = retry
                            attach_preflight = {
                                **preflight,
                                "attach_strategy": "managed_helper_then_memray_retry",
                            }
                        elif native_live_tool:
                            return self._collect_native_live(
                                output_dir,
                                task,
                                native_live_tool,
                                preflight=preflight,
                                attach_failure=result,
                                retry_failure=retry,
                            )
                        else:
                            return self._failed(
                                output_dir,
                                task,
                                "memray_attach_failed",
                                self._failure_detail(result, "Memray managed helper 未产出数据"),
                                failure_type=self._failure_type(result, capture),
                                exit_code=result.returncode,
                                stdout_excerpt=self._stdout_excerpt(result),
                                stderr_excerpt=self._stderr_excerpt(result),
                                retry_attempted=retry_was_attempted,
                                retry_exit_code=retry.returncode,
                                retry_stdout_excerpt=self._stdout_excerpt(retry),
                                retry_stderr_excerpt=self._stderr_excerpt(retry),
                                retry_skipped_reason=retry_skipped_reason,
                                helper_timeout=helper_failure_type == "timeout",
                                preflight={
                                    **preflight,
                                    "attach_strategy": "managed_helper_then_memray_retry",
                                },
                            )
                else:
                    result = self._run(
                        self._attach_command(memray, capture, task, task.duration_sec),
                        task.duration_sec + 45,
                        process_group=True,
                    )
                    if result.returncode != 0 or not capture.is_file() or capture.stat().st_size <= 0:
                        retry_capture = output_dir / "memray-retry.bin"
                        failure_type = self._failure_type(result, capture)
                        retry_skipped_reason = self._retry_skip_reason(result, failure_type)
                        retry_was_attempted = not bool(retry_skipped_reason)
                        retry = (
                            subprocess.CompletedProcess(
                                self._attach_command(memray, retry_capture, task, min(task.duration_sec, 5)),
                                125,
                                stdout=b"",
                                stderr=retry_skipped_reason.encode(),
                            )
                            if retry_skipped_reason
                            else self._run(
                                self._attach_command(memray, retry_capture, task, min(task.duration_sec, 5)),
                                max(30, min(task.duration_sec, 5) + 20),
                                process_group=True,
                            )
                        )
                        if retry.returncode == 0 and retry_capture.is_file() and retry_capture.stat().st_size > 0:
                            capture = retry_capture
                            attach_preflight = {
                                **preflight,
                                "attach_strategy": "memray_attach_retry",
                            }
                        elif native_live_tool:
                            return self._collect_native_live(
                                output_dir,
                                task,
                                native_live_tool,
                                preflight=preflight,
                                attach_failure=result,
                                retry_failure=retry,
                            )
                        else:
                            return self._failed(
                                output_dir,
                                task,
                                "memray_attach_failed",
                                self._failure_detail(result, "Memray attach 未产出数据"),
                                failure_type=self._failure_type(result, capture),
                                exit_code=result.returncode,
                                stdout_excerpt=self._stdout_excerpt(result),
                                stderr_excerpt=self._stderr_excerpt(result),
                                retry_attempted=retry_was_attempted,
                                retry_exit_code=retry.returncode,
                                retry_stdout_excerpt=self._stdout_excerpt(retry),
                                retry_stderr_excerpt=self._stderr_excerpt(retry),
                                retry_skipped_reason=retry_skipped_reason,
                                helper_timeout=failure_type == "timeout",
                                preflight={**preflight, "attach_strategy": "memray_attach_retry"},
                            )

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
                return self._failed(
                    output_dir,
                    task,
                    "memray_stats_failed",
                    self._failure_detail(stats, "Memray stats 未产出 JSON"),
                    failure_type=self._failure_type(stats, stats_path),
                    exit_code=stats.returncode,
                    stdout_excerpt=self._stdout_excerpt(stats),
                    stderr_excerpt=self._stderr_excerpt(stats),
                )
        official: dict[str, Any] = {}
        if stats_path is not None:
            try:
                official = json.loads(stats_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return self._failed(
                    output_dir,
                    task,
                    "memray_stats_invalid",
                    str(exc),
                    failure_type="output_unparseable",
                )

        leaks_path: Path | None = None
        retained: list[dict[str, Any]] = []
        if supplied_leaks:
            leaks_path = Path(supplied_leaks).resolve()
            if not self._is_allowed(leaks_path, "MINI_DROP_MEMRAY_ROOTS", "/var/lib/mini-drop/profiles,/tmp/mini-drop"):
                return self._blocked(output_dir, task, "leaks_path_not_allowed", "Memray leaks CSV 不在允许目录")
            try:
                retained = self._retained_hotspots(leaks_path)
            except (OSError, csv.Error, ValueError) as exc:
                return self._failed(
                    output_dir,
                    task,
                    "memray_leaks_invalid",
                    str(exc),
                    failure_type="output_unparseable",
                )

        payload = self._normalize_stats(
            official,
            mode,
            retained,
            has_capture=capture is not None,
            has_stats=stats_path is not None,
            has_leaks=leaks_path is not None,
            attach_preflight=attach_preflight,
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
    def _run(
        command: list[str],
        timeout: int,
        *,
        process_group: bool = False,
    ):
        if process_group:
            return PythonHeapCollector._run_process_group(command, timeout)
        try:
            return subprocess.run(command, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            stdout = getattr(exc, "stdout", b"") or b""
            stderr = getattr(exc, "stderr", b"") or b""
            if isinstance(stdout, str):
                stdout = stdout.encode()
            if isinstance(stderr, str):
                stderr = stderr.encode()
            return subprocess.CompletedProcess(
                command,
                124 if isinstance(exc, subprocess.TimeoutExpired) else 127,
                stdout=stdout,
                stderr=stderr + str(exc).encode(),
            )

    @staticmethod
    def _run_process_group(command: list[str], timeout: int):
        """Run attach helpers in an isolated process group and clean children on timeout."""
        if os.name != "posix":
            # The real attach path runs on Linux. Keep Windows development
            # tests on the existing subprocess.run seam.
            try:
                return subprocess.run(command, capture_output=True, timeout=timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                stdout = getattr(exc, "stdout", b"") or b""
                stderr = getattr(exc, "stderr", b"") or b""
                if isinstance(stdout, str):
                    stdout = stdout.encode()
                if isinstance(stderr, str):
                    stderr = stderr.encode()
                return subprocess.CompletedProcess(
                    command,
                    124 if isinstance(exc, subprocess.TimeoutExpired) else 127,
                    stdout=stdout,
                    stderr=stderr + str(exc).encode(),
                )
        return PythonHeapCollector._run_posix_process_group(command, timeout)

    @staticmethod
    def _run_posix_process_group(command: list[str], timeout: int):
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            return subprocess.CompletedProcess(
                command,
                127,
                stdout=b"",
                stderr=str(exc).encode(),
            )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            PythonHeapCollector._kill_process_group(process)
            stdout, stderr = process.communicate()
            stdout = stdout or getattr(exc, "stdout", b"") or b""
            stderr = stderr or getattr(exc, "stderr", b"") or b""
            return subprocess.CompletedProcess(
                command,
                124,
                stdout=stdout,
                stderr=stderr + b"\nmini_drop_attach_process_group_timeout",
            )
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=stdout or b"",
            stderr=stderr or b"",
        )

    @staticmethod
    def _kill_process_group(process: subprocess.Popen) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                    return
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, getattr(signal, "SIGKILL", 9))
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass

    @staticmethod
    def _attach_command(memray: str, capture: Path, task: CollectorTask, duration: int) -> list[str]:
        return [
            memray,
            "attach",
            "--output",
            str(capture),
            "--duration",
            str(duration),
            str(task.target_pid),
        ]

    @staticmethod
    def _helper_command(task: CollectorTask, capture: Path) -> list[str] | None:
        helper = str(os.getenv("MINI_DROP_MEMRAY_HELPER") or "").strip()
        if not helper or not os.path.isfile(helper) or not os.access(helper, os.X_OK):
            return None
        return [
            helper,
            "--pid",
            str(task.target_pid),
            "--duration",
            str(max(1, task.duration_sec)),
            "--output",
            str(capture),
        ]

    @staticmethod
    def _native_live_tool() -> str | None:
        configured = str(os.getenv("MINI_DROP_NATIVE_HEAP_LIVE_HELPER") or "").strip()
        if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        return None

    def _collect_native_live(
        self,
        output_dir: Path,
        task: CollectorTask,
        tool: str,
        *,
        preflight: dict[str, Any],
        attach_failure: subprocess.CompletedProcess,
        retry_failure: subprocess.CompletedProcess,
    ) -> CollectorResult:
        """Run a managed live native-allocation helper after Python attach fails.

        This result is deliberately partial: native allocator observations do
        not prove Python object retention and cannot create a Python heap root
        cause or source-line anchor.
        """
        raw_path = output_dir / "native_heap_live.txt"
        result = self._run(
            [
                tool,
                "--pid",
                str(task.target_pid),
                "--duration",
                str(max(1, task.duration_sec)),
                "--output",
                str(raw_path),
            ],
            task.duration_sec + 45,
            process_group=True,
        )
        if result.returncode != 0 or not raw_path.is_file() or raw_path.stat().st_size <= 0:
            return self._failed(
                output_dir,
                task,
                "memray_attach_failed",
                self._failure_detail(attach_failure, "Memray attach 与 native live helper 均未产出数据"),
                failure_type=self._failure_type(result, raw_path),
                exit_code=result.returncode,
                stdout_excerpt=self._stdout_excerpt(result),
                stderr_excerpt=self._stderr_excerpt(result) or self._stderr_excerpt(retry_failure),
                retry_attempted=True,
                retry_exit_code=retry_failure.returncode,
                retry_stdout_excerpt=self._stdout_excerpt(retry_failure),
                retry_stderr_excerpt=self._stderr_excerpt(retry_failure),
                retry_skipped_reason=self._stderr_excerpt(retry_failure)
                if retry_failure.returncode == 125
                else "",
                preflight={
                    **preflight,
                    "native_live_attempted": True,
                    "native_live_tool": Path(tool).name,
                },
            )
        payload = {
            "schema_version": "1.0",
            "producer": "native_allocator_live_helper",
            "mode": "native_live",
            "target_pid": task.target_pid,
            "heap_semantics": "native_allocation_observation",
            "allocation_hotspots": [],
            "retained_allocation_hotspots": [],
            "call_path_hotspots": [],
            "line_candidates": [],
            "raw_artifact_refs": ["artifact:native_heap_live"],
            "evidence_validity": {
                "execution_status": "completed",
                "artifact_status": "produced",
                "evidence_status": "partial",
                "reason": "native_allocator_observation_only",
                "detail": "Python heap attach 不可用，已降级为 native allocator 现场观察；不能证明 Python 对象 retention。",
                "attach_preflight": preflight,
                "attach_failure_output": self._combined_output_excerpt(attach_failure),
                "retry_failure_output": self._combined_output_excerpt(retry_failure),
                "attach_failure_reason": self._stderr_excerpt(attach_failure),
                "retry_failure_reason": self._stderr_excerpt(retry_failure),
            },
        }
        structured_path = output_dir / "python_heap_profile.json"
        structured_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts = [
            {
                **self._artifact("native_heap_live", raw_path, "text/plain"),
                "collector_family": "native_heap_live_profile",
            },
            {
                **self._artifact("python_heap_profile_json", structured_path, "application/json"),
                "collector_family": "python_heap_profile",
                "metadata": {"data": payload},
            },
        ]
        return CollectorResult(ok=True, reason="Memray 不可 attach，已降级为 native allocator 现场观察", artifacts=artifacts)

    @classmethod
    def _attach_preflight(cls, task: CollectorTask, *, helper_available: bool = False) -> dict[str, Any]:
        """Check attach prerequisites without claiming that attach succeeded."""
        pid = int(task.target_pid or 0)
        result: dict[str, Any] = {
            "target_pid": pid,
            "target_exists": cls._pid_exists(pid),
            "target_uid": None,
            "agent_uid": os.geteuid() if hasattr(os, "geteuid") else None,
            "same_pid_namespace": None,
            "same_mount_namespace": None,
            "ptrace_scope": None,
            "helper_available": bool(helper_available),
            "blocked_reason": "",
            "detail": "",
        }
        status_path = f"/proc/{pid}/status"
        try:
            for line in open(status_path, encoding="utf-8", errors="replace"):
                if line.startswith("Uid:"):
                    result["target_uid"] = int(line.split()[1])
                    break
        except FileNotFoundError:
            # Tests and non-Linux development hosts may provide a mocked PID
            # without procfs. Keep the preflight informational in that case.
            pass
        except (PermissionError, ValueError, IndexError):
            result["blocked_reason"] = "target_process_unstable"
            result["detail"] = "目标进程在 attach 预检期间不可读取"
            return result

        for name, key in (("pid", "same_pid_namespace"), ("mnt", "same_mount_namespace")):
            target_ns = f"/proc/{pid}/ns/{name}"
            self_ns = f"/proc/self/ns/{name}"
            try:
                result[key] = os.stat(target_ns).st_ino == os.stat(self_ns).st_ino
            except FileNotFoundError:
                result[key] = None
            except OSError:
                result[key] = False

        try:
            result["ptrace_scope"] = Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip()
        except OSError:
            result["ptrace_scope"] = None

        expected_uid = task.options.get("target_uid")
        if expected_uid not in (None, "") and result["target_uid"] is not None:
            try:
                if int(expected_uid) != int(result["target_uid"]):
                    result["blocked_reason"] = "permission_denied"
                    result["detail"] = "目标进程 UID 与诊断任务声明不一致"
                    return result
            except (TypeError, ValueError):
                result["blocked_reason"] = "permission_denied"
                result["detail"] = "目标 UID 声明不可解析"
                return result
        if result["same_pid_namespace"] is False and not (
            helper_available or task.options.get("allow_pid_namespace_mismatch")
        ):
            result["blocked_reason"] = "namespace_inaccessible"
            result["detail"] = "Agent 与目标进程不在同一 PID namespace，不能可靠 attach"
        elif result["same_pid_namespace"] is False:
            result["detail"] = "默认 attach 不在同一 PID namespace，将尝试容器内 helper"
        return result

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        return bool(pid) and os.path.isdir(f"/proc/{pid}")

    @staticmethod
    def _stdout_excerpt(result: subprocess.CompletedProcess) -> str:
        return result.stdout.decode("utf-8", errors="replace").strip()[:600]

    @staticmethod
    def _stderr_excerpt(result: subprocess.CompletedProcess) -> str:
        return result.stderr.decode("utf-8", errors="replace").strip()[:600]

    @classmethod
    def _combined_output_excerpt(cls, result: subprocess.CompletedProcess) -> str:
        parts = []
        stdout = cls._stdout_excerpt(result)
        stderr = cls._stderr_excerpt(result)
        if stdout:
            parts.append(f"stdout: {stdout}")
        if stderr:
            parts.append(f"stderr: {stderr}")
        return "\n".join(parts)[:1200]

    @classmethod
    def _failure_detail(cls, result: subprocess.CompletedProcess, fallback: str) -> str:
        return cls._combined_output_excerpt(result) or fallback

    @classmethod
    def _failure_type(cls, result: subprocess.CompletedProcess, output_path: Path) -> str:
        output = cls._combined_output_excerpt(result).lower()
        if result.returncode == 124 or "timeout" in output or "timed out" in output:
            return "timeout"
        if any(token in output for token in ("permission denied", "operation not permitted", "ptrace")):
            return "permission_denied"
        if any(token in output for token in ("namespace", "container", "pid namespace")):
            return "namespace_inaccessible"
        if any(token in output for token in ("incompatible", "unsupported", "not compatible")):
            return "incompatible_runtime"
        if result.returncode != 0:
            return "collector_exit_nonzero"
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            return "output_missing"
        return "collector_failed"

    @classmethod
    def _retry_skip_reason(
        cls,
        result: subprocess.CompletedProcess,
        failure_type: str,
    ) -> str:
        output = cls._combined_output_excerpt(result).lower()
        if failure_type == "timeout":
            return "retry_skipped_reason=managed_attach_timeout_process_state_uncertain"
        if failure_type == "permission_denied" and any(
            token in output
            for token in (
                "ptrace",
                "already traced",
                "operation not permitted",
                "failed to attach",
            )
        ):
            return "retry_skipped_reason=ptrace_conflict_or_attach_permission_denied"
        return ""

    @staticmethod
    def _normalize_stats(
        value: dict[str, Any],
        mode: str,
        retained: list[dict[str, Any]] | None = None,
        *,
        has_capture: bool = True,
        has_stats: bool = True,
        has_leaks: bool = False,
        attach_preflight: dict[str, Any] | None = None,
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
            "attach_preflight": attach_preflight or {},
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

    def _blocked(
        self,
        output_dir: Path,
        task: CollectorTask,
        reason: str,
        detail: str,
        *,
        preflight: dict[str, Any] | None = None,
    ) -> CollectorResult:
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
            "attach_preflight": preflight or {},
            "evidence_validity": {
                "execution_status": "failed",
                "artifact_status": "produced",
                "evidence_status": "blocked",
                "reason": reason,
                "detail": detail,
            },
        }
        return self._write_status_result(output_dir, task, payload, detail)

    def _failed(
        self,
        output_dir: Path,
        task: CollectorTask,
        reason: str,
        detail: str,
        *,
        failure_type: str,
        exit_code: int | None = None,
        stdout_excerpt: str = "",
        stderr_excerpt: str = "",
        retry_attempted: bool = False,
        retry_exit_code: int | None = None,
        retry_stdout_excerpt: str = "",
        retry_stderr_excerpt: str = "",
        retry_skipped_reason: str = "",
        helper_timeout: bool = False,
        preflight: dict[str, Any] | None = None,
    ) -> CollectorResult:
        payload = {
            "schema_version": "1.0",
            "producer": "memray",
            "mode": "failed",
            "target_pid": task.target_pid,
            "allocation_hotspots": [],
            "retained_allocation_hotspots": [],
            "call_path_hotspots": [],
            "line_candidates": [],
            "raw_artifact_refs": [],
            "attach_preflight": preflight or {},
            "evidence_validity": {
                "execution_status": "timed_out" if failure_type == "timeout" else "failed",
                "artifact_status": "missing",
                "evidence_status": "failed",
                "reason": reason,
                "detail": detail,
                "failure_type": failure_type,
                "exit_code": exit_code,
                "stdout_excerpt": stdout_excerpt,
                "stderr_excerpt": stderr_excerpt,
                "retry_attempted": retry_attempted,
                "retry_exit_code": retry_exit_code,
                "retry_stdout_excerpt": retry_stdout_excerpt,
                "retry_stderr_excerpt": retry_stderr_excerpt,
                "retry_skipped_reason": retry_skipped_reason,
                "helper_timeout": helper_timeout,
            },
        }
        return self._write_status_result(output_dir, task, payload, detail)

    @classmethod
    def _write_status_result(
        cls,
        output_dir: Path,
        task: CollectorTask,
        payload: dict[str, Any],
        detail: str,
    ) -> CollectorResult:
        path = output_dir / "python_heap_profile.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifact = {**cls._artifact("python_heap_profile_json", path, "application/json"), "collector_family": "python_heap_profile", "metadata": {"data": payload}}
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
