"""Bounded, revision-verified source context collector."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import tokenize
from pathlib import Path
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.python_source_syntax import PythonSourceSyntaxVerifier


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
            return self._result(
                output_dir,
                False,
                "source_root_not_allowed",
                "路径不在配置的源码根目录",
                {},
                evidence_status="blocked",
                source_verification_status="blocked",
            )
        if not source_root.is_dir():
            return self._result(
                output_dir,
                False,
                "source_root_missing",
                "源码根目录不存在",
                {},
                evidence_status="blocked",
                source_verification_status="file_missing",
            )
        git = shutil.which("git")
        ctags = shutil.which("ctags")
        if not git:
            return self._result(
                output_dir,
                False,
                "source_tools_missing",
                "Git 命令不可用",
                {},
                evidence_status="blocked",
                source_verification_status="blocked",
            )

        git_command = [git, "-c", f"safe.directory={source_root}", "-C", str(source_root)]
        revision_result = subprocess.run(
            [*git_command, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
        if revision_result.returncode != 0:
            return self._result(
                output_dir,
                False,
                "git_revision_unavailable",
                revision_result.stderr.strip(),
                {},
                evidence_status="blocked",
                source_verification_status="blocked",
            )
        revision = revision_result.stdout.strip()
        expected = str(task.options.get("source_revision") or "").strip()
        if expected and not revision.startswith(expected) and not expected.startswith(revision):
            return self._result(
                output_dir,
                False,
                "source_revision_mismatch",
                f"源码 revision 不匹配: expected={expected}, actual={revision}",
                {"revision": revision, "expected_revision": expected},
                evidence_status="blocked",
                source_verification_status="revision_mismatch",
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
        source_texts: dict[str, str] = {}
        syntax_candidates: dict[str, list[dict[str, Any]]] = {}
        candidates = [
            candidate
            for candidate in list(task.options.get("line_candidates") or [])
            if isinstance(candidate, dict)
        ]
        candidate_mappings = [
            self._candidate_mapping(candidate, tracked_files)
            for candidate in candidates
        ]
        candidates.sort(key=lambda candidate: self._candidate_sort_key(candidate, tracked_files))
        for candidate in candidates[: self.MAX_CANDIDATES]:
            relative_text = self._tracked_file(str(candidate.get("file") or ""), tracked_files)
            if not relative_text:
                continue
            relative = Path(relative_text)
            source_file = (source_root / relative).resolve()
            if not self._inside(source_file, source_root) or not source_file.is_file():
                continue
            line = max(1, int(candidate.get("line") or 1))
            source_text = self._read_source(source_file)
            source_texts[relative.as_posix()] = source_text
            lines = source_text.splitlines()
            start = max(1, line - self.CONTEXT_LINES)
            end = min(len(lines), line + self.CONTEXT_LINES)
            snippets.append({
                "file": relative.as_posix(),
                "focus_line": line,
                "symbol": str(candidate.get("symbol") or ""),
                "lines": [{"line": number, "text": lines[number - 1]} for number in range(start, end + 1)],
            })
            syntax_candidates.setdefault(relative.as_posix(), []).append({
                "line": line,
                "symbol": str(candidate.get("symbol") or ""),
                "line_origin": str(candidate.get("line_origin") or "runtime_focus"),
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "parent_candidate_id": str(candidate.get("parent_candidate_id") or ""),
                "evidence_refs": [candidate.get("evidence_ref")] if candidate.get("evidence_ref") else [],
            })
            enclosing = self._python_enclosing_context(relative, lines, line)
            if enclosing and not any(
                item["file"] == enclosing["file"]
                and item["start_line"] == enclosing["start_line"]
                and item["end_line"] == enclosing["end_line"]
                for item in enclosing_contexts
            ):
                enclosing_contexts.append(enclosing)
                reference_paths.extend(
                    {
                        "file": enclosing["file"],
                        "symbol": enclosing.get("symbol") or "",
                        **path,
                    }
                    for path in (enclosing.get("reference_paths") or [])
                    if isinstance(path, dict)
                )

        for path in reference_paths:
            file_name = self._tracked_file(str(path.get("file") or ""), tracked_files)
            if not file_name:
                continue
            symbol = str(path.get("symbol") or "")
            for upstream in path.get("upstream_candidates") or []:
                if isinstance(upstream, dict) and int(upstream.get("line") or 0) > 0:
                    syntax_candidates.setdefault(file_name, []).append({
                        "line": upstream["line"],
                        "symbol": symbol,
                        "line_origin": "reference_origin",
                        "candidate_id": str(path.get("candidate_id") or ""),
                        "parent_candidate_id": str(
                            path.get("parent_candidate_id")
                            or path.get("runtime_parent_candidate_id")
                            or ""
                        ),
                    })
            for source_line in path.get("source_lines") or []:
                if isinstance(source_line, dict) and int(source_line.get("line") or 0) > 0:
                    syntax_candidates.setdefault(file_name, []).append({
                        "line": source_line["line"],
                        "symbol": symbol,
                        "line_origin": "reference_step",
                        "candidate_id": str(path.get("candidate_id") or ""),
                        "parent_candidate_id": str(
                            source_line.get("parent_candidate_id")
                            or path.get("parent_candidate_id")
                            or path.get("runtime_parent_candidate_id")
                            or ""
                        ),
                    })

        # Reference paths can point at a different tracked file than the
        # original runtime frame. Load those files before AST verification so
        # a valid mechanism line is not reduced to an unverified hint.
        for file_name in list(syntax_candidates):
            tracked_name = self._tracked_file(file_name, tracked_files)
            if not tracked_name:
                continue
            if tracked_name != file_name:
                syntax_candidates[tracked_name].extend(syntax_candidates.pop(file_name))
                file_name = tracked_name
            if file_name in source_texts:
                continue
            source_file = (source_root / Path(file_name)).resolve()
            if self._inside(source_file, source_root) and source_file.is_file():
                source_texts[file_name] = self._read_source(source_file)

        source_syntax = []
        verified_source_lines = []
        for file_name, file_candidates in syntax_candidates.items():
            if Path(file_name).suffix.lower() != ".py":
                continue
            source_text = source_texts.get(file_name)
            if source_text is None:
                continue
            syntax_result = PythonSourceSyntaxVerifier.verify(
                relative_file=file_name,
                source=source_text,
                candidates=file_candidates,
                revision=revision,
            )
            source_syntax.append(syntax_result)
            verified_source_lines.extend(syntax_result.get("verified_source_lines") or [])

        source_reference_hints = [
            {
                **path,
                "evidence_role": "static_hint",
                "verification_status": "unverified",
            }
            for path in reference_paths[:12]
            if isinstance(path, dict)
        ]

        symbols = ""
        files = [str(source_root / item["file"]) for item in snippets]
        if files and ctags:
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
                "source_syntax": source_syntax,
                "verified_source_lines": verified_source_lines,
                "source_tools": {
                    "git": True,
                    "python_ast": True,
                    "ctags": bool(ctags),
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        source_hash = f"sha256:{hashlib.sha256(hash_input.encode('utf-8')).hexdigest()}"
        syntax_statuses = {
            str(item.get("source_syntax_status") or "")
            for item in source_syntax
            if isinstance(item, dict)
        }
        has_unparseable = "unparseable" in syntax_statuses
        input_candidate_count = len(candidates)
        mapped_candidate_count = sum(1 for item in candidate_mappings if item.get("tracked_file"))
        unmapped_line_candidates = [
            item
            for item in candidate_mappings
            if not item.get("tracked_file")
        ][:32]
        rejected_line_candidate_count = max(0, input_candidate_count - mapped_candidate_count)
        if verified_source_lines:
            snapshot_status = "partial" if has_unparseable else "valid"
        elif has_unparseable:
            snapshot_status = "unparseable"
        elif snippets:
            snapshot_status = "partial"
        else:
            snapshot_status = "blocked"
        missing_reason = (
            "runtime_line_candidate_missing"
            if input_candidate_count == 0
            else "runtime_line_candidate_not_repo_mappable"
            if mapped_candidate_count == 0
            else "no_readable_line_candidates"
        )
        snapshot_reason = (
            "source_context_and_ast_verified"
            if snapshot_status == "valid"
            else "source_context_partial_ast_failure"
            if snapshot_status == "partial" and has_unparseable
            else "source_context_without_verified_ast_line"
            if snapshot_status == "partial"
            else "python_ast_unparseable"
            if snapshot_status == "unparseable"
            else missing_reason
        )
        return self._result(
            output_dir,
            bool(snippets),
            "source_context_ready" if snippets else missing_reason,
            "源码上下文已按 revision 有界提取" if snippets else (
                "没有运行时 file:line 候选"
                if input_candidate_count == 0
                else "运行时 file:line 候选无法映射到当前 checkout"
            ),
            {
                "revision": revision,
                "source_context_hash": source_hash,
                "input_line_candidate_count": input_candidate_count,
                "mapped_line_candidate_count": mapped_candidate_count,
                "rejected_line_candidate_count": rejected_line_candidate_count,
                "candidate_mappings": candidate_mappings[:64],
                "unmapped_line_candidates": unmapped_line_candidates,
                "snippets": snippets,
                "enclosing_contexts": enclosing_contexts[:4],
                "reference_paths": reference_paths[:12],
                "source_reference_hints": source_reference_hints,
                "source_syntax": source_syntax[:12],
                "verified_source_lines": verified_source_lines[:64],
                "source_verification_status": (
                    "verified"
                    if snapshot_status == "valid"
                    else snapshot_status
                ),
                "source_verification_reason": snapshot_reason,
                "ctags_index": symbols,
                "source_tools": {
                    "git": True,
                    "python_ast": True,
                    "ctags": bool(ctags),
                },
            },
            evidence_status=snapshot_status,
            evidence_reason=snapshot_reason,
        )

    def _result(
        self,
        output_dir: Path,
        ok: bool,
        reason: str,
        detail: str,
        extra: dict[str, Any],
        *,
        evidence_status: str | None = None,
        evidence_reason: str | None = None,
        source_verification_status: str | None = None,
    ) -> CollectorResult:
        resolved_evidence_status = evidence_status or ("valid" if ok else "blocked")
        payload = {
            "schema_version": "1.0",
            "producer": "git+python.ast",
            **extra,
            "source_verification_status": (
                source_verification_status
                or str(extra.get("source_verification_status") or "")
                or (
                    "verified"
                    if resolved_evidence_status == "valid"
                    else resolved_evidence_status
                )
            ),
            "evidence_validity": {
                "execution_status": "completed" if ok else "failed",
                "artifact_status": "produced",
                "evidence_status": resolved_evidence_status,
                "reason": evidence_reason or reason,
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
        node = min(matches, key=lambda item: int(item.end_lineno) - int(item.lineno))
        outer_class = next(
            (
                item for item in matches
                if isinstance(item, ast.ClassDef)
                and int(item.lineno) <= int(node.lineno)
                and int(item.end_lineno) >= int(node.end_lineno)
            ),
            None,
        )
        # Report the innermost callable as the source context. Static
        # reference analysis still uses the enclosing class when helper
        # methods (for example an append/store method) are required.
        context_node = node
        analysis_scope = outer_class or node
        start = int(context_node.lineno)
        end = int(context_node.end_lineno)
        if end - start + 1 > cls.MAX_ENCLOSING_LINES:
            return None
        return {
            "file": relative.as_posix(),
            "symbol": cls._qualified_symbol(matches, node) or str(context_node.name),
            "kind": "class" if isinstance(context_node, ast.ClassDef) else "function",
            "analysis_scope_symbol": (
                cls._qualified_symbol(matches, outer_class)
                if outer_class is not None
                else cls._qualified_symbol(matches, node)
            ),
            "innermost_symbol": str(node.name),
            "innermost_kind": "class" if isinstance(node, ast.ClassDef) else "function",
            "qualified_symbol": cls._qualified_symbol(matches, node),
            "start_line": start,
            "end_line": end,
            "lines": [{"line": number, "text": lines[number - 1]} for number in range(start, end + 1)],
            "reference_paths": cls._python_reference_paths(analysis_scope, lines),
        }

    @staticmethod
    def _qualified_symbol(matches: list[ast.AST], node: ast.AST) -> str:
        scopes = [
            item for item in matches
            if int(getattr(item, "lineno", 0) or 0) <= int(getattr(node, "lineno", 0) or 0)
            and int(getattr(item, "end_lineno", 0) or 0) >= int(getattr(node, "end_lineno", 0) or 0)
        ]
        scopes.sort(key=lambda item: int(getattr(item, "lineno", 0) or 0))
        return ".".join(str(getattr(item, "name", "")) for item in scopes if getattr(item, "name", ""))

    @staticmethod
    def _read_source(source_file: Path) -> str:
        try:
            with tokenize.open(str(source_file)) as handle:
                return handle.read()
        except (OSError, SyntaxError, UnicodeError):
            return source_file.read_text(encoding="utf-8", errors="replace")

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

    @classmethod
    def _tracked_file(cls, candidate: str, tracked_files: list[str]) -> str:
        normalized = candidate.replace("\\", "/").lstrip("/")
        variants = cls._candidate_path_variants(normalized)
        matches: list[str] = []
        for item in tracked_files:
            tracked = item.replace("\\", "/").lstrip("/")
            if any(
                variant == tracked
                or variant.endswith(f"/{tracked}")
                or tracked.endswith(f"/{variant}")
                for variant in variants
                if variant
            ):
                matches.append(item)
        unique = list(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else ""

    @staticmethod
    def _candidate_path_variants(candidate: str) -> list[str]:
        normalized = str(candidate or "").replace("\\", "/").lstrip("/")
        if not normalized:
            return []
        variants = [normalized]
        parts = [part for part in normalized.split("/") if part]
        for marker in ("site-packages", "dist-packages"):
            if marker not in parts:
                continue
            marker_index = parts.index(marker)
            package_tail = "/".join(parts[marker_index + 1 :])
            if package_tail:
                variants.extend([package_tail, f"src/{package_tail}"])
        return list(dict.fromkeys(variants))

    @classmethod
    def _candidate_source_kind(cls, candidate: str, tracked_files: list[str]) -> str:
        normalized = str(candidate or "").replace("\\", "/").lstrip("/")
        text = normalized.lower()
        if not normalized or text.startswith("<") or text in {"[unknown]", "unknown"}:
            return "stdlib_or_frozen" if "frozen" in text else "native_or_unknown"
        if not normalized.endswith(".py"):
            return "native_or_unknown"
        if "/case/" in f"/{text}" and not any(
            token in f"/{text}/"
            for token in ("/case/src/", "/case/repo/", "/case/project/")
        ):
            return "case_driver"
        tracked = cls._tracked_file(normalized, tracked_files)
        if tracked:
            if "/site-packages/" in text or "/dist-packages/" in text:
                return "installed_package_source"
            return "repo_source"
        if text.startswith(("usr/local/lib/python", "usr/lib/python")) or "/lib/python" in text:
            return "stdlib_or_frozen"
        return "native_or_unknown"

    @classmethod
    def _candidate_mapping(cls, candidate: dict[str, Any], tracked_files: list[str]) -> dict[str, Any]:
        file_name = str(candidate.get("file") or "").replace("\\", "/")
        tracked = cls._tracked_file(file_name, tracked_files)
        try:
            line = int(candidate.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        return {
            "file": file_name,
            "line": line,
            "symbol": str(candidate.get("symbol") or candidate.get("function") or ""),
            "source_kind": cls._candidate_source_kind(file_name, tracked_files),
            "tracked_file": tracked,
            "mapped": bool(tracked),
        }

    @classmethod
    def _candidate_sort_key(cls, candidate: Any, tracked_files: list[str]) -> tuple[int, int, int]:
        if not isinstance(candidate, dict):
            return (1, 0, 1)
        file_name = str(candidate.get("file") or "").replace("\\", "/")
        symbol = str(candidate.get("symbol") or candidate.get("function") or "")
        source_kind = cls._candidate_source_kind(file_name, tracked_files)
        source_rank = {
            "repo_source": 0,
            "installed_package_source": 1,
            "case_driver": 4,
            "stdlib_or_frozen": 5,
            "native_or_unknown": 6,
        }.get(source_kind, 6)
        generic_runtime_frame = int(symbol.lower() in {
            "start", "worker", "main", "caller", "invoke", "new_func",
            "asynloop", "create_loop", "poll", "fire_timers",
        })
        return (source_rank, -cls._source_symbol_priority(symbol, file_name), generic_runtime_frame)

    @staticmethod
    def _source_symbol_priority(symbol: Any, file_name: Any = "") -> int:
        text = f"{str(symbol or '')} {str(file_name or '')}".lower()
        if "celery/app/trace.py" in text and any(
            token in text
            for token in ("handle_failure", "_log_error", "on_error", "trace_task", "fast_trace_task")
        ):
            return 3
        if "get_pickleable_exception" in text or "celery/utils/serialization.py" in text:
            return 1
        if any(
            token in text
            for token in (
                "error", "failure", "exception", "traceback", "retention",
                "compile", "handle_failure", "on_error", "trace_task",
            )
        ):
            return 2
        return 0

    @staticmethod
    def _map_host_path(path: Path) -> Path:
        if str(path).startswith("/home/") and Path("/host/home").is_dir():
            return (Path("/host/home") / path.relative_to("/home")).resolve()
        return path

    @classmethod
    def _allowed(cls, path: Path) -> bool:
        roots = [Path(item.strip()).resolve() for item in os.getenv("MINI_DROP_SOURCE_ROOTS", "/host/home,/usr/src").split(",") if item.strip()]
        return any(cls._inside(path, root) for root in roots)
