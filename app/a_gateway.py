"""Canonical A task boundary shared by plans, grading and teach-back.

Mutations lock goal, plan, then task. The preliminary ID lookups are only used
to locate those locks; ownership/state are checked again under the locks.
"""

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .core import ApiError, utcnow
from .models import (
    Concept,
    ConceptVersion,
    ExamGoal,
    Material,
    MaterialUnit,
    PlanTask,
    PlanTaskConcept,
    QuestionAttempt,
    QuestionSet,
    QuestionSetItem,
    StudyPlan,
    StudyPlanMaterial,
    TeachbackSession,
)
from .ports import QuestionTaskState, TaskGateway


def lock_owned_plan(db, user_id, plan_id, *, active=False):
    user_id, plan_id = str(user_id), str(plan_id)
    goal_id = db.scalar(
        select(StudyPlan.exam_goal_id).where(StudyPlan.id == plan_id, StudyPlan.user_id == user_id)
    )
    if goal_id is None:
        raise ApiError(404, "NOT_FOUND", "Study plan not found.")
    goal = db.scalar(
        select(ExamGoal)
        .where(ExamGoal.id == goal_id, ExamGoal.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    plan = db.scalar(
        select(StudyPlan)
        .where(StudyPlan.id == plan_id, StudyPlan.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if goal is None or plan is None or plan.exam_goal_id != goal.id:
        raise ApiError(404, "NOT_FOUND", "Study plan not found.")
    if active and (plan.status != "active" or goal.status != "active"):
        raise ApiError(409, "STALE_TASK", "Study plan is inactive.")
    # Keep the current-read goal alive in SQLAlchemy's weak identity map for
    # callers that inspect it after this function returns.
    db.info.setdefault("a_locked_goals", {})[goal.id] = goal
    return plan


def lock_owned_task(db, user_id, task_id, *, active=True, task_type=None):
    user_id, task_id = str(user_id), str(task_id)
    plan_id = db.scalar(
        select(PlanTask.study_plan_id)
        .join(StudyPlan, StudyPlan.id == PlanTask.study_plan_id)
        .where(PlanTask.id == task_id, StudyPlan.user_id == user_id)
    )
    if plan_id is None:
        raise ApiError(404, "NOT_FOUND", "Plan task not found.")
    plan = lock_owned_plan(db, user_id, plan_id, active=active)
    task = db.scalar(
        select(PlanTask)
        .where(PlanTask.id == task_id, PlanTask.study_plan_id == plan.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise ApiError(404, "NOT_FOUND", "Plan task not found.")
    if active and task.status == "skipped":
        raise ApiError(409, "STALE_TASK", "Plan task is skipped.")
    if task_type is not None and task.task_type != task_type:
        raise ApiError(422, "INVALID_TASK_TYPE", "Task type does not match this activity.")
    return plan, task


def task_completed_count(db, task, user_id, *, authoritative=False):
    """Incorrect but graded answers count; repeated answers count only once."""
    if task.task_type == "review":
        return task.target_count if task.status == "completed" else 0
    if task.task_type == "solve":
        stmt = (
            select(QuestionAttempt.question_id)
            .join(QuestionSet, QuestionSet.id == QuestionAttempt.question_set_id)
            .where(
                QuestionAttempt.user_id == str(user_id),
                QuestionSet.user_id == str(user_id),
                QuestionSet.task_id == task.id,
                QuestionAttempt.status == "graded",
            )
        )
        if authoritative:
            stmt = stmt.with_for_update()
        return len(set(db.scalars(stmt)))
    stmt = select(TeachbackSession.id).where(
        TeachbackSession.user_id == str(user_id),
        TeachbackSession.task_id == task.id,
        TeachbackSession.status == "completed",
    )
    if authoritative:
        stmt = stmt.with_for_update()
    return len(db.scalars(stmt).all())


def _update_progress(db, user_id, task_id, task_type):
    plan, task = lock_owned_task(db, user_id, task_id, active=False, task_type=task_type)
    goal = db.get(ExamGoal, plan.exam_goal_id)
    if plan.status != "active" or goal.status != "active" or task.status == "skipped":
        return task
    count = task_completed_count(db, task, user_id, authoritative=True)
    task.status = "completed" if count >= task.target_count else "in_progress" if count else "pending"
    task.completed_at = (task.completed_at or utcnow()) if task.status == "completed" else None
    return task


def update_solve_progress(db, user_id, task_id):
    return _update_progress(db, user_id, task_id, "solve")


def update_teachback_progress(db, user_id, task_id):
    return _update_progress(db, user_id, task_id, "teach_back")


def task_concepts(db, task_id):
    rows = db.execute(
        select(PlanTaskConcept, ConceptVersion, Concept, MaterialUnit)
        .join(ConceptVersion, ConceptVersion.id == PlanTaskConcept.concept_version_id)
        .join(Concept, Concept.id == PlanTaskConcept.concept_id)
        .join(MaterialUnit, MaterialUnit.id == Concept.unit_id)
        .where(PlanTaskConcept.task_id == str(task_id))
        .order_by(PlanTaskConcept.concept_version_id)
    ).all()
    return [
        {
            "concept_id": pin.concept_id,
            "concept_version_id": pin.concept_version_id,
            "concept_name": version.name,
            "material_id": concept.material_id,
            "unit_id": unit.id,
            "unit_title": unit.title,
        }
        for pin, version, concept, unit in rows
    ]


class CanonicalTaskGateway(TaskGateway):
    def read_question_task(self, session, user_id, task_id):
        try:
            plan, task = lock_owned_task(session, user_id, task_id, active=False)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        pins = task_concepts(session, task.id)
        units = {p["unit_id"] for p in pins}
        if len(units) != 1:
            raise ApiError(422, "RELATION_MISMATCH", "Task must pin concepts from one unit.")
        goal = session.get(ExamGoal, plan.exam_goal_id)
        return QuestionTaskState(
            task_id=task.id,
            user_id=plan.user_id,
            unit_id=next(iter(units)),
            concept_version_ids=frozenset(p["concept_version_id"] for p in pins),
            task_type=task.task_type,
            active_revision=plan.status == "active" and goal.status == "active" and task.status != "skipped",
            target_count=task.target_count,
            completed_count=task_completed_count(session, task, user_id, authoritative=True),
        )

    def material_in_use(self, session, user_id, material_id):
        material = session.get(Material, str(material_id))
        if material is None or material.user_id != str(user_id):
            raise ApiError(404, "NOT_FOUND", "Material not found.")
        # Retained history blocks deletion. A worker can hold an attempt/session
        # before needing a source FK lock; never wait for it while deletion owns
        # the material. A contended reference is conservatively still in use.
        probes = (
            select(StudyPlanMaterial.material_id).where(StudyPlanMaterial.material_id == material.id),
            select(PlanTaskConcept.task_id)
            .join(Concept, Concept.id == PlanTaskConcept.concept_id)
            .where(Concept.material_id == material.id),
            select(TeachbackSession.id)
            .join(ConceptVersion, ConceptVersion.id == TeachbackSession.concept_version_id)
            .join(Concept, Concept.id == ConceptVersion.concept_id)
            .where(Concept.material_id == material.id),
        )
        try:
            return any(
                session.scalar(probe.limit(1).with_for_update(nowait=True)) is not None for probe in probes
            )
        except OperationalError as exc:
            if getattr(exc.orig, "args", ())[:1] == (3572,):
                return True
            raise

    def read_question_attempts(self, session, user_id, set_id, question_ids):
        from language_learning_api.routers.questions import attempt_dto

        user_id, set_id = str(user_id), str(set_id)
        question_set = session.get(QuestionSet, set_id)
        if question_set is None or question_set.user_id != user_id:
            raise ApiError(404, "NOT_FOUND", "Question set not found.")
        requested = {str(item) for item in question_ids}
        members = set(
            session.scalars(
                select(QuestionSetItem.question_id).where(
                    QuestionSetItem.set_id == set_id, QuestionSetItem.question_id.in_(requested)
                )
            )
        )
        if members != requested:
            raise ApiError(404, "NOT_FOUND", "Question not found in this set.")
        result = {qid: {"latest_attempt": None, "has_graded_attempt": False} for qid in requested}
        attempts = session.scalars(
            select(QuestionAttempt)
            .where(
                QuestionAttempt.user_id == user_id,
                QuestionAttempt.question_set_id == set_id,
                QuestionAttempt.question_id.in_(requested),
            )
            .order_by(QuestionAttempt.answered_at.desc(), QuestionAttempt.id.desc())
        )
        for attempt in attempts:
            item = result[attempt.question_id]
            if item["latest_attempt"] is None:
                item["latest_attempt"] = attempt_dto(attempt, session)
            item["has_graded_attempt"] |= attempt.status == "graded"
        return result
