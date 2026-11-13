"""Local fault injection for lease, dispatch, polling, and credential ownership."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import sqlite3
import threading
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app import jobs as jobs_module
from app.core import utcnow
from app.jobs import JobService, get_job
from app.models import Job, Material
from app.providers import ProviderFailure
from app.worker import run_loop
from tests.test_jobs import KEY, Handler, enqueue, read_job
from tests.test_jobs import context as context


def test_production_lease_covers_generation_and_preparation(context):
    assert context.jobs.lease_seconds == 420
    assert context.jobs.generation_seconds == 300
    for seconds in (120, 300, 360):
        with pytest.raises(ValueError, match="Lease must exceed"):
            JobService(context, lease_seconds=seconds)
    for seconds in (0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError):
            JobService(context, prepare_seconds=seconds)
    with pytest.raises(ValueError, match="test profile"):
        JobService(context, lease_seconds=5, prepare_seconds=1, generation_timeout_seconds=1)
    context.settings = SimpleNamespace(profile="test")
    service = JobService(context, lease_seconds=5, prepare_seconds=1, generation_timeout_seconds=1)
    assert service.generation_seconds == 1


def test_full_generation_budget_does_not_expire_healthy_worker(context, monkeypatch):
    now = [utcnow()]
    monkeypatch.setattr(jobs_module, "utcnow", lambda: now[0])
    job_id = enqueue(context)

    def prepare(session, job):
        now[0] += timedelta(seconds=50)
        return {"source": "Test data"}

    def on_call():
        now[0] += timedelta(seconds=299)
        assert context.jobs.recover() == 0

    context.handler.prepare = prepare
    context.providers.on_call = on_call
    assert context.jobs.run_once()
    assert read_job(context, job_id).status == "succeeded"
    assert context.providers.calls == 1


def test_enqueue_never_stages_a_learner_credential(context):
    # Contract 1.4.0: provider keys are service-managed; nothing enters the vault.
    with context.db.transaction() as session:
        job = context.jobs.enqueue(
            session,
            user_id=context.owner,
            kind="analysis",
            resource_type="material",
            resource_id=context.resource,
            payload={},
            model_id="openai:test-model",
        )
        assert job.credential_ref is None


def _failed_with_legacy_credential(context):
    """A failed job left behind by the former BYOK deployment."""
    job_id = enqueue(context)
    ref = context.credentials.put(KEY)
    with context.db.transaction() as session:
        job = session.get(Job, job_id)
        job.status, job.retryable, job.error_code = "failed", True, "PROVIDER_UNAVAILABLE"
        job.credential_ref = ref
        session.get(Material, context.resource).processing_status = "failed"
    return job_id, ref


def _retry(context, session, job_id):
    return context.jobs.retry(
        session,
        job_id=job_id,
        user_id=context.owner,
        model_id="openai:test-model",
    )


@pytest.mark.parametrize("savepoint", [False, True])
def test_retry_rollback_preserves_legacy_ref(context, savepoint):
    job_id, old_ref = _failed_with_legacy_credential(context)
    if savepoint:
        with context.db.transaction() as session:
            with pytest.raises(RuntimeError, match="rollback fixture"):
                with session.begin_nested():
                    assert _retry(context, session, job_id).credential_ref is None
                    raise RuntimeError("rollback fixture")
    else:
        with pytest.raises(RuntimeError, match="rollback fixture"):
            with context.db.transaction() as session:
                assert _retry(context, session, job_id).credential_ref is None
                raise RuntimeError("rollback fixture")
    assert context.credentials.get(old_ref) == KEY
    job = read_job(context, job_id)
    assert (job.status, job.credential_ref, job.attempt_no) == ("failed", old_ref, 1)


def test_retry_commit_retires_legacy_ref_only_after_outer_commit(context):
    job_id, old_ref = _failed_with_legacy_credential(context)
    with context.db.transaction() as session:
        with session.begin_nested():
            assert _retry(context, session, job_id).credential_ref is None
        assert context.credentials.get(old_ref) == KEY
    assert context.credentials.get(old_ref) is None
    assert read_job(context, job_id).credential_ref is None


def test_uncertain_commit_retains_legacy_ref(context, monkeypatch):
    job_id, old_ref = _failed_with_legacy_credential(context)
    commit = context.db.engine.dialect.do_commit
    called = False

    def commit_then_lose_ack(connection):
        nonlocal called
        commit(connection)
        if not called:
            called = True
            raise sqlite3.OperationalError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(context.db.engine.dialect, "do_commit", commit_then_lose_ack)
    with pytest.raises(OperationalError):
        with context.db.transaction() as session:
            _retry(context, session, job_id)
    assert read_job(context, job_id).credential_ref is None
    # A lost acknowledgement does not authorize deleting the old owner's ref;
    # the bounded vault TTL and recover() purge it later.
    assert context.credentials.get(old_ref) == KEY


def test_known_stale_response_is_discarded_without_unknown_cost(context):
    job_id = enqueue(context)

    def supersede():
        with context.db.transaction() as session:
            session.get(Material, context.resource).latest_job_id = None

    context.providers.on_call = supersede
    assert context.jobs.run_once()
    job = read_job(context, job_id)
    assert (job.status, job.error_code, job.retryable) == ("failed", "INVALID_OUTPUT", False)
    assert job.dispatched_at is not None and job.result_url is None and job.credential_ref is None
    with context.db.transaction() as session:
        assert session.get(Material, context.resource).page_count is None
    assert not context.jobs.run_once() and context.providers.calls == 1


def test_external_preparation_releases_locks_and_revalidates_before_key_read(context, monkeypatch):
    started, release = threading.Event(), threading.Event()
    key_reads = []
    get = context.credentials.get
    monkeypatch.setattr(context.credentials, "get", lambda ref: (key_reads.append(ref), get(ref))[1])

    class SplitHandler(Handler):
        def prepare_snapshot(self, session, job):
            return job.resource_id

        def prepare_external(self, snapshot):
            started.set()
            assert release.wait(5)
            return {"source": "Test data"}

        def apply_prepared(self, session, job, prepared):
            return prepared

        def persist_prepared(self, session, job, output, prepared):
            return super().persist(session, job, output)

    context.jobs.handlers["analysis"] = SplitHandler()
    job_id = enqueue(context)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(context.jobs.run_once)
        try:
            assert started.wait(5)
            assert not key_reads
            with context.db.transaction() as session:
                session.get(Material, context.resource).latest_job_id = None
        finally:
            release.set()
        assert work.result(timeout=5)
    assert context.providers.calls == 0 and not key_reads
    assert read_job(context, job_id).dispatched_at is None


def test_preparation_deadline_fails_before_dispatch(context, monkeypatch):
    job_id = enqueue(context)
    ticks = iter((0, 60))
    monkeypatch.setattr(jobs_module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert context.jobs.run_once()
    job = read_job(context, job_id)
    assert (job.status, job.error_code, job.retryable) == ("failed", "PROVIDER_UNAVAILABLE", True)
    assert job.dispatched_at is None and context.providers.calls == 0


def test_provider_preflight_rejection_never_marks_dispatch(context):
    job_id = enqueue(context)

    def reject(*args):
        raise ProviderFailure("PROVIDER_UNAVAILABLE")

    context.providers.preflight = reject
    assert context.jobs.run_once()
    job = read_job(context, job_id)
    assert (job.status, job.retryable, job.dispatched_at) == ("failed", False, None)
    assert context.providers.calls == 0


def test_poll_race_has_one_winner_and_throttled_read_never_writes(context):
    job_id = enqueue(context)
    context.auth = SimpleNamespace(authenticate=lambda request: context.owner)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(context=context)), state=SimpleNamespace()
    )
    barrier = threading.Barrier(2)

    def poll():
        barrier.wait(timeout=5)
        response = get_job(UUID(job_id), request)
        return getattr(response, "status_code", 200)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: poll(), range(2))) == [200, 429]
    statements = []

    def capture(conn, cursor, statement, parameters, ctx, executemany):
        statements.append(statement.upper())

    event.listen(context.db.engine, "before_cursor_execute", capture)
    try:
        assert get_job(UUID(job_id), request).status_code == 429
    finally:
        event.remove(context.db.engine, "before_cursor_execute", capture)
    assert not any(statement.startswith("UPDATE") for statement in statements)


class StopSignal:
    def __init__(self, predicate=lambda: False, on_wait=None):
        self.predicate, self.on_wait, self.waits = predicate, on_wait, []

    def is_set(self):
        return self.predicate()

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.on_wait:
            self.on_wait()


@pytest.mark.parametrize("stage", ["recover", "_claim", "outbox"])
def test_worker_recovers_iteration_faults_with_sanitized_backoff(context, monkeypatch, caplog, stage):
    import app.maintenance as maintenance

    job_id = enqueue(context)
    calls = 0
    target = maintenance if stage == "outbox" else context.jobs
    name = "drain_deletions" if stage == "outbox" else stage
    original = (lambda ctx: 0) if stage == "outbox" else getattr(target, name)

    def fault_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("private SQL", {"key": KEY}, RuntimeError(KEY))
        return original(*args)

    monkeypatch.setattr(maintenance, "drain_deletions", lambda ctx: 0)
    monkeypatch.setattr(target, name, fault_once)
    stopped = StopSignal(
        predicate=lambda: len(stopped.waits) >= 1 and read_job(context, job_id).status == "succeeded"
    )
    assert run_loop(context, stopped) == 0
    assert stopped.waits == [1] and context.providers.calls == 1
    assert KEY not in caplog.text and "private SQL" not in caplog.text


def test_failed_failure_persistence_recovers_unknown_without_redispatch(context, monkeypatch, caplog):
    import app.maintenance as maintenance

    job_id = enqueue(context)
    context.providers.failure = ProviderFailure("PROVIDER_AUTH", retryable=True)
    finish = context.jobs._finish_failure
    failures = 0

    def fault_once(*args):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OperationalError("private SQL", {}, RuntimeError(KEY))
        return finish(*args)

    def expire():
        with context.db.transaction() as session:
            session.get(Job, job_id).lease_expires_at = utcnow() - timedelta(seconds=1)

    monkeypatch.setattr(context.jobs, "_finish_failure", fault_once)
    monkeypatch.setattr(maintenance, "drain_deletions", lambda ctx: 0)
    stopped = StopSignal(
        predicate=lambda: read_job(context, job_id).status == "outcome_unknown", on_wait=expire
    )
    assert run_loop(context, stopped) == 0
    assert context.providers.calls == 1 and stopped.waits == [1, 1]
    assert KEY not in caplog.text


def test_worker_exits_nonzero_after_bounded_consecutive_failures(monkeypatch, caplog):
    import app.maintenance as maintenance

    calls = []

    def fail():
        calls.append(1)
        raise RuntimeError(KEY)

    monkeypatch.setattr(maintenance, "drain_deletions", lambda ctx: 0)
    stopped = StopSignal()
    assert run_loop(SimpleNamespace(jobs=SimpleNamespace(run_once=fail)), stopped) == 1
    assert len(calls) == 5 and stopped.waits == [1, 2, 4, 8]
    assert KEY not in caplog.text
