"""
Mini-Drop HTTP API 入口。

启动 FastAPI 服务（端口 8191），同时在后台线程运行 gRPC server（端口 50051）。
两者共享同一个 SqlRepository 实例——Agent 通过 gRPC 上报的数据，
Web 通过 HTTP API 即时可见。
"""

from __future__ import annotations

import server.app._env  # noqa: F401 — 自动加载 .env

import json as _json_mod
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path as _Path
from urllib.parse import quote as _url_quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import asyncio
import json as _json
import queue as _queue
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

from server.app.common_utils import json_safe, status_value
from server.app.ai_provider import get_ai_settings, test_openai_compatible_provider
from server.app.ai_provider_profiles import (
    AIProviderProfileCreate,
    AIProviderProfileUpdate,
    activate_profile,
    create_profile,
    delete_profile,
    get_profile,
    list_profiles,
    update_profile,
)
from server.app.ai_validation import AIValidationBusy, run_ai_validation_suite
from server.app.database import init_db, new_session
from server.app.event_bus import BUS, notify_diagnosis_complete
from server.app.prometheus_metrics import record_diagnosis, record_http_request, REGISTRY
from server.app.grpc_server import serve_in_background
from server.app.logging_utils import log_event
from server.app.nlp.intent_parser import parse_intent
from server.app.nlp.process_resolver import resolve_pid
from server.app.nlp.summarizer import summarize, suggest_followup
from server.app.diagnosis import DiagnosisOrchestrator
from server.app.diagnosis.audit_bundle import build_audit_bundle
from server.app.diagnosis.evidence_structurer import (
    StructuredEvidence,
    rca_inputs_from_structured,
    structure_artifact_evidence,
)
from server.app.diagnosis.probe_registry import list_probes as list_registered_probes
from server.app.diagnosis.schemas import (
    ApprovalRequest,
    BulkApprovalRequest,
    CreateDiagnosisRequest,
    DependencyEdge,
    DiagnosisBudget,
    DiagnosisContext,
    ServiceInstance,
    SourceContext,
    TERMINAL_DIAGNOSIS_STATUSES,
    TimeRange,
)
from server.app.diagnosis.watch_runtime import (
    CreateWatchSubscriptionRequest,
    PersistentAgentRuntime,
    WatchEvaluationRequest,
    WatchRegistry,
)
from server.app.rca.pipelines import normalize_pipeline_id
from server.app.rca.report import run_diagnosis_context
from server.app.rca.strategies import normalize_strategy_id
from server.app.schemas import (
    APIResponse,
    CreateTaskRequest,
    MAX_SAMPLE_RATE,
    MAX_TASK_DURATION_SEC,
    RCAFeedbackRequest,
    TaskView,
)
from server.app.sql_repository import SqlRepository
from server.app import storage as store

repo = SqlRepository()
diagnosis_orchestrator = DiagnosisOrchestrator(repo)
watch_registry = WatchRegistry(repo)
watch_runtime = PersistentAgentRuntime(watch_registry, repo)
watch_reconcile_executor = ThreadPoolExecutor(
    max_workers=max(1, int(os.getenv("MINI_DROP_WATCH_RECONCILE_WORKERS", "2"))),
    thread_name_prefix="watch-reconcile",
)
watch_analysis_executor = ThreadPoolExecutor(
    max_workers=max(1, int(os.getenv("MINI_DROP_WATCH_ANALYSIS_WORKERS", "2"))),
    thread_name_prefix="watch-analysis",
)


def _on_task_terminal(
    task_id: str,
    status: str,
    reason: str,
    artifacts: list[dict[str, Any]],
) -> None:
    """异步回灌 Watch 深度任务，避免阻塞 gRPC 结果上报。"""
    watch_reconcile_executor.submit(
        _reconcile_watch_task_terminal,
        task_id,
        status,
        reason,
        artifacts,
    )


def _reconcile_watch_task_terminal(
    task_id: str,
    status: str,
    reason: str,
    artifacts: list[dict[str, Any]],
) -> None:
    """回灌 Watch 深度任务，并在同窗任务全部结束后自动分析。"""
    task = repo.tasks.get(task_id)
    if task is None:
        return
    request_params = getattr(task, "request_params", {}) or {}
    options = request_params.get("options", {}) if isinstance(request_params, dict) else {}
    if not isinstance(options, dict) or options.get("collection_mode") not in {
        "triggered_group",
        "delayed_followup",
    }:
        return

    incident = watch_runtime.ingest_collector_task_result(
        task_id,
        status=status,
        status_reason=reason or getattr(task, "status_reason", "") or "",
        artifacts=artifacts,
        task_options=options,
    )
    if incident is None or incident.status != "ready_for_analysis":
        return
    if incident.analysis_status in {"analyzing", "analyzed", "analysis_failed"}:
        return
    if (
        incident.analysis_status == "needs_evidence"
        and options.get("collection_mode") != "delayed_followup"
    ):
        return

    _create_watch_incident_diagnosis(incident.incident_id)


def _create_watch_incident_diagnosis(incident_id: str) -> dict[str, Any]:
    """Create the real AI diagnosis session for a frozen watch incident."""
    incident = watch_registry.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="异常窗口不存在")
    watch = watch_registry.get(incident.watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="监视订阅不存在")
    if not incident.structured_evidence:
        raise HTTPException(status_code=409, detail="异常窗口缺少结构化证据，无法分析")

    started_at = time.time()
    watch_registry.update_incident_analysis(
        incident_id,
        analysis_status="analyzing",
        analysis_result={
            "mode": "ai_cluster_diagnosis",
            "incident_id": incident_id,
            "watch_id": watch.watch_id,
            "analysis_started_at": started_at,
            "message": "正在创建 AI 集群诊断会话",
        },
    )
    try:
        request = _build_watch_incident_diagnosis_request(watch, incident)
        detail = diagnosis_orchestrator.create(request, creator_id="watch_runtime")
    except Exception as exc:
        return _persist_watch_diagnosis_creation_failure(
            incident,
            error_type=type(exc).__name__,
            detail=str(exc)[:300],
            started_at=started_at,
        )

    diagnosis_id = str(detail.get("diagnosis_id") or "")
    status = str(detail.get("status") or "")
    analysis_status = _watch_analysis_status_from_diagnosis(status)
    result = {
        "mode": "ai_cluster_diagnosis",
        "diagnosis_id": diagnosis_id,
        "analysis_session_id": diagnosis_id,
        "incident_id": incident.incident_id,
        "watch_id": watch.watch_id,
        "trigger_event_id": incident.trigger_event_id,
        "evidence_cohort_id": incident.evidence_cohort_id,
        "timing_relation": "same_window",
        "collection_mode": "rolling_snapshot",
        "diagnosis_status": status,
        "created_from": "watch_incident",
        "elapsed_sec": round(time.time() - started_at, 3),
    }
    updated = watch_registry.update_incident_analysis(
        incident_id,
        analysis_status=analysis_status,
        analysis_session_id=diagnosis_id,
        analysis_result=result,
    )
    return {
        **result,
        "status": status,
        "incident": updated.model_dump(mode="json"),
        "diagnosis": detail,
    }


def _build_watch_incident_diagnosis_request(watch: Any, incident: Any) -> CreateDiagnosisRequest:
    target_config = watch.target_config if isinstance(watch.target_config, dict) else {}
    service_id = watch.target.service_id or target_config.get("service_id") or "watch_target"
    environment = str(target_config.get("environment") or "unknown")
    window_start, window_end = _non_empty_watch_window(incident)
    instance_id = (
        watch.target.instance_id
        or str(target_config.get("instance_id") or "")
        or f"{watch.target.agent_id}:{watch.target.target_pid}"
    )
    agent = repo.agents.get(watch.target.agent_id)
    host_id = (
        str(target_config.get("host_id") or "")
        or str(getattr(agent, "hostname", "") or "")
        or str(getattr(agent, "host_id", "") or "")
        or watch.target.agent_id
    )
    source_context = _source_context_from_watch_config(target_config)
    instance = ServiceInstance(
        service_id=service_id,
        instance_id=instance_id,
        host_id=host_id,
        agent_id=watch.target.agent_id,
        pid=watch.target.target_pid,
        container_id=target_config.get("container_id"),
        environment=environment,
        source_context=source_context,
    )
    budget = None
    if isinstance(target_config.get("diagnosis_budget"), dict):
        budget = DiagnosisBudget.model_validate(target_config["diagnosis_budget"])
    return CreateDiagnosisRequest(
        query=(
            f"持续监视发现 {service_id} 在 {window_start.isoformat()} 到 "
            f"{window_end.isoformat()} 出现 {incident.trigger_type}。"
            f"请优先复用 evidence_cohort_id={incident.evidence_cohort_id} 的同窗冻结证据，"
            "按受控 AI 树生成候选、下探补证并给出证据支持的最细定位。"
        ),
        context=DiagnosisContext(
            service_id=service_id,
            environment=environment,
            time_range=TimeRange(
                start=window_start,
                end=window_end,
                source="request_context",
            ),
            instances=[instance],
            dependencies=_dependency_edges_from_watch_config(service_id, target_config),
            source_context=source_context,
        ),
        budget_profile=str(target_config.get("budget_profile") or os.getenv(
            "MINI_DROP_WATCH_DIAGNOSIS_BUDGET_PROFILE",
            "production_safe",
        )),
        auto_execute_policy=str(target_config.get("auto_execute_policy") or "all_registered"),
        budget=budget,
    )


def _non_empty_watch_window(incident: Any) -> tuple[Any, Any]:
    start = incident.window_start
    end = incident.window_end
    if end > start:
        return start, end
    return start - timedelta(seconds=1), end


def _source_context_from_watch_config(target_config: dict[str, Any]) -> SourceContext | None:
    source = target_config.get("source_context")
    if not isinstance(source, dict):
        source = {
            key: target_config[key]
            for key in (
                "source_paths",
                "repo_revision",
                "language",
                "symbol_map_paths",
                "build_id",
                "container_workdir",
            )
            if key in target_config
        }
    if not source:
        return None
    return SourceContext.model_validate(source)


def _dependency_edges_from_watch_config(service_id: str, target_config: dict[str, Any]) -> list[DependencyEdge]:
    edges: list[DependencyEdge] = []
    raw_targets = target_config.get("dependency_targets")
    if isinstance(raw_targets, list):
        for index, item in enumerate(raw_targets):
            if not isinstance(item, dict):
                continue
            target_service = str(
                item.get("target_service")
                or item.get("dependency_id")
                or item.get("name")
                or item.get("host")
                or f"dependency_{index + 1}"
            )
            edges.append(DependencyEdge(
                source_service=service_id,
                target_service=target_service,
                relation=item.get("relation") or "CALLS",
                protocol=item.get("protocol"),
                host=item.get("host"),
                port=item.get("port"),
                url=item.get("url"),
                path=item.get("path"),
                confidence=item.get("confidence") or "medium",
                source=item.get("source") or "watch_target_config",
            ))
    redis_target = target_config.get("redis_target")
    redis_items = redis_target if isinstance(redis_target, list) else [redis_target]
    for item in redis_items:
        if not item:
            continue
        host = item.get("host") if isinstance(item, dict) else str(item).split(":")[0]
        port = item.get("port") if isinstance(item, dict) else None
        if port is None and isinstance(item, str) and ":" in item:
            _, raw_port = item.rsplit(":", 1)
            port = int(raw_port) if raw_port.isdigit() else None
        edges.append(DependencyEdge(
            source_service=service_id,
            target_service=(item.get("target_service") if isinstance(item, dict) else None) or "redis",
            relation="READS_FROM",
            protocol="redis",
            host=host,
            port=port,
            confidence="medium",
            source="watch_target_config",
        ))
    return edges


def _watch_analysis_status_from_diagnosis(status: str) -> str:
    if status == "FAILED":
        return "analysis_failed"
    if status in {"INSUFFICIENT_EVIDENCE", "BUDGET_EXHAUSTED", "TOPOLOGY_UNAVAILABLE"}:
        return "needs_evidence"
    if status in TERMINAL_DIAGNOSIS_STATUSES:
        return "analyzed"
    return "analyzing"


def _persist_watch_diagnosis_creation_failure(
    incident: Any,
    *,
    error_type: str,
    detail: str,
    started_at: float,
) -> dict[str, Any]:
    result = {
        "mode": "ai_cluster_diagnosis",
        "incident_id": incident.incident_id,
        "watch_id": incident.watch_id,
        "analysis_status": "analysis_failed",
        "auto_analysis_error": error_type,
        "message": "创建 AI 集群诊断会话失败，已保留冻结证据，可从异常窗口重试",
        "detail": detail,
        "retryable": True,
        "elapsed_sec": round(time.time() - started_at, 3),
        "preserved_evidence_refs": [
            item.get("evidence_ref")
            for item in incident.snapshot_refs
            if item.get("evidence_ref")
        ],
    }
    updated = watch_registry.update_incident_analysis(
        incident.incident_id,
        analysis_status="analysis_failed",
        analysis_result=result,
    )
    return {
        **result,
        "incident": updated.model_dump(mode="json"),
    }


def _execute_watch_incident_analysis(
    incident_id: str,
    *,
    analysis_strategy: Optional[str] = None,
    analysis_pipeline: Optional[str] = None,
) -> Any:
    """Run one analysis with a bounded wait and an attempt guard."""
    attempt_id = f"attempt_{uuid4().hex[:12]}"
    started_at = time.time()
    watch_registry.update_incident_analysis(
        incident_id,
        analysis_status="analyzing",
        analysis_result={
            "analysis_attempt_id": attempt_id,
            "analysis_started_at": started_at,
        },
    )
    timeout_sec = max(
        30,
        int(os.getenv("MINI_DROP_WATCH_ANALYSIS_TIMEOUT_SEC", "150")),
    )
    future = watch_analysis_executor.submit(
        _run_watch_incident_analysis,
        incident_id=incident_id,
        analysis_strategy=analysis_strategy,
        analysis_pipeline=analysis_pipeline,
    )
    try:
        result = future.result(timeout=timeout_sec)
    except TimeoutError:
        future.cancel()
        return _persist_watch_analysis_failure(
            incident_id,
            attempt_id=attempt_id,
            error_type="analysis_timeout",
            message="AI 树分析超过有界等待时间，已保留冻结证据，可从异常窗口重试",
            timeout_sec=timeout_sec,
            started_at=started_at,
        )
    except Exception as exc:
        return _persist_watch_analysis_failure(
            incident_id,
            attempt_id=attempt_id,
            error_type=type(exc).__name__,
            message="AI 树分析执行失败，已保留冻结证据，可从异常窗口重试",
            timeout_sec=timeout_sec,
            started_at=started_at,
            detail=str(exc)[:300],
        )
    result["analysis_attempt_id"] = attempt_id
    return _finish_watch_incident_analysis(
        incident_id,
        result,
        attempt_id=attempt_id,
    )


def _persist_watch_analysis_failure(
    incident_id: str,
    *,
    attempt_id: str,
    error_type: str,
    message: str,
    timeout_sec: int,
    started_at: float,
    detail: str = "",
) -> Any:
    incident = watch_registry.get_incident(incident_id)
    if incident is None:
        return None
    result = {
        "analysis_session_id": f"analysis_{incident_id}",
        "incident_id": incident_id,
        "analysis_attempt_id": attempt_id,
        "analysis_status": "analysis_failed",
        "auto_analysis_error": error_type,
        "message": message,
        "detail": detail,
        "retryable": True,
        "timeout_sec": timeout_sec,
        "elapsed_sec": round(time.time() - started_at, 3),
        "preserved_evidence_refs": [
            item.get("evidence_ref")
            for item in incident.snapshot_refs
            if item.get("evidence_ref")
        ],
        "structured_evidence": incident.structured_evidence,
    }
    return watch_registry.update_incident_analysis(
        incident_id,
        analysis_status="analysis_failed",
        analysis_session_id=result["analysis_session_id"],
        analysis_result=result,
    )


def _finish_watch_incident_analysis(
    incident_id: str,
    result: dict[str, Any],
    *,
    attempt_id: str | None = None,
) -> Any:
    """Persist the result and schedule one bounded automatic follow-up round."""
    current = watch_registry.get_incident(incident_id)
    if current is None:
        return None
    current_attempt = (current.analysis_result or {}).get("analysis_attempt_id")
    if attempt_id is not None and current_attempt != attempt_id:
        return current
    if current.analysis_status == "analysis_failed":
        return current
    needs_evidence = bool(result.get("not_enough_evidence"))
    updated = watch_registry.update_incident_analysis(
        incident_id,
        analysis_status="needs_evidence" if needs_evidence else "analyzed",
        analysis_session_id=result["analysis_session_id"],
        analysis_result=result,
    )
    if needs_evidence:
        watch = watch_registry.get(updated.watch_id)
        if watch is not None and watch.trigger_action == "auto_all_registered":
            watch_runtime.schedule_followup_tasks(
                incident_id,
                result.get("next_evidence_requests") or [],
            )
    return watch_registry.get_incident(incident_id) or updated


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """应用生命周期：启动时拉起 gRPC，关闭时停止。"""
    init_db()
    watch_registry.load_persisted()
    if os.getenv("MINIO_AUTO_CREATE_BUCKET", "0") == "1":
        _ensure_minio_bucket_with_retry(os.getenv("MINIO_BUCKET", "mini-drop"))
    _grpc = serve_in_background(
        repo,
        watch_runtime=watch_runtime,
        on_task_terminal=_on_task_terminal,
    )
    _offline_task = asyncio.create_task(_offline_sweeper())
    try:
        yield
    finally:
        _offline_task.cancel()
        try:
            await _offline_task
        except asyncio.CancelledError:
            pass
        _grpc.stop(grace=None).wait(timeout=5)
        watch_reconcile_executor.shutdown(wait=False, cancel_futures=True)
        watch_analysis_executor.shutdown(wait=False, cancel_futures=True)


async def _offline_sweeper() -> None:
    timeout_sec = int(os.getenv("AGENT_OFFLINE_TIMEOUT_SEC", "30"))
    interval_sec = max(1, min(timeout_sec // 2, 15))
    while True:
        repo.mark_offline_agents(timeout_sec=timeout_sec)
        if hasattr(repo, "persist_agent_metric_snapshots"):
            repo.persist_agent_metric_snapshots()
        diagnosis_orchestrator.advance_active()
        await asyncio.sleep(interval_sec)


def _ensure_minio_bucket_with_retry(bucket: str) -> None:
    attempts = max(1, int(os.getenv("MINI_DROP_MINIO_READY_RETRIES", "5")))
    delay_sec = max(0.0, float(os.getenv("MINI_DROP_MINIO_READY_DELAY_SEC", "1")))
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            store.ensure_bucket(bucket)
            return
        except Exception as exc:
            last_exc = exc
            log_event(
                "warning",
                "minio_bucket_init_retry",
                bucket=bucket,
                attempt=attempt,
                attempts=attempts,
                error=type(exc).__name__,
            )
            if attempt < attempts and delay_sec > 0:
                time.sleep(delay_sec)

    if last_exc is None:
        raise RuntimeError("minio_bucket_init_failed: all retries exhausted with no exception")
    raise last_exc


app = FastAPI(title="Mini-Drop Server", version="0.1.0", lifespan=_lifespan)

# CORS 中间件：允许前端跨域开发访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("MINI_DROP_CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# request-id 中间件：为每个 HTTP 请求生成唯一 ID，注入响应头、请求状态和结构化日志
@app.middleware("http")
async def _request_id(request: Request, call_next):
    import uuid
    rid = request.headers.get("x-request-id", uuid.uuid4().hex[:12])
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    return response


@app.middleware("http")
async def _access_log(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        log_event(
            "error",
            "http_request_failed",
            request_id=getattr(request.state, "request_id", ""),
            method=request.method,
            path=request.url.path,
            error=type(exc).__name__,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        raise

    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    log_event(
        "info",
        "http_request",
        request_id=getattr(request.state, "request_id", ""),
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        latency_ms=latency_ms,
    )
    record_http_request(request.method, request.url.path, response.status_code, latency_ms)
    return response


@app.middleware("http")
async def _api_key_auth(request: Request, call_next):
    if _requires_api_auth(request):
        expected = os.getenv("MINI_DROP_API_KEY", "")
        token = _extract_api_token(request)
        if not expected:
            return JSONResponse(
                status_code=500,
                content={"detail": "API auth enabled but MINI_DROP_API_KEY is empty"},
            )
        if not token or not secrets.compare_digest(token, expected):
            return JSONResponse(status_code=401, content={"detail": "无效 API Key"})
    return await call_next(request)


def _task_view(record) -> TaskView:
    """将 TaskRecord 转为前端模型。"""
    return TaskView(
        id=record.id,
        name=record.name,
        agent_id=record.agent_id,
        target_pid=record.target_pid,
        collector_type=record.collector_type,
        sample_rate=record.sample_rate,
        duration_sec=record.duration_sec,
        status=status_value(record.status),
        status_reason=record.status_reason,
        request_params=record.request_params,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def _requires_api_auth(request: Request) -> bool:
    if os.getenv("MINI_DROP_API_AUTH_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    path = request.url.path
    return path.startswith("/api/") and path not in {"/api/healthz", "/api/metrics", "/api/auth/set-cookie", "/api/auth/clear-cookie"}


def _extract_api_token(request: Request) -> str | None:
    # 1. Authorization: Bearer <token> header
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # 2. X-API-Key header
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    # 3. HttpOnly cookie (preferred for browser clients — resists XSS exfiltration)
    cookie = request.cookies.get("mini_drop_api_key")
    if cookie:
        return cookie.strip()
    return None


# ── 通用 ──────────────────────────────────────────────────────


@app.get("/api/events/stream")
async def sse_stream(request: Request, since: str = ""):
    """Server-Sent Events 实时推送。

    客户端通过 EventSource 连接此端点，接收任务状态变更、
    Agent 上下线、诊断完成等实时事件。

    用法：const es = new EventSource('/api/events/stream');
          es.onmessage = (e) => console.log(JSON.parse(e.data));
    """
    from fastapi.responses import StreamingResponse

    async def event_generator():
        queue = BUS.subscribe()
        try:
            # 先发送历史事件（如果客户端提供了 since 时间戳）
            for event in BUS.get_history(since if since else None):
                yield f"event: {event['event']}\ndata: {_json.dumps(event['data'], ensure_ascii=False, default=str)}\n\n"

            # 持续推送新事件
            while True:
                try:
                    event = await asyncio.to_thread(queue.get, True, 30.0)
                    yield f"event: {event['event']}\ndata: {_json.dumps(event['data'], ensure_ascii=False, default=str)}\n\n"
                except _queue.Empty:
                    # 每 30 秒发一个注释行保活
                    yield ":keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            BUS.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 禁用缓冲
        },
    )


@app.get("/api/metrics")
def prometheus_metrics() -> Any:
    """Prometheus 指标端点。

    返回 text/plain 格式的指标数据，可被 Prometheus server 抓取。
    无需鉴权（抓取时 Prometheus 通常不带自定义 header）。
    """
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=REGISTRY.generate(), media_type="text/plain; charset=utf-8")


@app.get("/api/healthz")
def healthz() -> APIResponse:
    """健康检查端点：验证服务自身及关键依赖（数据库、对象存储）的状态。

    Kubernetes liveness/readiness probe 可通过此端点区分：
      - 200 + healthy=true  → 服务完全可用
      - 200 + healthy=false → 服务存活但依赖不可用（readiness 应标记为未就绪）
      - 非 200               → 服务未存活
    """
    checks: dict[str, dict] = {}

    # 数据库连通性检查
    try:
        from sqlalchemy import text as _sa_text
        session = new_session()
        try:
            session.execute(_sa_text("SELECT 1"))
        finally:
            session.close()
        checks["database"] = {"status": "ok"}
    except Exception as exc:
        checks["database"] = {"status": "unavailable", "error": str(exc)[:200]}

    # 对象存储连通性检查
    try:
        store.ensure_bucket(os.getenv("MINIO_BUCKET", "mini-drop"))
        checks["storage"] = {"status": "ok"}
    except Exception as exc:
        checks["storage"] = {"status": "unavailable", "error": str(exc)[:200]}

    all_ok = all(c["status"] == "ok" for c in checks.values())
    return APIResponse(data={
        "service": "mini-drop-server",
        "version": "0.1.0",
        "healthy": all_ok,
        "checks": checks,
    })


@app.get("/api/ai-config")
def ai_config() -> APIResponse:
    """Return safe AI configuration metadata without exposing the API key."""
    settings = get_ai_settings()
    return APIResponse(data={
        "enabled": settings.enabled,
        "provider": settings.provider,
        "base_url": settings.base_url,
        "model": settings.model,
        "source": settings.source,
        "profile_id": settings.profile_id,
        "has_api_key": bool(settings.api_key),
        "features": {
            "nlp": settings.nlp_enabled,
            "rca": settings.rca_enabled,
            "summarize": settings.summarize_enabled,
        },
        "control_api_auth": {
            "enabled": os.getenv("MINI_DROP_API_AUTH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"},
        },
    })


@app.post("/api/ai-config/test")
def test_active_ai_config() -> APIResponse:
    settings = get_ai_settings()
    if not settings.api_key:
        return APIResponse(data={
            "passed": False,
            "http_status": 0,
            "provider": settings.provider,
            "model": settings.model,
            "base_url": settings.base_url,
            "source": settings.source,
            "profile_id": settings.profile_id,
            "duration_ms": 0,
            "content_valid": False,
            "message": "当前 AI Provider 未配置 API Key",
        })
    result = test_openai_compatible_provider(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
    )
    return APIResponse(data={
        **result,
        "provider": settings.provider,
        "base_url": settings.base_url,
        "source": settings.source,
        "profile_id": settings.profile_id,
    })


@app.get("/api/ai-provider-profiles")
def list_ai_provider_profiles() -> APIResponse:
    return APIResponse(data={"items": list_profiles()})


@app.post("/api/ai-provider-profiles")
def create_ai_provider_profile(payload: AIProviderProfileCreate) -> APIResponse:
    return APIResponse(data=create_profile(payload))


@app.patch("/api/ai-provider-profiles/{profile_id}")
def update_ai_provider_profile(profile_id: str, payload: AIProviderProfileUpdate) -> APIResponse:
    item = update_profile(profile_id, payload)
    if item is None:
        raise HTTPException(status_code=404, detail="AI Provider Profile 不存在")
    return APIResponse(data=item)


@app.post("/api/ai-provider-profiles/{profile_id}/activate")
def activate_ai_provider_profile(profile_id: str) -> APIResponse:
    item = activate_profile(profile_id)
    if item is None:
        raise HTTPException(status_code=404, detail="AI Provider Profile 不存在")
    return APIResponse(data=item)


@app.delete("/api/ai-provider-profiles/{profile_id}")
def delete_ai_provider_profile(profile_id: str) -> APIResponse:
    if not delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="AI Provider Profile 不存在")
    return APIResponse(data={"deleted": True, "profile_id": profile_id})


@app.post("/api/ai-provider-profiles/test")
def test_ai_provider_profile(payload: AIProviderProfileCreate) -> APIResponse:
    result = test_openai_compatible_provider(
        base_url=payload.base_url,
        api_key=payload.api_key,
        model=payload.model,
    )
    return APIResponse(data=result)


@app.post("/api/ai-provider-profiles/{profile_id}/test")
def test_saved_ai_provider_profile(profile_id: str) -> APIResponse:
    item = get_profile(profile_id)
    if item is None:
        raise HTTPException(status_code=404, detail="AI Provider Profile 不存在")
    settings = get_ai_settings()
    if settings.profile_id != profile_id:
        return APIResponse(data={
            "passed": False,
            "http_status": 0,
            "model": item.get("model"),
            "duration_ms": 0,
            "content_valid": False,
            "message": "只能测试当前激活的 Profile；请先激活后再测试，避免后端回显或传输已保存密钥。",
        })
    result = test_openai_compatible_provider(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
    )
    return APIResponse(data=result)


@app.post("/api/ai-validation/runs")
def run_ai_validation() -> APIResponse:
    """Run the complete provider + Drop AI validation suite on demand."""
    try:
        result = run_ai_validation_suite()
    except AIValidationBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return APIResponse(data=result)


@app.get("/api/me")
def current_user() -> APIResponse:
    return APIResponse(data={
        "user_id": "demo_user",
        "name": "Mini-Drop Demo User",
        "role": "admin",
    })


@app.post("/api/auth/set-cookie")
def auth_set_cookie(request: Request, body: dict) -> APIResponse:
    """通过 HttpOnly cookie 设置 API Key（比 localStorage 更安全）。

    POST /api/auth/set-cookie
    {"api_key": "sk-..."}

    浏览器将自动在后续请求中携带该 cookie，
    JavaScript 无法通过 document.cookie 读取（HttpOnly）。
    """
    from fastapi.responses import JSONResponse as _JsonResp
    api_key = (body or {}).get("api_key", "").strip()
    if not api_key:
        return APIResponse(code=400, message="api_key 不能为空")
    resp = _JsonResp(content={"code": 0, "message": "ok", "data": None})
    resp.set_cookie(
        key="mini_drop_api_key",
        value=api_key,
        httponly=True,
        samesite="lax",
        secure=False,  # 开发环境 HTTP；生产环境应设为 True 配合 HTTPS
        max_age=7 * 24 * 3600,  # 7 天
        path="/api",
    )
    return resp


@app.post("/api/auth/clear-cookie")
def auth_clear_cookie() -> APIResponse:
    """清除 HttpOnly cookie。"""
    from fastapi.responses import JSONResponse as _JsonResp
    resp = _JsonResp(content={"code": 0, "message": "ok", "data": None})
    resp.delete_cookie(key="mini_drop_api_key", path="/api")
    return resp


# ── Agent（查询面） ────────────────────────────────────────────


@app.get("/api/agents")
def list_agents(
    limit: int = 1000,
    offset: int = 0,
) -> APIResponse:
    """返回 Agent 列表。支持分页。

    调用前自动检查离线。可通过 ?limit=50&offset=0 分页。
    """
    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)
    repo.mark_offline_agents()
    all_items = []
    for agent in repo.agents.values():
        item = repo.as_dict(agent)
        item["latest_metrics"] = getattr(repo, "agent_metrics", {}).get(agent.id, {})
        item["collector_profile"] = item["latest_metrics"].get("collector_profile", {})
        all_items.append(item)
    total = len(all_items)
    page = all_items[offset:offset + limit] if offset < total else []
    return APIResponse(data={"items": page, "total": total, "offset": offset, "limit": limit})


@app.get("/api/audit-logs")
def list_audit_logs(
    limit: int = 1000,
    offset: int = 0,
) -> APIResponse:
    """返回审计日志列表。支持分页。"""
    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)
    all_items = [repo.as_dict(log) for log in repo.audit_logs]
    total = len(all_items)
    page = all_items[offset:offset + limit] if offset < total else []
    return APIResponse(data={"items": page, "total": total, "offset": offset, "limit": limit})


# ── 任务 ──────────────────────────────────────────────────────


@app.post("/api/tasks")
def create_task(payload: CreateTaskRequest) -> APIResponse:
    if payload.target_pid <= 0:
        raise HTTPException(status_code=400, detail="target_pid 必须为正整数")
    if payload.target_pid > 4194304:  # Linux pid_max 上限
        raise HTTPException(status_code=400, detail=f"target_pid 超出有效范围: {payload.target_pid}")
    if payload.duration_sec <= 0:
        raise HTTPException(status_code=400, detail="duration_sec 必须为正整数")
    if payload.duration_sec > MAX_TASK_DURATION_SEC:
        raise HTTPException(status_code=400, detail=f"duration_sec 不能超过 {MAX_TASK_DURATION_SEC}")
    if payload.sample_rate <= 0:
        raise HTTPException(status_code=400, detail="sample_rate 必须为正整数")
    if payload.sample_rate > MAX_SAMPLE_RATE:
        raise HTTPException(status_code=400, detail=f"sample_rate 不能超过 {MAX_SAMPLE_RATE}")
    try:
        task = repo.create_task(payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return APIResponse(data={"task_id": task.id, "status": status_value(task.status)})


@app.get("/api/tasks")
def list_tasks(
    limit: int = 1000,
    offset: int = 0,
    search: str = "",
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> APIResponse:
    """返回任务列表。支持分页、搜索、排序。

    可通过 ?limit=50&offset=0&search=perf&sort_by=name&sort_order=asc 过滤。
    """
    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)

    all_items = [_task_view(t).model_dump() for t in repo.tasks.values()]

    # 搜索：按任务名称模糊匹配
    if search:
        q = search.lower()
        all_items = [t for t in all_items if q in (t.get("name") or "").lower() or q in (t.get("id") or "").lower()]

    # 排序
    sort_keys = {"name", "status", "created_at", "agent_id", "collector_type", "target_pid"}
    by = sort_by if sort_by in sort_keys else "created_at"
    reverse = sort_order.lower() == "desc"
    all_items.sort(key=lambda x: x.get(by, "") or "", reverse=reverse)

    total = len(all_items)
    page = all_items[offset:offset + limit] if offset < total else []
    return APIResponse(data={"items": page, "total": total, "offset": offset, "limit": limit})


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> APIResponse:
    task = repo.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    data = _task_view(task).model_dump()
    if status_value(task.status) == "DONE":
        data["latest_analysis"] = _ensure_task_analysis_session(task_id)
    else:
        data["latest_analysis"] = _latest_task_analysis(task_id)
    return APIResponse(data=data)


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str) -> APIResponse:
    """删除任务及其关联的事件、产物和诊断结果。"""
    task = repo.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 终态保护：RUNNING/ANALYZING 不允许删除
    active_statuses = {"PENDING", "RUNNING", "UPLOADING", "ANALYZING"}
    if status_value(task.status) in active_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"任务状态为 {status_value(task.status)}，请等待任务完成或失败后再删除",
        )
    deleted = repo.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="任务不存在")
    return APIResponse(data={"task_id": task_id, "deleted": True})


@app.get("/api/tasks/{task_id}/events")
def get_task_events(task_id: str) -> APIResponse:
    if task_id not in repo.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    items = [repo.as_dict(e) for e in repo.events if e.task_id == task_id]
    return APIResponse(data=items)


@app.get("/api/tasks/{task_id}/artifacts")
def get_task_artifacts(task_id: str) -> APIResponse:
    if task_id not in repo.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    return APIResponse(data=repo.artifacts.get(task_id, []))


@app.get("/api/tasks/{task_id}/artifacts/{artifact_type}/content")
def get_task_artifact_content(task_id: str, artifact_type: str, index: Optional[int] = None) -> APIResponse:
    if task_id not in repo.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    for artifact in repo.artifacts.get(task_id, []):
        if artifact.get("artifact_type") != artifact_type:
            continue
        if index is not None and artifact.get("metadata", {}).get("window_index") != index:
            continue
        local_path = artifact.get("local_path")
        path = _resolve_artifact_path_or_none(local_path)
        if path is None and artifact.get("object_key"):
            text = _read_artifact_object_text(artifact)
            if artifact_type.endswith("_json") or artifact.get("content_type") == "application/json":
                return APIResponse(data=_json_mod.loads(text))
            return APIResponse(data={"text": text})
        if path is None:
            raise HTTPException(status_code=404, detail="本地产物不存在")
        if artifact_type.endswith("_json") or artifact.get("content_type") == "application/json":
            return APIResponse(data=_json_mod.loads(path.read_text(encoding="utf-8")))
        return APIResponse(data={"text": path.read_text(encoding="utf-8", errors="replace")})
    raise HTTPException(status_code=404, detail="产物不存在")


@app.get("/api/tasks/{task_id}/artifacts/{artifact_type}/download")
def download_task_artifact(task_id: str, artifact_type: str, index: Optional[int] = None):
    """经 Server 流式下载产物，使浏览器无需直接访问 MinIO 9000 端口。"""
    if task_id not in repo.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    for artifact in repo.artifacts.get(task_id, []):
        if artifact.get("artifact_type") != artifact_type:
            continue
        if index is not None and artifact.get("metadata", {}).get("window_index") != index:
            continue

        filename = _safe_download_filename(
            artifact.get("filename") or artifact.get("object_key") or f"{artifact_type}.bin"
        )
        media_type = artifact.get("content_type") or "application/octet-stream"
        path = _resolve_artifact_path_or_none(artifact.get("local_path"))
        if path is not None:
            return FileResponse(path, media_type=media_type, filename=filename)

        bucket = artifact.get("bucket") or os.getenv("MINIO_BUCKET", "mini-drop")
        key = _validate_presign_request(bucket, artifact.get("object_key", ""))
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{_url_quote(filename)}",
            "X-Content-Type-Options": "nosniff",
        }
        return StreamingResponse(
            store.stream_object(bucket, key),
            media_type=media_type,
            headers=headers,
        )
    raise HTTPException(status_code=404, detail="产物不存在")


@app.get("/api/storage/presign")
def presign_url(bucket: str = "mini-drop", key: str = "", expires: int = 3600) -> APIResponse:
    """生成 MinIO 预签名下载 URL。"""
    key = _validate_presign_request(bucket, key)
    try:
        url = store.presigned_get_url(bucket, key, expires)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return APIResponse(data={"url": url, "expires_sec": expires})


def _latest_task_analysis(task_id: str) -> dict[str, Any] | None:
    runs = repo.list_diagnoses_for_task(task_id)
    if not runs:
        return None
    latest = repo.get_diagnosis(runs[0]["id"])
    if latest is None:
        return None
    run = latest["run"]
    report = latest.get("report") or {}
    report_json = report.get("report") or {}
    return {
        "analysis_session_id": run["id"],
        "diagnosis_id": run["id"],
        "task_id": run["task_id"],
        "status": run["status"],
        "summary": run.get("summary", ""),
        "validated": run.get("validated", False),
        "analysis_pipeline": report_json.get("analysis_pipeline"),
        "analysis_strategy": report_json.get("analysis_strategy"),
        "not_enough_evidence": report.get("not_enough_evidence"),
        "report": report_json,
        "ranked_causes": report.get("ranked_causes", []),
    }


def _ensure_task_analysis_session(task_id: str) -> dict[str, Any] | None:
    latest = _latest_task_analysis(task_id)
    if latest is not None:
        return latest
    task = repo.tasks.get(task_id)
    if task is not None and _is_diagnosis_child_task(task):
        return _collection_only_task_analysis(task)
    return _run_task_analysis(
        task_id=task_id,
        selected_strategy=normalize_strategy_id(os.getenv("MINI_DROP_RCA_STRATEGY", "linear")),
        selected_pipeline=normalize_pipeline_id(os.getenv("MINI_DROP_RCA_PIPELINE", "evidence_to_attribution")),
        collection_mode="manual_single",
    )


def _run_task_analysis(
    *,
    task_id: str,
    selected_strategy: str,
    selected_pipeline: str,
    collection_mode: str = "manual_single",
) -> dict[str, Any]:
    task = repo.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if _is_diagnosis_child_task(task):
        return _collection_only_task_analysis(task)

    artifacts = repo.artifacts.get(task_id, [])
    artifact_values = {
        "top_json": _extract_artifact_json(artifacts, "top_json"),
        "flamegraph_json": _extract_artifact_json(artifacts, "flamegraph_json"),
        "flamegraph_svg": _extract_artifact_text(artifacts, "flamegraph_svg"),
        "ebpf_metrics": _extract_artifact_json(artifacts, "ebpf_metrics"),
        "sys_metrics": _extract_artifact_json(artifacts, "sys_metrics"),
        "memory_json": _extract_artifact_json(artifacts, "memory_json"),
        "depth_evidence_json": _extract_artifact_json(artifacts, "depth_evidence_json"),
        "continuous_top_json": _extract_artifact_json(artifacts, "continuous_top_json"),
        "continuous_flamegraph_json": _extract_artifact_json(artifacts, "continuous_flamegraph_json"),
        "continuous_summary": _extract_artifact_json(artifacts, "continuous_summary"),
        "log_window_json": _extract_artifact_json(artifacts, "log_window_json"),
        "dependency_check_json": _extract_artifact_json(artifacts, "dependency_check_json"),
        "redis_check_json": _extract_artifact_json(artifacts, "redis_check_json"),
    }
    artifact_values = _normalize_analysis_artifact_values(artifact_values)
    structured_evidence = structure_artifact_evidence(
        task_id=task_id,
        artifacts=artifacts,
        artifact_values=artifact_values,
        evidence_window={"collection_mode": collection_mode, "timing_relation": "unknown"},
    )
    rca_inputs = rca_inputs_from_structured(structured_evidence)

    task_events = [repo.as_dict(e) for e in repo.events if e.task_id == task_id]
    agent_record = repo.agents.get(task.agent_id)
    model_name = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    diagnosis_id = repo.create_diagnosis_run(task_id, model_name)

    outcome = run_diagnosis_context(
        task_id=task_id,
        task_record=task,
        top_functions=rca_inputs["top_functions"],
        ebpf_metrics=rca_inputs["ebpf_metrics"],
        sys_metrics=rca_inputs["sys_metrics"],
        evidence_index=rca_inputs["evidence_index"],
        failure_events=[event.get("reason", "") for event in task_events if event.get("reason")],
        feedback_priors=repo.get_feedback_priors(),
        task_events=task_events,
        agent_record=agent_record,
        repo=repo,
        analysis_strategy=selected_strategy,
        analysis_pipeline=selected_pipeline,
        structured_evidence=structured_evidence.model_dump(mode="json"),
    )
    report = outcome.report
    ranked_causes = [c.model_dump() for c in report.report.ranked_causes]
    confidence = ranked_causes[0]["confidence"] if ranked_causes else 0.0

    for tool_result in outcome.tool_results:
        repo.add_diagnosis_tool_result(
            diagnosis_id=diagnosis_id,
            tool_name=tool_result.tool_name,
            status=tool_result.status,
            evidence_ref=tool_result.evidence_ref,
            input_json=tool_result.input,
            output_json=tool_result.output,
            error_message=tool_result.error_message,
        )

    report_id = repo.add_diagnosis_report(
        diagnosis_id=diagnosis_id,
        report_json=report.report.model_dump(),
        ranked_causes=ranked_causes,
        confidence=confidence,
        not_enough_evidence=report.report.not_enough_evidence,
    )

    repair_plan_data = None
    if outcome.repair_plan is not None:
        repair_plan_data = outcome.repair_plan.model_dump()
        repo.add_repair_plan(
            diagnosis_id=diagnosis_id,
            plan_id=outcome.repair_plan.plan_id,
            cause_id=outcome.repair_plan.cause_id,
            risk_level=outcome.repair_plan.risk_level,
            actions=[action.model_dump() for action in outcome.repair_plan.actions],
            executed_actions=[
                action.model_dump() for action in outcome.repair_plan.actions
                if action.status == "executed"
            ],
            requires_user_confirm=outcome.repair_plan.requires_user_confirm,
            status=outcome.repair_plan.status,
        )

    diag_status = "DONE" if report.validated else "FAILED"
    repo.finish_diagnosis_run(
        diagnosis_id=diagnosis_id,
        status=diag_status,
        summary=report.report.summary,
        validated=report.validated,
        retry_count=report.retry_count,
    )
    record_diagnosis(diag_status)
    notify_diagnosis_complete(task_id, diagnosis_id, diag_status)

    return {
        "analysis_session_id": diagnosis_id,
        "diagnosis_id": diagnosis_id,
        "report_id": report_id,
        "task_id": task_id,
        "analysis_strategy": selected_strategy,
        "analysis_pipeline": selected_pipeline,
        "collection_mode": collection_mode,
        "model": report.model_name,
        "validated": report.validated,
        "summary": report.report.summary,
        "report": report.report.model_dump(),
        "ranked_causes": ranked_causes,
        "facts": report.report.facts,
        "not_enough_evidence": report.report.not_enough_evidence,
        "tool_results": [item.model_dump() for item in outcome.tool_results],
        "structured_evidence": report.report.structured_evidence,
        "repair_plan": repair_plan_data,
    }


def _is_diagnosis_child_task(task: Any) -> bool:
    request_params = getattr(task, "request_params", {}) or {}
    options = request_params.get("options", {}) if isinstance(request_params, dict) else {}
    return isinstance(options, dict) and bool(options.get("diagnosis_id"))


def _collection_only_task_analysis(task: Any) -> dict[str, Any]:
    request_params = getattr(task, "request_params", {}) or {}
    options = request_params.get("options", {}) if isinstance(request_params, dict) else {}
    diagnosis_id = str(options.get("diagnosis_id") or "")
    step_id = str(options.get("diagnosis_step_id") or "")
    return {
        "analysis_session_id": None,
        "diagnosis_id": diagnosis_id,
        "task_id": task.id,
        "status": "SKIPPED",
        "summary": "该任务是 AI 诊断会话的采集子任务，只产出结构化证据；最终结论请查看父级 AI 诊断会话。",
        "validated": True,
        "analysis_strategy": "collection_only",
        "analysis_pipeline": "diagnosis_session_child_task",
        "collection_mode": options.get("collection_mode") or "diagnosis_probe",
        "not_enough_evidence": False,
        "report": {
            "mode": "collection_only",
            "parent_diagnosis_id": diagnosis_id,
            "diagnosis_step_id": step_id,
            "collector_type": getattr(task, "collector_type", ""),
            "artifact_count": len(repo.artifacts.get(task.id, [])),
        },
        "ranked_causes": [],
        "facts": [],
        "tool_results": [],
        "repair_plan": None,
    }


def _run_watch_incident_analysis(
    *,
    incident_id: str,
    analysis_strategy: Optional[str] = None,
    analysis_pipeline: Optional[str] = None,
) -> dict[str, Any]:
    incident = watch_registry.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="异常窗口不存在")
    watch = watch_registry.get(incident.watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="监视订阅不存在")
    try:
        selected_strategy = normalize_strategy_id(
            analysis_strategy or os.getenv("MINI_DROP_RCA_STRATEGY", "linear")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        selected_pipeline = normalize_pipeline_id(
            analysis_pipeline or os.getenv("MINI_DROP_RCA_PIPELINE", "evidence_to_attribution")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    structured = StructuredEvidence.model_validate(incident.structured_evidence)
    rca_inputs = rca_inputs_from_structured(structured)
    task_id = f"watch_incident:{incident.incident_id}"
    task_record = SimpleNamespace(
        id=task_id,
        name=f"Watch incident analysis {incident.incident_id}",
        agent_id=watch.target.agent_id,
        target_pid=watch.target.target_pid,
        collector_type="rolling_snapshot",
        sample_rate=0,
        duration_sec=0,
        status="DONE",
        status_reason="Frozen watch incident same-window snapshot",
        request_params={
            "watch_id": watch.watch_id,
            "incident_id": incident.incident_id,
            "trigger_event_id": incident.trigger_event_id,
            "evidence_cohort_id": incident.evidence_cohort_id,
            "collection_mode": "rolling_snapshot",
            "timing_relation": "same_window",
        },
    )
    task_events = [
        {
            "task_id": task_id,
            "to_status": "DONE",
            "reason": "WatchIncident 冻结现场进入 AI 树分析",
            "metadata": task_record.request_params,
        }
    ]
    agent_record = repo.agents.get(watch.target.agent_id)
    model_name = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    outcome = run_diagnosis_context(
        task_id=task_id,
        task_record=task_record,
        top_functions=rca_inputs["top_functions"],
        ebpf_metrics=rca_inputs["ebpf_metrics"],
        sys_metrics=rca_inputs["sys_metrics"],
        evidence_index=rca_inputs["evidence_index"],
        failure_events=[],
        feedback_priors=repo.get_feedback_priors(),
        task_events=task_events,
        agent_record=agent_record,
        repo=repo,
        auto_execute_safe=False,
        analysis_strategy=selected_strategy,
        analysis_pipeline=selected_pipeline,
        structured_evidence=structured.model_dump(mode="json"),
    )
    report = outcome.report
    ranked_causes = [cause.model_dump() for cause in report.report.ranked_causes]
    ai_tree = [item.model_dump(mode="json") for item in report.report.ai_tree]
    next_evidence_requests = list(dict.fromkeys(
        request
        for item in ai_tree
        for request in item.get("next_evidence_requests", [])
        if request
    ))
    analysis_result = report.report.analysis_result
    conclusion_boundary = (
        report.report.conclusion_boundary.model_dump(mode="json")
        if report.report.conclusion_boundary is not None
        else None
    )
    analysis_session_id = f"analysis_{incident.incident_id}"
    return {
        "analysis_session_id": analysis_session_id,
        "incident_id": incident.incident_id,
        "watch_id": watch.watch_id,
        "trigger_event_id": incident.trigger_event_id,
        "evidence_cohort_id": incident.evidence_cohort_id,
        "analysis_strategy": selected_strategy,
        "analysis_pipeline": selected_pipeline,
        "collection_mode": "rolling_snapshot",
        "timing_relation": "same_window",
        "model": report.model_name,
        "validated": report.validated,
        "summary": report.report.summary,
        "report": report.report.model_dump(),
        "ranked_causes": ranked_causes,
        "facts": report.report.facts,
        "not_enough_evidence": report.report.not_enough_evidence,
        "ai_tree": ai_tree,
        "next_evidence_requests": next_evidence_requests,
        "missing_evidence": list(report.report.missing_evidence),
        "collection_gaps": list(report.report.collection_gaps),
        "blocked_upgrades": list(report.report.blocked_upgrades),
        "conclusion_boundary": conclusion_boundary,
        "analysis_result": (
            analysis_result.model_dump(mode="json")
            if analysis_result is not None
            else None
        ),
        "tool_results": [item.model_dump() for item in outcome.tool_results],
        "structured_evidence": report.report.structured_evidence,
        "repair_plan": outcome.repair_plan.model_dump() if outcome.repair_plan is not None else None,
    }


@app.post("/api/tasks/{task_id}/diagnose")
def diagnose_task(
    task_id: str,
    analysis_strategy: Optional[str] = None,
    analysis_pipeline: Optional[str] = None,
) -> APIResponse:
    if task_id not in repo.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        selected_strategy = normalize_strategy_id(
            analysis_strategy or os.getenv("MINI_DROP_RCA_STRATEGY", "linear")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        selected_pipeline = normalize_pipeline_id(
            analysis_pipeline or os.getenv("MINI_DROP_RCA_PIPELINE", "evidence_to_attribution")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return APIResponse(data=_run_task_analysis(
        task_id=task_id,
        selected_strategy=selected_strategy,
        selected_pipeline=selected_pipeline,
        collection_mode="manual_single",
    ))


@app.get("/api/tasks/{task_id}/analysis-session")
def get_task_analysis_session(task_id: str) -> APIResponse:
    task = repo.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if status_value(task.status) != "DONE":
        return APIResponse(data=_latest_task_analysis(task_id))
    return APIResponse(data=_ensure_task_analysis_session(task_id))


@app.get("/api/tasks/{task_id}/diagnoses")
def list_task_diagnoses(task_id: str) -> APIResponse:
    if task_id not in repo.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    return APIResponse(data=repo.list_diagnoses_for_task(task_id))


@app.get("/api/diagnoses/{diagnosis_id}")
def get_diagnosis(diagnosis_id: str) -> APIResponse:
    item = repo.get_diagnosis(diagnosis_id)
    if item is None:
        raise HTTPException(status_code=404, detail="诊断不存在")
    return APIResponse(data=item)


@app.post("/api/diagnoses/{diagnosis_id}/feedback")
def submit_diagnosis_feedback(diagnosis_id: str, payload: RCAFeedbackRequest) -> APIResponse:
    item = repo.get_diagnosis(diagnosis_id)
    if item is None:
        raise HTTPException(status_code=404, detail="诊断不存在")
    task_id = item["run"]["task_id"]
    repo.record_rca_feedback(
        diagnosis_id=diagnosis_id,
        task_id=task_id,
        predicted_cause_id=payload.predicted_cause_id,
        feedback_label=payload.feedback_label,
        corrected_cause_id=payload.corrected_cause_id,
        feedback_note=payload.feedback_note,
    )
    return APIResponse(data={"diagnosis_id": diagnosis_id, "feedback_saved": True})


# ── AI 集群诊断会话（v1）──────────────────────────────────────


@app.post("/api/v1/diagnoses")
def create_diagnosis_session(payload: CreateDiagnosisRequest) -> APIResponse:
    """创建独立诊断会话，并只编排注册表中的受控探针。"""
    try:
        data = diagnosis_orchestrator.create(payload, creator_id="demo_user")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return APIResponse(data=data)


@app.get("/api/v1/diagnoses")
def list_diagnosis_sessions(limit: int = 100, offset: int = 0) -> APIResponse:
    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)
    items = diagnosis_orchestrator.list(limit=limit, offset=offset)
    return APIResponse(data=json_safe({
        "items": items,
        "total": diagnosis_orchestrator.store.count_sessions(),
        "offset": offset,
        "limit": limit,
    }))


@app.get("/api/v1/diagnoses/{diagnosis_id}")
def get_diagnosis_session(diagnosis_id: str) -> APIResponse:
    data = diagnosis_orchestrator.get(diagnosis_id, advance=True)
    if data is None:
        raise HTTPException(status_code=404, detail="诊断会话不存在")
    return APIResponse(data=json_safe(data))


@app.delete("/api/v1/diagnoses/{diagnosis_id}")
def delete_diagnosis_session(diagnosis_id: str) -> APIResponse:
    data = diagnosis_orchestrator.store.get_session(diagnosis_id)
    if data is None:
        raise HTTPException(status_code=404, detail="诊断会话不存在")
    if data.get("status") not in TERMINAL_DIAGNOSIS_STATUSES:
        raise HTTPException(status_code=400, detail="诊断仍在运行，请等待终态后再删除")
    if not diagnosis_orchestrator.store.delete_session(diagnosis_id):
        raise HTTPException(status_code=404, detail="诊断会话不存在")
    return APIResponse(data={"diagnosis_id": diagnosis_id, "deleted": True})


@app.get("/api/v1/diagnoses/{diagnosis_id}/audit-bundle")
def get_diagnosis_audit_bundle(diagnosis_id: str) -> APIResponse:
    data = build_audit_bundle(diagnosis_id, diagnosis_orchestrator, repo)
    if data is None:
        raise HTTPException(status_code=404, detail="诊断会话不存在")
    return APIResponse(data=data)


@app.post("/api/v1/diagnoses/{diagnosis_id}/approvals")
def approve_diagnosis_probe(diagnosis_id: str, payload: ApprovalRequest) -> APIResponse:
    try:
        data = diagnosis_orchestrator.approve(diagnosis_id, payload)
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "不存在" in message else 409
        raise HTTPException(status_code=status_code, detail=message) from exc
    return APIResponse(data=data)


@app.post("/api/v1/diagnoses/{diagnosis_id}/approvals/bulk")
def approve_waiting_diagnosis_probes(diagnosis_id: str, payload: BulkApprovalRequest) -> APIResponse:
    try:
        data = diagnosis_orchestrator.approve_waiting(diagnosis_id, payload)
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "不存在" in message else 409
        raise HTTPException(status_code=status_code, detail=message) from exc
    return APIResponse(data=data)


@app.get("/api/v1/probes")
def list_probe_definitions() -> APIResponse:
    return APIResponse(data=[probe.model_dump(mode="json") for probe in list_registered_probes()])


# ── Persistent Agent watch subscriptions ─────────────────────


@app.post("/api/v1/watches")
def create_watch_subscription(payload: CreateWatchSubscriptionRequest) -> APIResponse:
    if payload.target.agent_id not in repo.agents:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    watch = watch_registry.create(payload)
    return APIResponse(data=watch.model_dump(mode="json"))


@app.get("/api/v1/watches")
def list_watch_subscriptions(
    agent_id: str = "",
    include_disabled: bool = False,
) -> APIResponse:
    items = watch_registry.list(
        agent_id=agent_id or None,
        include_disabled=include_disabled,
    )
    return APIResponse(data={"items": [item.model_dump(mode="json") for item in items]})


@app.get("/api/v1/watches/{watch_id}")
def get_watch_subscription(watch_id: str) -> APIResponse:
    watch = watch_registry.get(watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="监视订阅不存在")
    data = watch.model_dump(mode="json")
    data["incidents"] = [item.model_dump(mode="json") for item in watch_registry.list_incidents(watch_id)]
    return APIResponse(data=data)


@app.delete("/api/v1/watches/{watch_id}")
def disable_watch_subscription(watch_id: str) -> APIResponse:
    watch = watch_registry.disable(watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="监视订阅不存在")
    return APIResponse(data=watch.model_dump(mode="json"))


@app.get("/api/v1/watches/{watch_id}/incidents")
def list_watch_incidents(watch_id: str) -> APIResponse:
    if watch_registry.get(watch_id) is None:
        raise HTTPException(status_code=404, detail="监视订阅不存在")
    items = watch_registry.list_incidents(watch_id)
    return APIResponse(data={"items": [item.model_dump(mode="json") for item in items]})


@app.post("/api/v1/watch-incidents/{incident_id}/analyze")
def analyze_watch_incident(
    incident_id: str,
    analysis_strategy: Optional[str] = None,
    analysis_pipeline: Optional[str] = None,
) -> APIResponse:
    _ = analysis_strategy, analysis_pipeline
    return APIResponse(data=_create_watch_incident_diagnosis(incident_id))


@app.get("/api/v1/agents/{agent_id}/watch-leases")
def list_agent_watch_leases(agent_id: str) -> APIResponse:
    if agent_id not in repo.agents:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    leases = watch_runtime.list_leases(agent_id)
    return APIResponse(data={"items": [lease.model_dump(mode="json") for lease in leases]})


@app.post("/api/v1/agents/{agent_id}/process-inventory/refresh")
def refresh_agent_process_inventory(agent_id: str) -> APIResponse:
    agent = repo.agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if status_value(agent.status) != "ONLINE":
        raise HTTPException(status_code=409, detail="Agent 不在线，无法刷新进程清单")
    capabilities = set(getattr(agent, "capabilities", []) or [])
    if "process_inventory" not in capabilities:
        raise HTTPException(status_code=409, detail="Agent 未注册 process_inventory 能力")
    task = repo.create_task(CreateTaskRequest(
        name=f"刷新进程清单:{agent_id}",
        agent_id=agent_id,
        target_pid=1,
        collector_type="process_inventory",
        sample_rate=1,
        duration_sec=1,
        options={"source": "watch_target_picker"},
    ))
    return APIResponse(data={"task_id": task.id, "status": status_value(task.status)})


@app.get("/api/v1/agents/{agent_id}/processes")
def list_agent_processes(agent_id: str, query: str = "", limit: int = 100) -> APIResponse:
    if agent_id not in repo.agents:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    inventory = _latest_process_inventory(agent_id)
    if inventory is None:
        return APIResponse(data={
            "items": [],
            "total": 0,
            "inventory_status": "missing",
            "message": "未找到已完成的进程清单，请先刷新",
        })
    processes = inventory.get("processes") if isinstance(inventory.get("processes"), list) else []
    needle = query.strip().lower()
    if needle:
        processes = [
            item for item in processes
            if needle in _process_search_text(item)
        ]
    processes = sorted(
        processes,
        key=lambda item: (-(float(item.get("cpu_percent") or 0.0)), -(float(item.get("rss_mb") or 0.0)), int(item.get("pid") or 0)),
    )
    limit = min(max(limit, 1), 500)
    return APIResponse(data={
        "items": processes[:limit],
        "total": len(processes),
        "inventory_status": "ready",
        "collected_at": inventory.get("collected_at"),
        "summary": inventory.get("summary") or {},
    })


@app.post("/api/v1/watches/{watch_id}/evaluate")
def evaluate_watch_subscription(watch_id: str, payload: WatchEvaluationRequest) -> APIResponse:
    try:
        result = watch_runtime.evaluate(watch_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="监视订阅不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return APIResponse(data=result.model_dump(mode="json"))


def _extract_artifact_json(artifacts: list[dict], artifact_type: str) -> dict | None:
    """从 artifacts 列表中提取指定类型的 JSON 数据。"""
    for art in artifacts:
        if art.get("artifact_type") == artifact_type:
            local_path = art.get("local_path", "")
            try:
                path = _resolve_artifact_path_or_none(local_path)
                if path is not None:
                    return _json_mod.loads(path.read_text(encoding="utf-8"))
                if art.get("object_key"):
                    return _json_mod.loads(_read_artifact_object_text(art))
            except HTTPException as exc:
                log_event(
                    "warning",
                    "artifact_json_unavailable",
                    artifact_type=artifact_type,
                    local_path=local_path,
                    status_code=exc.status_code,
                )
                return None
            except Exception as exc:
                log_event(
                    "warning",
                    "artifact_json_parse_failed",
                    artifact_type=artifact_type,
                    local_path=local_path,
                    error=type(exc).__name__,
                )
                return None
    return None


def _extract_artifact_text(artifacts: list[dict], artifact_type: str) -> str | None:
    """从 artifacts 列表中提取指定类型的文本数据。"""
    for art in artifacts:
        if art.get("artifact_type") != artifact_type:
            continue
        try:
            local_path = art.get("local_path", "")
            path = _resolve_artifact_path_or_none(local_path)
            if path is not None:
                if path.stat().st_size > 2 * 1024 * 1024:
                    return None
                return path.read_text(encoding="utf-8", errors="replace")
            if art.get("object_key"):
                text = _read_artifact_object_text(art)
                if len(text) <= 2 * 1024 * 1024:
                    return text
        except HTTPException as exc:
            log_event(
                "warning",
                "artifact_text_unavailable",
                artifact_type=artifact_type,
                local_path=art.get("local_path", ""),
                status_code=exc.status_code,
            )
            return None
        except Exception as exc:
            log_event(
                "warning",
                "artifact_text_parse_failed",
                artifact_type=artifact_type,
                local_path=art.get("local_path", ""),
                error=type(exc).__name__,
            )
            return None
    return None


def _latest_process_inventory(agent_id: str) -> dict[str, Any] | None:
    tasks = [
        task for task in repo.tasks.values()
        if task.agent_id == agent_id
        and task.collector_type == "process_inventory"
        and status_value(task.status) == "DONE"
    ]
    tasks.sort(key=lambda item: item.finished_at or item.created_at, reverse=True)
    for task in tasks:
        data = _extract_artifact_json(repo.artifacts.get(task.id, []), "process_inventory_json")
        if isinstance(data, dict):
            return data
    return None


def _process_search_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key) or "")
        for key in ("pid", "comm", "cmdline", "user", "service_guess", "instance_guess")
    ).lower()


def _artifact_root() -> _Path:
    return _Path(os.getenv("MINI_DROP_ARTIFACT_ROOT", "/tmp/mini-drop")).expanduser().resolve()


def _resolve_artifact_path(local_path: str | None) -> _Path:
    if not local_path:
        raise HTTPException(status_code=404, detail="本地产物不存在")

    root = _artifact_root()
    candidate = _Path(local_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()

    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=403, detail="产物路径不在允许目录内")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="本地产物不存在")
    return resolved


def _resolve_artifact_path_or_none(local_path: str | None) -> _Path | None:
    try:
        return _resolve_artifact_path(local_path)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


def _read_artifact_object_text(artifact: dict) -> str:
    bucket = artifact.get("bucket") or os.getenv("MINIO_BUCKET", "mini-drop")
    key = _validate_presign_request(bucket, artifact.get("object_key", ""))
    try:
        return store.read_object_bytes(bucket, key).decode("utf-8", errors="replace")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_event("warning", "artifact_object_read_failed", bucket=bucket, object_key=key, error=type(exc).__name__)
        raise HTTPException(status_code=404, detail="对象存储产物不存在") from exc


def _normalize_analysis_artifact_values(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    if normalized.get("top_json") is None and normalized.get("continuous_top_json") is not None:
        normalized["top_json"] = normalized["continuous_top_json"]
    if normalized.get("flamegraph_json") is None and normalized.get("continuous_flamegraph_json") is not None:
        normalized["flamegraph_json"] = normalized["continuous_flamegraph_json"]
    summary = normalized.get("continuous_summary")
    if isinstance(summary, dict):
        depth = normalized.get("depth_evidence_json") if isinstance(normalized.get("depth_evidence_json"), dict) else {}
        depth.setdefault("baseline_summary", summary)
        normalized["depth_evidence_json"] = depth
    return normalized


def _validate_presign_request(bucket: str, key: str) -> str:
    allowed_bucket = os.getenv("MINIO_BUCKET", "mini-drop")
    if bucket != allowed_bucket:
        raise HTTPException(status_code=403, detail="bucket 不在允许范围内")
    if not key:
        raise HTTPException(status_code=400, detail="key 参数不能为空")
    normalized = key.replace("\\", "/")
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise HTTPException(status_code=400, detail="key 路径不合法")
    if not normalized.startswith("tasks/"):
        raise HTTPException(status_code=403, detail="key 不在任务产物目录内")
    return normalized


def _safe_download_filename(value: str) -> str:
    filename = _Path(value.replace("\\", "/")).name
    filename = "".join(ch for ch in filename if ch >= " " and ch not in {'"', ';'})
    return filename[:255] or "artifact.bin"


# ── NLP 自然语言采集 ────────────────────────────────────────────


@app.post("/api/nlp/parse")
def nlp_parse_intent(body: dict) -> APIResponse:
    """将用户自然语言描述解析为结构化任务参数。"""
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")
    if len(query) > 500:
        raise HTTPException(status_code=400, detail="query 不能超过 500 字符")

    intent = parse_intent(query)
    candidates = resolve_pid(intent.process_name)

    return APIResponse(data={
        "process_name": intent.process_name,
        "collector_type": intent.collector_type,
        "duration_sec": intent.duration_sec,
        "sample_rate": intent.sample_rate,
        "reasoning": intent.reasoning,
        "candidate_pids": [c.to_dict() for c in candidates],
    })


@app.post("/api/nlp/summarize")
def nlp_summarize_task(body: dict) -> APIResponse:
    """对已完成任务的结果进行 AI 总结并生成追问建议。"""
    task_id = body.get("task_id", "")
    if not task_id:
        raise HTTPException(status_code=400, detail="task_id 不能为空")

    task = repo.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    artifacts = repo.artifacts.get(task_id, [])
    top_functions = _extract_artifact_json(artifacts, "top_json") or []
    ebpf_metrics = _extract_artifact_json(artifacts, "ebpf_metrics")
    suggestions = []

    # 从 top_functions 中提取提示
    for func in top_functions[:5]:
        name = func.get("name", "").lower()
        if "fib" in name:
            suggestions.append("检测到递归 Fibonacci 热点，建议改用迭代 + 记忆化或查表法替代")
        elif "sort" in name:
            suggestions.append("排序开销较高，检查数据集大小，考虑原地排序或基数排序替代")
        elif "json" in name:
            suggestions.append("JSON 编解码占用 CPU 显著，检查是否存在不必要的重复序列化")
        elif "malloc" in name:
            suggestions.append("malloc 调用频繁，考虑使用内存池或 jemalloc 分配器")

    summary = summarize(top_functions, list(set(suggestions))[:3])
    collector = task.collector_type if hasattr(task, "collector_type") else "perf_cpu"
    questions = suggest_followup(top_functions, collector, ebpf_metrics)

    return APIResponse(data={
        "task_id": task_id,
        "summary": summary,
        "followup_questions": questions,
    })


# ── 启动入口 ──────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("SERVER_PORT", "8191")),
    )
