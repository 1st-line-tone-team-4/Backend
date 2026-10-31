"""Local regressions for MySQL boundary handling, without a live DB or provider."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
from pymysql.err import OperationalError as MySQLError
import pytest
from sqlalchemy import MetaData, String, select
from sqlalchemy.exc import OperationalError

from app.auth import JWTAuthenticator
from app.core import ApiError, Settings, error_response, idempotent, uid, utcnow
from app.db import Database
from app.models import IdempotencyRequest, User


def signed_auth(db, issuer="boundary-issuer"):
    """Generate a fresh fixture key; no real authentication material is used."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    auth = JWTAuthenticator(
        Settings(auth_public_key=public, auth_issuer=issuer, auth_audience="boundary-audience"), db
    )

    def request(subject):
        token = jwt.encode(
            {
                "iss": issuer,
                "aud": "boundary-audience",
                "sub": subject,
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            private,
            algorithm="RS256",
        )
        return SimpleNamespace(headers={"Authorization": "Bearer " + token})

    return auth, request


@pytest.mark.parametrize(
    "stored_issuer,stored_subject,subject,allowed",
    [
        ("boundary-issuer", "subject", "subject", True),
        ("boundary-issuer", "subject ", "subject ", True),
        ("boundary-issuer", "subject", "subject ", False),
        ("boundary-issuer", "subject ", "subject", False),
        ("boundary-issuer ", "subject", "subject", False),
    ],
)
def test_auth_rejects_collation_aliases(tmp_path, stored_issuer, stored_subject, subject, allowed):
    db = Database("sqlite:///" + str(tmp_path / "identity.sqlite3"))
    # SQLite RTRIM reproduces the relevant PAD SPACE equality. The live MySQL
    # checks also exercise the actual utf8mb4_bin fixture schema.
    table = User.__table__.to_metadata(MetaData())
    for name in ("auth_issuer", "auth_subject"):
        table.c[name].type = String(255, collation="RTRIM")
    table.create(db.engine)
    user_id = uid()
    try:
        with db.transaction() as session:
            session.add(
                User(id=user_id, auth_issuer=stored_issuer, auth_subject=stored_subject, created_at=utcnow())
            )
        with db.transaction() as session:
            # Prove the SQL lookup returns the alias, instead of relying on a
            # nonmatching query that would pass even without the auth fix.
            assert (
                session.scalar(
                    select(User.id).where(User.auth_issuer == "boundary-issuer", User.auth_subject == subject)
                )
                == user_id
            )
        auth, request = signed_auth(db)
        if allowed:
            assert auth.authenticate(request(subject)) == user_id
        else:
            with pytest.raises(ApiError) as exc:
                auth.authenticate(request(subject))
            assert (exc.value.status, exc.value.code) == (401, "UNAUTHORIZED")
    finally:
        db.engine.dispose()


@pytest.fixture
def context(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "claims.sqlite3"))
    db.create_schema()
    user_id = uid()
    with db.transaction() as session:
        session.add(User(id=user_id, auth_issuer="fixture", auth_subject=user_id, created_at=utcnow()))
    try:
        yield SimpleNamespace(db=db, user=user_id)
    finally:
        db.engine.dispose()


class FaultDatabase:
    """Inject DBAPI errors around real transactions and verify rollback boundaries."""

    def __init__(self, db, *, phase="claim", failures=1, code=1213, dialect="mysql"):
        self.db = db
        self.engine = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self.phase, self.failures, self.code = phase, failures, code
        self.sessions = []

    def fail(self):
        self.failures -= 1
        raise OperationalError("fixture SQL", {}, MySQLError(self.code, "Injected database failure"))

    @contextmanager
    def transaction(self):
        with self.db.transaction() as session:
            self.sessions.append(session)
            flush = session.flush
            flush_count = 0

            def flush_with_failure(*args, **kwargs):
                nonlocal flush_count
                # Ignore automatic no-op flushes from query execution. The
                # failure follows an actual claim insert/completion update.
                pending = bool(session.new or session.dirty)
                result = flush(*args, **kwargs)
                if pending:
                    flush_count += 1
                    target = 1 if self.phase == "claim" else 2
                    if self.phase != "committed" and flush_count == target and self.failures:
                        self.fail()
                return result

            session.flush = flush_with_failure
            yield session
        if self.phase == "committed" and self.failures:
            # Simulate a lost commit acknowledgement: data committed, caller
            # received an error, and the callback must never run a second time.
            self.fail()


def invoke(context, action):
    return idempotent(context, context.user, "boundaryClaim", "fixture-claim-key", {"value": 1}, action)


def test_mysql_claim_deadlock_retries_a_new_transaction_before_action(context):
    fault = FaultDatabase(context.db, failures=2)
    calls = []
    response = invoke(
        SimpleNamespace(db=fault, user=context.user),
        lambda session: (calls.append(session) or 201, {"id": "only-action"}),
    )
    assert response.status_code == 201
    assert len(fault.sessions) == len(set(fault.sessions)) == 3
    assert calls == [fault.sessions[-1]]
    with context.db.transaction() as session:
        rows = session.scalars(select(IdempotencyRequest)).all()
        assert len(rows) == 1 and rows[0].status == "completed"
    replay = invoke(context, lambda session: pytest.fail("Replay reran the callback"))
    assert (replay.status_code, replay.body) == (response.status_code, response.body)


def test_mysql_claim_deadlock_exhaustion_is_bounded_and_has_retry_after(context):
    fault = FaultDatabase(context.db, failures=10)
    with pytest.raises(ApiError) as exc:
        invoke(SimpleNamespace(db=fault, user=context.user), lambda session: pytest.fail("Action ran"))
    assert len(fault.sessions) == 3
    assert (exc.value.status, exc.value.code) == (409, "REQUEST_IN_PROGRESS")
    response = error_response(SimpleNamespace(state=SimpleNamespace()), exc.value)
    assert response.headers["Retry-After"] == "2"
    with context.db.transaction() as session:
        assert session.scalar(select(IdempotencyRequest)) is None


@pytest.mark.parametrize(
    "dialect,code", [("mysql", 1205), ("mysql", 2006), ("mysql", 2013), ("sqlite", 1213)]
)
def test_other_operational_errors_are_not_retried(context, dialect, code):
    fault = FaultDatabase(context.db, code=code, dialect=dialect)
    with pytest.raises(OperationalError):
        invoke(SimpleNamespace(db=fault, user=context.user), lambda session: pytest.fail("Action ran"))
    assert len(fault.sessions) == 1
    with context.db.transaction() as session:
        assert session.scalar(select(IdempotencyRequest)) is None


@pytest.mark.parametrize("phase", ["action", "completion", "committed"])
def test_deadlock_after_action_starts_never_repeats_callback(context, phase):
    fault = FaultDatabase(context.db, phase=phase, failures=1)
    calls = []

    def action(session):
        calls.append(session)
        if phase == "action":
            fault.fail()
        return 201, {"id": "one-side-effect"}

    with pytest.raises(OperationalError):
        invoke(SimpleNamespace(db=fault, user=context.user), action)
    assert len(fault.sessions) == len(calls) == 1
    with context.db.transaction() as session:
        row = session.scalar(select(IdempotencyRequest))
        if phase == "committed":
            assert row.status == "completed"
        else:
            assert row is None


def test_replay_commit_failure_is_not_retried(context):
    invoke(context, lambda session: (201, {"id": "already-completed"}))
    fault = FaultDatabase(context.db, phase="committed")
    with pytest.raises(OperationalError):
        invoke(SimpleNamespace(db=fault, user=context.user), lambda session: pytest.fail("Replay ran action"))
    assert len(fault.sessions) == 1
