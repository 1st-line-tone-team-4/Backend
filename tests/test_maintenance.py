from types import SimpleNamespace
from datetime import timedelta

from app.core import uid, utcnow
from app.db import Database
from app.models import StorageDeletion
from app.maintenance import drain_deletions


def test_outbox_retries_failure_and_replays_already_deleted(tmp_path):
    db = Database("sqlite:///" + (tmp_path / "outbox.sqlite3").as_posix())
    db.create_schema()
    target = tmp_path / "source"
    target.write_bytes(b"private lecture")

    class Storage:
        fail = True

        def delete(self, key):
            if self.fail:
                raise OSError("temporary private storage failure")
            target.unlink(missing_ok=True)

    storage = Storage()
    ctx = SimpleNamespace(db=db, storage=storage)
    rid = uid()
    with db.transaction() as s:
        s.add(StorageDeletion(id=rid, storage_key="source", delete_after=utcnow(), attempt_count=0))
    assert drain_deletions(ctx) == 0 and target.exists()
    with db.transaction() as s:
        row = s.get(StorageDeletion, rid)
        assert row.attempt_count == 1 and row.deleted_at is None
        row.delete_after = utcnow() - timedelta(seconds=1)
    storage.fail = False
    # Simulate crash after file removal but before the successful DB commit.
    target.unlink()
    assert drain_deletions(ctx) == 1
    assert drain_deletions(ctx) == 0
    with db.transaction() as s:
        row = s.get(StorageDeletion, rid)
        assert row.deleted_at is not None and row.attempt_count == 2
