"""RCA analysis pipeline selection."""

from __future__ import annotations


_ALIASES = {
    "legacy": "legacy",
    "old": "legacy",
    "hypothesis": "legacy",
    "hypothesis_validation": "legacy",
    "evidence": "evidence_to_attribution",
    "evidence_to_attribution": "evidence_to_attribution",
    "evidence-to-attribution": "evidence_to_attribution",
    "eta": "evidence_to_attribution",
    "default": "evidence_to_attribution",
    "current": "evidence_to_attribution",
}


def normalize_pipeline_id(value: str | None) -> str:
    key = (value or "evidence_to_attribution").strip().lower().replace("-", "_")
    try:
        return _ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"未知 RCA 分析链路: {value}") from exc
