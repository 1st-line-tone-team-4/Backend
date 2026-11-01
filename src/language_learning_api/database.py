from collections.abc import Generator

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from language_learning_api.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_session_factory():
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
