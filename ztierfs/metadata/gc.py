"""Deferred physical payload deletion queue.

Business transactions enqueue physical deletes here instead of unlinking files
before SQLite commit. After commit, the filesystem can drain the queue; if the
process crashes first, fsck/cleanup can drain it later.
"""

from __future__ import annotations

import sqlite3

from .base import MetadataMixinBase


class GarbageCollectionMixin(MetadataMixinBase):
    """SQLite helpers for deferred payload deletion."""

    def enqueue_pending_block_file_deletion(
        self, digest: str, tier: int, now: int
    ) -> None:
        """Queue a tiered block file for deletion after the current transaction commits."""
        self._db.execute(
            """
            INSERT OR IGNORE INTO pending_deletions
                (digest, tier, enqueued_ns)
            VALUES (?, ?, ?)
            """,
            (digest, tier, now),
        )

    def pending_deletions(
        self, limit: int = 256, *, tier: int | None = None
    ) -> list[sqlite3.Row]:
        """Return queued physical deletions in FIFO order."""
        tier_filter = "" if tier is None else "WHERE tier = ?"
        params = (limit,) if tier is None else (tier, limit)
        return self._db.execute(
            f"""
            SELECT id, digest, tier, enqueued_ns
            FROM pending_deletions
            {tier_filter}
            ORDER BY id
            LIMIT ?
            """,
            params,
        ).fetchall()

    def remove_pending_deletion(self, deletion_id: int) -> None:
        """Remove a queue item after its physical delete has succeeded or is unnecessary."""
        self._db.execute(
            "DELETE FROM pending_deletions WHERE id = ?",
            (deletion_id,),
        )

    def remove_pending_deletions(self, deletion_ids: list[int]) -> None:
        """Remove queue items after their physical deletes have succeeded or are unnecessary."""
        if not deletion_ids:
            return
        self._db.executemany(
            "DELETE FROM pending_deletions WHERE id = ?",
            [(deletion_id,) for deletion_id in deletion_ids],
        )

    def defer_pending_deletion(self, deletion_id: int, now: int) -> None:
        """Keep a failed queue item and refresh its timestamp for later retry."""
        self._db.execute(
            "UPDATE pending_deletions SET enqueued_ns = ? WHERE id = ?",
            (now, deletion_id),
        )

    def defer_pending_deletions(self, deletion_ids: list[int], now: int) -> None:
        """Refresh failed queue items for later retry."""
        if not deletion_ids:
            return
        self._db.executemany(
            "UPDATE pending_deletions SET enqueued_ns = ? WHERE id = ?",
            [(now, deletion_id) for deletion_id in deletion_ids],
        )
