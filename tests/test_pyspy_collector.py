"""py-spy 采集器单元测试。"""

import subprocess
import json
from unittest import mock

import pytest

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.pyspy import PySpyCollector


@pytest.fixture(name="collector")
def collector_fixture() -> PySpyCollector:
    return PySpyCollector()


@pytest.fixture(name="task")
def task_fixture() -> CollectorTask:
    return CollectorTask(
        id="pyspy_test_001",
        collector_type="pyspy",
        target_pid=1234,
        sample_rate=99,
        duration_sec=10,
    )


class TestPySpyAvailability:
    """py-spy 可用性检查。"""

    def test_pyspy_not_installed(self, collector, task):
        with mock.patch("shutil.which", return_value=None):
            result = collector.collect(task)
        assert result.ok is False
        assert "py-spy" in result.reason

    def test_pid_not_exists(self, collector, task):
        with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), \
             mock.patch.object(collector, "_pid_exists", return_value=False):
            result = collector.collect(task)
        assert result.ok is False
        assert "不存在" in result.reason


class TestPySpyExecution:
    """py-spy 子进程执行路径。"""

    def test_execution_success(self, collector, task, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)
        svg_file = tmp_path / task.id / "pyspy.svg"
        svg_file.parent.mkdir(parents=True, exist_ok=True)
        svg_file.write_text("<svg></svg>")

        mock_result = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.run", return_value=mock_result):
            result = collector.collect(task)

        assert result.ok is True
        assert result.artifacts[0]["artifact_type"] == "flamegraph_svg"
        assert any(item["artifact_type"] == "top_json" for item in result.artifacts)

    def test_command_uses_task_sample_rate(self, collector, task, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)
        svg_file = tmp_path / task.id / "pyspy.svg"
        svg_file.parent.mkdir(parents=True, exist_ok=True)
        svg_file.write_text("<svg></svg>")

        mock_result = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.run", return_value=mock_result) as run_mock:
            result = collector.collect(task)

        assert result.ok is True
        cmd = run_mock.call_args.args[0]
        assert "-r" in cmd
        assert cmd[cmd.index("-r") + 1] == str(task.sample_rate)

    def test_nonzero_exit(self, collector, task, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)

        mock_result = mock.MagicMock(
            returncode=1,
            stdout=b"",
            stderr=b"process is not a Python program",
        )

        with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.run", return_value=mock_result):
            result = collector.collect(task)

        assert result.ok is False
        assert "执行失败" in result.reason

    def test_native_unwind_error_retries_without_native(self, collector, task, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)
        svg_file = tmp_path / task.id / "pyspy.svg"
        svg_file.parent.mkdir(parents=True, exist_ok=True)

        failed = mock.MagicMock(
            returncode=1,
            stdout=b"",
            stderr=b"Error: UNW_EBADREG: bad register number",
        )
        succeeded = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

        def fake_run(cmd, **_kwargs):
            if "--native" in cmd:
                return failed
            svg_file.write_text("<svg></svg>")
            return succeeded

        with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.run", side_effect=fake_run) as run_mock:
            result = collector.collect(task)

        assert result.ok is True
        assert run_mock.call_count == 2
        assert "--native" in run_mock.call_args_list[0].args[0]
        assert "--native" not in run_mock.call_args_list[1].args[0]

    def test_native_unwind_einval_retries_without_native(self, collector, task, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)
        svg_file = tmp_path / task.id / "pyspy.svg"
        svg_file.parent.mkdir(parents=True, exist_ok=True)

        failed = mock.MagicMock(
            returncode=1,
            stdout=b"",
            stderr=b"Error: UNW_EINVAL: unsupported operation or bad value",
        )
        succeeded = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

        def fake_run(cmd, **_kwargs):
            if "--native" in cmd:
                return failed
            svg_file.write_text("<svg><title>worker (1 samples, 100%)</title></svg>")
            return succeeded

        with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.run", side_effect=fake_run) as run_mock:
            result = collector.collect(task)

        assert result.ok is True
        assert run_mock.call_count == 2
        assert "--native" not in run_mock.call_args_list[1].args[0]

    def test_timeout(self, collector, task, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)

        with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["py-spy"], timeout=40)):
            result = collector.collect(task)

        assert result.ok is False
        assert "超时" in result.reason

    def test_svg_not_produced(self, collector, task, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)
        (tmp_path / task.id).mkdir(parents=True, exist_ok=True)

        mock_result = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.run", return_value=mock_result):
            result = collector.collect(task)

        assert result.ok is False
        assert "SVG" in result.reason

    def test_extracts_top_json_from_svg_titles(self, collector, task, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)
        svg_file = tmp_path / task.id / "pyspy.svg"
        svg_file.parent.mkdir(parents=True, exist_ok=True)
        svg_file.write_text(
            "<svg><title>threading.Lock.acquire (42 samples, 70%)</title>"
            "<title>worker.run (12 samples, 20%)</title></svg>",
            encoding="utf-8",
        )

        mock_result = mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch("shutil.which", return_value="/usr/bin/py-spy"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.run", return_value=mock_result):
            result = collector.collect(task)

        top_artifact = next(item for item in result.artifacts if item["artifact_type"] == "top_json")
        top = json.loads(open(top_artifact["local_path"], encoding="utf-8").read())
        assert result.ok is True
        assert "top.json" in top_artifact["filename"]
        assert top_artifact["size_bytes"] > 2
        assert top[0]["name"] == "threading.Lock.acquire"
        assert top[0]["percent"] == 70.0


class TestPidCheck:
    """PID 检查。"""

    def test_pid_exists(self, collector):
        with mock.patch("os.path.isdir", return_value=True) as m:
            assert collector._pid_exists(42) is True
            m.assert_called_with("/proc/42")
