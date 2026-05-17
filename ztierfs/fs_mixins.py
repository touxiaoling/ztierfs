"""各 FUSE mixin 共享的字段类型（单独继承不可用）。

权限检查、`LowLevelAttr` 映射等由 `AccessControlMixin`、`MetadataOpsMixin` 等实现；
读前瞻相关字段仅在单进程、单挂载实例内有效，不与其它进程协调。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from collections.abc import Callable
from pathlib import Path

from .block_store import BlockStore
from .file_content import FileContentService
from .handles import HandleTable
from .locks import AdvisoryLockTable
from .metadata import MetadataStore


class FileSystemMixinBase:
    """组成 ZTierFS 的 mixin 所共享的字段约定（非独立基类）。"""

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
