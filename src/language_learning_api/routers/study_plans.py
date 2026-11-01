from datetime import date, datetime, timedelta, timezone
import json
import hashlib
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Header, HTTPException, Path, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from language_learning_api.auth import CurrentUser, DatabaseSession
from language_learning_api.models import (Concept, ConceptVersion, ExamGoal, IdempotencyRecord, ImprovementAction, Job, Material,
    PlanTask, QuestionAttempt, QuestionSet, StudyPlan, StudyPlanMaterial, TeachbackSession, Unit, utc_now)
from language_learning_api.routers.common import decode_cursor, encode_cursor, request_hash
from language_learning_api.schemas import (AcceptedEnvelope, PlanTaskEnvelope, PlanTaskPatch, PlanTaskResponse,
    StudyPlanEnvelope, StudyPlanInput, StudyPlanPage, StudyPlanResponse, StudyPlanSummary, TodayEnvelope)

router = APIRouter(tags=["학습 계획과 오늘 학습"])
try:
    TODAY_TZ = ZoneInfo("Asia/Seoul")
except ZoneInfoNotFoundError:  # Windows Python installations may omit the OS timezone database.
    TODAY_TZ = timezone(timedelta(hours=9))
MAX_TASKS = 6000


def today_seoul() -> date:
    return datetime.now(TODAY_TZ).date()


def task_title(task: PlanTask) -> str:
    concept = task.concepts[0]["concept_name"] if task.concepts else "학습"
    label = {"solve": "문제 풀기", "teach_back": "설명하기", "review": "복습하기", "concept": "개념 학습"}[task.task_type]
    return f"{concept} · {label}"


def task_dict(task: PlanTask) -> dict:
    return {
        "id": task.id, "due_date": task.due_date, "task_type": task.task_type,
        "concept_ids": task.concept_ids, "concept_version_ids": task.concept_version_ids,
        "target_count": task.target_count, "completed_count": task.completed_count,
        "status": task.status, "completed_at": task.completed_at, "title": task_title(task),
        "display_order": task.display_order, "concepts": task.concepts,
    }


def plan_dict(plan: StudyPlan) -> dict:
    return {"id": plan.id, "exam_goal_id": plan.exam_goal_id, "revision": plan.revision,
        "status": plan.status, "generated_at": plan.generated_at,
        "tasks": [task_dict(t) for t in sorted(plan.tasks, key=lambda x: x.display_order)],
        "material_ids": [link.material_id for link in sorted(plan.material_links, key=lambda x: x.ordinal)]}


def get_owned_plan(db, owner_id: UUID, plan_id: UUID, *, with_tasks=True) -> StudyPlan:
    stmt = select(StudyPlan).where(StudyPlan.id == plan_id, StudyPlan.owner_id == owner_id)
    if with_tasks:
        stmt = stmt.options(selectinload(StudyPlan.tasks))
    plan = db.scalar(stmt)
    if plan is None:
        raise HTTPException(404, "NOT_FOUND")
    return plan


@router.post("/v1/study-plans", response_model=StudyPlanEnvelope, status_code=201)
def create_study_plan(body: StudyPlanInput, user: CurrentUser, db: DatabaseSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]):
    operation = "POST:/v1/study-plans"
    digest = request_hash(body.model_dump(mode="json"))
    previous = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.owner_id == user.id,
        IdempotencyRecord.operation == operation, IdempotencyRecord.key == idempotency_key))
    if previous:
        created = previous.created_at.replace(tzinfo=timezone.utc) if previous.created_at.tzinfo is None else previous.created_at
        if datetime.now(timezone.utc) - created >= timedelta(hours=24):
            db.delete(previous); db.flush(); previous = None
    if previous:
        if previous.request_hash != digest: raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
        return previous.response_body
    goal = db.scalar(select(ExamGoal).where(ExamGoal.id == body.exam_goal_id, ExamGoal.owner_id == user.id).with_for_update())
    if goal is None:
        raise HTTPException(404, "NOT_FOUND")
    if goal.status != "active":
        raise HTTPException(422, "EXAM_GOAL_INACTIVE")
    days = (goal.exam_date - today_seoul()).days
    if days < 0:
        raise HTTPException(422, "EXAM_PASSED")
    if days > 180:
        raise HTTPException(422, "EXAM_TOO_FAR")
    prior = db.scalar(select(StudyPlan).where(StudyPlan.exam_goal_id == goal.id, StudyPlan.status == "active"))
    if prior:
        raise HTTPException(409, "ACTIVE_PLAN_EXISTS")
    materials = list(db.scalars(select(Material).where(Material.id.in_(body.material_ids), Material.owner_id == user.id)))
    if len(materials) != len(body.material_ids) or any(m.status != "ready" or m.archived for m in materials):
        raise HTTPException(422, "MATERIAL_NOT_READY")
    units = list(db.scalars(select(Unit).where(Unit.material_id.in_(body.material_ids)).order_by(Unit.material_id, Unit.ordinal, Unit.id)))
    concepts = list(db.scalars(select(Concept).where(Concept.unit_id.in_([u.id for u in units])))) if units else []
    by_unit = {u.id: u for u in units}
    concept_ordered = sorted(concepts, key=lambda c: (body.material_ids.index(next(u.material_id for u in units if u.id == c.unit_id)), by_unit[c.unit_id].ordinal, c.created_at, c.id))
    concept_rows = []
    for concept in concept_ordered:
        if not concept.current_version_id:
            continue
        version = db.get(ConceptVersion, concept.current_version_id)
        if not version or version.concept_id != concept.id or version.status != "confirmed":
            continue
        unit = by_unit[concept.unit_id]
        concept_rows.append({"concept_id": concept.id, "concept_version_id": version.id, "concept_name": version.name,
            "material_id": unit.material_id, "unit_id": unit.id, "unit_title": unit.title})
    if not concept_rows:
        raise HTTPException(422, "NO_CONFIRMED_CONCEPTS")
    task_specs = [(c, kind, 5 if kind == "solve" else 1) for c in concept_rows for kind in ("solve", "teach_back", "review")]
    if len(task_specs) > MAX_TASKS:
        raise HTTPException(422, "RESOURCE_LIMIT_REACHED")
    plan = StudyPlan(owner_id=user.id, exam_goal_id=goal.id, revision=1, status="active")
    db.add(plan)
    db.flush()
    for ordinal, material_id in enumerate(body.material_ids, start=1):
        plan.material_links.append(StudyPlanMaterial(material_id=material_id, ordinal=ordinal))
    span = max(1, days)
    for index, (concept, kind, target) in enumerate(task_specs):
        due = today_seoul() + timedelta(days=min(max(0, days - 1), (index * span) // len(task_specs)))
        concept_json = {key: str(value) if isinstance(value, UUID) else value for key, value in concept.items()}
        plan.tasks.append(PlanTask(due_date=due, task_type=kind, concept_ids=[str(concept["concept_id"])],
            concept_version_ids=[str(concept["concept_version_id"])], concepts=[concept_json], target_count=target,
            completed_count=0, status="pending", display_order=index + 1))
    db.flush()
    response = {"data": plan_dict(plan)}
    response = jsonable_encoder(response)
    db.add(IdempotencyRecord(owner_id=user.id, operation=operation, key=idempotency_key, request_hash=digest,
        response_status=201, response_body=response)); db.commit()
    return response


@router.get("/v1/study-plans", response_model=StudyPlanPage)
def list_study_plans(user: CurrentUser, db: DatabaseSession,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    exam_goal_id: UUID | None = None, status: str | None = None):
    if status not in (None, "active", "superseded"):
        raise HTTPException(422, "VALIDATION_ERROR")
    filters = {"exam_goal_id": str(exam_goal_id) if exam_goal_id else None, "status": status}
    stmt = select(StudyPlan).where(StudyPlan.owner_id == user.id)
    if exam_goal_id:
        stmt = stmt.where(StudyPlan.exam_goal_id == exam_goal_id)
    if status:
        stmt = stmt.where(StudyPlan.status == status)
    if cursor:
        created, item_id = decode_cursor(cursor, filters)
        stmt = stmt.where((StudyPlan.generated_at < created) | ((StudyPlan.generated_at == created) & (StudyPlan.id < item_id)))
    rows = list(db.scalars(stmt.options(selectinload(StudyPlan.tasks)).order_by(StudyPlan.generated_at.desc(), StudyPlan.id.desc()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].generated_at, rows[-1].id, filters) if more and rows else None
    data = [StudyPlanSummary(id=p.id, exam_goal_id=p.exam_goal_id, revision=p.revision, status=p.status,
        generated_at=p.generated_at, material_ids=p.material_ids, task_count=len(p.tasks)) for p in rows]
    return {"data": data, "next_cursor": next_cursor}


@router.get("/v1/study-plans/{planId}", response_model=StudyPlanEnvelope)
def get_study_plan(plan_id: Annotated[UUID, Path(alias="planId")], user: CurrentUser, db: DatabaseSession):
    return {"data": plan_dict(get_owned_plan(db, user.id, plan_id))}


@router.post("/v1/study-plans/{planId}/reschedule", response_model=StudyPlanEnvelope, status_code=201)
def reschedule_study_plan(plan_id: Annotated[UUID, Path(alias="planId")], user: CurrentUser, db: DatabaseSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]):
    operation = f"POST:/v1/study-plans/{plan_id}/reschedule"
    digest = request_hash({"plan_id": str(plan_id)})
    previous = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.owner_id == user.id,
        IdempotencyRecord.operation == operation, IdempotencyRecord.key == idempotency_key))
    if previous:
        created = previous.created_at.replace(tzinfo=timezone.utc) if previous.created_at.tzinfo is None else previous.created_at
        if datetime.now(timezone.utc) - created >= timedelta(hours=24):
            db.delete(previous); db.flush(); previous = None
    if previous:
        if previous.request_hash != digest: raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
        return previous.response_body
    old = get_owned_plan(db, user.id, plan_id)
    if old.status != "active":
        raise HTTPException(409, "PLAN_NOT_ACTIVE")
    goal = db.scalar(select(ExamGoal).where(ExamGoal.id == old.exam_goal_id, ExamGoal.owner_id == user.id))
    today = today_seoul()
    remaining = [t for t in old.tasks if t.status != "completed"]
    if not remaining:
        raise HTTPException(422, "NO_REMAINING_TASKS")
    task_ids = [t.id for t in remaining]
    busy = db.scalar(select(TeachbackSession.id).where(TeachbackSession.owner_id == user.id,
        TeachbackSession.task_id.in_(task_ids), TeachbackSession.status.in_(["active", "report_pending"])).limit(1))
    if not busy:
        busy = db.scalar(select(Job.id).where(Job.owner_id == user.id, Job.status.in_(["queued", "running", "outcome_unknown"]),
            Job.resource_type == "question_set", Job.resource_id.in_(select(QuestionSet.id).where(QuestionSet.task_id.in_(task_ids)))).limit(1))
    if not busy:
        busy = db.scalar(select(Job.id).join(QuestionAttempt, Job.resource_id == QuestionAttempt.id).where(
            Job.owner_id == user.id, Job.status.in_(["queued", "running", "outcome_unknown"]),
            Job.resource_type == "attempt", QuestionAttempt.question_set_id.in_(select(QuestionSet.id).where(QuestionSet.task_id.in_(task_ids)))).limit(1))
    if busy:
        raise HTTPException(409, "JOB_IN_PROGRESS")
    revision = max(db.scalars(select(StudyPlan.revision).where(StudyPlan.exam_goal_id == goal.id)), default=old.revision) + 1
    new = StudyPlan(owner_id=user.id, exam_goal_id=goal.id, revision=revision, status="active")
    old.status = "superseded"
    db.add(new); db.flush()
    for link in old.material_links:
        new.material_links.append(StudyPlanMaterial(material_id=link.material_id, ordinal=link.ordinal))
    days = max(1, (goal.exam_date - today).days)
    for index, task in enumerate(remaining):
        due = today + timedelta(days=min(max(0, days - 1), index * days // len(remaining)))
        new.tasks.append(PlanTask(due_date=due, task_type=task.task_type, concept_ids=task.concept_ids,
            concept_version_ids=task.concept_version_ids, concepts=task.concepts,
            target_count=max(1, task.target_count - task.completed_count), completed_count=0,
            status="pending", display_order=index + 1))
    db.flush()
    response = {"data": plan_dict(new)}
    response = jsonable_encoder(response)
    db.add(IdempotencyRecord(owner_id=user.id, operation=operation, key=idempotency_key, request_hash=digest,
        response_status=201, response_body=response)); db.commit()
    return response


@router.patch("/v1/plan-tasks/{taskId}", response_model=PlanTaskEnvelope)
def update_plan_task(task_id: Annotated[UUID, Path(alias="taskId")], body: PlanTaskPatch, user: CurrentUser, db: DatabaseSession):
    task = db.scalar(select(PlanTask).join(StudyPlan).where(PlanTask.id == task_id, StudyPlan.owner_id == user.id))
    if task is None:
        raise HTTPException(404, "NOT_FOUND")
    plan = db.get(StudyPlan, task.plan_id)
    if plan.status != "active":
        raise HTTPException(409, "PLAN_SUPERSEDED")
    if body.status == "completed" and task.task_type != "review":
        raise HTTPException(422, "STATUS_DERIVED_FROM_ACTIVITY")
    if body.status == "skipped" and task.status == "completed":
        raise HTTPException(422, "TASK_ALREADY_COMPLETED")
    if body.status == "skipped":
        active_statuses = ["queued", "running", "outcome_unknown"]
        busy = None
        if task.task_type == "solve":
            task_sets = select(QuestionSet.id).where(QuestionSet.task_id == task.id)
            active_attempts = select(QuestionAttempt.id).where(QuestionAttempt.question_set_id.in_(task_sets))
            busy = db.scalar(select(Job.id).where(Job.owner_id == user.id, Job.status.in_(active_statuses),
                Job.resource_type == "question_set", Job.resource_id.in_(task_sets)).limit(1))
            if not busy:
                busy = db.scalar(select(Job.id).where(Job.owner_id == user.id, Job.status.in_(active_statuses),
                    Job.resource_type == "attempt", Job.resource_id.in_(active_attempts)).limit(1))
        elif task.task_type == "teach_back":
            busy = db.scalar(select(TeachbackSession.id).where(TeachbackSession.owner_id == user.id,
                TeachbackSession.task_id == task.id, TeachbackSession.status.in_(["active", "report_pending"])).limit(1))
        if busy:
            raise HTTPException(409, "JOB_IN_PROGRESS")
    if body.status == "pending" and (task.status not in ("skipped", "completed") or
        (task.status == "completed" and task.task_type != "review")):
        raise HTTPException(422, "INVALID_STATUS_TRANSITION")
    if task.task_type == "solve":
        task.completed_count = db.scalar(select(func.count(func.distinct(QuestionAttempt.question_id))).where(QuestionAttempt.owner_id == user.id, QuestionAttempt.status == "succeeded", QuestionAttempt.question_set_id.in_(select(QuestionSet.id).where(QuestionSet.task_id == task.id))) ) or 0
    elif task.task_type == "teach_back":
        task.completed_count = db.scalar(select(func.count(TeachbackSession.id)).where(TeachbackSession.owner_id == user.id, TeachbackSession.task_id == task.id, TeachbackSession.status == "completed")) or 0
    if body.status == "skipped":
        task.status = "skipped"
    elif body.status == "pending":
        task.completed_at = None
        if task.task_type == "review":
            task.completed_count = 0; task.status = "pending"
        else:
            task.status = "completed" if task.completed_count >= task.target_count else "in_progress" if task.completed_count else "pending"
            if task.status == "completed": task.completed_at = utc_now()
    elif task.task_type == "review":
        task.status = "completed"; task.completed_count = task.target_count; task.completed_at = utc_now()
    elif task.completed_count >= task.target_count:
        task.status = "completed"; task.completed_at = task.completed_at or utc_now()
    else:
        task.status = "in_progress" if task.completed_count else "pending"
    db.commit(); db.refresh(task)
    return {"data": task_dict(task)}


@router.get("/v1/today", response_model=TodayEnvelope)
def get_today(user: CurrentUser, db: DatabaseSession, exam_goal_id: UUID | None = None):
    day = today_seoul()
    goal = None
    plan = None
    if exam_goal_id:
        goal = db.scalar(select(ExamGoal).where(ExamGoal.id == exam_goal_id, ExamGoal.owner_id == user.id))
        if goal is None:
            raise HTTPException(404, "NOT_FOUND")
        if goal.status == "active":
            plan = db.scalar(select(StudyPlan).where(StudyPlan.owner_id == user.id, StudyPlan.exam_goal_id == goal.id,
                StudyPlan.status == "active").options(selectinload(StudyPlan.tasks)).order_by(StudyPlan.generated_at.desc(), StudyPlan.id.desc()))
    else:
        pair = db.execute(select(ExamGoal, StudyPlan).join(StudyPlan, StudyPlan.exam_goal_id == ExamGoal.id)
            .options(selectinload(StudyPlan.tasks), selectinload(StudyPlan.material_links))
            .where(ExamGoal.owner_id == user.id, ExamGoal.status == "active", StudyPlan.owner_id == user.id,
                StudyPlan.status == "active").order_by(StudyPlan.generated_at.desc(), StudyPlan.id.desc()).limit(1)).first()
        if pair:
            goal, plan = pair
    tasks = [t for t in plan.tasks if t.due_date <= day] if plan else []
    tasks.sort(key=lambda t: t.display_order)
    denom = sum(t.status != "skipped" for t in tasks)
    progress = sum(t.status == "completed" for t in tasks) / denom if denom else 0
    action = None
    if plan:
        pinned = {concept_id for task in plan.tasks for concept_id in task.concept_ids}
        actions = list(db.scalars(select(ImprovementAction).where(ImprovementAction.owner_id == user.id,
            ImprovementAction.status == "pending").order_by(ImprovementAction.updated_at.desc())))
        action_row = next((item for item in actions if str(item.concept_id) in pinned), None)
        action = action_row.text if action_row else None
    return {"data": {"date": day, "progress": progress, "tasks": [task_dict(t) for t in tasks], "next_action": action,
        "exam_goal_id": goal.id if goal else None, "study_plan_id": plan.id if plan else None,
        "days_remaining": max(0, (goal.exam_date - day).days) if goal else None}}
