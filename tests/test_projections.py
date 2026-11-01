"""Public read models keep answer redaction and bounded child-query costs."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.content import QuestionsHandler, get_grading_question, set_dto
from app.core import ApiError, iso, uid, utcnow
from app.models import (
    Concept,
    ConceptVersion,
    Job,
    MaterialChunk,
    Question,
    QuestionConcept,
    QuestionEvidence,
    QuestionSet,
)
from app.projections import attempt_dtos, question_dtos
from tests.test_content import question_output, setup as _content_setup

content_setup = _content_setup


@contextmanager
def select_count(engine):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def create_questions(db, ids, count):
    with db.transaction() as session:
        question_set = session.get(QuestionSet, ids["set"])
        question_set.requested_count = count
        output = {"questions": [question_output(ids)["questions"][0] for _ in range(count)]}
        job = session.get(Job, ids["job"])
        handler = QuestionsHandler(None)
        handler.persist(session, job, output)
        handler.set_state(session, job, "succeeded")


def expected_evidence(ids):
    return [
        {
            "chunk_id": ids["chunk"],
            "material_id": ids["material"],
            "material_name": "notes.txt",
            "page_number": 1,
            "excerpt": "Source excerpt text",
        }
    ]


@pytest.mark.parametrize("count", [1, 20])
def test_question_page_projection_and_set_queries_are_bounded(content_setup, count):
    db, ids = content_setup
    create_questions(db, ids, count)
    with db.transaction() as session:
        questions = list(session.scalars(select(Question).order_by(Question.id)))
        with select_count(db.engine) as statements:
            result = question_dtos(session, questions)
        assert len(statements) == 2
        for question in questions:
            assert result[question.id] == {
                "id": question.id,
                "question_type": "short_answer",
                "concept_ids": [ids["concept"]],
                "concept_version_ids": [ids["version"]],
                "prompt": "Question?",
                "options": [],
                "difficulty": "easy",
                "evidence": expected_evidence(ids),
            }
        with pytest.raises(ApiError) as denied:
            get_grading_question(session, ids["other"], questions[0].id)
        assert denied.value.status == 404

    # Cold identity map: bounded child reads must not rely on previously cached rows.
    with db.transaction() as session:
        question_set = session.get(QuestionSet, ids["set"])
        with select_count(db.engine) as statements:
            result = set_dto(session, question_set)
        assert len(statements) <= 5
        assert len(result["items"]) == count
        assert [item["ordinal"] for item in result["items"]] == list(range(1, count + 1))
        assert result["next_ordinal"] == 1
        assert all("answer_key" not in item["question"] for item in result["items"])


def test_attempt_page_loads_only_graded_evidence_once_and_preserves_order(content_setup):
    db, ids = content_setup
    create_questions(db, ids, 20)
    with db.transaction() as session:
        question_ids = list(session.scalars(select(Question.id).order_by(Question.id)))
        now = utcnow()
        attempts = [
            SimpleNamespace(
                id=uid(),
                question_id=question_ids[index % 20],
                question_set_id=ids["set"],
                answer={"type": "short_answer", "text": "My answer"},
                status=("graded", "grading", "failed")[index % 3],
                is_correct=False,
                grading_feedback="Feedback",
                correct_answer={"type": "short_answer", "text": "Answer"},
                explanation="Reason",
                job_id=uid(),
                answered_at=now,
            )
            for index in range(100)
        ]
        with select_count(db.engine) as statements:
            result = attempt_dtos(attempts, session)
        assert len(statements) == 1
        assert [row["id"] for row in result] == [attempt.id for attempt in attempts]
        for attempt, row in zip(attempts, result, strict=True):
            assert row["answered_at"] == iso(now) and row["answer"] == attempt.answer
            if attempt.status != "graded":
                assert row["result"] is None
            else:
                assert row["result"] == {
                    "is_correct": False,
                    "feedback": "Feedback",
                    "correct_answer": attempt.correct_answer,
                    "explanation": "Reason",
                    "evidence": expected_evidence(ids),
                }
        with select_count(db.engine) as statements:
            assert attempt_dtos([], session) == [] and question_dtos(session, []) == {}
        assert not statements


def test_multiple_concept_and_evidence_links_sort_by_id_not_insertion(content_setup):
    db, ids = content_setup
    create_questions(db, ids, 1)
    # These IDs sort before the randomly generated original links but are inserted later.
    earlier_concept = "00000000-0000-4000-8000-000000000001"
    earlier_chunk = "00000000-0000-4000-8000-000000000002"
    version_id, now = uid(), utcnow()
    with db.transaction() as session:
        question = session.scalar(select(Question))
        session.add(
            Concept(id=earlier_concept, material_id=ids["material"], unit_id=ids["unit"], created_at=now)
        )
        session.add(
            MaterialChunk(
                id=earlier_chunk,
                material_id=ids["material"],
                ordinal=3,
                page_number=2,
                content="Additional evidence.",
                created_at=now,
            )
        )
        session.flush()
        session.add(
            ConceptVersion(
                id=version_id,
                concept_id=earlier_concept,
                version_no=1,
                name="Earlier ID",
                summary="Summary",
                is_confirmed=True,
                confirmed_at=now,
                created_at=now,
            )
        )
        session.flush()
        session.add(
            QuestionConcept(
                question_id=question.id, concept_id=earlier_concept, concept_version_id=version_id
            )
        )
        session.add(QuestionEvidence(question_id=question.id, chunk_id=earlier_chunk))
        session.flush()
        result = question_dtos(session, [question])[question.id]
        assert result["concept_ids"] == [earlier_concept, ids["concept"]]
        assert result["concept_version_ids"] == [version_id, ids["version"]]
        assert [row["chunk_id"] for row in result["evidence"]] == [earlier_chunk, ids["chunk"]]
        assert result["evidence"][0]["page_number"] == 2
        assert result["evidence"][0]["excerpt"] == "Additional evidence."
