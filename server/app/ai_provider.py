"""AI provider configuration and OpenAI-compatible chat client.

The runtime is intentionally vendor-neutral. Any provider that exposes an
OpenAI-compatible `/v1/chat/completions` endpoint can be used by setting URL,
API key and model through environment variables.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Literal

from server.app.common_utils import env_bool
from server.app.ai_provider_profiles import get_active_profile_settings, normalize_base_url

FeatureName = Literal["nlp", "rca", "summarize"]


@dataclass(frozen=True)
class AISettings:
    enabled: str
    provider: str
    base_url: str
    api_key: str
    model: str
    source: str
    profile_id: str | None
    nlp_enabled: bool
    rca_enabled: bool
    summarize_enabled: bool


def get_ai_settings() -> AISettings:
    active_profile = get_active_profile_settings()
    if active_profile:
        mode = str(active_profile["enabled"]).strip().lower()
        provider = str(active_profile["provider"]).strip()
        base_url = normalize_base_url(str(active_profile["base_url"]))
        api_key = str(active_profile["api_key"]).strip()
        model = str(active_profile["model"]).strip()
        source = "profile"
        profile_id = str(active_profile["profile_id"])
    else:
        mode = os.getenv("MINI_DROP_AI_ENABLED", "full").strip().lower()
        provider = _first_non_empty("MINI_DROP_AI_PROVIDER", "DEEPSEEK_PROVIDER", default="deepseek")
        base_url = normalize_base_url(_first_non_empty("MINI_DROP_AI_BASE_URL", "DEEPSEEK_API_BASE", default="https://api.deepseek.com"))
        api_key = _first_non_empty("MINI_DROP_AI_API_KEY", "DEEPSEEK_API_KEY", default="")
        model = _first_non_empty("MINI_DROP_AI_MODEL", "DEEPSEEK_MODEL", default="deepseek-v4-flash")
        source = "env"
        profile_id = None

    defaults = _mode_defaults(mode)
    feature_flags = _apply_feature_overrides(defaults)
    return AISettings(
        enabled=mode,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        source=source,
        profile_id=profile_id,
        nlp_enabled=feature_flags["nlp"],
        rca_enabled=feature_flags["rca"],
        summarize_enabled=feature_flags["summarize"],
    )


def is_feature_enabled(feature: FeatureName) -> bool:
    settings = get_ai_settings()
    if not settings.api_key:
        return False
    return {
        "nlp": settings.nlp_enabled,
        "rca": settings.rca_enabled,
        "summarize": settings.summarize_enabled,
    }[feature]


def chat_completions(payload: dict[str, Any], timeout: int = 60):
    settings = get_ai_settings()
    return _post_json(
        _chat_url(settings.base_url),
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )


def test_openai_compatible_provider(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: int = 30,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = _post_json(
            _chat_url(base_url.rstrip("/")),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly MINI_DROP_OK"}],
            "temperature": 0,
            "max_tokens": 200,
        },
            timeout=timeout,
        )
    except Exception as exc:
        return {
            "passed": False,
            "http_status": 0,
            "model": model,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "content_valid": False,
            "error_type": type(exc).__name__,
            "message": f"Provider 请求失败：{type(exc).__name__}",
        }
    try:
        payload = response.json() if response.status_code == 200 else {}
    except ValueError:
        return {
            "passed": False,
            "http_status": response.status_code,
            "model": model,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "content_valid": False,
            "error_type": "InvalidJSON",
            "message": "Provider 响应不是 OpenAI-compatible JSON",
        }
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    content_valid = "MINI_DROP_OK" in str(message.get("content") or "")
    return {
        "passed": response.status_code == 200 and content_valid,
        "http_status": response.status_code,
        "model": payload.get("model") or model,
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "content_valid": content_valid,
        "message": "AI Provider 连接正常" if response.status_code == 200 and content_valid else f"Provider 响应未通过内容校验（HTTP {response.status_code}）",
    }


def _chat_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _mode_defaults(mode: str) -> dict[str, bool]:
    if mode == "none":
        return {"nlp": False, "rca": False, "summarize": False}
    if mode == "nlp-only":
        return {"nlp": True, "rca": False, "summarize": False}
    if mode == "rca-only":
        return {"nlp": False, "rca": True, "summarize": False}
    return {"nlp": True, "rca": True, "summarize": True}


def _first_non_empty(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _apply_feature_overrides(mode_defaults: dict[str, bool]) -> dict[str, bool]:
    """Apply per-feature env flags without bypassing the global AI mode.

    MINI_DROP_AI_ENABLED is the upper bound. For example, `none` always disables
    every feature even if `.env` still contains MINI_DROP_NLP_ENABLED=true.
    """
    env_names = {
        "nlp": "MINI_DROP_NLP_ENABLED",
        "rca": "MINI_DROP_RCA_ENABLED",
        "summarize": "MINI_DROP_SUMMARIZE_ENABLED",
    }
    result: dict[str, bool] = {}
    for feature, default_enabled in mode_defaults.items():
        result[feature] = bool(default_enabled) and env_bool(env_names[feature], default_enabled)
    return result


def _post_json(url: str, headers: dict, json: dict, timeout: int):
    import requests
    return requests.post(url, headers=headers, json=json, timeout=timeout)
