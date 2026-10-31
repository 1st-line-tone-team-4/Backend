from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import uuid
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from .models import IdempotencyRequest

def uid(): return str(uuid.uuid4())
def utcnow(): return datetime.now(timezone.utc).replace(tzinfo=None)
def iso(dt): return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if dt else None

@dataclass
class Settings:
    profile: str = "production"
    database_url: str = ""
    storage_root: str = ""
    public_base_url: str = ""
    credential_encryption_key: str = ""
    signing_secret: str = ""
    auth_public_key: str = ""
    auth_issuer: str = ""
    auth_audience: str = ""
    enabled_models: tuple = ()

    @classmethod
    def from_env(cls):
        return cls(**{k: (tuple(filter(None, os.getenv(k.upper(), "").split(","))) if k == "enabled_models" else os.getenv(k.upper(), field.default)) for k, field in cls.__dataclass_fields__.items()})

class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.message = status, code, message
        self.details = details if details is not None else []
        super().__init__(message)

def error_response(request, exc):
    request_id = getattr(request.state, "request_id", None) or uid()
    return JSONResponse(status_code=exc.status, headers={"Retry-After": "2"} if exc.code == "REQUEST_IN_PROGRESS" else None, content={"error": {"code": exc.code, "message": exc.message, "request_id": request_id, "details": exc.details}})

async def api_error_handler(request, exc): return error_response(request, exc)
def get_context(request: Request): return request.app.state.context
def get_user(request: Request): return get_context(request).auth.authenticate(request)

def idempotent(ctx, user_id, operation, key, request_data, action):
    if not isinstance(key, str) or not 16 <= len(key) <= 128:
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency key must contain 16 to 128 characters.")
    try:
        encoded = json.dumps(request_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ApiError(400, "INVALID_REQUEST", "Request cannot be canonicalized.")
    digest = hashlib.sha256(encoded).hexdigest()
    identity = (user_id, operation, key)
    def replay(row):
        if row.request_hash != digest:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used for a different request.")
        if row.status != "completed":
            raise ApiError(409, "REQUEST_IN_PROGRESS", "Request is still in progress.")
        return JSONResponse(status_code=row.http_status, content=row.response_body)
    try:
        with ctx.db.transaction() as session:
            now = utcnow()
            # A conditional delete acquires the key lock before replacing an expired
            # record; a concurrent request cannot delete a newly inserted record.
            session.execute(delete(IdempotencyRequest).where(
                IdempotencyRequest.user_id == user_id,
                IdempotencyRequest.operation_id == operation,
                IdempotencyRequest.idempotency_key == key,
                IdempotencyRequest.expires_at <= now,
            ))
            row = session.get(IdempotencyRequest, identity)
            if row:
                return replay(row)
            row = IdempotencyRequest(user_id=user_id, operation_id=operation, idempotency_key=key, request_hash=digest, status="in_progress", created_at=now, expires_at=now+timedelta(hours=24))
            session.add(row)
            session.flush()
            status, body = action(session)
            # Validate serialization before committing any domain changes.
            json.dumps(body, allow_nan=False)
            row.status, row.http_status, row.response_body = "completed", status, body
            session.flush()
        return JSONResponse(status_code=status, content=body)
    except IntegrityError:
        # A concurrent insert may have won; unrelated integrity errors must propagate.
        with ctx.db.transaction() as session:
            row = session.get(IdempotencyRequest, identity)
            if row is not None:
                return replay(row)
        raise


