"""Compatibility dependencies backed by the shared verified JWT identity."""

from typing import Annotated
from fastapi import Depends, Request
from sqlalchemy.orm import Session
from app.core import ApiError, get_user
from app.models import User
from .database import get_db


def get_current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    user = db.get(User, get_user(request))
    if user is None:
        raise ApiError(401, "UNAUTHORIZED", "Authentication required.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DatabaseSession = Annotated[Session, Depends(get_db)]
