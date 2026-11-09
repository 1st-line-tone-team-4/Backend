from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import uuid
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError, OperationalError
from .models import IdempotencyRequest


def uid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


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
    # Optional internal AI server (repo AI). Empty keeps local extraction.
    ai_service_url: str = ""
    ai_service_api_key: str = ""

    @classmethod
    def from_env(cls):
        return cls(
            **{
                k: (
                    tuple(filter(None, os.getenv(k.upper(), "").split(",")))
                    if k == "enabled_models"
                    else os.getenv(k.upper(), field.default)
                )
                for k, field in cls.__dataclass_fields__.items()
            }
        )


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.message = status, code, message
        self.details = details if details is not None else []
        super().__init__(message)


def error_response(request, exc):
    request_id = getattr(request.state, "request_id", None) or uid()
    return JSONResponse(
        status_code=exc.status,
        headers={"Retry-After": "2"} if exc.code == "REQUEST_IN_PROGRESS" else None,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
                "details": exc.details,
            }
        },
    )


async def api_error_handler(request, exc):
    return error_response(request, exc)


def get_context(request: Request):
    return request.app.state.context


def get_user(request: Request):
    return get_context(request).auth.authenticate(request)


def request_envelope(request, body, *, query_as_dict=False):
    """Build the existing request identity with caller-owned body serialization.

    Raw query strings preserve duplicates/escaping for materials/jobs/teach-back;
    content deliberately retains its historical last-value dictionary form.
    """
    return {
        "method": request.method,
        "path": request.url.path,
        "query": dict(request.query_params) if query_as_dict else str(request.url.query),
        "body": body,
    }


def strict_json(value):
    """Reject non-JSON constants and duplicate keys rather than repairing output."""

    def reject_constant(_):
        raise ValueError("Non-JSON number")

    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    return json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_object)


def _idempotency_digest(key, request_data):
    if not isinstance(key, str) or not 16 <= len(key) <= 128:
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency key must contain 16 to 128 characters.")
    try:
        encoded = json.dumps(
            request_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ApiError(400, "INVALID_REQUEST", "Request cannot be canonicalized.")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_response(row, digest):
    if row.request_hash != digest:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used for a different request.")
    if row.status != "completed":
        raise ApiError(409, "REQUEST_IN_PROGRESS", "Request is still in progress.")
    return JSONResponse(status_code=row.http_status, content=row.response_body)


def idempotency_replay(ctx, user_id, operation, key, request_data):
    """Read a completed response before expensive, read-only preparation.

    This does not claim the request. The final idempotent transaction must still
    recheck the claim and domain state after preparation has finished.
    """
    digest = _idempotency_digest(key, request_data)
    with ctx.db.transaction() as session:
        row = session.get(IdempotencyRequest, (user_id, operation, key))
        if row is not None and row.expires_at > utcnow():
            return _idempotency_response(row, digest)
    return None


def idempotent(ctx, user_id, operation, key, request_data, action):
    digest = _idempotency_digest(key, request_data)
    identity = (user_id, operation, key)

    for attempt in range(3):
        claiming = True
        try:
            with ctx.db.transaction() as session:
                now = utcnow()
                # Serialize expired-row replacement. For a missing key, InnoDB
                # gap locks can deadlock at insert; only that claim is retryable.
                session.execute(
                    delete(IdempotencyRequest).where(
                        IdempotencyRequest.user_id == user_id,
                        IdempotencyRequest.operation_id == operation,
                        IdempotencyRequest.idempotency_key == key,
                        IdempotencyRequest.expires_at <= now,
                    )
                )
                row = session.get(IdempotencyRequest, identity)
                if row:
                    claiming = False
                    return _idempotency_response(row, digest)
                row = IdempotencyRequest(
                    user_id=user_id,
                    operation_id=operation,
                    idempotency_key=key,
                    request_hash=digest,
                    status="in_progress",
                    created_at=now,
                    expires_at=now + timedelta(hours=24),
                )
                session.add(row)
                session.flush()
                claiming = False
                status, body = action(session)
                # Validate serialization before committing any domain changes.
                json.dumps(body, allow_nan=False)
                row.status, row.http_status, row.response_body = "completed", status, body
                session.flush()
            return JSONResponse(status_code=status, content=body)
        except OperationalError as exc:
            if (
                not claiming
                or ctx.db.engine.dialect.name != "mysql"
                or getattr(exc.orig, "args", ())[:1] != (1213,)
            ):
                raise
            # The transaction context has rolled back. Never repeat a callback
            # or retry an uncertain commit, timeout, or connection failure.
            if attempt == 2:
                raise ApiError(409, "REQUEST_IN_PROGRESS", "Request is still in progress.") from None
        except IntegrityError:
            if not claiming:
                raise
            # A concurrent claim may have won; unrelated errors must propagate.
            with ctx.db.transaction() as session:
                row = session.get(IdempotencyRequest, identity)
                if row is not None:
                    return _idempotency_response(row, digest)
            raise
