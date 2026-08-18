"""AI 集群诊断会话、探针审批、预算和证据链测试。"""

import pytest
from fastapi.testclient import TestClient

from server.app.common_utils import json_safe
from server.app.database import init_db, reset_engine
from server.app.diagnosis.audit_bundle import _structured_evidence, build_readiness_gate
from server.app.diagnosis.benchmark_score import aggregate_results, score_audit_bundle
from server.app.diagnosis import orchestrator as orchestrator_module
from server.app.main import app, repo
from server.app.main import diagnosis_orchestrator
from server.app.models import Base
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


def test_existing_structured_evidence_uses_legal_transition_and_completes(client: TestClient):
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
    transitions = [(event["from_status"], event["to_status"]) for event in detail["events"]]
    assert ("ANALYZING_EXISTING_DATA", "ANALYZING") in transitions
    assert (
        ("ANALYZING", "COLLECTING") in transitions
        or ("ANALYZING", "WAITING_APPROVAL") in transitions
        or ("ANALYZING", "CONCLUDING") in transitions
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
    assert any(check["name"] == "structured_evidence_non_empty" for check in bundle["readiness_gate"]["checks"])
    assert any(
        check["name"] == "required_collector_family_has_structured_artifact"
        for check in bundle["readiness_gate"]["checks"]
    )


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


def test_ai_controlled_tree_probe_selection_creates_followup(client: TestClient, monkeypatch):
    repo.register_agent(
        "a1", "host-1", "10.0.0.1",
        capabilities=[
            "sys_metrics",
            "redis_check",
        ],
    )
    repo.agents["a1"].capabilities = ["sys_metrics", "redis_check"]

    def fake_generate_tree(*, analyzer_result, **_kwargs):
        tree = analyzer_result.controlled_ai_tree.model_copy(deep=True)
        assert tree.probe_edges
        tree.probe_edges[0] = tree.probe_edges[0].model_copy(update={
            "probe_requests": ["redis_check"],
            "reason": "AI 从 Probe Manifest 中选择 Redis 专项检查下探。",
        })
        return tree

    monkeypatch.setattr(orchestrator_module, "generate_controlled_ai_tree", fake_generate_tree)
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    task_id = data["child_task_ids"][0]
    summary = _normal_summary()
    summary["avg_cpu_user_pct"] = 92.0
    _finish_sys_metrics_task(task_id, summary)

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]

    assert any(
        probe["probe_id"] == "process_redis_check"
        and (probe.get("parameters") or {}).get("evidence_gap") == "redis_check"
        for probe in detail["probes"]
    )


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


def test_session_controlled_tree_keeps_function_level_when_assessment_has_function_anchor():
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
    assert tree.layers[1].primary_causes[0].supported_level == "function"


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
    assert [node.candidate_id for node in layer1.primary_causes] == ["off_cpu_wait_hotspot"]
    assert any(node.candidate_id == "rejected_same_host_noisy_neighbor" for node in layer1.rejected_causes)
    assert any(node.candidate_id == "unknown_downstream_dependency" for node in layer1.unknown_causes)
    assert tree.layers[2].unknown_causes[0].candidate_id == "blocked_line_upgrade"
    assert tree.layers[2].unknown_causes[0].status == "forbidden"
    assert "blocked_line_upgrade" in tree.final_unknown_causes


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


def test_runtime_contention_query_plans_off_cpu_and_python_runtime(client: TestClient):
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
    assert "process_off_cpu_profile" in probe_ids
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
    off_cpu_probe = next(item for item in data["probes"] if item["probe_id"] == "process_off_cpu_profile")
    task_id = off_cpu_probe["task_id"]
    metrics_probe = next(item for item in data["probes"] if item["probe_id"] == "host_process_metrics")
    _finish_sys_metrics_task(metrics_probe["task_id"], _normal_summary())

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
    assert assessment["supported_level"] == "function"
    assert assessment["primary_anchor"]["anchor"] == "pthread_mutex_lock"
    assert "pthread_mutex_lock" in summary
    assert "interruptible_sleep_or_lock_wait" in summary
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
            },
            {
                "parameters": {"evidence_gap": "redis_check"},
                "status": "COMPLETED",
            },
            {
                "parameters": {"evidence_gap": "log_scan"},
                "status": "FAILED",
            },
        ],
    )

    assert filtered == ["log_scan"]


def test_completed_depth_evidence_is_not_requested_again_even_when_low_gain():
    filtered = orchestrator_module._filter_pending_evidence_requests(
        "diag-1",
        ["baseline_window_profile", "trace_endpoint_profile", "off_cpu_wait_profile"],
        [
            {
                "parameters": {"evidence_gap": "baseline_window_profile"},
                "status": "COMPLETED",
            },
            {
                "parameters": {"evidence_gap": "trace_endpoint_profile"},
                "status": "COMPLETED",
            },
        ],
        task_observations=[],
    )

    assert filtered == ["off_cpu_wait_profile"]


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


def test_continuous_baseline_artifacts_are_structured_for_diagnosis(client: TestClient):
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

    assert bundle["structured_evidence"]
    assert "service.hot_loop" in str(bundle["structured_evidence"])


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
        assert detail["latest_conclusion"]["root_cause_candidates"]
        assert detail["latest_conclusion"]["cluster_assessment"]["evidence_refs"]
        assert detail["latest_conclusion"]["diagnostic_commands"]
        assert all(cmd["auto_execute"] is False for cmd in detail["latest_conclusion"]["diagnostic_commands"])
        candidate = detail["latest_conclusion"]["root_cause_candidates"][0]
        assert candidate["confidence_level"] in {"低", "中", "高"}
        assert candidate["evidence_refs"]
        evidence_ids = {item["evidence_id"] for item in detail["evidence"]}
        assert set(candidate["evidence_refs"]).issubset(evidence_ids)
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
        assert detail["resource_budget"]["max_parallel_probes"] == 3
        assert detail["resource_budget"]["max_medium_risk_probes"] == 1

    def test_default_budget_is_180_with_60_second_followup_reserve(self, client: TestClient):
        detail = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]

        assert detail["resource_budget"]["max_total_probe_cpu_seconds"] == 180
        assert detail["resource_budget"]["follow_up_reserve_seconds"] == 60
        assert diagnosis_orchestrator._duration_limit(detail, "initial") == 120
        assert diagnosis_orchestrator._duration_limit(detail, "followup") == 180

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
        assert assessment["confidence_level"] in {"中", "高"}
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
    assert created == 4
    probes = diagnosis_orchestrator.store.list_probes(diagnosis_id)
    gaps = {
        (item.get("parameters") or {}).get("evidence_gap")
        for item in probes
        if (item.get("parameters") or {}).get("evidence_gap")
    }
    assert {
        "baseline_window_profile",
        "dependency_check",
        "log_scan",
        "redis_check",
    }.issubset(gaps)

    assert diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["off_cpu_wait_profile", "baseline_window_profile", "dependency_check", "log_scan", "redis_check"],
        parent_task,
    ) == 0


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
    assert detail["status"] == "COMPLETED"
    assert tree["final_supported_level"] == "service"
    assert any("redis" in item for item in primary_ids)
    assert tree["layers"][0]["primary_causes"]
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
