from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError

from app.a_gateway import lock_owned_plan, lock_owned_task, task_completed_count, task_concepts
from app.core import ApiError, get_context, get_user, idempotent, iso, uid, utcnow
from app.models import (
    Concept,
    ConceptVersion,
    ExamGoal,
    ImprovementAction,
    Job,
    Material,
    MaterialUnit,
    PlanTask,
    PlanTaskConcept,
    QuestionAttempt,
    QuestionSet,
    SessionReport,
    StudyPlan,
    StudyPlanMaterial,
    TeachbackSession,
)
from language_learning_api.routers.common import decode_cursor, encode_cursor
from language_learning_api.routers.exam_goals import today_seoul
from language_learning_api.schemas import (
    PlanTaskEnvelope,
    PlanTaskPatch,
    StudyPlanEnvelope,
    StudyPlanInput,
    StudyPlanPage,
    TodayEnvelope,
)

router = APIRouter(tags=["학습 계획과 오늘 학습"])
MAX_TASKS = 6000


def task_dict(db, task, owner, *, completed_count=None):
    concepts = task_concepts(db, task.id)
    concept_name = concepts[0]["concept_name"] if concepts else "학습"
    if len(concepts) > 1:
        concept_name += f" 외 {len(concepts) - 1}개"
    label = {"solve": "문제 풀기", "teach_back": "설명하기", "review": "복습하기"}[task.task_type]
    return {
        "id": task.id,
        "due_date": task.due_date.isoformat(),
        "task_type": task.task_type,
        "concept_ids": [c["concept_id"] for c in concepts],
        "concept_version_ids": [c["concept_version_id"] for c in concepts],
        "target_count": task.target_count,
        "completed_count": task_completed_count(db, task, owner)
        if completed_count is None
        else completed_count,
        "status": task.status,
        "completed_at": iso(task.completed_at),
        "title": f"{concept_name} · {label}",
        "display_order": task.ordinal,
        "concepts": concepts,
    }


def material_ids(db, plan_id):
    return list(
        db.scalars(
            select(StudyPlanMaterial.material_id)
            .where(StudyPlanMaterial.study_plan_id == plan_id)
            .order_by(StudyPlanMaterial.ordinal)
        )
    )


def plan_tasks(db, plan_id, *, lock=False):
    stmt = select(PlanTask).where(PlanTask.study_plan_id == plan_id).order_by(PlanTask.ordinal)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.scalars(stmt).all()


def plan_dict(db, plan):
    return {
        "id": plan.id,
        "exam_goal_id": plan.exam_goal_id,
        "revision": plan.revision,
        "status": plan.status,
        "generated_at": iso(plan.generated_at),
        "tasks": [task_dict(db, task, plan.user_id) for task in plan_tasks(db, plan.id)],
        "material_ids": material_ids(db, plan.id),
    }


def get_owned_plan(db, owner, plan_id):
    plan = db.scalar(select(StudyPlan).where(StudyPlan.id == str(plan_id), StudyPlan.user_id == str(owner)))
    if plan is None:
        raise ApiError(404, "NOT_FOUND", "Study plan not found.")
    return plan


def _valid_goal_days(goal):
    if goal.status != "active":
        raise ApiError(422, "EXAM_GOAL_INACTIVE", "Exam goal is inactive.")
    days = (goal.exam_date - today_seoul()).days
    if days < 0:
        raise ApiError(422, "EXAM_PASSED", "Exam date has passed.")
    if days > 180:
        raise ApiError(422, "EXAM_TOO_FAR", "Exam date is more than 180 days away.")
    return days


def _ensure_not_busy(db, owner, tasks):
    task_ids = [task.id for task in tasks]
    if not task_ids:
        return
    # NOWAIT obtains a current read even under MySQL REPEATABLE READ, while
    # refusing a job held by a worker (job -> goal) instead of creating a cycle.
    try:
        sets = db.scalars(
            select(QuestionSet.id)
            .where(QuestionSet.task_id.in_(task_ids), QuestionSet.user_id == owner)
            .order_by(QuestionSet.id)
            .with_for_update(nowait=True)
        ).all()
        attempts = db.scalars(
            select(QuestionAttempt.id)
            .where(QuestionAttempt.question_set_id.in_(sets), QuestionAttempt.user_id == owner)
            .order_by(QuestionAttempt.id)
            .with_for_update(nowait=True)
        ).all()
        sessions = db.scalars(
            select(TeachbackSession)
            .where(TeachbackSession.task_id.in_(task_ids), TeachbackSession.user_id == owner)
            .order_by(TeachbackSession.id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        ).all()
        jobs = select(Job.id).where(
            Job.user_id == owner,
            Job.status.in_(["queued", "running"]),
            or_(
                (Job.resource_type == "question_set") & Job.resource_id.in_(sets),
                (Job.resource_type == "attempt") & Job.resource_id.in_(attempts),
                (Job.resource_type == "session") & Job.resource_id.in_([s.id for s in sessions]),
            ),
        )
        if db.scalar(jobs.limit(1).with_for_update(nowait=True)):
            raise ApiError(409, "JOB_IN_PROGRESS", "A task has an unfinished job.")
        if any(s.status in ("active", "report_pending") for s in sessions):
            raise ApiError(409, "JOB_IN_PROGRESS", "A task has an active teach-back session.")
    except OperationalError as exc:
        if getattr(exc.orig, "args", ())[:1] == (3572,):
            raise ApiError(409, "JOB_IN_PROGRESS", "A task is being updated; try again.") from None
        raise


def _new_task(db, plan, ordinal, due, kind, target, concepts):
    task = PlanTask(
        id=uid(),
        study_plan_id=plan.id,
        ordinal=ordinal,
        due_date=due,
        task_type=kind,
        target_count=target,
        status="pending",
        completed_at=None,
        created_at=utcnow(),
    )
    db.add(task)
    db.flush()
    for concept in concepts:
        db.add(
            PlanTaskConcept(
                task_id=task.id,
                concept_id=concept["concept_id"],
                concept_version_id=concept["concept_version_id"],
            )
        )
    return task


@router.post("/v1/study-plans", response_model=StudyPlanEnvelope, status_code=201)
def create_study_plan(
    body: StudyPlanInput,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
):
    ctx, owner = get_context(request), get_user(request)
    data = body.model_dump(mode="json")

    def action(db):
        goal = db.scalar(
            select(ExamGoal)
            .where(ExamGoal.id == data["exam_goal_id"], ExamGoal.user_id == owner)
            .with_for_update()
        )
        if goal is None:
            raise ApiError(404, "NOT_FOUND", "Exam goal not found.")
        days = _valid_goal_days(goal)
        if db.scalar(
            select(StudyPlan.id)
            .where(StudyPlan.exam_goal_id == goal.id, StudyPlan.status == "active")
            .with_for_update()
        ):
            raise ApiError(409, "ACTIVE_PLAN_EXISTS", "An active plan already exists for this goal.")
        mids = data["material_ids"]
        materials = db.scalars(
            select(Material)
            .where(Material.id.in_(mids), Material.user_id == owner)
            .order_by(Material.id)
            .with_for_update()
        ).all()
        if len(materials) != len(mids) or any(
            m.processing_status != "ready" or m.archived for m in materials
        ):
            raise ApiError(422, "MATERIAL_NOT_READY", "All materials must be ready and unarchived.")
        versions = db.execute(
            select(Concept, ConceptVersion, MaterialUnit)
            .join(MaterialUnit, MaterialUnit.id == Concept.unit_id)
            .join(ConceptVersion, ConceptVersion.concept_id == Concept.id)
            .where(Concept.material_id.in_(mids))
            .order_by(Concept.id, ConceptVersion.version_no.desc())
            .with_for_update()
        ).all()
        latest = {}
        for row in versions:
            latest.setdefault(row[0].id, row)
        rows = [row for row in latest.values() if row[1].is_confirmed]
        rows.sort(
            key=lambda row: (mids.index(row[0].material_id), row[2].ordinal, row[0].created_at, row[0].id)
        )
        if not rows:
            raise ApiError(422, "NO_CONFIRMED_CONCEPTS", "No current confirmed concepts are available.")
        if len(rows) * 3 > MAX_TASKS:
            raise ApiError(422, "RESOURCE_LIMIT_REACHED", "The plan would exceed 6000 tasks.")
        plan = StudyPlan(
            id=uid(), user_id=owner, exam_goal_id=goal.id, revision=1, status="active", generated_at=utcnow()
        )
        db.add(plan)
        db.flush()
        for ordinal, mid in enumerate(mids, 1):
            db.add(StudyPlanMaterial(study_plan_id=plan.id, material_id=mid, ordinal=ordinal))
        specs = [
            (concept, version, kind)
            for concept, version, unit in rows
            for kind in ("solve", "teach_back", "review")
        ]
        today, span = today_seoul(), max(1, days)
        for index, (concept, version, kind) in enumerate(specs):
            due = today + timedelta(days=index * span // len(specs))
            _new_task(
                db,
                plan,
                index + 1,
                due,
                kind,
                5 if kind == "solve" else 1,
                [{"concept_id": concept.id, "concept_version_id": version.id}],
            )
        db.flush()
        return 201, {"data": plan_dict(db, plan)}

    return idempotent(
        ctx,
        owner,
        "createStudyPlan",
        idempotency_key,
        {"method": "POST", "path": "/v1/study-plans", "query": dict(request.query_params), "body": data},
        action,
    )


@router.get("/v1/study-plans", response_model=StudyPlanPage)
def list_study_plans(
    request: Request,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    exam_goal_id: UUID | None = None,
    status: str | None = None,
):
    ctx, owner = get_context(request), get_user(request)
    if status not in (None, "active", "superseded"):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid plan status.")
    scope = {
        "owner": owner,
        "operation": "listStudyPlans",
        "exam_goal_id": str(exam_goal_id) if exam_goal_id else None,
        "status": status,
    }
    with ctx.db.transaction() as db:
        stmt = select(StudyPlan).where(StudyPlan.user_id == owner)
        if exam_goal_id:
            stmt = stmt.where(StudyPlan.exam_goal_id == str(exam_goal_id))
        if status:
            stmt = stmt.where(StudyPlan.status == status)
        if cursor:
            created, item_id = decode_cursor(cursor, scope, secret=ctx.settings.signing_secret)
            stmt = stmt.where(
                (StudyPlan.generated_at < created)
                | ((StudyPlan.generated_at == created) & (StudyPlan.id < item_id))
            )
        rows = db.scalars(
            stmt.order_by(StudyPlan.generated_at.desc(), StudyPlan.id.desc()).limit(limit + 1)
        ).all()
        next_cursor = (
            encode_cursor(
                rows[limit - 1].generated_at, rows[limit - 1].id, scope, secret=ctx.settings.signing_secret
            )
            if len(rows) > limit
            else None
        )
        data = [
            {
                "id": p.id,
                "exam_goal_id": p.exam_goal_id,
                "revision": p.revision,
                "status": p.status,
                "generated_at": iso(p.generated_at),
                "material_ids": material_ids(db, p.id),
                "task_count": db.scalar(
                    select(func.count()).select_from(PlanTask).where(PlanTask.study_plan_id == p.id)
                ),
            }
            for p in rows[:limit]
        ]
        return {"data": data, "next_cursor": next_cursor}


@router.get("/v1/study-plans/{planId}", response_model=StudyPlanEnvelope)
def get_study_plan(plan_id: Annotated[UUID, Path(alias="planId")], request: Request):
    ctx, owner = get_context(request), get_user(request)
    with ctx.db.transaction() as db:
        return {"data": plan_dict(db, get_owned_plan(db, owner, plan_id))}


@router.post("/v1/study-plans/{planId}/reschedule", response_model=StudyPlanEnvelope, status_code=201)
def reschedule_study_plan(
    plan_id: Annotated[UUID, Path(alias="planId")],
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
):
    ctx, owner = get_context(request), get_user(request)
    plan_id = str(plan_id)

    def action(db):
        old = lock_owned_plan(db, owner, plan_id)
        if old.status != "active":
            raise ApiError(409, "PLAN_NOT_ACTIVE", "Only an active plan may be rescheduled.")
        goal = db.get(ExamGoal, old.exam_goal_id)
        if goal.status != "active":
            raise ApiError(422, "EXAM_GOAL_INACTIVE", "Exam goal is inactive.")
        days = max(1, (goal.exam_date - today_seoul()).days)
        remaining = [task for task in plan_tasks(db, old.id, lock=True) if task.status != "completed"]
        if not remaining:
            raise ApiError(422, "NO_REMAINING_TASKS", "The plan has no remaining tasks.")
        if len(remaining) > MAX_TASKS:
            raise ApiError(422, "RESOURCE_LIMIT_REACHED", "The plan would exceed 6000 tasks.")
        _ensure_not_busy(db, owner, remaining)
        counts = {t.id: task_completed_count(db, t, owner, authoritative=True) for t in remaining}
        old.status = "superseded"
        db.flush()  # Release active_goal_id before inserting the successor.
        new = StudyPlan(
            id=uid(),
            user_id=owner,
            exam_goal_id=goal.id,
            revision=old.revision + 1,
            status="active",
            generated_at=utcnow(),
        )
        db.add(new)
        db.flush()
        for ordinal, mid in enumerate(material_ids(db, old.id), 1):
            db.add(StudyPlanMaterial(study_plan_id=new.id, material_id=mid, ordinal=ordinal))
        today, span = today_seoul(), max(1, days)
        for index, task in enumerate(remaining):
            _new_task(
                db,
                new,
                index + 1,
                today + timedelta(days=index * span // len(remaining)),
                task.task_type,
                max(1, task.target_count - counts[task.id]),
                task_concepts(db, task.id),
            )
        db.flush()
        return 201, {"data": plan_dict(db, new)}

    return idempotent(
        ctx,
        owner,
        "rescheduleStudyPlan",
        idempotency_key,
        {
            "method": "POST",
            "path": f"/v1/study-plans/{plan_id}/reschedule",
            "query": dict(request.query_params),
            "body": {},
        },
        action,
    )


@router.patch("/v1/plan-tasks/{taskId}", response_model=PlanTaskEnvelope)
def update_plan_task(task_id: Annotated[UUID, Path(alias="taskId")], body: PlanTaskPatch, request: Request):
    ctx, owner = get_context(request), get_user(request)
    with ctx.db.transaction() as db:
        plan, task = lock_owned_task(db, owner, task_id, active=False)
        if plan.status != "active":
            raise ApiError(409, "PLAN_SUPERSEDED", "The plan has been superseded.")
        if body.status == "completed" and task.task_type != "review":
            raise ApiError(422, "STATUS_DERIVED_FROM_ACTIVITY", "Task completion is derived from activity.")
        if body.status == "skipped" and task.status == "completed":
            raise ApiError(422, "TASK_ALREADY_COMPLETED", "Completed tasks cannot be skipped.")
        if body.status == "skipped":
            _ensure_not_busy(db, owner, [task])
        if body.status == "pending" and (
            task.status not in ("skipped", "completed")
            or (task.status == "completed" and task.task_type != "review")
        ):
            raise ApiError(422, "INVALID_STATUS_TRANSITION", "This task cannot be reset to pending.")
        count = task_completed_count(db, task, owner, authoritative=True)
        if body.status == "skipped":
            task.status = "skipped"
        elif task.task_type == "review":
            task.status = body.status
        else:
            task.status = "completed" if count >= task.target_count else "in_progress" if count else "pending"
        task.completed_at = (task.completed_at or utcnow()) if task.status == "completed" else None
        db.flush()
        return {
            "data": task_dict(
                db, task, owner, completed_count=task_completed_count(db, task, owner, authoritative=True)
            )
        }


@router.get("/v1/today", response_model=TodayEnvelope)
def get_today(request: Request, exam_goal_id: UUID | None = None):
    ctx, owner = get_context(request), get_user(request)
    day = today_seoul()
    with ctx.db.transaction() as db:
        goal = plan = None
        if exam_goal_id:
            goal = db.scalar(
                select(ExamGoal).where(ExamGoal.id == str(exam_goal_id), ExamGoal.user_id == owner)
            )
            if goal is None:
                raise ApiError(404, "NOT_FOUND", "Exam goal not found.")
            if goal.status == "active":
                plan = db.scalar(
                    select(StudyPlan).where(
                        StudyPlan.user_id == owner,
                        StudyPlan.exam_goal_id == goal.id,
                        StudyPlan.status == "active",
                    )
                )
        else:
            pair = db.execute(
                select(ExamGoal, StudyPlan)
                .join(StudyPlan, StudyPlan.exam_goal_id == ExamGoal.id)
                .where(
                    ExamGoal.user_id == owner,
                    ExamGoal.status == "active",
                    StudyPlan.user_id == owner,
                    StudyPlan.status == "active",
                )
                .order_by(StudyPlan.generated_at.desc(), StudyPlan.id.desc())
                .limit(1)
            ).first()
            if pair:
                goal, plan = pair
        tasks = [t for t in plan_tasks(db, plan.id) if t.due_date <= day] if plan else []
        denominator = sum(t.status != "skipped" for t in tasks)
        progress = sum(t.status == "completed" for t in tasks) / denominator if denominator else 0
        action = None
        if plan:
            pins = (
                select(PlanTaskConcept.concept_id)
                .join(PlanTask, PlanTask.id == PlanTaskConcept.task_id)
                .where(PlanTask.study_plan_id == plan.id)
            )
            action = db.scalar(
                select(SessionReport.next_action)
                .join(ImprovementAction, ImprovementAction.report_id == SessionReport.id)
                .join(TeachbackSession, TeachbackSession.id == SessionReport.session_id)
                .where(
                    TeachbackSession.user_id == owner,
                    ImprovementAction.status == "pending",
                    ImprovementAction.concept_id.in_(pins),
                )
                .order_by(ImprovementAction.updated_at.desc(), ImprovementAction.id.desc())
                .limit(1)
            )
        return {
            "data": {
                "date": day,
                "progress": progress,
                "tasks": [task_dict(db, task, owner) for task in tasks],
                "next_action": action,
                "exam_goal_id": goal.id if goal else None,
                "study_plan_id": plan.id if plan else None,
                "days_remaining": max(0, (goal.exam_date - day).days) if goal else None,
            }
        }
