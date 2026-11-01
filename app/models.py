"""Canonical shared persistence schema for the A and B API domains."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime as SQLDateTime,
    ForeignKeyConstraint,
    Integer,
    BigInteger,
    JSON,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    TypeDecorator,
    false,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.mysql import LONGTEXT, DATETIME, CHAR

LongText = Text().with_variant(LONGTEXT(), "mysql")
DateTime = SQLDateTime().with_variant(DATETIME(fsp=3), "mysql")


class UUIDStringType(TypeDecorator):
    """Keep the CHAR(36) contract and string results; accept UUID bind values."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(CHAR(36) if dialect.name == "mysql" else String(36))

    def process_bind_param(self, value, dialect):
        return None if value is None else str(value)


UUIDString = UUIDStringType()


def _uid():
    return str(uuid4())


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(UUIDString, primary_key=True)
    auth_issuer = Column(String(255), nullable=False)
    auth_subject = Column(String(255), nullable=False)
    email = Column(String(320))
    display_name = Column(String(120))
    timezone = Column(String(64), nullable=False, default="Asia/Seoul", server_default="Asia/Seoul")
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("auth_issuer", "auth_subject"),
        CheckConstraint("timezone = 'Asia/Seoul'"),
    )


class Job(Base):
    __tablename__ = "ai_jobs"
    id = Column(UUIDString, primary_key=True)
    user_id = Column(UUIDString, nullable=False)
    kind = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    resource_type = Column(String(32), nullable=False)
    resource_id = Column(UUIDString, nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    request_payload = Column(JSON, nullable=False)
    model_id = Column(String(100), nullable=False)
    attempt_no = Column(SmallInteger, nullable=False)
    retryable = Column(Boolean, nullable=False)
    error_code = Column(String(64))
    result_url = Column(String(512))
    fencing_token = Column(BigInteger, nullable=False)
    lease_expires_at = Column(DateTime)
    dispatched_at = Column(DateTime)
    credential_ref = Column(String(64))
    last_polled_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    active_fingerprint = Column(
        String(64),
        Computed(
            "CASE WHEN status IN ('queued','running') THEN request_fingerprint ELSE NULL END", persisted=True
        ),
    )
    active_resource_id = Column(
        UUIDString,
        Computed("CASE WHEN status IN ('queued','running') THEN resource_id ELSE NULL END", persisted=True),
    )
    previous_job_id = Column(UUIDString)
    __table_args__ = (
        CheckConstraint(
            "kind IN ('analysis', 'questions', 'grade', 'message', 'report', 'learning_explanation')"
        ),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'outcome_unknown')"),
        CheckConstraint(
            "resource_type IN ('material', 'question_set', 'attempt', 'session', 'learning_explanation')"
        ),
        UniqueConstraint("user_id", "active_fingerprint"),
        UniqueConstraint("resource_type", "active_resource_id"),
        UniqueConstraint("previous_job_id"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(["previous_job_id"], ["ai_jobs.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        CheckConstraint("attempt_no BETWEEN 1 AND 3"),
        CheckConstraint("fencing_token >= 0"),
        CheckConstraint("status = 'failed' OR retryable = FALSE"),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ('CREDENTIAL_EXPIRED', 'PROVIDER_AUTH', "
            "'PROVIDER_RATE_LIMIT', 'PROVIDER_UNAVAILABLE', 'INVALID_OUTPUT', 'TIMEOUT_UNKNOWN', "
            "'INTERNAL_ERROR')",
            name="ck_ai_jobs_error_code",
        ),
    )


class Material(Base):
    __tablename__ = "materials"
    id = Column(UUIDString, primary_key=True)
    user_id = Column(UUIDString, nullable=False)
    filename = Column(String(255), nullable=False)
    mime_type = Column(String(100), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    storage_key = Column(String(512), nullable=False)
    processing_status = Column(String(32), nullable=False)
    page_count = Column(Integer)
    archived = Column(Boolean, nullable=False, default=False, server_default=false())
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    latest_job_id = Column(UUIDString)
    __table_args__ = (
        CheckConstraint("processing_status IN ('upload_pending', 'queued', 'processing', 'ready', 'failed')"),
        CheckConstraint(
            "mime_type IN ('application/pdf', 'text/plain', 'image/png', 'image/jpeg')",
            name="ck_materials_mime_type",
        ),
        UniqueConstraint("storage_key"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(["latest_job_id"], ["ai_jobs.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        CheckConstraint("size_bytes BETWEEN 1 AND 20971520"),
        CheckConstraint("page_count IS NULL OR page_count >= 1"),
    )


class MaterialUnit(Base):
    __tablename__ = "material_units"
    id = Column(UUIDString, primary_key=True)
    material_id = Column(UUIDString, nullable=False)
    ordinal = Column(Integer, nullable=False)
    title = Column(String(200), nullable=False)
    __table_args__ = (
        UniqueConstraint("material_id", "ordinal"),
        UniqueConstraint("id", "material_id"),
        ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        CheckConstraint("ordinal >= 1"),
    )


class MaterialChunk(Base):
    __tablename__ = "material_chunks"
    id = Column(UUIDString, primary_key=True)
    material_id = Column(UUIDString, nullable=False)
    page_number = Column(Integer, nullable=False)
    ordinal = Column(Integer, nullable=False)
    content = Column(LongText, nullable=False)
    embedding_ref = Column(String(512))
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("material_id", "ordinal"),
        UniqueConstraint("id", "material_id"),
        ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        CheckConstraint("page_number >= 1"),
        CheckConstraint("ordinal >= 1"),
    )


class Concept(Base):
    __tablename__ = "concepts"
    id = Column(UUIDString, primary_key=True)
    material_id = Column(UUIDString, nullable=False)
    unit_id = Column(UUIDString, nullable=False)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("id", "material_id"),
        UniqueConstraint("id", "unit_id"),
        ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(
            ["unit_id", "material_id"],
            ["material_units.id", "material_units.material_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class ConceptVersion(Base):
    __tablename__ = "concept_versions"
    id = Column(UUIDString, primary_key=True)
    concept_id = Column(UUIDString, nullable=False)
    version_no = Column(Integer, nullable=False)
    name = Column(String(200), nullable=False)
    summary = Column(Text)
    is_confirmed = Column(Boolean, nullable=False)
    confirmed_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("concept_id", "version_no"),
        UniqueConstraint("id", "concept_id"),
        ForeignKeyConstraint(["concept_id"], ["concepts.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        CheckConstraint("version_no >= 1"),
        CheckConstraint(
            "(is_confirmed = FALSE AND confirmed_at IS NULL) OR (is_confirmed = TRUE AND confirmed_at IS NOT NULL AND summary IS NOT NULL AND char_length(summary) > 0)"
        ),
    )


class ConceptChecklistItem(Base):
    __tablename__ = "concept_checklist_items"
    id = Column(UUIDString, primary_key=True)
    concept_version_id = Column(UUIDString, nullable=False)
    ordinal = Column(Integer, nullable=False)
    criterion = Column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("concept_version_id", "ordinal"),
        UniqueConstraint("id", "concept_version_id"),
        ForeignKeyConstraint(
            ["concept_version_id"], ["concept_versions.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        CheckConstraint("ordinal BETWEEN 1 AND 50"),
    )


class ConceptVersionEvidence(Base):
    __tablename__ = "concept_version_evidence"
    concept_version_id = Column(UUIDString, primary_key=True)
    chunk_id = Column(UUIDString, primary_key=True)
    quote = Column(Text)
    __table_args__ = (
        ForeignKeyConstraint(
            ["concept_version_id"], ["concept_versions.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(["chunk_id"], ["material_chunks.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
    )


class LearningExplanation(Base):
    __tablename__ = "learning_explanations"
    id = Column(UUIDString, primary_key=True)
    user_id = Column(UUIDString, nullable=False)
    concept_id = Column(UUIDString, nullable=False)
    concept_version_id = Column(UUIDString, nullable=False)
    material_id = Column(UUIDString, nullable=False)
    explanation_language = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    latest_job_id = Column(UUIDString, nullable=False)
    content_title = Column(String(200))
    content_markdown = Column(LongText)
    created_at = Column(DateTime, nullable=False)
    generated_at = Column(DateTime)
    __table_args__ = (
        CheckConstraint("explanation_language IN ('ko', 'en')"),
        CheckConstraint("status IN ('generating', 'ready', 'failed', 'outcome_unknown')"),
        UniqueConstraint("id", "concept_version_id"),
        UniqueConstraint("latest_job_id"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(
            ["concept_version_id"], ["concept_versions.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["concept_version_id", "concept_id"],
            ["concept_versions.id", "concept_versions.concept_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["concept_id", "material_id"],
            ["concepts.id", "concepts.material_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(["latest_job_id"], ["ai_jobs.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        CheckConstraint(
            "(status = 'ready' AND content_title IS NOT NULL AND char_length(content_title) BETWEEN 1 AND 200 AND content_markdown IS NOT NULL AND char_length(content_markdown) BETWEEN 1 AND 24000 AND generated_at IS NOT NULL) OR (status <> 'ready' AND content_title IS NULL AND content_markdown IS NULL AND generated_at IS NULL)"
        ),
        CheckConstraint("generated_at IS NULL OR generated_at >= created_at"),
    )


class LearningExplanationEvidence(Base):
    __tablename__ = "learning_explanation_evidence"
    explanation_id = Column(UUIDString, primary_key=True)
    concept_version_id = Column(UUIDString, nullable=False)
    chunk_id = Column(UUIDString, primary_key=True)
    excerpt = Column(Text, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["explanation_id", "concept_version_id"],
            ["learning_explanations.id", "learning_explanations.concept_version_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["concept_version_id", "chunk_id"],
            ["concept_version_evidence.concept_version_id", "concept_version_evidence.chunk_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("char_length(excerpt) BETWEEN 1 AND 2000"),
    )


class QuestionSet(Base):
    __tablename__ = "question_sets"
    id = Column(UUIDString, primary_key=True)
    user_id = Column(UUIDString, nullable=False)
    unit_id = Column(UUIDString, nullable=False)
    task_id = Column(UUIDString)
    difficulty = Column(String(32), nullable=False)
    requested_count = Column(SmallInteger, nullable=False)
    status = Column(String(32), nullable=False)
    job_id = Column(UUIDString, nullable=False)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("difficulty IN ('easy', 'medium', 'hard')"),
        CheckConstraint("status IN ('generating', 'ready', 'failed')"),
        UniqueConstraint("job_id"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(["unit_id"], ["material_units.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(["task_id"], ["plan_tasks.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(["job_id"], ["ai_jobs.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        CheckConstraint("requested_count BETWEEN 1 AND 20"),
    )


class QuestionSetConcept(Base):
    __tablename__ = "question_set_concepts"
    set_id = Column(UUIDString, primary_key=True)
    concept_id = Column(UUIDString, primary_key=True)
    concept_version_id = Column(UUIDString, nullable=False)
    __table_args__ = (
        UniqueConstraint("set_id", "concept_version_id"),
        ForeignKeyConstraint(["set_id"], ["question_sets.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(
            ["concept_version_id"], ["concept_versions.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["concept_version_id", "concept_id"],
            ["concept_versions.id", "concept_versions.concept_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class Question(Base):
    __tablename__ = "questions"
    id = Column(UUIDString, primary_key=True)
    user_id = Column(UUIDString, nullable=False)
    question_type = Column(String(32), nullable=False)
    prompt = Column(LongText, nullable=False)
    options = Column(JSON, nullable=False)
    answer_key = Column(JSON, nullable=False)
    public_explanation = Column(Text, nullable=False)
    accepted_answers = Column(JSON, nullable=False)
    difficulty = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("question_type IN ('multiple_choice', 'short_answer')"),
        CheckConstraint("difficulty IN ('easy', 'medium', 'hard')"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
    )


class QuestionConcept(Base):
    __tablename__ = "question_concepts"
    question_id = Column(UUIDString, primary_key=True)
    concept_id = Column(UUIDString, primary_key=True)
    concept_version_id = Column(UUIDString, nullable=False)
    __table_args__ = (
        UniqueConstraint("question_id", "concept_version_id"),
        ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(
            ["concept_version_id"], ["concept_versions.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["concept_version_id", "concept_id"],
            ["concept_versions.id", "concept_versions.concept_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class QuestionEvidence(Base):
    __tablename__ = "question_evidence"
    question_id = Column(UUIDString, primary_key=True)
    chunk_id = Column(UUIDString, primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(["chunk_id"], ["material_chunks.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
    )


class QuestionSetItem(Base):
    __tablename__ = "question_set_items"
    set_id = Column(UUIDString, primary_key=True)
    question_id = Column(UUIDString, primary_key=True)
    ordinal = Column(SmallInteger, nullable=False)
    __table_args__ = (
        UniqueConstraint("set_id", "ordinal"),
        UniqueConstraint("question_id"),
        ForeignKeyConstraint(["set_id"], ["question_sets.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
        CheckConstraint("ordinal BETWEEN 1 AND 20"),
    )


class IdempotencyRequest(Base):
    __tablename__ = "idempotency_requests"
    user_id = Column(UUIDString, primary_key=True)
    operation_id = Column(String(120), primary_key=True)
    idempotency_key = Column(
        String(128).with_variant(String(128, collation="utf8mb4_0900_bin"), "mysql"), primary_key=True
    )
    request_hash = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False)
    http_status = Column(SmallInteger)
    response_body = Column(JSON)
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("status IN ('in_progress', 'completed')"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"),
    )


class StorageDeletion(Base):
    __tablename__ = "storage_deletion_outbox"
    id = Column(UUIDString, primary_key=True)
    storage_key = Column(String(512), nullable=False)
    delete_after = Column(DateTime, nullable=False)
    deleted_at = Column(DateTime)
    attempt_count = Column(Integer, nullable=False)
    __table_args__ = (CheckConstraint("attempt_count >= 0"),)


class ExamGoal(Base):
    __tablename__ = "exam_goals"
    id = Column(UUIDString, primary_key=True, default=_uid)
    user_id = Column(UUIDString, nullable=False)
    title = Column(String(120), nullable=False)
    exam_type = Column(String(32), nullable=False)
    exam_date = Column(Date, nullable=False)
    status = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)
    __table_args__ = (
        CheckConstraint(
            "exam_type IN ('school_exam','toeic','toefl','entrance_exam','other')",
            name="ck_exam_goals_exam_type",
        ),
        CheckConstraint("status IN ('active','paused','completed')", name="ck_exam_goals_status"),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_exam_goals_0", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
    )


class StudyPlan(Base):
    __tablename__ = "study_plans"
    id = Column(UUIDString, primary_key=True, default=_uid)
    user_id = Column(UUIDString, nullable=False)
    exam_goal_id = Column(UUIDString, nullable=False)
    revision = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False)
    generated_at = Column(DateTime, nullable=False, default=_utcnow)
    active_goal_id = Column(
        UUIDString, Computed("CASE WHEN status = 'active' THEN exam_goal_id ELSE NULL END", persisted=True)
    )
    __table_args__ = (
        CheckConstraint("status IN ('active','superseded')", name="ck_study_plans_status"),
        UniqueConstraint("exam_goal_id", "revision", name="uq_study_plans_0"),
        UniqueConstraint("active_goal_id", name="uq_study_plans_1"),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_study_plans_0", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["exam_goal_id"],
            ["exam_goals.id"],
            name="fk_study_plans_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="ck_study_plans_0"),
    )


class StudyPlanMaterial(Base):
    __tablename__ = "study_plan_materials"
    study_plan_id = Column(UUIDString, primary_key=True)
    material_id = Column(UUIDString, primary_key=True)
    ordinal = Column(Integer, nullable=False)
    __table_args__ = (
        UniqueConstraint("study_plan_id", "ordinal", name="uq_study_plan_materials_0"),
        ForeignKeyConstraint(
            ["study_plan_id"],
            ["study_plans.id"],
            name="fk_study_plan_materials_0",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name="fk_study_plan_materials_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("ordinal >= 1", name="ck_study_plan_materials_0"),
    )


class PlanTask(Base):
    __tablename__ = "plan_tasks"
    id = Column(UUIDString, primary_key=True, default=_uid)
    study_plan_id = Column(UUIDString, nullable=False)
    ordinal = Column(Integer, nullable=False)
    due_date = Column(Date, nullable=False)
    task_type = Column(String(32), nullable=False)
    target_count = Column(SmallInteger, nullable=False)
    status = Column(String(32), nullable=False)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    __table_args__ = (
        CheckConstraint("task_type IN ('solve','teach_back','review')", name="ck_plan_tasks_task_type"),
        CheckConstraint(
            "status IN ('pending','in_progress','completed','skipped')", name="ck_plan_tasks_status"
        ),
        UniqueConstraint("study_plan_id", "ordinal", name="uq_plan_tasks_0"),
        ForeignKeyConstraint(
            ["study_plan_id"],
            ["study_plans.id"],
            name="fk_plan_tasks_0",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("ordinal >= 1", name="ck_plan_tasks_0"),
        CheckConstraint("target_count BETWEEN 1 AND 20", name="ck_plan_tasks_1"),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_plan_tasks_2",
        ),
    )


class PlanTaskConcept(Base):
    __tablename__ = "plan_task_concepts"
    task_id = Column(UUIDString, primary_key=True)
    concept_id = Column(UUIDString, primary_key=True)
    concept_version_id = Column(UUIDString, nullable=False)
    __table_args__ = (
        UniqueConstraint("task_id", "concept_version_id", name="uq_plan_task_concepts_0"),
        ForeignKeyConstraint(
            ["task_id"],
            ["plan_tasks.id"],
            name="fk_plan_task_concepts_0",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["concept_version_id"],
            ["concept_versions.id"],
            name="fk_plan_task_concepts_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["concept_version_id", "concept_id"],
            ["concept_versions.id", "concept_versions.concept_id"],
            name="fk_plan_task_concepts_2",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class QuestionAttempt(Base):
    __tablename__ = "question_attempts"
    id = Column(UUIDString, primary_key=True, default=_uid)
    user_id = Column(UUIDString, nullable=False)
    question_id = Column(UUIDString, nullable=False)
    question_set_id = Column(UUIDString, nullable=False)
    answer = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False)
    is_correct = Column(Boolean, nullable=True)
    grading_feedback = Column(Text, nullable=True)
    correct_answer = Column(JSON, nullable=True)
    explanation = Column(Text, nullable=True)
    job_id = Column(UUIDString, nullable=False)
    answered_at = Column(DateTime, nullable=False, default=_utcnow)
    __table_args__ = (
        CheckConstraint("status IN ('grading','graded','failed')", name="ck_question_attempts_status"),
        UniqueConstraint("job_id", name="uq_question_attempts_0"),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_question_attempts_0", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["question_set_id", "question_id"],
            ["question_set_items.set_id", "question_set_items.question_id"],
            name="fk_question_attempts_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["job_id"],
            ["ai_jobs.id"],
            name="fk_question_attempts_2",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "(status = 'graded' AND is_correct IS NOT NULL AND grading_feedback IS NOT NULL AND correct_answer IS NOT NULL AND explanation IS NOT NULL) OR (status <> 'graded' AND is_correct IS NULL)",
            name="ck_question_attempts_0",
        ),
    )


class TeachbackSession(Base):
    __tablename__ = "teachback_sessions"
    id = Column(UUIDString, primary_key=True, default=_uid)
    user_id = Column(UUIDString, nullable=False)
    concept_version_id = Column(UUIDString, nullable=False)
    task_id = Column(UUIDString, nullable=True)
    attempt_id = Column(UUIDString, nullable=True)
    previous_session_id = Column(UUIDString, nullable=True)
    explanation_language = Column(String(32), nullable=False)
    student_persona = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    question_count = Column(SmallInteger, nullable=False)
    started_at = Column(DateTime, nullable=False, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)
    latest_job_id = Column(UUIDString, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "explanation_language IN ('ko','en')", name="ck_teachback_sessions_explanation_language"
        ),
        CheckConstraint(
            "student_persona IN ('curious_beginner','confused_friend','exam_candidate')",
            name="ck_teachback_sessions_student_persona",
        ),
        CheckConstraint(
            "status IN ('active','report_pending','completed','abandoned')",
            name="ck_teachback_sessions_status",
        ),
        UniqueConstraint("id", "concept_version_id", name="uq_teachback_sessions_0"),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_teachback_sessions_0",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["concept_version_id"],
            ["concept_versions.id"],
            name="fk_teachback_sessions_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["task_id"],
            ["plan_tasks.id"],
            name="fk_teachback_sessions_2",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["attempt_id"],
            ["question_attempts.id"],
            name="fk_teachback_sessions_3",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_session_id"],
            ["teachback_sessions.id"],
            name="fk_teachback_sessions_4",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["latest_job_id"],
            ["ai_jobs.id"],
            name="fk_teachback_sessions_5",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("question_count BETWEEN 0 AND 3", name="ck_teachback_sessions_0"),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_teachback_sessions_1",
        ),
    )


class SessionMessage(Base):
    __tablename__ = "session_messages"
    id = Column(UUIDString, primary_key=True, default=_uid)
    session_id = Column(UUIDString, nullable=False)
    sequence_no = Column(Integer, nullable=False)
    role = Column(String(32), nullable=False)
    input_mode = Column(String(32), nullable=False)
    content = Column(LongText, nullable=False)
    job_id = Column(UUIDString, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="ck_session_messages_role"),
        CheckConstraint("input_mode IN ('text','voice_transcript')", name="ck_session_messages_input_mode"),
        UniqueConstraint("session_id", "sequence_no", name="uq_session_messages_0"),
        UniqueConstraint("job_id", "role", name="uq_session_messages_1"),
        ForeignKeyConstraint(
            ["session_id"],
            ["teachback_sessions.id"],
            name="fk_session_messages_0",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["job_id"], ["ai_jobs.id"], name="fk_session_messages_1", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        CheckConstraint("sequence_no >= 1", name="ck_session_messages_0"),
    )


class SessionReport(Base):
    __tablename__ = "session_reports"
    id = Column(UUIDString, primary_key=True, default=_uid)
    session_id = Column(UUIDString, nullable=False)
    concept_version_id = Column(UUIDString, nullable=False)
    coverage = Column(Numeric(5, 4), nullable=True)
    coverage_formula = Column(String(32), nullable=False, server_default="weighted-v1", default="weighted-v1")
    assessable_count = Column(SmallInteger, nullable=False)
    weighted_score = Column(Numeric(6, 2), nullable=False)
    next_action = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_session_reports_0"),
        UniqueConstraint("id", "concept_version_id", name="uq_session_reports_1"),
        ForeignKeyConstraint(
            ["concept_version_id"],
            ["concept_versions.id"],
            name="fk_session_reports_0",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["session_id", "concept_version_id"],
            ["teachback_sessions.id", "teachback_sessions.concept_version_id"],
            name="fk_session_reports_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("assessable_count >= 0", name="ck_session_reports_0"),
        CheckConstraint(
            "weighted_score >= 0 AND weighted_score <= assessable_count", name="ck_session_reports_1"
        ),
        CheckConstraint("coverage_formula = 'weighted-v1'", name="ck_session_reports_2"),
        CheckConstraint(
            "(assessable_count = 0 AND coverage IS NULL AND weighted_score = 0) OR (assessable_count > 0 AND coverage IS NOT NULL AND coverage = ROUND(weighted_score * 1.0 / assessable_count, 4))",
            name="ck_session_reports_3",
        ),
    )


class ReportItem(Base):
    __tablename__ = "report_items"
    id = Column(UUIDString, primary_key=True, default=_uid)
    report_id = Column(UUIDString, nullable=False)
    concept_version_id = Column(UUIDString, nullable=False)
    checklist_item_id = Column(UUIDString, nullable=False)
    status = Column(String(32), nullable=False)
    description = Column(Text, nullable=False)
    confidence = Column(String(32), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "status IN ('explained','partial','omission','error','unverified')", name="ck_report_items_status"
        ),
        CheckConstraint("confidence IN ('low','medium','high')", name="ck_report_items_confidence"),
        UniqueConstraint("report_id", "checklist_item_id", name="uq_report_items_0"),
        UniqueConstraint("id", "report_id", name="uq_report_items_1"),
        ForeignKeyConstraint(
            ["concept_version_id"],
            ["concept_versions.id"],
            name="fk_report_items_0",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["report_id", "concept_version_id"],
            ["session_reports.id", "session_reports.concept_version_id"],
            name="fk_report_items_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["checklist_item_id", "concept_version_id"],
            ["concept_checklist_items.id", "concept_checklist_items.concept_version_id"],
            name="fk_report_items_2",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class ReportEvidence(Base):
    __tablename__ = "report_evidence"
    report_item_id = Column(UUIDString, primary_key=True)
    chunk_id = Column(UUIDString, primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["report_item_id"],
            ["report_items.id"],
            name="fk_report_evidence_0",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["chunk_id"],
            ["material_chunks.id"],
            name="fk_report_evidence_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class ImprovementAction(Base):
    __tablename__ = "improvement_actions"
    id = Column(UUIDString, primary_key=True, default=_uid)
    report_id = Column(UUIDString, nullable=False)
    concept_id = Column(UUIDString, nullable=False)
    status = Column(String(32), nullable=False)
    followup_session_id = Column(UUIDString, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','attempted','completed')", name="ck_improvement_actions_status"
        ),
        UniqueConstraint("report_id", name="uq_improvement_actions_0"),
        ForeignKeyConstraint(
            ["report_id"],
            ["session_reports.id"],
            name="fk_improvement_actions_0",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["concept_id"],
            ["concepts.id"],
            name="fk_improvement_actions_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["followup_session_id"],
            ["teachback_sessions.id"],
            name="fk_improvement_actions_2",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class FeedbackReport(Base):
    __tablename__ = "feedback_reports"
    id = Column(UUIDString, primary_key=True, default=_uid)
    user_id = Column(UUIDString, nullable=False)
    session_id = Column(UUIDString, nullable=False)
    report_id = Column(UUIDString, nullable=False)
    report_item_id = Column(UUIDString, nullable=True)
    reason = Column(String(32), nullable=False)
    comment = Column(String(2000), nullable=True)
    status = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    __table_args__ = (
        CheckConstraint(
            "reason IN ('incorrect','unsupported','unclear','other')", name="ck_feedback_reports_reason"
        ),
        CheckConstraint(
            "status IN ('received','reviewing','resolved','rejected')", name="ck_feedback_reports_status"
        ),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_feedback_reports_0", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["session_id"],
            ["teachback_sessions.id"],
            name="fk_feedback_reports_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["report_id"],
            ["session_reports.id"],
            name="fk_feedback_reports_2",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["report_item_id", "report_id"],
            ["report_items.id", "report_items.report_id"],
            name="fk_feedback_reports_3",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class Publication(Base):
    __tablename__ = "publications"
    id = Column(UUIDString, primary_key=True, default=_uid)
    user_id = Column(UUIDString, nullable=False)
    question_id = Column(UUIDString, nullable=False)
    status = Column(String(32), nullable=False)
    question_type = Column(String(32), nullable=False)
    prompt = Column(LongText, nullable=False)
    options = Column(JSON, nullable=False)
    answer_key = Column(JSON, nullable=False)
    accepted_answers = Column(JSON, nullable=False)
    explanation = Column(Text, nullable=False)
    difficulty = Column(String(32), nullable=False)
    consented_at = Column(DateTime, nullable=False, default=_utcnow)
    published_at = Column(DateTime, nullable=False, default=_utcnow)
    withdrawn_at = Column(DateTime, nullable=True)
    active_question_id = Column(
        UUIDString, Computed("CASE WHEN status = 'active' THEN question_id ELSE NULL END", persisted=True)
    )
    keywords = Column(JSON, nullable=False)
    __table_args__ = (
        CheckConstraint("status IN ('active','withdrawn')", name="ck_publications_status"),
        CheckConstraint(
            "question_type IN ('multiple_choice','short_answer')", name="ck_publications_question_type"
        ),
        CheckConstraint("difficulty IN ('easy','medium','hard')", name="ck_publications_difficulty"),
        UniqueConstraint("active_question_id", name="uq_publications_0"),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_publications_0", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name="fk_publications_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class PublicAttempt(Base):
    __tablename__ = "public_attempts"
    id = Column(UUIDString, primary_key=True, default=_uid)
    user_id = Column(UUIDString, nullable=False)
    publication_id = Column(UUIDString, nullable=False)
    answer = Column(JSON, nullable=False)
    is_correct = Column(Boolean, nullable=False)
    correct_answer = Column(JSON, nullable=False)
    explanation = Column(Text, nullable=False)
    question_snapshot = Column(JSON, nullable=False)
    answered_at = Column(DateTime, nullable=False, default=_utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_public_attempts_0", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name="fk_public_attempts_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )


class ScoreClaim(Base):
    __tablename__ = "score_claims"
    id = Column(UUIDString, primary_key=True, default=_uid)
    user_id = Column(UUIDString, nullable=False)
    public_attempt_id = Column(UUIDString, nullable=False)
    exam_type = Column(String(32), nullable=False)
    score = Column(Numeric(10, 2), nullable=False)
    max_score = Column(Numeric(10, 2), nullable=False)
    exam_date = Column(Date, nullable=False)
    status = Column(String(32), nullable=False)
    proof_storage_key = Column(String(512), nullable=False)
    mime_type = Column(String(100), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    reviewer_id = Column(UUIDString, nullable=True)
    reason = Column(String(1000), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    proof_deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    active_attempt_id = Column(
        UUIDString,
        Computed(
            "CASE WHEN status IN ('upload_pending','submitted','verified') THEN public_attempt_id ELSE NULL END",
            persisted=True,
        ),
    )
    public_score_consented_at = Column(DateTime, nullable=False, default=_utcnow)
    __table_args__ = (
        CheckConstraint(
            "exam_type IN ('toeic','toefl','entrance_exam','other')", name="ck_score_claims_exam_type"
        ),
        CheckConstraint(
            "status IN ('upload_pending','submitted','verified','rejected','withdrawn')",
            name="ck_score_claims_status",
        ),
        UniqueConstraint("active_attempt_id", "exam_date", name="uq_score_claims_0"),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_score_claims_0", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["public_attempt_id"],
            ["public_attempts.id"],
            name="fk_score_claims_1",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reviewer_id"], ["users.id"], name="fk_score_claims_2", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        CheckConstraint("score >= 0 AND max_score > 0 AND score <= max_score", name="ck_score_claims_0"),
        CheckConstraint("size_bytes BETWEEN 1 AND 10485760", name="ck_score_claims_1"),
        CheckConstraint("reviewer_id IS NULL OR reviewer_id <> user_id", name="ck_score_claims_2"),
        CheckConstraint("exam_type <> 'toeic' OR max_score = 990", name="ck_score_claims_3"),
        CheckConstraint("exam_type <> 'toefl' OR max_score = 120", name="ck_score_claims_4"),
    )
