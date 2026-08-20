"""Scheme B deployment contract tests."""

from docs.ai_ops_v2_test.scripts.deploy_scheme_b_vm import SCHEME_B_FILES


def test_scheme_b_syncs_runtime_collectors_and_session_ai_modules():
    required = {
        "agent/mini_drop_agent/collectors/off_cpu.py",
        "agent/mini_drop_agent/collectors/perf.py",
        "agent/mini_drop_agent/collectors/python_heap.py",
        "agent/mini_drop_agent/collectors/python_heap_reference.py",
        "agent/mini_drop_agent/collectors/trace.py",
        "agent/mini_drop_agent/collectors/source_mechanism.py",
        "agent/mini_drop_agent/collectors/source_snapshot.py",
        "deploy/collectors/pyheap/pyheap_dump",
        "server/app/diagnosis/codeql_query_guard.py",
        "server/app/diagnosis/orchestrator.py",
        "server/app/diagnosis/intent.py",
        "server/app/diagnosis/session_conclusion.py",
        "server/app/rca/llm_client.py",
    }

    assert required <= set(SCHEME_B_FILES)
