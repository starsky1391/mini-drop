"""Bounded, revision-verified source context collector."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class SourceSnapshotCollector:
    OUTPUT_BASE = "/tmp/mini-drop"
    CONTEXT_LINES = 25
    MAX_CANDIDATES = 10
    MAX_ENCLOSING_LINES = 400

    def collect(self, task: CollectorTask) -> CollectorResult:
        output_dir = Path(self.OUTPUT_BASE) / task.id
        output_dir.mkdir(parents=True, exist_ok=True)
        source_root = self._map_host_path(Path(str(task.options.get("source_root") or "")).resolve())
        if not self._allowed(source_root):
            return self._result(output_dir, False, "source_root_not_allowed", "路径不在配置的源码根目录", {})
        if not source_root.is_dir():
            return self._result(output_dir, False, "source_root_missing", "源码根目录不存在", {})
        git = shutil.which("git")
        ctags = shutil.which("ctags")
        if not git or not ctags:
            return self._result(output_dir, False, "source_tools_missing", "Git 或 universal-ctags 命令不可用", {})

        git_command = [git, "-c", f"safe.directory={source_root}", "-C", str(source_root)]
        revision_result = subprocess.run(
            [*git_command, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
        if revision_result.returncode != 0:
            return self._result(output_dir, False, "git_revision_unavailable", revision_result.stderr.strip(), {})
        revision = revision_result.stdout.strip()
        expected = str(task.options.get("source_revision") or "").strip()
        if expected and not revision.startswith(expected) and not expected.startswith(revision):
            return self._result(
                output_dir,
                False,
                "source_revision_mismatch",
                f"源码 revision 不匹配: expected={expected}, actual={revision}",
                {"revision": revision, "expected_revision": expected},
            )

        tracked_result = subprocess.run(
            [*git_command, "ls-files", "-z"], capture_output=True, timeout=15
        )
        tracked_files = []
        if tracked_result.returncode == 0:
            raw_tracked = tracked_result.stdout
            if isinstance(raw_tracked, bytes):
                raw_tracked = raw_tracked.decode("utf-8", errors="replace")
            tracked_files = [item for item in str(raw_tracked).split("\0") if item]
        snippets = []
        enclosing_contexts = []
        reference_paths = []
        for candidate in (task.options.get("line_candidates") or [])[: self.MAX_CANDIDATES]:
            if not isinstance(candidate, dict):
                continue
            relative_text = self._tracked_file(str(candidate.get("file") or ""), tracked_files)
            if not relative_text:
                continue
            relative = Path(relative_text)
            source_file = (source_root / relative).resolve()
            if not self._inside(source_file, source_root) or not source_file.is_file():
                continue
            line = max(1, int(candidate.get("line") or 1))
            lines = source_file.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(1, line - self.CONTEXT_LINES)
            end = min(len(lines), line + self.CONTEXT_LINES)
            snippets.append({
                "file": relative.as_posix(),
                "focus_line": line,
                "symbol": str(candidate.get("symbol") or ""),
                "lines": [{"line": number, "text": lines[number - 1]} for number in range(start, end + 1)],
            })
            enclosing = self._python_enclosing_context(relative, lines, line)
            if enclosing and not any(
                item["file"] == enclosing["file"]
                and item["start_line"] == enclosing["start_line"]
                and item["end_line"] == enclosing["end_line"]
                for item in enclosing_contexts
            ):
                enclosing_contexts.append(enclosing)
                reference_paths.extend(enclosing.get("reference_paths") or [])

        symbols = ""
        files = [str(source_root / item["file"]) for item in snippets]
        if files:
            symbol_result = subprocess.run(
                [ctags, "-x", "--_xformat=%N\\t%F\\t%n\\t%K", *files],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if symbol_result.returncode == 0:
                symbols = symbol_result.stdout[:20000]
        hash_input = json.dumps(
            {
                "revision": revision,
                "snippets": snippets,
                "enclosing_contexts": enclosing_contexts,
                "reference_paths": reference_paths,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        source_hash = f"sha256:{hashlib.sha256(hash_input.encode('utf-8')).hexdigest()}"
        return self._result(
            output_dir,
            bool(snippets),
            "source_context_ready" if snippets else "no_readable_line_candidates",
            "源码上下文已按 revision 有界提取" if snippets else "没有可读取的行候选",
            {
                "revision": revision,
                "source_context_hash": source_hash,
                "snippets": snippets,
                "enclosing_contexts": enclosing_contexts[:4],
                "reference_paths": reference_paths[:12],
                "ctags_index": symbols,
            },
        )

    def _result(self, output_dir: Path, ok: bool, reason: str, detail: str, extra: dict[str, Any]) -> CollectorResult:
        payload = {
            "schema_version": "1.0",
            "producer": "git+universal-ctags",
            **extra,
            "evidence_validity": {
                "execution_status": "completed" if ok else "failed",
                "artifact_status": "produced",
                "evidence_status": "valid" if ok else "blocked",
                "reason": reason,
                "detail": detail,
            },
        }
        path = output_dir / "source_snapshot.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return CollectorResult(
            ok=ok,
            reason=detail,
            artifacts=[{
                "artifact_type": "source_snapshot_json",
                "filename": path.name,
                "local_path": str(path),
                "content_type": "application/json",
                "size_bytes": path.stat().st_size,
                "collector_family": "source_snapshot",
                "metadata": {"data": payload},
            }],
        )

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    @classmethod
    def _python_enclosing_context(cls, relative: Path, lines: list[str], focus_line: int) -> dict[str, Any] | None:
        if relative.suffix.lower() != ".py":
            return None
        try:
            tree = ast.parse("\n".join(lines))
        except SyntaxError:
            return None
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and int(getattr(node, "lineno", 0) or 0) <= focus_line <= int(getattr(node, "end_lineno", 0) or 0)
        ]
        if not matches:
            return None
        classes = [node for node in matches if isinstance(node, ast.ClassDef)]
        node = min(classes or matches, key=lambda item: int(item.end_lineno) - int(item.lineno))
        start = int(node.lineno)
        end = int(node.end_lineno)
        if end - start + 1 > cls.MAX_ENCLOSING_LINES:
            return None
        return {
            "file": relative.as_posix(),
            "symbol": str(node.name),
            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
            "start_line": start,
            "end_line": end,
            "lines": [{"line": number, "text": lines[number - 1]} for number in range(start, end + 1)],
            "reference_paths": cls._python_reference_paths(node, lines),
        }

    @classmethod
    def _python_reference_paths(cls, scope: ast.AST, lines: list[str]) -> list[dict[str, Any]]:
        storage_methods: dict[str, dict[str, Any]] = {}
        assignments: dict[str, ast.AST] = {}
        aggregate_containers: dict[str, str] = {}
        code_sinks: dict[str, dict[str, Any]] = {}
        collection_origins: dict[str, list[dict[str, Any]]] = {}

        for node in ast.walk(scope):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and value is not None:
                        assignments[target.id] = value
                        container = cls._tupled_self_container(value)
                        if container:
                            aggregate_containers[target.id] = container
            if isinstance(node, ast.Call) and len(node.args) == 1:
                collection = cls._named_collection_call(node.func, "append")
                value = node.args[0]
                if collection and isinstance(value, (ast.Tuple, ast.List)):
                    for element in value.elts:
                        kind = cls._source_kind(element, assignments)
                        if kind != "bound_method_or_attribute":
                            continue
                        collection_origins.setdefault(collection, []).append({
                            "expression": cls._source_text(element, lines),
                            "source_kind": kind,
                            "line": int(getattr(element, "lineno", node.lineno)),
                        })
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parameters = {
                arg.arg
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            }
            for child in ast.walk(node):
                if not isinstance(child, ast.Call) or len(child.args) != 1:
                    continue
                container = cls._self_attribute_call(child.func, "append")
                argument = child.args[0]
                if container and isinstance(argument, ast.Name) and argument.id in parameters:
                    storage_methods[node.name] = {
                        "container": f"self.{container}",
                        "line": child.lineno,
                    }

        for node in ast.walk(scope):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if not names or not isinstance(node.value, ast.Call) or cls._call_name(node.value.func) != "CodeType":
                continue
            for nested in ast.walk(node.value):
                if not isinstance(nested, ast.Call) or cls._call_name(nested.func) != "tuple" or len(nested.args) != 1:
                    continue
                container = cls._self_attribute(nested.args[0])
                if container:
                    for name in names:
                        code_sinks[name] = {
                            "container": f"self.{container}",
                            "sink": cls._source_text(node.value, lines),
                            "line": node.lineno,
                        }
            for argument in node.value.args:
                if not isinstance(argument, ast.Starred) or not isinstance(argument.value, ast.Name):
                    continue
                container = aggregate_containers.get(argument.value.id)
                if container:
                    for name in names:
                        code_sinks[name] = {
                            "container": container,
                            "sink": cls._source_text(node.value, lines),
                            "line": node.lineno,
                        }

        function_sinks: dict[str, dict[str, Any]] = {}
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call) or cls._call_name(node.func) != "FunctionType":
                continue
            for argument in node.args:
                if isinstance(argument, ast.Name) and argument.id in code_sinks:
                    function_sinks[argument.id] = {
                        "retained_by": cls._source_text(node, lines),
                        "line": node.lineno,
                    }

        paths = []
        seen = set()
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            method = cls._self_method_name(node.func)
            storage = storage_methods.get(method)
            if not storage or len(node.args) != 1:
                continue
            matching_code = next(
                ((name, sink) for name, sink in code_sinks.items() if sink["container"] == storage["container"]),
                None,
            )
            if not matching_code or matching_code[0] not in function_sinks:
                continue
            source = node.args[0]
            source_expression = cls._source_text(source, lines)
            key = (source_expression, node.lineno, storage["container"])
            if key in seen:
                continue
            seen.add(key)
            code_name, code_sink = matching_code
            function_sink = function_sinks[code_name]
            line_numbers = [node.lineno, storage["line"], code_sink["line"], function_sink["line"]]
            upstream_candidates = cls._loop_value_origins(scope, source, collection_origins, lines)
            paths.append({
                "source_expression": source_expression,
                "source_kind": "loop_value" if upstream_candidates else cls._source_kind(source, assignments),
                "upstream_candidates": upstream_candidates,
                "stored_via": f"self.{method}(...)",
                "container": storage["container"],
                "sink": code_sink["sink"],
                "runtime_slot": "CodeType.co_consts",
                "retained_by": function_sink["retained_by"],
                "retention_chain": [
                    source_expression,
                    f"self.{method}(...)",
                    storage["container"],
                    "CodeType.co_consts",
                    "FunctionType",
                ],
                "source_lines": [
                    {"line": number, "text": lines[number - 1]}
                    for number in dict.fromkeys(line_numbers)
                    if 1 <= number <= len(lines)
                ],
            })
        return paths[:12]

    @staticmethod
    def _named_collection_call(node: ast.AST, method: str) -> str:
        if not isinstance(node, ast.Attribute) or node.attr != method:
            return ""
        return node.value.id if isinstance(node.value, ast.Name) else ""

    @classmethod
    def _loop_value_origins(
        cls,
        scope: ast.AST,
        source: ast.AST,
        origins: dict[str, list[dict[str, Any]]],
        lines: list[str],
    ) -> list[dict[str, Any]]:
        if not isinstance(source, ast.Name):
            return []
        matches = []
        seen = set()
        for node in ast.walk(scope):
            if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Name):
                continue
            names = [item.id for item in ast.walk(node.target) if isinstance(item, ast.Name)]
            if source.id not in names:
                continue
            for item in origins.get(node.iter.id, []):
                candidate = {
                    **item,
                    "flows_as": source.id,
                    "flow_evidence": cls._source_text(node.target, lines),
                }
                key = (candidate["expression"], candidate["line"], candidate["flows_as"])
                if key not in seen:
                    seen.add(key)
                    matches.append(candidate)
        return matches[:8]

    @staticmethod
    def _self_attribute(node: ast.AST) -> str:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            return node.attr
        return ""

    @classmethod
    def _self_attribute_call(cls, node: ast.AST, method: str) -> str:
        if not isinstance(node, ast.Attribute) or node.attr != method:
            return ""
        return cls._self_attribute(node.value)

    @staticmethod
    def _self_method_name(node: ast.AST) -> str:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            return node.attr
        return ""

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""

    @staticmethod
    def _source_text(node: ast.AST, lines: list[str]) -> str:
        return ast.get_source_segment("\n".join(lines), node) or ""

    @classmethod
    def _tupled_self_container(cls, node: ast.AST) -> str:
        for child in ast.walk(node):
            if not isinstance(child, ast.Call) or cls._call_name(child.func) != "tuple" or len(child.args) != 1:
                continue
            container = cls._self_attribute(child.args[0])
            if container:
                return f"self.{container}"
        return ""

    @classmethod
    def _source_kind(cls, node: ast.AST, assignments: dict[str, ast.AST]) -> str:
        value = assignments.get(node.id) if isinstance(node, ast.Name) else node
        if isinstance(value, ast.Call) and cls._call_name(value.func) == "partial":
            return "partial_callable"
        if isinstance(value, ast.Attribute):
            return "bound_method_or_attribute"
        if isinstance(value, (ast.Name, ast.Lambda)):
            return "callable_or_value"
        return "expression"

    @staticmethod
    def _tracked_file(candidate: str, tracked_files: list[str]) -> str:
        normalized = candidate.replace("\\", "/").lstrip("/")
        matches = []
        for item in tracked_files:
            tracked = item.replace("\\", "/").lstrip("/")
            if normalized == tracked or normalized.endswith(f"/{tracked}"):
                matches.append(item)
        return matches[0] if len(matches) == 1 else ""

    @staticmethod
    def _map_host_path(path: Path) -> Path:
        if str(path).startswith("/home/") and Path("/host/home").is_dir():
            return (Path("/host/home") / path.relative_to("/home")).resolve()
        return path

    @classmethod
    def _allowed(cls, path: Path) -> bool:
        roots = [Path(item.strip()).resolve() for item in os.getenv("MINI_DROP_SOURCE_ROOTS", "/host/home,/usr/src").split(",") if item.strip()]
        return any(cls._inside(path, root) for root in roots)
