from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "docs" / "real_cases" / "pr_cases" / "run_pr_case_vm.py"
PR_CASES_ROOT = ROOT / "docs" / "real_cases" / "pr_cases"


def load_runner():
    spec = importlib.util.spec_from_file_location("pr_case_vm_runner", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pr_case_runner_defaults_use_400_second_workload_and_1200_second_diagnosis_wait():
    runner = load_runner()

    assert runner.DEFAULT_CASE_DURATION_SEC == 400
    assert runner.DEFAULT_DIAGNOSIS_TIMEOUT_SEC == 1200

    scripts = sorted(PR_CASES_ROOT.glob("*/run_vulnerable_only.ps1"))
    assert len(scripts) == 12
    for script in scripts:
        content = script.read_text(encoding="utf-8")
        assert "[int]$DurationSec = 400" in content
        assert "[int]$DiagnosisTimeoutSec = 1200" in content


def test_diagnosis_timeout_refreshes_control_plane_before_return(monkeypatch):
    runner = load_runner()
    calls = {"get_count": 0}

    class TimeoutAPI:
        def call(self, path, method="GET", body=None, timeout=90):
            if path == "/api/v1/diagnoses":
                return {"diagnosis_id": "diag-timeout"}
            calls["get_count"] += 1
            if calls["get_count"] > 1:
                return {"status": "COMPLETED", "headline": "final refresh completed"}
            return {"status": "RUNNING", "probes": []}

    monkeypatch.setattr(runner.time, "monotonic", iter([0.0, 0.5, 2.0]).__next__)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    result = runner.diagnosis(
        TimeoutAPI(),
        {
            "source_context": {"repo_revision": "revision"},
            "target": {"service_id": "service-a"},
            "diagnosis_query": "定位 Python CPU 热点",
        },
        duration_sec=400,
        timeout_sec=1,
    )

    assert result["diagnosis_id"] == "diag-timeout"
    assert result["terminal"] is True
    assert result["detail"]["status"] == "COMPLETED"
    assert calls["get_count"] == 2


def test_diagnosis_waits_for_runner_release_after_terminal_status(monkeypatch):
    runner = load_runner()
    calls = {"get_count": 0}

    class SettlingAPI:
        def call(self, path, method="GET", body=None, timeout=90):
            if path == "/api/v1/diagnoses":
                return {"diagnosis_id": "diag-settling"}
            calls["get_count"] += 1
            if calls["get_count"] == 1:
                return {
                    "status": "INSUFFICIENT_EVIDENCE",
                    "runner_control": {
                        "release_requested": False,
                        "reason": "outstanding_probes",
                        "outstanding_probes": [{"step_id": "step-source", "status": "RUNNING"}],
                    },
                    "probes": [{"step_id": "step-source", "status": "RUNNING"}],
                }
            return {
                "status": "INSUFFICIENT_EVIDENCE",
                "runner_control": {
                    "release_requested": True,
                    "reason": "diagnosis_settled",
                    "released_at": "2026-08-24T00:00:00+00:00",
                    "outstanding_probes": [],
                },
                "probes": [{"step_id": "step-source", "status": "COMPLETED"}],
            }

    monkeypatch.setattr(runner.time, "monotonic", iter([0.0, 1.0, 2.0]).__next__)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    result = runner.diagnosis(
        SettlingAPI(),
        {
            "source_context": {"repo_revision": "revision"},
            "target": {"service_id": "service-a"},
            "diagnosis_query": "定位 Python CPU 热点",
        },
        duration_sec=400,
        timeout_sec=10,
    )

    assert result["runner_release_reason"] == "diagnosis_settled"
    assert result["runner_release"]["release_requested"] is True
    assert calls["get_count"] == 2


def test_run_stage_stops_workload_after_runner_release(tmp_path, monkeypatch):
    runner = load_runner()
    commands: list[str] = []

    class ReleaseRemote:
        def run(self, command: str, *, timeout: int = 600) -> str:
            commands.append(command)
            if "docker inspect" in command:
                return f"1234|5678|{'a' * 64}|cid={'b' * 64} name=case-target-1\n"
            return ""

        def put_tree(self, local_root: Path, remote_root: str) -> None:
            return None

        def get_tree(self, remote_root: str, local_root: Path) -> None:
            local_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        runner,
        "diagnosis",
        lambda *args, **kwargs: {
            "diagnosis_id": "diag-release",
            "detail": {"status": "PARTIAL_COMPLETED"},
            "terminal": True,
            "runner_release_reason": "diagnosis_settled",
            "runner_release": {
                "release_requested": True,
                "reason": "diagnosis_settled",
                "released_at": "2026-08-24T00:00:00+00:00",
                "outstanding_probes": [],
            },
        },
    )

    case = {
        "case_id": "case-target",
        "repo": "https://example.invalid/repo.git",
        "compose_project": "case-target",
        "service_id": "case-target",
        "container_workdir": "/app",
        "language": "python",
        "target_pattern": "python",
        "workload": {"kind": "synthetic"},
        "diagnosis_query": "定位 Python CPU 热点",
    }

    result = runner.run_stage(
        ReleaseRemote(),
        case,
        revision="revision",
        stage="vulnerable",
        mode="vulnerable",
        duration_sec=400,
        diagnosis_timeout_sec=400,
        output_root=tmp_path,
        api_key="test-key",
    )

    assert result["runner_control"]["release_requested"] is True
    assert result["workload_stopped_by_runner_release"] is True
    assert not any("test -f" in command and "/complete" in command for command in commands)
