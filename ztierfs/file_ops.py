"""文件句柄释放、预读与孤儿 inode 清理等 low-level FUSE 共用支撑。"""

from concurrent.futures import ThreadPoolExecutor
from time import time_ns

from loguru import logger

from .fs_mixins import FileSystemMixinBase


class FileOpsMixin(FileSystemMixinBase):
    """供 `InodeFuseMixin` 复用的文件侧辅助逻辑。"""

    def _schedule_readahead(self, plan, offset: int, data_len: int, fh) -> None:
        """在顺序读场景下异步预取后续若干文件块。"""
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
        """预读工作线程执行的单次 chunk 拉取。"""
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

    def _flush(self) -> None:
        """刷新元数据事务。"""
        logger.debug("刷新元数据事务")
        self.metadata.commit()

    def _release(self, fh) -> int:
        """释放文件句柄，并在最后一个已 unlink 句柄关闭后清理孤儿 inode。"""
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
        """删除目录项，必要时同步回收无链接且未打开的 inode payload。"""
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
        """清理已无链接且无打开句柄的孤儿 inode。"""
        node = self.metadata.node_by_id(inode_id)
        if node is None or node["nlink"] > 0:
            return
        self._delete_inode_payload(node)

    def _delete_inode_payload(self, node) -> None:
        """删除 inode 的内容引用与元数据行。"""
        if node["kind"] == "file":
            self.file_content.remove_file_chunks(node["id"])
        logger.debug(
            "删除 inode payload 和元数据：inode={}，kind={}", node["id"], node["kind"]
        )
        self.metadata.delete_node(node["id"])
