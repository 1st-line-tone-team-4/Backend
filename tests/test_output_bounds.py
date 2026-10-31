from types import SimpleNamespace

import pytest

from app.content import ExplanationHandler, QuestionsHandler
from app.core import ApiError
from app.materials import AnalysisHandler
from app.models import Job, MaterialChunk
from tests.test_content import setup as setup, question_output
from tests.test_materials import env as env


@pytest.mark.parametrize("field,limit", [("title", 200), ("explanation_markdown", 24000), ("excerpt", 2000)])
def test_explanation_rejects_raw_oversize_even_when_trimmed_value_fits(setup, field, limit):
    db, ids = setup
    value = "x" + " " * limit
    output = {
        "title": "Title",
        "explanation_markdown": f"Text [근거: {ids['chunk']}]",
        "evidence": [{"chunk_id": ids["chunk"], "excerpt": "Source"}],
    }
    if field == "excerpt":
        # Keep the padded excerpt grounded, isolating the raw length violation.
        with db.transaction() as s:
            s.get(MaterialChunk, ids["chunk"]).content = value
        output["evidence"][0]["excerpt"] = value
    elif field == "explanation_markdown":
        output[field] = output[field] + " " * limit
    else:
        output[field] = value
    with pytest.raises(ApiError) as error:
        with db.transaction() as s:
            ExplanationHandler(SimpleNamespace()).persist(s, s.get(Job, ids["ex_job"]), output)
    assert error.value.code == "INVALID_OUTPUT"


@pytest.mark.parametrize("field", ["prompt", "public_explanation", "answer_key"])
def test_question_rejects_padded_raw_oversize(setup, field):
    db, ids = setup
    output = question_output(ids)
    if field == "answer_key":
        output["questions"][0][field]["text"] = "x" + " " * 12000
    else:
        output["questions"][0][field] = "x" + " " * 12000
    with pytest.raises(ApiError) as error:
        with db.transaction() as s:
            QuestionsHandler(SimpleNamespace()).persist(s, s.get(Job, ids["job"]), output)
    assert error.value.code == "INVALID_OUTPUT"


@pytest.mark.parametrize(
    "field,limit", [("title", 200), ("name", 200), ("summary", 12000), ("checklist", 2000)]
)
def test_analysis_rejects_padded_raw_oversize(env, field, limit):
    ctx, _, _, _, job_id = env
    handler = AnalysisHandler(ctx)
    with ctx.db.transaction() as s:
        chunk = handler.prepare(s, s.get(Job, job_id))["chunks"][0]
    concept = {
        "name": "Concept",
        "summary": "Summary",
        "checklist_items": ["Explain"],
        "evidence": [{"chunk_id": chunk["id"], "quote": "hello"}],
    }
    unit = {"title": "Unit", "concepts": [concept]}
    if field == "title":
        unit[field] = "x" + " " * limit
    elif field == "checklist":
        concept["checklist_items"] = ["x" + " " * limit]
    else:
        concept[field] = "x" + " " * limit
    with pytest.raises(ApiError) as error:
        with ctx.db.transaction() as s:
            handler.persist(s, s.get(Job, job_id), {"units": [unit]})
    assert error.value.code == "INVALID_OUTPUT"
