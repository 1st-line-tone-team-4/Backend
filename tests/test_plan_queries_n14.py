"""N14: bounded canonical plan reads over real JWT-authenticated loopback HTTP."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert, select

from app.core import uid, utcnow
from app.models import (
    Concept,
    ConceptVersion,
    ExamGoal,
    Job,
    PlanTask,
    PlanTaskConcept,
    QuestionAttempt,
    StudyPlan,
    StudyPlanMaterial,
    TeachbackSession,
)
from language_learning_api.routers.exam_goals import today_seoul
from tests.ab_support import ab_http_application
from tests.test_ab_plans import graded_attempt, material, validate


@pytest.fixture
def query_env(tmp_path):
    with ab_http_application(tmp_path) as fixture:
        fixture.owner = fixture.client.get("/v1/me").json()["data"]["id"]
        fixture.other = fixture.client.get("/v1/me", headers=fixture.other_headers).json()["data"]["id"]
        yield fixture


@contextmanager
def domain_selects(fixture):
    """Count route SELECTs, excluding the unchanged JWT user lookup."""
    statements = []

    def count(connection, cursor, statement, parameters, context, many):
        normalized = statement.lstrip().upper()
        if normalized.startswith("SELECT") and "FROM USERS" not in normalized:
            statements.append(statement)

    event.listen(fixture.ctx.db.engine, "before_cursor_execute", count)
    try:
        yield statements
    finally:
        event.remove(fixture.ctx.db.engine, "before_cursor_execute", count)


def seed_plan(fixture, count, *, content=None, owner=None):
    """Bulk canonical seed keeps measurement independent of setup INSERT cost."""
    owner = owner or fixture.owner
    content = content or material(fixture, owner=owner)
    goal_id, plan_id, now = uid(), uid(), utcnow()
    tasks = [
        {
            "id": uid(),
            "study_plan_id": plan_id,
            "ordinal": index + 1,
            "due_date": today_seoul(),
            "task_type": ("solve", "teach_back", "review")[index % 3],
            "target_count": 5 if index % 3 == 0 else 1,
            "status": "pending",
            "completed_at": None,
            "created_at": now,
        }
        for index in range(count)
    ]
    with fixture.ctx.db.transaction() as db:
        db.add(
            ExamGoal(
                id=goal_id,
                user_id=owner,
                title="Query budget exam",
                exam_type="other",
                exam_date=today_seoul(),
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(
            StudyPlan(
                id=plan_id, user_id=owner, exam_goal_id=goal_id, revision=1, status="active", generated_at=now
            )
        )
        db.flush()
        db.add(StudyPlanMaterial(study_plan_id=plan_id, material_id=content.id, ordinal=1))
        db.execute(insert(PlanTask), tasks)
        db.execute(
            insert(PlanTaskConcept),
            [
                {
                    "task_id": task["id"],
                    "concept_id": content.concept_id,
                    "concept_version_id": content.version_id,
                }
                for task in tasks
            ],
        )
    return SimpleNamespace(id=plan_id, goal_id=goal_id, tasks=tasks, content=content)


def test_plan_detail_and_today_selects_are_bounded(query_env):
    fx = query_env
    observations = {}
    for size in (3, 300):
        seeded = seed_plan(fx, size)
        with domain_selects(fx) as statements:
            response = fx.client.get("/v1/study-plans/" + seeded.id)
        assert response.status_code == 200, response.text
        validate(response, "StudyPlanResponse")
        observations[(size, "plan")] = len(statements)
        assert [task["id"] for task in response.json()["data"]["tasks"]] == [
            task["id"] for task in seeded.tasks
        ]
        with domain_selects(fx) as statements:
            today = fx.client.get("/v1/today", params={"exam_goal_id": seeded.goal_id})
        assert today.status_code == 200, today.text
        validate(today, "TodayResponse")
        observations[(size, "today")] = len(statements)
        assert today.json()["data"]["tasks"] == response.json()["data"]["tasks"]
    assert all(count <= (6 if name == "plan" else 7) for (size, name), count in observations.items()), (
        observations
    )
    assert observations[(3, "plan")] == observations[(300, "plan")], observations
    assert observations[(3, "today")] == observations[(300, "today")], observations


def test_plan_list_selects_are_bounded_by_page_not_plan_count(query_env):
    fx = query_env
    content = material(fx)
    seed_plan(fx, 3, content=content)
    with domain_selects(fx) as statements:
        small = fx.client.get("/v1/study-plans", params={"limit": 100})
    assert small.status_code == 200, small.text
    small_count = len(statements)
    for _ in range(29):
        seed_plan(fx, 3, content=content)
    with domain_selects(fx) as statements:
        large = fx.client.get("/v1/study-plans", params={"limit": 100})
    assert large.status_code == 200, large.text
    assert len(large.json()["data"]) == 30
    assert all(row["task_count"] == 3 and row["material_ids"] == [content.id] for row in large.json()["data"])
    assert small_count <= 3 and len(statements) <= 3, (small_count, len(statements))


def test_maximum_plan_6000_tasks_has_bounded_selects(query_env):
    seeded = seed_plan(query_env, 6000)
    with domain_selects(query_env) as statements:
        response = query_env.client.get("/v1/study-plans/" + seeded.id)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["tasks"]) == 6000
    assert [task["display_order"] for task in data["tasks"]] == list(range(1, 6001))
    assert data["tasks"][0]["concept_version_ids"] == [seeded.content.version_id]
    assert all(task["completed_count"] == 0 for task in data["tasks"])
    assert len(statements) <= 6, len(statements)


def test_create_response_selects_are_bounded_and_idempotent(query_env):
    fx, counts = query_env, []
    for concepts_count in (1, 100):
        content = material(fx)
        if concepts_count > 1:
            now = utcnow()
            concepts = [
                {"id": uid(), "material_id": content.id, "unit_id": content.unit_id, "created_at": now}
                for _ in range(concepts_count - 1)
            ]
            with fx.ctx.db.transaction() as db:
                db.execute(insert(Concept), concepts)
                db.execute(
                    insert(ConceptVersion),
                    [
                        {
                            "id": uid(),
                            "concept_id": concept["id"],
                            "version_no": 1,
                            "name": "Bulk concept",
                            "summary": "Confirmed summary",
                            "is_confirmed": True,
                            "confirmed_at": now,
                            "created_at": now,
                        }
                        for concept in concepts
                    ],
                )
        created_goal = fx.client.post(
            "/v1/exam-goals",
            headers={"Idempotency-Key": uid()},
            json={"title": "Create budget", "exam_type": "other", "exam_date": today_seoul().isoformat()},
        )
        assert created_goal.status_code == 201, created_goal.text
        body = {"exam_goal_id": created_goal.json()["data"]["id"], "material_ids": [content.id]}
        key = uid()
        with domain_selects(fx) as statements:
            response = fx.client.post("/v1/study-plans", json=body, headers={"Idempotency-Key": key})
        assert response.status_code == 201, response.text
        counts.append(len(statements))
        assert len(response.json()["data"]["tasks"]) == 3 * concepts_count
        assert all(task["completed_count"] == 0 for task in response.json()["data"]["tasks"])
        assert (
            fx.client.post("/v1/study-plans", json=body, headers={"Idempotency-Key": key}).json()
            == response.json()
        )
    assert max(counts) <= 12 and counts[0] == counts[1], counts


def test_reschedule_selects_are_bounded_with_authoritative_remaining_counts(query_env):
    fx, counts = query_env, []
    for size in (3, 300):
        seeded = seed_plan(fx, size)
        solve, _, review = seeded.tasks[:3]
        set_id, question_id, _ = graded_attempt(fx, solve["id"], seeded.content)
        graded_attempt(fx, solve["id"], seeded.content, set_id=set_id, question_id=question_id)
        graded_attempt(fx, solve["id"], seeded.content, set_id=set_id)
        with fx.ctx.db.transaction() as db:
            task = db.get(PlanTask, review["id"])
            task.status, task.completed_at = "completed", utcnow()
        key = uid()
        with domain_selects(fx) as statements:
            response = fx.client.post(
                "/v1/study-plans/" + seeded.id + "/reschedule", headers={"Idempotency-Key": key}
            )
        assert response.status_code == 201, response.text
        counts.append(len(statements))
        data = response.json()["data"]
        assert data["revision"] == 2 and len(data["tasks"]) == size - 1
        assert data["tasks"][0]["target_count"] == 3
        assert all(task["completed_count"] == 0 and task["status"] == "pending" for task in data["tasks"])
        assert all(task["concept_version_ids"] == [seeded.content.version_id] for task in data["tasks"])
        replay = fx.client.post(
            "/v1/study-plans/" + seeded.id + "/reschedule", headers={"Idempotency-Key": key}
        )
        assert replay.json() == response.json()
    assert max(counts) <= 20 and counts[0] == counts[1], counts


def test_batch_progress_preserves_distinct_answers_sessions_review_and_pin_order(query_env):
    fx = query_env
    seeded = seed_plan(fx, 6)
    solve, teach, review = seeded.tasks[:3]
    set_id, question_id, _ = graded_attempt(fx, solve["id"], seeded.content)
    graded_attempt(fx, solve["id"], seeded.content, set_id=set_id, question_id=question_id)
    graded_attempt(fx, solve["id"], seeded.content, set_id=set_id)
    graded_attempt(fx, solve["id"], seeded.content, set_id=set_id, status="grading")
    second_concept, second_version = uid(), uid()
    with fx.ctx.db.transaction() as db:
        db.add(
            Concept(
                id=second_concept,
                material_id=seeded.content.id,
                unit_id=seeded.content.unit_id,
                created_at=utcnow(),
            )
        )
        db.flush()
        db.add(
            ConceptVersion(
                id=second_version,
                concept_id=second_concept,
                version_no=1,
                name="Second pinned concept",
                summary="Second summary",
                is_confirmed=True,
                confirmed_at=utcnow(),
                created_at=utcnow(),
            )
        )
        db.flush()
        db.add(
            PlanTaskConcept(task_id=solve["id"], concept_id=second_concept, concept_version_id=second_version)
        )
        for status in ("completed", "abandoned"):
            db.add(
                TeachbackSession(
                    id=uid(),
                    user_id=fx.owner,
                    task_id=teach["id"],
                    concept_version_id=seeded.content.version_id,
                    explanation_language="en",
                    student_persona="curious_beginner",
                    status=status,
                    question_count=0,
                    started_at=utcnow(),
                    completed_at=utcnow() if status == "completed" else None,
                )
            )
        db.get(PlanTask, teach["id"]).status = "completed"
        db.get(PlanTask, teach["id"]).completed_at = utcnow()
        db.get(PlanTask, review["id"]).status = "completed"
        db.get(PlanTask, review["id"]).completed_at = utcnow()
        db.get(PlanTask, seeded.tasks[5]["id"]).status = "skipped"
    response = fx.client.get("/v1/study-plans/" + seeded.id)
    validate(response, "StudyPlanResponse")
    tasks = response.json()["data"]["tasks"]
    assert [task["completed_count"] for task in tasks] == [2, 1, 1, 0, 0, 0]
    assert [task["status"] for task in tasks] == [
        "in_progress",
        "completed",
        "completed",
        "pending",
        "pending",
        "skipped",
    ]
    assert tasks[0]["concept_version_ids"] == sorted([seeded.content.version_id, second_version])
    assert tasks[0]["concept_ids"] == [item["concept_id"] for item in tasks[0]["concepts"]]
    assert tasks[0]["title"] == tasks[0]["concepts"][0]["concept_name"] + " 외 1개 · 문제 풀기"
    today = fx.client.get("/v1/today", params={"exam_goal_id": seeded.goal_id})
    assert today.json()["data"]["tasks"] == tasks
    assert today.json()["data"]["progress"] == 2 / 5
    assert fx.client.get("/v1/study-plans/" + seeded.id, headers=fx.other_headers).status_code == 404
    assert (
        fx.client.get(
            "/v1/today", params={"exam_goal_id": seeded.goal_id}, headers=fx.other_headers
        ).status_code
        == 404
    )
    assert fx.client.get("/v1/study-plans", headers=fx.other_headers).json()["data"] == []
    from app.a_gateway import task_completed_count, task_completed_counts, task_concepts, task_concepts_many

    with fx.ctx.db.transaction() as db:
        rows = db.scalars(select(PlanTask).where(PlanTask.study_plan_id == seeded.id)).all()
        assert task_completed_counts(db, rows, fx.owner) == {
            task.id: task_completed_count(db, task, fx.owner) for task in rows
        }
        assert task_concepts_many(db, [task.id for task in rows], fx.owner) == {
            task.id: task_concepts(db, task.id) for task in rows
        }


def assert_mysql_batched_progress_current_read(fixture):
    """A stale RR snapshot must not hide progress once the owner locks are held."""
    from app.a_gateway import lock_owned_plan, lock_owned_task, task_completed_counts, update_solve_progress

    assert fixture.ctx.db.engine.dialect.name == "mysql"
    if not getattr(fixture, "owner", None):
        fixture.owner = fixture.client.get("/v1/me").json()["data"]["id"]
    seeded = seed_plan(fixture, 3)
    solve, teach, _ = seeded.tasks
    _, _, attempt_id = graded_attempt(fixture, solve["id"], seeded.content, status="grading")
    session_id = uid()
    with fixture.ctx.db.transaction() as db:
        db.add(
            TeachbackSession(
                id=session_id,
                user_id=fixture.owner,
                task_id=teach["id"],
                concept_version_id=seeded.content.version_id,
                explanation_language="en",
                student_persona="curious_beginner",
                status="active",
                question_count=0,
                started_at=utcnow(),
                completed_at=None,
            )
        )
    with fixture.ctx.db.transaction() as reader:
        assert reader.connection().get_isolation_level() == "REPEATABLE READ"
        rows = reader.scalars(select(PlanTask).where(PlanTask.study_plan_id == seeded.id)).all()
        assert task_completed_counts(reader, rows, fixture.owner)[solve["id"]] == 0
        with fixture.ctx.db.transaction() as writer:
            lock_owned_task(writer, fixture.owner, solve["id"])
            lock_owned_task(writer, fixture.owner, teach["id"])
            attempt = writer.get(QuestionAttempt, attempt_id)
            attempt.status, attempt.is_correct = "graded", False
            writer.get(Job, attempt.job_id).status = "succeeded"
            session = writer.get(TeachbackSession, session_id)
            session.status, session.completed_at = "completed", utcnow()
            task = writer.get(PlanTask, teach["id"])
            task.status, task.completed_at = "completed", utcnow()
            writer.flush()
            update_solve_progress(writer, fixture.owner, solve["id"])
        lock_owned_plan(reader, fixture.owner, seeded.id)
        rows = reader.scalars(
            select(PlanTask)
            .where(PlanTask.study_plan_id == seeded.id)
            .order_by(PlanTask.ordinal)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        stale = task_completed_counts(reader, rows, fixture.owner)
        assert stale[solve["id"]] == stale[teach["id"]] == 0
        with domain_selects(fixture) as statements:
            fresh = task_completed_counts(reader, rows, fixture.owner, authoritative=True)
        assert fresh[solve["id"]] == fresh[teach["id"]] == 1
        assert len(statements) <= 2, len(statements)
