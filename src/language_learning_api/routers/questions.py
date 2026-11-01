import json
from datetime import datetime, timezone, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Path, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from language_learning_api.auth import CurrentUser, DatabaseSession
from language_learning_api.models import IdempotencyRecord, Job, Question, QuestionAttempt, QuestionSet
from language_learning_api.routers.common import decode_cursor, encode_cursor, request_hash
from language_learning_api.schemas import (AcceptedEnvelope, AttemptInput, QuestionAttemptPage,
    QuestionEnvelope, QuestionResponse)

router = APIRouter(tags=["문제 풀이"])


def job_dto(job: Job) -> dict:
    return {"id": job.id, "kind": job.kind, "status": job.status, "resource_type": job.resource_type,
        "resource_id": job.resource_id, "attempt_no": job.attempt_no, "retryable": job.retryable,
        "error_code": job.error_code, "result_url": job.result_url, "created_at": job.created_at,
        "updated_at": job.updated_at, "previous_job_id": job.previous_job_id}


def attempt_dto(attempt: QuestionAttempt) -> dict:
    return {"id": attempt.id, "question_id": attempt.question_id, "question_set_id": attempt.question_set_id,
        "answer": attempt.answer, "status": attempt.status, "result": attempt.result,
        "job_id": attempt.job_id, "answered_at": attempt.answered_at}


def owned_question(db, user_id: UUID, question_id: UUID) -> tuple[Question, QuestionSet]:
    row = db.execute(select(Question, QuestionSet).join(QuestionSet, Question.set_id == QuestionSet.id)
        .where(Question.id == question_id, QuestionSet.owner_id == user_id)).first()
    if row is None:
        raise HTTPException(404, "NOT_FOUND")
    return row[0], row[1]


def idempotent_response(db, owner_id: UUID, operation: str, key: str, digest: str):
    record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.owner_id == owner_id,
        IdempotencyRecord.operation == operation, IdempotencyRecord.key == key))
    if record:
        created = record.created_at.replace(tzinfo=timezone.utc) if record.created_at.tzinfo is None else record.created_at
        if datetime.now(timezone.utc) - created < timedelta(hours=24):
            if record.request_hash != digest:
                raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
            return record.response_body
        db.delete(record); db.flush()
    return None


@router.get("/v1/questions/{questionId}", response_model=QuestionEnvelope)
def get_question(question_id: Annotated[UUID, Path(alias="questionId")], user: CurrentUser, db: DatabaseSession):
    question, _ = owned_question(db, user.id, question_id)
    return {"data": QuestionResponse(id=question.id, question_type=question.question_type,
        concept_ids=question.concept_ids, concept_version_ids=question.concept_version_ids,
        prompt=question.prompt, options=question.options, difficulty=question.difficulty, evidence=question.evidence)}


@router.post("/v1/questions/{questionId}/attempts", response_model=AcceptedEnvelope, status_code=202)
def submit_attempt(question_id: Annotated[UUID, Path(alias="questionId")], body: AttemptInput,
    user: CurrentUser, db: DatabaseSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    ai_key: Annotated[str, Header(alias="X-AI-Key", min_length=1, max_length=512)]):
    question, question_set = owned_question(db, user.id, question_id)
    if question_set.status != "ready":
        raise HTTPException(409, "QUESTION_SET_NOT_READY")
    if question_set.id != body.question_set_id:
        raise HTTPException(422, "QUESTION_SET_MISMATCH")
    answer = body.answer.model_dump(exclude_none=True)
    if answer["type"] != question.question_type:
        raise HTTPException(422, "ANSWER_TYPE_MISMATCH")
    if question.question_type == "multiple_choice":
        option_ids = {item.get("id") for item in question.options}
        if answer.get("selected_option_ids", [None])[0] not in option_ids or not 2 <= len(option_ids) <= 5:
            raise HTTPException(422, "INVALID_OPTIONS")
    op = f"POST:/v1/questions/{question_id}/attempts"
    digest = request_hash({"answer": answer, "question_set_id": body.question_set_id, "model_id": body.model_id})
    cached = idempotent_response(db, user.id, op, idempotency_key, digest)
    if cached:
        return cached
    busy = db.scalar(select(QuestionAttempt.id).where(QuestionAttempt.owner_id == user.id,
        QuestionAttempt.question_id == question.id, QuestionAttempt.status == "grading").limit(1))
    if busy:
        raise HTTPException(409, "JOB_IN_PROGRESS")
    attempt_id = uuid4()
    job = Job(owner_id=user.id, kind="grade", status="queued", resource_type="attempt", resource_id=attempt_id, model_id=body.model_id)
    db.add(job); db.flush()
    attempt = QuestionAttempt(id=attempt_id, owner_id=user.id, question_id=question.id, question_set_id=question_set.id,
        answer=answer, status="grading", job_id=job.id)
    db.add(attempt); db.flush()
    response = {"data": {"job": job_dto(job)}}
    response = jsonable_encoder(response)
    db.add(IdempotencyRecord(owner_id=user.id, operation=op, key=idempotency_key, request_hash=digest,
        response_status=202, response_body=response))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        cached = idempotent_response(db, user.id, op, idempotency_key, digest)
        if cached:
            return cached
        raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
    return response


@router.get("/v1/questions/{questionId}/attempts", response_model=QuestionAttemptPage)
def list_question_attempts(question_id: Annotated[UUID, Path(alias="questionId")], user: CurrentUser,
    db: DatabaseSession, cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    owned_question(db, user.id, question_id)
    stmt = select(QuestionAttempt).where(QuestionAttempt.owner_id == user.id, QuestionAttempt.question_id == question_id)
    if cursor:
        created, item_id = decode_cursor(cursor)
        stmt = stmt.where((QuestionAttempt.answered_at < created) | ((QuestionAttempt.answered_at == created) & (QuestionAttempt.id < item_id)))
    rows = list(db.scalars(stmt.order_by(QuestionAttempt.answered_at.desc(), QuestionAttempt.id.desc()).limit(limit + 1)))
    more = len(rows) > limit; rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].answered_at, rows[-1].id) if more and rows else None
    return {"data": [attempt_dto(a) for a in rows], "next_cursor": next_cursor}


@router.get("/v1/attempts/{attemptId}")
def get_attempt(attempt_id: Annotated[UUID, Path(alias="attemptId")], user: CurrentUser, db: DatabaseSession):
    attempt = db.scalar(select(QuestionAttempt).where(QuestionAttempt.id == attempt_id, QuestionAttempt.owner_id == user.id))
    if attempt is None:
        raise HTTPException(404, "NOT_FOUND")
    return {"data": attempt_dto(attempt)}
