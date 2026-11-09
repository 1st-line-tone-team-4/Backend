"""Isolated, bounded source parsing. The child never receives application secrets.

Limits apply per API process. Windows memory monitoring is best effort, not a
hard kernel limit; deploy behind OS/container memory limits for defense in depth.
"""

import io
import json
import os
import subprocess
import sys
import threading
from .core import ApiError

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 120_000
MAX_PAGES = 200
MAX_PIXELS = 40_000_000
MAX_PRIVATE_BYTES = 768 * 1024 * 1024
TIMEOUT_SECONDS = 20
_SLOTS = threading.BoundedSemaphore(2)


def _windows_monitor(process, finished):
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        get = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        get.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        get.restype = wintypes.BOOL
        while not finished.wait(0.1):
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            measured = get(wintypes.HANDLE(int(process._handle)), ctypes.byref(counters), counters.cb)
            if not measured or counters.PrivateUsage > MAX_PRIVATE_BYTES:
                if process.poll() is None:
                    process.kill()
                return
    except Exception:
        # Fail closed if process monitoring cannot be initialized.
        try:
            process.kill()
        except OSError:
            pass


def extract_bytes(mime, data, *, require_text=True):
    """Validate and parse locally. require_text=False accepts a PDF without a
    text layer (scanned) when an external recognizer will supply the text."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_SOURCE_BYTES:
        raise ApiError(422, "INVALID_UPLOAD", "Upload size is invalid.")
    if mime not in ("text/plain", "application/pdf", "image/png", "image/jpeg"):
        raise ApiError(422, "INVALID_UPLOAD", "Upload content is unsupported.")
    if not _SLOTS.acquire(blocking=False):
        raise ApiError(503, "EXTRACTION_BUSY", "File validation is busy; try again.")
    proc = None
    finished = threading.Event()
    try:
        safe_env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TEMP", "TMP", "LANG", "LC_ALL")
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.extraction", "--child", mime]
            + ([] if require_text else ["--allow-empty"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=safe_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        monitor = threading.Thread(target=_windows_monitor, args=(proc, finished), daemon=True)
        monitor.start()
        try:
            output, _ = proc.communicate(data, timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise ApiError(422, "INVALID_UPLOAD", "File validation exceeded its time limit.") from None
        if proc.returncode != 0 or len(output) > MAX_TEXT_BYTES * 6 + 4096:
            raise ApiError(422, "INVALID_UPLOAD", "Upload content is invalid or exceeds processing limits.")
        try:
            result = json.loads(output)
            if not isinstance(result, list) or not 1 <= len(result) <= MAX_PAGES:
                raise ValueError()
            if any(item is not None and not isinstance(item, str) for item in result):
                raise ValueError()
            if sum(len(item.encode("utf-8")) for item in result if item is not None) > MAX_TEXT_BYTES:
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise ApiError(
                422, "INVALID_UPLOAD", "Upload content is invalid or exceeds processing limits."
            ) from None
    finally:
        finished.set()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        _SLOTS.release()


def _parse(mime, data, require_text=True):
    if os.name != "nt":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    if not 0 < len(data) <= MAX_SOURCE_BYTES:
        raise ValueError()
    if mime == "text/plain":
        if b"\x00" in data or data.startswith((b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            raise ValueError()
        pages = [data.decode("utf-8")]
    elif mime == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError()
        import pypdf.filters as filters

        # This child exits after one document; no settings change in the server process.
        for name in (
            "ZLIB_MAX_OUTPUT_LENGTH",
            "LZW_MAX_OUTPUT_LENGTH",
            "RUN_LENGTH_MAX_OUTPUT_LENGTH",
            "JBIG2_MAX_OUTPUT_LENGTH",
        ):
            if hasattr(filters, name):
                setattr(filters, name, min(getattr(filters, name), 8 * 1024 * 1024))
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
            raise ValueError()
        pages = []
        text_bytes = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            text_bytes += len(text.encode("utf-8"))
            if text_bytes > MAX_TEXT_BYTES:
                raise ValueError()
            pages.append(text)
    elif mime in ("image/png", "image/jpeg"):
        if not data.startswith(b"\x89PNG\r\n\x1a\n" if mime == "image/png" else b"\xff\xd8\xff"):
            raise ValueError()
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if (
                image.format != ("PNG" if mime == "image/png" else "JPEG")
                or image.width * image.height > MAX_PIXELS
            ):
                raise ValueError()
            image.verify()
        return [None]
    else:
        raise ValueError()
    if sum(len(x.encode("utf-8")) for x in pages) > MAX_TEXT_BYTES or (
        require_text and not any(x.strip() for x in pages)
    ):
        raise ValueError()
    return pages


def _child():
    try:
        data = sys.stdin.buffer.read(MAX_SOURCE_BYTES + 1)
        pages = _parse(sys.argv[2], data, require_text=sys.argv[3:] != ["--allow-empty"])
        sys.stdout.buffer.write(json.dumps(pages, ensure_ascii=True).encode("ascii"))
    except Exception:
        sys.exit(1)


if __name__ == "__main__" and len(sys.argv) in (3, 4) and sys.argv[1] == "--child":
    _child()
