import pytest
from sqlalchemy import select

from app.core import ApiError, uid, utcnow
from app.db import Database
from app.models import (
    User,
    Material,
    MaterialUnit,
    MaterialChunk,
    Concept,
    ConceptVersion,
    ConceptVersionEvidence,
    Job,
    QuestionSet,
    QuestionSetConcept,
    Question,
    LearningExplanation,
)
from app.content import QuestionsHandler, ExplanationHandler, get_grading_question, set_dto, explanation_dto


@pytest.fixture
def setup():
    db = Database("sqlite:///:memory:")
    db.create_schema()
    ids = {
        k: uid()
        for k in (
            "user",
            "other",
            "material",
            "unit",
            "chunk",
            "foreign_chunk",
            "concept",
            "version",
            "set",
            "job",
            "explanation",
            "ex_job",
        )
    }
    now = utcnow()
    with db.transaction() as s:
        for key in ("user", "other"):
            s.add(
                User(id=ids[key], auth_issuer="test", auth_subject=key, timezone="Asia/Seoul", created_at=now)
            )
        s.flush()
        s.add(
            Material(
                id=ids["material"],
                user_id=ids["user"],
                filename="notes.txt",
                mime_type="text/plain",
                size_bytes=10,
                storage_key="key",
                processing_status="ready",
                archived=False,
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()
        s.add(MaterialUnit(id=ids["unit"], material_id=ids["material"], ordinal=1, title="Unit"))
        for i, key in enumerate(("chunk", "foreign_chunk"), 1):
            s.add(
                MaterialChunk(
                    id=ids[key],
                    material_id=ids["material"],
                    ordinal=i,
                    page_number=1,
                    content="Source excerpt text",
                    created_at=now,
                )
            )
        s.flush()
        s.add(Concept(id=ids["concept"], material_id=ids["material"], unit_id=ids["unit"], created_at=now))
        s.flush()
        s.add(
            ConceptVersion(
                id=ids["version"],
                concept_id=ids["concept"],
                version_no=1,
                name="Concept",
                summary="Summary",
                is_confirmed=True,
                confirmed_at=now,
                created_at=now,
            )
        )
        s.flush()
        s.add(ConceptVersionEvidence(concept_version_id=ids["version"], chunk_id=ids["chunk"]))
        s.flush()
        for key, kind, resource, target in [
            ("job", "questions", "question_set", "set"),
            ("ex_job", "learning_explanation", "learning_explanation", "explanation"),
        ]:
            s.add(
                Job(
                    id=ids[key],
                    user_id=ids["user"],
                    kind=kind,
                    status="running",
                    resource_type=resource,
                    resource_id=ids[target],
                    request_fingerprint=key,
                    request_payload={},
                    model_id="model-a",
                    attempt_no=1,
                    retryable=False,
                    fencing_token=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        s.flush()
        s.add(
            QuestionSet(
                id=ids["set"],
                user_id=ids["user"],
                unit_id=ids["unit"],
                difficulty="easy",
                requested_count=1,
                status="generating",
                job_id=ids["job"],
                created_at=now,
            )
        )
        s.add(
            LearningExplanation(
                id=ids["explanation"],
                user_id=ids["user"],
                concept_id=ids["concept"],
                concept_version_id=ids["version"],
                material_id=ids["material"],
                explanation_language="ko",
                status="generating",
                latest_job_id=ids["ex_job"],
                created_at=now,
            )
        )
        s.flush()
        s.add(
            QuestionSetConcept(
                set_id=ids["set"], concept_id=ids["concept"], concept_version_id=ids["version"]
            )
        )
    return db, ids


def question_output(ids):
    return {
        "questions": [
            {
                "question_type": "short_answer",
                "concept_version_ids": [ids["version"]],
                "prompt": "Question?",
                "options": [],
                "answer_key": {"type": "short_answer", "text": "Answer"},
                "public_explanation": "Reason",
                "accepted_answers": ["Answer"],
                "evidence_chunk_ids": [ids["chunk"]],
            }
        ]
    }


def explanation_output(ids):
    return {
        "title": "Title",
        "explanation_markdown": f"A claim [근거: {ids['chunk']}]",
        "evidence": [{"chunk_id": ids["chunk"], "excerpt": "Source excerpt"}],
    }


def test_question_output_validation_and_private_grading(setup):
    db, ids = setup
    with db.transaction() as s:
        job = s.get(Job, ids["job"])
        handler = QuestionsHandler(None)
        bad = question_output(ids)
        bad["questions"][0]["evidence_chunk_ids"] = [ids["foreign_chunk"]]
        with pytest.raises(ApiError, match="Generated output"):
            handler.persist(s, job, bad)
        assert not list(s.scalars(select(Question)))
        handler.persist(s, job, question_output(ids))
        handler.set_state(s, job, "succeeded")
        dto = set_dto(s, s.get(QuestionSet, ids["set"]))
        q = dto["items"][0]["question"]
        assert "answer_key" not in q and "public_explanation" not in q
        assert get_grading_question(s, ids["user"], q["id"])["answer_key"]["text"] == "Answer"
        with pytest.raises(ApiError) as exc:
            get_grading_question(s, ids["other"], q["id"])
        assert exc.value.status == 404


def test_invalid_question_shape_is_atomic(setup):
    db, ids = setup
    with db.transaction() as s:
        data = question_output(ids)
        data["questions"][0]["answer_key"] = {"type": "multiple_choice", "selected_option_ids": ["x"]}
        with pytest.raises(ApiError):
            QuestionsHandler(None).persist(s, s.get(Job, ids["job"]), data)
        assert not list(s.scalars(select(Question)))


def test_explanation_requires_source_and_citation(setup):
    db, ids = setup
    with db.transaction() as s:
        job = s.get(Job, ids["ex_job"])
        handler = ExplanationHandler(None)
        data = explanation_output(ids)
        data["evidence"][0]["chunk_id"] = ids["foreign_chunk"]
        with pytest.raises(ApiError):
            handler.persist(s, job, data)
        data = explanation_output(ids)
        data["explanation_markdown"] = "No source citation"
        with pytest.raises(ApiError):
            handler.persist(s, job, data)
        data = explanation_output(ids)
        data["evidence"][0]["excerpt"] = "Fabricated"
        with pytest.raises(ApiError):
            handler.persist(s, job, data)
        handler.persist(s, job, explanation_output(ids))
        dto = explanation_dto(s, s.get(LearningExplanation, ids["explanation"]))
        assert dto["model_id"] == "model-a" and dto["content"]["evidence"][0]["excerpt"] == "Source excerpt"


def test_pinned_history_survives_new_current_version(setup):
    db, ids = setup
    with db.transaction() as s:
        now = utcnow()
        s.add(
            ConceptVersion(
                id=uid(),
                concept_id=ids["concept"],
                version_no=2,
                name="New",
                summary="New summary",
                is_confirmed=True,
                confirmed_at=now,
                created_at=now,
            )
        )
        s.flush()
        handler = ExplanationHandler(None)
        handler.persist(s, s.get(Job, ids["ex_job"]), explanation_output(ids))
        result = explanation_dto(s, s.get(LearningExplanation, ids["explanation"]))
        assert result["concept_version_id"] == ids["version"]
        assert result["content"]["evidence"][0]["chunk_id"] == ids["chunk"]


def test_attempt_adapter_drives_latest_and_next_ordinal(setup):
    db, ids = setup
    with db.transaction() as s:
        handler = QuestionsHandler(None)
        job = s.get(Job, ids["job"])
        handler.persist(s, job, question_output(ids))
        handler.set_state(s, job, "succeeded")

        class Gateway:
            def read_question_attempts(self, session, user_id, set_id, question_ids):
                assert user_id == ids["user"] and set_id == ids["set"]
                return {question_ids[0]: {"latest_attempt": {"status": "failed"}, "has_graded_attempt": True}}

        dto = set_dto(s, s.get(QuestionSet, ids["set"]), Gateway())
        assert dto["items"][0]["latest_attempt"]["status"] == "failed"
        assert dto["next_ordinal"] is None


def test_cursor_signatures_and_scope(setup):
    import base64
    from app.content import _page, _cursor_decode

    db, ids = setup
    scope = {"user": ids["user"], "resource": "question_sets", "task_id": None}
    with db.transaction() as s:
        # Page one contains an existing row; a synthetic second row forces a cursor.
        second_id, second_job = uid(), uid()
        now = utcnow()
        s.add(
            Job(
                id=second_job,
                user_id=ids["user"],
                kind="questions",
                status="failed",
                resource_type="question_set",
                resource_id=second_id,
                request_fingerprint="another",
                request_payload={},
                model_id="m",
                attempt_no=1,
                retryable=False,
                fencing_token=0,
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()
        s.add(
            QuestionSet(
                id=second_id,
                user_id=ids["user"],
                unit_id=ids["unit"],
                difficulty="easy",
                requested_count=1,
                status="failed",
                job_id=second_job,
                created_at=now,
            )
        )
        s.flush()
        stmt = (
            select(QuestionSet)
            .where(QuestionSet.user_id == ids["user"])
            .order_by(QuestionSet.created_at.desc(), QuestionSet.id.desc())
        )
        _, cursor = _page(s, stmt, QuestionSet, 1, None, "secret", scope)
        assert cursor and _cursor_decode(cursor, "secret", scope)
        tampered = cursor[:-2] + ("AA" if cursor[-2:] != "AA" else "BB")
        unsigned = base64.urlsafe_b64encode(b'["2026-01-01T00:00:00","bad"]').decode()
        for token, check_scope in [
            (tampered, scope),
            (unsigned, scope),
            (cursor, {**scope, "task_id": uid()}),
            (cursor, {**scope, "user": ids["other"]}),
        ]:
            with pytest.raises(ApiError) as exc:
                _cursor_decode(token, "secret", check_scope)
            assert exc.value.status == 422
