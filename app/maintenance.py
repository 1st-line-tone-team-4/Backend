"""Idempotent private-source deletion outbox consumer."""

from datetime import timedelta

from sqlalchemy import select

from .core import utcnow
from .models import StorageDeletion


def drain_deletions(ctx, limit=100):
    """Remove due objects, retaining failed work for bounded backoff retry.

    File deletion is idempotent. A crash after deleting bytes and before commit
    replays safely. Holding the DB row lock avoids competing consumers on MySQL;
    SQLite remains safe because missing local files count as already deleted.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Deletion batch size must be between 1 and 100")
    removed = 0
    with ctx.db.transaction() as session:
        rows = session.scalars(
            select(StorageDeletion)
            .where(StorageDeletion.deleted_at.is_(None), StorageDeletion.delete_after <= utcnow())
            .order_by(StorageDeletion.delete_after, StorageDeletion.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.attempt_count += 1
            try:
                ctx.storage.delete(row.storage_key)
            except OSError:
                # The row remains pending. Do not log private object names.
                row.delete_after = utcnow() + timedelta(seconds=min(3600, 2 ** min(row.attempt_count, 12)))
            else:
                row.deleted_at = utcnow()
                removed += 1
    return removed
