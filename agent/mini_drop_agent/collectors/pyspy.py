"""py-spy user-space collector with structured raw stack output.

py-spy 通过读取目标进程内存直接获取 Python 调用栈，
无需修改目标代码或重启进程。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class PySpyCollector:
    """py-spy sampling profiler。"""

    OUTPUT_BASE = "/tmp/mini-drop"
    _IDLE_NAMES = (
        "poll",
        "epoll_wait",
        "epoll_pwait",
        "select",
        "pselect",
        "recv",
        "recv_bytes",
        "drain_events",
        "wait",
        "sleep",
        "futex",
        "idle",
        "workloop",
    )
    _FRAMEWORK_NAMES = (
        "celery",
        "kombu",
        "billiard",
        "asyncio",
        "multiprocessing",
        "consumer",
        "hub",
    )

    def collect(self, task: CollectorTask) -> CollectorResult:
        pyspy = shutil.which("py-spy")
        if pyspy is None:
            return CollectorResult(
                ok=False,
                reason="py-spy 命令不可用，请通过 pip install py-spy 安装",
            )

        if not self._pid_exists(task.target_pid):
            return CollectorResult(
                ok=False,
                reason=f"目标 PID {task.target_pid} 不存在",
            )

        process_state = self._process_state(task.target_pid)
        if process_state in {"T", "t"}:
            output_dir = os.path.join(self.OUTPUT_BASE, task.id)
            os.makedirs(output_dir, exist_ok=True)
            status_path = os.path.join(output_dir, "pyspy_status.json")
            payload = {
                "schema_version": "1.0",
                "collector_family": "python_runtime_profile",
                "target_pid": task.target_pid,
                "process_state": process_state,
                "evidence_validity": {
                    "execution_status": "completed",
                    "artifact_status": "produced",
                    "evidence_status": "blocked",
                    "reason": "blocked_by_target_state",
                },
            }
            with open(status_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            return CollectorResult(
                ok=False,
                reason=f"blocked_by_target_state: PID {task.target_pid} state={process_state}",
                artifacts=[{
                    "artifact_type": "pyspy_status_json",
                    "filename": "pyspy_status.json",
                    "local_path": status_path,
                    "content_type": "application/json",
                    "size_bytes": os.path.getsize(status_path),
                    "collector_family": "python_runtime_profile",
                    "metadata": {"data": payload},
                }],
            )

        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)
        raw_path = os.path.join(output_dir, "pyspy.raw")
        top_path = os.path.join(output_dir, "top.json")
        stacks_path = os.path.join(output_dir, "stack_samples.json")

        base_cmd = [
            pyspy, "record",
            "-p", str(task.target_pid),
            "-d", str(task.duration_sec),
            "-r", str(task.sample_rate),
            "--full-filenames",
            "--format", "raw",
            "-o", raw_path,
        ]
        cmd = base_cmd + ["--native"]  # 同时显示 C 扩展调用帧

        timeout = task.duration_sec + 30

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CollectorResult(
                ok=False,
                reason=f"py-spy 执行超时 (>{timeout}s)",
            )
        except Exception as exc:
            return CollectorResult(
                ok=False,
                reason=f"py-spy 异常: {exc}",
            )

        if proc.returncode != 0 and self._should_retry_without_native(proc.stderr):
            try:
                proc = subprocess.run(
                    base_cmd,
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return CollectorResult(
                    ok=False,
                    reason=f"py-spy 降级重试超时 (>{timeout}s)",
                )
            except Exception as exc:
                return CollectorResult(
                    ok=False,
                    reason=f"py-spy 降级重试异常: {exc}",
                )

        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            return CollectorResult(
                ok=False,
                reason=f"py-spy 执行失败 (exit={proc.returncode}): {err[:200]}",
            )

        if not os.path.isfile(raw_path) or os.path.getsize(raw_path) == 0:
            return CollectorResult(
                ok=False,
                reason="py-spy 未产出 raw 折叠栈文件",
            )

        with open(raw_path, "r", encoding="utf-8", errors="replace") as fh:
            structured = self._parse_raw_text(fh.read())
        top_functions = structured["top_functions"]
        if not top_functions:
            return CollectorResult(ok=False, reason="py-spy raw 产物没有有效 Python 栈")
        with open(top_path, "w", encoding="utf-8") as fh:
            json.dump(top_functions, fh, ensure_ascii=False, indent=2)
        with open(stacks_path, "w", encoding="utf-8") as fh:
            json.dump(structured, fh, ensure_ascii=False, indent=2)

        return CollectorResult(
            ok=True,
            reason="py-spy raw 栈采集与结构化完成",
            artifacts=[
                {
                    "artifact_type": "pyspy_raw",
                    "filename": "pyspy.raw",
                    "local_path": raw_path,
                    "content_type": "text/plain",
                    "size_bytes": os.path.getsize(raw_path),
                },
                {
                    "artifact_type": "top_json",
                    "filename": "top.json",
                    "local_path": top_path,
                    "content_type": "application/json",
                    "size_bytes": os.path.getsize(top_path),
                    "metadata": {"data": top_functions},
                },
                {
                    "artifact_type": "python_stack_samples_json",
                    "filename": "stack_samples.json",
                    "local_path": stacks_path,
                    "content_type": "application/json",
                    "size_bytes": os.path.getsize(stacks_path),
                    "collector_family": "python_runtime_profile",
                    "metadata": {"data": structured},
                },
            ],
        )

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        return os.path.isdir(f"/proc/{pid}")

    @staticmethod
    def _process_state(pid: int) -> str:
        try:
            text = open(f"/proc/{pid}/stat", "r", encoding="utf-8").read()
        except OSError:
            return ""
        tail = text[text.rfind(")") + 1 :].strip().split()
        return tail[0] if tail else ""

    @staticmethod
    def _should_retry_without_native(stderr: bytes) -> bool:
        text = stderr.decode("utf-8", errors="replace")
        return (
            "UNW_EBADREG" in text
            or "UNW_EINVAL" in text
            or "bad register number" in text
            or "unsupported operation or bad value" in text
            or "failed to get os threadid" in text.lower()
        )

    @staticmethod
    def _parse_raw_text(text: str, limit: int = 20) -> dict:
        stacks: list[dict] = []
        leaf_counts: dict[tuple[str, str, int], int] = {}
        leaf_paths: dict[tuple[str, str, int], list[str]] = {}
        leaf_state_counts: dict[tuple[str, str, int], dict[str, int]] = {}
        frame_counts: dict[tuple[str, str, int], int] = {}
        frame_paths: dict[tuple[str, str, int], dict[tuple[str, ...], int]] = {}
        frame_depths: dict[tuple[str, str, int], dict[int, int]] = {}
        state_counts: dict[str, int] = {}
        total_samples = 0
        for raw_line in text.splitlines():
            match = re.match(r"^(.*)\s+(-?\d+)\s*$", raw_line.strip())
            if not match:
                continue
            count = int(match.group(2))
            if count <= 0:
                continue
            frames = [PySpyCollector._parse_frame(item) for item in match.group(1).split(";")]
            frames = [frame for frame in frames if frame is not None]
            if not frames:
                continue
            leaf = frames[-1]
            if PySpyCollector._invalid_anchor(leaf["name"]):
                continue
            sample_state = PySpyCollector._classify_state(frames)
            state_counts[sample_state] = state_counts.get(sample_state, 0) + count
            total_samples += count
            names = [frame["name"] for frame in frames]
            key = (leaf["name"], leaf["file"], leaf["line"])
            leaf_counts[key] = leaf_counts.get(key, 0) + count
            leaf_paths.setdefault(key, names)
            state_bucket = leaf_state_counts.setdefault(key, {})
            state_bucket[sample_state] = state_bucket.get(sample_state, 0) + count
            for frame_index, frame in enumerate(frames):
                if PySpyCollector._invalid_anchor(frame["name"]):
                    continue
                frame_key = (frame["name"], frame["file"], frame["line"])
                frame_counts[frame_key] = frame_counts.get(frame_key, 0) + count
                path_key = tuple(names)
                paths = frame_paths.setdefault(frame_key, {})
                paths[path_key] = paths.get(path_key, 0) + count
                depth_from_leaf = len(frames) - frame_index - 1
                depths = frame_depths.setdefault(frame_key, {})
                depths[depth_from_leaf] = depths.get(depth_from_leaf, 0) + count
            stacks.append({
                "frames": frames,
                "call_path": names,
                "hot_frame": leaf["name"],
                "file": leaf["file"],
                "line": leaf["line"],
                "sample_count": count,
                "sample_state": sample_state,
            })

        for item in stacks:
            item["percent"] = round(item["sample_count"] / total_samples * 100.0, 2) if total_samples else 0.0
        top_functions = []
        for (name, file_name, line), count in leaf_counts.items():
            state_bucket = leaf_state_counts.get((name, file_name, line), {})
            state = max(state_bucket.items(), key=lambda item: (item[1], item[0]))[0] if state_bucket else PySpyCollector._classify_call_path(leaf_paths[(name, file_name, line)])
            top_functions.append({
                "name": name,
                "file": file_name,
                "line": line,
                "samples": count,
                "percent": round(count / total_samples * 100.0, 2) if total_samples else 0.0,
                "call_path": leaf_paths[(name, file_name, line)],
                "state": state,
            })
        top_functions.sort(key=lambda item: (-item["samples"], item["name"], item["file"], item["line"]))
        top_functions = top_functions[:limit]
        for item in top_functions:
            item["is_idle_like"] = item["state"] in {"blocked_io", "scheduler_wait", "idle_loop"}
        line_candidates = [
            {
                "symbol": item["name"],
                "file": item["file"],
                "line": item["line"],
                "samples": item["samples"],
                "percent": item["percent"],
                "call_path": item["call_path"],
            }
            for item in top_functions
            if item["file"] and item["line"] > 0
        ]
        source_line_candidates = []
        for (name, file_name, line), count in frame_counts.items():
            if not file_name or line <= 0:
                continue
            paths = frame_paths[(name, file_name, line)]
            call_path = list(max(paths.items(), key=lambda item: (item[1], item[0]))[0])
            depth = max(
                frame_depths[(name, file_name, line)].items(),
                key=lambda item: (item[1], -item[0]),
            )[0]
            source_line_candidates.append({
                "symbol": name,
                "file": file_name,
                "line": line,
                "samples": count,
                "percent": round(count / total_samples * 100.0, 2) if total_samples else 0.0,
                "call_path": call_path,
                "frame_depth": depth,
                "frame_type": "leaf" if depth == 0 else "intermediate",
            })
        source_line_candidates.sort(
            key=lambda item: (
                0 if item["frame_type"] == "leaf" else 1,
                -item["samples"],
                item["frame_depth"],
                item["symbol"],
                item["file"],
                item["line"],
            )
        )
        filtered_idle_frames = [item for item in top_functions if item.get("is_idle_like")]
        candidate_frames = [item for item in top_functions if not item.get("is_idle_like")]
        sample_quality = PySpyCollector._sample_quality(total_samples, state_counts)
        return {
            "schema_version": "1.0",
            "producer": "py-spy",
            "format": "raw_collapsed",
            "total_samples": total_samples,
            "stack_samples": stacks,
            "top_functions": top_functions,
            "candidate_frames": candidate_frames[:limit],
            "filtered_idle_frames": filtered_idle_frames[:limit],
            "sample_quality": sample_quality,
            "call_path_hotspots": stacks[:limit],
            "line_candidates": source_line_candidates[: max(limit * 4, len(line_candidates))],
            "evidence_validity": {
                "execution_status": "completed",
                "artifact_status": "produced",
                "evidence_status": "valid" if top_functions else "insufficient",
                "reason": "structured_raw_stacks" if top_functions else "no_valid_stack_anchor",
            },
        }

    @staticmethod
    def _parse_frame(value: str) -> dict | None:
        value = value.strip()
        if not value:
            return None
        match = re.match(r"^(.*?)\s+\((.*):(\d+)\)$", value)
        if not match:
            return {"name": value, "file": "", "line": 0}
        return {"name": match.group(1).strip(), "file": match.group(2).strip(), "line": int(match.group(3))}

    @staticmethod
    def _invalid_anchor(name: str) -> bool:
        normalized = name.strip().lower()
        return not normalized or normalized in {"[unknown]", "unknown", "all", "root"} or bool(
            re.fullmatch(r"(?:0x)?[0-9a-f]+", normalized)
        )

    @classmethod
    def _classify_call_path(cls, call_path: list[str]) -> str:
        text = " ".join(call_path).lower()
        if any(token in text for token in ("recv", "drain_events", "poll", "select", "epoll_wait", "epoll_pwait")):
            return "blocked_io"
        if any(token in text for token in ("sleep", "wait", "futex", "park", "idle", "workloop")):
            return "scheduler_wait"
        leaf = call_path[-1].lower() if call_path else ""
        if cls._is_framework_loop_leaf(leaf):
            return "framework_loop"
        if any(token in leaf for token in ("celery/", "kombu/", "billiard/", "asyncio/", "multiprocessing/")):
            return "executing"
        if any(token in text for token in ("celery", "kombu", "billiard", "asyncio", "multiprocessing", "consumer", "hub")):
            return "framework_loop"
        if any(token in leaf for token in ("/opt/", "/home/", "/src/", "site-packages/")):
            return "executing"
        return "unknown"

    @classmethod
    def _classify_state(cls, frames: list[dict]) -> str:
        if not frames:
            return "unknown"
        names = " ".join(str(frame.get("name") or "") for frame in frames).lower()
        files = " ".join(str(frame.get("file") or "") for frame in frames).lower()
        combined = f"{names} {files}"
        if any(token in combined for token in ("recv", "drain_events", "poll", "select", "epoll_wait", "epoll_pwait")):
            return "blocked_io"
        if any(token in combined for token in ("sleep", "wait", "futex", "park", "idle", "workloop")):
            return "scheduler_wait"
        leaf = frames[-1]
        leaf_name = str(leaf.get("name") or "").lower()
        leaf_file = str(leaf.get("file") or "").lower()
        if cls._is_framework_loop_leaf(f"{leaf_name} {leaf_file}"):
            return "framework_loop"
        if leaf_file and leaf.get("line", 0):
            return "executing"
        if any(token in combined for token in ("celery", "kombu", "billiard", "asyncio", "multiprocessing", "consumer", "hub")):
            return "framework_loop"
        if any(token in combined for token in ("celery/", "/opt/", "/home/", "/src/", "site-packages/celery", "celery_case_tasks")):
            return "executing"
        return "unknown"

    @staticmethod
    def _is_framework_loop_leaf(value: str) -> bool:
        return any(
            token in value
            for token in (
                "consumer",
                "drain_events",
                "event_loop",
                "asynloop",
                "workloop",
                "hub",
                "kombu.transport",
                "billiard.pool",
            )
        )

    @classmethod
    def _sample_quality(cls, total_samples: int, state_counts: dict[str, int]) -> dict:
        if total_samples <= 0:
            return {
                "diagnostic_value": "low",
                "dominant_state": "unknown",
                "non_idle_ratio": 0.0,
                "primitive_frame_ratio": 0.0,
                "framework_loop_ratio": 0.0,
                "target_code_ratio": 0.0,
                "sample_count": 0,
                "stable_across_samples": False,
                "reason": "没有可解析的 py-spy 栈样本。",
            }

        dominant_state = "unknown"
        dominant_count = 0
        for state, count in state_counts.items():
            if count > dominant_count or (count == dominant_count and state < dominant_state):
                dominant_state = state
                dominant_count = count

        primitive_count = sum(state_counts.get(state, 0) for state in {"blocked_io", "scheduler_wait"})
        framework_count = state_counts.get("framework_loop", 0)
        target_count = state_counts.get("executing", 0)
        non_idle_ratio = round(max(0.0, (total_samples - primitive_count) / total_samples), 4)
        primitive_ratio = round(primitive_count / total_samples, 4)
        framework_ratio = round(framework_count / total_samples, 4)
        target_ratio = round(target_count / total_samples, 4)

        if dominant_state == "executing" and target_ratio >= 0.35 and non_idle_ratio >= 0.4:
            diagnostic_value = "high"
            reason = "样本主要落在目标代码路径，且空闲/等待帧占比较低。"
        elif target_ratio >= 0.15 and non_idle_ratio >= 0.25:
            diagnostic_value = "medium"
            reason = "样本含有一定目标代码路径，但仍混有框架循环或等待帧，需要交叉证据。"
        else:
            diagnostic_value = "low"
            if dominant_state in {"blocked_io", "scheduler_wait", "framework_loop"}:
                reason = f"样本主要集中在 {dominant_state}，缺少可升级为根因的业务执行栈。"
            else:
                reason = "样本对目标代码的诊断价值不足。"

        stable_across_samples = dominant_count >= max(3, int(total_samples * 0.5))
        return {
            "diagnostic_value": diagnostic_value,
            "dominant_state": dominant_state,
            "non_idle_ratio": round(non_idle_ratio, 4),
            "primitive_frame_ratio": round(primitive_ratio, 4),
            "framework_loop_ratio": round(framework_ratio, 4),
            "target_code_ratio": round(target_ratio, 4),
            "sample_count": total_samples,
            "stable_across_samples": stable_across_samples,
            "reason": reason,
        }
