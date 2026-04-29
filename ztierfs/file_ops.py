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
    def _create(self, path: str, mode: int, flags: int = os.O_RDWR) -> int:
        logger.debug("创建或截断文件：path={}，mode={:o}，flags={:#x}", path, mode, flags)
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
        fh = self._create(path, mode, flags)
        with self.metadata.read_transaction():
            node = self._node_from_handle_or_path(path, fh)
            return fh, self._attrs_from_node(node)

    def _open(self, path: str, flags: int) -> int:
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
                node = self.metadata.get_node(path)
                self.file_content.truncate_file(node["id"], path, 0)
                logger.debug("打开时截断文件：path={}，inode={}", path, inode_id)
        return self.handles.new(inode_id, self._lock_owner())

    def _node_from_handle_or_path(self, path: str, fh) -> Any:
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
        file_id = self.handles.file_id(fh)
        if file_id is not None:
            return file_id
        with self.metadata.read_transaction():
            return self.metadata.get_node(path)["id"]

    def _read(self, path: str, size: int, offset: int, fh) -> bytes:
        logger.debug("读取文件：path={}，size={}，offset={}，fh={}", path, size, offset, fh)
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
            should_flush = self.block_store.record_block_accesses(accesses, now) or should_flush
            if should_flush:
                with self.metadata.transaction():
                    for access in accesses:
                        self.block_store.record_block_presence(access, now)
        if self.block_store.take_demotion_request():
            logger.debug("读取触发热层降级检查：path={}", path)
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()
        self._schedule_readahead(plan, offset, len(data), fh)
        logger.debug("读取完成：path={}，inode={}，返回字节={}", path, plan.file_id, len(data))
        return data

    def _schedule_readahead(self, plan, offset: int, data_len: int, fh) -> None:
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

    def _prefetch_chunk(self, file_id: int, chunk_index: int, key: tuple[int, int]) -> None:
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
        logger.debug("写入文件：path={}，bytes={}，offset={}，fh={}", path, len(data), offset, fh)
        self._ensure_trash_directory_for_caller()
        inode_id = self._inode_id_from_handle_or_path(path, fh)
        with self._content_lock(inode_id):
            with self.metadata.read_transaction():
                node = self._node_from_handle_or_path(path, fh)
                self._require_access(node, os.W_OK)
                prepared = self.file_content.prepare_write_file(node, path, data, offset)
            with self.metadata.transaction():
                written = self.file_content.commit_prepared_write(prepared)
                logger.debug("写入完成：path={}，inode={}，bytes={}", path, node["id"], written)
        if self.block_store.take_demotion_request():
            logger.debug("写入触发热层降级检查：path={}", path)
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()
        return written

    def _truncate(self, path: str, length: int, fh=None) -> None:
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
                logger.debug("截断完成：path={}，inode={}，length={}", path, node["id"], length)
        if self.block_store.take_demotion_request():
            logger.debug("截断触发热层降级检查：path={}", path)
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()

    def _flush(self) -> None:
        logger.debug("刷新元数据事务")
        self.metadata.commit()

    def _release(self, fh) -> int:
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
        now = time_ns()
        remaining = self.metadata.remove_entry(
            node["parent_id"], node["name"], node["id"], now
        )
        if remaining > 0:
            logger.debug("删除目录项后 inode 仍有硬链接：inode={}，remaining={}", node["id"], remaining)
            return
        if self.handles.has_open_file(node["id"]):
            logger.debug("删除目录项后 inode 仍被打开，延迟清理：inode={}", node["id"])
            return
        self._delete_inode_payload(node)

    def _cleanup_unlinked_inode(self, inode_id: int) -> None:
        node = self.metadata.node_by_id(inode_id)
        if node is None or node["nlink"] > 0:
            return
        self._delete_inode_payload(node)

    def _delete_inode_payload(self, node) -> None:
        if node["kind"] == "file":
            self.file_content.remove_file_chunks(node["id"])
        logger.debug("删除 inode payload 和元数据：inode={}，kind={}", node["id"], node["kind"])
        self.metadata.delete_node(node["id"])
