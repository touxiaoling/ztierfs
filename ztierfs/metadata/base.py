from __future__ import annotations

import sqlite3
import threading

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .access_stats import BlockAccessStats
    from ztierfs.payload_store import PayloadStore


class MetadataMixinBase:
    """Shared type contract for metadata mixins composed into MetadataStore."""

    _deferred_access_lock: threading.Lock
    _deferred_node_atimes: dict[int, int]
    _deferred_block_accesses: dict[str, BlockAccessStats]
    _deferred_access_started_ns: int | None
    _deferred_access_flush_blocks: int
    _deferred_access_flush_ns: int
    payload_store: "PayloadStore"

    @property
    def _db(self) -> sqlite3.Connection:
        raise NotImplementedError

    def increment_block_refcount(self, digest: str) -> None:
        raise NotImplementedError

    def block_exists(self, digest: str) -> bool:
        raise NotImplementedError

    def upsert_file_chunk(
        self, file_id: int, chunk_index: int, digest: str, size: int
    ) -> None:
        raise NotImplementedError

    def clone_file_node(
        self,
        source_id: int,
        parent_id: int,
        name: str,
        *,
        mode: int,
        uid: int,
        gid: int,
        size: int,
        now: int,
    ) -> int:
        raise NotImplementedError

    def set_inline_file(
        self,
        node_id: int,
        data: bytes,
        *,
        compressed: bool,
        raw_size: int,
        now: int,
    ) -> None:
        raise NotImplementedError

    def clear_inline_file(self, node_id: int) -> None:
        raise NotImplementedError
