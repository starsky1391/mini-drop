import json
from types import SimpleNamespace

from server.app.diagnosis import intent as intent_module
from server.app.diagnosis.schemas import CreateDiagnosisRequest


def _request(query: str, *, source_context: dict | None = None) -> CreateDiagnosisRequest:
    return CreateDiagnosisRequest.model_validate({
        "query": query,
        "context": {
            "service_id": "werkzeug-routing",
            "instances": [{
                "service_id": "werkzeug-routing",
                "instance_id": "werkzeug-vulnerable",
                "host_id": "worker1",
                "agent_id": "agent-worker1",
                "pid": 21347,
                "source_context": source_context,
            }],
        },
    })


def test_retained_allocation_is_memory_not_io(monkeypatch):
    monkeypatch.setattr(intent_module, "is_feature_enabled", lambda feature: False)

    intent = intent_module.parse_diagnosis_intent(_request(
        "Memray retained allocation shows growing RSS and a possible memory leak",
    ))

    assert intent.symptom == "memory_pressure"


def test_trusted_memray_context_overrides_model_io_classification(monkeypatch):
    request = _request(
        "Analyze the retained allocations for this process",
        source_context={
            "memray_result_path": "/var/lib/mini-drop/profiles/werkzeug.bin",
            "memray_leaks_path": "/var/lib/mini-drop/profiles/werkzeug-leaks.csv",
        },
    )
    fallback = intent_module._fallback_intent(request)
    model_payload = fallback.model_copy(update={"symptom": "io_degradation"}).model_dump(mode="json")
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {"arguments": json.dumps(model_payload)},
                    }],
                },
            }],
        },
    )
    monkeypatch.setattr(intent_module, "is_feature_enabled", lambda feature: True)
    monkeypatch.setattr(
        intent_module,
        "get_ai_settings",
        lambda: SimpleNamespace(provider="openai", model="test-model"),
    )
    monkeypatch.setattr(intent_module, "chat_completions", lambda payload, timeout: response)

    intent = intent_module.parse_diagnosis_intent(request)

    assert intent.symptom == "memory_pressure"


def test_standalone_io_terms_still_classify_as_io(monkeypatch):
    monkeypatch.setattr(intent_module, "is_feature_enabled", lambda feature: False)

    plain_io = intent_module.parse_diagnosis_intent(_request("Investigate high io latency"))
    slash_io = intent_module.parse_diagnosis_intent(_request("Investigate elevated I/O latency"))

    assert plain_io.symptom == "io_degradation"
    assert slash_io.symptom == "io_degradation"


def test_diagnosis_request_accepts_application_runtime_log_paths():
    request = CreateDiagnosisRequest.model_validate({
        "query": "Investigate Python queue backlog",
        "context": {
            "service_id": "python-worker",
            "source_context": {
                "application_runtime_log_paths": ["/host/evidence/workload.ndjson"],
            },
            "instances": [{
                "service_id": "python-worker",
                "instance_id": "python-worker-1",
                "host_id": "worker1",
                "agent_id": "agent-worker1",
                "pid": 21347,
                "application_runtime_log_paths": ["/host/evidence/worker_observations.ndjson"],
            }],
        },
    })

    assert request.context.source_context
    assert request.context.source_context.application_runtime_log_paths == ["/host/evidence/workload.ndjson"]
    assert request.context.instances[0].application_runtime_log_paths == [
        "/host/evidence/worker_observations.ndjson",
    ]
