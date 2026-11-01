"""Wire/digest fixtures captured from the original route implementations.

These literal cursors were generated before extraction, using a synthetic key.
They pin compatibility for persisted client cursors and idempotent requests.
"""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select
from starlette.requests import Request

from app import content, jobs, materials, providers
from app.core import ApiError, _idempotency_digest, request_envelope, strict_json
from app.db import Database
from app.models import Material, User
from app.pagination import (
    content_page,
    decode_content_cursor,
    decode_cursor,
    decode_material_cursor,
    decode_opaque_cursor,
    encode_content_cursor,
    encode_cursor,
    encode_material_cursor,
    encode_opaque_cursor,
)
from language_learning_api.routers import common, teachback
from language_learning_api.schemas import TeachbackSessionInput

OWNER = "11111111-1111-4111-8111-111111111111"
IDENTITY = "22222222-2222-4222-8222-222222222222"
SECOND = "33333333-3333-4333-8333-333333333333"
FOURTH = "44444444-4444-4444-8444-444444444444"
SECRET = "pagination-compat-fixture-secret-2026"
STAMP = datetime(2026, 1, 2, 3, 4, 5, 123000)
SCOPE = {"user": OWNER, "resource": "question_sets", "task_id": None}
FILTERS = {"owner": OWNER, "operation": "listSessionRecords", "status": "active", "label": "\ud55c"}
MESSAGE_FILTERS = {"owner": OWNER, "operation": "listTeachbackMessages", "session_id": IDENTITY}
QUERY = "tag=one&tag=two&q=%ED%95%9C&space=a+b&empty="
QUERY_DICT = {"tag": "two", "q": "\ud55c", "space": "a b", "empty": ""}
KEY = "compat-fixture-idempotency-key"

MATERIAL_CURSOR = (
    "eyJhcmNoaXZlZCI6dHJ1ZSwiaWQiOiIyMjIyMjIyMi0yMjIyLTQyMjItODIyMi0yMjIyMjIyMjIyMjIiLCJ0aW1lIjoi"
    "MjAyNi0wMS0wMlQwMzowNDowNS4xMjMwMDAiLCJ1c2VyIjoiMTExMTExMTEtMTExMS00MTExLTgxMTEtMTExMTExMTEx"
    "MTExIn0uZWMxMDVmMmMyYmQ1ZWQ2NjIyOTg4MzEzMDE2YzM2NzRkODY5MGIzNWRlN2Y1NTc1OTIxNWUwNjcwNmU0NTRkYg"
)
CONTENT_CURSOR = (
    "eyJpZCI6IjIyMjIyMjIyLTIyMjItNDIyMi04MjIyLTIyMjIyMjIyMjIyMiIsInNjb3BlIjp7InJlc291cmNlIjoicXVlc3Rp"
    "b25fc2V0cyIsInRhc2tfaWQiOm51bGwsInVzZXIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEifSwidGlt"
    "ZXN0YW1wIjoiMjAyNi0wMS0wMlQwMzowNDowNS4xMjMwMDAifS4xM2ZkMzQ0NDM3N2ZmODQ2YTBiODJhMmQyZWYxMjA1"
    "OGE4MWI5MmE5YTIyYmQyNDA4Y2I0ZGEyNDM1NTE0MDMy"
)
A_TIMESTAMP_CURSOR = (
    "eyJmaWx0ZXJfaGFzaCI6IjU2MjQ2NDU3NDA0NWU5MTNlMjAxOWQ4NjQzNDg4YmM1MDUyZTM5ODAzNmQxMTI2YWEzMTFjMjEw"
    "ZTI4NWQ4MzUiLCJwYXlsb2FkIjp7ImNyZWF0ZWRfYXQiOiIyMDI2LTAxLTAxVDE4OjA0OjA1LjEyMzAwMCswMDowMCIsImlk"
    "IjoiMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyIn19."
    "adf43ef865291196a52830f2dd502f77a56643d1fcad8f8c5324f6e5fb3d17a2"
)
A_OPAQUE_CURSOR = (
    "eyJmaWx0ZXJfaGFzaCI6IjBjNzAyM2E4MzhmZTlmMDVjZTZiM2NiY2VjYTY2NDlkNDYwZWQ3M2U0ZGQ3NWYxMzRhMjQzZmI5"
    "ZjY5OWFiNjYiLCJwYXlsb2FkIjp7InNlcXVlbmNlX25vIjozfX0."
    "e9cdd65f4b34e55d9f4e2879a9d7a8887420c9780399bffe7932dc4bc06a266e"
)


def request_for(ctx=None):
    ctx = ctx or SimpleNamespace(auth=SimpleNamespace(authenticate=lambda request: OWNER))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/materials",
            "query_string": QUERY.encode(),
            "headers": [(b"x-ai-key", b"synthetic-compat-key"), (b"idempotency-key", KEY.encode())],
            "app": SimpleNamespace(state=SimpleNamespace(context=ctx)),
        }
    )


@pytest.fixture
def cursor_db():
    db = Database("sqlite:///:memory:")
    db.create_schema()
    with db.transaction() as session:
        session.add(User(id=OWNER, auth_issuer="fixture", auth_subject="fixture", created_at=STAMP))
        session.flush()
        for identity, stamp in ((IDENTITY, STAMP), (SECOND, STAMP - timedelta(seconds=1))):
            session.add(
                Material(
                    id=identity,
                    user_id=OWNER,
                    filename="fixture.txt",
                    mime_type="text/plain",
                    size_bytes=4,
                    storage_key=identity,
                    processing_status="ready",
                    archived=False,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
    yield db
    db.engine.dispose()


def test_original_material_and_content_cursor_bytes_survive_route_extraction(cursor_db):
    ctx = SimpleNamespace(
        auth=SimpleNamespace(authenticate=lambda request: OWNER),
        db=cursor_db,
        storage=SimpleNamespace(secret=SECRET.encode()),
    )
    page = materials.listing(request_for(ctx), None, 1, True)
    assert page["next_cursor"] == MATERIAL_CURSOR
    assert encode_material_cursor(STAMP, IDENTITY, OWNER, True, SECRET.encode()) == MATERIAL_CURSOR
    assert decode_material_cursor(MATERIAL_CURSOR, OWNER, True, SECRET.encode()) == (STAMP, IDENTITY)
    with cursor_db.transaction() as session:
        stmt = select(Material).order_by(Material.created_at.desc(), Material.id.desc())
        rows, cursor = content._page(session, stmt, Material, 1, None, SECRET, SCOPE)
        assert [row.id for row in rows] == [IDENTITY]
        assert cursor == CONTENT_CURSOR
    assert encode_content_cursor(STAMP, IDENTITY, SECRET, SCOPE) == CONTENT_CURSOR
    assert content._cursor_decode(CONTENT_CURSOR, SECRET, SCOPE) == (STAMP, IDENTITY)


def test_original_a_cursor_bytes_and_compatibility_exports():
    local = STAMP.replace(tzinfo=timezone(timedelta(hours=9)))
    assert common.encode_cursor(local, IDENTITY, FILTERS, secret=SECRET) == A_TIMESTAMP_CURSOR
    assert decode_cursor(A_TIMESTAMP_CURSOR, FILTERS, secret=SECRET) == (STAMP - timedelta(hours=9), IDENTITY)
    assert common.encode_opaque_cursor({"sequence_no": 3}, MESSAGE_FILTERS, secret=SECRET) == A_OPAQUE_CURSOR
    assert decode_opaque_cursor(A_OPAQUE_CURSOR, MESSAGE_FILTERS, secret=SECRET) == {"sequence_no": 3}
    assert common.encode_cursor is encode_cursor
    assert common.decode_cursor is decode_cursor
    assert common.encode_opaque_cursor is encode_opaque_cursor
    assert common.decode_opaque_cursor is decode_opaque_cursor
    assert content._page is content_page and content._cursor_decode is decode_content_cursor


@pytest.mark.parametrize("family", ["material", "content", "a"])
def test_tampered_cursor_and_changed_owner_filter_keep_exact_errors(family):
    if family == "material":
        token = MATERIAL_CURSOR

        def decode(value, owner=OWNER, archived=True):
            return decode_material_cursor(value, owner, archived, SECRET.encode())

        wrong_scopes = [lambda: decode(token, "other"), lambda: decode(token, OWNER, False)]
    elif family == "content":
        token = CONTENT_CURSOR

        def decode(value, scope=SCOPE):
            return decode_content_cursor(value, SECRET, scope)

        wrong_scopes = [
            lambda: decode(token, {**SCOPE, "user": "other"}),
            lambda: decode(token, {**SCOPE, "task_id": IDENTITY}),
        ]
    else:
        token = A_TIMESTAMP_CURSOR

        def decode(value, filters=FILTERS):
            return decode_cursor(value, filters, secret=SECRET)

        wrong_scopes = [
            lambda: decode(token, {**FILTERS, "owner": "other"}),
            lambda: decode(token, {**FILTERS, "status": "completed"}),
            lambda: decode(token, {**FILTERS, "operation": "different"}),
        ]
    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    for operation in [lambda: decode(tampered), *wrong_scopes]:
        with pytest.raises(ApiError) as caught:
            operation()
        assert (caught.value.status, caught.value.code, caught.value.message) == (
            422,
            "INVALID_CURSOR",
            "Cursor is invalid for this request." if family == "a" else "Invalid cursor.",
        )


def embedded_fixture(value):
    payload = json.dumps(value).encode()
    signature = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest().encode()
    return base64.urlsafe_b64encode(payload + b"." + signature).decode()


def test_existing_timestamp_validation_and_empty_cursor_differences_are_preserved():
    aware = STAMP.replace(tzinfo=timezone.utc)
    material = embedded_fixture(
        {"user": OWNER, "archived": True, "time": aware.isoformat(), "id": "legacy-id", "extra": 1}
    )
    assert decode_material_cursor(material, OWNER, True, SECRET.encode()) == (aware, "legacy-id")
    for changes in ({"timestamp": aware.isoformat()}, {"id": "legacy-id"}, {"extra": 1}):
        content_token = embedded_fixture(
            {"timestamp": STAMP.isoformat(), "id": IDENTITY, "scope": SCOPE, **changes}
        )
        with pytest.raises(ApiError):
            decode_content_cursor(content_token, SECRET, SCOPE)
    assert decode_content_cursor("", SECRET, SCOPE) is None
    assert decode_content_cursor(None, SECRET, SCOPE) is None
    for operation in (
        lambda: decode_material_cursor("", OWNER, True, SECRET.encode()),
        lambda: decode_cursor("", FILTERS, secret=SECRET),
    ):
        with pytest.raises(ApiError):
            operation()
    assert decode_cursor(encode_cursor(STAMP, IDENTITY, secret=SECRET), secret=SECRET) == (STAMP, IDENTITY)


def test_a_cursor_limits_and_signed_value_validation_stay_unchanged():
    for malformed in (None, "x" * 4097, "a.b.c", "%%%25." + "0" * 64):
        with pytest.raises(ApiError) as caught:
            decode_opaque_cursor(malformed, {}, secret=SECRET)
        assert caught.value.code == "INVALID_CURSOR"
    with pytest.raises(ValueError, match="at least 32 bytes"):
        encode_opaque_cursor({}, {}, secret="short")
    with pytest.raises(ValueError, match="too large"):
        encode_opaque_cursor({"source": "x" * 4096}, {}, secret=SECRET)
    for payload in ({"created_at": 12, "id": IDENTITY}, {"created_at": STAMP.isoformat(), "id": IDENTITY}):
        token = encode_opaque_cursor(payload, {}, secret=SECRET)
        with pytest.raises(ApiError):
            decode_cursor(token, {}, secret=SECRET)


@pytest.mark.parametrize("family", ["material", "content"])
def test_tied_timestamp_pagination_survives_deleted_boundary(cursor_db, family):
    with cursor_db.transaction() as session:
        session.get(Material, SECOND).created_at = STAMP
        session.add(
            Material(
                id=FOURTH,
                user_id=OWNER,
                filename="four.txt",
                mime_type="text/plain",
                size_bytes=4,
                storage_key=FOURTH,
                processing_status="ready",
                archived=False,
                created_at=STAMP,
                updated_at=STAMP,
            )
        )

    def page(cursor):
        if family == "material":
            ctx = SimpleNamespace(
                auth=SimpleNamespace(authenticate=lambda request: OWNER),
                db=cursor_db,
                storage=SimpleNamespace(secret=SECRET.encode()),
            )
            response = materials.listing(request_for(ctx), cursor, 2, True)
            return [row["id"] for row in response["data"]], response["next_cursor"]
        with cursor_db.transaction() as session:
            stmt = (
                select(Material)
                .where(Material.user_id == OWNER)
                .order_by(Material.created_at.desc(), Material.id.desc())
            )
            rows, nxt = content._page(session, stmt, Material, 2, cursor, SECRET, SCOPE)
            return [row.id for row in rows], nxt

    ids, cursor = page(None)
    assert ids == [FOURTH, SECOND] and cursor
    with cursor_db.transaction() as session:
        session.delete(session.get(Material, SECOND))
    ids, cursor = page(cursor)
    assert ids == [IDENTITY] and cursor is None


def test_request_envelopes_reproduce_captured_idempotency_digests():
    request = request_for()
    body = {"model_id": "test:grounded", "label": "\ud55c", "nullable": None}
    model = jobs.ModelBody(model_id="test:grounded")
    teach = TeachbackSessionInput(
        concept_version_id=IDENTITY, explanation_language="ko", student_persona="curious_beginner"
    )
    shapes = {
        "materials": (
            materials.reqdata(request, body),
            "7b891dba6ad8ce2d0da92c8776556b8b5f058fca64695705b457d71c86f0621a",
        ),
        "content": (
            request_envelope(request, body, query_as_dict=True),
            "f13162466dfb29ee7e6f2649b49d7c84d4607977119cb88c5170933ac06dba99",
        ),
        "jobs": (
            jobs.request_data(request, model),
            "5f3f58ef2b80538783fdfb788a0d06ecd26bcf796d75303b33db7f490af57941",
        ),
        "teachback": (
            teachback.request_data(request, teach),
            "34a6aae6c17e3d2ea874a4e8b5e6b3c65872aa8f0901142b17373e6ce155ecb8",
        ),
        "a_attempt": (
            {"path": request.url.path, "body": body},
            "be880e5abab2081946e4984b471bd221d8da68c6f8cc299bcb25ceeebf7bccd8",
        ),
        "a_plan": (
            {"method": "POST", "path": "/v1/study-plans", "query": dict(request.query_params), "body": body},
            "601f88d567da6a04e2a1ed6ef6cf45ff7fbc3af033d20af46d46edc19e056cd7",
        ),
    }
    for name, (envelope, digest) in shapes.items():
        assert _idempotency_digest(KEY, envelope) == digest, name
    assert shapes["materials"][0]["query"] == QUERY
    assert shapes["content"][0]["query"] == QUERY_DICT
    assert shapes["materials"][0]["body"] is body
    assert shapes["teachback"][0]["body"]["previous_session_id"] is None


def test_routes_pass_the_original_envelope_shapes_to_idempotency(monkeypatch):
    request = request_for()
    captured = []

    def capture(ctx, owner, operation, key, data, action):
        captured.append((operation, data))
        return data

    for module in (content, materials, jobs, teachback):
        monkeypatch.setattr(module, "idempotent", capture)
    upload = materials.Upload(filename="fixture.txt", mime_type="text/plain", size_bytes=4)
    assert materials.create(upload, request, KEY)["query"] == QUERY
    explanation = content.ExplanationInput(
        concept_version_id=IDENTITY, explanation_language="ko", model_id="test:grounded"
    )
    assert (
        content.create_explanation(explanation, request, KEY, "synthetic-compat-key")["query"] == QUERY_DICT
    )
    assert captured[-1][1]["body"] == {
        "concept_version_id": IDENTITY,
        "explanation_language": "ko",
        "model_id": "test:grounded",
    }
    assert jobs.retry_job(UUID(IDENTITY), jobs.ModelBody(model_id="test:grounded"), request)["query"] == QUERY
    teach = TeachbackSessionInput(
        concept_version_id=IDENTITY, explanation_language="ko", student_persona="curious_beginner"
    )
    assert teachback.create_session(teach, request)["body"] == teach.model_dump(mode="json")


def test_strict_json_remains_a_provider_compatibility_export():
    assert providers.strict_json is strict_json
    assert strict_json(b'{"items":[1,true,null],"text":"\\ud55c"}') == {
        "items": [1, True, None],
        "text": "\ud55c",
    }
    for raw, message in (
        (b'{"id":1,"id":2}', "Duplicate JSON key"),
        (b'{"value":NaN}', "Non-JSON number"),
        (b'{"value":Infinity}', "Non-JSON number"),
    ):
        with pytest.raises(ValueError, match=message):
            strict_json(raw)
