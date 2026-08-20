"""Validation and rendering for bounded AI-directed CodeQL investigations."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


MAX_CODEQL_QUERY_CHARS = 12_000
CODEQL_TEMPLATE_VERSION = "python-global-taint-v1"


def validate_ai_generated_codeql_query(
    value: object,
    *,
    allowed_candidate_ids: set[str] | None = None,
    allowed_anchors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("ai_generated_query 必须是对象")
    question = str(value.get("investigation_question") or "").strip()
    candidate_id = str(value.get("candidate_id") or "").strip()
    expected_relation = str(value.get("expected_relation") or "").strip().lower()
    if not question or len(question) > 500:
        raise ValueError("investigation_question 缺失或过长")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,100}", candidate_id):
        raise ValueError("candidate_id 缺失或非法")
    if allowed_candidate_ids is not None and candidate_id not in allowed_candidate_ids:
        raise ValueError("CodeQL query 绑定了越界候选")
    if expected_relation not in {"supports", "refutes"}:
        raise ValueError("expected_relation 必须是 supports 或 refutes")
    normalized_allowed = [_normalize_anchor(item) for item in allowed_anchors or []]
    normalized_allowed = [item for item in normalized_allowed if item]
    if allowed_anchors is not None and not normalized_allowed:
        raise ValueError("CodeQL query 没有可选择的已验证源码锚点")
    source_anchor = _normalize_anchor(value.get("source_anchor"))
    sink_anchor = _normalize_anchor(value.get("sink_anchor"))
    if source_anchor is None or sink_anchor is None:
        raise ValueError("CodeQL query 必须选择 source_anchor 和 sink_anchor")
    if _anchor_key(source_anchor) == _anchor_key(sink_anchor):
        raise ValueError("source_anchor 和 sink_anchor 不能相同")
    if normalized_allowed:
        if not _anchor_allowed(source_anchor, normalized_allowed) or not _anchor_allowed(sink_anchor, normalized_allowed):
            raise ValueError("CodeQL query 选择了未验证的源码锚点")

    raw_query = str(value.get("query") or "").strip()
    if len(raw_query) > MAX_CODEQL_QUERY_CHARS:
        raise ValueError("AI 原始 CodeQL query 超过长度限制")
    intent = {
        "investigation_question": question,
        "candidate_id": candidate_id,
        "expected_relation": expected_relation,
        "source_anchor": source_anchor,
        "sink_anchor": sink_anchor,
        "template_version": CODEQL_TEMPLATE_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(intent, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    result: dict[str, Any] = {
        "origin": "ai_guarded_anchor_spec",
        **intent,
        "query_spec_hash": f"sha256:{digest}",
    }
    if raw_query:
        result["raw_query_hash"] = f"sha256:{hashlib.sha256(raw_query.encode('utf-8')).hexdigest()}"
    return result


def render_version_locked_codeql_query(value: dict[str, Any]) -> str:
    """Render executable QL from guarded intent, never from model-written QL."""
    source = _normalize_anchor(value.get("source_anchor"))
    sink = _normalize_anchor(value.get("sink_anchor"))
    if source is None or sink is None:
        raise ValueError("缺少可执行的 CodeQL source/sink 锚点")
    source_file = _ql_string(source["file"])
    sink_file = _ql_string(sink["file"])
    candidate = re.sub(r"[^A-Za-z0-9_\-]", "_", str(value.get("candidate_id") or "candidate"))[:80]
    return f'''/**
 * @name Mini-Drop guarded mechanism path
 * @description Version-locked global flow between two evidence-verified source anchors.
 * @kind path-problem
 * @id mini-drop/guarded-mechanism-{candidate}
 * @problem.severity recommendation
 */
import python
import semmle.python.dataflow.new.DataFlow
import semmle.python.dataflow.new.TaintTracking

private predicate anchored(DataFlow::Node node, string relativePath, int line) {{
  exists(Expr expression |
    node.asExpr() = expression and
    expression.getLocation().getFile().getRelativePath() = relativePath and
    expression.getLocation().getStartLine() <= line and
    expression.getLocation().getEndLine() >= line
  )
}}

private module MiniDropConfig implements DataFlow::ConfigSig {{
  predicate isSource(DataFlow::Node source) {{
    anchored(source, "{source_file}", {source["line"]})
  }}

  predicate isSink(DataFlow::Node sink) {{
    anchored(sink, "{sink_file}", {sink["line"]})
  }}
}}

private module MiniDropFlow = TaintTracking::Global<MiniDropConfig>;
import MiniDropFlow::PathGraph

from MiniDropFlow::PathNode source, MiniDropFlow::PathNode sink
where MiniDropFlow::flowPath(source, sink)
select sink.getNode(), source, sink,
  "Evidence-verified value flow from $@ to $@.", source.getNode(), "source", sink.getNode(), "sink"
'''


def _normalize_anchor(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    file_name = str(value.get("file") or "").strip().replace("\\", "/")
    file_name = re.sub(r"^file:(?://)?", "", file_name)
    try:
        line = int(value.get("line") or 0)
    except (TypeError, ValueError):
        return None
    if not file_name or line <= 0 or line > 10_000_000 or "\x00" in file_name:
        return None
    if file_name.startswith("/") or re.match(r"^[A-Za-z]:/", file_name):
        file_name = file_name.lstrip("/")
    if any(part == ".." for part in file_name.split("/")):
        return None
    return {
        "file": file_name[:500],
        "line": line,
        "symbol": str(value.get("symbol") or "")[:300],
    }


def _anchor_key(value: dict[str, Any]) -> tuple[str, int]:
    return str(value["file"]).replace("\\", "/").lstrip("/"), int(value["line"])


def _anchor_allowed(value: dict[str, Any], allowed: list[dict[str, Any]]) -> bool:
    file_name, line = _anchor_key(value)
    for item in allowed:
        allowed_file, allowed_line = _anchor_key(item)
        if line == allowed_line and (
            file_name == allowed_file
            or file_name.endswith(f"/{allowed_file}")
            or allowed_file.endswith(f"/{file_name}")
        ):
            return True
    return False


def _ql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
