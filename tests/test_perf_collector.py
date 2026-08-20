"""perf 采集器单元测试。

在非 Linux 环境下，真实的 perf record 无法执行。
测试通过 mock subprocess.Popen 覆盖各执行分支。
"""

import subprocess
from unittest import mock

import pytest

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.perf import PerfCollector


@pytest.fixture(name="collector")
def collector_fixture() -> PerfCollector:
    return PerfCollector()


@pytest.fixture(name="task")
def task_fixture() -> CollectorTask:
    return CollectorTask(
        id="task_test_001",
        collector_type="perf_cpu",
        target_pid=1234,
        sample_rate=99,
        duration_sec=10,
        options={"callgraph": "fp", "event": "cpu-cycles"},
    )


def _mock_popen_complete(returncode=0, stdout=b"", stderr=b"", *, pid=9999, side_effect=None):
    """构造一个模拟的 Popen 实例。"""
    p = mock.MagicMock()
    p.returncode = returncode
    p.pid = pid
    if side_effect:
        p.communicate.side_effect = side_effect
    else:
        p.communicate.return_value = (stdout, stderr)
    return p


class TestPerfAvailabilityChecks:
    """perf 命令可用性和目标检查。"""

    def test_perf_not_installed_returns_failure(self, collector: PerfCollector, task: CollectorTask):
        with mock.patch("shutil.which", return_value=None):
            result = collector.collect(task)
        assert result.ok is False
        assert "perf 命令不可用" in result.reason

    def test_pid_not_exists_returns_failure(self, collector: PerfCollector, task: CollectorTask):
        with mock.patch("shutil.which", return_value="/usr/bin/perf"), \
             mock.patch.object(collector, "_pid_exists", return_value=False):
            result = collector.collect(task)
        assert result.ok is False
        assert "不存在" in result.reason


class TestPerfExecution:
    """perf record 子进程执行路径。"""

    def test_perf_completes_successfully(self, collector: PerfCollector, task: CollectorTask, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)

        perf_data = tmp_path / task.id / "perf.data"
        perf_data.parent.mkdir(parents=True, exist_ok=True)
        perf_data.write_text("perf data")

        mock_proc = _mock_popen_complete()

        with mock.patch("shutil.which", return_value="/usr/bin/perf"), \
             mock.patch.object(collector, "_read_paranoid", return_value=4), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch.object(collector, "_analyze_perf_data", return_value=([], "")), \
             mock.patch("os.setpgrp", create=True):
            result = collector.collect(task)

        assert result.ok is True
        assert "perf_event_paranoid=4" in result.reason
        assert result.artifacts[0]["artifact_type"] == "raw"
        assert result.artifacts[0]["size_bytes"] > 0

    def test_perf_defaults_to_user_space_cycles(self, collector: PerfCollector, task: CollectorTask, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)
        task = CollectorTask(
            id=task.id,
            collector_type=task.collector_type,
            target_pid=task.target_pid,
            sample_rate=task.sample_rate,
            duration_sec=task.duration_sec,
            options={"callgraph": "fp"},
        )
        perf_data = tmp_path / task.id / "perf.data"
        perf_data.parent.mkdir(parents=True, exist_ok=True)
        perf_data.write_text("perf data")
        mock_proc = _mock_popen_complete()

        with mock.patch("shutil.which", return_value="/usr/bin/perf"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             mock.patch.object(collector, "_analyze_perf_data", return_value=([], "")):
            result = collector.collect(task)

        assert result.ok is True
        cmd = mock_popen.call_args.args[0]
        assert "--all-user" in cmd
        assert cmd[cmd.index("-e") + 1] == "cpu-cycles:u"

    def test_perf_can_disable_user_space_only_mode(self, collector: PerfCollector, task: CollectorTask, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)
        task = CollectorTask(
            id=task.id,
            collector_type=task.collector_type,
            target_pid=task.target_pid,
            sample_rate=task.sample_rate,
            duration_sec=task.duration_sec,
            options={"callgraph": "fp", "all_user": False},
        )
        perf_data = tmp_path / task.id / "perf.data"
        perf_data.parent.mkdir(parents=True, exist_ok=True)
        perf_data.write_text("perf data")
        mock_proc = _mock_popen_complete()

        with mock.patch("shutil.which", return_value="/usr/bin/perf"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             mock.patch.object(collector, "_analyze_perf_data", return_value=([], "")):
            result = collector.collect(task)

        assert result.ok is True
        assert "--all-user" not in mock_popen.call_args.args[0]

    def test_perf_attaches_analyzer_artifacts(self, collector: PerfCollector, task: CollectorTask, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)

        perf_data = tmp_path / task.id / "perf.data"
        perf_data.parent.mkdir(parents=True, exist_ok=True)
        perf_data.write_text("perf data")

        mock_proc = _mock_popen_complete()
        analysis = [
            {"artifact_type": "flamegraph_json", "filename": "flamegraph.json"},
            {"artifact_type": "top_json", "filename": "top.json"},
        ]

        with mock.patch("shutil.which", return_value="/usr/bin/perf"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch.object(collector, "_analyze_perf_data", return_value=(analysis, "")), \
             mock.patch("os.setpgrp", create=True):
            result = collector.collect(task)

        artifact_types = {item["artifact_type"] for item in result.artifacts}
        assert {"raw", "flamegraph_json", "top_json"} <= artifact_types

    def test_perf_attaches_depth_evidence_artifact(self, collector: PerfCollector, task: CollectorTask, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)

        perf_data = tmp_path / task.id / "perf.data"
        task_dir = perf_data.parent
        task_dir.mkdir(parents=True, exist_ok=True)
        perf_data.write_text("perf data")
        (task_dir / "collapsed.txt").write_text("main;worker;compute_hotspot 100\n")
        (task_dir / "top.json").write_text('[{"name":"compute_hotspot","percent":68.5}]')

        mock_proc = _mock_popen_complete()
        mock_run = mock.MagicMock(return_value=mock.MagicMock(returncode=0, stdout=b"", stderr=b""))

        with mock.patch("shutil.which", return_value="/usr/bin/perf"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("subprocess.run", mock_run), \
             mock.patch("os.setpgrp", create=True):
            result = collector.collect(task)

        depth = next(item for item in result.artifacts if item["artifact_type"] == "depth_evidence_json")
        data = depth["metadata"]["data"]
        assert data["stack_samples"]
        assert data["stack_samples"][0]["call_path"] == "main;worker;compute_hotspot"
        assert data["wait_reason"] == "stack_sampling"
        assert data["context"]["context_id"]
        assert data["line_candidates"] == []

    def test_perf_permission_failure_includes_runtime_diagnostics(self, collector: PerfCollector, task: CollectorTask, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)

        mock_proc = _mock_popen_complete(returncode=255, stderr=b"perf_event_open: Operation not permitted")

        with mock.patch("shutil.which", return_value="/usr/bin/perf"), \
             mock.patch.object(collector, "_read_paranoid", return_value=4), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.setpgrp", create=True):
            result = collector.collect(task)

        assert result.ok is False
        assert "perf record 执行失败" in result.reason
        assert "perf_event_paranoid=4" in result.reason
        assert "Operation not permitted" in result.reason
        assert "PERFMON" in result.reason

    def test_perf_timeout_kills_process_group(self, collector: PerfCollector, task: CollectorTask, tmp_path):
        collector.OUTPUT_BASE = str(tmp_path)

        mock_proc = _mock_popen_complete(
            pid=9999,
            side_effect=subprocess.TimeoutExpired(cmd=["perf"], timeout=40),
        )

        with mock.patch("shutil.which", return_value="/usr/bin/perf"), \
             mock.patch.object(collector, "_pid_exists", return_value=True), \
             mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("os.setpgrp", create=True), \
             mock.patch("os.getpgid", create=True, return_value=9999), \
             mock.patch("os.killpg", create=True) as mock_kill:
            result = collector.collect(task)

        assert result.ok is False
        assert "超时" in result.reason
        mock_kill.assert_called()


class TestPidCheck:
    """PID 存在性检查。"""

    def test_pid_exists_on_linux_proc(self, collector: PerfCollector):
        with mock.patch("os.path.isdir", return_value=True) as mock_isdir:
            assert collector._pid_exists(1234) is True
            mock_isdir.assert_called_with("/proc/1234")

    def test_pid_missing_on_linux_proc(self, collector: PerfCollector):
        with mock.patch("os.path.isdir", return_value=False):
            assert collector._pid_exists(99999) is False


class TestParanoidRead:
    """perf_event_paranoid 只作为诊断上下文读取。"""

    def test_paranoid_enabled(self, collector: PerfCollector):
        mock_open = mock.mock_open(read_data="1\n")
        with mock.patch("builtins.open", mock_open):
            assert collector._read_paranoid() == 1

    def test_paranoid_disabled(self, collector: PerfCollector):
        mock_open = mock.mock_open(read_data="3\n")
        with mock.patch("builtins.open", mock_open):
            assert collector._read_paranoid() == 3

    def test_paranoid_file_missing(self, collector: PerfCollector):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            assert collector._read_paranoid() is None
