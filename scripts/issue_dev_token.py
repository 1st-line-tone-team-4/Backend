"""Issue a development RS256 access token for the deployed dev backend.

The backend verifies tokens with AUTH_PUBLIC_KEY / AUTH_ISSUER / AUTH_AUDIENCE.
Keep the matching private key outside every repository; only its public key goes
into the server's .env. A first request with a new ``sub`` provisions that user.

    python scripts/issue_dev_token.py --private-key PATH --sub arim --days 7
    python scripts/issue_dev_token.py --new-keypair DIR      # once, prints the public key path
"""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ISSUER = "local-dev-issuer"
AUDIENCE = "local-dev-audience"


def new_keypair(directory):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / "dev-auth-private.pem"
    public = directory / "dev-auth-public.pem"
    if private.exists():
        sys.exit(f"Refusing to overwrite {private}")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    print(public)


def issue(private_key, sub, days, name, email):
    import jwt

    if not 1 <= days <= 30:
        sys.exit("--days must be between 1 and 30")
    now = datetime.now(timezone.utc)
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + timedelta(days=days)}
    if name:
        claims["name"] = name
    if email:
        claims["email"] = email
    print(jwt.encode(claims, Path(private_key).read_text(encoding="utf-8"), algorithm="RS256"))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--new-keypair", metavar="DIR")
    parser.add_argument("--private-key")
    parser.add_argument("--sub")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--name")
    parser.add_argument("--email")
    args = parser.parse_args()
    if args.new_keypair:
        return new_keypair(args.new_keypair)
    if not args.private_key or not args.sub:
        parser.error("--private-key and --sub are required to issue a token")
    issue(args.private_key, args.sub, args.days, args.name, args.email)


if __name__ == "__main__":
    main()
