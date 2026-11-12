"""Client for the team's internal AI server (repo 1st-line-tone-team-4/AI).

First integration step: document recognition only. When ``AI_SERVICE_URL`` is
set, the analysis worker sends PDF/PNG/JPEG bytes to the AI server's
``POST /internal/materials/extract`` (Upstage Document Parse) and builds analysis
chunks from the recognized page text instead of local pypdf text / vision input.
Plain text keeps the local path. Without ``AI_SERVICE_URL`` nothing changes.

The AI server never assigns database IDs; chunk IDs stay backend-derived. The
server keeps its own per-file cache and returns 409 DOCUMENT_PROCESSING_STATE
for a previously failed/unknown request, so this client never retries by itself.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from .providers import ProviderFailure

AI_EXTRACT_MIME_TYPES = ("application/pdf", "image/png", "image/jpeg")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Every code below must satisfy the ai_jobs.error_code CHECK (migrations/004).
# Must stay below JobService.prepare_seconds (60s): extraction runs in the
# worker's preparation phase, before the provider call is dispatched.
DEFAULT_TIMEOUT_SECONDS = 50


def validate_service_config(base_url, api_key, *, transport=None, profile=None):
    """Shared AI_SERVICE_URL/API key rules for every AI server client."""
    parts = urlsplit(base_url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        raise ValueError("AI_SERVICE_URL must be an absolute http(s) URL without query or fragment")
    if (
        parts.scheme == "http"
        and parts.hostname not in ("127.0.0.1", "localhost", "::1")
        and profile == "production"
    ):
        raise ValueError("Production AI_SERVICE_URL must use https unless it is loopback")
    if not api_key:
        raise ValueError("AI_SERVICE_API_KEY is required when AI_SERVICE_URL is set")
    if transport is not None and profile != "test":
        raise ValueError("Custom AI service transports are test-only")
    return base_url.rstrip("/")


class AiDocumentClient:
    def __init__(
        self, base_url, api_key, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, transport=None, profile=None
    ):
        validate_service_config(base_url, api_key, transport=transport, profile=profile)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @classmethod
    def from_settings(cls, settings):
        if not getattr(settings, "ai_service_url", ""):
            return None
        return cls(settings.ai_service_url, settings.ai_service_api_key, profile=settings.profile)

    def handles(self, mime_type):
        return mime_type in AI_EXTRACT_MIME_TYPES

    def extract_pages(self, mime_type, data, *, material_id=None, filename="material"):
        """Return recognized text per original page (1-based order, gaps as "")."""
        params = {"filename": (filename or "material")[:255]}
        if material_id:
            params["material_id"] = str(material_id)
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout_seconds) as client:
                response = client.post(
                    self.base_url + "/internal/materials/extract",
                    params=params,
                    content=data,
                    headers={"Content-Type": mime_type, "X-AI-API-Key": self.api_key},
                )
        except httpx.TimeoutException:
            # Upstage may already have processed (and billed) the document.
            raise ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True) from None
        except httpx.HTTPError:
            raise ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True) from None
        if response.status_code != 200:
            raise _failure(response.status_code)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ProviderFailure("INVALID_OUTPUT")
        try:
            body = response.json()
            if body.get("ready_for_analysis") is not True:
                raise ProviderFailure("INVALID_OUTPUT")
            pages = body["document"]["pages"]
            by_number = {}
            for page in pages:
                number, text = page["number"], page["text"]
                if type(number) is not int or not isinstance(text, str) or number < 1 or number in by_number:
                    raise ValueError()
                by_number[number] = text
        except ProviderFailure:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderFailure("INVALID_OUTPUT") from None
        if not by_number or max(by_number) > 200 or not any(t.strip() for t in by_number.values()):
            raise ProviderFailure("INVALID_OUTPUT")
        return [by_number.get(n, "") for n in range(1, max(by_number) + 1)]


def _failure(status):
    if status in (401, 403):
        return ProviderFailure("PROVIDER_UNAVAILABLE")  # server-to-server key misconfigured
    if status == 409:
        return ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    if status == 422:
        return ProviderFailure("INVALID_OUTPUT")  # AI server rejected the recognized document
    if status == 429:
        return ProviderFailure("PROVIDER_RATE_LIMIT", retryable=True)
    if status in (502, 503):
        return ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True)
    if status == 504:
        return ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    return ProviderFailure("PROVIDER_UNAVAILABLE")
