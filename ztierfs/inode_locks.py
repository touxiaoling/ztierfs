"""按 inode 划分的可重入内容锁：在**单挂载进程**内串行化同一普通文件上的数据路径。

与 `MetadataStore` 内部的连接/读写锁、写事务等「元数据锁」不同：后者保护 SQLite
中 inodes、目录项、块表、chunk 映射等**库级**一致性与跨 inode 操作；本模块只提供
`threading.RLock`，供 `file_content` 等与**单文件内容**相关的逻辑与元数据写事务
组合使用（通常先 `with _content_lock(inode):` 再进入 `metadata.transaction()`）。"""

import threading

from contextlib import contextmanager

from loguru import logger

from .fs_mixins import FileSystemMixinBase


class InodeLocksMixin(FileSystemMixinBase):
    """实现 `FileSystemMixinBase._content_lock`：为每个 inode 懒创建并缓存 `RLock`。

    **内容锁**（本 mixin 的 `_content_lock`）按 inode 将读写字节、截断、以及依赖
    `file_content` 的块/chunk 更新等操作在进程内**串行化**，避免同一 inode 上并发
    交错导致块引用、chunk 行与落库状态不一致。锁为可重入，同一线程可嵌套进入。

    **元数据锁**在 `MetadataStore` / 连接层实现，不在本文件：用于保护连接池、
    只读连接与写事务、以及延迟访问等，保证 SQLite 元数据完整；与按 inode 的内容
    锁**正交**，调用方常将二者嵌套使用。POSIX 建议性文件锁由 `AdvisoryLockTable`
    记录，亦不同于上述两种锁。"""

    @contextmanager
    def _content_lock(self, inode_id: int):
        """在上下文中持有所给 `inode_id` 对应的内容锁（可重入）。

        与元数据层锁区分：本锁只保证**该 inode** 上文件数据相关代码路径的互斥；
        写库仍须通过 `MetadataStore` 的写事务及其内部同步机制。未命中缓存时会
        在 `_content_locks_guard` 下创建 `RLock` 并记入 `_content_locks`。"""
        with self._content_locks_guard:
            lock = self._content_locks.get(inode_id)
            if lock is None:
                lock = threading.RLock()
                self._content_locks[inode_id] = lock
                logger.debug("创建 inode 内容锁：inode={}", inode_id)
        with lock:
            yield
