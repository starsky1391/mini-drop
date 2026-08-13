"""py-spy 用户态采集器：对 Python 进程进行采样并输出火焰图 SVG。

py-spy 通过读取目标进程内存直接获取 Python 调用栈，
无需修改目标代码或重启进程。
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class PySpyCollector:
    """py-spy sampling profiler。"""

    OUTPUT_BASE = "/tmp/mini-drop"

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

        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)
        svg_path = os.path.join(output_dir, "pyspy.svg")
        top_path = os.path.join(output_dir, "top.json")

        base_cmd = [
            pyspy, "record",
            "-p", str(task.target_pid),
            "-d", str(task.duration_sec),
            "-r", str(task.sample_rate),
            "-o", svg_path,
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

        if not os.path.isfile(svg_path) or os.path.getsize(svg_path) == 0:
            return CollectorResult(
                ok=False,
                reason="py-spy 未产出 SVG 文件",
            )

        top_functions = self._extract_top_functions(svg_path)
        with open(top_path, "w", encoding="utf-8") as fh:
            json.dump(top_functions, fh, ensure_ascii=False, indent=2)

        size = os.path.getsize(svg_path)
        return CollectorResult(
            ok=True,
            reason="py-spy 采集完成",
            artifacts=[
                {
                    "artifact_type": "flamegraph_svg",
                    "filename": "pyspy.svg",
                    "local_path": svg_path,
                    "content_type": "image/svg+xml",
                    "size_bytes": size,
                },
                {
                    "artifact_type": "top_json",
                    "filename": "top.json",
                    "local_path": top_path,
                    "content_type": "application/json",
                    "size_bytes": os.path.getsize(top_path),
                }
            ],
        )

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        return os.path.isdir(f"/proc/{pid}")

    @staticmethod
    def _should_retry_without_native(stderr: bytes) -> bool:
        text = stderr.decode("utf-8", errors="replace")
        return (
            "UNW_EBADREG" in text
            or "UNW_EINVAL" in text
            or "bad register number" in text
            or "unsupported operation or bad value" in text
        )

    @staticmethod
    def _extract_top_functions(svg_path: str, limit: int = 20) -> list[dict]:
        try:
            with open(svg_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return []
        counter: dict[str, dict[str, float | int | str]] = {}
        for raw_title in re.findall(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL):
            title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
            if not title:
                continue
            name = title.split(" (", 1)[0].strip()
            if not name or name.lower() in {"all", "root"}:
                continue
            samples = PySpyCollector._extract_number(title, (r"(\d+)\s+samples?", r"samples:\s*(\d+)"))
            percent = PySpyCollector._extract_number(title, (r"\(([\d.]+)%\)", r"([\d.]+)%"))
            item = counter.setdefault(name, {"name": name, "samples": 0, "percent": 0.0})
            item["samples"] = max(int(item["samples"]), int(samples))
            item["percent"] = max(float(item["percent"]), float(percent))
        items = list(counter.values())
        items.sort(key=lambda item: (-float(item.get("percent") or 0.0), -int(item.get("samples") or 0), str(item["name"])))
        return items[:limit]

    @staticmethod
    def _extract_number(text: str, patterns: tuple[str, ...]) -> float:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                return float(match.group(1))
            except ValueError:
                continue
        return 0.0
