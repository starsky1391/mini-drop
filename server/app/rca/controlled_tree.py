"""Hard guards shared by Analyzer, LLM review, and session AI trees."""

from __future__ import annotations

import re
from typing import Any, Iterable

from server.app.rca.models import AITreeCandidateNode, AITreeLayer, ControlledAITree


_PRIMITIVE_PATTERNS = (
    ("wait_primitive", re.compile(
        r"(?:^|[._:/])(?:clock_)?nanosleep(?:64)?(?:$|[+._:/])|"
        r"(?:^|[._:/])(?:u?sleep)(?:$|[+._:/])|"
        r"pthread_(?:cond_wait|mutex_lock|rwlock_[a-z_]+)(?:$|[+._:/])|"
        r"(?:^|[._:/])futex(?:$|[+._:/])",
        re.IGNORECASE,
    )),
    ("scheduler_primitive", re.compile(
        r"(?:^|[._:/])(?:schedule|finish_task_switch|context_switch)(?:$|[+._:/])",
        re.IGNORECASE,
    )),
    ("syscall_primitive", re.compile(
        r"(?:^|[._:/])(?:epoll_wait|epoll_pwait|poll|ppoll|select|pselect|syscall)(?:$|[+._:/])|"
        r"(?:^|[._:/])__x64_sys_[a-z0-9_]+",
        re.IGNORECASE,
    )),
    ("runtime_primitive", re.compile(
        r"(?:^|[._:/])runtime[._](?:futex|park|notesleep|notetsleepg|usleep)(?:$|[+._:/])",
        re.IGNORECASE,
    )),
)

_OBSERVATIONAL_CANDIDATE_IDS = {
    "python_runtime_stack_hotspot",
    "python_userland_hotspot",
    "off_cpu_wait_hotspot",
}


def classify_primitive(symbol: str | None) -> str | None:
    value = str(symbol or "").strip()
    if not value:
        return None
    for kind, pattern in _PRIMITIVE_PATTERNS:
        if pattern.search(value):
            return kind
    return None


def enforce_conclusion_eligibility(tree: ControlledAITree | None) -> ControlledAITree | None:
    if tree is None:
        return None

    layers: list[AITreeLayer] = []
    for layer in tree.layers:
        grouped = {"primary": [], "secondary": [], "rejected": [], "unknown": []}
        for node in _layer_nodes(layer):
            guarded = _guard_candidate(node)
            role = guarded.role
            if role in {"primary", "secondary"} and guarded.causal_status == "contradicted":
                role = "rejected"
                guarded = guarded.model_copy(update={"role": role})
            elif role in {"primary", "secondary"} and not guarded.conclusion_eligible:
                role = "unknown"
                guarded = guarded.model_copy(update={"role": role})
            elif role == "unknown" and guarded.conclusion_eligible:
                role = "secondary"
                guarded = guarded.model_copy(update={"role": role})
            grouped[role].append(guarded)
        layers.append(layer.model_copy(update={
            "primary_causes": grouped["primary"],
            "secondary_causes": grouped["secondary"],
            "rejected_causes": grouped["rejected"],
            "unknown_causes": grouped["unknown"],
        }))

    return tree.model_copy(update={
        "layers": layers,
        "final_primary_causes": _final_ids(layers, "primary", eligible_only=True),
        "final_secondary_causes": _final_ids(layers, "secondary", eligible_only=True),
        "final_rejected_causes": _final_ids(layers, "rejected"),
        "final_unknown_causes": _final_ids(layers, "unknown"),
    })


def _guard_candidate(node: AITreeCandidateNode) -> AITreeCandidateNode:
    primitive_kind = node.primitive_kind or classify_primitive(node.target) or classify_primitive(node.mechanism)
    refs = list(dict.fromkeys([
        *node.evidence_refs,
        *node.self_challenge.supporting_evidence_refs,
    ]))
    reason = ""
    eligible = (
        node.generated_by in {"ai", "ai_candidate", "ai_guarded"}
        or node.generated_by == "analyzer" and node.conclusion_eligible
    ) and node.claim_status not in {"boundary", "duplicate", "rejected"}
    if not eligible:
        reason = "候选尚未通过 AI 或 Analyzer 工程资格门禁。"
    observational_candidate = (
        node.candidate_id in _OBSERVATIONAL_CANDIDATE_IDS
        or node.mechanism in _OBSERVATIONAL_CANDIDATE_IDS
    )
    if observational_candidate:
        eligible = False
        reason = "该节点只表示采样热点或等待观察，不能作为正式主因；需挂在基础定位之后作为附加解释。"
    elif node.role == "rejected" or node.status in {"contradicted", "rejected"}:
        eligible = False
        reason = "候选已经被明确反证或拒绝。"
    elif node.status == "forbidden":
        eligible = False
        reason = "当前策略或证据边界禁止继续升级，但不构成对候选的反证。"
    elif node.depth_kind != "base":
        eligible = False
        reason = "机制或边界节点只能作为附加解释，不能进入正式基础结论。"
    elif node.status != "supported" or node.causal_status != "supported":
        eligible = False
        reason = "当前只有观察或相关性，尚未形成受支持的因果判断。"
    elif node.claim_type not in {
        "root_cause",
        "complete_root_cause",
        "direct_root_cause",
        "complete_source_root_cause",
        "likely_root_cause",
    }:
        eligible = False
        reason = "节点类型不是可进入最终结论的根因声明。"
    elif not node.mechanism.strip() or not node.target.strip():
        eligible = False
        reason = "根因声明缺少具体机制或具体目标。"
    elif not refs:
        eligible = False
        reason = "根因声明没有可回溯的支持证据。"
    elif primitive_kind:
        eligible = False
        reason = "当前目标是等待、调度、系统调用或运行时原语，缺少上层业务栈，不能作为根因函数。"

    decision = node.decision
    causal_status = node.causal_status
    claim_type = node.claim_type
    if primitive_kind and causal_status == "supported":
        causal_status = "unproven"
        claim_type = "observation_only"
        decision = "continue_probe"
    if observational_candidate:
        causal_status = "unproven"
        claim_type = "observation_only"
        decision = "continue_probe"
    if node.status in {"contradicted", "rejected"}:
        causal_status = "contradicted"
        decision = "reject_candidate"
    elif node.status == "forbidden":
        causal_status = "inconclusive"
        decision = "backtrack"
    elif eligible:
        decision = "conclude"

    return node.model_copy(update={
        "primitive_kind": primitive_kind,
        "claim_type": claim_type,
        "causal_status": causal_status,
        "decision": decision,
        "conclusion_eligible": eligible,
        "eligibility_reason": reason or "机制、目标、因果状态和证据引用满足结论资格门禁。",
    })


def qualify_ai_candidate(
    node: AITreeCandidateNode,
    *,
    valid_evidence_refs: set[str] | None = None,
    known_candidate_ids: set[str] | None = None,
    anchor_evidence_refs: set[str] | None = None,
    runtime_anchor_evidence_refs: set[str] | None = None,
    line_anchor_evidence_refs: set[str] | None = None,
) -> tuple[bool, str]:
    """Return the single eligibility decision used by formal conclusions."""
    if node.generated_by not in {"ai", "ai_candidate", "ai_guarded"}:
        return False, "candidate 来源不是 AI。"
    if known_candidate_ids is not None and node.candidate_id not in known_candidate_ids:
        return False, "candidate ID 不属于当前 AI DAG。"
    if node.relation != "root" and not node.parent_candidate_ids:
        return False, "candidate 缺少显式来源父节点。"
    if node.origin_parent_candidate_id and node.origin_parent_candidate_id not in node.parent_candidate_ids:
        return False, "candidate 的 origin_parent_candidate_id 不在 parent_candidate_ids 中。"
    if known_candidate_ids is not None and any(
        parent_id not in known_candidate_ids
        for parent_id in node.parent_candidate_ids
    ):
        return False, "candidate 引用了当前 AI DAG 不存在的父节点。"
    if valid_evidence_refs is not None and any(ref not in valid_evidence_refs for ref in node.evidence_refs):
        return False, "candidate 包含当前会话不存在的 evidence ref。"
    if anchor_evidence_refs is not None and not (
        set(node.evidence_refs) & anchor_evidence_refs
    ):
        return False, "candidate 没有引用真实运行时或源码锚点证据。"
    if runtime_anchor_evidence_refs is not None and not (
        set(node.evidence_refs) & runtime_anchor_evidence_refs
    ):
        return False, "candidate 只有源码上下文或摘要证据，没有同窗运行时锚点。"
    if (
        node.supported_level == "line"
        and line_anchor_evidence_refs is not None
        and not (set(node.evidence_refs) & line_anchor_evidence_refs)
    ):
        return False, "candidate 声明 line，但引用证据没有真实 file:line 锚点。"
    guarded = _guard_candidate(node)
    if not guarded.conclusion_eligible:
        return False, guarded.eligibility_reason
    if guarded.decision != "conclude" or guarded.causal_status != "supported":
        return False, "AI 候选尚未以 supported/conclude 状态闭合。"
    return True, guarded.eligibility_reason


def evidence_refs_with_anchors(
    evidence_catalog: Iterable[dict[str, Any]] | None,
) -> set[str]:
    """Return evidence IDs that contain a bounded runtime/source anchor."""
    result: set[str] = set()
    for item in evidence_catalog or []:
        if not isinstance(item, dict) or not _contains_runtime_or_source_anchor(item):
            continue
        for key in ("evidence_id", "evidence_ref", "raw_artifact_ref", "derived_artifact_ref"):
            value = str(item.get(key) or "").strip()
            if value:
                result.add(value)
    return result


def evidence_refs_with_runtime_anchors(
    evidence_catalog: Iterable[dict[str, Any]] | None,
) -> set[str]:
    result: set[str] = set()
    for item in evidence_catalog or []:
        if not isinstance(item, dict) or not _contains_runtime_anchor(item):
            continue
        for key in ("evidence_id", "evidence_ref", "raw_artifact_ref", "derived_artifact_ref"):
            value = str(item.get(key) or "").strip()
            if value:
                result.add(value)
    return result


def evidence_refs_with_line_anchors(
    evidence_catalog: Iterable[dict[str, Any]] | None,
) -> set[str]:
    result: set[str] = set()
    for item in evidence_catalog or []:
        if not isinstance(item, dict) or not _contains_line_anchor(item):
            continue
        for key in ("evidence_id", "evidence_ref", "raw_artifact_ref", "derived_artifact_ref"):
            value = str(item.get(key) or "").strip()
            if value:
                result.add(value)
    return result


def _contains_runtime_or_source_anchor(value: Any, *, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, dict):
        if any(
            key in value and value.get(key)
            for key in (
                "runtime_anchor",
                "source_anchor",
                "specific_anchor",
                "primary_anchor",
                "runtime_line_candidates",
                "line_candidates",
                "call_path_hotspots",
                "stack_samples",
                "top_wait_stacks",
                "runtime_control_event",
                "source_relations",
                "mechanism_paths",
            )
        ):
            return True
        if any(
            key in value and value.get(key)
            for key in (
                "pid",
                "tid",
                "service_id",
                "instance_id",
                "endpoint",
                "dependency_id",
                "file",
                "file_path",
                "line",
                "symbol",
                "function",
                "call_path",
                "top_frame",
            )
        ):
            return True
        return any(
            _contains_runtime_or_source_anchor(child, depth=depth + 1)
            for child in value.values()
        )
    if isinstance(value, list):
        return any(
            _contains_runtime_or_source_anchor(child, depth=depth + 1)
            for child in value
        )
    return False


def _contains_runtime_anchor(value: Any, *, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, dict):
        if any(
            key in value and value.get(key)
            for key in (
                "runtime_anchor",
                "specific_anchor",
                "primary_anchor",
                "runtime_line_candidates",
                "call_path_hotspots",
                "stack_samples",
                "top_wait_stacks",
                "runtime_control_event",
                "pid",
                "tid",
                "service_id",
                "instance_id",
                "endpoint",
                "dependency_id",
                "top_frame",
            )
        ):
            return True
        return any(
            _contains_runtime_anchor(child, depth=depth + 1)
            for child in value.values()
        )
    if isinstance(value, list):
        return any(
            _contains_runtime_anchor(child, depth=depth + 1)
            for child in value
        )
    return False


def _contains_line_anchor(value: Any, *, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, dict):
        line = value.get("line", value.get("line_number", value.get("focus_line")))
        if (
            value.get("file") or value.get("file_path")
        ) and str(line or "").isdigit() and int(line) > 0:
            return True
        return any(
            _contains_line_anchor(child, depth=depth + 1)
            for child in value.values()
        )
    if isinstance(value, list):
        return any(
            _contains_line_anchor(child, depth=depth + 1)
            for child in value
        )
    return False


def _layer_nodes(layer: AITreeLayer) -> list[AITreeCandidateNode]:
    return [
        *layer.primary_causes,
        *layer.secondary_causes,
        *layer.rejected_causes,
        *layer.unknown_causes,
    ]


def _final_ids(layers: list[AITreeLayer], role: str, *, eligible_only: bool = False) -> list[str]:
    nodes = [node for layer in layers for node in _layer_nodes(layer)]
    selected_ids = {
        node.candidate_id
        for node in nodes
        if node.role == role and (not eligible_only or node.conclusion_eligible)
    }
    frontier_ids = set(select_terminal_candidate_ids(nodes, selected_ids))
    return [
        node.candidate_id
        for node in nodes
        if node.candidate_id in frontier_ids
    ]


def select_terminal_candidate_ids(
    nodes: list[AITreeCandidateNode],
    selected_ids: set[str],
) -> list[str]:
    """Keep selected DAG nodes that have no selected descendant."""
    if not selected_ids:
        return []
    parents = {
        node.candidate_id: [
            parent_id for parent_id in node.parent_candidate_ids if parent_id
        ]
        for node in nodes
    }
    selected_ancestors: set[str] = set()
    for candidate_id in selected_ids:
        stack = list(parents.get(candidate_id, []))
        visited: set[str] = set()
        while stack:
            parent_id = stack.pop()
            if parent_id in visited:
                continue
            visited.add(parent_id)
            if parent_id in selected_ids:
                selected_ancestors.add(parent_id)
            stack.extend(parents.get(parent_id, []))
    return [
        node.candidate_id
        for node in nodes
        if node.candidate_id in selected_ids and node.candidate_id not in selected_ancestors
    ]
