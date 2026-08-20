"""CodeQL source mechanism collector tests."""

import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.source_mechanism import SourceMechanismCollector
from server.app.diagnosis.codeql_query_guard import (
    render_version_locked_codeql_query,
    validate_ai_generated_codeql_query,
)


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
                    "path_anchors": [
                        {"file": "src/routing.py", "line": 20, "symbol": "compile"},
                        {"file": "src/routing.py", "line": 21, "symbol": "compile"},
                    ],
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
    assert payload["segment_coverage"]["complete"] is True
    assert "query" not in payload["query"]
    query_artifact = next(item for item in result.artifacts if item["artifact_type"] == "codeql_query")
    executed_query = Path(query_artifact["local_path"]).read_text(encoding="utf-8")
    assert "DataFlow::ConfigSig" in executed_query
    assert "TaintTracking::Global<MiniDropConfig>" in executed_query
    assert "where source = sink" not in executed_query
    qlpack = Path(query_artifact["local_path"]).parent / "qlpack.yml"
    assert "codeql/python-all: '*'" in qlpack.read_text(encoding="utf-8")


def test_codeql_guard_rejects_out_of_range_duplicate_and_unverified_anchors():
    allowed = [
        {"file": "src/routing.py", "line": line}
        for line in range(20, 27)
    ]
    base = {
        "investigation_question": "值是否沿锚点链传播？",
        "candidate_id": "candidate_1",
        "expected_relation": "supports",
    }
    invalid_paths = [
        [allowed[0]],
        allowed[:6] + [allowed[6]],
        [allowed[0], allowed[0]],
        [allowed[0], {"file": "src/other.py", "line": 99}],
        [allowed[0], {"file": "../routing.py", "line": 21}, allowed[1]],
    ]

    for path_anchors in invalid_paths:
        with pytest.raises(ValueError):
            validate_ai_generated_codeql_query(
                {**base, "path_anchors": path_anchors},
                allowed_candidate_ids={"candidate_1"},
                allowed_anchors=allowed,
            )


def test_codeql_renderer_emits_each_ordered_anchor_segment():
    anchors = [
        {"file": "src/routing.py", "line": 1066},
        {"file": "src/routing.py", "line": 962},
        {"file": "src/routing.py", "line": 852},
        {"file": "src/routing.py", "line": 1119},
    ]

    query = render_version_locked_codeql_query({
        "candidate_id": "bound_method_retention",
        "path_anchors": anchors,
    })

    for index in range(4):
        assert f"private predicate miniDropAnchor{index}" in query
    for index in range(3):
        assert (
            f"miniDropAnchor{index}(source.getNode()) and "
            f"miniDropAnchor{index + 1}(sink.getNode())"
        ) in query


def test_codeql_segment_coverage_requires_every_ordered_segment():
    anchors = [
        {"file": "src/routing.py", "line": 1066},
        {"file": "src/routing.py", "line": 962},
        {"file": "src/routing.py", "line": 852},
        {"file": "src/routing.py", "line": 1119},
    ]
    metadata = {"path_anchors": anchors}
    partial_paths = [
        {"evidence_ref": "source_mechanism.mechanism_paths[0]", "anchor_matches": [0, 1]},
        {"evidence_ref": "source_mechanism.mechanism_paths[1]", "anchor_matches": [1, 2]},
    ]
    complete_paths = [
        *partial_paths,
        {"evidence_ref": "source_mechanism.mechanism_paths[2]", "anchor_matches": [2, 3]},
    ]

    partial = SourceMechanismCollector._segment_coverage(partial_paths, metadata, anchors)
    complete = SourceMechanismCollector._segment_coverage(complete_paths, metadata, anchors)

    assert partial == {
        "required_segments": 3,
        "covered_segments": 2,
        "complete": False,
        "segments": [
            {
                "from_anchor_index": 0,
                "to_anchor_index": 1,
                "covered": True,
                "evidence_refs": ["source_mechanism.mechanism_paths[0]"],
            },
            {
                "from_anchor_index": 1,
                "to_anchor_index": 2,
                "covered": True,
                "evidence_refs": ["source_mechanism.mechanism_paths[1]"],
            },
            {
                "from_anchor_index": 2,
                "to_anchor_index": 3,
                "covered": False,
                "evidence_refs": [],
            },
        ],
    }
    assert complete["required_segments"] == 3
    assert complete["covered_segments"] == 3
    assert complete["complete"] is True
