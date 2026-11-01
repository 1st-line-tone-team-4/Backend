from email.errors import HeaderParseError
from email.headerregistry import Address

from jsonschema import FormatChecker
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .core import ApiError, uid, utcnow
from .models import User

_EMAIL_FORMAT = FormatChecker(formats=("email",))


class JWTAuthenticator:
    """Verify the configured issuer and provision its first authenticated user."""

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
            if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
                raise ValueError("invalid subject")
        except (jwt.PyJWTError, ValueError, TypeError):
            raise ApiError(401, "UNAUTHORIZED", "Invalid authentication token.") from None

        def find(session):
            user = session.scalar(
                select(User).where(
                    User.auth_issuer == self.settings.auth_issuer, User.auth_subject == subject
                )
            )
            # Database collations may ignore trailing spaces or case. Identity
            # claims must match exactly even when SQL equality returned a row.
            if user is not None and (
                user.auth_issuer != self.settings.auth_issuer or user.auth_subject != subject
            ):
                raise ApiError(401, "UNAUTHORIZED", "Invalid authentication token.")
            return user

        def optional_claim(name, maximum):
            value = claims.get(name)
            return value if isinstance(value, str) and len(value) <= maximum else None

        def optional_email():
            value = optional_claim("email", 320)
            if value is None or not _EMAIL_FORMAT.conforms(value, "email"):
                return None
            try:
                mailbox = Address(addr_spec=value)
            except (HeaderParseError, ValueError):
                return None
            # Validate syntax only; identity comes from issuer/subject, and
            # provisioning must never perform address or domain lookups.
            return value if mailbox.username and mailbox.domain else None

        try:
            with self.db.transaction() as session:
                user = find(session)
                if user is None:
                    user = User(
                        id=uid(),
                        auth_issuer=self.settings.auth_issuer,
                        auth_subject=subject,
                        email=optional_email(),
                        display_name=optional_claim("name", 120),
                        timezone="Asia/Seoul",
                        created_at=utcnow(),
                    )
                    session.add(user)
                    session.flush()
                owner = user.id
            return owner
        except IntegrityError:
            # Concurrent first login can win. Never authenticate a SQL collation alias.
            with self.db.transaction() as session:
                user = find(session)
                if user is not None:
                    return user.id
            raise ApiError(401, "UNAUTHORIZED", "Invalid authentication token.") from None
