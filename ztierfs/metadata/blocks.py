"""blocks / block_payloads 上的 refcount 与冷热层（tier）信息。"""

import sqlite3

from collections.abc import Mapping
from dataclasses import dataclass
from time import time_ns
from typing import TYPE_CHECKING

from .base import MetadataMixinBase
from .schema import BLOCK_RECORD_SELECT


@dataclass(frozen=True)
class BlockInsert:
    digest: str
    compressed: bool
    raw_size: int
    stored_size: int
    inline_payload: bytes | None = None


class BlockMetadataMixin(MetadataMixinBase):
    """内容寻址块在库内的存在性、引用计数与冷热位置更新。"""

    if TYPE_CHECKING:

        def enqueue_pending_block_file_deletion(
            self, digest: str, tier: int, now: int
        ) -> None: ...

    def block_exists(self, digest: str) -> bool:
        """处理 block exists。"""
        return (
            self._db.execute(
                "SELECT 1 FROM blocks WHERE hash = ?", (digest,)
            ).fetchone()
            is not None
        )

    def existing_block_hashes(self, digests: list[str]) -> set[str]:
        """Return the subset of ``digests`` that already have block metadata."""
        if not digests:
            return set()
        placeholders = ",".join("?" for _ in digests)
        rows = self._db.execute(
            f"SELECT hash FROM blocks WHERE hash IN ({placeholders})",
            digests,
        ).fetchall()
        return {row["hash"] for row in rows}

    def block_record(self, digest: str) -> sqlite3.Row | None:
        """处理 block record。"""
        return self._db.execute(
            f"""
            SELECT *
            FROM ({BLOCK_RECORD_SELECT}) AS block_record
            WHERE hash = ?
            """,
            (digest,),
        ).fetchone()

    def block_records(self, digests: list[str]) -> dict[str, sqlite3.Row]:
        """Return block records for the requested digests, keyed by hash."""
        if not digests:
            return {}
        placeholders = ",".join("?" for _ in digests)
        rows = self._db.execute(
            f"""
            SELECT *
            FROM ({BLOCK_RECORD_SELECT}) AS block_record
            WHERE hash IN ({placeholders})
            """,
            digests,
        ).fetchall()
        return {row["hash"]: row for row in rows}

    def block_refcount_and_presence(self, digest: str) -> sqlite3.Row | None:
        """处理 block refcount and presence。"""
        return self._db.execute(
            """
            SELECT
                refcount,
                storage_kind AS storage,
                hot_present,
                cold_present,
                preferred_tier
            FROM blocks
            WHERE hash = ?
            """,
            (digest,),
        ).fetchone()

    def insert_block(
        self,
        digest: str,
        *,
        compressed: bool,
        raw_size: int,
        stored_size: int,
        now: int,
        inline_payload: bytes | None = None,
    ) -> None:
        """处理 insert block。"""
        is_inline = inline_payload is not None
        self._db.execute(
            """
            INSERT INTO blocks (
                hash, storage_kind, preferred_tier, compressed, raw_size, stored_size,
                refcount, atime_ns, read_count, hot_present, cold_present
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?)
            """,
            (
                digest,
                "inline" if is_inline else "tiered",
                0 if is_inline else 1,
                int(compressed),
                raw_size,
                stored_size,
                now,
                0 if is_inline else 1,
                0,
            ),
        )
        if is_inline:
            self._db.execute(
                """
                INSERT INTO block_payloads (hash, payload)
                VALUES (?, ?)
                """,
                (digest, inline_payload),
            )

    def insert_blocks(self, blocks: list[BlockInsert], *, now: int) -> None:
        """Insert multiple new block records and optional inline payload rows."""
        if not blocks:
            return
        block_rows = []
        payload_rows = []
        for block in blocks:
            digest = block.digest
            inline_payload = block.inline_payload
            is_inline = inline_payload is not None
            block_rows.append(
                (
                    digest,
                    "inline" if is_inline else "tiered",
                    0 if is_inline else 1,
                    int(block.compressed),
                    block.raw_size,
                    block.stored_size,
                    now,
                    0 if is_inline else 1,
                    0,
                )
            )
            if is_inline:
                payload_rows.append((digest, inline_payload))
        self._db.executemany(
            """
            INSERT INTO blocks (
                hash, storage_kind, preferred_tier, compressed, raw_size, stored_size,
                refcount, atime_ns, read_count, hot_present, cold_present
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?)
            """,
            block_rows,
        )
        if payload_rows:
            self._db.executemany(
                """
                INSERT INTO block_payloads (hash, payload)
                VALUES (?, ?)
                """,
                payload_rows,
            )

    def increment_block_refcount(self, digest: str) -> None:
        """将指定块的引用计数加一（有新 chunk 或链接指向该内容地址块时调用）。"""
        self._db.execute(
            """
            UPDATE blocks
            SET refcount = refcount + 1,
                cold_gc_enqueued_ns = NULL
            WHERE hash = ?
            """,
            (digest,),
        )

    def decrement_block_refcount(self, digest: str) -> None:
        """将指定块的引用计数减一（移除 chunk 引用或 unlink 导致释放时调用）。"""
        self._db.execute(
            "UPDATE blocks SET refcount = refcount - 1 WHERE hash = ?", (digest,)
        )

    def apply_block_refcount_deltas(
        self, deltas: Mapping[str, int], *, now: int | None = None
    ) -> None:
        """批量应用内容块引用计数变化；降至 0 的 tiered 块登记提交后物理删除。

        ``deltas`` 中正数表示新增 chunk 引用，负数表示移除引用，0 会被忽略。调用方应已在
        同一事务内完成对应 ``file_chunks`` 变更；本函数负责把 ``blocks.refcount`` 与之收敛。
        """
        pending = {digest: delta for digest, delta in deltas.items() if delta}
        if not pending:
            return

        positive_updates = [
            (delta, digest) for digest, delta in pending.items() if delta > 0
        ]
        if positive_updates:
            self._db.executemany(
                """
                UPDATE blocks
                SET refcount = refcount + ?,
                    cold_gc_enqueued_ns = NULL
                WHERE hash = ?
                """,
                positive_updates,
            )

        negative_digests = [digest for digest, delta in pending.items() if delta < 0]
        if not negative_digests:
            return
        deletion_time = time_ns() if now is None else now
        placeholders = ",".join("?" for _ in negative_digests)
        rows = self._db.execute(
            f"""
            SELECT
                hash,
                refcount,
                storage_kind AS storage,
                hot_present,
                cold_present
            FROM blocks
            WHERE hash IN ({placeholders})
            """,
            negative_digests,
        ).fetchall()
        rows_by_hash = {row["hash"]: row for row in rows}
        for digest in negative_digests:
            row = rows_by_hash.get(digest)
            if row is None:
                raise RuntimeError(
                    f"block refcount delta targets missing block {digest}"
                )
            new_refcount = int(row["refcount"]) + pending[digest]
            if new_refcount < 0:
                raise RuntimeError(f"block refcount would become negative for {digest}")
            if new_refcount > 0:
                self._db.execute(
                    "UPDATE blocks SET refcount = ? WHERE hash = ?",
                    (new_refcount, digest),
                )
                continue
            if row["storage"] == "tiered" and row["cold_present"]:
                self.mark_cold_garbage(digest, deletion_time)
                if row["hot_present"]:
                    self.enqueue_pending_block_file_deletion(digest, 1, deletion_time)
                continue
            self.delete_block(digest)
            if row["storage"] == "tiered" and row["hot_present"]:
                self.enqueue_pending_block_file_deletion(digest, 1, deletion_time)

    def delete_block(self, digest: str) -> None:
        """删除 block 元数据。"""
        self._db.execute("DELETE FROM blocks WHERE hash = ?", (digest,))

    def mark_cold_garbage(self, digest: str, now: int) -> None:
        """Keep an unreferenced cold copy indexed for later reuse and maintenance GC."""
        self._db.execute(
            """
            UPDATE blocks
            SET refcount = 0,
                hot_present = 0,
                cold_present = 1,
                preferred_tier = 2,
                cold_gc_enqueued_ns = COALESCE(cold_gc_enqueued_ns, ?)
            WHERE hash = ?
            """,
            (now, digest),
        )

    def inline_block_payload(self, digest: str) -> bytes | None:
        """处理 inline block payload。"""
        row = self._db.execute(
            """
            SELECT payload
            FROM block_payloads
            WHERE hash = ?
            """,
            (digest,),
        ).fetchone()
        if row is None:
            return None
        return bytes(row["payload"])

    def set_block_presence(
        self,
        digest: str,
        *,
        hot_present: bool | None = None,
        cold_present: bool | None = None,
        preferred_tier: int | None = None,
        last_promoted_ns: int | None = None,
        last_demoted_ns: int | None = None,
        cold_verified_ns: int | None = None,
    ) -> None:
        """更新块在冷热层的物理存在标记及 blocks 表中的冷热迁移动态字段。

        hot_present / cold_present 不传则不改；preferred_tier、last_promoted_ns、
        last_demoted_ns、cold_verified_ns 仅更新已给出的列；若本次调用未产生任何赋值则直接返回。
        """
        assignments: list[str] = []
        values: list[int | str | None] = []
        if hot_present is not None:
            assignments.append("hot_present = ?")
            values.append(int(hot_present))
        if cold_present is not None:
            assignments.append("cold_present = ?")
            values.append(int(cold_present))
        if preferred_tier is not None:
            assignments.append("preferred_tier = ?")
            values.append(preferred_tier)
        if last_promoted_ns is not None:
            assignments.append("last_promoted_ns = ?")
            values.append(last_promoted_ns)
        if last_demoted_ns is not None:
            assignments.append("last_demoted_ns = ?")
            values.append(last_demoted_ns)
        if cold_verified_ns is not None:
            assignments.append("cold_verified_ns = ?")
            values.append(cold_verified_ns)
        if not assignments:
            return
        values.append(digest)
        self._db.execute(
            f"UPDATE blocks SET {', '.join(assignments)} WHERE hash = ?",
            values,
        )

    def hot_tier_stored_size(self) -> int:
        """处理 hot tier stored size。"""
        return self._db.execute(
            """
            SELECT COALESCE(SUM(stored_size), 0)
            FROM blocks
            WHERE storage_kind = 'tiered' AND hot_present = 1
            """
        ).fetchone()[0]

    def demotion_candidate(
        self,
        *,
        protected_prefix_chunks: int,
        max_atime_ns: int,
    ) -> sqlite3.Row | None:
        """选出一只适合从热层降级到冷层的块（若无可选则返回 None）。

        查询意图：仅考虑 tiered 且当前在热层存在位置的块；最近访问时间不晚于
        max_atime_ns；排除任一所属文件的 chunk_index 落在「文件开头受保护前缀」
        （chunk_index < protected_prefix_chunks）内的块，避免大块头部频繁读被迁冷；
        与 file_chunks 左连接得到该块在各文件中出现的最早 chunk 序号 first_chunk_index。
        分组后在候选集中按「读次数升序 → 越早出现在文件中的块越优先（序号降序）→
        访问时间升序」排序，取 LIMIT 1。返回列含 hash、stored_size、cold_present、
        first_chunk_index（无关联 chunk 时用极大序号占位）。
        """
        return self._db.execute(
            """
            SELECT
                blocks.hash,
                blocks.stored_size,
                blocks.cold_present,
                COALESCE(MIN(file_chunks.chunk_index), 2147483647) AS first_chunk_index
            FROM blocks
            LEFT JOIN file_chunks ON file_chunks.hash = blocks.hash
            WHERE blocks.storage_kind = 'tiered'
              AND blocks.hot_present = 1
              AND blocks.atime_ns <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM file_chunks AS protected_chunks
                  WHERE protected_chunks.hash = blocks.hash
                    AND protected_chunks.chunk_index < ?
              )
            GROUP BY blocks.hash
            ORDER BY
                blocks.read_count ASC,
                first_chunk_index DESC,
                blocks.atime_ns ASC
            LIMIT 1
            """,
            (max_atime_ns, protected_prefix_chunks),
        ).fetchone()

    def promoted_cold_copy_candidates(self, cutoff_ns: int) -> list[sqlite3.Row]:
        """处理 promoted cold copy candidates。"""
        return self._db.execute(
            """
            SELECT blocks.hash
            FROM blocks
            WHERE blocks.storage_kind = 'tiered'
              AND blocks.hot_present = 1
              AND blocks.cold_present = 1
              AND blocks.preferred_tier = 1
              AND blocks.last_promoted_ns IS NOT NULL
              AND blocks.last_promoted_ns <= ?
            ORDER BY last_promoted_ns ASC
            """,
            (cutoff_ns,),
        ).fetchall()
