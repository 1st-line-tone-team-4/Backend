"""No paid calls: all providers/transports in these tests are local substitutes."""

import base64
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core import ApiError, Settings, uid, utcnow
from app.credentials import Credentials, check_key
from app.db import Database
from app.jobs import JobService, RestartBody, job_dto
from app.models import Job, Material, User
from app.providers import ProviderFailure, ProviderRegistry


KEY = "sk-test-only-not-a-real-provider-secret"


class FakeProvider:
    """Deliberately confined to tests; production code has no fake fallback."""

    def __init__(self):
        self.calls = 0
        self.failure = None
        self.on_call = None

    def ensure_model(self, model):
        if model != "openai:test-model":
            raise ApiError(422, "MODEL_UNAVAILABLE", "Unavailable")

    def generate(self, model, key, kind, payload):
        self.calls += 1
        assert key == KEY
        if self.on_call:
            self.on_call()
        if self.failure:
            raise self.failure
        return {"page_count": 1}


class Handler:
    def __init__(self):
        self.invalid = False

    def is_current(self, session, job):
        resource = session.get(Material, job.resource_id)
        return resource is not None and resource.latest_job_id == job.id

    def set_state(self, session, job, state):
        if self.is_current(session, job):
            session.get(Material, job.resource_id).processing_status = {
                "queued": "queued",
                "running": "processing",
                "succeeded": "ready",
                "failed": "failed",
                "outcome_unknown": "failed",
                "cancelled": "failed",
            }[state]

    def prepare(self, session, job):
        return {"source": "Test data"}

    def persist(self, session, job, output):
        session.get(Material, job.resource_id).page_count = output["page_count"]
        if self.invalid:
            raise ApiError(422, "INVALID_OUTPUT", "Invalid generated object")
        return f"/v1/materials/{job.resource_id}"


@pytest.fixture
def context(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "domain.db"))
    db.create_schema()
    encryption_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    ctx = SimpleNamespace(
        db=db, credentials=Credentials(tmp_path / "vault.db", encryption_key), providers=FakeProvider()
    )
    ctx.jobs = JobService(ctx)
    ctx.handler = Handler()
    ctx.jobs.register("analysis", ctx.handler)
    ctx.owner, ctx.resource = uid(), uid()
    with db.transaction() as session:
        session.add(User(id=ctx.owner, auth_issuer="test", auth_subject=ctx.owner, created_at=utcnow()))
        session.flush()
        session.add(
            Material(
                id=ctx.resource,
                user_id=ctx.owner,
                filename="source.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                storage_key=uid(),
                processing_status="queued",
                archived=False,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
    ctx.encryption_key = encryption_key
    yield ctx
    db.engine.dispose()


def enqueue(ctx):
    with ctx.db.transaction() as session:
        job = ctx.jobs.enqueue(
            session,
            user_id=ctx.owner,
            kind="analysis",
            resource_type="material",
            resource_id=ctx.resource,
            payload={"material_id": ctx.resource},
            model_id="openai:test-model",
            ai_key=KEY,
        )
        session.get(Material, ctx.resource).latest_job_id = job.id
        return job.id


def read_job(ctx, job_id):
    with ctx.db.transaction() as session:
        return session.get(Job, job_id)


def expire_lease(ctx, job_id):
    with ctx.db.transaction() as session:
        session.get(Job, job_id).lease_expires_at = utcnow() - timedelta(seconds=1)


def test_success_and_no_plaintext_in_database_or_vault(context):
    job_id = enqueue(context)
    ref = read_job(context, job_id).credential_ref
    assert KEY.encode() not in context.credentials.path.read_bytes()
    assert KEY.encode() not in (context.credentials.path.parent / "domain.db").read_bytes()
    assert context.jobs.run_once() is True
    job = read_job(context, job_id)
    assert job.status == "succeeded"
    assert job.result_url == f"/v1/materials/{context.resource}"
    assert job.credential_ref is None
    assert context.credentials.get(ref) is None
    assert "credential_ref" not in job_dto(job)
    assert "request_payload" not in job_dto(job)
    assert context.jobs.run_once() is False
    assert context.providers.calls == 1


def test_expired_credential_fails_before_call(context):
    job_id = enqueue(context)
    context.credentials.delete(read_job(context, job_id).credential_ref)
    context.jobs.run_once()
    job = read_job(context, job_id)
    assert (job.status, job.error_code, job.retryable) == ("failed", "CREDENTIAL_EXPIRED", True)
    assert context.providers.calls == 0


@pytest.mark.parametrize("code", ["PROVIDER_AUTH", "PROVIDER_RATE_LIMIT", "PROVIDER_UNAVAILABLE"])
def test_definite_rejection_retries_only_explicitly_and_max_three(context, code):
    job_id = enqueue(context)
    context.providers.failure = ProviderFailure(code, retryable=True)
    for attempt in (1, 2, 3):
        assert context.jobs.run_once()
        job = read_job(context, job_id)
        assert job.status == "failed" and job.attempt_no == attempt
        assert job.retryable is (attempt < 3)
        assert context.jobs.run_once() is False
        if attempt < 3:
            with context.db.transaction() as session:
                context.jobs.retry(
                    session, job_id=job_id, user_id=context.owner, model_id="openai:test-model", ai_key=KEY
                )
    assert context.providers.calls == 3
    with pytest.raises(ApiError, match="cannot be retried"):
        with context.db.transaction() as session:
            context.jobs.retry(
                session, job_id=job_id, user_id=context.owner, model_id="openai:test-model", ai_key=KEY
            )


def test_uncertain_timeout_never_blind_retry_and_requires_cost_consent(context):
    job_id = enqueue(context)
    context.providers.failure = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    context.jobs.run_once()
    assert read_job(context, job_id).status == "outcome_unknown"
    assert context.jobs.run_once() is False
    for acknowledgement in (False, None, 1):
        with pytest.raises(ApiError):
            with context.db.transaction() as session:
                context.jobs.restart(
                    session,
                    job_id=job_id,
                    user_id=context.owner,
                    model_id="openai:test-model",
                    ai_key=KEY,
                    acknowledge_possible_duplicate_cost=acknowledgement,
                )
    with context.db.transaction() as session:
        successor = context.jobs.restart(
            session,
            job_id=job_id,
            user_id=context.owner,
            model_id="openai:test-model",
            ai_key=KEY,
            acknowledge_possible_duplicate_cost=True,
        )
        new_id = successor.id
    assert new_id != job_id
    assert read_job(context, new_id).previous_job_id == job_id
    assert read_job(context, job_id).status == "outcome_unknown"
    with pytest.raises(ApiError):
        with context.db.transaction() as session:
            context.jobs.restart(
                session,
                job_id=job_id,
                user_id=context.owner,
                model_id="openai:test-model",
                ai_key=KEY,
                acknowledge_possible_duplicate_cost=True,
            )


def test_crash_after_dispatch_recovery_is_unknown_not_a_second_call(context):
    class ProcessCrash(BaseException):
        pass

    job_id = enqueue(context)
    context.providers.failure = ProcessCrash()
    with pytest.raises(ProcessCrash):
        context.jobs.run_once()
    assert read_job(context, job_id).dispatched_at is not None
    expire_lease(context, job_id)
    assert context.jobs.recover() == 1
    assert read_job(context, job_id).status == "outcome_unknown"
    context.providers.failure = None
    assert context.jobs.run_once() is False
    assert context.providers.calls == 1


def test_crash_before_dispatch_safe_reclaim_does_not_increment_attempt(context):
    job_id = enqueue(context)
    claim = context.jobs._claim()
    expire_lease(context, job_id)
    assert context.jobs.recover() == 1
    assert read_job(context, job_id).status == "queued"
    with context.db.transaction() as session:
        assert context.jobs._guard(session, *claim) is None
    context.jobs.run_once()
    job = read_job(context, job_id)
    assert (job.status, job.attempt_no) == ("succeeded", 1)
    assert context.providers.calls == 1


def test_two_workers_dispatch_at_most_once(context):
    job_id = enqueue(context)
    barrier = threading.Barrier(2)

    def run():
        barrier.wait(timeout=5)
        return context.jobs.run_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert any(results)
    assert context.providers.calls == 1
    assert read_job(context, job_id).status == "succeeded"


def test_old_worker_cannot_commit_after_recovery_and_successor(context):
    job_id = enqueue(context)
    started, release = threading.Event(), threading.Event()
    context.providers.on_call = lambda: (started.set(), release.wait(10))
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(context.jobs.run_once)
        assert started.wait(5)
        expire_lease(context, job_id)
        context.jobs.recover()
        with context.db.transaction() as session:
            new = context.jobs.restart(
                session,
                job_id=job_id,
                user_id=context.owner,
                model_id="openai:test-model",
                ai_key=KEY,
                acknowledge_possible_duplicate_cost=True,
            )
            new_id = new.id
        release.set()
        assert work.result(timeout=5)
    with context.db.transaction() as session:
        resource = session.get(Material, context.resource)
        assert resource.latest_job_id == new_id
        assert resource.page_count is None
        assert resource.processing_status == "queued"
    assert read_job(context, job_id).status == "outcome_unknown"
    assert context.providers.calls == 1


def test_invalid_output_rolls_back_partial_results(context):
    job_id = enqueue(context)
    context.handler.invalid = True
    context.jobs.run_once()
    job = read_job(context, job_id)
    assert (job.status, job.error_code, job.retryable) == ("failed", "INVALID_OUTPUT", False)
    with context.db.transaction() as session:
        assert session.get(Material, context.resource).page_count is None
    assert context.providers.calls == 1


def test_cancel_before_dispatch_and_foreign_owner(context):
    job_id = enqueue(context)
    with pytest.raises(ApiError) as err:
        context.jobs.cancel(job_id=job_id, user_id=uid())
    assert err.value.status == 404
    assert context.jobs.cancel(job_id=job_id, user_id=context.owner)["status"] == "failed"
    assert context.jobs.run_once() is False
    assert context.providers.calls == 0


def test_cancel_during_call_fences_result(context):
    job_id = enqueue(context)
    context.providers.on_call = lambda: context.jobs.cancel(job_id=job_id, user_id=context.owner)
    context.jobs.run_once()
    assert read_job(context, job_id).status == "outcome_unknown"
    with context.db.transaction() as session:
        assert session.get(Material, context.resource).page_count is None


def test_queue_deadline_prevents_paid_call(context):
    job_id = enqueue(context)
    with context.db.transaction() as session:
        session.get(Job, job_id).updated_at = utcnow() - timedelta(minutes=11)
    assert context.jobs.run_once() is False
    job = read_job(context, job_id)
    assert (job.status, job.error_code, job.retryable) == ("failed", "PROVIDER_UNAVAILABLE", True)
    assert context.providers.calls == 0


def test_archive_existing_job_does_not_discard_result(context):
    job_id = enqueue(context)
    with context.db.transaction() as session:
        session.get(Material, context.resource).archived = True
    context.jobs.run_once()
    assert read_job(context, job_id).status == "succeeded"


def test_vault_process_shared_expiry_and_tamper_rejection(context):
    ref = context.credentials.put(KEY)
    # Child only prints a boolean; neither command line nor output contains BYOK.
    code = "from app.credentials import Credentials; import os; v=Credentials(os.environ['TEST_VAULT'],os.environ['TEST_CIPHER']); print(bool(v.get(os.environ['TEST_REF'])))"
    env = dict(
        os.environ, TEST_VAULT=str(context.credentials.path), TEST_CIPHER=context.encryption_key, TEST_REF=ref
    )
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "True"
    with sqlite3.connect(context.credentials.path) as db:
        db.execute("UPDATE credentials SET expires_at=expires_at+100 WHERE ref=?", (ref,))
    assert context.credentials.get(ref) is None
    ref = context.credentials.put(KEY)
    with sqlite3.connect(context.credentials.path) as db:
        db.execute("UPDATE credentials SET expires_at=0 WHERE ref=?", (ref,))
    assert context.credentials.get(ref) is None
    with pytest.raises(ValueError):
        Credentials(context.credentials.path, context.encryption_key, ttl_seconds=901)


def test_separate_worker_process_consumes_committed_job(context):
    job_id = enqueue(context)
    code = """
import os, runpy
from types import SimpleNamespace
from app.db import Database
from app.credentials import Credentials
from app.jobs import JobService
fixtures = runpy.run_path('tests/test_jobs.py')
ctx = SimpleNamespace(db=Database(os.environ['TEST_DB']), credentials=Credentials(os.environ['TEST_VAULT'], os.environ['TEST_CIPHER']), providers=fixtures['FakeProvider']())
ctx.jobs = JobService(ctx)
ctx.jobs.register('analysis', fixtures['Handler']())
assert ctx.jobs.run_once()
assert ctx.providers.calls == 1
print('worker-completed')
"""
    env = dict(
        os.environ,
        TEST_DB=str(context.db.engine.url),
        TEST_VAULT=str(context.credentials.path),
        TEST_CIPHER=context.encryption_key,
    )
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "worker-completed"
    assert read_job(context, job_id).status == "succeeded"
    assert context.providers.calls == 0  # Work actually happened outside this process.


def test_two_concurrent_restarts_have_one_successor(context):
    job_id = enqueue(context)
    context.providers.failure = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    context.jobs.run_once()
    barrier = threading.Barrier(2)

    def restart():
        barrier.wait(timeout=5)
        try:
            with context.db.transaction() as session:
                new = context.jobs.restart(
                    session,
                    job_id=job_id,
                    user_id=context.owner,
                    model_id="openai:test-model",
                    ai_key=KEY,
                    acknowledge_possible_duplicate_cost=True,
                )
                return new.id
        except ApiError as exc:
            assert exc.status == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: restart(), range(2)))
    assert len([x for x in results if x]) == 1
    with context.db.transaction() as session:
        assert len(session.scalars(select(Job).where(Job.previous_job_id == job_id)).all()) == 1


@pytest.mark.parametrize("value", [1, "true", False, None])
def test_restart_rejects_coerced_consent(value):
    with pytest.raises(ValidationError):
        RestartBody(model_id="openai:test-model", acknowledge_possible_duplicate_cost=value)


def test_provider_disabled_by_default_and_fixed_allowlist():
    registry = ProviderRegistry(Settings())
    assert registry.list_models() == []
    with pytest.raises(ApiError):
        registry.ensure_model("openai:anything")
    for model in ("https://evil.test/model", "openai:../evil", "test:mock"):
        with pytest.raises(ValueError):
            ProviderRegistry(Settings(enabled_models=(model,)))
    with pytest.raises(ValueError):
        ProviderRegistry(Settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    for key in ("", "x" * 513, "key\nInjected", "has space"):
        with pytest.raises(ApiError):
            check_key(key)


def test_openai_non_generating_validation_and_multimodal_fixed_endpoint():
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == "api.openai.com"
        assert request.headers["authorization"] == "Bearer " + KEY
        if request.method == "GET":
            return httpx.Response(200, json={"id": "test-model"})
        body = json.loads(request.content)
        assert body["messages"][1]["content"][1]["image_url"]["url"] == "data:image/png;base64,YQ=="
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}
        )

    registry = ProviderRegistry(
        Settings(profile="test", enabled_models=("openai:test-model",)),
        transport=httpx.MockTransport(respond),
    )
    assert registry.validate_key("openai:test-model", KEY)["valid"] is True
    assert len(calls) == 1 and calls[0].method == "GET"
    assert registry.generate(
        "openai:test-model",
        KEY,
        "analysis",
        {"image_data_url": "data:image/png;base64,YQ==", "instructions": "JSON"},
    ) == {"ok": True}
    assert calls[1].url.path == "/v1/chat/completions"


@pytest.mark.parametrize(
    "status,code,retry,unknown",
    [
        (401, "PROVIDER_AUTH", True, False),
        (429, "PROVIDER_RATE_LIMIT", True, False),
        (503, "TIMEOUT_UNKNOWN", False, True),
        (302, "PROVIDER_UNAVAILABLE", False, False),
    ],
)
def test_provider_failure_classification_no_error_body_leak(status, code, retry, unknown):
    registry = ProviderRegistry(
        Settings(profile="test", enabled_models=("openai:test-model",)),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text=KEY, headers={"Location": "https://evil.test"})
        ),
    )
    with pytest.raises(ProviderFailure) as err:
        registry.generate("openai:test-model", KEY, "analysis", {})
    assert (err.value.code, err.value.retryable, err.value.outcome_unknown) == (code, retry, unknown)
    assert KEY not in str(err.value)


def test_provider_oversized_response_is_bounded_nonretryable_invalid_output():
    registry = ProviderRegistry(
        Settings(profile="test", enabled_models=("openai:test-model",)),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        ),
    )
    with pytest.raises(ProviderFailure) as error:
        registry.generate("openai:test-model", KEY, "analysis", {})
    assert error.value.code == "INVALID_OUTPUT"
    assert error.value.retryable is False


def test_provider_total_deadline_includes_slow_stream():
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            await asyncio.sleep(1)
            yield b"}"

    registry = ProviderRegistry(
        Settings(profile="test", enabled_models=("openai:test-model",)),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowBody())),
    )
    registry.TOTAL_TIMEOUT_SECONDS = 0.01
    with pytest.raises(ProviderFailure) as error:
        registry.generate("openai:test-model", KEY, "analysis", {})
    assert error.value.code == "TIMEOUT_UNKNOWN"
    assert error.value.outcome_unknown is True


def test_explicit_provider_token_cap_is_validated_without_calls():
    settings = Settings(profile="test", enabled_models=("openai:test-model",))
    assert ProviderRegistry(settings).max_completion_tokens == 16384
    for value in (0, -1, 16385, True, 1.5, "256"):
        with pytest.raises(ValueError):
            ProviderRegistry(settings, max_completion_tokens=value)
    calls = []

    def respond(request):
        calls.append(request)
        assert json.loads(request.content)["max_completion_tokens"] == 256
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}
        )

    registry = ProviderRegistry(settings, max_completion_tokens=256, transport=httpx.MockTransport(respond))
    assert registry.generate("openai:test-model", KEY, "analysis", {}) == {"ok": True}
    assert len(calls) == 1
