"""Deterministic final reducer for controlled diagnosis candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from server.app.diagnosis.canonical_claim_lineage import ensure_claim_lineage, hash_claim
from server.app.rca.models import AITreeCandidateNode, AITreeLayer, ControlledAITree


LEVEL_ORDER = {
    level: index
    for index, level in enumerate((
        "resource", "host", "process", "thread", "syscall", "dependency",
        "service", "endpoint", "function", "call_path", "line",
    ))
}


@dataclass(frozen=True)
class RefinementValidation:
    valid: bool
    code: str = ""
    reason: str = ""


def _value(item: Mapping[str, Any] | AITreeCandidateNode, field: str, default: Any = "") -> Any:
    return getattr(item, field, default) if isinstance(item, AITreeCandidateNode) else item.get(field, default)


def validate_claim_refinement(
    parent: Mapping[str, Any] | AITreeCandidateNode,
    child: Mapping[str, Any] | AITreeCandidateNode,
) -> RefinementValidation:
    parent_claim_hash = hash_claim(_value(parent, "claim"))
    child_claim_hash = hash_claim(_value(child, "claim"))
    if not child_claim_hash:
        return RefinementValidation(False, "refinement_not_more_specific", "refinement claim 不能为空")
    if parent_claim_hash == child_claim_hash:
        return RefinementValidation(False, "duplicate_claim", "refinement claim 与来源父节点相同")

    parent_level = LEVEL_ORDER.get(str(_value(parent, "supported_level")), 0)
    child_level = LEVEL_ORDER.get(str(_value(child, "supported_level")), 0)
    parent_chain = tuple(_value(parent, "causal_chain", []) or [])
    child_chain = tuple(_value(child, "causal_chain", []) or [])
    specifics = (
        bool(str(_value(child, "mechanism")).strip())
        and str(_value(child, "mechanism")).strip() != str(_value(parent, "mechanism")).strip(),
        bool(str(_value(child, "target")).strip())
        and str(_value(child, "target")).strip() != str(_value(parent, "target")).strip(),
        child_level > parent_level,
        bool(str(_value(child, "localization_object")).strip())
        and str(_value(child, "localization_object")).strip() != str(_value(parent, "localization_object")).strip(),
        len(child_chain) > len(parent_chain),
    )
    if not any(specifics):
        return RefinementValidation(
            False,
            "refinement_not_more_specific",
            "refinement 未增加机制、目标、定位层级、定位对象或因果链",
        )
    return RefinementValidation(True)


def _all_nodes(layer: AITreeLayer) -> list[AITreeCandidateNode]:
    return [
        *layer.primary_causes,
        *layer.secondary_causes,
        *layer.rejected_causes,
        *layer.unknown_causes,
    ]


def reduce_candidate_state(
    tree: ControlledAITree,
    *,
    current_round: int | None = None,
) -> ControlledAITree:
    """Normalize lineage and remove invalid refinement nodes from session_main."""
    known: dict[str, AITreeCandidateNode] = {}
    records = list(tree.data_quality.get("records") or [])
    layers: list[AITreeLayer] = []

    for layer in sorted(tree.layers, key=lambda item: (item.depth, item.layer_id)):
        grouped: dict[str, list[AITreeCandidateNode]] = {
            "primary": [], "secondary": [], "rejected": [], "unknown": [],
        }
        for node in _all_nodes(layer):
            lineage = ensure_claim_lineage(node.model_dump(mode="python"))
            normalized = node.model_copy(update={
                field: lineage[field]
                for field in (
                    "generated_by", "claim_origin", "claim_transform", "claim_status",
                    "claim_hash", "source_claim_hash", "source_candidate_id", "source_round",
                    "source_event_id",
                )
            })
            if (
                normalized.relation == "boundary"
                or normalized.depth_kind == "boundary"
                or normalized.node_type in {"stop_boundary", "evidence_gap"}
            ):
                normalized = normalized.model_copy(update={
                    "boundary_message": normalized.boundary_message or normalized.claim,
                    "claim": "",
                    "claim_transform": "boundary",
                    "claim_status": "boundary",
                    "claim_hash": "",
                    "conclusion_eligible": False,
                })
            if (
                tree.tree_kind == "session_main"
                and (
                    normalized.generated_by == "history"
                    or normalized.claim_transform == "restored"
                    or current_round is not None
                    and normalized.source_round is not None
                    and normalized.source_round != current_round
                )
            ):
                records.append({
                    "candidate_id": normalized.candidate_id,
                    "status": "history_excluded",
                    "source_round": normalized.source_round,
                })
                continue

            parent = known.get(str(normalized.origin_parent_candidate_id or ""))
            if parent is not None:
                validation = (
                    RefinementValidation(False, "duplicate_claim", "子节点 claim 与来源父节点相同")
                    if normalized.claim_hash and normalized.claim_hash == parent.claim_hash
                    else validate_claim_refinement(parent, normalized)
                    if normalized.relation == "refinement"
                    else RefinementValidation(True)
                )
                if not validation.valid:
                    merged_refs = list(dict.fromkeys([*parent.evidence_refs, *normalized.evidence_refs]))
                    parent = parent.model_copy(update={"evidence_refs": merged_refs})
                    known[parent.candidate_id] = parent
                    for existing_layer in layers:
                        for role in grouped:
                            items = getattr(existing_layer, f"{role}_causes")
                            for index, item in enumerate(items):
                                if item.candidate_id == parent.candidate_id:
                                    items[index] = parent
                    records.append({
                        "candidate_id": normalized.candidate_id,
                        "origin_parent_candidate_id": parent.candidate_id,
                        "status": validation.code,
                        "reason": validation.reason,
                        "claim_hash": normalized.claim_hash,
                    })
                    continue
            known[normalized.candidate_id] = normalized
            grouped[normalized.role].append(normalized)
        layers.append(layer.model_copy(update={
            "primary_causes": grouped["primary"],
            "secondary_causes": grouped["secondary"],
            "rejected_causes": grouped["rejected"],
            "unknown_causes": grouped["unknown"],
        }))

    emitted = set(known)
    data_quality = dict(tree.data_quality)
    data_quality["records"] = records
    return tree.model_copy(update={
        "layers": layers,
        "data_quality": data_quality,
        "final_primary_causes": [item for item in tree.final_primary_causes if item in emitted],
        "final_secondary_causes": [item for item in tree.final_secondary_causes if item in emitted],
        "final_rejected_causes": [item for item in tree.final_rejected_causes if item in emitted],
        "final_unknown_causes": [item for item in tree.final_unknown_causes if item in emitted],
    })
