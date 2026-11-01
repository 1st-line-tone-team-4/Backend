"""Atomic callbacks used by the B-part worker to finish A-part jobs.

The worker should call these functions inside its own worker task after validating
the provider output. They intentionally do not make AI calls or trust caller-owned IDs.
"""
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from language_learning_api.models import (ConceptVersion, ImprovementAction, Job, PlanTask, Question,
    QuestionAttempt, QuestionSet, SessionReport, StudyPlan, TeachbackMessage, TeachbackSession, utc_now)


def complete_grade(db: Session, job_id: UUID, *, is_correct: bool, correct_answer: dict, explanation: str) -> None:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if not job or job.kind != "grade" or job.status not in ("queued", "running"):
        raise HTTPException(409, "JOB_NOT_ACTIVE")
    attempt = db.scalar(select(QuestionAttempt).where(QuestionAttempt.id == job.resource_id, QuestionAttempt.owner_id == job.owner_id).with_for_update())
    if not attempt: raise HTTPException(404, "ATTEMPT_NOT_FOUND")
    if attempt.job_id != job.id: raise HTTPException(409, "STALE_JOB")
    attempt.status = "succeeded"
    attempt.result = {"is_correct": is_correct, "correct_answer": correct_answer, "explanation": explanation}
    job.status = "succeeded"; job.retryable = False; job.error_code = None
    job.result_url = f"/v1/attempts/{attempt.id}"; job.updated_at = utc_now()
    _update_solve_task(db, attempt)
    db.commit()


def complete_message(db: Session, job_id: UUID, *, assistant_content: str) -> None:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if not job or job.kind != "message" or job.status not in ("queued", "running"):
        raise HTTPException(409, "JOB_NOT_ACTIVE")
    session = db.scalar(select(TeachbackSession).where(TeachbackSession.id == job.resource_id, TeachbackSession.owner_id == job.owner_id).with_for_update())
    if not session or session.status != "active": raise HTTPException(409, "SESSION_NOT_ACTIVE")
    if session.latest_job_id != job.id: raise HTTPException(409, "STALE_JOB")
    last_seq = db.scalar(select(func.max(TeachbackMessage.sequence_no)).where(TeachbackMessage.session_id == session.id)) or 0
    db.add(TeachbackMessage(session_id=session.id, role="assistant", content=assistant_content,
        input_mode="text", sequence_no=last_seq + 1))
    session.question_count += 1
    job.status = "succeeded"; job.retryable = False; job.error_code = None
    job.result_url = f"/v1/teachback-sessions/{session.id}/messages"; job.updated_at = utc_now()
    db.commit()


def complete_report(db: Session, job_id: UUID, *, items: list[dict], next_action: str) -> SessionReport:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if not job or job.kind != "report" or job.status not in ("queued", "running"):
        raise HTTPException(409, "JOB_NOT_ACTIVE")
    session = db.scalar(select(TeachbackSession).where(TeachbackSession.id == job.resource_id, TeachbackSession.owner_id == job.owner_id).with_for_update())
    if not session or session.status != "report_pending": raise HTTPException(409, "SESSION_NOT_REPORT_PENDING")
    if session.latest_job_id != job.id: raise HTTPException(409, "STALE_JOB")
    version = db.get(ConceptVersion, session.concept_version_id)
    if not version: raise HTTPException(409, "CONCEPT_VERSION_NOT_FOUND")
    checklist = version.checklist or []
    valid_statuses = {"explained", "partial", "omission", "error", "unverified"}
    required_ids = {str(item.get("id", item.get("checklist_item_id"))) for item in checklist}
    received_ids = {str(item.get("checklist_item_id")) for item in items}
    valid_chunks = {str(item.get("chunk_id")) for item in version.evidence or [] if item.get("chunk_id")}
    evidence_invalid = any(
        any(str(evidence.get("chunk_id")) not in valid_chunks for evidence in item.get("evidence", []))
        or (item.get("status") == "error" and not item.get("evidence"))
        for item in items
    )
    if len(items) != len(checklist) or required_ids != received_ids or any(item.get("status") not in valid_statuses for item in items) or evidence_invalid:
        raise HTTPException(422, "INVALID_REPORT_OUTPUT")
    score = 0.0; assessable = 0
    for item in items:
        if item["status"] == "unverified": continue
        assessable += 1
        score += {"explained": 1.0, "partial": 0.5, "omission": 0.0, "error": 0.0}[item["status"]]
    coverage = round(score / assessable, 4) if assessable else None
    report = SessionReport(session_id=session.id, concept_version_id=version.id, version_no=version.version_no,
        coverage=coverage, coverage_formula="weighted-v1", assessable_count=assessable,
        weighted_score=score, items=jsonable_encoder(items), next_action=next_action)
    db.add(report); db.flush()
    action = ImprovementAction(owner_id=session.owner_id, report_id=report.id, concept_id=session.concept_id,
        text=next_action, status="pending")
    db.add(action); db.flush(); report.action_id = action.id
    session.status = "completed"; session.completed_at = utc_now()
    job.status = "succeeded"; job.retryable = False; job.error_code = None
    job.result_url = f"/v1/teachback-sessions/{session.id}/report"; job.updated_at = utc_now()
    _update_teachback_task(db, session)
    db.commit(); db.refresh(report)
    return report


def _update_solve_task(db: Session, attempt: QuestionAttempt) -> None:
    question_set = db.get(QuestionSet, attempt.question_set_id)
    if not question_set or not question_set.task_id: return
    task = db.scalar(select(PlanTask).where(PlanTask.id == question_set.task_id).with_for_update())
    if not task: return
    plan = db.get(StudyPlan, task.plan_id)
    if not plan or plan.status != "active" or task.status == "skipped": return
    completed = db.scalar(select(func.count(func.distinct(QuestionAttempt.question_id))).join(QuestionSet,
        QuestionAttempt.question_set_id == QuestionSet.id).where(QuestionSet.task_id == task.id,
        QuestionAttempt.owner_id == task_owner_id(db, task), QuestionAttempt.status == "succeeded")) or 0
    task.completed_count = completed
    task.status = "completed" if completed >= task.target_count else "in_progress" if completed else "pending"
    task.completed_at = utc_now() if task.status == "completed" else None


def _update_teachback_task(db: Session, session: TeachbackSession) -> None:
    if not session.task_id: return
    task = db.scalar(select(PlanTask).where(PlanTask.id == session.task_id).with_for_update())
    if not task: return
    plan = db.get(StudyPlan, task.plan_id)
    if not plan or plan.status != "active" or task.status == "skipped": return
    completed = db.scalar(select(func.count(TeachbackSession.id)).where(TeachbackSession.task_id == task.id,
        TeachbackSession.owner_id == session.owner_id, TeachbackSession.status == "completed")) or 0
    task.completed_count = completed
    task.status = "completed" if completed >= task.target_count else "in_progress" if completed else "pending"
    task.completed_at = utc_now() if task.status == "completed" else None


def task_owner_id(db: Session, task: PlanTask) -> UUID:
    plan = db.get(StudyPlan, task.plan_id)
    if not plan: raise HTTPException(404, "PLAN_NOT_FOUND")
    return plan.owner_id
