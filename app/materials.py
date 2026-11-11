from datetime import datetime, timezone
from dataclasses import dataclass
import base64
from fastapi import APIRouter, Request, Header, Query
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from .extraction import extract_bytes
from pydantic import BaseModel, ConfigDict, Field, field_validator
from uuid import UUID
from sqlalchemy import select, delete, func
from sqlalchemy.exc import IntegrityError
from .core import ApiError, get_context, get_user, idempotent, idempotency_replay, uid, utcnow, iso
from .core import request_envelope as reqdata
from .pagination import decode_material_cursor, encode_material_cursor
from .models import (
    Material,
    MaterialUnit,
    MaterialChunk,
    Concept,
    ConceptVersion,
    ConceptChecklistItem,
    ConceptVersionEvidence,
    Job,
    QuestionSet,
    LearningExplanation,
    StorageDeletion,
)

from .projections import evidence_dto
from .ai_context import material_name, wants_ai_context

router = APIRouter()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Upload(Strict):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str
    size_bytes: int = Field(ge=1, le=20971520)


class Complete(Strict):
    model_id: str = Field(min_length=1, max_length=100)


class Archive(Strict):
    archived: bool


class ConceptInput(Strict):
    concept_id: str | None
    unit_id: str
    base_version_id: str | None
    name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=12000)
    checklist_items: list[str] = Field(min_length=1, max_length=50)
    evidence_chunk_ids: list[str] = Field(min_length=1)

    @field_validator("concept_id", "unit_id", "base_version_id")
    @classmethod
    def valid_id(cls, value):
        if value is None:
            return None
        return str(UUID(value))

    @field_validator("evidence_chunk_ids")
    @classmethod
    def valid_evidence(cls, values):
        return [str(UUID(value)) for value in values]


class Confirm(Strict):
    concepts: list[ConceptInput] = Field(min_length=1, max_length=100)


def err(status, code, msg):
    raise ApiError(status, code, msg)


def validate_path_id(value):
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        err(422, "INVALID_ID", "Path identifier must be a UUID.")


def owned(s, user, mid, lock=False):
    validate_path_id(mid)
    q = select(Material).where(Material.id == mid, Material.user_id == user)
    if lock:
        q = q.with_for_update()
    m = s.scalar(q)
    if not m:
        err(404, "NOT_FOUND", "Material not found.")
    return m


def mdto(m):
    return {
        k: getattr(m, k)
        for k in (
            "id",
            "filename",
            "mime_type",
            "size_bytes",
            "processing_status",
            "page_count",
            "archived",
            "latest_job_id",
        )
    } | {"created_at": iso(m.created_at)}


def current(s, cid):
    return s.scalar(
        select(ConceptVersion)
        .where(ConceptVersion.concept_id == cid)
        .order_by(ConceptVersion.version_no.desc())
        .limit(1)
    )


def version_dto(s, v):
    items = s.scalars(
        select(ConceptChecklistItem)
        .where(ConceptChecklistItem.concept_version_id == v.id)
        .order_by(ConceptChecklistItem.ordinal)
    ).all()
    ev = s.scalars(
        select(ConceptVersionEvidence)
        .where(ConceptVersionEvidence.concept_version_id == v.id)
        .order_by(ConceptVersionEvidence.chunk_id)
    ).all()
    return {
        "id": v.id,
        "version_no": v.version_no,
        "name": v.name,
        "summary": v.summary,
        "is_confirmed": v.is_confirmed,
        "is_current": current(s, v.concept_id).id == v.id,
        "confirmed_at": iso(v.confirmed_at),
        "checklist_items": [{"id": i.id, "ordinal": i.ordinal, "criterion": i.criterion} for i in items],
        "evidence": [evidence_dto(s, e.chunk_id, e.quote) for e in ev],
    }


def get_confirmed_version(s, user_id, concept_version_id, require_current=False):
    v = s.get(ConceptVersion, concept_version_id)
    c = s.get(Concept, v.concept_id) if v else None
    m = s.get(Material, c.material_id) if c else None
    if not m or m.user_id != user_id:
        err(404, "NOT_FOUND", "Concept version not found.")
    if not v.is_confirmed or (require_current and current(s, c.id).id != v.id):
        err(422, "CONCEPT_NOT_CONFIRMED", "Concept version is not confirmed or current.")
    return v


def extract(m, data, *, require_text=True):
    if len(data) != m.size_bytes:
        err(422, "INVALID_UPLOAD", "Upload size does not match.")
    if require_text:
        return extract_bytes(m.mime_type, data)
    return extract_bytes(m.mime_type, data, require_text=False)


def ai_recognizes(ctx, mime_type):
    ai_documents = getattr(ctx, "ai_documents", None)
    return ai_documents is not None and ai_documents.handles(mime_type)


@dataclass(frozen=True)
class MaterialSnapshot:
    """Immutable scalar identity; safe to retain after the DB session closes."""

    id: str
    user_id: str
    storage_key: str
    mime_type: str
    size_bytes: int
    latest_job_id: str | None
    processing_status: str

    @classmethod
    def of(cls, material):
        return cls(**{name: getattr(material, name) for name in cls.__dataclass_fields__})

    def matches(self, material):
        return self == self.of(material)


@dataclass(frozen=True)
class PreparedAnalysis:
    material: MaterialSnapshot
    job_id: str
    payload: dict


@router.post("/materials")
def create(body: Upload, request: Request, idempotency_key: str = Header(alias="Idempotency-Key")):
    ctx = get_context(request)
    user = get_user(request)
    if body.mime_type not in ("application/pdf", "text/plain", "image/png", "image/jpeg"):
        err(422, "INVALID_UPLOAD", "Unsupported MIME type.")

    def action(s):
        now = utcnow()
        mid = uid()
        m = Material(
            id=mid,
            user_id=user,
            filename=body.filename,
            mime_type=body.mime_type,
            size_bytes=body.size_bytes,
            storage_key=uid(),
            processing_status="upload_pending",
            archived=False,
            created_at=now,
            updated_at=now,
        )
        s.add(m)
        s.flush()
        url, exp = ctx.storage.sign(m, "put", 600)
        return 201, {
            "data": {
                "material": mdto(m),
                "upload_url": url,
                "expires_at": iso(datetime.fromtimestamp(exp, timezone.utc).replace(tzinfo=None)),
            }
        }

    return idempotent(
        ctx, user, "createMaterialUpload", idempotency_key, reqdata(request, body.model_dump()), action
    )


@router.put("/private-storage/{materialId}")
async def put_file(materialId: str, request: Request, token: str):
    validate_path_id(materialId)
    ctx = get_context(request)
    owner = ctx.storage.verify(token, materialId, "put")

    # Never hold a DB transaction, row lock or pooled connection while the body
    # streams in or the parser subprocess runs. Sync DB work runs off the loop.
    def snapshot():
        with ctx.db.transaction() as s:
            m = owned(s, owner, materialId)
            if m.processing_status != "upload_pending":
                err(409, "UPLOAD_CLOSED", "Upload is closed.")
            return m.mime_type, m.size_bytes, m.storage_key

    mime_type, size_bytes, storage_key = await run_in_threadpool(snapshot)
    if request.headers.get("content-type", "").split(";")[0].strip() != mime_type:
        err(422, "INVALID_UPLOAD", "Content type does not match.")
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data) > size_bytes or len(data) > 20971520:
            err(422, "INVALID_UPLOAD", "Upload is too large.")
    content = bytes(data)
    if len(content) != size_bytes:
        err(422, "INVALID_UPLOAD", "Upload size does not match.")
    # Match complete(): an enabled recognizer supplies text for scanned PDFs.
    # Keep the ordinary parser call unchanged for non-OCR deployments/types.
    if ai_recognizes(ctx, mime_type):
        await run_in_threadpool(extract_bytes, mime_type, content, require_text=False)
    else:
        await run_in_threadpool(extract_bytes, mime_type, content)

    def commit():
        # Stage and fsync without a DB connection, then re-check under a short
        # row lock. Only the exclusive hard-link publication occurs while locked.
        with ctx.storage.staged_write(storage_key, content) as publish:
            with ctx.db.transaction() as s:
                m = owned(s, owner, materialId, True)
                if (
                    m.processing_status != "upload_pending"
                    or m.storage_key != storage_key
                    or m.mime_type != mime_type
                    or m.size_bytes != size_bytes
                ):
                    err(409, "UPLOAD_CLOSED", "Upload is closed.")
                publish()

    await run_in_threadpool(commit)
    return Response(status_code=204)


@router.get("/private-storage/{materialId}")
def get_file(materialId: str, request: Request, token: str):
    validate_path_id(materialId)
    ctx = get_context(request)
    owner = ctx.storage.verify(token, materialId, "get")
    with ctx.db.transaction() as s:
        m = owned(s, owner, materialId)
        data = ctx.storage.read(m.storage_key)
        return Response(
            data,
            media_type=m.mime_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline",
            },
        )


@router.post("/materials/{materialId}/complete")
def complete(
    materialId: str,
    body: Complete,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    ai_key: str = Header(alias="X-AI-Key"),
):
    ctx = get_context(request)
    user = get_user(request)

    request_data = reqdata(request, body.model_dump())
    replay = idempotency_replay(ctx, user, "completeMaterialUpload", idempotency_key, request_data)
    if replay is not None:
        return replay
    with ctx.db.transaction() as s:
        m = owned(s, user, materialId)
        if m.archived or m.processing_status != "upload_pending":
            err(409, "INVALID_STATE", "Material cannot be completed.")
        snapshot = MaterialSnapshot.of(m)
    # Parsing is bounded by extraction.py and never holds a DB connection. The
    # immutable object key is checked again with all other mutable state below.
    source_data = ctx.storage.read(snapshot.storage_key)
    # With the AI server configured, a scanned PDF (no text layer) is accepted:
    # Upstage OCR supplies the text in the analysis job.
    pages = extract(snapshot, source_data, require_text=not ai_recognizes(ctx, snapshot.mime_type))
    preflight = getattr(ctx.providers, "preflight", None)
    # Vision limits apply only when the image itself goes to the LLM provider.
    if (
        snapshot.mime_type.startswith("image/")
        and callable(preflight)
        and not ai_recognizes(ctx, snapshot.mime_type)
    ):
        from .providers import ProviderFailure

        try:
            preflight(
                body.model_id,
                "analysis",
                {
                    "image_data_url": "data:"
                    + snapshot.mime_type
                    + ";base64,"
                    + base64.b64encode(source_data).decode()
                },
            )
        except ProviderFailure:
            err(422, "INVALID_UPLOAD", "Image exceeds the selected provider's processing limits.")

    def action(s):
        m = owned(s, user, materialId, True)
        if m.archived or m.processing_status != "upload_pending" or not snapshot.matches(m):
            err(409, "INVALID_STATE", "Material cannot be completed.")
        m.page_count = len(pages)
        job = ctx.jobs.enqueue(
            s,
            user_id=user,
            kind="analysis",
            resource_type="material",
            resource_id=m.id,
            payload={"material_id": m.id},
            model_id=body.model_id,
            ai_key=ai_key,
        )
        m.latest_job_id = job.id
        m.processing_status = "queued"
        m.updated_at = utcnow()
        from .jobs import job_dto

        return 202, {"data": {"job": job_dto(job)}}

    return idempotent(ctx, user, "completeMaterialUpload", idempotency_key, request_data, action)


@router.get("/materials")
def listing(
    request: Request,
    cursor: str | None = Query(None, max_length=1024),
    limit: int = Query(20, ge=1, le=100),
    include_archived: bool = False,
):
    ctx = get_context(request)
    user = get_user(request)
    with ctx.db.transaction() as s:
        q = select(Material).where(Material.user_id == user)
        if not include_archived:
            q = q.where(Material.archived.is_(False))
        if cursor is not None:
            dt, cid = decode_material_cursor(cursor, user, include_archived, ctx.storage.secret)
            q = q.where((Material.created_at < dt) | ((Material.created_at == dt) & (Material.id < cid)))
        rows = s.scalars(q.order_by(Material.created_at.desc(), Material.id.desc()).limit(limit + 1)).all()
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = None
        if more:
            nxt = encode_material_cursor(
                rows[-1].created_at, rows[-1].id, user, include_archived, ctx.storage.secret
            )
        return {"data": [mdto(m) for m in rows], "next_cursor": nxt}


@router.get("/materials/{materialId}")
def detail(materialId: str, request: Request):
    with get_context(request).db.transaction() as s:
        return {"data": mdto(owned(s, get_user(request), materialId))}


@router.patch("/materials/{materialId}")
def archive(materialId: str, body: Archive, request: Request):
    with get_context(request).db.transaction() as s:
        m = owned(s, get_user(request), materialId, True)
        m.archived = body.archived
        m.updated_at = utcnow()
        return {"data": mdto(m)}


@router.get("/materials/{materialId}/source")
def source(materialId: str, request: Request, page: int = Query(ge=1)):
    ctx = get_context(request)
    with ctx.db.transaction() as s:
        m = owned(s, get_user(request), materialId)
        if m.page_count is None or page > m.page_count:
            err(422, "INVALID_PAGE", "Page is out of range.")
        url, exp = ctx.storage.sign(m, "get", 300)
        if m.mime_type == "application/pdf":
            url += f"#page={page}"
        return {
            "data": {
                "url": url,
                "expires_at": iso(datetime.fromtimestamp(exp, timezone.utc).replace(tzinfo=None)),
                "page_number": page,
                "mime_type": m.mime_type,
            }
        }


@router.get("/materials/{materialId}/chunks/{chunkId}")
def chunk(materialId: str, chunkId: str, request: Request):
    validate_path_id(chunkId)
    with get_context(request).db.transaction() as s:
        owned(s, get_user(request), materialId)
        c = s.get(MaterialChunk, chunkId)
        if not c or c.material_id != materialId:
            err(404, "NOT_FOUND", "Chunk not found.")
        return {"data": evidence_dto(s, c.id)}


@router.get("/materials/{materialId}/units")
def units(materialId: str, request: Request):
    with get_context(request).db.transaction() as s:
        owned(s, get_user(request), materialId)
        rows = s.scalars(
            select(MaterialUnit).where(MaterialUnit.material_id == materialId).order_by(MaterialUnit.ordinal)
        ).all()
        return {
            "data": [
                {"id": u.id, "material_id": u.material_id, "ordinal": u.ordinal, "title": u.title}
                for u in rows
            ]
        }


@router.get("/materials/{materialId}/concepts")
def concepts(materialId: str, request: Request):
    with get_context(request).db.transaction() as s:
        owned(s, get_user(request), materialId)
        cs = s.scalars(
            select(Concept).where(Concept.material_id == materialId).order_by(Concept.created_at, Concept.id)
        ).all()
        return {
            "data": [
                {
                    "concept_id": c.id,
                    "unit_id": c.unit_id,
                    "current_version": version_dto(s, current(s, c.id)),
                }
                for c in cs
            ]
        }


@router.get("/materials/{materialId}/concepts/{conceptId}/versions")
def versions(materialId: str, conceptId: str, request: Request):
    validate_path_id(conceptId)
    with get_context(request).db.transaction() as s:
        owned(s, get_user(request), materialId)
        c = s.get(Concept, conceptId)
        if not c or c.material_id != materialId:
            err(404, "NOT_FOUND", "Concept not found.")
        return {
            "data": [
                version_dto(s, v)
                for v in s.scalars(
                    select(ConceptVersion)
                    .where(ConceptVersion.concept_id == c.id)
                    .order_by(ConceptVersion.version_no.desc())
                ).all()
            ]
        }


@router.patch("/materials/{materialId}/concepts")
def confirm(materialId: str, body: Confirm, request: Request):
    ctx = get_context(request)
    user = get_user(request)
    try:
        with ctx.db.transaction() as s:
            m = owned(s, user, materialId, True)
            if m.archived or m.processing_status != "ready":
                err(422, "INVALID_STATE", "Material is not available for confirmation.")
            existing_count = s.scalar(
                select(func.count()).select_from(Concept).where(Concept.material_id == m.id)
            )
            if existing_count + sum(item.concept_id is None for item in body.concepts) > 100:
                err(422, "CONCEPT_LIMIT", "Material may contain at most 100 concepts.")
            seen = set()
            result = []
            for item in body.concepts:
                if (
                    not item.name.strip()
                    or not item.summary.strip()
                    or any(not x.strip() or len(x) > 2000 for x in item.checklist_items)
                    or len(set(item.evidence_chunk_ids)) != len(item.evidence_chunk_ids)
                ):
                    err(422, "INVALID_CONCEPT", "Concept fields are invalid.")
                u = s.get(MaterialUnit, item.unit_id)
                if not u or u.material_id != m.id:
                    err(422, "INVALID_CONCEPT", "Unit does not belong to material.")
                chunks = s.scalars(
                    select(MaterialChunk).where(
                        MaterialChunk.id.in_(item.evidence_chunk_ids), MaterialChunk.material_id == m.id
                    )
                ).all()
                if len(chunks) != len(item.evidence_chunk_ids):
                    err(422, "INVALID_CONCEPT", "Evidence does not belong to material.")
                if item.concept_id:
                    if item.concept_id in seen:
                        err(422, "INVALID_CONCEPT", "Duplicate concept.")
                    seen.add(item.concept_id)
                    c = s.get(Concept, item.concept_id)
                    if not c or c.material_id != m.id or c.unit_id != u.id:
                        err(422, "INVALID_CONCEPT", "Concept does not belong to unit.")
                    prev = current(s, c.id)
                    if not prev or prev.id != item.base_version_id:
                        err(409, "VERSION_CONFLICT", "Concept version has changed.")
                    count = s.scalar(
                        select(func.count())
                        .select_from(ConceptVersion)
                        .where(ConceptVersion.concept_id == c.id)
                    )
                    if count >= 1000:
                        err(422, "VERSION_LIMIT", "Concept may contain at most 1000 versions.")
                    n = prev.version_no + 1
                else:
                    if item.base_version_id is not None:
                        err(422, "INVALID_CONCEPT", "New concept must have no base version.")
                    c = Concept(id=uid(), material_id=m.id, unit_id=u.id, created_at=utcnow())
                    s.add(c)
                    s.flush()
                    n = 1
                now = utcnow()
                v = ConceptVersion(
                    id=uid(),
                    concept_id=c.id,
                    version_no=n,
                    name=item.name,
                    summary=item.summary,
                    is_confirmed=True,
                    confirmed_at=now,
                    created_at=now,
                )
                s.add(v)
                s.flush()
                for i, criterion in enumerate(item.checklist_items, 1):
                    s.add(
                        ConceptChecklistItem(
                            id=uid(), concept_version_id=v.id, ordinal=i, criterion=criterion
                        )
                    )
                for cid in item.evidence_chunk_ids:
                    s.add(ConceptVersionEvidence(concept_version_id=v.id, chunk_id=cid, quote=None))
                s.flush()
                result.append({"concept_id": c.id, "unit_id": u.id, "version": version_dto(s, v)})
            return {"data": result}
    except IntegrityError:
        err(409, "VERSION_CONFLICT", "Concept version has changed.")


@router.delete("/materials/{materialId}", status_code=204)
def remove(materialId: str, request: Request):
    ctx = get_context(request)
    user = get_user(request)
    validate_path_id(materialId)
    with ctx.db.transaction() as s:
        # Read IDs without range locks, then lock existing primary keys before
        # material (worker/retry order). An empty MySQL next-key range lock here
        # would deadlock a concurrent complete() inserting its first job.
        job_ids = s.scalars(
            select(Job.id)
            .where(Job.user_id == user, Job.resource_type == "material", Job.resource_id == materialId)
            .order_by(Job.id)
        ).all()
        jobs = [s.scalar(select(Job).where(Job.id == jid).with_for_update()) for jid in job_ids]
        jobs = [job for job in jobs if job is not None]
        m = owned(s, user, materialId, True)
        if m.latest_job_id is not None and m.latest_job_id not in {job.id for job in jobs}:
            err(409, "MATERIAL_IN_USE", "Material has a new job; try again later.")
        unit_ids = select(MaterialUnit.id).where(MaterialUnit.material_id == m.id)
        if (
            ctx.task_gateway.material_in_use(s, user, m.id)
            or s.scalar(select(QuestionSet.id).where(QuestionSet.unit_id.in_(unit_ids)).limit(1))
            or s.scalar(
                select(LearningExplanation.id).where(LearningExplanation.material_id == m.id).limit(1)
            )
            or any(job.status in ("queued", "running") for job in jobs)
        ):
            err(409, "MATERIAL_IN_USE", "Material is referenced; archive it instead.")
        concept_ids = select(Concept.id).where(Concept.material_id == m.id)
        version_ids = select(ConceptVersion.id).where(ConceptVersion.concept_id.in_(concept_ids))
        s.execute(
            delete(ConceptVersionEvidence).where(ConceptVersionEvidence.concept_version_id.in_(version_ids))
        )
        s.execute(
            delete(ConceptChecklistItem).where(ConceptChecklistItem.concept_version_id.in_(version_ids))
        )
        s.execute(delete(ConceptVersion).where(ConceptVersion.concept_id.in_(concept_ids)))
        s.execute(delete(Concept).where(Concept.material_id == m.id))
        s.execute(delete(MaterialChunk).where(MaterialChunk.material_id == m.id))
        s.execute(delete(MaterialUnit).where(MaterialUnit.material_id == m.id))
        m.latest_job_id = None
        s.flush()
        for j in jobs:
            # Retain outcome/cost audit and predecessor links after content is
            # removed. An absent material cannot be retried/restarted. No source
            # text, actionable credential, or dead result URL is retained here.
            j.request_payload = {}
            j.result_url = None
            j.retryable = False
            if j.credential_ref:
                ctx.credentials.delete(j.credential_ref)
                j.credential_ref = None
        s.add(
            StorageDeletion(
                id=uid(), storage_key=m.storage_key, delete_after=utcnow(), deleted_at=None, attempt_count=0
            )
        )
        s.delete(m)
    return Response(status_code=204)


def analysis_chunks(m, data, job_id, pages=None):
    import uuid

    pages = extract(m, data) if pages is None else pages
    chunks = []
    for page, text in enumerate(pages, 1):
        for start in range(0, len(text), 4000):
            textpart = text[start : start + 4000]
            if textpart.strip():
                ordinal = len(chunks) + 1
                chunks.append(
                    {
                        "id": str(uuid.uuid5(uuid.UUID(job_id), str(ordinal))),
                        "page_number": page,
                        "ordinal": ordinal,
                        "content": textpart,
                    }
                )
    return chunks


class AnalysisHandler:
    def __init__(self, ctx):
        self.ctx = ctx

    def is_current(self, s, job):
        m = s.get(Material, job.resource_id)
        return bool(m and m.user_id == job.user_id and m.latest_job_id == job.id)

    def set_state(self, s, job, state):
        m = s.get(Material, job.resource_id)
        if m and m.user_id == job.user_id and m.latest_job_id == job.id:
            m.processing_status = {
                "queued": "queued",
                "running": "processing",
                "succeeded": "ready",
                "failed": "failed",
                "outcome_unknown": "failed",
                "cancelled": "failed",
            }[state]
            m.updated_at = utcnow()

    def prepare_snapshot(self, s, job):
        m = owned(s, job.user_id, job.resource_id)
        if not self.is_current(s, job):
            err(409, "STALE_JOB", "Analysis job is stale.")
        return MaterialSnapshot.of(m)

    def prepare_external(self, snapshot):
        m = snapshot
        data = self.ctx.storage.read(m.storage_key)
        instructions = "Return exactly {units:[{title,concepts:[{name,summary,checklist_items:[string],evidence:[{chunk_id,quote}]}]}]}. Cite only supplied chunk IDs; each quote must be an exact nonempty substring of that chunk."
        if ai_recognizes(self.ctx, m.mime_type):
            # Local validation still runs first (format/limits); recognition text
            # then comes from the AI server, so images become plain text chunks.
            extract(m, data, require_text=False)
            pages = self.ctx.ai_documents.extract_pages(m.mime_type, data, material_id=m.id)
            payload = {
                "instructions": instructions,
                "chunks": analysis_chunks(m, data, m.latest_job_id, pages),
            }
            return PreparedAnalysis(m, m.latest_job_id, payload)
        if m.mime_type.startswith("image/"):
            import uuid

            extract(m, data)
            cid = str(uuid.uuid5(uuid.UUID(m.latest_job_id), "1"))
            payload = {
                "instructions": instructions
                + " For this image also return source_text: a faithful transcription of visible text; cite the supplied chunk_id and quote exact substrings of source_text.",
                "image_data_url": "data:" + m.mime_type + ";base64," + base64.b64encode(data).decode(),
                "chunk_id": cid,
            }
        else:
            payload = {"instructions": instructions, "chunks": analysis_chunks(m, data, m.latest_job_id)}
        return PreparedAnalysis(m, m.latest_job_id, payload)

    def _revalidate_prepared(self, s, job, prepared):
        m = owned(s, job.user_id, job.resource_id, True)
        if not self.is_current(s, job) or prepared.job_id != job.id or not prepared.material.matches(m):
            err(409, "STALE_JOB", "Analysis job is stale.")
        return m

    def apply_prepared(self, s, job, prepared):
        m = self._revalidate_prepared(s, job, prepared)
        if "chunks" in prepared.payload and wants_ai_context(self.ctx, job.model_id):
            # The AI server needs the material identity carried by every SourceChunk.
            return {
                **prepared.payload,
                "ai_context": {"material_id": m.id, "material_name": material_name(m)},
            }
        return prepared.payload

    def prepare(self, s, job):
        # Compatibility for direct handler users. The worker always invokes the
        # split interface so external storage/parsing runs outside its transaction.
        return self.prepare_external(self.prepare_snapshot(s, job)).payload

    def persist(self, s, job, output):
        prepared = self.prepare_external(self.prepare_snapshot(s, job))
        return self.persist_prepared(s, job, output, prepared)

    def persist_prepared(self, s, job, output, prepared):
        m = self._revalidate_prepared(s, job, prepared)
        # AI-server recognition already turned an image into text chunks; only the
        # vision path (no "chunks" in the prepared payload) returns source_text.
        if m.mime_type.startswith("image/") and "chunks" not in prepared.payload:
            source = output.get("source_text") if isinstance(output, dict) else None
            if not isinstance(source, str) or not source.strip() or len(source) > 100000:
                err(422, "INVALID_OUTPUT", "Image transcription is invalid.")
            chunks = [
                {
                    "id": prepared.payload["chunk_id"],
                    "page_number": 1,
                    "ordinal": 1,
                    "content": source,
                }
            ]
            output = {k: v for k, v in output.items() if k != "source_text"}
        else:
            chunks = prepared.payload["chunks"]
        cmap = {c["id"]: c for c in chunks}

        def invalid():
            err(422, "INVALID_OUTPUT", "Analysis output is invalid.")

        if (
            not isinstance(output, dict)
            or set(output) != {"units"}
            or not isinstance(output["units"], list)
            or not 1 <= len(output["units"]) <= 200
        ):
            invalid()
        total = 0
        for u in output["units"]:
            if (
                not isinstance(u, dict)
                or set(u) != {"title", "concepts"}
                or not isinstance(u["title"], str)
                or not u["title"].strip()
                or len(u["title"]) > 200
                or not isinstance(u["concepts"], list)
                or not u["concepts"]
            ):
                invalid()
            total += len(u["concepts"])
            if total > 100:
                invalid()
            for c in u["concepts"]:
                if not isinstance(c, dict) or set(c) != {"name", "summary", "checklist_items", "evidence"}:
                    invalid()
                if (
                    not isinstance(c["name"], str)
                    or not c["name"].strip()
                    or len(c["name"]) > 200
                    or not isinstance(c["summary"], str)
                    or not c["summary"].strip()
                    or len(c["summary"]) > 12000
                ):
                    invalid()
                if (
                    not isinstance(c["checklist_items"], list)
                    or not 1 <= len(c["checklist_items"]) <= 50
                    or any(
                        not isinstance(x, str) or not x.strip() or len(x) > 2000 for x in c["checklist_items"]
                    )
                ):
                    invalid()
                if not isinstance(c["evidence"], list) or not c["evidence"]:
                    invalid()
                seen = set()
                for e in c["evidence"]:
                    if (
                        not isinstance(e, dict)
                        or set(e) != {"chunk_id", "quote"}
                        or not isinstance(e["chunk_id"], str)
                        or e["chunk_id"] not in cmap
                        or e["chunk_id"] in seen
                        or not isinstance(e["quote"], str)
                        or not e["quote"].strip()
                        or e["quote"] not in cmap[e["chunk_id"]]["content"]
                    ):
                        invalid()
                    seen.add(e["chunk_id"])
        now = utcnow()
        for c in chunks:
            s.add(
                MaterialChunk(
                    id=c["id"],
                    material_id=m.id,
                    page_number=c["page_number"],
                    ordinal=c["ordinal"],
                    content=c["content"],
                    created_at=now,
                )
            )
        s.flush()
        for ordinal, u in enumerate(output["units"], 1):
            unit = MaterialUnit(id=uid(), material_id=m.id, ordinal=ordinal, title=u["title"])
            s.add(unit)
            s.flush()
            for c in u["concepts"]:
                concept = Concept(id=uid(), material_id=m.id, unit_id=unit.id, created_at=now)
                s.add(concept)
                s.flush()
                v = ConceptVersion(
                    id=uid(),
                    concept_id=concept.id,
                    version_no=1,
                    name=c["name"],
                    summary=c["summary"],
                    is_confirmed=False,
                    confirmed_at=None,
                    created_at=now,
                )
                s.add(v)
                s.flush()
                for i, criterion in enumerate(c["checklist_items"], 1):
                    s.add(
                        ConceptChecklistItem(
                            id=uid(), concept_version_id=v.id, ordinal=i, criterion=criterion
                        )
                    )
                for e in c["evidence"]:
                    s.add(
                        ConceptVersionEvidence(
                            concept_version_id=v.id, chunk_id=e["chunk_id"], quote=e["quote"]
                        )
                    )
        s.flush()
        return f"/v1/materials/{m.id}"
