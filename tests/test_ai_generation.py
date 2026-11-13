"""Backend -> AI server generation adapter (gemini:* models, ai-internal-api.yaml 0.3.0).

The fake AI server below answers with the AI repo's documented response shapes
(src/models.py) and checks the request headers/IDs the real server requires.
No network, AI provider or paid call is used.
"""

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

import tests.test_ab_e2e as ab_e2e
from app.ai_generation import (
    AI_SERVER_MANAGED_KEY,
    AiServerProvider,
    CompositeProviders,
    GeminiTeachbackClient,
    _ChunkIds,
    _generation_failure,
    _verbatim,
    build_providers,
)
from app.core import ApiError, Settings
from app.providers import ProviderFailure, ProviderRegistry
from tests.ab_support import KEY, ab_http_application

GEMINI = "gemini:fixture-model"
SERVER_KEY = "server-to-server-key"
ANSWER = "When total external force is zero"


def v4(value):
    parsed = uuid.UUID(str(value))
    assert parsed.version == 4, "AI server identifiers must be UUID v4"
    return str(parsed)


KINDS = {
    "/internal/materials/analyze": "analysis",
    "/internal/questions/generate": "questions",
    "/internal/questions/grade": "grade",
    "/internal/explanations/generate": "learning_explanation",
    "/internal/evaluations/generate": "report",
}


class FakeAiServer:
    """Minimal stand-in for the AI server: validates IDs, echoes grounded evidence."""

    def __init__(self, calls=None, expected_key=KEY):
        self.calls = [] if calls is None else calls
        self.expected_key = expected_key

    def __call__(self, request):
        assert request.headers["X-AI-API-Key"] == SERVER_KEY
        assert request.headers.get("X-AI-Key") == self.expected_key
        body = json.loads(request.content)
        path = request.url.path
        if path in KINDS:
            self.calls.append((KINDS[path], body))
        if path == "/internal/materials/analyze":
            chunk = body["chunks"][0]
            v4(chunk["id"]), v4(chunk["material_id"])
            evidence = {k: chunk[k] for k in ("material_id", "material_name", "page_number")}
            return httpx.Response(
                200,
                json={
                    "state": "draft",
                    "units": [
                        {
                            "title": "Momentum",
                            "concepts": [
                                {
                                    "name": "Momentum conservation",
                                    "summary": "Momentum is conserved when total external force is zero.",
                                    "checklist_items": ["State the external force condition."],
                                    "keywords": ["momentum"],
                                    "evidence": [
                                        {**evidence, "chunk_id": chunk["id"], "excerpt": chunk["text"][:60]}
                                    ],
                                    "estimated_difficulty": None,
                                    "estimated_importance": None,
                                }
                            ],
                        }
                    ],
                },
            )
        context = body["context"]
        for chunk in context["chunks"]:
            v4(chunk["id"])
        concept = context["concepts"][0]
        version = concept["version"]
        evidence = version["evidence"][0]
        if path == "/internal/questions/generate":
            assert 5 <= body["count"] <= 20
            item = {
                "question": {
                    "question_type": "short_answer",
                    "concept_ids": [concept["concept_id"]],
                    "concept_version_ids": [version["id"]],
                    "prompt": "When is momentum conserved?",
                    "options": [],
                    "difficulty": body["difficulty"],
                    "evidence": [evidence],
                },
                "correct_answer": {"type": "short_answer", "text": ANSWER},
                "explanation": "The source states the external force condition.",
                "accepted_answers": [ANSWER],
                "arithmetic_expression": None,
            }
            return httpx.Response(
                200,
                json={
                    "items": [item] * body["count"],
                    "verification": [{"status": "not_verified", "scope": "fixture"}] * body["count"],
                },
            )
        if path == "/internal/questions/grade":
            stored = body["stored_question"]
            correct = body["answer"]["text"].strip().lower() == stored["correct_answer"]["text"].lower()
            return httpx.Response(
                200,
                json={
                    "is_correct": correct,
                    "feedback": "Compare with the external force condition.",
                    "correct_answer": stored["correct_answer"],
                    "explanation": stored["explanation"],
                    "evidence": stored["question"]["evidence"],
                },
            )
        if path == "/internal/explanations/generate":
            return httpx.Response(
                200,
                json={
                    "title": "Momentum conservation",
                    "explanation_markdown": f"## 핵심 내용\n\n- Total momentum stays constant. [근거: {evidence['chunk_id']}]",
                    "evidence": [evidence, evidence],
                },
            )
        if path == "/internal/evaluations/generate":
            assert any(m["role"] == "user" for m in body["history"])
            items = [
                {
                    "concept_id": concept["concept_id"],
                    "concept_version_id": version["id"],
                    "checklist_item_id": item["id"],
                    "status": "explained",
                    "description": "The learner stated the external force condition.",
                    "confidence": "high",
                    "evidence": [evidence],
                }
                for item in version["checklist_items"]
            ]
            return httpx.Response(
                200,
                json={
                    "concept_version_id": version["id"],
                    "coverage": 1.0,
                    "coverage_formula": "weighted-v1",
                    "assessable_count": len(items),
                    "weighted_score": float(len(items)),
                    "items": items,
                    "next_action": "Explain a case where external force changes momentum.",
                    "weak_checklist_item_ids": [],
                    "weak_concept_ids": [],
                },
            )
        raise AssertionError("Unexpected AI server path " + path)


def gemini_reply(text='{"assistant_content": "Why must the external force be zero?"}', reason="STOP"):
    return httpx.Response(
        200, json={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": reason}]}
    )


def provider(fake=None, gemini=None):
    teachback = GeminiTeachbackClient(
        transport=httpx.MockTransport(gemini or (lambda request: gemini_reply())), profile="test"
    )
    return AiServerProvider(
        "http://ai.test",
        SERVER_KEY,
        (GEMINI,),
        transport=httpx.MockTransport(fake or FakeAiServer()),
        profile="test",
        teachback=teachback,
        provider_key=KEY,
    )


def test_full_http_pipeline_through_the_ai_server_adapter(tmp_path, monkeypatch):
    # Same signed-JWT HTTP journey as test_ab_e2e, but every AI step goes through
    # the adapter: analysis/questions/grade/explanation/report to the AI server,
    # teach-back replies to Gemini, and each mapped output must persist.
    calls = []
    gemini_bodies = []

    def gemini(request):
        assert request.headers["x-goog-api-key"] == KEY
        assert request.url.path == "/v1beta/models/fixture-model:generateContent"
        body = json.loads(request.content)
        gemini_bodies.append(body)
        calls.append(("message", body["generationConfig"]))
        return gemini_reply()

    monkeypatch.setattr(ab_e2e, "MODEL", GEMINI)
    with ab_http_application(tmp_path) as fixture:
        fixture.ctx.providers = CompositeProviders(
            ProviderRegistry(Settings(profile="test")), provider(FakeAiServer(calls), gemini)
        )
        fixture.provider = SimpleNamespace(calls=calls)
        ab_e2e.assert_ab_http_pipeline(fixture)
    prompt = json.loads(gemini_bodies[0]["contents"][0]["parts"][0]["text"])
    assert "ai_context" not in prompt and prompt["questions_remaining"] == 3
    analysis = next(body for kind, body in calls if kind == "analysis")
    assert analysis["chunks"][0]["material_name"] == "local-momentum.txt"


def test_chunk_ids_round_trip_as_uuid_v4():
    ids = _ChunkIds()
    v5 = str(uuid.uuid5(uuid.uuid4(), "1"))
    plain = str(uuid.uuid4())
    mapped = ids.out(v5)
    assert uuid.UUID(mapped).version == 4 and mapped != v5
    assert ids.out(v5) == mapped and ids.back(mapped) == v5
    assert ids.out(plain) == plain and ids.back(plain) == plain
    with pytest.raises(ValueError):
        ids.back(str(uuid.uuid4()))


def test_verbatim_recovers_exact_source_text():
    text = "Momentum is  conserved\nwhen external force is zero."
    assert _verbatim("Momentum is  conserved", text) == "Momentum is  conserved"
    assert _verbatim("momentum is conserved when", text) == "Momentum is  conserved\nwhen"
    assert _verbatim("energy", text) is None and _verbatim("  ", text) is None


@pytest.mark.parametrize(
    "status, code, expected, retryable, unknown",
    [
        (422, "PROVIDER_AUTH", "PROVIDER_AUTH", True, False),
        (422, "VALIDATION_ERROR", "PROVIDER_UNAVAILABLE", False, False),
        (429, "RATE_LIMITED", "PROVIDER_RATE_LIMIT", True, False),
        (502, "INVALID_OUTPUT", "INVALID_OUTPUT", False, False),
        (502, "PROVIDER_REQUEST_INVALID", "PROVIDER_UNAVAILABLE", False, False),
        (502, "PROVIDER_BAD_GATEWAY", "TIMEOUT_UNKNOWN", False, True),
        (503, "CONFIGURATION_ERROR", "PROVIDER_UNAVAILABLE", False, False),
        (503, "PROVIDER_SERVICE_UNAVAILABLE", "TIMEOUT_UNKNOWN", False, True),
        (504, "PROVIDER_TIMEOUT", "TIMEOUT_UNKNOWN", False, True),
        (409, None, "TIMEOUT_UNKNOWN", False, True),
        (401, None, "PROVIDER_UNAVAILABLE", False, False),
        (413, None, "PROVIDER_UNAVAILABLE", False, False),
    ],
)
def test_ai_server_errors_map_to_job_error_codes(status, code, expected, retryable, unknown):
    content = json.dumps({"error": {"code": code, "message": "x"}}).encode() if code else b"{}"
    failure = _generation_failure(status, content)
    assert (failure.code, failure.retryable, failure.outcome_unknown) == (expected, retryable, unknown)


def explanation_payload():
    chunk_a, chunk_b = str(uuid.uuid5(uuid.uuid4(), "1")), str(uuid.uuid5(uuid.uuid4(), "2"))
    material, version = str(uuid.uuid4()), str(uuid.uuid4())

    def source(chunk, text):
        return {
            "chunk_id": chunk,
            "material_id": material,
            "material_name": "m.txt",
            "page_number": 1,
            "excerpt": text,
        }

    context = {
        "concepts": [
            {
                "concept_id": str(uuid.uuid4()),
                "unit_id": str(uuid.uuid4()),
                "material_id": material,
                "version": {
                    "id": version,
                    "version_no": 1,
                    "name": "Momentum",
                    "summary": "Momentum is conserved.",
                    "is_confirmed": True,
                    "is_current": True,
                    "confirmed_at": "2026-11-12T00:00:00Z",
                    "checklist_items": [{"id": str(uuid.uuid4()), "ordinal": 1, "criterion": "State it."}],
                    "evidence": [
                        source(chunk_a, "Momentum is conserved."),
                        source(chunk_b, "Forces change it."),
                    ],
                },
            }
        ],
        "chunks": [
            {
                "id": chunk_a,
                "material_id": material,
                "material_name": "m.txt",
                "page_number": 1,
                "text": "Momentum is conserved.",
            },
            {
                "id": chunk_b,
                "material_id": material,
                "material_name": "m.txt",
                "page_number": 1,
                "text": "Forces change it.",
            },
        ],
    }
    payload = {
        "instructions": "x",
        "language": "ko",
        "version": {"id": version},
        "ai_context": {"context": context},
    }
    return payload, chunk_a, chunk_b


def test_explanation_maps_ids_back_and_drops_citations_without_evidence():
    payload, chunk_a, chunk_b = explanation_payload()

    def respond(request):
        body = json.loads(request.content)
        a, b = (c["id"] for c in body["context"]["chunks"])
        return httpx.Response(
            200,
            json={
                "title": "Momentum",
                "explanation_markdown": f"- kept [근거: {a}]\n- dropped [근거: {b}]",
                "evidence": [
                    {
                        "chunk_id": a,
                        "material_id": "m",
                        "material_name": "m.txt",
                        "page_number": 1,
                        "excerpt": "momentum IS conserved",
                    },
                    {
                        "chunk_id": b,
                        "material_id": "m",
                        "material_name": "m.txt",
                        "page_number": 1,
                        "excerpt": "not in source",
                    },
                ],
            },
        )

    output = provider(lambda request: respond(request)).generate(GEMINI, KEY, "learning_explanation", payload)
    assert output["evidence"] == [{"chunk_id": chunk_a, "excerpt": "Momentum is conserved"}]
    assert f"[근거: {chunk_a}]" in output["explanation_markdown"]
    assert "[근거:" not in output["explanation_markdown"].split("dropped")[1]


def report_payload():
    payload, chunk_a, _ = explanation_payload()
    context = payload["ai_context"]["context"]
    checklist = context["concepts"][0]["version"]["checklist_items"]
    return {
        "language": "ko",
        "concept": {
            "id": context["concepts"][0]["concept_id"],
            "version_id": context["concepts"][0]["version"]["id"],
        },
        "checklist": [
            {"id": c["id"], "criterion": c["criterion"], "ordinal": c["ordinal"]} for c in checklist
        ],
        "source_evidence": [
            {
                "chunk_id": chunk_a,
                "material_id": "m",
                "material_name": "m.txt",
                "page_number": 1,
                "source_excerpt": "Momentum is conserved.",
            }
        ],
        "conversation": [{"role": "user", "content": "It is conserved without external force."}],
        "instructions": "x",
        "ai_context": {"context": context},
    }, checklist[0]["id"]


def test_report_without_verbatim_evidence_becomes_unverified_and_is_clipped():
    payload, item_id = report_payload()

    def respond(request):
        body = json.loads(request.content)
        chunk = body["context"]["chunks"][0]["id"]
        return httpx.Response(
            200,
            json={
                "concept_version_id": body["concept_version_id"],
                "coverage": 1.0,
                "coverage_formula": "weighted-v1",
                "assessable_count": 1,
                "weighted_score": 1.0,
                "items": [
                    {
                        "concept_id": payload["concept"]["id"],
                        "concept_version_id": body["concept_version_id"],
                        "checklist_item_id": item_id,
                        "status": "explained",
                        "description": "d" * 5000,
                        "confidence": "high",
                        "evidence": [
                            {
                                "chunk_id": chunk,
                                "material_id": "m",
                                "material_name": "m",
                                "page_number": 1,
                                "excerpt": "Forces change it.",
                            }
                        ],
                    }
                ],
                "next_action": "n" * 5000,
                "weak_checklist_item_ids": [],
                "weak_concept_ids": [],
            },
        )

    output = provider(respond).generate(GEMINI, KEY, "report", payload)
    item = output["items"][0]
    assert (item["status"], item["confidence"], item["evidence"]) == ("unverified", "low", [])
    assert len(item["description"]) == 1200 and len(output["next_action"]) == 2000


def test_small_question_sets_request_the_ai_minimum_and_keep_the_first_items():
    payload, _, _ = explanation_payload()
    context = payload["ai_context"]["context"]
    version = context["concepts"][0]["version"]
    seen = {}

    def respond(request):
        body = json.loads(request.content)
        seen.update(body)
        evidence = body["context"]["concepts"][0]["version"]["evidence"][0]
        item = {
            "question": {
                "question_type": "multiple_choice",
                "concept_ids": [context["concepts"][0]["concept_id"]],
                "concept_version_ids": [version["id"]],
                "prompt": "Q",
                "options": [{"id": "A", "text": "yes"}, {"id": "B", "text": "no"}],
                "difficulty": "easy",
                "evidence": [evidence, evidence],
            },
            "correct_answer": {"type": "multiple_choice", "selected_option_ids": ["A"]},
            "explanation": "Because.",
            "accepted_answers": [],
        }
        return httpx.Response(200, json={"items": [item] * body["count"], "verification": []})

    questions_payload = {
        "count": 2,
        "difficulty": "easy",
        "versions": [{"id": version["id"]}],
        "ai_context": {"unit_id": context["concepts"][0]["unit_id"], "context": context},
    }
    output = provider(respond).generate(GEMINI, KEY, "questions", questions_payload)
    assert seen["count"] == 5 and len(output["questions"]) == 2
    first = output["questions"][0]
    assert first["answer_key"] == {"type": "multiple_choice", "selected_option_ids": ["A"]}
    assert first["evidence_chunk_ids"] == [version["evidence"][0]["chunk_id"]]


def test_transport_failures_never_hide_a_possible_paid_call():
    payload, _, _ = explanation_payload()

    def connect(request):
        raise httpx.ConnectError("refused")

    def read(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ProviderFailure) as refused:
        provider(connect).generate(GEMINI, KEY, "learning_explanation", payload)
    assert (refused.value.code, refused.value.retryable) == ("PROVIDER_UNAVAILABLE", True)
    with pytest.raises(ProviderFailure) as slow:
        provider(read).generate(GEMINI, KEY, "learning_explanation", payload)
    assert (slow.value.code, slow.value.outcome_unknown) == ("TIMEOUT_UNKNOWN", True)


def test_missing_ai_context_or_oversized_request_fails_before_dispatch():
    with pytest.raises(ProviderFailure) as failure:
        provider().preflight(GEMINI, "learning_explanation", {"instructions": "x"})
    assert failure.value.code == "PROVIDER_UNAVAILABLE"
    payload, _, _ = explanation_payload()
    payload["ai_context"]["context"]["chunks"][0]["text"] = "가" * 800_000  # > 2 MiB as UTF-8
    with pytest.raises(ProviderFailure) as oversized:
        provider().preflight(GEMINI, "learning_explanation", payload)
    assert oversized.value.code == "PROVIDER_UNAVAILABLE"


def test_service_key_comes_from_server_settings_not_requests():
    assert provider().service_key(GEMINI) == KEY
    assert not hasattr(provider(), "validate_key")
    # Without a backend GEMINI_API_KEY the AI server's own key is used.
    unconfigured = AiServerProvider("https://ai.test", SERVER_KEY, (GEMINI,), profile="production")
    assert unconfigured.service_key(GEMINI) == AI_SERVER_MANAGED_KEY
    assert unconfigured.list_models()[0]["available"] is True
    unconfigured.ensure_model(GEMINI)


def test_ai_server_owns_key_and_model_list_when_backend_has_none():
    seen = []

    def ai_server(request):
        seen.append(request)
        assert request.headers["X-AI-API-Key"] == SERVER_KEY
        assert "X-AI-Key" not in request.headers
        if request.url.path == "/internal/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": GEMINI, "provider": "gemini", "label": GEMINI, "available": True},
                        {"id": "gemini:off", "provider": "gemini", "label": "off", "available": False},
                        {"id": "not-a-gemini-id", "provider": "x", "label": "x", "available": True},
                    ]
                },
            )
        return FakeAiServer(expected_key=None)(request)

    managed = AiServerProvider(
        "http://ai.test", SERVER_KEY, None, transport=httpx.MockTransport(ai_server), profile="test"
    )
    assert [(m["id"], m["available"]) for m in managed.list_models()] == [
        (GEMINI, True),
        ("gemini:off", False),
    ]
    assert managed.handles_model(GEMINI) and not managed.handles_model("not-a-gemini-id")
    key = managed.service_key(GEMINI)
    assert key == AI_SERVER_MANAGED_KEY
    payload, _, _ = explanation_payload()
    managed.preflight(GEMINI, "learning_explanation", payload)
    assert managed.generate(GEMINI, key, "learning_explanation", payload)
    # The model list is cached, not fetched for every check.
    assert sum(r.url.path == "/internal/models" for r in seen) == 1
    # Text teach-back still calls Gemini from the backend and needs its key.
    with pytest.raises(ProviderFailure) as missing:
        managed.preflight(GEMINI, "message", {"instructions": "x"})
    assert (missing.value.code, missing.value.retryable) == ("CREDENTIAL_UNAVAILABLE", True)


def test_unreachable_ai_server_lists_no_models():
    def down(request):
        raise httpx.ConnectError("down")

    managed = AiServerProvider(
        "http://ai.test", SERVER_KEY, None, transport=httpx.MockTransport(down), profile="test"
    )
    assert managed.list_models() == []
    with pytest.raises(ApiError) as unavailable:
        managed.ensure_model(GEMINI)
    assert unavailable.value.code == "MODEL_UNAVAILABLE"


def test_ai_service_url_alone_enables_ai_server_models():
    settings = Settings(profile="test", ai_service_url="http://ai.test", ai_service_api_key=SERVER_KEY)
    ai = AiServerProvider.from_settings(settings)
    assert ai is not None and ai.models is None
    assert AiServerProvider.from_settings(Settings(profile="test")) is None


@pytest.mark.parametrize(
    "response, code, retryable, unknown",
    [
        (gemini_reply(reason="MAX_TOKENS"), "INVALID_OUTPUT", False, False),
        (gemini_reply(text="not json"), "INVALID_OUTPUT", False, False),
        (httpx.Response(400, json={}), "PROVIDER_UNAVAILABLE", False, False),
        (httpx.Response(403, json={}), "PROVIDER_AUTH", True, False),
        (httpx.Response(429, json={}), "PROVIDER_RATE_LIMIT", True, False),
        (httpx.Response(503, json={}), "TIMEOUT_UNKNOWN", False, True),
    ],
)
def test_teachback_message_failures(response, code, retryable, unknown):
    with pytest.raises(ProviderFailure) as failure:
        provider(gemini=lambda request: response).generate(GEMINI, KEY, "message", {"instructions": "x"})
    assert (failure.value.code, failure.value.retryable, failure.value.outcome_unknown) == (
        code,
        retryable,
        unknown,
    )


def test_configuration_and_routing():
    assert AiServerProvider.from_settings(Settings(profile="test")) is None
    with pytest.raises(ValueError):
        AiServerProvider.from_settings(Settings(profile="test", ai_service_models=(GEMINI,)))
    for models in ((), ("openai:gpt",), (GEMINI, GEMINI), ("gemini:bad model",)):
        with pytest.raises(ValueError):
            AiServerProvider("http://ai.test", SERVER_KEY, models, profile="test")
    with pytest.raises(ValueError):
        AiServerProvider(
            "http://ai.test", SERVER_KEY, (GEMINI,), transport=httpx.MockTransport(lambda r: None)
        )
    with pytest.raises(ValueError):
        AiServerProvider("http://ai.example", SERVER_KEY, (GEMINI,), profile="production")

    settings = Settings(
        profile="test",
        enabled_models=("openai:test-model",),
        ai_service_url="http://127.0.0.1:8001",
        ai_service_api_key=SERVER_KEY,
        ai_service_models=(GEMINI,),
    )
    composite = build_providers(settings)
    assert isinstance(composite, CompositeProviders)
    assert [m["id"] for m in composite.list_models()] == ["openai:test-model", GEMINI]
    assert composite.uses_ai_server(GEMINI) and not composite.uses_ai_server("openai:test-model")
    composite.ensure_model(GEMINI)
    composite.ensure_model("openai:test-model")
    with pytest.raises(ApiError):
        composite.ensure_model("gemini:other")
    assert isinstance(build_providers(Settings(profile="test")), ProviderRegistry)


def test_ai_service_models_environment_list(monkeypatch):
    monkeypatch.setenv("AI_SERVICE_MODELS", f"{GEMINI}, gemini:second ,")
    assert Settings.from_env().ai_service_models == (GEMINI, "gemini:second")
