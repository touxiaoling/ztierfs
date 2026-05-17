"""各 FUSE mixin 共享的字段类型与抽象方法（单独继承不可用）。

权限检查、`LowLevelAttr` 映射等由 `AccessControlMixin`、`MetadataOpsMixin` 等实现；
读前瞻相关字段仅在单进程、单挂载实例内有效，不与其它进程协调。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .block_store import BlockStore
from .file_content import FileContentService
from .handles import HandleTable
from .locks import AdvisoryLockTable
from .metadata import MetadataStore


class FileSystemMixinBase:
    """组成 ZTierFS 的 mixin 所共享的字段与抽象方法约定（非独立基类）。"""

    metadata: MetadataStore
    handles: HandleTable
    locks: AdvisoryLockTable
    block_store: BlockStore
    file_content: FileContentService
    tier1: Path
    tier2: Path
    chunk_size: int
    _lock: threading.RLock
    _content_locks_guard: threading.Lock
    _content_locks: dict[int, threading.RLock]
    _caller_provider: Callable[[], tuple[int, int, int]]
    readahead_blocks: int
    readahead_workers: int
    _read_positions: dict[int, int]
    _readahead_inflight: set[tuple[int, int]]
    _readahead_executor: ThreadPoolExecutor | None
    _readahead_lock: threading.Lock

    def _require_open_access(self, node: Any, flags: int) -> None:
        """子类实现：按 ``open(2)`` 的 ``O_RDONLY`` / ``O_WRONLY`` / ``O_RDWR``（及 ``O_TRUNC``）
        映射为读/写权限掩码，再校验调用方对该 inode 是否具备相应 POSIX mode 权限；不满足时抛
        ``FuseOSError(EACCES)``。"""
        raise NotImplementedError

    def _require_access(self, node: Any, mask: int) -> None:
        """子类实现：在 FUSE 请求上下文中，按调用方有效 uid/gid 对照 inode 的 mode 做
        ``R_OK`` / ``W_OK`` / ``X_OK``（及 ``F_OK`` 存在性）检查；``node`` 缺失时抛 ``ENOENT``，
        权限不足时抛 ``EACCES``。"""
        raise NotImplementedError

    def _require_amode_access(self, node: Any, amode: int) -> None:
        """子类实现：将 ``access(2)`` 风格的 ``amode``（含 macOS 删除访问位等）归并为对 inode、
        必要时对父目录的 ``_require_access`` 调用链，供 ``_access`` 等路径复用。"""
        raise NotImplementedError

    def _require_owner(self, node: Any) -> None:
        """子类实现：要求调用方有效 uid 为该 inode 的拥有者（常见 ``root`` 豁免规则由实现约定）；
        否则抛 ``EPERM``。"""
        raise NotImplementedError

    def _caller_ids(self) -> tuple[int, int, int]:
        """子类实现：返回当前 FUSE 调用对应的 ``(uid, gid, pid)``；无请求上下文时回退为进程
        ``getuid`` / ``getgid`` / ``getpid``，供权限与锁主体等逻辑使用。"""
        raise NotImplementedError

    def _creation_owner(self) -> tuple[int, int]:
        """子类实现：返回新建 inode（文件、目录、符号链接等）时应写入元数据的 ``(uid, gid)``，
        通常取自 ``_caller_ids``。"""
        raise NotImplementedError

    def _lock_owner(self) -> int:
        """子类实现：返回本挂载进程内 POSIX 建议性文件锁所用的「锁主体」整型标识（用于区分用户
        与进程），与 ``AdvisoryLockTable``、句柄释放路径一致。"""
        raise NotImplementedError

    def _ensure_trash_directory_for_caller(self) -> None:
        """子类实现：按当前调用方 uid/gid 在元数据中确保回收站侧目录结构存在（可带 uid 级缓存），
        供 Finder 等通过 ``rename`` 丢入废纸篓前的路径依赖。"""
        raise NotImplementedError

    def _attrs_from_node(self, node: Any) -> Any:
        """子类实现：将 SQLite 中的 inode 行映射为 ``macfusepy.LowLevelAttr``（含目录 ``st_nlink``
        计数约定、``st_blocks`` 由实际占用换算等）。"""
        raise NotImplementedError

    def _schedule_readahead(
        self, plan: Any, offset: int, data_len: int, fh: Any
    ) -> None:
        """子类实现：在顺序读等条件下，为给定读计划异步提交后续若干文件 chunk 的预读任务（受
        ``readahead_blocks`` / ``readahead_workers`` 与去重集合约束），失败应静默忽略。"""
        raise NotImplementedError

    @contextmanager
    def _content_lock(self, inode_id: int) -> Iterator[None]:
        """子类实现：上下文管理器，在 ``with`` 体内持有该 inode 的**内容锁**（可重入），用于在单
        挂载进程内串行化同一普通文件上的读写、截断与块/chunk 更新路径。"""
        raise NotImplementedError
        yield

    def _remove_entry_node(self, node: Any) -> None:
        """子类实现：在元数据事务中删除一条目录项、更新 ``nlink``；若链接计数归零且无打开句柄，
        则删除 inode 及其文件块引用等 payload。"""
        raise NotImplementedError

    def _flush(self) -> None:
        """子类实现：将挂起的元数据写事务提交（例如 ``MetadataStore.commit``），使已完成的操作
        对连接可见。"""
        raise NotImplementedError

    def _clonefile(self, source: str, target: str) -> None:
        """子类实现：在 macOS ``clonefile`` 语义下复制 inode：共享 chunk 与 xattr、递增块引用计数，
        不重写 payload。"""
        raise NotImplementedError

    def _release(self, fh: Any) -> int:
        """子类实现：关闭 ``fh``：从句柄表移除、释放该 inode 上的建议锁主体；若 ``nlink==0`` 且无
        其它打开引用则清理孤儿 inode。"""
        raise NotImplementedError

    def _statfs(self) -> dict[str, int]:
        """子类实现：汇总热层、冷层挂载点的 ``statvfs`` 信息，返回 FUSE 期望的块数、可用块、
        ``f_bsize`` 等字段字典。"""
        raise NotImplementedError
