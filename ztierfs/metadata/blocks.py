import sqlite3

from .base import MetadataMixinBase
from .schema import BLOCK_RECORD_SELECT


class BlockMetadataMixin(MetadataMixinBase):
    def block_exists(self, digest: str) -> bool:
        return (
            self._db.execute("SELECT 1 FROM blocks WHERE hash = ?", (digest,)).fetchone()
            is not None
        )

    def block_record(self, digest: str) -> sqlite3.Row | None:
        return self._db.execute(
            f"""
            SELECT *
            FROM ({BLOCK_RECORD_SELECT}) AS block_record
            WHERE hash = ?
            """,
            (digest,),
        ).fetchone()

    def block_refcount_and_presence(self, digest: str) -> sqlite3.Row | None:
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
        is_inline = inline_payload is not None
        payload_store = self.payload_store.name if is_inline else "sqlite"
        payload_key = None
        stored_payload = inline_payload
        if is_inline and payload_store != "sqlite":
            payload_key = f"block/{digest}"
            assert inline_payload is not None
            self.payload_store.put(payload_key, inline_payload)
            stored_payload = None
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
                INSERT INTO block_payloads (hash, payload, payload_store, payload_key)
                VALUES (?, ?, ?, ?)
                """,
                (digest, stored_payload, payload_store, payload_key),
            )
        else:
            self._db.execute(
                "INSERT INTO block_locations (hash, tier) VALUES (?, 1)",
                (digest,),
            )

    def increment_block_refcount(self, digest: str) -> None:
        self._db.execute(
            "UPDATE blocks SET refcount = refcount + 1 WHERE hash = ?", (digest,)
        )

    def decrement_block_refcount(self, digest: str) -> None:
        self._db.execute(
            "UPDATE blocks SET refcount = refcount - 1 WHERE hash = ?", (digest,)
        )

    def delete_block(self, digest: str) -> None:
        row = self._db.execute(
            """
            SELECT payload_store, payload_key
            FROM block_payloads
            WHERE hash = ?
            """,
            (digest,),
        ).fetchone()
        self._db.execute("DELETE FROM blocks WHERE hash = ?", (digest,))
        if row is not None and row["payload_store"] != "sqlite":
            self.payload_store.delete(row["payload_key"])

    def inline_block_payload(self, digest: str) -> bytes | None:
        row = self._db.execute(
            """
            SELECT payload, payload_store, payload_key
            FROM block_payloads
            WHERE hash = ?
            """,
            (digest,),
        ).fetchone()
        if row is None:
            return None
        if row["payload_store"] == "sqlite":
            return bytes(row["payload"])
        return self.payload_store.get(row["payload_key"])

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
