"""Text-budget boundaries and durable non-dispatch, using only fake transports."""

import json

import httpx
import pytest
from sqlalchemy import func, select

from app.core import Settings
from app.extraction import MAX_PAGES, MAX_TEXT_BYTES
from app.materials import analysis_chunks
from app.models import Job, SessionMessage, SessionReport, TeachbackSession
from app.providers import ProviderFailure, ProviderRegistry
from tests.test_ab_teachback import KEY, create, env as env, job, post
from tests.test_provider_review_n import image_bytes, image_data_url, response_for

MODELS = ("openai:test-model", "anthropic:claude-haiku-5-5")


def registry_for(model, calls):
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=response_for(model))

    return ProviderRegistry(
        Settings(profile="test", enabled_models=(model,), openai_api_key=KEY, anthropic_api_key=KEY),
        transport=httpx.MockTransport(respond),
    )


def prompt_from(body):
    content = body["messages"][-1]["content"]
    return next(block["text"] for block in content if block["type"] == "text")


@pytest.mark.parametrize("model", MODELS)
def test_exact_utf8_text_budget_preserves_full_prompt_then_rejects_one_byte_more(model):
    calls = []
    registry = registry_for(model, calls)
    overhead = len(json.dumps({"source": ""}).encode("utf-8"))
    payload = {"source": "x" * (registry.MAX_PROMPT_BYTES - overhead)}
    registry.preflight(model, "report", payload)
    assert calls == []
    assert registry.generate(model, KEY, "report", payload) == {"ok": True}
    assert len(prompt_from(calls[0]).encode("utf-8")) == registry.MAX_PROMPT_BYTES
    assert json.loads(prompt_from(calls[0])) == payload
    payload["source"] += "x"
    for operation in (
        lambda: registry.preflight(model, "report", payload),
        lambda: registry.generate(model, KEY, "report", payload),
    ):
        with pytest.raises(ProviderFailure) as caught:
            operation()
        assert caught.value.code == "PROVIDER_UNAVAILABLE"
        assert not caught.value.retryable and not caught.value.outcome_unknown
    assert len(calls) == 1


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("source", ["한" * 350_000, "\x01" * 175_000, "\ud800"])
def test_multibyte_escaped_or_invalid_unicode_never_dispatches(model, source):
    calls = []
    registry = registry_for(model, calls)
    assert len(source) < registry.MAX_PROMPT_BYTES
    with pytest.raises(ProviderFailure) as caught:
        registry.generate(model, KEY, "message", {"source": source})
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert not caught.value.outcome_unknown
    assert calls == []


@pytest.mark.parametrize("model", MODELS)
def test_separately_validated_image_does_not_consume_text_budget(model):
    calls = []
    registry = registry_for(model, calls)
    payload = {"instructions": "Read image"}
    registry.MAX_PROMPT_BYTES = len(json.dumps(payload).encode("utf-8"))
    payload["image_data_url"] = image_data_url(image_bytes())
    assert len(payload["image_data_url"]) > registry.MAX_PROMPT_BYTES
    registry.generate(model, KEY, "analysis", payload)
    assert json.loads(prompt_from(calls[0])) == {"instructions": "Read image"}
    assert any(block["type"] in {"image", "image_url"} for block in calls[0]["messages"][-1]["content"])
    payload["image_data_url"] = "data:image/png;base64,not-valid"
    with pytest.raises(ProviderFailure):
        registry.generate(model, KEY, "analysis", payload)
    assert len(calls) == 1


def test_maximum_extraction_even_with_worst_json_escaping_fits(monkeypatch):
    # Every page remains nonempty; the source exactly reaches its existing limit.
    page_size = MAX_TEXT_BYTES // MAX_PAGES
    pages = ["\x01" * (page_size - 1) + "x"] * MAX_PAGES
    assert sum(len(page.encode("utf-8")) for page in pages) == MAX_TEXT_BYTES
    monkeypatch.setattr("app.materials.extract", lambda material, data: pages)
    chunks = analysis_chunks(None, b"unused", "d21a01e5-4aa5-4cf6-a9a4-d0f71f9f49cf")
    calls = []
    registry = registry_for(MODELS[0], calls)
    payload = {"instructions": "Analyze source", "chunks": chunks}
    registry.generate(MODELS[0], KEY, "analysis", payload)
    prompt = prompt_from(calls[0])
    assert len(prompt.encode("utf-8")) < registry.MAX_PROMPT_BYTES
    assert json.loads(prompt) == payload


@pytest.mark.parametrize("kind", ["grade", "message", "report"])
def test_a_output_caps_remain_compatible_with_domain_character_limits(kind):
    calls = []
    registry = registry_for(MODELS[0], calls)
    registry.generate(MODELS[0], KEY, kind, {})
    assert calls[0]["max_completion_tokens"] == 16384


def test_growing_failed_message_history_rejected_before_dispatch_without_losing_history(env):
    sid = create(env, task=False)
    model = MODELS[0]
    sent_prompt_sizes = []

    def reject_request(request):
        body = json.loads(request.content)
        sent_prompt_sizes.append(len(prompt_from(body).encode("utf-8")))
        # A known, non-billed rejection allows the user to submit another message.
        return httpx.Response(400, json={"error": "synthetic test rejection"})

    env.ctx.providers = ProviderRegistry(
        Settings(profile="test", enabled_models=(model,), openai_api_key=KEY, anthropic_api_key=KEY),
        transport=httpx.MockTransport(reject_request),
    )
    content = "한" * 12000
    overflow_job = None
    for count in range(1, 40):
        response = post(
            env,
            f"/v1/teachback-sessions/{sid}/messages",
            {"model_id": model, "content": content, "input_mode": "text"},
        )
        assert response.status_code == 202, response.text
        jid = response.json()["data"]["job"]["id"]
        before = len(sent_prompt_sizes)
        env.ctx.jobs.run_once()
        state = job(env, jid)
        assert state["status"] == "failed" and state["error_code"] == "PROVIDER_UNAVAILABLE"
        assert not state["retryable"] and state["credential_ref"] is None
        if len(sent_prompt_sizes) == before:
            overflow_job = jid
            break
    assert overflow_job and len(sent_prompt_sizes) == count - 1
    assert all(size <= ProviderRegistry.MAX_PROMPT_BYTES for size in sent_prompt_sizes)
    with env.ctx.db.transaction() as db:
        assert db.get(Job, overflow_job).dispatched_at is None
        assert db.get(TeachbackSession, sid).question_count == 0
        messages = db.scalars(
            select(SessionMessage)
            .where(SessionMessage.session_id == sid)
            .order_by(SessionMessage.sequence_no)
        ).all()
        assert len(messages) == count
        assert [message.sequence_no for message in messages] == list(range(1, count + 1))
        assert all(message.content == content for message in messages)
    # Completion also retains all explanations, and fails locally when they do not fit.
    response = post(env, f"/v1/teachback-sessions/{sid}/complete", {"model_id": model})
    assert response.status_code == 202
    report_job = response.json()["data"]["job"]["id"]
    env.ctx.jobs.run_once()
    assert len(sent_prompt_sizes) == count - 1
    with env.ctx.db.transaction() as db:
        report = db.get(Job, report_job)
        assert report.status == "failed" and report.dispatched_at is None
        assert report.error_code == "PROVIDER_UNAVAILABLE" and not report.retryable
        assert db.scalar(select(func.count(SessionReport.id))) == 0
        assert db.scalar(select(func.count(SessionMessage.id))) == count
