import sqlite3

from .base import MetadataMixinBase
from .schema import BLOCK_RECORD_SELECT


class ChunkMetadataMixin(MetadataMixinBase):
    def chunk_block(self, file_id: int, chunk_index: int) -> sqlite3.Row | None:
        return self._db.execute(
            f"""
            SELECT file_chunks.size, block_record.*
            FROM file_chunks
            JOIN ({BLOCK_RECORD_SELECT}) AS block_record
                ON block_record.hash = file_chunks.hash
            WHERE file_chunks.file_id = ? AND file_chunks.chunk_index = ?
            """,
            (file_id, chunk_index),
        ).fetchone()

    def chunk_blocks(
        self, file_id: int, first_index: int, last_index: int
    ) -> dict[int, sqlite3.Row]:
        rows = self._db.execute(
            f"""
            SELECT file_chunks.chunk_index, file_chunks.size, block_record.*
            FROM file_chunks
            JOIN ({BLOCK_RECORD_SELECT}) AS block_record
                ON block_record.hash = file_chunks.hash
            WHERE file_chunks.file_id = ?
              AND file_chunks.chunk_index BETWEEN ? AND ?
            """,
            (file_id, first_index, last_index),
        ).fetchall()
        return {row["chunk_index"]: row for row in rows}

    def file_allocated_size(self, file_id: int) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(size), 0) AS allocated FROM file_chunks WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        return int(row["allocated"])

    def file_chunk_hash(self, file_id: int, chunk_index: int) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT hash FROM file_chunks WHERE file_id = ? AND chunk_index = ?",
            (file_id, chunk_index),
        ).fetchone()

    def delete_file_chunk(self, file_id: int, chunk_index: int) -> None:
        self._db.execute(
            "DELETE FROM file_chunks WHERE file_id = ? AND chunk_index = ?",
            (file_id, chunk_index),
        )

    def update_file_chunk_size(self, file_id: int, chunk_index: int, size: int) -> None:
        self._db.execute(
            "UPDATE file_chunks SET size = ? WHERE file_id = ? AND chunk_index = ?",
            (size, file_id, chunk_index),
        )

    def upsert_file_chunk(
        self, file_id: int, chunk_index: int, digest: str, size: int
    ) -> None:
        self._db.execute(
            """
            INSERT INTO file_chunks (file_id, chunk_index, hash, size)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(file_id, chunk_index) DO UPDATE SET
                hash = excluded.hash,
                size = excluded.size
            """,
            (file_id, chunk_index, digest, size),
        )

    def attach_file_chunk_to_block(
        self, file_id: int, chunk_index: int, digest: str, size: int
    ) -> None:
        """Record a chunk reference and its block refcount in one transaction."""
        self.upsert_file_chunk(file_id, chunk_index, digest, size)
        self.increment_block_refcount(digest)

    def file_chunk_hashes_from(self, file_id: int, first_index: int) -> list[str]:
        rows = self._db.execute(
            "SELECT hash FROM file_chunks WHERE file_id = ? AND chunk_index >= ?",
            (file_id, first_index),
        ).fetchall()
        return [row["hash"] for row in rows]

    def delete_file_chunks_from(self, file_id: int, first_index: int) -> None:
        self._db.execute(
            "DELETE FROM file_chunks WHERE file_id = ? AND chunk_index >= ?",
            (file_id, first_index),
        )
