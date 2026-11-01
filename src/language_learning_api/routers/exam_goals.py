from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy import select

from app.core import ApiError, get_context, get_user, idempotent, uid, utcnow
from app.models import ExamGoal
from language_learning_api.routers.common import decode_cursor, encode_cursor
from language_learning_api.schemas import ExamGoalEnvelope, ExamGoalInput, ExamGoalPage, ExamGoalPatch

router = APIRouter(prefix="/v1/exam-goals", tags=["시험 목표"])
MAX_PLAN_DAYS = 180
STAGES = ["concept", "practice", "teach_back", "review"]


def today_seoul() -> date:
    try:
        local_tz = ZoneInfo("Asia/Seoul")
    except ZoneInfoNotFoundError:
        local_tz = timezone(timedelta(hours=9))
    return datetime.now(local_tz).date()


def to_response(goal):
    remaining = (goal.exam_date - today_seoul()).days
    eligible = 0 <= remaining <= MAX_PLAN_DAYS
    return {
        "id": goal.id,
        "title": goal.title,
        "exam_type": goal.exam_type,
        "exam_date": goal.exam_date.isoformat(),
        "status": goal.status,
        "course_preview": {
            "days_remaining": max(0, remaining),
            "eligible": eligible,
            "reason": None if eligible else "EXAM_PASSED" if remaining < 0 else "EXAM_TOO_FAR",
            "stages": STAGES if eligible else [],
        },
    }


@router.get("", response_model=ExamGoalPage)
def list_exam_goals(
    request: Request,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    ctx, owner = get_context(request), get_user(request)
    scope = {"owner": owner, "operation": "listExamGoals"}
    with ctx.db.transaction() as db:
        stmt = select(ExamGoal).where(ExamGoal.user_id == owner)
        if cursor:
            created, goal_id = decode_cursor(cursor, scope, secret=ctx.settings.signing_secret)
            stmt = stmt.where(
                (ExamGoal.created_at < created) | ((ExamGoal.created_at == created) & (ExamGoal.id < goal_id))
            )
        rows = db.scalars(
            stmt.order_by(ExamGoal.created_at.desc(), ExamGoal.id.desc()).limit(limit + 1)
        ).all()
        next_cursor = (
            encode_cursor(
                rows[limit - 1].created_at, rows[limit - 1].id, scope, secret=ctx.settings.signing_secret
            )
            if len(rows) > limit
            else None
        )
        return {"data": [to_response(goal) for goal in rows[:limit]], "next_cursor": next_cursor}


@router.post("", response_model=ExamGoalEnvelope, status_code=201)
def create_exam_goal(
    body: ExamGoalInput,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
):
    ctx, owner = get_context(request), get_user(request)
    data = body.model_dump(mode="json")

    def action(db):
        if body.exam_date < today_seoul():
            raise ApiError(422, "EXAM_PASSED", "Exam date must be today or later.")
        goal = ExamGoal(
            id=uid(),
            user_id=owner,
            title=body.title,
            exam_type=body.exam_type,
            exam_date=body.exam_date,
            status="active",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(goal)
        db.flush()
        return 201, {"data": to_response(goal)}

    return idempotent(
        ctx,
        owner,
        "createExamGoal",
        idempotency_key,
        {"method": "POST", "path": "/v1/exam-goals", "query": dict(request.query_params), "body": data},
        action,
    )


@router.patch("/{goalId}", response_model=ExamGoalEnvelope)
def update_exam_goal(goal_id: Annotated[UUID, Path(alias="goalId")], body: ExamGoalPatch, request: Request):
    ctx, owner = get_context(request), get_user(request)
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise ApiError(422, "VALIDATION_ERROR", "At least one changed field is required.")
    with ctx.db.transaction() as db:
        goal = db.scalar(
            select(ExamGoal).where(ExamGoal.id == str(goal_id), ExamGoal.user_id == owner).with_for_update()
        )
        if goal is None:
            raise ApiError(404, "NOT_FOUND", "Exam goal not found.")
        if "exam_date" in updates and updates["exam_date"] < today_seoul():
            raise ApiError(422, "EXAM_PASSED", "Exam date must be today or later.")
        for field, value in updates.items():
            setattr(goal, field, value)
        goal.updated_at = utcnow()
        return {"data": to_response(goal)}
