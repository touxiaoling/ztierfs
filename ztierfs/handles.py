"""FUSE 文件句柄（fh）与 inode 的映射表；rename/unlink 后仍按打开时绑定的 inode 读写。"""

import threading


class HandleTable:
    """进程内 FUSE 文件句柄（fh）的分配及其与 inode（file_id）的绑定。

    每次 ``new`` 分配单调递增的 fh，并把它映射到当时的 inode；``release`` 解除映射。
    按 inode 维护打开引用计数：同一 inode 可被多个 fh 同时打开，计数用于判断
    是否仍有未关闭句柄。已 unlink 的 inode 可延后到最后一次 ``release`` 把计数
    归零后再做数据清理，从而符合「打开文件在关闭前仍可访问原内容」的语义。
    """

    def __init__(self, lock: threading.RLock):
        """使用调用方提供的可重入锁保护内部表；初始化 fh 游标与三张映射/计数表。"""
        self._lock = lock
        self._next_fh = 0
        self._handles: dict[int, int] = {}
        self._lock_owners: dict[int, int] = {}
        self._open_counts: dict[int, int] = {}

    def new(self, file_id: int, lock_owner: int | None = None) -> int:
        """分配新的 fh，绑定到 ``file_id``（inode），并将该 inode 的打开计数加一。

        ``lock_owner`` 为进程内建议锁的分桶键；若为 ``None`` 则默认使用本 fh，
        使每个打开描述在锁表上独立。
        """
        with self._lock:
            self._next_fh += 1
            self._handles[self._next_fh] = file_id
            self._lock_owners[self._next_fh] = (
                lock_owner if lock_owner is not None else self._next_fh
            )
            self._open_counts[file_id] = self._open_counts.get(file_id, 0) + 1
            return self._next_fh

    def release(self, fh) -> tuple[int, int] | None:
        """关闭句柄：移除 fh→inode 与 fh→lock_owner，并将对应 inode 的打开计数减一。

        若 ``fh`` 为整数且曾由 ``new`` 登记，返回 ``(file_id, lock_owner)`` 供上层释放建议锁等；
        计数在本表内已更新，归零后 ``has_open_file(file_id)`` 为假，可与 inode 是否已 unlink
        结合安排延迟清理。未知或非法 ``fh`` 返回 ``None``。
        """
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
        """由 fh 查询当前绑定的 inode（``file_id``）；非整数或未登记则 ``None``。"""
        with self._lock:
            if not isinstance(fh, int):
                return None
            return self._handles.get(fh)

    def lock_owner(self, fh) -> int | None:
        """返回该 fh 在建议锁实现中使用的锁主体标识；非整数或未登记则 ``None``。"""
        with self._lock:
            if not isinstance(fh, int):
                return None
            return self._lock_owners.get(fh)

    def has_open_file(self, file_id: int) -> bool:
        """若该 inode 的打开计数大于零则真，用于判断能否安全做 unlink 后的延迟清理。"""
        with self._lock:
            return self._open_counts.get(file_id, 0) > 0
