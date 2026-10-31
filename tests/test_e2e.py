"""Real loopback HTTP -> durable SQL job -> worker -> SQL -> owner GET.

No paid AI, external storage, or A business logic is used in this suite.
"""

from pathlib import Path
from threading import Thread
from time import monotonic, sleep
import json
import multiprocessing
import socket

import httpx
import pytest
import uvicorn
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select

from app.core import uid
from app.models import Job, LearningExplanation, Question, PlanTask
from app.providers import ProviderFailure
from tests.support import test_application as make_test_application

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts/openapi-1.3.0.json").read_text(encoding="utf-8")
)
OWNER = {"Authorization": "Bearer test-owner-token"}
OTHER = {"Authorization": "Bearer test-other-token"}
MODEL = "test:grounded"
KEY = "test-transient-key"


def validate(response, path, method="get"):
    operation = CONTRACT["paths"][path][method]
    definition = operation["responses"].get(str(response.status_code))
    assert definition is not None, f"Undocumented status {response.status_code}: {response.text}"
    if "$ref" in definition:
        schema = {"$ref": "#/components/schemas/ErrorEnvelope"}
    else:
        if "content" not in definition:
            assert response.content == b""
            return
        schema = definition["content"]["application/json"]["schema"]
    Draft202012Validator(
        {**schema, "components": CONTRACT["components"]}, format_checker=FormatChecker()
    ).validate(response.json())


@pytest.fixture
def live(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    fixture = make_test_application(tmp_path, f"http://{host}:{port}")
    server = uvicorn.Server(
        uvicorn.Config(
            fixture.app, host=host, port=port, log_level="critical", access_log=False, lifespan="off"
        )
    )
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = monotonic() + 10
    while not server.started:
        assert thread.is_alive() and monotonic() < deadline, "HTTP test server failed to start"
        sleep(0.01)
    fixture.client = httpx.Client(
        base_url=f"http://{host}:{port}", headers=OWNER, timeout=15, trust_env=False
    )
    try:
        yield fixture
    finally:
        fixture.client.close()
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        fixture.ctx.db.engine.dispose()
        assert not thread.is_alive(), "HTTP test server leaked"


def post(live, path, body, *, idem=None, headers=None):
    return live.client.post(
        path, json=body, headers={"Idempotency-Key": idem or uid(), "X-AI-Key": KEY, **(headers or {})}
    )


def upload(live):
    text = b"Momentum is conserved when the sum of external forces is zero."
    body = {"filename": "lecture.txt", "mime_type": "text/plain", "size_bytes": len(text)}
    key = uid()
    response = post(live, "/v1/materials", body, idem=key)
    assert response.status_code == 201, response.text
    validate(response, "/materials", "post")
    replay = post(live, "/v1/materials", body, idem=key)
    assert replay.json() == response.json()
    assert post(live, "/v1/materials", {**body, "filename": "different.txt"}, idem=key).status_code == 409
    item = response.json()["data"]
    assert (
        live.client.put(item["upload_url"], content=text, headers={"Content-Type": "text/plain"}).status_code
        == 204
    )
    assert (
        live.client.put(item["upload_url"], content=text, headers={"Content-Type": "text/plain"}).status_code
        == 409
    )
    return item["material"]["id"]


def analyzed(live):
    mid = upload(live)
    response = post(live, f"/v1/materials/{mid}/complete", {"model_id": MODEL})
    assert response.status_code == 202, response.text
    validate(response, "/materials/{materialId}/complete", "post")
    jid = response.json()["data"]["job"]["id"]
    assert live.ctx.jobs.run_once()
    result = live.client.get(f"/v1/jobs/{jid}")
    assert result.json()["data"]["status"] == "succeeded", result.text
    validate(result, "/jobs/{jobId}")
    assert live.client.get(f"/v1/jobs/{jid}").status_code == 429
    material = live.client.get(f"/v1/materials/{mid}")
    assert material.json()["data"]["processing_status"] == "ready"
    validate(material, "/materials/{materialId}")
    concepts = live.client.get(f"/v1/materials/{mid}/concepts")
    validate(concepts, "/materials/{materialId}/concepts")
    return mid, concepts.json()["data"][0]


def confirmed(live):
    mid, concept = analyzed(live)
    draft = concept["current_version"]
    item = {
        "concept_id": concept["concept_id"],
        "unit_id": concept["unit_id"],
        "base_version_id": draft["id"],
        "name": draft["name"],
        "summary": draft["summary"],
        "checklist_items": [x["criterion"] for x in draft["checklist_items"]],
        "evidence_chunk_ids": [x["chunk_id"] for x in draft["evidence"]],
    }
    response = live.client.patch(f"/v1/materials/{mid}/concepts", json={"concepts": [item]})
    assert response.status_code == 200, response.text
    validate(response, "/materials/{materialId}/concepts", "patch")
    version = response.json()["data"][0]["version"]
    assert version["is_confirmed"] and version["id"] != draft["id"]
    conflict = live.client.patch(f"/v1/materials/{mid}/concepts", json={"concepts": [item]})
    assert conflict.status_code == 409
    return mid, concept, version


def test_complete_content_pipeline_and_a_boundary(live):
    mid, concept, version = confirmed(live)
    task_id = uid()
    owner_id = live.auth.tokens["test-owner-token"]
    with live.ctx.db.transaction() as s:
        s.add(PlanTask(id=task_id))
    live.tasks.tasks[task_id] = {
        "owner": owner_id,
        "material": mid,
        "unit": concept["unit_id"],
        "versions": [version["id"]],
        "task_type": "solve",
        "target_count": 3,
        "active_revision": True,
        "completed_count": 1,
    }
    data = {
        "unit_id": concept["unit_id"],
        "concept_version_ids": [version["id"]],
        "task_id": task_id,
        "count": 2,
        "difficulty": "easy",
        "model_id": MODEL,
    }
    over_target = post(live, "/v1/question-sets", {**data, "count": 3})
    assert over_target.status_code == 422
    assert over_target.json()["error"]["code"] == "TASK_TARGET_EXCEEDED"
    live.tasks.tasks[task_id]["task_type"] = "teach_back"
    wrong_type = post(live, "/v1/question-sets", data)
    assert wrong_type.status_code == 422 and wrong_type.json()["error"]["code"] == "INVALID_TASK_TYPE"
    live.tasks.tasks[task_id]["task_type"] = "solve"
    live.tasks.tasks[task_id]["active_revision"] = False
    stale_task = post(live, "/v1/question-sets", data)
    assert stale_task.status_code == 409 and stale_task.json()["error"]["code"] == "STALE_TASK"
    live.tasks.tasks[task_id]["active_revision"] = True
    response = post(live, "/v1/question-sets", data)
    assert response.status_code == 202, response.text
    validate(response, "/question-sets", "post")
    set_id = response.json()["data"]["job"]["resource_id"]
    assert post(live, "/v1/question-sets", data).status_code == 409
    assert live.ctx.jobs.run_once()
    result = live.client.get(f"/v1/question-sets/{set_id}")
    assert result.json()["data"]["status"] == "ready", result.text
    validate(result, "/question-sets/{setId}")
    questions = result.json()["data"]["items"]
    assert len(questions) == 2 and result.json()["data"]["next_ordinal"] == 1
    public = live.client.get("/v1/questions/" + questions[0]["question"]["id"])
    validate(public, "/questions/{questionId}")
    assert "answer_key" not in public.text and "accepted_answers" not in public.text
    from app.content import get_grading_question

    with live.ctx.db.transaction() as s:
        grading = get_grading_question(s, owner_id, questions[0]["question"]["id"])
        assert grading["answer_key"]["text"]
    # The temporary A adapter reports an already-graded answer without B writing task state.
    live.tasks.attempts[(owner_id, set_id, questions[0]["question"]["id"])] = {
        "latest_attempt": None,
        "has_graded_attempt": True,
    }
    assert live.client.get(f"/v1/question-sets/{set_id}").json()["data"]["next_ordinal"] == 2
    explanation = post(
        live,
        "/v1/learning-explanations",
        {"concept_version_id": version["id"], "explanation_language": "ko", "model_id": MODEL},
    )
    assert explanation.status_code == 202, explanation.text
    validate(explanation, "/learning-explanations", "post")
    eid = explanation.json()["data"]["job"]["resource_id"]
    pending = live.client.get(f"/v1/learning-explanations/{eid}")
    assert pending.json()["data"]["content"] is None
    validate(pending, "/learning-explanations/{explanationId}")
    assert live.ctx.jobs.run_once()
    result = live.client.get(f"/v1/learning-explanations/{eid}")
    assert result.json()["data"]["status"] == "ready", result.text
    validate(result, "/learning-explanations/{explanationId}")
    assert result.json()["data"]["model_id"] == MODEL
    history = live.client.get("/v1/learning-explanations", params={"concept_version_id": version["id"]})
    validate(history, "/learning-explanations")
    assert history.json()["data"][0]["id"] == eid
    for path in [
        f"/v1/materials/{mid}",
        f"/v1/question-sets/{set_id}",
        f"/v1/questions/{questions[0]['question']['id']}",
        f"/v1/learning-explanations/{eid}",
    ]:
        denied = live.client.get(path, headers=OTHER)
        assert denied.status_code == 404, denied.text
    assert live.tasks.progress_writes == 0
    assert live.tasks.tasks[task_id]["completed_count"] == 1
    assert live.client.delete(f"/v1/materials/{mid}").status_code == 409
    assert live.client.patch(f"/v1/materials/{mid}", json={"archived": True}).status_code == 200
    assert live.client.get(f"/v1/learning-explanations/{eid}").status_code == 200
    assert (
        post(
            live,
            "/v1/learning-explanations",
            {"concept_version_id": version["id"], "explanation_language": "ko", "model_id": MODEL},
        ).status_code
        == 422
    )
    assert len(live.provider.calls) == 3
    assert not live.ctx.jobs.run_once()
    with live.ctx.db.transaction() as s:
        assert all(j.credential_ref is None for j in s.scalars(select(Job)))


def test_failure_retry_unknown_restart_and_atomic_invalid_output(live):
    mid, concept, version = confirmed(live)
    body = {"concept_version_id": version["id"], "explanation_language": "en", "model_id": MODEL}
    response = post(live, "/v1/learning-explanations", body)
    job = response.json()["data"]["job"]
    live.provider.fail_next = ProviderFailure("PROVIDER_RATE_LIMIT", retryable=True)
    live.ctx.jobs.run_once()
    failed = live.client.get(f"/v1/jobs/{job['id']}")
    assert failed.json()["data"]["retryable"] is True
    validate(failed, "/jobs/{jobId}")
    retry = post(live, f"/v1/jobs/{job['id']}/retry", {"model_id": MODEL})
    assert retry.status_code == 202, retry.text
    validate(retry, "/jobs/{jobId}/retry", "post")
    assert retry.json()["data"]["job"]["id"] == job["id"]
    live.provider.fail_next = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    live.ctx.jobs.run_once()
    result = live.client.get(f"/v1/learning-explanations/{job['resource_id']}")
    assert result.json()["data"]["status"] == "outcome_unknown"
    before = len(live.provider.calls)
    assert not live.ctx.jobs.run_once() and len(live.provider.calls) == before
    assert post(live, f"/v1/jobs/{job['id']}/retry", {"model_id": MODEL}).status_code == 409
    assert (
        post(
            live,
            f"/v1/jobs/{job['id']}/restart",
            {"model_id": MODEL, "acknowledge_possible_duplicate_cost": False},
        ).status_code
        == 422
    )
    restart = post(
        live,
        f"/v1/jobs/{job['id']}/restart",
        {"model_id": MODEL, "acknowledge_possible_duplicate_cost": True},
    )
    assert restart.status_code == 202, restart.text
    validate(restart, "/jobs/{jobId}/restart", "post")
    assert restart.json()["data"]["job"]["previous_job_id"] == job["id"]
    assert restart.json()["data"]["job"]["resource_id"] == job["resource_id"]
    live.provider.invalid_next = True
    live.ctx.jobs.run_once()
    result = live.client.get(f"/v1/learning-explanations/{job['resource_id']}")
    assert result.json()["data"]["status"] == "failed" and result.json()["data"]["content"] is None
    with live.ctx.db.transaction() as s:
        assert len(list(s.scalars(select(LearningExplanation)))) == 1
        assert not list(s.scalars(select(Question)))
    assert KEY.encode() not in Path(live.ctx.settings.database_url.removeprefix("sqlite:///")).read_bytes()


def test_auth_validation_and_model_boundaries(live):
    denied = live.client.get("/v1/materials", headers={"Authorization": "Bearer invalid"})
    assert denied.status_code == 401
    validate(denied, "/materials")
    result = post(
        live, "/v1/materials", {"filename": "x", "mime_type": "text/plain", "size_bytes": 1, "api_key": KEY}
    )
    assert result.status_code == 422 and KEY not in result.text
    validate(result, "/materials", "post")
    models = live.client.get("/v1/ai/models")
    validate(models, "/ai/models")
    response = post(live, "/v1/ai/validate-key", {"model_id": MODEL})
    assert response.status_code == 200
    validate(response, "/ai/validate-key", "post")
    assert not live.provider.calls
    assert live.client.get("/v1/materials", params={"cursor": "garbage"}).status_code == 422


def _worker_subprocess(settings):
    from app.main import build_context
    from tests.support import FixtureAuth, FixtureProvider, FixtureTasks

    ctx = build_context(
        settings, auth=FixtureAuth(), providers=FixtureProvider(), task_gateway=FixtureTasks()
    )
    ctx.jobs.run_once()
    ctx.db.engine.dispose()


def test_durable_job_executes_in_separate_worker_process(live):
    mid = upload(live)
    response = post(live, f"/v1/materials/{mid}/complete", {"model_id": MODEL})
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["job"]["id"]
    proc = multiprocessing.get_context("spawn").Process(target=_worker_subprocess, args=(live.ctx.settings,))
    proc.start()
    proc.join(timeout=20)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        pytest.fail("Worker process did not terminate")
    assert proc.exitcode == 0
    response = live.client.get(f"/v1/jobs/{job_id}")
    assert response.json()["data"]["status"] == "succeeded", response.text
    assert live.client.get(f"/v1/materials/{mid}").json()["data"]["processing_status"] == "ready"
    assert live.provider.calls == []


@pytest.mark.parametrize("successor_status", ["queued", "running", "succeeded", "failed", "outcome_unknown"])
def test_unknown_cost_ack_survives_every_successor_state(live, successor_status):
    mid, concept, version = confirmed(live)
    data = {
        "unit_id": concept["unit_id"],
        "concept_version_ids": [version["id"]],
        "count": 1,
        "difficulty": "easy",
        "model_id": MODEL,
    }
    response = post(live, "/v1/question-sets", data)
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["job"]["id"]
    live.provider.fail_next = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    assert live.ctx.jobs.run_once()
    blocked = post(live, "/v1/question-sets", data)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "POSSIBLE_DUPLICATE_COST"
    restarted = post(
        live,
        f"/v1/jobs/{job_id}/restart",
        {"model_id": MODEL, "acknowledge_possible_duplicate_cost": True},
    )
    assert restarted.status_code == 202, restarted.text
    successor_id = restarted.json()["data"]["job"]["id"]
    if successor_status == "running":
        assert live.ctx.jobs._claim()[0] == successor_id
    elif successor_status != "queued":
        if successor_status == "failed":
            live.provider.fail_next = ProviderFailure("PROVIDER_AUTH", retryable=True)
        elif successor_status == "outcome_unknown":
            live.provider.fail_next = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
        assert live.ctx.jobs.run_once()
    with live.ctx.db.transaction() as s:
        assert s.get(Job, job_id).status == "outcome_unknown"
        assert s.get(Job, successor_id).status == successor_status
    calls_before = len(live.provider.calls)
    again = post(live, "/v1/question-sets", data)
    active = successor_status in ("queued", "running")
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == ("JOB_IN_PROGRESS" if active else "POSSIBLE_DUPLICATE_COST")
    assert len(live.provider.calls) == calls_before
    if successor_status == "outcome_unknown":
        chained = post(
            live,
            f"/v1/jobs/{successor_id}/restart",
            {"model_id": MODEL, "acknowledge_possible_duplicate_cost": True},
        )
        assert chained.status_code == 202, chained.text
        assert live.ctx.jobs.run_once()
        with live.ctx.db.transaction() as s:
            assert s.get(Job, chained.json()["data"]["job"]["id"]).status == "succeeded"
        after_chain = post(live, "/v1/question-sets", data)
        assert after_chain.status_code == 409
        assert after_chain.json()["error"]["code"] == "POSSIBLE_DUPLICATE_COST"
    acknowledged = post(live, "/v1/question-sets", {**data, "acknowledge_possible_duplicate_cost": True})
    assert acknowledged.status_code == (409 if active else 202), acknowledged.text


def test_task_pinned_to_older_confirmed_version_can_still_generate(live):
    mid, concept, v1 = confirmed(live)
    item = {
        "concept_id": concept["concept_id"],
        "unit_id": concept["unit_id"],
        "base_version_id": v1["id"],
        "name": v1["name"],
        "summary": v1["summary"] + " (revised)",
        "checklist_items": [x["criterion"] for x in v1["checklist_items"]],
        "evidence_chunk_ids": [x["chunk_id"] for x in v1["evidence"]],
    }
    response = live.client.patch(f"/v1/materials/{mid}/concepts", json={"concepts": [item]})
    assert response.status_code == 200, response.text
    task_id = uid()
    with live.ctx.db.transaction() as s:
        s.add(PlanTask(id=task_id))
    live.tasks.tasks[task_id] = {
        "owner": live.auth.tokens["test-owner-token"],
        "material": mid,
        "unit": concept["unit_id"],
        "versions": [v1["id"]],
        "task_type": "solve",
        "target_count": 1,
        "active_revision": True,
        "completed_count": 0,
    }
    data = {
        "unit_id": concept["unit_id"],
        "concept_version_ids": [v1["id"]],
        "count": 1,
        "difficulty": "easy",
        "model_id": MODEL,
    }
    stale = post(live, "/v1/question-sets", data)
    assert stale.status_code == 422 and stale.json()["error"]["code"] == "CONCEPT_NOT_CONFIRMED"
    pinned = post(live, "/v1/question-sets", {**data, "task_id": task_id})
    assert pinned.status_code == 202, pinned.text
    assert live.ctx.jobs.run_once()
    set_id = pinned.json()["data"]["job"]["resource_id"]
    generated = live.client.get(f"/v1/question-sets/{set_id}").json()["data"]
    assert generated["status"] == "ready"
    assert generated["items"][0]["question"]["concept_version_ids"] == [v1["id"]]


def test_padded_ai_title_is_known_invalid_output_without_partial_content(live, monkeypatch):
    _, _, version = confirmed(live)
    generate = live.provider.generate

    def padded(*args):
        output = generate(*args)
        output["title"] = "x" + " " * 200
        return output

    monkeypatch.setattr(live.provider, "generate", padded)
    response = post(
        live,
        "/v1/learning-explanations",
        {
            "concept_version_id": version["id"],
            "explanation_language": "ko",
            "model_id": MODEL,
        },
    )
    assert response.status_code == 202
    job = response.json()["data"]["job"]
    assert live.ctx.jobs.run_once()
    with live.ctx.db.transaction() as s:
        row = s.get(Job, job["id"])
        assert (row.status, row.error_code, row.retryable) == ("failed", "INVALID_OUTPUT", False)
        assert row.credential_ref is None
    result = live.client.get("/v1/learning-explanations/" + job["resource_id"]).json()["data"]
    assert result["status"] == "failed" and result["content"] is None
