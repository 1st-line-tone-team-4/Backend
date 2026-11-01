"""Local HTTP fixture with real JWT verification and the canonical A/B gateway.

Only the provider is replaced. RSA signing material and the fake BYOK value are
test-only; neither is written to output or included in recorded provider calls.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
import base64
import os
import socket

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import uvicorn

from app.core import Settings, uid
from app.main import create_app
from tests.support import FixtureProvider


MODEL = "test:grounded"
KEY = "test-transient-key"


class ABFixtureProvider(FixtureProvider):
    """Deterministic provider for the complete canonical learning workflow."""

    def generate(self, model_id, key, kind, payload):
        assert key == KEY, "The local fixture accepts only its fake credential"
        self.ensure_model(model_id)
        if kind not in {"grade", "message", "report"}:
            return super().generate(model_id, key, kind, payload)
        self.calls.append((kind, deepcopy(payload)))
        if self.fail_next:
            failure, self.fail_next = self.fail_next, None
            raise failure
        if self.invalid_next:
            self.invalid_next = False
            return {"untrusted": "wrong shape"}
        if kind == "grade":
            answer_key = payload["question"]["answer_key"]
            return {
                "is_correct": payload["answer"] == answer_key,
                "correct_answer": deepcopy(answer_key),
                "feedback": "Check the external force condition against the source.",
                "explanation": "The source explains that zero net external force conserves momentum.",
            }
        if kind == "message":
            return {"assistant_content": "Why must the total external force be zero?"}
        sources = payload["source_evidence"]
        evidence = (
            [{"chunk_id": sources[0]["chunk_id"], "excerpt": sources[0]["source_excerpt"][:100]}]
            if sources
            else []
        )
        return {
            "items": [
                {
                    "checklist_item_id": item["id"],
                    "status": "explained" if evidence else "unverified",
                    "description": "The external force condition was stated in the explanation.",
                    "confidence": "high" if evidence else "low",
                    "evidence": deepcopy(evidence),
                }
                for item in payload["checklist"]
            ],
            "next_action": "Explain an example in which nonzero external force changes total momentum.",
        }


@contextmanager
def ab_http_application(tmp_path, *, database_url=None, create_schema=True):
    """Serve the actual app on loopback; leave first-login provisioning to callers.

    With ``create_schema=False``, callers must supply an already migrated test
    database. Each fixture uses unique JWT identities so it can share that DB
    with independent MySQL checks without depending on or changing their users.
    """
    root = Path(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = (
        signing_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )
    identity = uid()
    issuer = "https://ab-fixture.invalid/" + identity
    audience = "ab-fixture-" + identity
    subjects = {role: role + "-" + uid() for role in ("owner", "other")}
    now = datetime.now(timezone.utc)

    def authorization(role):
        token = jwt.encode(
            {
                "iss": issuer,
                "aud": audience,
                "sub": subjects[role],
                "iat": now,
                "exp": now + timedelta(hours=1),
                "email": role + "@ab-fixture.invalid",
                "name": "A/B fixture " + role,
            },
            signing_key,
            algorithm="RS256",
        )
        return {"Authorization": "Bearer " + token}

    owner_headers, other_headers = authorization("owner"), authorization("other")
    sock = socket.socket()
    app = server = thread = client = None
    try:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
        base_url = f"http://{host}:{port}"
        settings = Settings(
            profile="test",
            database_url=database_url or "sqlite:///" + (root / "ab-domain.sqlite3").as_posix(),
            storage_root=str(root / "private"),
            public_base_url=base_url,
            signing_secret=base64.urlsafe_b64encode(os.urandom(48)).decode("ascii"),
            credential_encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
            auth_public_key=public_key,
            auth_issuer=issuer,
            auth_audience=audience,
            enabled_models=(MODEL,),
        )
        provider = ABFixtureProvider()
        # Deliberately omit auth/task_gateway overrides: exercise their defaults.
        app = create_app(settings, providers=provider, create_schema=create_schema)
        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="critical", access_log=False, lifespan="off")
        )
        thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = monotonic() + 10
        while not server.started:
            assert thread.is_alive() and monotonic() < deadline, "A/B HTTP fixture failed to start"
            sleep(0.01)
        client = httpx.Client(base_url=base_url, headers=owner_headers, timeout=15, trust_env=False)
        yield SimpleNamespace(
            app=app,
            ctx=app.state.context,
            provider=provider,
            client=client,
            owner_headers=owner_headers,
            other_headers=other_headers,
            settings=settings,
            owner_subject=subjects["owner"],
            other_subject=subjects["other"],
            owner=None,
            other=None,
            base_url=base_url,
        )
    finally:
        if client is not None:
            client.close()
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=5)
        sock.close()
        if app is not None:
            app.state.context.db.engine.dispose()
        assert thread is None or not thread.is_alive(), "A/B HTTP fixture leaked its server thread"
