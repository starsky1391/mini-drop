"""Validation for bounded AI-generated CodeQL investigation queries."""

from __future__ import annotations

import hashlib
import re


MAX_CODEQL_QUERY_CHARS = 12_000
_ALLOWED_IMPORT_PREFIXES = ("python", "semmle.python")


def validate_ai_generated_codeql_query(
    value: object,
    *,
    allowed_candidate_ids: set[str] | None = None,
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("ai_generated_query 必须是对象")
    question = str(value.get("investigation_question") or "").strip()
    candidate_id = str(value.get("candidate_id") or "").strip()
    expected_relation = str(value.get("expected_relation") or "").strip().lower()
    query = str(value.get("query") or "").strip()
    if not question or len(question) > 500:
        raise ValueError("investigation_question 缺失或过长")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,100}", candidate_id):
        raise ValueError("candidate_id 缺失或非法")
    if allowed_candidate_ids is not None and candidate_id not in allowed_candidate_ids:
        raise ValueError("CodeQL query 绑定了越界候选")
    if expected_relation not in {"supports", "refutes"}:
        raise ValueError("expected_relation 必须是 supports 或 refutes")
    if not query or len(query) > MAX_CODEQL_QUERY_CHARS:
        raise ValueError("CodeQL query 缺失或超过长度限制")
    lowered = query.lower()
    if "@kind path-problem" not in lowered:
        raise ValueError("CodeQL query 必须声明 @kind path-problem")
    if not re.search(r"(?im)^\s*select\s+", query):
        raise ValueError("CodeQL query 缺少 select")
    imports = re.findall(r"(?im)^\s*import\s+([A-Za-z0-9_.]+)\s*$", query)
    if not imports or any(not item.startswith(_ALLOWED_IMPORT_PREFIXES) for item in imports):
        raise ValueError("CodeQL query 包含未允许的 import")
    if re.search(r"(?im)^\s*import\s+['\"]", query):
        raise ValueError("CodeQL query 不允许导入任意路径")
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return {
        "origin": "ai_guarded",
        "investigation_question": question,
        "candidate_id": candidate_id,
        "expected_relation": expected_relation,
        "query": query,
        "query_hash": f"sha256:{digest}",
    }
