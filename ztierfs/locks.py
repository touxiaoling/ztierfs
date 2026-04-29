import errno
import fcntl
import sys

from dataclasses import dataclass

from macfusepy import FuseOSError


@dataclass(slots=True)
class FileLock:
    inode_id: int
    owner: int
    pid: int
    lock_type: int
    start: int
    end: int | None


class AdvisoryLockTable:
    def __init__(self) -> None:
        self._locks: list[FileLock] = []

    def apply(
        self, inode_id: int, owner: int, pid: int, cmd: int, lock: dict[str, int]
    ) -> dict[str, int] | None:
        lock_type = int(lock.get("l_type", fcntl.F_UNLCK))
        start = int(lock.get("l_start", 0))
        length = int(lock.get("l_len", 0))
        end = None if length == 0 else start + length - 1

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
        self._locks = [
            lock
            for lock in self._locks
            if not (lock.inode_id == inode_id and lock.owner == owner)
        ]

    def _conflict(
        self, inode_id: int, owner: int, lock_type: int, start: int, end: int | None
    ) -> FileLock | None:
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
        left_last = left_end if left_end is not None else sys.maxsize
        right_last = right_end if right_end is not None else sys.maxsize
        return left_start <= right_last and right_start <= left_last
