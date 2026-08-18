"""智能归因模块测试。

覆盖：证据采集 / 候选规则匹配 / 置信度校准 /
       Prompt 模板 / 输出校验 / 自修复重试 / 降级行为。
"""

import json
from unittest import mock

import pytest

from server.app.rca.calibrator import calibrate, interpret_confidence
from server.app.rca.candidates import generate_candidates, load_rules
from server.app.rca.evidence import collect_evidence, evidence_to_json
from server.app.rca.attribution import analyze_evidence
from server.app.rca.llm_client import (
    _attach_analysis_result,
    _collect_evidence_paths,
    _extract_json,
    _call_deepseek,
    _ref_exists,
    _validate_and_parse,
    generate_controlled_ai_tree,
)
from server.app.diagnosis.probe_registry import build_probe_manifest
from server.app.rca.models import CandidateCause, CauseEntry, DiagnosisReport, EvidenceInput, FeedbackPrior
from server.app.rca.prompt import build_system_prompt, build_user_message
from server.app.rca.report import run_diagnosis, run_diagnosis_context
from server.app.rca.tools import run_rca_tools


# ── 模拟 Task Record ──


class _StubTask:
    def __init__(self):
        self.id = "task_test"
        self.agent_id = "agent_test"
        self.collector_type = "perf_cpu"
        self.target_pid = 1234
        self.duration_sec = 15
        self.sample_rate = 99
        self.status = "DONE"
        self.status_reason = "analysis completed"


# ── 证据采集 ──


class TestEvidenceCollection:
    """证据采集层。"""

    def test_collects_all_fields(self):
        task = _StubTask()
        ev = collect_evidence(
            task_id=task.id, task_record=task,
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.0}],
            ebpf_metrics={"io_latency_us": {"[128,256)": 10}},
            suggestions=["检查递归"],
            failure_events=["permission denied"],
            baseline_diff={"cpu_percent_delta": 42.0},
            agent_stats={"max_cpu_percent": 3.1},
        )
        assert ev.task_metadata["collector_type"] == "perf_cpu"
        assert len(ev.top_functions) == 1
        assert ev.ebpf_metrics is not None
        assert len(ev.suggestions) == 1
        assert len(ev.failure_events) == 1
        assert ev.baseline_diff is not None

    def test_evidence_json_is_valid(self):
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task,
                              top_functions=[{"name": "f", "samples": 1, "percent": 100.0}])
        text = evidence_to_json(ev)
        data = json.loads(text)
        assert "task_metadata" in data
        assert "top_functions" in data

    def test_fields_ordered_by_recency(self):
        """top_functions 应出现在 ebpf_metrics 之前（证据按重要性排序，
        越重要越靠后 → 近因效应）。实际输出中重要字段放在后面。"""
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task,
                              top_functions=[{"name": "f", "samples": 1, "percent": 50.0}],
                              ebpf_metrics={"latency": {}})
        text = evidence_to_json(ev)
        # ebpf_metrics 应在 top_functions 之后出现（JSON 字符串位置）
        tf_pos = text.index("top_functions")
        ebpf_pos = text.index("ebpf_metrics")
        assert tf_pos < ebpf_pos

    def test_evidence_index_is_compacted_by_default(self):
        task = _StubTask()
        ev = collect_evidence(
            task_id="t1",
            task_record=task,
            evidence_index={
                "stack_samples": [
                    {
                        "hot_frame": f"frame_{index}",
                        "call_path": f"main;worker;frame_{index}",
                        "stack_fragment": ["main", "worker", f"frame_{index}"],
                    }
                    for index in range(8)
                ],
                "context": {
                    "call_path": "main;worker;frame_0",
                    "endpoint": "/api/order/create",
                },
            },
        )

        text = evidence_to_json(ev)
        data = json.loads(text)

        assert "evidence_index_raw" not in data
        assert len(data["evidence_index"]["stack_samples"]) == 5

    def test_evidence_index_can_hydrate_requested_refs(self):
        task = _StubTask()
        ev = collect_evidence(
            task_id="t1",
            task_record=task,
            evidence_index={
                "stack_samples": [
                    {
                        "hot_frame": "compute_hotspot",
                        "call_path": "main;worker;compute_hotspot",
                        "stack_fragment": ["main", "worker", "compute_hotspot"],
                        "wait_reason": "cpu_hotspot",
                    }
                ],
                "context": {
                    "call_path": "main;worker;compute_hotspot",
                    "endpoint": "/api/order/create",
                },
            },
        )

        text = evidence_to_json(ev, requested_refs=["evidence_index.stack_samples[0].call_path"])
        data = json.loads(text)

        assert data["evidence_index_raw"]["stack_samples"][0]["call_path"] == "main;worker;compute_hotspot"


# ── 候选规则 ──


class TestCandidateGeneration:
    """规则引擎。"""

    def test_rules_loaded_from_external_file(self):
        load_rules.cache_clear()
        rules = load_rules()
        assert any(rule["candidate_id"] == "cpu_hotspot_recursive" for rule in rules)
        assert all("match_type" in rule for rule in rules)

    def test_cpu_hotspot_matched(self):
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task,
                              top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}])
        candidates = generate_candidates(ev)
        ids = [c.candidate_id for c in candidates]
        assert "cpu_hotspot_recursive" in ids

    def test_io_wait_matched_from_ebpf(self):
        task = _StubTask()
        task.collector_type = "ebpf_io"
        ev = collect_evidence(task_id="t1", task_record=task,
                              ebpf_metrics={"io_latency_us": {"[128,256)": 10}})
        candidates = generate_candidates(ev)
        assert any(c.candidate_id == "io_wait_high" for c in candidates)

    def test_no_rules_fallback(self):
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task)
        candidates = generate_candidates(ev)
        assert len(candidates) == 1
        assert candidates[0].candidate_id == "insufficient_data"

    def test_target_pid_invalid_matched(self):
        task = _StubTask()
        task.status = "FAILED"
        ev = collect_evidence(task_id="t1", task_record=task,
                              failure_events=["目标 PID 不存在"])
        candidates = generate_candidates(ev)
        assert any(c.candidate_id == "target_pid_invalid" for c in candidates)

    def test_feedback_prior_adjusts_score(self):
        task = _StubTask()
        ev = collect_evidence(task_id="t1", task_record=task,
                              top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}])
        candidates_no_prior = generate_candidates(ev)

        priors = {"cpu_hotspot_recursive": FeedbackPrior(
            candidate_id="cpu_hotspot_recursive", positive_count=2, negative_count=0, weight_delta=0.10)}
        candidates_with_prior = generate_candidates(ev, priors)

        cpu_no = next(c for c in candidates_no_prior if c.candidate_id == "cpu_hotspot_recursive")
        cpu_with = next(c for c in candidates_with_prior if c.candidate_id == "cpu_hotspot_recursive")
        assert cpu_with.rule_score > cpu_no.rule_score


# ── 置信度校准 ──


class TestCalibrator:
    """校准器。"""

    def test_calibration_outputs_confidence(self):
        ev = EvidenceInput(task_metadata={"duration_sec": 15},
                           top_functions=[{"name": "f", "samples": 100, "percent": 80.0}])
        candidates = [CandidateCause(
            candidate_id="cpu_hotspot_recursive",
            description="CPU hotspot",
            evidence_refs=["top_functions[0]"],
            rule_score=0.83,
        )]
        calibrated = calibrate(candidates, ev)
        assert len(calibrated) == 1
        assert 0.0 <= calibrated[0].final_confidence <= 1.0

    def test_confidence_interpretation(self):
        assert interpret_confidence(0.85) == "高置信"
        assert interpret_confidence(0.70) == "可能"
        assert interpret_confidence(0.50) == "待验证"
        assert interpret_confidence(0.30) == "证据不足"


class TestControlledAITreeMerge:
    """受控 AI 树只允许 LLM 改写解释，不允许越界裁决。"""

    def test_llm_can_enrich_controlled_tree_explanations(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        evidence.analysis_result = analysis.model_dump(mode="json")
        proposed_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        proposed_primary = proposed_tree.layers[0].primary_causes[0]
        proposed_tree.layers[0].primary_causes[0] = proposed_primary.model_copy(update={
            "claim": "AI 提议：热点集中在 compute_hotspot，当前只支持函数层定位。",
            "self_challenge": proposed_primary.self_challenge.model_copy(update={
                "why_this_claim": "CPU 与 top function 证据同向支持。",
                "why_not_other_claims": "IO wait 证据没有同向升高。",
            }),
        })
        report = DiagnosisReport(
            summary="CPU hotspot",
            ranked_causes=[CauseEntry(
                cause_id="cpu_hotspot_recursive",
                confidence=0.8,
                claim="CPU hotspot",
                evidence_refs=["top_functions[0]"],
            )],
            facts=["compute_hotspot 72%"],
            controlled_ai_tree=proposed_tree,
        )

        attached = _attach_analysis_result(report, evidence)

        assert attached.controlled_ai_tree.layers[0].primary_causes[0].claim.startswith("AI 提议")
        assert attached.controlled_ai_tree.final_primary_causes == analysis.controlled_ai_tree.final_primary_causes
        assert attached.controlled_ai_tree.probe_edges == analysis.controlled_ai_tree.probe_edges

    def test_llm_controlled_tree_cannot_promote_level_or_add_refs(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        evidence.analysis_result = analysis.model_dump(mode="json")
        unsafe_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        unsafe_primary = unsafe_tree.layers[0].primary_causes[0]
        unsafe_tree.layers[0].primary_causes[0] = unsafe_primary.model_copy(update={
            "supported_level": "line",
            "claim": "越权提升到代码行。",
            "evidence_refs": ["evidence_index.fake_line[0]"],
        })
        report = DiagnosisReport(
            summary="CPU hotspot",
            ranked_causes=[CauseEntry(
                cause_id="cpu_hotspot_recursive",
                confidence=0.8,
                claim="CPU hotspot",
                evidence_refs=["top_functions[0]"],
            )],
            facts=["compute_hotspot 72%"],
            controlled_ai_tree=unsafe_tree,
        )

        attached = _attach_analysis_result(report, evidence)

        assert attached.controlled_ai_tree == analysis.controlled_ai_tree

    def test_llm_controlled_tree_can_select_manifest_probe(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        manifest = build_probe_manifest()
        evidence = evidence.model_copy(update={
            "analysis_result": {
                **analysis.model_dump(mode="json"),
                "probe_registry_manifest": manifest,
            }
        })
        proposed_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        proposed_tree.probe_edges[0] = proposed_tree.probe_edges[0].model_copy(update={
            "probe_requests": ["redis_check"],
            "reason": "AI 选择 Redis 专项检查来反证下游依赖方向。",
        })
        mock_resp = mock.MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps(proposed_tree.model_dump(mode="json"))}}]
        }

        with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", return_value=mock_resp):
                tree = generate_controlled_ai_tree(
                    task_id="t1",
                    evidence=evidence,
                    analyzer_result=analysis,
                    probe_manifest=manifest,
                )

        assert tree.probe_edges[0].probe_requests == ["redis_check"]

    def test_llm_call_retries_with_openai_compatible_payload(self):
        bad_resp = mock.MagicMock(status_code=502, text="proxy reset")
        good_resp = mock.MagicMock(status_code=200)
        good_resp.json.return_value = {
            "choices": [{"message": {"content": "{\"ok\": true}"}}]
        }
        payloads = []

        def fake_chat(payload, timeout):
            payloads.append(payload)
            return bad_resp if len(payloads) == 1 else good_resp

        with mock.patch("server.app.rca.llm_client.chat_completions", side_effect=fake_chat):
            assert _call_deepseek([{"role": "user", "content": "json"}], "model") == "{\"ok\": true}"

        assert "thinking" in payloads[0]
        assert "response_format" in payloads[0]
        assert "thinking" not in payloads[1]
        assert "response_format" not in payloads[1]

    def test_llm_controlled_tree_rejects_unregistered_probe_request(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "compute_hotspot", "percent": 72.0}],
            sys_metrics={"summary": {"avg_cpu_user_pct": 92.0, "avg_cpu_iowait_pct": 1.0}},
        )
        analysis = analyze_evidence(
            evidence,
            [CandidateCause(
                candidate_id="cpu_hotspot_recursive",
                description="CPU hotspot",
                evidence_refs=["top_functions[0]"],
                rule_score=0.8,
            )],
        )
        manifest = build_probe_manifest()
        proposed_tree = analysis.controlled_ai_tree.model_copy(deep=True)
        proposed_tree.probe_edges[0] = proposed_tree.probe_edges[0].model_copy(update={
            "probe_requests": ["arbitrary_shell"],
        })
        mock_resp = mock.MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps(proposed_tree.model_dump(mode="json"))}}]
        }

        with mock.patch.dict("os.environ", {"MINI_DROP_AI_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", return_value=mock_resp):
                tree = generate_controlled_ai_tree(
                    task_id="t1",
                    evidence=evidence,
                    analyzer_result=analysis,
                    probe_manifest=manifest,
                )

        assert tree == analysis.controlled_ai_tree


# ── Prompt 模板 ──


class TestPromptTemplate:
    """System prompt 和 user message 生成。"""

    def test_prompt_contains_constraints(self):
        prompt = build_system_prompt()
        assert "硬性约束" in prompt
        assert "evidence_refs" in prompt
        assert "样例" in prompt

    def test_prompt_contains_few_shots(self):
        prompt = build_system_prompt()
        assert "fib_hotspot" in prompt  # shot 1
        assert "io_wait_high" in prompt  # shot 2
        assert "debuginfo" in prompt  # shot 3

    def test_prompt_duplicates_constraints(self):
        """核心约束应在 prompt 头尾各出现一次，对抗遗忘。"""
        prompt = build_system_prompt()
        count = prompt.count("硬性约束")
        assert count >= 2

    def test_user_message_puts_evidence_at_end(self):
        msg = build_user_message('{"top":[]}', '[{"id":"x"}]')
        assert "当前证据" in msg
        assert "候选原因" in msg
        assert msg.index("当前证据") < msg.index("候选原因")

    def test_model_tag_in_prompt(self):
        prompt = build_system_prompt("deepseek-chat")
        assert "DeepSeek Chat" in prompt

    def test_flash_model_tag(self):
        prompt = build_system_prompt("deepseek-4-flash")
        assert "DeepSeek V4 Flash" in prompt


# ── JSON 提取 ──


class TestJsonExtraction:
    """从 LLM 原始输出提取 JSON。"""

    def test_extracts_plain_json(self):
        assert _extract_json('{"a":1}') == '{"a":1}'

    def test_extracts_from_code_block(self):
        raw = '```json\n{"summary":"test"}\n```'
        assert _extract_json(raw) == '{"summary":"test"}'

    def test_extracts_from_non_json_block(self):
        raw = '```\n{"x":1}\n```'
        assert _extract_json(raw) == '{"x":1}'

    def test_extracts_nested_json(self):
        raw = 'Some text\n{"ranked_causes":[{"cause_id":"c1"}]}\nMore text'
        result = _extract_json(raw)
        assert result is not None
        assert "ranked_causes" in result

    def test_no_json_returns_none(self):
        assert _extract_json("just plain text no json") is None


# ── 校验与解析 ──


class TestValidationAndParsing:
    """LLM 输出校验 + 自修复。"""

    def test_valid_report_passes(self):
        evidence = EvidenceInput(
            task_metadata={"duration_sec": 15},
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}],
            baseline_diff={"cpu_percent_delta": 42.0},
        )
        raw = json.dumps({
            "summary": "CPU 热点在 fib",
            "ranked_causes": [{
                "cause_id": "cpu_hotspot_recursive",
                "confidence": 0.85,
                "claim": "fib 导致高 CPU",
                "evidence_refs": ["top_functions[0]", "baseline_diff"],
                "uncertainties": [],
                "verification_steps": ["加入缓存重测"],
            }],
            "facts": ["fib 占 68.5%"],
            "not_enough_evidence": False,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is not None
        assert issues == []
        assert report.summary == "CPU 热点在 fib"

    def test_rejects_cause_outside_analysis_boundary(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}],
            analysis_result={
                "allowed_cause_ids": ["cpu_hotspot_recursive"],
                "conclusion_boundary": {"can_claim_root_cause": True},
                "primary_cause_id": "cpu_hotspot_recursive",
                "stability_score": 0.91,
                "primary_cause_reason": "function 层级更深、支持事实更多",
                "symptoms": [],
                "localizations": [],
                "attributions": [],
                "evidence_challenges": [],
            },
        )
        raw = json.dumps({
            "summary": "错误归因",
            "ranked_causes": [{
                "cause_id": "io_wait_high",
                "confidence": 0.8,
                "claim": "IO 等待是根因",
                "evidence_refs": ["top_functions[0]"],
                "uncertainties": [],
                "verification_steps": [],
            }],
            "facts": ["fib 占 68.5%"],
            "not_enough_evidence": False,
        })

        report, issues = _validate_and_parse(raw, evidence)

        assert report is None
        assert any("不在结构化分析允许的原因集合" in issue for issue in issues)

    def test_analysis_result_is_attached_to_report(self):
        evidence = EvidenceInput(
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}],
            analysis_result={
                "allowed_cause_ids": ["cpu_hotspot_recursive"],
                "conclusion_boundary": {"can_claim_root_cause": True},
                "primary_cause_id": "cpu_hotspot_recursive",
                "stability_score": 0.91,
                "primary_cause_reason": "function 层级更深、支持事实更多",
                "symptoms": [{"symptom_id": "sym_cpu_utilization_high", "symptom_type": "cpu_utilization_high", "severity": "high", "fact_ids": ["fact_cpu_user_high"]}],
                "localizations": [{"level": "function", "target": "fib", "fact_ids": ["fact_top_function_0", "fact_cpu_user_high"]}],
                "attributions": [{"candidate_id": "cpu_hotspot_recursive", "status": "supported", "supporting_fact_ids": ["fact_cpu_user_high", "fact_top_function_0"], "opposing_fact_ids": [], "missing_evidence": [], "max_supported_level": "function"}],
                "evidence_challenges": [{"candidate_id": "cpu_hotspot_recursive", "critical_fact_ids": ["fact_cpu_user_high", "fact_top_function_0"], "critical_fact_groups": [["fact_cpu_user_high", "fact_top_function_0"]], "tests": [], "conclusion_stability": "stable"}],
            },
        )
        raw = json.dumps({
            "summary": "CPU 热点",
            "ranked_causes": [{
                "cause_id": "cpu_hotspot_recursive",
                "confidence": 0.85,
                "claim": "fib 导致高 CPU",
                "evidence_refs": ["top_functions[0]"],
                "uncertainties": [],
                "verification_steps": [],
            }],
            "facts": ["fib 占 68.5%"],
            "not_enough_evidence": False,
        })

        report, issues = _validate_and_parse(raw, evidence)

        assert report is not None
        assert issues == []
        assert report.analysis_result is not None
        assert report.primary_cause_id == "cpu_hotspot_recursive"
        assert report.stability_score == 0.91
        assert report.conclusion_boundary is not None
        assert report.ai_tree == []
        assert report.graph_entities == []
        assert report.graph_links == []
        assert report.graph_extension_points == []

    def test_requires_insufficient_flag_when_analysis_forbids_root_cause(self):
        evidence = EvidenceInput(
            analysis_result={
                "allowed_cause_ids": [],
                "conclusion_boundary": {"can_claim_root_cause": False},
            },
        )
        raw = json.dumps({
            "summary": "仍然给出结论",
            "ranked_causes": [],
            "facts": [],
            "not_enough_evidence": False,
        })

        report, issues = _validate_and_parse(raw, evidence)

        assert report is None
        assert any("必须标记为证据不足" in issue for issue in issues)

    def test_bad_evidence_ref_rejected(self):
        evidence = EvidenceInput()
        raw = json.dumps({
            "summary": "test",
            "ranked_causes": [{
                "cause_id": "x",
                "confidence": 0.5,
                "claim": "bad",
                "evidence_refs": ["nonexistent_field"],
                "uncertainties": [],
                "verification_steps": [],
            }],
            "facts": [],
            "not_enough_evidence": False,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is None
        assert len(issues) > 0
        assert "evidence_refs" in issues[0]

    def test_missing_required_fields_rejected(self):
        evidence = EvidenceInput()
        raw = '{"summary":"x"}'
        report, issues = _validate_and_parse(raw, evidence)
        assert report is None
        assert len(issues) > 0

    def test_invalid_json_rejected(self):
        evidence = EvidenceInput()
        report, issues = _validate_and_parse("not json", evidence)
        assert report is None

    def test_not_enough_evidence_without_causes_is_ok(self):
        evidence = EvidenceInput()
        raw = json.dumps({
            "summary": "insufficient data",
            "ranked_causes": [],
            "facts": ["few samples"],
            "not_enough_evidence": True,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is not None
        assert issues == []

    def test_empty_causes_without_flag_fails(self):
        evidence = EvidenceInput()
        raw = json.dumps({
            "summary": "empty",
            "ranked_causes": [],
            "facts": [],
            "not_enough_evidence": False,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is None

    def test_ref_exists_in_evidence(self):
        paths = {"top_functions": {"name", "samples", "percent"}}
        assert _ref_exists("top_functions[0]", paths) is True
        assert _ref_exists("top_functions", paths) is True
        assert _ref_exists("nonexistent", paths) is False

    def test_tool_result_ref_is_valid(self):
        evidence = EvidenceInput(
            tool_results=[{
                "tool_name": "get_flamegraph_top",
                "status": "success",
                "evidence_ref": "tool_results.get_flamegraph_top",
                "output": {},
            }]
        )
        raw = json.dumps({
            "summary": "tool evidence",
            "ranked_causes": [{
                "cause_id": "cpu_hotspot_recursive",
                "confidence": 0.7,
                "claim": "hotspot",
                "evidence_refs": ["tool_results.get_flamegraph_top"],
                "uncertainties": [],
                "verification_steps": [],
            }],
            "facts": ["tool ok"],
            "not_enough_evidence": False,
        })
        report, issues = _validate_and_parse(raw, evidence)
        assert report is not None
        assert issues == []


# ── 工具证据与修复计划 ──


class _StubRepo:
    def __init__(self):
        self.created_payloads = []

    def create_task(self, payload):
        self.created_payloads.append(payload)

        class _Task:
            id = "task_followup"

        return _Task()


class TestToolAndRepairFlow:
    """工具证据链和 safe_auto 修复动作。"""

    def test_tool_results_are_structured_evidence(self):
        tools = run_rca_tools(
            task_record=_StubTask(),
            top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
        )
        flame_tool = next(item for item in tools if item.tool_name == "get_flamegraph_top")
        assert flame_tool.status == "success"
        assert flame_tool.evidence_ref == "tool_results.get_flamegraph_top"

    def test_context_builds_and_executes_safe_followup(self):
        stub_repo = _StubRepo()
        with mock.patch.dict("os.environ", {}, clear=True):
            outcome = run_diagnosis_context(
                task_id="task_test",
                task_record=_StubTask(),
                top_functions=[{"name": "fib_hotspot", "samples": 100, "percent": 68.5}],
                repo=stub_repo,
            )
        assert outcome.report.report.ranked_causes[0].cause_id == "cpu_hotspot_recursive"
        assert outcome.repair_plan is not None
        assert outcome.repair_plan.status == "safe_actions_executed"
        assert stub_repo.created_payloads[0].collector_type == "pyspy"


# ── 自修复重试 ──


class TestSelfRepair:
    """LLM 调用失败后的自修复重试。"""

    def test_retries_on_validation_failure(self):
        """首次返回 bad JSON → 重试返回 good JSON。"""
        evidence = EvidenceInput(
            task_metadata={"duration_sec": 15},
            top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}],
            baseline_diff={"cpu_percent_delta": 42.0},
        )
        good_json = json.dumps({
            "summary": "after repair",
            "ranked_causes": [{
                "cause_id": "cpu_hotspot_recursive",
                "confidence": 0.80,
                "claim": "fixed",
                "evidence_refs": ["top_functions[0]"],
                "uncertainties": [],
                "verification_steps": ["step"],
            }],
            "facts": ["f1"],
            "not_enough_evidence": False,
        })

        # mock: 第一次返回坏 JSON → 第二次返回好 JSON
        mock_resp_bad = mock.MagicMock(status_code=200)
        mock_resp_bad.json.return_value = {
            "choices": [{"message": {"content": '{"summary":"bad","ranked_causes":[]}'}}]
        }
        mock_resp_good = mock.MagicMock(status_code=200)
        mock_resp_good.json.return_value = {
            "choices": [{"message": {"content": good_json}}]
        }

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", side_effect=[mock_resp_bad, mock_resp_good]):
                from server.app.rca.llm_client import diagnose
                result = diagnose(
                    task_id="t1",
                    evidence=evidence,
                    candidates_json='[{"candidate_id":"cpu_hotspot_recursive"}]',
                )
                assert result.validated is True
                assert result.retry_count == 1
                assert result.report.summary == "after repair"

    def test_max_retries_exceeded_returns_failure(self):
        evidence = EvidenceInput()
        bad_json = '{"summary":"x","ranked_causes":[],"facts":[],"not_enough_evidence":false}'

        mock_resp = mock.MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": bad_json}}]
        }

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
            with mock.patch("server.app.rca.llm_client.chat_completions", return_value=mock_resp):
                from server.app.rca.llm_client import diagnose
                result = diagnose(
                    task_id="t1",
                    evidence=evidence,
                    candidates_json="[]",
                )
                assert result.validated is False
                assert result.retry_count >= 1


# ── 降级行为 ──


class TestFallback:
    """API Key 未配置时的降级。"""

    def test_no_api_key_returns_rule_only(self):
        evidence = EvidenceInput(task_metadata={"collector_type": "perf_cpu"})
        with mock.patch.dict("os.environ", {}, clear=True):
            from server.app.rca.llm_client import diagnose
            result = diagnose(task_id="t1", evidence=evidence, candidates_json="[]")
            assert result.model_name == "rule-engine-only"
            assert result.report.not_enough_evidence is True

    def test_run_diagnosis_with_no_key(self):
        task = _StubTask()
        with mock.patch.dict("os.environ", {}, clear=True):
            result = run_diagnosis(task_id="t1", task_record=task,
                                   top_functions=[{"name": "fib", "samples": 100, "percent": 68.5}])
            assert result is not None
            assert result.model_name == "rule-engine-only"

    def test_run_diagnosis_does_not_promote_cpu_signal_without_hotspot(self):
        task = _StubTask()
        sys_metrics = {
            "sample_count": 10,
            "summary": {
                "avg_cpu_user_pct": 92.0,
                "avg_cpu_sys_pct": 5.0,
                "avg_cpu_iowait_pct": 1.0,
                "load1m": 8.0,
                "thread_count": 20,
                "thread_trend": "stable",
                "fd_count": 20,
                "fd_trend": "stable",
                "fd_max": 25,
                "vmrss_mb": 200,
                "vmrss_mb_max": 210,
                "ctx_nonvoluntary_rate": 10,
                "net_rx_kbps": 10,
                "net_tx_kbps": 10,
            },
        }
        with mock.patch.dict("os.environ", {}, clear=True):
            result = run_diagnosis(
                task_id="t1",
                task_record=task,
                sys_metrics=sys_metrics,
            )
        assert result.report.ranked_causes == []
        assert result.report.not_enough_evidence is True
