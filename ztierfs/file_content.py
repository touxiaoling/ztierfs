"""普通文件字节层：按固定 chunk 切分，读路径上缺失映射视为稀疏零字节，写路径支持局部覆盖与截断。

协调 ``file_chunks``、内容寻址块的引用计数与 ``BlockStore``；小文件可整段内联存 SQLite，
超过阈值或迁出时再写入块存储；``PreparedBlock`` / ``PreparedFileWrite`` 使编码与 refcount 更新成对发生。
"""

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
    """对 ``plan_read`` 结果的封装：要么整文件内联切片，要么按 chunk 列表读块。

    内联路径时 ``inline_data`` 非空、``chunks`` 为空；分块路径时 ``inline_data`` 为 ``None``，
    ``chunks`` 按文件偏移顺序覆盖 ``[offset, offset+size)`` 与文件末尾的交集。
    """

    file_id: int
    chunks: list[ChunkRead]
    inline_data: bytes | None = None


@dataclass(frozen=True)
class PreparedInlineFile:
    """待提交的小文件整文件内联：字节形态与是否压缩已定，供 ``set_prepared_inline_file`` 写入元数据。

    ``payload`` 为存入 SQLite 的字节串（可能已是 zstd 压缩包）；``raw_size`` 为解压后的逻辑长度。
    ``clear_chunks`` 为真时，提交前会先删掉 ``file_chunks`` 并递减旧块 refcount。
    """

    payload: bytes
    compressed: bool
    raw_size: int
    clear_chunks: bool


@dataclass(frozen=True)
class PreparedFileWrite:
    """待提交的写入结果：非空写要么是整文件内联，要么是若干 ``PreparedBlock`` 按 chunk 绑定。

    ``inline_file`` 与 ``chunks`` 互斥（一次提交只走其一）。``clear_inline`` 为真表示在写块之前
    清除内联列（从内联迁回分块存储时）。提交时通过 ``set_prepared_chunk`` / 内联路径更新块引用，
    替换旧块会先 ``decrement_block``、新块 ``ensure_prepared_block`` 后 ``attach``，维持 refcount 一致。
    """

    file_id: int
    bytes_written: int
    new_size: int
    inline_file: PreparedInlineFile | None
    chunks: list[tuple[int, PreparedBlock]]
    clear_inline: bool = False


class FileContentService:
    """在 ``file_chunks``、块引用计数与 ``BlockStore`` 之间编排普通文件的字节读写。

    负责生成读取计划（含稀疏与内联）、准备压缩/内容寻址块、提交后更新 inode 大小与时间戳；
    截断与覆盖路径与元数据、块 refcount 在同一调用链上保持一致。
    """

    def __init__(
        self,
        metadata: MetadataStore,
        block_store: BlockStore,
        *,
        chunk_size: int,
        small_file_inline_max: int,
        compression_allowed: Callable[[str], bool],
    ):
        """绑定元数据、块存储、分块大小、小文件内联上限及按路径是否允许压缩。"""
        self.metadata = metadata
        self.block_store = block_store
        self.chunk_size = chunk_size
        self.small_file_inline_max = small_file_inline_max
        self.compression_allowed = compression_allowed

    def read_file(self, node, size: int, offset: int) -> bytes:
        """按节点读 ``[offset, offset+size)``：生成计划、读块/内联、更新访问时间与块访问统计。"""
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
        """构造 ``FileReadPlan``：内联则切片内存字节；否则列出覆盖区间的 chunk 及每段 ``ChunkRead``。

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
        """按计划拼接返回值：内联直接返回；否则对 ``row`` 为 ``None`` 的段输出零填充，有则向 ``BlockStore`` 取快照并切片。"""
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
        """将 ``PreparedFileWrite`` 落库：内联路径写内联列；否则可选清除内联后逐 chunk ``set_prepared_chunk``，最后更新文件大小。"""
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
        """将文件截断到 ``length``：仍满足内联阈值则截断内联；否则先 ``promote_inline_file`` 再删尾部 chunk 或重写最后一个部分 chunk。

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
        """删除 ``file_chunks`` 中该索引并递减对应块 refcount。"""
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
        """从 ``first_index`` 起删除所有 chunk 记录；若 ``first_index==0`` 同时清除内联列，并对每个被删哈希 ``decrement_block``。"""
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
        """当前配置下给定逻辑大小是否允许仅用内联列存放（受 ``small_file_inline_max`` 与 ``chunk_size`` 约束）。"""
        return (
            self.small_file_inline_max > 0
            and size <= self.small_file_inline_max
            and size <= self.chunk_size
        )

    def has_inline_payload(self, node) -> bool:
        """节点是否在元数据中存有内联文件体（``inline_stored_size`` 非零）。"""
        return bool(node["inline_stored_size"])

    def set_inline_file(
        self, file_id: int, data: bytes, compress: bool, *, clear_chunks: bool = True
    ) -> None:
        """将原始字节编码为内联并写入；``clear_chunks`` 控制是否先清空分块表。"""
        self.set_prepared_inline_file(
            file_id, self.prepare_inline_file(data, compress, clear_chunks=clear_chunks)
        )

    def prepare_inline_file(
        self, data: bytes, compress: bool, *, clear_chunks: bool
    ) -> PreparedInlineFile:
        """构造 ``PreparedInlineFile``：可选 zstd 压缩至不超过内联上限，否则存明文；记录逻辑 ``raw_size``。"""
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
        """应用内联写入：空 payload 等价清空文件；否则按需 ``remove_file_chunks`` 再写入内联列与时间戳。"""
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
        """把当前内联文件体迁出到分块存储：解码后清内联列，将数据写入 chunk 0（含压缩策略）。"""
        data = self.decode_inline_payload(node)
        self.metadata.clear_inline_file(node["id"])
        if data:
            self.set_chunk(node["id"], 0, data, self.compression_allowed(path))

    def decode_inline_payload(self, node) -> bytes:
        """从元数据行读出内联字节：未压缩则校验长度；压缩则 zstd 解压并校验与 ``node["size"]`` 一致。"""
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
