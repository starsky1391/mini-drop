"""Safe, user-triggered validation suite for Mini-Drop AI services.

The suite exercises the provider and every AI-assisted Drop layer with small,
synthetic inputs. Results intentionally exclude API keys, monetary balances,
model-generated reasoning and raw provider responses.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

import requests

from server.app.ai_provider import chat_completions, get_ai_settings
from server.app.diagnosis.intent import parse_diagnosis_intent
from server.app.diagnosis.schemas import CreateDiagnosisRequest
from server.app.nlp.intent_parser import parse_intent
from server.app.nlp.summarizer import summarize
from server.app.rca.llm_client import diagnose
from server.app.rca.models import EvidenceInput


class AIValidationBusy(RuntimeError):
    """Raised when another validation run is already consuming model calls."""


_validation_lock = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(
    check_id: str,
    name: str,
    layer: str,
    passed: bool,
    started: float,
    detail: str,
    **metrics: Any,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "name": name,
        "layer": layer,
        "status": "PASS" if passed else "FAIL",
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "detail": detail,
        "metrics": metrics,
    }


def _safe_check(
    check_id: str,
    name: str,
    layer: str,
    function: Callable[[], tuple[bool, str, dict[str, Any]]],
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        passed, detail, metrics = function()
        return _result(check_id, name, layer, passed, started, detail, **metrics)
    except Exception as exc:  # validation must report a failure, not break the page
        return _result(
            check_id,
            name,
            layer,
            False,
            started,
            f"验证调用异常：{type(exc).__name__}",
            error_type=type(exc).__name__,
        )


def _check_configuration() -> tuple[bool, str, dict[str, Any]]:
    settings = get_ai_settings()
    features = {
        "nlp": settings.nlp_enabled,
        "rca": settings.rca_enabled,
        "summarize": settings.summarize_enabled,
    }
    passed = bool(settings.api_key) and settings.enabled not in {"none", "off"} and all(features.values())
    detail = "AI Provider 配置与 Drop 三项能力均已启用" if passed else "AI Provider Key 或功能开关未完整启用"
    return passed, detail, {
        "mode": settings.enabled,
        "source": settings.source,
        "profile_id": settings.profile_id,
        "features": features,
        "key_present": bool(settings.api_key),
    }


def _check_balance() -> tuple[bool, str, dict[str, Any]]:
    settings = get_ai_settings()
    response = requests.get(
        f"{settings.base_url}/user/balance",
        headers={"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json"},
        timeout=20,
    )
    if response.status_code in {404, 405}:
        return True, "Provider 未暴露余额接口，已按 OpenAI-compatible 可选能力跳过", {
            "http_status": response.status_code,
            "optional_capability": "unsupported",
        }
    payload = response.json() if response.status_code == 200 else {}
    available = payload.get("is_available") is True
    passed = response.status_code == 200 and available
    detail = "账户可调用；具体余额不展示" if passed else f"账户不可用（HTTP {response.status_code}）"
    return passed, detail, {
        "http_status": response.status_code,
        "is_available": available,
        "currency_count": len(payload.get("balance_infos") or []),
    }


def _check_model_discovery() -> tuple[bool, str, dict[str, Any]]:
    settings = get_ai_settings()
    response = requests.get(
        _models_url(settings.base_url),
        headers={"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json"},
        timeout=20,
    )
    payload = response.json() if response.status_code == 200 else {}
    model_ids = {
        item.get("id") for item in payload.get("data", []) if isinstance(item, dict)
    }
    present = settings.model in model_ids
    passed = response.status_code == 200 and present
    detail = f"已发现配置模型 {settings.model}" if passed else f"模型不可用（HTTP {response.status_code}）"
    return passed, detail, {"http_status": response.status_code, "model_present": present}


def _models_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def _check_chat_completion() -> tuple[bool, str, dict[str, Any]]:
    settings = get_ai_settings()
    response = chat_completions({
        "model": settings.model,
        "messages": [{"role": "user", "content": "Reply with exactly MINI_DROP_OK"}],
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": 32,
    }, timeout=30)
    payload = response.json() if response.status_code == 200 else {}
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    content_valid = "MINI_DROP_OK" in str(message.get("content") or "")
    passed = response.status_code == 200 and content_valid
    detail = "基础对话响应和内容约束通过" if passed else f"基础对话失败（HTTP {response.status_code}）"
    usage = payload.get("usage") or {}
    return passed, detail, {
        "http_status": response.status_code,
        "response_model": payload.get("model"),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _check_nlp_intent() -> tuple[bool, str, dict[str, Any]]:
    intent = parse_intent("请对 mysqld 进程执行 17 秒的 eBPF IO 延迟分析，采样率 101Hz")
    passed = (
        intent.process_name == "mysqld"
        and intent.collector_type == "ebpf_io"
        and intent.duration_sec == 17
        and intent.sample_rate == 101
        and bool(intent.raw_llm_output)
    )
    detail = "自然语言已通过 AI Tool Call 转为受约束任务参数" if passed else "意图解析未命中 AI 结构化输出"
    return passed, detail, {
        "collector_type": intent.collector_type,
        "duration_sec": intent.duration_sec,
        "sample_rate": intent.sample_rate,
        "ai_tool_output": bool(intent.raw_llm_output),
    }


def _check_cluster_intent() -> tuple[bool, str, dict[str, Any]]:
    request = CreateDiagnosisRequest.model_validate({
        "query": (
            "staging 的 checkout-service 单实例 CPU 使用率从 20% 上升到 95%，"
            "perf 采样也显示 CPU 热点，请诊断 CPU 饱和原因"
        ),
        "context": {"service_id": "checkout-service", "environment": "staging"},
        "budget_profile": "production_safe",
    })
    intent = parse_diagnosis_intent(request)
    passed = (
        intent.intent_type == "performance_diagnosis"
        and intent.symptom == "cpu_saturation"
        and intent.target_service == "checkout-service"
        and intent.environment == "staging"
        and intent.constraints.no_high_risk_probe is True
        and intent.constraints.no_automatic_remediation is True
    )
    detail = "集群诊断意图、范围和安全约束有效" if passed else "集群诊断意图 Schema 或安全约束失败"
    return passed, detail, {
        "symptom": intent.symptom,
        "target_service": intent.target_service,
        "high_risk_probe_blocked": intent.constraints.no_high_risk_probe,
        "automatic_remediation_blocked": intent.constraints.no_automatic_remediation,
    }


def _check_summary() -> tuple[bool, str, dict[str, Any]]:
    text = summarize(
        [
            {"name": "fib_hotspot", "samples": 680, "percent": 68.0},
            {"name": "json_encode", "samples": 210, "percent": 21.0},
        ],
        ["递归计算热点明显，建议使用迭代或缓存"],
    )
    template_fallback = "最可能原因：请触发 AI 归因进行深度分析" in text
    passed = bool(text) and len(text) <= 150 and not template_fallback
    detail = "AI 总结已生成并满足 150 字硬限制" if passed else "AI 总结降级或超过长度限制"
    return passed, detail, {
        "summary_length": len(text),
        "within_limit": len(text) <= 150,
        "template_fallback": template_fallback,
    }


def _check_rca() -> tuple[bool, str, dict[str, Any]]:
    settings = get_ai_settings()
    evidence = EvidenceInput(
        task_metadata={"task_id": "ai_validation", "collector_type": "perf_cpu", "status": "DONE"},
        top_functions=[{"name": "fib_hotspot", "samples": 680, "percent": 68.0}],
        suggestions=["递归热点明显，建议使用迭代或缓存"],
    )
    candidates = json.dumps([{
        "candidate_id": "cpu_hotspot_recursive",
        "description": "递归热点导致 CPU 饱和",
        "evidence_refs": ["top_functions[0]"],
        "final_confidence": 0.86,
        "missing_evidence": [],
    }], ensure_ascii=False)
    report = diagnose("ai_validation", evidence, candidates, model_name=settings.model)
    references = [ref for cause in report.report.ranked_causes for ref in cause.evidence_refs]
    passed = report.validated and bool(report.report.ranked_causes) and bool(references)
    detail = "RCA JSON Schema、证据引用和置信度校验通过" if passed else "RCA 输出未通过工程校验"
    return passed, detail, {
        "validated": report.validated,
        "cause_count": len(report.report.ranked_causes),
        "evidence_reference_count": len(references),
        "retry_count": report.retry_count,
    }


def run_ai_validation_suite() -> dict[str, Any]:
    """Run the complete suite once; concurrent runs are rejected to limit spend."""
    if not _validation_lock.acquire(blocking=False):
        raise AIValidationBusy("已有 AI 验证正在运行")
    try:
        settings = get_ai_settings()
        started_at = _utcnow()
        started = time.perf_counter()
        checks = [
            _safe_check("configuration", "AI 配置与功能开关", "配置", _check_configuration),
            _safe_check("balance", "账户可用性", "Provider", _check_balance),
            _safe_check("model_discovery", "模型发现", "Provider", _check_model_discovery),
            _safe_check("chat_completion", "基础模型对话", "Provider", _check_chat_completion),
            _safe_check("nlp_intent", "自然语言任务解析", "Drop NLP", _check_nlp_intent),
            _safe_check("cluster_intent", "集群诊断意图与安全约束", "Drop 集群诊断", _check_cluster_intent),
            _safe_check("summary", "任务结果 AI 总结", "Drop 总结", _check_summary),
            _safe_check("rca", "智能归因与证据校验", "Drop RCA", _check_rca),
        ]
        passed_count = sum(item["status"] == "PASS" for item in checks)
        result = {
            "run_id": f"ai_validation_{uuid4().hex[:12]}",
            "status": "PASSED" if passed_count == len(checks) else "FAILED",
            "provider": settings.provider,
            "model": settings.model,
            "base_url": settings.base_url,
            "source": settings.source,
            "profile_id": settings.profile_id,
            "started_at": started_at,
            "finished_at": _utcnow(),
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "passed_count": passed_count,
            "failed_count": len(checks) - passed_count,
            "total_count": len(checks),
            "checks": checks,
            "security": {
                "api_key_exposed": False,
                "balance_amount_exposed": False,
                "raw_reasoning_exposed": False,
            },
        }
        serialized = json.dumps(result, ensure_ascii=False)
        if settings.api_key and settings.api_key in serialized:
            raise RuntimeError("AI validation response contained a secret")
        return result
    finally:
        _validation_lock.release()
