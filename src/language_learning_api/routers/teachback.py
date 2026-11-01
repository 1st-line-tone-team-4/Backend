from datetime import datetime, timezone, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Path, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from language_learning_api.auth import CurrentUser, DatabaseSession
from language_learning_api.models import (Concept, ConceptVersion, FeedbackReport, IdempotencyRecord, ImprovementAction,
    Job, Material, PlanTask, Question, QuestionAttempt, StudyPlan, TeachbackMessage, TeachbackSession, Unit, SessionReport, utc_now)
from language_learning_api.routers.common import decode_cursor, encode_cursor, request_hash
from language_learning_api.schemas import (AcceptedEnvelope, CompleteSessionInput, FeedbackEnvelope, FeedbackInput,
    FinalAnswerInput, MessageInput, MessagePage, SessionReportEnvelope, TeachbackEnvelope,
    TeachbackSessionInput, TeachbackSessionPatch)

router = APIRouter(tags=["Teach-back"])


def session_dto(session: TeachbackSession) -> dict:
    return {"id": session.id, "concept_id": session.concept_id, "concept_version_id": session.concept_version_id,
        "version_no": session.version_no, "task_id": session.task_id, "attempt_id": session.attempt_id,
        "previous_session_id": session.previous_session_id, "explanation_language": session.explanation_language,
        "student_persona": session.student_persona, "status": session.status, "question_count": session.question_count,
        "latest_job_id": session.latest_job_id, "started_at": session.started_at, "completed_at": session.completed_at}


def message_dto(message: TeachbackMessage) -> dict:
    return {"id": message.id, "role": message.role, "content": message.content, "input_mode": message.input_mode,
        "sequence_no": message.sequence_no, "created_at": message.created_at}


def job_dto(job: Job) -> dict:
    return {"id": job.id, "kind": job.kind, "status": job.status, "resource_type": job.resource_type,
        "resource_id": job.resource_id, "attempt_no": job.attempt_no, "retryable": job.retryable,
        "error_code": job.error_code, "result_url": job.result_url, "created_at": job.created_at,
        "updated_at": job.updated_at, "previous_job_id": job.previous_job_id}


def get_session(db, owner_id: UUID, session_id: UUID) -> TeachbackSession:
    session = db.scalar(select(TeachbackSession).where(TeachbackSession.id == session_id, TeachbackSession.owner_id == owner_id))
    if session is None:
        raise HTTPException(404, "NOT_FOUND")
    return session


def idem(db, owner_id: UUID, op: str, key: str, digest: str):
    record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.owner_id == owner_id,
        IdempotencyRecord.operation == op, IdempotencyRecord.key == key))
    if record:
        created = record.created_at.replace(tzinfo=timezone.utc) if record.created_at.tzinfo is None else record.created_at
        if datetime.now(timezone.utc) - created >= timedelta(hours=24):
            db.delete(record); db.flush(); return None
        if record.request_hash != digest:
            raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
        return record.response_body
    return None


def save_idem(db, owner_id: UUID, op: str, key: str, digest: str, response: dict, status: int = 200):
    db.add(IdempotencyRecord(owner_id=owner_id, operation=op, key=key, request_hash=digest,
        response_status=status, response_body=jsonable_encoder(response)))


@router.post("/v1/teachback-sessions", response_model=TeachbackEnvelope, status_code=201)
def create_session(body: TeachbackSessionInput, user: CurrentUser, db: DatabaseSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]):
    op = "POST:/v1/teachback-sessions"; digest = request_hash(body.model_dump(mode="json"))
    cached = idem(db, user.id, op, idempotency_key, digest)
    if cached: return cached
    version = db.get(ConceptVersion, body.concept_version_id)
    concept = db.get(Concept, version.concept_id) if version else None
    unit = db.get(Unit, concept.unit_id) if concept else None
    material = db.get(Material, unit.material_id) if unit else None
    if not version or not concept or not material or material.owner_id != user.id or version.status != "confirmed" or concept.current_version_id != version.id:
        raise HTTPException(404, "NOT_FOUND")
    if body.task_id:
        task = db.scalar(select(PlanTask).join(StudyPlan).where(PlanTask.id == body.task_id, StudyPlan.owner_id == user.id))
        if not task: raise HTTPException(404, "NOT_FOUND")
        if task.task_type != "teach_back" or str(version.id) not in task.concept_version_ids:
            raise HTTPException(422, "TASK_VERSION_MISMATCH")
        active = db.scalar(select(TeachbackSession.id).where(TeachbackSession.owner_id == user.id,
            TeachbackSession.task_id == task.id, TeachbackSession.status.in_(["active", "report_pending"])).limit(1))
        if active: raise HTTPException(409, "ACTIVE_SESSION_EXISTS")
    if body.attempt_id:
        attempt = db.scalar(select(QuestionAttempt).where(QuestionAttempt.id == body.attempt_id, QuestionAttempt.owner_id == user.id))
        if not attempt: raise HTTPException(404, "NOT_FOUND")
        question = db.get(Question, attempt.question_id)
        if attempt.status != "succeeded" or not question or str(concept.id) not in question.concept_ids:
            raise HTTPException(422, "ATTEMPT_NOT_GRADED_OR_CONCEPT_MISMATCH")
    if body.previous_session_id:
        previous = get_session(db, user.id, body.previous_session_id)
        if previous.concept_id != concept.id: raise HTTPException(422, "CONCEPT_MISMATCH")
    session = TeachbackSession(owner_id=user.id, concept_id=concept.id, concept_version_id=version.id,
        version_no=version.version_no, task_id=body.task_id, attempt_id=body.attempt_id,
        previous_session_id=body.previous_session_id, explanation_language=body.explanation_language,
        student_persona=body.student_persona, status="active", question_count=0)
    db.add(session); db.flush()
    response = {"data": session_dto(session)}
    save_idem(db, user.id, op, idempotency_key, digest, response, 201)
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409, "ACTIVE_SESSION_EXISTS")
    return response


@router.get("/v1/teachback-sessions/{sessionId}", response_model=TeachbackEnvelope)
def get_session_route(session_id: Annotated[UUID, Path(alias="sessionId")], user: CurrentUser, db: DatabaseSession):
    return {"data": session_dto(get_session(db, user.id, session_id))}


@router.patch("/v1/teachback-sessions/{sessionId}", response_model=TeachbackEnvelope)
def abandon_session(session_id: Annotated[UUID, Path(alias="sessionId")], body: TeachbackSessionPatch,
    user: CurrentUser, db: DatabaseSession):
    session = get_session(db, user.id, session_id)
    if session.status != "active": raise HTTPException(409, "SESSION_NOT_ACTIVE")
    if session.latest_job_id:
        job = db.get(Job, session.latest_job_id)
        if job and job.status in ("queued", "running", "outcome_unknown"): raise HTTPException(409, "JOB_IN_PROGRESS")
    session.status = body.status; db.commit(); db.refresh(session)
    return {"data": session_dto(session)}


@router.get("/v1/teachback-sessions/{sessionId}/messages", response_model=MessagePage)
def list_messages(session_id: Annotated[UUID, Path(alias="sessionId")], user: CurrentUser, db: DatabaseSession,
    cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    get_session(db, user.id, session_id)
    stmt = select(TeachbackMessage).where(TeachbackMessage.session_id == session_id)
    if cursor:
        created, item_id = decode_cursor(cursor)
        stmt = stmt.where((TeachbackMessage.created_at > created) | ((TeachbackMessage.created_at == created) & (TeachbackMessage.id > item_id)))
    rows = list(db.scalars(stmt.order_by(TeachbackMessage.created_at.asc(), TeachbackMessage.id.asc()).limit(limit + 1)))
    more = len(rows) > limit; rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if more and rows else None
    return {"data": [message_dto(m) for m in rows], "next_cursor": next_cursor}


@router.post("/v1/teachback-sessions/{sessionId}/messages", response_model=AcceptedEnvelope, status_code=202)
def send_message(session_id: Annotated[UUID, Path(alias="sessionId")], body: MessageInput, user: CurrentUser,
    db: DatabaseSession, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    ai_key: Annotated[str, Header(alias="X-AI-Key", min_length=1, max_length=512)]):
    session = get_session(db, user.id, session_id)
    op = f"POST:/v1/teachback-sessions/{session_id}/messages"; digest = request_hash(body.model_dump(mode="json"))
    cached = idem(db, user.id, op, idempotency_key, digest)
    if cached: return cached
    if session.status != "active": raise HTTPException(409, "SESSION_NOT_ACTIVE")
    if session.question_count >= 3: raise HTTPException(422, "QUESTION_LIMIT_REACHED")
    if session.latest_job_id:
        latest = db.get(Job, session.latest_job_id)
        if latest and latest.status in ("queued", "running", "outcome_unknown"): raise HTTPException(409, "JOB_IN_PROGRESS")
    job = Job(owner_id=user.id, kind="message", status="queued", resource_type="session", resource_id=session.id, model_id=body.model_id)
    db.add(job); db.flush()
    seq = (db.scalar(select(func.max(TeachbackMessage.sequence_no)).where(TeachbackMessage.session_id == session.id)) or 0) + 1
    msg = TeachbackMessage(session_id=session.id, role="user", content=body.content, input_mode=body.input_mode, sequence_no=seq)
    db.add(msg); session.latest_job_id = job.id; db.flush()
    response = {"data": {"job": job_dto(job)}}
    save_idem(db, user.id, op, idempotency_key, digest, response, 202)
    db.commit(); return response


@router.post("/v1/teachback-sessions/{sessionId}/complete", response_model=AcceptedEnvelope, status_code=202)
def complete_session(session_id: Annotated[UUID, Path(alias="sessionId")], body: CompleteSessionInput,
    user: CurrentUser, db: DatabaseSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    ai_key: Annotated[str, Header(alias="X-AI-Key", min_length=1, max_length=512)]):
    session = get_session(db, user.id, session_id)
    op = f"POST:/v1/teachback-sessions/{session_id}/complete"; digest = request_hash(body.model_dump(mode="json"))
    cached = idem(db, user.id, op, idempotency_key, digest)
    if cached: return cached
    if session.status == "report_pending" and session.latest_job_id:
        job = db.get(Job, session.latest_job_id)
        if job and job.kind == "report": return {"data": {"job": job_dto(job)}}
    if session.status != "active": raise HTTPException(409, "SESSION_NOT_ACTIVE")
    user_messages = db.scalar(select(func.count(TeachbackMessage.id)).where(TeachbackMessage.session_id == session.id, TeachbackMessage.role == "user")) or 0
    if user_messages < 1: raise HTTPException(422, "MESSAGE_REQUIRED")
    if session.latest_job_id:
        latest = db.get(Job, session.latest_job_id)
        if latest and latest.status in ("queued", "running", "outcome_unknown"): raise HTTPException(409, "JOB_IN_PROGRESS")
    job = Job(owner_id=user.id, kind="report", status="queued", resource_type="session", resource_id=session.id, model_id=body.model_id)
    db.add(job); db.flush(); session.latest_job_id = job.id; session.status = "report_pending"
    response = {"data": {"job": job_dto(job)}}
    save_idem(db, user.id, op, idempotency_key, digest, response, 202)
    db.commit(); return response


@router.get("/v1/teachback-sessions/{sessionId}/report", response_model=SessionReportEnvelope)
def get_report(session_id: Annotated[UUID, Path(alias="sessionId")], user: CurrentUser, db: DatabaseSession):
    get_session(db, user.id, session_id)
    report = db.scalar(select(SessionReport).where(SessionReport.session_id == session_id))
    if report is None: raise HTTPException(409, "REPORT_NOT_READY")
    return {"data": {"id": report.id, "session_id": report.session_id, "concept_version_id": report.concept_version_id,
        "version_no": report.version_no, "coverage": report.coverage, "coverage_formula": report.coverage_formula,
        "assessable_count": report.assessable_count, "weighted_score": report.weighted_score,
        "items": report.items, "next_action": report.next_action, "action_id": report.action_id}}


@router.post("/v1/teachback-sessions/{sessionId}/feedback", response_model=FeedbackEnvelope, status_code=201)
def report_feedback(session_id: Annotated[UUID, Path(alias="sessionId")], body: FeedbackInput, user: CurrentUser,
    db: DatabaseSession, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]):
    get_session(db, user.id, session_id)
    op = f"POST:/v1/teachback-sessions/{session_id}/feedback"; digest = request_hash(body.model_dump(mode="json"))
    cached = idem(db, user.id, op, idempotency_key, digest)
    if cached: return cached
    report = db.scalar(select(SessionReport).where(SessionReport.session_id == session_id))
    if report is None: raise HTTPException(409, "REPORT_NOT_READY")
    if body.report_item_id and not any(str(item.get("id")) == str(body.report_item_id) for item in report.items):
        raise HTTPException(422, "REPORT_ITEM_MISMATCH")
    feedback = FeedbackReport(owner_id=user.id, session_id=session_id, report_item_id=body.report_item_id,
        reason=body.reason, comment=body.comment, status="received")
    db.add(feedback); db.flush()
    response = {"data": {"id": feedback.id, "session_id": feedback.session_id, "report_item_id": feedback.report_item_id,
        "reason": feedback.reason, "comment": feedback.comment, "status": feedback.status, "created_at": feedback.created_at}}
    save_idem(db, user.id, op, idempotency_key, digest, response, 201)
    db.commit()
    return response


@router.post("/v1/teachback-sessions/{sessionId}/final-answer", status_code=201)
def final_answer(session_id: Annotated[UUID, Path(alias="sessionId")], body: FinalAnswerInput, user: CurrentUser,
    db: DatabaseSession, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]):
    session = get_session(db, user.id, session_id)
    op = f"POST:/v1/teachback-sessions/{session_id}/final-answer"; digest = request_hash(body.model_dump(mode="json"))
    cached = idem(db, user.id, op, idempotency_key, digest)
    if cached: return cached
    if session.status != "active" or session.question_count != 3: raise HTTPException(409, "FINAL_ANSWER_NOT_ALLOWED")
    if session.latest_job_id:
        job = db.get(Job, session.latest_job_id)
        if job and job.status in ("queued", "running", "outcome_unknown"): raise HTTPException(409, "JOB_IN_PROGRESS")
    last = db.scalar(select(TeachbackMessage).where(TeachbackMessage.session_id == session.id).order_by(TeachbackMessage.sequence_no.desc()).limit(1))
    if not last or last.role != "assistant": raise HTTPException(409, "FINAL_ANSWER_NOT_ALLOWED")
    msg = TeachbackMessage(session_id=session.id, role="user", content=body.content, input_mode=body.input_mode, sequence_no=last.sequence_no + 1)
    db.add(msg); db.flush()
    response = {"data": message_dto(msg)}
    save_idem(db, user.id, op, idempotency_key, digest, response, 201)
    db.commit(); return response
