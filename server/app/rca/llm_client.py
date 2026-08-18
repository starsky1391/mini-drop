"""DeepSeek API 客户端。

工程校验层：LLM JSON 响应的格式校验 → 证据引用完整性 → 自修复重试。
"""

from __future__ import annotations

import json
import os
import re
import time

from server.app.ai_provider import chat_completions, get_ai_settings, is_feature_enabled
from server.app.rca.models import (
    CauseEntry,
    ControlledAITree,
    DiagnosisReport,
    EvidenceAttributionResult,
    EvidenceInput,
    ValidatedReport,
)
from server.app.rca.prompt import build_system_prompt, build_user_message


# 最大自修复重试次数
MAX_RETRIES = 2


def generate_controlled_ai_tree(
    *,
    task_id: str,
    evidence: EvidenceInput,
    analyzer_result: EvidenceAttributionResult,
    probe_manifest: dict,
    model_name: str | None = None,
) -> ControlledAITree | None:
    """Ask the LLM to generate the controlled AI tree, then enforce hard boundaries."""
    fallback_tree = analyzer_result.controlled_ai_tree
    if fallback_tree is None:
        return None
    if not is_feature_enabled("rca"):
        return fallback_tree

    model_name = model_name or get_ai_settings().model
    messages = [
        {"role": "system", "content": _build_controlled_tree_system_prompt()},
        {"role": "user", "content": _build_controlled_tree_user_message(
            evidence=evidence,
            analyzer_result=analyzer_result,
            probe_manifest=probe_manifest,
        )},
    ]
    last_error = ""
    for attempt in range(1 + MAX_RETRIES):
        try:
            raw = _call_deepseek(messages, model_name)
            proposed = ControlledAITree.model_validate(json.loads(_extract_json(raw) or "{}"))
            merged = _merge_llm_controlled_tree(
                llm_tree=proposed,
                analyzer_tree=fallback_tree,
                evidence=evidence,
                probe_manifest=probe_manifest,
            )
            if merged != fallback_tree or _llm_tree_shape_is_safe(proposed, fallback_tree, evidence, probe_manifest):
                return merged
            last_error = "controlled_ai_tree 越过 Analyzer 边界或引用了非法证据/探针"
        except Exception as exc:
            last_error = str(exc)
        if attempt < MAX_RETRIES:
            messages.append({"role": "user", "content": f"上一次 controlled_ai_tree 无效：{last_error}。请只基于模板候选和 Probe Manifest 修正 JSON。"})
    return fallback_tree

def diagnose(
    task_id: str,
    evidence: EvidenceInput,
    candidates_json: str,
    model_name: str | None = None,
) -> ValidatedReport:
    """执行智能归因：LLM 推理 + 校验 + 自修复。

    Args:
        task_id: 任务 ID。
        evidence: 结构化证据。
        candidates_json: 校准后候选原因列表的 JSON 字符串。
        model_name: DeepSeek 模型名。

    Returns:
        ValidatedReport，包含校验通过的 DiagnosisReport。
    """
    model_name = model_name or get_ai_settings().model
    evidence_json = _serialize_evidence(evidence)
    system_prompt = build_system_prompt(model_name)
    user_message = build_user_message(evidence_json, candidates_json)

    if not is_feature_enabled("rca"):
        return _fallback_report(task_id, evidence, candidates_json)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    last_error = ""
    for attempt in range(1 + MAX_RETRIES):
        try:
            raw = _call_deepseek(messages, model_name)
            report, issues = _validate_and_parse(raw, evidence)
            if not issues:
                report = _attach_analysis_result(report, evidence)
                return ValidatedReport(
                    task_id=task_id,
                    model_name=model_name,
                    evidence_snapshot=json.loads(evidence_json),
                    report=report,
                    validated=True,
                    retry_count=attempt,
                )

            # 校验失败 → 构造修复提示追加到 messages
            last_error = "; ".join(issues)
            repair_prompt = (
                f"上一次你的输出校验失败：{last_error}\n"
                "请修正后重新输出 JSON。"
            )
            messages.append({"role": "assistant", "content": raw[:200]})
            messages.append({"role": "user", "content": repair_prompt})

        except Exception as exc:
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                time.sleep(1 * (attempt + 1))  # 指数退避
                continue

    # 全部重试失败
    return ValidatedReport(
        task_id=task_id,
        model_name=model_name,
        evidence_snapshot=json.loads(evidence_json) if evidence_json else {},
        report=_attach_analysis_result(DiagnosisReport(
            summary=f"归因失败（已重试 {MAX_RETRIES} 次）: {last_error}",
            ranked_causes=[],
            facts=[],
            not_enough_evidence=True,
        ), evidence),
        validated=False,
        validation_issues=[last_error],
        retry_count=MAX_RETRIES,
    )


# ── 内部 ──


def _serialize_evidence(evidence: EvidenceInput) -> str:
    """将证据序列化为 JSON，字段按近因效应排序——越重要的越靠后。"""
    from server.app.rca.evidence import evidence_to_json
    return evidence_to_json(evidence)


def _call_deepseek(messages: list[dict], model: str) -> str:
    """调用 DeepSeek Chat API。

    Args:
        messages: 对话历史（system + user）。
        model: 模型名称。

    Returns:
        LLM 原始响应文本。

    Raises:
        RuntimeError: API 返回非 200。
    """
    payload = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "disabled"},
        "temperature": 0.1,  # 低温：归因需要确定性而非创意
        "max_tokens": int(os.getenv("MINI_DROP_RCA_MAX_TOKENS", "4096")),
        "response_format": {"type": "json_object"},
    }
    timeout = max(10, int(os.getenv("MINI_DROP_RCA_LLM_TIMEOUT_SEC", "90")))
    resp = chat_completions(payload, timeout=timeout)
    if resp.status_code in (400, 422) or resp.status_code >= 500:
        compatible_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"thinking", "response_format"}
        }
        resp = chat_completions(compatible_payload, timeout=timeout)

    if resp.status_code != 200:
        raise RuntimeError(f"DeepSeek API 返回 {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    message = body["choices"][0]["message"]
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content

    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content

    raise RuntimeError("DeepSeek API 返回的消息缺少可解析内容")



def _validate_and_parse(raw: str, evidence: EvidenceInput) -> tuple[DiagnosisReport | None, list[str]]:
    """校验 LLM 输出并解析为 DiagnosisReport。

    校验规则：
      1. JSON 可解析
      2. 所有字段类型正确（通过 Pydantic 校验）
      3. 每条 cause 的 evidence_refs 必须引用 evidence 中存在的字段
      4. confidence 在 [0, 1]
      5. ranked_causes 不空（除非 not_enough_evidence=True）
    """
    issues: list[str] = []

    # 步骤 1：提取 JSON
    json_text = _extract_json(raw)
    if not json_text:
        return None, ["无法从 LLM 输出中提取 JSON"]

    # 步骤 2：Pydantic 解析
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        return None, [f"JSON 解析失败: {exc}"]

    try:
        report = DiagnosisReport(**data)
    except Exception as exc:
        return None, [f"Schema 校验失败: {exc}"]

    # 步骤 3：证据引用完整性——每条 cause 的 evidence_refs 必须在 evidence 中可找到
    valid_paths = _collect_evidence_paths(evidence)
    for i, cause in enumerate(report.ranked_causes):
        for ref in cause.evidence_refs:
            if not _ref_exists(ref, valid_paths):
                issues.append(f"ranked_causes[{i}].evidence_refs 中的 '{ref}' 不在证据路径中")

    # 步骤 4：边界校验
    if report.not_enough_evidence and not report.ranked_causes:
        pass  # 证据不足 + 无候选 = 合理
    elif not report.ranked_causes and not report.not_enough_evidence:
        issues.append("ranked_causes 为空但 not_enough_evidence=false")

    analysis_result = evidence.analysis_result or {}
    if analysis_result:
        allowed_cause_ids = set(analysis_result.get("allowed_cause_ids", []))
        boundary = analysis_result.get("conclusion_boundary", {})
        can_claim_root_cause = bool(boundary.get("can_claim_root_cause", False))
        primary_cause_id = analysis_result.get("primary_cause_id")
        stability_score = float(analysis_result.get("stability_score", 0.0) or 0.0)

        for i, cause in enumerate(report.ranked_causes):
            if cause.cause_id not in allowed_cause_ids:
                issues.append(
                    f"ranked_causes[{i}].cause_id '{cause.cause_id}' 不在结构化分析允许的原因集合中"
                )

        if primary_cause_id and report.ranked_causes and report.ranked_causes[0].cause_id != primary_cause_id:
            issues.append(
                f"ranked_causes[0].cause_id '{report.ranked_causes[0].cause_id}' 必须优先与结构化主因 '{primary_cause_id}' 一致"
            )

        boundary_reason = str(boundary.get("reason", "") or "")
        if boundary_reason and any(
            token in boundary_reason
            for token in ("off_cpu_wait_profile", "trace_endpoint_profile", "baseline_window_profile")
        ):
            if not (
                report.missing_evidence
                or report.blocked_upgrades
                or report.collection_gaps
            ):
                issues.append("结构化边界已经说明缺少采集能力，报告也必须同步输出 missing_evidence / blocked_upgrades / collection_gaps")

        if not can_claim_root_cause:
            if not report.not_enough_evidence:
                issues.append("结构化分析禁止根因结论时，报告必须标记为证据不足")
            for i, cause in enumerate(report.ranked_causes):
                if cause.confidence >= 0.4:
                    issues.append(
                        f"ranked_causes[{i}] 在证据不足时 confidence 必须低于 0.4"
                    )
        elif primary_cause_id and stability_score >= 0.7 and report.ranked_causes:
            if report.ranked_causes[0].confidence < report.ranked_causes[-1].confidence:
                issues.append("高稳定性场景下 ranked_causes 应保持主因优先且置信度不低于后续候选")

    if issues:
        return None, issues

    return _attach_analysis_result(report, evidence), []


def _attach_analysis_result(report: DiagnosisReport, evidence: EvidenceInput) -> DiagnosisReport:
    """把结构化分析结果挂回最终报告对象，保证最终输出形态完整。"""
    analysis_result = evidence.analysis_result or {}
    if not analysis_result:
        return report

    normalized_result = dict(analysis_result)
    boundary = dict(normalized_result.get("conclusion_boundary") or {})
    boundary.setdefault("reason", normalized_result.get("primary_cause_reason", ""))
    boundary.setdefault("max_supported_level", "resource")
    normalized_result["conclusion_boundary"] = boundary

    analysis_result_model = EvidenceAttributionResult.model_validate(normalized_result)
    controlled_tree = _merge_llm_controlled_tree(
        llm_tree=report.controlled_ai_tree,
        analyzer_tree=analysis_result_model.controlled_ai_tree,
        evidence=evidence,
        probe_manifest=analysis_result.get("probe_registry_manifest") if isinstance(analysis_result, dict) else None,
    )
    return report.model_copy(update={
        "analysis_result": analysis_result_model,
        "symptoms": analysis_result_model.symptoms,
        "localizations": analysis_result_model.localizations,
        "ai_tree": analysis_result_model.ai_tree,
        "controlled_ai_tree": controlled_tree,
        "graph_entities": analysis_result_model.graph_entities,
        "graph_links": analysis_result_model.graph_links,
        "attributions": analysis_result_model.attributions,
        "evidence_challenges": analysis_result_model.evidence_challenges,
        "missing_evidence": analysis_result_model.missing_evidence,
        "blocked_upgrades": analysis_result_model.blocked_upgrades,
        "collection_gaps": analysis_result_model.collection_gaps,
        "graph_extension_points": analysis_result_model.graph_extension_points,
        "structured_evidence": evidence.analysis_result.get("structured_evidence") if evidence.analysis_result else None,
        "primary_cause_id": analysis_result_model.primary_cause_id,
        "stability_score": analysis_result_model.stability_score,
        "primary_cause_reason": analysis_result_model.primary_cause_reason,
        "secondary_causes": analysis_result.get("secondary_causes", []),
        "correlated_symptoms": analysis_result.get("correlated_symptoms", []),
        "unsupported_causes": analysis_result.get("unsupported_causes", []),
        "conclusion_boundary": analysis_result_model.conclusion_boundary,
    })


def _merge_llm_controlled_tree(
    *,
    llm_tree: ControlledAITree | None,
    analyzer_tree: ControlledAITree | None,
    evidence: EvidenceInput,
    probe_manifest: dict | None = None,
) -> ControlledAITree | None:
    """Adopt LLM tree decisions only inside Analyzer and probe-registry boundaries."""
    if analyzer_tree is None or llm_tree is None:
        return analyzer_tree

    if not _llm_tree_shape_is_safe(llm_tree, analyzer_tree, evidence, probe_manifest):
        return analyzer_tree

    return analyzer_tree.model_copy(update={
        "layers": [layer.model_copy(update={"generated_by": "ai_guarded"}) for layer in llm_tree.layers],
        "probe_edges": llm_tree.probe_edges,
        "final_supported_level": analyzer_tree.final_supported_level,
        "final_primary_causes": _group_candidate_ids(llm_tree.layers, "primary"),
        "final_secondary_causes": _group_candidate_ids(llm_tree.layers, "secondary"),
        "final_rejected_causes": _group_candidate_ids(llm_tree.layers, "rejected"),
        "final_unknown_causes": _group_candidate_ids(llm_tree.layers, "unknown"),
        "stop_reason": llm_tree.stop_reason or analyzer_tree.stop_reason,
    })


def _group_candidate_ids(layers, role: str) -> list[str]:
    ids = []
    for layer in layers:
        for item in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]:
            if item.role == role and item.candidate_id not in ids:
                ids.append(item.candidate_id)
    return ids


def _llm_tree_shape_is_safe(
    llm_tree: ControlledAITree,
    analyzer_tree: ControlledAITree,
    evidence: EvidenceInput,
    probe_manifest: dict | None = None,
) -> bool:
    if llm_tree.tree_id != analyzer_tree.tree_id:
        return False
    if llm_tree.final_supported_level != analyzer_tree.final_supported_level:
        return False
    if [layer.layer_id for layer in llm_tree.layers] != [layer.layer_id for layer in analyzer_tree.layers]:
        return False

    analyzer_candidates = {
        item.candidate_id: item
        for layer in analyzer_tree.layers
        for item in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    analyzer_candidate_ids = set(analyzer_candidates)
    valid_paths = _collect_evidence_paths(evidence)
    max_level_order = _level_order(analyzer_tree.final_supported_level)
    llm_candidate_ids = set()

    for layer in llm_tree.layers:
        for item in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]:
            base = analyzer_candidates.get(item.candidate_id)
            if base is None:
                return False
            llm_candidate_ids.add(item.candidate_id)
            if any(parent_id not in analyzer_candidate_ids for parent_id in item.parent_candidate_ids):
                return False
            if not _candidate_status_is_safe(base.status, item.status):
                return False
            if _level_order(item.supported_level) > max_level_order:
                return False
            if _level_order(item.supported_level) > _level_order(base.supported_level):
                return False
            refs = [
                *item.evidence_refs,
                *item.self_challenge.supporting_evidence_refs,
                *item.self_challenge.opposing_evidence_refs,
            ]
            if any(not _ref_exists(ref, valid_paths) for ref in refs):
                return False

    if llm_candidate_ids != analyzer_candidate_ids:
        return False

    analyzer_layer_ids = {layer.layer_id for layer in analyzer_tree.layers}
    allowed_requests = _manifest_allowed_probe_requests(probe_manifest, analyzer_tree)
    for edge in llm_tree.probe_edges:
        if edge.from_layer_id not in analyzer_layer_ids:
            return False
        if edge.to_layer_id is not None and edge.to_layer_id not in analyzer_layer_ids:
            return False
        if any(candidate_id not in analyzer_candidate_ids for candidate_id in edge.from_candidate_ids):
            return False
        if any(candidate_id not in analyzer_candidate_ids for candidate_id in edge.to_candidate_ids):
            return False
        if any(request not in allowed_requests for request in edge.probe_requests):
            return False
    return True


def _candidate_status_is_safe(base_status: str, proposed_status: str) -> bool:
    if proposed_status == base_status:
        return True
    allowed_downgrades = {
        "supported": {"weakened", "missing_evidence", "unknown"},
        "weakened": {"missing_evidence", "unknown"},
        "missing_evidence": {"unknown"},
        "forbidden": set(),
        "unknown": set(),
    }
    return proposed_status in allowed_downgrades.get(base_status, set())


def _manifest_allowed_probe_requests(probe_manifest: dict | None, analyzer_tree: ControlledAITree) -> set[str]:
    allowed = {
        request
        for edge in analyzer_tree.probe_edges
        for request in edge.probe_requests
    }
    if isinstance(probe_manifest, dict):
        for item in probe_manifest.get("available_probes", []):
            if isinstance(item, dict) and item.get("evidence_family"):
                allowed.add(str(item["evidence_family"]))
    return {item for item in allowed if item}


def _level_order(level: str) -> int:
    order = {
        "resource": 0,
        "host": 1,
        "process": 2,
        "thread": 3,
        "syscall": 4,
        "dependency": 5,
        "service": 6,
        "endpoint": 7,
        "function": 8,
        "call_path": 9,
        "line": 10,
    }
    return order.get(level, 0)


def _build_controlled_tree_system_prompt() -> str:
    return """你是 Mini-Drop 的受控 AI 树生成器。

你必须输出一个 ControlledAITree JSON 对象，不能输出 markdown。

硬性规则：
1. tree_id、schema_version、source_context_hash、final_supported_level 必须沿用 Analyzer 模板。
2. layer_id 必须沿用 Analyzer 模板，不得新增或删除层。
3. candidate_id 必须来自 Analyzer 模板，不得新增候选；模板里的所有候选必须保留且只能出现一次。
4. 你可以在已有候选内重新分配 primary / secondary / rejected / unknown，并填写 parent_candidate_ids / probe_edges 的 candidate 血缘，但不得引用不存在的 candidate。
5. supported_level 不得超过 Analyzer 给出的 final_supported_level，也不得超过候选原始 supported_level。
6. evidence_refs 只能引用当前证据中真实存在的路径。
7. probe_edges[].probe_requests 只能选择 Probe Manifest 中的 evidence_family，不能写 probe_id，不能写任意 shell、sysctl、修复动作。
8. 你必须为每个候选填写 self_challenge：为什么是它、为什么不是其他、支持证据、反驳证据、缺失证据、什么会改变结论。
9. 证据不足时要停在当前证据支持层级，并通过 probe_edges 请求最小必要补证，而不是强行给更细结论。
"""


def _build_controlled_tree_user_message(
    *,
    evidence: EvidenceInput,
    analyzer_result: EvidenceAttributionResult,
    probe_manifest: dict,
) -> str:
    payload = {
        "current_evidence": json.loads(_serialize_evidence(evidence)),
        "analyzer_boundaries": {
            "facts": [item.model_dump(mode="json") for item in analyzer_result.facts],
            "localizations": [item.model_dump(mode="json") for item in analyzer_result.localizations],
            "attributions": [item.model_dump(mode="json") for item in analyzer_result.attributions],
            "missing_evidence": analyzer_result.missing_evidence,
            "blocked_upgrades": analyzer_result.blocked_upgrades,
            "collection_gaps": analyzer_result.collection_gaps,
            "allowed_cause_ids": analyzer_result.allowed_cause_ids,
            "primary_cause_id": analyzer_result.primary_cause_id,
            "conclusion_boundary": analyzer_result.conclusion_boundary.model_dump(mode="json"),
        },
        "controlled_tree_template": analyzer_result.controlled_ai_tree.model_dump(mode="json") if analyzer_result.controlled_ai_tree else None,
        "probe_registry_manifest": probe_manifest,
        "output": "只输出 ControlledAITree JSON 对象。",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _extract_json(raw: str | None) -> str | None:
    """从 LLM 原始输出中提取 JSON。

    处理以下情况：
      - 纯 JSON
      - ```json ... ``` 包裹
      - ``` ... ``` 包裹
    """
    if not raw:
        return None
    text = raw.strip()

    # 尝试匹配 ```json ... ``` 或 ``` ... ```
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 尝试找到第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]

    return None


def _collect_evidence_paths(evidence: EvidenceInput) -> dict[str, set[str]]:
    """收集 evidence 中所有可引用的字段路径。

    Returns:
        {"top_functions": {"name", "samples", "percent"}, ...}
    """
    paths: dict[str, set[str]] = {}

    if evidence.top_functions:
        paths["top_functions"] = set()
        for item in evidence.top_functions[:3]:
            paths["top_functions"].update(item.keys())

    if evidence.ebpf_metrics:
        paths["ebpf_metrics"] = set(evidence.ebpf_metrics.keys())

    if evidence.baseline_diff:
        paths["baseline_diff"] = set(evidence.baseline_diff.keys())

    if evidence.agent_stats:
        paths["agent_stats"] = set(evidence.agent_stats.keys())

    if evidence.evidence_index:
        paths["evidence_index"] = _collect_index_paths(evidence.evidence_index)

    if evidence.task_metadata:
        paths["task_metadata"] = set(evidence.task_metadata.keys())

    if evidence.tool_results:
        # tool_results can be referenced by tool_name and also by generic keys
        tool_paths: set[str] = set()
        for item in evidence.tool_results:
            tn = item.get("tool_name", "")
            if tn:
                tool_paths.add(tn)
            # collect common tool result top-level fields
            for field in ("status", "evidence_ref", "tool_name"):
                val = item.get(field)
                if val:
                    tool_paths.add(f"{tn}.{field}" if tn else field)
            # also collect sub-keys of tool output so LLM can reference them
            out = item.get("output", {}) if isinstance(item.get("output"), dict) else {}
            for k in out.keys():
                tool_paths.add(f"{tn}.output.{k}" if tn else k)
        paths["tool_results"] = tool_paths

    # Top-level scalar fields on EvidenceInput — LLM can reference them directly
    if evidence.failure_events:
        paths["failure_events"] = set()  # list field — any value is valid
    if evidence.suggestions:
        paths["suggestions"] = set()  # list field
    if evidence.sys_metrics:
        paths["sys_metrics"] = set(evidence.sys_metrics.keys()) if isinstance(evidence.sys_metrics, dict) else set()

    if evidence.analysis_result:
        paths["analysis_result"] = set(evidence.analysis_result.keys())

    return paths


def _collect_index_paths(value, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            next_prefix = f"{prefix}.{key_text}" if prefix else key_text
            paths.add(next_prefix)
            paths.update(_collect_index_paths(item, next_prefix))
        return paths
    if isinstance(value, list):
        for item in value:
            paths.update(_collect_index_paths(item, prefix))
        return paths
    if prefix:
        paths.add(prefix)
    return paths


def _ref_exists(ref: str, valid_paths: dict[str, set[str]]) -> bool:
    """检查 evidence_ref 是否在有效路径集合中。

    支持格式：
      - "top_functions[0]" → 索引被去掉，检查顶层 key 存在
      - "task_metadata.status" → 检查嵌套路径
      - "tool_results[3].output.failure_reasons" → LLM 用索引引用 tool_results，
        校验时去掉索引 + tool_name 前缀做 lenient 匹配
    """
    # 去掉索引后缀: "top_functions[0]" → "top_functions"
    base = re.sub(r"\[\d+\]", "", ref)
    # 取顶层 key
    top = base.split(".")[0]

    if top not in valid_paths:
        return False

    # 没有子路径 → 顶层 key 存在即通过
    if "." not in base:
        return True

    sub = base.split(".", 1)[1]
    sub_paths = valid_paths[top]

    # 精确匹配
    if sub in sub_paths:
        return True

    # Lenient: tool_results 的 LLM 可能用索引或省略 tool_name 前缀
    #   e.g. ref="tool_results.output.failure_reasons"
    #   而 valid path 是 "inspect_task_events.output.failure_reasons"
    #   检查是否有任何 valid path 末尾段匹配
    if top == "tool_results":
        for valid_sub in sub_paths:
            if valid_sub.endswith("." + sub.split(".", 1)[-1] if "." in sub else sub):
                return True
            # Also check if ref's output.{key} matches valid's output.{key}
            if sub.startswith("output."):
                out_key = sub.split("output.", 1)[-1]
                if valid_sub.endswith(f".output.{out_key}"):
                    return True

    return False


def _fallback_report(task_id: str, evidence: EvidenceInput, candidates_json: str = "[]") -> ValidatedReport:
    """API Key 未配置时的降级报告（纯规则引擎输出）。"""
    ranked: list[CauseEntry] = []
    try:
        candidates = json.loads(candidates_json)
    except json.JSONDecodeError:
        candidates = []

    for item in candidates[:3]:
        confidence = float(item.get("final_confidence", 0.0))
        if item.get("candidate_id") == "insufficient_data":
            continue
        ranked.append(CauseEntry(
            cause_id=item.get("candidate_id", "unknown"),
            confidence=confidence,
            claim=item.get("description", "规则引擎候选归因"),
            evidence_refs=item.get("evidence_refs", []),
            uncertainties=item.get("missing_evidence", []),
            verification_steps=["补充采集或对比 baseline 后复核该结论"],
        ))

    not_enough = len(ranked) == 0
    report = DiagnosisReport(
        summary="未配置 DEEPSEEK_API_KEY，归因引擎使用规则候选与工具证据生成降级报告。",
        ranked_causes=ranked,
        facts=evidence.suggestions if evidence.suggestions else ["无规则命中"],
        not_enough_evidence=not_enough,
    )
    return ValidatedReport(
        task_id=task_id,
        model_name="rule-engine-only",
        evidence_snapshot=evidence.model_dump() if isinstance(evidence, EvidenceInput) else {},
        report=_attach_analysis_result(report, evidence),
        validated=True,
    )
