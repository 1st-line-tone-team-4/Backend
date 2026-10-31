import io
import pytest
from PIL import Image
from pypdf import PdfWriter
from app.core import ApiError
from app import extraction


def test_source_validation_and_extracted_text_cap():
    assert extraction.extract_bytes("text/plain", b"valid text") == ["valid text"]
    for mime, data in [
        ("text/plain", b"\xff"),
        ("text/plain", b"%PDF-fake"),
        ("text/plain", b"x" * (extraction.MAX_TEXT_BYTES + 1)),
        ("image/png", b"\x89PNG\r\n\x1a\ninvalid"),
    ]:
        with pytest.raises(ApiError) as exc:
            extraction.extract_bytes(mime, data)
        assert exc.value.status == 422
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buf, format="PNG")
    assert extraction.extract_bytes("image/png", buf.getvalue()) == [None]


def test_pdf_page_limit():
    pdf = PdfWriter()
    for _ in range(201):
        pdf.add_blank_page(width=20, height=20)
    buf = io.BytesIO()
    pdf.write(buf)
    with pytest.raises(ApiError) as exc:
        extraction.extract_bytes("application/pdf", buf.getvalue())
    assert exc.value.status == 422


def test_timeout_kills_child_and_releases_slot(monkeypatch):
    processes = []
    original = extraction.subprocess.Popen

    def record(*args, **kwargs):
        proc = original(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(extraction.subprocess, "Popen", record)
    monkeypatch.setattr(extraction, "TIMEOUT_SECONDS", 0.001)
    with pytest.raises(ApiError) as exc:
        extraction.extract_bytes("text/plain", b"hello")
    assert exc.value.status == 422
    assert processes[0].poll() is not None
    assert extraction._SLOTS.acquire(blocking=False)
    extraction._SLOTS.release()


def test_concurrency_budget_fails_closed():
    assert extraction._SLOTS.acquire(blocking=False)
    assert extraction._SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(ApiError) as exc:
            extraction.extract_bytes("text/plain", b"hello")
        assert exc.value.status == 503
    finally:
        extraction._SLOTS.release()
        extraction._SLOTS.release()
