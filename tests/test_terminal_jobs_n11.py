"""N11: terminal job history must not permanently lock unrelated learning actions.

All API paths use loopback HTTP, real fixture JWTs and the canonical worker.
Only the provider is deterministic; the BYOK value is a test-only constant.
"""

from time import monotonic
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core import ApiError, uid, utcnow
from app.models import (
    Concept,
    ConceptChecklistItem,
    ConceptVersion,
    ConceptVersionEvidence,
    Job,
    Material,
    MaterialChunk,
    MaterialUnit,
    QuestionAttempt,
)
from app.providers import ProviderFailure
from language_learning_api.routers.exam_goals import today_seoul
from tests.ab_support import MODEL, ab_http_application
from tests.test_ab_e2e import post, run_job, validate


def seed_terminal_graph(fx):
    """Create only source fixtures directly; plans, sets and attempts use HTTP."""
    fx.owner = fx.client.get("/v1/me").json()["data"]["id"]
    ids = {key: uid() for key in ("material", "unit", "chunk", "concept", "version", "item")}
    now = utcnow()
    with fx.ctx.db.transaction() as db:
        db.add(
            Material(
                id=ids["material"],
                user_id=fx.owner,
                filename="terminal-history.txt",
                mime_type="text/plain",
                size_bytes=54,
                storage_key="fixture/" + uid(),
                processing_status="ready",
                archived=False,
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(MaterialUnit(id=ids["unit"], material_id=ids["material"], ordinal=1, title="Momentum"))
        db.add(
            MaterialChunk(
                id=ids["chunk"],
                material_id=ids["material"],
                ordinal=1,
                page_number=1,
                content="Momentum is conserved when total external force is zero.",
                created_at=now,
            )
        )
        db.flush()
        db.add(Concept(id=ids["concept"], material_id=ids["material"], unit_id=ids["unit"], created_at=now))
        db.flush()
        db.add(
            ConceptVersion(
                id=ids["version"],
                concept_id=ids["concept"],
                version_no=1,
                name="Momentum conservation",
                summary="Zero external force conserves momentum.",
                is_confirmed=True,
                confirmed_at=now,
                created_at=now,
            )
        )
        db.flush()
        db.add(
            ConceptChecklistItem(
                id=ids["item"],
                concept_version_id=ids["version"],
                ordinal=1,
                criterion="Explain the zero external force condition.",
            )
        )
        db.add(ConceptVersionEvidence(concept_version_id=ids["version"], chunk_id=ids["chunk"]))
    goal = post(
        fx,
        "/v1/exam-goals",
        {"title": "Terminal recovery", "exam_type": "other", "exam_date": today_seoul().isoformat()},
    )
    assert goal.status_code == 201, goal.text
    plan = post(
        fx, "/v1/study-plans", {"exam_goal_id": goal.json()["data"]["id"], "material_ids": [ids["material"]]}
    )
    assert plan.status_code == 201, plan.text
    fx.plan = plan.json()["data"]
    fx.solve = next(task for task in fx.plan["tasks"] if task["task_type"] == "solve")
    fx.teach = next(task for task in fx.plan["tasks"] if task["task_type"] == "teach_back")
    accepted = post(
        fx,
        "/v1/question-sets",
        {
            "unit_id": ids["unit"],
            "task_id": fx.solve["id"],
            "concept_version_ids": [ids["version"]],
            "count": 1,
            "difficulty": "easy",
            "model_id": MODEL,
        },
    )
    run_job(fx, accepted)
    fx.set_id = accepted.json()["data"]["job"]["resource_id"]
    question_set = fx.client.get("/v1/question-sets/" + fx.set_id)
    fx.question_id = question_set.json()["data"]["items"][0]["question"]["id"]
    fx.ids = ids
    return fx


@pytest.fixture
def terminal_env(tmp_path):
    with ab_http_application(tmp_path) as fx:
        yield seed_terminal_graph(fx)


def grade(fx, answer="First answer", *, key=None):
    return post(
        fx,
        f"/v1/questions/{fx.question_id}/attempts",
        {"question_set_id": fx.set_id, "model_id": MODEL, "answer": {"type": "short_answer", "text": answer}},
        key=key,
    )


def new_session(fx):
    return post(
        fx,
        "/v1/teachback-sessions",
        {
            "concept_version_id": fx.ids["version"],
            "task_id": fx.teach["id"],
            "explanation_language": "en",
            "student_persona": "curious_beginner",
        },
    )


def message(fx, session_id, content="Momentum is conserved without external force.", *, key=None):
    return post(
        fx,
        f"/v1/teachback-sessions/{session_id}/messages",
        {"content": content, "input_mode": "text", "model_id": MODEL},
        key=key,
    )


def fail_unknown(fx, accepted):
    assert accepted.status_code == 202, accepted.text
    fx.provider.fail_next = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    assert fx.ctx.jobs.run_once()
    job_id = accepted.json()["data"]["job"]["id"]
    with fx.ctx.db.transaction() as db:
        job = db.get(Job, job_id)
        assert job.status == "outcome_unknown" and job.dispatched_at is not None
        assert job.retryable is False and job.credential_ref is None
        return SimpleNamespace(
            id=job.id,
            resource_id=job.resource_id,
            dispatched_at=job.dispatched_at,
            fencing_token=job.fencing_token,
            request_payload=job.request_payload,
        )


def assert_unknown_history(fx, prior):
    with fx.ctx.db.transaction() as db:
        job = db.get(Job, prior.id)
        assert job.status == "outcome_unknown" and job.dispatched_at == prior.dispatched_at
        assert job.request_payload == prior.request_payload and job.fencing_token == prior.fencing_token
        assert job.credential_ref is None
        assert fx.ctx.jobs._guard(db, job.id, prior.fencing_token) is None


def assert_restart_stale(fx, job_id):
    reply = post(
        fx,
        f"/v1/jobs/{job_id}/restart",
        {"model_id": MODEL, "acknowledge_possible_duplicate_execution": True},
    )
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "STALE_JOB", reply.text


def skip(fx, task):
    return fx.client.patch("/v1/plan-tasks/" + task["id"], json={"status": "skipped"})


def replan(fx):
    return post(fx, "/v1/study-plans/" + fx.plan["id"] + "/reschedule", {})


def test_unknown_grade_history_allows_different_answer_skip_and_replan(terminal_env):
    fx = terminal_env
    prior = fail_unknown(fx, grade(fx))
    same = grade(fx)
    assert same.status_code == 409 and same.json()["error"]["code"] == "DUPLICATE_EXECUTION_ACK_REQUIRED"
    no_ack = post(
        fx,
        f"/v1/jobs/{prior.id}/restart",
        {"model_id": MODEL, "acknowledge_possible_duplicate_execution": False},
    )
    assert no_ack.status_code == 422
    run_job(fx, grade(fx, "A different learning answer"))
    assert_restart_stale(fx, prior.id)
    skipped, replanned = skip(fx, fx.solve), replan(fx)
    assert (skipped.status_code, replanned.status_code) == (200, 201), (
        f"terminal grade history blocked skip={skipped.text}; replan={replanned.text}"
    )
    validate(skipped, "/plan-tasks/{taskId}", "patch")
    validate(replanned, "/study-plans/{planId}/reschedule", "post")
    assert_unknown_history(fx, prior)
    with fx.ctx.db.transaction() as db:
        assert db.get(QuestionAttempt, prior.resource_id).status == "failed"


def test_unknown_message_allows_abandon_fresh_session_skip_and_replan(terminal_env):
    fx = terminal_env
    session_id = new_session(fx).json()["data"]["id"]
    prior = fail_unknown(fx, message(fx, session_id))
    active_duplicate = new_session(fx)
    assert (
        active_duplicate.status_code == 409
        and active_duplicate.json()["error"]["code"] == "ACTIVE_SESSION_EXISTS"
    )
    abandoned = fx.client.patch(f"/v1/teachback-sessions/{session_id}", json={"status": "abandoned"})
    if abandoned.status_code != 200:
        blocked_new, blocked_skip, blocked_replan = new_session(fx), skip(fx, fx.teach), replan(fx)
        pytest.fail(
            f"terminal message blocks abandon={abandoned.status_code}/{abandoned.text}; "
            f"new={blocked_new.status_code}; skip={blocked_skip.status_code}; replan={blocked_replan.status_code}"
        )
    fresh = new_session(fx)
    assert fresh.status_code == 201, fresh.text
    assert_restart_stale(fx, prior.id)
    fresh_id = fresh.json()["data"]["id"]
    assert (
        fx.client.patch(f"/v1/teachback-sessions/{fresh_id}", json={"status": "abandoned"}).status_code == 200
    )
    skipped, replanned = skip(fx, fx.teach), replan(fx)
    assert (skipped.status_code, replanned.status_code) == (200, 201), (skipped.text, replanned.text)
    assert_unknown_history(fx, prior)


def test_unknown_message_can_continue_with_new_message_without_redispatching_history(terminal_env):
    fx = terminal_env
    session_id = new_session(fx).json()["data"]["id"]
    key = uid()
    accepted = message(fx, session_id, key=key)
    prior = fail_unknown(fx, accepted)
    calls = len(fx.provider.calls)
    assert message(fx, session_id, key=key).json() == accepted.json()
    assert fx.ctx.jobs.run_once() is False and len(fx.provider.calls) == calls
    successor = message(fx, session_id, "Here is a new explanation of the force condition.")
    assert successor.status_code == 202, successor.text
    assert_restart_stale(fx, prior.id)
    run_job(fx, successor)
    assert len(fx.provider.calls) == calls + 1
    assert_unknown_history(fx, prior)


def test_unknown_message_can_finish_session_with_a_new_report(terminal_env):
    fx = terminal_env
    session_id = new_session(fx).json()["data"]["id"]
    prior = fail_unknown(fx, message(fx, session_id))
    report = post(fx, f"/v1/teachback-sessions/{session_id}/complete", {"model_id": MODEL})
    assert report.status_code == 202, report.text
    assert_restart_stale(fx, prior.id)
    run_job(fx, report)
    assert fx.client.get(f"/v1/teachback-sessions/{session_id}").json()["data"]["status"] == "completed"
    assert_unknown_history(fx, prior)


@pytest.mark.parametrize("kind", ["grade", "message"])
@pytest.mark.parametrize("state", ["queued", "running"])
def test_queued_and_running_jobs_still_block_conflicting_actions(terminal_env, kind, state):
    fx = terminal_env
    if kind == "grade":
        accepted, task, session_id = grade(fx), fx.solve, None
    else:
        session_id = new_session(fx).json()["data"]["id"]
        accepted, task = message(fx, session_id), fx.teach
    assert accepted.status_code == 202
    job_id = accepted.json()["data"]["job"]["id"]
    if state == "running":
        claimed = fx.ctx.jobs._claim()
        assert claimed and claimed[0] == job_id
    calls = len(fx.provider.calls)
    assert skip(fx, task).status_code == 409
    assert replan(fx).status_code == 409
    if session_id:
        assert (
            fx.client.patch(f"/v1/teachback-sessions/{session_id}", json={"status": "abandoned"}).status_code
            == 409
        )
        assert message(fx, session_id, "Another explanation").status_code == 409
        assert (
            post(fx, f"/v1/teachback-sessions/{session_id}/complete", {"model_id": MODEL}).status_code == 409
        )
    with fx.ctx.db.transaction() as db:
        assert db.get(Job, job_id).status == state
    assert len(fx.provider.calls) == calls


def test_unknown_report_can_be_abandoned_without_losing_history(terminal_env):
    fx = terminal_env
    session_id = new_session(fx).json()["data"]["id"]
    run_job(fx, message(fx, session_id))
    accepted = post(fx, f"/v1/teachback-sessions/{session_id}/complete", {"model_id": MODEL})
    prior = fail_unknown(fx, accepted)
    again = post(fx, f"/v1/teachback-sessions/{session_id}/complete", {"model_id": MODEL})
    assert again.status_code == 202 and again.json()["data"]["job"]["id"] == prior.id
    assert again.json()["data"]["job"]["status"] == "outcome_unknown"
    assert new_session(fx).status_code == 409
    assert skip(fx, fx.teach).status_code == 409 and replan(fx).status_code == 409
    abandoned = fx.client.patch(f"/v1/teachback-sessions/{session_id}", json={"status": "abandoned"})
    assert abandoned.status_code == 200 and abandoned.json()["data"]["status"] == "abandoned"
    assert_restart_stale(fx, prior.id)
    assert skip(fx, fx.teach).status_code == 200 and replan(fx).status_code == 201
    assert_unknown_history(fx, prior)


def assert_mysql_terminal_history_current_reads(fixture):
    """Wire into the existing migrated, disposable MySQL AB HTTP fixture.

    Establish old RR snapshots explicitly. Locking domain reads must allow a
    now-terminal job and still reject a newly queued one; latest-job NOWAIT
    must yield a bounded 409 while a worker owns its row, then release cleanly.
    """
    from app.a_gateway import lock_owned_task
    from language_learning_api.routers.study_plans import _ensure_not_busy
    from language_learning_api.routers.teachback import no_busy_job
    from language_learning_api.services.teachback_handlers import owned_session

    assert fixture.ctx.db.engine.dialect.name == "mysql"
    fx = seed_terminal_graph(fixture)
    accepted = grade(fx)
    assert accepted.status_code == 202
    old_id = accepted.json()["data"]["job"]["id"]
    with fx.ctx.db.transaction() as stale:
        assert stale.connection().get_isolation_level() == "REPEATABLE READ"
        cached = stale.get(Job, old_id)
        assert cached.status == "queued"
        prior_grade = fail_unknown(fx, accepted)
        assert cached.status == "queued", "Fixture must retain a stale consistent-read snapshot"
        _, task = lock_owned_task(stale, fx.owner, fx.solve["id"])
        _ensure_not_busy(stale, fx.owner, [task])

    # A newly accepted attempt cannot disappear behind an older RR snapshot.
    with fx.ctx.db.transaction() as stale:
        assert stale.get(Job, old_id).status == "outcome_unknown"
        successor = grade(fx, "A new answer after unknown")
        assert successor.status_code == 202
        _, task = lock_owned_task(stale, fx.owner, fx.solve["id"])
        with pytest.raises(ApiError) as blocked:
            _ensure_not_busy(stale, fx.owner, [task])
        assert blocked.value.code == "JOB_IN_PROGRESS"
    run_job(fx, successor)
    assert_restart_stale(fx, old_id)
    assert skip(fx, fx.solve).status_code == 200
    assert_unknown_history(fx, prior_grade)

    session_id = new_session(fx).json()["data"]["id"]
    accepted = message(fx, session_id)
    job_id = accepted.json()["data"]["job"]["id"]
    with fx.ctx.db.transaction() as stale:
        cached = stale.get(Job, job_id)
        assert cached.status == "queued"
        prior_message = fail_unknown(fx, accepted)
        assert cached.status == "queued"
        session = owned_session(stale, fx.owner, session_id, lock=True)
        assert no_busy_job(stale, session).status == "outcome_unknown"

    with fx.ctx.db.transaction() as worker:
        worker.scalar(select(Job).where(Job.id == job_id).with_for_update())
        started = monotonic()
        blocked = fx.client.patch(f"/v1/teachback-sessions/{session_id}", json={"status": "abandoned"})
        assert monotonic() - started < 5, "NOWAIT must avoid a job -> goal/session deadlock"
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "JOB_IN_PROGRESS"
    assert fx.client.get(f"/v1/teachback-sessions/{session_id}").json()["data"]["status"] == "active"
    assert (
        fx.client.patch(f"/v1/teachback-sessions/{session_id}", json={"status": "abandoned"}).status_code
        == 200
    )
    assert_restart_stale(fx, job_id)
    assert skip(fx, fx.teach).status_code == 200
    assert replan(fx).status_code == 201
    assert_unknown_history(fx, prior_message)
