"""Optional cross-origin access for a separately hosted frontend."""

import base64
import os

import pytest
from fastapi.testclient import TestClient

from app.core import Settings
from app.main import cors_origins, create_app
from tests.support import FixtureAuth, FixtureProvider, FixtureTasks

FRONTEND = "https://team4-frontend.example"


def application(tmp_path, origins):
    settings = Settings(
        profile="test",
        database_url="sqlite:///" + (tmp_path / "cors.sqlite3").as_posix(),
        storage_root=str(tmp_path / "private"),
        public_base_url="http://testserver",
        signing_secret="test-signing-secret-" * 3,
        credential_encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode(),
        cors_allowed_origins=origins,
    )
    return create_app(
        settings,
        auth=FixtureAuth(),
        providers=FixtureProvider(),
        task_gateway=FixtureTasks(),
        create_schema=True,
    )


def test_configured_origin_gets_preflight_and_response_headers(tmp_path):
    client = TestClient(application(tmp_path, (FRONTEND,)))
    preflight = client.options(
        "/v1/materials",
        headers={
            "Origin": FRONTEND,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == FRONTEND
    allowed = preflight.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "content-type", "idempotency-key"):
        assert header in allowed
    # Contract 1.4.0 removed the learner-supplied X-AI-Key header.
    assert "x-ai-key" not in allowed
    legacy = client.options(
        "/v1/materials",
        headers={
            "Origin": FRONTEND,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-ai-key",
        },
    )
    assert legacy.status_code == 400
    assert "access-control-allow-credentials" not in preflight.headers

    response = client.get("/v1/me", headers={"Origin": FRONTEND})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == FRONTEND
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


def test_unlisted_origin_and_default_configuration_get_no_cors_headers(tmp_path):
    client = TestClient(application(tmp_path, (FRONTEND,)))
    response = client.get("/v1/me", headers={"Origin": "https://other.example"})
    assert "access-control-allow-origin" not in response.headers

    (tmp_path / "default").mkdir()
    default = TestClient(application(tmp_path / "default", ()))
    preflight = default.options(
        "/v1/materials",
        headers={"Origin": FRONTEND, "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.parametrize(
    "origin",
    ["*", "https://team4.example/", "https://team4.example/app", "ftp://team4.example", "team4.example"],
)
def test_origins_must_be_exact(origin):
    with pytest.raises(ValueError):
        cors_origins(Settings(profile="test", cors_allowed_origins=(origin,)))


def test_production_requires_https_origins():
    with pytest.raises(ValueError):
        cors_origins(Settings(profile="production", cors_allowed_origins=("http://team4.example",)))
    assert cors_origins(Settings(profile="production", cors_allowed_origins=(FRONTEND,))) == (FRONTEND,)


def test_environment_list_is_trimmed(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", f" {FRONTEND} , http://127.0.0.1:5173,")
    assert Settings.from_env().cors_allowed_origins == (FRONTEND, "http://127.0.0.1:5173")
