"""Canonical A/B teach-back HTTP -> credential vault -> fenced worker -> normalized SQL."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select, update

from app.core import ApiError, uid, utcnow
from app.models import (
    Concept,
    ConceptChecklistItem,
    ConceptVersion,
    ConceptVersionEvidence,
    ExamGoal,
    ImprovementAction,
    Job,
    Material,
    MaterialChunk,
    MaterialUnit,
    PlanTask,
    PlanTaskConcept,
    Publication,
    PublicAttempt,
    Question,
    ReportEvidence,
    ReportItem,
    SessionMessage,
    SessionReport,
    StudyPlan,
    TeachbackSession,
)
from app.providers import ProviderFailure
from tests.support import test_application as make_application

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts/openapi-1.3.0.json").read_text(encoding="utf-8")
)
MODEL = "test:grounded"
KEY = "test-transient-key"
OTHER = {"Authorization": "Bearer test-other-token"}


def validate(response, path, method="get"):
    definition = CONTRACT["paths"][path][method]["responses"][str(response.status_code)]
    schema = (
        {"$ref": "#/components/schemas/ErrorEnvelope"}
        if "$ref" in definition
        else definition["content"]["application/json"]["schema"]
    )
    Draft202012Validator(
        {**schema, "components": CONTRACT["components"]}, format_checker=FormatChecker()
    ).validate(response.json())


@pytest.fixture
def env(tmp_path):
    env = make_application(tmp_path)
    env.ids = {
        key: uid()
        for key in ("material", "unit", "chunk", "concept", "version", "item", "goal", "plan", "task")
    }
    ids, now = env.ids, utcnow()
    owner = env.auth.tokens["test-owner-token"]
    with env.ctx.db.transaction() as db:
        db.add(
            Material(
                id=ids["material"],
                user_id=owner,
                filename="lecture.txt",
                mime_type="text/plain",
                size_bytes=50,
                storage_key="test-only",
                processing_status="ready",
                archived=False,
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(MaterialUnit(id=ids["unit"], material_id=ids["material"], ordinal=1, title="Momentum"))
        db.add(
            MaterialChunk(
                id=ids["chunk"],
                material_id=ids["material"],
                ordinal=1,
                page_number=1,
                content="Momentum is conserved when external force is zero.",
                created_at=now,
            )
        )
        db.flush()
        db.add(Concept(id=ids["concept"], unit_id=ids["unit"], material_id=ids["material"], created_at=now))
        db.flush()
        db.add(
            ConceptVersion(
                id=ids["version"],
                concept_id=ids["concept"],
                version_no=1,
                name="Momentum",
                summary="External force determines total momentum.",
                is_confirmed=True,
                confirmed_at=now,
                created_at=now,
            )
        )
        db.flush()
        db.add(
            ConceptChecklistItem(
                id=ids["item"],
                concept_version_id=ids["version"],
                ordinal=1,
                criterion="State the zero force condition.",
            )
        )
        db.add(
            ConceptVersionEvidence(
                concept_version_id=ids["version"], chunk_id=ids["chunk"], quote="external force is zero"
            )
        )
        db.add(
            ExamGoal(
                id=ids["goal"],
                user_id=owner,
                title="Exam",
                exam_type="other",
                exam_date=now.date() + timedelta(days=3),
                status="active",
            )
        )
        db.flush()
        db.add(
            StudyPlan(id=ids["plan"], user_id=owner, exam_goal_id=ids["goal"], revision=1, status="active")
        )
        db.flush()
        db.add(
            PlanTask(
                id=ids["task"],
                study_plan_id=ids["plan"],
                ordinal=1,
                due_date=now.date(),
                task_type="teach_back",
                target_count=1,
                status="pending",
            )
        )
        db.flush()
        db.add(
            PlanTaskConcept(task_id=ids["task"], concept_id=ids["concept"], concept_version_id=ids["version"])
        )
    env.outputs = []
    env.calls = []
    original_generate = env.provider.generate

    def generate(model_id, key, kind, payload):
        assert model_id == MODEL and key == KEY
        if kind not in {"message", "report"}:
            return original_generate(model_id, key, kind, payload)
        env.calls.append((kind, deepcopy(payload)))
        if env.outputs:
            output = env.outputs.pop(0)
            if isinstance(output, Exception):
                raise output
            return output
        if kind == "message":
            return {"assistant_content": "Why must external force be zero?"}
        assert kind == "report"
        return report_output(env)

    env.provider.generate = generate
    env.client = TestClient(env.app, headers={"Authorization": "Bearer test-owner-token"})
    yield env
    env.client.close()
    env.ctx.db.engine.dispose()


def report_output(env, *, status="explained"):
    return {
        "items": [
            {
                "checklist_item_id": env.ids["item"],
                "status": status,
                "description": "The force condition was stated.",
                "confidence": "high",
                "evidence": []
                if status == "unverified"
                else [{"chunk_id": env.ids["chunk"], "excerpt": "external force is zero"}],
            }
        ],
        "next_action": "Explain an example with a nonzero external force.",
    }


def post(env, path, body, *, idem=None, headers=None):
    return env.client.post(
        path, json=body, headers={"Idempotency-Key": idem or uid(), "X-AI-Key": KEY, **(headers or {})}
    )


def create(env, *, task=True, **extra):
    body = {
        "concept_version_id": env.ids["version"],
        "student_persona": "curious_beginner",
        "explanation_language": "en",
        **extra,
    }
    if task:
        body["task_id"] = env.ids["task"]
    response = post(env, "/v1/teachback-sessions", body)
    assert response.status_code == 201, response.text
    validate(response, "/teachback-sessions", "post")
    return response.json()["data"]["id"]


def send(env, sid, *, content="Momentum stays fixed when external force is zero.", idem=None):
    response = post(
        env,
        f"/v1/teachback-sessions/{sid}/messages",
        {"content": content, "input_mode": "text", "model_id": MODEL},
        idem=idem,
    )
    assert response.status_code == 202, response.text
    validate(response, "/teachback-sessions/{sessionId}/messages", "post")
    return response.json()["data"]["job"]["id"]


def job(env, jid):
    with env.ctx.db.transaction() as db:
        row = db.get(Job, jid)
        return {
            name: getattr(row, name)
            for name in (
                "status",
                "error_code",
                "retryable",
                "credential_ref",
                "attempt_no",
                "previous_job_id",
            )
        }


def complete(env, sid):
    response = post(env, f"/v1/teachback-sessions/{sid}/complete", {"model_id": MODEL})
    assert response.status_code == 202, response.text
    validate(response, "/teachback-sessions/{sessionId}/complete", "post")
    return response.json()["data"]["job"]["id"]


def test_three_questions_final_report_feedback_and_once_only_progress(env):
    sid = create(env)
    assert (
        post(
            env,
            "/v1/teachback-sessions",
            {
                "concept_version_id": env.ids["version"],
                "task_id": env.ids["task"],
                "student_persona": "curious_beginner",
                "explanation_language": "en",
            },
        ).status_code
        == 409
    )
    assert post(env, f"/v1/teachback-sessions/{sid}/complete", {"model_id": MODEL}).status_code == 422
    for _ in range(3):
        jid = send(env, sid)
        assert env.ctx.jobs.run_once()
        assert job(env, jid)["status"] == "succeeded"
        assert job(env, jid)["credential_ref"] is None
    blocked = post(
        env,
        f"/v1/teachback-sessions/{sid}/messages",
        {"content": "Again", "input_mode": "text", "model_id": MODEL},
    )
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "QUESTION_LIMIT_REACHED"
    final_body = {"content": "The force changes total momentum.", "input_mode": "voice_transcript"}
    idem = uid()
    final = post(env, f"/v1/teachback-sessions/{sid}/final-answer", final_body, idem=idem)
    assert final.status_code == 201
    validate(final, "/teachback-sessions/{sessionId}/final-answer", "post")
    assert (
        post(env, f"/v1/teachback-sessions/{sid}/final-answer", final_body, idem=idem).json() == final.json()
    )
    assert post(env, f"/v1/teachback-sessions/{sid}/final-answer", final_body).status_code == 409
    jid = complete(env, sid)
    assert complete(env, sid) == jid
    assert env.ctx.jobs.run_once()
    assert job(env, jid)["status"] == "succeeded"
    report = env.client.get(f"/v1/teachback-sessions/{sid}/report")
    validate(report, "/teachback-sessions/{sessionId}/report")
    data = report.json()["data"]
    assert data["coverage"] == 1 and len(data["items"]) == 1 and data["action_id"]
    feedback = post(
        env,
        f"/v1/teachback-sessions/{sid}/feedback",
        {"report_item_id": data["items"][0]["id"], "reason": "incorrect", "comment": "Please review."},
    )
    assert feedback.status_code == 201
    validate(feedback, "/teachback-sessions/{sessionId}/feedback", "post")
    assert env.client.get(f"/v1/teachback-sessions/{sid}/report").json() == report.json()
    update = env.client.patch(
        f"/v1/records/actions/{data['action_id']}", json={"status": "completed", "followup_session_id": None}
    )
    assert update.status_code == 200
    validate(update, "/records/actions/{actionId}", "patch")
    assert update.json()["data"]["text"] == data["next_action"]
    assert not env.ctx.jobs.run_once()
    with env.ctx.db.transaction() as db:
        assert db.get(PlanTask, env.ids["task"]).status == "completed"
        assert db.scalar(select(func.count(SessionReport.id))) == 1
        assert db.scalar(select(func.count(ReportItem.id))) == 1
        assert db.scalar(select(func.count(ReportEvidence.chunk_id))) == 1
        assert db.scalar(select(func.count(ImprovementAction.id))) == 1
        assert db.scalar(select(func.count(SessionMessage.id))) == 7
    assert len(env.calls) == 4
    assert KEY not in json.dumps(env.calls)


def test_retry_reuses_message_then_unknown_requires_explicit_restart(env):
    sid = create(env, task=False)
    jid = send(env, sid)
    env.outputs = [ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True)]
    env.ctx.jobs.run_once()
    assert job(env, jid)["retryable"]
    response = post(env, f"/v1/jobs/{jid}/retry", {"model_id": MODEL})
    assert response.status_code == 202, response.text
    env.outputs = [ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)]
    env.ctx.jobs.run_once()
    assert job(env, jid)["status"] == "outcome_unknown"
    assert post(env, f"/v1/jobs/{jid}/retry", {"model_id": MODEL}).status_code == 409
    assert (
        post(
            env, f"/v1/jobs/{jid}/restart", {"model_id": MODEL, "acknowledge_possible_duplicate_cost": False}
        ).status_code
        == 422
    )
    restarted = post(
        env, f"/v1/jobs/{jid}/restart", {"model_id": MODEL, "acknowledge_possible_duplicate_cost": True}
    )
    assert restarted.status_code == 202, restarted.text
    successor = restarted.json()["data"]["job"]["id"]
    env.ctx.jobs.run_once()
    assert job(env, successor)["status"] == "succeeded"
    assert job(env, successor)["previous_job_id"] == jid
    with env.ctx.db.transaction() as db:
        assert db.scalar(select(func.count(SessionMessage.id)).where(SessionMessage.session_id == sid)) == 2
        assert db.get(TeachbackSession, sid).question_count == 1


def test_invalid_evidence_atomic_failure_and_report_restart(env):
    sid = create(env)
    send(env, sid)
    env.ctx.jobs.run_once()
    invalid = report_output(env)
    invalid["items"][0]["evidence"][0]["excerpt"] = "fabricated source"
    jid = complete(env, sid)
    env.outputs = [invalid]
    env.ctx.jobs.run_once()
    assert job(env, jid)["status"] == "failed" and job(env, jid)["error_code"] == "INVALID_OUTPUT"
    with env.ctx.db.transaction() as db:
        assert db.scalar(select(func.count(SessionReport.id))) == 0
        assert db.get(TeachbackSession, sid).status == "report_pending"
        assert db.get(PlanTask, env.ids["task"]).status == "pending"
    restarted = post(
        env, f"/v1/jobs/{jid}/restart", {"model_id": MODEL, "acknowledge_possible_duplicate_cost": True}
    )
    assert restarted.status_code == 202, restarted.text
    env.ctx.jobs.run_once()
    assert env.client.get(f"/v1/teachback-sessions/{sid}/report").status_code == 200
    assert (
        post(
            env, f"/v1/jobs/{jid}/restart", {"model_id": MODEL, "acknowledge_possible_duplicate_cost": True}
        ).status_code
        == 409
    )


def test_failed_new_messages_preserve_full_history_and_fence_old_job(env):
    sid = create(env, task=False)
    old = None
    for index in range(5):
        old = send(env, sid, content=f"Attempted explanation {index}")
        env.outputs = [ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True)]
        env.ctx.jobs.run_once()
    send(env, sid)
    env.ctx.jobs.run_once()
    assert post(env, f"/v1/jobs/{old}/retry", {"model_id": MODEL}).status_code == 409
    send(env, sid)
    env.ctx.jobs.run_once()
    complete(env, sid)
    env.ctx.jobs.run_once()
    assert env.client.get(f"/v1/teachback-sessions/{sid}/report").status_code == 200
    assert len(env.calls[-1][1]["conversation"]) == 9


def test_pinned_old_version_owner_bound_sequence_cursors_and_abandon(env):
    with env.ctx.db.transaction() as db:
        db.add(
            ConceptVersion(
                id=uid(),
                concept_id=env.ids["concept"],
                version_no=2,
                name="New name",
                summary="New summary",
                is_confirmed=True,
                confirmed_at=utcnow(),
                created_at=utcnow(),
            )
        )
    sid = create(env)
    send(env, sid)
    env.ctx.jobs.run_once()
    with env.ctx.db.transaction() as db:
        for message in db.scalars(select(SessionMessage).where(SessionMessage.session_id == sid)):
            message.created_at = utcnow().replace(microsecond=0)
    first = env.client.get(f"/v1/teachback-sessions/{sid}/messages?limit=1")
    assert first.json()["data"][0]["sequence_no"] == 1
    cursor = first.json()["next_cursor"]
    second = env.client.get(f"/v1/teachback-sessions/{sid}/messages", params={"limit": 1, "cursor": cursor})
    assert second.json()["data"][0]["sequence_no"] == 2
    validate(second, "/teachback-sessions/{sessionId}/messages")
    assert env.client.get(f"/v1/teachback-sessions/{sid}/messages", headers=OTHER).status_code == 404
    other_sid = create(env, task=False)
    assert (
        env.client.get(f"/v1/teachback-sessions/{other_sid}/messages", params={"cursor": cursor}).status_code
        == 422
    )
    assert env.client.get("/v1/records/sessions", params={"cursor": cursor}).status_code == 422
    abandoned = env.client.patch(f"/v1/teachback-sessions/{sid}", json={"status": "abandoned"})
    assert abandoned.status_code == 200
    assert (
        post(
            env,
            f"/v1/teachback-sessions/{sid}/messages",
            {"content": "No", "input_mode": "text", "model_id": MODEL},
        ).status_code
        == 409
    )
    assert env.calls[0][1]["concept"]["version_id"] == env.ids["version"]


def test_latest_unverified_report_does_not_reuse_older_coverage_for_trend(env):
    for status in ("partial", "explained", "unverified"):
        sid = create(env, task=False)
        send(env, sid)
        env.ctx.jobs.run_once()
        complete(env, sid)
        env.outputs = [report_output(env, status=status)]
        env.ctx.jobs.run_once()
    response = env.client.get("/v1/records/weaknesses")
    assert response.status_code == 200, response.text
    validate(response, "/records/weaknesses")
    assert response.json()["data"][0]["latest_coverage"] is None
    assert response.json()["data"][0]["trend"] == "unknown"
    assert response.json()["data"][0]["teachback_count"] == 3
    for path in ("sessions", "actions", "weaknesses", "public-attempts"):
        own = env.client.get("/v1/records/" + path)
        validate(own, "/records/" + path)
        assert env.client.get("/v1/records/" + path, headers=OTHER).json()["data"] == []


def test_uploaded_analyzed_confirmed_api_version_without_quote_works(env):
    from tests.test_e2e import confirmed

    material_id, concept, version = confirmed(env)
    env.ids.update(
        material=material_id,
        concept=concept["concept_id"],
        version=version["id"],
        item=version["checklist_items"][0]["id"],
        chunk=version["evidence"][0]["chunk_id"],
    )
    with env.ctx.db.transaction() as db:
        assert db.get(ConceptVersionEvidence, (env.ids["version"], env.ids["chunk"])).quote is None
    sid = create(env, task=False)
    send(env, sid)
    env.ctx.jobs.run_once()
    jid = complete(env, sid)
    output = report_output(env)
    output["items"][0]["evidence"][0]["excerpt"] = "external forces is zero"
    env.outputs = [output]
    env.ctx.jobs.run_once()
    assert job(env, jid)["status"] == "succeeded"
    response = env.client.get(f"/v1/teachback-sessions/{sid}/report")
    validate(response, "/teachback-sessions/{sessionId}/report")
    assert response.json()["data"]["items"][0]["evidence"][0]["material_id"] == material_id


def test_credential_ttl_no_call_and_abandon_fences_retry(env):
    sid = create(env, task=False)
    jid = send(env, sid)
    ref = job(env, jid)["credential_ref"]
    with env.ctx.credentials._connect() as vault:
        vault.execute("UPDATE credentials SET expires_at=0 WHERE ref=?", (ref,))
    env.ctx.jobs.run_once()
    assert env.calls == []
    assert job(env, jid)["error_code"] == "CREDENTIAL_EXPIRED"
    assert job(env, jid)["retryable"]
    assert env.ctx.credentials.get(ref) is None
    assert env.client.patch(f"/v1/teachback-sessions/{sid}", json={"status": "abandoned"}).status_code == 200
    response = post(env, f"/v1/jobs/{jid}/retry", {"model_id": MODEL})
    assert response.status_code == 409 and response.json()["error"]["code"] == "STALE_JOB"


def test_paid_report_can_finish_after_goal_pause_without_progress(env):
    sid = create(env)
    send(env, sid)
    env.ctx.jobs.run_once()
    jid = complete(env, sid)
    original = env.provider.generate

    def pause_during_call(model, key, kind, payload):
        with env.ctx.db.transaction() as db:
            db.get(ExamGoal, env.ids["goal"]).status = "paused"
        return original(model, key, kind, payload)

    env.provider.generate = pause_during_call
    env.ctx.jobs.run_once()
    assert job(env, jid)["status"] == "succeeded"
    with env.ctx.db.transaction() as db:
        assert db.get(TeachbackSession, sid).status == "completed"
        assert db.get(PlanTask, env.ids["task"]).status == "pending"
    denied = post(
        env,
        "/v1/teachback-sessions",
        {
            "concept_version_id": env.ids["version"],
            "task_id": env.ids["task"],
            "student_persona": "curious_beginner",
            "explanation_language": "en",
        },
    )
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "STALE_TASK"


def test_credential_in_message_rejected_and_echoed_output_not_saved(env):
    sid = create(env, task=False)
    response = post(
        env,
        f"/v1/teachback-sessions/{sid}/messages",
        {"content": "my key " + KEY, "input_mode": "text", "model_id": MODEL},
    )
    assert response.status_code == 422
    with env.ctx.db.transaction() as db:
        assert db.scalar(select(func.count(SessionMessage.id))) == 0
    jid = send(env, sid)
    env.outputs = [{"assistant_content": "Echo " + KEY}]
    env.ctx.jobs.run_once()
    assert job(env, jid)["error_code"] == "INVALID_OUTPUT"
    with env.ctx.db.transaction() as db:
        assert db.get(TeachbackSession, sid).question_count == 0
        assert all(KEY not in row.content for row in db.scalars(select(SessionMessage)))
        assert all(KEY not in json.dumps(row.request_payload) for row in db.scalars(select(Job)))


def test_followup_action_owner_concept_time_and_records_cursor_binding(env):
    prior = create(env, task=False)
    sid = create(env, task=False)
    send(env, sid)
    env.ctx.jobs.run_once()
    complete(env, sid)
    env.ctx.jobs.run_once()
    data = env.client.get(f"/v1/teachback-sessions/{sid}/report").json()["data"]
    action_url = "/v1/records/actions/" + data["action_id"]
    assert (
        env.client.patch(action_url, json={"status": "attempted", "followup_session_id": prior}).status_code
        == 422
    )
    followup = create(env, task=False, previous_session_id=sid)
    response = env.client.patch(action_url, json={"status": "attempted", "followup_session_id": followup})
    assert response.status_code == 200
    assert (
        env.client.patch(
            action_url, json={"status": "completed", "followup_session_id": None}, headers=OTHER
        ).status_code
        == 404
    )
    page = env.client.get("/v1/records/sessions?limit=1").json()
    cursor = page["next_cursor"]
    assert cursor
    assert env.client.get("/v1/records/sessions", params={"cursor": cursor}, headers=OTHER).status_code == 422
    assert env.client.get("/v1/records/actions", params={"cursor": cursor}).status_code == 422


@pytest.mark.parametrize("count,status", [(32, "explained"), (16, "partial")])
def test_coverage_uses_canonical_sql_rounding(count, status, env):
    items = [env.ids["item"]]
    with env.ctx.db.transaction() as db:
        for index in range(2, count + 1):
            item_id = uid()
            items.append(item_id)
            db.add(
                ConceptChecklistItem(
                    id=item_id,
                    concept_version_id=env.ids["version"],
                    ordinal=index,
                    criterion=f"Assessment criterion {index}",
                )
            )
    sid = create(env, task=False)
    send(env, sid)
    env.ctx.jobs.run_once()
    jid = complete(env, sid)
    output = report_output(env, status=status)
    output["items"].extend(
        {
            "checklist_item_id": item,
            "status": "omission",
            "description": "Not discussed.",
            "confidence": "high",
            "evidence": [],
        }
        for item in items[1:]
    )
    env.outputs = [output]
    env.ctx.jobs.run_once()
    assert job(env, jid)["status"] == "succeeded"
    response = env.client.get(f"/v1/teachback-sessions/{sid}/report")
    validate(response, "/teachback-sessions/{sessionId}/report")
    assert response.json()["data"]["coverage"] == 0.0313


@pytest.mark.parametrize("question_type", ["short_answer", "multiple_choice"])
def test_public_attempt_snapshot_survives_withdrawal_and_never_leaks_private_fields(env, question_type):
    owner, other = env.auth.tokens["test-owner-token"], env.auth.tokens["test-other-token"]
    question_id, publication_id = uid(), uid()
    answer = (
        {"type": question_type, "text": "zero force"}
        if question_type == "short_answer"
        else {"type": question_type, "selected_option_ids": ["a"]}
    )
    options = (
        []
        if question_type == "short_answer"
        else [{"id": "a", "text": "Zero force"}, {"id": "b", "text": "Constant momentum"}]
    )
    snapshot = {
        "question_type": question_type,
        "prompt": "Public question",
        "options": options,
        "difficulty": "easy",
        "private_evidence": "DO_NOT_EXPOSE",
        "answer_key": {"private": "DO_NOT_EXPOSE"},
    }
    expected = {}
    with env.ctx.db.transaction() as db:
        db.add(
            Question(
                id=question_id,
                user_id=owner,
                question_type=question_type,
                prompt="PRIVATE_QUESTION",
                options=options,
                answer_key=answer,
                public_explanation="Public explanation",
                accepted_answers=["zero force"],
                difficulty="easy",
                created_at=utcnow(),
            )
        )
        db.flush()
        db.add(
            Publication(
                id=publication_id,
                user_id=owner,
                question_id=question_id,
                status="withdrawn",
                question_type=question_type,
                prompt="Public question",
                options=options,
                answer_key=answer,
                accepted_answers=["zero force"],
                explanation="Public explanation",
                difficulty="easy",
                keywords=[],
                withdrawn_at=utcnow(),
            )
        )
        db.flush()
        for user in (owner, other):
            expected[user] = uid()
            db.add(
                PublicAttempt(
                    id=expected[user],
                    user_id=user,
                    publication_id=publication_id,
                    answer={**answer, "private_metadata": "DO_NOT_EXPOSE"},
                    correct_answer=answer,
                    is_correct=True,
                    explanation="Public explanation",
                    question_snapshot=snapshot,
                )
            )
    for headers, user in (({}, owner), (OTHER, other)):
        response = env.client.get("/v1/records/public-attempts", headers=headers)
        validate(response, "/records/public-attempts")
        assert [row["id"] for row in response.json()["data"]] == [expected[user]]
        assert response.json()["data"][0]["answer"] == answer
        assert response.json()["data"][0]["correct_answer"] == answer
        assert "PRIVATE_QUESTION" not in response.text and "DO_NOT_EXPOSE" not in response.text


@pytest.mark.parametrize("change", ["confirmation", "owner"])
def test_standalone_creation_revalidates_current_source_instead_of_cached_models(env, change):
    from language_learning_api.routers.teachback import lock_standalone_version

    owner = env.auth.tokens["test-owner-token"]
    with env.ctx.db.transaction() as db:
        cached_version = db.get(ConceptVersion, env.ids["version"])
        cached_material = db.get(Material, env.ids["material"])
        assert cached_version.is_confirmed and cached_material.user_id == owner
        if change == "confirmation":
            db.execute(
                update(ConceptVersion)
                .where(ConceptVersion.id == env.ids["version"])
                .values(is_confirmed=False, confirmed_at=None)
                .execution_options(synchronize_session=False)
            )
            assert cached_version.is_confirmed
        else:
            db.execute(
                update(Material)
                .where(Material.id == env.ids["material"])
                .values(user_id=env.auth.tokens["test-other-token"])
                .execution_options(synchronize_session=False)
            )
            assert cached_material.user_id == owner
        with pytest.raises(ApiError) as caught:
            lock_standalone_version(db, owner, env.ids["version"])
        assert caught.value.status == (422 if change == "confirmation" else 404)
        assert db.scalar(select(func.count(TeachbackSession.id))) == 0


def assert_mysql_standalone_link_lock_order(fixture, pipeline_result):
    """Exercise actual NOWAIT SQL and FK parents in the disposable MySQL fixture.

    A task operation may own a completed session or graded attempt before it
    needs the source material/version. Standalone creation must return a known
    conflict, release its source lock, and allow an identical-key retry later.
    """
    from threading import Thread

    import httpx

    from app.models import QuestionAttempt, QuestionConcept

    assert fixture.ctx.db.engine.dialect.name == "mysql"
    completed_id = pipeline_result["session_id"]
    with fixture.ctx.db.transaction() as db:
        completed = db.get(TeachbackSession, completed_id)
        assert completed.status == "completed"
        version_id = completed.concept_version_id
        concept_id = db.get(ConceptVersion, version_id).concept_id
        attempt_id = db.scalar(
            select(QuestionAttempt.id)
            .join(QuestionConcept, QuestionConcept.question_id == QuestionAttempt.question_id)
            .where(
                QuestionAttempt.user_id == pipeline_result["owner"],
                QuestionAttempt.status == "graded",
                QuestionConcept.concept_id == concept_id,
            )
            .order_by(QuestionAttempt.id)
            .limit(1)
        )
        assert attempt_id is not None
    provider_calls = len(fixture.provider.calls)
    for field, model, parent_id in (
        ("previous_session_id", TeachbackSession, completed_id),
        ("attempt_id", QuestionAttempt, attempt_id),
    ):
        body = {
            "concept_version_id": version_id,
            "student_persona": "curious_beginner",
            "explanation_language": "en",
            field: parent_id,
        }
        key = uid()
        responses, failures = [], []

        def create_linked():
            try:
                with httpx.Client(
                    base_url=str(fixture.client.base_url),
                    headers=dict(fixture.client.headers),
                    timeout=5,
                    trust_env=False,
                ) as client:
                    responses.append(
                        client.post("/v1/teachback-sessions", json=body, headers={"Idempotency-Key": key})
                    )
            except Exception as exc:
                failures.append(type(exc).__name__)

        request_thread = Thread(target=create_linked, daemon=True)
        try:
            with fixture.ctx.db.transaction() as blocking:
                assert blocking.scalar(select(model).where(model.id == parent_id).with_for_update())
                request_thread.start()
                request_thread.join(timeout=6)
                assert not request_thread.is_alive(), "Linked-session acceptance waited on the FK parent"
                assert not failures, failures
                assert len(responses) == 1
                response = responses[0]
                assert response.status_code == 409, response.text
                assert response.json()["error"]["code"] == "RESOURCE_CHANGED"
                validate(response, "/teachback-sessions", "post")
                # This is the other half of the former cycle. The rejected
                # request must have rolled back and released Material X.
                assert blocking.scalar(
                    select(Material.id)
                    .where(Material.id == pipeline_result["material_id"])
                    .with_for_update(nowait=True)
                )
        finally:
            if request_thread.ident is not None:
                request_thread.join(timeout=7)
        assert not request_thread.is_alive(), "Linked-session HTTP thread leaked"
        retried = fixture.client.post("/v1/teachback-sessions", json=body, headers={"Idempotency-Key": key})
        assert retried.status_code == 201, retried.text
        validate(retried, "/teachback-sessions", "post")
        assert retried.json()["data"][field] == parent_id
    assert len(fixture.provider.calls) == provider_calls
