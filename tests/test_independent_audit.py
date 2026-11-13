"""Independent adversarial checks; every provider call uses MockTransport."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import hmac
import sqlite3
from threading import Barrier
from types import SimpleNamespace

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import select, func

from app.core import ApiError, Settings, idempotent, uid, utcnow
from app.credentials import Credentials
from app.db import Database
from app.models import Material, MaterialChunk, Job, User
from app.providers import ProviderFailure, ProviderRegistry
from app.storage import LocalStorage


@pytest.mark.parametrize(
    "status,code,retryable,unknown",
    [
        (401, "PROVIDER_AUTH", True, False),
        (403, "PROVIDER_AUTH", True, False),
        (429, "PROVIDER_RATE_LIMIT", True, False),
        (408, "TIMEOUT_UNKNOWN", False, True),
        (409, "TIMEOUT_UNKNOWN", False, True),
        (500, "TIMEOUT_UNKNOWN", False, True),
        (503, "TIMEOUT_UNKNOWN", False, True),
        (400, "PROVIDER_UNAVAILABLE", False, False),
    ],
)
def test_provider_response_classification_does_not_echo_secret(status, code, retryable, unknown):
    secret = "audit-secret-never-echo"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": secret})

    registry = ProviderRegistry(
        Settings(profile="test", enabled_models=("openai:test-model",)),
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(ProviderFailure) as exc:
        registry.generate("openai:test-model", secret, "analysis", {"chunks": []})
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.openai.com/v1/chat/completions"
    assert exc.value.code == code
    assert exc.value.retryable is retryable
    assert exc.value.outcome_unknown is unknown
    assert secret not in str(exc.value)


@pytest.mark.parametrize(
    "error,retryable,unknown",
    [
        (httpx.ConnectTimeout, True, False),
        (httpx.ReadTimeout, False, True),
        (httpx.WriteError, False, True),
        (httpx.RemoteProtocolError, False, True),
    ],
)
def test_transport_uncertainty_never_becomes_safe_retry(error, retryable, unknown):
    def respond(request):
        raise error("sensitive upstream diagnostic", request=request)

    registry = ProviderRegistry(
        Settings(profile="test", enabled_models=("openai:test-model",)),
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(ProviderFailure) as exc:
        registry.generate("openai:test-model", "audit-secret", "analysis", {})
    assert exc.value.retryable is retryable
    assert exc.value.outcome_unknown is unknown
    assert "sensitive" not in str(exc.value)


def test_vault_expiry_removes_ciphertext_and_plaintext_is_absent(tmp_path):
    path = tmp_path / "vault.db"
    vault = Credentials(path, Fernet.generate_key())
    key = "unique-plaintext-audit-secret"
    ref = vault.put(key)
    assert key.encode() not in path.read_bytes()
    assert vault.get(ref) == key
    with sqlite3.connect(path) as db:
        db.execute("UPDATE credentials SET expires_at=0 WHERE ref=?", (ref,))
    assert vault.get(ref) is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM credentials").fetchone()[0] == 0


def test_signed_storage_cannot_change_owner_resource_or_purpose(tmp_path):
    storage = LocalStorage(
        Settings(
            profile="test",
            storage_root=str(tmp_path),
            public_base_url="http://testserver",
            signing_secret="s" * 32,
        )
    )
    material = SimpleNamespace(id=uid(), user_id=uid())
    url, _ = storage.sign(material, "get", 60)
    token = url.split("token=")[1]
    assert storage.verify(token, material.id, "get") == material.user_id
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    for forged, mid, purpose in [
        (token, uid(), "get"),
        (token, material.id, "put"),
        (
            base64.urlsafe_b64encode(raw.replace(material.user_id, uid()).encode()).decode(),
            material.id,
            "get",
        ),
    ]:
        with pytest.raises(ApiError) as exc:
            storage.verify(forged, mid, purpose)
        assert exc.value.status == 403


def test_concurrent_idempotency_body_conflict_has_one_domain_write(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "audit.db"))
    db.create_schema()
    user = uid()
    with db.transaction() as session:
        session.add(User(id=user, auth_issuer="audit", auth_subject=user, created_at=utcnow()))
    ctx = SimpleNamespace(db=db)
    barrier = Barrier(2)

    def run(value):
        def action(session):
            now = utcnow()
            material = Material(
                id=uid(),
                user_id=user,
                filename=value,
                mime_type="text/plain",
                size_bytes=1,
                storage_key=uid(),
                processing_status="upload_pending",
                created_at=now,
                updated_at=now,
            )
            session.add(material)
            return 201, {"id": material.id}

        barrier.wait()
        try:
            return idempotent(
                ctx, user, "auditCreate", "same-idempotency-key", {"body": value}, action
            ).status_code
        except ApiError as exc:
            assert exc.code == "IDEMPOTENCY_CONFLICT"
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(run, ["one", "two"]))
    assert sorted(statuses) == [201, 409]
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Material)) == 1


def test_content_rejects_timezone_cursor_before_comparison():
    from app.content import _cursor_decode

    secret, scope = "audit-signing-secret", ["sets", uid()]
    payload = json.dumps({"timestamp": "2026-01-01T00:00:00+00:00", "id": uid(), "scope": scope}).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest().encode()
    cursor = base64.urlsafe_b64encode(payload + b"." + signature).decode()
    with pytest.raises(ApiError) as exc:
        _cursor_decode(cursor, secret, scope)
    assert exc.value.code == "INVALID_CURSOR"


def test_cost_acknowledgement_requires_json_boolean():
    from app.content import SetInput
    from app.jobs import RestartBody
    from pydantic import ValidationError

    for value in (1, "true", "yes"):
        with pytest.raises(ValidationError):
            RestartBody(model_id="x", acknowledge_possible_duplicate_execution=value)
        with pytest.raises(ValidationError):
            SetInput(
                unit_id=uid(),
                concept_version_ids=[uid()],
                count=1,
                difficulty="easy",
                model_id="x",
                acknowledge_possible_duplicate_execution=value,
            )


def _queued_analysis(tmp_path):
    from fastapi.testclient import TestClient
    from tests.support import test_application as application_fixture

    env = application_fixture(tmp_path)
    headers = {"Authorization": "Bearer test-owner-token", "Idempotency-Key": uid()}
    with TestClient(env.app) as client:
        text = b"Momentum is conserved when total external force is zero."
        response = client.post(
            "/v1/materials",
            headers=headers,
            json={"filename": "audit.txt", "mime_type": "text/plain", "size_bytes": len(text)},
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert (
            client.put(data["upload_url"], headers={"Content-Type": "text/plain"}, content=text).status_code
            == 204
        )
        headers.update({"Idempotency-Key": uid()})
        response = client.post(
            "/v1/materials/" + data["material"]["id"] + "/complete",
            headers=headers,
            json={"model_id": env.provider.model_id},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["data"]["job"]["id"]
    return env, job_id, data["material"]["id"]


def test_cancel_during_provider_call_fences_late_success(tmp_path):
    env, job_id, material_id = _queued_analysis(tmp_path)
    generate = env.provider.generate

    def racing_generate(*args):
        result = generate(*args)
        env.ctx.jobs.cancel(job_id=job_id, user_id=env.auth.tokens["test-owner-token"])
        return result

    env.provider.generate = racing_generate
    assert env.ctx.jobs.run_once()
    with env.ctx.db.transaction() as session:
        job = session.get(Job, job_id)
        assert job.status == "outcome_unknown"
        assert job.credential_ref is None and job.result_url is None
        assert session.get(Material, material_id).processing_status == "failed"
        assert session.scalar(select(func.count()).select_from(MaterialChunk)) == 0
    assert not env.ctx.jobs.run_once()
    assert len(env.provider.calls) == 1


def test_missing_service_key_fails_before_provider_dispatch(tmp_path):
    env, job_id, material_id = _queued_analysis(tmp_path)
    env.provider.service_key = lambda model_id: None  # Operator has not configured the key.
    assert env.ctx.jobs.run_once()
    with env.ctx.db.transaction() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed" and job.retryable
        assert job.error_code == "CREDENTIAL_UNAVAILABLE"
        assert job.dispatched_at is None and job.credential_ref is None
        assert session.get(Material, material_id).processing_status == "failed"
    assert env.provider.calls == []
