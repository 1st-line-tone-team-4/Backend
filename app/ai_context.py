"""Build the AI server's StudyContext from canonical rows (read-only).

The team AI server (repo AI, ``ai-internal-api.yaml``) never reads our DB: every
generation request carries the confirmed concept versions and the source chunks
they cite. Handlers add this under ``payload["ai_context"]`` only when the job's
model is routed to the AI server, so OpenAI/Anthropic prompts are unchanged.

Callers must already have verified ownership of the versions they pass in.
"""

from sqlalchemy import select

from .core import iso
from .models import (
    Concept,
    ConceptChecklistItem,
    ConceptVersion,
    ConceptVersionEvidence,
    Material,
    MaterialChunk,
)

# AI server field limits (src/models.py): SourceEvidence.excerpt <= 2000 chars,
# Name <= 200 chars. Backend chunks are <= 4000 chars and fit SourceChunk.text.
MAX_EXCERPT = 2000
MAX_NAME = 200


def wants_ai_context(ctx, model_id):
    check = getattr(getattr(ctx, "providers", None), "uses_ai_server", None)
    return bool(callable(check) and check(model_id))


def material_name(material):
    return (material.filename or "material")[:MAX_NAME]


def current(session, concept_id):
    # Same rule as app.materials.current; kept here to avoid an import cycle.
    return session.scalar(
        select(ConceptVersion)
        .where(ConceptVersion.concept_id == concept_id)
        .order_by(ConceptVersion.version_no.desc())
        .limit(1)
    )


def confirmed_concept(session, version, evidence, *, is_current=None):
    """One ConfirmedConcept; ``evidence`` is a list of SourceEvidence dicts."""
    concept = session.get(Concept, version.concept_id)
    items = session.scalars(
        select(ConceptChecklistItem)
        .where(ConceptChecklistItem.concept_version_id == version.id)
        .order_by(ConceptChecklistItem.ordinal)
    ).all()
    if is_current is None:
        is_current = current(session, concept.id).id == version.id
    return {
        "concept_id": concept.id,
        "unit_id": concept.unit_id,
        "material_id": concept.material_id,
        "version": {
            "id": version.id,
            "version_no": version.version_no,
            "name": version.name[:MAX_NAME],
            "summary": version.summary,
            "is_confirmed": bool(version.is_confirmed),
            "is_current": bool(is_current),
            "confirmed_at": iso(version.confirmed_at),
            "checklist_items": [{"id": i.id, "ordinal": i.ordinal, "criterion": i.criterion} for i in items],
            "evidence": evidence,
        },
    }


def study_context(session, version_ids):
    """StudyContext for confirmed versions; chunks carry their full stored text."""
    concepts, chunks = [], {}
    for version_id in dict.fromkeys(version_ids):
        version = session.get(ConceptVersion, version_id)
        evidence = []
        pins = session.scalars(
            select(ConceptVersionEvidence)
            .where(ConceptVersionEvidence.concept_version_id == version.id)
            .order_by(ConceptVersionEvidence.chunk_id)
        ).all()
        for pin in pins:
            chunk = session.get(MaterialChunk, pin.chunk_id)
            material = session.get(Material, chunk.material_id)
            excerpt = pin.quote if pin.quote is not None else chunk.content
            evidence.append(
                {
                    "chunk_id": chunk.id,
                    "material_id": material.id,
                    "material_name": material_name(material),
                    "page_number": chunk.page_number,
                    # A prefix stays a verbatim substring of the chunk text.
                    "excerpt": excerpt[:MAX_EXCERPT],
                }
            )
            chunks.setdefault(
                chunk.id,
                {
                    "id": chunk.id,
                    "material_id": material.id,
                    "material_name": material_name(material),
                    "page_number": chunk.page_number,
                    "text": chunk.content,
                },
            )
        concepts.append(confirmed_concept(session, version, evidence))
    return {"concepts": concepts, "chunks": list(chunks.values())}


def pinned_study_context(session, version, source_evidence):
    """StudyContext limited to a teach-back session's pinned excerpts.

    Report validation accepts only excerpts inside each pinned ``source_excerpt``,
    so the AI server receives exactly that text as the chunk.
    """
    evidence, chunks = [], []
    for item in source_evidence:
        name = (item["material_name"] or "material")[:MAX_NAME]
        evidence.append(
            {
                "chunk_id": item["chunk_id"],
                "material_id": item["material_id"],
                "material_name": name,
                "page_number": item["page_number"],
                "excerpt": item["source_excerpt"][:MAX_EXCERPT],
            }
        )
        chunks.append(
            {
                "id": item["chunk_id"],
                "material_id": item["material_id"],
                "material_name": name,
                "page_number": item["page_number"],
                "text": item["source_excerpt"],
            }
        )
    return {"concepts": [confirmed_concept(session, version, evidence)], "chunks": chunks}
