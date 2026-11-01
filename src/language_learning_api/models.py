from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from language_learning_api.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Store UTC wall time in MySQL DATETIME and restore an aware UTC value on read."""
    impl = DateTime
    cache_ok = True

    def __init__(self):
        super().__init__(timezone=True)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("auth_issuer", "auth_subject", name="uq_users_auth_identity"),
        CheckConstraint("timezone = 'Asia/Seoul'", name="ck_users_timezone_seoul"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    auth_issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Seoul")
    exam_goals: Mapped[list["ExamGoal"]] = relationship(back_populates="owner")


class ExamGoal(Base):
    __tablename__ = "exam_goals"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    exam_type: Mapped[str] = mapped_column(String(32), nullable=False)
    exam_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    owner: Mapped[User] = relationship(back_populates="exam_goals")


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("owner_id", "operation", "key", name="uq_idempotency_owner_operation_key"),)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


# Shared content tables. B-part writes materials/concepts/questions; A-part reads
# them and owns plans, attempts, teach-back, reports and learning records.
class Material(Base):
    __tablename__ = "materials"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready")
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Unit(Base):
    __tablename__ = "units"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    material_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)


class Concept(Base):
    __tablename__ = "concepts"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("units.id", ondelete="RESTRICT"), nullable=False, index=True)
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class ConceptVersion(Base):
    __tablename__ = "concept_versions"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    concept_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="confirmed")
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    checklist: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class StudyPlan(Base):
    __tablename__ = "study_plans"
    __table_args__ = (UniqueConstraint("exam_goal_id", "revision", name="uq_study_plan_goal_revision"),)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    exam_goal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("exam_goals.id", ondelete="RESTRICT"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    tasks: Mapped[list["PlanTask"]] = relationship(back_populates="plan", cascade="all, delete-orphan", order_by="PlanTask.display_order")
    material_links: Mapped[list["StudyPlanMaterial"]] = relationship(back_populates="plan", cascade="all, delete-orphan", order_by="StudyPlanMaterial.ordinal")


class StudyPlanMaterial(Base):
    __tablename__ = "study_plan_materials"
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("study_plans.id", ondelete="RESTRICT"), primary_key=True)
    material_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("materials.id", ondelete="RESTRICT"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    plan: Mapped[StudyPlan] = relationship(back_populates="material_links")


class PlanTask(Base):
    __tablename__ = "plan_tasks"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("study_plans.id", ondelete="RESTRICT"), nullable=False, index=True)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(24), nullable=False)
    concept_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    concept_version_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    concepts: Mapped[list] = mapped_column(JSON, nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    plan: Mapped[StudyPlan] = relationship(back_populates="tasks")


class QuestionSet(Base):
    __tablename__ = "question_sets"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("plan_tasks.id", ondelete="RESTRICT"))
    difficulty: Mapped[str] = mapped_column(String(16), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready")
    concept_version_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("question_sets.id", ondelete="RESTRICT"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    question_type: Mapped[str] = mapped_column(String(24), nullable=False)
    concept_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    concept_version_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    difficulty: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    answer_key: Mapped[dict] = mapped_column(JSON, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    result_url: Mapped[str | None] = mapped_column(String(500))
    previous_job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    model_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now)


class QuestionAttempt(Base):
    __tablename__ = "question_attempts"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    question_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False, index=True)
    question_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("question_sets.id", ondelete="RESTRICT"), nullable=False)
    answer: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="grading")
    result: Mapped[dict | None] = mapped_column(JSON)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False)
    answered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TeachbackSession(Base):
    __tablename__ = "teachback_sessions"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    concept_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=False)
    concept_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("concept_versions.id", ondelete="RESTRICT"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("plan_tasks.id", ondelete="RESTRICT"))
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("question_attempts.id", ondelete="RESTRICT"))
    previous_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    explanation_language: Mapped[str] = mapped_column(String(8), nullable=False)
    student_persona: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TeachbackMessage(Base):
    __tablename__ = "teachback_messages"
    __table_args__ = (UniqueConstraint("session_id", "sequence_no", name="uq_teachback_message_sequence"),)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("teachback_sessions.id", ondelete="RESTRICT"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    input_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class SessionReport(Base):
    __tablename__ = "session_reports"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("teachback_sessions.id", ondelete="RESTRICT"), nullable=False, unique=True)
    concept_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("concept_versions.id", ondelete="RESTRICT"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage: Mapped[float | None] = mapped_column(Float)
    coverage_formula: Mapped[str] = mapped_column(String(40), nullable=False, default="weighted-v1")
    assessable_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weighted_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    next_action: Mapped[str | None] = mapped_column(Text)
    action_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class FeedbackReport(Base):
    __tablename__ = "feedback_reports"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("teachback_sessions.id", ondelete="RESTRICT"), nullable=False)
    report_item_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    reason: Mapped[str] = mapped_column(String(24), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="received")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class ImprovementAction(Base):
    __tablename__ = "improvement_actions"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    report_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("session_reports.id", ondelete="RESTRICT"), nullable=False)
    concept_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    followup_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("teachback_sessions.id", ondelete="RESTRICT"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now)


class PublicAttempt(Base):
    """Immutable private copy of a user's result on a shared question."""
    __tablename__ = "public_attempts"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    publication_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    answer: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    correct_answer: Mapped[dict] = mapped_column(JSON, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    question_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
