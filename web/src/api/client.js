/** Mini-Drop HTTP API 客户端。

所有 Web 请求通过此模块调用 Server REST API。
axios 拦截器统一处理错误码和响应格式。

认证方式（按优先级）:
  1. HttpOnly cookie (mini_drop_api_key) — 首选，XSS 无法窃取
  2. localStorage Bearer token — 兼容旧版
  3. X-API-Key header — 兼容直接调用
*/

import axios from "axios";

const API_KEY_STORAGE_KEY = "mini-drop-api-key";

const api = axios.create({
  baseURL: "/api",
  timeout: 30000,
  withCredentials: true,  // 发送 HttpOnly cookie
});

api.interceptors.request.use((config) => {
  // cookie 会自动携带，不再需要手动设置 Authorization header
  // 但保留兼容：如果 cookie 不可用，fallback 到 localStorage
  const token = getStoredApiKey();
  if (token) {
    config.headers["X-API-Key"] = token;
  }
  return config;
});

/** 响应拦截：统一提取 data 字段，简化调用方代码 */
api.interceptors.response.use(
  (resp) => {
    const body = resp.data;
    if (body.code === 0) return body.data;
    throw new Error(body.message || "未知错误");
  },
  (err) => {
    const detail = err.response?.status === 401
      ? "访问认证失败：请在右上角填写 Mini-Drop API Key 并点击保存"
      : err.response?.data?.detail || err.message;
    throw new Error(detail);
  },
);

// ── 通用 ────────────────────────────────────────────────────────

export function getStoredApiKey() {
  try {
    return window.localStorage.getItem(API_KEY_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function setStoredApiKey(token) {
  try {
    const normalized = (token || "").trim();
    if (normalized) {
      window.localStorage.setItem(API_KEY_STORAGE_KEY, normalized);
    } else {
      window.localStorage.removeItem(API_KEY_STORAGE_KEY);
    }
  } catch {
    // Ignore unavailable localStorage in restricted browser contexts.
  }
}

/** 通过 HttpOnly cookie 设置 API Key（比 localStorage 更安全，XSS 无法读取）。*/
export async function setCookieApiKey(token) {
  await axios.post("/api/auth/set-cookie", { api_key: token });
}

/** 清除 HttpOnly cookie。*/
export async function clearCookieApiKey() {
  await axios.post("/api/auth/clear-cookie");
}

/** 统一设置 API Key：优先 HttpOnly cookie，同时更新 localStorage 作为降级。*/
export async function saveApiKey(token) {
  const trimmed = (token || "").trim();
  setStoredApiKey(trimmed);  // 降级方案
  if (trimmed) {
    try {
      await setCookieApiKey(trimmed);
    } catch {
      // cookie 设置失败时不影响 localStorage 降级
      console.warn("HttpOnly cookie 设置失败，使用 localStorage 降级方案");
    }
  } else {
    try {
      await clearCookieApiKey();
    } catch {
      // ignore
    }
  }
}

export function healthz() {
  return api.get("/healthz");
}

function itemsOf(value) {
  if (Array.isArray(value)) return value;
  return value?.items || [];
}

// ── Agent ────────────────────────────────────────────────────────

export function listAgents() {
  return api.get("/agents").then(itemsOf);
}

export function listAuditLogs() {
  return api.get("/audit-logs").then(itemsOf);
}

// ── 任务 ────────────────────────────────────────────────────────

export function createTask(payload) {
  return api.post("/tasks", payload);
}

export function listTasks(params = {}) {
  return api.get("/tasks", { params }).then(itemsOf);
}

export function getTask(taskId) {
  return api.get(`/tasks/${taskId}`);
}

export function deleteTask(taskId) {
  return api.delete(`/tasks/${taskId}`);
}

export function getTaskEvents(taskId) {
  return api.get(`/tasks/${taskId}/events`);
}

export function getTaskArtifacts(taskId) {
  return api.get(`/tasks/${taskId}/artifacts`);
}

export function getTaskArtifactContent(taskId, artifactType, params = {}) {
  return api.get(`/tasks/${taskId}/artifacts/${artifactType}/content`, { params });
}

export async function downloadTaskArtifact(taskId, artifactType, params = {}) {
  const token = getStoredApiKey();
  const response = await axios.get(
    `/api/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactType)}/download`,
    {
      params,
      responseType: "blob",
      withCredentials: true,
      headers: token ? { "X-API-Key": token } : {},
    },
  );
  const disposition = response.headers["content-disposition"] || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  let filename = `${artifactType}.bin`;
  if (encoded) {
    try { filename = decodeURIComponent(encoded); } catch { filename = encoded; }
  }
  return { blob: response.data, filename };
}

export function triggerDiagnose(taskId, params = {}) {
  return api.post(`/tasks/${taskId}/diagnose`, null, { params });
}

export function listTaskDiagnoses(taskId) {
  return api.get(`/tasks/${taskId}/diagnoses`);
}

export function getDiagnosis(diagnosisId) {
  return api.get(`/diagnoses/${diagnosisId}`);
}

export function submitDiagnosisFeedback(diagnosisId, payload) {
  return api.post(`/diagnoses/${diagnosisId}/feedback`, payload);
}

// ── AI 集群诊断会话 ──────────────────────────────────────────────

export function createDiagnosisSession(payload) {
  return api.post("/v1/diagnoses", payload);
}

export function listDiagnosisSessions(params = {}) {
  return api.get("/v1/diagnoses", { params }).then(itemsOf);
}

export function getDiagnosisSession(diagnosisId) {
  return api.get(`/v1/diagnoses/${diagnosisId}`);
}

export function approveDiagnosisProbe(diagnosisId, payload) {
  return api.post(`/v1/diagnoses/${diagnosisId}/approvals`, payload);
}

export function approveWaitingDiagnosisProbes(diagnosisId, payload) {
  return api.post(`/v1/diagnoses/${diagnosisId}/approvals/bulk`, payload);
}

export function listProbeDefinitions() {
  return api.get("/v1/probes");
}

// ── Persistent Watch ──────────────────────────────────────────

export function createWatchSubscription(payload) {
  return api.post("/v1/watches", payload);
}

export function listWatchSubscriptions(params = {}) {
  return api.get("/v1/watches", { params }).then(itemsOf);
}

export function getWatchSubscription(watchId) {
  return api.get(`/v1/watches/${watchId}`);
}

export function listWatchIncidents(watchId) {
  return api.get(`/v1/watches/${watchId}/incidents`).then(itemsOf);
}

export function analyzeWatchIncident(incidentId, params = {}) {
  return api.post(`/v1/watch-incidents/${incidentId}/analyze`, null, { params, timeout: 180000 });
}

export function disableWatchSubscription(watchId) {
  return api.delete(`/v1/watches/${watchId}`);
}

export function listAgentWatchLeases(agentId) {
  return api.get(`/v1/agents/${agentId}/watch-leases`).then(itemsOf);
}

export function refreshAgentProcessInventory(agentId) {
  return api.post(`/v1/agents/${agentId}/process-inventory/refresh`);
}

export function listAgentProcesses(agentId, params = {}) {
  return api.get(`/v1/agents/${agentId}/processes`, { params }).then((value) => ({
    ...value,
    items: itemsOf(value),
  }));
}

export function evaluateWatchSubscription(watchId, payload) {
  return api.post(`/v1/watches/${watchId}/evaluate`, payload);
}

// ── NLP 自然语言采集 ────────────────────────────────────────────

export function nlpParse(query) {
  return api.post("/nlp/parse", { query });
}

export function nlpSummarize(taskId) {
  return api.post("/nlp/summarize", { task_id: taskId });
}

// ── 存储 ──────────────────────────────────────────────────────────

export function getPresignUrl(bucket, key, expires = 3600) {
  return api.get("/storage/presign", { params: { bucket, key, expires } });
}

// ── 配置 ──────────────────────────────────────────────────────────

export function getAIConfig() {
  return api.get("/ai-config");
}

export function runAIValidation() {
  return api.post("/ai-validation/runs", {}, { timeout: 180000 });
}

export function getCurrentUser() {
  return api.get("/me");
}

// ── SSE 事件 ──────────────────────────────────────────────────────

/**
 * 创建 SSE EventSource 连接。
 * @param {string} [since] - ISO 时间戳，只获取该时间之后的事件
 * @returns {EventSource}
 */
export function createEventSource(since = "") {
  const params = since ? `?since=${encodeURIComponent(since)}` : "";
  return new EventSource(`/api/events/stream${params}`);
}

// ── Prometheus 指标 ───────────────────────────────────────────────

export function getMetrics() {
  return api.get("/metrics");
}
