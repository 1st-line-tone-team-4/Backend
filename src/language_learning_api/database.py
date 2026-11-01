"""Compatibility dependency; all domains use the application's single database."""

from fastapi import Request
from app.core import get_context
from app.models import Base  # noqa: F401


def get_db(request: Request):
    with get_context(request).db.session_factory() as session:
        yield session
