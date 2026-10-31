import base64
import json
import hashlib
import hmac
import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, or_, and_

from .core import ApiError, get_context, get_user, idempotent, iso, uid, utcnow
from .models import (
    Concept,
    ConceptVersion,
    ConceptVersionEvidence,
    Job,
    LearningExplanation,
    LearningExplanationEvidence,
    Material,
    MaterialChunk,
    MaterialUnit,
    Question,
    QuestionConcept,
    QuestionEvidence,
    QuestionSet,
    QuestionSetConcept,
    QuestionSetItem,
)
from .materials import get_confirmed_version, evidence_dto, version_dto
from .jobs import accepted

router = APIRouter()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetInput(Strict):
    unit_id: UUID
    task_id: UUID | None = None
    concept_version_ids: list[UUID] = Field(min_length=1)
    count: int = Field(ge=1, le=20, strict=True)
    difficulty: Literal["easy", "medium", "hard"]
    model_id: str = Field(min_length=1, max_length=100)
    acknowledge_possible_duplicate_cost: StrictBool = False


class ExplanationInput(Strict):
    concept_version_id: UUID
    explanation_language: Literal["ko", "en"]
    model_id: str = Field(min_length=1, max_length=100)


def missing():
    raise ApiError(404, "NOT_FOUND", "Resource not found.")


def invalid():
    raise ApiError(422, "INVALID_OUTPUT", "Generated output is invalid.")


def _version(session, user, vid, current=False):
    return get_confirmed_version(session, user, vid, require_current=current)


def _job_body(job):
    return accepted(job)


def _request_data(request, body):
    return {
        "method": request.method,
        "path": request.url.path,
        "query": dict(request.query_params),
        "body": body,
    }


def _owned(session, cls, rid, user):
    obj = session.get(cls, rid)
    if obj is None or obj.user_id != user:
        missing()
    return obj


def _cursor_decode(cursor, secret, scope):
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload, signature = raw.rsplit(b".", 1)
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest().encode()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        value = json.loads(payload)
        if (
            not isinstance(value, dict)
            or set(value) != {"timestamp", "id", "scope"}
            or value["scope"] != scope
        ):
            raise ValueError()
        from datetime import datetime

        stamp = datetime.fromisoformat(value["timestamp"])
        if stamp.tzinfo is not None or str(UUID(value["id"])) != value["id"]:
            raise ValueError()
        return stamp, value["id"]
    except Exception:
        raise ApiError(422, "INVALID_CURSOR", "Invalid cursor.")


def _page(session, stmt, model, limit, cursor, secret, scope):
    c = _cursor_decode(cursor, secret, scope)
    if c:
        stmt = stmt.where(or_(model.created_at < c[0], and_(model.created_at == c[0], model.id < c[1])))
    rows = list(session.scalars(stmt.limit(limit + 1)))
    chosen = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = chosen[-1]
        payload = json.dumps(
            {"timestamp": last.created_at.isoformat(), "id": last.id, "scope": scope},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest().encode()
        next_cursor = base64.urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")
    return chosen, next_cursor


def question_dto(session, q):
    links = session.scalars(
        select(QuestionConcept)
        .where(QuestionConcept.question_id == q.id)
        .order_by(QuestionConcept.concept_id)
    ).all()
    ev = session.scalars(
        select(QuestionEvidence)
        .where(QuestionEvidence.question_id == q.id)
        .order_by(QuestionEvidence.chunk_id)
    ).all()
    return {
        "id": q.id,
        "question_type": q.question_type,
        "concept_ids": [x.concept_id for x in links],
        "concept_version_ids": [x.concept_version_id for x in links],
        "prompt": q.prompt,
        "options": q.options,
        "difficulty": q.difficulty,
        "evidence": [evidence_dto(session, x.chunk_id) for x in ev],
    }


def get_grading_question(session, user_id, question_id):
    q = _owned(session, Question, question_id, user_id)
    return {
        **question_dto(session, q),
        "answer_key": q.answer_key,
        "public_explanation": q.public_explanation,
        "accepted_answers": q.accepted_answers,
    }


def set_dto(session, s, task_gateway=None):
    links = session.scalars(
        select(QuestionSetConcept)
        .where(QuestionSetConcept.set_id == s.id)
        .order_by(QuestionSetConcept.concept_id)
    ).all()
    rows = (
        session.scalars(
            select(QuestionSetItem).where(QuestionSetItem.set_id == s.id).order_by(QuestionSetItem.ordinal)
        ).all()
        if s.status == "ready"
        else []
    )
    attempts = (
        task_gateway.read_question_attempts(session, s.user_id, s.id, [x.question_id for x in rows])
        if task_gateway and rows
        else {}
    )
    items = [
        {
            "ordinal": x.ordinal,
            "question": question_dto(session, session.get(Question, x.question_id)),
            "latest_attempt": attempts.get(x.question_id, {}).get("latest_attempt"),
        }
        for x in rows
    ]
    next_ordinal = next(
        (x.ordinal for x in rows if not attempts.get(x.question_id, {}).get("has_graded_attempt", False)),
        None,
    )
    return {
        "id": s.id,
        "unit_id": s.unit_id,
        "task_id": s.task_id,
        "difficulty": s.difficulty,
        "count": s.requested_count,
        "status": s.status,
        "items": items,
        "next_ordinal": next_ordinal,
        "job_id": s.job_id,
        "concept_version_ids": [x.concept_version_id for x in links],
    }


def explanation_dto(session, e):
    job = session.get(Job, e.latest_job_id)
    content = None
    if e.status == "ready":
        ev = session.scalars(
            select(LearningExplanationEvidence)
            .where(LearningExplanationEvidence.explanation_id == e.id)
            .order_by(LearningExplanationEvidence.chunk_id)
        ).all()
        content = {
            "title": e.content_title,
            "explanation_markdown": e.content_markdown,
            "evidence": [evidence_dto(session, x.chunk_id, x.excerpt) for x in ev],
        }
    return {
        "id": e.id,
        "concept_id": e.concept_id,
        "concept_version_id": e.concept_version_id,
        "material_id": e.material_id,
        "explanation_language": e.explanation_language,
        "status": e.status,
        "latest_job_id": e.latest_job_id,
        "model_id": job.model_id,
        "content": content,
        "created_at": iso(e.created_at),
        "generated_at": iso(e.generated_at),
    }


@router.post("/question-sets")
def create_set(
    body: SetInput,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    ai_key: str = Header(alias="X-AI-Key"),
):
    ctx, user = get_context(request), get_user(request)
    data = body.model_dump(mode="json")
    vids = data["concept_version_ids"]
    if len(set(vids)) != len(vids):
        raise ApiError(422, "INVALID_REQUEST", "Concept versions must be unique.")

    def action(session):
        unit = session.get(MaterialUnit, data["unit_id"])
        material = session.get(Material, unit.material_id) if unit else None
        if not material or material.user_id != user:
            missing()
        if material.archived or material.processing_status != "ready":
            raise ApiError(422, "MATERIAL_NOT_READY", "Material is not available for generation.")
        # A plan task pins the exact confirmed versions it was planned with, so a
        # later re-confirmation must not block its practice set. Without a task,
        # only the current confirmed version may be used.
        versions = [_version(session, user, v, not data["task_id"]) for v in vids]
        concepts = [session.get(Concept, v.concept_id) for v in versions]
        if any(c.unit_id != unit.id for c in concepts) or len({c.id for c in concepts}) != len(concepts):
            raise ApiError(422, "INVALID_CONCEPT_VERSION", "Versions must belong to the selected unit.")
        if data["task_id"]:
            ctx.task_gateway.validate_question_task(session, user, data["task_id"], unit.id, vids)
        ctx.providers.ensure_model(data["model_id"])
        rid = uid()
        payload = {k: data[k] for k in ("unit_id", "task_id", "concept_version_ids", "count", "difficulty")}
        payload["concept_version_ids"] = sorted(vids)
        for prior in session.scalars(
            select(Job).where(
                Job.user_id == user,
                Job.kind == "questions",
                Job.status.in_(["queued", "running", "outcome_unknown"]),
            )
        ):
            if prior.request_payload == payload:
                if prior.status in ("queued", "running"):
                    raise ApiError(409, "JOB_IN_PROGRESS", "A matching job is already active.")
                # An unknown outcome stops mattering once it was explicitly
                # restarted or its set was later completed.
                resolved = session.scalar(
                    select(Job.id).where(Job.previous_job_id == prior.id)
                ) or session.scalar(
                    select(QuestionSet.id).where(
                        QuestionSet.id == prior.resource_id, QuestionSet.status == "ready"
                    )
                )
                if not resolved and not data["acknowledge_possible_duplicate_cost"]:
                    raise ApiError(409, "POSSIBLE_DUPLICATE_COST", "The previous outcome is unknown.")
        job = ctx.jobs.enqueue(
            session,
            user_id=user,
            kind="questions",
            resource_type="question_set",
            resource_id=rid,
            payload=payload,
            model_id=data["model_id"],
            ai_key=ai_key,
        )
        s = QuestionSet(
            id=rid,
            user_id=user,
            unit_id=unit.id,
            task_id=data["task_id"],
            difficulty=data["difficulty"],
            requested_count=data["count"],
            status="generating",
            job_id=job.id,
            created_at=utcnow(),
        )
        session.add(s)
        session.flush()
        for v in versions:
            session.add(QuestionSetConcept(set_id=rid, concept_id=v.concept_id, concept_version_id=v.id))
        return 202, _job_body(job)

    return idempotent(ctx, user, "createQuestionSet", idempotency_key, _request_data(request, data), action)


@router.get("/question-sets")
def list_sets(
    request: Request,
    cursor: str | None = Query(None, max_length=1024),
    limit: int = Query(20, ge=1, le=100),
    task_id: UUID | None = None,
):
    ctx, user = get_context(request), get_user(request)
    with ctx.db.transaction() as session:
        stmt = (
            select(QuestionSet)
            .where(QuestionSet.user_id == user)
            .order_by(QuestionSet.created_at.desc(), QuestionSet.id.desc())
        )
        if task_id:
            stmt = stmt.where(QuestionSet.task_id == str(task_id))
        rows, nxt = _page(
            session,
            stmt,
            QuestionSet,
            limit,
            cursor,
            ctx.settings.signing_secret,
            {"user": user, "resource": "question_sets", "task_id": str(task_id) if task_id else None},
        )
        return {"data": [set_dto(session, r, ctx.task_gateway) for r in rows], "next_cursor": nxt}


@router.get("/question-sets/{set_id}")
def get_set(set_id: UUID, request: Request):
    ctx, user = get_context(request), get_user(request)
    with ctx.db.transaction() as session:
        return {"data": set_dto(session, _owned(session, QuestionSet, str(set_id), user), ctx.task_gateway)}


@router.get("/questions/{question_id}")
def get_question(question_id: UUID, request: Request):
    ctx, user = get_context(request), get_user(request)
    with ctx.db.transaction() as session:
        return {"data": question_dto(session, _owned(session, Question, str(question_id), user))}


@router.post("/learning-explanations")
def create_explanation(
    body: ExplanationInput,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    ai_key: str = Header(alias="X-AI-Key"),
):
    ctx, user = get_context(request), get_user(request)
    data = body.model_dump(mode="json")

    def action(session):
        v = _version(session, user, data["concept_version_id"])
        c = session.get(Concept, v.concept_id)
        m = session.get(Material, c.material_id)
        if m.archived or m.processing_status != "ready":
            raise ApiError(422, "MATERIAL_NOT_READY", "Material is not available for generation.")
        ctx.providers.ensure_model(data["model_id"])
        rid = uid()
        payload = {"concept_version_id": v.id, "explanation_language": data["explanation_language"]}
        for prior in session.scalars(
            select(Job).where(
                Job.user_id == user,
                Job.kind == "learning_explanation",
                Job.model_id == data["model_id"],
                Job.status.in_(["queued", "running"]),
            )
        ):
            if prior.request_payload == payload:
                raise ApiError(409, "JOB_IN_PROGRESS", "A matching job is already active.")
        job = ctx.jobs.enqueue(
            session,
            user_id=user,
            kind="learning_explanation",
            resource_type="learning_explanation",
            resource_id=rid,
            payload=payload,
            model_id=data["model_id"],
            ai_key=ai_key,
        )
        session.add(
            LearningExplanation(
                id=rid,
                user_id=user,
                concept_id=c.id,
                concept_version_id=v.id,
                material_id=m.id,
                explanation_language=data["explanation_language"],
                status="generating",
                latest_job_id=job.id,
                created_at=utcnow(),
            )
        )
        return 202, _job_body(job)

    return idempotent(
        ctx, user, "createLearningExplanation", idempotency_key, _request_data(request, data), action
    )


@router.get("/learning-explanations")
def list_explanations(
    request: Request,
    concept_version_id: UUID,
    cursor: str | None = Query(None, max_length=1024),
    limit: int = Query(20, ge=1, le=100),
):
    ctx, user = get_context(request), get_user(request)
    with ctx.db.transaction() as session:
        _version(session, user, str(concept_version_id))
        stmt = (
            select(LearningExplanation)
            .where(
                LearningExplanation.user_id == user,
                LearningExplanation.concept_version_id == str(concept_version_id),
            )
            .order_by(LearningExplanation.created_at.desc(), LearningExplanation.id.desc())
        )
        rows, nxt = _page(
            session,
            stmt,
            LearningExplanation,
            limit,
            cursor,
            ctx.settings.signing_secret,
            {
                "user": user,
                "resource": "learning_explanations",
                "concept_version_id": str(concept_version_id),
            },
        )
        return {"data": [explanation_dto(session, r) for r in rows], "next_cursor": nxt}


@router.get("/learning-explanations/{explanation_id}")
def get_explanation(explanation_id: UUID, request: Request):
    ctx, user = get_context(request), get_user(request)
    with ctx.db.transaction() as session:
        return {
            "data": explanation_dto(session, _owned(session, LearningExplanation, str(explanation_id), user))
        }


def _generation_version(session, version):
    result = version_dto(session, version)
    result["evidence"] = [evidence_dto(session, item["chunk_id"]) for item in result["evidence"]]
    return result


class QuestionsHandler:
    def __init__(self, ctx):
        self.ctx = ctx

    def is_current(self, session, job):
        row = session.get(QuestionSet, job.resource_id)
        return bool(row and row.user_id == job.user_id and row.job_id == job.id)

    def set_state(self, session, job, state):
        if self.is_current(session, job):
            row = session.get(QuestionSet, job.resource_id)
            row.status = (
                "generating"
                if state in ("queued", "running")
                else "ready"
                if state == "succeeded"
                else "failed"
            )

    def prepare(self, session, job):
        if not self.is_current(session, job):
            raise ValueError("stale job")
        s = session.get(QuestionSet, job.resource_id)
        links = session.scalars(select(QuestionSetConcept).where(QuestionSetConcept.set_id == s.id)).all()
        return {
            "instructions": (
                'Return one JSON object, without a Markdown fence, with exactly the key "questions". '
                "Its value must contain exactly count questions in the desired order. Each question is an "
                "object with exactly these keys: question_type, concept_version_ids, prompt, options, "
                "answer_key, public_explanation, accepted_answers, evidence_chunk_ids. "
                'question_type is "multiple_choice" or "short_answer". concept_version_ids is a nonempty '
                "unique list of IDs from the supplied versions; evidence_chunk_ids is a nonempty unique "
                "list of chunk IDs from evidence attached to those same versions. prompt and "
                "public_explanation are nonempty strings of at most 12000 characters. "
                "For multiple_choice, options is a list of 2 to 5 objects, each with exactly nonempty "
                "string keys id and text; option IDs must be unique. The answer_key must have exactly "
                'this shape: {"type":"multiple_choice","selected_option_ids":["A"]}, with exactly '
                "one selected ID that matches an option ID. For short_answer, options must be [], and "
                'answer_key must have exactly this shape: {"type":"short_answer","text":"answer"}; '
                "the answer text must be 1 to 12000 characters. accepted_answers is an array of "
                "nonempty strings, each at most 12000 characters; use [] if none. Do not add keys."
            ),
            "count": s.requested_count,
            "difficulty": s.difficulty,
            "versions": [
                _generation_version(session, session.get(ConceptVersion, x.concept_version_id)) for x in links
            ],
        }

    def persist(self, session, job, output):
        if not self.is_current(session, job):
            raise ValueError("stale job")
        s = session.get(QuestionSet, job.resource_id)
        versions = {
            x.concept_version_id: x.concept_id
            for x in session.scalars(select(QuestionSetConcept).where(QuestionSetConcept.set_id == s.id))
        }
        allowed = {}
        for vid, cid in versions.items():
            concept = session.get(Concept, cid)
            allowed[vid] = {
                chunk_id
                for chunk_id in session.scalars(
                    select(ConceptVersionEvidence.chunk_id).where(
                        ConceptVersionEvidence.concept_version_id == vid
                    )
                )
                if session.get(MaterialChunk, chunk_id)
                and session.get(MaterialChunk, chunk_id).material_id == concept.material_id
            }
        if (
            not isinstance(output, dict)
            or set(output) != {"questions"}
            or not isinstance(output["questions"], list)
            or len(output["questions"]) != s.requested_count
        ):
            invalid()
        validated = []
        for item in output["questions"]:
            if not isinstance(item, dict) or set(item) != {
                "question_type",
                "concept_version_ids",
                "prompt",
                "options",
                "answer_key",
                "public_explanation",
                "accepted_answers",
                "evidence_chunk_ids",
            }:
                invalid()
            typ, vids, opts, answer, ev = (
                item[k]
                for k in (
                    "question_type",
                    "concept_version_ids",
                    "options",
                    "answer_key",
                    "evidence_chunk_ids",
                )
            )
            if (
                typ not in ("multiple_choice", "short_answer")
                or not isinstance(vids, list)
                or not vids
                or not all(isinstance(x, str) and x in versions for x in vids)
                or len(set(vids)) != len(vids)
            ):
                invalid()
            if not all(
                isinstance(item[k], str) and 0 < len(item[k].strip()) <= 12000
                for k in ("prompt", "public_explanation")
            ):
                invalid()
            if not isinstance(opts, list) or not all(
                isinstance(o, dict)
                and set(o) == {"id", "text"}
                and all(isinstance(o[k], str) and o[k].strip() for k in ("id", "text"))
                for o in opts
            ):
                invalid()
            ids = [o["id"] for o in opts]
            if len(set(ids)) != len(ids) or not isinstance(answer, dict):
                invalid()
            if typ == "multiple_choice":
                if (
                    not 2 <= len(opts) <= 5
                    or set(answer) != {"type", "selected_option_ids"}
                    or answer.get("type") != typ
                    or not isinstance(answer.get("selected_option_ids"), list)
                    or len(answer["selected_option_ids"]) != 1
                    or answer["selected_option_ids"][0] not in ids
                ):
                    invalid()
            elif (
                opts
                or set(answer) != {"type", "text"}
                or answer.get("type") != typ
                or not isinstance(answer.get("text"), str)
                or not 1 <= len(answer["text"].strip()) <= 12000
            ):
                invalid()
            accepted_answers = item["accepted_answers"]
            if not isinstance(accepted_answers, list) or not all(
                isinstance(x, str) and x.strip() and len(x) <= 12000 for x in accepted_answers
            ):
                invalid()
            if (
                not isinstance(ev, list)
                or not ev
                or not all(isinstance(x, str) for x in ev)
                or len(set(ev)) != len(ev)
                or not set(ev) <= set().union(*(allowed[v] for v in vids))
            ):
                invalid()
            validated.append(item)
        for ordinal, item in enumerate(validated, 1):
            qid = uid()
            session.add(
                Question(
                    id=qid,
                    user_id=job.user_id,
                    question_type=item["question_type"],
                    prompt=item["prompt"],
                    options=item["options"],
                    answer_key=item["answer_key"],
                    public_explanation=item["public_explanation"],
                    accepted_answers=item["accepted_answers"],
                    difficulty=s.difficulty,
                    created_at=utcnow(),
                )
            )
            session.flush()
            session.add(QuestionSetItem(set_id=s.id, question_id=qid, ordinal=ordinal))
            for vid in item["concept_version_ids"]:
                session.add(
                    QuestionConcept(question_id=qid, concept_id=versions[vid], concept_version_id=vid)
                )
            for chunk in item["evidence_chunk_ids"]:
                session.add(QuestionEvidence(question_id=qid, chunk_id=chunk))
        session.flush()
        return "/v1/question-sets/" + s.id


class ExplanationHandler:
    def __init__(self, ctx):
        self.ctx = ctx

    def is_current(self, session, job):
        row = session.get(LearningExplanation, job.resource_id)
        return bool(row and row.user_id == job.user_id and row.latest_job_id == job.id)

    def set_state(self, session, job, state):
        if self.is_current(session, job):
            row = session.get(LearningExplanation, job.resource_id)
            row.status = (
                "generating"
                if state in ("queued", "running")
                else "ready"
                if state == "succeeded"
                else "outcome_unknown"
                if state == "outcome_unknown"
                else "failed"
            )

    def prepare(self, session, job):
        if not self.is_current(session, job):
            raise ValueError("stale job")
        e = session.get(LearningExplanation, job.resource_id)
        return {
            "instructions": (
                "Return one JSON object without a Markdown fence, with exactly these keys: title, "
                "explanation_markdown, evidence. title is a nonempty string of at most 200 characters; "
                "explanation_markdown is a nonempty string of at most 24000 characters. "
                "evidence is a nonempty array of objects with exactly the keys chunk_id and excerpt. "
                "Use unique chunk IDs supplied in the pinned version evidence. Each excerpt must be "
                "a nonempty literal substring of its source chunk, at most 2000 characters. "
                "Cite at least one evidence chunk in explanation_markdown using the exact pattern "
                "[\uadfc\uac70: <chunk_id>], replacing <chunk_id> with its actual ID. Every cited ID "
                "must be present in the output evidence. Do not add keys."
            ),
            "language": e.explanation_language,
            "version": _generation_version(session, session.get(ConceptVersion, e.concept_version_id)),
        }

    def persist(self, session, job, output):
        if not self.is_current(session, job):
            raise ValueError("stale job")
        e = session.get(LearningExplanation, job.resource_id)
        if not isinstance(output, dict) or set(output) != {"title", "explanation_markdown", "evidence"}:
            invalid()
        title, markdown, evidence = output["title"], output["explanation_markdown"], output["evidence"]
        if (
            not isinstance(title, str)
            or not 1 <= len(title.strip()) <= 200
            or not isinstance(markdown, str)
            or not 1 <= len(markdown.strip()) <= 24000
            or not isinstance(evidence, list)
            or not evidence
        ):
            invalid()
        allowed = set(
            session.scalars(
                select(ConceptVersionEvidence.chunk_id).where(
                    ConceptVersionEvidence.concept_version_id == e.concept_version_id
                )
            )
        )
        found = set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"chunk_id", "excerpt"}:
                invalid()
            chunk_id, excerpt = item["chunk_id"], item["excerpt"]
            if (
                not isinstance(chunk_id, str)
                or chunk_id not in allowed
                or chunk_id in found
                or not isinstance(excerpt, str)
                or not 1 <= len(excerpt.strip()) <= 2000
            ):
                invalid()
            chunk = session.get(MaterialChunk, chunk_id)
            if not chunk or chunk.material_id != e.material_id or excerpt not in chunk.content:
                invalid()
            found.add(chunk_id)
        citations = re.findall(r"\[근거:\s*([^\]\s]+)\s*\]", markdown)
        if not citations or not set(citations) <= found:
            invalid()
        for item in evidence:
            session.add(
                LearningExplanationEvidence(
                    explanation_id=e.id,
                    concept_version_id=e.concept_version_id,
                    chunk_id=item["chunk_id"],
                    excerpt=item["excerpt"],
                )
            )
        e.content_title, e.content_markdown, e.generated_at = title, markdown, utcnow()
        # DB constraint requires content and ready status to change together.
        e.status = "ready"
        session.flush()
        return "/v1/learning-explanations/" + e.id
