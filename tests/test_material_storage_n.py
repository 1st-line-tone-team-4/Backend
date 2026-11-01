"""N4/N7/N9 failure, race, transaction-occupancy and deletion regressions."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Barrier, Event
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app import materials, storage
from app.core import ApiError, Settings, uid
from app.models import Job, Material, MaterialChunk, StorageDeletion
from app.providers import ProviderFailure
from app.storage import LocalStorage
from tests.support import test_application as make_application


OWNER = {"Authorization": "Bearer test-owner-token"}
MODEL = "test:grounded"
KEY = "test-transient-key"
TEXT = b"Momentum is conserved without external force."


@pytest.fixture
def local(tmp_path):
    return LocalStorage(
        Settings(
            profile="test",
            storage_root=str(tmp_path / "objects"),
            signing_secret="s" * 32,
            public_base_url="http://testserver",
        )
    )


@pytest.fixture
def web(tmp_path):
    fixture = make_application(tmp_path)
    with TestClient(fixture.app, headers=OWNER) as client:
        fixture.client = client
        yield fixture
    fixture.ctx.db.engine.dispose()


def post(web, path, body, key=None):
    return web.client.post(
        path,
        json=body,
        headers={"Idempotency-Key": key or uid(), "X-AI-Key": KEY},
    )


def uploaded(web, *, put=True):
    response = post(
        web,
        "/v1/materials",
        {"filename": "source.txt", "mime_type": "text/plain", "size_bytes": len(TEXT)},
    )
    assert response.status_code == 201, response.text
    item = response.json()["data"]
    if put:
        response = web.client.put(item["upload_url"], content=TEXT, headers={"Content-Type": "text/plain"})
        assert response.status_code == 204, response.text
    return item["material"]["id"], item["upload_url"]


def completed(web, mid, key=None):
    return post(web, f"/v1/materials/{mid}/complete", {"model_id": MODEL}, key)


class WriteProxy:
    def __init__(self, handle, failure):
        self.handle, self.failure, self.writes = handle, failure, 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def write(self, data):
        self.writes += 1
        if self.failure == "partial" and self.writes > 1:
            raise OSError("injected partial write")
        if self.failure == "zero":
            return 0
        return self.handle.write(data[:3])

    def flush(self):
        if self.failure == "flush":
            raise OSError("injected flush")
        return self.handle.flush()

    def fileno(self):
        return self.handle.fileno()


def inject_failure(monkeypatch, failure):
    # Do not patch the process-global os module: parser subprocess pipe setup
    # also uses fdopen, but this injection targets only object storage writes.
    isolated_os = SimpleNamespace(fdopen=storage.os.fdopen, fsync=storage.os.fsync, link=storage.os.link)
    monkeypatch.setattr(storage, "os", isolated_os)
    if failure in ("partial", "flush", "zero", "short"):
        original = storage.os.fdopen
        monkeypatch.setattr(storage.os, "fdopen", lambda *a, **k: WriteProxy(original(*a, **k), failure))
    else:

        def fail(*args, **kwargs):
            raise OSError("injected storage failure")

        monkeypatch.setattr(storage.os, failure, fail)


@pytest.mark.parametrize("failure", ["partial", "zero", "flush", "fsync", "link"])
def test_atomic_upload_failure_never_publishes_and_same_key_can_retry(local, monkeypatch, failure):
    key = uid()
    with monkeypatch.context() as patch:
        inject_failure(patch, failure)
        with pytest.raises(ApiError) as exc:
            local.write(key, TEXT)
        assert exc.value.status == 503
        assert exc.value.code == "UPLOAD_STORAGE_UNAVAILABLE"
        assert not local._path(key).exists()
        assert list(local.root.iterdir()) == []
    local.write(key, TEXT)
    assert local.read(key) == TEXT


def test_short_writes_complete_before_publication(local, monkeypatch):
    inject_failure(monkeypatch, "short")
    key = uid()
    local.write(key, TEXT)
    assert local.read(key) == TEXT
    assert [p.name for p in local.root.iterdir()] == [key]


def test_concurrent_uploads_publish_one_whole_object_without_replacement(local, monkeypatch):
    key = uid()
    barrier = Barrier(2)
    original = storage.os.link

    def linked(*args, **kwargs):
        barrier.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage.os, "link", linked)

    def upload(data):
        try:
            local.write(key, data)
            return 204, data
        except ApiError as exc:
            return exc.status, data

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upload, (b"first" * 1000, b"other" * 1000)))
    assert sorted(status for status, _ in results) == [204, 409]
    assert local.read(key) == next(data for status, data in results if status == 204)
    assert [p.name for p in local.root.iterdir()] == [key]


@pytest.mark.parametrize("failure", ["partial", "fsync", "link"])
def test_http_upload_retries_after_storage_failure(web, monkeypatch, failure):
    mid, url = uploaded(web, put=False)
    with monkeypatch.context() as patch:
        inject_failure(patch, failure)
        response = web.client.put(url, content=TEXT, headers={"Content-Type": "text/plain"})
        assert response.status_code == 503, response.text
        with web.ctx.db.transaction() as session:
            assert session.get(Material, mid).processing_status == "upload_pending"
    response = web.client.put(url, content=TEXT, headers={"Content-Type": "text/plain"})
    assert response.status_code == 204, response.text
    assert web.client.put(url, content=TEXT, headers={"Content-Type": "text/plain"}).status_code == 409


def test_upload_fsync_uses_no_db_connection_and_delete_prevents_publication(web, monkeypatch):
    mid, url = uploaded(web, put=False)
    entered, release = Event(), Event()
    original = storage.os.fsync

    def paused(fd):
        assert web.ctx.db.engine.pool.checkedout() == 0
        entered.set()
        assert release.wait(5)
        return original(fd)

    monkeypatch.setattr(storage.os, "fsync", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(web.client.put, url, content=TEXT, headers={"Content-Type": "text/plain"})
        try:
            assert entered.wait(5)
            assert web.client.delete(f"/v1/materials/{mid}").status_code == 204
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 404
    assert {path.name for path in web.ctx.storage.root.iterdir()} == {".credentials.sqlite3"}


@pytest.mark.parametrize("mutation", ["archive", "object", "state", "owner", "delete"])
def test_complete_parses_outside_transaction_then_revalidates(web, monkeypatch, mutation):
    mid, _ = uploaded(web)
    entered, release = Event(), Event()
    original_read = web.ctx.storage.read

    def read(key):
        assert web.ctx.db.engine.pool.checkedout() == 0
        return original_read(key)

    def parse(mime, data):
        assert web.ctx.db.engine.pool.checkedout() == 0
        entered.set()
        assert release.wait(5)
        return [data.decode()]

    monkeypatch.setattr(web.ctx.storage, "read", read)
    monkeypatch.setattr(materials, "extract_bytes", parse)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(completed, web, mid)
        try:
            assert entered.wait(5)
            # Another idempotent write must finish while extraction is blocked.
            assert (
                post(
                    web,
                    "/v1/materials",
                    {"filename": "other.txt", "mime_type": "text/plain", "size_bytes": 1},
                ).status_code
                == 201
            )
            if mutation == "archive":
                assert web.client.patch(f"/v1/materials/{mid}", json={"archived": True}).status_code == 200
            elif mutation == "delete":
                assert web.client.delete(f"/v1/materials/{mid}").status_code == 204
            else:
                with web.ctx.db.transaction() as session:
                    m = session.get(Material, mid)
                    if mutation == "object":
                        m.storage_key = uid()
                    elif mutation == "state":
                        m.processing_status = "failed"
                    else:
                        m.user_id = web.auth.tokens["test-other-token"]
        finally:
            release.set()
        response = future.result(timeout=5)
        assert response.status_code == (404 if mutation in ("owner", "delete") else 409), response.text
    with web.ctx.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert web.provider.calls == []


def test_complete_replay_needs_no_source_and_preserves_conflict_semantics(web, monkeypatch):
    mid, _ = uploaded(web)
    key = uid()
    initial = completed(web, mid, key)
    assert initial.status_code == 202, initial.text

    def forbidden(*args):
        raise AssertionError("A replay must not read or extract source")

    monkeypatch.setattr(web.ctx.storage, "read", forbidden)
    monkeypatch.setattr(materials, "extract_bytes", forbidden)
    replay = completed(web, mid, key)
    assert replay.status_code == 202 and replay.json() == initial.json()
    conflict = post(web, f"/v1/materials/{mid}/complete", {"model_id": "different"}, key)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_concurrent_complete_same_key_enqueues_once_after_external_parsing(web, monkeypatch):
    mid, _ = uploaded(web)
    key, barrier = uid(), Barrier(2)

    def parse(mime, data):
        barrier.wait(timeout=5)
        return [data.decode()]

    monkeypatch.setattr(materials, "extract_bytes", parse)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: completed(web, mid, key), range(2)))
    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json() == responses[1].json()
    with web.ctx.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_worker_extracts_once_without_connection_and_reuses_grounding_for_persist(web, monkeypatch):
    mid, _ = uploaded(web)
    result = completed(web, mid)
    assert result.status_code == 202, result.text
    jid = result.json()["data"]["job"]["id"]
    reads, parses = [], []
    original = web.ctx.storage.read

    def read(key):
        assert web.ctx.db.engine.pool.checkedout() == 0
        reads.append(key)
        return original(key)

    def parse(mime, data):
        assert web.ctx.db.engine.pool.checkedout() == 0
        parses.append(mime)
        return [data.decode()]

    monkeypatch.setattr(web.ctx.storage, "read", read)
    monkeypatch.setattr(materials, "extract_bytes", parse)
    assert web.ctx.jobs.run_once()
    with web.ctx.db.transaction() as session:
        assert session.get(Job, jid).status == "succeeded"
        assert session.scalar(select(func.count()).select_from(MaterialChunk)) == 1
    assert len(reads) == len(parses) == len(web.provider.calls) == 1


def test_worker_external_parse_allows_cancellation_and_prevents_dispatch(web, monkeypatch):
    mid, _ = uploaded(web)
    response = completed(web, mid)
    jid = response.json()["data"]["job"]["id"]
    entered, release = Event(), Event()

    def parse(mime, data):
        assert web.ctx.db.engine.pool.checkedout() == 0
        entered.set()
        assert release.wait(5)
        return [data.decode()]

    monkeypatch.setattr(materials, "extract_bytes", parse)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(web.ctx.jobs.run_once)
        try:
            assert entered.wait(5)
            # Job lock is free, as is the DB write lock on SQLite.
            result = web.ctx.jobs.cancel(job_id=jid, user_id=web.auth.tokens["test-owner-token"])
            assert result["status"] == "failed"
        finally:
            release.set()
        assert future.result(timeout=5)
    assert web.provider.calls == []
    with web.ctx.db.transaction() as session:
        assert session.get(Job, jid).dispatched_at is None
        assert session.scalar(select(func.count()).select_from(MaterialChunk)) == 0


def test_snapshot_is_immutable_and_object_change_rejected_before_result(web):
    mid, _ = uploaded(web)
    response = completed(web, mid)
    jid = response.json()["data"]["job"]["id"]
    handler = web.ctx.jobs.handlers["analysis"]
    with web.ctx.db.transaction() as session:
        job = session.get(Job, jid)
        snapshot = handler.prepare_snapshot(session, job)
    with pytest.raises(FrozenInstanceError):
        snapshot.storage_key = uid()
    prepared = handler.prepare_external(snapshot)
    with web.ctx.db.transaction() as session:
        session.get(Material, mid).storage_key = uid()
    with web.ctx.db.transaction() as session:
        with pytest.raises(ApiError, match="stale"):
            handler.apply_prepared(session, session.get(Job, jid), prepared)


def test_delete_preserves_terminal_audit_chain_and_redacts_source(web):
    mid, _ = uploaded(web)
    first = completed(web, mid).json()["data"]["job"]["id"]
    web.provider.fail_next = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    assert web.ctx.jobs.run_once()
    restart = {"model_id": MODEL, "acknowledge_possible_duplicate_cost": True}
    response = post(web, f"/v1/jobs/{first}/restart", restart)
    assert response.status_code == 202, response.text
    second = response.json()["data"]["job"]["id"]
    web.provider.fail_next = ProviderFailure("INVALID_OUTPUT", retryable=False)
    assert web.ctx.jobs.run_once()
    response = post(web, f"/v1/jobs/{second}/restart", restart)
    assert response.status_code == 202, response.text
    third = response.json()["data"]["job"]["id"]
    assert web.ctx.jobs.run_once()
    ref = web.ctx.credentials.put(KEY)
    with web.ctx.db.transaction() as session:
        for jid in (first, second, third):
            job = session.get(Job, jid)
            job.request_payload = {"material_id": mid, "evidence": "private source text"}
        session.get(Job, first).credential_ref = ref
        before = {
            jid: (session.get(Job, jid).dispatched_at, session.get(Job, jid).created_at)
            for jid in (first, second, third)
        }
    response = web.client.delete(f"/v1/materials/{mid}")
    assert response.status_code == 204, response.text
    with web.ctx.db.transaction() as session:
        assert session.get(Material, mid) is None
        assert session.scalar(select(func.count()).select_from(MaterialChunk)) == 0
        assert session.scalar(select(func.count()).select_from(StorageDeletion)) == 1
        for jid, status, previous in (
            (first, "outcome_unknown", None),
            (second, "failed", first),
            (third, "succeeded", second),
        ):
            job = session.get(Job, jid)
            assert (job.status, job.previous_job_id) == (status, previous)
            assert (job.dispatched_at, job.created_at) == before[jid]
            assert job.request_payload == {}
            assert job.result_url is None and job.credential_ref is None
            assert job.retryable is False
    assert web.ctx.credentials.get(ref) is None
    audit = web.client.get(f"/v1/jobs/{first}")
    assert audit.status_code == 200 and audit.json()["data"]["status"] == "outcome_unknown"
    assert post(web, f"/v1/jobs/{first}/restart", restart).json()["error"]["code"] == "STALE_JOB"
    assert post(web, f"/v1/jobs/{second}/retry", {"model_id": MODEL}).status_code == 409
    assert (
        web.client.get(f"/v1/jobs/{third}", headers={"Authorization": "Bearer test-other-token"}).status_code
        == 404
    )
    assert len(web.provider.calls) == 3
