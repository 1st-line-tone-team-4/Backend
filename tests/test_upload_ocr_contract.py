"""Signed-upload OCR parity, using only local synthetic PDFs and recognizers."""

import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from tests.support import test_application as make_application


@pytest.mark.parametrize("recognizes_pdf, expected", [(False, 422), (True, 204)])
def test_signed_upload_textless_pdf_requires_configured_recognizer(tmp_path, recognizes_pdf, expected):
    env = make_application(tmp_path)
    calls = []
    env.ctx.ai_documents = SimpleNamespace(
        handles=lambda mime: recognizes_pdf and mime == "application/pdf",
        extract_pages=lambda *args, **kwargs: calls.append(args),
    )
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    content = buffer.getvalue()
    with TestClient(env.app) as client:
        response = client.post(
            "/v1/materials",
            headers={"Authorization": "Bearer test-owner-token", "Idempotency-Key": "ocr-upload-test-0001"},
            json={"filename": "scan.pdf", "mime_type": "application/pdf", "size_bytes": len(content)},
        )
        assert response.status_code == 201
        url = response.json()["data"]["upload_url"]
        result = client.put(url, content=content, headers={"Content-Type": "application/pdf"})
        assert result.status_code == expected
        if expected == 422:
            assert result.json()["error"]["code"] == "INVALID_UPLOAD"
        else:
            assert (
                client.put(url, content=content, headers={"Content-Type": "application/pdf"}).status_code
                == 409
            )
    assert calls == []  # Recognition is deferred to the job, never paid during PUT.
    assert env.provider.calls == []


def test_ocr_upload_still_rejects_invalid_pdf_bytes(tmp_path):
    env = make_application(tmp_path)
    env.ctx.ai_documents = SimpleNamespace(handles=lambda mime: True)
    with TestClient(env.app) as client:
        result = client.post(
            "/v1/materials",
            headers={"Authorization": "Bearer test-owner-token", "Idempotency-Key": "ocr-upload-test-0002"},
            json={"filename": "bad.pdf", "mime_type": "application/pdf", "size_bytes": 5},
        )
        url = result.json()["data"]["upload_url"]
        assert (
            client.put(url, content=b"hello", headers={"Content-Type": "application/pdf"}).status_code == 422
        )
    assert env.provider.calls == []
