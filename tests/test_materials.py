from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from app.core import Settings, uid, utcnow, ApiError
from app.db import Database
from app.models import User, Material, MaterialChunk, Job
from app.materials import AnalysisHandler, owned, extract
from app.storage import LocalStorage


@pytest.fixture
def env(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "test.db"))
    db.create_schema()
    user = uid()
    other = uid()
    mid = uid()
    jid = uid()
    key = uid()
    now = utcnow()
    with db.transaction() as s:
        for i in (user, other):
            s.add(User(id=i, auth_issuer="i", auth_subject=i, timezone="Asia/Seoul", created_at=now))
        s.flush()
        s.add(
            Material(
                id=mid,
                user_id=user,
                filename="x.txt",
                mime_type="text/plain",
                size_bytes=5,
                storage_key=key,
                processing_status="upload_pending",
                archived=False,
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()
        s.add(
            Job(
                id=jid,
                user_id=user,
                kind="analysis",
                status="queued",
                resource_type="material",
                resource_id=mid,
                request_fingerprint="f" * 64,
                request_payload={},
                model_id="test",
                attempt_no=1,
                retryable=False,
                fencing_token=0,
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()
        s.get(Material, mid).latest_job_id = jid
    settings = Settings(
        profile="test",
        storage_root=str(tmp_path / "objects"),
        public_base_url="http://testserver",
        signing_secret="x" * 32,
    )
    ctx = SimpleNamespace(db=db, storage=LocalStorage(settings))
    ctx.storage.write(key, b"hello")
    return ctx, user, other, mid, jid


def test_storage_owner_purpose_expiry_and_validation(env):
    ctx, user, other, mid, jid = env
    with ctx.db.transaction() as s:
        m = owned(s, user, mid)
        with pytest.raises(ApiError):
            owned(s, other, mid)
        url, _ = ctx.storage.sign(m, "get", -1)
        token = url.split("token=")[1]
        with pytest.raises(ApiError):
            ctx.storage.verify(token, mid, "get")
        url, _ = ctx.storage.sign(m, "put", 300)
        token = url.split("token=")[1]
        with pytest.raises(ApiError):
            ctx.storage.verify(token, mid, "get")
        with pytest.raises(ApiError):
            extract(m, b"\xff" * 5)
        assert extract(m, b"hello") == ["hello"]


def test_invalid_output_and_stale_job_leave_no_chunks(env):
    ctx, user, other, mid, jid = env
    handler = AnalysisHandler(ctx)
    with ctx.db.transaction() as s:
        job = s.get(Job, jid)
        prompt = handler.prepare(s, job)
        bad = {
            "units": [
                {
                    "title": "Unit",
                    "concepts": [
                        {
                            "name": "N",
                            "summary": "S",
                            "checklist_items": ["C"],
                            "evidence": [{"chunk_id": prompt["chunks"][0]["id"], "quote": "invented"}],
                        }
                    ],
                }
            ]
        }
        with pytest.raises(ApiError) as exc:
            handler.persist(s, job, bad)
        assert exc.value.code == "INVALID_OUTPUT"
        assert s.scalar(select(func.count()).select_from(MaterialChunk)) == 0
        job.id = jid  # keep identity
        fake = SimpleNamespace(id=uid(), resource_id=mid, user_id=user)
        with pytest.raises(ApiError) as exc:
            handler.persist(s, fake, bad)
        assert exc.value.code == "STALE_JOB"


def test_analysis_persists_grounded_output_atomically_and_confirmation_conflicts(env):
    from app.materials import confirm, Confirm
    from app.models import Concept, ConceptVersion, MaterialUnit

    ctx, user, other, mid, jid = env
    ctx.auth = SimpleNamespace(authenticate=lambda request: user)
    handler = AnalysisHandler(ctx)
    with ctx.db.transaction() as s:
        job = s.get(Job, jid)
        chunk = handler.prepare(s, job)["chunks"][0]
        output = {
            "units": [
                {
                    "title": "Unit",
                    "concepts": [
                        {
                            "name": "N",
                            "summary": "S",
                            "checklist_items": ["C"],
                            "evidence": [{"chunk_id": chunk["id"], "quote": "hello"}],
                        }
                    ],
                }
            ]
        }
        handler.persist(s, job, output)
        handler.set_state(s, job, "succeeded")
    with ctx.db.transaction() as s:
        concept = s.scalar(select(Concept))
        version = s.scalar(select(ConceptVersion))
        unit = s.scalar(select(MaterialUnit))
        ids = (concept.id, version.id, unit.id, chunk["id"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(context=ctx)))

    def payload(base):
        return Confirm(
            concepts=[
                {
                    "concept_id": ids[0],
                    "base_version_id": base,
                    "unit_id": ids[2],
                    "name": "Edited",
                    "summary": "Summary",
                    "checklist_items": ["Explain"],
                    "evidence_chunk_ids": [ids[3]],
                }
            ]
        )

    result = confirm(mid, payload(ids[1]), request)
    assert result["data"][0]["version"]["version_no"] == 2
    with pytest.raises(ApiError) as exc:
        confirm(mid, payload(ids[1]), request)
    assert exc.value.code == "VERSION_CONFLICT"
    with ctx.db.transaction() as s:
        assert s.get(ConceptVersion, ids[1]).name == "N"
        assert s.scalar(select(func.count()).select_from(ConceptVersion)) == 2


def test_http_upload_and_private_access(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core import api_error_handler
    from app.materials import router

    ctx, user, other, mid, jid = env
    ctx.auth = SimpleNamespace(authenticate=lambda request: user)
    app = FastAPI()
    app.state.context = ctx
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(router, prefix="/v1")
    with TestClient(app) as client:
        response = client.post(
            "/v1/materials",
            headers={"Idempotency-Key": "i" * 16},
            json={"filename": "new.txt", "mime_type": "text/plain", "size_bytes": 5},
        )
        assert response.status_code == 201
        data = response.json()["data"]
        url = data["upload_url"]
        new_id = data["material"]["id"]
        assert client.put(url, content=b"\xff" * 5, headers={"Content-Type": "text/plain"}).status_code == 422
        assert client.put(url, content=b"hello", headers={"Content-Type": "text/plain"}).status_code == 204
        assert client.put(url, content=b"hello", headers={"Content-Type": "text/plain"}).status_code == 409
        assert client.get(url).status_code == 403
        ctx.auth = SimpleNamespace(authenticate=lambda request: other)
        assert client.get("/v1/materials/" + new_id).status_code == 404


def test_image_preparation_and_grounded_transcription(env):
    import io
    from PIL import Image

    ctx, user, other, mid, jid = env
    handler = AnalysisHandler(ctx)
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buf, format="PNG")
    data = buf.getvalue()
    with ctx.db.transaction() as s:
        m = s.get(Material, mid)
        m.mime_type = "image/png"
        m.size_bytes = len(data)
        ctx.storage.delete(m.storage_key)
        ctx.storage.write(m.storage_key, data)
        job = s.get(Job, jid)
        prompt = handler.prepare(s, job)
        assert prompt["image_data_url"].startswith("data:image/png;base64,")
        output = {
            "source_text": "A visible sentence",
            "units": [
                {
                    "title": "U",
                    "concepts": [
                        {
                            "name": "N",
                            "summary": "S",
                            "checklist_items": ["C"],
                            "evidence": [{"chunk_id": prompt["chunk_id"], "quote": "visible"}],
                        }
                    ],
                }
            ],
        }
        handler.persist(s, job, output)
        assert s.get(MaterialChunk, prompt["chunk_id"]).content == "A visible sentence"


def test_strict_body_cursor_and_path_validation(env):
    from pydantic import ValidationError
    from app.materials import Upload, Archive, listing, detail

    ctx, user, other, mid, jid = env
    ctx.auth = SimpleNamespace(authenticate=lambda request: user)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(context=ctx)))
    with pytest.raises(ValidationError):
        Upload(filename="x", mime_type="text/plain", size_bytes=True)
    with pytest.raises(ValidationError):
        Archive(archived="true")
    with pytest.raises(ApiError) as exc:
        listing(request, cursor="bad", limit=20, include_archived=False)
    assert exc.value.status == 422
    with pytest.raises(ApiError) as exc:
        detail("bad", request)
    assert exc.value.status == 422


def test_material_and_concept_response_match_contract(env):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from app.materials import mdto, version_dto
    from app.models import ConceptVersion

    ctx, user, other, mid, jid = env
    handler = AnalysisHandler(ctx)
    contract = json.loads(
        Path("contracts/openapi-1.4.0-monthly-subscription.json").read_text(encoding="utf-8")
    )

    def check(name, value):
        schema = {"$ref": "#/components/schemas/" + name, "components": contract["components"]}
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(value)

    with ctx.db.transaction() as s:
        job = s.get(Job, jid)
        chunk = handler.prepare(s, job)["chunks"][0]
        handler.persist(
            s,
            job,
            {
                "units": [
                    {
                        "title": "U",
                        "concepts": [
                            {
                                "name": "N",
                                "summary": "S",
                                "checklist_items": ["C"],
                                "evidence": [{"chunk_id": chunk["id"], "quote": "hello"}],
                            }
                        ],
                    }
                ]
            },
        )
        check("Material", mdto(s.get(Material, mid)))
        check("ConceptVersion", version_dto(s, s.scalar(select(ConceptVersion))))


def test_limits_and_deletion_outbox(env):
    from datetime import timedelta
    from app.materials import Confirm, confirm, remove
    from app.models import Concept, ConceptVersion, MaterialUnit, StorageDeletion

    ctx, user, other, mid, jid = env
    ctx.auth = SimpleNamespace(authenticate=lambda request: user)
    ctx.task_gateway = SimpleNamespace(material_in_use=lambda *args: False)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(context=ctx)))
    unit, concept, chunk = uid(), uid(), uid()
    now = utcnow()
    with ctx.db.transaction() as s:
        s.get(Material, mid).processing_status = "ready"
        s.get(Job, jid).status = "failed"
        s.add(MaterialUnit(id=unit, material_id=mid, ordinal=1, title="U"))
        s.flush()
        s.add(
            MaterialChunk(
                id=chunk, material_id=mid, page_number=1, ordinal=1, content="hello", created_at=now
            )
        )
        for i in range(100):
            s.add(Concept(id=concept if i == 0 else uid(), material_id=mid, unit_id=unit, created_at=now))
        s.flush()
        s.add_all(
            [
                ConceptVersion(
                    id=uid(), concept_id=concept, version_no=i, name="N", is_confirmed=False, created_at=now
                )
                for i in range(1, 1001)
            ]
        )
    with ctx.db.transaction() as s:
        base = s.scalar(
            select(ConceptVersion)
            .where(ConceptVersion.concept_id == concept)
            .order_by(ConceptVersion.version_no.desc())
        ).id

    def entry(cid, baseid):
        return {
            "concept_id": cid,
            "base_version_id": baseid,
            "unit_id": unit,
            "name": "N",
            "summary": "S",
            "checklist_items": ["C"],
            "evidence_chunk_ids": [chunk],
        }

    with pytest.raises(ApiError) as exc:
        confirm(mid, Confirm(concepts=[entry(None, None)]), request)
    assert exc.value.code == "CONCEPT_LIMIT"
    with pytest.raises(ApiError) as exc:
        confirm(mid, Confirm(concepts=[entry(concept, base)]), request)
    assert exc.value.code == "VERSION_LIMIT"
    before = utcnow()
    remove(mid, request)
    with ctx.db.transaction() as s:
        row = s.scalar(select(StorageDeletion))
        assert row.delete_after <= utcnow() and row.delete_after >= before - timedelta(seconds=1)


def test_upload_does_not_hold_db_transaction_during_parse(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core import api_error_handler
    import app.materials as materials

    ctx, user, other, mid, jid = env
    ctx.auth = SimpleNamespace(authenticate=lambda request: user)
    app = FastAPI()
    app.state.context = ctx
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(materials.router, prefix="/v1")
    observed = {}
    original = materials.extract_bytes

    def probing_extract(mime, data):
        # A pooled connection or open write transaction here would starve other
        # requests on MySQL and block this concurrent writer on SQLite.
        observed["checked_out"] = ctx.db.engine.pool.checkedout()
        with ctx.db.transaction() as s:
            s.get(Material, mid).updated_at = utcnow()
        return original(mime, data)

    monkeypatch.setattr(materials, "extract_bytes", probing_extract)
    with TestClient(app) as client:
        response = client.post(
            "/v1/materials",
            headers={"Idempotency-Key": "u" * 16},
            json={"filename": "new.txt", "mime_type": "text/plain", "size_bytes": 5},
        )
        url = response.json()["data"]["upload_url"]
        assert client.put(url, content=b"hello", headers={"Content-Type": "text/plain"}).status_code == 204
    assert observed["checked_out"] == 0
