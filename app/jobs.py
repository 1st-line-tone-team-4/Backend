"""Durable job state machine with conservative external-call crash recovery.

Dispatch is durably marked before any provider call. Losing a worker after that
point yields outcome_unknown; only an explicit, cost-acknowledged restart may
call again. Leases and fencing protect both job and resource writes. There is no
public cancellation endpoint in the contract. Internal cancel terminalizes a
queued/pre-dispatch job as failed and a dispatched job as outcome_unknown.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .core import ApiError, get_context, get_user, idempotent, iso, uid, utcnow
from .credentials import check_key
from .models import Job, Material, QuestionSet, LearningExplanation, IdempotencyRequest
from .providers import ProviderFailure

router = APIRouter()
TERMINAL = ("succeeded", "failed", "outcome_unknown")
SAFE_FAILURES = {"CREDENTIAL_EXPIRED", "PROVIDER_AUTH", "PROVIDER_RATE_LIMIT", "PROVIDER_UNAVAILABLE"}


def job_dto(job):
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "resource_type": job.resource_type,
        "resource_id": job.resource_id,
        "attempt_no": job.attempt_no,
        "retryable": job.retryable,
        "error_code": job.error_code,
        "result_url": job.result_url,
        "created_at": iso(job.created_at),
        "updated_at": iso(job.updated_at),
        "previous_job_id": job.previous_job_id,
    }


def accepted(job):
    return {"data": {"job": job_dto(job)}}


class JobService:
    def __init__(self, ctx, *, lease_seconds=120):
        if lease_seconds < 1:
            raise ValueError("Lease must be positive")
        self.ctx = ctx
        self.handlers = {}
        self.lease_seconds = lease_seconds

    def register(self, kind, handler):
        if kind not in ("analysis", "questions", "learning_explanation", "grade", "message", "report"):
            raise ValueError("Unknown contract job kind")
        if kind in ("grade", "message", "report") and not callable(getattr(handler, "lock_resource", None)):
            raise ValueError(
                "A domain handlers must implement lock_resource(session, job, require_failed=bool)"
            )
        self.handlers[kind] = handler

    def enqueue(self, session, *, user_id, kind, resource_type, resource_id, payload, model_id, ai_key):
        self.ctx.providers.ensure_model(model_id)
        check_key(ai_key)
        if kind not in self.handlers:
            raise ApiError(503, "JOB_HANDLER_UNAVAILABLE", "The requested job handler is unavailable.")
        serialized = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        if ai_key in serialized:
            raise ApiError(422, "INVALID_REQUEST", "Credentials cannot be included in persisted input.")
        fingerprint_data = {"kind": kind, "payload": payload}
        if kind != "questions":
            fingerprint_data["model_id"] = model_id
        if kind == "analysis":
            fingerprint_data["resource_id"] = resource_id
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        active = session.scalar(
            select(Job.id).where(
                Job.user_id == user_id,
                Job.request_fingerprint == fingerprint,
                Job.status.in_(("queued", "running")),
            )
        )
        if active:
            raise ApiError(409, "JOB_IN_PROGRESS", "An equivalent job is already active.")
        now = utcnow()
        job = Job(
            id=uid(),
            user_id=user_id,
            kind=kind,
            status="queued",
            resource_type=resource_type,
            resource_id=resource_id,
            request_fingerprint=fingerprint,
            request_payload=payload,
            model_id=model_id,
            attempt_no=1,
            retryable=False,
            fencing_token=0,
            created_at=now,
            updated_at=now,
            credential_ref=self.ctx.credentials.put(ai_key),
        )
        try:
            # Nested transaction turns active-fingerprint races into a stable 409
            # without poisoning the caller's idempotency transaction.
            with session.begin_nested():
                session.add(job)
                session.flush()
        except IntegrityError:
            self.ctx.credentials.delete(job.credential_ref)
            raise ApiError(409, "JOB_IN_PROGRESS", "An equivalent job is already active.") from None
        return job

    def _resource(self, session, job, *, require_failed=False):
        mapping = {
            "material": (Material, "latest_job_id", "processing_status"),
            "question_set": (QuestionSet, "job_id", "status"),
            "learning_explanation": (LearningExplanation, "latest_job_id", "status"),
        }
        spec = mapping.get(job.resource_type)
        if not spec:
            # The shared engine has no A tables or business rules. An explicitly
            # registered A adapter owns owner/latest/status checks and row locks.
            lock_resource = getattr(self.handlers[job.kind], "lock_resource", None)
            if callable(lock_resource):
                return lock_resource(session, job, require_failed=require_failed)
            raise ApiError(409, "STALE_JOB", "The resource cannot be resumed by this service.")
        cls, pointer, state = spec
        resource = session.scalar(select(cls).where(cls.id == job.resource_id).with_for_update())
        valid = (
            resource is not None and resource.user_id == job.user_id and getattr(resource, pointer) == job.id
        )
        if require_failed:
            valid = valid and not getattr(resource, "archived", False)
        if require_failed and valid:
            valid = getattr(resource, state) in ("failed", "outcome_unknown")
        if not valid or not self.handlers[job.kind].is_current(session, job):
            raise ApiError(409, "STALE_JOB", "The job no longer owns the current resource state.")
        return resource, pointer

    def _guard(self, session, job_id, token):
        now = utcnow()
        result = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.fencing_token == token,
                Job.lease_expires_at > now,
            )
            .values(fencing_token=token)
        )
        if result.rowcount != 1:
            return None
        return session.get(Job, job_id, populate_existing=True)

    def _secret_cleanup(self, job):
        self.ctx.credentials.delete(job.credential_ref)
        job.credential_ref = None

    def recover(self):
        """Recover expired leases without ever redispatching an uncertain call."""
        self.ctx.credentials.purge()
        count = 0
        with self.ctx.db.transaction() as session:
            queue_deadline = utcnow() - timedelta(minutes=10)
            for candidate in session.scalars(
                select(Job)
                .where(
                    Job.status == "queued",
                    Job.kind.in_(tuple(self.handlers)),
                    Job.updated_at <= queue_deadline,
                )
                .limit(100)
            ).all():
                result = session.execute(
                    update(Job)
                    .where(Job.id == candidate.id, Job.status == "queued", Job.updated_at <= queue_deadline)
                    .values(
                        status="failed",
                        retryable=Job.attempt_no < 3,
                        error_code="PROVIDER_UNAVAILABLE",
                        fencing_token=Job.fencing_token + 1,
                        updated_at=utcnow(),
                    )
                )
                if result.rowcount != 1:
                    continue
                job = session.get(Job, candidate.id, populate_existing=True)
                if job.kind in self.handlers and self.handlers[job.kind].is_current(session, job):
                    self.handlers[job.kind].set_state(session, job, "failed")
                self._secret_cleanup(job)
                count += 1
            candidates = session.scalars(
                select(Job)
                .where(
                    Job.status == "running",
                    Job.kind.in_(tuple(self.handlers)),
                    Job.lease_expires_at <= utcnow(),
                )
                .limit(100)
            ).all()
            for candidate in candidates:
                result = session.execute(
                    update(Job)
                    .where(
                        Job.id == candidate.id,
                        Job.status == "running",
                        Job.fencing_token == candidate.fencing_token,
                        Job.lease_expires_at <= utcnow(),
                    )
                    .values(fencing_token=Job.fencing_token + 1)
                )
                if result.rowcount != 1:
                    continue
                job = session.get(Job, candidate.id, populate_existing=True)
                job.updated_at, job.lease_expires_at = utcnow(), None
                if job.dispatched_at is None:
                    job.status = "queued"
                    if job.kind in self.handlers and self.handlers[job.kind].is_current(session, job):
                        self.handlers[job.kind].set_state(session, job, "queued")
                else:
                    job.status, job.error_code, job.retryable = "outcome_unknown", "TIMEOUT_UNKNOWN", False
                    if job.kind in self.handlers and self.handlers[job.kind].is_current(session, job):
                        self.handlers[job.kind].set_state(session, job, "outcome_unknown")
                    self._secret_cleanup(job)
                count += 1
            # Also cleans credentials after a terminal transaction/process crash.
            for job in session.scalars(
                select(Job).where(Job.status.in_(TERMINAL), Job.credential_ref.is_not(None))
            ):
                self._secret_cleanup(job)
        return count

    def _claim(self):
        with self.ctx.db.transaction() as session:
            job_id = session.scalar(
                select(Job.id)
                .where(
                    Job.status == "queued",
                    Job.kind.in_(tuple(self.handlers)),
                    Job.updated_at > utcnow() - timedelta(minutes=10),
                )
                .order_by(Job.created_at, Job.id)
                .limit(1)
            )
            if not job_id:
                return None
            result = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(
                    status="running",
                    fencing_token=Job.fencing_token + 1,
                    lease_expires_at=utcnow() + timedelta(seconds=self.lease_seconds),
                    updated_at=utcnow(),
                )
            )
            if result.rowcount != 1:
                return None
            job = session.get(Job, job_id)
            return job.id, job.fencing_token

    def _finish_failure(self, job_id, token, failure):
        with self.ctx.db.transaction() as session:
            job = self._guard(session, job_id, token)
            if not job:
                return
            job.status = "outcome_unknown" if failure.outcome_unknown else "failed"
            job.error_code = failure.code
            job.retryable = bool(
                failure.retryable
                and failure.code in SAFE_FAILURES
                and job.attempt_no < 3
                and not failure.outcome_unknown
            )
            job.updated_at, job.lease_expires_at, job.result_url = utcnow(), None, None
            if self.handlers[job.kind].is_current(session, job):
                self.handlers[job.kind].set_state(session, job, job.status)
            self._secret_cleanup(job)

    def run_once(self):
        self.recover()
        claimed = self._claim()
        if not claimed:
            return False
        job_id, token = claimed
        key = None
        dispatched = False
        try:
            with self.ctx.db.transaction() as session:
                job = self._guard(session, job_id, token)
                if not job:
                    return True
                self._resource(session, job)
                key = self.ctx.credentials.get(job.credential_ref)
                if key is None:
                    raise ProviderFailure("CREDENTIAL_EXPIRED", retryable=True)
                self.ctx.providers.ensure_model(job.model_id)
                self.handlers[job.kind].set_state(session, job, "running")
                payload = self.handlers[job.kind].prepare(session, job)
                job.dispatched_at = utcnow()
                model_id, kind = job.model_id, job.kind
                # This transaction commits before the external call begins.
            dispatched = True
            output = self.ctx.providers.generate(model_id, key, kind, payload)
            if not isinstance(output, dict) or key in json.dumps(output, ensure_ascii=False):
                raise ProviderFailure("INVALID_OUTPUT")
            with self.ctx.db.transaction() as session:
                job = self._guard(session, job_id, token)
                if not job:
                    return True
                self._resource(session, job)
                try:
                    url = self.handlers[job.kind].persist(session, job, output)
                except (ApiError, ValueError, TypeError, KeyError, AssertionError):
                    raise ProviderFailure("INVALID_OUTPUT") from None
                if not isinstance(url, str) or not url.startswith("/v1/") or len(url) > 512 or "://" in url:
                    raise ProviderFailure("INVALID_OUTPUT")
                job.status, job.result_url, job.error_code, job.retryable = "succeeded", url, None, False
                job.updated_at, job.lease_expires_at = utcnow(), None
                self.handlers[job.kind].set_state(session, job, "succeeded")
                self._secret_cleanup(job)
        except ProviderFailure as exc:
            self._finish_failure(job_id, token, exc)
        except ApiError:
            self._finish_failure(job_id, token, ProviderFailure("INTERNAL_ERROR", outcome_unknown=dispatched))
        except Exception:
            # Never log exception text: a provider library can include credentials.
            self._finish_failure(job_id, token, ProviderFailure("INTERNAL_ERROR", outcome_unknown=dispatched))
        finally:
            key = None
        return True

    def _locked_owned(self, session, job_id, user_id):
        # Conditional UPDATE also obtains a write lock on SQLite, whose SELECT
        # FOR UPDATE is a no-op. All state changes share this lock ordering.
        result = session.execute(
            update(Job)
            .where(Job.id == job_id, Job.user_id == user_id)
            .values(fencing_token=Job.fencing_token)
        )
        if result.rowcount != 1:
            raise ApiError(404, "NOT_FOUND", "Job not found.")
        return session.get(Job, job_id, populate_existing=True)

    def retry(self, session, *, job_id, user_id, model_id, ai_key):
        job = self._locked_owned(session, job_id, user_id)
        if job.status != "failed" or not job.retryable or job.attempt_no >= 3:
            raise ApiError(409, "JOB_NOT_RETRYABLE", "The job cannot be retried.")
        if model_id != job.model_id:
            raise ApiError(409, "MODEL_MISMATCH", "Retry must use the original model.")
        self.ctx.providers.ensure_model(model_id)
        self._resource(session, job, require_failed=True)
        ref = self.ctx.credentials.put(ai_key)
        self._secret_cleanup(job)
        job.credential_ref = ref
        job.status, job.retryable, job.error_code = "queued", False, None
        job.attempt_no += 1
        job.fencing_token += 1
        job.dispatched_at, job.lease_expires_at, job.result_url = None, None, None
        job.updated_at, job.last_polled_at = utcnow(), None
        self.handlers[job.kind].set_state(session, job, "queued")
        session.flush()
        return job

    def restart(self, session, *, job_id, user_id, model_id, ai_key, acknowledge_possible_duplicate_cost):
        if acknowledge_possible_duplicate_cost is not True:
            raise ApiError(
                422,
                "COST_ACKNOWLEDGEMENT_REQUIRED",
                "Explicit possible duplicate cost acknowledgement is required.",
            )
        old = self._locked_owned(session, job_id, user_id)
        eligible = old.status == "outcome_unknown" or (
            old.status == "failed" and (not old.retryable or old.attempt_no >= 3)
        )
        if not eligible:
            raise ApiError(409, "JOB_NOT_RESTARTABLE", "The job cannot be restarted.")
        if model_id != old.model_id:
            raise ApiError(409, "MODEL_MISMATCH", "Restart must use the original model.")
        resource, pointer = self._resource(session, old, require_failed=True)
        if session.scalar(select(Job.id).where(Job.previous_job_id == old.id)):
            raise ApiError(409, "STALE_JOB", "This job already has a successor.")
        job = self.enqueue(
            session,
            user_id=user_id,
            kind=old.kind,
            resource_type=old.resource_type,
            resource_id=old.resource_id,
            payload=old.request_payload,
            model_id=model_id,
            ai_key=ai_key,
        )
        job.previous_job_id = old.id
        setattr(resource, pointer, job.id)
        session.flush()
        self.handlers[job.kind].set_state(session, job, "queued")
        return job

    def cancel(self, *, job_id, user_id):
        """Internal administrative/resource cleanup; never exposed as a route.

        Cannot recall a paid call. A concurrent worker is fenced before return.
        """
        with self.ctx.db.transaction() as session:
            job = self._locked_owned(session, job_id, user_id)
            if job.status in TERMINAL:
                return job_dto(job)
            job.status = "outcome_unknown" if job.dispatched_at else "failed"
            job.retryable, job.error_code, job.result_url = False, "INTERNAL_ERROR", None
            job.fencing_token += 1
            job.updated_at, job.lease_expires_at = utcnow(), None
            if self.handlers[job.kind].is_current(session, job):
                self.handlers[job.kind].set_state(session, job, job.status)
            self._secret_cleanup(job)
            return job_dto(job)


class ModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model_id: str = Field(min_length=1, max_length=100)


class RestartBody(ModelBody):
    acknowledge_possible_duplicate_cost: StrictBool

    @field_validator("acknowledge_possible_duplicate_cost")
    @classmethod
    def require_acknowledgement(cls, value):
        if value is not True:
            raise ValueError("Explicit acknowledgement is required")
        return value


def request_data(request, body):
    return {
        "method": request.method,
        "path": request.url.path,
        "query": str(request.url.query),
        "body": body.model_dump(),
    }


@router.get("/ai/models")
def list_models(request: Request):
    get_user(request)
    return {"data": get_context(request).providers.list_models()}


@router.post("/ai/validate-key")
def validate_key(body: ModelBody, request: Request):
    ctx, owner = get_context(request), get_user(request)
    key = check_key(request.headers.get("X-AI-Key"))

    def action(session):
        return 200, {"data": ctx.providers.validate_key(body.model_id, key)}

    return idempotent(
        ctx,
        owner,
        "validateAIKey",
        request.headers.get("Idempotency-Key"),
        request_data(request, body),
        action,
    )


@router.get("/jobs/{job_id}")
def get_job(job_id: UUID, request: Request):
    ctx, owner = get_context(request), get_user(request)
    with ctx.db.transaction() as session:
        job = ctx.jobs._locked_owned(session, str(job_id), owner)
        now = utcnow()
        if job.last_polled_at and now - job.last_polled_at < timedelta(seconds=2):
            # Returned directly so the required Retry-After survives generic errors.
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "2"},
                content={
                    "error": {
                        "code": "POLL_RATE_LIMIT",
                        "message": "Poll jobs no more than once every two seconds.",
                        "request_id": getattr(request.state, "request_id", None) or uid(),
                        "details": [],
                    }
                },
            )
        job.last_polled_at = now
        return {"data": job_dto(job)}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: UUID, body: ModelBody, request: Request):
    ctx, owner = get_context(request), get_user(request)
    key = check_key(request.headers.get("X-AI-Key"))

    def action(session):
        job = ctx.jobs.retry(session, job_id=str(job_id), user_id=owner, model_id=body.model_id, ai_key=key)
        return 202, accepted(job)

    return idempotent(
        ctx, owner, "retryJob", request.headers.get("Idempotency-Key"), request_data(request, body), action
    )


@router.post("/jobs/{job_id}/restart")
def restart_job(job_id: UUID, body: RestartBody, request: Request):
    ctx, owner = get_context(request), get_user(request)
    key = check_key(request.headers.get("X-AI-Key"))
    idem_key = request.headers.get("Idempotency-Key")

    def action(session):
        # A restart requires a fresh key, not one already used for an earlier job
        # mutation. Same restart request still replays through idempotent().
        reused = session.scalar(
            select(IdempotencyRequest).where(
                IdempotencyRequest.user_id == owner,
                IdempotencyRequest.idempotency_key == idem_key,
                IdempotencyRequest.operation_id != "restartTerminalJob",
            )
        )
        if reused:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Restart requires a new idempotency key.")
        job = ctx.jobs.restart(
            session,
            job_id=str(job_id),
            user_id=owner,
            model_id=body.model_id,
            ai_key=key,
            acknowledge_possible_duplicate_cost=body.acknowledge_possible_duplicate_cost,
        )
        return 202, accepted(job)

    return idempotent(ctx, owner, "restartTerminalJob", idem_key, request_data(request, body), action)
