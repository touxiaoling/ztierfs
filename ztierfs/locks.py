"""进程内 POSIX advisory 文件锁（`fcntl`）的记录与冲突检测。

仅表示本挂载进程内的锁状态；多独立挂载或其它进程看不到此表，也不构成强制锁。
"""

import errno
import fcntl
import sys

from dataclasses import dataclass

from macfusepy import FuseOSError


@dataclass(slots=True)
class FileLock:
    """本进程锁表中的一条 POSIX 风格建议锁记录。

    对应 `struct flock` 的片段：`inode_id` 为内部文件节点 id；`owner` 为与
    打开句柄绑定的锁归属标识（同一 inode 上不同 `fh` 可区分）；`pid` 为登记
    时的进程号；`lock_type` 为 `F_RDLCK` / `F_WRLCK`；`start` 为区间起点字节
    偏移，`end` 为闭区间终点，若为 ``None`` 表示锁延伸至文件末尾（与
    ``l_len == 0`` 一致）。
    """
    inode_id: int
    owner: int
    pid: int
    lock_type: int
    start: int
    end: int | None


class AdvisoryLockTable:
    """单挂载进程内的 POSIX 建议锁（`fcntl`）状态表。

    仅跟踪本 `ZTierFS` 进程内通过 FUSE 登记的锁；不与其他挂载进程或内核
    强制锁协调，也不构成对其它进程的排他访问。
    """

    def __init__(self) -> None:
        """创建空的建议锁列表。"""
        self._locks: list[FileLock] = []

    def apply(
        self, inode_id: int, owner: int, pid: int, cmd: int, lock: dict[str, int]
    ) -> dict[str, int] | None:
        """按 `cmd` 执行与 `fcntl` 建议锁一致的逻辑（进程内、非强制）。

        - **F_GETLK**：只读探测。若存在与请求区间及锁类型不兼容的另一
          `owner` 的已登记锁，则在返回的 `lock` 副本中填入该冲突锁的
          `l_type`、`l_pid`、`l_start`、`l_len`；否则将 `l_type` 置为
          `F_UNLCK` 且 `l_pid` 为 0。不增删本表中的锁。
        - **F_SETLK** / **F_SETLKW**（本实现中二者均非阻塞）：`F_UNLCK` 时
          解除本 `owner` 上与区间重叠的锁；`F_RDLCK` / `F_WRLCK` 时若与
          其它 `owner` 冲突则抛出 ``EAGAIN``，否则先对该 `owner` 重叠区间
          解锁再追加新锁。成功加/解锁时返回 ``None``。
        """
        lock_type = int(lock.get("l_type", fcntl.F_UNLCK))
        start = int(lock.get("l_start", 0))
        length = int(lock.get("l_len", 0))
        end = None if length == 0 else start + length - 1

        # F_GETLK：只读查询与当前表冲突的持有者，不登记或释放锁。
        if cmd == fcntl.F_GETLK:
            conflict = self._conflict(inode_id, owner, lock_type, start, end)
            result = dict(lock)
            if conflict is None:
                result["l_type"] = fcntl.F_UNLCK
                result["l_pid"] = 0
            else:
                result["l_type"] = conflict.lock_type
                result["l_pid"] = conflict.pid
                result["l_start"] = conflict.start
                result["l_len"] = (
                    0 if conflict.end is None else conflict.end - conflict.start + 1
                )
            return result

        if cmd not in (fcntl.F_SETLK, getattr(fcntl, "F_SETLKW", fcntl.F_SETLK)):
            raise FuseOSError(errno.EINVAL)

        if lock_type == fcntl.F_UNLCK:
            self._unlock(inode_id, owner, start, end)
            return None

        if lock_type not in (fcntl.F_RDLCK, fcntl.F_WRLCK):
            raise FuseOSError(errno.EINVAL)
        if self._conflict(inode_id, owner, lock_type, start, end) is not None:
            raise FuseOSError(errno.EAGAIN)

        self._unlock(inode_id, owner, start, end)
        self._locks.append(FileLock(inode_id, owner, pid, lock_type, start, end))
        return None

    def release_owner(self, inode_id: int, owner: int) -> None:
        """在关闭文件句柄时调用：删除该 inode 上属于给定 `owner` 的全部锁。"""
        self._locks = [
            lock
            for lock in self._locks
            if not (lock.inode_id == inode_id and lock.owner == owner)
        ]

    def _conflict(
        self, inode_id: int, owner: int, lock_type: int, start: int, end: int | None
    ) -> FileLock | None:
        """返回与本 inode、字节区间重叠且来自其它 `owner` 的第一条互斥锁。

        读锁之间不互斥；写锁与读锁或写锁在重叠时互斥。
        """
        if lock_type == fcntl.F_UNLCK:
            return None
        for lock in self._locks:
            if lock.inode_id != inode_id or lock.owner == owner:
                continue
            if lock.lock_type == fcntl.F_RDLCK and lock_type == fcntl.F_RDLCK:
                continue
            if self._overlaps(lock.start, lock.end, start, end):
                return lock
        return None

    def _unlock(self, inode_id: int, owner: int, start: int, end: int | None) -> None:
        """从本 `owner` 在 inode 上的锁中抠除与 ``[start, end]`` 重叠的部分。

        重叠段删除；未重叠的前缀、后缀保留为至多两条连续区间记录。
        """
        next_locks: list[FileLock] = []
        for lock in self._locks:
            if (
                lock.inode_id != inode_id
                or lock.owner != owner
                or not self._overlaps(lock.start, lock.end, start, end)
            ):
                next_locks.append(lock)
                continue
            if lock.start < start:
                next_locks.append(
                    FileLock(
                        lock.inode_id,
                        lock.owner,
                        lock.pid,
                        lock.lock_type,
                        lock.start,
                        start - 1,
                    )
                )
            if end is not None and (lock.end is None or lock.end > end):
                next_locks.append(
                    FileLock(
                        lock.inode_id,
                        lock.owner,
                        lock.pid,
                        lock.lock_type,
                        end + 1,
                        lock.end,
                    )
                )
        self._locks = next_locks

    def _overlaps(
        self,
        left_start: int,
        left_end: int | None,
        right_start: int,
        right_end: int | None,
    ) -> bool:
        """判断两段字节区间是否相交（``end is None`` 视为延伸到文件尾）。"""
        left_last = left_end if left_end is not None else sys.maxsize
        right_last = right_end if right_end is not None else sys.maxsize
        return left_start <= right_last and right_start <= left_last
