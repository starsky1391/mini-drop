"""Hard guards shared by Analyzer, LLM review, and session AI trees."""

from __future__ import annotations

import re

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
    eligible = node.generated_by in {"ai_candidate", "ai_guarded"}
    if not eligible:
        reason = "只有 AI 生成并通过当前门禁的候选可以进入正式根因结论。"
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
) -> tuple[bool, str]:
    """Return the single eligibility decision used by formal conclusions."""
    if node.generated_by not in {"ai_candidate", "ai_guarded"}:
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
    guarded = _guard_candidate(node)
    if not guarded.conclusion_eligible:
        return False, guarded.eligibility_reason
    if guarded.decision != "conclude" or guarded.causal_status != "supported":
        return False, "AI 候选尚未以 supported/conclude 状态闭合。"
    return True, guarded.eligibility_reason


def _layer_nodes(layer: AITreeLayer) -> list[AITreeCandidateNode]:
    return [
        *layer.primary_causes,
        *layer.secondary_causes,
        *layer.rejected_causes,
        *layer.unknown_causes,
    ]


def _final_ids(layers: list[AITreeLayer], role: str, *, eligible_only: bool = False) -> list[str]:
    result: list[str] = []
    for layer in layers:
        for node in _layer_nodes(layer):
            if node.role != role or (eligible_only and not node.conclusion_eligible):
                continue
            if node.candidate_id not in result:
                result.append(node.candidate_id)
    return result
