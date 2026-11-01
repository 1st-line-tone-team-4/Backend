"""Shared real-HTTP probes exercised with both SQLite and the MySQL CI database."""

from threading import Event, Thread

import httpx
from sqlalchemy import event, select, text, update

from app.core import uid
from app.models import IdempotencyRequest, Job, Material
from app.providers import ProviderFailure
import tests.test_e2e as e2e


def _short_lock_wait(session):
    if session.bind.dialect.name == "mysql":
        session.execute(text("SET SESSION innodb_lock_wait_timeout = 2"))


def assert_key_validation_releases_transaction(live):
    entered, release = Event(), Event()
    results, failures, calls = [], [], []
    original = live.provider.validate_key
    idem = uid()

    def blocked(model, key):
        calls.append(model)  # Never retain even synthetic credential values.
        entered.set()
        assert release.wait(20), "Validation probe did not release the provider"
        return original(model, key)

    def request():
        try:
            with httpx.Client(
                base_url=str(live.client.base_url), headers=e2e.OWNER, timeout=25, trust_env=False
            ) as client:
                results.append(
                    client.post(
                        "/v1/ai/validate-key",
                        json={"model_id": e2e.MODEL},
                        headers={"Idempotency-Key": idem, "X-AI-Key": e2e.KEY},
                    )
                )
        except BaseException as exc:
            failures.append(exc)

    live.provider.validate_key = blocked
    thread = Thread(target=request, daemon=True)
    try:
        thread.start()
        assert entered.wait(10), "Provider validation did not start"
        with live.ctx.db.transaction() as session:
            _short_lock_wait(session)
            claim = session.scalar(
                select(IdempotencyRequest).where(IdempotencyRequest.idempotency_key == idem)
            )
            assert claim is not None and claim.status == "in_progress"
            # The durable claim must be readable and writable while HTTP waits.
            result = session.execute(
                update(IdempotencyRequest)
                .where(IdempotencyRequest.idempotency_key == idem)
                .values(expires_at=IdempotencyRequest.expires_at)
            )
            assert result.rowcount == 1
        same = e2e.post(live, "/v1/ai/validate-key", {"model_id": e2e.MODEL}, idem=idem)
        assert same.status_code == 409 and same.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
        changed = e2e.post(
            live,
            "/v1/ai/validate-key",
            {"model_id": e2e.MODEL},
            idem=idem,
            headers={"X-AI-Key": "different-synthetic-key"},
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        if failures:
            raise failures[0]
        assert len(results) == 1 and results[0].status_code == 200
        replay = e2e.post(live, "/v1/ai/validate-key", {"model_id": e2e.MODEL}, idem=idem)
        assert replay.status_code == 200 and replay.json() == results[0].json()
        assert calls == [e2e.MODEL]
    finally:
        release.set()
        thread.join(26)
        live.provider.validate_key = original


def assert_analysis_preparation_releases_locks(live):
    material_id = e2e.upload(live)
    queued = e2e.post(live, f"/v1/materials/{material_id}/complete", {"model_id": e2e.MODEL})
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["data"]["job"]["id"]
    handler = live.ctx.jobs.handlers["analysis"]
    original = handler.prepare_external
    entered, release = Event(), Event()
    results, failures = [], []

    def blocked(snapshot):
        entered.set()
        assert release.wait(20), "Preparation probe did not release extraction"
        return original(snapshot)

    def work():
        try:
            results.append(live.ctx.jobs.run_once())
        except BaseException as exc:
            failures.append(exc)

    handler.prepare_external = blocked
    thread = Thread(target=work, daemon=True)
    try:
        thread.start()
        assert entered.wait(10), "Analysis did not reach external preparation"
        polled = live.client.get(f"/v1/jobs/{job_id}")
        assert polled.status_code == 200, polled.text
        assert polled.json()["data"]["status"] == "running"
        assert live.client.get(f"/v1/jobs/{job_id}").status_code == 429
        with live.ctx.db.transaction() as session:
            _short_lock_wait(session)
            for model, identity in ((Material, material_id), (Job, job_id)):
                result = session.execute(
                    update(model).where(model.id == identity).values(updated_at=model.updated_at)
                )
                assert result.rowcount == 1
        release.set()
        thread.join(15)
        assert not thread.is_alive()
        if failures:
            raise failures[0]
        assert results == [True]
        with live.ctx.db.transaction() as session:
            assert session.get(Job, job_id).status == "succeeded"
    finally:
        release.set()
        thread.join(26)
        handler.prepare_external = original


def assert_mysql_delete_does_not_gap_lock_first_complete(live):
    """An empty audit-ID snapshot must not block a first analysis Job insert."""
    assert live.ctx.db.engine.dialect.name == "mysql"
    material_id = e2e.upload(live)
    paused, release = Event(), Event()
    results, failures = [], []

    def after_snapshot(conn, cursor, statement, parameters, context, executemany):
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if (
            not paused.is_set()
            and statement.lstrip().startswith("SELECT ai_jobs.id")
            and "ai_jobs.resource_id" in statement
            and material_id in values
        ):
            paused.set()
            assert release.wait(20), "First complete did not finish during audit snapshot"

    def remove():
        try:
            with httpx.Client(
                base_url=str(live.client.base_url), headers=e2e.OWNER, timeout=25, trust_env=False
            ) as client:
                results.append(client.delete(f"/v1/materials/{material_id}"))
        except BaseException as exc:
            failures.append(exc)

    engine = live.ctx.db.engine
    event.listen(engine, "after_cursor_execute", after_snapshot)
    thread = Thread(target=remove, daemon=True)
    try:
        thread.start()
        assert paused.wait(10), "Delete did not read its initially empty audit list"
        queued = e2e.post(live, f"/v1/materials/{material_id}/complete", {"model_id": e2e.MODEL})
        assert queued.status_code == 202, queued.text
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        if failures:
            raise failures[0]
        assert len(results) == 1 and results[0].status_code == 409
        assert results[0].json()["error"]["code"] == "MATERIAL_IN_USE"
        with live.ctx.db.transaction() as session:
            material = session.get(Material, material_id)
            assert material is not None and material.processing_status == "queued"
            job_id = queued.json()["data"]["job"]["id"]
            assert material.latest_job_id == job_id and session.get(Job, job_id).status == "queued"
        assert live.ctx.jobs.run_once()
    finally:
        release.set()
        thread.join(26)
        event.remove(engine, "after_cursor_execute", after_snapshot)


def assert_unknown_job_audit_survives_material_deletion(live):
    material_id = e2e.upload(live)
    queued = e2e.post(live, f"/v1/materials/{material_id}/complete", {"model_id": e2e.MODEL})
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["data"]["job"]["id"]
    before = len(live.provider.calls)
    live.provider.fail_next = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    assert live.ctx.jobs.run_once()
    removed = live.client.delete(f"/v1/materials/{material_id}")
    assert removed.status_code == 204, removed.text
    audited = live.client.get(f"/v1/jobs/{job_id}")
    assert audited.status_code == 200 and audited.json()["data"]["status"] == "outcome_unknown"
    assert audited.json()["data"]["result_url"] is None
    with live.ctx.db.transaction() as session:
        assert session.get(Material, material_id) is None
        job = session.get(Job, job_id)
        assert job is not None and job.request_payload == {} and job.credential_ref is None
        assert not job.retryable and job.dispatched_at is not None
    restarted = e2e.post(
        live,
        f"/v1/jobs/{job_id}/restart",
        {"model_id": e2e.MODEL, "acknowledge_possible_duplicate_cost": True},
    )
    assert restarted.status_code == 409 and restarted.json()["error"]["code"] == "STALE_JOB"
    assert len(live.provider.calls) == before + 1
