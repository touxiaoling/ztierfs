from __future__ import annotations

import threading

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
    """Shared type contract for mixins composed into ZTierFS."""

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

    def _require_open_access(self, node: Any, flags: int) -> None:
        raise NotImplementedError

    def _require_access(self, node: Any, mask: int) -> None:
        raise NotImplementedError

    def _require_amode_access(self, node: Any, amode: int) -> None:
        raise NotImplementedError

    def _require_owner(self, node: Any) -> None:
        raise NotImplementedError

    def _caller_ids(self) -> tuple[int, int, int]:
        raise NotImplementedError

    def _creation_owner(self) -> tuple[int, int]:
        raise NotImplementedError

    def _lock_owner(self) -> int:
        raise NotImplementedError

    def _ensure_trash_directory_for_caller(self) -> None:
        raise NotImplementedError

    def _attrs_from_node(self, node: Any) -> Any:
        raise NotImplementedError

    def _schedule_readahead(
        self, plan: Any, offset: int, data_len: int, fh: Any
    ) -> None:
        raise NotImplementedError

    @contextmanager
    def _content_lock(self, inode_id: int) -> Iterator[None]:
        raise NotImplementedError
        yield

    def _node_from_handle_or_path(self, path: str, fh: Any) -> Any:
        raise NotImplementedError

    def _remove_entry_node(self, node: Any) -> None:
        raise NotImplementedError

    def _access(self, path: str, amode: int) -> int:
        raise NotImplementedError

    def _chmod(self, path: str, mode: int, fh: Any = None) -> None:
        raise NotImplementedError

    def _chown(self, path: str, uid: int, gid: int, fh: Any = None) -> None:
        raise NotImplementedError

    def _create(self, path: str, mode: int, flags: int = 0) -> Any:
        raise NotImplementedError

    def _flush(self) -> None:
        raise NotImplementedError

    def _getattr(self, path: str, fh: Any = None) -> dict[str, int]:
        raise NotImplementedError

    def _getxattr(self, path: str, name: str) -> bytes:
        raise NotImplementedError

    def _link(self, target: str, source: str) -> None:
        raise NotImplementedError

    def _clonefile(self, source: str, target: str) -> None:
        raise NotImplementedError

    def _listxattr(self, path: str) -> list[str]:
        raise NotImplementedError

    def _lock_file(
        self, path: str, fh: Any, cmd: int, lock: dict[str, int]
    ) -> dict[str, int] | None:
        raise NotImplementedError

    def _mkdir(self, path: str, mode: int) -> Any:
        raise NotImplementedError

    def _mknod(self, path: str, mode: int, dev: int) -> Any:
        raise NotImplementedError

    def _open(self, path: str, flags: int) -> int:
        raise NotImplementedError

    def _read(self, path: str, size: int, offset: int, fh: Any) -> bytes:
        raise NotImplementedError

    def _readdir(self, path: str) -> list[Any]:
        raise NotImplementedError

    def _readlink(self, path: str) -> str:
        raise NotImplementedError

    def _release(self, fh: Any) -> int:
        raise NotImplementedError

    def _removexattr(self, path: str, name: str) -> None:
        raise NotImplementedError

    def _rename(self, old: str, new: str, flags: int) -> None:
        raise NotImplementedError

    def _rmdir(self, path: str) -> None:
        raise NotImplementedError

    def _setxattr(self, path: str, name: str, value: bytes, options: int) -> None:
        raise NotImplementedError

    def _statfs(self) -> dict[str, int]:
        raise NotImplementedError

    def _symlink(self, target: str, source: str) -> Any:
        raise NotImplementedError

    def _truncate(self, path: str, length: int, fh: Any = None) -> None:
        raise NotImplementedError

    def _unlink(self, path: str) -> None:
        raise NotImplementedError

    def _utimens(self, path: str, times: Any, fh: Any = None) -> None:
        raise NotImplementedError

    def _write(self, path: str, data: bytes, offset: int, fh: Any) -> int:
        raise NotImplementedError
