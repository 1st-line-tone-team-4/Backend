"""Owner/filter-bound cursors shared by the integrated A routes."""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from uuid import UUID
from app.core import ApiError
from app.providers import strict_json


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()


def encode_opaque_cursor(payload, filters, *, secret):
    if len(secret.encode()) < 32:
        raise ValueError("Cursor signing secret must contain at least 32 bytes")
    data = _canonical({"payload": payload, "filter_hash": hashlib.sha256(_canonical(filters)).hexdigest()})
    signature = hmac.new(secret.encode(), b"a-cursor:v1\0" + data, hashlib.sha256).hexdigest()
    cursor = base64.urlsafe_b64encode(data).decode().rstrip("=") + "." + signature
    if len(cursor) > 4096:
        raise ValueError("Cursor payload is too large")
    return cursor


def decode_opaque_cursor(cursor, filters, *, secret):
    try:
        if not isinstance(cursor, str) or len(cursor) > 4096 or len(secret.encode()) < 32:
            raise ValueError()
        encoded, signature = cursor.split(".")
        data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        expected = hmac.new(secret.encode(), b"a-cursor:v1\0" + data, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        record = strict_json(data)
        if not isinstance(record, dict) or set(record) != {"payload", "filter_hash"}:
            raise ValueError()
        if record["filter_hash"] != hashlib.sha256(_canonical(filters)).hexdigest():
            raise ValueError()
        return record["payload"]
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ApiError(422, "INVALID_CURSOR", "Cursor is invalid for this request.") from None


def encode_cursor(created_at, item_id, filters=None, *, secret):
    when = (
        created_at.replace(tzinfo=timezone.utc)
        if created_at.tzinfo is None
        else created_at.astimezone(timezone.utc)
    )
    return encode_opaque_cursor({"created_at": when.isoformat(), "id": str(item_id)}, filters, secret=secret)


def decode_cursor(cursor, filters=None, *, secret):
    try:
        data = decode_opaque_cursor(cursor, filters, secret=secret)
        if not isinstance(data, dict) or set(data) != {"created_at", "id"}:
            raise ValueError()
        if not isinstance(data["created_at"], str) or not isinstance(data["id"], str):
            raise ValueError()
        when = datetime.fromisoformat(data["created_at"])
        if when.tzinfo is None:
            raise ValueError()
        return when.astimezone(timezone.utc).replace(tzinfo=None), str(UUID(data["id"]))
    except (ValueError, TypeError, KeyError):
        raise ApiError(422, "INVALID_CURSOR", "Cursor is invalid for this request.") from None


def request_hash(payload):
    return hashlib.sha256(_canonical(payload)).hexdigest()
