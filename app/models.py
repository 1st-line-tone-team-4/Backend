"""B persistence schema; A task table is a minimal integration reference."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime as SQLDateTime,
    ForeignKeyConstraint,
    Integer,
    BigInteger,
    JSON,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.mysql import LONGTEXT, DATETIME, CHAR

LongText = Text().with_variant(LONGTEXT(), "mysql")
DateTime = SQLDateTime().with_variant(DATETIME(fsp=3), "mysql")
UUIDString = String(36).with_variant(CHAR(36), "mysql")

Base = declarative_base()


class PlanTask(Base):
    __tablename__ = "plan_tasks"
    id = Column(UUIDString, primary_key=True)


class User(Base):
    __tablename__ = "users"
    id = Column(UUIDString, primary_key=True)
    auth_issuer = Column(String(255), nullable=False)
    auth_subject = Column(String(255), nullable=False)
    email = Column(String(320))
    display_name = Column(String(120))
    timezone = Column(String(64), nullable=False, default="Asia/Seoul")
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
