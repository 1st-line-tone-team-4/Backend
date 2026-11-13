"""A/B grading regressions; providers and credentials below are test fixtures only."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.a_gateway import CanonicalTaskGateway, task_completed_count
from app.content import QuestionsHandler
from app.core import uid, utcnow
from app.models import (
    Concept,
    ConceptVersion,
    ConceptVersionEvidence,
    ExamGoal,
    Job,
    Material,
    MaterialChunk,
    MaterialUnit,
    PlanTask,
    PlanTaskConcept,
    Question,
    QuestionAttempt,
    QuestionSet,
    QuestionSetConcept,
    StudyPlan,
)
from app.providers import ProviderFailure
from tests.support import test_application as make_application


@pytest.fixture
def grade_env(tmp_path):
    fx = make_application(tmp_path)
    fx.ctx.task_gateway = CanonicalTaskGateway()
    owner = fx.auth.tokens["test-owner-token"]
    ids = {
        k: uid()
        for k in ("material", "unit", "chunk", "concept", "version", "goal", "plan", "task", "set", "set_job")
    }
    now = utcnow()
    with fx.ctx.db.transaction() as db:
        db.add(
            Material(
                id=ids["material"],
                user_id=owner,
                filename="notes.txt",
                mime_type="text/plain",
                size_bytes=10,
                storage_key="fixture-key",
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
                content="Momentum is conserved when external force is zero.",
                created_at=now,
            )
        )
        db.flush()
        db.add(Concept(id=ids["concept"], material_id=ids["material"], unit_id=ids["unit"], created_at=now))
        db.add(
            ExamGoal(
                id=ids["goal"],
                user_id=owner,
                title="Test",
                exam_type="other",
                exam_date=date.today() + timedelta(days=3),
                status="active",
            )
        )
        db.flush()
        db.add(
            ConceptVersion(
                id=ids["version"],
                concept_id=ids["concept"],
                version_no=1,
                name="Momentum",
                summary="Zero external force conserves momentum.",
                is_confirmed=True,
                confirmed_at=now,
                created_at=now,
            )
        )
        db.add(
            StudyPlan(id=ids["plan"], user_id=owner, exam_goal_id=ids["goal"], revision=1, status="active")
        )
        db.flush()
        db.add(ConceptVersionEvidence(concept_version_id=ids["version"], chunk_id=ids["chunk"]))
        db.add(
            PlanTask(
                id=ids["task"],
                study_plan_id=ids["plan"],
                ordinal=1,
                due_date=date.today(),
                task_type="solve",
                target_count=2,
                status="pending",
            )
        )
        job = Job(
            id=ids["set_job"],
            user_id=owner,
            kind="questions",
            status="succeeded",
            resource_type="question_set",
            resource_id=ids["set"],
            request_fingerprint=uid(),
            request_payload={},
            model_id=fx.provider.model_id,
            attempt_no=1,
            retryable=False,
            fencing_token=0,
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.flush()
        db.add(
            PlanTaskConcept(task_id=ids["task"], concept_id=ids["concept"], concept_version_id=ids["version"])
        )
        db.add(
            QuestionSet(
                id=ids["set"],
                user_id=owner,
                unit_id=ids["unit"],
                task_id=ids["task"],
                difficulty="easy",
                requested_count=2,
                status="generating",
                job_id=job.id,
                created_at=now,
            )
        )
        db.flush()
        db.add(
            QuestionSetConcept(
                set_id=ids["set"], concept_id=ids["concept"], concept_version_id=ids["version"]
            )
        )
        db.flush()
        output = {
            "questions": [
                {
                    "question_type": "short_answer",
                    "concept_version_ids": [ids["version"]],
                    "prompt": f"When is momentum conserved? {i}",
                    "options": [],
                    "answer_key": {"type": "short_answer", "text": "Zero external force"},
                    "public_explanation": "The source identifies the external force condition.",
                    "accepted_answers": ["Zero external force"],
                    "evidence_chunk_ids": [ids["chunk"]],
                }
                for i in range(2)
            ]
        }
        handler = QuestionsHandler(fx.ctx)
        handler.persist(db, job, output)
        handler.set_state(db, job, "succeeded")
        fx.questions = list(db.scalars(select(Question.id).order_by(Question.id)))
    fx.ids, fx.owner = ids, owner
    fx.client = TestClient(fx.app)
    fx.output_override = None
    original = fx.provider.generate

    def generate(model, key, kind, payload):
        if kind != "grade":
            return original(model, key, kind, payload)
        fx.provider.calls.append((kind, deepcopy(payload)))
        if fx.provider.fail_next:
            failure, fx.provider.fail_next = fx.provider.fail_next, None
            raise failure
        if fx.output_override is not None:
            return fx.output_override
        return {
            "is_correct": payload["answer"] == payload["question"]["answer_key"],
            "correct_answer": deepcopy(payload["question"]["answer_key"]),
            "feedback": "Check the external force condition.",
            "explanation": "The source explains zero net external force.",
        }

    fx.provider.generate = generate
    return fx


def headers(*, other=False, key=None):
    return {
        "Authorization": "Bearer test-other-token" if other else "Bearer test-owner-token",
        "Idempotency-Key": key or uid(),
    }


def submit(fx, *, q=None, answer="Zero external force", key=None, other=False, set_id=None):
    return fx.client.post(
        f"/v1/questions/{q or fx.questions[0]}/attempts",
        headers=headers(other=other, key=key),
        json={
            "question_set_id": set_id or fx.ids["set"],
            "model_id": fx.provider.model_id,
            "answer": answer if isinstance(answer, dict) else {"type": "short_answer", "text": answer},
        },
    )


def run_grade(fx, response):
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["job"]["id"]
    assert fx.ctx.jobs.run_once()
    with fx.ctx.db.transaction() as db:
        return db.get(Job, job_id), db.get(QuestionAttempt, response.json()["data"]["job"]["resource_id"])


def test_grade_end_to_end_normalized_result_progress_and_privacy(grade_env):
    fx = grade_env
    key = uid()
    response = submit(fx, key=key)
    assert submit(fx, key=key).json() == response.json()
    assert submit(fx).status_code == 409
    job, attempt = run_grade(fx, response)
    assert (job.status, attempt.status, job.result_url) == (
        "succeeded",
        "graded",
        f"/v1/attempts/{attempt.id}",
    )
    assert job.credential_ref is None
    result = fx.client.get(job.result_url, headers=headers()).json()["data"]["result"]
    assert set(result) == {"is_correct", "feedback", "correct_answer", "explanation", "evidence"}
    assert result["is_correct"] is True and result["evidence"][0]["chunk_id"] == fx.ids["chunk"]
    public_question = fx.client.get(f"/v1/questions/{attempt.question_id}", headers=headers()).json()["data"]
    assert "answer_key" not in public_question and "accepted_answers" not in public_question
    assert fx.client.get(job.result_url, headers=headers(other=True)).status_code == 404
    page = fx.client.get(f"/v1/questions/{attempt.question_id}/attempts", headers=headers()).json()
    assert page["data"][0]["result"] == result
    run_grade(fx, submit(fx, answer="A wrong answer"))
    with fx.ctx.db.transaction() as db:
        task = db.get(PlanTask, fx.ids["task"])
        assert task.status == "in_progress" and task_completed_count(db, task, fx.owner) == 1
    run_grade(fx, submit(fx, q=fx.questions[1], answer="Wrong answer also completes practice"))
    with fx.ctx.db.transaction() as db:
        task = db.get(PlanTask, fx.ids["task"])
        assert task.status == "completed" and task_completed_count(db, task, fx.owner) == 2


def test_ownership_membership_and_answer_validation(grade_env):
    fx = grade_env
    assert submit(fx, other=True).status_code == 404
    assert submit(fx, set_id=uid()).status_code == 404
    assert submit(fx, answer="  ").status_code == 422
    assert submit(fx, answer={"type": "multiple_choice", "selected_option_ids": ["A"]}).status_code == 422
    assert (
        submit(fx, answer={"type": "short_answer", "text": "Answer", "selected_option_ids": None}).status_code
        == 422
    )
    with fx.ctx.db.transaction() as db:
        duplicate_set = QuestionSet(
            id=uid(),
            user_id=fx.owner,
            unit_id=fx.ids["unit"],
            difficulty="easy",
            requested_count=1,
            status="ready",
            job_id=uid(),
            created_at=utcnow(),
        )
        db.add(
            Job(
                id=duplicate_set.job_id,
                user_id=fx.owner,
                kind="questions",
                status="succeeded",
                resource_type="question_set",
                resource_id=duplicate_set.id,
                request_fingerprint=uid(),
                request_payload={},
                model_id=fx.provider.model_id,
                attempt_no=1,
                retryable=False,
                fencing_token=0,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        db.flush()
        db.add(duplicate_set)
    assert submit(fx, set_id=duplicate_set.id).json()["error"]["code"] == "RELATION_MISMATCH"


@pytest.mark.parametrize(
    "change",
    [
        {"is_correct": "true"},
        {"feedback": " "},
        {"explanation": "x" * 12001},
        {"correct_answer": {"type": "short_answer", "text": "Invented"}},
        {"extra": "untrusted"},
    ],
)
def test_invalid_provider_output_is_atomic_nonretryable_failure(grade_env, change):
    fx = grade_env
    fx.output_override = {
        "is_correct": True,
        "feedback": "Feedback",
        "explanation": "Explanation",
        "correct_answer": {"type": "short_answer", "text": "Zero external force"},
        **change,
    }
    job, attempt = run_grade(fx, submit(fx))
    assert (job.status, job.error_code, job.retryable, attempt.status) == (
        "failed",
        "INVALID_OUTPUT",
        False,
        "failed",
    )
    assert attempt.is_correct is None and attempt.correct_answer is None
    with fx.ctx.db.transaction() as db:
        task = db.get(PlanTask, fx.ids["task"])
        assert task.status == "pending" and task_completed_count(db, task, fx.owner) == 0


def test_retry_unknown_restart_and_new_attempt_fencing(grade_env):
    fx = grade_env
    fx.provider.fail_next = ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True)
    job, attempt = run_grade(fx, submit(fx))
    assert job.retryable and attempt.status == "failed"
    retry = fx.client.post(
        f"/v1/jobs/{job.id}/retry", headers=headers(), json={"model_id": fx.provider.model_id}
    )
    assert retry.status_code == 202 and retry.json()["data"]["job"]["attempt_no"] == 2
    fx.provider.fail_next = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    assert fx.ctx.jobs.run_once()
    assert submit(fx).json()["error"]["code"] == "DUPLICATE_EXECUTION_ACK_REQUIRED"
    assert (
        fx.client.post(
            f"/v1/jobs/{job.id}/retry", headers=headers(), json={"model_id": fx.provider.model_id}
        ).status_code
        == 409
    )
    restart_url = f"/v1/jobs/{job.id}/restart"
    assert (
        fx.client.post(
            restart_url,
            headers=headers(),
            json={"model_id": fx.provider.model_id, "acknowledge_possible_duplicate_execution": False},
        ).status_code
        == 422
    )
    restarted = fx.client.post(
        restart_url,
        headers=headers(),
        json={"model_id": fx.provider.model_id, "acknowledge_possible_duplicate_execution": True},
    )
    assert restarted.status_code == 202
    assert restarted.json()["data"]["job"]["previous_job_id"] == job.id
    assert restarted.json()["data"]["job"]["resource_id"] == attempt.id
    newer_job, _ = run_grade(fx, restarted)
    assert newer_job.status == "succeeded"
    assert (
        fx.client.post(
            restart_url,
            headers=headers(),
            json={"model_id": fx.provider.model_id, "acknowledge_possible_duplicate_execution": True},
        ).status_code
        == 409
    )


def test_new_answer_makes_failed_job_stale_and_old_plan_cannot_progress(grade_env):
    fx = grade_env
    fx.provider.fail_next = ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True)
    old, _ = run_grade(fx, submit(fx))
    fresh = submit(fx, answer="New response")
    retry = fx.client.post(
        f"/v1/jobs/{old.id}/retry", headers=headers(), json={"model_id": fx.provider.model_id}
    )
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "STALE_JOB"
    with fx.ctx.db.transaction() as db:
        db.get(StudyPlan, fx.ids["plan"]).status = "superseded"
    job, attempt = run_grade(fx, fresh)
    assert job.status == "succeeded" and attempt.status == "graded"
    with fx.ctx.db.transaction() as db:
        assert db.get(PlanTask, fx.ids["task"]).status == "pending"
    assert submit(fx, answer="Another response").json()["error"]["code"] == "STALE_TASK"


def test_concurrent_submissions_create_one_active_grade(grade_env):
    fx = grade_env
    barrier = Barrier(2)

    def send(answer):
        barrier.wait(timeout=10)
        return submit(fx, answer=answer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(send, ["First", "Second"]))
    assert sorted(r.status_code for r in replies) == [202, 409]
    with fx.ctx.db.transaction() as db:
        assert db.scalar(select(func.count()).select_from(QuestionAttempt)) == 1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "grade")) == 1


def test_multiple_choice_is_ai_graded_but_cannot_override_trusted_verdict(grade_env):
    fx = grade_env
    answer = {"type": "multiple_choice", "selected_option_ids": ["A"]}
    with fx.ctx.db.transaction() as db:
        question = db.get(Question, fx.questions[0])
        question.question_type = "multiple_choice"
        question.options = [{"id": "A", "text": "Zero external force"}, {"id": "B", "text": "Any force"}]
        question.answer_key = answer
    assert submit(fx, answer={"type": "multiple_choice", "selected_option_ids": ["C"]}).status_code == 422
    fx.output_override = {
        "is_correct": False,
        "correct_answer": answer,
        "feedback": "Feedback",
        "explanation": "Explanation",
    }
    job, attempt = run_grade(fx, submit(fx, answer=answer))
    assert job.error_code == "INVALID_OUTPUT" and attempt.status == "failed"
    assert len(fx.provider.calls) == 1


def test_missing_service_key_fails_without_provider_call_and_can_retry(grade_env):
    fx = grade_env
    response = submit(fx)
    assert response.status_code == 202
    job_id = response.json()["data"]["job"]["id"]
    fx.provider.service_key = lambda model_id: None  # Operator has not configured the key.
    job, attempt = run_grade(fx, response)
    del fx.provider.service_key
    assert (job.error_code, job.retryable, attempt.status) == ("CREDENTIAL_UNAVAILABLE", True, "failed")
    assert fx.provider.calls == []
    retry = fx.client.post(
        f"/v1/jobs/{job_id}/retry", headers=headers(), json={"model_id": fx.provider.model_id}
    )
    job, attempt = run_grade(fx, retry)
    assert job.status == "succeeded" and attempt.status == "graded"


def test_attempt_pagination_is_scoped_and_never_regrades(grade_env):
    fx = grade_env
    for value in ("One", "Two", "Three"):
        run_grade(fx, submit(fx, answer=value))
    url = f"/v1/questions/{fx.questions[0]}/attempts"
    first = fx.client.get(url, headers=headers(), params={"limit": 2}).json()
    assert len(first["data"]) == 2 and first["next_cursor"]
    second = fx.client.get(url, headers=headers(), params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["data"]) == 1 and second["next_cursor"] is None
    assert not {x["id"] for x in first["data"]} & {x["id"] for x in second["data"]}
    assert (
        fx.client.get(
            f"/v1/questions/{fx.questions[1]}/attempts",
            headers=headers(),
            params={"cursor": first["next_cursor"]},
        ).status_code
        == 422
    )
    assert len(fx.provider.calls) == 3


def test_task_pinned_version_mismatch_is_rejected(grade_env):
    fx = grade_env
    with fx.ctx.db.transaction() as db:
        version = ConceptVersion(
            id=uid(),
            concept_id=fx.ids["concept"],
            version_no=2,
            name="Updated",
            summary="Updated summary",
            is_confirmed=True,
            confirmed_at=utcnow(),
            created_at=utcnow(),
        )
        db.add(version)
        db.flush()
        db.get(PlanTaskConcept, (fx.ids["task"], fx.ids["concept"])).concept_version_id = version.id
    response = submit(fx)
    assert response.status_code == 422 and response.json()["error"]["code"] == "RELATION_MISMATCH"
    assert fx.provider.calls == []


def test_concurrent_distinct_grades_complete_one_task_without_lost_progress(grade_env):
    fx = grade_env
    responses = [submit(fx, q=q) for q in fx.questions]
    assert all(r.status_code == 202 for r in responses)
    first_dispatched, rendezvous = Event(), Barrier(2)
    generate = fx.provider.generate

    def concurrent_generate(*args):
        output = generate(*args)
        first_dispatched.set()
        rendezvous.wait(timeout=10)
        return output

    fx.provider.generate = concurrent_generate
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fx.ctx.jobs.run_once)
        assert first_dispatched.wait(timeout=10)
        second = pool.submit(fx.ctx.jobs.run_once)
        assert first.result(timeout=20) and second.result(timeout=20)
    with fx.ctx.db.transaction() as db:
        task = db.get(PlanTask, fx.ids["task"])
        assert task.status == "completed" and task_completed_count(db, task, fx.owner) == 2
        assert set(db.scalars(select(Job.status).where(Job.kind == "grade"))) == {"succeeded"}


def test_progress_failure_rolls_back_attempt_and_task_together(grade_env, monkeypatch):
    import app.a_gateway as gateway

    fx = grade_env
    original = gateway.update_solve_progress

    def fail_after_progress(*args):
        original(*args)
        args[0].flush()
        raise ValueError("Fixture persistence failure")

    monkeypatch.setattr(gateway, "update_solve_progress", fail_after_progress)
    job, attempt = run_grade(fx, submit(fx))
    assert job.status == "failed" and job.error_code == "INVALID_OUTPUT"
    assert attempt.status == "failed" and attempt.is_correct is None
    with fx.ctx.db.transaction() as db:
        task = db.get(PlanTask, fx.ids["task"])
        assert task.status == "pending" and task_completed_count(db, task, fx.owner) == 0
