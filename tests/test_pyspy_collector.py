"""py-spy collector tests."""

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.pyspy import PySpyCollector


@pytest.fixture(name="collector")
def collector_fixture() -> PySpyCollector:
    return PySpyCollector()


@pytest.fixture(name="task")
def task_fixture() -> CollectorTask:
    return CollectorTask(id="pyspy_test_001", collector_type="pyspy", target_pid=1234, sample_rate=99, duration_sec=10)


def _write_raw(tmp_path, task, text="worker;run (app.py:12) 10\n"):
    raw_file = tmp_path / task.id / "pyspy.raw"
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_text(text, encoding="utf-8")
    return raw_file


def test_pyspy_not_installed(collector, task):
    with mock.patch("shutil.which", return_value=None):
        result = collector.collect(task)
    assert result.ok is False
    assert "py-spy" in result.reason


def test_stopped_target_returns_structured_blocked_artifact(collector, task, tmp_path):
    collector.OUTPUT_BASE = str(tmp_path)
    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch.object(collector, "_process_state", return_value="T"):
        result = collector.collect(task)
    payload = result.artifacts[0]["metadata"]["data"]
    assert result.ok is False
    assert payload["evidence_validity"]["evidence_status"] == "blocked"
    assert payload["evidence_validity"]["reason"] == "blocked_by_target_state"


def test_pid_not_exists(collector, task):
    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=False
    ):
        result = collector.collect(task)
    assert result.ok is False
    assert "不存在" in result.reason


def test_execution_success(collector, task, tmp_path):
    collector.OUTPUT_BASE = str(tmp_path)
    _write_raw(tmp_path, task)
    completed = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")
    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", return_value=completed):
        result = collector.collect(task)
    assert result.ok is True
    assert result.artifacts[0]["artifact_type"] == "pyspy_raw"
    assert any(item["artifact_type"] == "top_json" for item in result.artifacts)
    assert any(item["artifact_type"] == "python_stack_samples_json" for item in result.artifacts)


def test_command_uses_raw_format_and_task_sample_rate(collector, task, tmp_path):
    collector.OUTPUT_BASE = str(tmp_path)
    _write_raw(tmp_path, task)
    completed = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")
    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", return_value=completed) as run_mock:
        result = collector.collect(task)
    assert result.ok is True
    cmd = run_mock.call_args.args[0]
    assert cmd[cmd.index("-r") + 1] == str(task.sample_rate)
    assert cmd[cmd.index("--format") + 1] == "raw"


def test_nonzero_exit(collector, task, tmp_path):
    collector.OUTPUT_BASE = str(tmp_path)
    completed = mock.MagicMock(returncode=1, stdout=b"", stderr=b"process is not a Python program")
    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", return_value=completed):
        result = collector.collect(task)
    assert result.ok is False
    assert "执行失败" in result.reason


@pytest.mark.parametrize(
    "native_error",
    [
        b"Error: UNW_EBADREG: bad register number",
        b"Error: failed to get os threadid",
    ],
)
def test_native_error_retries_without_native(collector, task, tmp_path, native_error):
    collector.OUTPUT_BASE = str(tmp_path)
    raw_file = tmp_path / task.id / "pyspy.raw"
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    failed = mock.MagicMock(returncode=1, stdout=b"", stderr=native_error)
    succeeded = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    def fake_run(cmd, **_kwargs):
        if "--native" in cmd:
            return failed
        raw_file.write_text("worker;run (app.py:12) 10\n")
        return succeeded

    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", side_effect=fake_run) as run_mock:
        result = collector.collect(task)
    assert result.ok is True
    assert run_mock.call_count == 2
    assert "--native" in run_mock.call_args_list[0].args[0]
    assert "--native" not in run_mock.call_args_list[1].args[0]


def test_timeout(collector, task, tmp_path):
    collector.OUTPUT_BASE = str(tmp_path)
    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["py-spy"], timeout=40)):
        result = collector.collect(task)
    assert result.ok is False
    assert "超时" in result.reason


def test_raw_not_produced(collector, task, tmp_path):
    collector.OUTPUT_BASE = str(tmp_path)
    (tmp_path / task.id).mkdir(parents=True, exist_ok=True)
    completed = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")
    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", return_value=completed):
        result = collector.collect(task)
    assert result.ok is False
    assert "raw" in result.reason


def test_extracts_structured_stacks_and_source_lines_from_raw(collector, task, tmp_path):
    collector.OUTPUT_BASE = str(tmp_path)
    _write_raw(
        tmp_path,
        task,
        "thread-1;main (repro.py:42);Map.__init__ (werkzeug/routing.py:1521);Rule.compile (werkzeug/routing.py:768) 21\n"
        "thread-1;main (repro.py:42);idle (repro.py:55) 9\n",
    )
    completed = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")
    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", return_value=completed):
        result = collector.collect(task)
    top_path = next(item["local_path"] for item in result.artifacts if item["artifact_type"] == "top_json")
    stacks_path = next(
        item["local_path"] for item in result.artifacts if item["artifact_type"] == "python_stack_samples_json"
    )
    top = json.loads(open(top_path, encoding="utf-8").read())
    stacks = json.loads(open(stacks_path, encoding="utf-8").read())
    assert top[0]["name"] == "Rule.compile"
    assert top[0]["percent"] == 70.0
    assert top[0]["file"] == "werkzeug/routing.py"
    assert top[0]["line"] == 768
    assert top[0]["call_path"][-2:] == ["Map.__init__", "Rule.compile"]
    assert stacks["total_samples"] == 30
    assert stacks["line_candidates"][0]["line"] == 768
    assert any(
        item["file"] == "werkzeug/routing.py"
        and item["line"] == 1521
        and item["frame_type"] == "intermediate"
        for item in stacks["line_candidates"]
    )
    assert sum(item["percent"] for item in top) == 100.0


def test_raw_parser_labels_idle_heavy_samples_as_low_quality(collector):
    payload = collector._parse_raw_text(
        "thread-1;main (repro.py:42);poll (app.py:9) 18\n"
        "thread-1;main (repro.py:42);poll (app.py:9) 2\n"
    )

    assert payload["sample_quality"]["diagnostic_value"] == "low"
    assert payload["sample_quality"]["dominant_state"] in {"blocked_io", "scheduler_wait"}
    assert payload["filtered_idle_frames"][0]["name"] == "poll"
    assert payload["candidate_frames"] == []
    assert payload["top_functions"][0]["is_idle_like"] is True


def test_raw_parser_keeps_executing_samples_as_high_quality(collector):
    payload = collector._parse_raw_text(
        "thread-1;main (repro.py:42);worker.handle (celery/app/trace.py:651);compute_hotspot (app.py:9) 18\n"
        "thread-1;main (repro.py:42);worker.handle (celery/app/trace.py:651);compute_hotspot (app.py:9) 2\n"
    )

    assert payload["sample_quality"]["diagnostic_value"] == "high"
    assert payload["sample_quality"]["target_code_ratio"] >= 0.9
    assert payload["filtered_idle_frames"] == []
    assert payload["candidate_frames"][0]["name"] == "compute_hotspot"


def test_celery_wait_stack_is_not_classified_as_executing(collector):
    payload = collector._parse_raw_text(
        "billiard.worker;celery.worker.consumer;poll (kombu/transport/redis.py:88) 20\n"
    )

    assert payload["top_functions"][0]["state"] == "blocked_io"
    assert payload["sample_quality"]["dominant_state"] == "blocked_io"


def test_celery_consumer_loop_is_framework_loop(collector):
    payload = collector._parse_raw_text(
        "billiard.worker;celery.worker.consumer.loop (celery/worker/consumer/consumer.py:88) 20\n"
    )

    assert payload["top_functions"][0]["state"] == "framework_loop"
    assert payload["sample_quality"]["diagnostic_value"] == "low"


def test_raw_parser_rejects_unknown_addresses_and_invalid_samples(collector):
    payload = collector._parse_raw_text(
        "thread;[unknown] 12\n"
        "thread;0x7ffee 8\n"
        "thread;valid (app.py:9) -4\n"
        "thread;work (app.py:10) 5\n"
    )
    assert payload["total_samples"] == 5
    assert payload["top_functions"][0]["name"] == "work"
    assert payload["top_functions"][0]["file"] == "app.py"
    assert payload["top_functions"][0]["line"] == 10
    assert payload["top_functions"][0]["samples"] == 5
    assert payload["top_functions"][0]["percent"] == 100.0
    assert payload["top_functions"][0]["call_path"] == ["thread", "work"]


def test_raw_parser_keeps_python_line_below_unknown_native_leaf(collector):
    payload = collector._parse_raw_text(
        "thread;request (app.py:9);retry (urllib3/util/retry.py:337);0x7ffee 12\n"
    )

    assert payload["total_samples"] == 12
    assert payload["top_functions"][0]["name"] == "retry"
    assert payload["top_functions"][0]["file"] == "urllib3/util/retry.py"
    assert payload["top_functions"][0]["line"] == 337
    assert payload["stack_samples"][0]["leaf_frame"] == "0x7ffee"
    assert payload["collection_quality"]["parsed_python_line_sample_count"] == 12


def test_native_profile_without_python_line_retries_python_only(collector, task, tmp_path):
    collector.OUTPUT_BASE = str(tmp_path)
    completed = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    def fake_run(cmd, **_kwargs):
        output = Path(cmd[cmd.index("-o") + 1])
        if "--native" in cmd:
            output.write_text("thread;0x7ffee 10\n", encoding="utf-8")
        else:
            output.write_text("thread;retry (urllib3/util/retry.py:337) 10\n", encoding="utf-8")
        return completed

    with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), mock.patch.object(
        collector, "_pid_exists", return_value=True
    ), mock.patch("subprocess.run", side_effect=fake_run) as run_mock:
        result = collector.collect(task)

    structured = next(
        item["metadata"]["data"]
        for item in result.artifacts
        if item["artifact_type"] == "python_stack_samples_json"
    )
    assert result.ok is True
    assert run_mock.call_count == 2
    assert structured["line_candidates"][0]["line"] == 337
    assert structured["collection_attempts"][-1]["mode"] == "python_only_after_no_python_line"
    assert any(item["artifact_type"] == "pyspy_python_raw" for item in result.artifacts)


def test_pid_exists(collector):
    with mock.patch("os.path.isdir", return_value=True) as check:
        assert collector._pid_exists(42) is True
        check.assert_called_with("/proc/42")
