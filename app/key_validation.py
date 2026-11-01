"""Idempotent, non-generating key validation without holding a DB connection.

The short-lived claim is committed before provider I/O. Unlike generation, this
read may be requested again after failure or an expired claim; no HTTP request
is retried automatically. Random claim tokens fence late completion and cleanup
without introducing schema columns or retaining credentials.
"""

from datetime import timedelta
import hashlib
import hmac
import json

from fastapi.responses import JSONResponse
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError, OperationalError

from .core import ApiError, uid, utcnow
from .credentials import check_key
from .models import IdempotencyRequest


OPERATION = "validateAIKey"
# Longer than the provider's bounded validation request, but short enough to
# recover an abandoned claim without waiting for the completed-response TTL.
CLAIM_SECONDS = 120
REPLAY_HOURS = 24


def _canonical(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ApiError(400, "INVALID_REQUEST", "Request cannot be canonicalized.") from None


def _request_digest(ctx, owner, model_id, key, request_data):
    # Do not persist a raw credential, a plain key hash, or the HMAC itself.
    # Owner/model context and a versioned domain separate this use of the
    # signing secret from storage signatures and from other credentials.
    fingerprint = hmac.new(
        ctx.settings.signing_secret.encode("utf-8"),
        b"validateAIKey:credential:v1\0" + _canonical([owner, model_id, key]),
        hashlib.sha256,
    ).hexdigest()
    return hashlib.sha256(
        _canonical({"request": request_data, "credential_fingerprint": fingerprint})
    ).hexdigest()


def _in_progress():
    return ApiError(409, "REQUEST_IN_PROGRESS", "Request is still in progress.")


def _replay(row, digest):
    if row.request_hash != digest:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used for a different request.")
    if row.status != "completed":
        raise _in_progress()
    return JSONResponse(status_code=row.http_status, content=row.response_body)


def _identity(owner, idem_key):
    return (
        IdempotencyRequest.user_id == owner,
        IdempotencyRequest.operation_id == OPERATION,
        IdempotencyRequest.idempotency_key == idem_key,
    )


def _claim(ctx, owner, idem_key, digest):
    identity = (owner, OPERATION, idem_key)
    for attempt in range(3):
        claiming = True
        try:
            with ctx.db.transaction() as session:
                now = utcnow()
                # Match generic idempotency's expired-row serialization. A
                # missing-row InnoDB gap-lock deadlock is safe to retry only
                # while claiming, before any commit or provider request.
                session.execute(
                    delete(IdempotencyRequest).where(
                        *_identity(owner, idem_key), IdempotencyRequest.expires_at <= now
                    )
                )
                row = session.get(IdempotencyRequest, identity)
                if row is not None:
                    claiming = False
                    return _replay(row, digest)
                token = uid()
                session.add(
                    IdempotencyRequest(
                        user_id=owner,
                        operation_id=OPERATION,
                        idempotency_key=idem_key,
                        request_hash=digest,
                        status="in_progress",
                        response_body={"claim_token": token},
                        created_at=now,
                        expires_at=now + timedelta(seconds=CLAIM_SECONDS),
                    )
                )
                session.flush()
                claiming = False
            return token
        except OperationalError as exc:
            if (
                not claiming
                or ctx.db.engine.dialect.name != "mysql"
                or getattr(exc.orig, "args", ())[:1] != (1213,)
            ):
                raise
            if attempt == 2:
                raise _in_progress() from None
        except IntegrityError:
            if not claiming:
                raise
            with ctx.db.transaction() as session:
                row = session.get(IdempotencyRequest, identity)
                if row is not None:
                    return _replay(row, digest)
            raise


def _locked_claim(session, owner, idem_key, digest, token, *, require_unexpired):
    conditions = [
        *_identity(owner, idem_key),
        IdempotencyRequest.request_hash == digest,
        IdempotencyRequest.status == "in_progress",
    ]
    if require_unexpired:
        conditions.append(IdempotencyRequest.expires_at > utcnow())
    # UPDATE locks on SQLite as well as MySQL. Inspect the token only after
    # obtaining this lock, so an expired predecessor cannot affect its successor.
    result = session.execute(update(IdempotencyRequest).where(*conditions).values(status="in_progress"))
    if result.rowcount != 1:
        return None
    row = session.get(IdempotencyRequest, (owner, OPERATION, idem_key), populate_existing=True)
    return row if row.response_body == {"claim_token": token} else None


def _release(ctx, owner, idem_key, digest, token):
    with ctx.db.transaction() as session:
        row = _locked_claim(session, owner, idem_key, digest, token, require_unexpired=False)
        if row is not None:
            session.delete(row)


def _complete(ctx, owner, idem_key, digest, token, body):
    with ctx.db.transaction() as session:
        row = _locked_claim(session, owner, idem_key, digest, token, require_unexpired=True)
        if row is None:
            raise _in_progress()
        row.status, row.http_status, row.response_body = "completed", 200, body
        row.expires_at = utcnow() + timedelta(hours=REPLAY_HOURS)
        session.flush()
    return JSONResponse(status_code=200, content=body)


def validate_key_request(ctx, owner, model_id, key, idem_key, request_data):
    """Validate once per live claim; identical completed requests replay for 24h."""
    check_key(key)
    ctx.providers.ensure_model(model_id)
    if not isinstance(idem_key, str) or not 16 <= len(idem_key) <= 128:
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency key must contain 16 to 128 characters.")
    digest = _request_digest(ctx, owner, model_id, key, request_data)
    claim = _claim(ctx, owner, idem_key, digest)
    if isinstance(claim, JSONResponse):
        return claim

    # _claim has committed and closed its session before this external GET.
    try:
        result = ctx.providers.validate_key(model_id, key)
        if (
            not isinstance(result, dict)
            or type(result.get("valid")) is not bool
            or result.get("model_id") != model_id
        ):
            raise ValueError("Invalid validation result")
        # Persist only the public DTO, never provider bodies or exception text.
        body = {"data": {"valid": result["valid"], "model_id": model_id}}
    except Exception as exc:
        if isinstance(exc, ApiError) and (exc.status, exc.code) == (422, "PROVIDER_AUTH"):
            failure = ApiError(422, "PROVIDER_AUTH", "The provider rejected the credential.")
        else:
            failure = ApiError(
                503, "KEY_VALIDATION_UNAVAILABLE", "Non-generating key validation is unavailable."
            )
        try:
            _release(ctx, owner, idem_key, digest, claim)
        except Exception:
            # Cleanup failure never causes another HTTP attempt. The bounded
            # lease permits a later explicit request even after a process crash.
            pass
        raise failure from None

    # Never retry provider I/O after a failed or uncertain completion commit.
    return _complete(ctx, owner, idem_key, digest, claim, body)
