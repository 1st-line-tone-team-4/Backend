"""Teach-back domain participant in B's fenced, transactional worker."""

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select

from app.a_gateway import lock_owned_task, update_teachback_progress
from app.ai_context import pinned_study_context, wants_ai_context
from app.core import ApiError, uid, utcnow
from app.materials import get_confirmed_version
from app.models import (
    ImprovementAction,
    PlanTaskConcept,
    ReportEvidence,
    ReportItem,
    SessionMessage,
    SessionReport,
    TeachbackSession,
)
from .teachback_prompts import (
    pinned_context,
    prepare_message_prompt,
    prepare_report_prompt,
    validate_message_output,
    validate_report_output,
)


def owned_session(db, owner, session_id, *, lock=False, active_task=True):
    session = db.scalar(
        select(TeachbackSession).where(
            TeachbackSession.id == str(session_id), TeachbackSession.user_id == owner
        )
    )
    if session is None:
        raise ApiError(404, "NOT_FOUND", "Session not found.")
    if lock:
        # Read immutable task ID first, then share goal -> plan -> task -> session order.
        if session.task_id:
            lock_owned_task(db, owner, session.task_id, active=active_task, task_type="teach_back")
        session = db.scalar(
            select(TeachbackSession)
            .where(TeachbackSession.id == session.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    return session


class TeachbackHandler:
    def __init__(self, ctx, kind):
        if kind not in {"message", "report"}:
            raise ValueError("Unknown teach-back kind")
        self.ctx, self.kind = ctx, kind

    def is_current(self, db, job):
        session = db.get(TeachbackSession, job.resource_id)
        expected = "active" if self.kind == "message" else "report_pending"
        return bool(
            session
            and session.user_id == job.user_id
            and session.latest_job_id == job.id
            and session.status == expected
        )

    def lock_resource(self, db, job, *, require_failed=False):
        # Accepted paid work may finish after a plan/goal is paused. Domain
        # pointer/state still fence abandoned sessions; progress skips inactive plans.
        session = owned_session(db, job.user_id, job.resource_id, lock=True, active_task=False)
        if job.kind != self.kind or job.resource_type != "session" or not self.is_current(db, job):
            raise ApiError(409, "STALE_JOB", "The session cannot accept this job.")
        # Both a retry and a cost-acknowledged restart use this guard.
        if require_failed and job.status not in {"failed", "outcome_unknown"}:
            raise ApiError(409, "STALE_JOB", "The job is not terminal.")
        version = get_confirmed_version(db, job.user_id, session.concept_version_id)
        if session.task_id and not db.scalar(
            select(PlanTaskConcept.task_id).where(
                PlanTaskConcept.task_id == session.task_id, PlanTaskConcept.concept_version_id == version.id
            )
        ):
            raise ApiError(409, "STALE_JOB", "The task no longer pins this version.")
        if self.kind == "message":
            last = db.scalar(
                select(SessionMessage)
                .where(SessionMessage.session_id == session.id)
                .order_by(SessionMessage.sequence_no.desc())
                .limit(1)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                session.question_count >= 3
                or not last
                or last.role != "user"
                or last.id != job.request_payload.get("message_id")
            ):
                raise ApiError(409, "STALE_JOB", "The message was superseded.")
        elif db.scalar(select(SessionReport.id).where(SessionReport.session_id == session.id)):
            raise ApiError(409, "STALE_JOB", "The session already has a report.")
        return session, "latest_job_id"

    def set_state(self, db, job, state):
        # Failure/unknown keeps active or report_pending; engine owns all job state.
        return None

    def prepare(self, db, job):
        session, _ = self.lock_resource(db, job)
        version = get_confirmed_version(db, job.user_id, session.concept_version_id)
        payload = (prepare_message_prompt if self.kind == "message" else prepare_report_prompt)(
            db, session, version
        )
        if self.kind == "report" and wants_ai_context(self.ctx, job.model_id):
            payload["ai_context"] = {"context": pinned_study_context(db, version, payload["source_evidence"])}
        return payload

    def persist(self, db, job, output):
        session, _ = self.lock_resource(db, job)
        version = get_confirmed_version(db, job.user_id, session.concept_version_id)
        if self.kind == "message":
            content = validate_message_output(output)
            last = db.scalar(
                select(SessionMessage)
                .where(SessionMessage.session_id == session.id)
                .order_by(SessionMessage.sequence_no.desc())
                .limit(1)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            db.add(
                SessionMessage(
                    id=uid(),
                    session_id=session.id,
                    role="assistant",
                    content=content,
                    input_mode="text",
                    sequence_no=last.sequence_no + 1,
                    job_id=job.id,
                    created_at=utcnow(),
                )
            )
            session.question_count += 1
            db.flush()
            return f"/v1/teachback-sessions/{session.id}/messages"
        validated = validate_report_output(output, pinned_context(db, session, version))
        assessable = [i for i in validated["items"] if i["status"] != "unverified"]
        score = sum(
            {"explained": 1, "partial": 0.5, "omission": 0, "error": 0}[i["status"]] for i in assessable
        )
        coverage = (
            (Decimal(str(score)) / Decimal(len(assessable))).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if assessable
            else None
        )
        report = SessionReport(
            id=uid(),
            session_id=session.id,
            concept_version_id=version.id,
            coverage=coverage,
            coverage_formula="weighted-v1",
            assessable_count=len(assessable),
            weighted_score=score,
            next_action=validated["next_action"],
            created_at=utcnow(),
        )
        db.add(report)
        db.flush()
        for item in validated["items"]:
            row = ReportItem(
                id=uid(),
                report_id=report.id,
                concept_version_id=version.id,
                checklist_item_id=item["checklist_item_id"],
                status=item["status"],
                description=item["description"],
                confidence=item["confidence"],
            )
            db.add(row)
            db.flush()
            db.add_all(
                [ReportEvidence(report_item_id=row.id, chunk_id=chunk_id) for chunk_id in item["chunk_ids"]]
            )
        db.add(
            ImprovementAction(
                id=uid(),
                report_id=report.id,
                concept_id=version.concept_id,
                status="pending",
                updated_at=utcnow(),
            )
        )
        session.status, session.completed_at = "completed", utcnow()
        db.flush()
        if session.task_id:
            update_teachback_progress(db, session.user_id, session.task_id)
        return f"/v1/teachback-sessions/{session.id}/report"
