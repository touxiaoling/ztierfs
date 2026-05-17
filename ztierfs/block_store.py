"""内容寻址块存储（tier1=热层，tier2=冷层）。

负责 zstd 压缩、按内容摘要在各 tier 目录下的原子写入、与 SQLite `blocks`
元数据协同（引用计数、存在性、preferred_tier）。包含读 LRU 缓存、异步 prepare、热→冷降级与读冷层时的
copy-up，以及在元数据与物理文件不一致时的有限自修复。

块文件即用户数据：须保持临时文件+rename、fsync 等与项目其它模块一致的原子性与持久化约定。
"""

import errno
import os
import shutil
import threading

from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from collections import OrderedDict
from pathlib import Path
from time import time_ns

import compression.zstd as zstd
from loguru import logger
from macfusepy import FuseOSError

from .block_layout import block_file_path
from .metadata import MetadataStore
from .perf import timed
from .tier_access import (
    PathMissing,
    PathUnavailable,
    probe_path,
    read_path_bytes,
    unlink_path,
)


@dataclass(frozen=True)
class TieringPolicy:
    """冷热分层（tiering）策略：用字节阈值驱动热层→冷层降级与写放大控制。

    ``hot_max_bytes`` / ``hot_min_bytes`` 构成滞回区间：热层已存 payload 总字节数超过
    ``hot_max_bytes`` 时触发降级循环，直至回落到 ``hot_min_bytes`` 以下或没有候选块。
    ``protected_prefix_chunks`` 保护每个文件最前若干逻辑块不参与降级，降低顺序读头延迟。
    ``min_hot_age_ns`` 以纳秒计的最小“在热层年龄”，过新的块不作为降级候选，避免抖动。
    ``cold_copy_cleanup_age_ns`` 在 copy-up 后保留冷层副本的保留期，超时后由维护清理删除
    冷层冗余文件（元数据仍由调用方配合更新）。
    """

    hot_max_bytes: int
    hot_min_bytes: int
    protected_prefix_chunks: int = 4
    min_hot_age_ns: int = 24 * 60 * 60 * 1_000_000_000
    cold_copy_cleanup_age_ns: int = 0

    def __post_init__(self) -> None:
        """校验各字段非负及 ``hot_min_bytes`` 不大于 ``hot_max_bytes``。"""
        if self.hot_max_bytes < 0:
            raise ValueError("hot_max_bytes must not be negative")
        if self.hot_min_bytes < 0:
            raise ValueError("hot_min_bytes must not be negative")
        if self.hot_min_bytes > self.hot_max_bytes:
            raise ValueError("hot_min_bytes must not exceed hot_max_bytes")
        if self.protected_prefix_chunks < 0:
            raise ValueError("protected_prefix_chunks must not be negative")
        if self.min_hot_age_ns < 0:
            raise ValueError("min_hot_age_ns must not be negative")
        if self.cold_copy_cleanup_age_ns < 0:
            raise ValueError("cold_copy_cleanup_age_ns must not be negative")


@dataclass(frozen=True)
class PreparedBlock:
    """经 ``prepare_block(s)`` 得到的待落盘块描述，供 ``ensure_prepared_block`` 原子写入与 SQLite 登记。

    ``digest`` 为原始未压缩数据的 SHA-256 十六进制摘要；``raw_size`` 为逻辑块原始字节数。
    ``payload`` 为磁盘或内联存储字节序列（可能经 zstd 压缩，由 ``compressed`` 标明）。
    ``inline_payload`` 非 ``None`` 时表示整块走 SQLite 内联存储，不写 tier 目录下的块文件。
    """

    digest: str
    raw_size: int
    payload: bytes
    compressed: bool
    inline_payload: bytes | None


@dataclass(frozen=True)
class BlockAccess:
    """单次读路径解析结果，供延迟刷新的访问统计与冷热存在位（presence）修正。

    ``tier``：0 表示内联块；1 热层（tier1）；2 冷层（tier2）。``stored_size`` 为本次读取所依据
    的存储字节数。``hot_present`` / ``cold_present`` / ``preferred_tier`` / ``last_promoted_ns``
    在自修复路径上可携带待写回 SQLite 的修正值。``request_demotion`` 为真时表示热层写入已累积
    到策略阈值，应在提交访问记录后尽快执行 ``demote_cold_blocks`` 等降级逻辑。
    """

    digest: str
    tier: int
    stored_size: int
    hot_present: bool | None = None
    cold_present: bool | None = None
    preferred_tier: int | None = None
    last_promoted_ns: int | None = None
    request_demotion: bool = False


class BlockStore:
    """内容寻址块在热层（tier1）与冷层（tier2）目录上的 IO、进程内读解码缓存与分层迁移。

    新块默认先 ``write_block_file`` 到热层，再以临时文件 + ``fsync`` + ``os.replace`` 保证
    块文件原子落盘；跨层 ``copy_block`` 同样经临时文件与目录 ``fsync``。读冷层且满足策略时
    可异步 **copy-up** 到热层并更新 ``preferred_tier``；热层超水位时 **降级** 将 payload 复制
    到冷层后删除热层文件。``read_cache_bytes`` 控制解码后明文 LRU 缓存上限（内联块亦可命中）。

    注意：``ensure_prepared_block`` / ``demote_cold_blocks`` / copy-up 回调内对 ``MetadataStore``
    的更新须在调用方约定的写事务或本类已开启的事务中与块文件操作一致提交，避免元数据与磁盘脱节。
    """

    def __init__(
        self,
        metadata: MetadataStore,
        tier1_blocks: Path,
        tier2_blocks: Path,
        *,
        policy: TieringPolicy,
        compression_level: int | None,
        compression_min_bytes: int,
        inline_max_bytes: int,
        read_cache_bytes: int = 128 * 1024 * 1024,
    ):
        """绑定元数据存储、热/冷层块根目录、``TieringPolicy`` 与压缩/内联/读缓存容量（字节）。"""
        self.metadata = metadata
        self.tier1_blocks = tier1_blocks
        self.tier2_blocks = tier2_blocks
        self.policy = policy
        self.compression_level = compression_level
        self.compression_min_bytes = compression_min_bytes
        self.inline_max_bytes = inline_max_bytes
        self.read_cache_bytes = max(0, read_cache_bytes)
        self._read_cache: OrderedDict[str, bytes] = OrderedDict()
        self._read_cache_size = 0
        self._read_cache_lock = threading.Lock()
        self._demotion_requested = False
        self._hot_bytes_added_since_check = 0
        self._prepare_executor: ThreadPoolExecutor | None = None
        self._prepare_executor_lock = threading.Lock()
        self._copy_up_inflight: set[str] = set()
        self._copy_up_lock = threading.Lock()

    def close(self) -> None:
        """刷净延迟块访问并关闭后台 ``prepare`` 线程池；应在卸载或进程退出前调用。"""
        logger.info("关闭块存储")
        self.metadata.commit()
        self.drain_pending_deletions()
        executor = self._prepare_executor
        if executor is not None:
            logger.debug("关闭块准备线程池")
            executor.shutdown(wait=True)

    def read_block_snapshot(self, row, expected_size: int) -> tuple[bytes, BlockAccess]:
        """返回截断/零填充至 ``expected_size`` 的数据与 ``BlockAccess``；不单独刷写延迟访问。

        内联块与 tier 块均可能命中进程内 **LRU 解码缓存**。从冷层读且 ``should_copy_up_from_cold``
        成立时会 **调度异步 copy-up**（不阻塞当前读）。缺块或解压失败抛 ``EIO``。
        """
        if row["storage"] == "inline":
            inline_payload = row["inline_payload"]
            if inline_payload is None:
                logger.error("内联块缺少 payload：hash={}", row["hash"])
                raise FuseOSError(errno.EIO)
            cached = self._cache_get(row["hash"])
            if cached is not None:
                logger.debug(
                    "读取内联块缓存命中：hash={}，raw_size={}",
                    row["hash"][:12],
                    len(cached),
                )
                access = BlockAccess(
                    digest=row["hash"], tier=0, stored_size=row["stored_size"]
                )
                return cached[:expected_size].ljust(expected_size, b"\x00"), access
            payload = bytes(inline_payload)
            data = self.decode_payload(row, payload)
            logger.debug(
                "读取内联块：hash={}，raw_size={}，stored_size={}",
                row["hash"][:12],
                row["raw_size"],
                row["stored_size"],
            )
            access = BlockAccess(
                digest=row["hash"], tier=0, stored_size=row["stored_size"]
            )
            self._cache_put(row["hash"], data)
            return data[:expected_size].ljust(expected_size, b"\x00"), access

        cached = self._cache_get(row["hash"])
        if cached is not None:
            tier = self._metadata_preferred_tier(row)
            logger.debug(
                "读取块缓存命中：hash={}，raw_size={}", row["hash"][:12], len(cached)
            )
            access = BlockAccess(
                digest=row["hash"],
                tier=tier,
                stored_size=row["stored_size"],
            )
            if self.should_copy_up_from_cold(row, tier):
                self.schedule_cold_copy_up(row["hash"], row["stored_size"])
            return cached[:expected_size].ljust(expected_size, b"\x00"), access

        path, tier, repair = self._read_path(row, repair_metadata=False)
        try:
            with timed(
                "block_io.read",
                bytes_key="block_io.read_bytes",
                size=row["stored_size"],
            ):
                payload = read_path_bytes(path)
        except PathMissing as exc:
            logger.error(
                "块文件读取时消失：hash={}，tier={}，path={}",
                row["hash"][:12],
                tier,
                path,
            )
            raise FuseOSError(errno.EIO) from exc
        except PathUnavailable as exc:
            logger.warning(
                "块文件临时不可用：hash={}，tier={}，path={}，error={}",
                row["hash"][:12],
                tier,
                path,
                exc,
            )
            raise FuseOSError(errno.EIO) from exc
        data = self.decode_payload(row, payload)
        self._cache_put(row["hash"], data)
        logger.debug(
            "读取块文件：hash={}，tier={}，stored_size={}，path={}",
            row["hash"][:12],
            tier,
            row["stored_size"],
            path,
        )

        hp_flag = repair.get("hot_present")
        cp_flag = repair.get("cold_present")
        access = BlockAccess(
            digest=row["hash"],
            tier=tier,
            stored_size=row["stored_size"],
            hot_present=hp_flag if type(hp_flag) is bool else None,
            cold_present=cp_flag if type(cp_flag) is bool else None,
            preferred_tier=repair.get("preferred_tier"),
        )
        if self.should_copy_up_from_cold(row, tier):
            self.schedule_cold_copy_up(row["hash"], row["stored_size"])
        return data[:expected_size].ljust(expected_size, b"\x00"), access

    def read_block_snapshots(
        self, requests: list[tuple[object, int]]
    ) -> list[tuple[bytes, BlockAccess]]:
        """批量 ``read_block_snapshot``；多请求时在共享线程池中并行读盘/解码。"""
        if len(requests) <= 1:
            return [
                self.read_block_snapshot(row, expected_size)
                for row, expected_size in requests
            ]
        executor = self._executor()
        futures = [
            executor.submit(self.read_block_snapshot, row, expected_size)
            for row, expected_size in requests
        ]
        return [future.result() for future in futures]

    def should_copy_up_from_cold(self, row, tier: int) -> bool:
        """当本次从冷层（tier2）读取、且块 ``stored_size`` 不超过 ``hot_max_bytes`` 时建议 copy-up。"""
        return (
            tier == 2
            and self.policy.hot_max_bytes > 0
            and row["stored_size"] <= self.policy.hot_max_bytes
        )

    def schedule_cold_copy_up(self, digest: str, stored_size: int) -> None:
        """对同一 ``digest`` 去重后在线程池中异步执行 ``_copy_up_cold_block``，避免重复 in-flight。"""
        with self._copy_up_lock:
            if digest in self._copy_up_inflight:
                return
            self._copy_up_inflight.add(digest)
        logger.debug(
            "调度冷层块后台提升：hash={}，stored_size={}", digest[:12], stored_size
        )
        future = self._executor().submit(self._copy_up_cold_block, digest, stored_size)
        future.add_done_callback(lambda done: self._finish_cold_copy_up(digest, done))

    def _copy_up_cold_block(self, digest: str, stored_size: int) -> None:
        """将冷层块 **copy-up** 到热层（原子复制），在写事务中标记冷热均存在、首选热层并尝试 ``demote_cold_blocks``。"""
        self.copy_block(digest, 2, 1)
        if not probe_path(self.block_path(digest, 1)).present:
            logger.debug("冷层块后台提升未产生热层副本：hash={}", digest[:12])
            return
        now = time_ns()
        with self.metadata.transaction():
            self.metadata.set_block_presence(
                digest,
                hot_present=True,
                cold_present=True,
                preferred_tier=1,
                last_promoted_ns=now,
            )
            self.demote_cold_blocks()
        logger.info(
            "冷层块后台提升到热层：hash={}，stored_size={}", digest[:12], stored_size
        )

    def _finish_cold_copy_up(self, digest: str, future: Future[None]) -> None:
        """释放 in-flight 标记并消费 ``Future``；失败时记录异常但不向 FUSE 上抛。"""
        with self._copy_up_lock:
            self._copy_up_inflight.discard(digest)
        try:
            future.result()
        except Exception:
            logger.exception("冷层块后台提升失败：hash={}", digest[:12])

    def record_block_access(self, access: BlockAccess, now: int) -> None:
        """合并延迟访问时间戳、按 ``BlockAccess`` 更新存在位/首选层，并立即 ``flush_deferred_accesses``。"""
        self.metadata.defer_block_accesses(iter((access.digest,)), now)
        self.record_block_presence(access, now)
        self.metadata.flush_deferred_accesses()

    def record_block_accesses(self, accesses: Iterable[BlockAccess], now: int) -> bool:
        """批量延迟访问；若队列已满或任一条目需元数据修正则返回真，提示调用方尽快 flush。"""
        pending = list(accesses)
        should_flush = self.metadata.defer_block_accesses(
            (access.digest for access in pending), now
        )
        return should_flush or any(
            self.access_requires_metadata_update(access) for access in pending
        )

    def access_requires_metadata_update(self, access: BlockAccess) -> bool:
        """判断该次访问是否携带需写回 SQLite 的存在位、首选层、提升时间或显式降级请求。"""
        return (
            access.hot_present is not None
            or access.cold_present is not None
            or access.preferred_tier is not None
            or access.last_promoted_ns is not None
            or access.request_demotion
        )

    def record_block_presence(self, access: BlockAccess, now: int) -> None:
        """若 ``BlockAccess`` 含存在位或首选层信息则 ``set_block_presence``；``request_demotion`` 时置内部降级标志。"""
        if (
            access.hot_present is not None
            or access.cold_present is not None
            or access.preferred_tier is not None
            or access.last_promoted_ns is not None
        ):
            self.metadata.set_block_presence(
                access.digest,
                hot_present=access.hot_present,
                cold_present=access.cold_present,
                preferred_tier=access.preferred_tier,
                last_promoted_ns=access.last_promoted_ns
                or (now if access.request_demotion else None),
            )
        if access.request_demotion:
            self._demotion_requested = True

    def ensure_prepared_block(self, block: PreparedBlock) -> None:
        """若块不存在：非内联则 **原子写入** 热层块文件并 ``note_hot_write``，再 ``insert_block`` 登记。

        已存在时直接返回。调用方须保证与引用计数/文件块元数据在同一元数据事务中一致提交。
        """
        digest = block.digest
        if self.metadata.block_exists(digest):
            logger.debug("块已存在，跳过写入：hash={}", digest[:12])
            return

        if block.inline_payload is None:
            self.write_block_file(digest, 1, block.payload)
            self.note_hot_write(len(block.payload))
        now = time_ns()
        self.metadata.insert_block(
            digest,
            compressed=block.compressed,
            raw_size=block.raw_size,
            stored_size=len(block.payload),
            now=now,
            inline_payload=block.inline_payload,
        )
        logger.debug(
            "记录新块：hash={}，raw_size={}，stored_size={}，compressed={}，storage={}",
            digest[:12],
            block.raw_size,
            len(block.payload),
            block.compressed,
            "inline" if block.inline_payload is not None else "tiered",
        )

    def prepare_blocks(
        self, chunks: Iterable[tuple[int, bytes]], compress: bool
    ) -> list[tuple[int, PreparedBlock]]:
        """对多块并行或同步计算摘要与编码，填充 **读缓存** 明文，并判定是否 **内联** 存储。"""
        pending = list(chunks)
        if len(pending) <= 1:
            logger.debug("同步准备块：count={}，compress={}", len(pending), compress)
            return [
                (chunk_index, self._prepare_block_sync(data, compress))
                for chunk_index, data in pending
            ]

        executor = self._executor()
        logger.debug("并行准备块：count={}，compress={}", len(pending), compress)
        digest_futures = [
            executor.submit(self._timed_digest_block, data)
            for _chunk_index, data in pending
        ]
        payload_futures: list[Future[tuple[bytes, bool]]] | None = None
        if compress:
            payload_futures = [
                executor.submit(self._timed_encode_block, data, compress)
                for _chunk_index, data in pending
            ]
        prepared: list[tuple[int, PreparedBlock]] = []
        for index, ((chunk_index, data), digest_future) in enumerate(
            zip(pending, digest_futures, strict=True)
        ):
            if payload_futures is None:
                payload, compressed = data, False
            else:
                payload, compressed = payload_futures[index].result()
            digest = digest_future.result()
            prepared.append(
                (chunk_index, self._prepared_block(digest, data, payload, compressed))
            )
        return prepared

    def prepare_block(self, data: bytes, compress: bool) -> PreparedBlock:
        """单块 ``prepare_blocks`` 的便捷封装。"""
        return self.prepare_blocks([(0, data)], compress)[0][1]

    def _prepare_block_sync(self, data: bytes, compress: bool) -> PreparedBlock:
        """在当前线程完成编码、摘要、缓存与内联判定（无并行开销）。"""
        payload, compressed = self._timed_encode_block(data, compress)
        digest = self._timed_digest_block(data)
        return self._prepared_block(digest, data, payload, compressed)

    def _prepared_block(
        self, digest: str, data: bytes, payload: bytes, compressed: bool
    ) -> PreparedBlock:
        """用已计算好的摘要与 payload 构造 ``PreparedBlock``，并填充明文读缓存。"""
        self._cache_put(digest, data)
        inline_payload = payload if self.should_inline(payload) else None
        return PreparedBlock(
            digest=digest,
            raw_size=len(data),
            payload=payload,
            compressed=compressed,
            inline_payload=inline_payload,
        )

    def _executor(self) -> ThreadPoolExecutor:
        """懒创建用于 ``prepare_blocks`` / 并行读 / copy-up 的共享 ``ThreadPoolExecutor``。"""
        executor = self._prepare_executor
        if executor is not None:
            return executor
        with self._prepare_executor_lock:
            if self._prepare_executor is None:
                logger.debug("创建块准备线程池")
                self._prepare_executor = ThreadPoolExecutor(
                    max_workers=max(2, min((os.cpu_count() or 2), 8)),
                    thread_name_prefix="ztierfs-block-prepare",
                )
            return self._prepare_executor

    @staticmethod
    def digest_block(data: bytes) -> str:
        """返回 ``data`` 的 SHA-256 十六进制摘要（与内容寻址主键一致）。"""
        return sha256(data).hexdigest()

    def _timed_digest_block(self, data: bytes) -> str:
        """带性能计时的 ``digest_block``，供并行 ``prepare`` 路径使用。"""
        with timed(
            "block_prepare.hash", bytes_key="block_prepare.hash_bytes", size=len(data)
        ):
            return self.digest_block(data)

    def _timed_encode_block(self, data: bytes, compress: bool) -> tuple[bytes, bool]:
        """带性能计时的 ``encode_block``。"""
        with timed(
            "block_prepare.encode", bytes_key="block_prepare.raw_bytes", size=len(data)
        ):
            return self.encode_block(data, compress)

    def decrement_block(self, digest: str) -> None:
        """引用计数递减；降至零时删除 SQLite 块行并把物理 payload 登记到提交后 GC 队列。"""
        row = self.metadata.block_refcount_and_presence(digest)
        if row is None:
            logger.warning("递减块引用时未找到块记录：hash={}", digest[:12])
            return
        self.metadata.apply_block_refcount_deltas({digest: -1})
        logger.debug(
            "递减块引用：hash={}，refcount={}->{}",
            digest[:12],
            row["refcount"],
            row["refcount"] - 1,
        )

    def encode_block(self, data: bytes, compress: bool) -> tuple[bytes, bool]:
        """按策略尝试 zstd；过短、禁用压缩或压缩无体积收益时返回原始字节与 ``compressed=False``。"""
        if not compress:
            logger.debug("跳过压缩块：raw_size={}，原因=路径策略", len(data))
            return data, False
        if len(data) < self.compression_min_bytes:
            logger.debug("跳过压缩块：raw_size={}，原因=小于压缩阈值", len(data))
            return data, False
        packed = zstd.compress(data, level=self.compression_level)
        if len(packed) >= len(data):
            logger.debug(
                "跳过压缩块：raw_size={}，packed_size={}，原因=压缩无收益",
                len(data),
                len(packed),
            )
            return data, False
        logger.debug("压缩块成功：raw_size={}，packed_size={}", len(data), len(packed))
        return packed, True

    def decode_payload(self, row, payload: bytes) -> bytes:
        """按 ``row["compressed"]`` 解压或直通，并校验解码后长度等于 ``row["raw_size"]``。"""
        try:
            data = zstd.decompress(payload) if row["compressed"] else payload
        except zstd.ZstdError as exc:
            logger.error(
                "块解压失败：hash={}，stored_size={}", row["hash"][:12], len(payload)
            )
            raise FuseOSError(errno.EIO) from exc
        if len(data) != row["raw_size"]:
            logger.error(
                "块解码大小不匹配：hash={}，expected={}，actual={}",
                row["hash"][:12],
                row["raw_size"],
                len(data),
            )
            raise FuseOSError(errno.EIO)
        return data

    def should_inline(self, payload: bytes) -> bool:
        """当 ``inline_max_bytes > 0`` 且 ``payload`` 不超过该阈值时整块可放入 SQLite 内联列。"""
        return self.inline_max_bytes > 0 and len(payload) <= self.inline_max_bytes

    def _cache_get(self, digest: str) -> bytes | None:
        """LRU 命中则将键移到队尾并返回 **解码后明文**；缓存关闭或未命中返回 ``None``。"""
        if self.read_cache_bytes <= 0:
            return None
        with self._read_cache_lock:
            data = self._read_cache.get(digest)
            if data is None:
                return None
            self._read_cache.move_to_end(digest)
            return data

    def _cache_put(self, digest: str, data: bytes) -> None:
        """插入或更新明文缓存并在总字节数超 ``read_cache_bytes`` 时从队首驱逐（单块过大则不缓存）。"""
        if self.read_cache_bytes <= 0 or len(data) > self.read_cache_bytes:
            return
        with self._read_cache_lock:
            previous = self._read_cache.pop(digest, None)
            if previous is not None:
                self._read_cache_size -= len(previous)
            self._read_cache[digest] = data
            self._read_cache_size += len(data)
            while self._read_cache_size > self.read_cache_bytes and self._read_cache:
                _old_digest, old_data = self._read_cache.popitem(last=False)
                self._read_cache_size -= len(old_data)

    def block_path(self, digest: str, tier: int) -> Path:
        """返回给定 ``digest`` 在 tier1（1）或 tier2（2）下的内容寻址块文件路径。"""
        return block_file_path(self.tier1_blocks, self.tier2_blocks, digest, tier)

    def take_demotion_request(self) -> bool:
        """原子地读取并清除内部“需要 **热层降级**”标志（由 ``note_hot_write`` 或访问记录置位）。"""
        requested = self._demotion_requested
        self._demotion_requested = False
        return requested

    def note_hot_write(self, stored_size: int) -> None:
        """累加热层新写字节；自上次检查以来累计达到 ``hot_max_bytes`` 时置 **降级请求** 并重置累计。"""
        if self.policy.hot_max_bytes <= 0:
            return
        self._hot_bytes_added_since_check += stored_size
        if self._hot_bytes_added_since_check >= self.policy.hot_max_bytes:
            self._demotion_requested = True
            self._hot_bytes_added_since_check = 0

    def write_block_file(self, digest: str, tier: int, payload: bytes) -> None:
        """向 ``tier`` 目录 **原子写入** 块文件：临时文件 → ``fsync`` → ``os.replace`` → 父目录 ``fsync``。

        目标路径已存在则跳过（幂等）。``payload`` 为磁盘存储字节序列（可为压缩形式）。
        """
        path = self.block_path(digest, tier)
        if probe_path(path).present:
            logger.debug("块文件已存在，跳过写入：hash={}，tier={}", digest[:12], tier)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with timed(
            "block_io.write", bytes_key="block_io.write_bytes", size=len(payload)
        ):
            with open(tmp, "wb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
        os.replace(tmp, path)
        self.fsync_dir(path.parent)
        logger.debug(
            "写入块文件完成：hash={}，tier={}，bytes={}",
            digest[:12],
            tier,
            len(payload),
        )

    def delete_block_file(self, digest: str) -> None:
        """依次尝试删除 tier1 与 tier2 上同名块文件；路径临时不可用时记警告并跳过该层。"""
        for candidate_tier in (1, 2):
            try:
                if unlink_path(self.block_path(digest, candidate_tier)):
                    logger.debug(
                        "删除块文件：hash={}，tier={}", digest[:12], candidate_tier
                    )
            except PathUnavailable:
                logger.warning(
                    "删除块文件跳过：块路径临时不可用，hash={}，tier={}",
                    digest[:12],
                    candidate_tier,
                )

    def drain_pending_deletions(self, *, limit: int = 256) -> int:
        """Delete queued physical payloads after their metadata transaction has committed."""
        removed = 0
        while True:
            with self.metadata.read_transaction():
                rows = self.metadata.pending_deletions(limit)
            if not rows:
                return removed
            for row in rows:
                now = time_ns()
                try:
                    if row["kind"] == "block_file":
                        deleted_or_missing = not unlink_path(
                            self.block_path(row["digest"], row["tier"])
                        )
                except PathUnavailable:
                    logger.warning(
                        "待 GC 块文件暂时不可用：id={}，hash={}，tier={}",
                        row["id"],
                        str(row["digest"])[:12],
                        row["tier"],
                    )
                    with self.metadata.transaction():
                        self.metadata.defer_pending_deletion(row["id"], now)
                    continue
                except OSError:
                    logger.exception("待 GC payload 删除失败：id={}", row["id"])
                    with self.metadata.transaction():
                        self.metadata.defer_pending_deletion(row["id"], now)
                    continue
                with self.metadata.transaction():
                    self.metadata.remove_pending_deletion(row["id"])
                removed += 1
                if deleted_or_missing:
                    logger.debug(
                        "待 GC payload 已清理：id={}，kind={}", row["id"], row["kind"]
                    )

    def copy_block(self, digest: str, source_tier: int, target_tier: int) -> None:
        """跨层 **原子复制**：``shutil.copyfile`` 至临时文件、读 ``fsync``、``replace`` 落位、目录 ``fsync``。

        源缺失、任一侧路径不可用或目标已存在时安全返回；用于 **copy-up** 与降级前冷层落盘。
        """
        source = self.block_path(digest, source_tier)
        target = self.block_path(digest, target_tier)
        source_probe = probe_path(source)
        if source_probe.unavailable:
            logger.warning(
                "复制块跳过：源块临时不可用，hash={}，source_tier={}，error={}",
                digest[:12],
                source_tier,
                source_probe.error,
            )
            return
        if source_probe.missing:
            logger.warning(
                "复制块失败：源块不存在，hash={}，source_tier={}",
                digest[:12],
                source_tier,
            )
            return
        target_probe = probe_path(target)
        if target_probe.unavailable:
            logger.warning(
                "复制块跳过：目标层临时不可用，hash={}，target_tier={}，error={}",
                digest[:12],
                target_tier,
                target_probe.error,
            )
            return
        if target_probe.present:
            logger.debug(
                "复制块跳过：目标已存在，hash={}，target_tier={}",
                digest[:12],
                target_tier,
            )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        shutil.copyfile(source, tmp)
        with open(tmp, "rb") as file:
            os.fsync(file.fileno())
        os.replace(tmp, target)
        self.fsync_dir(target.parent)
        logger.debug(
            "复制块完成：hash={}，{} -> {}", digest[:12], source_tier, target_tier
        )

    def demote_cold_blocks(self) -> None:
        """当热层已存 payload 总字节超过 ``hot_max_bytes`` 时，循环选取候选块 **降级** 到冷层。

        先 ``flush_deferred_accesses``；若冷层尚无副本则 ``copy_block(1→2)``，再删热层文件并更新
        SQLite 存在位与 ``preferred_tier=2``。受 ``protected_prefix_chunks`` 与 ``min_hot_age_ns`` 约束；
        冷/热路径不可用时中止本轮。
        """
        if self.policy.hot_max_bytes <= 0:
            return
        self.metadata.flush_deferred_accesses()
        total = self.metadata.hot_tier_stored_size()
        if total <= self.policy.hot_max_bytes:
            return
        target = self.policy.hot_min_bytes
        logger.info("开始热层降级：hot_bytes={}，target={}", total, target)
        while total > target:
            row = self.metadata.demotion_candidate(
                protected_prefix_chunks=self.policy.protected_prefix_chunks,
                max_atime_ns=time_ns() - self.policy.min_hot_age_ns,
            )
            if row is None:
                logger.info("热层降级停止：没有可降级候选，hot_bytes={}", total)
                return
            now = time_ns()
            cold_probe = probe_path(self.block_path(row["hash"], 2))
            if cold_probe.unavailable:
                logger.warning(
                    "热层降级暂停：冷层临时不可用，hash={}，error={}",
                    row["hash"][:12],
                    cold_probe.error,
                )
                return
            if not row["cold_present"] or cold_probe.missing:
                self.copy_block(row["hash"], 1, 2)
            try:
                unlink_path(self.block_path(row["hash"], 1))
            except PathUnavailable:
                logger.warning(
                    "热层降级停止：热层块临时不可用，hash={}", row["hash"][:12]
                )
                return
            self.metadata.set_block_presence(
                row["hash"],
                hot_present=False,
                cold_present=True,
                preferred_tier=2,
                last_demoted_ns=now,
                cold_verified_ns=now,
            )
            total -= row["stored_size"]
            logger.debug(
                "降级块完成：hash={}，stored_size={}，remaining_hot_bytes={}",
                row["hash"][:12],
                row["stored_size"],
                total,
            )

    def cleanup_promoted_cold_copies(self) -> int:
        """删除 **copy-up** 后超过 ``cold_copy_cleanup_age_ns`` 仍留在冷层的冗余副本，并清除 ``cold_present``。

        ``cold_copy_cleanup_age_ns <= 0`` 时禁用。返回成功删除的块数。
        """
        if self.policy.cold_copy_cleanup_age_ns <= 0:
            return 0
        cutoff = time_ns() - self.policy.cold_copy_cleanup_age_ns
        removed = 0
        for row in self.metadata.promoted_cold_copy_candidates(cutoff):
            path = self.block_path(row["hash"], 2)
            probe = probe_path(path)
            if probe.unavailable:
                logger.warning(
                    "跳过清理冷层副本：冷层临时不可用，hash={}，error={}",
                    row["hash"][:12],
                    probe.error,
                )
                continue
            try:
                unlink_path(path)
            except PathUnavailable:
                logger.warning(
                    "跳过清理冷层副本：冷层临时不可用，hash={}", row["hash"][:12]
                )
                continue
            self.metadata.set_block_presence(row["hash"], cold_present=False)
            removed += 1
            logger.debug("清理提升后遗留冷层副本：hash={}", row["hash"][:12])
        return removed

    def _metadata_preferred_tier(self, row) -> int:
        """仅按 SQLite preferred/presence 选择预计读取层；缓存命中时避免磁盘探测。"""
        if row["preferred_tier"] == 2 and row["cold_present"]:
            return 2
        if row["preferred_tier"] == 1 and row["hot_present"]:
            return 1
        if row["hot_present"]:
            return 1
        return 2

    def _read_path(
        self, row, *, repair_metadata: bool = True
    ) -> tuple[Path, int, dict[str, bool | int]]:
        """优先相信 SQLite 首选层；读失败后再探测 fallback 并返回可延迟提交的 presence 修正。"""
        digest = row["hash"]
        preferred_tier = self._metadata_preferred_tier(row)
        preferred_path = self.block_path(digest, preferred_tier)
        alternate_tier = 2 if preferred_tier == 1 else 1
        alternate_path = self.block_path(digest, alternate_tier)
        repair: dict[str, bool | int] = {}

        try:
            preferred_probe = probe_path(preferred_path)
        except OSError as exc:
            logger.warning(
                "块首选层探测失败：hash={}，tier={}，path={}，error={}",
                digest[:12],
                preferred_tier,
                preferred_path,
                exc,
            )
            preferred_probe = None

        if preferred_probe is not None and preferred_probe.present:
            return preferred_path, preferred_tier, repair

        if preferred_probe is not None and preferred_probe.unavailable:
            logger.warning(
                "块首选层临时不可用：hash={}，tier={}，path={}，error={}",
                digest[:12],
                preferred_tier,
                preferred_path,
                preferred_probe.error,
            )
            alternate_probe = self._probe_declared_alternate(
                row, alternate_tier, alternate_path
            )
            if alternate_probe is not None and alternate_probe.present:
                logger.warning(
                    "块首选层临时不可用，临时改读另一层且不修复元数据：hash={}，tier={}",
                    digest[:12],
                    alternate_tier,
                )
                return alternate_path, alternate_tier, repair
            raise FuseOSError(errno.EIO)

        missing_tiers = {preferred_tier}
        alternate_probe = self._probe_declared_alternate(
            row, alternate_tier, alternate_path
        )
        if alternate_probe is not None:
            if alternate_probe.present:
                if preferred_tier == 1:
                    repair = {
                        "hot_present": False,
                        "preferred_tier": alternate_tier,
                    }
                else:
                    repair = {
                        "cold_present": False,
                        "preferred_tier": alternate_tier,
                    }
                if repair_metadata:
                    self.metadata.set_block_presence(
                        digest,
                        hot_present=False if preferred_tier == 1 else None,
                        cold_present=False if preferred_tier == 2 else None,
                        preferred_tier=alternate_tier,
                    )
                logger.warning(
                    "块首选层缺失，改用另一层：hash={}，missing_tier={}，read_tier={}",
                    digest[:12],
                    preferred_tier,
                    alternate_tier,
                )
                return alternate_path, alternate_tier, repair
            if alternate_probe.unavailable:
                logger.warning(
                    "块备用层临时不可用：hash={}，tier={}，path={}，error={}",
                    digest[:12],
                    alternate_tier,
                    alternate_path,
                    alternate_probe.error,
                )
                raise FuseOSError(errno.EIO)
            missing_tiers.add(alternate_tier)

        full_probe = self._probe_all_tiers(digest)
        hot_probe = full_probe[1]
        cold_probe = full_probe[2]
        if cold_probe.unavailable and not hot_probe.present:
            logger.warning(
                "冷层块临时不可用：hash={}，path={}，error={}",
                digest[:12],
                self.block_path(digest, 2),
                cold_probe.error,
            )
            raise FuseOSError(errno.EIO)

        hot_exists = hot_probe.present
        cold_exists = cold_probe.present
        if hot_exists or cold_exists:
            repaired_preferred = 1 if hot_exists else 2
            repair = {
                "hot_present": hot_exists,
                "preferred_tier": repaired_preferred,
            }
            if not cold_probe.unavailable:
                repair["cold_present"] = cold_exists
            logger.warning(
                "块元数据位置与磁盘不一致，准备修复：hash={}，hot={}，cold={}",
                digest[:12],
                hot_exists,
                cold_exists,
            )
            if repair_metadata:
                self.metadata.set_block_presence(
                    digest,
                    hot_present=hot_exists,
                    cold_present=None if cold_probe.unavailable else cold_exists,
                    preferred_tier=repaired_preferred,
                )
            return (
                self.block_path(digest, repaired_preferred),
                repaired_preferred,
                repair,
            )

        hot_present = False if 1 in missing_tiers else None
        cold_present = False if 2 in missing_tiers else None
        if hot_present is not None:
            repair["hot_present"] = hot_present
        if cold_present is not None:
            repair["cold_present"] = cold_present
        if repair_metadata:
            self.metadata.set_block_presence(
                digest,
                hot_present=hot_present,
                cold_present=cold_present,
            )
        logger.error("块元数据引用的 payload 缺失：hash={}", digest[:12])
        raise FuseOSError(errno.EIO)

    def _probe_declared_alternate(self, row, tier: int, path: Path):
        """仅在 metadata 声明备用层存在时探测，避免正常读固定 probe 双层。"""
        if tier == 1 and not row["hot_present"]:
            return None
        if tier == 2 and not row["cold_present"]:
            return None
        return probe_path(path)

    def _probe_all_tiers(self, digest: str):
        """首选层和声明备用层都不可读时，完整探测两层用于 presence 修正。"""
        return {
            1: probe_path(self.block_path(digest, 1)),
            2: probe_path(self.block_path(digest, 2)),
        }

    def fsync_dir(self, path: Path) -> None:
        """在支持 ``O_DIRECTORY`` 的平台上对目录 fd 执行 ``fsync``，巩固 **rename 落盘** 的目录项持久化。"""
        if not hasattr(os, "O_DIRECTORY"):
            return
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
