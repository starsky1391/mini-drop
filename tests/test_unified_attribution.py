from server.app.diagnosis.attribution_engine import (
    build_attribution_graph,
    build_scenario_facts,
    qualify_attribution,
)
from server.app.diagnosis.attribution_models import ProbeManifestEntry
from server.app.diagnosis.probe_registry import build_probe_manifest


def test_manifest_entries_expose_capability_boundaries():
    manifest = build_probe_manifest()
    assert manifest["schema_version"] == "2.0"
    entries = [
        ProbeManifestEntry.model_validate(item)
        for item in manifest["available_probes"]
    ]
    source = next(item for item in entries if item.probe_id == "process_source_snapshot")
    assert source.capability_role == "source_relation"
    assert "正式根因" in source.cannot_establish
    assert "source_line_candidate" in source.produces


def test_runtime_primitive_is_cost_center_not_root_cause():
    facts = build_scenario_facts({
        "evidence_window": {
            "timing_relation": "same_window",
            "evidence_cohort_id": "cohort-1",
        },
        "top_functions": [{
            "name": "poll",
            "percent": 91.0,
            "samples": 91,
            "evidence_ref": "ev:profile",
        }],
        "sys_metrics": {"summary": {"avg_cpu_iowait_pct": 12.0}},
        "evidence_index": {
            "evidence_validity_by_family": {"python_runtime_profile": "valid"},
        },
    })
    assert facts.cost_centers[0].primitive is True
    assert not facts.trigger_candidates
    assert "trigger" in qualify_attribution(
        build_attribution_graph(evidence={
            "top_functions": [{
                "name": "poll",
                "percent": 91.0,
                "samples": 91,
                "evidence_ref": "ev:profile",
            }],
            "sys_metrics": {"summary": {"avg_cpu_iowait_pct": 12.0}},
            "evidence_index": {
                "evidence_validity_by_family": {"python_runtime_profile": "valid"},
                "evidence_window": {
                    "timing_relation": "same_window",
                    "evidence_cohort_id": "cohort-1",
                },
            },
        })
    ).missing_evidence


def test_source_line_and_mechanism_are_relations_until_all_gates_exist():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {
                "timing_relation": "same_window",
                "evidence_cohort_id": "cohort-1",
            },
            "top_functions": [{
                "name": "Rule.compile",
                "file": "werkzeug/routing.py",
                "line": 768,
                "percent": 72.0,
                "evidence_ref": "ev:runtime",
            }],
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 82.0}},
            "evidence_index": {
                "evidence_validity_by_family": {
                    "python_runtime_profile": "valid",
                    "source_snapshot": "valid",
                },
            },
            "source_snapshot_json": {
                "verified_line_candidates": [{
                    "file": "werkzeug/routing.py",
                    "line": 768,
                    "symbol": "Rule.compile",
                    "evidence_ref": "ev:source",
                    "eligibility_status": "verified",
                }],
            },
        },
        target={"service_id": "web", "pid": 123},
    )
    assert graph.source_relations[0].relation == "calls"
    assert graph.source_relations[0].status == "verified"
    qualification = qualify_attribution(graph)
    assert qualification.level in {"L1", "L2"}
    assert qualification.qualification != "formal_root_cause"
    assert qualification.decision == "continue_probe"


def test_codeql_path_is_source_relation_not_runtime_proof():
    graph = build_attribution_graph(
        evidence={
            "evidence_window": {"timing_relation": "same_window"},
            "top_functions": [{
                "name": "Rule.compile",
                "percent": 80.0,
                "evidence_ref": "ev:runtime",
            }],
            "sys_metrics": {"summary": {"avg_cpu_user_pct": 80.0}},
            "evidence_index": {
                "evidence_validity_by_family": {"source_mechanism_query": "valid"},
            },
        },
        source_mechanism={
            "mechanism_paths": [{
                "status": "supported",
                "statement": "container write path",
                "relations": ["retains"],
                "evidence_ref": "ev:codeql",
                "nodes": [
                    {"file": "werkzeug/routing.py", "line": 768, "symbol": "Rule.compile"},
                    {"file": "werkzeug/routing.py", "line": 770, "symbol": "Map._rules"},
                ],
            }],
        },
    )
    assert any(item.relation == "retains" for item in graph.source_relations)
    qualification = qualify_attribution(graph)
    assert qualification.level == "L2"
    assert qualification.causal_status == "unproven"
