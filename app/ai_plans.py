"""Study plan scheduling by the team AI server (``POST /internal/study-plan/generate``).

The AI server's planner is pure Python (no external model call): it orders
concepts by weakness/progress and spreads learn → practice → teach-back →
review across the days before the exam. The backend keeps ownership, IDs,
persistence and the public contract, so the AI plan is mapped onto the public
task types (contract 1.4.0):

- ``practice`` → ``solve`` (count = questions), ``teachback`` → ``teach_back``,
  ``review`` → ``review``.
- ``learn`` has no public task type yet and is stored as ``review``: both open the
  concept study screen (learning explanation + checklist) in the frontend and are
  completed there, so the AI plan's first study day stays visible on /today.
- ``target_count`` is capped at 20 per public task; larger counts are split into
  several tasks on the same day.

Any problem (no AI server, request too large for its limits, transport error,
unexpected response) returns ``None`` and the caller uses the local scheduler,
so plan creation never depends on the AI server being up.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx

from .ai_context import study_context

log = logging.getLogger(__name__)

PLAN_TIMEOUT_SECONDS = 20
MAX_TARGET = 20
MAX_CONCEPTS = 100
MAX_CHUNKS = 200
MAX_REQUEST_BYTES = 2 * 1024 * 1024 - 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TASK_TYPES = {"learn": "review", "practice": "solve", "teachback": "teach_back", "review": "review"}


def ai_plan_specs(ctx, db, rows, materials, *, exam_date, today):
    """Return ``[(due_date, task_type, target_count, concept, version)]`` or ``None``."""
    client = getattr(ctx, "ai_documents", None)
    if client is None or len(rows) > MAX_CONCEPTS:
        return None
    from .ai_generation import _ChunkIds, _context, _encode, strict_json

    by_version = {version.id: (concept, version) for concept, version, _unit in rows}
    try:
        context = study_context(db, list(by_version))
        if not context["chunks"] or len(context["chunks"]) > MAX_CHUNKS:
            return None
        body = {
            "total_pages": max(1, sum(m.page_count or 1 for m in materials)),
            "context": _context(context, _ChunkIds()),
            "exam_date": exam_date.isoformat(),
            "today": today.isoformat(),
        }
        content = _encode(body)
        if len(content) > MAX_REQUEST_BYTES:
            return None
        with httpx.Client(
            transport=client.transport, timeout=PLAN_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
        ) as http:
            response = http.post(
                client.base_url + "/internal/study-plan/generate",
                content=content,
                headers={
                    "X-AI-API-Key": client.api_key,
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            )
        if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
            log.warning("AI study plan unavailable (status %s); using local scheduler", response.status_code)
            return None
        plan = strict_json(response.content)
        specs = []
        for day in sorted(plan["days"], key=lambda d: d["date"]):
            due = date.fromisoformat(day["date"])
            if due < today or due > exam_date:
                return None
            for task in day["tasks"]:
                kind = TASK_TYPES.get(task["activity"])
                if kind is None:
                    continue
                pair = by_version.get(task["concept_version_id"])
                count = task["count"]
                if pair is None or pair[0].id != task["concept_id"] or type(count) is not int or count < 1:
                    return None
                while count > 0:
                    specs.append((due, kind, min(count, MAX_TARGET), pair[0], pair[1]))
                    count -= MAX_TARGET
        return specs or None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("AI study plan failed (%s); using local scheduler", type(exc).__name__)
        return None
