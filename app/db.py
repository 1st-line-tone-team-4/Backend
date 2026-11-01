from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from .models import Base


def _run_callbacks(callbacks, outcome):
    failed = False
    for entry in callbacks:
        callback = entry[outcome]
        if callback is not None:
            try:
                callback()
            except Exception:
                # Never propagate vault/SQL exception bodies or skip later cleanup.
                failed = True
    if failed:
        raise RuntimeError("Transaction cleanup could not be completed") from None


class Database:
    def __init__(self, url):
        opts = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            opts["connect_args"] = {"check_same_thread": False, "timeout": 5}
            if url in ("sqlite://", "sqlite:///:memory:"):
                opts["poolclass"] = StaticPool
        self.engine = create_engine(url, **opts)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def configure(connection, record):
                connection.create_function(
                    "char_length",
                    1,
                    lambda value: len(value) if value is not None else None,
                    deterministic=True,
                )
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

            @event.listens_for(self.engine, "savepoint")
            def begin_before_savepoint(connection, name):
                # Python's SQLite legacy mode does not BEGIN for a SELECT or
                # SAVEPOINT. Releasing the first savepoint would commit its
                # writes outside the caller's outer transaction otherwise.
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql("BEGIN")

        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_schema(self):
        """For isolated development and tests only; not a production migration."""
        Base.metadata.create_all(self.engine)

    def on_transaction_end(self, session, *, commit=None, rollback=None, uncertain=None):
        """Register external side effects for this transaction/savepoint's outcome.

        A successful savepoint promotes its callbacks to its parent. Only the
        outer commit may retire a credential; known rollback discards new ones.
        A failed COMMIT acknowledgement has a separate, uncertain outcome.
        """
        callbacks = session.info.get("outcome_callbacks")
        if callbacks is None or not session.in_transaction():
            raise RuntimeError("Database.transaction is required for cleanup callbacks")
        callbacks.append(
            {
                "transaction": session.get_nested_transaction() or session.get_transaction(),
                "commit": commit,
                "rollback": rollback,
                "uncertain": uncertain,
            }
        )

    @contextmanager
    def transaction(self):
        with self.session_factory() as session:
            callbacks = session.info["outcome_callbacks"] = []

            @event.listens_for(session, "after_commit")
            def promote_savepoint(current):
                nested = current.get_nested_transaction()
                if nested is not None:
                    for entry in callbacks:
                        if entry["transaction"] is nested:
                            entry["transaction"] = nested.parent

            @event.listens_for(session, "after_soft_rollback")
            def discard_savepoint(current, previous):
                if previous.nested:
                    discarded = [entry for entry in callbacks if entry["transaction"] is previous]
                    callbacks[:] = [entry for entry in callbacks if entry["transaction"] is not previous]
                    _run_callbacks(discarded, "rollback")

            commit_started = False
            try:
                with session.begin():
                    yield session
                    # Flush failures are known rollbacks. Once COMMIT begins,
                    # a lost acknowledgement cannot prove that it rolled back.
                    session.flush()
                    commit_started = True
            except BaseException:
                _run_callbacks(callbacks, "uncertain" if commit_started else "rollback")
                raise
            else:
                # Kept outside the rollback block: a cleanup failure cannot
                # undo a committed transaction or delete its new credential.
                _run_callbacks(callbacks, "commit")
            finally:
                callbacks.clear()
                session.info.pop("outcome_callbacks", None)
