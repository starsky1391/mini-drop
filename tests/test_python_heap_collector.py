"""Memray-backed Python heap collector tests."""

import json
import os
import signal
import subprocess
from unittest import mock

import agent.mini_drop_agent.collectors.python_heap as python_heap_module
from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.python_heap import PythonHeapCollector


def _task(tmp_path):
    return CollectorTask(
        id="heap-1",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={"instrumented_result": str(tmp_path / "capture.bin")},
    )


def test_memray_missing_emits_structured_blocked_artifact(tmp_path):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    with mock.patch("shutil.which", return_value=None):
        result = collector.collect(_task(tmp_path))
    assert result.ok is False
    payload = result.artifacts[0]["metadata"]["data"]
    assert payload["producer"] == "memray"
    assert payload["evidence_validity"]["evidence_status"] == "blocked"
    assert payload["evidence_validity"]["reason"] == "memray_not_installed"


def test_memray_attach_failure_is_failed_after_one_light_retry(tmp_path):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="heap-attach-failed",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=10,
        options={},
    )
    failed = mock.MagicMock(
        returncode=1,
        stdout=b"",
        stderr=b"memray attach failed: operation not permitted",
    )
    with mock.patch("shutil.which", return_value="/usr/bin/memray"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", return_value=failed) as run:
        result = collector.collect(task)

    assert result.ok is False
    # Attach permission/ptrace failures do not trigger a blind second attach.
    assert run.call_count == 1
    payload = result.artifacts[0]["metadata"]["data"]
    validity = payload["evidence_validity"]
    assert payload["mode"] == "failed"
    assert validity["evidence_status"] == "failed"
    assert validity["reason"] == "memray_attach_failed"
    assert validity["failure_type"] == "permission_denied"
    assert validity["failure_category"] == "permission_denied"
    assert validity["retry_attempted"] is False
    assert validity["retry_skipped_reason"] == (
        "retry_skipped_reason=ptrace_conflict_or_attach_permission_denied"
    )
    assert validity["stdout_excerpt"] == ""
    assert payload["retained_allocation_hotspots"] == []
    assert "attach_preflight" in payload


def test_memray_attach_failure_preserves_stdout_for_diagnosis(tmp_path, monkeypatch):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    helper = tmp_path / "memray-helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("MINI_DROP_MEMRAY_HELPER", str(helper))
    task = CollectorTask(
        id="heap-helper-output",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    )

    def fake_run(command, **_kwargs):
        if command[0] == str(helper):
            return mock.MagicMock(
                returncode=1,
                stdout=b"target process couldn't open the memray shared library",
                stderr=b"",
            )
        return mock.MagicMock(returncode=1, stdout=b"", stderr=b"")

    with mock.patch("shutil.which", return_value="/usr/bin/memray"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", side_effect=fake_run):
        result = collector.collect(task)

    payload = result.artifacts[0]["metadata"]["data"]
    validity = payload["evidence_validity"]
    assert validity["failure_type"] == "collector_exit_nonzero"
    assert validity["failure_category"] == "memray_attach_failed"
    assert "target process couldn't open" in validity["stdout_excerpt"]
    assert "target process couldn't open" in validity["detail"]


def test_attach_process_group_timeout_kills_the_group_and_marks_timeout(monkeypatch):
    collector = PythonHeapCollector()
    process = mock.MagicMock(pid=4321, returncode=-9)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["helper"], 1),
        (b"", b""),
    ]
    process.wait.side_effect = subprocess.TimeoutExpired(["helper"], 2)
    killpg = mock.Mock()
    fake_os = mock.Mock(
        killpg=killpg,
        SIGTERM=signal.SIGTERM,
        SIGKILL=9,
    )
    fake_os.name = "posix"
    monkeypatch.setattr(
        python_heap_module.subprocess,
        "Popen",
        mock.Mock(return_value=process),
    )
    monkeypatch.setattr(python_heap_module, "os", fake_os)

    result = collector._run_posix_process_group(["helper"], 1)

    assert result.returncode == 124
    assert "mini_drop_attach_process_group_timeout" in result.stderr.decode()
    assert killpg.call_count == 2
    assert killpg.call_args_list[0].args == (4321, mock.ANY)


def test_attach_preflight_records_managed_helper_availability(tmp_path):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="heap-preflight-helper",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    )

    with mock.patch.object(collector, "_pid_exists", return_value=True), mock.patch(
        "os.stat",
        side_effect=FileNotFoundError,
    ):
        result = collector._attach_preflight(task, helper_available=True)

    assert result["helper_available"] is True


def test_memray_helper_can_attach_without_preloading_target(tmp_path, monkeypatch):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    helper = tmp_path / "memray-helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("MINI_DROP_MEMRAY_HELPER", str(helper))
    task = CollectorTask(
        id="heap-helper",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    )
    capture = tmp_path / "out" / task.id / "memray.bin"

    def fake_run(command, **_kwargs):
        if command[0] == str(helper):
            capture.parent.mkdir(parents=True, exist_ok=True)
            capture.write_bytes(b"memray")
        elif "stats" in command:
            stats_path = command[command.index("-o") + 1]
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({"top_allocations_by_size": [{"location": "worker:worker.py:10", "size": 1, "count": 1}]}, handle)
        return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    with mock.patch("shutil.which", return_value="/usr/bin/memray"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", side_effect=fake_run) as run:
        result = collector.collect(task)

    assert result.ok is True
    assert any(call.args[0][0] == str(helper) for call in run.call_args_list)


def test_managed_helper_is_attempted_before_plain_memray_attach(tmp_path, monkeypatch):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    helper = tmp_path / "memray-helper-first"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("MINI_DROP_MEMRAY_HELPER", str(helper))
    task = CollectorTask(
        id="heap-helper-first",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    )
    capture = tmp_path / "out" / task.id / "memray.bin"
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command[0])
        if command[0] == str(helper):
            capture.parent.mkdir(parents=True, exist_ok=True)
            capture.write_bytes(b"memray")
        elif "stats" in command:
            stats_path = command[command.index("-o") + 1]
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "top_allocations_by_size": [{
                        "location": "worker:worker.py:10",
                        "size": 1,
                        "count": 1,
                    }],
                }, handle)
        return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    with mock.patch("shutil.which", return_value="/usr/bin/memray"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", side_effect=fake_run):
        result = collector.collect(task)

    assert result.ok is True
    assert calls[0] == str(helper)
    assert not any(command == "/usr/bin/memray" for command in calls[:1])


def test_managed_helper_phase_trace_is_saved_in_heap_evidence(tmp_path, monkeypatch):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    helper = tmp_path / "memray-helper-trace"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("MINI_DROP_MEMRAY_HELPER", str(helper))
    task = CollectorTask(
        id="heap-helper-trace",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    )
    capture = tmp_path / "out" / task.id / "memray.bin"

    def fake_run(command, **_kwargs):
        if command[0] == str(helper):
            capture.parent.mkdir(parents=True, exist_ok=True)
            capture.write_bytes(b"memray")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    b'{"phase":"runtime_resolved","host_pid":1234,"target_pid":17}\n'
                    b'{"phase":"runtime_staged","file_count":4}\n'
                    b'{"phase":"attach_started","method":"gdb"}\n'
                ),
                stderr=b"",
            )
        if "stats" in command:
            stats_path = command[command.index("-o") + 1]
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "top_allocations_by_size": [{
                        "location": "worker:worker.py:10",
                        "size": 1,
                        "count": 1,
                    }],
                }, handle)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    with mock.patch("shutil.which", return_value="/usr/bin/memray"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", side_effect=fake_run):
        result = collector.collect(task)

    assert result.ok is True
    payload = next(
        item["metadata"]["data"]
        for item in result.artifacts
        if item["artifact_type"] == "python_heap_profile_json"
    )
    assert payload["attach_preflight"]["helper_trace"]["completed_phases"] == [
        "runtime_resolved",
        "runtime_staged",
        "attach_started",
    ]
    assert payload["attach_preflight"]["helper_trace"]["events"][0]["target_pid"] == 17


def test_memray_helper_can_run_when_cli_is_not_installed(tmp_path, monkeypatch):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    helper = tmp_path / "memray-helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("MINI_DROP_MEMRAY_HELPER", str(helper))
    task = CollectorTask(
        id="heap-helper-without-cli",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    )
    capture = tmp_path / "out" / task.id / "memray.bin"

    def fake_run(command, **_kwargs):
        if command[0] == str(helper):
            capture.parent.mkdir(parents=True, exist_ok=True)
            capture.write_bytes(b"memray")
        elif "stats" in command:
            stats_path = command[command.index("-o") + 1]
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({"top_allocations_by_size": [{"location": "worker:worker.py:10", "size": 1, "count": 1}]}, handle)
        return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    with mock.patch("shutil.which", return_value=None), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", side_effect=fake_run) as run:
        result = collector.collect(task)

    assert result.ok is True
    assert any(call.args[0][0] == str(helper) for call in run.call_args_list)


def test_native_live_fallback_is_partial_and_never_emits_python_retention(tmp_path, monkeypatch):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    helper = tmp_path / "native-helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("MINI_DROP_NATIVE_HEAP_LIVE_HELPER", str(helper))
    task = CollectorTask(
        id="heap-native-live",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    )

    def fake_run(command, **_kwargs):
        if command[0] == str(helper):
            output_path = command[command.index("--output") + 1]
            output_path = str(output_path)
            with open(output_path, "w", encoding="utf-8") as handle:
                handle.write("native allocator sample\n")
            return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")
        return mock.MagicMock(returncode=1, stdout=b"", stderr=b"memray attach failed")

    with mock.patch("shutil.which", return_value="/usr/bin/memray"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", side_effect=fake_run):
        result = collector.collect(task)

    assert result.ok is True
    payload = next(item["metadata"]["data"] for item in result.artifacts if item["artifact_type"] == "python_heap_profile_json")
    assert payload["mode"] == "native_live"
    assert payload["heap_semantics"] == "native_allocation_observation"
    assert payload["evidence_validity"]["evidence_status"] == "partial"
    assert payload["retained_allocation_hotspots"] == []
    assert payload["line_candidates"] == []


def test_memray_missing_target_is_blocked_without_running_collector(tmp_path):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="heap-missing-pid",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    )
    with mock.patch("shutil.which", return_value="/usr/bin/memray"), mock.patch.object(
        collector, "_pid_exists", return_value=False
    ), mock.patch("subprocess.run") as run:
        result = collector.collect(task)

    assert result.ok is False
    run.assert_not_called()
    payload = result.artifacts[0]["metadata"]["data"]
    assert payload["mode"] == "blocked"
    assert payload["evidence_validity"]["reason"] == "missing_target_pid"
    assert payload["evidence_validity"]["failure_category"] == "target_exit"


def test_memray_preflight_namespace_failure_uses_stable_category(tmp_path):
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="heap-namespace",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={},
    )
    preflight = {
        "target_pid": 1234,
        "blocked_reason": "namespace_inaccessible",
        "failure_type": "namespace_unreachable",
        "detail": "PID namespace unavailable",
    }
    with mock.patch("shutil.which", return_value="/usr/bin/memray"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch.object(collector, "_attach_preflight", return_value=preflight):
        result = collector.collect(task)

    payload = result.artifacts[0]["metadata"]["data"]
    assert payload["evidence_validity"]["failure_category"] == "namespace_unreachable"


def test_memray_official_stats_are_normalized_with_lines(tmp_path, monkeypatch):
    result_path = tmp_path / "capture.bin"
    result_path.write_bytes(b"memray")
    monkeypatch.setenv("MINI_DROP_MEMRAY_ROOTS", str(tmp_path))
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")

    def fake_run(cmd, **_kwargs):
        output_path = cmd[cmd.index("-o") + 1]
        payload = {
            "metadata": {"peak_memory": 8192},
            "total_num_allocations": 32,
            "total_memory_allocated": 16384,
            "peak_memory_allocated": 8192,
            "top_allocations_by_size": [
                {"location": "compile:werkzeug/routing.py:768", "size": 4096, "count": 12},
            ],
            "top_allocations_by_count": [
                {"location": "bind:werkzeug/routing.py:915", "size": 2048, "count": 20},
            ],
        }
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    with mock.patch("shutil.which", return_value="/usr/bin/memray"), mock.patch(
        "subprocess.run", side_effect=fake_run
    ):
        result = collector.collect(_task(tmp_path))

    assert result.ok is True
    payload = next(item["metadata"]["data"] for item in result.artifacts if item["artifact_type"] == "python_heap_profile_json")
    assert payload["mode"] == "instrumented_result"
    assert payload["allocation_hotspots"][0]["function"] == "compile"
    assert payload["allocation_hotspots"][0]["line"] == 768
    assert payload["line_candidates"][0]["file"] == "werkzeug/routing.py"
    assert payload["evidence_validity"]["evidence_status"] == "valid"
    assert any(item["artifact_type"] == "memray_capture" for item in result.artifacts)


def test_instrumented_result_outside_spool_is_rejected(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"memray")
    monkeypatch.setenv("MINI_DROP_MEMRAY_ROOTS", str(allowed))
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = _task(tmp_path)
    with mock.patch("shutil.which", return_value="/usr/bin/memray"):
        result = collector.collect(task)
    assert result.ok is False
    assert "允许的 Memray 目录" in result.reason


def test_memray_compatible_official_reports_emit_retained_hotspots(tmp_path, monkeypatch):
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(json.dumps({
        "total_num_allocations": 200,
        "total_bytes_allocated": 32768,
        "metadata": {"peak_memory": 8192},
        "top_allocations_by_count": [
            {"location": "build_op:/case/src/werkzeug/routing.py:930", "count": 80},
        ],
    }), encoding="utf-8")
    leaks_path = tmp_path / "leaks.csv"
    leaks_path.write_text(
        "allocator,num_allocations,size,tid,thread_name,stack_trace\n"
        "PYMALLOC_MALLOC,12,4096,1,main,compile;/case/src/werkzeug/routing.py;1119|_compile_builder;/case/src/werkzeug/routing.py;1128\n"
        "PYMALLOC_MALLOC,8,2048,1,main,compile;/case/src/werkzeug/routing.py;1119|_compile_builder;/case/src/werkzeug/routing.py;1128\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_DROP_MEMRAY_ROOTS", str(tmp_path))
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="heap-reports",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={"memray_stats_path": str(stats_path), "memray_leaks_path": str(leaks_path)},
    )

    with mock.patch("shutil.which", return_value=None), mock.patch("subprocess.run") as run:
        result = collector.collect(task)

    assert result.ok is True
    run.assert_not_called()
    payload = next(item["metadata"]["data"] for item in result.artifacts if item["artifact_type"] == "python_heap_profile_json")
    assert payload["mode"] == "official_reports"
    assert payload["summary"]["total_memory_allocated"] == 32768
    assert payload["retained_allocation_hotspots"][0] == {
        "function": "compile",
        "file": "/case/src/werkzeug/routing.py",
        "line": 1119,
        "size_bytes": 6144,
        "allocation_count": 20,
        "call_path": ["compile", "_compile_builder"],
    }
    assert payload["line_candidates"][0]["evidence_ref"].startswith(
        "python_heap_profile.retained_allocation_hotspots"
    )
    assert payload["evidence_validity"]["reason"] == "memray_retained_allocation_stacks"
    assert any(item["artifact_type"] == "memray_leaks_csv" for item in result.artifacts)


def test_memray_aggregate_capture_accepts_official_leaks_without_stats(tmp_path, monkeypatch):
    capture_path = tmp_path / "aggregate.bin"
    capture_path.write_bytes(b"official-memray-aggregate")
    leaks_path = tmp_path / "leaks.csv"
    leaks_path.write_text(
        "allocator,num_allocations,size,tid,thread_name,stack_trace\n"
        "PYMALLOC_MALLOC,4,1024,1,main,compile;/case/src/werkzeug/routing.py;1119\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_DROP_MEMRAY_ROOTS", str(tmp_path))
    collector = PythonHeapCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = CollectorTask(
        id="heap-aggregate",
        collector_type="python_heap_profile",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={"instrumented_result": str(capture_path), "memray_leaks_path": str(leaks_path)},
    )

    with mock.patch("shutil.which", return_value=None), mock.patch("subprocess.run") as run:
        result = collector.collect(task)

    assert result.ok is True
    run.assert_not_called()
    payload = next(item["metadata"]["data"] for item in result.artifacts if item["artifact_type"] == "python_heap_profile_json")
    assert payload["summary"] == {
        "total_num_allocations": 0,
        "total_memory_allocated": 0,
        "peak_memory_allocated": 0,
    }
    assert payload["raw_artifact_refs"] == ["artifact:memray_capture", "artifact:memray_leaks_csv"]
