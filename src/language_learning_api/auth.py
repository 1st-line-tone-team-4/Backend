from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.orm import Session

from language_learning_api.config import get_settings
from language_learning_api.database import get_db
from language_learning_api.models import User

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")

    settings = get_settings()
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_public_key.replace("\\n", "\n"),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "iss", "aud", "exp"]},
        )
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED") from exc

    issuer = claims["iss"]
    subject = claims["sub"]
    user = db.scalar(select(User).where(User.auth_issuer == issuer, User.auth_subject == subject))
    if user is None:
        user = User(
            auth_issuer=issuer,
            auth_subject=subject,
            email=claims.get("email"),
            display_name=claims.get("name"),
            timezone="Asia/Seoul",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DatabaseSession = Annotated[Session, Depends(get_db)]
