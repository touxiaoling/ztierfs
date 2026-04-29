"""按 inode 细分的可重入锁，序列化同一文件内容的并发读写/截断。"""

import threading

from contextlib import contextmanager

from loguru import logger

from .fs_mixins import FileSystemMixinBase


class InodeLocksMixin(FileSystemMixinBase):
    """在 ZTierFS 内为每个 inode 懒创建 RLock，供 file_content 等路径使用。"""

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
