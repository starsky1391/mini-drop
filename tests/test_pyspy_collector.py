"""py-spy collector tests."""

import json
import subprocess
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


def test_raw_parser_rejects_unknown_addresses_and_invalid_samples(collector):
    payload = collector._parse_raw_text(
        "thread;[unknown] 12\n"
        "thread;0x7ffee 8\n"
        "thread;valid (app.py:9) -4\n"
        "thread;work (app.py:10) 5\n"
    )
    assert payload["total_samples"] == 5
    assert payload["top_functions"] == [{
        "name": "work",
        "file": "app.py",
        "line": 10,
        "samples": 5,
        "percent": 100.0,
        "call_path": ["thread", "work"],
    }]


def test_pid_exists(collector):
    with mock.patch("os.path.isdir", return_value=True) as check:
        assert collector._pid_exists(42) is True
        check.assert_called_with("/proc/42")
