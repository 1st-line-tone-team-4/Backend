"""Read-only question/attempt projections shared by HTTP routes and the A gateway.

Callers must establish ownership and set membership before passing rows or IDs.
These projections expose public evidence, never the stored answer key. Grading
results are included only for attempts whose durable state is ``graded``.
"""

from sqlalchemy import select

from .core import ApiError, iso
from .models import Material, MaterialChunk, QuestionConcept, QuestionEvidence


def _evidence(chunk, material, excerpt=None):
    if chunk is None or material is None:
        raise ApiError(404, "NOT_FOUND", "Evidence not found.")
    return {
        "chunk_id": chunk.id,
        "material_id": material.id,
        "material_name": material.filename,
        "page_number": chunk.page_number,
        "excerpt": chunk.content if excerpt is None else excerpt,
    }


def evidence_dto(db, chunk_id, excerpt=None):
    chunk = db.get(MaterialChunk, chunk_id)
    material = db.get(Material, chunk.material_id) if chunk is not None else None
    return _evidence(chunk, material, excerpt)


def question_evidence_many(db, question_ids):
    """Load a bounded page's evidence in one query, in the established ID order."""
    result = {question_id: [] for question_id in question_ids}
    if not result:
        return result
    rows = db.execute(
        select(QuestionEvidence.question_id, MaterialChunk, Material)
        .outerjoin(MaterialChunk, MaterialChunk.id == QuestionEvidence.chunk_id)
        .outerjoin(Material, Material.id == MaterialChunk.material_id)
        .where(QuestionEvidence.question_id.in_(result))
        .order_by(QuestionEvidence.question_id, QuestionEvidence.chunk_id)
    )
    for question_id, chunk, material in rows:
        result[question_id].append(_evidence(chunk, material))
    return result


def question_dtos(db, questions):
    """Project already-owned questions with two child queries for the whole page."""
    questions = list(questions)
    links = {question.id: [] for question in questions}
    if not links:
        return {}
    for link in db.scalars(
        select(QuestionConcept)
        .where(QuestionConcept.question_id.in_(links))
        .order_by(QuestionConcept.question_id, QuestionConcept.concept_id)
    ):
        links[link.question_id].append(link)
    evidence = question_evidence_many(db, links)
    return {
        question.id: {
            "id": question.id,
            "question_type": question.question_type,
            "concept_ids": [link.concept_id for link in links[question.id]],
            "concept_version_ids": [link.concept_version_id for link in links[question.id]],
            "prompt": question.prompt,
            "options": question.options,
            "difficulty": question.difficulty,
            "evidence": evidence[question.id],
        }
        for question in questions
    }


def question_dto(db, question):
    return question_dtos(db, [question])[question.id]


def attempt_dtos(attempts, db):
    """Only graded attempts expose results; evidence is read once for the page."""
    attempts = list(attempts)
    evidence = question_evidence_many(
        db, {attempt.question_id for attempt in attempts if attempt.status == "graded"}
    )
    result = []
    for attempt in attempts:
        grading = None
        if attempt.status == "graded":
            grading = {
                "is_correct": attempt.is_correct,
                "feedback": attempt.grading_feedback,
                "correct_answer": attempt.correct_answer,
                "explanation": attempt.explanation,
                "evidence": evidence[attempt.question_id],
            }
        result.append(
            {
                "id": attempt.id,
                "question_id": attempt.question_id,
                "question_set_id": attempt.question_set_id,
                "answer": attempt.answer,
                "status": attempt.status,
                "result": grading,
                "job_id": attempt.job_id,
                "answered_at": iso(attempt.answered_at),
            }
        )
    return result


def attempt_dto(attempt, db):
    return attempt_dtos([attempt], db)[0]
