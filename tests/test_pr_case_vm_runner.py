from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "docs" / "real_cases" / "pr_cases" / "run_pr_case_vm.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("pr_case_vm_runner", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
