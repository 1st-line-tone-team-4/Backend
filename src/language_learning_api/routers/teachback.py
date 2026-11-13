"""Canonical teach-back API, sharing B auth, idempotency and the durable worker."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.a_gateway import lock_owned_task, task_completed_count
from app.core import ApiError, get_context, get_user, idempotent, iso, uid, utcnow, request_envelope
from app.jobs import accepted
from app.materials import evidence_dto, get_confirmed_version
from app.models import (
    Concept,
    ConceptChecklistItem,
    ConceptVersion,
    ConceptVersionEvidence,
    FeedbackReport,
    ImprovementAction,
    Job,
    Material,
    PlanTaskConcept,
    QuestionAttempt,
    QuestionConcept,
    ReportEvidence,
    ReportItem,
    SessionMessage,
    SessionReport,
    TeachbackSession,
)
from language_learning_api.routers.common import encode_opaque_cursor, decode_opaque_cursor
from language_learning_api.schemas import (
    CompleteSessionInput,
    FeedbackInput,
    FinalAnswerInput,
    MessageInput,
    TeachbackSessionInput,
    TeachbackSessionPatch,
)
from language_learning_api.services.teachback_handlers import owned_session

router = APIRouter(prefix="/v1", tags=["Teach-back"])


def session_dto(db, session):
    version = db.get(ConceptVersion, session.concept_version_id)
    return {
        "id": session.id,
        "concept_id": version.concept_id,
        "concept_version_id": version.id,
        "version_no": version.version_no,
        "task_id": session.task_id,
        "attempt_id": session.attempt_id,
        "previous_session_id": session.previous_session_id,
        "explanation_language": session.explanation_language,
        "student_persona": session.student_persona,
        "status": session.status,
        "question_count": session.question_count,
        "latest_job_id": session.latest_job_id,
        "started_at": iso(session.started_at),
        "completed_at": iso(session.completed_at),
    }


def message_dto(message):
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "input_mode": message.input_mode,
        "sequence_no": message.sequence_no,
        "created_at": iso(message.created_at),
    }


def request_data(request, body):
    return request_envelope(request, body.model_dump(mode="json"))


def mutate(request, body, operation, action):
    return idempotent(
        get_context(request),
        get_user(request),
        operation,
        request.headers.get("Idempotency-Key"),
        request_data(request, body),
        action,
    )


def latest_job(db, session):
    if not session.latest_job_id:
        return None
    try:
        # Worker holds job before session. Never wait on that inverse lock order.
        return db.scalar(
            select(Job)
            .where(Job.id == session.latest_job_id, Job.user_id == session.user_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except OperationalError as exc:
        if getattr(exc.orig, "args", [None])[0] == 3572:
            raise ApiError(409, "JOB_IN_PROGRESS", "A worker is updating the session.") from None
        raise


def no_busy_job(db, session):
    job = latest_job(db, session)
    if job and job.status in {"queued", "running"}:
        raise ApiError(409, "JOB_IN_PROGRESS", "Resolve the current job first.")
    return job


def last_message(db, session_id):
    return db.scalar(
        select(SessionMessage)
        .where(SessionMessage.session_id == session_id)
        .order_by(SessionMessage.sequence_no.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def lock_standalone_version(db, owner, version_id):
    """Serialize a new standalone reference with material deletion.

    Locate the parent without locks, then use current reads in the same order as
    deletion: material before concept/version. Historical confirmed pins remain
    valid; a repeatable-read snapshot must not resurrect a deleted source.
    """
    material_id = db.scalar(
        select(Concept.material_id)
        .join(ConceptVersion, ConceptVersion.concept_id == Concept.id)
        .where(ConceptVersion.id == version_id)
    )
    material = db.scalar(
        select(Material)
        .where(Material.id == material_id, Material.user_id == owner)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if material is None:
        raise ApiError(404, "NOT_FOUND", "Concept version not found.")
    version = db.scalar(
        select(ConceptVersion)
        .join(Concept, Concept.id == ConceptVersion.concept_id)
        .where(ConceptVersion.id == version_id, Concept.material_id == material.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if version is None:
        raise ApiError(404, "NOT_FOUND", "Concept version not found.")
    if not version.is_confirmed:
        raise ApiError(422, "CONCEPT_NOT_CONFIRMED", "Concept version is not confirmed.")
    return version


def lock_session_reference(db, model, owner, identity):
    """Acquire FK parents without waiting while holding source/task locks.

    A preliminary owner lookup preserves 404 isolation. The locking read then
    refreshes state. SQLAlchemy's MySQL dialect renders shared NOWAIT using the
    legacy LOCK IN SHARE MODE form, so use supported FOR UPDATE NOWAIT here.
    """
    predicate = (model.id == identity, model.user_id == owner)
    if db.scalar(select(model.id).where(*predicate)) is None:
        raise ApiError(404, "NOT_FOUND", "Related learning record not found.")
    try:
        row = db.scalar(
            select(model)
            .where(*predicate)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except OperationalError as exc:
        if getattr(exc.orig, "args", [None])[0] == 3572:
            raise ApiError(409, "RESOURCE_CHANGED", "Related learning record is being updated.") from None
        raise
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Related learning record not found.")
    return row


@router.post("/teachback-sessions", status_code=201)
def create_session(body: TeachbackSessionInput, request: Request):
    owner = get_user(request)

    def action(db):
        task_id = str(body.task_id) if body.task_id else None
        version = (
            get_confirmed_version(db, owner, str(body.concept_version_id))
            if task_id
            else lock_standalone_version(db, owner, str(body.concept_version_id))
        )
        if task_id:
            _, task = lock_owned_task(db, owner, task_id, task_type="teach_back")
            if not db.scalar(
                select(PlanTaskConcept.task_id).where(
                    PlanTaskConcept.task_id == task_id, PlanTaskConcept.concept_version_id == version.id
                )
            ):
                raise ApiError(422, "TASK_VERSION_MISMATCH", "Task does not pin this version.")
            if db.scalar(
                select(TeachbackSession.id)
                .where(
                    TeachbackSession.user_id == owner,
                    TeachbackSession.task_id == task_id,
                    TeachbackSession.status.in_(("active", "report_pending")),
                )
                .with_for_update()
                .limit(1)
            ):
                raise ApiError(409, "ACTIVE_SESSION_EXISTS", "Task already has an active session.")
            if task_completed_count(db, task, owner, authoritative=True) >= task.target_count:
                raise ApiError(409, "TASK_COMPLETED", "Task target is already complete.")
        attempt_id = str(body.attempt_id) if body.attempt_id else None
        if attempt_id:
            attempt = lock_session_reference(db, QuestionAttempt, owner, attempt_id)
            if attempt.status != "graded" or not db.scalar(
                select(QuestionConcept.question_id).where(
                    QuestionConcept.question_id == attempt.question_id,
                    QuestionConcept.concept_id == version.concept_id,
                )
            ):
                raise ApiError(
                    422, "ATTEMPT_NOT_GRADED_OR_CONCEPT_MISMATCH", "Attempt must be graded for this concept."
                )
        previous_id = str(body.previous_session_id) if body.previous_session_id else None
        if previous_id:
            previous = lock_session_reference(db, TeachbackSession, owner, previous_id)
            if db.get(ConceptVersion, previous.concept_version_id).concept_id != version.concept_id:
                raise ApiError(422, "CONCEPT_MISMATCH", "Previous session must concern the same concept.")
        session = TeachbackSession(
            id=uid(),
            user_id=owner,
            concept_version_id=version.id,
            task_id=task_id,
            attempt_id=attempt_id,
            previous_session_id=previous_id,
            explanation_language=body.explanation_language,
            student_persona=body.student_persona,
            status="active",
            question_count=0,
            started_at=utcnow(),
        )
        try:
            with db.begin_nested():
                db.add(session)
                db.flush()
        except IntegrityError as exc:
            if (
                getattr(exc.orig, "args", [None])[0] == 1452
                or getattr(exc.orig, "sqlite_errorcode", None) == 787
            ):
                raise ApiError(
                    409, "RESOURCE_CHANGED", "Related data changed while creating the session."
                ) from None
            raise
        return 201, {"data": session_dto(db, session)}

    return mutate(request, body, "createTeachbackSession", action)


@router.get("/teachback-sessions/{sessionId}")
def get_session_route(session_id: Annotated[UUID, Path(alias="sessionId")], request: Request):
    ctx, owner = get_context(request), get_user(request)
    with ctx.db.transaction() as db:
        return {"data": session_dto(db, owned_session(db, owner, session_id))}


@router.patch("/teachback-sessions/{sessionId}")
def abandon_session(
    session_id: Annotated[UUID, Path(alias="sessionId")], body: TeachbackSessionPatch, request: Request
):
    ctx, owner = get_context(request), get_user(request)
    with ctx.db.transaction() as db:
        # Abandonment is cleanup, including after the task/plan becomes inactive.
        # Keep parent -> session locking; latest_job uses NOWAIT against workers.
        session = owned_session(db, owner, session_id, lock=True, active_task=False)
        if session.status == "abandoned":
            return {"data": session_dto(db, session)}
        if session.status not in {"active", "report_pending"}:
            raise ApiError(409, "SESSION_NOT_ACTIVE", "Session is not active.")
        job = no_busy_job(db, session)
        if session.status == "report_pending":
            if (
                job is None
                or job.kind != "report"
                or job.resource_type != "session"
                or job.resource_id != session.id
                or job.status not in {"failed", "outcome_unknown", "succeeded"}
            ):
                raise ApiError(409, "SESSION_NOT_ACTIVE", "Session is not active.")
        if db.scalar(
            select(SessionReport.id).where(SessionReport.session_id == session.id).with_for_update()
        ):
            raise ApiError(409, "SESSION_NOT_ACTIVE", "Session is not active.")
        # Preserve job pointers, terminal diagnostics and all learning history.
        session.status = body.status
        db.flush()
        return {"data": session_dto(db, session)}


@router.get("/teachback-sessions/{sessionId}/messages")
def list_messages(
    session_id: Annotated[UUID, Path(alias="sessionId")],
    request: Request,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    ctx, owner = get_context(request), get_user(request)
    filters = {"owner": owner, "operation": "listTeachbackMessages", "session_id": str(session_id)}
    with ctx.db.transaction() as db:
        owned_session(db, owner, session_id)
        stmt = select(SessionMessage).where(SessionMessage.session_id == str(session_id))
        if cursor:
            payload = decode_opaque_cursor(cursor, filters, secret=ctx.settings.signing_secret)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"sequence_no"}
                or type(payload["sequence_no"]) is not int
                or payload["sequence_no"] < 1
            ):
                raise ApiError(422, "VALIDATION_ERROR", "Invalid message cursor.")
            stmt = stmt.where(SessionMessage.sequence_no > payload["sequence_no"])
        rows = list(db.scalars(stmt.order_by(SessionMessage.sequence_no).limit(limit + 1)))
        more, rows = len(rows) > limit, rows[:limit]
        next_cursor = (
            encode_opaque_cursor(
                {"sequence_no": rows[-1].sequence_no}, filters, secret=ctx.settings.signing_secret
            )
            if more and rows
            else None
        )
        return {"data": [message_dto(m) for m in rows], "next_cursor": next_cursor}


@router.post("/teachback-sessions/{sessionId}/messages", status_code=202)
def send_message(session_id: Annotated[UUID, Path(alias="sessionId")], body: MessageInput, request: Request):
    ctx, owner = get_context(request), get_user(request)

    def action(db):
        session = owned_session(db, owner, session_id, lock=True)
        if session.status != "active":
            raise ApiError(409, "SESSION_NOT_ACTIVE", "Session is not active.")
        if session.question_count >= 3:
            raise ApiError(
                422, "QUESTION_LIMIT_REACHED", "Use final-answer or complete after three questions."
            )
        no_busy_job(db, session)
        last = last_message(db, session.id)
        message = SessionMessage(
            id=uid(),
            session_id=session.id,
            role="user",
            content=body.content,
            input_mode=body.input_mode,
            sequence_no=last.sequence_no + 1 if last else 1,
            created_at=utcnow(),
        )
        db.add(message)
        db.flush()
        job = ctx.jobs.enqueue(
            db,
            user_id=owner,
            kind="message",
            resource_type="session",
            resource_id=session.id,
            payload={"session_id": session.id, "message_id": message.id},
            model_id=body.model_id,
        )
        message.job_id, session.latest_job_id = job.id, job.id
        db.flush()
        return 202, accepted(job)

    return mutate(request, body, "sendTeachbackMessage", action)


@router.post("/teachback-sessions/{sessionId}/complete", status_code=202)
def complete_session(
    session_id: Annotated[UUID, Path(alias="sessionId")], body: CompleteSessionInput, request: Request
):
    ctx, owner = get_context(request), get_user(request)

    def action(db):
        session = owned_session(db, owner, session_id, lock=True)
        if session.status == "report_pending":
            job = latest_job(db, session)
            if job and job.kind == "report":
                return 202, accepted(job)
        if session.status != "active":
            raise ApiError(409, "SESSION_NOT_ACTIVE", "Session is not active.")
        no_busy_job(db, session)
        if not db.scalar(
            select(SessionMessage.id)
            .where(SessionMessage.session_id == session.id, SessionMessage.role == "user")
            .limit(1)
        ):
            raise ApiError(422, "MESSAGE_REQUIRED", "Explain the concept before completing.")
        job = ctx.jobs.enqueue(
            db,
            user_id=owner,
            kind="report",
            resource_type="session",
            resource_id=session.id,
            payload={"session_id": session.id},
            model_id=body.model_id,
        )
        session.latest_job_id, session.status = job.id, "report_pending"
        db.flush()
        return 202, accepted(job)

    return mutate(request, body, "completeTeachbackSession", action)


def report_dto(db, report):
    version = db.get(ConceptVersion, report.concept_version_id)
    rows = db.scalars(
        select(ReportItem)
        .join(ConceptChecklistItem, ConceptChecklistItem.id == ReportItem.checklist_item_id)
        .where(ReportItem.report_id == report.id)
        .order_by(ConceptChecklistItem.ordinal)
    ).all()
    items = []
    for item in rows:
        pins = db.execute(
            select(ReportEvidence, ConceptVersionEvidence)
            .join(
                ConceptVersionEvidence,
                (ConceptVersionEvidence.chunk_id == ReportEvidence.chunk_id)
                & (ConceptVersionEvidence.concept_version_id == version.id),
            )
            .where(ReportEvidence.report_item_id == item.id)
            .order_by(ReportEvidence.chunk_id)
        ).all()
        items.append(
            {
                "id": item.id,
                "concept_id": version.concept_id,
                "concept_version_id": version.id,
                "checklist_item_id": item.checklist_item_id,
                "status": item.status,
                "description": item.description,
                "confidence": item.confidence,
                "evidence": [evidence_dto(db, pin.chunk_id, pin.quote) for _, pin in pins],
            }
        )
    action_id = db.scalar(select(ImprovementAction.id).where(ImprovementAction.report_id == report.id))
    return {
        "id": report.id,
        "session_id": report.session_id,
        "concept_version_id": version.id,
        "version_no": version.version_no,
        "coverage": float(report.coverage) if report.coverage is not None else None,
        "coverage_formula": report.coverage_formula,
        "assessable_count": report.assessable_count,
        "weighted_score": float(report.weighted_score),
        "items": items,
        "next_action": report.next_action,
        "action_id": action_id,
    }


@router.get("/teachback-sessions/{sessionId}/report")
def get_report(session_id: Annotated[UUID, Path(alias="sessionId")], request: Request):
    ctx, owner = get_context(request), get_user(request)
    with ctx.db.transaction() as db:
        owned_session(db, owner, session_id)
        report = db.scalar(select(SessionReport).where(SessionReport.session_id == str(session_id)))
        if report is None:
            raise ApiError(409, "REPORT_NOT_READY", "Report is not ready.")
        return {"data": report_dto(db, report)}


@router.post("/teachback-sessions/{sessionId}/feedback", status_code=201)
def report_feedback(
    session_id: Annotated[UUID, Path(alias="sessionId")], body: FeedbackInput, request: Request
):
    owner = get_user(request)

    def action(db):
        session = owned_session(db, owner, session_id)
        report = db.scalar(select(SessionReport).where(SessionReport.session_id == session.id))
        if report is None:
            raise ApiError(409, "REPORT_NOT_READY", "Report is not ready.")
        item_id = str(body.report_item_id) if body.report_item_id else None
        if item_id and not db.scalar(
            select(ReportItem.id).where(ReportItem.id == item_id, ReportItem.report_id == report.id)
        ):
            raise ApiError(422, "REPORT_ITEM_MISMATCH", "Item must belong to this report.")
        feedback = FeedbackReport(
            id=uid(),
            user_id=owner,
            session_id=session.id,
            report_id=report.id,
            report_item_id=item_id,
            reason=body.reason,
            comment=body.comment,
            status="received",
            created_at=utcnow(),
        )
        db.add(feedback)
        db.flush()
        return 201, {
            "data": {
                "id": feedback.id,
                "session_id": session.id,
                "report_item_id": item_id,
                "reason": feedback.reason,
                "comment": feedback.comment,
                "status": feedback.status,
                "created_at": iso(feedback.created_at),
            }
        }

    return mutate(request, body, "reportIncorrectFeedback", action)


@router.post("/teachback-sessions/{sessionId}/final-answer", status_code=201)
def final_answer(
    session_id: Annotated[UUID, Path(alias="sessionId")], body: FinalAnswerInput, request: Request
):
    owner = get_user(request)

    def action(db):
        session = owned_session(db, owner, session_id, lock=True)
        if session.status != "active" or session.question_count != 3:
            raise ApiError(
                409, "FINAL_ANSWER_NOT_ALLOWED", "Final answer requires three completed questions."
            )
        no_busy_job(db, session)
        last = last_message(db, session.id)
        if not last or last.role != "assistant":
            raise ApiError(409, "FINAL_ANSWER_NOT_ALLOWED", "Final answer was already submitted.")
        message = SessionMessage(
            id=uid(),
            session_id=session.id,
            role="user",
            content=body.content,
            input_mode=body.input_mode,
            sequence_no=last.sequence_no + 1,
            created_at=utcnow(),
        )
        db.add(message)
        db.flush()
        return 201, {"data": message_dto(message)}

    return mutate(request, body, "submitFinalTeachbackAnswer", action)
