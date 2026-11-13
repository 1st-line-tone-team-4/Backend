"""N13 subscription policy, idempotency and concurrent generation outcomes.

HTTP uses real fixture JWTs and the canonical worker; only the provider is local.
The MySQL helper exercises old REPEATABLE READ snapshots and independent source
requests without imposing a particular duplicate-detection implementation.
"""

from contextlib import closing
from datetime import timedelta
from threading import Barrier, Event, Lock, Thread
from unittest.mock import patch
import sqlite3

import httpx
import pytest
from sqlalchemy import event, select

from app.core import uid, utcnow
from app.models import IdempotencyRequest, Job, LearningExplanation
from tests.ab_support import MODEL, ab_http_application
from tests.test_ab_e2e import post, run_job, validate
from tests.test_terminal_jobs_n11 import assert_unknown_history, fail_unknown, seed_terminal_graph


@pytest.fixture
def explanation_env(tmp_path):
    with ab_http_application(tmp_path) as fixture:
        yield seed_terminal_graph(fixture)


def explanation_body(fixture, language="en"):
    return {
        "concept_version_id": fixture.ids["version"],
        "explanation_language": language,
        "model_id": MODEL,
    }


def assert_busy(response):
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "JOB_IN_PROGRESS", response.text


def test_explanation_same_key_active_guard_and_owner_isolation(explanation_env):
    fx, key = explanation_env, uid()
    body = explanation_body(fx)
    calls = len(fx.provider.calls)
    first = post(fx, "/v1/learning-explanations", body, key=key)
    assert first.status_code == 202, first.text
    validate(first, "/learning-explanations", "post")
    assert post(fx, "/v1/learning-explanations", body, key=key).json() == first.json()
    assert_busy(post(fx, "/v1/learning-explanations", body))
    changed = post(fx, "/v1/learning-explanations", explanation_body(fx, "ko"), key=key)
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    job = first.json()["data"]["job"]
    foreign = [
        post(fx, "/v1/learning-explanations", body, headers=fx.other_headers),
        fx.client.get("/v1/learning-explanations/" + job["resource_id"], headers=fx.other_headers),
        fx.client.get(
            "/v1/learning-explanations",
            params={"concept_version_id": fx.ids["version"]},
            headers=fx.other_headers,
        ),
        post(
            fx,
            f"/v1/jobs/{job['id']}/restart",
            {"model_id": MODEL, "acknowledge_possible_duplicate_execution": True},
            headers=fx.other_headers,
        ),
    ]
    assert all(response.status_code == 404 for response in foreign), [r.text for r in foreign]
    assert len(fx.provider.calls) == calls
    run_job(fx, first)
    assert len(fx.provider.calls) == calls + 1


def test_explanation_running_job_blocks_new_key_but_replays_original(explanation_env):
    fx, key = explanation_env, uid()
    body = explanation_body(fx)
    first = post(fx, "/v1/learning-explanations", body, key=key)
    assert first.status_code == 202, first.text
    entered, resume = Event(), Event()
    failures = []
    generate = fx.provider.generate

    def held_provider(*args, **kwargs):
        entered.set()
        assert resume.wait(10), "Local provider was not released"
        return generate(*args, **kwargs)

    def worker():
        try:
            assert fx.ctx.jobs.run_once()
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=worker, daemon=True)
    with patch.object(fx.provider, "generate", held_provider):
        try:
            thread.start()
            assert entered.wait(5), "Worker did not reach the local provider"
            with fx.ctx.db.transaction() as db:
                assert db.get(Job, first.json()["data"]["job"]["id"]).status == "running"
            assert_busy(post(fx, "/v1/learning-explanations", body))
            replay = post(fx, "/v1/learning-explanations", body, key=key)
            assert replay.status_code == 202 and replay.json() == first.json()
        finally:
            resume.set()
            thread.join(10)
    assert not thread.is_alive() and not failures, failures
    with fx.ctx.db.transaction() as db:
        assert db.get(Job, first.json()["data"]["job"]["id"]).status == "succeeded"


def test_explicit_explanation_after_unknown_needs_no_ack_and_preserves_history(explanation_env):
    fx, key = explanation_env, uid()
    body = explanation_body(fx)
    first = post(fx, "/v1/learning-explanations", body, key=key)
    prior = fail_unknown(fx, first)
    calls = len(fx.provider.calls)
    replay = post(fx, "/v1/learning-explanations", body, key=key)
    assert replay.status_code == 202 and replay.json() == first.json()
    assert fx.ctx.jobs.run_once() is False
    retry = post(fx, f"/v1/jobs/{prior.id}/retry", {"model_id": MODEL})
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "JOB_NOT_RETRYABLE"
    for restart_body in (
        {"model_id": MODEL},
        {"model_id": MODEL, "acknowledge_possible_duplicate_execution": False},
    ):
        rejected = post(fx, f"/v1/jobs/{prior.id}/restart", restart_body)
        assert rejected.status_code == 422, rejected.text
    saved = fx.client.get("/v1/learning-explanations/" + prior.resource_id)
    assert saved.status_code == 200 and saved.json()["data"]["status"] == "outcome_unknown"
    listed = fx.client.get("/v1/learning-explanations", params={"concept_version_id": fx.ids["version"]})
    assert listed.status_code == 200 and len(listed.json()["data"]) == 1
    assert len(fx.provider.calls) == calls

    # This explicit new-generation POST contains only the three canonical
    # fields. An unknown predecessor does not add a per-generation cost prompt.
    fresh = post(fx, "/v1/learning-explanations", body)
    assert fresh.status_code == 202, fresh.text
    fresh_job = fresh.json()["data"]["job"]
    assert fresh_job["id"] != prior.id and fresh_job["resource_id"] != prior.resource_id
    assert len(fx.provider.calls) == calls
    assert_unknown_history(fx, prior)
    run_job(fx, fresh)
    assert len(fx.provider.calls) == calls + 1
    assert_unknown_history(fx, prior)
    with fx.ctx.db.transaction() as db:
        assert db.get(Job, fresh_job["id"]).previous_job_id is None
        assert db.get(LearningExplanation, prior.resource_id).latest_job_id == prior.id
    assert fx.ctx.jobs.run_once() is False

    # The existing shared restart API still requires its explicit ack and
    # attaches a successor to the original resource rather than creating one.
    restarted = post(
        fx,
        f"/v1/jobs/{prior.id}/restart",
        {"model_id": MODEL, "acknowledge_possible_duplicate_execution": True},
    )
    assert restarted.status_code == 202, restarted.text
    assert restarted.json()["data"]["job"]["resource_id"] == prior.resource_id
    run_job(fx, restarted)
    with fx.ctx.db.transaction() as db:
        assert db.get(Job, prior.id).status == "outcome_unknown"
        assert db.get(Job, restarted.json()["data"]["job"]["id"]).previous_job_id == prior.id


def _credential_refs(fixture):
    # Inspect only opaque references in this fixture's private vault, never keys
    # or ciphertext. A losing concurrent insert must discard its staged entry.
    with closing(sqlite3.connect(fixture.ctx.credentials.path)) as vault:
        return {row[0] for row in vault.execute("SELECT ref FROM credentials")}


def _cancel_source_explanations(fixture, version_ids):
    with fixture.ctx.db.transaction() as db:
        ids = [
            job.id
            for job in db.scalars(
                select(Job).where(
                    Job.user_id == fixture.owner,
                    Job.kind == "learning_explanation",
                    Job.status.in_(("queued", "running")),
                )
            )
            if job.request_payload["concept_version_id"] in version_ids
        ]
    for identity in ids:
        fixture.ctx.jobs.cancel(job_id=identity, user_id=fixture.owner)


def _request_thread(fixture, body, key, index, results, failures):
    try:
        with httpx.Client(
            base_url=fixture.base_url, headers=fixture.owner_headers, timeout=20, trust_env=False
        ) as client:
            results[index] = client.post(
                "/v1/learning-explanations",
                json=body,
                headers={"Idempotency-Key": key},
            )
    except BaseException as exc:
        failures.append(exc)


def _assert_generation_outcomes(fx, bodies, keys, results, statuses, prior_refs, prior_calls):
    assert sorted(response.status_code for response in results.values()) == sorted(statuses), [
        response.text for response in results.values()
    ]
    identities = {(body["concept_version_id"], body["explanation_language"]) for body in bodies}
    accepted_ids = set()
    for index, response in results.items():
        if response.status_code == 409:
            assert_busy(response)
        else:
            accepted_ids.add(response.json()["data"]["job"]["id"])
            replay = post(fx, "/v1/learning-explanations", bodies[index], key=keys[index])
            assert replay.status_code == 202 and replay.json() == response.json()
    with fx.ctx.db.transaction() as db:
        jobs = [
            job
            for job in db.scalars(
                select(Job).where(
                    Job.user_id == fx.owner,
                    Job.kind == "learning_explanation",
                    Job.status.in_(("queued", "running")),
                )
            )
            if (job.request_payload["concept_version_id"], job.request_payload["explanation_language"])
            in identities
        ]
        assert {job.id for job in jobs} == accepted_ids
        assert all(job.status == "queued" and job.dispatched_at is None for job in jobs)
        # Contract 1.4.0: no learner credential is staged for accepted jobs.
        assert all(job.credential_ref is None for job in jobs)
        assert _credential_refs(fx) == prior_refs
        resources = db.scalars(
            select(LearningExplanation).where(LearningExplanation.latest_job_id.in_(accepted_ids))
        ).all()
        assert len(resources) == len(accepted_ids)
        assert all(resource.status == "generating" for resource in resources)
        for index, response in results.items():
            claim = db.get(IdempotencyRequest, (fx.owner, "createLearningExplanation", keys[index]))
            if response.status_code == 202:
                assert claim is not None and claim.status == "completed"
            else:
                # An expired synthetic claim is restored by rollback; a new
                # rejected key leaves no claim at all.
                assert claim is None or claim.expires_at < utcnow()
    assert len(fx.provider.calls) == prior_calls, "Acceptance must not dispatch a provider call"


def _concurrent_generation(fx, bodies, *, keys=None, synchronize_enqueue=False):
    keys, results, failures = keys or [uid(), uid()], {}, []
    prior_refs, prior_calls = _credential_refs(fx), len(fx.provider.calls)
    rendezvous = Barrier(2)
    original_enqueue = fx.ctx.jobs.enqueue

    def enqueue_together(*args, **kwargs):
        # Synchronize two real transactions after their domain validation. No
        # guard, database result, fingerprint or insertion is substituted.
        if synchronize_enqueue:
            rendezvous.wait(10)
        return original_enqueue(*args, **kwargs)

    threads = [
        Thread(target=_request_thread, args=(fx, body, keys[index], index, results, failures), daemon=True)
        for index, body in enumerate(bodies)
    ]
    versions = {body["concept_version_id"] for body in bodies}
    try:
        with patch.object(fx.ctx.jobs, "enqueue", enqueue_together):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
            assert not any(thread.is_alive() for thread in threads), "Concurrent generation did not finish"
            assert not failures, failures
        _assert_generation_outcomes(
            fx,
            bodies,
            keys,
            results,
            [202, 409] if bodies[0] == bodies[1] else [202, 202],
            prior_refs,
            prior_calls,
        )
    finally:
        rendezvous.abort()
        for thread in threads:
            if thread.ident is not None:
                thread.join(21)
        # Scan the exact synthetic sources, including any unexpectedly accepted
        # loser or a response which arrived only during failure cleanup.
        _cancel_source_explanations(fx, versions)
    assert _credential_refs(fx) == prior_refs
    assert len(fx.provider.calls) == prior_calls


def test_concurrent_same_explanation_has_one_accepted_job_and_no_dispatch(explanation_env):
    fx = explanation_env
    body = explanation_body(fx)
    _concurrent_generation(fx, [body, body])


def test_concurrent_independent_explanations_both_succeed(explanation_env):
    fx = explanation_env
    first = explanation_body(fx)
    seed_terminal_graph(fx)
    second = explanation_body(fx)
    assert first["concept_version_id"] != second["concept_version_id"]
    _concurrent_generation(fx, [first, second])


def _seed_separate_claim_gaps(fixture):
    """Requests must overlap in the domain rather than waiting on a claim gap."""
    prefix, now = "n13-" + uid(), utcnow()
    keys = [prefix + f"-{index:02d}" for index in range(5)]
    with fixture.ctx.db.transaction() as db:
        for index, key in enumerate(keys):
            db.add(
                IdempotencyRequest(
                    user_id=fixture.owner,
                    operation_id="createLearningExplanation",
                    idempotency_key=key,
                    request_hash="0" * 64,
                    status="completed",
                    http_status=200,
                    response_body={},
                    created_at=now - timedelta(days=2),
                    expires_at=now - timedelta(seconds=1) if index in (1, 3) else now + timedelta(hours=1),
                )
            )
    return keys[1], keys[3]


def _mysql_same_input_snapshot(fx):
    engine, body = fx.ctx.db.engine, explanation_body(fx)
    keys, results, failures = _seed_separate_claim_gaps(fx), {}, []
    first_locked, second_waiting, resume = Event(), Event(), Event()
    mutex, ordinal, marker = Lock(), 0, "n13_source_" + uid()
    prior_refs, prior_calls = _credential_refs(fx), len(fx.provider.calls)

    def before_material(connection, cursor, statement, parameters, context, many):
        nonlocal ordinal
        if "FROM materials" in statement and "FOR UPDATE" in statement:
            with mutex:
                ordinal += 1
                index = ordinal
            assert index <= 2, "Unexpected source lock during the controlled race"
            connection.info[marker] = index
            if index == 2:
                # Earlier idempotency/confirmed-version reads established this
                # request's RR snapshot while the first still holds Material.
                second_waiting.set()

    def after_material(connection, cursor, statement, parameters, context, many):
        if "FROM materials" in statement and "FOR UPDATE" in statement:
            index = connection.info.pop(marker)
            if index == 1:
                first_locked.set()
                assert resume.wait(15), "First acceptance was not released"

    threads = [
        Thread(target=_request_thread, args=(fx, body, keys[index], index, results, failures), daemon=True)
        for index in range(2)
    ]
    event.listen(engine, "before_cursor_execute", before_material)
    event.listen(engine, "after_cursor_execute", after_material)
    try:
        threads[0].start()
        assert first_locked.wait(5), "First request did not acquire its source lock"
        threads[1].start()
        assert second_waiting.wait(5), "Second request did not establish its older snapshot"
        resume.set()
        for thread in threads:
            thread.join(12)
        assert not any(thread.is_alive() for thread in threads) and not failures, failures
        _assert_generation_outcomes(fx, [body, body], keys, results, [202, 409], prior_refs, prior_calls)
    finally:
        resume.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(21)
        event.remove(engine, "before_cursor_execute", before_material)
        event.remove(engine, "after_cursor_execute", after_material)
        _cancel_source_explanations(fx, {body["concept_version_id"]})
    assert _credential_refs(fx) == prior_refs
    assert len(fx.provider.calls) == prior_calls


def assert_mysql_explanation_subscription_concurrency(fixture):
    """Use the signed-JWT fixture before any helper leaves global queued jobs."""
    fx = fixture
    assert fx.ctx.db.engine.dialect.name == "mysql"
    with fx.ctx.db.engine.connect() as connection:
        assert connection.get_isolation_level() == "REPEATABLE READ"
    seed_terminal_graph(fx)
    _mysql_same_input_snapshot(fx)
    first = explanation_body(fx)
    seed_terminal_graph(fx)
    second = explanation_body(fx)
    assert first["concept_version_id"] != second["concept_version_id"]
    # With distinct source locks, both requests reach enqueue together. Missing
    # active keys must not introduce mutually blocking RR gap-lock upgrades.
    _concurrent_generation(fx, [first, second], keys=_seed_separate_claim_gaps(fx), synchronize_enqueue=True)
