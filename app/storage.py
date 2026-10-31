import base64
import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import quote
from .core import ApiError


class LocalStorage:
    def __init__(self, settings):
        self.root = Path(settings.storage_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret = settings.signing_secret.encode()
        self.base = settings.public_base_url.rstrip("/")
        if len(self.secret) < 32 or (
            settings.profile == "production" and not self.base.startswith("https://")
        ):
            raise ValueError("Private storage requires a strong signing secret and production HTTPS.")

    def _path(self, key):
        if not isinstance(key, str) or len(key) != 36 or any(c not in "0123456789abcdef-" for c in key):
            raise ApiError(400, "INVALID_STORAGE_KEY", "Invalid storage key.")
        return self.root / key

    def sign(self, material, purpose, seconds):
        exp = int(time.time()) + seconds
        payload = f"{purpose}:{material.id}:{material.user_id}:{exp}"
        sig = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode().rstrip("=")
        return f"{self.base}/v1/private-storage/{material.id}?token={quote(token)}", exp

    def verify(self, token, material_id, purpose):
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
            p, mid, owner, exp, sig = raw.split(":")
            payload = f"{p}:{mid}:{owner}:{exp}"
            expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
            if (
                p != purpose
                or mid != material_id
                or int(exp) <= int(time.time())
                or not hmac.compare_digest(sig, expected)
            ):
                raise ValueError()
            return owner
        except Exception:
            raise ApiError(403, "INVALID_TOKEN", "Storage token is invalid or expired.")

    def write(self, key, data):
        path = self._path(key)
        # exclusive creation prevents replacement after a signed upload
        try:
            with path.open("xb") as f:
                f.write(data)
        except FileExistsError:
            raise ApiError(409, "UPLOAD_EXISTS", "Upload already exists.")

    def read(self, key):
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise ApiError(422, "UPLOAD_MISSING", "Upload is missing.")

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)
