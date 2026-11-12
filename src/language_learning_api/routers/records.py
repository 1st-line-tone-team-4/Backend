"""Owner-scoped canonical records. Public snapshots never expose private questions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.core import ApiError, get_context, get_user, iso, utcnow
from app.models import (
    Concept,
    ConceptVersion,
    ImprovementAction,
    Material,
    PublicAttempt,
    QuestionAttempt,
    QuestionConcept,
    SessionReport,
    TeachbackSession,
)
from language_learning_api.routers.common import decode_cursor, encode_cursor
from language_learning_api.schemas import ImprovementActionPatch

router = APIRouter(prefix="/v1/records", tags=["Learning records"])
Cursor = Annotated[str | None, Query(max_length=1024)]
Limit = Annotated[int, Query(ge=1, le=100)]


def page_query(stmt, column, identity, cursor, filters, secret, limit):
    if cursor:
        created, item_id = decode_cursor(cursor, filters, secret=secret)
        stmt = stmt.where((column < created) | ((column == created) & (identity < item_id)))
    return stmt.order_by(column.desc(), identity.desc()).limit(limit + 1)


def next_cursor(rows, more, column, filters, secret):
    return (
        encode_cursor(getattr(rows[-1], column), rows[-1].id, filters, secret=secret)
        if more and rows
        else None
    )


def public_answer(answer):
    field = "selected_option_ids" if answer["type"] == "multiple_choice" else "text"
    return {"type": answer["type"], field: answer[field]}


def public_snapshot(snapshot):
    # Allow-list even stored snapshots: older imports may contain private metadata.
    return {
        "question_type": snapshot["question_type"],
        "prompt": snapshot["prompt"],
        "difficulty": snapshot["difficulty"],
        "options": [{"id": option["id"], "text": option["text"]} for option in snapshot["options"]],
    }


@router.get("/public-attempts")
def list_public_attempts(request: Request, cursor: Cursor = None, limit: Limit = 20):
    ctx, owner = get_context(request), get_user(request)
    filters = {"owner": owner, "operation": "listPublicAttempts"}
    with ctx.db.transaction() as db:
        stmt = select(PublicAttempt).where(PublicAttempt.user_id == owner)
        rows = list(
            db.scalars(
                page_query(
                    stmt,
                    PublicAttempt.answered_at,
                    PublicAttempt.id,
                    cursor,
                    filters,
                    ctx.settings.signing_secret,
                    limit,
                )
            )
        )
        more, rows = len(rows) > limit, rows[:limit]
        # The stored snapshot remains available even if its publication is withdrawn.
        return {
            "data": [
                {
                    "id": item.id,
                    "publication_id": item.publication_id,
                    "answer": public_answer(item.answer),
                    "is_correct": item.is_correct,
                    "correct_answer": public_answer(item.correct_answer),
                    "explanation": item.explanation,
                    "answered_at": iso(item.answered_at),
                    "question_snapshot": public_snapshot(item.question_snapshot),
                }
                for item in rows
            ],
            "next_cursor": next_cursor(rows, more, "answered_at", filters, ctx.settings.signing_secret),
        }


@router.get("/sessions")
def list_sessions(request: Request, cursor: Cursor = None, limit: Limit = 20):
    ctx, owner = get_context(request), get_user(request)
    filters = {"owner": owner, "operation": "listSessionRecords"}
    with ctx.db.transaction() as db:
        stmt = select(TeachbackSession).where(TeachbackSession.user_id == owner)
        rows = list(
            db.scalars(
                page_query(
                    stmt,
                    TeachbackSession.started_at,
                    TeachbackSession.id,
                    cursor,
                    filters,
                    ctx.settings.signing_secret,
                    limit,
                )
            )
        )
        more, rows = len(rows) > limit, rows[:limit]
        output = []
        for session in rows:
            version = db.get(ConceptVersion, session.concept_version_id)
            output.append(
                {
                    "id": session.id,
                    "concept_id": version.concept_id,
                    "concept_version_id": version.id,
                    "version_no": version.version_no,
                    "status": session.status,
                    "explanation_language": session.explanation_language,
                    "started_at": iso(session.started_at),
                }
            )
        return {
            "data": output,
            "next_cursor": next_cursor(rows, more, "started_at", filters, ctx.settings.signing_secret),
        }


@router.get("/weaknesses")
def list_weaknesses(request: Request, cursor: Cursor = None, limit: Limit = 20):
    ctx, owner = get_context(request), get_user(request)
    filters = {"owner": owner, "operation": "listWeaknesses"}
    with ctx.db.transaction() as db:
        newer = aliased(ConceptVersion)
        stmt = select(Concept, ConceptVersion).join(ConceptVersion, ConceptVersion.concept_id == Concept.id)
        stmt = stmt.join(Material, Material.id == Concept.material_id).where(
            Material.user_id == owner,
            ConceptVersion.is_confirmed.is_(True),
            ~select(newer.id)
            .where(newer.concept_id == Concept.id, newer.version_no > ConceptVersion.version_no)
            .exists(),
        )
        rows = list(
            db.execute(
                page_query(
                    stmt, Concept.created_at, Concept.id, cursor, filters, ctx.settings.signing_secret, limit
                )
            )
        )
        more, rows = len(rows) > limit, rows[:limit]
        output = []
        for concept, version in rows:
            reports = db.scalars(
                select(SessionReport)
                .join(TeachbackSession, TeachbackSession.id == SessionReport.session_id)
                .where(
                    TeachbackSession.user_id == owner,
                    TeachbackSession.status == "completed",
                    SessionReport.concept_version_id == version.id,
                )
                .order_by(SessionReport.created_at.desc(), SessionReport.id.desc())
                .limit(2)
            ).all()
            coverage = float(reports[0].coverage) if reports and reports[0].coverage is not None else None
            trend = "unknown"
            if (
                len(reports) == 2
                and all(r.coverage is not None for r in reports)
                and reports[0].coverage_formula == reports[1].coverage_formula
            ):
                delta = float(reports[0].coverage - reports[1].coverage)
                trend = "improving" if delta > 0.0001 else "declining" if delta < -0.0001 else "stable"
            count = (
                db.scalar(
                    select(func.count(TeachbackSession.id)).where(
                        TeachbackSession.user_id == owner,
                        TeachbackSession.concept_version_id == version.id,
                        TeachbackSession.status == "completed",
                    )
                )
                or 0
            )
            later = aliased(QuestionAttempt)
            attempts = (
                select(func.count(QuestionAttempt.id))
                .join(QuestionConcept, QuestionConcept.question_id == QuestionAttempt.question_id)
                .where(
                    QuestionAttempt.user_id == owner,
                    QuestionAttempt.status == "graded",
                    QuestionAttempt.is_correct.is_(False),
                    QuestionConcept.concept_version_id == version.id,
                    ~select(later.id)
                    .where(
                        later.user_id == owner,
                        later.status == "graded",
                        later.question_id == QuestionAttempt.question_id,
                        (later.answered_at > QuestionAttempt.answered_at)
                        | (
                            (later.answered_at == QuestionAttempt.answered_at)
                            & (later.id > QuestionAttempt.id)
                        ),
                    )
                    .exists(),
                )
            )
            output.append(
                {
                    "concept_id": concept.id,
                    "concept_name": version.name,
                    "concept_version_id": version.id,
                    "latest_coverage": coverage,
                    "incorrect_attempts": db.scalar(attempts) or 0,
                    "teachback_count": count,
                    "trend": trend,
                }
            )
        return {
            "data": output,
            "next_cursor": encode_cursor(
                rows[-1][0].created_at, rows[-1][0].id, filters, secret=ctx.settings.signing_secret
            )
            if more and rows
            else None,
        }


def action_dto(action, report):
    return {
        "id": action.id,
        "report_id": action.report_id,
        "concept_id": action.concept_id,
        "text": report.next_action,
        "status": action.status,
        "followup_session_id": action.followup_session_id,
        "updated_at": iso(action.updated_at),
    }


def owned_actions(owner):
    return (
        select(ImprovementAction, SessionReport)
        .join(SessionReport, SessionReport.id == ImprovementAction.report_id)
        .join(TeachbackSession, TeachbackSession.id == SessionReport.session_id)
        .where(TeachbackSession.user_id == owner)
    )


@router.get("/actions")
def list_actions(request: Request, cursor: Cursor = None, limit: Limit = 20):
    ctx, owner = get_context(request), get_user(request)
    filters = {"owner": owner, "operation": "listImprovementActions"}
    with ctx.db.transaction() as db:
        rows = list(
            db.execute(
                page_query(
                    owned_actions(owner),
                    ImprovementAction.updated_at,
                    ImprovementAction.id,
                    cursor,
                    filters,
                    ctx.settings.signing_secret,
                    limit,
                )
            )
        )
        more, rows = len(rows) > limit, rows[:limit]
        return {
            "data": [action_dto(action, report) for action, report in rows],
            "next_cursor": encode_cursor(
                rows[-1][0].updated_at, rows[-1][0].id, filters, secret=ctx.settings.signing_secret
            )
            if more and rows
            else None,
        }


@router.patch("/actions/{actionId}")
def update_action(
    action_id: Annotated[UUID, Path(alias="actionId")], body: ImprovementActionPatch, request: Request
):
    ctx, owner = get_context(request), get_user(request)
    with ctx.db.transaction() as db:
        row = db.execute(
            owned_actions(owner).where(ImprovementAction.id == str(action_id)).with_for_update()
        ).first()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "Action not found.")
        action, report = row
        followup_id = str(body.followup_session_id) if body.followup_session_id else None
        if followup_id:
            followup = db.scalar(
                select(TeachbackSession)
                .join(ConceptVersion, ConceptVersion.id == TeachbackSession.concept_version_id)
                .where(
                    TeachbackSession.id == followup_id,
                    TeachbackSession.user_id == owner,
                    ConceptVersion.concept_id == action.concept_id,
                    TeachbackSession.started_at > report.created_at,
                )
            )
            if followup is None:
                raise ApiError(
                    422,
                    "FOLLOWUP_SESSION_MISMATCH",
                    "Follow-up must be your later session for the same concept.",
                )
        # Completion is the user's self-report; it does not alter the immutable report.
        action.followup_session_id, action.status, action.updated_at = followup_id, body.status, utcnow()
        db.flush()
        return {"data": action_dto(action, report)}
