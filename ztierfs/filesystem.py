"""组装 ZTierFS：组合 FUSE 与各领域 mixin，并完成元数据、块层、句柄等初始化。"""

import os
import threading

from collections.abc import Iterable
from pathlib import Path
from time import time_ns
from typing import Callable

from loguru import logger
from macfusepy import LoggingMixIn, fuse_get_context

from .access_control import AccessControlMixin
from .block_store import BlockStore, TieringPolicy
from .constants import (
    CHUNK_SIZE,
    COMPRESSED_SUFFIXES,
    DEFAULT_COMPRESSION_MIN_BYTES,
    DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS,
    DEFAULT_HOT_CACHE_MAX_BYTES,
    DEFAULT_HOT_CACHE_MIN_BYTES,
    DEFAULT_INLINE_MAX_BYTES,
    DEFAULT_MIN_HOT_AGE_SECONDS,
    DEFAULT_PROTECTED_PREFIX_CHUNKS,
    DEFAULT_READ_CACHE_BYTES,
    DEFAULT_READAHEAD_BLOCKS,
    DEFAULT_READAHEAD_WORKERS,
)
from .file_content import FileContentService
from .file_ops import FileOpsMixin
from .handles import HandleTable
from .inode_locks import InodeLocksMixin
from .inode_fuse import InodeFuseMixin
from .locks import AdvisoryLockTable
from .metadata import MetadataStore
from .metadata.connection import SQLitePragmas
from .metadata_ops import MetadataOpsMixin
from .namespace_ops import NamespaceOpsMixin
from .pathing import compression_allowed
from .payload_store import FileKVPayloadStore, NullPayloadStore
from .perf import OperationProfiler


class ZTierFS(
    InodeFuseMixin,
    FileOpsMixin,
    NamespaceOpsMixin,
    MetadataOpsMixin,
    AccessControlMixin,
    InodeLocksMixin,
    LoggingMixIn,
):
    """基于 SQLite、内容寻址块和 POSIX 元数据语义的可写 FUSE 文件系统。"""

    def __init__(
        self,
        tier1: str | os.PathLike[str],
        tier2: str | os.PathLike[str],
        database: str | os.PathLike[str] | None = None,
        *,
        chunk_size: int = CHUNK_SIZE,
        hot_cache_max_bytes: int = DEFAULT_HOT_CACHE_MAX_BYTES,
        hot_cache_min_bytes: int | None = None,
        protected_prefix_chunks: int = DEFAULT_PROTECTED_PREFIX_CHUNKS,
        min_hot_age_seconds: int = DEFAULT_MIN_HOT_AGE_SECONDS,
        cold_copy_cleanup_age_seconds: int = DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS,
        compressed_suffixes: Iterable[str] = COMPRESSED_SUFFIXES,
        compression_level: int | None = None,
        compression_min_bytes: int = DEFAULT_COMPRESSION_MIN_BYTES,
        inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES,
        read_cache_bytes: int = DEFAULT_READ_CACHE_BYTES,
        readahead_blocks: int = DEFAULT_READAHEAD_BLOCKS,
        readahead_workers: int = DEFAULT_READAHEAD_WORKERS,
        sqlite_synchronous: str = "NORMAL",
        payload_store: str = "sqlite",
        payload_store_path: str | os.PathLike[str] | None = None,
        update_config: bool = False,
        profile_interval_seconds: float = 0,
        caller_provider: Callable[[], tuple[int, int, int]] | None = None,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if inline_max_bytes < 0:
            raise ValueError("inline_max_bytes must not be negative")
        if compression_min_bytes < 0:
            raise ValueError("compression_min_bytes must not be negative")
        if read_cache_bytes < 0:
            raise ValueError("read_cache_bytes must not be negative")
        if readahead_blocks < 0:
            raise ValueError("readahead_blocks must not be negative")
        if readahead_workers < 0:
            raise ValueError("readahead_workers must not be negative")
        if sqlite_synchronous.upper() not in {"FULL", "NORMAL", "OFF"}:
            raise ValueError("sqlite_synchronous must be FULL, NORMAL, or OFF")
        if payload_store not in {"sqlite", "filekv"}:
            raise ValueError("payload_store must be sqlite or filekv")
        if profile_interval_seconds < 0:
            raise ValueError("profile_interval_seconds must not be negative")
        hot_max = hot_cache_max_bytes
        hot_min = hot_cache_min_bytes
        if hot_min is None:
            hot_min = min(DEFAULT_HOT_CACHE_MIN_BYTES, hot_max)

        self.tier1 = Path(tier1).resolve()
        self.tier2 = Path(tier2).resolve()
        self.database = (
            Path(database).resolve() if database else self.tier1 / "ztierfs.sqlite3"
        )
        self.chunk_size = chunk_size
        self.hot_cache_max_bytes = hot_max
        self.hot_cache_min_bytes = hot_min
        self.tiering_policy = TieringPolicy(
            hot_max_bytes=hot_max,
            hot_min_bytes=hot_min,
            protected_prefix_chunks=protected_prefix_chunks,
            min_hot_age_ns=min_hot_age_seconds * 1_000_000_000,
            cold_copy_cleanup_age_ns=cold_copy_cleanup_age_seconds * 1_000_000_000,
        )
        self.compressed_suffixes = frozenset(
            suffix.lower() for suffix in compressed_suffixes
        )
        self.compression_level = compression_level
        self.compression_min_bytes = compression_min_bytes
        self.inline_max_bytes = inline_max_bytes
        self.read_cache_bytes = read_cache_bytes
        self.readahead_blocks = readahead_blocks
        self.readahead_workers = readahead_workers
        self.sqlite_synchronous = sqlite_synchronous.upper()
        self.payload_store_name = payload_store
        self.payload_store_path = (
            Path(payload_store_path).resolve()
            if payload_store_path is not None
            else self.tier1 / "payload-kv"
            if payload_store == "filekv"
            else None
        )
        self.profile_interval_seconds = profile_interval_seconds
        self._caller_provider = caller_provider or fuse_get_context

        self.tier1_blocks = self.tier1 / "blocks"
        self.tier2_blocks = self.tier2 / "blocks"
        self._lock = threading.RLock()
        self._content_locks_guard = threading.Lock()
        self._content_locks: dict[int, threading.RLock] = {}
        self._read_positions: dict[int, int] = {}
        self._readahead_inflight: set[tuple[int, int]] = set()
        self._readahead_executor = None
        self._readahead_lock = threading.Lock()
        self._ensured_trash_uids: set[int] = set()
        self._operation_profiler = (
            OperationProfiler(interval_seconds=profile_interval_seconds)
            if profile_interval_seconds
            else None
        )

        self.tier1.mkdir(parents=True, exist_ok=True)
        self.tier2.mkdir(parents=True, exist_ok=True)
        self.tier1_blocks.mkdir(parents=True, exist_ok=True)
        self.tier2_blocks.mkdir(parents=True, exist_ok=True)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "初始化文件系统：tier1={}，tier2={}，database={}，chunk_size={}，hot_cache={}..{}，inline_max={}，zstd_level={}",
            self.tier1,
            self.tier2,
            self.database,
            self.chunk_size,
            self.hot_cache_min_bytes,
            self.hot_cache_max_bytes,
            self.inline_max_bytes,
            self.compression_level,
        )
        if self._operation_profiler is not None:
            logger.info("启用 FUSE 性能统计：interval={}s", profile_interval_seconds)

        if payload_store == "filekv":
            payload_store_path = self.payload_store_path
            assert payload_store_path is not None
            inline_payload_store = FileKVPayloadStore(payload_store_path)
        else:
            inline_payload_store = NullPayloadStore()
        metadata: MetadataStore | None = None
        try:
            metadata = MetadataStore(
                self.database,
                self._lock,
                pragmas=SQLitePragmas(synchronous=self.sqlite_synchronous),
                payload_store=inline_payload_store,
            )
            self.metadata = metadata
            self._ensure_filesystem_config(update_config=update_config)
        except Exception:
            if metadata is not None:
                metadata.close()
            raise
        self.handles = HandleTable(self._lock)
        self.locks = AdvisoryLockTable()
        self.block_store = BlockStore(
            self.metadata,
            self.tier1_blocks,
            self.tier2_blocks,
            policy=self.tiering_policy,
            compression_level=compression_level,
            compression_min_bytes=compression_min_bytes,
            inline_max_bytes=inline_max_bytes,
            read_cache_bytes=read_cache_bytes,
        )
        self.file_content = FileContentService(
            self.metadata,
            self.block_store,
            chunk_size=chunk_size,
            small_file_inline_max=inline_max_bytes,
            compression_allowed=self._compression_allowed,
        )

    def _compression_allowed(self, path: str) -> bool:
        return compression_allowed(path, self.compressed_suffixes)

    def _ensure_filesystem_config(self, *, update_config: bool) -> None:
        payload_store_path = (
            str(self.payload_store_path)
            if self.payload_store_path is not None
            else None
        )
        desired = {
            "hot_tier_path": str(self.tier1),
            "cold_tier_path": str(self.tier2),
            "payload_store": self.payload_store_name,
            "payload_store_path": payload_store_path,
        }
        with self.metadata.transaction():
            current = self.metadata.filesystem_config()
            if current is None or update_config:
                self.metadata.set_filesystem_config(**desired)
                return
            mismatches = [
                key for key, value in desired.items() if current[key] != value
            ]
            if mismatches:
                details = ", ".join(
                    f"{key}: stored={current[key]!r}, requested={desired[key]!r}"
                    for key in mismatches
                )
                raise RuntimeError(
                    "metadata database belongs to different storage paths; "
                    f"use --update-config to rewrite local config ({details})"
                )

    def _ensure_trash_directory_for_caller(self) -> None:
        uid, gid, _pid = self._caller_ids()
        if uid in self._ensured_trash_uids:
            return
        with self.metadata.transaction():
            self.metadata.ensure_trash_directories(uid, gid, time_ns())
        self._ensured_trash_uids.add(uid)
