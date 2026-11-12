"""Backend ↔ AI server document recognition (first cross-repo integration step)."""

import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.ai_service import AiDocumentClient
from app.core import ApiError, Settings, uid, utcnow
from app.db import Database
from app.materials import AnalysisHandler, extract
from app.models import Job, Material, User
from app.providers import ProviderFailure
from app.storage import LocalStorage


def client(handler, **kwargs):
    return AiDocumentClient(
        "http://127.0.0.1:8001",
        "server-key",
        transport=httpx.MockTransport(handler),
        profile="test",
        **kwargs,
    )


def extraction(pages, ready=True):
    return {
        "document": {"pages": [{"number": n, "text": t} for n, t in pages]},
        "chunk_drafts": [],
        "method": "upstage",
        "warnings": [],
        "ready_for_analysis": ready,
        "mime_type": "application/pdf",
    }


def test_sends_raw_bytes_with_server_key_and_maps_pages():
    seen = {}

    def handler(request):
        seen.update(
            path=request.url.path,
            params=dict(request.url.params),
            mime=request.headers["content-type"],
            key=request.headers["x-ai-api-key"],
            body=request.content,
        )
        return httpx.Response(200, json=extraction([(1, "page one"), (3, "page three")]))

    pages = client(handler).extract_pages("application/pdf", b"%PDF-raw", material_id="m-1", filename="a.pdf")
    assert pages == ["page one", "", "page three"]
    assert seen == {
        "path": "/internal/materials/extract",
        "params": {"filename": "a.pdf", "material_id": "m-1"},
        "mime": "application/pdf",
        "key": "server-key",
        "body": b"%PDF-raw",
    }


@pytest.mark.parametrize(
    "status, code, retryable, unknown",
    [
        (401, "PROVIDER_UNAVAILABLE", False, False),
        (409, "TIMEOUT_UNKNOWN", False, True),
        (422, "INVALID_OUTPUT", False, False),
        (429, "PROVIDER_RATE_LIMIT", True, False),
        (503, "PROVIDER_UNAVAILABLE", True, False),
        (504, "TIMEOUT_UNKNOWN", False, True),
    ],
)
def test_error_statuses_map_to_job_error_codes(status, code, retryable, unknown):
    with pytest.raises(ProviderFailure) as caught:
        client(lambda r: httpx.Response(status, json={"error": {}})).extract_pages("image/png", b"x")
    assert (caught.value.code, caught.value.retryable, caught.value.outcome_unknown) == (
        code,
        retryable,
        unknown,
    )


def test_timeout_is_outcome_unknown_and_never_retried():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderFailure) as caught:
        client(handler).extract_pages("application/pdf", b"%PDF-")
    assert caught.value.code == "TIMEOUT_UNKNOWN" and caught.value.outcome_unknown
    assert calls == [1]


@pytest.mark.parametrize(
    "body",
    [
        extraction([(1, "text")], ready=False),
        extraction([(1, "   ")]),
        extraction([(2, "a"), (2, "b")]),
        {"document": {}},
        ["not", "an", "object"],
    ],
)
def test_unusable_recognition_is_invalid_output(body):
    with pytest.raises(ProviderFailure) as caught:
        client(lambda r: httpx.Response(200, json=body)).extract_pages("application/pdf", b"%PDF-")
    assert caught.value.code == "INVALID_OUTPUT"


def test_configuration_rules():
    assert AiDocumentClient.from_settings(Settings(profile="test")) is None
    with pytest.raises(ValueError):
        AiDocumentClient("http://127.0.0.1:8001", "")
    with pytest.raises(ValueError):
        AiDocumentClient("ftp://ai", "k")
    with pytest.raises(ValueError):
        AiDocumentClient("http://ai.internal", "k", profile="production")
    with pytest.raises(ValueError):
        AiDocumentClient(
            "http://127.0.0.1", "k", transport=httpx.MockTransport(lambda r: None), profile="production"
        )
    assert (
        AiDocumentClient("https://ai.internal/", "k", profile="production").base_url == "https://ai.internal"
    )
    configured = AiDocumentClient.from_settings(
        Settings(profile="test", ai_service_url="http://127.0.0.1:8001", ai_service_api_key="k")
    )
    assert configured.handles("application/pdf") and configured.handles("image/jpeg")
    assert not configured.handles("text/plain")


@pytest.fixture
def image_material(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "test.db"))
    db.create_schema()
    user, mid, jid, key, now = uid(), uid(), uid(), uid(), utcnow()
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buf, format="PNG")
    data = buf.getvalue()
    with db.transaction() as s:
        s.add(User(id=user, auth_issuer="i", auth_subject=user, timezone="Asia/Seoul", created_at=now))
        s.flush()
        s.add(
            Material(
                id=mid,
                user_id=user,
                filename="board.png",
                mime_type="image/png",
                size_bytes=len(data),
                storage_key=key,
                processing_status="queued",
                archived=False,
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()
        s.add(
            Job(
                id=jid,
                user_id=user,
                kind="analysis",
                status="queued",
                resource_type="material",
                resource_id=mid,
                request_fingerprint="f" * 64,
                request_payload={},
                model_id="test",
                attempt_no=1,
                retryable=False,
                fencing_token=0,
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()
        s.get(Material, mid).latest_job_id = jid
    settings = Settings(
        profile="test",
        storage_root=str(tmp_path / "objects"),
        public_base_url="http://t",
        signing_secret="x" * 32,
    )
    ctx = SimpleNamespace(db=db, storage=LocalStorage(settings))
    ctx.storage.write(key, data)
    return ctx, mid, jid


def test_analysis_uses_ai_recognition_text_chunks_for_images(image_material):
    ctx, mid, jid = image_material
    sent = []

    def handler(request):
        sent.append(dict(request.url.params))
        return httpx.Response(200, json=extraction([(1, "x" * 4100)]))

    ctx.ai_documents = client(handler)
    with ctx.db.transaction() as s:
        payload = AnalysisHandler(ctx).prepare(s, s.get(Job, jid))
    assert "image_data_url" not in payload
    assert [(c["page_number"], c["ordinal"], len(c["content"])) for c in payload["chunks"]] == [
        (1, 1, 4000),
        (1, 2, 100),
    ]
    assert sent == [{"filename": "material", "material_id": mid}]
    json.dumps(payload)


def test_ai_recognized_image_persists_recognized_text_chunks(image_material):
    # Regression: persistence used the vision branch (source_text) for every image,
    # so AI-recognized images always failed with INVALID_OUTPUT.
    from sqlalchemy import select

    from app.models import Concept, MaterialChunk

    ctx, mid, jid = image_material
    ctx.ai_documents = client(
        lambda request: httpx.Response(200, json=extraction([(1, "Momentum is conserved.")]))
    )
    handler = AnalysisHandler(ctx)
    with ctx.db.transaction() as s:
        snapshot = handler.prepare_snapshot(s, s.get(Job, jid))
    prepared = handler.prepare_external(snapshot)
    chunk = prepared.payload["chunks"][0]
    output = {
        "units": [
            {
                "title": "Momentum",
                "concepts": [
                    {
                        "name": "Conservation",
                        "summary": "Momentum is conserved.",
                        "checklist_items": ["State the conservation rule."],
                        "evidence": [{"chunk_id": chunk["id"], "quote": "Momentum is conserved"}],
                    }
                ],
            }
        ]
    }
    with ctx.db.transaction() as s:
        assert handler.persist_prepared(s, s.get(Job, jid), output, prepared) == f"/v1/materials/{mid}"
    with ctx.db.transaction() as s:
        chunks = s.scalars(select(MaterialChunk).where(MaterialChunk.material_id == mid)).all()
        assert [(c.id, c.page_number, c.content) for c in chunks] == [
            (chunk["id"], 1, "Momentum is conserved.")
        ]
        assert s.scalar(select(Concept.id).where(Concept.material_id == mid))


def test_without_ai_server_images_still_go_to_vision_provider(image_material):
    ctx, mid, jid = image_material
    ctx.ai_documents = None
    with ctx.db.transaction() as s:
        payload = AnalysisHandler(ctx).prepare(s, s.get(Job, jid))
    assert payload["image_data_url"].startswith("data:image/png;base64,")


def test_scanned_pdf_passes_local_validation_only_when_text_is_optional():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()
    m = SimpleNamespace(mime_type="application/pdf", size_bytes=len(data))
    with pytest.raises(ApiError):
        extract(m, data)
    assert extract(m, data, require_text=False) == [""]
