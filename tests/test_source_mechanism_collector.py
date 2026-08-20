"""CodeQL source mechanism collector tests."""

import hashlib
import json
from pathlib import Path
from unittest import mock

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.source_mechanism import SourceMechanismCollector


def _task(repo, sarif, **options):
    return CollectorTask(
        id="codeql-1",
        collector_type="source_mechanism_query",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={
            "source_root": str(repo),
            "source_revision": "abc123",
            "line_candidates": [{"file": "src/routing.py", "line": 20, "symbol": "compile"}],
            "codeql_sarif_path": str(sarif),
            **options,
        },
    )


def _sarif(path, location_count=3):
    locations = []
    for index in range(location_count):
        locations.append({
            "location": {
                "physicalLocation": {
                    "artifactLocation": {"uri": "src/routing.py"},
                    "region": {"startLine": 20 + index},
                },
                "message": {"text": "value stored in container" if index == 1 else "data flows"},
            },
        })
    path.write_text(json.dumps({
        "runs": [{"results": [{
            "ruleId": "mini-drop/python-retention",
            "message": {"text": "bound method reaches generated code constants"},
            "properties": {"candidate_relation": "supports"},
            "codeFlows": [{"threadFlows": [{"locations": locations}]}],
        }]}],
    }), encoding="utf-8")


def test_codeql_sarif_is_normalized_and_bounded(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sarif = tmp_path / "result.sarif"
    _sarif(sarif, location_count=80)
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.setenv("MINI_DROP_CODEQL_ARTIFACT_ROOTS", str(tmp_path))
    collector = SourceMechanismCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")

    with mock.patch.object(collector, "_git", side_effect=["abc123", "abc123", "origin/repo"]):
        result = collector.collect(_task(repo, sarif, query_path="/tmp/arbitrary.ql"))

    assert result.ok is True
    payload = next(item["metadata"]["data"] for item in result.artifacts if item["artifact_type"] == "source_mechanism_json")
    path = payload["mechanism_paths"][0]
    assert payload["producer"] == "codeql"
    assert path["candidate_relation"] == "supports"
    assert len(path["nodes"]) == collector.MAX_NODES
    assert path["edges"][0]["type"] in {"data_flow", "container_write"}
    assert path["evidence_ref"] == "source_mechanism.mechanism_paths[0]"
    assert any(item["artifact_type"] == "codeql_sarif" for item in result.artifacts)
    assert "query_path" not in payload


def test_codeql_revision_mismatch_is_structured_blocked(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sarif = tmp_path / "result.sarif"
    _sarif(sarif)
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.setenv("MINI_DROP_CODEQL_ARTIFACT_ROOTS", str(tmp_path))
    collector = SourceMechanismCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")

    with mock.patch.object(collector, "_git", side_effect=["new-head", "old-head"]):
        result = collector.collect(_task(repo, sarif))

    payload = result.artifacts[0]["metadata"]["data"]
    assert result.ok is False
    assert payload["evidence_validity"]["evidence_status"] == "blocked"
    assert payload["evidence_validity"]["reason"] == "source_revision_mismatch"


def test_codeql_maps_host_source_root_before_policy_check(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sarif = tmp_path / "result.sarif"
    _sarif(sarif)
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.setenv("MINI_DROP_CODEQL_ARTIFACT_ROOTS", str(tmp_path))
    collector = SourceMechanismCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    monkeypatch.setattr(collector, "_map_host_path", lambda path: repo)

    task = _task(repo, sarif)
    task = task.__class__(**{
        **task.__dict__,
        "options": {**task.options, "source_root": "/home/worker1/cases/werkzeug"},
    })
    with mock.patch.object(collector, "_git", side_effect=["abc123", "abc123", "origin/repo"]):
        result = collector.collect(task)

    assert result.ok is True


def test_codeql_cache_key_is_stable_for_same_revision(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sarif = tmp_path / "result.sarif"
    _sarif(sarif)
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.setenv("MINI_DROP_CODEQL_ARTIFACT_ROOTS", str(tmp_path))
    monkeypatch.setenv("MINI_DROP_CODEQL_QUERY_PACK_VERSION", "werkzeug-v1")
    collector = SourceMechanismCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")

    expected = hashlib.sha256("origin/repo\0abc123\0python\0werkzeug-v1".encode()).hexdigest()
    keys = []
    for task_id in ("codeql-a", "codeql-b"):
        task = _task(repo, sarif)
        task = task.__class__(**{**task.__dict__, "id": task_id})
        with mock.patch.object(collector, "_git", side_effect=["abc123", "abc123", "origin/repo"]):
            result = collector.collect(task)
        keys.append(next(item["metadata"]["data"] for item in result.artifacts if item["artifact_type"] == "source_mechanism_json")["cache"]["cache_key"])
    assert keys == [expected, expected]


def test_codeql_missing_tool_and_suite_is_structured_blocked(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.delenv("MINI_DROP_CODEQL_QUERY_SUITE", raising=False)
    collector = SourceMechanismCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = _task(repo, tmp_path / "missing.sarif")
    task = task.__class__(**{**task.__dict__, "options": {key: value for key, value in task.options.items() if key != "codeql_sarif_path"}})

    with mock.patch.object(collector, "_git", side_effect=["abc123", "abc123", "origin/repo"]), mock.patch("shutil.which", return_value=None):
        result = collector.collect(task)

    assert result.artifacts[0]["metadata"]["data"]["evidence_validity"]["reason"] == "codeql_not_installed"


def test_ai_generated_codeql_query_is_revalidated_saved_and_audited(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cache = tmp_path / "cache"
    monkeypatch.setenv("MINI_DROP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.setenv("MINI_DROP_CODEQL_CACHE_ROOT", str(cache))
    collector = SourceMechanismCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    query = """/**
 * @name Mini-Drop path
 * @kind path-problem
 * @id mini-drop/path
 */
import python
import semmle.python.dataflow.new.DataFlow
from DataFlow::PathNode source, DataFlow::PathNode sink
where source = sink
select sink.getNode(), source, sink, "same node"
"""
    task = _task(repo, tmp_path / "unused")
    task = task.__class__(**{
        **task.__dict__,
        "options": {
            key: value for key, value in task.options.items() if key != "codeql_sarif_path"
        } | {
                "ai_generated_query": {
                    "origin": "ai_guarded",
                    "investigation_question": "值是否进入生成代码常量？",
                    "candidate_id": "ai_proposal_bound_method",
                    "expected_relation": "supports",
                    "source_anchor": {"file": "src/routing.py", "line": 20, "symbol": "compile"},
                    "sink_anchor": {"file": "src/routing.py", "line": 21, "symbol": "compile"},
                    "query": query,
                },
                "line_candidates": [
                    {"file": "src/routing.py", "line": 20, "symbol": "compile"},
                    {"file": "src/routing.py", "line": 21, "symbol": "compile"},
                ],
            },
        })

    def fake_run(command, timeout):
        if "create" in command:
            Path(command[command.index("create") + 1]).mkdir(parents=True)
        if "analyze" in command:
            output = next(item.split("=", 1)[1] for item in command if item.startswith("--output="))
            _sarif(Path(output))
        return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    with mock.patch.object(
        collector,
        "_git",
        side_effect=["abc123", "abc123", "origin/repo", "src/routing.py"],
    ), mock.patch.object(
        collector, "_run", side_effect=fake_run
    ), mock.patch("shutil.which", return_value="/usr/local/bin/codeql"):
        result = collector.collect(task)

    assert result.ok is True
    assert any(item["artifact_type"] == "codeql_query" for item in result.artifacts)
    payload = next(item["metadata"]["data"] for item in result.artifacts if item["artifact_type"] == "source_mechanism_json")
    assert payload["query"]["origin"] == "ai_guarded_anchor_spec"
    assert payload["query"]["query_spec_hash"].startswith("sha256:")
    assert payload["query"]["raw_query_hash"].startswith("sha256:")
    assert payload["mechanism_paths"][0]["candidate_id"] == "ai_proposal_bound_method"
    assert "query" not in payload["query"]
    query_artifact = next(item for item in result.artifacts if item["artifact_type"] == "codeql_query")
    executed_query = Path(query_artifact["local_path"]).read_text(encoding="utf-8")
    assert "DataFlow::ConfigSig" in executed_query
    assert "TaintTracking::Global<MiniDropConfig>" in executed_query
    assert "where source = sink" not in executed_query
