"""普通文件字节层：按固定 chunk 切分，读路径上缺失映射视为稀疏零字节，写路径支持局部覆盖与截断。

协调 ``file_chunks``、内容寻址块的引用计数与 ``BlockStore``；小文件可作为 inline block
存入 SQLite，但文件内容仍统一通过 ``file_chunks -> blocks`` 表达。
"""

import errno

from collections.abc import Callable
from dataclasses import dataclass
from time import time_ns
from typing import Any

from loguru import logger
from macfusepy import FuseOSError

from .block_store import BlockAccess, BlockStore, PreparedBlock
from .metadata import MetadataStore
from .metadata.chunks import ChunkReplacement


@dataclass(frozen=True)
class ChunkRead:
    """单次读取计划中，某一逻辑 chunk 的切片信息。

    ``row`` 为 ``file_chunks`` 对应的块元数据行；若为 ``None`` 表示该区间未映射块，
    执行阶段按稀疏语义用零字节填充 ``[start, stop)``，不占块存储、不涉及 refcount。
    ``expected_size`` 为该 chunk 在文件内的有效长度（尾块可小于 ``chunk_size``）。
    """

    row: Any | None
    chunk_index: int
    expected_size: int
    start: int
    stop: int


@dataclass(frozen=True)
class FileReadPlan:
    """对 ``plan_read`` 结果的封装：``chunks`` 按文件偏移顺序覆盖目标区间。"""

    file_id: int
    chunks: list[ChunkRead]


@dataclass(frozen=True)
class PreparedFileWrite:
    """待提交的写入结果：若干 ``PreparedBlock`` 按 chunk 绑定。"""

    file_id: int
    bytes_written: int
    new_size: int
    chunks: list[tuple[int, PreparedBlock]]


class FileContentService:
    """在 ``file_chunks``、块引用计数与 ``BlockStore`` 之间编排普通文件的字节读写。

    负责生成读取计划（含稀疏）、准备压缩/内容寻址块、提交后更新 inode 大小与时间戳；
    截断与覆盖路径与元数据、块 refcount 在同一调用链上保持一致。
    """

    def __init__(
        self,
        metadata: MetadataStore,
        block_store: BlockStore,
        *,
        chunk_size: int,
        compression_allowed: Callable[[str], bool],
    ):
        """绑定元数据、块存储、分块大小及按路径是否允许压缩。"""
        self.metadata = metadata
        self.block_store = block_store
        self.chunk_size = chunk_size
        self.compression_allowed = compression_allowed

    def read_file(self, node, size: int, offset: int) -> bytes:
        """按节点读 ``[offset, offset+size)``：生成计划、读块、更新访问时间与块访问统计。"""
        with self.metadata.read_transaction():
            plan = self.plan_read(node, size, offset)
        data, accesses = self.execute_read_plan(plan)
        if not data:
            return data
        now = time_ns()
        should_flush = self.metadata.defer_node_atime(node["id"], now)
        if accesses:
            should_flush = (
                self.block_store.record_block_accesses(accesses, now) or should_flush
            )
        if should_flush:
            with self.metadata.transaction():
                for access in accesses:
                    self.block_store.record_block_presence(access, now)
        if self.block_store.take_demotion_request():
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()
        return data

    def plan_read(self, node, size: int, offset: int) -> FileReadPlan:
        """构造 ``FileReadPlan``：列出覆盖区间的 chunk 及每段 ``ChunkRead``。

        无映射 chunk 在计划中 ``row`` 为 ``None``（稀疏）；offset 越界或 size<=0 返回空计划。
        """
        if size <= 0:
            logger.debug("生成读取计划：inode={}，size<=0，返回空计划", node["id"])
            return FileReadPlan(file_id=node["id"], chunks=[])
        if offset < 0:
            raise FuseOSError(errno.EINVAL)
        if node["kind"] != "file":
            raise FuseOSError(errno.EISDIR)
        if offset >= node["size"]:
            logger.debug(
                "生成读取计划：inode={}，offset={} 超过文件大小 {}",
                node["id"],
                offset,
                node["size"],
            )
            return FileReadPlan(file_id=node["id"], chunks=[])

        end = min(offset + size, node["size"])
        chunks: list[ChunkRead] = []
        first_chunk = offset // self.chunk_size
        last_chunk = (end - 1) // self.chunk_size
        block_rows = (
            None
            if first_chunk == last_chunk
            else self.metadata.chunk_blocks(node["id"], first_chunk, last_chunk)
        )
        for chunk_index in range(first_chunk, last_chunk + 1):
            chunk_start = chunk_index * self.chunk_size
            chunk_end = min(chunk_start + self.chunk_size, node["size"])
            expected_size = chunk_end - chunk_start
            row = (
                self.metadata.chunk_block(node["id"], chunk_index)
                if block_rows is None
                else block_rows.get(chunk_index)
            )
            start = max(offset - chunk_start, 0)
            stop = min(end - chunk_start, expected_size)
            chunks.append(ChunkRead(row, chunk_index, expected_size, start, stop))

        logger.debug(
            "生成读取计划：inode={}，offset={}，size={}，chunks={}",
            node["id"],
            offset,
            size,
            len(chunks),
        )
        return FileReadPlan(file_id=node["id"], chunks=chunks)

    def execute_read_plan(self, plan: FileReadPlan) -> tuple[bytes, list[BlockAccess]]:
        """按计划拼接返回值：对 ``row`` 为 ``None`` 的段输出零填充，有则向 ``BlockStore`` 取快照并切片。"""
        chunks: list[bytes] = []
        accesses: list[BlockAccess] = []
        block_reads = [read for read in plan.chunks if read.row is not None]
        block_results = iter(
            self.block_store.read_block_snapshots(
                [(read.row, read.expected_size) for read in block_reads]
            )
        )
        for read in plan.chunks:
            if read.row is None:
                logger.debug(
                    "读取稀疏块：inode={}，expected_size={}",
                    plan.file_id,
                    read.expected_size,
                )
                chunks.append(b"\x00" * (read.stop - read.start))
                continue
            chunk, access = next(block_results)
            accesses.append(access)
            chunks.append(chunk[read.start : read.stop])
        logger.debug(
            "执行读取计划完成：inode={}，chunks={}，block_accesses={}",
            plan.file_id,
            len(plan.chunks),
            len(accesses),
        )
        return b"".join(chunks), accesses

    def write_file(self, node, path: str, data: bytes, offset: int) -> int:
        """``prepare_write_file`` 后 ``commit_prepared_write``；返回本次写入字节数。"""
        return self.commit_prepared_write(
            self.prepare_write_file(node, path, data, offset)
        )

    def prepare_write_file(
        self, node, path: str, data: bytes, offset: int
    ) -> PreparedFileWrite:
        """计算写入后的 ``PreparedFileWrite``：小文件可仍保持内联；否则合并旧内联/已存在 chunk 与覆盖数据，再 ``prepare_blocks``。

        稀疏扩展会先读出逻辑零再写入；尾块与跨 chunk 覆盖已展开为完整 chunk 字节再寻址写块。
        """
        if offset < 0:
            raise FuseOSError(errno.EINVAL)
        if not data:
            return PreparedFileWrite(node["id"], 0, node["size"], [])
        if node["kind"] != "file":
            raise FuseOSError(errno.EISDIR)

        old_size = node["size"]
        new_size = max(old_size, offset + len(data))
        compress = self.compression_allowed(path)
        logger.debug(
            "准备写入文件内容：inode={}，path={}，old_size={}，new_size={}，bytes={}，compress={}",
            node["id"],
            path,
            old_size,
            new_size,
            len(data),
            compress,
        )
        data_pos = 0
        first_chunk = offset // self.chunk_size
        last_chunk = (offset + len(data) - 1) // self.chunk_size

        if old_size == 0 and offset == 0 and first_chunk == last_chunk:
            return PreparedFileWrite(
                node["id"],
                len(data),
                new_size,
                self.block_store.prepare_blocks([(first_chunk, data)], compress),
            )

        pending_chunks: list[tuple[int, bytes]] = []
        pending_chunk_indexes: set[int] = set()
        for chunk_index in range(first_chunk, last_chunk + 1):
            chunk_start = chunk_index * self.chunk_size
            chunk_end = min(chunk_start + self.chunk_size, new_size)
            chunk_len = chunk_end - chunk_start
            existing_len = max(0, min(self.chunk_size, old_size - chunk_start))

            write_start = max(offset - chunk_start, 0)
            write_stop = min(offset + len(data) - chunk_start, chunk_len)
            take = write_stop - write_start
            source = data[data_pos : data_pos + take]
            data_pos += take
            if write_start == 0 and take == chunk_len:
                pending_chunks.append((chunk_index, source))
                pending_chunk_indexes.add(chunk_index)
                continue
            chunk = bytearray(self.read_chunk(node["id"], chunk_index, existing_len))
            if len(chunk) < chunk_len:
                chunk.extend(b"\x00" * (chunk_len - len(chunk)))
            chunk[write_start:write_stop] = source
            pending_chunks.append((chunk_index, bytes(chunk)))
            pending_chunk_indexes.add(chunk_index)

        logger.debug(
            "写入文件内容分块完成：inode={}，first_chunk={}，last_chunk={}，pending_chunks={}",
            node["id"],
            first_chunk,
            last_chunk,
            len(pending_chunks),
        )
        return PreparedFileWrite(
            node["id"],
            len(data),
            new_size,
            self.block_store.prepare_blocks(pending_chunks, compress),
        )

    def commit_prepared_write(self, write: PreparedFileWrite) -> int:
        """将 ``PreparedFileWrite`` 落库：提交 chunk 替换，最后更新文件大小。"""
        if write.bytes_written == 0:
            return 0
        self.set_prepared_chunks(write.file_id, write.chunks)
        self.metadata.set_node_size(write.file_id, write.new_size, time_ns())
        return write.bytes_written

    def truncate_file(self, file_id: int, path: str, length: int) -> None:
        """将文件截断到 ``length``：删除尾部 chunk 或重写最后一个部分 chunk。

        删除 chunk 会递减块 refcount；缩短时在边界块上保留 ``length`` 之前的字节。
        """
        if length < 0:
            raise FuseOSError(errno.EINVAL)
        node = self.metadata.node_by_id(file_id)
        if node is None:
            raise FuseOSError(errno.ENOENT)
        old_size = node["size"]
        if length == old_size:
            logger.debug("截断跳过：inode={}，length 未变化 {}", file_id, length)
            return

        compress = self.compression_allowed(path)
        logger.debug(
            "截断文件内容：inode={}，path={}，old_size={}，new_size={}，compress={}",
            file_id,
            path,
            old_size,
            length,
            compress,
        )
        if length == 0:
            self.remove_file_chunks(file_id)
        else:
            first_removed = (length + self.chunk_size - 1) // self.chunk_size
            self.remove_file_chunks(file_id, first_removed)
            if length < old_size and length % self.chunk_size:
                last_index = length // self.chunk_size
                keep = length - last_index * self.chunk_size
                chunk = self.read_chunk(
                    file_id,
                    last_index,
                    min(self.chunk_size, old_size - last_index * self.chunk_size),
                )
                self.set_chunk(file_id, last_index, chunk[:keep], compress)

        now = time_ns()
        self.metadata.set_node_size(file_id, length, now)

    def read_chunk(self, file_id: int, chunk_index: int, expected_size: int) -> bytes:
        """读取指定逻辑 chunk 的完整有效字节；元数据无映射时返回长度为 ``expected_size`` 的零填充（稀疏）。"""
        if expected_size <= 0:
            return b""
        row = self.metadata.chunk_block(file_id, chunk_index)
        if row is None:
            logger.debug(
                "读取缺失 chunk，返回稀疏零填充：inode={}，chunk={}，expected_size={}",
                file_id,
                chunk_index,
                expected_size,
            )
            return b"\x00" * expected_size
        data, _access = self.block_store.read_block_snapshot(row, expected_size)
        return data

    def set_chunk(
        self, file_id: int, chunk_index: int, data: bytes, compress: bool
    ) -> None:
        """将 ``data`` 写成该 chunk 的内容；空数据等价于 ``remove_chunk``。"""
        if not data:
            self.remove_chunk(file_id, chunk_index)
            return
        self.set_prepared_chunk(
            file_id, chunk_index, self.block_store.prepare_block(data, compress)
        )

    def set_prepared_chunk(
        self, file_id: int, chunk_index: int, block: PreparedBlock
    ) -> None:
        """提交 ``PreparedBlock``：哈希未变则只更新 chunk 记录尺寸；否则确保块存在、绑定新哈希并 ``decrement_block`` 旧哈希。"""
        self.set_prepared_chunks(file_id, [(chunk_index, block)])

    def set_prepared_chunks(
        self, file_id: int, chunks: list[tuple[int, PreparedBlock]]
    ) -> None:
        """提交一组已准备块：payload 先落地，再批量替换 chunk/refcount 元数据。"""
        if not chunks:
            return
        replacements: dict[int, ChunkReplacement] = {}
        self.block_store.ensure_prepared_blocks(block for _chunk_index, block in chunks)
        for chunk_index, block in chunks:
            replacements[chunk_index] = ChunkReplacement(block.digest, block.raw_size)
        deltas = self.metadata.replace_file_chunks(file_id, replacements)
        logger.debug(
            "批量提交 chunk：inode={}，chunks={}，refcount_deltas={}",
            file_id,
            len(replacements),
            len(deltas),
        )

    def remove_chunk(self, file_id: int, chunk_index: int) -> None:
        """删除 ``file_chunks`` 中该索引并递减对应块 refcount。"""
        deltas = self.metadata.replace_file_chunks(
            file_id, {chunk_index: ChunkReplacement(None)}
        )
        if deltas:
            logger.debug(
                "删除 chunk：inode={}，chunk={}，refcount_deltas={}",
                file_id,
                chunk_index,
                len(deltas),
            )

    def remove_file_chunks(self, file_id: int, first_index: int = 0) -> None:
        """从 ``first_index`` 起删除所有 chunk 记录，并对每个被删哈希更新 refcount。"""
        deltas = self.metadata.replace_file_chunks(file_id, {}, delete_from=first_index)
        logger.debug(
            "删除文件 chunk 范围：inode={}，first_index={}，refcount_deltas={}",
            file_id,
            first_index,
            len(deltas),
        )
