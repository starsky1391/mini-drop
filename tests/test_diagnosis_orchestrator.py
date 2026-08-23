"""AI 集群诊断会话、探针审批、预算和证据链测试。"""

import json
from pathlib import Path

import pytest
from datetime import timedelta
from fastapi.testclient import TestClient

from server.app.common_utils import json_safe
from server.app.database import init_db, reset_engine
from server.app.diagnosis.audit_bundle import _structured_evidence, build_readiness_gate
from server.app.diagnosis.benchmark_score import aggregate_results, score_audit_bundle
from server.app.diagnosis import orchestrator as orchestrator_module
from server.app.main import app, repo
from server.app.main import diagnosis_orchestrator
from server.app.models import Base
from server.app.rca.models import AITreeCandidateNode, AITreeLayer, RootCauseCluster
from server.app.state_machine import Actor, TaskStatus


@pytest.fixture(autouse=True)
def _reset_repo(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MINI_DROP_AI_ENABLED", "none")
    monkeypatch.delenv("MINI_DROP_ALLOWED_SERVICES", raising=False)
    monkeypatch.delenv("MINI_DROP_API_AUTH_ENABLED", raising=False)
    reset_engine()
    init_db()
    repo._task_queues.clear()
    repo.agent_metrics.clear()
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu", "ebpf_io", "memory_smaps"],
    )
    yield
    from server.app.database import _get_engine
    Base.metadata.drop_all(bind=_get_engine())
    reset_engine()


@pytest.fixture(name="client")
def client_fixture():
    return TestClient(app)


def test_audit_bundle_json_safe_replaces_non_finite_floats():
    assert json_safe({"ok": 1.0, "bad": float("nan"), "items": [float("inf")]}) == {
        "ok": 1.0,
        "bad": None,
        "items": [None],
    }


def test_readiness_gate_tolerates_truncated_runtime_counts():
    bundle = {
        "probes": [{
            "probe_id": "process_off_cpu_profile",
            "task_id": "task-1",
            "status": "COMPLETED",
        }],
        "evidence": [{
            "observed_value": {
                "summary": {
                    "evidence_index": {
                        "off_cpu_wait": {
                            "summary": {"sample_count": "[TRUNCATED]"},
                            "top_wait_stacks": [],
                        },
                    },
                },
            },
        }],
        "latest_conclusion": {
            "controlled_ai_tree": {
                "layers": [{"generated_by": "ai_guarded"}],
            },
            "root_cause_candidates": [{"evidence_refs": ["e1"]}],
        },
        "child_task_ids": ["task-1"],
        "artifacts": [{"collector_family": "off_cpu_wait_profile"}],
        "structured_evidence": [{"artifact_type": "off_cpu_wait_json"}],
        "runtime_trace": [{"stage": "test"}],
    }

    gate = build_readiness_gate(bundle)

    assert gate["status"] == "FAIL"
    assert any(check["name"] == "runtime_stack_quality_non_empty" for check in gate["checks"])


def test_werkzeug_blocked_mechanism_regression_fixture_preserves_parent_boundary():
    fixture_path = Path(__file__).parent / "fixtures" / "diagnosis" / "werkzeug_1521_blocked_mechanism_session.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert fixture["probe_input"]["ai_generated_query"] is None
    assert fixture["probe_result"]["status"] == "blocked"
    assert fixture["parent_candidate"]["evidence_refs"] == ["ev-memray", "ev-source"]
    assert fixture["parent_candidate"]["conclusion_eligible"] is False
    assert fixture["invalid_previous_conclusion"] == {
        "abstained": True,
        "confidence_level": "高",
        "root_cause_candidates": [],
        "possible_root_causes": [],
    }


def test_source_snapshot_prefers_agent_visible_host_source_path():
    source_context = {
        "source_paths": [
            "/case/src",
            "/home/worker1/mini-drop-real-cases/werkzeug-1521/src",
        ],
        "container_workdir": "/case",
        "repo_revision": "a220671d",
    }

    options = diagnosis_orchestrator._collector_probe_parameters(
        "process_source_snapshot",
        {"source_context": source_context},
        {"pid": 1234, "service_id": "werkzeug-routing", "instance_id": "vulnerable"},
    )

    assert options["source_root"] == "/home/worker1/mini-drop-real-cases/werkzeug-1521/src"
    assert options["source_revision"] == "a220671d"


def test_python_heap_official_report_paths_are_forwarded_from_source_context():
    source_context = {
        "source_paths": ["/home/worker1/mini-drop-real-cases/werkzeug-1521/src"],
        "memray_result_path": "/var/lib/mini-drop/profiles/werkzeug.bin",
        "memray_stats_path": "/var/lib/mini-drop/profiles/werkzeug-stats.json",
        "memray_leaks_path": "/var/lib/mini-drop/profiles/werkzeug-leaks.csv",
    }

    options = diagnosis_orchestrator._collector_probe_parameters(
        "process_python_heap_profile",
        {"source_context": source_context},
        {"pid": 1234, "service_id": "werkzeug-routing", "instance_id": "vulnerable"},
    )

    assert options["instrumented_result"].endswith("werkzeug.bin")
    assert options["memray_stats_path"].endswith("werkzeug-stats.json")
    assert options["memray_leaks_path"].endswith("werkzeug-leaks.csv")


def test_memory_followup_sequence_overrides_generic_trace_requests():
    requests = orchestrator_module._merge_assessment_followups(
        ["off_cpu_wait_profile", "trace_endpoint_profile"],
        ["python_heap_profile"],
        sufficient_dependency=False,
    )

    assert requests == ["python_heap_profile"]


def _payload(query: str = "服务 service-a CPU 飙高，请定位原因") -> dict:
    return {
        "query": query,
        "context": {
            "service_id": "service-a",
            "environment": "production",
            "instances": [{
                "service_id": "service-a",
                "instance_id": "service-a-1",
                "host_id": "host-1",
                "agent_id": "a1",
                "pid": 1234,
                "environment": "production",
            }],
        },
        "budget_profile": "production_safe",
    }


def test_remote_artifact_falls_back_to_object_storage_when_agent_path_is_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MINI_DROP_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        orchestrator_module.storage,
        "read_object_bytes",
        lambda bucket, key: b'{"avg_cpu_user_pct": 88.0}',
    )

    value = diagnosis_orchestrator._read_artifact_json({
        "artifact_type": "sys_metrics",
        "bucket": "mini-drop",
        "object_key": "tasks/task-remote/sys_metrics.json",
        "local_path": str(tmp_path / "worker-only" / "sys_metrics.json"),
    })

    assert value == {"avg_cpu_user_pct": 88.0}


def test_truncated_metadata_falls_back_to_raw_artifact_content(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_DROP_ARTIFACT_ROOT", str(tmp_path))
    raw = {
        "events": [{
            "action": {"operation": "signal", "signal": "SIGSTOP"},
            "target": {"pid": 1234},
            "effect": {"expected_state": "T"},
        }],
        "evidence_validity": {"evidence_status": "valid"},
    }
    monkeypatch.setattr(
        orchestrator_module.storage,
        "read_object_bytes",
        lambda bucket, key: json.dumps(raw).encode("utf-8"),
    )

    value = diagnosis_orchestrator._read_artifact_json({
        "artifact_type": "runtime_control_event_json",
        "bucket": "mini-drop",
        "object_key": "tasks/task/runtime_control_events.json",
        "local_path": str(tmp_path / "missing" / "runtime_control_events.json"),
        "metadata": {
            "data": {
                "events": [{
                    "action": {"signal": "[TRUNCATED]"},
                    "target": {"pid": "[TRUNCATED]"},
                    "effect": {"expected_state": "[TRUNCATED]"},
                }],
            },
        },
    })

    assert value == raw


def test_existing_evidence_without_cohort_is_not_reused(client: TestClient):
    task_id = client.post("/api/tasks", json={
        "name": "reusable-sys-metrics",
        "agent_id": "a1",
        "target_pid": 1234,
        "collector_type": "sys_metrics",
        "duration_sec": 5,
    }).json()["data"]["task_id"]
    summary = _normal_summary()
    summary["avg_cpu_user_pct"] = 92.0
    _finish_sys_metrics_task(task_id, summary)

    response = client.post("/api/v1/diagnoses", json=_payload())

    assert response.status_code == 200
    detail = response.json()["data"]
    assert detail["status"] in {"COLLECTING", "WAITING_APPROVAL"}
    assert task_id not in detail["child_task_ids"]
    assert all(
        repo.tasks[child_id].request_params["options"].get("evidence_cohort_id") == detail["diagnosis_id"]
        for child_id in detail["child_task_ids"]
    )


def test_diagnosis_audit_bundle_exports_runtime_trace_and_readiness_gate(client: TestClient):
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    task_id = data["child_task_ids"][0]
    summary = _normal_summary()
    summary["avg_cpu_user_pct"] = 92.0
    _finish_sys_metrics_task(task_id, summary)

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    response = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}/audit-bundle")

    assert response.status_code == 200
    bundle = response.json()["data"]
    assert bundle["diagnosis_id"] == data["diagnosis_id"]
    assert bundle["runtime_trace"]
    assert bundle["probes"]
    assert bundle["child_task_ids"] == detail["child_task_ids"]
    assert bundle["readiness_gate"]["status"] in {"PASS", "FAIL"}
    assert "scope" in {item["stage"] for item in bundle["runtime_trace"]}
    assert any(check["name"] == "structured_evidence_non_empty" for check in bundle["readiness_gate"]["checks"])
    assert any(
        check["name"] == "required_collector_family_has_structured_artifact"
        for check in bundle["readiness_gate"]["checks"]
    )


def test_repeated_analysis_records_one_guard_event_per_child_task(client: TestClient):
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    task_id = data["child_task_ids"][0]
    _finish_sys_metrics_task(task_id, _normal_summary())

    task = repo.tasks[task_id]
    diagnosis_orchestrator._analyze_tasks(data["diagnosis_id"], [task])
    diagnosis_orchestrator._analyze_tasks(data["diagnosis_id"], [task])
    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    guard_events = [
        event
        for event in detail["events"]
        if event["event_type"] == "controlled_ai_tree_guard_result"
        and event["payload"].get("task_id") == task_id
    ]

    assert len(guard_events) == 1


def test_diagnosis_child_task_skips_legacy_single_task_rca(client: TestClient):
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    task_id = data["child_task_ids"][0]
    _finish_sys_metrics_task(task_id, _normal_summary())

    detail = client.get(f"/api/tasks/{task_id}").json()["data"]
    forced = client.post(f"/api/tasks/{task_id}/diagnose").json()["data"]
    history = client.get(f"/api/tasks/{task_id}/diagnoses").json()["data"]

    assert detail["latest_analysis"]["status"] == "SKIPPED"
    assert detail["latest_analysis"]["analysis_pipeline"] == "diagnosis_session_child_task"
    assert detail["latest_analysis"]["diagnosis_id"] == data["diagnosis_id"]
    assert forced["status"] == "SKIPPED"
    assert forced["report"]["mode"] == "collection_only"
    assert history == []


def test_diagnosis_session_can_be_deleted_without_deleting_child_task(client: TestClient):
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    child_task_id = data["child_task_ids"][0]
    diagnosis_orchestrator.store.update_session(diagnosis_id, status="FAILED")

    response = client.delete(f"/api/v1/diagnoses/{diagnosis_id}")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "diagnosis_id": diagnosis_id,
        "deleted": True,
    }
    assert client.get(f"/api/v1/diagnoses/{diagnosis_id}").status_code == 404
    assert client.get(f"/api/tasks/{child_task_id}").status_code == 200


def test_child_task_tree_remains_fallback_when_session_ai_does_not_succeed(client: TestClient, monkeypatch):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[
            "sys_metrics",
            "redis_check",
        ],
    )
    repo.agents["a1"].capabilities = ["sys_metrics", "redis_check"]

    monkeypatch.setattr(orchestrator_module, "generate_session_conclusion_review", lambda **kwargs: {
        "ai_review_status": "failed",
        "ai_review_scope": "session",
        "ai_review_attempts": 3,
        "ai_review_model": "test-model",
        "ai_review_error": "invalid structured response",
        "review": None,
    })
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    task_id = data["child_task_ids"][0]
    summary = _normal_summary()
    summary["avg_cpu_user_pct"] = 92.0
    _finish_sys_metrics_task(task_id, summary)

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]

    conclusion = detail["latest_conclusion"]
    assert conclusion["ai_review_status"] == "failed"
    assert detail["budget_used"]["model_calls"] == 3
    assert all(
        layer["generated_by"] == "analyzer_fallback"
        for layer in conclusion["controlled_ai_tree"]["layers"]
    )


def test_successful_candidate_generation_keeps_analyzer_observation_distinct_from_fallback():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag_ai_candidate_source",
        cluster_assessment={
            "classification": "self_code_or_process_pressure",
            "summary": "目标进程存在异常压力，仍需候选调查。",
            "supported_level": "process",
            "confidence": 0.35,
            "evidence_refs": ["ev-rss"],
            "conclusion_eligible": False,
        },
        candidates=[],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )

    assert tree is not None
    assert all(layer.generated_by == "analyzer_observation" for layer in tree.layers)

    review = {
        "ai_review_status": "succeeded",
        "active_candidate_ids": ["ai_candidate_runtime_path"],
        "candidate_proposals": [{
            "candidate_id": "ai_candidate_runtime_path",
            "claim": "运行时任务路径需要进一步验证。",
            "mechanism": "runtime_task_path",
            "target": "celery-worker",
            "supported_level": "process",
            "decision": "needs_more_evidence",
            "causal_status": "needs_more_evidence",
            "role": "unknown",
            "parent_candidate_ids": ["self_code_or_process_pressure"],
            "origin_parent_candidate_id": "self_code_or_process_pressure",
            "evidence_refs": ["ev-rss"],
            "missing_evidence": ["python_runtime_profile"],
        }],
    }

    updated = orchestrator_module._apply_candidate_review(tree, review)

    assert not any(layer.generated_by == "analyzer_fallback" for layer in updated.layers)
    assert updated.layers[-1].generated_by == "ai_candidate"
    assert updated.retained_candidate_id == "ai_candidate_runtime_path"
    ai_node = next(
        node
        for node in updated.layers[-1].unknown_causes
        if node.candidate_id == "ai_candidate_runtime_path"
    )
    assert ai_node.generated_by == "ai_candidate"


def test_initial_ai_candidate_stays_investigation_only_even_if_model_says_conclude():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag_initial_candidate_gate",
        cluster_assessment={
            "classification": "self_code_or_process_pressure",
            "summary": "目标进程存在异常压力，仍需候选调查。",
            "supported_level": "process",
            "confidence": 0.35,
            "evidence_refs": ["ev-rss"],
            "conclusion_eligible": False,
        },
        candidates=[],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )

    updated = orchestrator_module._apply_candidate_review(tree, {
        "ai_review_status": "succeeded",
        "active_candidate_ids": ["ai_candidate_premature"],
        "candidate_proposals": [{
            "candidate_id": "ai_candidate_premature",
            "claim": "模型认为该路径可能解释异常。",
            "mechanism": "premature_mechanism",
            "target": "worker",
            "supported_level": "process",
            "decision": "conclude",
            "causal_status": "supported",
            "role": "primary",
            "parent_candidate_ids": ["self_code_or_process_pressure"],
            "origin_parent_candidate_id": "self_code_or_process_pressure",
            "evidence_refs": ["ev-rss"],
        }],
    })

    node = next(
        node
        for layer in updated.layers
        for node in layer.unknown_causes + layer.primary_causes + layer.secondary_causes
        if node.candidate_id == "ai_candidate_premature"
    )
    assert node.status == "missing_evidence"
    assert node.causal_status == "unproven"
    assert node.decision == "continue_probe"
    assert node.conclusion_eligible is False
    assert updated.retained_candidate_id == "ai_candidate_premature"


def test_candidate_tree_ingestion_records_deferred_and_missing_parent_candidates():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag_candidate_ingestion",
        cluster_assessment={
            "classification": "self_code_or_process_pressure",
            "summary": "目标进程存在异常压力，仍需候选调查。",
            "supported_level": "process",
            "confidence": 0.35,
            "evidence_refs": ["ev-rss"],
            "conclusion_eligible": False,
        },
        candidates=[],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )
    coarse_id = tree.emitted_coarse_ids[0]
    review = {
        "ai_review_status": "succeeded",
        "active_candidate_ids": ["ai_candidate_active"],
        "deferred_candidate_ids": ["ai_candidate_deferred"],
        "candidate_proposals": [
            {
                "candidate_id": "ai_candidate_active",
                "claim": "active 候选",
                "mechanism": "runtime_path",
                "target": "worker",
                "supported_level": "process",
                "role": "unknown",
                "parent_candidate_ids": ["coarse_insufficient_evidence"],
                "origin_parent_candidate_id": "coarse_insufficient_evidence",
                "evidence_refs": ["ev-rss"],
            },
            {
                "candidate_id": "ai_candidate_deferred",
                "claim": "deferred 候选",
                "mechanism": "broker_path",
                "target": "broker",
                "supported_level": "service",
                "role": "unknown",
                "parent_candidate_ids": [coarse_id],
                "origin_parent_candidate_id": coarse_id,
                "evidence_refs": ["ev-rss"],
            },
            {
                "candidate_id": "ai_candidate_missing_parent",
                "claim": "无父节点候选",
                "mechanism": "unknown_path",
                "target": "worker",
                "supported_level": "process",
                "role": "unknown",
                "parent_candidate_ids": ["missing-parent"],
                "origin_parent_candidate_id": "missing-parent",
                "evidence_refs": ["ev-rss"],
            },
        ],
    }

    updated = orchestrator_module._apply_candidate_review(tree, review)

    nodes = {
        node.candidate_id
        for layer in updated.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    assert "ai_candidate_active" in nodes
    assert "ai_candidate_deferred" not in nodes


def test_candidate_tree_ingestion_respects_explicit_empty_active_selection():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag_empty_active_selection",
        cluster_assessment={
            "classification": "self_code_or_process_pressure",
            "summary": "目标进程存在异常压力，仍需候选调查。",
            "supported_level": "process",
            "confidence": 0.35,
            "evidence_refs": ["ev-rss"],
            "conclusion_eligible": False,
        },
        candidates=[],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )
    coarse_id = tree.emitted_coarse_ids[0]
    review = {
        "ai_review_status": "succeeded",
        "active_candidate_ids": [],
        "candidate_proposals": [{
            "candidate_id": "ai_candidate_deferred_only",
            "claim": "保留为审计方向，不在本轮深探。",
            "mechanism": "unselected_path",
            "target": "worker",
            "supported_level": "process",
            "role": "unknown",
            "parent_candidate_ids": [coarse_id],
            "origin_parent_candidate_id": coarse_id,
            "evidence_refs": ["ev-rss"],
        }],
    }

    updated = orchestrator_module._apply_candidate_review(tree, review)

    nodes = {
        node.candidate_id
        for layer in updated.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    assert "ai_candidate_deferred_only" not in nodes
    assert review["tree_ingestion_diagnostics"][0]["failure_code"] == "candidate_deferred_from_tree"
    diagnostics = review["tree_ingestion_diagnostics"]
    assert {
        item["failure_code"]
        for item in diagnostics
    } == {"candidate_deferred_from_tree"}


def test_verified_line_rewrites_emitted_parent_and_attaches_runtime_observations():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag_line_parent_repair",
        cluster_assessment={
            "classification": "python_memory_retention",
            "summary": "worker 存在持续内存压力。",
            "supported_level": "line",
            "max_supported_level": "line",
            "confidence": 0.42,
            "evidence_refs": ["ev-rss", "ev-stack"],
            "conclusion_eligible": False,
            "primary_anchor": {
                "source_context_hash": "sha256:source",
                "source_revision": "rev-1",
                "file": "/opt/celery-src/celery/app/trace.py",
                "line": 651,
                "supported_level": "line",
                "evidence_refs": ["ev-source"],
            },
        },
        candidates=[
            {
                "candidate_id": "service-runtime",
                "description": "worker 服务级运行时压力。",
                "root_entity": "celery-worker",
                "max_supported_level": "service",
                "confidence_level": "中",
                "rank": 1,
                "evidence_refs": ["ev-rss"],
                "relation": "alternative",
            },
            {
                "candidate_id": "line-existing",
                "description": "异常处理行 /opt/celery-src/celery/app/trace.py:651 需要继续验证。",
                "root_entity": "celery-worker",
                "max_supported_level": "line",
                "confidence_level": "低",
                "rank": 2,
                "evidence_refs": ["ev-stack"],
                "target": "/opt/celery-src/celery/app/trace.py:651",
                "relation": "refinement",
            },
            {
                "candidate_id": "python_runtime_stack_hotspot",
                "description": "运行时栈热点观察。",
                "root_entity": "celery-worker",
                "max_supported_level": "process",
                "confidence_level": "低",
                "rank": 3,
                "evidence_refs": ["ev-stack"],
                "relation": "alternative",
            },
            {
                "candidate_id": "python_userland_hotspot",
                "description": "用户态热点观察。",
                "root_entity": "celery-worker",
                "max_supported_level": "process",
                "confidence_level": "低",
                "rank": 4,
                "evidence_refs": ["ev-stack"],
                "relation": "alternative",
            },
        ],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )

    assert tree is not None
    nodes = {
        node.candidate_id: node
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    coarse_id = tree.emitted_coarse_ids[0]
    assert nodes["line-existing"].parent_candidate_ids == [coarse_id]
    assert nodes["line-existing"].origin_parent_candidate_id == coarse_id
    assert any(
        layer.layer_id.endswith("line_refinement")
        and any(node.candidate_id == "line-existing" for node in layer.unknown_causes)
        for layer in tree.layers
    )
    assert nodes["python_runtime_stack_hotspot"].parent_candidate_ids == ["line-existing"]
    assert nodes["python_userland_hotspot"].parent_candidate_ids == ["line-existing"]


def test_readiness_gate_fails_completed_probe_without_structured_family_artifact():
    bundle = {
        "runtime_trace": [{"stage": "evidence"}],
        "probes": [{
            "probe_id": "process_dependency_check",
            "task_id": "task_dependency",
            "status": "COMPLETED",
        }],
        "child_task_ids": ["task_dependency"],
        "tasks": [{"id": "task_dependency", "collector_type": "dependency_check"}],
        "artifacts": [{"task_id": "task_dependency", "artifact_type": "raw"}],
        "structured_evidence": {"version": 1},
        "evidence_refs": ["ev_1"],
    }

    gate = build_readiness_gate(bundle)
    check = next(item for item in gate["checks"] if item["name"] == "required_collector_family_has_structured_artifact")

    assert gate["status"] == "FAIL"
    assert check["status"] == "FAIL"


def test_readiness_gate_accepts_controlled_tree_from_normalized_conclusion():
    bundle = {
        "runtime_trace": [{"stage": "conclusion"}],
        "probes": [{
            "probe_id": "process_dependency_check",
            "task_id": "task_dependency",
            "status": "COMPLETED",
        }],
        "child_task_ids": ["task_dependency"],
        "tasks": [{"id": "task_dependency", "collector_type": "dependency_check"}],
        "artifacts": [{"task_id": "task_dependency", "artifact_type": "dependency_check_json"}],
        "structured_evidence": {"version": 1},
        "evidence_refs": ["ev_1"],
        "conclusion": {
            "ai_review_status": "succeeded",
            "ai_review_scope": "session",
            "controlled_ai_tree": {
                "layers": [{"generated_by": "ai_guarded"}],
                "probe_edges": [{"edge_id": "edge_1"}],
            },
        },
    }

    gate = build_readiness_gate(bundle)
    checks = {item["name"]: item["status"] for item in gate["checks"]}

    assert checks["controlled_ai_tree_present"] == "PASS"
    assert checks["controlled_ai_tree_ai_guarded"] == "PASS"


def test_readiness_gate_rejects_ai_guarded_label_when_session_review_failed():
    bundle = {
        "latest_conclusion": {
            "ai_review_status": "failed",
            "ai_review_scope": "session",
            "controlled_ai_tree": {"layers": [{"generated_by": "ai_guarded"}]},
        },
    }

    gate = build_readiness_gate(bundle)
    checks = {item["name"]: item["status"] for item in gate["checks"]}

    assert checks["controlled_ai_tree_ai_guarded"] == "FAIL"


def test_compound_readiness_rejects_clusters_without_independent_evidence():
    bundle = {
        "latest_conclusion": {
            "ai_review_status": "succeeded",
            "ai_review_scope": "session",
            "ai_review_attempts": 1,
            "cluster_assessment": {"classification": "compound_incident"},
            "root_cause_clusters": [
                {
                    "mechanism": "downstream_dependency_failure",
                    "target": "paymentservice",
                    "conclusion_eligible": True,
                    "evidence_refs": ["ev-shared"],
                },
                {
                    "mechanism": "same_host_cpu_contention",
                    "target": "noise-generator",
                    "conclusion_eligible": True,
                    "evidence_refs": ["ev-shared"],
                },
            ],
            "controlled_ai_tree": {"layers": [{"generated_by": "ai_guarded"}]},
        },
    }

    gate = build_readiness_gate(bundle)
    checks = {item["name"]: item["status"] for item in gate["checks"]}

    assert checks["session_ai_review_succeeded"] == "PASS"
    assert checks["compound_clusters_evidence_backed"] == "FAIL"


def test_compound_readiness_uses_evidence_even_when_prediction_is_single_cause():
    bundle = {
        "artifacts": [
            {
                "artifact_type": "sys_metrics",
                "metadata": {
                    "avg_cpu_user_pct": 96.0,
                    "avg_cpu_sys_pct": 1.0,
                    "avg_host_cpu_busy_pct": 99.0,
                },
            },
            {
                "artifact_type": "dependency_check_json",
                "metadata": {"failed_dependencies": ["paymentservice"]},
            },
        ],
        "latest_conclusion": {
            "classification": "downstream_dependency",
            "root_cause_clusters": [{
                "mechanism": "downstream_dependency_failure",
                "target": "paymentservice",
                "conclusion_eligible": True,
                "evidence_refs": ["ev-dependency"],
            }],
        },
    }

    gate = build_readiness_gate(bundle)
    checks = {item["name"]: item["status"] for item in gate["checks"]}

    assert checks["compound_clusters_evidence_backed"] == "FAIL"


def test_compound_scoring_requires_two_independently_evidenced_clusters():
    conclusion = {
        "classification": "compound_incident",
        "location_type": ["downstream", "same_host"],
        "domain_type": ["network", "cpu"],
        "ai_review_status": "succeeded",
        "ai_review_scope": "session",
        "root_cause_clusters": [{
            "mechanism": "downstream_dependency_failure",
            "target": "paymentservice",
            "conclusion_eligible": True,
            "explained_symptoms": ["checkout failed"],
            "evidence_refs": ["ev-shared"],
        }, {
            "mechanism": "same_host_cpu_contention",
            "target": "noise-generator",
            "conclusion_eligible": True,
            "explained_symptoms": ["checkout slow"],
            "evidence_refs": ["ev-shared"],
        }],
    }
    result = score_audit_bundle(
        {"diagnosis_id": "diag-compound", "conclusion": conclusion},
        {"case_id": "compound", "expected": {
            "classification": "compound_incident",
            "location_type": ["downstream", "same_host"],
            "domain_type": ["network", "cpu"],
        }},
    )

    assert result["dimensions"]["root_cause"]["exact_root_match"] is True
    assert result["dimensions"]["root_cause_clusters"]["compound_cluster_match"] is False
    assert result["exact_root_match"] is False


def test_compound_scoring_requires_all_expected_location_and_domain_values():
    result = score_audit_bundle(
        {"diagnosis_id": "diag-partial-compound", "conclusion": {
            "classification": "compound_incident",
            "location_type": ["same_host"],
            "domain_type": ["cpu"],
        }},
        {"case_id": "compound", "expected": {
            "classification": "compound_incident",
            "location_type": ["downstream", "same_host"],
            "domain_type": ["network", "cpu"],
        }},
    )

    root = result["dimensions"]["root_cause"]
    assert root["location_type"]["matched"] is False
    assert root["domain_type"]["matched"] is False
    assert root["exact_root_match"] is False


def test_downstream_runtime_control_keeps_downstream_location_and_network_impact():
    cluster = RootCauseCluster(
        cluster_id="runtime-downstream",
        mechanism="process_suspended",
        target="paymentservice",
        claim="paymentservice was paused",
        conclusion_eligible=True,
    )
    session = {"target_scope": {
        "downstream_service_ids": ["paymentservice"],
        "downstream_instance_ids": ["paymentservice-1"],
    }}

    fields = orchestrator_module._compound_location_fields([cluster], session)
    candidates = orchestrator_module._root_cause_cluster_candidates([cluster], session)

    assert fields["location_type"] == ["downstream"]
    assert fields["domain_type"] == ["runtime", "network"]
    assert candidates[0]["location_type"] == "downstream"
    assert candidates[0]["domain_type"] == "runtime"


def test_session_controlled_tree_keeps_unproven_function_localization_out_of_final_causes():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag_function_level",
        cluster_assessment={
            "classification": "self_code_or_process_pressure",
            "summary": "等待栈已收敛到 runtime.futex.abi0+35。",
            "supported_level": "function",
            "max_supported_level": "process",
            "confidence": 0.58,
            "evidence_refs": ["ev_wait"],
        },
        candidates=[{
            "candidate_id": "off_cpu_wait_hotspot",
            "rank": 1,
            "description": "Off-CPU 等待栈指向 runtime.futex.abi0+35。",
            "confidence_level": "中",
            "evidence_refs": ["ev_wait"],
        }],
        followup_requests=[],
        probes=[{
            "status": "COMPLETED",
            "parameters": {"evidence_gap": "off_cpu_wait_profile"},
            "evidence_refs": ["ev_wait"],
        }],
        child_trees=[{
            "layers": [{"generated_by": "ai_guarded"}],
        }],
    )

    assert tree is not None
    assert tree.final_supported_level == "function"
    node = next(
        item for layer in tree.layers for item in layer.unknown_causes
        if item.candidate_id == "off_cpu_wait_hotspot"
    )
    assert node.node_type == "orphan"
    assert node.supported_level == "function"
    assert node.conclusion_eligible is False
    assert tree.final_primary_causes == []


def test_session_controlled_tree_contains_rejected_unknown_and_blocked_branches():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag_branching_tree",
        cluster_assessment={
            "classification": "self_code_or_process_pressure",
            "summary": "off-CPU 等待栈已把问题收敛到 runtime.futex.abi0+35。",
            "supported_level": "function",
            "max_supported_level": "process",
            "confidence": 0.58,
            "evidence_refs": ["ev_wait", "ev_sys"],
            "primary_anchor": {
                "supported_level": "function",
                "anchor": "runtime.futex.abi0+35",
                "evidence_ref": "ev_wait",
                "blocked_upgrade_reason": "缺少锁持有者线程、业务调用栈或源码符号映射，不能直接升级到代码行。",
            },
            "alternative_hypotheses": [
                {
                    "hypothesis": "same_host_noisy_neighbor",
                    "status": "weakened",
                    "reason": "同宿主观测未显示更强资源压力。",
                    "supported_level": "host",
                    "evidence_refs": ["ev_sys"],
                },
                {
                    "hypothesis": "downstream_dependency",
                    "status": "missing_evidence",
                    "reason": "缺少下游依赖可达性和日志证据。",
                    "supported_level": "service",
                    "missing_evidence": ["dependency_check", "log_scan"],
                },
            ],
        },
        candidates=[{
            "candidate_id": "off_cpu_wait_hotspot",
            "rank": 1,
            "description": "Off-CPU 等待栈指向 runtime.futex.abi0+35。",
            "confidence_level": "中",
            "evidence_refs": ["ev_wait"],
        }],
        followup_requests=[],
        probes=[{
            "status": "COMPLETED",
            "parameters": {"evidence_gap": "off_cpu_wait_profile"},
            "evidence_refs": ["ev_wait"],
        }],
        child_trees=[{"layers": [{"generated_by": "ai_guarded"}]}],
    )

    assert tree is not None
    layer1 = tree.layers[1]
    assert layer1.primary_causes == []
    assert not any(node.candidate_id == "off_cpu_wait_hotspot" for node in layer1.unknown_causes)
    assert any(
        node.candidate_id == "off_cpu_wait_hotspot" and node.node_type == "orphan"
        for layer in tree.layers
        for node in layer.unknown_causes
    )
    assert any(node.candidate_id == "rejected_same_host_noisy_neighbor" for node in layer1.rejected_causes)
    assert any(node.candidate_id == "unknown_downstream_dependency" for node in layer1.unknown_causes)
    assert not any(edge.transition_type == "backtrack" for edge in tree.probe_edges)
    assert not any(
        node.candidate_id == "blocked_line_upgrade"
        for layer in tree.layers
        for node in layer.unknown_causes
    )
    assert all(
        node.origin_parent_candidate_id
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
        if node.depth_kind in {"mechanism", "boundary"}
    )


def test_session_tree_uses_local_stop_boundaries_without_truncating_branches():
    alternatives = [
        {
            "hypothesis": f"alt_{index}",
            "status": "missing_evidence",
            "reason": f"候选分支 {index} 缺少同窗证据。",
            "supported_level": "service",
            "missing_evidence": ["log_scan"],
        }
        for index in range(6)
    ]
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag_local_stop_forest",
        cluster_assessment={
            "classification": "process_memory_retention",
            "summary": "RSS 在目标 worker 进程内持续增长，GC 后仍保持。",
            "diagnostic_claim": "worker 进程内存保留是当前最细有效定位。",
            "claim_type": "direct_root_cause",
            "causal_status": "supported",
            "conclusion_eligible": True,
            "supported_level": "process",
            "max_supported_level": "process",
            "confidence": 0.82,
            "mechanism": "process_memory_retention",
            "claim_target": "celery-worker",
            "evidence_refs": ["ev_rss", "ev_barrier"],
            "alternative_hypotheses": alternatives,
        },
        candidates=[{
            "candidate_id": "process_worker_memory_growth",
            "rank": 1,
            "root_entity": "celery-worker",
            "description": "worker 进程 RSS 在同一 workload 第二批继续增长。",
            "confidence_level": "高",
            "evidence_refs": ["ev_rss"],
            "max_supported_level": "process",
        }],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )

    assert tree is not None
    assert tree.final_primary_causes == []
    assert tree.stop_source_candidate_ids == []
    all_nodes = [
        node
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    ]
    stop_node = next(node for node in all_nodes if node.candidate_id == "stop_boundary_process_worker_memory_growth")
    assert stop_node.node_type == "stop_boundary"
    assert stop_node.parent_candidate_ids == ["process_worker_memory_growth"]
    assert stop_node.origin_parent_candidate_id == "process_worker_memory_growth"
    assert stop_node.conclusion_eligible is False
    assert stop_node.candidate_id not in tree.final_primary_causes
    assert sum(1 for node in all_nodes if node.candidate_id.startswith("unknown_alt_")) == 6


def test_session_tree_places_runtime_observations_under_base_candidate():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag_observation_context",
        cluster_assessment={
            "classification": "self_code_or_process_pressure",
            "summary": "调用路径热点只支持局部定位。",
            "supported_level": "call_path",
            "confidence": 0.49,
            "evidence_refs": ["ev_stack", "ev_runtime"],
        },
        candidates=[
            {
                "candidate_id": "python_runtime_stack_hotspot",
                "rank": 1,
                "description": "Python 运行时采样产出非空调用栈。",
                "evidence_refs": ["ev_stack"],
                "max_supported_level": "call_path",
                "source_candidate_id": "celery-worker",
            },
            {
                "candidate_id": "python_userland_hotspot",
                "rank": 2,
                "description": "Python 用户态函数热点。",
                "evidence_refs": ["ev_runtime"],
                "max_supported_level": "call_path",
                "source_candidate_id": "celery-worker",
            },
            {
                "candidate_id": "celery-worker",
                "rank": 3,
                "description": "热点集中在 celery worker 的调用路径。",
                "evidence_refs": ["ev_stack"],
                "max_supported_level": "call_path",
            },
        ],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )

    assert tree is not None
    layer1 = tree.layers[1]
    assert [node.candidate_id for node in layer1.unknown_causes] == ["celery-worker"]
    observation_layer = next(layer for layer in tree.layers if layer.layer_id == "session_layer_2_observation_context")
    observations = {node.candidate_id: node for node in observation_layer.unknown_causes}
    assert observations["python_runtime_stack_hotspot"].node_type == "observation"
    assert observations["python_runtime_stack_hotspot"].parent_candidate_ids == ["celery-worker"]
    assert observations["python_userland_hotspot"].parent_candidate_ids == ["celery-worker"]
    stop_node = next(
        node for layer in tree.layers for node in layer.unknown_causes
        if node.candidate_id == "stop_boundary_celery-worker"
    )
    assert stop_node.parent_candidate_ids == ["celery-worker"]
    assert "stop_boundary_python_runtime_stack_hotspot" not in tree.final_unknown_causes


def test_observation_source_parent_is_preserved_when_node_enters_observation_layer():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-observation-source-parent",
        cluster_assessment={
            "classification": "self_code_or_process_pressure",
            "summary": "运行时观察需要挂到显式来源。",
            "supported_level": "function",
            "confidence": 0.4,
            "evidence_refs": ["ev-stack"],
        },
        candidates=[
            {
                "candidate_id": "python_runtime_stack_hotspot",
                "rank": 1,
                "description": "运行时栈观察",
                "source_candidate_id": "celery-worker",
                "evidence_refs": ["ev-stack"],
                "max_supported_level": "function",
            },
            {
                "candidate_id": "celery-worker",
                "rank": 2,
                "description": "worker 基础定位",
                "evidence_refs": ["ev-stack"],
                "max_supported_level": "function",
            },
        ],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )

    assert tree is not None
    observation = next(
        node
        for layer in tree.layers
        for node in layer.unknown_causes
        if node.candidate_id == "python_runtime_stack_hotspot"
    )
    assert observation.node_type == "observation"
    assert observation.parent_candidate_ids == ["celery-worker"]
    assert observation.origin_parent_candidate_id == "celery-worker"


def test_probe_gate_failure_is_promoted_to_top_level_diagnostic():
    failures = orchestrator_module._collect_probe_gate_failures(
        [],
        [{
            "event_type": "followup_probe_input_missing",
            "payload": {
                "evidence_gap": "source_mechanism_query",
                "probe_id": "process_source_mechanism_query",
                "candidate_id": "ai_proposal_trace",
                "origin_parent_candidate_id": "verified_line_trace",
                "reason": "缺少受控查询参数",
            },
        }],
    )

    assert failures == [{
        "stage": "followup_probe",
        "gate": "source_mechanism_query",
        "failure_code": "missing_guarded_probe_input",
        "candidate_id": "ai_proposal_trace",
        "probe_id": "process_source_mechanism_query",
        "reason": "缺少受控查询参数",
        "actual_value": "missing",
        "status": "blocked",
        "evidence_refs": [],
        "initial_evidence_refs": [],
        "missing_initial_evidence_refs": [],
        "parent_candidate_id": "verified_line_trace",
        "origin_parent_candidate_id": "verified_line_trace",
        "retained_parent_candidate_id": "verified_line_trace",
        "conclusion_eligible": False,
    }]


def test_candidate_generation_output_includes_bounded_accepted_candidate_claims():
    output = orchestrator_module._candidate_generation_output({
        "ai_review_status": "succeeded",
        "candidate_proposals": [{
            "candidate_id": "ai_candidate_memory",
            "claim": "RSS 增长需要堆证据验证",
            "mechanism": "python_heap_growth",
            "target": "worker",
            "role": "unknown",
            "relation": "refinement",
            "supported_level": "function",
            "decision": "needs_more_evidence",
            "causal_status": "needs_more_evidence",
            "evidence_refs": ["ev-rss"],
            "missing_evidence": ["python_heap_profile"],
            "parent_candidate_ids": ["coarse_memory"],
            "origin_parent_candidate_id": "coarse_memory",
            "probe_requests": ["python_heap_profile"],
        }],
        "initial_evidence_context": {"evidence_refs": ["ev-rss"]},
    })

    assert output["accepted_candidate_ids"] == ["ai_candidate_memory"]
    assert output["accepted_candidates"][0]["claim"] == "RSS 增长需要堆证据验证"
    assert output["accepted_candidates"][0]["origin_parent_candidate_id"] == "coarse_memory"


def test_audit_structured_evidence_merges_task_families_without_last_empty_task_erasing_signals():
    evidence = [
        {
            "query_or_probe": "structured_evidence_json",
            "observed_value": {
                "summary": {
                    "task_id": "log-task",
                    "artifact_refs": [{"artifact_type": "log_window_json", "evidence_ref": "task:log"}],
                    "top_functions": [],
                    "stack_summary": {"sample_count": 0},
                    "confidence_inputs": {
                        "has_log_signal": True,
                        "log_error_cluster_count": 5,
                        "artifact_types": ["log_window_json"],
                        "collector_families": ["log_scan"],
                    },
                    "log_window_json": {
                        "summary": {"matched_records": 35, "error_cluster_count": 5},
                    },
                },
            },
        },
        {
            "query_or_probe": "structured_evidence_json",
            "observed_value": {
                "summary": {
                    "task_id": "offcpu-task",
                    "artifact_refs": [{"artifact_type": "off_cpu_wait_json", "evidence_ref": "task:offcpu"}],
                    "top_functions": [{"name": "0xdeadbeef", "samples": 58, "percent": 13.5}],
                    "stack_summary": {
                        "sample_count": 58,
                        "stack_sample_count": 58,
                        "has_wait_reason": True,
                        "total_wait_ms": 29007.78,
                    },
                    "confidence_inputs": {
                        "has_wait_or_io_signal": True,
                        "sample_count": 58,
                        "artifact_types": ["off_cpu_wait_json"],
                        "collector_families": ["off_cpu_wait_profile"],
                    },
                    "off_cpu_wait_json": {"summary": {"sample_count": 58}},
                },
            },
        },
        {
            "query_or_probe": "structured_evidence_json",
            "observed_value": {
                "summary": {
                    "task_id": "failed-perf-task",
                    "artifact_refs": [],
                    "top_functions": [],
                    "stack_summary": {"sample_count": 0},
                    "confidence_inputs": {
                        "artifact_types": [],
                        "collector_families": [],
                    },
                },
            },
        },
    ]

    merged = _structured_evidence(evidence)

    assert merged["scope"] == "diagnosis"
    assert merged["task_count"] == 3
    assert merged["confidence_inputs"]["has_log_signal"] is True
    assert merged["confidence_inputs"]["has_wait_or_io_signal"] is True
    assert merged["confidence_inputs"]["log_error_cluster_count"] == 5
    assert merged["top_functions"][0]["name"] == "0xdeadbeef"
    assert merged["stack_summary"]["stack_sample_count"] == 58
    assert merged["evidence_index"]["log_scan"]["summary"]["matched_records"] == 35


def test_runtime_contention_query_starts_with_low_cost_control_history(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "off_cpu_wait_profile", "pyspy"],
    )
    payload = _payload("Python服务线程很多但吞吐持续下降，请判断主要阻塞位置。")
    payload["context"]["service_id"] = "python-service"
    payload["context"]["instances"][0]["service_id"] = "python-service"
    payload["context"]["instances"][0]["instance_id"] = "python-service-1"
    payload["auto_execute_policy"] = "safe_only"
    response = client.post("/api/v1/diagnoses", json=payload)

    assert response.status_code == 200
    detail = response.json()["data"]
    assert detail["normalized_intent"]["symptom"] == "runtime_contention"
    probe_ids = [probe["probe_id"] for probe in detail["probes"]]
    assert "process_runtime_control_history" in probe_ids
    assert "process_log_scan" in probe_ids
    assert "process_off_cpu_profile" not in probe_ids
    assert "process_python_runtime_profile" not in probe_ids
    assert "process_io_latency" not in probe_ids


def test_off_cpu_wait_summary_uses_specific_function_anchor(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "off_cpu_wait_profile"],
    )
    payload = _payload("Python服务线程很多但吞吐持续下降，请判断主要阻塞位置。")
    payload["context"]["service_id"] = "python-service"
    payload["context"]["instances"][0]["service_id"] = "python-service"
    payload["context"]["instances"][0]["instance_id"] = "python-service-1"
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    metrics_probe = next(item for item in data["probes"] if item["probe_id"] == "host_process_metrics")
    _finish_sys_metrics_task(metrics_probe["task_id"], _normal_summary())
    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    off_cpu_probe = next(item for item in detail["probes"] if item["probe_id"] == "process_off_cpu_profile")
    task_id = off_cpu_probe["task_id"]

    repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
    repo.transition_task(task_id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
    repo.transition_task(task_id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
    repo.add_artifacts(task_id, [{
        "artifact_type": "off_cpu_wait_json",
        "object_key": f"tasks/{task_id}/off_cpu_wait.json",
        "metadata": {
            "data": {
                "summary": {
                    "sample_count": 138,
                    "blocked_thread_count": 6,
                    "total_wait_ms": 2400.0,
                    "top_wait_reason": "interruptible_sleep_or_lock_wait",
                    "has_wait_reason": True,
                },
                "top_wait_stacks": [{
                    "wait_reason": "interruptible_sleep_or_lock_wait",
                    "stack": ["pthread_mutex_lock", "python_service.handle_request"],
                    "top_frame": "pthread_mutex_lock",
                    "samples": 138,
                    "wait_ms": 2400.0,
                    "percent": 72.4,
                }],
            },
        },
    }])
    repo.transition_task(task_id, TaskStatus.DONE, "analysis complete", Actor.ANALYZER)

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    assessment = detail["latest_conclusion"]["cluster_assessment"]
    summary = detail["latest_conclusion"]["summary"]

    assert assessment["classification"] == "self_code_or_process_pressure"
    assert assessment["supported_level"] == "syscall"
    assert assessment["claim_type"] == "observation_only"
    assert assessment["conclusion_eligible"] is False
    assert assessment["primary_anchor"]["anchor"] == "pthread_mutex_lock"
    assert "pthread_mutex_lock" in summary
    assert "不能区分主动 sleep、退避重试、锁等待或上游阻塞" in summary
    assert "证据主要集中在目标实例自身" not in summary


def test_unsymbolized_off_cpu_address_stays_at_process_level():
    anchor = orchestrator_module._specific_diagnostic_anchor(
        {
            "off_cpu_wait_json": {
                "summary": {
                    "top_wait_reason": "interruptible_sleep_or_lock_wait",
                },
                "top_wait_stacks": [{
                    "top_frame": "0x758465027fac",
                    "samples": 58,
                    "percent": 12.66,
                    "wait_reason": "interruptible_sleep_or_lock_wait",
                    "wait_ms": 29009.77,
                }],
            },
        },
        {
            "service_id": "cartservice",
            "instance_id": "cartservice-1",
            "pid": 20061,
        },
        {},
        [],
    )

    assert anchor["supported_level"] == "process"
    assert anchor["anchor_type"] == "off_cpu_wait_unsymbolized_address"
    assert "符号映射" in anchor["blocked_upgrade_reason"]


def test_completed_dependency_evidence_is_not_requested_again():
    filtered = orchestrator_module._filter_pending_evidence_requests(
        "diag-1",
        ["dependency_check", "log_scan", "redis_check"],
        [
            {
                "parameters": {"evidence_gap": "dependency_check"},
                "status": "COMPLETED",
                "evidence_status": "valid",
            },
            {
                "parameters": {"evidence_gap": "redis_check"},
                "status": "COMPLETED",
                "evidence_status": "valid",
            },
            {
                "parameters": {"evidence_gap": "log_scan"},
                "status": "FAILED",
            },
        ],
    )

    assert filtered == ["log_scan"]


def test_completed_empty_depth_evidence_remains_pending():
    filtered = orchestrator_module._filter_pending_evidence_requests(
        "diag-1",
        ["baseline_window_profile", "trace_endpoint_profile", "off_cpu_wait_profile"],
        [
            {
                "parameters": {"evidence_gap": "baseline_window_profile"},
                "status": "COMPLETED",
                "evidence_status": "empty_window",
            },
            {
                "parameters": {"evidence_gap": "trace_endpoint_profile"},
                "status": "COMPLETED",
                "evidence_status": "empty_window",
            },
        ],
        task_observations=[],
    )

    assert filtered == ["baseline_window_profile", "trace_endpoint_profile", "off_cpu_wait_profile"]


def test_downstream_dependency_summary_names_redis_and_anchor():
    summary = orchestrator_module._downstream_dependency_summary(
        {
            "instance_id": "cartservice-1",
            "pid": 20061,
            "anchor_type": "off_cpu_wait_unsymbolized_address",
            "anchor": "0x758465027fac",
            "wait_reason": "interruptible_sleep_or_lock_wait",
            "samples": 60,
            "blocked_upgrade_reason": "当前等待栈顶部仍是未符号化地址，需补 debuginfo/符号映射后才能升级到函数。",
        },
        [{
            "redis": {
                "failed": True,
                "max_latency_ms": 1200,
                "slowlog_entry_count": 1,
            },
            "dependency": {"failed_count": 1},
        }],
        {
            "target_scope": {
                "dependency_targets": [{
                    "dependency_id": "redis-cart",
                    "protocol": "redis",
                    "host": "redis-cart",
                }]
            }
        },
    )

    assert "redis-cart" in summary
    assert "最大延迟 1200ms" in summary
    assert "0x758465027fac" in summary
    assert "符号映射" in summary


def test_readiness_gate_fails_when_runtime_stack_is_empty():
    bundle = {
        "runtime_trace": [{"stage": "evidence"}],
        "probes": [{
            "probe_id": "process_baseline_window",
            "task_id": "task_runtime",
            "status": "COMPLETED",
        }],
        "child_task_ids": ["task_runtime"],
        "tasks": [{"id": "task_runtime", "collector_type": "baseline_window_profile"}],
        "artifacts": [{"task_id": "task_runtime", "artifact_type": "continuous_summary"}],
        "structured_evidence": {"version": 1},
        "evidence": [{
            "query_or_probe": "structured_evidence_json",
            "observed_value": {
                "summary": {
                    "top_functions": [],
                    "stack_summary": {"sample_count": 0, "stack_sample_count": 0, "has_wait_reason": False},
                    "call_path_hotspots": [],
                }
            },
        }],
        "evidence_refs": ["ev_1"],
    }

    gate = build_readiness_gate(bundle)
    check = next(item for item in gate["checks"] if item["name"] == "runtime_stack_quality_non_empty")

    assert gate["status"] == "FAIL"
    assert check["status"] == "FAIL"


def test_readiness_accepts_explicit_optional_runtime_empty_window():
    bundle = {
        "runtime_trace": [{"stage": "evidence"}],
        "probes": [{
            "probe_id": "process_trace_endpoint_profile",
            "task_id": "task_runtime",
            "status": "COMPLETED",
            "evidence_status": "empty_window",
        }],
        "child_task_ids": ["task_runtime"],
        "tasks": [{"id": "task_runtime", "collector_type": "trace_endpoint_profile"}],
        "artifacts": [{"task_id": "task_runtime", "artifact_type": "trace_endpoint_profile_json"}],
        "structured_evidence": {"version": 1},
        "evidence": [],
        "evidence_refs": ["ev_dependency"],
    }

    gate = build_readiness_gate(bundle)
    checks = {item["name"]: item["status"] for item in gate["checks"]}

    assert checks["runtime_stack_quality_non_empty"] == "PASS"
    assert checks["completed_probe_evidence_valid"] == "PASS"


def test_readiness_gate_rejects_stopped_state_as_stack_evidence():
    bundle = {
        "runtime_trace": [{"stage": "evidence"}],
        "probes": [{
            "probe_id": "process_off_cpu_profile",
            "task_id": "task_runtime",
            "status": "COMPLETED",
        }],
        "child_task_ids": ["task_runtime"],
        "tasks": [{"id": "task_runtime", "collector_type": "off_cpu_wait_profile"}],
        "artifacts": [{"task_id": "task_runtime", "artifact_type": "off_cpu_wait_json"}],
        "structured_evidence": {"version": 1},
        "evidence": [{
            "query_or_probe": "structured_evidence_json",
            "observed_value": {
                "summary": {
                    "sys_metrics": {
                        "summary": {
                            "process_state": "T",
                            "stopped_sample_ratio": 1.0,
                        },
                    },
                },
            },
        }],
        "evidence_refs": ["ev_1"],
    }

    gate = build_readiness_gate(bundle)
    check = next(item for item in gate["checks"] if item["name"] == "runtime_stack_quality_non_empty")

    assert check["status"] == "FAIL"


def test_persistent_stopped_process_becomes_runtime_stall_conclusion(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics"],
    )
    payload = _payload("服务进程存在、端口可连，但业务工作没有继续推进，进程疑似卡住。")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    metrics_probe = next(item for item in data["probes"] if item["probe_id"] == "host_process_metrics")
    summary = _normal_summary()
    summary.update({
        "process_state": "T",
        "process_state_name": "T (stopped)",
        "process_state_counts": {"T": 10},
        "stopped_sample_count": 10,
        "stopped_sample_ratio": 1.0,
    })
    _finish_sys_metrics_task(metrics_probe["task_id"], summary)

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    conclusion = detail["latest_conclusion"]
    assessment = conclusion["cluster_assessment"]

    assert assessment["classification"] == "runtime_stall"
    assert assessment["mechanism"] == "process_suspended"
    assert assessment["claim_type"] == "direct_failure_mechanism"
    assert assessment["conclusion_eligible"] is False
    assert assessment["location_type"] == "self"
    assert assessment["domain_type"] == "runtime"
    assert assessment["classification"] == "runtime_stall"
    assert "T (stopped)" in conclusion["summary"]
    assert "无法确认是谁" in conclusion["summary"]
    assert conclusion["abstained"] is True
    assert conclusion["root_cause_candidates"] == []
    assert conclusion["controlled_ai_tree"]["final_primary_causes"] == []


def test_runtime_stall_with_same_window_signal_becomes_direct_root_cause(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "log_scan", "runtime_control_history"],
    )
    payload = _payload("服务进程存在、端口可连，但业务工作没有继续推进，进程疑似卡住。")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    probes = {item["probe_id"]: item for item in data["probes"]}
    summary = _normal_summary()
    summary.update({
        "process_state": "T",
        "process_state_name": "T (stopped)",
        "process_state_counts": {"T": 10},
        "stopped_sample_count": 10,
        "stopped_sample_ratio": 1.0,
    })
    _finish_sys_metrics_task(probes["host_process_metrics"]["task_id"], summary)
    _finish_structured_task(probes["process_log_scan"]["task_id"], "log_window_json", {
        "summary": {"source_status": "no_error", "matched_records": 0},
        "error_clusters": [],
        "evidence_validity": {
            "execution_status": "completed",
            "artifact_status": "produced",
            "evidence_status": "partial",
            "reason": "no errors in bounded window",
        },
    })
    _finish_structured_task(probes["process_runtime_control_history"]["task_id"], "runtime_control_event_json", {
        "target_pid": 1234,
        "events": [{
            "event_type": "signal_sent",
            "observed_at": "2026-08-19T06:21:31Z",
            "actor": {"pid": 4321, "comm": "bash", "uid": 1000},
            "action": {"operation": "signal", "signal": "SIGSTOP"},
            "target": {"pid": 1234, "comm": "python-service"},
            "effect": {"expected_state": "T"},
            "evidence_ref": "runtime_control.events[0]",
        }],
        "causal_edges": [
            {"source": "bash", "relation": "ISSUED", "target": "SIGSTOP"},
            {"source": "SIGSTOP", "relation": "TARGETED", "target": "1234"},
        ],
        "summary": {"event_count": 1, "has_control_actor": True, "has_direct_control_chain": True},
        "evidence_validity": {
            "execution_status": "completed",
            "artifact_status": "produced",
            "evidence_status": "valid",
            "reason": "matching runtime control event found",
        },
    })

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    conclusion = detail["latest_conclusion"]
    assessment = conclusion["cluster_assessment"]

    assert assessment["claim_type"] == "direct_root_cause"
    assert assessment["conclusion_eligible"] is True
    assert assessment["origin_unknown"] is True
    assert assessment["runtime_control_event"]["actor"]["comm"] == "bash"
    assert "上游来源证据" in conclusion["summary"]
    assert conclusion["abstained"] is True
    assert conclusion["root_cause_candidates"] == []
    assert conclusion["causal_chain"] == []
    assert conclusion["formal_root_cause"] is None


def test_runtime_stall_with_source_provenance_becomes_complete_source_root_cause(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "log_scan", "runtime_control_history"],
    )
    payload = _payload("服务进程存在、端口可连，但业务工作没有继续推进，进程疑似卡住。")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    probes = {item["probe_id"]: item for item in data["probes"]}
    summary = _normal_summary()
    summary.update({
        "process_state": "T",
        "process_state_name": "T (stopped)",
        "process_state_counts": {"T": 10},
        "stopped_sample_count": 10,
        "stopped_sample_ratio": 1.0,
    })
    _finish_sys_metrics_task(probes["host_process_metrics"]["task_id"], summary)
    _finish_structured_task(probes["process_log_scan"]["task_id"], "log_window_json", {
        "summary": {"source_status": "no_error", "matched_records": 0},
        "error_clusters": [],
        "evidence_validity": {"execution_status": "completed", "artifact_status": "produced", "evidence_status": "partial"},
    })
    _finish_structured_task(probes["process_runtime_control_history"]["task_id"], "runtime_control_event_json", {
        "target_pid": 1234,
        "events": [{
            "event_type": "signal_sent",
            "observed_at": "2026-08-19T06:21:31Z",
            "actor": {"pid": 4321, "comm": "bash", "uid": 1000},
            "action": {"operation": "signal", "signal": "SIGSTOP"},
            "target": {"pid": 1234, "comm": "python-service"},
            "effect": {"expected_state": "T"},
            "source_provenance": {
                "controller": "fault-runner",
                "redacted_command_source": "scenario:runtime-stall",
                "initiated_at": "2026-08-19T06:21:30Z",
            },
            "evidence_ref": "runtime_control.events[0]",
        }],
        "summary": {"event_count": 1, "has_direct_control_chain": True, "has_complete_source_chain": True},
        "evidence_validity": {"execution_status": "completed", "artifact_status": "produced", "evidence_status": "valid"},
    })

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    assessment = detail["latest_conclusion"]["cluster_assessment"]

    assert assessment["claim_type"] == "complete_source_root_cause"
    assert assessment["origin_unknown"] is False
    assert assessment["source_provenance"]["controller"] == "fault-runner"


def test_benchmark_score_module_scores_bundle_against_oracle(client: TestClient):
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    task_id = data["child_task_ids"][0]
    summary = _normal_summary()
    summary["avg_cpu_user_pct"] = 92.0
    _finish_sys_metrics_task(task_id, summary)
    bundle = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}/audit-bundle").json()["data"]
    oracle = {
        "case_id": "OB-SINGLE-CPU-001",
        "expected": {
            "location_type": "self",
            "domain_type": "cpu",
            "classification": "self_code_or_process_pressure",
        },
        "evidence": {"required_collectors": ["sys_metrics"], "minimum_independent_sources": 1},
        "trace": {"runtime_required": True},
    }

    result = score_audit_bundle(bundle, oracle)
    aggregate = aggregate_results([result])

    assert result["case_id"] == "OB-SINGLE-CPU-001"
    assert "root_cause" in result["dimensions"]
    assert aggregate["run_count"] == 1


def test_unscoped_continuous_baseline_is_not_reused_for_diagnosis(client: TestClient):
    task_id = client.post("/api/tasks", json={
        "name": "baseline-window",
        "agent_id": "a1",
        "target_pid": 1234,
        "collector_type": "baseline_window_profile",
        "duration_sec": 5,
    }).json()["data"]["task_id"]
    repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
    repo.transition_task(task_id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
    repo.transition_task(task_id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
    repo.add_artifacts(task_id, [
        {
            "artifact_type": "continuous_top_json",
            "object_key": f"tasks/{task_id}/continuous_top_json.json",
            "metadata": {"data": [{"name": "service.hot_loop", "samples": 80, "percent": 66.6}]},
        },
        {
            "artifact_type": "continuous_summary",
            "object_key": f"tasks/{task_id}/continuous_summary.json",
            "metadata": {"data": {"window_count": 3, "drift": "high"}},
        },
    ])
    repo.transition_task(task_id, TaskStatus.DONE, "analysis complete", Actor.ANALYZER)

    detail = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    bundle = client.get(f"/api/v1/diagnoses/{detail['diagnosis_id']}/audit-bundle").json()["data"]

    assert task_id not in detail["child_task_ids"]
    assert bundle["structured_evidence"] == {}


def _finish_sys_metrics_task(task_id: str, summary: dict):
    repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
    repo.transition_task(task_id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
    repo.transition_task(task_id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
    repo.add_artifacts(task_id, [{
        "artifact_type": "sys_metrics",
        "object_key": f"tasks/{task_id}/sys_metrics.json",
        "metadata": {
            "data": {
                "sample_count": 10,
                "summary": summary,
            },
        },
    }])
    repo.transition_task(task_id, TaskStatus.DONE, "analysis complete", Actor.ANALYZER)


def _finish_depth_task(task_id: str):
    repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
    repo.transition_task(task_id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
    repo.transition_task(task_id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
    repo.add_artifacts(task_id, [{
        "artifact_type": "depth_evidence_json",
        "object_key": f"tasks/{task_id}/depth.json",
        "metadata": {
            "data": {
                "context": {
                    "trace_id": "trace-1",
                    "context_id": "ctx-1",
                    "call_path": "gateway;service-a;busy_cpu",
                    "endpoint": "/orders",
                    "service": "service-a",
                    "instance": "service-a-1",
                },
                "stack_samples": [{
                    "hot_frame": "microservices_test.common.busy_cpu",
                    "sample_count": 120,
                    "percent": 64.0,
                    "call_path": "gateway;service-a;busy_cpu",
                    "wait_reason": "cpu-bound",
                    "context_id": "ctx-1",
                }],
            },
        },
    }])
    repo.transition_task(task_id, TaskStatus.DONE, "analysis complete", Actor.ANALYZER)


def _finish_structured_task(task_id: str, artifact_type: str, data: dict):
    repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
    repo.transition_task(task_id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
    repo.transition_task(task_id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
    repo.add_artifacts(task_id, [{
        "artifact_type": artifact_type,
        "object_key": f"tasks/{task_id}/{artifact_type}.json",
        "metadata": {"data": data},
    }])
    repo.transition_task(task_id, TaskStatus.DONE, "analysis complete", Actor.ANALYZER)


def _task_for_step(step_id: str):
    for task in repo.tasks.values():
        if (task.request_params or {}).get("options", {}).get("diagnosis_step_id") == step_id:
            return task
    raise AssertionError(f"task not found for step {step_id}")


def _normal_summary() -> dict:
    return {
        "avg_cpu_user_pct": 18.0,
        "avg_cpu_sys_pct": 4.0,
        "avg_cpu_iowait_pct": 1.0,
        "load1m": 0.8,
        "thread_count": 20,
        "thread_trend": "stable",
        "fd_count": 20,
        "fd_trend": "stable",
        "fd_max": 25,
        "vmrss_mb": 200,
        "vmrss_mb_max": 220,
        "ctx_nonvoluntary_rate": 10,
        "net_rx_kbps": 10,
        "net_tx_kbps": 10,
    }


def _sys_metric_probe_by_instance(data: dict) -> dict:
    return {
        item["target"]["instance_id"]: item
        for item in data["probes"]
        if item["probe_id"] == "host_process_metrics"
    }


class TestDiagnosisSessionAPI:
    def test_missing_instance_mapping_requires_scope_confirmation(self, client: TestClient):
        response = client.post("/api/v1/diagnoses", json={
            "query": "服务 service-a 为什么变慢",
            "context": {"service_id": "service-a", "environment": "production"},
        })
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "NEEDS_SCOPE_CONFIRMATION"
        assert data["child_task_ids"] == []
        assert data["normalized_intent"]["ambiguities"] == ["service_instance_mapping"]
        assert data["latest_conclusion"]["diagnostic_commands"]
        assert all(cmd["auto_execute"] is False for cmd in data["latest_conclusion"]["diagnostic_commands"])

    def test_create_schedules_only_registered_low_risk_probe(self, client: TestClient):
        payload = _payload()
        payload["auto_execute_policy"] = "safe_only"
        response = client.post("/api/v1/diagnoses", json=payload)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "COLLECTING"
        assert len(data["child_task_ids"]) == 1
        probes = {item["probe_id"]: item for item in data["probes"]}
        assert probes["host_process_metrics"]["status"] in {"SCHEDULED", "RUNNING"}
        assert probes["process_cpu_profile"]["status"] == "WAITING_APPROVAL"

        task = repo.tasks[data["child_task_ids"][0]]
        assert task.collector_type == "sys_metrics"
        assert task.request_params["options"]["registered_probe"] is True
        assert task.request_params["options"]["diagnosis_step_id"].startswith("step_")

    def test_r2_probe_requires_explicit_single_execution_approval(self, client: TestClient):
        payload = _payload()
        payload["auto_execute_policy"] = "safe_only"
        data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        r2 = next(item for item in data["probes"] if item["risk_level"] == "R2")
        approved = client.post(
            f"/api/v1/diagnoses/{data['diagnosis_id']}/approvals",
            json={
                "step_id": r2["step_id"],
                "decision": "approve",
                "scope": "single_execution",
                "approver_id": "operator-1",
            },
        )
        assert approved.status_code == 200
        detail = approved.json()["data"]
        approved_probe = next(item for item in detail["probes"] if item["step_id"] == r2["step_id"])
        assert approved_probe["approved_by"] == "operator-1"
        assert approved_probe["task_id"]
        assert detail["budget_used"]["medium_risk_probes"] == 1
        assert repo.tasks[approved_probe["task_id"]].collector_type == "perf_cpu"

    def test_bulk_approval_approves_current_waiting_registered_probe(self, client: TestClient):
        payload = _payload()
        payload["auto_execute_policy"] = "safe_only"
        data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        waiting = [
            item for item in data["probes"]
            if item["status"] == "WAITING_APPROVAL"
        ]
        assert waiting

        approved = client.post(
            f"/api/v1/diagnoses/{data['diagnosis_id']}/approvals/bulk",
            json={
                "decision": "approve",
                "scope": "all_waiting",
                "approver_id": "operator-1",
            },
        )

        assert approved.status_code == 200
        detail = approved.json()["data"]
        probe = next(item for item in detail["probes"] if item["step_id"] == waiting[0]["step_id"])
        assert probe["approved_by"] == "operator-1"
        assert probe["task_id"]
        assert repo.tasks[probe["task_id"]].collector_type == "perf_cpu"

    def test_all_registered_policy_schedules_initial_r2_without_manual_approval(self, client: TestClient):
        payload = _payload()
        payload["budget_profile"] = "development"
        payload["auto_execute_policy"] = "all_registered"
        data = client.post("/api/v1/diagnoses", json=payload).json()["data"]

        probes = {item["probe_id"]: item for item in data["probes"]}

        assert probes["process_cpu_profile"]["requires_approval"] is False
        assert probes["process_cpu_profile"]["parameters"]["execution_policy"] == "all_registered"
        assert probes["process_cpu_profile"]["status"] in {"SCHEDULED", "RUNNING"}
        assert probes["process_cpu_profile"]["task_id"]
        assert repo.tasks[probes["process_cpu_profile"]["task_id"]].collector_type == "perf_cpu"

    def test_completed_probe_produces_evidence_linked_candidate(self, client: TestClient):
        payload = _payload()
        payload["auto_execute_policy"] = "safe_only"
        data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        task_id = data["child_task_ids"][0]
        repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
        repo.transition_task(task_id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
        repo.transition_task(task_id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
        repo.add_artifacts(task_id, [{
            "artifact_type": "sys_metrics",
            "object_key": f"tasks/{task_id}/sys_metrics.json",
            "metadata": {
                "data": {
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
                },
            },
        }])
        repo.transition_task(task_id, TaskStatus.DONE, "analysis complete", Actor.ANALYZER)

        detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
        assert detail["status"] in {"COLLECTING", "WAITING_APPROVAL"}
        assert detail["latest_conclusion"]["root_cause_candidates"] == []
        assert detail["latest_conclusion"]["possible_root_causes"] == []
        assert detail["latest_conclusion"]["cluster_assessment"]["evidence_refs"]
        assert detail["latest_conclusion"]["diagnostic_commands"]
        assert all(cmd["auto_execute"] is False for cmd in detail["latest_conclusion"]["diagnostic_commands"])
        evidence_ids = {item["evidence_id"] for item in detail["evidence"]}
        assert set(detail["latest_conclusion"]["cluster_assessment"]["evidence_refs"]).issubset(evidence_ids)
        assert all(item["integrity_hash"].startswith("sha256:") for item in detail["evidence"])
        assert any(item.get("task_id") == task_id for item in detail["probes"])

    def test_rejected_deep_probe_can_end_as_insufficient_evidence(self, client: TestClient):
        payload = _payload()
        payload["auto_execute_policy"] = "safe_only"
        data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        task_id = data["child_task_ids"][0]
        repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
        repo.transition_task(task_id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
        repo.transition_task(task_id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
        repo.transition_task(task_id, TaskStatus.DONE, "no structured output", Actor.ANALYZER)
        waiting = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
        assert waiting["status"] == "WAITING_APPROVAL"
        r2 = next(item for item in waiting["probes"] if item["risk_level"] == "R2")

        rejected = client.post(
            f"/api/v1/diagnoses/{data['diagnosis_id']}/approvals",
            json={"step_id": r2["step_id"], "decision": "reject", "approver_id": "operator-1"},
        )
        assert rejected.status_code == 200
        detail = rejected.json()["data"]
        assert detail["status"] == "INSUFFICIENT_EVIDENCE"
        assert detail["latest_conclusion"]["confidence_level"] == "不可判断"

    def test_unknown_fields_are_rejected(self, client: TestClient):
        payload = _payload()
        payload["context"]["shell"] = "rm -rf /"
        response = client.post("/api/v1/diagnoses", json=payload)
        assert response.status_code == 422

    def test_service_allowlist_is_enforced(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("MINI_DROP_ALLOWED_SERVICES", "service-b")
        response = client.post("/api/v1/diagnoses", json=_payload())
        assert response.status_code == 403

    def test_requested_budget_cannot_exceed_policy_profile(self, client: TestClient):
        payload = _payload()
        payload["budget"] = {
            "max_hosts": 20,
            "max_service_instances": 100,
            "max_topology_hops": 3,
            "max_duration_minutes": 60,
            "max_parallel_probes": 10,
            "max_artifact_size_mb": 4096,
            "max_model_calls": 30,
            "max_medium_risk_probes": 5,
            "max_total_probe_cpu_seconds": 3600,
        }
        detail = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        assert detail["resource_budget"]["max_hosts"] == 5
        assert detail["resource_budget"]["max_topology_hops"] == 2
        assert detail["resource_budget"]["max_parallel_probes"] == 3
        assert detail["resource_budget"]["max_medium_risk_probes"] == 1

    def test_default_budget_is_180_with_60_second_followup_reserve(self, client: TestClient):
        detail = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]

        assert detail["resource_budget"]["max_total_probe_cpu_seconds"] == 180
        assert detail["resource_budget"]["follow_up_reserve_seconds"] == 60
        assert diagnosis_orchestrator._duration_limit(detail, "initial") == 120
        assert diagnosis_orchestrator._duration_limit(detail, "followup") == 180

    def test_diagnosis_collection_context_uses_evidence_window_contract(self, client: TestClient):
        payload = _payload()
        payload["context"]["time_range"] = {
            "start": "2026-08-19T00:00:00Z",
            "end": "2026-08-19T00:15:00Z",
            "source": "request_context",
        }
        detail = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        probe = next(item for item in detail["probes"] if item["probe_id"] == "host_process_metrics")
        options = repo.tasks[probe["task_id"]].request_params["options"]

        assert options["collection_mode"] == "delayed_followup"
        assert isinstance(options["window_start"], str)
        assert isinstance(options["window_end"], str)
        assert options["evidence_cohort_id"] == detail["diagnosis_id"]
        assert options["window_start"] != detail["requested_time_range"]["start"]
        assert options["timing_relation"] == "delayed_followup"

    def test_target_scope_stops_at_two_topology_hops(self, client: TestClient):
        for index in range(2, 5):
            repo.register_agent(
                f"a{index}", f"host-{index}", f"10.0.0.{index}",
                capabilities=["sys_metrics", "trace_endpoint_profile"],
            )
        payload = _payload("service-a 延迟升高，沿调用链下探")
        payload["budget_profile"] = "development"
        payload["budget"] = {"max_topology_hops": 3}
        payload["context"]["instances"].extend([
            {"service_id": "service-b", "instance_id": "service-b-1", "host_id": "host-2", "agent_id": "a2", "pid": 2002, "environment": "production"},
            {"service_id": "service-c", "instance_id": "service-c-1", "host_id": "host-3", "agent_id": "a3", "pid": 2003, "environment": "production"},
            {"service_id": "service-d", "instance_id": "service-d-1", "host_id": "host-4", "agent_id": "a4", "pid": 2004, "environment": "production"},
        ])
        payload["context"]["dependencies"] = [
            {"source_service": "service-a", "target_service": "service-b", "relation": "CALLS", "confidence": "high", "source": "test"},
            {"source_service": "service-b", "target_service": "service-c", "relation": "CALLS", "confidence": "high", "source": "test"},
            {"source_service": "service-c", "target_service": "service-d", "relation": "CALLS", "confidence": "high", "source": "test"},
        ]

        detail = client.post("/api/v1/diagnoses", json=payload).json()["data"]

        instances = {item["instance_id"]: item for item in detail["target_scope"]["instances"]}
        assert detail["resource_budget"]["max_topology_hops"] == 2
        assert instances["service-a-1"]["topology_hop"] == 0
        assert instances["service-b-1"]["topology_hop"] == 1
        assert instances["service-c-1"]["topology_hop"] == 2
        assert "service-d-1" not in instances
        assert all(probe["target"]["instance_id"] != "service-d-1" for probe in detail["probes"])

    def test_requested_followup_reserve_is_capped_with_total_budget(self, client: TestClient):
        payload = _payload()
        payload["budget"] = {
            "max_total_probe_cpu_seconds": 150,
            "follow_up_reserve_seconds": 100,
        }
        detail = client.post("/api/v1/diagnoses", json=payload).json()["data"]

        assert detail["resource_budget"]["max_total_probe_cpu_seconds"] == 150
        assert detail["resource_budget"]["follow_up_reserve_seconds"] == 60
        assert diagnosis_orchestrator._follow_up_reserve(detail) == 60
        assert diagnosis_orchestrator._duration_limit(detail, "initial") == 90

    def test_budget_block_event_records_phase_and_next_action(self, client: TestClient):
        session = {
            "resource_budget": {
                "max_total_probe_cpu_seconds": 180,
                "follow_up_reserve_seconds": 60,
                "max_duration_minutes": 10,
            },
            "budget_used": {"probe_duration_seconds": 180},
        }
        payload = diagnosis_orchestrator._budget_block_event_payload(
            session,
            phase="followup",
            requested_seconds=15,
            reason="总采集时长预算已用尽",
        )

        assert payload == {
            "budget_phase": "followup",
            "used_seconds": 180,
            "limit_seconds": 180,
            "reserved_seconds": 60,
            "requested_seconds": 15,
            "reason": "总采集时长预算已用尽",
            "next_action": "减少采集范围，或保留预算给下一轮 AI 树 follow-up",
        }

    def test_probe_registry_exposes_no_shell_command(self, client: TestClient):
        probes = client.get("/api/v1/probes").json()["data"]
        assert probes
        assert all("command" not in probe for probe in probes)
        assert {probe["risk_level"] for probe in probes}.issubset({"R0", "R1", "R2", "R3"})

    def test_same_host_noisy_neighbor_assessment_uses_multiple_agents(self, client: TestClient):
        repo.register_agent(
            "a2", "host-1", "10.0.0.2",
            capabilities=["sys_metrics", "perf_cpu", "ebpf_io", "memory_smaps"],
        )
        payload = _payload("service-a 变慢，判断是不是被同宿主其他服务影响")
        payload["budget_profile"] = "development"
        payload["auto_execute_policy"] = "safe_only"
        payload["context"]["instances"].append({
            "service_id": "service-b",
            "instance_id": "service-b-1",
            "host_id": "host-1",
            "agent_id": "a2",
            "pid": 4321,
            "environment": "production",
        })

        data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        probes = {
            item["target"]["instance_id"]: item
            for item in data["probes"]
            if item["probe_id"] == "host_process_metrics"
        }
        assert set(probes) == {"service-a-1", "service-b-1"}

        noisy_summary = _normal_summary()
        noisy_summary.update({
            "avg_cpu_user_pct": 86.0,
            "avg_cpu_sys_pct": 9.0,
            "avg_cpu_iowait_pct": 24.0,
            "load1m": 9.0,
        })
        _finish_sys_metrics_task(probes["service-a-1"]["task_id"], _normal_summary())
        _finish_sys_metrics_task(probes["service-b-1"]["task_id"], noisy_summary)

        detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
        assessment = detail["latest_conclusion"]["cluster_assessment"]
        assert detail["status"] in {"COMPLETED", "COLLECTING", "WAITING_APPROVAL"}
        assert assessment["classification"] == "same_host_noisy_neighbor"
        assert assessment["confidence_level"] == "不可判断"
        assert assessment["observation_confidence"] >= assessment["confidence"]
        assert len(assessment["compared_targets"]) == 2
        evidence_ids = {item["evidence_id"] for item in detail["evidence"]}
        assert set(assessment["evidence_refs"]).issubset(evidence_ids)
        commands = detail["latest_conclusion"]["diagnostic_commands"]
        assert any(cmd["risk_level"] == "R2" and cmd["requires_approval"] for cmd in commands)
        assert all(cmd["execution_policy"] == "human_review_required" for cmd in commands)

    def test_shared_io_wait_prefers_host_contention_over_generic_neighbor(self, client: TestClient):
        repo.register_agent(
            "a2", "host-1", "10.0.0.2",
            capabilities=["sys_metrics", "perf_cpu", "ebpf_io", "memory_smaps"],
        )
        payload = _payload("service-a 变慢，检查同宿主 I/O 争抢")
        payload["budget_profile"] = "development"
        payload["auto_execute_policy"] = "safe_only"
        payload["context"]["instances"].append({
            "service_id": "service-b",
            "instance_id": "service-b-1",
            "host_id": "host-1",
            "agent_id": "a2",
            "pid": 4321,
            "environment": "production",
        })

        data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        probes = _sys_metric_probe_by_instance(data)
        target_io = _normal_summary()
        target_io.update({"avg_cpu_iowait_pct": 28.0, "load1m": 6.0})
        neighbor_io = _normal_summary()
        neighbor_io.update({"avg_cpu_iowait_pct": 34.0, "load1m": 7.0})
        _finish_sys_metrics_task(probes["service-a-1"]["task_id"], target_io)
        _finish_sys_metrics_task(probes["service-b-1"]["task_id"], neighbor_io)

        detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
        assessment = detail["latest_conclusion"]["cluster_assessment"]
        assert detail["status"] in {"COMPLETED", "COLLECTING", "WAITING_APPROVAL"}
        assert assessment["classification"] == "host_resource_contention"
        assert any(target["pressure"]["io_wait"] for target in assessment["compared_targets"])
        assert any(cmd["command_id"] == "cmd_io_latency" for cmd in detail["latest_conclusion"]["diagnostic_commands"])

    def test_downstream_pressure_is_reported_as_root_cause_node_not_first_alert(self, client: TestClient):
        repo.register_agent(
            "a2", "host-2", "10.0.0.2",
            capabilities=["sys_metrics", "perf_cpu", "ebpf_io", "memory_smaps"],
        )
        payload = _payload("service-a 延迟升高，逐层检查调用链真正根因")
        payload["budget_profile"] = "development"
        payload["auto_execute_policy"] = "safe_only"
        payload["context"]["instances"].append({
            "service_id": "service-b",
            "instance_id": "service-b-1",
            "host_id": "host-2",
            "agent_id": "a2",
            "pid": 4321,
            "environment": "production",
        })
        payload["context"]["dependencies"] = [{
            "source_service": "service-a",
            "target_service": "service-b",
            "relation": "CALLS",
            "confidence": "high",
            "source": "test_topology",
        }]

        data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        probes = _sys_metric_probe_by_instance(data)
        downstream_hot = _normal_summary()
        downstream_hot.update({"avg_cpu_user_pct": 91.0, "avg_cpu_sys_pct": 6.0, "load1m": 12.0})
        _finish_sys_metrics_task(probes["service-a-1"]["task_id"], _normal_summary())
        _finish_sys_metrics_task(probes["service-b-1"]["task_id"], downstream_hot)

        detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
        assessment = detail["latest_conclusion"]["cluster_assessment"]
        assert detail["status"] in {"COLLECTING", "WAITING_APPROVAL"}
        assert assessment["classification"] == "downstream_dependency"
        assert "service-b" in detail["target_scope"]["downstream_service_ids"]
        assert any(
            target["service_id"] == "service-b" and target["pressure"]["cpu"]
            for target in assessment["compared_targets"]
        )
        assert any(item["hypothesis"] == "same_host_noisy_neighbor" for item in assessment["ruled_out"])
        evidence_ids = {item["evidence_id"] for item in detail["evidence"]}
        assert set(assessment["evidence_refs"]).issubset(evidence_ids)


def test_ai_tree_evidence_request_maps_to_followup_probe_once(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[
            "sys_metrics",
            "perf_cpu",
            "ebpf_io",
            "memory_smaps",
            "baseline_window_profile",
            "log_scan",
            "dependency_check",
            "redis_check",
        ],
    )
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]
    created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["baseline_window_profile", "off_cpu_wait_profile", "dependency_check", "log_scan", "redis_check", "unknown_request"],
        parent_task,
    )
    assert 1 <= created <= 3
    probes = diagnosis_orchestrator.store.list_probes(diagnosis_id)
    gaps = {
        (item.get("parameters") or {}).get("evidence_gap")
        for item in probes
        if (item.get("parameters") or {}).get("evidence_gap")
    }
    assert "baseline_window_profile" in gaps

    second_created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["off_cpu_wait_profile", "baseline_window_profile", "dependency_check", "log_scan", "redis_check"],
        parent_task,
    )
    followups = [
        item for item in diagnosis_orchestrator.store.list_probes(diagnosis_id)
        if (item.get("parameters") or {}).get("budget_phase") == "followup"
    ]
    assert created + second_created <= 3
    assert len(followups) <= 3


def test_ai_tree_stops_creating_followups_after_five_rounds(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[
            "sys_metrics",
            "perf_cpu",
            "ebpf_io",
            "memory_smaps",
            "baseline_window_profile",
        ],
    )
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]
    diagnosis_orchestrator.store.update_session(
        diagnosis_id,
        conclusion_versions=[{"version": index + 1} for index in range(6)],
    )

    created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["baseline_window_profile"],
        parent_task,
    )
    schedulable = diagnosis_orchestrator._has_schedulable_followup_work(
        diagnosis_id,
        ["baseline_window_profile"],
        diagnosis_orchestrator.store.list_probes(diagnosis_id),
        parent_task,
    )

    assert created == 0
    assert schedulable is False
    assert any(
        event["event_type"] == "followup_round_limit_reached"
        and event["payload"]["max_followup_rounds"] == 5
        for event in diagnosis_orchestrator.store.get_detail(diagnosis_id)["events"]
    )


def test_development_ai_tree_allows_mechanism_retry_beyond_five_rounds(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[*repo.agents["a1"].capabilities, "source_mechanism_query"],
    )
    payload = _payload("服务 service-a 内存持续增长")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]
    diagnosis_orchestrator.store.update_session(
        diagnosis_id,
        conclusion_versions=[{"version": index + 1} for index in range(6)],
    )

    created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["source_mechanism_query"],
        parent_task,
        probe_inputs={
            "source_mechanism_query": {
                "ai_generated_query": {
                    "candidate_id": "ai_proposal_retention",
                    "origin_parent_candidate_id": "coarse_python_memory_retention",
                    "query_spec_hash": "sha256:new-query",
                },
            },
        },
    )

    assert created == 1
    followup = next(
        probe for probe in diagnosis_orchestrator.store.list_probes(diagnosis_id)
        if (probe.get("parameters") or {}).get("evidence_gap") == "source_mechanism_query"
    )
    assert followup["parameters"]["followup_round"] == 5


def test_ai_tree_allows_third_followup_after_initial_analysis(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[
            "sys_metrics",
            "perf_cpu",
            "memory_smaps",
            "baseline_window_profile",
            "source_snapshot",
        ],
    )
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]
    diagnosis_orchestrator.store.update_session(
        diagnosis_id,
        conclusion_versions=[{"version": index + 1} for index in range(3)],
    )

    created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["source_snapshot"],
        parent_task,
    )

    assert created == 1
    followup = next(
        item
        for item in diagnosis_orchestrator.store.list_probes(diagnosis_id)
        if (item.get("parameters") or {}).get("evidence_gap") == "source_snapshot"
    )
    assert followup["parameters"]["followup_round"] == 2


def test_memory_investigation_does_not_fall_through_to_wait_or_network_probes():
    requests = orchestrator_module._merge_assessment_followups(
        ["off_cpu_wait_profile", "trace_endpoint_profile"],
        [],
        sufficient_dependency=False,
        memory_only=True,
    )

    assert requests == []


def test_downstream_assessment_has_minimal_industrial_adapter_evidence_plan(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "log_scan", "dependency_check", "redis_check"],
    )
    repo.register_agent(
        "a2", "host-2", "10.0.0.2",
        capabilities=["sys_metrics", "log_scan", "dependency_check", "redis_check"],
    )
    payload = _payload("service-a 延迟升高，逐层检查调用链真正根因")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    payload["context"]["instances"].append({
        "service_id": "redis",
        "instance_id": "redis-1",
        "host_id": "host-2",
        "agent_id": "a2",
        "pid": 4321,
        "environment": "production",
    })
    payload["context"]["dependencies"] = [{
        "source_service": "service-a",
        "target_service": "redis",
        "relation": "CALLS",
        "protocol": "redis",
        "host": "127.0.0.1",
        "port": 6379,
        "confidence": "high",
        "source": "test_topology",
    }]

    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    probes = _sys_metric_probe_by_instance(data)
    downstream_hot = _normal_summary()
    downstream_hot.update({"avg_cpu_user_pct": 91.0, "avg_cpu_sys_pct": 6.0, "load1m": 12.0})
    _finish_sys_metrics_task(probes["service-a-1"]["task_id"], _normal_summary())
    _finish_sys_metrics_task(probes["redis-1"]["task_id"], downstream_hot)
    for probe in diagnosis_orchestrator.store.list_probes(data["diagnosis_id"]):
        task_id = probe.get("task_id")
        if not task_id or probe["probe_id"] == "host_process_metrics":
            continue
        if probe["probe_id"] == "process_dependency_check":
            _finish_structured_task(task_id, "dependency_check_json", {
                "summary": {"failed_dependencies": []},
                "checks": [{"dependency_id": "redis", "success": True}],
            })
        elif probe["probe_id"] == "process_log_scan":
            _finish_structured_task(task_id, "log_window_json", {
                "summary": {"matched_records": 0},
                "error_clusters": [],
            })
        elif probe["probe_id"] == "process_redis_check":
            _finish_structured_task(task_id, "redis_check_json", {
                "connectivity": {"ping_ok": True, "exporter_up": True},
                "latency_summary": {"max_latency_ms": 1},
                "slowlog_summary": {"entry_count": 0},
            })

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    all_gaps = {
        (probe.get("parameters") or {}).get("evidence_gap")
        for probe in detail["probes"]
    }
    assert detail["latest_conclusion"]["cluster_assessment"]["classification"] == "downstream_dependency"
    assert {"dependency_check", "log_scan", "redis_check"}.issubset(all_gaps)
    dependency_probe = next(probe for probe in detail["probes"] if probe["probe_id"] == "process_dependency_check")
    redis_probe = next(probe for probe in detail["probes"] if probe["probe_id"] == "process_redis_check")
    assert dependency_probe["parameters"]["targets"][0]["host"] == "127.0.0.1"
    assert dependency_probe["parameters"]["targets"][0]["protocol"] == "redis"
    assert redis_probe["parameters"]["host"] == "127.0.0.1"
    assert redis_probe["parameters"]["port"] == 6379
    assert redis_probe["parameters"]["collector_invocation"]["target_config"]["redis_target"]["url"] == "redis://127.0.0.1:6379"


def test_downstream_container_initial_plan_includes_runtime_control_history(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "log_scan", "dependency_check", "runtime_control_history"],
    )
    repo.register_agent(
        "a2", "host-2", "10.0.0.2",
        capabilities=["sys_metrics", "log_scan", "dependency_check", "runtime_control_history"],
    )
    payload = _payload("service-a 延迟升高，逐层检查调用链真正根因")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    payload["budget"] = {
        "max_hosts": 5,
        "max_service_instances": 10,
        "max_topology_hops": 1,
        "max_duration_minutes": 10,
        "max_parallel_probes": 10,
        "max_artifact_size_mb": 500,
        "max_model_calls": 6,
        "max_medium_risk_probes": 5,
        "max_total_probe_cpu_seconds": 600,
        "follow_up_reserve_seconds": 0,
    }
    payload["context"]["instances"].append({
        "service_id": "paymentservice",
        "instance_id": "payment-1",
        "host_id": "host-2",
        "agent_id": "a2",
        "pid": 52544,
        "container_id": "payment-container-123",
        "environment": "production",
    })
    payload["context"]["dependencies"] = [{
        "source_service": "service-a",
        "target_service": "paymentservice",
        "relation": "CALLS",
        "protocol": "grpc",
        "host": "paymentservice",
        "port": 50051,
        "confidence": "high",
        "source": "test_topology",
    }]

    response = client.post("/api/v1/diagnoses", json=payload)

    assert response.status_code == 200
    detail = response.json()["data"]
    runtime_probes = [
        probe for probe in detail["probes"]
        if probe["probe_id"] == "process_runtime_control_history"
    ]
    assert len(runtime_probes) == 1
    assert runtime_probes[0]["target"]["instance_id"] == "payment-1"
    invocation = runtime_probes[0]["parameters"]["collector_invocation"]
    assert invocation["target_config"]["container_id"] == "payment-container-123"


def test_downstream_process_without_runtime_identity_does_not_plan_control_history(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "log_scan", "dependency_check", "runtime_control_history"],
    )
    repo.register_agent(
        "a2", "host-2", "10.0.0.2",
        capabilities=["sys_metrics", "log_scan", "dependency_check", "runtime_control_history"],
    )
    payload = _payload("service-a 延迟升高，逐层检查调用链真正根因")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    payload["budget"] = {
        "max_hosts": 5,
        "max_service_instances": 10,
        "max_topology_hops": 1,
        "max_duration_minutes": 10,
        "max_parallel_probes": 10,
        "max_artifact_size_mb": 500,
        "max_model_calls": 6,
        "max_medium_risk_probes": 5,
        "max_total_probe_cpu_seconds": 600,
        "follow_up_reserve_seconds": 0,
    }
    payload["context"]["instances"].append({
        "service_id": "paymentservice",
        "instance_id": "payment-1",
        "host_id": "host-2",
        "agent_id": "a2",
        "pid": 52544,
        "environment": "production",
    })
    payload["context"]["dependencies"] = [{
        "source_service": "service-a",
        "target_service": "paymentservice",
        "relation": "CALLS",
        "protocol": "grpc",
        "host": "paymentservice",
        "port": 50051,
        "confidence": "high",
        "source": "test_topology",
    }]

    response = client.post("/api/v1/diagnoses", json=payload)

    assert response.status_code == 200
    detail = response.json()["data"]
    assert not any(
        probe["probe_id"] == "process_runtime_control_history"
        and probe["target"]["instance_id"] == "payment-1"
        for probe in detail["probes"]
    )


def test_redis_dependency_session_tree_matches_cluster_conclusion(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "log_scan", "dependency_check", "redis_check", "perf_cpu", "off_cpu_wait_profile", "trace_endpoint_profile", "baseline_window_profile"],
    )
    payload = _payload("service-a 延迟升高，检查 Redis 依赖")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    payload["context"]["dependencies"] = [{
        "source_service": "service-a",
        "target_service": "redis-cart",
        "relation": "CALLS",
        "protocol": "redis",
        "host": "redis-cart",
        "port": 6379,
        "confidence": "high",
        "source": "test_topology",
    }]

    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    probes = _sys_metric_probe_by_instance(data)
    _finish_sys_metrics_task(probes["service-a-1"]["task_id"], _normal_summary())
    first_detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    for probe in first_detail["probes"]:
        task_id = probe.get("task_id")
        gap = (probe.get("parameters") or {}).get("evidence_gap")
        if not task_id or not gap:
            continue
        if gap == "dependency_check":
            _finish_structured_task(task_id, "dependency_check_json", {
                "summary": {"failed_dependencies": ["redis-cart"]},
                "checks": [{"dependency_id": "redis-cart", "success": False, "error_type": "tcp_timeout"}],
            })
        elif gap == "redis_check":
            _finish_structured_task(task_id, "redis_check_json", {
                "connectivity": {"ping_ok": False, "exporter_up": False, "error_type": "redis_unreachable"},
                "latency_summary": {"max_latency_ms": 0},
                "slowlog_summary": {"entry_count": 0},
            })
        elif gap == "log_scan":
            _finish_structured_task(task_id, "log_window_json", {
                "summary": {"matched_records": 2, "error_cluster_count": 1},
                "error_clusters": [{"message": "redis timeout", "count": 2}],
            })
        elif gap in {"cpu_profile", "off_cpu_wait_profile", "trace_endpoint_profile", "baseline_window_profile"}:
            repo.transition_task(task_id, TaskStatus.FAILED, "perf_event_paranoid=4 permission denied", Actor.AGENT)

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    conclusion = detail["latest_conclusion"]
    tree = conclusion["controlled_ai_tree"]
    primary_ids = tree["final_primary_causes"]
    pending_requests = set(conclusion["next_evidence_requests"])

    assert conclusion["cluster_assessment"]["classification"] == "downstream_dependency"
    assert conclusion["cluster_assessment"]["root_entity"] == "redis-cart"
    assert detail["status"] == "INSUFFICIENT_EVIDENCE"
    assert tree["final_supported_level"] == "service"
    assert primary_ids == []
    assert tree["layers"][0]["primary_causes"] == []
    assert tree["layers"][0]["unknown_causes"]
    assert any("redis" in node["candidate_id"] for node in tree["layers"][1]["unknown_causes"])
    assert "insufficient_data" not in primary_ids
    assert pending_requests.isdisjoint({"cpu_profile", "off_cpu_wait_profile", "trace_endpoint_profile", "baseline_window_profile"})


def test_target_scoped_redis_invocation_does_not_leak_between_diagnoses(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu", "ebpf_io", "memory_smaps", "dependency_check", "redis_check"],
    )

    def create_with_redis(service: str, redis_host: str, redis_port: int):
        payload = _payload(f"{service} 延迟升高，检查 Redis 依赖")
        payload["auto_execute_policy"] = "all_registered"
        payload["context"]["service_id"] = service
        payload["context"]["instances"][0]["service_id"] = service
        payload["context"]["instances"][0]["instance_id"] = f"{service}-1"
        payload["context"]["dependencies"] = [{
            "source_service": service,
            "target_service": f"{service}-redis",
            "relation": "CALLS",
            "protocol": "redis",
            "host": redis_host,
            "port": redis_port,
            "confidence": "high",
            "source": "test_topology",
        }]
        data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
        parent_task = repo.tasks[data["child_task_ids"][0]]
        diagnosis_orchestrator._plan_followup_requests(
            data["diagnosis_id"],
            ["redis_check", "dependency_check"],
            parent_task,
        )
        for probe in diagnosis_orchestrator.store.list_probes(data["diagnosis_id"]):
            if (probe.get("parameters") or {}).get("evidence_gap") in {"redis_check", "dependency_check"}:
                diagnosis_orchestrator._schedule_probe(probe["step_id"])
        return client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]

    first = create_with_redis("service-a", "redis-a.local", 6379)
    second = create_with_redis("service-b", "redis-b.local", 6380)

    first_redis = next(
        probe for probe in first["probes"]
        if (probe.get("parameters") or {}).get("evidence_gap") == "redis_check"
    )
    second_redis = next(
        probe for probe in second["probes"]
        if (probe.get("parameters") or {}).get("evidence_gap") == "redis_check"
    )

    assert first_redis["parameters"]["collector_invocation"]["target_config"]["redis_target"]["url"] == "redis://redis-a.local:6379"
    assert second_redis["parameters"]["collector_invocation"]["target_config"]["redis_target"]["url"] == "redis://redis-b.local:6380"
    assert first_redis["parameters"]["collector_invocation"]["target_context"]["service_id"] == "service-a"
    assert second_redis["parameters"]["collector_invocation"]["target_context"]["service_id"] == "service-b"

    diagnosis_orchestrator._schedule_probe(first_redis["step_id"])
    diagnosis_orchestrator._schedule_probe(second_redis["step_id"])
    first_task = _task_for_step(first_redis["step_id"])
    second_task = _task_for_step(second_redis["step_id"])
    assert first_task.request_params["options"]["target_config"]["redis_target"]["host"] == "redis-a.local"
    assert second_task.request_params["options"]["target_config"]["redis_target"]["host"] == "redis-b.local"


def test_collect_analyze_followup_probe_then_reanalyze(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[
            "sys_metrics",
            "perf_cpu",
            "ebpf_io",
            "memory_smaps",
            "off_cpu_wait_profile",
            "trace_endpoint_profile",
            "baseline_window_profile",
        ],
    )
    payload = _payload()
    payload["auto_execute_policy"] = "all_registered"
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    initial_task_id = data["child_task_ids"][0]
    hot_summary = _normal_summary()
    hot_summary.update({"avg_cpu_user_pct": 94.0, "load1m": 9.0})
    _finish_sys_metrics_task(initial_task_id, hot_summary)

    first_detail = client.get(f"/api/v1/diagnoses/{diagnosis_id}").json()["data"]
    auto_depth_task_ids = [
        probe["task_id"]
        for probe in first_detail["probes"]
        if probe.get("probe_id") != "host_process_metrics" and probe.get("task_id")
    ]
    assert auto_depth_task_ids

    for task_id in auto_depth_task_ids:
        _finish_depth_task(task_id)

    second_detail = client.get(f"/api/v1/diagnoses/{diagnosis_id}").json()["data"]
    assert second_detail["latest_conclusion"]
    assert any(
        item.get("observed_value", {}).get("task_id") in auto_depth_task_ids
        for item in second_detail["evidence"]
    )
    assert second_detail["latest_conclusion"]["coverage"]["task_count"] >= 2


def test_all_registered_policy_can_schedule_high_risk_followup(client: TestClient):
    repo.agents["a1"].capabilities = [*repo.agents["a1"].capabilities, "off_cpu_wait_profile"]
    payload = _payload("服务 service-a 内存压力升高")
    payload["auto_execute_policy"] = "all_registered"
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]
    created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["off_cpu_wait_profile"],
        parent_task,
    )
    assert created == 1
    followup = next(
        item for item in diagnosis_orchestrator.store.list_probes(diagnosis_id)
        if (item.get("parameters") or {}).get("evidence_gap") == "off_cpu_wait_profile"
    )
    assert followup["requires_approval"] is False
    assert followup["parameters"]["execution_policy"] == "all_registered"
    assert followup["status"] in {"SCHEDULED", "RUNNING", "WAITING_APPROVAL", "UNAVAILABLE"}


def test_deferred_followup_reloaded_before_conclusion(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[*repo.agents["a1"].capabilities, "off_cpu_wait_profile"],
    )
    payload = _payload("服务 service-a 内存压力升高，请继续定位到函数")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    payload["budget"] = {
        "max_parallel_probes": 1,
        "max_total_probe_cpu_seconds": 3600,
        "follow_up_reserve_seconds": 0,
    }
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]
    repo.transition_task(parent_task.id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)

    created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["off_cpu_wait_profile"],
        parent_task,
    )
    planned = next(
        probe for probe in diagnosis_orchestrator.store.list_probes(diagnosis_id)
        if (probe.get("parameters") or {}).get("evidence_gap") == "off_cpu_wait_profile"
    )
    assert created == 1
    assert planned["status"] == "PLANNED"

    hot_summary = _normal_summary()
    hot_summary.update({"avg_cpu_user_pct": 93.0, "load1m": 8.0})
    repo.transition_task(parent_task.id, TaskStatus.UPLOADING, "collected", Actor.AGENT)
    repo.transition_task(parent_task.id, TaskStatus.ANALYZING, "analyzing", Actor.ANALYZER)
    repo.add_artifacts(parent_task.id, [{
        "artifact_type": "sys_metrics",
        "object_key": f"tasks/{parent_task.id}/sys_metrics.json",
        "metadata": {
            "data": {
                "sample_count": 10,
                "summary": hot_summary,
            },
        },
    }])
    repo.transition_task(parent_task.id, TaskStatus.DONE, "analysis complete", Actor.ANALYZER)

    detail = client.get(f"/api/v1/diagnoses/{diagnosis_id}").json()["data"]
    followup = next(
        probe for probe in detail["probes"]
        if (probe.get("parameters") or {}).get("evidence_gap") == "off_cpu_wait_profile"
    )

    assert detail["status"] == "COLLECTING"
    assert followup["status"] in {"PLANNED", "SCHEDULED", "RUNNING"}
    if followup["task_id"]:
        assert followup["task_id"] in detail["child_task_ids"]
    assert any(
        probe["status"] in {"PLANNED", "SCHEDULED", "RUNNING"}
        for probe in detail["probes"]
        if (probe.get("parameters") or {}).get("evidence_gap")
    )


def test_stale_running_child_task_is_failed_before_waiting(client: TestClient, monkeypatch):
    monkeypatch.setenv("MINI_DROP_DIAGNOSIS_TASK_STALE_GRACE_SEC", "0")
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    task_id = data["child_task_ids"][0]
    repo.transition_task(task_id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
    repo.tasks[task_id].started_at = repo.tasks[task_id].started_at - timedelta(seconds=999)

    detail = client.get(f"/api/v1/diagnoses/{diagnosis_id}").json()["data"]
    probe = next(item for item in detail["probes"] if item.get("task_id") == task_id)

    assert repo.tasks[task_id].status == TaskStatus.FAILED
    assert probe["status"] == "FAILED"


def test_default_policy_auto_executes_registered_r2_followup(client: TestClient):
    data = client.post("/api/v1/diagnoses", json=_payload("服务 service-a 内存压力升高")).json()["data"]
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu", "ebpf_io", "memory_smaps", "off_cpu_wait_profile"],
    )
    parent_task = repo.tasks[data["child_task_ids"][0]]

    created = diagnosis_orchestrator._plan_followup_requests(
        data["diagnosis_id"],
        ["off_cpu_wait_profile"],
        parent_task,
    )

    followup = next(
        item for item in diagnosis_orchestrator.store.list_probes(data["diagnosis_id"])
        if (item.get("parameters") or {}).get("evidence_gap") == "off_cpu_wait_profile"
    )
    assert created == 1
    assert data["risk_budget"]["auto_execute_policy"] == "all_registered"
    assert followup["requires_approval"] is False
    assert followup["status"] in {"SCHEDULED", "RUNNING", "PLANNED"}


def test_probe_fingerprint_reuses_stable_blocked_result_between_diagnoses(client: TestClient):
    first = client.post("/api/v1/diagnoses", json=_payload("服务 service-a 内存压力升高")).json()["data"]
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=["sys_metrics", "perf_cpu", "ebpf_io", "memory_smaps", "off_cpu_wait_profile"],
    )
    first_parent = repo.tasks[first["child_task_ids"][0]]
    diagnosis_orchestrator._plan_followup_requests(
        first["diagnosis_id"],
        ["off_cpu_wait_profile"],
        first_parent,
    )
    first_probe = next(
        item for item in diagnosis_orchestrator.store.list_probes(first["diagnosis_id"])
        if (item.get("parameters") or {}).get("evidence_gap") == "off_cpu_wait_profile"
    )
    first_task = repo.tasks[first_probe["task_id"]]
    repo.transition_task(first_task.id, TaskStatus.RUNNING, "agent accepted", Actor.SERVER)
    repo.transition_task(first_task.id, TaskStatus.FAILED, "perf_event_paranoid=4 permission denied", Actor.AGENT)

    second = client.post("/api/v1/diagnoses", json=_payload("服务 service-a 内存压力升高")).json()["data"]
    second_parent = repo.tasks[second["child_task_ids"][0]]
    diagnosis_orchestrator._plan_followup_requests(
        second["diagnosis_id"],
        ["off_cpu_wait_profile"],
        second_parent,
    )
    second_probe = next(
        item for item in diagnosis_orchestrator.store.list_probes(second["diagnosis_id"])
        if (item.get("parameters") or {}).get("evidence_gap") == "off_cpu_wait_profile"
    )

    assert second_probe["task_id"] == first_task.id
    assert second_probe["status"] == "FAILED"
    assert any(
        event["event_type"] == "probe_reused"
        and event["payload"].get("reuse_status") == "reuse_blocked_result"
        for event in diagnosis_orchestrator.store.get_detail(second["diagnosis_id"])["events"]
    )


def test_completed_probe_is_not_reused_across_diagnosis_cohorts(client: TestClient):
    first = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    first_probe = next(probe for probe in first["probes"] if probe["probe_id"] == "host_process_metrics")
    _finish_sys_metrics_task(first_probe["task_id"], _normal_summary())

    second = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    second_probe = next(probe for probe in second["probes"] if probe["probe_id"] == "host_process_metrics")

    assert second["target_scope"]["evidence_cohort_id"] == second["diagnosis_id"]
    assert first_probe["task_id"] != second_probe["task_id"]
    first_options = repo.tasks[first_probe["task_id"]].request_params["options"]
    second_options = repo.tasks[second_probe["task_id"]].request_params["options"]
    assert first_options["evidence_cohort_id"] != second_options["evidence_cohort_id"]


def test_dependency_conclusion_keeps_function_depth_requests():
    assessment = {
        "classification": "downstream_dependency",
        "confidence": 0.82,
        "supported_level": "process",
        "primary_anchor": {"supported_level": "process"},
    }
    session = {
        "target_scope": {
            "dependency_targets": [{
                "dependency_id": "redis-cart",
                "protocol": "redis",
                "host": "redis-cart",
            }],
        },
    }

    requests = orchestrator_module._assessment_followup_requests(assessment, session)

    assert {"dependency_check", "log_scan", "redis_check"} <= set(requests)
    assert {"cpu_profile", "off_cpu_wait_profile", "trace_endpoint_profile"} <= set(requests)


def test_memory_followup_is_sequential_heap_runtime_then_source():
    assessment = {"classification": "self_code_or_process_pressure", "primary_anchor": {"supported_level": "process"}}
    base = {"normalized_intent": {"symptom": "memory_pressure"}, "target_scope": {}}

    assert orchestrator_module._assessment_followup_requests(assessment, base) == ["python_heap_profile"]
    heap_done = {
        **base,
        "completed_depth_evidence_gaps": ["python_heap_profile"],
        "probe_evidence_status": {"python_heap_profile": "valid"},
    }
    assert orchestrator_module._assessment_followup_requests(assessment, heap_done) == ["python_runtime_profile"]
    runtime_done = {
        **heap_done,
        "completed_depth_evidence_gaps": ["python_heap_profile", "python_runtime_profile"],
        "probe_evidence_status": {"python_heap_profile": "valid", "python_runtime_profile": "valid"},
    }
    assert orchestrator_module._assessment_followup_requests(assessment, runtime_done) == ["source_snapshot"]


def test_memory_followup_continues_after_heap_failure():
    assessment = {"classification": "self_code_or_process_pressure", "primary_anchor": {"supported_level": "process"}}
    base = {"normalized_intent": {"symptom": "memory_pressure"}, "target_scope": {}}
    heap_failed = {
        **base,
        "probe_evidence_status": {"python_heap_profile": "failed"},
        "probe_attempt_counts": {"python_heap_profile": 1},
    }
    assert orchestrator_module._assessment_followup_requests(assessment, heap_failed) == ["python_runtime_profile"]


def test_missing_candidate_provenance_is_orphan_not_coarse_fallback():
    node = orchestrator_module.AITreeCandidateNode(
        candidate_id="orphan-alternative",
        relation="alternative",
        role="unknown",
        claim="未绑定来源的候选",
        supported_level="process",
    )
    result = orchestrator_module._attach_coarse_parent_if_missing(node, "coarse_real")
    assert result.parent_candidate_ids == []
    assert result.origin_parent_candidate_id is None
    assert result.node_type == "orphan"


def test_unparented_alternative_stays_orphan_unless_explicitly_independent():
    base = {
        "classification": "self_code_or_process_pressure",
        "summary": "目标进程存在异常压力，仍需继续核验。",
        "supported_level": "process",
        "confidence": 0.3,
        "evidence_refs": ["ev-rss"],
        "conclusion_eligible": False,
    }
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-alternative-provenance",
        cluster_assessment=base,
        candidates=[{
            "candidate_id": "unbound-alternative",
            "description": "没有来源的备选",
            "root_entity": "worker",
            "max_supported_level": "process",
            "relation": "alternative",
            "evidence_refs": ["ev-rss"],
        }, {
            "candidate_id": "independent-alternative",
            "description": "明确独立的备选",
            "root_entity": "worker",
            "max_supported_level": "process",
            "relation": "alternative",
            "independent": True,
            "evidence_refs": ["ev-rss"],
        }],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )
    nodes = {
        node.candidate_id: node
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    assert nodes["unbound-alternative"].node_type == "orphan"
    assert nodes["unbound-alternative"].parent_candidate_ids == []
    assert nodes["independent-alternative"].parent_candidate_ids == [tree.emitted_coarse_ids[0]]


def test_session_tree_preserves_single_source_hash_and_rejects_revision_conflict():
    kwargs = {
        "diagnosis_id": "diag-source",
        "cluster_assessment": {
            "classification": "self_code_or_process_pressure",
            "summary": "Rule.compile 行候选需要源码验证。",
            "supported_level": "line",
            "max_supported_level": "line",
            "confidence": 0.6,
            "evidence_refs": ["ev-line"],
        },
        "candidates": [{
            "candidate_id": "rule_compile",
            "rank": 1,
            "description": "Rule.compile retained allocation",
            "confidence_level": "中",
            "evidence_refs": ["ev-line"],
        }],
        "followup_requests": [],
        "probes": [],
    }
    single = orchestrator_module._build_session_controlled_ai_tree(
        **kwargs,
        child_trees=[{"source_context_hash": "sha256:analyzer", "layers": []}],
        source_snapshot_hashes=["sha256:one"],
    )
    conflict = orchestrator_module._build_session_controlled_ai_tree(
        **kwargs,
        child_trees=[{"source_context_hash": "sha256:analyzer", "layers": []}],
        source_snapshot_hashes=["sha256:one", "sha256:two"],
    )

    assert single.source_context_hash == "sha256:one"
    assert conflict.source_context_hash is None
    assert conflict.final_supported_level == "function"
    assert "revision" in conflict.stop_reason


def test_session_tree_ignores_non_source_analyzer_hashes_for_line_boundary():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-source-hash-kind",
        cluster_assessment={
            "classification": "python_memory_retention",
            "summary": "源码和内存证据已定位到代码行。",
            "supported_level": "line",
            "confidence": 0.9,
            "evidence_refs": ["ev-source"],
            "conclusion_eligible": True,
            "claim_type": "direct_root_cause",
            "causal_status": "supported",
            "mechanism": "python_code_constant_retention",
            "claim_target": "routing.py:988",
        },
        candidates=[],
        followup_requests=[],
        probes=[],
        child_trees=[
            {"source_context_hash": "sha256:generic-analyzer", "layers": []},
            {"source_context_hash": "sha256:verified-source", "layers": []},
        ],
        source_snapshot_hashes=["sha256:verified-source"],
    )

    assert tree.source_context_hash == "sha256:verified-source"
    assert tree.final_supported_level == "line"
    assert "冲突" not in tree.stop_reason


def test_source_snapshot_hashes_only_include_valid_source_artifacts():
    hashes = orchestrator_module._source_snapshot_hashes([
            {"source_snapshot": {
                "source_context_hash": "sha256:one",
                "revision": "abc123",
                "evidence_validity": {"evidence_status": "valid"},
            }},
        {"source_snapshot": {
            "source_context_hash": "sha256:blocked",
            "evidence_validity": {"evidence_status": "blocked"},
        }},
        {"source_snapshot": {"source_context_hash": "sha256:one"}},
    ])

    assert hashes == ["sha256:one"]


def test_source_snapshot_upgrades_matching_call_path_anchor_to_line_only():
    anchor = {
        "supported_level": "call_path",
        "anchor": "Map.__init__ -> Rule.compile",
        "function": "Rule.compile",
        "file": "werkzeug/routing.py",
        "line": 768,
    }
    observations = [{
        "source_snapshot": {
            "source_context_hash": "sha256:verified",
            "revision": "abc123",
            "snippets": [{
                "file": "werkzeug/routing.py",
                "focus_line": 768,
                "lines": [{"line": 768, "text": "def compile(self):"}],
            }],
        },
    }]

    upgraded = orchestrator_module._verified_source_anchor(anchor, observations)
    mismatched = orchestrator_module._verified_source_anchor({**anchor, "line": 999}, observations)

    assert upgraded["supported_level"] == "line"
    assert upgraded["source_context_hash"] == "sha256:verified"
    assert mismatched["supported_level"] == "call_path"


def test_python_call_path_keeps_verified_project_frame_for_source_upgrade():
    values = {
        "depth_evidence_json": {
            "line_candidates": [
                {"file": "/usr/local/lib/python3.11/logging/__init__.py", "line": 630, "symbol": "formatTime"},
                {"file": "/opt/celery-src/celery/app/trace.py", "line": 222, "symbol": "handle_failure"},
            ],
        },
    }
    call_paths = [{
        "call_path": ["worker", "handle_failure", "formatTime"],
        "function": "formatTime",
        "file": "/usr/local/lib/python3.11/logging/__init__.py",
        "line": 630,
    }]

    candidate = orchestrator_module._source_line_candidate_for_call_path(values, call_paths)

    assert candidate == {
        "file": "/opt/celery-src/celery/app/trace.py",
        "line": 222,
        "symbol": "handle_failure",
    }


def test_unmatched_source_line_does_not_upgrade_call_path():
    anchor = {
        "supported_level": "call_path",
        "file": "/usr/local/lib/python3.11/logging/__init__.py",
        "line": 630,
        "function": "formatTime",
    }
    observations = [{
        "source_snapshot": {
            "source_context_hash": "sha256:verified",
            "revision": "abc123",
            "snippets": [{
                "file": "celery/app/trace.py",
                "focus_line": 222,
                "lines": [{"line": 222, "text": "handle_failure"}],
            }],
        },
    }]

    result = orchestrator_module._verified_source_anchor(anchor, observations)

    assert result["supported_level"] == "call_path"
    assert "source_context_hash" not in result


def test_verified_source_fallback_chooses_unique_failure_handling_line():
    anchor = {
        "supported_level": "call_path",
        "anchor": "worker -> redis -> logging",
        "file": "/usr/local/lib/python3.11/site-packages/redis/client.py",
        "line": 76,
        "function": "__setitem__",
        "runtime_line_candidates": [
            {"file": "/case/celery_case_tasks.py", "line": 27, "symbol": "unhandled_failure"},
            {"file": "/opt/celery-src/celery/app/trace.py", "line": 255, "symbol": "_log_error"},
        ],
    }
    observations = [{
        "evidence_refs": ["ev-source"],
        "source_snapshot": {
            "source_context_hash": "sha256:verified",
            "revision": "abc123",
            "snippets": [
                {"file": "celery/app/task.py", "focus_line": 27, "symbol": "unhandled_failure", "lines": [{"line": 27}]},
                {"file": "celery/app/trace.py", "focus_line": 255, "symbol": "_log_error", "lines": [{"line": 255}]},
            ],
            "evidence_validity": {"evidence_status": "valid"},
        },
    }]

    result = orchestrator_module._verified_source_anchor(anchor, observations)

    assert result["supported_level"] == "line"
    assert result["file"] == "celery/app/trace.py"
    assert result["line"] == 255
    assert result["source_hint_level"] == "partial_localization"
    assert result["root_claim_allowed"] is False


def test_celery_exception_helper_does_not_replace_trace_failure_anchor():
    values = {
        "depth_evidence_json": {
            "line_candidates": [
                {
                    "file": "/opt/celery-src/celery/utils/serialization.py",
                    "line": 164,
                    "symbol": "get_pickleable_exception",
                },
                {
                    "file": "/opt/celery-src/celery/app/trace.py",
                    "line": 647,
                    "symbol": "fast_trace_task",
                },
            ],
        },
    }
    call_paths = [{
        "call_path": ["fast_trace_task", "trace_task", "handle_failure", "get_pickleable_exception"],
        "function": "get_pickleable_exception",
        "file": "/opt/celery-src/celery/utils/serialization.py",
        "line": 164,
    }]

    candidate = orchestrator_module._source_line_candidate_for_call_path(values, call_paths)

    assert candidate == {
        "file": "/opt/celery-src/celery/app/trace.py",
        "line": 647,
        "symbol": "fast_trace_task",
    }


def test_source_snapshot_prefers_celery_trace_failure_frame_over_exception_helper():
    anchor = {
        "supported_level": "call_path",
        "anchor": "fast_trace_task -> trace_task -> handle_failure -> get_pickleable_exception",
        "file": "/usr/local/lib/python3.11/site-packages/logging/__init__.py",
        "line": 630,
        "function": "formatTime",
        "runtime_line_candidates": [
            {"file": "/opt/celery-src/celery/utils/serialization.py", "line": 164, "symbol": "get_pickleable_exception"},
            {"file": "/opt/celery-src/celery/app/trace.py", "line": 647, "symbol": "fast_trace_task"},
        ],
    }
    observations = [{
        "source_snapshot": {
            "source_context_hash": "sha256:verified",
            "revision": "abc123",
            "snippets": [
                {
                    "file": "celery/utils/serialization.py",
                    "focus_line": 164,
                    "symbol": "get_pickleable_exception",
                    "lines": [{"line": 164}],
                },
                {
                    "file": "celery/app/trace.py",
                    "focus_line": 647,
                    "symbol": "fast_trace_task",
                    "lines": [{"line": 647}],
                },
            ],
            "evidence_validity": {"evidence_status": "valid"},
        },
    }]

    result = orchestrator_module._verified_source_anchor(anchor, observations)

    assert result["supported_level"] == "line"
    assert result["file"] == "celery/app/trace.py"
    assert result["line"] == 647


def test_generic_direct_source_match_does_not_bypass_failure_line_selection():
    anchor = {
        "supported_level": "call_path",
        "anchor": "worker -> start -> poll",
        "function": "start",
        "file": "/opt/celery-src/celery/bootsteps.py",
        "line": 116,
        "runtime_line_candidates": [
            {"file": "/opt/celery-src/celery/bootsteps.py", "line": 116, "symbol": "start"},
            {"file": "/opt/celery-src/celery/app/trace.py", "line": 255, "symbol": "_log_error"},
        ],
    }
    observations = [{
        "source_snapshot": {
            "source_context_hash": "sha256:verified",
            "revision": "abc123",
            "snippets": [
                {"file": "celery/bootsteps.py", "focus_line": 116, "symbol": "start", "lines": [{"line": 116}]},
                {"file": "celery/app/trace.py", "focus_line": 255, "symbol": "_log_error", "lines": [{"line": 255}]},
            ],
            "evidence_validity": {"evidence_status": "valid"},
        },
    }]

    result = orchestrator_module._verified_source_anchor(anchor, observations)

    assert result["supported_level"] == "line"
    assert result["file"] == "celery/app/trace.py"
    assert result["line"] == 255


def test_source_snapshot_task_options_keep_session_line_candidates(monkeypatch):
    candidate = {"file": "/case/src/werkzeug/routing.py", "line": 1120, "symbol": "compile"}
    monkeypatch.setattr(
        diagnosis_orchestrator,
        "_session_line_candidates",
        lambda diagnosis_id: [candidate] if diagnosis_id == "diag-source" else [],
    )
    definition = orchestrator_module.get_probe("process_source_snapshot")
    step = {
        "diagnosis_id": "diag-source",
        "step_id": "step-source",
        "parameters": {"duration_sec": 10, "sample_rate": 1},
    }
    source_context = {
        "source_paths": ["/home/worker1/mini-drop-real-cases/werkzeug-1521"],
        "repo_revision": "a220671d",
    }
    target = {
        "pid": 1234,
        "agent_id": "a1",
        "host_id": "host-1",
        "service_id": "werkzeug-routing",
        "instance_id": "werkzeug-vulnerable",
        "source_context": source_context,
    }

    options = diagnosis_orchestrator._task_options_for_probe(
        step,
        definition,
        {"source_context": source_context},
        target,
    )

    assert options["line_candidates"] == [candidate]


def test_optional_mechanism_task_options_keep_guarded_ai_inputs(monkeypatch):
    candidate = {"file": "/case/src/werkzeug/routing.py", "line": 844, "symbol": "__init__"}
    monkeypatch.setattr(
        diagnosis_orchestrator,
        "_session_line_candidates",
        lambda diagnosis_id: [candidate],
    )
    source_context = {
        "source_paths": ["/home/worker1/mini-drop-real-cases/werkzeug-1521"],
        "repo_revision": "a220671d",
    }
    target = {
        "pid": 1234,
        "agent_id": "a1",
        "host_id": "host-1",
        "service_id": "werkzeug-routing",
        "instance_id": "werkzeug-vulnerable",
        "container_id": "container-1",
        "source_context": source_context,
    }
    generated_query = {
        "origin": "ai_guarded",
        "candidate_id": "ai_proposal_bound_method",
        "expected_relation": "supports",
        "query": "import python",
    }
    codeql_options = diagnosis_orchestrator._task_options_for_probe(
        {
            "diagnosis_id": "diag-source",
            "step_id": "step-codeql",
            "parameters": {
                "duration_sec": 30,
                "sample_rate": 1,
                "ai_generated_query": generated_query,
            },
        },
        orchestrator_module.get_probe("process_source_mechanism_query"),
        {"source_context": source_context},
        target,
    )
    pyheap_options = diagnosis_orchestrator._task_options_for_probe(
        {
            "diagnosis_id": "diag-source",
            "step_id": "step-pyheap",
                "parameters": {
                    "duration_sec": 30,
                    "sample_rate": 1,
                    "candidate_id": "ai_proposal_bound_method",
                    "object_type_hints": ["method", "code"],
            },
        },
        orchestrator_module.get_probe("process_python_heap_reference"),
        {"source_context": source_context},
        target,
    )

    assert codeql_options["ai_generated_query"] == generated_query
    assert codeql_options["collector_fingerprint"].startswith("sha256:")
    assert pyheap_options["candidate_id"] == "ai_proposal_bound_method"
    assert pyheap_options["object_type_hints"] == ["method", "code"]
    assert pyheap_options["collector_fingerprint"].startswith("sha256:")


def test_development_budget_accepts_requested_model_calls():
    requested = orchestrator_module.DiagnosisBudget(max_model_calls=12)

    budget = diagnosis_orchestrator._effective_budget("development", requested)

    assert budget.max_model_calls == 12


def test_memray_backed_python_memory_scope_starts_with_metrics_only():
    scope = {
        "source_context": {
            "language": "python",
            "memray_result_path": "/profiles/capture.bin",
        },
        "instances": [],
        "dependency_targets": [],
    }

    assert orchestrator_module._scope_probe_ids("memory_pressure", scope) == ["host_process_metrics"]


def test_memory_retention_anchor_prefers_memray_bytes_and_verified_source():
    observations = [
        {
            "collector_type": "python_heap_profile",
            "target": {"service_id": "werkzeug-routing", "instance_id": "vulnerable", "pid": 1234},
            "evidence_refs": ["ev-heap"],
            "python_heap_profile": {
                "evidence_validity": {"evidence_status": "valid"},
                "retained_allocation_hotspots": [{
                    "function": "compile",
                    "file": "/case/src/werkzeug/routing.py",
                    "line": 1120,
                    "size_bytes": 8_574_832,
                    "allocation_count": 63_992,
                    "call_path": ["compile", "_compile_builder", "bind", "Map.__init__"],
                }],
            },
        },
        {
            "collector_type": "source_snapshot",
            "target": {"service_id": "werkzeug-routing", "instance_id": "vulnerable", "pid": 1234},
            "evidence_refs": ["ev-source"],
            "source_snapshot": {
                "revision": "a220671d",
                "source_context_hash": "sha256:verified",
                "snippets": [{
                    "file": "src/werkzeug/routing.py",
                    "focus_line": 1120,
                    "lines": [{"line": 1120, "text": "co = types.CodeType(*code_args)"}],
                }],
            },
        },
    ]

    anchor = orchestrator_module._verified_source_anchor(
        orchestrator_module._memory_retention_anchor(observations),
        observations,
    )

    assert anchor["anchor_type"] == "verified_source_line"
    assert anchor["supported_level"] == "line"
    assert anchor["line"] == 1120
    assert anchor["size_bytes"] == 8_574_832
    assert anchor["evidence_refs"] == ["ev-heap", "ev-source"]


def test_ai_evidence_summary_keeps_memray_hotspot_and_source_text():
    heap = orchestrator_module._summarize_artifact_value("python_heap_profile_json", {
        "producer": "memray",
        "retained_allocation_hotspots": [{
            "function": "compile",
            "file": "/case/src/werkzeug/routing.py",
            "line": 1120,
            "size_bytes": 1024,
            "allocation_count": 8,
            "call_path": ["compile", "_compile_builder"],
        }],
    })
    source = orchestrator_module._summarize_artifact_value("source_snapshot_json", {
        "revision": "a220671d",
        "source_context_hash": "sha256:verified",
        "snippets": [{
            "file": "src/werkzeug/routing.py",
            "focus_line": 1120,
            "symbol": "compile",
            "lines": [{"line": 1120, "text": "co = types.CodeType(*code_args)"}],
        }],
    })

    assert heap["retained_allocation_hotspots"][0]["size_bytes"] == 1024
    assert source["snippets"][0]["lines"][0]["text"] == "co = types.CodeType(*code_args)"


def test_memory_retention_allocation_line_alone_is_not_root_cause_eligible():
    metadata = orchestrator_module._assessment_claim_metadata(
        classification="python_memory_retention",
        session={"target_scope": {"target_service": "werkzeug-routing"}},
        anchor={
            "anchor": "src/werkzeug/routing.py:844 __init__",
            "source_context_hash": "sha256:verified",
            "file": "src/werkzeug/routing.py",
            "line": 844,
            "function": "__init__",
            "size_bytes": 1024,
            "allocation_count": 8,
        },
        evidence_refs=["ev-heap", "ev-source"],
        downstream_dependency_failure=False,
        shared_iowait=False,
        neighbor_pressure=False,
        runtime_control=None,
    )

    assert metadata["conclusion_eligible"] is False
    assert metadata["claim_type"] == "direct_failure_mechanism"
    assert metadata["causal_status"] == "unproven"


def test_source_snapshot_ast_reference_hint_is_not_root_cause_eligible():
    metadata = orchestrator_module._assessment_claim_metadata(
        classification="python_memory_retention",
        session={"target_scope": {"target_service": "werkzeug-routing"}},
        anchor={
            "anchor": "src/werkzeug/routing.py:844 __init__",
            "source_context_hash": "sha256:verified",
            "file": "src/werkzeug/routing.py",
            "line": 844,
            "function": "__init__",
            "size_bytes": 1024,
            "allocation_count": 8,
            "reference_paths": [{
                "source_expression": "operation",
                "upstream_candidates": [{"expression": "self.converter.to_url"}],
                "container": "self.consts",
                "runtime_slot": "CodeType.co_consts",
                "retained_by": "types.FunctionType(code, {})",
            }],
        },
        evidence_refs=["ev-heap", "ev-source"],
        downstream_dependency_failure=False,
        shared_iowait=False,
        neighbor_pressure=False,
        runtime_control=None,
    )

    assert metadata["conclusion_eligible"] is False
    assert metadata["claim_type"] == "direct_failure_mechanism"
    assert metadata["mechanism"] == "python_memory_retention"


def test_codeql_and_pyheap_reference_chain_is_root_cause_eligible():
    metadata = orchestrator_module._assessment_claim_metadata(
        classification="python_memory_retention",
        session={"target_scope": {"target_service": "werkzeug-routing"}},
        anchor={
            "anchor": "src/werkzeug/routing.py:844 __init__",
            "source_context_hash": "sha256:verified",
            "source_revision": "a220671d",
            "file": "src/werkzeug/routing.py",
            "line": 844,
            "function": "__init__",
            "size_bytes": 1024,
            "allocation_count": 8,
            "mechanism_paths": [{
                "path_id": "bound-method-code-const",
                "candidate_id": "ai_proposal_bound_method",
                "candidate_relation": "supports",
                "anchor_matches": [0],
                "evidence_ref": "source_mechanism.mechanism_paths[0]",
            }],
            "runtime_reference_paths": [{
                "path_id": "function-to-map",
                "candidate_id": "ai_proposal_bound_method",
                "nodes": [{"type": "function"}, {"type": "Map"}],
                "evidence_ref": "python_heap_reference.reference_paths[0]",
            }],
        },
        evidence_refs=["ev-heap", "ev-codeql", "ev-pyheap"],
        downstream_dependency_failure=False,
        shared_iowait=False,
        neighbor_pressure=False,
        runtime_control=None,
    )

    assert metadata["conclusion_eligible"] is True
    assert metadata["claim_type"] == "direct_root_cause"
    assert metadata["mechanism"] == "python_code_constant_retention"


def test_memory_followup_requests_optional_mechanism_then_runtime_reference():
    assessment = {
        "classification": "python_memory_retention",
        "mechanism": "python_memory_retention",
        "primary_anchor": {
            "supported_level": "line",
            "file": "src/werkzeug/routing.py",
            "line": 844,
            "source_context_hash": "sha256:verified",
            "source_revision": "a220671d",
        },
    }
    base = {
        "normalized_intent": {"symptom": "memory_pressure"},
        "completed_depth_evidence_gaps": ["python_heap_profile", "python_runtime_profile", "source_snapshot"],
        "probe_evidence_status": {
            "python_heap_profile": "valid",
            "python_runtime_profile": "valid",
            "source_snapshot": "valid",
        },
    }

    assert orchestrator_module._assessment_followup_requests(assessment, base) == ["source_mechanism_query"]
    mechanism_done = {
        **base,
        "completed_depth_evidence_gaps": [*base["completed_depth_evidence_gaps"], "source_mechanism_query"],
        "probe_evidence_status": {**base["probe_evidence_status"], "source_mechanism_query": "valid"},
    }
    assert orchestrator_module._assessment_followup_requests(assessment, mechanism_done) == ["python_heap_reference"]
    blocked = {
        **base,
        "probe_evidence_status": {**base["probe_evidence_status"], "source_mechanism_query": "blocked"},
        "probe_attempt_counts": {"source_mechanism_query": 1},
    }
    assert orchestrator_module._assessment_followup_requests(assessment, blocked) == ["source_mechanism_query"]
    partial = {
        **base,
        "probe_evidence_status": {**base["probe_evidence_status"], "source_mechanism_query": "partial"},
        "probe_attempt_counts": {"source_mechanism_query": 1},
    }
    assert orchestrator_module._assessment_followup_requests(assessment, partial) == ["source_mechanism_query"]
    exhausted = {
        **partial,
        "probe_attempt_counts": {
            "source_mechanism_query": orchestrator_module.MAX_SOURCE_MECHANISM_ATTEMPTS,
        },
    }
    assert orchestrator_module._assessment_followup_requests(assessment, exhausted) == ["python_heap_reference"]


def test_source_mechanism_partial_allows_new_query_but_not_same_query(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[*repo.agents["a1"].capabilities, "source_mechanism_query"],
    )
    payload = _payload("服务 service-a 内存持续增长")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]
    target = data["target_scope"]["instances"][0]
    first_step = "step-codeql-first"
    diagnosis_orchestrator.store.add_probe({
        "step_id": first_step,
        "diagnosis_id": diagnosis_id,
        "probe_id": "process_source_mechanism_query",
        "target": target,
        "parameters": {
            "evidence_gap": "source_mechanism_query",
            "candidate_id": "ai_proposal_retention",
            "origin_parent_candidate_id": "coarse_python_memory_retention",
            "ai_generated_query": {"query_spec_hash": "sha256:first"},
        },
        "reason": "first attempt",
        "risk_level": "R2",
        "requires_approval": False,
        "status": "COMPLETED",
        "evidence_status": "partial",
    })

    duplicate = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["source_mechanism_query"],
        parent_task,
        probe_inputs={
            "source_mechanism_query": {
                "ai_generated_query": {"query_spec_hash": "sha256:first"},
                "candidate_id": "ai_proposal_retention",
                "origin_parent_candidate_id": "coarse_python_memory_retention",
            },
        },
    )
    fresh = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["source_mechanism_query"],
        parent_task,
        probe_inputs={
            "source_mechanism_query": {
                "ai_generated_query": {"query_spec_hash": "sha256:second"},
                "candidate_id": "ai_proposal_retention",
                "origin_parent_candidate_id": "coarse_python_memory_retention",
            },
        },
    )

    assert duplicate == 0
    assert fresh == 1
    mechanism_probes = [
        probe for probe in diagnosis_orchestrator.store.list_probes(diagnosis_id)
        if (probe.get("parameters") or {}).get("evidence_gap") == "source_mechanism_query"
    ]
    assert len(mechanism_probes) == 2
    assert mechanism_probes[-1]["parameters"]["ai_generated_query"]["query_spec_hash"] == "sha256:second"
    invocation = mechanism_probes[-1]["parameters"]["collector_invocation"]
    assert invocation["target_config"]["ai_generated_query"]["query_spec_hash"] == "sha256:second"


def test_source_mechanism_without_guarded_input_is_not_dispatched(client: TestClient):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[*repo.agents["a1"].capabilities, "source_mechanism_query"],
    )
    payload = _payload("服务 service-a 内存持续增长")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]

    created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["source_mechanism_query"],
        parent_task,
        probe_inputs={},
    )

    assert created == 0
    assert not any(
        (probe.get("parameters") or {}).get("evidence_gap") == "source_mechanism_query"
        for probe in diagnosis_orchestrator.store.list_probes(diagnosis_id)
    )
    events = (diagnosis_orchestrator.store.get_detail(diagnosis_id) or {}).get("events", [])
    assert any(
        event.get("event_type") == "followup_probe_input_missing"
        and (event.get("payload") or {}).get("evidence_gap") == "source_mechanism_query"
        for event in events
    )


def test_ordinary_verified_line_does_not_enter_optional_mechanism_branch():
    assessment = {
        "classification": "self_code_or_process_pressure",
        "primary_anchor": {
            "supported_level": "line",
            "file": "src/service.py",
            "line": 42,
            "source_context_hash": "sha256:verified",
            "source_revision": "abc123",
        },
    }
    session = {
        "normalized_intent": {"symptom": "cpu_saturation"},
        "completed_depth_evidence_gaps": ["cpu_profile", "source_snapshot"],
        "probe_evidence_status": {"cpu_profile": "valid", "source_snapshot": "valid"},
    }

    requests = orchestrator_module._assessment_followup_requests(assessment, session)

    assert "source_mechanism_query" not in requests
    assert "python_heap_reference" not in requests


def test_optional_mechanism_probe_failure_is_nonblocking():
    failed = type("Task", (), {"id": "task-codeql", "status": "FAILED"})()
    probes = [{
        "diagnosis_id": "diag-mechanism",
        "task_id": "task-codeql",
        "parameters": {"evidence_gap": "source_mechanism_query"},
    }]

    assert orchestrator_module._failed_tasks_are_only_optional_mechanism_followups(
        "diag-mechanism", [failed], probes
    ) is True

    initial_failure = type("Task", (), {"id": "task-metrics", "status": "FAILED"})()
    assert orchestrator_module._failed_tasks_are_only_optional_mechanism_followups(
        "diag-mechanism",
        [initial_failure],
        [{
            "diagnosis_id": "diag-mechanism",
            "task_id": "task-metrics",
            "parameters": {"evidence_gap": "host_process_metrics"},
        }],
    ) is False


def test_session_tree_rebuilds_supported_and_refuted_codeql_candidates():
    assessment = {
        "classification": "python_memory_retention",
        "summary": "Memray 已定位表层分配行，CodeQL 正在区分持有机制。",
        "supported_level": "line",
        "confidence": 0.8,
        "evidence_refs": ["ev-heap", "ev-codeql"],
        "conclusion_eligible": False,
            "claim_type": "direct_failure_mechanism",
            "causal_status": "supported",
            "claim_target": "src/werkzeug/routing.py:844",
            "primary_anchor": {
                "source_context_hash": "sha256:test",
                "source_revision": "rev-1",
                "file": "src/werkzeug/routing.py",
                "line": 844,
                "mechanism_paths": [
                {
                    "candidate_id": "ai_proposal_bound_method",
                    "candidate_relation": "supports",
                    "anchor_matches": [0],
                    "summary": "converter.to_url reaches generated code constants",
                    "evidence_ref": "source_mechanism.mechanism_paths[0]",
                },
                {
                    "candidate_id": "ai_proposal_defaults",
                    "candidate_relation": "refutes",
                    "anchor_matches": [0],
                    "summary": "defaults does not reach the generated code constant sink",
                    "evidence_ref": "source_mechanism.mechanism_paths[1]",
                },
            ],
            "runtime_reference_paths": [],
        },
    }

    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-werkzeug-mechanism",
        cluster_assessment=assessment,
        candidates=[],
        followup_requests=["python_heap_reference"],
        probes=[],
        child_trees=[],
    )

    nodes = {
        node.candidate_id: node
        for layer in tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
    }
    assert nodes["python_memory_retention"].role == "unknown"
    assert nodes["python_memory_retention"].conclusion_eligible is False
    assert all(node.depth_kind != "mechanism" for node in nodes.values())
    assert nodes["python_memory_retention"].node_type == "orphan"
    line_anchor = next(
        node for node in nodes.values()
        if node.node_type == "line_anchor"
    )
    assert line_anchor.parent_candidate_ids == ["coarse_python_memory_retention"]


def test_partial_codeql_chain_keeps_existing_candidate_unresolved_and_backtracks():
    observations = [{
        "evidence_refs": ["ev-codeql-partial"],
        "source_mechanism": {
            "revision": "abc123",
            "query": {
                "candidate_id": "python_runtime_stack_hotspot",
                "query_spec_hash": "sha256:partial",
            },
            "segment_coverage": {
                "required_segments": 3,
                "covered_segments": 1,
                "complete": False,
            },
            "evidence_validity": {
                "evidence_status": "partial",
                "reason": "codeql_incomplete_segment_coverage",
            },
            "mechanism_paths": [{
                "candidate_id": "python_runtime_stack_hotspot",
                "candidate_relation": "supports",
                "anchor_matches": [2, 3],
                "evidence_ref": "source_mechanism.mechanism_paths[0]",
            }],
        },
    }]
    anchor = orchestrator_module._mechanism_enriched_anchor({
        "source_context_hash": "sha256:verified",
        "source_revision": "abc123",
        "evidence_refs": ["ev-source"],
    }, observations)
    assessment = {
        "classification": "python_memory_retention",
        "summary": "表层内存保留位置已确认。",
        "supported_level": "line",
        "confidence": 0.8,
        "evidence_refs": anchor["evidence_refs"],
        "conclusion_eligible": False,
        "claim_type": "direct_failure_mechanism",
        "causal_status": "unproven",
        "primary_anchor": anchor,
    }
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-codeql-partial",
        cluster_assessment=assessment,
        candidates=[{
            "candidate_id": "python_runtime_stack_hotspot",
            "rank": 1,
            "description": "运行时热点候选",
            "evidence_refs": ["ev-source"],
            "max_supported_level": "line",
        }],
        followup_requests=["source_mechanism_query"],
        probes=[],
        child_trees=[],
    )

    unresolved = [
        node
        for layer in tree.layers
        for node in layer.unknown_causes
        if node.candidate_id == "python_runtime_stack_hotspot"
    ]
    assert unresolved
    assert unresolved[0].status == "supported"
    assert unresolved[0].causal_status == "unproven"
    assert unresolved[0].decision == "continue_probe"
    assert unresolved[0].depth_kind == "base"
    assert unresolved[0].node_type == "orphan"
    assert unresolved[0].origin_parent_candidate_id is None
    assert "python_runtime_stack_hotspot" not in tree.final_primary_causes
    assert all(node.depth_kind != "mechanism" for layer in tree.layers for node in [
        *layer.primary_causes,
        *layer.secondary_causes,
        *layer.rejected_causes,
        *layer.unknown_causes,
    ])


def test_source_mechanism_is_attached_only_after_verified_line_base_node():
    assessment = {
        "classification": "python_memory_retention",
        "summary": "源码行已被运行时证据定位。",
        "supported_level": "line",
        "confidence": 0.86,
        "evidence_refs": ["ev-runtime", "ev-source", "ev-codeql", "ev-heap"],
        "conclusion_eligible": True,
        "claim_type": "direct_root_cause",
        "causal_status": "supported",
        "claim_target": "celery/app/trace.py:1120",
        "mechanism": "python_traceback_retention",
        "primary_anchor": {
            "source_context_hash": "sha256:celery",
            "source_revision": "a83070e5ec748c32325332db422756cfdd709aae",
            "file": "celery/app/trace.py",
            "line": 1120,
            "mechanism_paths": [{
                "candidate_id": "ai_proposal_trace_cycle",
                "candidate_relation": "supports",
                "anchor_matches": [0],
                "summary": "failure traceback remains reachable from task exception handling",
                "evidence_ref": "ev-codeql",
            }],
            "runtime_reference_paths": [{
                "candidate_id": "ai_proposal_trace_cycle",
                "nodes": [{"type": "traceback"}, {"type": "task"}],
                "evidence_ref": "ev-heap",
            }],
        },
    }
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-celery-mechanism",
        cluster_assessment=assessment,
        candidates=[{
            "candidate_id": "celery-trace-line",
            "rank": 1,
            "description": "异常处理行保留 traceback",
            "evidence_refs": ["ev-runtime", "ev-source"],
            "max_supported_level": "line",
        }],
        followup_requests=[],
        probes=[{
            "parameters": {
                "candidate_id": "ai_proposal_trace_cycle",
                "origin_parent_candidate_id": "celery-trace-line",
            },
            "status": "COMPLETED",
            "evidence_status": "valid",
        }],
        child_trees=[],
    )

    nodes = {
        node.candidate_id: node
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    mechanism = nodes["ai_proposal_trace_cycle"]
    line_id = orchestrator_module._verified_line_candidate_id(
        assessment["primary_anchor"],
        assessment["classification"],
    )
    assert mechanism.depth_kind == "mechanism"
    assert mechanism.supported_level == "call_path"
    assert mechanism.parent_candidate_ids == ["celery-trace-line"]
    assert mechanism.origin_parent_candidate_id == "celery-trace-line"

    invalid_origin = [
        {**item, "origin_parent_candidate_id": "coarse_python_memory_retention"}
        for item in assessment["primary_anchor"]["mechanism_paths"]
    ]
    invalid_assessment = {
        **assessment,
        "primary_anchor": {**assessment["primary_anchor"], "mechanism_paths": invalid_origin},
    }
    invalid_tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-celery-invalid-mechanism-parent",
        cluster_assessment=invalid_assessment,
        candidates=[{
            "candidate_id": "celery-trace-line",
            "rank": 1,
            "description": "异常处理行保留 traceback",
            "evidence_refs": ["ev-runtime", "ev-source"],
            "max_supported_level": "line",
        }],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )
    assert all(
        node.depth_kind != "mechanism"
        for layer in invalid_tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    )


def test_duplicate_verified_line_hotspots_collapse_and_failed_deep_probe_returns_to_line():
    assessment = {
        "classification": "self_code_or_process_pressure",
        "summary": "源码热点已定位，但机制证据失败。",
        "supported_level": "line",
        "confidence": 0.68,
        "conclusion_eligible": False,
        "claim_type": "partial_localization",
        "causal_status": "unproven",
        "root_entity": "celery-worker",
        "claim_target": "celery/app/trace.py:258 _log_error",
        "primary_anchor": {
            "source_context_hash": "sha256:celery",
            "source_revision": "a83070e5ec748c32325332db422756cfdd709aae",
            "file": "celery/app/trace.py",
            "line": 258,
            "function": "_log_error",
        },
    }
    candidates = [
        {
            "candidate_id": candidate_id,
            "rank": rank,
            "description": "同一源码行的热点候选",
            "evidence_refs": ["ev-runtime"],
            "max_supported_level": "line",
        }
        for rank, candidate_id in enumerate(
            ["python_runtime_stack_hotspot", "python_userland_hotspot", "celery-worker"],
            start=1,
        )
    ]
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-celery-duplicate-line",
        cluster_assessment=assessment,
        candidates=candidates,
        followup_requests=["source_mechanism_query"],
        probes=[{
            "parameters": {
                "evidence_gap": "source_mechanism_query",
                "candidate_id": "ai_proposal_trace_retention",
                "origin_parent_candidate_id": "celery-worker",
            },
            "status": "FAILED",
            "evidence_status": "unparseable",
        }],
        child_trees=[],
    )
    line_id = orchestrator_module._verified_line_candidate_id(
        assessment["primary_anchor"],
        assessment["classification"],
    )
    nodes = {
        node.candidate_id: node
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    assert line_id not in nodes
    assert nodes["celery-worker"].supported_level == "line"
    assert line_id not in nodes
    boundary = nodes["gap_source_mechanism_query_ai_proposal_trace_retention"]
    assert boundary.parent_candidate_ids == ["celery-worker"]
    rollback = next(edge for edge in tree.probe_edges if edge.effect == "rollback")
    assert rollback.to_candidate_ids == ["celery-worker"]


def test_verified_source_line_is_synthesized_as_base_parent_for_mechanism():
    assessment = {
        "classification": "python_memory_retention",
        "summary": "异常处理源码行已验证。",
        "supported_level": "line",
        "confidence": 0.86,
        "evidence_refs": ["ev-runtime", "ev-source", "ev-codeql", "ev-heap"],
        "conclusion_eligible": True,
        "claim_type": "direct_root_cause",
        "causal_status": "supported",
        "claim_target": "celery/app/trace.py:1120",
        "mechanism": "python_traceback_retention",
        "primary_anchor": {
            "source_context_hash": "sha256:celery",
            "source_revision": "a83070e5ec748c32325332db422756cfdd709aae",
            "file": "celery/app/trace.py",
            "line": 1120,
            "function": "handle_failure",
            "mechanism_paths": [{
                "candidate_id": "ai_proposal_trace_cycle",
                "candidate_relation": "supports",
                "anchor_matches": [0],
                "summary": "failure traceback remains reachable from task exception handling",
                "evidence_ref": "ev-codeql",
                "origin_parent_candidate_id": "verified_line_placeholder",
            }],
            "runtime_reference_paths": [{
                "candidate_id": "ai_proposal_trace_cycle",
                "nodes": [{"type": "traceback"}, {"type": "task"}],
                "evidence_ref": "ev-heap",
            }],
        },
    }
    line_id = orchestrator_module._verified_line_candidate_id(
        assessment["primary_anchor"],
        assessment["classification"],
    )
    assessment["primary_anchor"]["mechanism_paths"][0]["origin_parent_candidate_id"] = line_id
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-synthesized-line-parent",
        cluster_assessment=assessment,
        candidates=[{
            "candidate_id": "python_runtime_stack_hotspot",
            "rank": 1,
            "description": "异常处理调用路径",
            "evidence_refs": ["ev-runtime"],
            "max_supported_level": "call_path",
        }],
        followup_requests=[],
        probes=[{
            "parameters": {
                "candidate_id": "ai_proposal_trace_cycle",
                "origin_parent_candidate_id": line_id,
            },
            "status": "COMPLETED",
            "evidence_status": "valid",
        }],
        child_trees=[],
    )

    nodes = {
        node.candidate_id: node
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    assert nodes[line_id].depth_kind == "base"
    assert nodes[line_id].supported_level == "line"
    assert nodes[line_id].parent_candidate_ids == ["coarse_python_memory_retention"]
    assert nodes[line_id].origin_parent_candidate_id == "coarse_python_memory_retention"
    assert "coarse_python_memory_retention" in nodes
    assert tree.final_primary_causes == [line_id]
    assert nodes["ai_proposal_trace_cycle"].parent_candidate_ids == [line_id]


def test_generic_event_loop_frame_does_not_upgrade_call_path_to_line():
    values = {
        "depth_evidence_json": {
            "line_candidates": [
                {"file": "/opt/celery-src/celery/bootsteps.py", "line": 116, "symbol": "start"},
                {"file": "/opt/celery-src/celery/worker/loops.py", "line": 97, "symbol": "asynloop"},
            ],
        },
    }
    call_paths = [{
        "call_path": ["worker", "start", "asynloop", "poll"],
        "function": "poll",
    }]

    assert orchestrator_module._source_line_candidate_for_call_path(values, call_paths) is None


def test_unbound_followup_does_not_create_orphan_boundary_node():
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-orphan-followup",
        cluster_assessment={
            "classification": "python_memory_retention",
            "summary": "已定位到源码行，但深探来源尚未保存。",
            "supported_level": "line",
            "confidence": 0.7,
            "conclusion_eligible": False,
            "primary_anchor": {
                "supported_level": "line",
                "blocked_upgrade_reason": "缺少源码机制证据。",
            },
        },
        candidates=[{
            "candidate_id": "line-root",
            "rank": 1,
            "description": "已定位的源码行",
            "max_supported_level": "line",
        }],
        followup_requests=["source_mechanism_query"],
        probes=[],
        child_trees=[],
    )

    deep_nodes = [
        node
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
        if node.depth_kind in {"mechanism", "boundary"}
    ]
    assert deep_nodes == []
    assert not any(node.candidate_id.startswith("gap_") for layer in tree.layers for node in layer.unknown_causes)


def test_previous_unrefuted_candidate_survives_blocked_deep_probe_as_checkpoint():
    previous_tree = {
        "layers": [{
            "layer_id": "prior-layer",
            "depth": 1,
            "primary_causes": [],
            "secondary_causes": [],
            "rejected_causes": [],
            "unknown_causes": [{
                "candidate_id": "python_code_constant_retention",
                "lineage_id": "python_code_constant_retention",
                "role": "unknown",
                "claim": "代码常量可能持有运行时对象。",
                "supported_level": "line",
                "confidence": 0.84,
                "status": "missing_evidence",
                "claim_type": "direct_failure_mechanism",
                "causal_status": "unproven",
                "decision": "continue_probe",
                "mechanism": "python_code_constant_retention",
                "target": "src/werkzeug/routing.py:844",
                "conclusion_eligible": False,
                "evidence_refs": ["ev-memray", "ev-source"],
                "self_challenge": {
                    "supporting_evidence_refs": ["ev-memray", "ev-source"],
                    "missing_evidence": ["source_mechanism_query"],
                },
            }],
        }],
    }
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-checkpoint",
        cluster_assessment={
            "classification": "python_memory_retention",
            "summary": "Memray 已确认对象持续保留。",
            "supported_level": "line",
            "confidence": 0.8,
            "evidence_refs": ["ev-memray", "ev-source"],
            "conclusion_eligible": False,
            "claim_type": "direct_failure_mechanism",
            "causal_status": "unproven",
        },
        candidates=[],
        followup_requests=["python_heap_reference"],
        probes=[],
        child_trees=[],
        previous_tree=previous_tree,
    )

    checkpoint = next(
        node
        for layer in tree.layers
        for node in layer.unknown_causes
        if node.candidate_id == "python_code_constant_retention"
    )
    assert checkpoint.status == "missing_evidence"
    assert checkpoint.causal_status == "unproven"
    assert checkpoint.decision == "continue_probe"
    assert checkpoint.evidence_refs == ["ev-memray", "ev-source"]
    assert checkpoint.conclusion_eligible is False
    assert "python_heap_reference" in checkpoint.self_challenge.missing_evidence


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        (
            {
                "abstained": False,
                "controlled_ai_tree": {"final_primary_causes": ["root-1"]},
                "root_cause_clusters": [],
            },
            "COMPLETED",
        ),
        (
            {
                "abstained": True,
                "controlled_ai_tree": {"final_primary_causes": []},
                "root_cause_clusters": [{"qualification": "possible_root_cause", "conclusion_eligible": False}],
                "possible_root_causes": [{"cluster_id": "possible-1"}],
            },
            "PARTIAL_COMPLETED",
        ),
        (
            {
                "abstained": True,
                "controlled_ai_tree": {"final_primary_causes": []},
                "root_cause_clusters": [{"qualification": "observation", "conclusion_eligible": False}],
                "possible_root_causes": [],
            },
            "INSUFFICIENT_EVIDENCE",
        ),
    ],
)
def test_terminal_status_uses_single_conclusion_eligibility_contract(conclusion, expected):
    assert orchestrator_module._diagnosis_terminal_status(conclusion).value == expected


def test_mechanism_anchor_only_closes_same_candidate_codeql_and_pyheap_paths():
    anchor = {
        "source_context_hash": "sha256:verified",
        "source_revision": "abc123",
        "evidence_refs": ["ev-source"],
    }
    observations = [
        {
            "evidence_refs": ["ev-codeql"],
            "source_mechanism": {
                "revision": "abc123",
                "evidence_validity": {"evidence_status": "valid"},
                "mechanism_paths": [{
                    "candidate_id": "ai_proposal_bound_method",
                    "candidate_relation": "supports",
                    "anchor_matches": [0],
                    "evidence_ref": "source_mechanism.mechanism_paths[0]",
                }],
            },
        },
        {
            "evidence_refs": ["ev-pyheap"],
            "python_heap_reference": {
                "evidence_validity": {"evidence_status": "valid"},
                "reference_paths": [{
                    "candidate_id": "ai_proposal_defaults",
                    "nodes": [{"type": "function"}, {"type": "Map"}],
                }],
            },
        },
    ]

    mismatched = orchestrator_module._mechanism_enriched_anchor(anchor, observations)
    observations[1]["python_heap_reference"]["reference_paths"][0]["candidate_id"] = "ai_proposal_bound_method"
    matched = orchestrator_module._mechanism_enriched_anchor(anchor, observations)

    assert mismatched["root_claim_allowed"] is False
    assert mismatched["runtime_reference_paths"] == []
    assert matched["root_claim_allowed"] is True
    assert matched["runtime_reference_paths"][0]["candidate_id"] == "ai_proposal_bound_method"


def test_werkzeug_1521_fixture_closes_bound_method_branch_and_greys_defaults():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "werkzeug_1521_mechanism_evidence.json").read_text(encoding="utf-8")
    )
    anchor = orchestrator_module._mechanism_enriched_anchor(
        {
            "anchor": "src/werkzeug/routing.py:844 BuilderCompiler.__init__",
            "file": "src/werkzeug/routing.py",
            "line": 844,
            "function": "BuilderCompiler.__init__",
            "source_context_hash": "sha256:werkzeug-1521",
            "source_revision": "a220671d66755a94630a212378754bb432811158",
            "size_bytes": 8_574_832,
            "allocation_count": 63_992,
            "evidence_refs": ["ev-memray", "ev-source"],
        },
        [
            {"source_mechanism": fixture["source_mechanism"], "evidence_refs": ["ev-codeql"]},
            {"python_heap_reference": fixture["python_heap_reference"], "evidence_refs": ["ev-pyheap"]},
        ],
    )
    metadata = orchestrator_module._assessment_claim_metadata(
        classification="python_memory_retention",
        session={"target_scope": {"target_service": "werkzeug-routing"}},
        anchor=anchor,
        evidence_refs=anchor["evidence_refs"],
        downstream_dependency_failure=False,
        shared_iowait=False,
        neighbor_pressure=False,
        runtime_control=None,
    )
    assessment = {
        "classification": "python_memory_retention",
        "summary": metadata["diagnostic_claim"],
        "supported_level": "line",
        "confidence": 0.93,
        "evidence_refs": anchor["evidence_refs"],
        "primary_anchor": anchor,
        **metadata,
    }
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-werkzeug-1521-fixture",
        cluster_assessment=assessment,
        candidates=[],
        followup_requests=[],
        probes=[{
            "parameters": {
                "candidate_id": "ai_proposal_bound_method_code_constant",
                "origin_parent_candidate_id": "python_memory_retention",
                "evidence_gap": "source_mechanism_query",
            },
        }, {
            "parameters": {
                "candidate_id": "ai_proposal_defaults",
                "origin_parent_candidate_id": "python_memory_retention",
                "evidence_gap": "source_mechanism_query",
            },
        }],
        child_trees=[],
        source_snapshot_hashes=["sha256:werkzeug-1521"],
    )
    nodes = {
        node.candidate_id: node
        for layer in tree.layers
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
    }

    source_symbols = [node["symbol"] for node in anchor["mechanism_paths"][0]["nodes"]]
    runtime_types = [node["type"] for node in anchor["runtime_reference_paths"][0]["nodes"]]
    assert source_symbols == [
        "converter.to_url",
        "BuilderCompiler.get_const",
        "self.consts.append",
        "types.CodeType",
    ]
    assert runtime_types == ["function", "code", "tuple", "method", "converter", "Map"]
    assert metadata["conclusion_eligible"] is True
    assert nodes["ai_proposal_bound_method_code_constant"].conclusion_eligible is False
    assert nodes["ai_proposal_bound_method_code_constant"].depth_kind == "mechanism"
    line_id = orchestrator_module._verified_line_candidate_id(anchor, "python_memory_retention")
    assert nodes["ai_proposal_bound_method_code_constant"].origin_parent_candidate_id == "python_memory_retention"
    assert "python_memory_retention" not in tree.final_primary_causes
    assert line_id not in tree.final_unknown_causes
    assert nodes["ai_proposal_defaults"].role == "rejected"
    assert nodes["ai_proposal_defaults"].status == "contradicted"


def test_all_registered_dependency_followup_schedules_function_depth_probes(client: TestClient):
    repo.register_agent(
        "a1",
        "host-1",
        "10.0.0.1",
        capabilities=[
        *repo.agents["a1"].capabilities,
        "dependency_check",
        "redis_check",
        "log_scan",
        "memory_smaps",
        "perf_cpu",
        "off_cpu_wait_profile",
        "trace_endpoint_profile",
        ],
    )
    payload = _payload("服务 service-a 内存压力升高，请继续定位到函数或调用路径")
    payload["budget_profile"] = "development"
    payload["auto_execute_policy"] = "all_registered"
    payload["context"]["dependencies"] = [{
        "source_service": "service-a",
        "target_service": "redis-cart",
        "relation": "READS_FROM",
        "protocol": "redis",
        "host": "redis-cart",
        "port": 6379,
        "url": "redis://redis-cart:6379",
        "confidence": "high",
        "source": "test",
    }]
    data = client.post("/api/v1/diagnoses", json=payload).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]

    created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["cpu_profile", "off_cpu_wait_profile", "trace_endpoint_profile"],
        parent_task,
    )

    assert created == 3
    followups = [
        probe for probe in diagnosis_orchestrator.store.list_probes(diagnosis_id)
        if (probe.get("parameters") or {}).get("evidence_gap")
    ]
    assert {
        "cpu_profile",
        "off_cpu_wait_profile",
        "trace_endpoint_profile",
    } <= {
        (probe.get("parameters") or {}).get("evidence_gap")
        for probe in followups
    }
    assert all(probe["requires_approval"] is False for probe in followups[-3:])
    assert all(probe["status"] in {"SCHEDULED", "PLANNED"} for probe in followups[-3:]), [
        {
            "probe_id": probe["probe_id"],
            "status": probe["status"],
            "task_id": probe["task_id"],
            "agent": {
                "status": repo.agents[probe["target"]["agent_id"]].status,
                "capabilities": repo.agents[probe["target"]["agent_id"]].capabilities,
            },
            "runner_task_kind": orchestrator_module.get_probe(probe["probe_id"]).runner_task_kind,
        }
        for probe in followups[-3:]
    ]

    _finish_sys_metrics_task(parent_task.id, _normal_summary())
    depth_gaps = {
        "cpu_profile",
        "off_cpu_wait_profile",
        "trace_endpoint_profile",
    }

    for _ in range(8):
        detail = client.get(f"/api/v1/diagnoses/{diagnosis_id}").json()["data"]
        depth_probes = [
            probe for probe in detail["probes"]
            if (probe.get("parameters") or {}).get("evidence_gap") in depth_gaps
        ]
        active = [
            probe for probe in depth_probes
            if probe.get("task_id")
            and orchestrator_module.status_value(repo.tasks[probe["task_id"]].status) in {
                "PENDING",
                "RUNNING",
                "UPLOADING",
                "ANALYZING",
            }
        ]
        planned = [probe for probe in depth_probes if probe["status"] == "PLANNED"]
        if not planned:
            break
        assert active, [
            {
                "probe_id": probe["probe_id"],
                "status": probe["status"],
                "task_id": probe["task_id"],
            }
            for probe in depth_probes
        ]
        for probe in active:
            _finish_depth_task(probe["task_id"])

    detail = client.get(f"/api/v1/diagnoses/{diagnosis_id}").json()["data"]
    depth_probes = [
        probe for probe in detail["probes"]
        if (probe.get("parameters") or {}).get("evidence_gap") in depth_gaps
    ]
    assert len(depth_probes) == 3
    assert all(probe["status"] in {"SCHEDULED", "RUNNING", "COMPLETED"} for probe in depth_probes)
    assert all(probe["task_id"] for probe in depth_probes)
    assert not any(probe["status"] == "PLANNED" for probe in depth_probes)


def _origin_backtrack_tree(*, probe_status="BLOCKED", evidence_status="blocked", multiple_parents=False):
    origin = "line_rule_compile"
    other_parent = "python_memory_retention"
    path = {
        "candidate_id": "bound_method_retention",
        "candidate_relation": "supports",
        "anchor_matches": [0],
        "origin_parent_candidate_id": origin,
        "parent_candidate_ids": [origin, other_parent] if multiple_parents else [origin],
        "evidence_ref": "source_mechanism.mechanism_paths[0]",
    }
    assessment = {
        "classification": "python_memory_retention",
        "summary": "源码行已定位，机制深探尚未完成。",
        "supported_level": "line",
        "confidence": 0.85,
        "conclusion_eligible": True,
        "claim_type": "direct_root_cause",
        "causal_status": "supported",
        "mechanism": "python_code_constant_retention",
        "claim_target": "werkzeug-routing",
        "primary_anchor": {
            "source_context_hash": "sha256:verified",
            "source_revision": "a220671d",
            "file": "src/werkzeug/routing.py",
            "line": 844,
            "mechanism_paths": [path],
        },
        "evidence_refs": ["ev-source"],
    }
    if evidence_status == "valid":
        assessment["primary_anchor"]["runtime_reference_paths"] = [{
            "candidate_id": "bound_method_retention",
            "evidence_ref": "python_heap_reference.reference_paths[0]",
        }]
    candidates = [
        {
            "candidate_id": origin,
            "rank": 1,
            "description": "源码行级基础定位",
            "evidence_refs": ["ev-source"],
            "max_supported_level": "line",
        },
    ]
    if multiple_parents:
        candidates.append({
            "candidate_id": other_parent,
            "rank": 2,
            "description": "另一条基础定位分支",
            "evidence_refs": ["ev-source"],
            "max_supported_level": "line",
        })
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-origin-backtrack",
        cluster_assessment=assessment,
        candidates=candidates,
        followup_requests=["source_mechanism_query"],
        probes=[{
            "parameters": {
                "evidence_gap": "source_mechanism_query",
                "candidate_id": "bound_method_retention",
                "origin_parent_candidate_id": origin,
            },
            "status": probe_status,
            "evidence_status": evidence_status,
        }],
        child_trees=[],
    )
    return tree


def test_blocked_deep_probe_rolls_back_to_its_origin_parent_only():
    tree = _origin_backtrack_tree()
    line_id = orchestrator_module._verified_line_candidate_id(
        {
            "file": "src/werkzeug/routing.py",
            "line": 844,
        },
        "python_memory_retention",
    )
    mechanism = next(
        node for layer in tree.layers for node in layer.unknown_causes
        if node.candidate_id == "bound_method_retention"
    )
    assert mechanism.origin_parent_candidate_id == "line_rule_compile"
    assert tree.final_primary_causes == []
    rollback = next(edge for edge in tree.probe_edges if edge.effect == "rollback")
    assert rollback.from_candidate_ids == ["bound_method_retention"]
    assert rollback.to_candidate_ids == ["line_rule_compile"]
    assert "coarse_" not in rollback.to_candidate_ids[0]


def test_conceptual_coarse_parent_maps_to_emitted_coarse_node_id():
    assert orchestrator_module._resolve_real_coarse_parent_id(
        "coarse_insufficient_evidence",
        "coarse_python_memory_retention",
    ) == "coarse_python_memory_retention"
    assert orchestrator_module._resolve_real_coarse_parent_id(
        "",
        "coarse_python_memory_retention",
    ) == "coarse_python_memory_retention"


def test_unparented_alternatives_are_orphans_without_emitted_provenance():
    assessment = {
        "classification": "self_code_or_process_pressure",
        "summary": "目标进程存在压力，其他分支尚缺证据。",
        "supported_level": "function",
        "confidence": 0.4,
        "evidence_refs": ["ev-runtime"],
        "conclusion_eligible": False,
        "alternative_hypotheses": [
            {
                "hypothesis": "same_host_noisy_neighbor",
                "status": "missing_evidence",
                "supported_level": "host",
                "reason": "同宿主窗口不足。",
            },
        ],
    }
    tree = orchestrator_module._build_session_controlled_ai_tree(
        diagnosis_id="diag-alternative-coarse-parent",
        cluster_assessment=assessment,
        candidates=[],
        followup_requests=[],
        probes=[],
        child_trees=[],
    )
    nodes = {
        node.candidate_id: node
        for layer in tree.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    alternative = nodes["unknown_same_host_noisy_neighbor"]
    assert alternative.node_type == "orphan"
    assert alternative.parent_candidate_ids == []
    assert alternative.origin_parent_candidate_id is None


def test_multiple_lineage_parents_collapse_to_the_single_origin_for_mechanism():
    tree = _origin_backtrack_tree(multiple_parents=True, probe_status="COMPLETED", evidence_status="partial")
    line_id = orchestrator_module._verified_line_candidate_id(
        {
            "file": "src/werkzeug/routing.py",
            "line": 844,
        },
        "python_memory_retention",
    )
    mechanism = next(
        node for layer in tree.layers for node in layer.unknown_causes
        if node.candidate_id == "bound_method_retention"
    )
    assert mechanism.parent_candidate_ids == ["line_rule_compile"]
    rollback = next(edge for edge in tree.probe_edges if edge.effect == "rollback")
    assert rollback.to_candidate_ids == ["line_rule_compile"]
    assert tree.final_primary_causes == []


def test_investigation_review_updates_existing_parent_and_appends_rollback_provenance():
    tree = _origin_backtrack_tree()
    tree = tree.model_copy(update={
        "budget": tree.budget.model_copy(update={"max_tree_depth": 6}),
    })
    review = {
        "selected_evidence_families": ["source_mechanism_query"],
        "candidate_updates": {
            "line_rule_compile": {
                "claim": "源码行父结论继续保留，深探尚未闭合。",
                "status": "partial",
                "causal_status": "inconclusive",
                "decision": "backtrack",
                "missing_evidence": ["source_mechanism_query"],
            },
        },
        "candidate_proposals": [{
            "candidate_id": "ai_proposal_followup_mechanism",
            "parent_candidate_ids": ["line_rule_compile"],
            "origin_parent_candidate_id": "line_rule_compile",
            "relation": "refinement",
            "claim": "异常路径可能保留 traceback 引用。",
            "mechanism": "traceback_reference_retention",
            "target": "celery/app/trace.py:844",
            "evidence_refs": ["ev-source"],
            "missing_evidence": ["source_mechanism_query"],
            "what_would_change_my_mind": "源码机制证据不支持该传播路径。",
        }],
        "rollback_edges": [{
            "edge_id": "rollback-followup-parent",
            "from_candidate_ids": ["ai_proposal_followup_mechanism"],
            "to_candidate_ids": ["line_rule_compile"],
            "transition_type": "backtrack",
            "effect": "rollback",
            "status": "blocked",
            "reason": "深探未完成，保留来源父结论。",
        }],
    }

    updated = orchestrator_module._apply_investigation_review(tree, review)
    nodes = {
        node.candidate_id: node
        for layer in updated.layers
        for node in [
            *layer.primary_causes,
            *layer.secondary_causes,
            *layer.rejected_causes,
            *layer.unknown_causes,
        ]
    }
    assert nodes["line_rule_compile"].claim == "源码行父结论继续保留，深探尚未闭合。"
    assert nodes["line_rule_compile"].status == "partial"
    assert nodes["ai_proposal_followup_mechanism"].generated_by == "ai_guarded"
    assert nodes["ai_proposal_followup_mechanism"].parent_candidate_ids == ["line_rule_compile"]
    rollback = next(edge for edge in updated.probe_edges if edge.edge_id == "rollback-followup-parent")
    assert rollback.from_candidate_ids == ["ai_proposal_followup_mechanism"]
    assert rollback.to_candidate_ids == ["line_rule_compile"]
    assert rollback.status == "blocked"


def test_supported_mechanism_is_additional_and_cannot_replace_base_primary():
    tree = _origin_backtrack_tree(probe_status="COMPLETED", evidence_status="valid")
    line_id = orchestrator_module._verified_line_candidate_id(
        {
            "file": "src/werkzeug/routing.py",
            "line": 844,
        },
        "python_memory_retention",
    )
    mechanism_layer = next(
        layer
        for layer in tree.layers
        if any(node.depth_kind == "mechanism" for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes])
    )
    mechanism_path = next(node for node in mechanism_layer.unknown_causes if node.depth_kind == "mechanism")
    assert mechanism_path.depth_kind == "mechanism"
    assert mechanism_path.conclusion_eligible is False
    assert mechanism_path.candidate_id not in tree.final_primary_causes
    assert tree.final_primary_causes == []


def _dag_node(candidate_id, *, parents=None, role="unknown", relation="alternative", depth_kind="base"):
    return AITreeCandidateNode(
        candidate_id=candidate_id,
        lineage_id=candidate_id,
        parent_candidate_ids=parents or [],
        origin_parent_candidate_id=(parents or [None])[0],
        relation=relation,
        role=role,
        claim=candidate_id,
        depth_kind=depth_kind,
    )


def test_dag_lineage_rebuilds_bidirectional_edges_for_multiple_parents_and_children():
    layers = [
        AITreeLayer(layer_id="l0", depth=0, unknown_causes=[_dag_node("root")]),
        AITreeLayer(
            layer_id="l1",
            depth=1,
            unknown_causes=[
                _dag_node("left", parents=["root"]),
                _dag_node("right", parents=["root"]),
            ],
        ),
        AITreeLayer(
            layer_id="l2",
            depth=2,
            unknown_causes=[_dag_node("merge", parents=["left", "right"], relation="causal_convergence")],
        ),
    ]
    validated = orchestrator_module._validate_tree_lineage(layers)
    nodes = {
        node.candidate_id: node
        for layer in validated
        for node in [*layer.primary_causes, *layer.secondary_causes, *layer.rejected_causes, *layer.unknown_causes]
    }
    assert nodes["root"].child_candidate_ids == ["left", "right"]
    assert nodes["merge"].parent_candidate_ids == ["left", "right"]
    assert nodes["left"].child_candidate_ids == ["merge"]
    assert nodes["right"].child_candidate_ids == ["merge"]


def test_dag_lineage_rejects_self_loop_and_missing_child_reference():
    with pytest.raises(ValueError, match="自身"):
        orchestrator_module._validate_tree_lineage([
            AITreeLayer(layer_id="l0", depth=0, unknown_causes=[_dag_node("root", parents=["root"])])
        ])
    orphan = _dag_node("root")
    orphan = orphan.model_copy(update={"child_candidate_ids": ["missing"]})
    with pytest.raises(ValueError, match="child_candidate_id 不存在"):
        orchestrator_module._validate_tree_lineage([
            AITreeLayer(layer_id="l0", depth=0, unknown_causes=[orphan])
        ])


def test_dag_lineage_preserves_missing_parent_as_orphan_data_quality_node():
    child = _dag_node("child", parents=["missing-parent"])
    validated = orchestrator_module._validate_tree_lineage([
        AITreeLayer(layer_id="l0", depth=0, unknown_causes=[_dag_node("root")]),
        AITreeLayer(layer_id="l1", depth=1, unknown_causes=[child]),
    ])

    node = validated[1].unknown_causes[0]
    assert node.node_type == "orphan"
    assert node.parent_candidate_ids == ["missing-parent"]
    assert node.origin_parent_candidate_id is None
    assert "missing-parent" in node.eligibility_reason


def test_dag_lineage_requires_unique_origin_for_multiple_real_parents():
    child = _dag_node("merge", parents=["left", "right"])
    child = child.model_copy(update={"origin_parent_candidate_id": None})
    validated = orchestrator_module._validate_tree_lineage([
        AITreeLayer(layer_id="l0", depth=0, unknown_causes=[
            _dag_node("left"),
            _dag_node("right"),
        ]),
        AITreeLayer(layer_id="l1", depth=1, unknown_causes=[child]),
    ])

    node = validated[1].unknown_causes[0]
    assert node.node_type == "orphan"
    assert node.parent_candidate_ids == ["left", "right"]
    assert node.origin_parent_candidate_id is None
