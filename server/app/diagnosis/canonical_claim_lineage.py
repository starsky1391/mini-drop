"""Canonical claim text normalization and provenance helpers."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Mapping


_PUNCTUATION = str.maketrans({
    "，": ",",
    "。": ".",
    "：": ":",
    "；": ";",
    "！": "!",
    "？": "?",
    "（": "(",
    "）": ")",
})


def normalize_claim(value: Any) -> str:
    """Return a stable representation for semantic duplicate checks."""
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_PUNCTUATION)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return re.sub(r"[\s,.:;!?]+$", "", text)


def hash_claim(value: Any) -> str:
    normalized = normalize_claim(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def canonical_claim_fields(
    claim: Any,
    *,
    generated_by: str,
    claim_origin: str,
    claim_transform: str = "original",
    claim_status: str = "active",
    source_candidate_id: str = "",
    source_claim_hash: str = "",
    source_round: int | None = None,
    source_event_id: str = "",
) -> dict[str, Any]:
    current_hash = hash_claim(claim)
    return {
        "generated_by": generated_by,
        "claim_origin": claim_origin,
        "claim_transform": claim_transform,
        "claim_status": claim_status,
        "claim_hash": current_hash,
        "source_claim_hash": source_claim_hash or current_hash,
        "source_candidate_id": source_candidate_id,
        "source_round": source_round,
        "source_event_id": source_event_id,
    }


def infer_claim_origin(record: Mapping[str, Any]) -> tuple[str, str]:
    generated_by = str(record.get("generated_by") or "")
    if generated_by in {"ai", "ai_candidate"}:
        return "ai", "ai_proposal"
    if generated_by == "history" or record.get("claim_transform") == "restored":
        return "history", "history_restore"
    if generated_by in {"fallback", "fallback_observation", "analyzer_fallback"}:
        return "fallback", "fallback_generated"
    if record.get("diagnostic_claim"):
        return "analyzer", "analyzer_diagnostic"
    if record.get("summary") and not record.get("description"):
        return "analyzer", "analyzer_summary"
    return "analyzer", "analyzer_rule"


def ensure_claim_lineage(record: Mapping[str, Any], *, claim_key: str = "claim") -> dict[str, Any]:
    """Fill missing lineage without changing an already registered source."""
    result = dict(record)
    claim = result.get(claim_key)
    if claim is None and claim_key != "description":
        claim = result.get("description") or result.get("diagnostic_claim") or result.get("summary") or ""
    generated_by, origin = infer_claim_origin(result)
    if result.get("generated_by") not in {"analyzer", "ai", "fallback", "history", "system"}:
        result["generated_by"] = generated_by
    result.setdefault("claim_origin", origin)
    result.setdefault("claim_transform", "original")
    result.setdefault("claim_status", "active")
    result["claim_hash"] = hash_claim(claim)
    result.setdefault("source_claim_hash", result["claim_hash"])
    result.setdefault("source_candidate_id", "")
    result.setdefault("source_round", None)
    result.setdefault("source_event_id", "")
    return result


def apply_ai_claim_update(existing: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """Apply an AI update while preserving lineage when claim text is absent."""
    result = ensure_claim_lineage(existing)
    result.update(update)
    if not isinstance(update.get("claim"), str) or not update["claim"].strip():
        for field in (
            "generated_by", "claim_origin", "claim_transform", "claim_status",
            "claim_hash", "source_claim_hash", "source_candidate_id", "source_round",
            "source_event_id",
        ):
            result[field] = ensure_claim_lineage(existing).get(field)
        result["claim"] = existing.get("claim", result.get("claim", ""))
        return result
    old_hash = hash_claim(existing.get("claim"))
    result.update(canonical_claim_fields(
        update["claim"],
        generated_by="ai",
        claim_origin="ai_update",
        claim_transform="refined",
        source_candidate_id=str(existing.get("candidate_id") or ""),
        source_claim_hash=old_hash,
        source_round=update.get("source_round"),
        source_event_id=str(update.get("source_event_id") or ""),
    ))
    return result


def inherit_claim(source: Mapping[str, Any], *, status: str = "inherited") -> dict[str, Any]:
    result = ensure_claim_lineage(source)
    result.update({
        "claim_transform": "inherited",
        "claim_status": status,
        "source_candidate_id": str(source.get("candidate_id") or source.get("source_candidate_id") or ""),
        "source_claim_hash": hash_claim(source.get("claim")),
    })
    return result


def boundary_metadata(message: Any) -> dict[str, Any]:
    return {
        "boundary_message": str(message or "").strip(),
        "claim_transform": "boundary",
        "claim_status": "boundary",
    }
