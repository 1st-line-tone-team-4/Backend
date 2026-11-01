from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExamGoalInput(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    exam_type: Literal["school_exam", "toeic", "toefl", "entrance_exam", "other"]
    exam_date: date


class ExamGoalPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    exam_date: date | None = None
    status: Literal["active", "paused", "completed"] | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("PATCH 필드는 null일 수 없습니다.")
        return value


class CoursePreview(BaseModel):
    days_remaining: int = Field(ge=0)
    eligible: bool
    reason: Literal["EXAM_TOO_FAR", "EXAM_PASSED"] | None
    stages: list[Literal["concept", "practice", "teach_back", "review"]]


class ExamGoalResponse(BaseModel):
    id: UUID
    title: str
    exam_type: str
    exam_date: date
    status: Literal["active", "paused", "completed"]
    course_preview: CoursePreview


class ExamGoalEnvelope(BaseModel):
    data: ExamGoalResponse


class ExamGoalPage(BaseModel):
    data: list[ExamGoalResponse]
    next_cursor: str | None


class UserProfile(BaseModel):
    id: UUID
    email: str | None
    display_name: str | None
    timezone: Literal["Asia/Seoul"]


class UserProfileEnvelope(BaseModel):
    data: UserProfile


class PlanTaskConcept(BaseModel):
    concept_id: UUID
    concept_version_id: UUID
    concept_name: str
    material_id: UUID
    unit_id: UUID
    unit_title: str


class PlanTaskResponse(BaseModel):
    id: UUID
    due_date: date
    task_type: Literal["concept", "solve", "teach_back", "review"]
    concept_ids: list[UUID]
    concept_version_ids: list[UUID]
    target_count: int
    completed_count: int
    status: Literal["pending", "in_progress", "completed", "skipped"]
    completed_at: datetime | None
    title: str
    display_order: int
    concepts: list[PlanTaskConcept]


class StudyPlanResponse(BaseModel):
    id: UUID
    exam_goal_id: UUID
    revision: int
    status: Literal["active", "superseded"]
    generated_at: datetime
    tasks: list[PlanTaskResponse]
    material_ids: list[UUID]


class StudyPlanSummary(BaseModel):
    id: UUID
    exam_goal_id: UUID
    revision: int
    status: Literal["active", "superseded"]
    generated_at: datetime
    material_ids: list[UUID]
    task_count: int


class StudyPlanEnvelope(BaseModel):
    data: StudyPlanResponse


class StudyPlanPage(BaseModel):
    data: list[StudyPlanSummary]
    next_cursor: str | None


class StudyPlanInput(StrictModel):
    exam_goal_id: UUID
    material_ids: list[UUID] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_materials(self):
        if len(set(self.material_ids)) != len(self.material_ids):
            raise ValueError("material_ids must be unique")
        return self


class PlanTaskPatch(StrictModel):
    status: Literal["pending", "completed", "skipped"]


class PlanTaskEnvelope(BaseModel):
    data: PlanTaskResponse


class TodayConcept(BaseModel):
    concept_id: UUID
    concept_version_id: UUID
    concept_name: str
    material_id: UUID
    unit_id: UUID
    unit_title: str


class TodayTask(PlanTaskResponse):
    pass


class TodaySummary(BaseModel):
    date: date
    progress: float
    tasks: list[TodayTask]
    next_action: str | None
    exam_goal_id: UUID | None
    study_plan_id: UUID | None
    days_remaining: int | None


class TodayEnvelope(BaseModel):
    data: TodaySummary


class QuestionOption(BaseModel):
    id: str
    text: str


class Evidence(BaseModel):
    chunk_id: UUID
    material_id: UUID
    material_name: str
    page_number: int | None = None
    excerpt: str


class QuestionResponse(BaseModel):
    id: UUID
    question_type: Literal["multiple_choice", "short_answer"]
    concept_ids: list[UUID]
    concept_version_ids: list[UUID]
    prompt: str
    options: list[QuestionOption]
    difficulty: Literal["easy", "medium", "hard"]
    evidence: list[Evidence]


class QuestionEnvelope(BaseModel):
    data: QuestionResponse


class MultipleChoiceAnswer(StrictModel):
    type: Literal["multiple_choice"]
    selected_option_ids: list[str] = Field(min_length=1, max_length=1)


class ShortAnswer(StrictModel):
    type: Literal["short_answer"]
    text: str = Field(min_length=1, max_length=12000)


QuestionAnswer = Annotated[MultipleChoiceAnswer | ShortAnswer, Field(discriminator="type")]


class AttemptInput(StrictModel):
    answer: QuestionAnswer
    question_set_id: UUID
    model_id: str = Field(min_length=1, max_length=100)


class QuestionResult(BaseModel):
    is_correct: bool
    feedback: str
    correct_answer: QuestionAnswer
    explanation: str
    evidence: list[Evidence]


class QuestionAttemptResponse(BaseModel):
    id: UUID
    question_id: UUID
    question_set_id: UUID
    answer: QuestionAnswer
    status: Literal["grading", "graded", "failed"]
    result: QuestionResult | None
    job_id: UUID
    answered_at: datetime


class QuestionAttemptPage(BaseModel):
    data: list[QuestionAttemptResponse]
    next_cursor: str | None


class AcceptedJob(BaseModel):
    id: UUID
    kind: Literal["analysis", "questions", "grade", "message", "report", "learning_explanation"]
    status: Literal["queued", "running", "succeeded", "failed", "outcome_unknown"]
    resource_type: Literal["material", "question_set", "attempt", "session", "learning_explanation"]
    resource_id: UUID
    attempt_no: int = 1
    retryable: bool = False
    error_code: str | None = None
    result_url: str | None = None
    created_at: datetime
    updated_at: datetime
    previous_job_id: UUID | None = None


class AcceptedEnvelope(BaseModel):
    data: dict[str, AcceptedJob]


class TeachbackSessionInput(StrictModel):
    concept_version_id: UUID
    task_id: UUID | None = None
    attempt_id: UUID | None = None
    previous_session_id: UUID | None = None
    explanation_language: Literal["ko", "en"]
    student_persona: Literal["curious_beginner", "confused_friend", "exam_candidate"]


class TeachbackSessionResponse(BaseModel):
    id: UUID
    concept_id: UUID
    concept_version_id: UUID
    version_no: int
    task_id: UUID | None
    attempt_id: UUID | None
    previous_session_id: UUID | None
    explanation_language: Literal["ko", "en"]
    student_persona: str
    status: Literal["active", "report_pending", "completed", "abandoned"]
    question_count: int
    latest_job_id: UUID | None
    started_at: datetime
    completed_at: datetime | None


class TeachbackEnvelope(BaseModel):
    data: TeachbackSessionResponse


class TeachbackSessionPatch(StrictModel):
    status: Literal["abandoned"]


class MessageInput(StrictModel):
    content: str = Field(min_length=1, max_length=12000)
    input_mode: Literal["text", "voice_transcript"]
    model_id: str = Field(min_length=1, max_length=100)


class MessageResponse(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    content: str
    input_mode: Literal["text", "voice_transcript"]
    sequence_no: int
    created_at: datetime


class MessagePage(BaseModel):
    data: list[MessageResponse]
    next_cursor: str | None


class CompleteSessionInput(StrictModel):
    model_id: str = Field(min_length=1, max_length=100)


class FinalAnswerInput(StrictModel):
    content: str = Field(min_length=1, max_length=12000)
    input_mode: Literal["text", "voice_transcript"]


class ReportItem(BaseModel):
    id: UUID
    concept_id: UUID
    concept_version_id: UUID
    checklist_item_id: UUID
    status: Literal["explained", "partial", "omission", "error", "unverified"]
    description: str
    confidence: Literal["low", "medium", "high"]
    evidence: list[Evidence]


class SessionReportResponse(BaseModel):
    id: UUID
    session_id: UUID
    concept_version_id: UUID
    version_no: int
    coverage: float | None
    coverage_formula: str
    assessable_count: int
    weighted_score: float
    items: list[ReportItem]
    next_action: str | None
    action_id: UUID | None


class SessionReportEnvelope(BaseModel):
    data: SessionReportResponse


class FeedbackInput(StrictModel):
    report_item_id: UUID | None = None
    reason: Literal["incorrect", "unsupported", "unclear", "other"]
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackResponse(BaseModel):
    id: UUID
    session_id: UUID
    report_item_id: UUID | None
    reason: str
    comment: str | None
    status: Literal["received", "reviewing", "resolved", "rejected"]
    created_at: datetime


class FeedbackEnvelope(BaseModel):
    data: FeedbackResponse


class WeaknessResponse(BaseModel):
    concept_id: UUID
    concept_name: str
    concept_version_id: UUID
    latest_coverage: float | None
    incorrect_attempts: int
    teachback_count: int
    trend: Literal["improving", "stable", "declining", "unknown"]


class WeaknessPage(BaseModel):
    data: list[WeaknessResponse]
    next_cursor: str | None


class SessionSummary(BaseModel):
    id: UUID
    concept_id: UUID
    concept_version_id: UUID
    version_no: int
    status: str
    explanation_language: Literal["ko", "en"]
    started_at: datetime


class SessionSummaryPage(BaseModel):
    data: list[SessionSummary]
    next_cursor: str | None


class ImprovementActionResponse(BaseModel):
    id: UUID
    report_id: UUID
    concept_id: UUID
    text: str
    status: Literal["pending", "attempted", "completed"]
    followup_session_id: UUID | None
    updated_at: datetime


class ImprovementActionPage(BaseModel):
    data: list[ImprovementActionResponse]
    next_cursor: str | None


class ImprovementActionPatch(StrictModel):
    status: Literal["pending", "attempted", "completed"]
    followup_session_id: UUID | None = None


class PublicQuestionSnapshot(BaseModel):
    question_type: Literal["multiple_choice", "short_answer"]
    prompt: str
    options: list[QuestionOption]
    difficulty: Literal["easy", "medium", "hard"]


class PublicAttemptResponse(BaseModel):
    id: UUID
    publication_id: UUID
    answer: QuestionAnswer
    is_correct: bool
    correct_answer: QuestionAnswer
    explanation: str
    answered_at: datetime
    question_snapshot: PublicQuestionSnapshot


class PublicAttemptPage(BaseModel):
    data: list[PublicAttemptResponse]
    next_cursor: str | None
