"""Real signed JWT -> loopback HTTP -> canonical A/B SQL -> fenced local worker.

Only the AI provider is injected. Authentication, task gateway, route assembly,
durable jobs, vault, storage and database constraints are the real application.
"""

from collections import Counter
from pathlib import Path
import json

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select

from app.a_gateway import CanonicalTaskGateway
from app.auth import JWTAuthenticator
from app.core import uid
from app.models import Job, PlanTaskConcept, QuestionAttempt, SessionMessage, SessionReport, User
from language_learning_api.routers.exam_goals import today_seoul
from tests.ab_support import KEY, MODEL, ab_http_application


CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts/openapi-1.4.0-monthly-subscription.json").read_text(
        "utf-8"
    )
)


def validate(response, path, method="get"):
    definition = CONTRACT["paths"][path][method]["responses"].get(str(response.status_code))
    assert definition is not None, f"Undocumented {response.status_code} for {method} {path}: {response.text}"
    if "$ref" in definition:
        schema = {"$ref": "#/components/schemas/ErrorEnvelope"}
    elif "content" not in definition:
        assert not response.content
        return
    else:
        schema = definition["content"]["application/json"]["schema"]
    Draft202012Validator(
        {**schema, "components": CONTRACT["components"]}, format_checker=FormatChecker()
    ).validate(response.json())
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-ID"]


def post(fixture, path, body, *, key=None, headers=None):
    return fixture.client.post(path, json=body, headers={"Idempotency-Key": key or uid(), **(headers or {})})


def run_job(fixture, accepted):
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["data"]["job"]["id"]
    assert fixture.ctx.jobs.run_once(), "Expected the queued local job to run"
    response = fixture.client.get("/v1/jobs/" + job_id)
    assert response.status_code == 200, response.text
    validate(response, "/jobs/{jobId}")
    assert response.json()["data"]["status"] == "succeeded", response.text
    with fixture.ctx.db.transaction() as db:
        assert db.get(Job, job_id).credential_ref is None
    return response.json()["data"]


def assert_ab_http_pipeline(fixture):
    """Reusable against SQLite or the already-migrated disposable MySQL fixture.

    The caller supplies a fresh JWT-authenticated HTTP application and provider.
    Run before other fixtures leave queued jobs in a shared database.
    """
    fx, client = fixture, fixture.client
    assert isinstance(fx.ctx.auth, JWTAuthenticator)
    assert type(fx.ctx.task_gateway) is CanonicalTaskGateway
    with fx.ctx.db.transaction() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(User).where(User.auth_issuer == fx.settings.auth_issuer)
            )
            == 0
        )
    denied = client.get("/v1/me", headers={"Authorization": "Bearer invalid-local-token"})
    assert denied.status_code == 401
    validate(denied, "/me")
    me = client.get("/v1/me")
    assert me.status_code == 200, me.text
    validate(me, "/me")
    fx.owner = me.json()["data"]["id"]
    assert me.json()["data"]["timezone"] == "Asia/Seoul"
    assert client.get("/v1/me").json() == me.json()
    other = client.get("/v1/me", headers=fx.other_headers)
    assert other.status_code == 200
    fx.other = other.json()["data"]["id"]
    assert fx.other != fx.owner
    with fx.ctx.db.transaction() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(User).where(User.auth_issuer == fx.settings.auth_issuer)
            )
            == 2
        )

    source = b"Momentum is conserved when the sum of external forces is zero."
    upload_body = {"filename": "local-momentum.txt", "mime_type": "text/plain", "size_bytes": len(source)}
    upload_key = uid()
    uploaded = post(fx, "/v1/materials", upload_body, key=upload_key)
    assert uploaded.status_code == 201, uploaded.text
    validate(uploaded, "/materials", "post")
    assert post(fx, "/v1/materials", upload_body, key=upload_key).json() == uploaded.json()
    upload = uploaded.json()["data"]
    mid = upload["material"]["id"]
    stored = client.put(upload["upload_url"], content=source, headers={"Content-Type": "text/plain"})
    assert stored.status_code == 204, stored.text
    analysis = post(fx, "/v1/materials/" + mid + "/complete", {"model_id": MODEL})
    validate(analysis, "/materials/{materialId}/complete", "post")
    run_job(fx, analysis)
    material = client.get("/v1/materials/" + mid)
    validate(material, "/materials/{materialId}")
    assert material.json()["data"]["processing_status"] == "ready"
    concepts = client.get("/v1/materials/" + mid + "/concepts")
    validate(concepts, "/materials/{materialId}/concepts")
    concept = concepts.json()["data"][0]
    draft = concept["current_version"]
    confirmed = client.patch(
        "/v1/materials/" + mid + "/concepts",
        json={
            "concepts": [
                {
                    "concept_id": concept["concept_id"],
                    "unit_id": concept["unit_id"],
                    "base_version_id": draft["id"],
                    "name": draft["name"],
                    "summary": draft["summary"],
                    "checklist_items": [item["criterion"] for item in draft["checklist_items"]],
                    "evidence_chunk_ids": [item["chunk_id"] for item in draft["evidence"]],
                }
            ]
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    validate(confirmed, "/materials/{materialId}/concepts", "patch")
    version = confirmed.json()["data"][0]["version"]
    assert version["is_confirmed"] and version["id"] != draft["id"]

    goal_body = {
        "title": "Local integration exam",
        "exam_type": "other",
        "exam_date": today_seoul().isoformat(),
    }
    goal_key = uid()
    goal = post(fx, "/v1/exam-goals", goal_body, key=goal_key)
    assert goal.status_code == 201, goal.text
    validate(goal, "/exam-goals", "post")
    assert post(fx, "/v1/exam-goals", goal_body, key=goal_key).json() == goal.json()
    conflict = post(fx, "/v1/exam-goals", {**goal_body, "title": "Different"}, key=goal_key)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    gid = goal.json()["data"]["id"]
    plan_body, plan_key = {"exam_goal_id": gid, "material_ids": [mid]}, uid()
    planned = post(fx, "/v1/study-plans", plan_body, key=plan_key)
    assert planned.status_code == 201, planned.text
    validate(planned, "/study-plans", "post")
    assert post(fx, "/v1/study-plans", plan_body, key=plan_key).json() == planned.json()
    plan = planned.json()["data"]
    pid = plan["id"]
    solve, teach, review = plan["tasks"]
    assert [item["task_type"] for item in plan["tasks"]] == ["solve", "teach_back", "review"]
    assert all(item["concept_version_ids"] == [version["id"]] for item in plan["tasks"])
    today = client.get("/v1/today", params={"exam_goal_id": gid})
    validate(today, "/today")
    assert today.json()["data"]["study_plan_id"] == pid
    assert today.json()["data"]["progress"] == 0 and len(today.json()["data"]["tasks"]) == 3

    set_body = {
        "unit_id": concept["unit_id"],
        "task_id": solve["id"],
        "concept_version_ids": [version["id"]],
        "count": 5,
        "difficulty": "easy",
        "model_id": MODEL,
    }
    set_key = uid()
    generated = post(fx, "/v1/question-sets", set_body, key=set_key)
    validate(generated, "/question-sets", "post")
    assert post(fx, "/v1/question-sets", set_body, key=set_key).json() == generated.json()
    run_job(fx, generated)
    set_id = generated.json()["data"]["job"]["resource_id"]
    question_set = client.get("/v1/question-sets/" + set_id)
    validate(question_set, "/question-sets/{setId}")
    questions = question_set.json()["data"]["items"]
    assert len(questions) == 5 and question_set.json()["data"]["next_ordinal"] == 1
    assert all(item["latest_attempt"] is None for item in questions)
    tasks = client.get("/v1/study-plans/" + pid).json()["data"]["tasks"]
    assert tasks[0]["status"] == "pending" and tasks[0]["completed_count"] == 0

    attempt_ids = []
    for index, item in enumerate(questions):
        qid = item["question"]["id"]
        public_question = client.get("/v1/questions/" + qid)
        validate(public_question, "/questions/{questionId}")
        assert "answer_key" not in public_question.text and "accepted_answers" not in public_question.text
        answer_text = (
            "When total external force is zero" if index == 0 else "An intentionally wrong fixture answer"
        )
        answer = {
            "question_set_id": set_id,
            "model_id": MODEL,
            "answer": {"type": "short_answer", "text": answer_text},
        }
        attempt_key = uid()
        submitted = post(fx, "/v1/questions/" + qid + "/attempts", answer, key=attempt_key)
        validate(submitted, "/questions/{questionId}/attempts", "post")
        assert (
            post(fx, "/v1/questions/" + qid + "/attempts", answer, key=attempt_key).json() == submitted.json()
        )
        if index == 0:
            assert post(fx, "/v1/questions/" + qid + "/attempts", answer).status_code == 409
        graded_job = run_job(fx, submitted)
        aid = graded_job["resource_id"]
        attempt_ids.append(aid)
        result = client.get("/v1/attempts/" + aid)
        validate(result, "/attempts/{attemptId}")
        assert result.json()["data"]["status"] == "graded"
        assert result.json()["data"]["result"]["is_correct"] == (index == 0)
        assert result.json()["data"]["result"]["evidence"]
        resumed = client.get("/v1/question-sets/" + set_id)
        validate(resumed, "/question-sets/{setId}")
        assert resumed.json()["data"]["next_ordinal"] == (index + 2 if index < 4 else None)
        assert resumed.json()["data"]["items"][index]["latest_attempt"]["id"] == aid
    tasks = client.get("/v1/study-plans/" + pid).json()["data"]["tasks"]
    assert tasks[0]["status"] == "completed" and tasks[0]["completed_count"] == 5
    assert client.get("/v1/today").json()["data"]["progress"] == 1 / 3

    explanation_key = uid()
    explanation_body = {"concept_version_id": version["id"], "explanation_language": "ko", "model_id": MODEL}
    explanation = post(fx, "/v1/learning-explanations", explanation_body, key=explanation_key)
    validate(explanation, "/learning-explanations", "post")
    assert (
        post(fx, "/v1/learning-explanations", explanation_body, key=explanation_key).json()
        == explanation.json()
    )
    explanation_job = run_job(fx, explanation)
    eid = explanation_job["resource_id"]
    explained = client.get("/v1/learning-explanations/" + eid)
    validate(explained, "/learning-explanations/{explanationId}")
    assert explained.json()["data"]["status"] == "ready" and explained.json()["data"]["content"]["evidence"]
    assert client.get("/v1/study-plans/" + pid).json()["data"]["tasks"] == tasks

    session_body = {
        "concept_version_id": version["id"],
        "task_id": teach["id"],
        "attempt_id": attempt_ids[1],
        "student_persona": "curious_beginner",
        "explanation_language": "en",
    }
    session_key = uid()
    started = post(fx, "/v1/teachback-sessions", session_body, key=session_key)
    assert started.status_code == 201, started.text
    validate(started, "/teachback-sessions", "post")
    assert post(fx, "/v1/teachback-sessions", session_body, key=session_key).json() == started.json()
    sid = started.json()["data"]["id"]
    for number in range(3):
        message_body = {
            "content": f"Explanation {number + 1}: total momentum is fixed at zero external force.",
            "input_mode": "text",
            "model_id": MODEL,
        }
        message_key = uid()
        sent = post(fx, "/v1/teachback-sessions/" + sid + "/messages", message_body, key=message_key)
        validate(sent, "/teachback-sessions/{sessionId}/messages", "post")
        assert (
            post(fx, "/v1/teachback-sessions/" + sid + "/messages", message_body, key=message_key).json()
            == sent.json()
        )
        run_job(fx, sent)
    blocked = post(fx, "/v1/teachback-sessions/" + sid + "/messages", message_body)
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "QUESTION_LIMIT_REACHED"
    final_body = {
        "content": "A nonzero external force changes total momentum.",
        "input_mode": "voice_transcript",
    }
    final_key = uid()
    final = post(fx, "/v1/teachback-sessions/" + sid + "/final-answer", final_body, key=final_key)
    assert final.status_code == 201, final.text
    validate(final, "/teachback-sessions/{sessionId}/final-answer", "post")
    assert (
        post(fx, "/v1/teachback-sessions/" + sid + "/final-answer", final_body, key=final_key).json()
        == final.json()
    )
    report_key = uid()
    completion = post(fx, "/v1/teachback-sessions/" + sid + "/complete", {"model_id": MODEL}, key=report_key)
    validate(completion, "/teachback-sessions/{sessionId}/complete", "post")
    assert (
        post(fx, "/v1/teachback-sessions/" + sid + "/complete", {"model_id": MODEL}, key=report_key).json()
        == completion.json()
    )
    run_job(fx, completion)
    report = client.get("/v1/teachback-sessions/" + sid + "/report")
    validate(report, "/teachback-sessions/{sessionId}/report")
    report_data = report.json()["data"]
    assert report_data["coverage"] == 1 and report_data["items"][0]["evidence"] and report_data["action_id"]
    session = client.get("/v1/teachback-sessions/" + sid)
    validate(session, "/teachback-sessions/{sessionId}")
    assert session.json()["data"]["status"] == "completed" and session.json()["data"]["question_count"] == 3
    messages = client.get("/v1/teachback-sessions/" + sid + "/messages", params={"limit": 2})
    validate(messages, "/teachback-sessions/{sessionId}/messages")
    assert len(messages.json()["data"]) == 2 and messages.json()["next_cursor"]
    seen, cursor = messages.json()["data"], messages.json()["next_cursor"]
    while cursor:
        page = client.get(
            "/v1/teachback-sessions/" + sid + "/messages", params={"limit": 2, "cursor": cursor}
        )
        validate(page, "/teachback-sessions/{sessionId}/messages")
        seen.extend(page.json()["data"])
        cursor = page.json()["next_cursor"]
    assert [item["sequence_no"] for item in seen] == list(range(1, 8))
    assert client.get("/v1/today").json()["data"]["progress"] == 2 / 3

    for path in ("/records/sessions", "/records/weaknesses", "/records/actions", "/records/public-attempts"):
        response = client.get("/v1" + path)
        validate(response, path)
    weaknesses = client.get("/v1/records/weaknesses").json()["data"]
    assert len(weaknesses) == 1
    assert weaknesses[0]["incorrect_attempts"] == 4 and weaknesses[0]["teachback_count"] == 1
    assert weaknesses[0]["latest_coverage"] == 1
    assert client.get("/v1/records/sessions").json()["data"][0]["id"] == sid
    action_id = report_data["action_id"]
    assert client.get("/v1/records/actions").json()["data"][0]["id"] == action_id
    assert client.get("/v1/today").json()["data"]["next_action"] == report_data["next_action"]
    action = client.patch(
        "/v1/records/actions/" + action_id, json={"status": "completed", "followup_session_id": None}
    )
    validate(action, "/records/actions/{actionId}", "patch")
    assert client.get("/v1/today").json()["data"]["next_action"] is None
    assert client.get("/v1/teachback-sessions/" + sid + "/report").json() == report.json()
    reviewed = client.patch("/v1/plan-tasks/" + review["id"], json={"status": "completed"})
    validate(reviewed, "/plan-tasks/{taskId}", "patch")
    assert client.get("/v1/today").json()["data"]["progress"] == 1

    for path in (
        "/materials/" + mid,
        "/study-plans/" + pid,
        "/question-sets/" + set_id,
        "/questions/" + questions[0]["question"]["id"],
        "/attempts/" + attempt_ids[0],
        "/learning-explanations/" + eid,
        "/teachback-sessions/" + sid,
        "/teachback-sessions/" + sid + "/messages",
        "/teachback-sessions/" + sid + "/report",
        "/jobs/" + completion.json()["data"]["job"]["id"],
    ):
        denied = client.get("/v1" + path, headers=fx.other_headers)
        assert denied.status_code == 404, denied.text
    for path in (
        "/exam-goals",
        "/study-plans",
        "/records/sessions",
        "/records/weaknesses",
        "/records/actions",
    ):
        assert client.get("/v1" + path, headers=fx.other_headers).json()["data"] == []
    assert client.get("/v1/today", params={"exam_goal_id": gid}, headers=fx.other_headers).status_code == 404
    assert (
        client.patch(
            "/v1/records/actions/" + action_id, json={"status": "pending"}, headers=fx.other_headers
        ).status_code
        == 404
    )
    assert client.delete("/v1/materials/" + mid).status_code == 409
    assert not fx.ctx.jobs.run_once()
    assert Counter(kind for kind, _ in fx.provider.calls) == {
        "analysis": 1,
        "questions": 1,
        "grade": 5,
        "learning_explanation": 1,
        "message": 3,
        "report": 1,
    }
    assert KEY not in json.dumps(fx.provider.calls)
    with fx.ctx.db.transaction() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(QuestionAttempt).where(QuestionAttempt.user_id == fx.owner)
            )
            == 5
        )
        assert (
            db.scalar(select(func.count()).select_from(SessionReport).where(SessionReport.session_id == sid))
            == 1
        )
        assert (
            db.scalar(
                select(func.count()).select_from(SessionMessage).where(SessionMessage.session_id == sid)
            )
            == 7
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(PlanTaskConcept)
                .where(PlanTaskConcept.task_id.in_([solve["id"], teach["id"], review["id"]]))
            )
            == 3
        )
        assert all(
            job.credential_ref is None for job in db.scalars(select(Job).where(Job.user_id == fx.owner))
        )
    return {"owner": fx.owner, "material_id": mid, "plan_id": pid, "session_id": sid}


def test_complete_signed_jwt_http_pipeline(tmp_path):
    with ab_http_application(tmp_path) as fixture:
        assert_ab_http_pipeline(fixture)
