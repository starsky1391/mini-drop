"""Python source syntax and line-span verification.

This module verifies source locations against the checked-out revision. It
does not infer runtime causality; callers must keep that boundary explicit.
"""

from __future__ import annotations

import ast
import hashlib
import platform
from typing import Any


_IGNORED_LOCATION_NODES = (
    ast.Load,
    ast.Store,
    ast.Del,
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
    ast.expr_context,
    ast.comprehension,
    ast.ExceptHandler,
    ast.arguments,
    ast.arg,
    ast.keyword,
    ast.alias,
    ast.withitem,
)

_SCOPE_NODES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


class PythonSourceSyntaxVerifier:
    """Verify candidate file:line locations using Python's official AST."""

    @staticmethod
    def verify(
        *,
        relative_file: str,
        source: str,
        candidates: list[dict[str, Any]],
        revision: str,
    ) -> dict[str, Any]:
        parser_version = platform.python_version()
        source_hash = f"sha256:{hashlib.sha256(source.encode('utf-8', errors='replace')).hexdigest()}"
        try:
            tree = ast.parse(source, filename=relative_file, mode="exec")
        except SyntaxError as exc:
            return {
                "parser": "python.ast",
                "parser_version": parser_version,
                "revision": revision,
                "file": relative_file,
                "source_hash": source_hash,
                "parse_status": "failed",
                "source_syntax_status": "unparseable",
                "evidence_status": "unparseable",
                "reason": "python_syntax_error",
                "detail": str(exc)[:300],
                "verified_source_lines": [],
            }

        entries: list[dict[str, Any]] = []
        scopes: list[dict[str, Any]] = []
        PythonSourceSyntaxVerifier._index(tree, [], entries, scopes)
        verified = []
        seen: set[tuple[int, str, str]] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                line = int(candidate.get("line") or candidate.get("focus_line") or 0)
            except (TypeError, ValueError):
                line = 0
            if line <= 0:
                continue
            match = PythonSourceSyntaxVerifier._verify_line(
                line=line,
                symbol=str(candidate.get("symbol") or candidate.get("function") or ""),
                entries=entries,
                scopes=scopes,
                source=source,
            )
            if not match:
                continue
            origin = str(candidate.get("line_origin") or "runtime_focus")
            key = (line, str(match["enclosing_symbol"]), origin)
            if key in seen:
                continue
            seen.add(key)
            verified.append({
                **match,
                "file": relative_file,
                "line_origin": origin,
                "evidence_role": "verified_source_line",
                "line_localization_status": "verified",
                "source_syntax_valid": True,
                "source_revision": revision,
                "source_hash": source_hash,
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "parent_candidate_id": str(candidate.get("parent_candidate_id") or ""),
                "evidence_refs": [
                    str(ref)
                    for ref in candidate.get("evidence_refs", [])
                    if str(ref or "").strip()
                ][:8],
            })

        return {
            "parser": "python.ast",
            "parser_version": parser_version,
            "revision": revision,
            "file": relative_file,
            "source_hash": source_hash,
            "parse_status": "valid",
            "source_syntax_status": "valid" if verified else "partial",
            "evidence_status": "valid" if verified else "partial",
            "reason": "python_ast_source_lines_verified" if verified else "no_candidate_in_ast_scope",
            "verified_source_lines": verified[:64],
        }

    @classmethod
    def _index(
        cls,
        node: ast.AST,
        scope_path: list[str],
        entries: list[dict[str, Any]],
        scopes: list[dict[str, Any]],
    ) -> None:
        lineno = int(getattr(node, "lineno", 0) or 0)
        end_lineno = int(getattr(node, "end_lineno", 0) or lineno)
        if lineno > 0:
            entry = {
                "node": node,
                "node_type": type(node).__name__,
                "start_line": lineno,
                "end_line": max(lineno, end_lineno),
                "start_col": int(getattr(node, "col_offset", 0) or 0),
                "end_col": int(getattr(node, "end_col_offset", 0) or 0),
                "scope_path": list(scope_path),
            }
            entries.append(entry)
            if isinstance(node, _SCOPE_NODES):
                scopes.append(entry)

        child_scope = list(scope_path)
        if isinstance(node, _SCOPE_NODES):
            child_scope.append(str(node.name))
        for child in ast.iter_child_nodes(node):
            cls._index(child, child_scope, entries, scopes)

    @classmethod
    def _verify_line(
        cls,
        *,
        line: int,
        symbol: str,
        entries: list[dict[str, Any]],
        scopes: list[dict[str, Any]],
        source: str,
    ) -> dict[str, Any] | None:
        containing = [
            item
            for item in entries
            if item["start_line"] <= line <= item["end_line"]
        ]
        if not containing:
            return None

        enclosing = [
            item
            for item in scopes
            if item["start_line"] <= line <= item["end_line"]
        ]
        enclosing.sort(key=lambda item: (
            item["end_line"] - item["start_line"],
            -item["start_line"],
        ))
        scope = enclosing[0] if enclosing else None
        enclosing_symbol = ".".join(scope["scope_path"]) if scope else ""
        if scope and isinstance(scope["node"], _SCOPE_NODES):
            enclosing_symbol = ".".join([
                *scope["scope_path"],
                str(scope["node"].name),
            ])
        symbol_match = cls._symbol_match(symbol, enclosing_symbol)

        location_nodes = [
            item
            for item in containing
            if not isinstance(item["node"], _IGNORED_LOCATION_NODES)
        ]
        statement_nodes = [
            item for item in location_nodes
            if isinstance(item["node"], ast.stmt)
        ]
        exact_statements = [
            item
            for item in statement_nodes
            if item["start_line"] == line
        ]
        exact = exact_statements or [
            item for item in location_nodes
            if item["start_line"] == line
        ]
        selectable = exact_statements or statement_nodes or exact or location_nodes
        selected = min(
            selectable,
            key=lambda item: (
                item["end_line"] - item["start_line"],
                item["end_col"] - item["start_col"],
            ),
        )
        start_line = int(selected["start_line"])
        end_line = int(selected["end_line"])
        source_lines = source.splitlines()
        source_text = "\n".join(
            source_lines[index - 1]
            for index in range(start_line, min(end_line, len(source_lines)) + 1)
        )
        return {
            "verified_line": line,
            "line_match": "exact_statement" if exact_statements else "enclosing_symbol",
            "node_type": selected["node_type"],
            "enclosing_symbol": enclosing_symbol,
            "enclosing_kind": (
                "class"
                if scope and isinstance(scope["node"], ast.ClassDef)
                else "function"
                if scope
                else "module"
            ),
            "symbol_match": symbol_match,
            "source_span": {
                "start_line": start_line,
                "end_line": end_line,
                "start_col": int(selected["start_col"]),
                "end_col": int(selected["end_col"]),
            },
            "source_text": source_text[:1000],
        }

    @staticmethod
    def _symbol_match(candidate: str, enclosing: str) -> str:
        candidate = str(candidate or "").strip()
        enclosing = str(enclosing or "").strip()
        if not candidate:
            return "missing"
        if candidate == enclosing:
            return "exact"
        if candidate.rsplit(".", 1)[-1] == enclosing.rsplit(".", 1)[-1]:
            return "compatible"
        if candidate.rsplit(".", 1)[-1] == enclosing:
            return "compatible"
        return "mismatch"
