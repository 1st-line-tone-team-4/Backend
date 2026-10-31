import base64
from contextlib import closing
import gc
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from app import credentials
from app.credentials import Credentials


ENCRYPTION_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
SYNTHETIC_KEY = "synthetic-vault-cleanup-key"


class TrackingConnection(sqlite3.Connection):
    transaction_open_at_close = None

    def close(self):
        self.transaction_open_at_close = self.in_transaction
        super().close()


@pytest.fixture
def connections(monkeypatch):
    original_connect = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        db = original_connect(*args, factory=TrackingConnection, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr(credentials.sqlite3, "connect", connect)
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        # Strong references also prevent reference counting from hiding leaks.
        yield opened
    finally:
        for db in opened:
            sqlite3.Connection.close(db)
        if gc_was_enabled:
            gc.enable()


def assert_closed(connections):
    assert connections
    for db in connections:
        assert db.transaction_open_at_close is False
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.cursor()


def test_operations_close_before_temporary_directory_cleanup(tmp_path, monkeypatch, connections):
    now = [1000.0]
    monkeypatch.setattr(credentials.time, "time", lambda: now[0])
    with TemporaryDirectory(dir=tmp_path) as directory:
        vault = Credentials(Path(directory) / "vault.db", ENCRYPTION_KEY)
        assert len(connections) == 1
        assert_closed(connections)

        ref = vault.put(SYNTHETIC_KEY)
        assert_closed(connections)
        assert vault.get(ref) == SYNTHETIC_KEY
        assert_closed(connections)
        assert vault.get("missing") is None
        assert_closed(connections)

        vault.delete(ref)
        assert_closed(connections)
        assert vault.get(ref) is None
        assert_closed(connections)

        ref = vault.put(SYNTHETIC_KEY)
        assert_closed(connections)
        now[0] += 900
        vault.purge()
        assert_closed(connections)
        assert vault.get(ref) is None
        assert_closed(connections)

        count = len(connections)
        assert vault.get(None) is None
        vault.delete(None)
        assert len(connections) == count

    # On Windows this raises WinError 32 if a SQLite handle remains open.
    assert not Path(directory).exists()
    assert_closed(connections)


@pytest.mark.parametrize("statement", ["PRAGMA", "CREATE TABLE"])
def test_initialization_failure_closes_connection(tmp_path, monkeypatch, connections, statement):
    def execute(db, sql, *args, **kwargs):
        if sql.startswith(statement):
            raise sqlite3.OperationalError("synthetic initialization failure")
        return sqlite3.Connection.execute(db, sql, *args, **kwargs)

    monkeypatch.setattr(TrackingConnection, "execute", execute)
    with pytest.raises(sqlite3.OperationalError, match="synthetic initialization failure"):
        Credentials(tmp_path / "vault.db", ENCRYPTION_KEY)
    assert len(connections) == 1
    assert_closed(connections)


@pytest.mark.parametrize(
    ("operation", "statement"),
    [("put", "INSERT"), ("get", "SELECT"), ("purge", "DELETE"), ("delete", "DELETE")],
)
def test_sql_failure_rolls_back_changes_and_closes(tmp_path, monkeypatch, connections, operation, statement):
    now = [1000.0]
    monkeypatch.setattr(credentials.time, "time", lambda: now[0])
    vault = Credentials(tmp_path / "vault.db", ENCRYPTION_KEY)
    refs = [vault.put(SYNTHETIC_KEY), vault.put(SYNTHETIC_KEY)]
    now[0] += 900

    def execute(db, sql, *args, **kwargs):
        result = sqlite3.Connection.execute(db, sql, *args, **kwargs)
        if sql.startswith(statement):
            # Fail after SQL executes, so pending writes must be rolled back.
            raise sqlite3.OperationalError("synthetic SQL failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(TrackingConnection, "execute", execute)
        with pytest.raises(sqlite3.OperationalError, match="synthetic SQL failure"):
            if operation == "put":
                vault.put(SYNTHETIC_KEY)
            elif operation == "get":
                vault.get(refs[0])
            elif operation == "purge":
                vault.purge()
            else:
                vault.delete(refs[0])
        assert_closed(connections)

    now[0] = 1000.0
    assert all(vault.get(ref) == SYNTHETIC_KEY for ref in refs)
    with closing(sqlite3.connect(vault.path)) as db:
        assert db.execute("SELECT count(*) FROM credentials").fetchone()[0] == 2
    assert_closed(connections)


def test_commit_failure_rolls_back_and_closes(tmp_path, monkeypatch, connections):
    vault = Credentials(tmp_path / "vault.db", ENCRYPTION_KEY)
    ref = vault.put(SYNTHETIC_KEY)
    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)

        def authorize(action, argument, *_):
            if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(credentials.sqlite3, "connect", connect)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            vault.delete(ref)
        assert_closed(connections)

    assert vault.get(ref) == SYNTHETIC_KEY
    assert_closed(connections)


def test_tamper_rejection_closes_read_and_terminal_delete(tmp_path, connections):
    vault = Credentials(tmp_path / "vault.db", ENCRYPTION_KEY)
    ref = vault.put(SYNTHETIC_KEY)
    with closing(sqlite3.connect(vault.path)) as db, db:
        db.execute("UPDATE credentials SET expires_at=expires_at+1 WHERE ref=?", (ref,))

    count = len(connections)
    assert vault.get(ref) is None
    assert len(connections) == count + 2
    assert_closed(connections)
    assert vault.get(ref) is None
    assert_closed(connections)
