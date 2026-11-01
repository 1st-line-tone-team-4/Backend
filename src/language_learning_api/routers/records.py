from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query
from sqlalchemy import select

from language_learning_api.auth import CurrentUser, DatabaseSession
from language_learning_api.models import (Concept, ConceptVersion, ImprovementAction, Material, Question, QuestionAttempt,
    PublicAttempt, QuestionSet, SessionReport, TeachbackSession, Unit)
from language_learning_api.routers.common import decode_cursor, encode_cursor
from language_learning_api.schemas import (ImprovementActionPage, ImprovementActionPatch, PublicAttemptPage,
    SessionSummaryPage, WeaknessPage)

router = APIRouter(prefix="/v1/records", tags=["학습 기록과 취약점"])


@router.get("/public-attempts", response_model=PublicAttemptPage)
def list_public_attempts(user: CurrentUser, db: DatabaseSession, cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20):
    stmt = select(PublicAttempt).where(PublicAttempt.owner_id == user.id)
    if cursor:
        created, item_id = decode_cursor(cursor)
        stmt = stmt.where((PublicAttempt.answered_at < created) | ((PublicAttempt.answered_at == created) & (PublicAttempt.id < item_id)))
    rows = list(db.scalars(stmt.order_by(PublicAttempt.answered_at.desc(), PublicAttempt.id.desc()).limit(limit + 1)))
    more = len(rows) > limit; rows = rows[:limit]
    return {"data": [{"id": item.id, "publication_id": item.publication_id, "answer": item.answer,
        "is_correct": item.is_correct, "correct_answer": item.correct_answer, "explanation": item.explanation,
        "answered_at": item.answered_at, "question_snapshot": item.question_snapshot} for item in rows],
        "next_cursor": encode_cursor(rows[-1].answered_at, rows[-1].id) if more and rows else None}


@router.get("/sessions", response_model=SessionSummaryPage)
def list_sessions(user: CurrentUser, db: DatabaseSession, cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20):
    stmt = select(TeachbackSession).where(TeachbackSession.owner_id == user.id)
    if cursor:
        created, item_id = decode_cursor(cursor)
        stmt = stmt.where((TeachbackSession.started_at < created) | ((TeachbackSession.started_at == created) & (TeachbackSession.id < item_id)))
    rows = list(db.scalars(stmt.order_by(TeachbackSession.started_at.desc(), TeachbackSession.id.desc()).limit(limit + 1)))
    more = len(rows) > limit; rows = rows[:limit]
    return {"data": [{"id": s.id, "concept_id": s.concept_id, "concept_version_id": s.concept_version_id,
        "version_no": s.version_no, "status": s.status, "explanation_language": s.explanation_language,
        "started_at": s.started_at} for s in rows],
        "next_cursor": encode_cursor(rows[-1].started_at, rows[-1].id) if more and rows else None}


@router.get("/weaknesses", response_model=WeaknessPage)
def list_weaknesses(user: CurrentUser, db: DatabaseSession, cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20):
    concepts = list(db.execute(select(Concept, ConceptVersion, Unit).join(ConceptVersion,
        Concept.current_version_id == ConceptVersion.id).join(Unit, Concept.unit_id == Unit.id)
        .join(Material, Unit.material_id == Material.id).where(Material.owner_id == user.id,
        ConceptVersion.status == "confirmed").order_by(Concept.created_at.desc(), Concept.id.desc())))
    # Cursors for this endpoint use concept creation time, retaining deterministic ordering.
    if cursor:
        created, item_id = decode_cursor(cursor)
        concepts = [row for row in concepts if row[0].created_at < created or (row[0].created_at == created and row[0].id < item_id)]
    page = concepts[:limit + 1]; more = len(page) > limit; page = page[:limit]
    output = []
    for concept, version, _unit in page:
        reports = list(db.scalars(select(SessionReport).join(TeachbackSession, SessionReport.session_id == TeachbackSession.id)
            .where(TeachbackSession.owner_id == user.id, SessionReport.concept_version_id == version.id,
                SessionReport.coverage.is_not(None)).order_by(SessionReport.created_at.desc(), SessionReport.id.desc()).limit(2)))
        coverage = reports[0].coverage if reports else None
        if len(reports) < 2: trend = "unknown"
        else:
            delta = (reports[0].coverage or 0) - (reports[1].coverage or 0)
            trend = "improving" if delta > 0.0001 else "declining" if delta < -0.0001 else "stable"
        sessions = list(db.scalars(select(TeachbackSession.id).where(TeachbackSession.owner_id == user.id,
            TeachbackSession.concept_version_id == version.id, TeachbackSession.status == "completed")))
        question_rows = list(db.execute(select(QuestionAttempt, Question).join(Question, QuestionAttempt.question_id == Question.id)
            .where(QuestionAttempt.owner_id == user.id, QuestionAttempt.status == "succeeded",
                Question.concept_version_ids.contains([str(version.id)]))))
        latest_by_question = {}
        for attempt, question in question_rows:
            if question.id not in latest_by_question or attempt.answered_at > latest_by_question[question.id].answered_at:
                latest_by_question[question.id] = attempt
        incorrect = sum(1 for attempt in latest_by_question.values() if attempt.result and not attempt.result.get("is_correct", True))
        output.append({"concept_id": concept.id, "concept_name": version.name, "concept_version_id": version.id,
            "latest_coverage": coverage, "incorrect_attempts": incorrect, "teachback_count": len(sessions), "trend": trend})
    next_cursor = encode_cursor(page[-1][0].created_at, page[-1][0].id) if more and page else None
    return {"data": output, "next_cursor": next_cursor}


def action_dto(action: ImprovementAction) -> dict:
    return {"id": action.id, "report_id": action.report_id, "concept_id": action.concept_id,
        "text": action.text, "status": action.status, "followup_session_id": action.followup_session_id,
        "updated_at": action.updated_at}


@router.get("/actions", response_model=ImprovementActionPage)
def list_actions(user: CurrentUser, db: DatabaseSession, cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20):
    stmt = select(ImprovementAction).where(ImprovementAction.owner_id == user.id)
    if cursor:
        created, item_id = decode_cursor(cursor)
        stmt = stmt.where((ImprovementAction.updated_at < created) | ((ImprovementAction.updated_at == created) & (ImprovementAction.id < item_id)))
    rows = list(db.scalars(stmt.order_by(ImprovementAction.updated_at.desc(), ImprovementAction.id.desc()).limit(limit + 1)))
    more = len(rows) > limit; rows = rows[:limit]
    return {"data": [action_dto(a) for a in rows], "next_cursor": encode_cursor(rows[-1].updated_at, rows[-1].id) if more and rows else None}


@router.patch("/actions/{actionId}")
def update_action(action_id: Annotated[UUID, Path(alias="actionId")], body: ImprovementActionPatch,
    user: CurrentUser, db: DatabaseSession):
    action = db.scalar(select(ImprovementAction).where(ImprovementAction.id == action_id, ImprovementAction.owner_id == user.id))
    if action is None: raise HTTPException(404, "NOT_FOUND")
    if body.followup_session_id:
        followup = db.scalar(select(TeachbackSession).where(TeachbackSession.id == body.followup_session_id,
            TeachbackSession.owner_id == user.id, TeachbackSession.concept_id == action.concept_id))
        if followup is None: raise HTTPException(422, "FOLLOWUP_SESSION_MISMATCH")
        action.followup_session_id = followup.id
    if body.status == "completed" and (not action.followup_session_id or
        db.scalar(select(TeachbackSession.id).where(TeachbackSession.id == action.followup_session_id,
            TeachbackSession.status == "completed")) is None):
        raise HTTPException(422, "FOLLOWUP_SESSION_REQUIRED")
    action.status = body.status; db.commit(); db.refresh(action)
    return {"data": action_dto(action)}
