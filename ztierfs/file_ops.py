"""普通文件的创建、打开、读写与截断等 FUSE 路径/句柄操作。"""

import errno
import os

from concurrent.futures import ThreadPoolExecutor
from stat import S_IFREG
from time import time_ns
from typing import Any

from loguru import logger
from macfusepy import FuseOSError

from .fs_mixins import FileSystemMixinBase


class FileOpsMixin(FileSystemMixinBase):
    """文件相关 FUSE 回调的高层实现（依赖元数据事务与 FileContentService）。"""

    def _create(self, path: str, mode: int, flags: int = os.O_RDWR) -> int:
        """内部：处理 create。"""
        logger.debug(
            "创建或截断文件：path={}，mode={:o}，flags={:#x}", path, mode, flags
        )
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            parent, name = self.metadata.parent_and_name(path)
            self._require_access(parent, os.W_OK | os.X_OK)
            existing = self.metadata.child(parent["id"], name)
            now = time_ns()
            if existing is not None:
                if existing["kind"] != "file":
                    raise FuseOSError(errno.EISDIR)
                self._require_open_access(existing, flags | os.O_TRUNC)
                self.file_content.remove_file_chunks(existing["id"])
                self.metadata.reset_file_node(
                    existing["id"], S_IFREG | (mode & 0o7777), now
                )
                file_id = existing["id"]
                logger.debug("复用已有文件并清空内容：path={}，inode={}", path, file_id)
            else:
                file_id = self.metadata.insert_node(
                    parent["id"],
                    name,
                    "file",
                    S_IFREG | (mode & 0o7777),
                    *self._creation_owner(),
                    now,
                )
                if file_id is None:
                    logger.error("创建文件失败：元数据未返回 inode，path={}", path)
                    raise FuseOSError(errno.EIO)
                logger.debug("创建新文件 inode：path={}，inode={}", path, file_id)
            return self.handles.new(file_id, self._lock_owner())

    def _create_with_attrs(self, path: str, mode: int, flags: int = os.O_RDWR):
        """内部：处理 create with attrs。"""
        fh = self._create(path, mode, flags)
        with self.metadata.read_transaction():
            node = self._node_from_handle_or_path(path, fh)
            return fh, self._attrs_from_node(node)

    def _open(self, path: str, flags: int) -> int:
        """内部：打开已存在的普通文件（非 create）。

        返回的整数 ``fh`` 是进程内文件句柄，由句柄表分配并与该文件的
        ``inode``（元数据中的节点 id）绑定；后续读写以 ``fh`` 为准即可
        定位到同一 inode，即使路径上的目录项被 rename/unlink，只要句柄
        未 release，仍操作原 inode。``path`` 仅在本次调用时用于解析节点
        与权限检查。若 ``flags`` 含 ``O_TRUNC``，在内容锁保护下将文件
        截断为 0 字节后再返回句柄。
        """
        logger.debug("打开文件：path={}，flags={:#x}", path, flags)
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            node = self.metadata.get_node(path)
            if node["kind"] != "file":
                raise FuseOSError(errno.EISDIR)
            self._require_open_access(node, flags)
            inode_id = node["id"]
        if flags & os.O_TRUNC:
            with self._content_lock(inode_id), self.metadata.transaction():
                node = self.metadata.node_by_id(inode_id)
                if node is None:
                    raise FuseOSError(errno.ENOENT)
                self.file_content.truncate_file(inode_id, self._name_for_inode(node), 0)
                logger.debug("打开时截断文件：path={}，inode={}", path, inode_id)
        return self.handles.new(inode_id, self._lock_owner())

    def _node_from_handle_or_path(self, path: str, fh) -> Any:
        """内部：解析本次 I/O 应对应的元数据节点行。

        若 ``fh`` 为有效句柄，优先用其绑定的 ``inode`` 查节点（与路径
        解耦，适用于已打开文件被改名或删除目录项后仍继续读写的语义）；
        否则回退为按 ``path`` 查找。二者均失败时抛出 ``ENOENT``。
        """
        file_id = self.handles.file_id(fh)
        if file_id is not None:
            row = self.metadata.node_by_id(file_id)
            if row is not None:
                return row
        row = self.metadata.lookup_node(path)
        if row is None:
            raise FuseOSError(errno.ENOENT)
        return row

    def _inode_id_from_handle_or_path(self, path: str, fh) -> int:
        """内部：解析本次 I/O 的 inode id（与 `_node_from_handle_or_path` 同规则）。

        有有效 ``fh`` 时直接取句柄上的 inode；否则在只读事务内按 ``path``
        解析。用于在加内容锁前确定锁定的 inode，避免与路径竞态。
        """
        file_id = self.handles.file_id(fh)
        if file_id is not None:
            return file_id
        with self.metadata.read_transaction():
            return self.metadata.get_node(path)["id"]

    def _read(self, path: str, size: int, offset: int, fh) -> bytes:
        """内部：从普通文件按 ``offset`` 起读取最多 ``size`` 字节。

        节点解析遵循「句柄优先、路径兜底」：有效 ``fh`` 绑定打开时的
        inode；无句柄或句柄无效时再用 ``path``。读路径上会检查读权限、
        执行分块读取计划、惰性刷新 atime/块访问统计，并可能触发热层降级；
        另根据顺序读启发式调度预读（见 `_schedule_readahead`）。
        """
        logger.debug(
            "读取文件：path={}，size={}，offset={}，fh={}", path, size, offset, fh
        )
        self._ensure_trash_directory_for_caller()
        inode_id = self._inode_id_from_handle_or_path(path, fh)
        with self._content_lock(inode_id):
            with self.metadata.read_transaction():
                node = self._node_from_handle_or_path(path, fh)
                self._require_access(node, os.R_OK)
                plan = self.file_content.plan_read(node, size, offset)
            data, accesses = self.file_content.execute_read_plan(plan)
        if plan.chunks:
            now = time_ns()
            should_flush = self.metadata.defer_node_atime(plan.file_id, now)
            should_flush = (
                self.block_store.record_block_accesses(accesses, now) or should_flush
            )
            if should_flush:
                with self.metadata.transaction():
                    for access in accesses:
                        self.block_store.record_block_presence(access, now)
        if self.block_store.take_demotion_request():
            logger.debug("读取触发热层降级检查：path={}", path)
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()
        self._schedule_readahead(plan, offset, len(data), fh)
        logger.debug(
            "读取完成：path={}，inode={}，返回字节={}", path, plan.file_id, len(data)
        )
        return data

    def _schedule_readahead(self, plan, offset: int, data_len: int, fh) -> None:
        """内部：在顺序读场景下异步预取后续若干文件块。

        用 ``fh`` 在 `_read_positions` 中记录「上一段已读区间的右端点」；
        仅当本次读取起点与上次连续（典型顺序读）且配置允许时，才从
        ``plan`` 最后一个 chunk 起向线程池提交后续 chunk 的预取任务。
        此处 ``fh`` 是打开句柄键，不是 inode；预取任务内再按 inode id
        查元数据，避免与 rename 后的路径不一致。非整数 ``fh`` 或关闭
        预读配置时直接返回。
        """
        if (
            not isinstance(fh, int)
            or self.readahead_blocks <= 0
            or self.readahead_workers <= 0
            or not data_len
            or not plan.chunks
        ):
            return
        previous_end = self._read_positions.get(fh)
        current_end = offset + data_len
        self._read_positions[fh] = current_end
        if previous_end is not None and previous_end != offset:
            return
        last_chunk = plan.chunks[-1].chunk_index
        next_chunk = last_chunk + 1
        for chunk_index in range(next_chunk, next_chunk + self.readahead_blocks):
            key = (plan.file_id, chunk_index)
            with self._readahead_lock:
                if key in self._readahead_inflight:
                    continue
                self._readahead_inflight.add(key)
                executor = self._readahead_executor
                if executor is None:
                    executor = ThreadPoolExecutor(
                        max_workers=self.readahead_workers,
                        thread_name_prefix="ztierfs-readahead",
                    )
                    self._readahead_executor = executor
            executor.submit(self._prefetch_chunk, plan.file_id, chunk_index, key)

    def _prefetch_chunk(
        self, file_id: int, chunk_index: int, key: tuple[int, int]
    ) -> None:
        """内部：预读工作线程执行的单次 chunk 拉取。

        ``file_id`` 为 inode；在只读事务内校验仍为普通文件且未越界后，
        若该 chunk 已有块映射则经 `BlockStore` 读入快照以预热缓存/冷层
        copy-up 路径。失败静默；结束时从 `_readahead_inflight` 移除 ``key``。
        """
        try:
            with self.metadata.read_transaction():
                node = self.metadata.node_by_id(file_id)
                if node is None or node["kind"] != "file":
                    return
                chunk_start = chunk_index * self.chunk_size
                if chunk_start >= node["size"]:
                    return
                expected_size = min(self.chunk_size, node["size"] - chunk_start)
                row = self.metadata.chunk_block(file_id, chunk_index)
            if row is not None:
                self.block_store.read_block_snapshot(row, expected_size)
        except Exception:
            logger.debug("预读 chunk 失败：inode={}，chunk={}", file_id, chunk_index)
        finally:
            with self._readahead_lock:
                self._readahead_inflight.discard(key)

    def _write(self, path: str, data: bytes, offset: int, fh) -> int:
        """内部：在 ``offset`` 处写入 ``data``，返回实际写入字节数。

        inode 解析与 `_read` 相同：优先 ``fh`` 绑定的打开 inode，否则
        ``path``。在内容锁内先只读准备写计划，再写事务提交，以保证
        chunk/块引用与元数据一致；写后可能触发热层降级检查。
        """
        logger.debug(
            "写入文件：path={}，bytes={}，offset={}，fh={}", path, len(data), offset, fh
        )
        self._ensure_trash_directory_for_caller()
        inode_id = self._inode_id_from_handle_or_path(path, fh)
        with self._content_lock(inode_id):
            with self.metadata.read_transaction():
                node = self._node_from_handle_or_path(path, fh)
                self._require_access(node, os.W_OK)
                prepared = self.file_content.prepare_write_file(
                    node, path, data, offset
                )
            with self.metadata.transaction():
                written = self.file_content.commit_prepared_write(prepared)
                logger.debug(
                    "写入完成：path={}，inode={}，bytes={}", path, node["id"], written
                )
        if self.block_store.take_demotion_request():
            logger.debug("写入触发热层降级检查：path={}", path)
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()
        return written

    def _truncate(self, path: str, length: int, fh=None) -> None:
        """内部：将普通文件截断或扩展到 ``length`` 字节。

        ``fh`` 非默认时与打开句柄关联的 inode 对齐（``ftruncate`` 语义）；
        ``fh`` 为 ``None`` 时仅依赖 ``path``（路径截断）。须写权限；
        非普通文件报 ``EISDIR``。完成后可能触发热层降级检查。
        """
        logger.debug("截断文件：path={}，length={}，fh={}", path, length, fh)
        self._ensure_trash_directory_for_caller()
        inode_id = self._inode_id_from_handle_or_path(path, fh)
        with self._content_lock(inode_id):
            with self.metadata.transaction():
                node = self._node_from_handle_or_path(path, fh)
                if node["kind"] != "file":
                    raise FuseOSError(errno.EISDIR)
                self._require_access(node, os.W_OK)
                self.file_content.truncate_file(node["id"], path, length)
                logger.debug(
                    "截断完成：path={}，inode={}，length={}", path, node["id"], length
                )
        if self.block_store.take_demotion_request():
            logger.debug("截断触发热层降级检查：path={}", path)
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()

    def _flush(self) -> None:
        """内部：处理 flush。"""
        logger.debug("刷新元数据事务")
        self.metadata.commit()

    def _release(self, fh) -> int:
        """内部：释放 ``open``/``create`` 返回的句柄 ``fh``。

        从句柄表摘除映射、清除该 ``fh`` 的预读游标、释放本进程对该 inode
        的 advisory 锁上下文。若该 inode 在进程内已无任何打开句柄，且
        磁盘上 ``nlink == 0``（已 unlink 的最后一道引用），则在此触发
        未链接 inode 的数据与元数据清理。返回值对 macFUSE 约定为 0。
        """
        logger.debug("释放文件句柄：fh={}", fh)
        released = self.handles.release(fh)
        if isinstance(fh, int):
            self._read_positions.pop(fh, None)
        if released is None:
            return 0

        file_id, lock_owner = released
        logger.debug(
            "释放文件句柄关联资源：fh={}，inode={}，lock_owner={}",
            fh,
            file_id,
            lock_owner,
        )
        self.locks.release_owner(file_id, lock_owner)
        if self.handles.has_open_file(file_id):
            return 0

        with self.metadata.read_transaction():
            node = self.metadata.node_by_id(file_id)
            needs_cleanup = node is not None and node["nlink"] == 0
        if needs_cleanup:
            with self.metadata.transaction():
                self._cleanup_unlinked_inode(file_id)
        return 0

    def _remove_entry_node(self, node) -> None:
        """内部：处理 remove entry node。"""
        now = time_ns()
        remaining = self.metadata.remove_entry(
            node["parent_id"], node["name"], node["id"], now
        )
        if remaining > 0:
            logger.debug(
                "删除目录项后 inode 仍有硬链接：inode={}，remaining={}",
                node["id"],
                remaining,
            )
            return
        if self.handles.has_open_file(node["id"]):
            logger.debug("删除目录项后 inode 仍被打开，延迟清理：inode={}", node["id"])
            return
        self._delete_inode_payload(node)

    def _cleanup_unlinked_inode(self, inode_id: int) -> None:
        """内部：处理 cleanup unlinked inode。"""
        node = self.metadata.node_by_id(inode_id)
        if node is None or node["nlink"] > 0:
            return
        self._delete_inode_payload(node)

    def _delete_inode_payload(self, node) -> None:
        """内部：处理 delete inode payload。"""
        if node["kind"] == "file":
            self.file_content.remove_file_chunks(node["id"])
        logger.debug(
            "删除 inode payload 和元数据：inode={}，kind={}", node["id"], node["kind"]
        )
        self.metadata.delete_node(node["id"])
