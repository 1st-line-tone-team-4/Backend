"""Compatibility exports for the shared A cursor codecs and body checksum."""

import hashlib
import json

from app.pagination import (
    decode_cursor as decode_cursor,
    decode_opaque_cursor as decode_opaque_cursor,
    encode_cursor as encode_cursor,
    encode_opaque_cursor as encode_opaque_cursor,
)


def request_hash(payload):
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
