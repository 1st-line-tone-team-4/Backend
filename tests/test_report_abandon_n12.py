"""N12: abandon terminal report work without erasing diagnostic history.

The application, signed JWTs, loopback HTTP and durable worker are real. Only
provider responses are synthetic; no external AI endpoint is contacted.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic

import pytest
from sqlalchemy import inspect, select

from app.core import uid, utcnow
from app.models import (
    IdempotencyRequest,
    ImprovementAction,
    Job,
    PlanTask,
    ReportEvidence,
    ReportItem,
    SessionMessage,
    SessionReport,
    TeachbackSession,
)
from app.providers import ProviderFailure
from tests.ab_support import MODEL, ab_http_application
from tests.test_ab_e2e import post, run_job, validate
from tests.test_terminal_jobs_n11 import message, new_session, replan, seed_terminal_graph, skip


@pytest.fixture
def report_env(tmp_path):
    with ab_http_application(tmp_path) as fx:
        yield seed_terminal_graph(fx)


def pending_report(fx):
    created = new_session(fx)
    assert created.status_code == 201, created.text
    session_id = created.json()["data"]["id"]
    run_job(fx, message(fx, session_id))
    accepted = post(fx, f"/v1/teachback-sessions/{session_id}/complete", {"model_id": MODEL})
    assert accepted.status_code == 202, accepted.text
    return session_id, accepted


def terminal_report(fx, failure="unknown"):
    session_id, accepted = pending_report(fx)
    fx.provider.fail_next = {
        "retryable": ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True),
        "nonretryable": ProviderFailure("INVALID_OUTPUT"),
        "unknown": ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True),
        "succeeded_without_report": ProviderFailure("INVALID_OUTPUT"),
    }[failure]
    assert fx.ctx.jobs.run_once()
    job_id = accepted.json()["data"]["job"]["id"]
    if failure == "succeeded_without_report":
        # Defensive recovery state: a terminal success marker has no saved report.
        with fx.ctx.db.transaction() as db:
            job = db.get(Job, job_id)
            job.status, job.error_code = "succeeded", None
            job.result_url = f"/v1/teachback-sessions/{session_id}/report"
    return session_id, job_id


def abandon(fx, session_id, *, headers=None):
    return fx.client.patch(
        f"/v1/teachback-sessions/{session_id}", json={"status": "abandoned"}, headers=headers
    )


def row_data(row, *, omit=()):
    return {
        column.key: deepcopy(getattr(row, column.key))
        for column in inspect(type(row)).columns
        if column.key not in omit
    }


def preserved_history(fx, session_id):
    """All persisted session/job/report history, plus progress and audit rows."""
    with fx.ctx.db.transaction() as db:
        session = db.get(TeachbackSession, session_id)
        reports = list(db.scalars(select(SessionReport).where(SessionReport.session_id == session_id)))
        report_ids = [report.id for report in reports]
        items = list(db.scalars(select(ReportItem).where(ReportItem.report_id.in_(report_ids))))
        return {
            "session": row_data(session, omit=("status",)),
            # GET /jobs legitimately advances only its polling timestamp.
            "jobs": [
                row_data(job, omit=("last_polled_at",))
                for job in db.scalars(select(Job).where(Job.resource_id == session_id).order_by(Job.id))
            ],
            "messages": [
                row_data(msg)
                for msg in db.scalars(
                    select(SessionMessage)
                    .where(SessionMessage.session_id == session_id)
                    .order_by(SessionMessage.sequence_no)
                )
            ],
            "reports": [row_data(report) for report in reports],
            "items": [row_data(item) for item in sorted(items, key=lambda item: item.id)],
            "evidence": [
                row_data(row)
                for row in db.scalars(
                    select(ReportEvidence)
                    .where(ReportEvidence.report_item_id.in_([item.id for item in items]))
                    .order_by(ReportEvidence.report_item_id, ReportEvidence.chunk_id)
                )
            ],
            "actions": [
                row_data(row)
                for row in db.scalars(
                    select(ImprovementAction)
                    .where(ImprovementAction.report_id.in_(report_ids))
                    .order_by(ImprovementAction.id)
                )
            ],
            "task": row_data(db.get(PlanTask, session.task_id)) if session.task_id else None,
            "audit": [
                row_data(row)
                for row in db.scalars(
                    select(IdempotencyRequest)
                    .where(IdempotencyRequest.user_id == fx.owner)
                    .order_by(IdempotencyRequest.operation_id, IdempotencyRequest.idempotency_key)
                )
            ],
        }


@pytest.mark.parametrize("failure", ["retryable", "nonretryable", "unknown", "succeeded_without_report"])
def test_terminal_report_abandon_preserves_history_and_replays_without_a_key(report_env, failure):
    fx = report_env
    session_id, job_id = terminal_report(fx, failure)
    before = preserved_history(fx, session_id)
    calls = len(fx.provider.calls)
    # A fresh page can still retrieve the pending session and terminal job.
    entered = fx.client.get(f"/v1/teachback-sessions/{session_id}")
    assert entered.json()["data"]["status"] == "report_pending"
    assert entered.json()["data"]["latest_job_id"] == job_id
    first = abandon(fx, session_id)
    assert first.status_code == 200, first.text
    validate(first, "/teachback-sessions/{sessionId}", "patch")
    assert first.json()["data"]["status"] == "abandoned"
    assert abandon(fx, session_id).json() == first.json()
    assert fx.client.get(f"/v1/teachback-sessions/{session_id}").json() == first.json()
    messages = fx.client.get(f"/v1/teachback-sessions/{session_id}/messages")
    assert messages.status_code == 200 and len(messages.json()["data"]) == 2
    validate(messages, "/teachback-sessions/{sessionId}/messages")
    job = fx.client.get(f"/v1/jobs/{job_id}")
    assert job.status_code == 200, job.text
    validate(job, "/jobs/{jobId}")
    prior_job = next(row for row in before["jobs"] if row["id"] == job_id)
    assert job.json()["data"]["status"] == prior_job["status"]
    assert fx.ctx.jobs.run_once() is False and len(fx.provider.calls) == calls
    assert preserved_history(fx, session_id) == before
    if failure != "succeeded_without_report":
        operation = "retry" if failure == "retryable" else "restart"
        body = {"model_id": MODEL}
        if operation == "restart":
            body["acknowledge_possible_duplicate_cost"] = True
        stale = post(fx, f"/v1/jobs/{job_id}/{operation}", body)
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "STALE_JOB", stale.text
        assert preserved_history(fx, session_id) == before


def test_unknown_report_abandon_unblocks_new_session_skip_replan_and_historical_replay(report_env):
    fx = report_env
    session_id, job_id = terminal_report(fx)
    assert new_session(fx).status_code == 409
    assert skip(fx, fx.teach).status_code == 409
    assert replan(fx).status_code == 409
    abandoned = abandon(fx, session_id)
    assert abandoned.status_code == 200, abandoned.text
    fresh = new_session(fx)
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["data"]["id"] != session_id
    assert abandon(fx, fresh.json()["data"]["id"]).status_code == 200
    assert skip(fx, fx.teach).status_code == 200
    assert abandon(fx, session_id).json() == abandoned.json()
    assert replan(fx).status_code == 201
    assert abandon(fx, session_id).json() == abandoned.json()
    with fx.ctx.db.transaction() as db:
        assert db.get(Job, job_id).status == "outcome_unknown"
        assert db.get(TeachbackSession, session_id).latest_job_id == job_id


def test_failed_report_can_be_abandoned_after_goal_pause(report_env):
    fx = report_env
    session_id, _ = terminal_report(fx, "nonretryable")
    paused = fx.client.patch(f"/v1/exam-goals/{fx.plan['exam_goal_id']}", json={"status": "paused"})
    assert paused.status_code == 200, paused.text
    before = preserved_history(fx, session_id)
    assert abandon(fx, session_id).status_code == 200
    assert abandon(fx, session_id).status_code == 200
    assert preserved_history(fx, session_id) == before


@pytest.mark.parametrize("state", ["queued", "running"])
def test_nonterminal_report_is_protected(report_env, state):
    fx = report_env
    session_id, accepted = pending_report(fx)
    job_id = accepted.json()["data"]["job"]["id"]
    if state == "running":
        assert fx.ctx.jobs._claim()[0] == job_id
    before = preserved_history(fx, session_id)
    denied = abandon(fx, session_id)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "JOB_IN_PROGRESS", denied.text
    assert fx.client.get(f"/v1/teachback-sessions/{session_id}").json()["data"]["status"] == "report_pending"
    assert preserved_history(fx, session_id) == before


@pytest.mark.parametrize("state", ["completed", "report_pending", "active"])
def test_saved_report_and_progress_are_immutable_during_abandon(report_env, state):
    fx = report_env
    session_id, accepted = pending_report(fx)
    run_job(fx, accepted)
    if state != "completed":
        # Even an inconsistent session status cannot discard a saved result.
        with fx.ctx.db.transaction() as db:
            session = db.get(TeachbackSession, session_id)
            session.status, session.completed_at = state, None
    report = fx.client.get(f"/v1/teachback-sessions/{session_id}/report")
    assert report.status_code == 200
    before = preserved_history(fx, session_id)
    assert before["reports"] and before["items"] and before["evidence"] and before["actions"]
    denied = abandon(fx, session_id)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "SESSION_NOT_ACTIVE"
    assert fx.client.get(f"/v1/teachback-sessions/{session_id}/report").json() == report.json()
    assert fx.client.get(f"/v1/teachback-sessions/{session_id}").json()["data"]["status"] == state
    assert preserved_history(fx, session_id) == before


@pytest.mark.parametrize("invalid", ["missing", "kind", "resource_type", "resource_id", "owner"])
def test_pending_report_requires_its_current_owned_report_job(report_env, invalid):
    fx = report_env
    session_id, job_id = terminal_report(fx)
    other_id = fx.client.get("/v1/me", headers=fx.other_headers).json()["data"]["id"]
    with fx.ctx.db.transaction() as db:
        job = db.get(Job, job_id)
        if invalid == "missing":
            db.get(TeachbackSession, session_id).latest_job_id = None
        elif invalid == "kind":
            job.kind = "message"
        elif invalid == "resource_type":
            job.resource_type = "question_set"
        elif invalid == "resource_id":
            job.resource_id = uid()
        else:
            job.user_id = other_id
    before = preserved_history(fx, session_id)
    denied = abandon(fx, session_id)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "SESSION_NOT_ACTIVE", denied.text
    assert preserved_history(fx, session_id) == before


def test_abandon_is_owner_scoped_including_idempotent_replay(report_env):
    fx = report_env
    session_id, _ = terminal_report(fx)
    assert abandon(fx, session_id, headers=fx.other_headers).status_code == 404
    assert abandon(fx, uid()).status_code == 404
    assert abandon(fx, session_id).status_code == 200
    assert abandon(fx, session_id, headers=fx.other_headers).status_code == 404


def test_late_report_response_after_recovery_and_abandon_cannot_persist(report_env, monkeypatch):
    fx = report_env
    session_id, accepted = pending_report(fx)
    job_id = accepted.json()["data"]["job"]["id"]
    entered, release = Event(), Event()
    generate = fx.provider.generate

    def delayed(model_id, key, kind, payload):
        assert kind == "report"
        entered.set()
        assert release.wait(10), "Synthetic report call was not released"
        return generate(model_id, key, kind, payload)

    monkeypatch.setattr(fx.provider, "generate", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(fx.ctx.jobs.run_once)
        try:
            assert entered.wait(5), "Worker did not reach the fake provider"
            assert abandon(fx, session_id).status_code == 409
            with fx.ctx.db.transaction() as db:
                job = db.get(Job, job_id)
                assert job.status == "running" and job.dispatched_at is not None
                old_token = job.fencing_token
                job.lease_expires_at = utcnow() - timedelta(seconds=1)
            assert fx.ctx.jobs.recover() == 1
            before = preserved_history(fx, session_id)
            assert abandon(fx, session_id).status_code == 200
        finally:
            release.set()
        assert worker.result(timeout=5)
    assert preserved_history(fx, session_id) == before
    with fx.ctx.db.transaction() as db:
        assert db.get(Job, job_id).status == "outcome_unknown"
        assert db.get(Job, job_id).fencing_token > old_token
        assert fx.ctx.jobs._guard(db, job_id, old_token) is None
        assert db.get(TeachbackSession, session_id).status == "abandoned"
        assert not db.scalar(select(SessionReport.id).where(SessionReport.session_id == session_id))


def test_parallel_abandon_returns_same_result_without_new_work(report_env):
    fx = report_env
    session_id, _ = terminal_report(fx)
    before = preserved_history(fx, session_id)
    gate = Barrier(2)

    def request_abandon():
        gate.wait(timeout=5)
        return abandon(fx, session_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request_abandon) for _ in range(2)]
        responses = [future.result(timeout=10) for future in futures]
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["data"]["status"] == "abandoned"
    assert preserved_history(fx, session_id) == before


def assert_mysql_report_abandon_current_reads(fixture):
    """Run with the existing migrated, disposable MySQL + real-JWT fixture.

    Exercise actual RR locking reads, NOWAIT rollback, and both deterministic
    restart/abandon interleavings. Test gates pause after real locks are acquired;
    they do not replace SQL, transaction boundaries or domain decisions.
    """
    from unittest.mock import patch

    from app.core import ApiError
    from language_learning_api.routers import teachback
    from language_learning_api.services.teachback_handlers import owned_session

    assert fixture.ctx.db.engine.dialect.name == "mysql"
    fx = seed_terminal_graph(fixture)
    session_id, accepted = pending_report(fx)
    job_id = accepted.json()["data"]["job"]["id"]
    with fx.ctx.db.transaction() as stale:
        assert stale.connection().get_isolation_level() == "REPEATABLE READ"
        cached = stale.get(Job, job_id)
        assert cached.status == "queued"
        fx.provider.fail_next = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
        assert fx.ctx.jobs.run_once()
        assert cached.status == "queued", "The fixture must retain its old consistent-read snapshot"
        current = owned_session(stale, fx.owner, session_id, lock=True, active_task=False)
        assert teachback.no_busy_job(stale, current).status == "outcome_unknown"

    # Inverse worker lock ordering must produce a bounded, known conflict and
    # release every session/parent lock before the HTTP response is returned.
    with fx.ctx.db.transaction() as worker:
        worker.scalar(select(Job).where(Job.id == job_id).with_for_update())
        started = monotonic()
        blocked = abandon(fx, session_id)
        assert monotonic() - started < 5, "Abandon waited on the worker's job row"
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "JOB_IN_PROGRESS"
        assert worker.scalar(
            select(PlanTask.id).where(PlanTask.id == fx.teach["id"]).with_for_update(nowait=True)
        )
        assert worker.scalar(
            select(TeachbackSession.id).where(TeachbackSession.id == session_id).with_for_update(nowait=True)
        )

    def restart(job_id):
        return post(
            fx,
            f"/v1/jobs/{job_id}/restart",
            {"model_id": MODEL, "acknowledge_possible_duplicate_cost": True},
        )

    # A cached terminal pointer must not hide a concurrently accepted successor.
    with fx.ctx.db.transaction() as stale:
        cached_session = stale.get(TeachbackSession, session_id)
        assert cached_session.latest_job_id == job_id
        successor = restart(job_id)
        assert successor.status_code == 202, successor.text
        assert cached_session.latest_job_id == job_id
        current = owned_session(stale, fx.owner, session_id, lock=True, active_task=False)
        assert current.latest_job_id == successor.json()["data"]["job"]["id"]
        with pytest.raises(ApiError) as denied:
            teachback.no_busy_job(stale, current)
        assert denied.value.code == "JOB_IN_PROGRESS"
    assert abandon(fx, session_id).status_code == 409
    fx.provider.fail_next = ProviderFailure("INVALID_OUTPUT")
    assert fx.ctx.jobs.run_once()
    assert abandon(fx, session_id).status_code == 200

    # Restart owns Job X first. Abandon owns parents/session, then NOWAIT fails;
    # once it rolls back, restart can finish and no abandoned queued job exists.
    session_id, job_id = terminal_report(fx)
    acquired, release = Event(), Event()
    locked_owned = fx.ctx.jobs._locked_owned

    def pause_restart(db, selected_id, owner):
        job = locked_owned(db, selected_id, owner)
        if selected_id == job_id:
            acquired.set()
            assert release.wait(10), "Restart lock gate was not released"
        return job

    with patch.object(fx.ctx.jobs, "_locked_owned", pause_restart), ThreadPoolExecutor(max_workers=1) as pool:
        restarting = pool.submit(restart, job_id)
        try:
            assert acquired.wait(5)
            blocked = abandon(fx, session_id)
            assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "JOB_IN_PROGRESS"
        finally:
            release.set()
        successor = restarting.result(timeout=5)
    assert successor.status_code == 202, successor.text
    assert abandon(fx, session_id).status_code == 409
    with fx.ctx.db.transaction() as db:
        session = db.get(TeachbackSession, session_id)
        assert session.status == "report_pending"
        assert db.get(Job, session.latest_job_id).status == "queued"
    fx.provider.fail_next = ProviderFailure("INVALID_OUTPUT")
    assert fx.ctx.jobs.run_once()
    assert abandon(fx, session_id).status_code == 200

    # Abandon owns parents/session/current Job X first. The competing restart
    # waits at its real job lock, then observes the committed abandoned session.
    session_id, job_id = terminal_report(fx)
    acquired, release, restart_entered = Event(), Event(), Event()
    latest = teachback.latest_job

    def pause_abandon(db, session):
        job = latest(db, session)
        if session.id == session_id:
            acquired.set()
            assert release.wait(10), "Abandon lock gate was not released"
        return job

    def observe_restart(db, selected_id, owner):
        restart_entered.set()
        return locked_owned(db, selected_id, owner)

    before = preserved_history(fx, session_id)
    with (
        patch.object(teachback, "latest_job", pause_abandon),
        patch.object(fx.ctx.jobs, "_locked_owned", observe_restart),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        abandoning = pool.submit(abandon, fx, session_id)
        try:
            assert acquired.wait(5)
            restarting = pool.submit(restart, job_id)
            assert restart_entered.wait(5)
        finally:
            release.set()
        abandoned, rejected = abandoning.result(timeout=5), restarting.result(timeout=5)
    assert abandoned.status_code == 200, abandoned.text
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "STALE_JOB", rejected.text
    assert abandon(fx, session_id).json() == abandoned.json()
    assert preserved_history(fx, session_id) == before
    assert fx.ctx.jobs.run_once() is False
