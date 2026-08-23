from server.app.diagnosis.canonical_probe_plan import (
    merge_probe_input_maps,
    normalize_probe_plan,
    union_probe_families,
)


def test_family_union_is_stable_and_deduplicated():
    assert union_probe_families(
        ["source_mechanism_query", "cpu_profile"],
        ["source_mechanism_query", "trace_endpoint_profile"],
    ) == ["source_mechanism_query", "cpu_profile", "trace_endpoint_profile"]


def test_complete_query_replaces_abbreviated_provenance_without_losing_hash():
    abbreviated = {
        "source_mechanism_query": {
            "candidate_id": "child",
            "origin_parent_candidate_id": "parent",
        },
    }
    complete = {
        "source_mechanism_query": {
            "candidate_id": "child",
            "origin_parent_candidate_id": "parent",
            "ai_generated_query": {
                "candidate_id": "child",
                "origin_parent_candidate_id": "parent",
                "investigation_question": "Which source path retains the object?",
                "query_spec_hash": "query-hash",
            },
        },
    }
    result = merge_probe_input_maps(abbreviated, complete)
    assert result.conflicts == []
    assert result.inputs["source_mechanism_query"]["ai_generated_query"]["query_spec_hash"] == "query-hash"


def test_conflicting_provenance_and_query_hash_are_explicit_and_do_not_overwrite():
    first = {"source_mechanism_query": {
        "candidate_id": "child-a",
        "origin_parent_candidate_id": "parent",
        "ai_generated_query": {"query_spec_hash": "hash-a"},
    }}
    second = {"source_mechanism_query": {
        "candidate_id": "child-b",
        "origin_parent_candidate_id": "other-parent",
        "ai_generated_query": {"query_spec_hash": "hash-b"},
    }}
    result = merge_probe_input_maps(first, second)
    assert result.inputs["source_mechanism_query"]["candidate_id"] == "child-a"
    assert result.inputs["source_mechanism_query"]["ai_generated_query"]["query_spec_hash"] == "hash-a"
    assert {item["field"] for item in result.conflicts} == {
        "candidate_id", "origin_parent_candidate_id", "ai_generated_query.query_spec_hash",
    }


def test_unknown_family_is_rejected_when_registry_scope_is_supplied():
    result = merge_probe_input_maps(
        {"arbitrary_shell": {"candidate_id": "child"}},
        allowed_families=["cpu_profile"],
    )
    assert result.inputs == {}
    assert result.conflicts == [{
        "evidence_family": "arbitrary_shell",
        "status": "unknown_probe_family",
    }]


def test_normalize_plan_deduplicates_identical_requests():
    inputs = {"cpu_profile": {
        "candidate_id": "child",
        "origin_parent_candidate_id": "parent",
        "target_scope": {"pid": 42},
        "evidence_window": {"start": "t0", "end": "t1"},
    }}
    plan = normalize_probe_plan(["cpu_profile", "cpu_profile"], inputs)
    assert len(plan) == 1
    assert plan[0]["target_scope_hash"]
    assert plan[0]["evidence_window_hash"]
