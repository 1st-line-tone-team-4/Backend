"""Private attempts use the shared durable job engine and canonical B questions."""

from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy import and_, or_, select

from app.content import get_grading_question, question_dto
from app.core import ApiError, get_context, get_user, idempotent, iso, uid, utcnow
from app.credentials import check_key
from app.jobs import accepted
from app.models import Job, Question, QuestionAttempt
from language_learning_api.routers.common import decode_cursor, encode_cursor
from language_learning_api.schemas import AcceptedEnvelope, AttemptInput, QuestionAttemptPage
from language_learning_api.services.grading import lock_grading_question, validate_answer

router = APIRouter(tags=["Question attempts"])


def attempt_dto(attempt, db):
    result = None
    if attempt.status == "graded":
        question = db.get(Question, attempt.question_id)
        result = {
            "is_correct": attempt.is_correct,
            "feedback": attempt.grading_feedback,
            "correct_answer": attempt.correct_answer,
            "explanation": attempt.explanation,
            "evidence": question_dto(db, question)["evidence"],
        }
    return {
        "id": attempt.id,
        "question_id": attempt.question_id,
        "question_set_id": attempt.question_set_id,
        "answer": attempt.answer,
        "status": attempt.status,
        "result": result,
        "job_id": attempt.job_id,
        "answered_at": iso(attempt.answered_at),
    }


@router.post("/v1/questions/{questionId}/attempts", response_model=AcceptedEnvelope, status_code=202)
def submit_attempt(
    question_id: Annotated[UUID, Path(alias="questionId")],
    body: AttemptInput,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    ai_key: Annotated[str, Header(alias="X-AI-Key", min_length=1, max_length=512)],
):
    ctx, user = get_context(request), get_user(request)
    check_key(ai_key)
    data = body.model_dump(mode="json", exclude_none=True)
    qid = str(question_id)

    def action(db):
        question, question_set = lock_grading_question(db, user, qid, data["question_set_id"], active=True)
        answer = validate_answer(data["answer"], question)
        # Locking reads see fresh attempts under MySQL REPEATABLE READ. The
        # question row serializes new attempts against retry/restart.
        attempts = list(
            db.scalars(
                select(QuestionAttempt)
                .where(
                    QuestionAttempt.user_id == user,
                    QuestionAttempt.question_id == qid,
                )
                .order_by(QuestionAttempt.answered_at.desc(), QuestionAttempt.id.desc())
                .with_for_update()
            )
        )
        if any(a.status == "grading" for a in attempts):
            raise ApiError(409, "JOB_IN_PROGRESS", "A grade for this question is already active.")
        if attempts and attempts[0].status == "failed" and attempts[0].answer == answer:
            prior = db.get(Job, attempts[0].job_id)
            code = (
                "POSSIBLE_DUPLICATE_COST"
                if prior and prior.status == "outcome_unknown"
                else "JOB_NOT_RETRYABLE"
            )
            raise ApiError(409, code, "Resume this attempt using its job retry or acknowledged restart.")
        attempt_id = uid()
        job = ctx.jobs.enqueue(
            db,
            user_id=user,
            kind="grade",
            resource_type="attempt",
            resource_id=attempt_id,
            payload={"question_id": qid, "question_set_id": question_set.id, "answer": answer},
            model_id=data["model_id"],
            ai_key=ai_key,
        )
        # Preserve the total order when two submissions share a clock tick.
        answered = utcnow()
        if attempts:
            answered = max(answered, attempts[0].answered_at + timedelta(milliseconds=1))
        db.add(
            QuestionAttempt(
                id=attempt_id,
                user_id=user,
                question_id=qid,
                question_set_id=question_set.id,
                answer=answer,
                status="grading",
                job_id=job.id,
                answered_at=answered,
            )
        )
        db.flush()
        return 202, accepted(job)

    return idempotent(
        ctx, user, "submitAttempt", idempotency_key, {"path": request.url.path, "body": data}, action
    )


@router.get("/v1/questions/{questionId}/attempts", response_model=QuestionAttemptPage)
def list_question_attempts(
    question_id: Annotated[UUID, Path(alias="questionId")],
    request: Request,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    ctx, user = get_context(request), get_user(request)
    with ctx.db.transaction() as db:
        get_grading_question(db, user, str(question_id))
        filters = {"owner": user, "question_id": str(question_id)}
        stmt = select(QuestionAttempt).where(
            QuestionAttempt.user_id == user, QuestionAttempt.question_id == str(question_id)
        )
        if cursor:
            created, item_id = decode_cursor(cursor, filters, secret=ctx.settings.signing_secret)
            stmt = stmt.where(
                or_(
                    QuestionAttempt.answered_at < created,
                    and_(QuestionAttempt.answered_at == created, QuestionAttempt.id < str(item_id)),
                )
            )
        rows = list(
            db.scalars(
                stmt.order_by(QuestionAttempt.answered_at.desc(), QuestionAttempt.id.desc()).limit(limit + 1)
            )
        )
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = (
            encode_cursor(rows[-1].answered_at, rows[-1].id, filters, secret=ctx.settings.signing_secret)
            if more and rows
            else None
        )
        return {"data": [attempt_dto(a, db) for a in rows], "next_cursor": nxt}


@router.get("/v1/attempts/{attemptId}")
def get_attempt(attempt_id: Annotated[UUID, Path(alias="attemptId")], request: Request):
    ctx, user = get_context(request), get_user(request)
    with ctx.db.transaction() as db:
        attempt = db.scalar(
            select(QuestionAttempt).where(
                QuestionAttempt.id == str(attempt_id), QuestionAttempt.user_id == user
            )
        )
        if attempt is None:
            raise ApiError(404, "NOT_FOUND", "Attempt not found.")
        return {"data": attempt_dto(attempt, db)}
