"""Collector invocation identity tests."""

from server.app.diagnosis.collector_invocation import (
    build_collector_invocation,
    collector_request_fingerprint,
)


def test_codeql_query_hash_partitions_result_reuse_but_same_query_reuses():
    invocation = build_collector_invocation(
        scope_source="diagnosis_scope",
        collector_family="source_mechanism_query",
        probe_id="process_source_mechanism_query",
        target_config={"pid": 1234, "source_context": {"repo_revision": "abc123"}},
        target_context={"agent_id": "a1", "pid": 1234},
    )
    base = {
        "source_revision": "abc123",
        "line_candidates": [{"file": "src/routing.py", "line": 20}],
        "ai_generated_query": {
            "query_hash": "sha256:one",
            "investigation_question": "bound method 是否进入 code constants？",
        },
    }
    first = collector_request_fingerprint(invocation, base)
    repeated = collector_request_fingerprint(invocation, dict(base))
    changed = collector_request_fingerprint(invocation, {
        **base,
        "ai_generated_query": {
            "query_hash": "sha256:two",
            "investigation_question": "defaults 是否进入 code constants？",
        },
    })

    assert first == repeated
    assert changed != first
