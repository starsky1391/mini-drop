"""Go pprof 采集器。

通过 net/http/pprof HTTP 端点对 Go 进程进行 CPU/Heap profile 采集。

前置条件：
  1. 目标 Go 程序已启用 net/http/pprof（import _ "net/http/pprof"）
  2. pprof HTTP 端口可访问（默认 6060，可通过 options.port 指定）
  3. Agent 可与目标进程网络互通

执行流程：
  1. 构造 pprof URL
  2. HTTP GET 拉取 profile（阻塞 duration_sec 秒）
  3. 保存原始 pprof 数据（protocol buffer gzip）
  4. 尝试 go tool pprof 生成 SVG 火焰图（可选）
  5. 返回产物元数据
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class PprofCollector:
    """Go pprof HTTP 采集器。"""

    OUTPUT_BASE = "/tmp/mini-drop"
    DEFAULT_PORT = 6060
    DEFAULT_ENDPOINT = "/debug/pprof/profile"
    DEFAULT_HEAP_ENDPOINT = "/debug/pprof/heap"
    HOTSPOT_LIMIT = 20

    def collect(self, task: CollectorTask) -> CollectorResult:
        port = task.options.get("port", self.DEFAULT_PORT)
        profile_kind = str(task.options.get("profile_kind") or "cpu").lower()
        if profile_kind not in {"cpu", "heap"}:
            return CollectorResult(ok=False, reason=f"无效的 profile_kind: {profile_kind}")
        default_endpoint = self.DEFAULT_HEAP_ENDPOINT if profile_kind == "heap" else self.DEFAULT_ENDPOINT
        endpoint = task.options.get("pprof_endpoint", default_endpoint)

        # 输入校验
        if not isinstance(port, int) or port < 1 or port > 65535:
            return CollectorResult(ok=False, reason=f"无效的端口: {port}")
        if not isinstance(endpoint, str) or not endpoint.startswith("/"):
            return CollectorResult(ok=False, reason=f"无效的 endpoint: {endpoint}，必须以 / 开头")

        timeout = task.duration_sec + 30

        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)
        pprof_raw_name = "heap.pb.gz" if profile_kind == "heap" else "profile.pb.gz"
        pprof_raw = os.path.join(output_dir, pprof_raw_name)
        flamegraph_svg = os.path.join(output_dir, "flamegraph.svg")
        pprof_top_text = os.path.join(output_dir, "pprof-top-lines.txt")
        heap_json = os.path.join(output_dir, "go_heap_profile.json")

        # 先用 URL 拉取原始 pprof 数据
        try:
            import urllib.request
            import urllib.error

            url = self._profile_url(port, endpoint, task.duration_sec, profile_kind)

            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()

            if not data:
                return CollectorResult(
                    ok=False,
                    reason=f"pprof {url} 返回空数据，目标 Go 进程可能未启用 pprof",
                )

            with open(pprof_raw, "wb") as fh:
                fh.write(data)

        except urllib.error.HTTPError as exc:
            return CollectorResult(
                ok=False,
                reason=f"pprof HTTP {exc.code}: {url}，请确认目标进程已启用 net/http/pprof",
            )
        except urllib.error.URLError as exc:
            return CollectorResult(
                ok=False,
                reason=f"pprof 连接失败: {exc.reason}，请确认端口 {port} 可访问",
            )
        except Exception as exc:
            return CollectorResult(
                ok=False,
                reason=f"pprof 采集异常: {exc}",
            )

        raw_size = os.path.getsize(pprof_raw) if os.path.isfile(pprof_raw) else 0
        artifacts: list[dict] = [{
            "artifact_type": "pprof_raw",
            "filename": pprof_raw_name,
            "local_path": pprof_raw,
            "content_type": "application/octet-stream",
            "size_bytes": raw_size,
            "metadata": {
                "profile_kind": profile_kind,
                "endpoint": endpoint,
            },
        }]

        if profile_kind == "heap":
            heap_payload, top_generated = self._build_heap_profile_json(task, pprof_raw, pprof_top_text)
            with open(heap_json, "w", encoding="utf-8") as fh:
                json.dump(heap_payload, fh, ensure_ascii=False, indent=2)
            if top_generated and os.path.isfile(pprof_top_text):
                artifacts.append({
                    "artifact_type": "pprof_top_text",
                    "filename": "pprof-top-lines.txt",
                    "local_path": pprof_top_text,
                    "content_type": "text/plain",
                    "size_bytes": os.path.getsize(pprof_top_text),
                })
            artifacts.append({
                "artifact_type": "go_heap_profile_json",
                "filename": "go_heap_profile.json",
                "local_path": heap_json,
                "content_type": "application/json",
                "size_bytes": os.path.getsize(heap_json),
                "metadata": {"data": heap_payload},
            })
            status = heap_payload.get("evidence_validity", {}).get("evidence_status", "unparseable")
            return CollectorResult(
                ok=True,
                reason=f"Go heap pprof 采集完成，{raw_size} 字节，结构化状态 {status}",
                artifacts=artifacts,
            )

        # 可选：用 go tool pprof 生成 SVG 火焰图
        svg_ok = self._pprof_to_svg(pprof_raw, flamegraph_svg, timeout=60)
        if svg_ok and os.path.isfile(flamegraph_svg):
            svg_size = os.path.getsize(flamegraph_svg)
            artifacts.append({
                "artifact_type": "flamegraph_svg",
                "filename": "flamegraph.svg",
                "local_path": flamegraph_svg,
                "content_type": "image/svg+xml",
                "size_bytes": svg_size,
            })

        return CollectorResult(
            ok=True,
            reason=f"pprof 采集完成，{raw_size} 字节" + ("，已生成火焰图" if svg_ok else "（go 未安装，跳过 SVG 生成）"),
            artifacts=artifacts,
        )

    # ── 内部方法 ────────────────────────────────────────────────

    @staticmethod
    def _profile_url(port: int, endpoint: str, duration: int, profile_kind: str) -> str:
        parts = urlsplit(endpoint)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if profile_kind == "heap":
            query.setdefault("gc", "1")
        else:
            query.setdefault("seconds", str(duration))
        path = parts.path or endpoint.split("?", 1)[0]
        return urlunsplit(("http", f"localhost:{port}", path, urlencode(query), ""))

    @classmethod
    def _build_heap_profile_json(
        cls,
        task: CollectorTask,
        raw_path: str,
        top_text_path: str,
    ) -> tuple[dict, bool]:
        go_bin = cls._find_go()
        sample_type = str(task.options.get("sample_type") or task.options.get("heap_sample_type") or "inuse_space")
        if go_bin is None:
            return cls._heap_payload(
                task,
                hotspots=[],
                line_candidates=[],
                evidence_status="blocked",
                reason="go tool 不可用，无法解析 heap pprof；原始 profile 已保存。",
                parser_status="blocked",
                raw_artifact_refs=["pprof_raw"],
            ), False
        try:
            proc = subprocess.run(
                [go_bin, "tool", "pprof", "-sample_index", sample_type, "-top", "-lines", raw_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return cls._heap_payload(
                task,
                hotspots=[],
                line_candidates=[],
                evidence_status="blocked",
                reason="go tool pprof -top -lines 超时；原始 profile 已保存。",
                parser_status="timeout",
                raw_artifact_refs=["pprof_raw"],
            ), False
        except (subprocess.SubprocessError, OSError) as exc:
            return cls._heap_payload(
                task,
                hotspots=[],
                line_candidates=[],
                evidence_status="blocked",
                reason=f"go tool pprof 执行失败: {exc}",
                parser_status="blocked",
                raw_artifact_refs=["pprof_raw"],
            ), False
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        with open(top_text_path, "w", encoding="utf-8") as fh:
            fh.write(output)
        if proc.returncode != 0:
            return cls._heap_payload(
                task,
                hotspots=[],
                line_candidates=[],
                evidence_status="unparseable",
                reason="go tool pprof 未能解析 heap profile。",
                parser_status="failed",
                raw_artifact_refs=["pprof_raw", "pprof_top_text"],
            ), True
        hotspots = cls._parse_heap_top_lines(output, sample_type=sample_type)
        status = "valid" if hotspots else "empty_window"
        reason = "Go heap pprof 解析出分配或 in-use 热点。" if hotspots else "Go heap pprof 未解析出有效热点。"
        return cls._heap_payload(
            task,
            hotspots=hotspots,
            line_candidates=cls._line_candidates(hotspots),
            evidence_status=status,
            reason=reason,
            parser_status="ok",
            raw_artifact_refs=["pprof_raw", "pprof_top_text"],
        ), True

    @classmethod
    def _heap_payload(
        cls,
        task: CollectorTask,
        *,
        hotspots: list[dict],
        line_candidates: list[dict],
        evidence_status: str,
        reason: str,
        parser_status: str,
        raw_artifact_refs: list[str],
    ) -> dict:
        sample_type = str(task.options.get("sample_type") or task.options.get("heap_sample_type") or "inuse_space")
        return {
            "schema_version": 1,
            "producer": "go pprof",
            "collector_type": task.collector_type,
            "profile_kind": "heap",
            "heap_mode": "gc=1",
            "sample_type": sample_type,
            "target_pid": task.target_pid,
            "duration_sec": task.duration_sec,
            "summary": {
                "hotspot_count": len(hotspots),
                "line_candidate_count": len(line_candidates),
                "top_function": hotspots[0].get("function") if hotspots else "",
                "top_file": hotspots[0].get("file") if hotspots else "",
                "top_line": hotspots[0].get("line") if hotspots else 0,
                "total_flat_bytes": sum(int(item.get("flat_bytes") or 0) for item in hotspots),
                "total_cum_bytes": sum(int(item.get("cum_bytes") or 0) for item in hotspots),
            },
            "hotspots": hotspots[:cls.HOTSPOT_LIMIT],
            "line_candidates": line_candidates[:cls.HOTSPOT_LIMIT],
            "raw_artifact_refs": raw_artifact_refs,
            "evidence_validity": {
                "evidence_status": evidence_status,
                "reason": reason,
                "parser_status": parser_status,
                "conclusion_scope": "allocation_hotspot_only",
                "root_cause_claim_allowed": False,
            },
        }

    @classmethod
    def _parse_heap_top_lines(cls, text: str, *, sample_type: str = "inuse_space") -> list[dict]:
        hotspots: list[dict] = []
        in_table = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("flat ") and "cum" in line:
                in_table = True
                continue
            if not in_table or line.startswith(("Showing nodes", "Dropped nodes")):
                continue
            item = cls._parse_heap_row(line, sample_type=sample_type)
            if item:
                item["evidence_ref"] = f"go_heap_profile.hotspots[{len(hotspots)}]"
                hotspots.append(item)
            if len(hotspots) >= cls.HOTSPOT_LIMIT:
                break
        hotspots.sort(key=lambda item: (-int(item.get("flat_bytes") or 0), -float(item.get("flat_percent") or 0.0), item["function"]))
        for index, item in enumerate(hotspots):
            item["evidence_ref"] = f"go_heap_profile.hotspots[{index}]"
        return hotspots

    @staticmethod
    def _parse_heap_row(line: str, *, sample_type: str = "inuse_space") -> dict | None:
        parts = line.split()
        if len(parts) < 6 or not parts[1].endswith("%") or not parts[4].endswith("%"):
            return None
        flat_bytes = _parse_pprof_size(parts[0])
        flat_percent = _parse_percent(parts[1])
        cum_bytes = _parse_pprof_size(parts[3])
        cum_percent = _parse_percent(parts[4])
        name_parts = parts[5:]
        location = " ".join(name_parts)
        if not location:
            return None
        function = location
        file_name = ""
        line_no = 0
        last_name = name_parts[-1] if name_parts else ""
        match = re.search(r"(.+?):(\d+)$", last_name)
        if match:
            file_name = match.group(1)
            line_no = int(match.group(2))
            function = " ".join(name_parts[:-1]).strip() or os.path.basename(file_name)
        if flat_bytes <= 0 and cum_bytes <= 0:
            return None
        return {
            "function": function,
            "file": file_name,
            "line": line_no,
            "flat_bytes": flat_bytes,
            "cum_bytes": cum_bytes,
            "flat_percent": round(flat_percent, 2),
            "cum_percent": round(cum_percent, 2),
            "sample_type": sample_type,
        }

    @staticmethod
    def _line_candidates(hotspots: list[dict]) -> list[dict]:
        candidates: list[dict] = []
        for item in hotspots:
            file_name = str(item.get("file") or "")
            line = int(item.get("line") or 0)
            if not file_name or line <= 0:
                continue
            candidate = {
                "file": file_name,
                "line": line,
                "symbol": str(item.get("function") or ""),
                "function": str(item.get("function") or ""),
                "evidence_ref": str(item.get("evidence_ref") or ""),
            }
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    @staticmethod
    def _pprof_to_svg(raw_path: str, output_path: str, timeout: int = 60) -> bool:
        go_bin = PprofCollector._find_go()
        if go_bin is None:
            return False

        try:
            proc = subprocess.run(
                [go_bin, "tool", "pprof", "-svg", "-output", output_path, raw_path],
                capture_output=True,
                timeout=timeout,
            )
            return proc.returncode == 0 and os.path.isfile(output_path)
        except (subprocess.SubprocessError, OSError):
            return False

    @staticmethod
    def _find_go() -> str | None:
        import shutil
        return shutil.which("go")


def _parse_percent(value: str) -> float:
    try:
        return float(value.rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def _parse_pprof_size(value: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]*)", value.strip())
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = {
        "b": 1,
        "kb": 1024,
        "kib": 1024,
        "mb": 1024 ** 2,
        "mib": 1024 ** 2,
        "gb": 1024 ** 3,
        "gib": 1024 ** 3,
    }.get(unit, 1)
    return int(number * multiplier)
