"""Real local JWT verification and integrated A cursor trust-boundary tests."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, local
from types import SimpleNamespace

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import event, func, select

from app.auth import JWTAuthenticator
from app.core import ApiError, Settings, uid, utcnow
from app.db import Database
from app.main import create_app
from app.models import ExamGoal, User
from language_learning_api.routers.common import (
    decode_cursor,
    decode_opaque_cursor,
    encode_cursor,
    encode_opaque_cursor,
)
from tests.support import FixtureProvider


@pytest.fixture(scope="module")
def signing_keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


def sign(signing_keys, *, subject="verified-user", changes=None, omit=(), algorithm="RS256"):
    claims = {
        "iss": "https://identity.test",
        "aud": "learning-test",
        "sub": subject,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    claims.update(changes or {})
    for key in omit:
        claims.pop(key, None)
    key = signing_keys[0]
    if algorithm.startswith("HS"):
        key = "test-only-hmac-key-that-is-never-a-production-key"
    elif algorithm == "none":
        key = ""
    return jwt.encode(claims, key, algorithm=algorithm)


def request(token, scheme="Bearer"):
    return SimpleNamespace(headers={"Authorization": f"{scheme} {token}"})


@pytest.fixture
def auth_env(tmp_path, signing_keys):
    settings = Settings(
        profile="test",
        database_url="sqlite:///" + (tmp_path / "auth.sqlite3").as_posix(),
        storage_root=str(tmp_path / "private"),
        public_base_url="http://testserver",
        signing_secret="test-only-cursor-secret-" * 3,
        credential_encryption_key=Fernet.generate_key().decode(),
        auth_public_key=signing_keys[1],
        auth_issuer="https://identity.test",
        auth_audience="learning-test",
    )
    app = create_app(settings, providers=FixtureProvider(), create_schema=True)
    return SimpleNamespace(app=app, ctx=app.state.context, client=TestClient(app), settings=settings)


def count_users(db):
    with db.transaction() as session:
        return session.scalar(select(func.count()).select_from(User))


def test_real_jwt_first_login_provisions_profile_once(auth_env, signing_keys):
    fx = auth_env
    token = sign(signing_keys, changes={"email": "learner@example.test", "name": "Learner"})
    headers = {"Authorization": "bearer " + token}
    assert count_users(fx.ctx.db) == 0
    first = fx.client.get("/v1/me", headers=headers)
    assert first.status_code == 200
    user = first.json()["data"]
    assert user["email"] == "learner@example.test" and user["display_name"] == "Learner"
    assert user["timezone"] == "Asia/Seoul"
    assert fx.client.get("/v1/me", headers=headers).json() == first.json()
    changed = sign(signing_keys, changes={"email": "changed@example.test", "name": "Changed"})
    assert fx.client.get("/v1/me", headers={"Authorization": "Bearer " + changed}).json() == first.json()
    assert count_users(fx.ctx.db) == 1
    with fx.ctx.db.transaction() as db:
        stored = db.get(User, user["id"])
        assert stored.auth_subject == "verified-user" and stored.auth_issuer == fx.settings.auth_issuer


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://attacker.test"},
        {"aud": "different-app"},
        {"exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        {"nbf": datetime.now(timezone.utc) + timedelta(minutes=5)},
        {"sub": ""},
        {"sub": "x" * 256},
        {"sub": 123},
        {"sub": None},
    ],
)
def test_invalid_claims_do_not_provision_and_return_sanitized_error(auth_env, signing_keys, changes):
    token = sign(signing_keys, changes=changes)
    response = auth_env.client.get("/v1/me", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401 and response.json()["error"]["code"] == "UNAUTHORIZED"
    assert token not in response.text
    assert count_users(auth_env.ctx.db) == 0


@pytest.mark.parametrize("claim", ["iss", "aud", "exp", "sub"])
def test_required_identity_claims_cannot_be_omitted(auth_env, signing_keys, claim):
    with pytest.raises(ApiError) as error:
        auth_env.ctx.auth.authenticate(request(sign(signing_keys, omit=(claim,))))
    assert error.value.status == 401 and count_users(auth_env.ctx.db) == 0


@pytest.mark.parametrize("algorithm", ["HS256", "RS384", "none"])
def test_unapproved_jwt_algorithms_cannot_provision(auth_env, signing_keys, algorithm):
    with pytest.raises(ApiError) as error:
        auth_env.ctx.auth.authenticate(request(sign(signing_keys, algorithm=algorithm)))
    assert error.value.status == 401 and count_users(auth_env.ctx.db) == 0


def test_wrong_signature_and_malformed_authorization_are_rejected(auth_env, signing_keys):
    different_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad_signature = sign((different_key, None))
    valid = sign(signing_keys)
    for header in (
        "",
        "Basic " + valid,
        "Bearer",
        "Bearer  " + valid,
        "Bearer " + valid + " suffix",
        "Bearer broken.token",
        "Bearer " + bad_signature,
    ):
        response = auth_env.client.get("/v1/me", headers={"Authorization": header})
        assert response.status_code == 401
    assert count_users(auth_env.ctx.db) == 0


def test_invalid_optional_profile_claims_are_ignored(auth_env, signing_keys):
    owner = auth_env.ctx.auth.authenticate(
        request(sign(signing_keys, changes={"email": {"injected": True}, "name": "x" * 121}))
    )
    with auth_env.ctx.db.transaction() as db:
        user = db.get(User, owner)
        assert user.email is None and user.display_name is None


@pytest.mark.parametrize(
    "email, expected",
    [
        ("", None),
        ("not-email", None),
        ("@", None),
        ("missing-domain@", None),
        ("@example.test", None),
        ("a@@example.test", None),
        ("a b@example.test", None),
        ("a@example.test\r\nBcc: victim@example.test", None),
        ("learner@example.test", "learner@example.test"),
        ("learner+practice@example.test", "learner+practice@example.test"),
        ('"quoted local"@example.test', '"quoted local"@example.test'),
        ("learner@localhost", "learner@localhost"),
    ],
)
def test_optional_email_claims_preserve_identity_and_me_contract(auth_env, signing_keys, email, expected):
    token = sign(signing_keys, changes={"email": email})
    response = auth_env.client.get("/v1/me", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    profile = response.json()["data"]
    assert profile["email"] == expected
    contract = json.loads(
        (
            Path(__file__).resolve().parents[1] / "contracts" / "openapi-1.4.0-monthly-subscription.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(
        contract["components"]["schemas"]["UserProfile"], format_checker=FormatChecker()
    ).validate(profile)
    assert count_users(auth_env.ctx.db) == 1
    with auth_env.ctx.db.transaction() as db:
        user = db.get(User, profile["id"])
        assert user.auth_subject == "verified-user" and user.email == expected


def initial_lookup_barrier(db):
    """Force both real transactions to observe a missing user before either inserts."""
    gate, seen = Barrier(2), local()

    def on_execute(state):
        result = state.invoke_statement()
        if state.is_select and not getattr(seen, "looked_up", False):
            seen.looked_up = True
            gate.wait(timeout=10)
        return result

    event.listen(db.session_factory, "do_orm_execute", on_execute, retval=True)
    return on_execute


def test_concurrent_first_login_converges_on_one_identity(auth_env, signing_keys):
    fx, token = auth_env, sign(signing_keys, subject="concurrent-new-user")
    listener = initial_lookup_barrier(fx.ctx.db)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            owners = list(pool.map(lambda _: fx.ctx.auth.authenticate(request(token)), range(2)))
    finally:
        event.remove(fx.ctx.db.session_factory, "do_orm_execute", listener)
    assert owners[0] == owners[1] and count_users(fx.ctx.db) == 1


def alias_database(tmp_path):
    """Exercise a SQL collation that aliases case/trailing spaces, as some deployments do."""
    db = Database("sqlite:///" + (tmp_path / "identity-alias.sqlite3").as_posix())

    @event.listens_for(db.engine, "connect")
    def configure(connection, _):
        def compare(left, right):
            a, b = left.rstrip().casefold(), right.rstrip().casefold()
            return (a > b) - (a < b)

        connection.create_collation("IDENTITY_ALIAS", compare)

    with db.engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TABLE users (
            id VARCHAR(36) PRIMARY KEY, auth_issuer TEXT COLLATE IDENTITY_ALIAS NOT NULL,
            auth_subject TEXT COLLATE IDENTITY_ALIAS NOT NULL, email VARCHAR(320),
            display_name VARCHAR(120), timezone VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL,
            UNIQUE(auth_issuer, auth_subject))""")
    return db


@pytest.mark.parametrize("subject", ["member", "Member "])
def test_database_collation_alias_cannot_authenticate_existing_subject(tmp_path, signing_keys, subject):
    db = alias_database(tmp_path)
    auth = JWTAuthenticator(
        Settings(
            auth_public_key=signing_keys[1],
            auth_issuer="https://identity.test",
            auth_audience="learning-test",
        ),
        db,
    )
    legitimate = auth.authenticate(request(sign(signing_keys, subject="Member")))
    with pytest.raises(ApiError) as error:
        auth.authenticate(request(sign(signing_keys, subject=subject)))
    assert error.value.status == 401 and count_users(db) == 1
    assert auth.authenticate(request(sign(signing_keys, subject="Member"))) == legitimate


def test_colliding_subject_race_never_returns_another_identity(tmp_path, signing_keys):
    db = alias_database(tmp_path)
    auth = JWTAuthenticator(
        Settings(
            auth_public_key=signing_keys[1],
            auth_issuer="https://identity.test",
            auth_audience="learning-test",
        ),
        db,
    )
    listener = initial_lookup_barrier(db)

    def authenticate(subject):
        try:
            return subject, auth.authenticate(request(sign(signing_keys, subject=subject)))
        except ApiError as error:
            return subject, error

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(authenticate, ["Member", "member"]))
    finally:
        event.remove(db.session_factory, "do_orm_execute", listener)
    successes = [(subject, result) for subject, result in results if isinstance(result, str)]
    failures = [(subject, result) for subject, result in results if isinstance(result, ApiError)]
    assert len(successes) == len(failures) == 1 and failures[0][1].status == 401
    with db.transaction() as session:
        user = session.get(User, successes[0][1])
        assert user.auth_subject == successes[0][0]
    assert count_users(db) == 1


def test_cursor_roundtrip_normalizes_utc_and_binds_owner_operation_filters():
    secret, identity = "test-only-cursor-key-" * 3, uid()
    local_time = datetime(2026, 1, 2, 9, 10, 11, 123000, timezone(timedelta(hours=9)))
    filters = {"owner": "one", "operation": "listStudyPlans", "status": "active", "exam_goal_id": uid()}
    cursor = encode_cursor(local_time, identity, filters, secret=secret)
    assert decode_cursor(cursor, dict(reversed(list(filters.items()))), secret=secret) == (
        datetime(2026, 1, 2, 0, 10, 11, 123000),
        identity,
    )
    for changed in (
        {**filters, "owner": "two"},
        {**filters, "operation": "listExamGoals"},
        {**filters, "status": "superseded"},
        {**filters, "exam_goal_id": None},
    ):
        with pytest.raises(ApiError) as error:
            decode_cursor(cursor, changed, secret=secret)
        assert error.value.code == "INVALID_CURSOR"
    with pytest.raises(ApiError):
        decode_cursor(cursor, filters, secret="another-test-cursor-key-" * 3)


@pytest.mark.parametrize(
    "malformed", ["", "missing-separator", "a.b.c", "%%%25." + "0" * 64, "a." + "0" * 64, "x" * 4097, None]
)
def test_malformed_or_oversize_opaque_cursor_is_rejected(malformed):
    with pytest.raises(ApiError) as error:
        decode_opaque_cursor(malformed, {}, secret="test-only-cursor-key-" * 3)
    assert error.value.status == 422 and error.value.code == "INVALID_CURSOR"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"created_at": "2026-01-01T00:00:00", "id": "bad"},
        {"created_at": "2026-01-01T00:00:00+00:00", "id": "not-a-uuid"},
        {"created_at": 42, "id": "00000000-0000-4000-8000-000000000001"},
        {"created_at": "2026-01-01T00:00:00+00:00", "id": 42},
    ],
)
def test_signed_cursor_still_requires_a_timestamp_and_uuid(payload):
    secret = "test-only-cursor-key-" * 3
    cursor = encode_opaque_cursor(payload, {}, secret=secret)
    with pytest.raises(ApiError) as error:
        decode_cursor(cursor, {}, secret=secret)
    assert error.value.code == "INVALID_CURSOR"


def test_payload_or_signature_tampering_cannot_move_page_boundary():
    secret = "test-only-cursor-key-" * 3
    cursor = encode_cursor(utcnow(), uid(), {"owner": "one"}, secret=secret)
    encoded, signature = cursor.split(".")
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    modified = base64.urlsafe_b64encode(raw.replace(b'"one"', b'"two"')).decode().rstrip("=")
    # Owner is represented by a filter digest, so mutate the actual payload too.
    modified = ("A" if modified[0] != "A" else "B") + modified[1:]
    for altered in (
        modified + "." + signature,
        encoded + "." + ("0" if signature[0] != "0" else "1") + signature[1:],
    ):
        with pytest.raises(ApiError):
            decode_cursor(altered, {"owner": "one"}, secret=secret)
    with pytest.raises(ValueError):
        encode_opaque_cursor({"large": "x" * 4096}, {}, secret=secret)


def test_real_jwt_goal_pages_follow_tied_timestamps_and_reject_cross_owner_cursor(auth_env, signing_keys):
    fx = auth_env
    owner_headers = {"Authorization": "Bearer " + sign(signing_keys)}
    other_headers = {"Authorization": "Bearer " + sign(signing_keys, subject="other-user")}
    owner = fx.client.get("/v1/me", headers=owner_headers).json()["data"]["id"]
    other = fx.client.get("/v1/me", headers=other_headers).json()["data"]["id"]
    stamp = datetime(2026, 1, 1, 0, 0, 0)
    ids = [uid() for _ in range(5)]
    with fx.ctx.db.transaction() as db:
        for i, identity in enumerate(ids):
            db.add(
                ExamGoal(
                    id=identity,
                    user_id=owner,
                    title=f"Goal {i}",
                    exam_type="other",
                    exam_date=datetime.now(timezone.utc).date() + timedelta(days=10),
                    status="active",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
        db.add(
            ExamGoal(
                id=uid(),
                user_id=other,
                title="Other private goal",
                exam_type="other",
                exam_date=datetime.now(timezone.utc).date() + timedelta(days=10),
                status="active",
                created_at=stamp,
                updated_at=stamp,
            )
        )
    first = fx.client.get("/v1/exam-goals", headers=owner_headers, params={"limit": 2})
    assert first.status_code == 200
    first_cursor = first.json()["next_cursor"]
    found, page = [], first.json()
    while True:
        found.extend(item["id"] for item in page["data"])
        if page["next_cursor"] is None:
            break
        reply = fx.client.get(
            "/v1/exam-goals", headers=owner_headers, params={"limit": 1, "cursor": page["next_cursor"]}
        )
        assert reply.status_code == 200
        page = reply.json()
    assert found == sorted(ids, reverse=True) and len(set(found)) == 5
    foreign = fx.client.get("/v1/exam-goals", headers=other_headers, params={"cursor": first_cursor})
    assert foreign.status_code == 422 and foreign.json()["error"]["code"] == "INVALID_CURSOR"
    wrong_route = fx.client.get(
        "/v1/records/sessions", headers=owner_headers, params={"cursor": first_cursor}
    )
    assert wrong_route.status_code == 422
    for malformed in ("invalid", "x" * 1025):
        assert (
            fx.client.get("/v1/exam-goals", headers=owner_headers, params={"cursor": malformed}).status_code
            == 422
        )
