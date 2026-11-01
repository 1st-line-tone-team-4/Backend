import base64
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from language_learning_api.auth import CurrentUser, DatabaseSession
from language_learning_api.config import get_settings
from language_learning_api.models import ExamGoal, IdempotencyRecord
from language_learning_api.schemas import ExamGoalEnvelope, ExamGoalInput, ExamGoalPage, ExamGoalPatch, ExamGoalResponse

router = APIRouter(prefix="/v1/exam-goals", tags=["시험 목표"])
MAX_PLAN_DAYS = 180
STAGES = ["concept", "practice", "teach_back", "review"]


def today_seoul() -> date:
    try:
        local_tz = ZoneInfo("Asia/Seoul")
    except ZoneInfoNotFoundError:
        local_tz = timezone(timedelta(hours=9))
    return datetime.now(local_tz).date()


def to_response(goal: ExamGoal) -> ExamGoalResponse:
    days = max(0, (goal.exam_date - today_seoul()).days)
    eligible = days <= MAX_PLAN_DAYS
    return ExamGoalResponse(
        id=goal.id,
        title=goal.title,
        exam_type=goal.exam_type,
        exam_date=goal.exam_date,
        status=goal.status,
        course_preview={
            "days_remaining": days,
            "eligible": eligible,
            "reason": None if eligible else "EXAM_TOO_FAR",
            "stages": STAGES if eligible else [],
        },
    )


def encode_cursor(created_at: str, goal_id: str) -> str:
    payload = json.dumps({"created_at": created_at, "id": goal_id}, separators=(",", ":")).encode()
    signature = hmac.new(get_settings().cursor_secret.encode(), payload, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + signature.hex()
    return token


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        signature = bytes.fromhex(encoded_signature)
        expected = hmac.new(get_settings().cursor_secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        data = json.loads(payload)
        return datetime.fromisoformat(data["created_at"]), UUID(data["id"])
    except Exception as exc:
        raise HTTPException(status_code=422, detail="VALIDATION_ERROR") from exc


@router.get("", response_model=ExamGoalPage)
def list_exam_goals(
    user: CurrentUser,
    db: DatabaseSession,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    stmt = select(ExamGoal).where(ExamGoal.owner_id == user.id)
    if cursor:
        created_at, goal_id = decode_cursor(cursor)
        stmt = stmt.where(
            (ExamGoal.created_at < created_at)
            | ((ExamGoal.created_at == created_at) & (ExamGoal.id < goal_id))
        )
    rows = list(db.scalars(stmt.order_by(ExamGoal.created_at.desc(), ExamGoal.id.desc()).limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        created_at = last.created_at.replace(tzinfo=timezone.utc).isoformat()
        next_cursor = encode_cursor(created_at, str(last.id))
    return {"data": [to_response(goal) for goal in rows], "next_cursor": next_cursor}


@router.post("", response_model=ExamGoalEnvelope, status_code=201)
def create_exam_goal(
    body: ExamGoalInput,
    user: CurrentUser,
    db: DatabaseSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
):
    if body.exam_date < today_seoul():
        raise HTTPException(status_code=422, detail="EXAM_PASSED")
    operation = "POST:/v1/exam-goals"
    canonical_body = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(canonical_body.encode()).hexdigest()
    previous = db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.owner_id == user.id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.key == idempotency_key,
    ))
    if previous:
        created_at = previous.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created_at >= timedelta(hours=24):
            db.delete(previous)
            db.commit()
            previous = None
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        return JSONResponse(status_code=previous.response_status, content=previous.response_body)

    goal = ExamGoal(owner_id=user.id, title=body.title, exam_type=body.exam_type, exam_date=body.exam_date)
    try:
        db.add(goal)
        db.flush()
        response_body = {"data": to_response(goal).model_dump(mode="json")}
        record = IdempotencyRecord(
            owner_id=user.id,
            operation=operation,
            key=idempotency_key,
            request_hash=request_hash,
            response_status=201,
            response_body=response_body,
        )
        db.add(record)
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.owner_id == user.id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.key == idempotency_key,
        ))
        if previous and previous.request_hash == request_hash:
            return JSONResponse(status_code=previous.response_status, content=previous.response_body)
        if previous:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        raise
    return response_body


@router.patch("/{goalId}", response_model=ExamGoalEnvelope)
def update_exam_goal(
    goal_id: Annotated[UUID, Path(alias="goalId")],
    body: ExamGoalPatch,
    user: CurrentUser,
    db: DatabaseSession,
):
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="VALIDATION_ERROR")
    if "exam_date" in updates and updates["exam_date"] < today_seoul():
        raise HTTPException(status_code=422, detail="EXAM_PASSED")
    goal = db.scalar(select(ExamGoal).where(ExamGoal.id == goal_id, ExamGoal.owner_id == user.id))
    if goal is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    for field, value in updates.items():
        setattr(goal, field, value)
    db.commit()
    db.refresh(goal)
    return {"data": to_response(goal)}
