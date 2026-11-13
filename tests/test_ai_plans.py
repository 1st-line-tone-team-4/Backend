"""Study plans scheduled by the team AI server, mapped onto the public task types."""

import json
from datetime import date
from types import SimpleNamespace
import uuid

import httpx

from app import ai_plans
from app.ai_service import AiDocumentClient

TODAY = date(2026, 12, 1)
EXAM = date(2026, 12, 10)
CONCEPT, VERSION, CHUNK = (str(uuid.uuid4()) for _ in range(3))
ROWS = [(SimpleNamespace(id=CONCEPT), SimpleNamespace(id=VERSION), SimpleNamespace())]
MATERIALS = [SimpleNamespace(page_count=12)]


def context(_db, version_ids):
    assert version_ids == [VERSION]
    evidence = {"chunk_id": CHUNK, "material_id": str(uuid.uuid4()), "material_name": "m", "page_number": 1}
    return {
        "concepts": [
            {"concept_id": CONCEPT, "version": {"id": VERSION, "evidence": [{**evidence, "excerpt": "x"}]}}
        ],
        "chunks": [
            {
                "id": CHUNK,
                "material_id": evidence["material_id"],
                "material_name": "m",
                "page_number": 1,
                "text": "x",
            }
        ],
    }


def task(activity, count, concept=CONCEPT, version=VERSION):
    return {
        "concept_id": concept,
        "concept_version_id": version,
        "name": "Momentum",
        "activity": activity,
        "count": count,
        "estimated_minutes": 10,
        "source_pages": [1],
        "priority": 1.0,
    }


def plan_response(days):
    return {
        "today": TODAY.isoformat(),
        "exam_date": EXAM.isoformat(),
        "total_pages": 12,
        "concept_count": 1,
        "urgent": False,
        "days": days,
        "today_tasks": [],
        "total_minutes": 40,
        "assumptions": [],
    }


def specs(monkeypatch, respond, seen=None):
    monkeypatch.setattr(ai_plans, "study_context", context)

    def handler(request):
        if seen is not None:
            seen.append(request)
        return respond(request)

    client = AiDocumentClient(
        "http://ai.test", "server-key", transport=httpx.MockTransport(handler), profile="test"
    )
    return ai_plans.ai_plan_specs(
        SimpleNamespace(ai_documents=client), None, ROWS, MATERIALS, exam_date=EXAM, today=TODAY
    )


def test_ai_plan_maps_activities_learn_as_review_and_splits_large_counts(monkeypatch):
    seen = []
    days = [
        {
            "date": "2026-12-02",
            "tasks": [task("practice", 25), task("teachback", 1)],
            "estimated_minutes": 30,
        },
        {"date": "2026-12-01", "tasks": [task("learn", 3)], "estimated_minutes": 10},
        {"date": "2026-12-03", "tasks": [task("review", 3)], "estimated_minutes": 10},
    ]
    result = specs(monkeypatch, lambda r: httpx.Response(200, json=plan_response(days)), seen)
    assert [(due.isoformat(), kind, target) for due, kind, target, _c, _v in result] == [
        ("2026-12-01", "review", 3),
        ("2026-12-02", "solve", 20),
        ("2026-12-02", "solve", 5),
        ("2026-12-02", "teach_back", 1),
        ("2026-12-03", "review", 3),
    ]
    request = seen[0]
    assert request.url.path == "/internal/study-plan/generate"
    assert request.headers["X-AI-API-Key"] == "server-key" and "X-AI-Key" not in request.headers
    body = json.loads(request.content)
    assert body["total_pages"] == 12 and body["exam_date"] == "2026-12-10" and body["today"] == "2026-12-01"
    assert "model_id" not in body


def test_unexpected_ai_plan_falls_back_to_local_scheduler(monkeypatch):
    stranger = task("practice", 2, concept=str(uuid.uuid4()))
    outside = [{"date": "2026-12-02", "tasks": [stranger], "estimated_minutes": 5}]
    assert specs(monkeypatch, lambda r: httpx.Response(200, json=plan_response(outside))) is None
    late = [{"date": "2026-12-11", "tasks": [task("practice", 2)], "estimated_minutes": 5}]
    assert specs(monkeypatch, lambda r: httpx.Response(200, json=plan_response(late))) is None
    unknown = [{"date": "2026-12-02", "tasks": [task("rest", 2)], "estimated_minutes": 5}]
    assert specs(monkeypatch, lambda r: httpx.Response(200, json=plan_response(unknown))) is None
    assert specs(monkeypatch, lambda r: httpx.Response(503, json={})) is None

    def down(request):
        raise httpx.ConnectError("down")

    assert specs(monkeypatch, down) is None


def test_no_ai_server_uses_local_scheduler():
    assert (
        ai_plans.ai_plan_specs(
            SimpleNamespace(ai_documents=None), None, ROWS, MATERIALS, exam_date=EXAM, today=TODAY
        )
        is None
    )
