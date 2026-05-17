"""各 FUSE mixin 共享的字段类型（单独继承不可用）。

权限检查、`LowLevelAttr` 映射等由 `AccessControlMixin`、`MetadataOpsMixin` 等实现；
读前瞻相关字段仅在单进程、单挂载实例内有效，不与其它进程协调。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .block_store import BlockStore
from .file_content import FileContentService
from .handles import HandleTable
from .locks import AdvisoryLockTable
from .metadata import MetadataStore

if TYPE_CHECKING:
    from macfusepy import LowLevelAttr

    from .file_content import FileReadPlan


class _ContextManager(Protocol):
    def __enter__(self) -> None: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None: ...


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

    if TYPE_CHECKING:

        def _attrs_from_node(self, node: Any) -> LowLevelAttr: ...

        def _ensure_trash_directory_for_caller(self) -> None: ...

        def _content_lock(self, inode_id: int) -> _ContextManager: ...

        def _require_owner(self, node: Any) -> None: ...

        def _require_access(self, node: Any, mask: int) -> None: ...

        def _require_open_access(self, node: Any, flags: int) -> None: ...

        def _require_amode_access(self, node: Any, amode: int) -> None: ...

        def _caller_ids(self) -> tuple[int, int, int]: ...

        def _creation_owner(self) -> tuple[int, int]: ...

        def _lock_owner(self) -> int: ...

        def _schedule_readahead(
            self, plan: FileReadPlan, offset: int, data_len: int, fh: Any
        ) -> None: ...

        def _flush(self) -> None: ...

        def _release(self, fh: Any) -> int: ...

        def _remove_entry_node(self, node: Any) -> None: ...

        def _statfs(self) -> dict[str, int]: ...
