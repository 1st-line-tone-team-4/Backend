"""Deterministic external/A substitutes, reachable only through explicit test injection."""

from copy import deepcopy
from types import SimpleNamespace
import base64
import os

from app.core import ApiError, Settings, uid, utcnow
from app.models import User
from app.ports import QuestionTaskState, TaskGateway


class FixtureAuth:
    def __init__(self):
        self.tokens = {"test-owner-token": uid(), "test-other-token": uid()}

    def authenticate(self, request):
        owner = self.tokens.get(request.headers.get("Authorization", "").removeprefix("Bearer "))
        if owner is None:
            raise ApiError(401, "UNAUTHORIZED", "Authentication required.")
        return owner


class FixtureTasks(TaskGateway):
    def __init__(self):
        self.tasks = {}
        self.attempts = {}
        self.progress_writes = 0

    def material_in_use(self, session, user_id, material_id):
        return any(t["owner"] == user_id and t["material"] == material_id for t in self.tasks.values())

    def read_question_task(self, session, user_id, task_id):
        task = self.tasks.get(task_id)
        if not task or task["owner"] != user_id:
            return None
        return QuestionTaskState(
            task_id=task_id,
            user_id=task["owner"],
            unit_id=task["unit"],
            concept_version_ids=frozenset(task["versions"]),
            task_type=task["task_type"],
            active_revision=task["active_revision"],
            target_count=task["target_count"],
            completed_count=task["completed_count"],
        )

    def read_question_attempts(self, session, user_id, set_id, question_ids):
        return {
            q: deepcopy(self.attempts[(user_id, set_id, q)])
            for q in question_ids
            if (user_id, set_id, q) in self.attempts
        }


class FixtureProvider:
    model_id = "test:grounded"

    def __init__(self):
        self.calls = []
        self.fail_next = None
        self.invalid_next = False

    def list_models(self):
        return [{"id": self.model_id, "provider": "test", "label": "Test fixture", "available": True}]

    def ensure_model(self, model_id):
        if model_id != self.model_id:
            raise ApiError(422, "MODEL_UNAVAILABLE", "Model is not configured.")

    def validate_key(self, model_id, key):
        self.ensure_model(model_id)
        return {"valid": key == "test-transient-key", "model_id": model_id}

    def generate(self, model_id, key, kind, payload):
        self.ensure_model(model_id)
        self.calls.append((kind, deepcopy(payload)))  # Deliberately never capture key.
        if self.fail_next:
            failure, self.fail_next = self.fail_next, None
            raise failure
        if self.invalid_next:
            self.invalid_next = False
            return {"untrusted": "wrong shape"}
        if kind == "analysis":
            chunk = payload["chunks"][0]
            return {
                "units": [
                    {
                        "title": "Momentum",
                        "concepts": [
                            {
                                "name": "Momentum conservation",
                                "summary": "Momentum is conserved when total external force is zero.",
                                "checklist_items": ["State the external force condition."],
                                "evidence": [{"chunk_id": chunk["id"], "quote": chunk["content"][:100]}],
                            }
                        ],
                    }
                ]
            }
        if kind == "questions":
            versions = payload["versions"]
            return {
                "questions": [
                    {
                        "question_type": "short_answer",
                        "concept_version_ids": [versions[0]["id"]],
                        "prompt": f"When is momentum conserved? ({i + 1})",
                        "options": [],
                        "answer_key": {"type": "short_answer", "text": "When total external force is zero"},
                        "public_explanation": "The external force condition follows the source.",
                        "accepted_answers": ["When total external force is zero"],
                        "evidence_chunk_ids": [versions[0]["evidence"][0]["chunk_id"]],
                    }
                    for i in range(payload["count"])
                ]
            }
        if kind == "learning_explanation":
            version = payload.get("version", payload.get("concept_version"))
            if version is None:
                raise AssertionError("Test provider must match explicit handler payload")
            evidence = version["evidence"][0]
            return {
                "title": "Momentum conservation",
                "explanation_markdown": f"Total momentum stays constant without external force. [근거: {evidence['chunk_id']}]",
                "evidence": [{"chunk_id": evidence["chunk_id"], "excerpt": evidence["excerpt"][:100]}],
            }
        raise AssertionError("Unregistered test kind")


def test_application(tmp_path, base_url="http://testserver", *, database_url=None, create_schema=True):
    from app.main import create_app

    settings = Settings(
        profile="test",
        database_url=database_url or "sqlite:///" + (tmp_path / "domain.sqlite3").as_posix(),
        storage_root=str(tmp_path / "private"),
        public_base_url=base_url,
        signing_secret="test-signing-secret-" * 3,
        credential_encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode(),
    )
    auth, provider, tasks = FixtureAuth(), FixtureProvider(), FixtureTasks()
    app = create_app(settings, auth=auth, providers=provider, task_gateway=tasks, create_schema=create_schema)
    with app.state.context.db.transaction() as s:
        for token, user in auth.tokens.items():
            s.add(
                User(
                    id=user,
                    auth_issuer="test-only",
                    auth_subject=token,
                    timezone="Asia/Seoul",
                    created_at=utcnow(),
                )
            )
    return SimpleNamespace(app=app, ctx=app.state.context, auth=auth, provider=provider, tasks=tasks)
