"""file_chunks 表：文件块索引与内容寻址块的映射，含与 blocks 联查及 refcount 配对调用约定。"""

import sqlite3

from typing import TYPE_CHECKING

from .base import MetadataMixinBase
from .schema import BLOCK_RECORD_SELECT


class ChunkMetadataMixin(MetadataMixinBase):
    """针对 `file_chunks` 与块元数据的读写：`file_chunks` 保存 (file_id, chunk_index) → hash/size，

    块的去重与冷热层信息在 `blocks`（及关联表）中；通过 hash 连接两端。
    单独的 DELETE/UPDATE `file_chunks` 不会自动维护块的引用计数；新增引用时应使用
    `attach_file_chunk_to_block`，移除映射后须在别处对已失效 digest 调用 `decrement_block_refcount`。
    """

    if TYPE_CHECKING:

        def increment_block_refcount(self, digest: str) -> None: ...

    def chunk_block(self, file_id: int, chunk_index: int) -> sqlite3.Row | None:
        """返回指定文件、序号对应 chunk 的映射行，并 JOIN 出该 digest 的完整块记录列。"""
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
        """在闭区间 [first_index, last_index] 内批量查询；键为 chunk_index，值为含块记录的 Row。"""
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
        """该文件在 `file_chunks` 中各行 size 之和（已映射 chunk 的体量合计）。"""
        row = self._db.execute(
            "SELECT COALESCE(SUM(size), 0) AS allocated FROM file_chunks WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        return int(row["allocated"])

    def file_chunk_hash(self, file_id: int, chunk_index: int) -> sqlite3.Row | None:
        """仅读取 `file_chunks` 一行中的 hash（无 JOIN blocks）。"""
        return self._db.execute(
            "SELECT hash FROM file_chunks WHERE file_id = ? AND chunk_index = ?",
            (file_id, chunk_index),
        ).fetchone()

    def delete_file_chunk(self, file_id: int, chunk_index: int) -> None:
        """删除单个 (file_id, chunk_index) 映射行；不修改块 refcount。"""
        self._db.execute(
            "DELETE FROM file_chunks WHERE file_id = ? AND chunk_index = ?",
            (file_id, chunk_index),
        )

    def update_file_chunk_size(self, file_id: int, chunk_index: int, size: int) -> None:
        """更新已有映射行的 size。"""
        self._db.execute(
            "UPDATE file_chunks SET size = ? WHERE file_id = ? AND chunk_index = ?",
            (size, file_id, chunk_index),
        )

    def upsert_file_chunk(
        self, file_id: int, chunk_index: int, digest: str, size: int
    ) -> None:
        """插入或更新 (file_id, chunk_index) 的 hash 与 size；不调整块 refcount。"""
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
        """先 upsert `file_chunks`，再对该 digest 调用 `increment_block_refcount`（新增一条文件→块引用）。"""
        self.upsert_file_chunk(file_id, chunk_index, digest, size)
        self.increment_block_refcount(digest)

    def file_chunk_hashes_from(self, file_id: int, first_index: int) -> list[str]:
        """列出从 `chunk_index >= first_index` 起所有映射行的 hash 列表（顺序与查询一致）。"""
        rows = self._db.execute(
            "SELECT hash FROM file_chunks WHERE file_id = ? AND chunk_index >= ?",
            (file_id, first_index),
        ).fetchall()
        return [row["hash"] for row in rows]

    def delete_file_chunks_from(self, file_id: int, first_index: int) -> None:
        """删除该文件中 `chunk_index >= first_index` 的所有映射行；不修改块 refcount。"""
        self._db.execute(
            "DELETE FROM file_chunks WHERE file_id = ? AND chunk_index >= ?",
            (file_id, first_index),
        )
