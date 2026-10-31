from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from .models import Base


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

        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_schema(self):
        """For isolated development and tests only; not a production migration."""
        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        with self.session_factory() as session:
            with session.begin():
                yield session
