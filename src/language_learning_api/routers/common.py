import base64
import hashlib
import hmac
import json
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException

from language_learning_api.config import get_settings


def _filter_digest(filters: dict | None) -> str:
    canonical = json.dumps(filters or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def encode_cursor(created_at: datetime, item_id: UUID, filters: dict | None = None) -> str:
    payload = json.dumps({"created_at": created_at.isoformat(), "id": str(item_id),
        "filter_hash": _filter_digest(filters)}, separators=(",", ":")).encode()
    signature = hmac.new(get_settings().cursor_secret.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + signature.hex()


def decode_cursor(cursor: str, filters: dict | None = None) -> tuple[datetime, UUID]:
    try:
        encoded, signature_hex = cursor.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        signature = bytes.fromhex(signature_hex)
        expected = hmac.new(get_settings().cursor_secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        data = json.loads(payload)
        if data.get("filter_hash") != _filter_digest(filters):
            raise ValueError("filter")
        return datetime.fromisoformat(data["created_at"]), UUID(data["id"])
    except Exception as exc:
        raise HTTPException(status_code=422, detail="VALIDATION_ERROR") from exc


def request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
