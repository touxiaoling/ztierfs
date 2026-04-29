"""FUSE 文件句柄（fh）到 inode 的映射；支持 rename/unlink 后仍按打开时 inode 读。"""

import threading


class HandleTable:
    """进程内 fh 分配、inode 绑定与按 inode 的打开计数（用于延迟 unlink 清理）。"""

    def __init__(self, lock: threading.RLock):
        self._lock = lock
        self._next_fh = 0
        self._handles: dict[int, int] = {}
        self._lock_owners: dict[int, int] = {}
        self._open_counts: dict[int, int] = {}

    def new(self, file_id: int, lock_owner: int | None = None) -> int:
        with self._lock:
            self._next_fh += 1
            self._handles[self._next_fh] = file_id
            self._lock_owners[self._next_fh] = (
                lock_owner if lock_owner is not None else self._next_fh
            )
            self._open_counts[file_id] = self._open_counts.get(file_id, 0) + 1
            return self._next_fh

    def release(self, fh) -> tuple[int, int] | None:
        with self._lock:
            if isinstance(fh, int):
                file_id = self._handles.pop(fh, None)
                lock_owner = self._lock_owners.pop(fh, fh)
                if file_id is not None:
                    remaining = self._open_counts.get(file_id, 0) - 1
                    if remaining > 0:
                        self._open_counts[file_id] = remaining
                    else:
                        self._open_counts.pop(file_id, None)
                    return file_id, lock_owner
            return None

    def file_id(self, fh) -> int | None:
        with self._lock:
            if not isinstance(fh, int):
                return None
            return self._handles.get(fh)

    def lock_owner(self, fh) -> int | None:
        with self._lock:
            if not isinstance(fh, int):
                return None
            return self._lock_owners.get(fh)

    def has_open_file(self, file_id: int) -> bool:
        with self._lock:
            return self._open_counts.get(file_id, 0) > 0
