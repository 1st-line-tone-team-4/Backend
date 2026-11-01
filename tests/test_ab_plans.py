"""Canonical A plans and B task boundary; all data and providers are local fixtures."""

from datetime import timedelta
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError

from app.a_gateway import CanonicalTaskGateway, lock_owned_task, update_solve_progress
from app.core import ApiError, api_error_handler, uid, utcnow
from app.db import Database
from app.models import (
    Concept,
    ConceptVersion,
    ExamGoal,
    IdempotencyRequest,
    Job,
    Material,
    MaterialUnit,
    PlanTask,
    PlanTaskConcept,
    Question,
    QuestionAttempt,
    QuestionSet,
    QuestionSetItem,
    StudyPlan,
    TeachbackSession,
    User,
)
from language_learning_api.routers import exam_goals, study_plans
from tests.support import FixtureAuth

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts/openapi-1.3.0.json").read_text("utf-8")
)
OWNER = {"Authorization": "Bearer test-owner-token"}
OTHER = {"Authorization": "Bearer test-other-token"}


@pytest.fixture
def plans(tmp_path):
    db = Database("sqlite:///" + (tmp_path / "plans.sqlite3").as_posix())
    db.create_schema()
    auth = FixtureAuth()
    ctx = SimpleNamespace(
        db=db,
        auth=auth,
        task_gateway=CanonicalTaskGateway(),
        settings=SimpleNamespace(signing_secret="local-test-signing-secret-" * 3),
    )
    app = FastAPI()
    app.state.context = ctx
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(exam_goals.router)
    app.include_router(study_plans.router)
    with db.transaction() as session:
        for token, owner in auth.tokens.items():
            session.add(
                User(
                    id=owner,
                    auth_issuer="test-only",
                    auth_subject=token,
                    timezone="Asia/Seoul",
                    created_at=utcnow(),
                )
            )
    with TestClient(app, headers=OWNER) as client:
        yield SimpleNamespace(
            ctx=ctx,
            app=app,
            client=client,
            auth=auth,
            owner=auth.tokens["test-owner-token"],
            other=auth.tokens["test-other-token"],
        )
    db.engine.dispose()


def validate(response, schema):
    schema = {
        "ExamGoalResponse": "ExamGoal",
        "StudyPlanResponse": "StudyPlan",
        "TodayResponse": "TodaySummary",
    }[schema]
    Draft202012Validator(
        {"$ref": "#/components/schemas/" + schema, "components": CONTRACT["components"]},
        format_checker=FormatChecker(),
    ).validate(response.json()["data"])


def post(fixture, path, body=None, key=None, headers=None):
    return fixture.client.post(path, json=body, headers={"Idempotency-Key": key or uid(), **(headers or {})})


def goal(fixture, days=10, *, headers=None):
    response = post(
        fixture,
        "/v1/exam-goals",
        {
            "title": "Momentum exam",
            "exam_type": "school_exam",
            "exam_date": (exam_goals.today_seoul() + timedelta(days=days)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    validate(response, "ExamGoalResponse")
    return response.json()["data"]


def material(fixture, *, owner=None, confirmed=True):
    owner = owner or fixture.owner
    mid, unit_id, cid, vid = uid(), uid(), uid(), uid()
    with fixture.ctx.db.transaction() as session:
        session.add(
            Material(
                id=mid,
                user_id=owner,
                filename="source.txt",
                mime_type="text/plain",
                size_bytes=8,
                storage_key="test/" + mid,
                processing_status="ready",
                archived=False,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.flush()
        session.add(MaterialUnit(id=unit_id, material_id=mid, ordinal=1, title="Momentum"))
        session.flush()
        session.add(Concept(id=cid, material_id=mid, unit_id=unit_id, created_at=utcnow()))
        session.flush()
        session.add(
            ConceptVersion(
                id=vid,
                concept_id=cid,
                version_no=1,
                name="Conservation",
                summary="No external force.",
                is_confirmed=confirmed,
                confirmed_at=utcnow() if confirmed else None,
                created_at=utcnow(),
            )
        )
    return SimpleNamespace(id=mid, unit_id=unit_id, concept_id=cid, version_id=vid)


def plan(fixture, *, goal_row=None, material_row=None):
    goal_row = goal_row or goal(fixture)
    material_row = material_row or material(fixture)
    response = post(
        fixture, "/v1/study-plans", {"exam_goal_id": goal_row["id"], "material_ids": [material_row.id]}
    )
    assert response.status_code == 201, response.text
    validate(response, "StudyPlanResponse")
    return response.json()["data"], material_row


def job(session, owner, kind, resource_type, resource_id, *, status="succeeded"):
    row = Job(
        id=uid(),
        user_id=owner,
        kind=kind,
        status=status,
        resource_type=resource_type,
        resource_id=resource_id,
        request_fingerprint=uid().replace("-", "") * 2,
        request_payload={},
        model_id="test:grounded",
        attempt_no=1,
        retryable=False,
        fencing_token=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def graded_attempt(fixture, task_id, content, *, question_id=None, set_id=None, status="graded"):
    with fixture.ctx.db.transaction() as session:
        if set_id is None:
            set_id = uid()
            generation = job(session, fixture.owner, "questions", "question_set", set_id)
            session.add(
                QuestionSet(
                    id=set_id,
                    user_id=fixture.owner,
                    unit_id=content.unit_id,
                    task_id=task_id,
                    difficulty="easy",
                    requested_count=5,
                    status="ready",
                    job_id=generation.id,
                    created_at=utcnow(),
                )
            )
            session.flush()
        if question_id is None:
            question_id = uid()
            session.add(
                Question(
                    id=question_id,
                    user_id=fixture.owner,
                    question_type="short_answer",
                    prompt="When?",
                    options=[],
                    answer_key={"type": "short_answer", "text": "zero"},
                    public_explanation="Zero external force.",
                    accepted_answers=[],
                    difficulty="easy",
                    created_at=utcnow(),
                )
            )
            session.flush()
            ordinal = (
                session.scalar(
                    select(func.max(QuestionSetItem.ordinal)).where(QuestionSetItem.set_id == set_id)
                )
                or 0
            ) + 1
            session.add(QuestionSetItem(set_id=set_id, question_id=question_id, ordinal=ordinal))
            session.flush()
        aid = uid()
        grading = job(
            session,
            fixture.owner,
            "grade",
            "attempt",
            aid,
            status="succeeded" if status == "graded" else "queued",
        )
        session.add(
            QuestionAttempt(
                id=aid,
                user_id=fixture.owner,
                question_id=question_id,
                question_set_id=set_id,
                answer={"type": "short_answer", "text": "wrong"},
                status=status,
                is_correct=False if status == "graded" else None,
                grading_feedback="Review the condition.",
                correct_answer={"type": "short_answer", "text": "zero"},
                explanation="Zero external force.",
                job_id=grading.id,
                answered_at=utcnow(),
            )
        )
        session.flush()
        if status == "graded":
            update_solve_progress(session, fixture.owner, task_id)
    return set_id, question_id, aid


def test_canonical_plan_pins_counts_generation_boundary_and_owner(plans):
    created, content = plan(plans)
    assert [task["task_type"] for task in created["tasks"]] == ["solve", "teach_back", "review"]
    assert [task["target_count"] for task in created["tasks"]] == [5, 1, 1]
    assert [task["completed_count"] for task in created["tasks"]] == [0, 0, 0]
    assert all(task["concept_version_ids"] == [content.version_id] for task in created["tasks"])
    solve = created["tasks"][0]
    with plans.ctx.db.transaction() as session:
        gateway = plans.ctx.task_gateway
        gateway.validate_question_task(
            session, plans.owner, solve["id"], content.unit_id, [content.version_id], count=5
        )
        gateway.validate_question_task(
            session, plans.owner, solve["id"], content.unit_id, [content.version_id], count=5
        )
        assert gateway.read_question_task(session, plans.other, solve["id"]) is None
        assert gateway.read_question_task(session, plans.owner, solve["id"]).completed_count == 0
        assert gateway.material_in_use(session, plans.owner, content.id)
        assert session.scalar(select(func.count()).select_from(PlanTaskConcept)) == 3
    assert plans.client.get("/v1/study-plans/" + created["id"], headers=OTHER).status_code == 404
    assert (
        plans.client.patch(
            "/v1/plan-tasks/" + solve["id"], json={"status": "skipped"}, headers=OTHER
        ).status_code
        == 404
    )
    assert plans.client.get("/v1/study-plans/" + created["id"].upper()).status_code == 200
    assert (
        plans.client.get(
            "/v1/today", params={"exam_goal_id": created["exam_goal_id"]}, headers=OTHER
        ).status_code
        == 404
    )


def test_goal_dates_idempotency_and_scoped_cursors(plans):
    key = uid()
    body = {"title": "Goal", "exam_type": "other", "exam_date": exam_goals.today_seoul().isoformat()}
    first = post(plans, "/v1/exam-goals", body, key)
    assert first.status_code == 201
    assert post(plans, "/v1/exam-goals", body, key).json() == first.json()
    assert post(plans, "/v1/exam-goals", {**body, "title": "Different"}, key).status_code == 409
    assert (
        post(
            plans,
            "/v1/exam-goals",
            {**body, "exam_date": (exam_goals.today_seoul() - timedelta(days=1)).isoformat()},
        ).status_code
        == 422
    )
    assert (
        plans.client.patch(
            "/v1/exam-goals/" + first.json()["data"]["id"],
            json={"exam_date": (exam_goals.today_seoul() - timedelta(days=1)).isoformat()},
        ).status_code
        == 422
    )
    distant = goal(plans, 181)
    assert distant["course_preview"] == {
        "days_remaining": 181,
        "eligible": False,
        "reason": "EXAM_TOO_FAR",
        "stages": [],
    }
    content = material(plans)
    rejected = post(plans, "/v1/study-plans", {"exam_goal_id": distant["id"], "material_ids": [content.id]})
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "EXAM_TOO_FAR"
    cursor = plans.client.get("/v1/exam-goals", params={"limit": 1}).json()["next_cursor"]
    assert cursor
    assert plans.client.get("/v1/exam-goals", params={"cursor": cursor}, headers=OTHER).status_code == 422
    assert plans.client.get("/v1/study-plans", params={"cursor": cursor}).status_code == 422
    with plans.ctx.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(IdempotencyRequest)) == 2


def test_current_confirmed_version_only_and_plan_idempotency(plans):
    content = material(plans)
    with plans.ctx.db.transaction() as session:
        session.add(
            ConceptVersion(
                id=uid(),
                concept_id=content.concept_id,
                version_no=2,
                name="New draft",
                summary=None,
                is_confirmed=False,
                confirmed_at=None,
                created_at=utcnow(),
            )
        )
    target = goal(plans)
    body = {"exam_goal_id": target["id"], "material_ids": [content.id]}
    rejected = post(plans, "/v1/study-plans", body)
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "NO_CONFIRMED_CONCEPTS"
    with plans.ctx.db.transaction() as session:
        current = session.scalar(
            select(ConceptVersion).where(
                ConceptVersion.concept_id == content.concept_id, ConceptVersion.version_no == 2
            )
        )
        current.is_confirmed, current.summary, current.confirmed_at = True, "Confirmed", utcnow()
        vid = current.id
    key = uid()
    created = post(plans, "/v1/study-plans", body, key)
    assert created.status_code == 201, created.text
    assert created.json()["data"]["tasks"][0]["concept_version_ids"] == [vid]
    assert post(plans, "/v1/study-plans", body, key).json() == created.json()
    assert post(plans, "/v1/study-plans", body).status_code == 409


def test_progress_is_distinct_graded_questions_and_reschedule_remaining(plans):
    created, content = plan(plans)
    solve, teach, review = created["tasks"]
    sid, qid, _ = graded_attempt(plans, solve["id"], content)
    graded_attempt(plans, solve["id"], content, question_id=qid, set_id=sid)
    graded_attempt(plans, solve["id"], content, set_id=sid)
    fetched = plans.client.get("/v1/study-plans/" + created["id"])
    assert fetched.json()["data"]["tasks"][0]["completed_count"] == 2
    assert fetched.json()["data"]["tasks"][0]["status"] == "in_progress"
    assert (
        plans.client.patch("/v1/plan-tasks/" + solve["id"], json={"status": "completed"}).status_code == 422
    )
    assert (
        plans.client.patch("/v1/plan-tasks/" + review["id"], json={"status": "completed"}).status_code == 200
    )
    key = uid()
    rescheduled = post(plans, "/v1/study-plans/" + created["id"] + "/reschedule", key=key)
    assert rescheduled.status_code == 201, rescheduled.text
    validate(rescheduled, "StudyPlanResponse")
    new = rescheduled.json()["data"]
    assert new["revision"] == 2 and [t["target_count"] for t in new["tasks"]] == [3, 1]
    assert all(t["completed_count"] == 0 for t in new["tasks"])
    assert new["tasks"][0]["concept_version_ids"] == solve["concept_version_ids"]
    assert (
        post(plans, "/v1/study-plans/" + created["id"] + "/reschedule", key=key).json() == rescheduled.json()
    )
    assert post(plans, "/v1/study-plans/" + new["id"] + "/reschedule", key=key).status_code == 409
    assert post(plans, "/v1/study-plans/" + created["id"] + "/reschedule").status_code == 409
    assert plans.client.patch("/v1/plan-tasks/" + review["id"], json={"status": "pending"}).status_code == 409
    with plans.ctx.db.transaction() as session:
        state = plans.ctx.task_gateway.read_question_task(session, plans.owner, solve["id"])
        assert not state.active_revision and state.completed_count == 2
        with pytest.raises(ApiError) as exc:
            plans.ctx.task_gateway.validate_question_task(
                session, plans.owner, solve["id"], content.unit_id, [content.version_id], count=1
            )
        assert exc.value.code == "STALE_TASK"
        assert plans.ctx.task_gateway.material_in_use(session, plans.owner, content.id)
    cursor = plans.client.get("/v1/study-plans", params={"limit": 1}).json()["next_cursor"]
    assert (
        plans.client.get("/v1/study-plans", params={"cursor": cursor, "status": "active"}).status_code == 422
    )


def test_busy_grade_blocks_reschedule_and_skip_without_partial_write(plans):
    created, content = plan(plans)
    solve = created["tasks"][0]
    graded_attempt(plans, solve["id"], content, status="grading")
    response = post(plans, "/v1/study-plans/" + created["id"] + "/reschedule")
    assert response.status_code == 409 and response.json()["error"]["code"] == "JOB_IN_PROGRESS"
    assert plans.client.patch("/v1/plan-tasks/" + solve["id"], json={"status": "skipped"}).status_code == 409
    with plans.ctx.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(StudyPlan)) == 1
        assert session.get(StudyPlan, created["id"]).status == "active"
        assert session.get(PlanTask, solve["id"]).status == "pending"


def test_today_scope_goal_patch_immutable_and_skipped_denominator(plans):
    created, content = plan(plans, goal_row=goal(plans, 0))
    solve, teach, review = created["tasks"]
    assert plans.client.patch("/v1/plan-tasks/" + solve["id"], json={"status": "skipped"}).status_code == 200
    assert plans.client.patch("/v1/plan-tasks/" + teach["id"], json={"status": "skipped"}).status_code == 200
    assert (
        plans.client.patch("/v1/plan-tasks/" + review["id"], json={"status": "completed"}).status_code == 200
    )
    today = plans.client.get("/v1/today")
    validate(today, "TodayResponse")
    assert today.json()["data"]["progress"] == 1
    future = (exam_goals.today_seoul() + timedelta(days=12)).isoformat()
    assert (
        plans.client.patch(
            "/v1/exam-goals/" + created["exam_goal_id"], json={"exam_date": future}
        ).status_code
        == 200
    )
    assert (
        plans.client.get("/v1/study-plans/" + created["id"]).json()["data"]["tasks"][-1]["due_date"]
        == review["due_date"]
    )
    assert (
        plans.client.patch("/v1/exam-goals/" + created["exam_goal_id"], json={"status": "paused"}).status_code
        == 200
    )
    scoped = plans.client.get("/v1/today", params={"exam_goal_id": created["exam_goal_id"]}).json()["data"]
    assert scoped["exam_goal_id"] == created["exam_goal_id"] and scoped["study_plan_id"] is None
    assert scoped["tasks"] == [] and scoped["progress"] == 0 and scoped["days_remaining"] == 12
    assert plans.client.get("/v1/today").json()["data"]["exam_goal_id"] is None
    with plans.ctx.db.transaction() as session:
        with pytest.raises(ApiError) as exc:
            lock_owned_task(session, plans.owner, solve["id"])
        assert exc.value.code == "STALE_TASK"


def test_gateway_attempt_history_is_private_latest_and_graded(plans):
    created, content = plan(plans)
    solve = created["tasks"][0]
    sid, qid, aid = graded_attempt(plans, solve["id"], content)
    _, _, later = graded_attempt(plans, solve["id"], content, set_id=sid, question_id=qid, status="grading")
    with plans.ctx.db.transaction() as session:
        gateway = plans.ctx.task_gateway
        history = gateway.read_question_attempts(session, plans.owner, sid, [qid])
        assert history[qid]["has_graded_attempt"]
        assert history[qid]["latest_attempt"]["id"] == later
        assert history[qid]["latest_attempt"]["result"] is None
        for owner, questions in [(plans.other, [qid]), (plans.owner, [uid()])]:
            with pytest.raises(ApiError) as exc:
                gateway.read_question_attempts(session, owner, sid, questions)
            assert exc.value.status == 404
        session.get(QuestionAttempt, later).answered_at -= timedelta(days=1)
        session.flush()
        graded = gateway.read_question_attempts(session, plans.owner, sid, [qid])[qid]["latest_attempt"]
        assert graded["id"] == aid and graded["result"]["is_correct"] is False
        assert graded["status"] == "graded" and graded["result"]["feedback"]


def test_material_use_includes_standalone_teachback_history(plans):
    content = material(plans)
    with plans.ctx.db.transaction() as session:
        assert not plans.ctx.task_gateway.material_in_use(session, plans.owner, content.id)
        session.add(
            TeachbackSession(
                id=uid(),
                user_id=plans.owner,
                concept_version_id=content.version_id,
                explanation_language="ko",
                student_persona="curious_beginner",
                status="abandoned",
                question_count=0,
                started_at=utcnow(),
                completed_at=None,
            )
        )
        session.flush()
        assert plans.ctx.task_gateway.material_in_use(session, plans.owner, content.id)
        with pytest.raises(ApiError) as exc:
            plans.ctx.task_gateway.material_in_use(session, plans.other, content.id)
        assert exc.value.status == 404


def test_task_limit_rolls_back_and_180_day_formula(plans, monkeypatch):
    target, content = goal(plans, 180), material(plans)
    body = {"exam_goal_id": target["id"], "material_ids": [content.id]}
    with monkeypatch.context() as patch:
        patch.setattr(study_plans, "MAX_TASKS", 2)
        rejected = post(plans, "/v1/study-plans", body)
        assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "RESOURCE_LIMIT_REACHED"
    with plans.ctx.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(StudyPlan)) == 0
    created = post(plans, "/v1/study-plans", body).json()["data"]
    expected = [(exam_goals.today_seoul() + timedelta(days=days)).isoformat() for days in (0, 60, 120)]
    assert [task["due_date"] for task in created["tasks"]] == expected
    with plans.ctx.db.transaction() as session:
        session.get(ExamGoal, target["id"]).exam_date = exam_goals.today_seoul() - timedelta(days=1)
    preview = plans.client.get("/v1/exam-goals").json()["data"][0]["course_preview"]
    assert preview == {"days_remaining": 0, "eligible": False, "reason": "EXAM_PASSED", "stages": []}
    # Existing plans retain explicit rescheduling semantics after the exam date.
    rescheduled = post(plans, "/v1/study-plans/" + created["id"] + "/reschedule")
    assert rescheduled.status_code == 201, rescheduled.text
    assert {task["due_date"] for task in rescheduled.json()["data"]["tasks"]} == {
        exam_goals.today_seoul().isoformat()
    }


def test_real_b_generation_reads_canonical_plan_without_completing(tmp_path):
    from tests.support import test_application as make_application
    from tests.test_e2e import confirmed

    fixture = make_application(tmp_path)
    fixture.owner = fixture.auth.tokens["test-owner-token"]
    fixture.ctx.task_gateway = CanonicalTaskGateway()
    try:
        with TestClient(fixture.app, headers=OWNER) as client:
            fixture.client = client
            mid, concept, version = confirmed(fixture)
            content = SimpleNamespace(
                id=mid, unit_id=concept["unit_id"], concept_id=concept["concept_id"], version_id=version["id"]
            )
            created, _ = plan(fixture, material_row=content)
            solve = created["tasks"][0]
            body = {
                "unit_id": content.unit_id,
                "task_id": solve["id"],
                "concept_version_ids": [content.version_id],
                "count": 5,
                "difficulty": "easy",
                "model_id": "test:grounded",
            }
            generated = post(fixture, "/v1/question-sets", body, headers={"X-AI-Key": "test-transient-key"})
            assert generated.status_code == 202, generated.text
            # Acceptance is fenced against archive, but already accepted work
            # must still finish using its immutable pinned content.
            assert client.patch("/v1/materials/" + mid, json={"archived": True}).status_code == 200
            assert fixture.ctx.jobs.run_once()
            set_id = generated.json()["data"]["job"]["resource_id"]
            result = client.get("/v1/question-sets/" + set_id)
            assert result.status_code == 200 and result.json()["data"]["status"] == "ready", result.text
            assert len(result.json()["data"]["items"]) == 5
            assert all(item["latest_attempt"] is None for item in result.json()["data"]["items"])
            fetched = client.get("/v1/study-plans/" + created["id"]).json()["data"]["tasks"][0]
            assert fetched["status"] == "pending" and fetched["completed_count"] == 0
            assert client.delete("/v1/materials/" + mid).status_code == 409
    finally:
        fixture.ctx.db.engine.dispose()


@pytest.mark.parametrize("resource", ["questions", "explanation"])
@pytest.mark.parametrize("changed", [{"archived": True}, {"processing_status": "failed"}])
def test_generation_acceptance_refreshes_material_after_stale_reads(tmp_path, monkeypatch, resource, changed):
    """SQLite regression for stale ORM state; the MySQL helper checks real RR waits."""
    import app.content as content_module
    from tests.support import test_application as make_application

    fixture = make_application(tmp_path)
    fixture.owner = fixture.auth.tokens["test-owner-token"]
    fixture.ctx.task_gateway = CanonicalTaskGateway()
    try:
        with TestClient(fixture.app, headers=OWNER) as client:
            fixture.client = client
            created, content = plan(fixture)

            def make_material_stale(db):
                stale = db.get(Material, content.id)
                assert not stale.archived and stale.processing_status == "ready"
                # Retain the identity-map object while changing persisted state
                # without ORM synchronization, as a lock wait can expose.
                db.info["fixture_stale_material"] = stale
                db.execute(
                    update(Material).where(Material.id == content.id).values(**changed),
                    execution_options={"synchronize_session": False},
                )

            if resource == "questions":
                validate_task = fixture.ctx.task_gateway.validate_question_task

                def task_boundary(db, *args, **kwargs):
                    validate_task(db, *args, **kwargs)
                    make_material_stale(db)

                monkeypatch.setattr(fixture.ctx.task_gateway, "validate_question_task", task_boundary)
                url = "/v1/question-sets"
                body = {
                    "unit_id": content.unit_id,
                    "task_id": created["tasks"][0]["id"],
                    "concept_version_ids": [content.version_id],
                    "count": 1,
                    "difficulty": "easy",
                    "model_id": "test:grounded",
                }
                operation = "createQuestionSet"
            else:
                get_version = content_module._version

                def version_boundary(db, *args, **kwargs):
                    version = get_version(db, *args, **kwargs)
                    make_material_stale(db)
                    return version

                monkeypatch.setattr(content_module, "_version", version_boundary)
                url = "/v1/learning-explanations"
                body = {
                    "concept_version_id": content.version_id,
                    "explanation_language": "en",
                    "model_id": "test:grounded",
                }
                operation = "createLearningExplanation"
            key = uid()
            rejected = post(fixture, url, body, key=key, headers={"X-AI-Key": "test-transient-key"})
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["error"]["code"] == "MATERIAL_NOT_READY"
            assert fixture.provider.calls == []
            with fixture.ctx.db.transaction() as db:
                assert db.scalar(select(func.count()).select_from(Job)) == 0
                assert db.scalar(select(func.count()).select_from(QuestionSet)) == 0
                assert db.get(IdempotencyRequest, (fixture.owner, operation, key)) is None
    finally:
        fixture.ctx.db.engine.dispose()


def test_generation_preserves_old_task_pins_and_explanation_versions(tmp_path):
    from tests.support import test_application as make_application

    fixture = make_application(tmp_path)
    fixture.owner = fixture.auth.tokens["test-owner-token"]
    fixture.ctx.task_gateway = CanonicalTaskGateway()
    try:
        with TestClient(fixture.app, headers=OWNER) as client:
            fixture.client = client
            created, content = plan(fixture)
            with fixture.ctx.db.transaction() as db:
                db.add(
                    ConceptVersion(
                        id=uid(),
                        concept_id=content.concept_id,
                        version_no=2,
                        name="New current",
                        summary="Updated source summary",
                        is_confirmed=True,
                        confirmed_at=utcnow(),
                        created_at=utcnow(),
                    )
                )
            body = {
                "unit_id": content.unit_id,
                "concept_version_ids": [content.version_id],
                "count": 1,
                "difficulty": "easy",
                "model_id": "test:grounded",
            }
            standalone = post(fixture, "/v1/question-sets", body, headers={"X-AI-Key": "test-transient-key"})
            assert (
                standalone.status_code == 422
                and standalone.json()["error"]["code"] == "CONCEPT_NOT_CONFIRMED"
            )
            pinned = post(
                fixture,
                "/v1/question-sets",
                {**body, "task_id": created["tasks"][0]["id"]},
                headers={"X-AI-Key": "test-transient-key"},
            )
            assert pinned.status_code == 202, pinned.text
            historical = post(
                fixture,
                "/v1/learning-explanations",
                {
                    "concept_version_id": content.version_id,
                    "explanation_language": "en",
                    "model_id": "test:grounded",
                },
                headers={"X-AI-Key": "test-transient-key"},
            )
            assert historical.status_code == 202, historical.text
    finally:
        fixture.ctx.db.engine.dispose()


def test_generation_job_nowait_contention_rolls_back_acceptance(tmp_path):
    from tests.support import test_application as make_application

    fixture = make_application(tmp_path)
    fixture.owner = fixture.auth.tokens["test-owner-token"]
    fixture.ctx.task_gateway = CanonicalTaskGateway()

    def locked_job(connection, cursor, statement, parameters, context, many):
        if "FROM ai_jobs" in statement and "ai_jobs.kind" in statement and "ai_jobs.status IN" in statement:
            raise OperationalError(statement, parameters, Exception(3572, "fixture NOWAIT contention"))

    try:
        with TestClient(fixture.app, headers=OWNER) as client:
            fixture.client = client
            created, content = plan(fixture)
            event.listen(fixture.ctx.db.engine, "before_cursor_execute", locked_job)
            key = uid()
            rejected = post(
                fixture,
                "/v1/question-sets",
                {
                    "unit_id": content.unit_id,
                    "task_id": created["tasks"][0]["id"],
                    "concept_version_ids": [content.version_id],
                    "count": 1,
                    "difficulty": "easy",
                    "model_id": "test:grounded",
                },
                key=key,
                headers={"X-AI-Key": "test-transient-key"},
            )
            assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "JOB_IN_PROGRESS"
            with fixture.ctx.db.transaction() as db:
                assert db.scalar(select(func.count()).select_from(Job)) == 0
                assert db.scalar(select(func.count()).select_from(QuestionSet)) == 0
                assert db.get(IdempotencyRequest, (fixture.owner, "createQuestionSet", key)) is None
    finally:
        event.remove(fixture.ctx.db.engine, "before_cursor_execute", locked_job)
        fixture.ctx.db.engine.dispose()


def assert_mysql_generation_acceptance_after_lock_wait(fixture):
    """Archive and re-confirmation must win over a request's earlier RR snapshot."""
    assert fixture.ctx.db.engine.dialect.name == "mysql"
    fixture.owner = fixture.auth.tokens["test-owner-token"]
    original_gateway = fixture.ctx.task_gateway
    fixture.ctx.task_gateway = CanonicalTaskGateway()
    try:
        for mutation in ("archive", "current_version"):
            created, content = plan(fixture)
            task_id = created["tasks"][0]["id"]
            body = {
                "unit_id": content.unit_id,
                "concept_version_ids": [content.version_id],
                "count": 1,
                "difficulty": "easy",
                "model_id": "test:grounded",
            }
            if mutation == "archive":
                body["task_id"] = task_id
            waiting, responses, failures = Event(), [], []
            table = "exam_goals" if mutation == "archive" else "materials"

            def reached_lock(connection, cursor, statement, parameters, context, many):
                if f"FROM {table}" in statement and "FOR UPDATE" in statement:
                    waiting.set()

            key = uid()

            def generate():
                try:
                    responses.append(
                        post(
                            fixture,
                            "/v1/question-sets",
                            body,
                            key=key,
                            headers={"X-AI-Key": "test-transient-key"},
                        )
                    )
                except BaseException as exc:
                    failures.append(exc)

            thread = Thread(target=generate, daemon=True)
            try:
                with fixture.ctx.db.transaction() as writer:
                    model, identity = (
                        (ExamGoal, created["exam_goal_id"])
                        if mutation == "archive"
                        else (Material, content.id)
                    )
                    writer.scalar(select(model).where(model.id == identity).with_for_update())
                    event.listen(fixture.ctx.db.engine, "before_cursor_execute", reached_lock)
                    thread.start()
                    assert waiting.wait(5), "Generation did not reach the expected lock"
                    assert thread.is_alive(), "Generation failed to wait for the owning lock"
                    if mutation == "archive":
                        # A material lock acquired before the task lock would
                        # block this independent request and fail the timeout.
                        archived = fixture.client.patch(
                            "/v1/materials/" + content.id, json={"archived": True}, timeout=3
                        )
                        assert archived.status_code == 200, archived.text
                    else:
                        writer.add(
                            ConceptVersion(
                                id=uid(),
                                concept_id=content.concept_id,
                                version_no=2,
                                name="New current",
                                summary="Updated source summary",
                                is_confirmed=True,
                                confirmed_at=utcnow(),
                                created_at=utcnow(),
                            )
                        )
                        writer.flush()
            finally:
                if event.contains(fixture.ctx.db.engine, "before_cursor_execute", reached_lock):
                    event.remove(fixture.ctx.db.engine, "before_cursor_execute", reached_lock)
                if thread.ident is not None:
                    thread.join(8)
            assert not thread.is_alive() and not failures
            assert responses[0].status_code == 422, responses[0].text
            assert responses[0].json()["error"]["code"] == (
                "MATERIAL_NOT_READY" if mutation == "archive" else "CONCEPT_NOT_CONFIRMED"
            )
            with fixture.ctx.db.transaction() as db:
                assert db.get(IdempotencyRequest, (fixture.owner, "createQuestionSet", key)) is None
                assert (
                    db.scalar(
                        select(func.count()).select_from(QuestionSet).where(QuestionSet.task_id == task_id)
                    )
                    == 0
                )
        # The paid worker owns Job first, then needs the version parent lock
        # when inserting generated QuestionConcept rows. Acceptance must not
        # wait for that Job while retaining its version lock.
        _, content = plan(fixture)
        body = {
            "unit_id": content.unit_id,
            "concept_version_ids": [content.version_id],
            "count": 1,
            "difficulty": "easy",
            "model_id": "test:grounded",
        }
        with fixture.ctx.db.transaction() as db:
            queued = job(db, fixture.owner, "questions", "question_set", uid(), status="queued")
            queued.request_payload = {key: value for key, value in body.items() if key != "model_id"}
            queued.request_payload["task_id"] = None
            queued_id = queued.id
        key = uid()
        with fixture.ctx.db.transaction() as worker:
            worker.scalar(select(Job).where(Job.id == queued_id).with_for_update())
            rejected = post(
                fixture, "/v1/question-sets", body, key=key, headers={"X-AI-Key": "test-transient-key"}
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["error"]["code"] == "JOB_IN_PROGRESS"
            worker.scalar(
                select(ConceptVersion)
                .where(ConceptVersion.id == content.version_id)
                .with_for_update(nowait=True)
            )
        with fixture.ctx.db.transaction() as db:
            assert db.get(IdempotencyRequest, (fixture.owner, "createQuestionSet", key)) is None
    finally:
        fixture.ctx.task_gateway = original_gateway


def assert_mysql_plan_reschedule_completion(fixture):
    """Called by the disposable MySQL integration fixture, never by SQLite.

    Exercise an old consistent-read snapshot before waiting on a completed
    review task's goal lock, then verify the successor excludes that task.
    """
    assert fixture.ctx.db.engine.dialect.name == "mysql"
    fixture.owner = fixture.auth.tokens["test-owner-token"]
    fixture.other = fixture.auth.tokens["test-other-token"]
    created, _ = plan(fixture)
    review = created["tasks"][2]
    waiting = Event()
    results, failures = [], []

    def reached_goal_lock(connection, cursor, statement, parameters, context, many):
        if "FROM exam_goals" in statement and "FOR UPDATE" in statement:
            waiting.set()

    def reschedule():
        try:
            results.append(post(fixture, "/v1/study-plans/" + created["id"] + "/reschedule"))
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=reschedule, daemon=True)
    try:
        with fixture.ctx.db.transaction() as session:
            _, task = lock_owned_task(session, fixture.owner, review["id"])
            event.listen(fixture.ctx.db.engine, "before_cursor_execute", reached_goal_lock)
            thread.start()
            assert waiting.wait(5), "Reschedule did not reach the goal lock"
            assert thread.is_alive(), "Reschedule did not wait for completion"
            task.status, task.completed_at = "completed", utcnow()
            session.flush()
        thread.join(8)
        assert not thread.is_alive() and not failures
        assert results[0].status_code == 201, results[0].text
        successor = results[0].json()["data"]
        assert successor["revision"] == 2
        assert [task["task_type"] for task in successor["tasks"]] == ["solve", "teach_back"]
        with fixture.ctx.db.transaction() as session:
            assert session.get(StudyPlan, created["id"]).status == "superseded"
            assert session.get(PlanTask, review["id"]).status == "completed"
    finally:
        event.remove(fixture.ctx.db.engine, "before_cursor_execute", reached_goal_lock)
        thread.join(8)

    # A worker holds Job before trying goal. NOWAIT must fail fast rather than
    # make rescheduling hold goal while waiting for that same Job.
    created, content = plan(fixture)
    _, _, aid = graded_attempt(fixture, created["tasks"][0]["id"], content, status="grading")
    with fixture.ctx.db.transaction() as worker:
        attempt = worker.get(QuestionAttempt, aid)
        worker.scalar(select(Job).where(Job.id == attempt.job_id).with_for_update())
        response = post(fixture, "/v1/study-plans/" + created["id"] + "/reschedule")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "JOB_IN_PROGRESS"
        worker.scalar(
            select(ExamGoal).where(ExamGoal.id == created["exam_goal_id"]).with_for_update(nowait=True)
        )
