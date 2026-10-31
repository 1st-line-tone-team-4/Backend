from sqlalchemy import select
from .core import ApiError
from .models import User


class JWTAuthenticator:
    """Verify RS256 JWTs and map a verified issuer/subject to an existing A user."""

    def __init__(self, settings, db):
        if not settings.auth_public_key or not settings.auth_issuer or not settings.auth_audience:
            raise ValueError("JWT public key, issuer, and audience are required")
        self.settings, self.db = settings, db

    def authenticate(self, request):
        import jwt

        header = request.headers.get("Authorization", "")
        scheme, separator, token = header.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or " " in token:
            raise ApiError(401, "UNAUTHORIZED", "Authentication required.")
        try:
            claims = jwt.decode(
                token,
                self.settings.auth_public_key,
                algorithms=["RS256"],
                issuer=self.settings.auth_issuer,
                audience=self.settings.auth_audience,
                options={"require": ["iss", "aud", "exp", "sub"]},
            )
            subject = claims["sub"]
            if not isinstance(subject, str) or not subject:
                raise ValueError("invalid subject")
        except (jwt.PyJWTError, ValueError, TypeError):
            raise ApiError(401, "UNAUTHORIZED", "Invalid authentication token.") from None
        with self.db.transaction() as session:
            user = session.scalar(
                select(User).where(
                    User.auth_issuer == self.settings.auth_issuer, User.auth_subject == subject
                )
            )
            if user is None:
                raise ApiError(401, "UNAUTHORIZED", "Invalid authentication token.")
            return user.id
