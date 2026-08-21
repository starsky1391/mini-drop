"""Bounded Git and universal-ctags source snapshot tests."""

import subprocess
from unittest import mock

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.source_snapshot import SourceSnapshotCollector


def _task(repo, revision="abc123"):
    return CollectorTask(
        id="source-1",
        collector_type="source_snapshot",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={
            "source_root": str(repo),
            "source_revision": revision,
            "line_candidates": [{"file": "werkzeug/routing.py", "line": 3, "symbol": "compile"}],
        },
    )


def test_source_snapshot_rejects_path_outside_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(allowed))
    result = SourceSnapshotCollector().collect(_task(outside))
    assert result.ok is False
    assert "源码根目录" in result.reason


def test_source_snapshot_verifies_revision_and_returns_bounded_context(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "werkzeug" / "routing.py"
    source.parent.mkdir(parents=True)
    source.write_text("one\ntwo\ndef compile():\n    return 1\nfive\n", encoding="utf-8")
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    collector = SourceSnapshotCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")

    def fake_run(cmd, **_kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"werkzeug/routing.py\0", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="compile\twerkzeug/routing.py\t/^def compile():$/;\"\tf\tline:3\n", stderr="")

    with mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), mock.patch(
        "subprocess.run", side_effect=fake_run
    ):
        result = collector.collect(_task(repo))

    assert result.ok is True
    payload = result.artifacts[0]["metadata"]["data"]
    assert payload["revision"] == "abc123"
    assert payload["source_context_hash"].startswith("sha256:")
    assert payload["snippets"][0]["focus_line"] == 3
    assert payload["snippets"][0]["lines"][0]["line"] == 1
    assert len(payload["snippets"][0]["lines"]) == 5


def test_source_snapshot_scopes_safe_directory_to_each_git_command(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "werkzeug" / "routing.py"
    source.parent.mkdir(parents=True)
    source.write_text("def compile():\n    pass\n", encoding="utf-8")
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    collector = SourceSnapshotCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"werkzeug/routing.py\0", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), mock.patch(
        "subprocess.run", side_effect=fake_run
    ):
        result = collector.collect(_task(repo))

    assert result.ok is True
    git_commands = [cmd for cmd in commands if cmd[0].endswith("/git")]
    assert len(git_commands) == 2
    assert all(f"safe.directory={repo.resolve()}" in cmd for cmd in git_commands)


def test_source_snapshot_maps_container_absolute_path_to_unique_git_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "werkzeug" / "routing.py"
    source.parent.mkdir(parents=True)
    source.write_text("def compile():\n    pass\n", encoding="utf-8")
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    collector = SourceSnapshotCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = _task(repo)
    task = task.__class__(**{
        **task.__dict__,
        "options": {**task.options, "line_candidates": [{"file": "/app/werkzeug/routing.py", "line": 1, "symbol": "compile"}]},
    })

    def fake_run(cmd, **_kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"werkzeug/routing.py\0", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), mock.patch(
        "subprocess.run", side_effect=fake_run
    ):
        result = collector.collect(task)
    assert result.ok is True
    assert result.artifacts[0]["metadata"]["data"]["snippets"][0]["file"] == "werkzeug/routing.py"


def test_source_snapshot_prioritizes_tracked_candidates_before_bounded_limit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "celery" / "app" / "trace.py"
    source.parent.mkdir(parents=True)
    source.write_text("def trace_task():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    collector = SourceSnapshotCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    candidates = [
        {"file": f"/usr/local/lib/python3.11/site-packages/pkg_{index}.py", "line": 1}
        for index in range(12)
    ]
    candidates.append({"file": "/opt/celery-src/celery/app/trace.py", "line": 1, "symbol": "trace_task"})
    base_task = _task(repo)
    task = base_task.__class__(**{
        **base_task.__dict__,
        "options": {**base_task.options, "line_candidates": candidates},
    })

    def fake_run(cmd, **_kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"celery/app/trace.py\0", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), mock.patch(
        "subprocess.run", side_effect=fake_run
    ):
        result = collector.collect(task)

    assert result.ok is True
    assert result.artifacts[0]["metadata"]["data"]["snippets"][0]["file"] == "celery/app/trace.py"


def test_source_snapshot_prioritizes_celery_trace_frame_before_exception_helper(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    trace = repo / "celery" / "app" / "trace.py"
    helper = repo / "celery" / "utils" / "serialization.py"
    trace.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    trace.write_text("\n".join(["pass"] * 646 + ["def fast_trace_task():", "    pass"]), encoding="utf-8")
    helper.write_text("\n".join(["pass"] * 163 + ["def get_pickleable_exception():", "    pass"]), encoding="utf-8")
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    collector = SourceSnapshotCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    candidates = [
        {"file": "/opt/celery-src/celery/utils/serialization.py", "line": 164, "symbol": "get_pickleable_exception"},
        {"file": "/opt/celery-src/celery/app/trace.py", "line": 647, "symbol": "fast_trace_task"},
    ]
    base_task = _task(repo)
    task = base_task.__class__(**{
        **base_task.__dict__,
        "options": {**base_task.options, "line_candidates": candidates},
    })

    def fake_run(cmd, **_kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=b"celery/app/trace.py\0celery/utils/serialization.py\0",
                stderr=b"",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), mock.patch(
        "subprocess.run", side_effect=fake_run
    ):
        result = collector.collect(task)

    snippets = result.artifacts[0]["metadata"]["data"]["snippets"]
    assert result.ok is True
    assert snippets[0]["file"] == "celery/app/trace.py"
    assert snippets[0]["focus_line"] == 647


def test_source_snapshot_rejects_revision_mismatch(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    completed = subprocess.CompletedProcess([], 0, stdout="different\n", stderr="")
    with mock.patch("shutil.which", return_value="/usr/bin/tool"), mock.patch("subprocess.run", return_value=completed):
        result = SourceSnapshotCollector().collect(_task(repo))
    assert result.ok is False
    payload = result.artifacts[0]["metadata"]["data"]
    assert payload["evidence_validity"]["reason"] == "source_revision_mismatch"


def test_source_snapshot_keeps_bounded_python_enclosing_class(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "werkzeug" / "routing.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class BuilderCompiler:\n"
        "    JOIN_EMPTY = ''.join\n"
        "    def compile(self):\n"
        "        constants = tuple(self.consts)\n"
        "        return types.CodeType(constants)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    collector = SourceSnapshotCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = _task(repo)
    task = task.__class__(**{
        **task.__dict__,
        "options": {**task.options, "line_candidates": [{"file": "werkzeug/routing.py", "line": 4, "symbol": "compile"}]},
    })

    def fake_run(cmd, **_kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"werkzeug/routing.py\0", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), mock.patch(
        "subprocess.run", side_effect=fake_run
    ):
        result = collector.collect(task)

    context = result.artifacts[0]["metadata"]["data"]["enclosing_contexts"][0]
    assert context["kind"] == "class"
    assert context["symbol"] == "BuilderCompiler"
    assert context["start_line"] == 1
    assert context["end_line"] == 5
    assert any("JOIN_EMPTY" in line["text"] for line in context["lines"])


def test_source_snapshot_extracts_callable_to_code_constant_reference_path(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "package" / "compiler.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from functools import partial\n"
        "import types\n"
        "class DynamicCompiler:\n"
        "    def __init__(self):\n"
        "        self.values = []\n"
        "    def remember(self, value):\n"
        "        self.values.append(value)\n"
        "        return len(self.values) - 1\n"
        "    def compile(self):\n"
        "        callback = partial(str, encoding='utf-8')\n"
        "        items = []\n"
        "        items.append((self.render, 'value'))\n"
        "        for operation, value in items:\n"
        "            self.remember(operation)\n"
        "        index = self.remember(callback)\n"
        "        code_args = [0, 0, 0, 0, b'', tuple(self.values), (), (), 'x', 'x', 1, b'']\n"
        "        code = types.CodeType(*code_args)\n"
        "        generated = types.FunctionType(code, {})\n"
        "        return generated, index\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    collector = SourceSnapshotCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = _task(repo)
    task = task.__class__(**{
        **task.__dict__,
        "options": {
            **task.options,
            "line_candidates": [{"file": "package/compiler.py", "line": 17, "symbol": "compile"}],
        },
    })

    def fake_run(cmd, **_kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"package/compiler.py\0", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), mock.patch(
        "subprocess.run", side_effect=fake_run
    ):
        result = collector.collect(task)

    paths = result.artifacts[0]["metadata"]["data"]["reference_paths"]
    callback_path = next(path for path in paths if path["source_expression"] == "callback")
    assert callback_path["source_kind"] == "partial_callable"
    assert callback_path["stored_via"] == "self.remember(...)"
    assert callback_path["container"] == "self.values"
    assert callback_path["runtime_slot"] == "CodeType.co_consts"
    assert callback_path["retention_chain"][-2:] == ["CodeType.co_consts", "FunctionType"]
    assert "CodeType" in callback_path["sink"]
    assert "FunctionType" in callback_path["retained_by"]
    assert len(callback_path["source_lines"]) == 4

    operation_path = next(path for path in paths if path["source_expression"] == "operation")
    assert operation_path["source_kind"] == "loop_value"
    assert operation_path["upstream_candidates"][0]["expression"] == "self.render"
    assert operation_path["upstream_candidates"][0]["source_kind"] == "bound_method_or_attribute"
    assert operation_path["upstream_candidates"][0]["flows_as"] == "operation"
