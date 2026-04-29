"""按固定分块组织的文件体：稀疏读、局部覆盖、截断、内联小文件与块引用更新。"""

import errno

from collections.abc import Callable
from dataclasses import dataclass
from time import time_ns
from typing import Any

import compression.zstd as zstd
from loguru import logger
from macfusepy import FuseOSError

from .block_store import BlockAccess, BlockStore, PreparedBlock
from .metadata import MetadataStore


@dataclass(frozen=True)
class ChunkRead:
    row: Any | None
    chunk_index: int
    expected_size: int
    start: int
    stop: int


@dataclass(frozen=True)
class FileReadPlan:
    file_id: int
    chunks: list[ChunkRead]
    inline_data: bytes | None = None


@dataclass(frozen=True)
class PreparedInlineFile:
    payload: bytes
    compressed: bool
    raw_size: int
    clear_chunks: bool


@dataclass(frozen=True)
class PreparedFileWrite:
    file_id: int
    bytes_written: int
    new_size: int
    inline_file: PreparedInlineFile | None
    chunks: list[tuple[int, PreparedBlock]]
    clear_inline: bool = False


class FileContentService:
    """文件字节与 file_chunks / blocks 元数据及 BlockStore 之间的协调层。"""

    def __init__(
        self,
        metadata: MetadataStore,
        block_store: BlockStore,
        *,
        chunk_size: int,
        small_file_inline_max: int,
        compression_allowed: Callable[[str], bool],
    ):
        self.metadata = metadata
        self.block_store = block_store
        self.chunk_size = chunk_size
        self.small_file_inline_max = small_file_inline_max
        self.compression_allowed = compression_allowed

    def read_file(self, node, size: int, offset: int) -> bytes:
        plan = self.plan_read(node, size, offset)
        data, accesses = self.execute_read_plan(plan)
        now = time_ns()
        self.metadata.touch_node_atime(node["id"], now)
        for access in accesses:
            self.block_store.record_block_access(access, now)
        return data

    def plan_read(self, node, size: int, offset: int) -> FileReadPlan:
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
        if self.has_inline_payload(node):
            data = self.decode_inline_payload(node)
            return FileReadPlan(
                file_id=node["id"], chunks=[], inline_data=data[offset:end]
            )

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
        if plan.inline_data is not None:
            return plan.inline_data, []
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
        return self.commit_prepared_write(
            self.prepare_write_file(node, path, data, offset)
        )

    def prepare_write_file(
        self, node, path: str, data: bytes, offset: int
    ) -> PreparedFileWrite:
        if offset < 0:
            raise FuseOSError(errno.EINVAL)
        if not data:
            return PreparedFileWrite(node["id"], 0, node["size"], None, [])
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
        if self.can_store_inline(new_size):
            if self.has_inline_payload(node):
                chunk = bytearray(self.decode_inline_payload(node))
                if len(chunk) < new_size:
                    chunk.extend(b"\x00" * (new_size - len(chunk)))
                chunk[offset : offset + len(data)] = data
                return PreparedFileWrite(
                    node["id"],
                    len(data),
                    new_size,
                    self.prepare_inline_file(
                        bytes(chunk), compress, clear_chunks=False
                    ),
                    [],
                )
            if old_size == 0 and offset == 0:
                return PreparedFileWrite(
                    node["id"],
                    len(data),
                    new_size,
                    self.prepare_inline_file(data, compress, clear_chunks=False),
                    [],
                )

        inline_data = None
        if self.has_inline_payload(node):
            inline_data = self.decode_inline_payload(node)

        data_pos = 0
        first_chunk = offset // self.chunk_size
        last_chunk = (offset + len(data) - 1) // self.chunk_size

        if old_size == 0 and offset == 0 and first_chunk == last_chunk:
            return PreparedFileWrite(
                node["id"],
                len(data),
                new_size,
                None,
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
            if inline_data is not None and chunk_index == 0:
                chunk = bytearray(inline_data[:existing_len])
            else:
                chunk = bytearray(
                    self.read_chunk(node["id"], chunk_index, existing_len)
                )
            if len(chunk) < chunk_len:
                chunk.extend(b"\x00" * (chunk_len - len(chunk)))
            chunk[write_start:write_stop] = source
            pending_chunks.append((chunk_index, bytes(chunk)))
            pending_chunk_indexes.add(chunk_index)

        if inline_data is not None and old_size > 0 and 0 not in pending_chunk_indexes:
            pending_chunks.insert(0, (0, inline_data))

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
            None,
            self.block_store.prepare_blocks(pending_chunks, compress),
            clear_inline=inline_data is not None,
        )

    def commit_prepared_write(self, write: PreparedFileWrite) -> int:
        if write.bytes_written == 0:
            return 0
        if write.inline_file is not None:
            self.set_prepared_inline_file(write.file_id, write.inline_file)
            return write.bytes_written
        if write.clear_inline:
            self.metadata.clear_inline_file(write.file_id)
        for chunk_index, prepared in write.chunks:
            self.set_prepared_chunk(write.file_id, chunk_index, prepared)
        self.metadata.set_node_size(write.file_id, write.new_size, time_ns())
        return write.bytes_written

    def truncate_file(self, file_id: int, path: str, length: int) -> None:
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
        if self.has_inline_payload(node):
            data = self.decode_inline_payload(node)
            if self.can_store_inline(length):
                self.set_inline_file(
                    file_id,
                    data[:length].ljust(length, b"\x00"),
                    compress,
                    clear_chunks=False,
                )
                return
            self.promote_inline_file(node, path)

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
        if not data:
            self.remove_chunk(file_id, chunk_index)
            return
        self.set_prepared_chunk(
            file_id, chunk_index, self.block_store.prepare_block(data, compress)
        )

    def set_prepared_chunk(
        self, file_id: int, chunk_index: int, block: PreparedBlock
    ) -> None:
        old = self.metadata.file_chunk_hash(file_id, chunk_index)

        if old is not None and old["hash"] == block.digest:
            self.metadata.update_file_chunk_size(file_id, chunk_index, block.raw_size)
            logger.debug(
                "chunk 指向未变化，仅更新大小：inode={}，chunk={}，hash={}",
                file_id,
                chunk_index,
                block.digest[:12],
            )
            return

        self.block_store.ensure_prepared_block(block)
        self.metadata.attach_file_chunk_to_block(
            file_id, chunk_index, block.digest, block.raw_size
        )
        logger.debug(
            "绑定 chunk 到块：inode={}，chunk={}，hash={}，raw_size={}",
            file_id,
            chunk_index,
            block.digest[:12],
            block.raw_size,
        )
        if old is not None:
            self.block_store.decrement_block(old["hash"])
            logger.debug(
                "替换 chunk 旧块引用：inode={}，chunk={}，old_hash={}",
                file_id,
                chunk_index,
                old["hash"][:12],
            )

    def remove_chunk(self, file_id: int, chunk_index: int) -> None:
        old = self.metadata.file_chunk_hash(file_id, chunk_index)
        if old is not None:
            self.metadata.delete_file_chunk(file_id, chunk_index)
            self.block_store.decrement_block(old["hash"])
            logger.debug(
                "删除 chunk：inode={}，chunk={}，old_hash={}",
                file_id,
                chunk_index,
                old["hash"][:12],
            )

    def remove_file_chunks(self, file_id: int, first_index: int = 0) -> None:
        if first_index == 0:
            self.metadata.clear_inline_file(file_id)
        hashes = self.metadata.file_chunk_hashes_from(file_id, first_index)
        self.metadata.delete_file_chunks_from(file_id, first_index)
        for digest in hashes:
            self.block_store.decrement_block(digest)
        logger.debug(
            "删除文件 chunk 范围：inode={}，first_index={}，count={}",
            file_id,
            first_index,
            len(hashes),
        )

    def can_store_inline(self, size: int) -> bool:
        return (
            self.small_file_inline_max > 0
            and size <= self.small_file_inline_max
            and size <= self.chunk_size
        )

    def has_inline_payload(self, node) -> bool:
        return bool(node["inline_stored_size"])

    def set_inline_file(
        self, file_id: int, data: bytes, compress: bool, *, clear_chunks: bool = True
    ) -> None:
        self.set_prepared_inline_file(
            file_id, self.prepare_inline_file(data, compress, clear_chunks=clear_chunks)
        )

    def prepare_inline_file(
        self, data: bytes, compress: bool, *, clear_chunks: bool
    ) -> PreparedInlineFile:
        if not data:
            return PreparedInlineFile(b"", False, 0, clear_chunks)
        payload = data
        compressed = False
        if compress and len(data) >= self.block_store.compression_min_bytes:
            packed, was_compressed = self.block_store.encode_block(data, True)
            if was_compressed and len(packed) <= self.small_file_inline_max:
                payload = packed
                compressed = True
        return PreparedInlineFile(payload, compressed, len(data), clear_chunks)

    def set_prepared_inline_file(
        self, file_id: int, inline_file: PreparedInlineFile
    ) -> None:
        if not inline_file.payload:
            self.remove_file_chunks(file_id)
            self.metadata.set_node_size(file_id, 0, time_ns())
            return
        if inline_file.clear_chunks:
            self.remove_file_chunks(file_id)
        self.metadata.set_inline_file(
            file_id,
            inline_file.payload,
            compressed=inline_file.compressed,
            raw_size=inline_file.raw_size,
            now=time_ns(),
        )

    def promote_inline_file(self, node, path: str) -> None:
        data = self.decode_inline_payload(node)
        self.metadata.clear_inline_file(node["id"])
        if data:
            self.set_chunk(node["id"], 0, data, self.compression_allowed(path))

    def decode_inline_payload(self, node) -> bytes:
        row = self.metadata.inline_payload(node["id"])
        if row is None:
            logger.error("inode 内联 payload 缺失：inode={}", node["id"])
            raise FuseOSError(errno.EIO)
        payload = bytes(row["payload"])
        if not row["compressed"]:
            if len(payload) != row["raw_size"] or len(payload) != node["size"]:
                logger.error(
                    "inode 内联 payload 大小不匹配：inode={}，expected={}，actual={}",
                    node["id"],
                    node["size"],
                    len(payload),
                )
                raise FuseOSError(errno.EIO)
            return payload
        try:
            data = zstd.decompress(payload)
        except zstd.ZstdError as exc:
            logger.error("inode 内联 payload 解压失败：inode={}", node["id"])
            raise FuseOSError(errno.EIO) from exc
        if len(data) != row["raw_size"] or len(data) != node["size"]:
            logger.error(
                "inode 内联 payload 大小不匹配：inode={}，expected={}，actual={}",
                node["id"],
                node["size"],
                len(data),
            )
            raise FuseOSError(errno.EIO)
        return data
