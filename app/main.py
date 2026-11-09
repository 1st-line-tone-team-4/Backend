"""Application assembly. Test substitutions are never selected by environment flags."""

from pathlib import Path
from types import SimpleNamespace
import copy
import json
import logging
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from starlette.exceptions import HTTPException

from .auth import JWTAuthenticator
from .core import ApiError, Settings, api_error_handler, error_response, uid, get_user
from .db import Database
from .a_gateway import CanonicalTaskGateway


def build_context(
    settings=None, *, auth=None, providers=None, task_gateway=None, ai_documents=None, create_schema=False
):
    settings = settings or Settings.from_env()
    if settings.profile not in {"production", "development", "test"}:
        raise ValueError("Unknown application profile")
    if (auth is not None or providers is not None or ai_documents is not None) and settings.profile != "test":
        raise ValueError("Authentication/provider substitutes are allowed only in test profile")
    if create_schema and settings.profile == "production":
        raise ValueError("Production schema changes require reviewed migrations")
    if not settings.database_url or not settings.storage_root:
        raise ValueError("DATABASE_URL and STORAGE_ROOT are required")
    if len(settings.signing_secret.encode()) < 32 or not settings.credential_encryption_key:
        raise ValueError("Externally supplied storage signing and credential encryption keys are required")
    if settings.profile == "production" and not settings.database_url.startswith("mysql+pymysql://"):
        raise ValueError("Production requires the MySQL database configuration")
    from .storage import LocalStorage
    from .providers import ProviderRegistry
    from .credentials import Credentials
    from .jobs import JobService
    from .materials import AnalysisHandler
    from .content import QuestionsHandler, ExplanationHandler

    db = Database(settings.database_url)
    if create_schema:
        db.create_schema()
    context = SimpleNamespace(settings=settings, db=db)
    context.auth = auth or JWTAuthenticator(settings, db)
    context.storage = LocalStorage(settings)
    context.providers = providers or ProviderRegistry(settings)
    from .ai_service import AiDocumentClient

    context.ai_documents = ai_documents or AiDocumentClient.from_settings(settings)
    context.credentials = Credentials(
        Path(settings.storage_root) / ".credentials.sqlite3", settings.credential_encryption_key
    )
    context.task_gateway = task_gateway or CanonicalTaskGateway()
    context.jobs = JobService(context)
    context.jobs.register("analysis", AnalysisHandler(context))
    context.jobs.register("questions", QuestionsHandler(context))
    context.jobs.register("learning_explanation", ExplanationHandler(context))
    from language_learning_api.services.grading import GradeHandler
    from language_learning_api.services.teachback_handlers import TeachbackHandler

    context.jobs.register("grade", GradeHandler(context))
    context.jobs.register("message", TeachbackHandler(context, "message"))
    context.jobs.register("report", TeachbackHandler(context, "report"))
    return context


def create_app(
    settings=None, *, auth=None, providers=None, task_gateway=None, ai_documents=None, create_schema=False
):
    context = build_context(
        settings,
        auth=auth,
        providers=providers,
        task_gateway=task_gateway,
        ai_documents=ai_documents,
        create_schema=create_schema,
    )
    app = FastAPI(title="Team4 Learning Backend", version="1.3.0", debug=False)
    app.state.context = context
    app.add_exception_handler(ApiError, api_error_handler)

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        request.state.request_id = uid()
        if context.settings.profile == "production" and request.url.scheme != "https":
            return error_response(request, ApiError(400, "HTTPS_REQUIRED", "HTTPS is required."))
        try:
            response = await call_next(request)
        except Exception:
            # Do not log SQL parameters, provider error bodies, headers, or raw exceptions.
            logging.getLogger("backend").error(
                "Unhandled request error request_id=%s", request.state.request_id
            )
            response = error_response(
                request, ApiError(500, "INTERNAL_ERROR", "The request could not be completed.")
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default errors may echo X-AI-Key or arbitrary uploaded data.
        details = [
            {"field": ".".join(str(x) for x in e["loc"]), "reason": "Invalid or missing value"}
            for e in exc.errors()
        ]
        return error_response(
            request, ApiError(422, "VALIDATION_ERROR", "Request validation failed.", details)
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(
            request, ApiError(exc.status_code, "HTTP_ERROR", "Request could not be processed.")
        )

    from .materials import router as materials_router
    from .content import router as content_router
    from .jobs import router as jobs_router

    for router in (materials_router, content_router, jobs_router):
        app.include_router(router, prefix="/v1")

    from language_learning_api.routers.exam_goals import router as goals_router
    from language_learning_api.routers.study_plans import router as plans_router
    from language_learning_api.routers.questions import router as attempts_router
    from language_learning_api.routers.teachback import router as teachback_router
    from language_learning_api.routers.records import router as records_router

    for router in (goals_router, plans_router, attempts_router, teachback_router, records_router):
        app.include_router(router)

    @app.get("/v1/me")
    def me(request: Request):
        from .models import User

        owner = get_user(request)
        with context.db.transaction() as session:
            user = session.get(User, owner)
            if user is None:
                raise ApiError(401, "UNAUTHORIZED", "Authentication required.")
            return {"data": {key: getattr(user, key) for key in ("id", "email", "display_name", "timezone")}}

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    # Publish only canonical operations actually mounted by the integrated server.
    source = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts" / "openapi-1.3.0.json").read_text(encoding="utf-8")
    )
    source = copy.deepcopy(source)
    source["info"]["title"] = "Team4 Learning Backend — implemented contract"
    source["info"]["description"] = (
        "Integrated A and B routes. See README for external authentication and deployment prerequisites."
    )

    def normalize(path):
        return re.sub(r"\{[^}]+\}", "{}", path.removeprefix("/v1"))

    inventory = get_openapi(title="Mounted routes", version="1.3.0", routes=app.routes)
    mounted = {
        (normalize(path), method) for path, operations in inventory["paths"].items() for method in operations
    }
    source["paths"] = {
        path: {
            method: operation
            for method, operation in operations.items()
            if method == "parameters" or (normalize(path), method) in mounted
        }
        for path, operations in source["paths"].items()
        if any((normalize(path), method) in mounted for method in operations)
    }
    source.pop("x-contract-rules", None)
    source.pop("x-decisions", None)
    app.openapi = lambda: source
    return app
