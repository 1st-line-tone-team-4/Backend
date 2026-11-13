"""Provider review regressions. Every request uses a fake transport."""

import asyncio
import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image, ImageFile

from app.core import Settings
from app.providers import ProviderFailure, ProviderRegistry


KEY = "provider-review-synthetic-key"
MODELS = ("openai:test-model", "anthropic:claude-haiku-5-5")


def image_bytes(size=(1, 1), image_format="PNG"):
    stream = BytesIO()
    with Image.new("RGB", size) as image:
        image.save(stream, format=image_format)
    return stream.getvalue()


def image_data_url(data, image_format="PNG"):
    media_type = "image/png" if image_format == "PNG" else "image/jpeg"
    return f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")


def response_for(model_id):
    if model_id.startswith("anthropic:"):
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"ok":true}'}]}
    return {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}


def registry_for(model_id, respond, **kwargs):
    return ProviderRegistry(
        Settings(profile="test", enabled_models=(model_id,), openai_api_key=KEY, anthropic_api_key=KEY),
        transport=httpx.MockTransport(respond),
        **kwargs,
    )


@pytest.mark.parametrize("model_id", MODELS)
def test_generation_has_fixed_total_and_http_budget(monkeypatch, model_id):
    deadlines, requests = [], []
    original_wait_for = asyncio.wait_for

    async def record_deadline(awaitable, timeout):
        deadlines.append(timeout)
        return await original_wait_for(awaitable, timeout)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=response_for(model_id))

    monkeypatch.setattr("app.providers.asyncio.wait_for", record_deadline)
    registry = registry_for(model_id, respond)
    assert registry.GENERATION_TIMEOUT_SECONDS == 300
    assert registry.TOTAL_TIMEOUT_SECONDS == 300
    # Contract 1.4.0 removed the non-generating key validation call.
    assert not hasattr(registry, "KEY_VALIDATION_TIMEOUT_SECONDS")
    assert not hasattr(registry, "validate_key")
    assert registry.generate(model_id, KEY, "analysis", {}) == {"ok": True}
    assert deadlines == [300]
    assert [request.method for request in requests] == ["POST"]
    assert [request.extensions["timeout"]["read"] for request in requests] == [300]
    assert all(request.extensions["timeout"]["connect"] == 10 for request in requests)


def test_generation_compatibility_timeout_cannot_extend_fixed_budget(monkeypatch):
    observed = []
    original_wait_for = asyncio.wait_for

    async def record_deadline(awaitable, timeout):
        observed.append(timeout)
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr("app.providers.asyncio.wait_for", record_deadline)
    model_id = MODELS[0]
    registry = registry_for(model_id, lambda request: httpx.Response(200, json=response_for(model_id)))
    registry.TOTAL_TIMEOUT_SECONDS = 600
    registry.generate(model_id, KEY, "analysis", {})
    assert observed == [300]


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize(
    "kind,expected",
    [("analysis", 16384), ("questions", 12288), ("learning_explanation", 4096), ("grade", 16384)],
)
@pytest.mark.parametrize("explicit_cap", [None, 256])
def test_kind_output_budget_preserves_explicit_live_cost_cap(model_id, kind, expected, explicit_cap):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=response_for(model_id))

    kwargs = {} if explicit_cap is None else {"max_completion_tokens": explicit_cap}
    registry = registry_for(model_id, respond, **kwargs)
    assert registry.generate(model_id, KEY, kind, {}) == {"ok": True}
    token_field = "max_tokens" if model_id.startswith("anthropic:") else "max_completion_tokens"
    assert bodies[0][token_field] == (expected if explicit_cap is None else explicit_cap)


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize(
    "status,code,retryable,unknown",
    [
        (400, "PROVIDER_UNAVAILABLE", False, False),
        (413, "PROVIDER_UNAVAILABLE", False, False),
        (401, "PROVIDER_AUTH", True, False),
        (403, "PROVIDER_AUTH", True, False),
        (429, "PROVIDER_RATE_LIMIT", True, False),
        (408, "TIMEOUT_UNKNOWN", False, True),
        (409, "TIMEOUT_UNKNOWN", False, True),
        (500, "TIMEOUT_UNKNOWN", False, True),
        (503, "TIMEOUT_UNKNOWN", False, True),
        (504, "TIMEOUT_UNKNOWN", False, True),
        (529, "TIMEOUT_UNKNOWN", False, True),
    ],
)
def test_error_classification_does_not_assume_server_errors_were_unprocessed(
    model_id, status, code, retryable, unknown
):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, text=KEY)

    registry = registry_for(model_id, respond)
    with pytest.raises(ProviderFailure) as error:
        registry.generate(model_id, KEY, "analysis", {})
    assert (error.value.code, error.value.retryable, error.value.outcome_unknown) == (
        code,
        retryable,
        unknown,
    )
    assert len(calls) == 1 and KEY not in str(error.value)


@pytest.mark.parametrize("model_id,limit", [(MODELS[0], 20 * 1024 * 1024), (MODELS[1], 7_500_000)])
def test_image_preflight_checks_real_decoded_boundary_and_calls_no_transport(model_id, limit):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=response_for(model_id))

    registry = registry_for(model_id, respond)
    png = image_bytes()
    at_limit = image_data_url(png + b"\0" * (limit - len(png)))
    payload = {"image_data_url": at_limit, "instructions": "Analyze source"}
    assert registry.preflight(model_id, "analysis", payload) is None
    assert payload["image_data_url"] == at_limit
    # For OpenAI, one byte over the decoded cap still fits the rounded base64
    # length ceiling. Checking only encoded string length would miss it.
    over_limit = image_data_url(png + b"\0" * (limit + 1 - len(png)))
    invalid_payload = {"image_data_url": over_limit}
    for action in (
        lambda: registry.preflight(model_id, "analysis", invalid_payload),
        lambda: registry.generate(model_id, KEY, "analysis", invalid_payload),
    ):
        with pytest.raises(ProviderFailure) as error:
            action()
        assert (error.value.code, error.value.retryable, error.value.outcome_unknown) == (
            "PROVIDER_UNAVAILABLE",
            False,
            False,
        )
    assert calls == []


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize(
    "image_data_url",
    [
        "data:image/png;base64,YQ=",
        "data:image/png;base64,YR==",
        "data:image/png;base64,YQ==\n",
        "data:image/png;base64,",
        "data:image/png;base64,====",
        "data:image/svg+xml;base64,YQ==",
        "https://untrusted.invalid/image.png",
        123,
    ],
)
def test_malformed_image_fails_locally_without_output_error_or_network(model_id, image_data_url):
    calls = []
    registry = registry_for(model_id, lambda request: calls.append(request))
    with pytest.raises(ProviderFailure) as error:
        registry.preflight(model_id, "analysis", {"image_data_url": image_data_url})
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert not error.value.retryable and not error.value.outcome_unknown
    assert calls == []


def test_anthropic_supported_api_key_header_is_used_for_generation():
    requests = []
    model_id = MODELS[1]

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=response_for(model_id))

    registry = registry_for(model_id, respond)
    registry.generate(model_id, KEY, "analysis", {})
    assert len(requests) == 1
    assert all(request.headers["x-api-key"] == KEY for request in requests)
    assert all(request.headers["anthropic-version"] == "2026-10-21" for request in requests)
    assert all("authorization" not in request.headers for request in requests)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
@pytest.mark.parametrize(
    "size,allowed", [((8000, 1), True), ((1, 8000), True), ((8001, 1), False), ((1, 8001), False)]
)
def test_anthropic_dimension_boundary_uses_headers_without_loading_pixels(
    monkeypatch, image_format, size, allowed
):
    data = image_bytes(size, image_format)
    payload = {"image_data_url": image_data_url(data, image_format)}
    calls = []
    registry = registry_for(MODELS[1], lambda request: calls.append(request))

    def reject_pixel_load(*args, **kwargs):
        pytest.fail("Provider preflight must not decode image pixels")

    monkeypatch.setattr(Image.Image, "load", reject_pixel_load)
    monkeypatch.setattr(ImageFile.ImageFile, "load", reject_pixel_load)
    if allowed:
        assert registry.preflight(MODELS[1], "analysis", payload) is None
    else:
        for action in (
            lambda: registry.preflight(MODELS[1], "analysis", payload),
            lambda: registry.generate(MODELS[1], KEY, "analysis", payload),
        ):
            with pytest.raises(ProviderFailure) as error:
                action()
            assert error.value.code == "PROVIDER_UNAVAILABLE"
            assert not error.value.retryable and not error.value.outcome_unknown
    assert calls == []


def test_tiny_encoded_png_with_excessive_width_is_rejected_before_network():
    data = image_bytes((9001, 1))
    assert len(data) < 128
    calls = []
    registry = registry_for(MODELS[1], lambda request: calls.append(request))
    with pytest.raises(ProviderFailure) as error:
        registry.preflight(MODELS[1], "analysis", {"image_data_url": image_data_url(data)})
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert calls == []


@pytest.mark.parametrize("data", [b"a", image_bytes()[:24], b"not an image"])
def test_anthropic_invalid_image_headers_are_local_input_failures(data):
    calls = []
    registry = registry_for(MODELS[1], lambda request: calls.append(request))
    with pytest.raises(ProviderFailure) as error:
        registry.preflight(MODELS[1], "analysis", {"image_data_url": image_data_url(data)})
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert not error.value.retryable and not error.value.outcome_unknown
    assert calls == []
