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
                (kind, digest, tier, payload_key, enqueued_ns)
            VALUES ('block_file', ?, ?, NULL, ?)
            """,
            (digest, tier, now),
        )

    def enqueue_pending_payload_deletion(self, payload_key: str, now: int) -> None:
        """Queue an external payload-store object for deletion after commit."""
        self._db.execute(
            """
            INSERT OR IGNORE INTO pending_deletions
                (kind, digest, tier, payload_key, enqueued_ns)
            VALUES ('payload_store', NULL, NULL, ?, ?)
            """,
            (payload_key, now),
        )

    def pending_deletions(self, limit: int = 256) -> list[sqlite3.Row]:
        """Return queued physical deletions in FIFO order."""
        return self._db.execute(
            """
            SELECT id, kind, digest, tier, payload_key, enqueued_ns
            FROM pending_deletions
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def remove_pending_deletion(self, deletion_id: int) -> None:
        """Remove a queue item after its physical delete has succeeded or is unnecessary."""
        self._db.execute(
            "DELETE FROM pending_deletions WHERE id = ?",
            (deletion_id,),
        )

    def defer_pending_deletion(self, deletion_id: int, now: int) -> None:
        """Keep a failed queue item and refresh its timestamp for later retry."""
        self._db.execute(
            "UPDATE pending_deletions SET enqueued_ns = ? WHERE id = ?",
            (now, deletion_id),
        )
