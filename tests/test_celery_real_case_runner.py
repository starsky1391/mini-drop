from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASE_ROOT = ROOT / "docs" / "real_cases" / "celery_8882"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CASE_ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRemote:
    def __init__(self):
        self.commands: list[str] = []

    def run(self, command: str, *, timeout: int = 600) -> str:
        self.commands.append(command)
        if command == "date -u +%Y-%m-%dT%H:%M:%S%z":
            return "2026-08-21T00:00:00+0000\n"
        if "docker inspect" in command:
            return f"1234|5678|{'a' * 64}\n"
        return ""

    def get_tree(self, remote_root: str, local_root: Path) -> None:
        local_root.mkdir(parents=True, exist_ok=True)


def test_pair_stage_modes_create_only_one_diagnosis(tmp_path, monkeypatch):
    runner = load_module("celery_case_runner", "run_case_vm.py")
    calls = {"api": 0, "diagnosis": 0, "diagnosis_revision": None}

    class FakeAPI:
        def __init__(self, api_key: str):
            assert api_key == "test-key"
            calls["api"] += 1

    def fake_diagnosis(api, target, source_context, timeout, time_range):
        calls["diagnosis"] += 1
        calls["diagnosis_revision"] = source_context["repo_revision"]
        return {"diagnosis_id": "diag-only", "detail": {"status": "PARTIAL_COMPLETED"}}

    monkeypatch.setattr(runner, "API", FakeAPI)
    monkeypatch.setattr(runner, "diagnosis", fake_diagnosis)
    remote = FakeRemote()

    vulnerable = runner.run_stage(
        remote,
        "test-key",
        stage="vulnerable",
        revision=runner.VULNERABLE_REVISION,
        remote_root_value="/tmp/python_worker_failure_case",
        duration_sec=60,
        diagnosis_timeout_sec=120,
        output_root=tmp_path,
        stage_role="diagnosis_target",
        diagnosis_mode="full",
        keep_running=False,
    )
    fixed = runner.run_stage(
        remote,
        "test-key",
        stage="fixed",
        revision=runner.FIXED_REVISION,
        remote_root_value="/tmp/python_worker_failure_case",
        duration_sec=60,
        diagnosis_timeout_sec=120,
        output_root=tmp_path,
        stage_role="regression_control",
        diagnosis_mode="none",
        keep_running=False,
    )

    assert calls == {
        "api": 1,
        "diagnosis": 1,
        "diagnosis_revision": runner.VULNERABLE_REVISION,
    }
    assert vulnerable["stage_role"] == "diagnosis_target"
    assert vulnerable["diagnosis_mode"] == "full"
    assert vulnerable["diagnosis"]["diagnosis_id"] == "diag-only"
    assert fixed["stage_role"] == "regression_control"
    assert fixed["diagnosis_mode"] == "none"
    assert "diagnosis" not in fixed
    assert vulnerable["runtime_manifest"]["workload"] == fixed["runtime_manifest"]["workload"]


def test_diagnosis_timeout_preserves_id_and_latest_detail(monkeypatch):
    runner = load_module("celery_case_timeout", "run_case_vm.py")
    calls = {"count": 0}

    class TimeoutAPI:
        def call(self, path, method="GET", payload=None, *, timeout=60):
            calls["count"] += 1
            if path == "/api/v1/diagnoses":
                return {"diagnosis_id": "diag-timeout"}
            return {
                "status": "RUNNING",
                "probes": [],
                "headline": "runtime observation still collecting",
            }

    monkeypatch.setattr(runner.time, "monotonic", iter([0.0, 2.0, 4.0]).__next__)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    result = runner.diagnosis(
        TimeoutAPI(),
        {"service_id": "celery-worker"},
        {"repo_revision": runner.VULNERABLE_REVISION},
        timeout=1,
        time_range={},
    )

    assert result["diagnosis_id"] == "diag-timeout"
    assert result["runner_status"] == "diagnosis_timeout"
    assert result["terminal"] is False
    assert result["detail"]["status"] == "RUNNING"


def test_run_stage_keeps_partial_result_when_producer_does_not_complete(tmp_path, monkeypatch):
    runner = load_module("celery_case_partial", "run_case_vm.py")

    class PartialRemote(FakeRemote):
        def run(self, command: str, *, timeout: int = 600) -> str:
            self.commands.append(command)
            if "submission_sample" in command:
                return ""
            if "date -u +%Y-%m-%dT%H:%M:%S%z" in command:
                return "2026-08-21T00:00:00+0000\n"
            if "docker inspect" in command:
                return f"1234|5678|{'a' * 64}\n"
            if "producer_complete" in command:
                raise RuntimeError("worker command failed (1): producer did not complete the workload")
            return ""

    class FakeAPI:
        def __init__(self, api_key: str):
            assert api_key == "test-key"

    monkeypatch.setattr(runner, "API", FakeAPI)
    monkeypatch.setattr(runner, "diagnosis", lambda *args, **kwargs: {"diagnosis_id": "diag", "detail": {"status": "COMPLETED"}})
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    remote = PartialRemote()

    result = runner.run_stage(
        remote,
        "test-key",
        stage="vulnerable",
        revision=runner.VULNERABLE_REVISION,
        remote_root_value="/tmp/python_worker_failure_case",
        duration_sec=60,
        diagnosis_timeout_sec=120,
        output_root=tmp_path,
        stage_role="diagnosis_target",
        diagnosis_mode="full",
        keep_running=True,
    )

    assert result["producer_completed"] is False
    assert result["diagnosis"]["diagnosis_id"] == "diag"
    assert result["stage_role"] == "diagnosis_target"


def test_vm_runtime_files_do_not_expose_oracle_labels():
    runner = load_module("celery_case_runtime_files", "run_case_vm.py")
    runtime_text = "\n".join(
        (CASE_ROOT / name).read_text(encoding="utf-8", errors="replace")
        for name in sorted(runner.RUNTIME_FILES)
    ).lower()

    assert "8882" not in runtime_text
    assert "9799" not in runtime_text
    assert "vulnerable" not in runtime_text
    assert "fixed" not in runtime_text


def write_stage_evidence(root: Path, revision: str, first_rss: int, second_rss: int) -> None:
    root.mkdir(parents=True)
    task_rows = [
        {
            "event": "failure_batch_barrier",
            "batch": 1,
            "rss_bytes": first_rss,
            "observed_at": "2026-08-21T00:00:10+00:00",
            "repo_revision": revision,
        },
        {
            "event": "failure_batch_barrier",
            "batch": 2,
            "rss_bytes": second_rss,
            "observed_at": "2026-08-21T00:00:20+00:00",
            "repo_revision": revision,
        },
    ]
    producer_rows = [
        {
            "event": "producer_complete",
            "submitted_failures": 2000,
            "submitted_controls": 1000,
            "repo_revision": revision,
        }
    ]
    worker_rows = [
        {"event": "rss_sample", "rss_bytes": first_rss, "observed_at": "2026-08-21T00:00:10+00:00"},
        {"event": "rss_sample", "rss_bytes": second_rss, "observed_at": "2026-08-21T00:00:20+00:00"},
    ]
    for name, rows in (
        ("task_observations.ndjson", task_rows),
        ("producer_observations.ndjson", producer_rows),
        ("worker_observations.ndjson", worker_rows),
    ):
        (root / name).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (root / "worker.log").write_text("unhandled-task-failure\n", encoding="utf-8")


def test_offline_oracle_requires_one_diagnosis_and_clean_control(tmp_path, monkeypatch):
    evaluator = load_module("celery_case_evaluator", "evaluate_case.py")
    workload = {"failure_count": 1000, "failure_batches": 2, "queue": "failure-workload"}
    source_node = {
        "candidate_id": "verified_line_trace",
        "depth_kind": "base",
        "claim": "celery/app/trace.py exception path",
    }
    tree = {
        "layers": [{"primary_causes": [source_node]}],
        "final_primary_causes": ["verified_line_trace"],
        "probe_edges": [],
    }
    diagnosis = {
        "diagnosis_id": "diag-only",
        "detail": {"conclusion_versions": [{"controlled_ai_tree": tree}], "probes": []},
    }
    run = {
        "case_id": "L4-CELERY-8882-EXCEPTION-MEMLEAK",
        "vulnerable": {
            "stage_role": "diagnosis_target",
            "diagnosis_mode": "full",
            "runtime_manifest": {
                "worker_pid": 100,
                "container_id": "a" * 64,
                "source_context": {"source_paths": ["/host/source"], "repo_revision": evaluator.VULNERABLE_REVISION},
                "workload": workload,
            },
            "diagnosis": diagnosis,
        },
        "fixed": {
            "stage_role": "regression_control",
            "diagnosis_mode": "none",
            "runtime_manifest": {
                "worker_pid": 200,
                "container_id": "b" * 64,
                "source_context": {"source_paths": ["/host/source"], "repo_revision": evaluator.FIXED_REVISION},
                "workload": workload,
            },
        },
    }
    evidence = tmp_path / "evidence"
    write_stage_evidence(evidence / "vulnerable", evaluator.VULNERABLE_REVISION, 40 << 20, 70 << 20)
    write_stage_evidence(evidence / "fixed", evaluator.FIXED_REVISION, 40 << 20, 44 << 20)
    oracle = {
        "expected_vulnerable_revision": evaluator.VULNERABLE_REVISION,
        "expected_fix_revision": evaluator.FIXED_REVISION,
    }

    result = evaluator.evaluate_run(run, tmp_path, oracle)
    assert result["checks"]["vulnerable_diagnosis_present"] is True
    assert result["checks"]["fixed_diagnosis_absent"] is True
    assert result["checks"]["worker_barriers_complete"] is True
    assert result["checks"]["runtime_visibility_clean"] is True

    run["fixed"]["diagnosis"] = diagnosis
    result = evaluator.evaluate_run(run, tmp_path, oracle)
    assert result["checks"]["fixed_diagnosis_absent"] is False

    del run["fixed"]["diagnosis"]
    del run["vulnerable"]["diagnosis"]
    result = evaluator.evaluate_run(run, tmp_path, oracle)
    assert result["checks"]["vulnerable_diagnosis_present"] is False
