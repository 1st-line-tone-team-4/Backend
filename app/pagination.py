"""Signed pagination codecs, preserving the three existing cursor wire formats.

Material and content cursors embed their hexadecimal signature inside base64.
A cursors sign a domain-prefixed payload and append the signature outside it.
Their validation, timestamp rules, and error messages intentionally differ.
"""

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
from uuid import UUID

from sqlalchemy import and_, or_

from .core import ApiError, strict_json


def _signature(secret, payload, domain=b""):
    return hmac.new(secret, domain + payload, hashlib.sha256).hexdigest()


def _encode_embedded(value, secret):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    signature = _signature(secret, payload).encode()
    return base64.urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")


def _decode_embedded(cursor, secret):
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    payload, signature = raw.rsplit(b".", 1)
    if not hmac.compare_digest(signature, _signature(secret, payload).encode()):
        raise ValueError()
    return json.loads(payload)


def encode_material_cursor(created_at, item_id, user, include_archived, secret):
    return _encode_embedded(
        {"user": user, "archived": include_archived, "time": created_at.isoformat(), "id": item_id},
        secret,
    )


def decode_material_cursor(cursor, user, include_archived, secret):
    try:
        value = _decode_embedded(cursor, secret)
        if value["user"] != user or value["archived"] != include_archived:
            raise ValueError()
        return datetime.fromisoformat(value["time"]), value["id"]
    except Exception:
        raise ApiError(422, "INVALID_CURSOR", "Invalid cursor.")


def encode_content_cursor(created_at, item_id, secret, scope):
    return _encode_embedded(
        {"timestamp": created_at.isoformat(), "id": item_id, "scope": scope}, secret.encode()
    )


def decode_content_cursor(cursor, secret, scope):
    if not cursor:
        return None
    try:
        value = _decode_embedded(cursor, secret.encode())
        if (
            not isinstance(value, dict)
            or set(value) != {"timestamp", "id", "scope"}
            or value["scope"] != scope
        ):
            raise ValueError()
        stamp = datetime.fromisoformat(value["timestamp"])
        if stamp.tzinfo is not None or str(UUID(value["id"])) != value["id"]:
            raise ValueError()
        return stamp, value["id"]
    except Exception:
        raise ApiError(422, "INVALID_CURSOR", "Invalid cursor.")


def content_page(session, stmt, model, limit, cursor, secret, scope):
    """Apply the content cursor boundary; callers retain their existing order."""
    boundary = decode_content_cursor(cursor, secret, scope)
    if boundary:
        stmt = stmt.where(
            or_(model.created_at < boundary[0], and_(model.created_at == boundary[0], model.id < boundary[1]))
        )
    rows = list(session.scalars(stmt.limit(limit + 1)))
    chosen = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = chosen[-1]
        next_cursor = encode_content_cursor(last.created_at, last.id, secret, scope)
    return chosen, next_cursor


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()


def encode_opaque_cursor(payload, filters, *, secret):
    if len(secret.encode()) < 32:
        raise ValueError("Cursor signing secret must contain at least 32 bytes")
    data = _canonical({"payload": payload, "filter_hash": hashlib.sha256(_canonical(filters)).hexdigest()})
    signature = _signature(secret.encode(), data, b"a-cursor:v1\0")
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
        expected = _signature(secret.encode(), data, b"a-cursor:v1\0")
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
