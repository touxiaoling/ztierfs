from collections.abc import Iterator
from dataclasses import dataclass

from .base import MetadataMixinBase

DEFAULT_DEFERRED_ACCESS_FLUSH_BLOCKS = 64
DEFAULT_DEFERRED_ACCESS_FLUSH_NS = 1_000_000_000


@dataclass(frozen=True)
class BlockAccessStats:
    digest: str
    atime_ns: int
    read_count: int



class AccessStatsMixin(MetadataMixinBase):
    def defer_node_atime(self, node_id: int, now: int) -> bool:
        with self._deferred_access_lock:
            self._start_deferred_accesses(now)
            self._deferred_node_atimes[node_id] = now
            return self._deferred_accesses_should_flush(now)

    def touch_block_atime(self, digest: str, now: int) -> None:
        self._db.execute(
            """
            UPDATE blocks
            SET atime_ns = ?, read_count = read_count + 1
            WHERE hash = ?
            """,
            (now, digest),
        )

    def defer_block_accesses(self, digests: Iterator[str], now: int) -> bool:
        with self._deferred_access_lock:
            self._start_deferred_accesses(now)
            for digest in digests:
                current = self._deferred_block_accesses.get(digest)
                read_count = 1 if current is None else current.read_count + 1
                self._deferred_block_accesses[digest] = BlockAccessStats(
                    digest=digest,
                    atime_ns=now,
                    read_count=read_count,
                )
            return self._deferred_accesses_should_flush(now)

    def has_deferred_accesses(self) -> bool:
        with self._deferred_access_lock:
            return bool(self._deferred_node_atimes or self._deferred_block_accesses)

    def flush_deferred_accesses(self) -> None:
        with self._deferred_access_lock:
            node_atimes = self._deferred_node_atimes
            block_accesses = list(self._deferred_block_accesses.values())
            self._deferred_node_atimes = {}
            self._deferred_block_accesses = {}
            self._deferred_access_started_ns = None

        if node_atimes:
            self._db.executemany(
                "UPDATE inodes SET atime_ns = ? WHERE id = ?",
                [(atime_ns, node_id) for node_id, atime_ns in node_atimes.items()],
            )
        if block_accesses:
            self._db.executemany(
                """
                UPDATE blocks
                SET atime_ns = ?, read_count = read_count + ?
                WHERE hash = ?
                """,
                [
                    (access.atime_ns, access.read_count, access.digest)
                    for access in block_accesses
                ],
            )

    def _start_deferred_accesses(self, now: int) -> None:
        if self._deferred_access_started_ns is None:
            self._deferred_access_started_ns = now

    def _deferred_accesses_should_flush(self, now: int) -> bool:
        if len(self._deferred_block_accesses) >= self._deferred_access_flush_blocks:
            return True
        if self._deferred_access_started_ns is None:
            return False
        return now - self._deferred_access_started_ns >= self._deferred_access_flush_ns
