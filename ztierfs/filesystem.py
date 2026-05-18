"""组装可挂载的 `ZTierFS`：按 MRO 将 inode 级 FUSE、文件内容、命名空间、元数据与权限等 mixin 组合在一起。

本模块负责构造期的组件接线与共享状态字段：解析冷热层路径、打开 SQLite `MetadataStore`、
校验或写入 `filesystem_config`、再挂接 `HandleTable`、`AdvisoryLockTable`、`BlockStore` 与
`FileContentService`。读前瞻与 `OperationProfiler` 的参数字段在此落盘，实际调度与日志输出
在其它 mixin（如读路径上的预取、FUSE `init`/`destroy`）中完成。根下 macOS 回收站目录
（`.Trashes` / `.Trashes/<uid>/`）按调用方 UID 惰性确保，见 `_ensure_trash_directory_for_caller`。
"""

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
    """可挂载的 FUSE 实例骨架：具体回调与语义由各 mixin 实现，`cli` 解析参数后构造本类并交给 macfusepy。"""

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
        update_config: bool = False,
        profile_interval_seconds: float = 0,
        caller_provider: Callable[[], tuple[int, int, int]] | None = None,
    ):
        """构造文件系统内核对象并完成组件接线。

        路径与策略：将 `tier1`/`tier2` 解析为热层与冷层根目录，默认在热层放置 SQLite 库
        （可用 `database` 覆盖）；创建 `blocks` 子目录与库父目录。分块大小、热层水位、受保护
        前缀块数、冷层副本清理年龄等写入 `TieringPolicy` 并交给 `BlockStore`；压缩级别、最小
        压缩长度、跳过后缀集合与内联阈值同时约束块层与 `FileContentService`。

        元数据与内联 payload：小文件内联 payload 与内联块 payload 始终存入 SQLite；
        `sqlite_synchronous` 控制 SQLite 同步模式。`update_config` 与已有
        `filesystem_config` 不一致时的行为见 `_ensure_filesystem_config`。

        组件顺序：在持锁的 `MetadataStore` 就绪并写入配置后，依次构造进程内 `HandleTable`、
        POSIX 建议锁表 `AdvisoryLockTable`、`BlockStore`（内容寻址块与冷热迁移）以及
        `FileContentService`（分块读写、稀疏与截断等）。全局 `threading.RLock` 供元数据与
        句柄表共用；按 inode 的 `_content_locks` 与按文件句柄的 `_read_positions` 在此初始化，
        供 mixin 在读写与顺序读检测中使用。

        读前瞻：`readahead_blocks` / `readahead_workers` 保存为实例属性；`_readahead_executor`
        初始为 ``None``，在首次需要预取时由读路径惰性创建线程池（名称前缀 ``ztierfs-readahead``），
        `_readahead_inflight` 与 `_readahead_lock` 用于去重并发预取任务。任一为 0 时关闭预读。

        性能采样：`profile_interval_seconds > 0` 时创建 `OperationProfiler`，按间隔汇总并
        记录热点操作；为 0 则不分配采样器。`caller_provider` 默认绑定 `fuse_get_context` 以取得
        ``(uid, gid, pid)``；测试或非标准上下文中可注入替代实现。

        回收站：`_ensured_trash_uids` 记录已为哪些 UID 在元数据中确保过 `.Trashes` 布局；
        实际创建在 `_ensure_trash_directory_for_caller` 中按请求上下文惰性执行。
        """
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

        metadata: MetadataStore | None = None
        try:
            metadata = MetadataStore(
                self.database,
                self._lock,
                pragmas=SQLitePragmas(synchronous=self.sqlite_synchronous),
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

        def drain_pending_deletions() -> None:
            self.block_store.drain_pending_deletions(max_deletions=64, tier=1)

        def drain_requested_demotions() -> None:
            self.block_store.drain_requested_demotions(max_blocks=1)

        self.metadata.add_after_commit_hook(drain_pending_deletions)
        self.metadata.add_after_commit_hook(drain_requested_demotions)
        self.file_content = FileContentService(
            self.metadata,
            self.block_store,
            chunk_size=chunk_size,
            compression_allowed=self._compression_allowed,
        )

    def _compression_allowed(self, path: str) -> bool:
        """根据构造期配置的已知已压缩后缀集合，判断给定 POSIX 路径是否允许再走 zstd。"""
        return compression_allowed(path, self.compressed_suffixes)

    def _ensure_filesystem_config(self, *, update_config: bool) -> None:
        """将当前冷热层路径持久化到 `filesystem_config`，并与已有记录对齐。

        库中尚无配置或 `update_config` 为真时，直接写入期望字段。否则逐项比对：若与当前
        进程传入的路径不一致，抛出 `RuntimeError`，提示应使用
        CLI 的 ``--update-config`` 显式改写本地配置，避免误用属于其它挂载点的数据库文件。
        """
        desired = {
            "hot_tier_path": str(self.tier1),
            "cold_tier_path": str(self.tier2),
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
        """按 FUSE 调用方 UID/GID 在根 inode 下确保 macOS 回收站目录树存在（幂等）。

        在元数据事务中调用 `ensure_trash_directories`：根级 ``.Trashes``（粘滞位、属主 root）
        及 ``.Trashes/<uid>/``（属主为当前用户）。同一 UID 在进程内只执行一次元数据写入，
        后续依赖 `_ensured_trash_uids` 短路；Finder 等将文件移入废纸篓前需此布局。
        """
        uid, gid, _pid = self._caller_ids()
        if uid in self._ensured_trash_uids:
            return
        with self.metadata.transaction():
            self.metadata.ensure_trash_directories(uid, gid, time_ns())
        self._ensured_trash_uids.add(uid)
