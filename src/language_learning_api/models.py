"""Compatibility import path for the single canonical A+B ORM registry.

Physical table/column names are defined in app.models. API projections belong
in services/routers; this module must never declare a second copy of a table.
"""

from app.models import (  # noqa: F401
    Base,
    Concept,
    ConceptChecklistItem,
    ConceptVersion,
    ConceptVersionEvidence,
    ExamGoal,
    FeedbackReport,
    IdempotencyRequest,
    ImprovementAction,
    Job,
    LearningExplanation,
    Material,
    MaterialChunk,
    MaterialUnit,
    PlanTask,
    PlanTaskConcept,
    PublicAttempt,
    Publication,
    Question,
    QuestionAttempt,
    QuestionConcept,
    QuestionEvidence,
    QuestionSet,
    QuestionSetConcept,
    QuestionSetItem,
    ReportEvidence,
    ReportItem,
    ScoreClaim,
    SessionMessage,
    SessionReport,
    StudyPlan,
    StudyPlanMaterial,
    TeachbackSession,
    User,
)
from app.core import utcnow as utc_now  # noqa: F401

# Class-name aliases only: these map the same physical tables and attributes.
Unit = MaterialUnit
TeachbackMessage = SessionMessage
IdempotencyRecord = IdempotencyRequest
