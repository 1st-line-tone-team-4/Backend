"""Synthetic credentials and explicit HTTP mocks for key-validation boundaries."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from threading import Barrier, Event, Lock
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import httpx
from pymysql.err import OperationalError as MySQLError
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.core import ApiError, Settings, api_error_handler, uid, utcnow
from app.db import Database
from app.key_validation import CLAIM_SECONDS, OPERATION, validate_key_request
from app.models import IdempotencyRequest, User
from app.providers import ProviderRegistry
from tests.test_mysql_boundaries import FaultDatabase


KEY = "synthetic-validation-key-not-real"
OTHER_KEY = "different-synthetic-key-not-real"
MODEL = "openai:validation-fixture"


def make_provider(respond):
    return ProviderRegistry(
        Settings(profile="test", enabled_models=(MODEL,)), transport=httpx.MockTransport(respond)
    )


@pytest.fixture
def context(tmp_path):
    db = Database("sqlite:///" + (tmp_path / "validation.sqlite3").as_posix())
    db.create_schema()
    owner = uid()
    with db.transaction() as session:
        session.add(User(id=owner, auth_issuer="fixture", auth_subject=owner, created_at=utcnow()))
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        assert db.engine.pool.checkedout() == 0
        return httpx.Response(200, json={"id": "validation-fixture"})

    ctx = SimpleNamespace(
        db=db,
        owner=owner,
        settings=Settings(signing_secret="synthetic-signing-secret-" * 3),
        providers=make_provider(respond),
        calls=calls,
    )
    try:
        yield ctx
    finally:
        db.engine.dispose()


def invoke(ctx, *, key=KEY, idem="validation-idempotency-key", model=MODEL, request_data=None):
    return validate_key_request(
        ctx,
        ctx.owner,
        model,
        key,
        idem,
        request_data
        if request_data is not None
        else {"method": "POST", "path": "/v1/ai/validate-key", "query": "", "body": {"model_id": model}},
    )


def claim_row(ctx, idem="validation-idempotency-key"):
    with ctx.db.transaction() as session:
        return session.get(IdempotencyRequest, (ctx.owner, OPERATION, idem))


def expire(ctx, idem="validation-idempotency-key"):
    with ctx.db.transaction() as session:
        row = session.get(IdempotencyRequest, (ctx.owner, OPERATION, idem))
        row.expires_at = utcnow() - timedelta(seconds=1)


def test_same_key_replays_different_key_conflicts_without_provider_call(context):
    first = invoke(context)
    assert json.loads(first.body) == {"data": {"valid": True, "model_id": MODEL}}
    replay = invoke(context)
    assert (replay.status_code, replay.body) == (200, first.body)
    with pytest.raises(ApiError) as conflict:
        invoke(context, key=OTHER_KEY)
    assert (conflict.value.status, conflict.value.code) == (409, "IDEMPOTENCY_CONFLICT")
    assert context.calls == [("GET", "/v1/models/validation-fixture")]
    row = claim_row(context)
    assert row.status == "completed"
    assert timedelta(hours=23) < row.expires_at - utcnow() <= timedelta(hours=24)
    persisted = repr(row.__dict__)
    assert KEY not in persisted and OTHER_KEY not in persisted
    assert hashlib.sha256(KEY.encode()).hexdigest() not in persisted
    assert "credential_fingerprint" not in persisted and "claim_token" not in persisted


def test_fingerprint_depends_on_server_secret_owner_and_model(context):
    invoke(context)
    original = claim_row(context).request_hash
    context.settings.signing_secret = "different-synthetic-server-secret-" * 2
    invoke(context, idem="other-idempotency-key")
    assert claim_row(context, "other-idempotency-key").request_hash != original
    # Keep the public request data identical to prove explicit credential scope,
    # rather than merely hashing model/owner fields present in the request DTO.
    data = {"fixed": "request"}
    invoke(context, idem="scope-idempotency-key", request_data=data)
    scoped = claim_row(context, "scope-idempotency-key").request_hash
    other_owner = uid()
    with context.db.transaction() as session:
        session.add(
            User(id=other_owner, auth_issuer="fixture", auth_subject=other_owner, created_at=utcnow())
        )
    other = SimpleNamespace(**{**vars(context), "owner": other_owner})
    invoke(other, idem="scope-idempotency-key", request_data=data)
    assert claim_row(other, "scope-idempotency-key").request_hash != scoped
    other_model = "openai:another-fixture"
    context.providers.models = (MODEL, other_model)
    invoke(context, idem="model-idempotency-key", model=other_model, request_data=data)
    assert claim_row(context, "model-idempotency-key").request_hash != scoped


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"key": ""}, "PROVIDER_AUTH"),
        ({"key": "contains whitespace"}, "PROVIDER_AUTH"),
        ({"model": "openai:disabled"}, "MODEL_UNAVAILABLE"),
        ({"idem": "short"}, "INVALID_IDEMPOTENCY_KEY"),
        ({"request_data": {"number": float("nan")}}, "INVALID_REQUEST"),
    ],
)
def test_invalid_input_creates_no_claim_and_makes_no_http_request(context, kwargs, code):
    with pytest.raises(ApiError) as failure:
        invoke(context, **kwargs)
    assert failure.value.code == code
    assert context.calls == []
    with context.db.transaction() as session:
        assert session.scalar(select(IdempotencyRequest)) is None


@pytest.mark.parametrize(
    "status,code",
    [(401, "PROVIDER_AUTH"), (429, "KEY_VALIDATION_UNAVAILABLE"), (503, "KEY_VALIDATION_UNAVAILABLE")],
)
def test_provider_failure_is_sanitized_releases_claim_and_never_retries(context, status, code):
    calls = []

    def respond(request):
        calls.append(request.method)
        return httpx.Response(status, text=KEY + " provider-private-body")

    context.providers = make_provider(respond)
    with pytest.raises(ApiError) as failure:
        invoke(context)
    assert failure.value.code == code
    assert KEY not in repr(failure.value.__dict__)
    assert calls == ["GET"] and claim_row(context) is None
    context.providers = make_provider(lambda request: httpx.Response(200, json={"id": "fixture"}))
    assert invoke(context).status_code == 200


def test_timeout_releases_claim_for_explicit_retry_without_generation(context):
    calls = []

    def timeout(request):
        calls.append(request.method)
        raise httpx.ReadTimeout(KEY + " private timeout body", request=request)

    context.providers = make_provider(timeout)
    with pytest.raises(ApiError) as failure:
        invoke(context)
    assert (failure.value.status, failure.value.code) == (503, "KEY_VALIDATION_UNAVAILABLE")
    assert KEY not in str(failure.value)
    assert calls == ["GET"] and claim_row(context) is None


def assert_key_validation_claim_race(base_context, owner, *, expired=False):
    """Reusable SQLite/MySQL check: concurrent claims and I/O without DB locks."""
    ctx = SimpleNamespace(db=base_context.db, settings=base_context.settings, owner=owner)
    idem = uid()
    if expired:
        ctx.providers = make_provider(lambda request: httpx.Response(200, json={"id": "fixture"}))
        invoke(ctx, idem=idem)
        expire(ctx, idem)
    started, release = Event(), Event()
    barrier, lock = Barrier(2), Lock()
    calls = []

    def slow_get(request):
        with lock:
            calls.append(request.method)
        started.set()
        assert release.wait(8)
        return httpx.Response(200, json={"id": "fixture"})

    ctx.providers = make_provider(slow_get)

    def request_once():
        barrier.wait(timeout=5)
        try:
            return invoke(ctx, idem=idem)
        except ApiError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request_once) for _ in range(2)]
        try:
            assert started.wait(5)
            # A concurrent request completes while the winner's HTTP is blocked.
            loser = next(as_completed(futures, timeout=5)).result()
            assert isinstance(loser, ApiError) and loser.code == "REQUEST_IN_PROGRESS"
            assert ctx.db.engine.pool.checkedout() == 0
            row = claim_row(ctx, idem)
            assert row.status == "in_progress" and set(row.response_body) == {"claim_token"}
            assert timedelta(0) < row.expires_at - utcnow() <= timedelta(seconds=CLAIM_SECONDS)
            # This write would block if the HTTP request retained its claim lock.
            with ctx.db.transaction() as session:
                session.execute(
                    update(IdempotencyRequest)
                    .where(
                        IdempotencyRequest.user_id == owner,
                        IdempotencyRequest.operation_id == OPERATION,
                        IdempotencyRequest.idempotency_key == idem,
                    )
                    .values(status=IdempotencyRequest.status)
                )
            with pytest.raises(ApiError) as conflict:
                invoke(ctx, key=OTHER_KEY, idem=idem)
            assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert sorted(
        result.status if isinstance(result, ApiError) else result.status_code for result in results
    ) == [200, 409]
    assert calls == ["GET"]
    assert invoke(ctx, idem=idem).status_code == 200 and calls == ["GET"]


@pytest.mark.parametrize("expired", [False, True])
def test_concurrent_claim_calls_provider_once_with_no_checked_out_connection(context, expired):
    assert_key_validation_claim_race(context, context.owner, expired=expired)


@pytest.mark.parametrize("late_failure", [False, True])
def test_expired_claim_completion_and_release_cannot_touch_successor(context, late_failure):
    started, release = Event(), Event()
    calls = []

    def respond(request):
        calls.append(request.method)
        if len(calls) == 1:
            started.set()
            assert release.wait(8)
            if late_failure:
                return httpx.Response(503)
        return httpx.Response(200, json={"id": "fixture"})

    context.providers = make_provider(respond)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(invoke, context)
        try:
            assert started.wait(5)
            expire(context)
            successor = invoke(context)
            assert successor.status_code == 200
        finally:
            release.set()
        with pytest.raises(ApiError) as failure:
            old.result(timeout=5)
    assert failure.value.code == ("KEY_VALIDATION_UNAVAILABLE" if late_failure else "REQUEST_IN_PROGRESS")
    assert claim_row(context).status == "completed"
    assert invoke(context).body == successor.body and calls == ["GET", "GET"]


def test_crashed_validation_claim_has_bounded_recovery(context):
    class SyntheticCrash(BaseException):
        pass

    def crash(request):
        raise SyntheticCrash()

    context.providers = make_provider(crash)
    with pytest.raises(SyntheticCrash):
        invoke(context)
    row = claim_row(context)
    assert row.status == "in_progress"
    assert row.expires_at - row.created_at == timedelta(seconds=CLAIM_SECONDS)
    with pytest.raises(ApiError) as pending:
        invoke(context)
    assert pending.value.code == "REQUEST_IN_PROGRESS"
    expire(context)
    context.providers = make_provider(lambda request: httpx.Response(200))
    assert invoke(context, key=OTHER_KEY).status_code == 200


def test_known_mysql_claim_deadlocks_retry_only_before_http(context):
    actual = context.db
    fault = FaultDatabase(actual, failures=2)
    context.db = fault
    assert invoke(context).status_code == 200
    assert len(context.calls) == 1 and len(fault.sessions) == 4


class TransactionFault:
    def __init__(self, db, transaction_number, *, after_commit):
        self.db, self.engine = db, db.engine
        self.target, self.after_commit = transaction_number, after_commit
        self.calls = 0

    def fail(self):
        raise OperationalError("synthetic SQL", {}, MySQLError(2013, "Synthetic lost connection"))

    @contextmanager
    def transaction(self):
        self.calls += 1
        fail = self.calls == self.target
        with self.db.transaction() as session:
            yield session
            if fail and not self.after_commit:
                self.fail()
        if fail and self.after_commit:
            self.fail()


@pytest.mark.parametrize("transaction_number,after_commit", [(1, False), (1, True), (2, False), (2, True)])
def test_commit_errors_never_trigger_provider_retry(context, transaction_number, after_commit):
    actual = context.db
    fault = TransactionFault(actual, transaction_number, after_commit=after_commit)
    context.db = fault
    with pytest.raises(OperationalError):
        invoke(context)
    assert len(context.calls) == transaction_number - 1
    assert fault.calls == transaction_number
    context.db = actual
    row = claim_row(context)
    if transaction_number == 1 and not after_commit:
        assert row is None
    elif transaction_number == 2 and after_commit:
        assert row.status == "completed"
        assert invoke(context).status_code == 200 and len(context.calls) == 1
    else:
        assert row.status == "in_progress"
        assert row.expires_at - row.created_at == timedelta(seconds=CLAIM_SECONDS)
        with pytest.raises(ApiError) as pending:
            invoke(context)
        assert pending.value.code == "REQUEST_IN_PROGRESS"
        expire(context)
        assert invoke(context).status_code == 200
        assert len(context.calls) == transaction_number


def test_independent_route_preserves_error_envelope_and_retry_after(context):
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)

    @app.post("/validate")
    def validate(request: Request):
        return invoke(context, key=request.headers.get("X-AI-Key"))

    with TestClient(app) as client:
        response = client.post("/validate", headers={"X-AI-Key": KEY})
        assert response.status_code == 200
        conflict = client.post("/validate", headers={"X-AI-Key": OTHER_KEY})
        assert conflict.status_code == 409
        assert set(conflict.json()["error"]) == {"code", "message", "request_id", "details"}
        with context.db.transaction() as session:
            row = session.get(IdempotencyRequest, (context.owner, OPERATION, "validation-idempotency-key"))
            row.status, row.http_status, row.response_body = "in_progress", None, {"claim_token": uid()}
        pending = client.post("/validate", headers={"X-AI-Key": KEY})
        assert pending.status_code == 409 and pending.headers["Retry-After"] == "2"
        assert pending.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
