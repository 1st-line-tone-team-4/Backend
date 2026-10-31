"""Live MySQL checks sharing the one disposable, initially empty DB fixture."""

from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier, Event, Lock, Thread, local
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core import ApiError, idempotent, uid, utcnow
from app.models import IdempotencyRequest, Job, User
from app.providers import ProviderFailure
import tests.test_e2e as e2e
from tests.test_mysql_boundaries import signed_auth


def assert_mysql_exact_jwt_identity(live):
    db = live.ctx.db
    owner = live.auth.tokens["test-owner-token"]
    auth, request = signed_auth(db, "test-only")
    assert auth.authenticate(request("test-owner-token")) == owner
    with db.transaction() as session:
        assert (
            session.scalar(
                select(User.id).where(
                    User.auth_issuer == "test-only", User.auth_subject == "test-owner-token "
                )
            )
            == owner
        ), "Fixture must reproduce MySQL PAD SPACE identity equality"
    with pytest.raises(ApiError) as exc:
        auth.authenticate(request("test-owner-token "))
    assert (exc.value.status, exc.value.code) == (401, "UNAUTHORIZED")

    subject = uid()
    with db.transaction() as session:
        session.add(
            User(id=uid(), auth_issuer="mysql-boundary-issuer ", auth_subject=subject, created_at=utcnow())
        )
    auth, request = signed_auth(db, "mysql-boundary-issuer")
    with pytest.raises(ApiError) as exc:
        auth.authenticate(request(subject))
    assert (exc.value.status, exc.value.code) == (401, "UNAUTHORIZED")


def assert_mysql_claim_deadlock(live):
    # Separate, bounded connections keep a failed rendezvous from leaking a
    # blocking statement into the HTTP fixture's connection pool.
    engine = create_engine(
        live.ctx.db.engine.url,
        isolation_level="REPEATABLE READ",
        connect_args={"connect_timeout": 5, "read_timeout": 5, "write_timeout": 5},
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @event.listens_for(engine, "connect")
    def bound_lock_wait(connection, record):
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 3")

    @contextmanager
    def transaction():
        with factory() as session:
            with session.begin():
                yield session

    ctx = SimpleNamespace(db=SimpleNamespace(engine=engine, transaction=transaction))
    owner, key = live.auth.tokens["test-owner-token"], uid()
    barrier, lock, per_thread = Barrier(2), Lock(), local()
    results, failures, calls, deadlocks = {}, [], [], []

    def pause_after_first_delete(conn, cursor, statement, parameters, execution_context, executemany):
        if getattr(per_thread, "racing", False) and statement.lstrip().upper().startswith(
            "DELETE FROM IDEMPOTENCY_REQUESTS"
        ):
            if not getattr(per_thread, "deleted", False):
                per_thread.deleted = True
                assert cursor.rowcount == 0, "Both first claims must target the same missing key"
                # Both missing-row DELETEs now hold compatible gap locks.
                # Releasing both INSERTs makes the deadlock reproducible.
                barrier.wait(timeout=5)

    def record_error(exception_context):
        if exception_context.original_exception.args[:1] == (1213,):
            with lock:
                deadlocks.append(1213)

    def action(session):
        with lock:
            calls.append(1)
        return 201, {"created": "once"}

    def run(index):
        per_thread.racing = True
        try:
            result = idempotent(ctx, owner, "mysqlConcurrentClaim", key, {"value": 1}, action)
            with lock:
                results[index] = result
        except BaseException as exc:
            with lock:
                failures.append(exc)

    threads = [Thread(target=run, args=(i,), daemon=True) for i in range(2)]
    event.listen(engine, "after_cursor_execute", pause_after_first_delete)
    event.listen(engine, "handle_error", record_error)
    try:
        with engine.connect() as conn:
            assert conn.get_isolation_level() == "REPEATABLE READ"
        for thread in threads:
            thread.start()
        deadline = monotonic() + 15
        for thread in threads:
            thread.join(timeout=max(0, deadline - monotonic()))
        assert not any(thread.is_alive() for thread in threads), "Concurrent claims did not finish"
        if failures:
            raise failures[0]
        assert deadlocks, "Regression must exercise an actual MySQL 1213 deadlock"
        assert len(calls) == 1
        assert [results[i].status_code for i in range(2)] == [201, 201]
        assert results[0].body == results[1].body
        replay = idempotent(ctx, owner, "mysqlConcurrentClaim", key, {"value": 1}, action)
        assert (replay.status_code, replay.body) == (201, results[0].body)
        assert len(calls) == 1
        with transaction() as session:
            rows = session.scalars(
                select(IdempotencyRequest).where(
                    IdempotencyRequest.user_id == owner,
                    IdempotencyRequest.operation_id == "mysqlConcurrentClaim",
                    IdempotencyRequest.idempotency_key == key,
                )
            ).all()
            assert len(rows) == 1 and rows[0].status == "completed"
    finally:
        barrier.abort()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=6)
        event.remove(engine, "after_cursor_execute", pause_after_first_delete)
        event.remove(engine, "handle_error", record_error)
        engine.dispose()


def assert_mysql_unknown_uses_current_read(live):
    _, concept, version = e2e.confirmed(live)
    body = {
        "unit_id": concept["unit_id"],
        "concept_version_ids": [version["id"]],
        "count": 1,
        "difficulty": "easy",
        "model_id": e2e.MODEL,
    }
    owner = live.auth.tokens["test-owner-token"]
    first_key, second_key = "mysql-snapshot-10-claim", "mysql-snapshot-30-claim"
    now = utcnow()
    # Existing expired claims plus intervening live rows place the requests in
    # separate PK gaps. They must overlap in domain reads, not wait on a claim.
    with live.ctx.db.transaction() as session:
        for key in (
            "mysql-snapshot-00-fence",
            first_key,
            "mysql-snapshot-20-fence",
            second_key,
            "mysql-snapshot-40-fence",
        ):
            session.add(
                IdempotencyRequest(
                    user_id=owner,
                    operation_id="createQuestionSet",
                    idempotency_key=key,
                    request_hash="0" * 64,
                    status="completed",
                    http_status=200,
                    response_body={},
                    created_at=now - timedelta(days=2),
                    expires_at=now - timedelta(seconds=1)
                    if key in (first_key, second_key)
                    else now + timedelta(hours=1),
                )
            )
    paused, resume, lock = Event(), Event(), Lock()
    result, failures = [], []
    paused_once = False

    def pause_before_matching_jobs(conn, cursor, statement, parameters, execution_context, executemany):
        nonlocal paused_once
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "ai_jobs.kind" in statement
            and "questions" in values
            and "outcome_unknown" in values
        ):
            with lock:
                if paused_once:
                    return
                paused_once = True
            # Material and version reads have established the first request's
            # RR snapshot; the matching-job query has not executed yet.
            paused.set()
            assert resume.wait(timeout=20), "Second request did not complete the unknown outcome"

    def first_request():
        try:
            with httpx.Client(
                base_url=str(live.client.base_url), headers=e2e.OWNER, timeout=25, trust_env=False
            ) as client:
                result.append(
                    client.post(
                        "/v1/question-sets",
                        json=body,
                        headers={"Idempotency-Key": first_key, "X-AI-Key": e2e.KEY},
                    )
                )
        except BaseException as exc:
            failures.append(exc)

    engine = live.ctx.db.engine
    with engine.connect() as conn:
        assert conn.get_isolation_level() == "REPEATABLE READ"
    event.listen(engine, "before_cursor_execute", pause_before_matching_jobs)
    thread = Thread(target=first_request, daemon=True)
    try:
        thread.start()
        assert paused.wait(timeout=10), "First request did not reach matching-job lookup"
        second = e2e.post(live, "/v1/question-sets", body, idem=second_key)
        assert second.status_code == 202, second.text
        job_id = second.json()["data"]["job"]["id"]
        before = len(live.provider.calls)
        live.provider.fail_next = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
        assert live.ctx.jobs.run_once()
        with live.ctx.db.transaction() as session:
            assert session.get(Job, job_id).status == "outcome_unknown"
        resume.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "First request did not resume"
        if failures:
            raise failures[0]
        assert len(result) == 1 and result[0].status_code == 409, [r.text for r in result]
        assert result[0].json()["error"]["code"] == "POSSIBLE_DUPLICATE_COST"
        assert len(live.provider.calls) == before + 1
        with live.ctx.db.transaction() as session:
            matching = [
                job
                for job in session.scalars(select(Job).where(Job.user_id == owner, Job.kind == "questions"))
                if job.request_payload["unit_id"] == concept["unit_id"]
            ]
            assert len(matching) == 1 and matching[0].id == job_id
    finally:
        resume.set()
        thread.join(timeout=26)
        event.remove(engine, "before_cursor_execute", pause_before_matching_jobs)
