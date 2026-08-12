"""AI 集群诊断会话、探针审批、预算和证据链测试。"""

import pytest
from fastapi.testclient import TestClient

from server.app.database import init_db, reset_engine
from server.app.diagnosis.audit_bundle import build_readiness_gate
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
    assert ("ANALYZING", "COLLECTING") in transitions or ("ANALYZING", "CONCLUDING") in transitions


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
        response = client.post("/api/v1/diagnoses", json=_payload())
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
        data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
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

    def test_completed_probe_produces_evidence_linked_candidate(self, client: TestClient):
        data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
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
        data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
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
    repo.agents["a1"].capabilities = [
        *repo.agents["a1"].capabilities,
        "off_cpu_wait_profile",
        "baseline_window_profile",
        "log_scan",
        "dependency_check",
        "redis_check",
    ]
    data = client.post("/api/v1/diagnoses", json=_payload()).json()["data"]
    diagnosis_id = data["diagnosis_id"]
    parent_task = repo.tasks[data["child_task_ids"][0]]
    created = diagnosis_orchestrator._plan_followup_requests(
        diagnosis_id,
        ["baseline_window_profile", "off_cpu_wait_profile", "dependency_check", "log_scan", "redis_check", "unknown_request"],
        parent_task,
    )
    assert created == 5
    probes = diagnosis_orchestrator.store.list_probes(diagnosis_id)
    gaps = {
        (item.get("parameters") or {}).get("evidence_gap")
        for item in probes
        if (item.get("parameters") or {}).get("evidence_gap")
    }
    assert {
        "baseline_window_profile",
        "off_cpu_wait_profile",
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

    detail = client.get(f"/api/v1/diagnoses/{data['diagnosis_id']}").json()["data"]
    all_gaps = {
        (probe.get("parameters") or {}).get("evidence_gap")
        for probe in detail["probes"]
    }
    followup = [
        probe
        for probe in detail["probes"]
        if (probe.get("parameters") or {}).get("parent_task_id")
    ]
    gaps = {(probe.get("parameters") or {}).get("evidence_gap") for probe in followup}

    assert detail["latest_conclusion"]["cluster_assessment"]["classification"] == "downstream_dependency"
    assert {"dependency_check", "log_scan", "redis_check"}.issubset(all_gaps)
    assert "redis_check" in gaps
    dependency_probe = next(probe for probe in detail["probes"] if probe["probe_id"] == "process_dependency_check")
    redis_probe = next(probe for probe in followup if probe["probe_id"] == "process_redis_check")
    assert dependency_probe["parameters"]["targets"][0]["host"] == "127.0.0.1"
    assert dependency_probe["parameters"]["targets"][0]["protocol"] == "redis"
    assert redis_probe["parameters"]["host"] == "127.0.0.1"
    assert redis_probe["parameters"]["port"] == 6379
    assert redis_probe["parameters"]["collector_invocation"]["target_config"]["redis_target"]["url"] == "redis://127.0.0.1:6379"


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
    first_conclusion = first_detail["latest_conclusion"]
    assert first_conclusion["next_evidence_requests"]
    followup_task_ids = [
        probe["task_id"]
        for probe in first_detail["probes"]
        if (probe.get("parameters") or {}).get("parent_task_id") == initial_task_id
        and probe.get("task_id")
    ]
    assert followup_task_ids

    for task_id in followup_task_ids:
        _finish_depth_task(task_id)

    second_detail = client.get(f"/api/v1/diagnoses/{diagnosis_id}").json()["data"]
    assert len(second_detail["conclusion_versions"]) >= 2
    assert any(
        item.get("observed_value", {}).get("task_id") in followup_task_ids
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
