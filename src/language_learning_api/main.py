from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from language_learning_api.auth import CurrentUser
from language_learning_api.routers.exam_goals import router as exam_goals_router
from language_learning_api.routers.study_plans import router as study_plans_router
from language_learning_api.routers.questions import router as questions_router
from language_learning_api.routers.teachback import router as teachback_router
from language_learning_api.routers.records import router as records_router
from language_learning_api.schemas import UserProfileEnvelope

app = FastAPI(title="Language Learning API")
app.include_router(exam_goals_router)
app.include_router(study_plans_router)
app.include_router(questions_router)
app.include_router(teachback_router)
app.include_router(records_router)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    code = exc.detail if isinstance(exc.detail, str) else "HTTP_ERROR"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": code, "request_id": str(uuid4()), "details": []}},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    details = [
        {"field": ".".join(str(part) for part in error["loc"] if part != "body"), "reason": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION_ERROR", "message": "요청 형식이 올바르지 않습니다.", "request_id": str(uuid4()), "details": details}},
    )


@app.get("/v1/health")
def health_check():
    return {"status": "ok"}


@app.get("/v1/me", response_model=UserProfileEnvelope, tags=["사용자"])
def get_me(user: CurrentUser):
    return {"data": {"id": user.id, "email": user.email, "display_name": user.display_name, "timezone": user.timezone}}
