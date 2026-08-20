"""Bounded AI explanation for one Watch anomaly point."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field

from server.app.ai_provider import chat_completions, get_ai_settings, is_feature_enabled
from server.app.diagnosis.schemas import StrictModel


class WatchAnomalyExplanation(StrictModel):
    summary: str = Field(min_length=1, max_length=240)
    why_triggered: str = Field(min_length=1, max_length=600)
    classification: Literal["resource_shift", "impact_signal", "hard_state"]
    observed_change: str = Field(min_length=1, max_length=400)
    same_window_context: list[str] = Field(default_factory=list, max_length=8)
    data_quality: str = Field(min_length=1, max_length=300)
    boundary_notice: str = Field(min_length=1, max_length=300)


def explain_watch_anomaly(
    *,
    watch: Any,
    incident: Any,
    anomaly_point: dict[str, Any],
) -> dict[str, Any]:
    if not is_feature_enabled("summarize"):
        raise RuntimeError("轻量 AI 解释未启用或当前 AI 配置缺少 API Key")
    settings = get_ai_settings()
    payload = {
        "watch": {
            "watch_id": watch.watch_id,
            "name": watch.name,
            "target": watch.target.model_dump(mode="json"),
        },
        "episode": {
            "episode_id": incident.episode_id,
            "window_start": incident.window_start.isoformat(),
            "window_end": incident.window_end.isoformat(),
            "impact_status": incident.impact_status,
            "occurrence_count": incident.occurrence_count,
        },
        "anomaly_point": {
            key: value
            for key, value in anomaly_point.items()
            if key not in {"explanation"}
        },
        "same_window_signals": [
            {
                "trigger_type": item.get("trigger_type"),
                "metric": item.get("metric"),
                "latest_value": item.get("latest_value"),
                "relative_shift": item.get("relative_shift"),
                "impact_status": item.get("impact_status"),
            }
            for item in incident.anomaly_points
        ],
    }
    response = chat_completions({
        "model": settings.model,
        "temperature": 0,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是持续监视异常点解释器。只解释为什么该数据点被触发、变化幅度、"
                    "同窗信号和数据质量。不得判断根因，不得提出探针，不得创建诊断树，"
                    "不得给修复建议。必须输出 JSON，字段为 summary、why_triggered、"
                    "classification、observed_change、same_window_context、data_quality、"
                    "boundary_notice。classification 只能是 resource_shift、impact_signal、hard_state。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
        ],
    }, timeout=45)
    if response.status_code != 200:
        raise RuntimeError(f"轻量 AI 解释请求失败（HTTP {response.status_code}）")
    try:
        body = response.json()
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = _parse_json_content(content)
        explanation = WatchAnomalyExplanation.model_validate(parsed)
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("轻量 AI 解释响应不符合结构化协议") from exc
    result = explanation.model_dump(mode="json")
    result.update({
        "model": body.get("model") or settings.model,
        "provider": settings.provider,
        "fingerprint": anomaly_point.get("fingerprint"),
        "cached": False,
    })
    return result


def _parse_json_content(content: Any) -> dict[str, Any]:
    text = str(content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("AI response must be an object")
    return value
