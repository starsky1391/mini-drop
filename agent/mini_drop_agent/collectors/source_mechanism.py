"""Bounded CodeQL source mechanism evidence collector."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from server.app.diagnosis.codeql_query_guard import validate_ai_generated_codeql_query


class SourceMechanismCollector:
    OUTPUT_BASE = "/tmp/mini-drop"
    MAX_PATHS = 12
    MAX_NODES = 32
    MAX_MESSAGE_LENGTH = 300

    def collect(self, task: CollectorTask) -> CollectorResult:
        output_dir = Path(self.OUTPUT_BASE) / task.id
        output_dir.mkdir(parents=True, exist_ok=True)
        source_root = Path(str(task.options.get("source_root") or "")).resolve()
        revision = str(task.options.get("source_revision") or task.options.get("repo_revision") or "").strip()
        anchors = self._anchors(task.options.get("line_candidates"))
        if not self._allowed(source_root, "MINI_DROP_SOURCE_ROOTS", "/host/home,/usr/src"):
            return self._blocked(output_dir, "source_root_not_allowed", "源码目录不在允许根目录内")
        if not source_root.is_dir() or not (source_root / ".git").exists():
            return self._blocked(output_dir, "source_repository_missing", "源码目录不是可用的 Git 仓库")
        if not revision:
            return self._blocked(output_dir, "source_revision_missing", "缺少准确的源码 revision")
        if not anchors:
            return self._blocked(output_dir, "line_anchor_missing", "缺少已验证的 file:line 锚点")

        actual = self._git(source_root, ["rev-parse", "HEAD"])
        expected = self._git(source_root, ["rev-parse", revision])
        if not actual or not expected or actual != expected:
            return self._blocked(
                output_dir,
                "source_revision_mismatch",
                f"源码 revision 不匹配: expected={expected or revision}, actual={actual or 'unknown'}",
                revision=actual or revision,
            )

        pack_version = os.getenv("MINI_DROP_CODEQL_QUERY_PACK_VERSION", "unversioned").strip() or "unversioned"
        identity = self._git(source_root, ["config", "--get", "remote.origin.url"]) or str(source_root)
        cache_key = hashlib.sha256(
            f"{identity}\0{actual}\0python\0{pack_version}".encode("utf-8")
        ).hexdigest()
        cache_root = Path(os.getenv("MINI_DROP_CODEQL_CACHE_ROOT", "/var/lib/mini-drop/codeql")).resolve()
        database = cache_root / cache_key / "database"
        cache_ready = database.parent / ".mini-drop-ready"
        cache_hit = database.is_dir() and cache_ready.is_file()

        supplied_sarif = str(task.options.get("codeql_sarif_path") or "").strip()
        generated_query = task.options.get("ai_generated_query")
        query_artifact: Path | None = None
        query_metadata: dict[str, str] = {}
        if supplied_sarif:
            sarif_path = Path(supplied_sarif).resolve()
            if not self._allowed(
                sarif_path,
                "MINI_DROP_CODEQL_ARTIFACT_ROOTS",
                f"{cache_root},{self.OUTPUT_BASE}",
            ):
                return self._blocked(output_dir, "sarif_path_not_allowed", "CodeQL SARIF 不在受管理目录")
            if not sarif_path.is_file() or sarif_path.stat().st_size <= 0:
                return self._blocked(output_dir, "sarif_missing", "CodeQL SARIF 不存在或为空")
        else:
            codeql = shutil.which("codeql")
            suite_text = os.getenv("MINI_DROP_CODEQL_QUERY_SUITE", "").strip()
            if not codeql:
                return self._blocked(output_dir, "codeql_not_installed", "CodeQL CLI 不可用", revision=actual)
            if generated_query:
                try:
                    query_metadata = validate_ai_generated_codeql_query(generated_query)
                except ValueError as exc:
                    return self._blocked(output_dir, "ai_generated_query_rejected", str(exc), revision=actual)
                query_artifact = output_dir / "ai-investigation.ql"
                query_artifact.write_text(query_metadata["query"], encoding="utf-8")
                query_source = query_artifact
            elif not suite_text:
                return self._blocked(output_dir, "managed_query_suite_missing", "未配置受管理 CodeQL query suite", revision=actual)
            else:
                query_source = Path(suite_text).resolve()
            if not query_source.is_file():
                return self._blocked(output_dir, "managed_query_suite_missing", "受管理 CodeQL query suite 不存在", revision=actual)
            if not cache_hit:
                database.parent.mkdir(parents=True, exist_ok=True)
                created = self._run([
                    codeql, "database", "create", str(database), "--language=python",
                    f"--source-root={source_root}", "--overwrite",
                ], timeout=max(300, task.duration_sec + 120))
                if created.returncode != 0:
                    return self._blocked(
                        output_dir,
                        "codeql_database_create_failed",
                        self._stderr(created) or "CodeQL database create 失败",
                        revision=actual,
                    )
                cache_ready.write_text(actual, encoding="utf-8")
            sarif_path = output_dir / "source-mechanism.sarif"
            analyzed = self._run([
                codeql, "database", "analyze", str(database), str(query_source),
                "--format=sarif-latest", f"--output={sarif_path}", "--rerun",
            ], timeout=max(300, task.duration_sec + 180))
            if analyzed.returncode != 0 or not sarif_path.is_file():
                return self._blocked(
                    output_dir,
                    "codeql_query_failed",
                    self._stderr(analyzed) or "CodeQL query 未产出 SARIF",
                    revision=actual,
                )

        try:
            sarif = json.loads(sarif_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return self._blocked(output_dir, "codeql_sarif_unparseable", str(exc), revision=actual)
        paths = self._mechanism_paths(sarif, anchors, query_metadata=query_metadata)
        source_hash = hashlib.sha256(f"{identity}\0{actual}".encode("utf-8")).hexdigest()
        anchored_paths = [path for path in paths if path.get("anchor_matches")]
        status = "valid" if anchored_paths else "partial" if paths else "empty_window"
        payload = {
            "schema_version": "1.0",
            "producer": "codeql",
            "revision": actual,
            "source_context_hash": f"sha256:{source_hash}",
            "query_pack_version": pack_version,
            "query": {
                key: value for key, value in query_metadata.items() if key != "query"
            },
            "cache": {"hit": cache_hit, "cache_key": cache_key},
            "database_ref": f"codeql-cache:{cache_key}",
            "line_anchors": anchors,
            "mechanism_paths": paths,
            "raw_artifact_refs": [
                "artifact:codeql_sarif",
                *(["artifact:codeql_query"] if query_artifact else []),
            ],
            "evidence_validity": {
                "execution_status": "completed",
                "artifact_status": "produced",
                "evidence_status": status,
                "reason": (
                    "codeql_anchored_code_flow_paths"
                    if anchored_paths
                    else "codeql_paths_without_line_anchor" if paths
                    else "no_codeql_code_flow_path"
                ),
            },
        }
        structured = output_dir / "source_mechanism.json"
        structured.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts = [
            self._artifact("codeql_sarif", sarif_path, "application/sarif+json"),
            {
                **self._artifact("source_mechanism_json", structured, "application/json"),
                "collector_family": "source_mechanism_query",
                "metadata": {"data": payload},
            },
        ]
        if query_artifact is not None:
            artifacts.insert(0, self._artifact("codeql_query", query_artifact, "text/plain"))
        return CollectorResult(ok=bool(paths), reason="CodeQL 源码机制证据结构化完成", artifacts=artifacts)

    @classmethod
    def _mechanism_paths(
        cls,
        sarif: dict[str, Any],
        anchors: list[dict[str, Any]],
        *,
        query_metadata: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for run in sarif.get("runs") or []:
            if not isinstance(run, dict):
                continue
            for result in run.get("results") or []:
                if not isinstance(result, dict):
                    continue
                rule_id = str(result.get("ruleId") or "unknown")[:160]
                relation = cls._relation(result)
                if relation == "unknown" and query_metadata:
                    relation = str(query_metadata.get("expected_relation") or "unknown")
                for flow in result.get("codeFlows") or []:
                    for thread in (flow.get("threadFlows") or []) if isinstance(flow, dict) else []:
                        nodes = []
                        for location in (thread.get("locations") or [])[: cls.MAX_NODES]:
                            node = cls._node(location)
                            if node:
                                nodes.append(node)
                        if not nodes:
                            continue
                        path_index = len(output)
                        edges = [
                            {
                                "from": nodes[index]["node_id"],
                                "to": nodes[index + 1]["node_id"],
                                "type": cls._edge_type(nodes[index + 1].get("message", "")),
                            }
                            for index in range(len(nodes) - 1)
                        ]
                        output.append({
                            "path_id": f"codeql_path_{path_index + 1}",
                            "rule_id": rule_id,
                            "summary": cls._message(result.get("message")),
                            "nodes": nodes,
                            "edges": edges,
                            "candidate_relation": relation,
                            "candidate_id": str(
                                (query_metadata or {}).get("candidate_id")
                                or (
                                    result.get("properties", {}).get("candidate_id")
                                    if isinstance(result.get("properties"), dict)
                                    else ""
                                )
                                or ""
                            ),
                            "anchor_matches": cls._anchor_matches(nodes, anchors),
                            "evidence_ref": f"source_mechanism.mechanism_paths[{path_index}]",
                        })
                        if len(output) >= cls.MAX_PATHS:
                            return output
        return output

    @classmethod
    def _node(cls, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        location = item.get("location") if isinstance(item.get("location"), dict) else item
        physical = location.get("physicalLocation") if isinstance(location.get("physicalLocation"), dict) else {}
        artifact = physical.get("artifactLocation") if isinstance(physical.get("artifactLocation"), dict) else {}
        region = physical.get("region") if isinstance(physical.get("region"), dict) else {}
        file_name = str(artifact.get("uri") or "")[:500]
        line = int(region.get("startLine") or 0)
        if not file_name or line <= 0:
            return None
        message = cls._message(item.get("message") or location.get("message"))
        node_id = hashlib.sha256(f"{file_name}:{line}:{message}".encode("utf-8")).hexdigest()[:16]
        logical = location.get("logicalLocations") or []
        symbol = ""
        if logical and isinstance(logical[0], dict):
            symbol = str(logical[0].get("fullyQualifiedName") or logical[0].get("name") or "")[:300]
        return {"node_id": node_id, "file": file_name, "line": line, "symbol": symbol, "message": message}

    @classmethod
    def _message(cls, value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("text") or value.get("markdown") or ""
        return str(value or "")[: cls.MAX_MESSAGE_LENGTH]

    @staticmethod
    def _relation(result: dict[str, Any]) -> str:
        properties = result.get("properties") if isinstance(result.get("properties"), dict) else {}
        value = str(properties.get("candidate_relation") or "").lower()
        if value in {"supports", "refutes", "unknown"}:
            return value
        tags = " ".join(str(item) for item in properties.get("tags") or []).lower()
        return "refutes" if "refute" in tags else "supports" if "support" in tags else "unknown"

    @staticmethod
    def _edge_type(message: str) -> str:
        text = message.lower()
        if "constant" in text or "code object" in text or "code generation" in text:
            return "code_generation"
        if "container" in text or "append" in text or "stored" in text:
            return "container_write"
        if "call" in text or "return" in text:
            return "call"
        return "data_flow"

    @staticmethod
    def _anchor_matches(nodes: list[dict[str, Any]], anchors: list[dict[str, Any]]) -> list[int]:
        matches = []
        for index, anchor in enumerate(anchors):
            anchor_file = str(anchor.get("file") or "").replace("\\", "/")
            anchor_line = int(anchor.get("line") or 0)
            if any(
                str(node.get("file") or "").replace("\\", "/").endswith(anchor_file)
                and int(node.get("line") or 0) == anchor_line
                for node in nodes
            ):
                matches.append(index)
        return matches

    @staticmethod
    def _anchors(value: Any) -> list[dict[str, Any]]:
        result = []
        for item in value if isinstance(value, list) else []:
            if not isinstance(item, dict):
                continue
            file_name = str(item.get("file") or "").strip()
            try:
                line = int(item.get("line") or 0)
            except (TypeError, ValueError):
                line = 0
            if file_name and line > 0:
                result.append({"file": file_name[:500], "line": line, "symbol": str(item.get("symbol") or "")[:300]})
        return result[:10]

    @staticmethod
    def _git(root: Path, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

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
    def _allowed(path: Path, env_name: str, default: str) -> bool:
        roots = [Path(item.strip()).resolve() for item in os.getenv(env_name, default).split(",") if item.strip()]
        return any(path == root or root in path.parents for root in roots)

    def _blocked(
        self,
        output_dir: Path,
        reason: str,
        detail: str,
        *,
        revision: str = "",
    ) -> CollectorResult:
        payload = {
            "schema_version": "1.0",
            "producer": "codeql",
            "revision": revision,
            "mechanism_paths": [],
            "raw_artifact_refs": [],
            "evidence_validity": {
                "execution_status": "failed",
                "artifact_status": "produced",
                "evidence_status": "blocked",
                "reason": reason,
                "detail": detail[:500],
            },
        }
        path = output_dir / "source_mechanism.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifact = {
            **self._artifact("source_mechanism_json", path, "application/json"),
            "collector_family": "source_mechanism_query",
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
