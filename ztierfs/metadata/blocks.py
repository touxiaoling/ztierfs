"""blocks / block_locations / block_payloads 上的 refcount 与冷热层（tier）信息。

block_locations.tier 约定：1 表示热层，2 表示冷层；inline 块不走冷热复制路径。
"""

import sqlite3

from .base import MetadataMixinBase
from .schema import BLOCK_RECORD_SELECT


class BlockMetadataMixin(MetadataMixinBase):
    """内容寻址块在库内的存在性、引用计数与冷热位置更新。"""

    def block_exists(self, digest: str) -> bool:
        """处理 block exists。"""
        return (
            self._db.execute(
                "SELECT 1 FROM blocks WHERE hash = ?", (digest,)
            ).fetchone()
            is not None
        )

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

    def block_refcount_and_presence(self, digest: str) -> sqlite3.Row | None:
        """处理 block refcount and presence。"""
        return self._db.execute(
            """
            SELECT
                blocks.refcount,
                blocks.storage_kind AS storage,
                CASE WHEN hot_locations.hash IS NULL THEN 0 ELSE 1 END AS hot_present,
                CASE WHEN cold_locations.hash IS NULL THEN 0 ELSE 1 END AS cold_present,
                blocks.preferred_tier
            FROM blocks
            LEFT JOIN block_locations AS hot_locations
              ON hot_locations.hash = blocks.hash AND hot_locations.tier = 1
            LEFT JOIN block_locations AS cold_locations
              ON cold_locations.hash = blocks.hash AND cold_locations.tier = 2
            WHERE blocks.hash = ?
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
                refcount, atime_ns, read_count
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0)
            """,
            (
                digest,
                "inline" if is_inline else "tiered",
                0 if is_inline else 1,
                int(compressed),
                raw_size,
                stored_size,
                now,
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
        else:
            self._db.execute(
                "INSERT INTO block_locations (hash, tier) VALUES (?, 1)",
                (digest,),
            )

    def increment_block_refcount(self, digest: str) -> None:
        """将指定块的引用计数加一（有新 chunk 或链接指向该内容地址块时调用）。"""
        self._db.execute(
            "UPDATE blocks SET refcount = refcount + 1 WHERE hash = ?", (digest,)
        )

    def decrement_block_refcount(self, digest: str) -> None:
        """将指定块的引用计数减一（移除 chunk 引用或 unlink 导致释放时调用）。"""
        self._db.execute(
            "UPDATE blocks SET refcount = refcount - 1 WHERE hash = ?", (digest,)
        )

    def delete_block(self, digest: str) -> None:
        """删除 block 元数据。"""
        self._db.execute("DELETE FROM blocks WHERE hash = ?", (digest,))

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

        hot_present / cold_present 为 True 时在对应 tier（1=热，2=冷）插入
        block_locations 行，为 False 时删除；不传则不改该行。
        preferred_tier、last_promoted_ns、last_demoted_ns、cold_verified_ns 仅更新
        blocks 表中已给出的列；若本次调用未产生任何 blocks 列赋值则直接返回。
        """
        assignments: list[str] = []
        values: list[int | str | None] = []
        if hot_present is not None:
            self._set_block_location(digest, 1, hot_present)
        if cold_present is not None:
            self._set_block_location(digest, 2, cold_present)
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

    def _set_block_location(self, digest: str, tier: int, present: bool) -> None:
        """按 tier（1 热 / 2 冷）插入或删除 block_locations 一行。"""
        if present:
            self._db.execute(
                "INSERT OR IGNORE INTO block_locations (hash, tier) VALUES (?, ?)",
                (digest, tier),
            )
        else:
            self._db.execute(
                "DELETE FROM block_locations WHERE hash = ? AND tier = ?",
                (digest, tier),
            )

    def hot_tier_stored_size(self) -> int:
        """处理 hot tier stored size。"""
        return self._db.execute(
            """
            SELECT COALESCE(SUM(stored_size), 0)
            FROM blocks
            JOIN block_locations ON block_locations.hash = blocks.hash
            WHERE blocks.storage_kind = 'tiered' AND block_locations.tier = 1
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
                CASE
                    WHEN cold_locations.hash IS NULL THEN 0
                    ELSE 1
                END AS cold_present,
                COALESCE(MIN(file_chunks.chunk_index), 2147483647) AS first_chunk_index
            FROM blocks
            JOIN block_locations AS hot_locations
              ON hot_locations.hash = blocks.hash AND hot_locations.tier = 1
            LEFT JOIN block_locations AS cold_locations
              ON cold_locations.hash = blocks.hash AND cold_locations.tier = 2
            LEFT JOIN file_chunks ON file_chunks.hash = blocks.hash
            WHERE blocks.storage_kind = 'tiered'
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
            JOIN block_locations AS hot_locations
              ON hot_locations.hash = blocks.hash AND hot_locations.tier = 1
            JOIN block_locations AS cold_locations
              ON cold_locations.hash = blocks.hash AND cold_locations.tier = 2
            WHERE blocks.storage_kind = 'tiered'
              AND blocks.preferred_tier = 1
              AND blocks.last_promoted_ns IS NOT NULL
              AND blocks.last_promoted_ns <= ?
            ORDER BY last_promoted_ns ASC
            """,
            (cutoff_ns,),
        ).fetchall()
