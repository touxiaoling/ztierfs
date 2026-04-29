import threading

from contextlib import contextmanager

from loguru import logger

from .fs_mixins import FileSystemMixinBase


class InodeLocksMixin(FileSystemMixinBase):
    @contextmanager
    def _content_lock(self, inode_id: int):
        with self._content_locks_guard:
            lock = self._content_locks.get(inode_id)
            if lock is None:
                lock = threading.RLock()
                self._content_locks[inode_id] = lock
                logger.debug("创建 inode 内容锁：inode={}", inode_id)
        with lock:
            yield
