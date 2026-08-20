"""PyHeap runtime reference collector tests."""

from unittest import mock

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.python_heap_reference import PythonHeapReferenceCollector


def _task(dump_path, **options):
    return CollectorTask(
        id="pyheap-1",
        collector_type="python_heap_reference",
        target_pid=1234,
        sample_rate=1,
        duration_sec=5,
        options={"pyheap_dump_path": str(dump_path), **options},
    )


def test_existing_pyheap_dump_emits_structured_reference_paths(tmp_path, monkeypatch):
    dump = tmp_path / "heap.pyheap"
    dump.write_bytes(b"official-pyheap")
    monkeypatch.setenv("MINI_DROP_PYHEAP_ROOTS", str(tmp_path))
    collector = PythonHeapReferenceCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    analysis = {
        "top_retained_objects": [{"node_id": "object_4", "type": "Map", "retained_bytes": 4096}],
        "reference_paths": [{
            "path_id": "pyheap_path_1",
            "root_type": "function",
            "target_type": "Map",
            "nodes": [{"node_id": "object_1", "type": "function"}, {"node_id": "object_4", "type": "Map"}],
            "edges": [{"from": "object_1", "to": "object_4", "type": "references"}],
            "retained_bytes": 4096,
            "evidence_ref": "python_heap_reference.reference_paths[0]",
        }],
    }

    with mock.patch.object(collector, "_analyze_dump", return_value=analysis):
        result = collector.collect(_task(dump))

    assert result.ok is True
    payload = next(item["metadata"]["data"] for item in result.artifacts if item["artifact_type"] == "python_heap_reference_json")
    assert payload["producer"] == "pyheap"
    assert payload["reference_paths"][0]["target_type"] == "Map"
    assert payload["raw_artifact_refs"] == ["artifact:pyheap_dump"]
    assert any(item["artifact_type"] == "pyheap_dump" and "metadata" not in item for item in result.artifacts)


def test_pyheap_dump_size_limit_is_structured_blocked(tmp_path, monkeypatch):
    dump = tmp_path / "heap.pyheap"
    dump.write_bytes(b"x" * 2048)
    monkeypatch.setenv("MINI_DROP_PYHEAP_ROOTS", str(tmp_path))
    collector = PythonHeapReferenceCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")

    result = collector.collect(_task(dump, max_dump_bytes=1024))

    payload = result.artifacts[0]["metadata"]["data"]
    assert result.ok is False
    assert payload["evidence_validity"]["evidence_status"] == "blocked"
    assert payload["evidence_validity"]["reason"] == "dump_size_limit_exceeded"


def test_inbound_reference_bfs_is_root_to_target_and_bounded():
    class Inbound:
        def __init__(self):
            self.values = {4: {3, 8}, 3: {2}, 2: {1}, 8: {9}, 9: set()}

        def __getitem__(self, address):
            return self.values.get(address, set())

    paths = PythonHeapReferenceCollector._inbound_paths(
        target=4,
        inbound=Inbound(),
        objects={address: object() for address in (1, 2, 3, 4, 8, 9)},
        thread_roots={1},
        max_depth=3,
        remaining=2,
    )

    assert [1, 2, 3, 4] in paths
    assert [9, 8, 4] in paths
    assert len(paths) <= 2


def test_pyheap_missing_runtime_capability_is_structured_blocked(tmp_path):
    collector = PythonHeapReferenceCollector()
    collector.OUTPUT_BASE = str(tmp_path / "out")
    task = _task(tmp_path / "unused")
    task = task.__class__(**{**task.__dict__, "options": {}})

    with mock.patch.object(collector, "_capture_capability_error", return_value=("gdb_not_installed", "GDB 不可用")):
        result = collector.collect(task)

    assert result.ok is False
    assert result.artifacts[0]["metadata"]["data"]["evidence_validity"]["reason"] == "gdb_not_installed"
