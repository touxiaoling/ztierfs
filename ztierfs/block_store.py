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
from .tier_access import PathMissing, PathUnavailable, probe_path, read_path_bytes, unlink_path


@dataclass(frozen=True)
class TieringPolicy:
    hot_max_bytes: int
    hot_min_bytes: int
    protected_prefix_chunks: int = 4
    min_hot_age_ns: int = 24 * 60 * 60 * 1_000_000_000
    cold_copy_cleanup_age_ns: int = 0

    def __post_init__(self) -> None:
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
    digest: str
    raw_size: int
    payload: bytes
    compressed: bool
    inline_payload: bytes | None


@dataclass(frozen=True)
class BlockAccess:
    digest: str
    tier: int
    stored_size: int
    hot_present: bool | None = None
    cold_present: bool | None = None
    preferred_tier: int | None = None
    last_promoted_ns: int | None = None
    request_demotion: bool = False


class BlockStore:
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
        logger.info("关闭块存储")
        if self.metadata.has_deferred_accesses():
            with self.metadata.transaction():
                pass
        executor = self._prepare_executor
        if executor is not None:
            logger.debug("关闭块准备线程池")
            executor.shutdown(wait=True)

    def read_block(self, row, expected_size: int) -> bytes:
        data, access = self.read_block_snapshot(row, expected_size)
        self.record_block_access(access, time_ns())
        return data

    def read_block_snapshot(self, row, expected_size: int) -> tuple[bytes, BlockAccess]:
        if row["storage"] == "inline":
            inline_payload = row["inline_payload"]
            if inline_payload is None and row["inline_payload_store"] != "sqlite":
                inline_payload = self.metadata.payload_store.get(row["inline_payload_key"])
            if inline_payload is None:
                logger.error("内联块缺少 payload：hash={}", row["hash"])
                raise FuseOSError(errno.EIO)
            cached = self._cache_get(row["hash"])
            if cached is not None:
                logger.debug("读取内联块缓存命中：hash={}，raw_size={}", row["hash"][:12], len(cached))
                access = BlockAccess(
                    digest=row["hash"], tier=0, stored_size=row["stored_size"]
                )
                return cached[:expected_size].ljust(expected_size, b"\x00"), access
            payload = bytes(inline_payload)
            data = self.decode_payload(row, payload)
            logger.debug("读取内联块：hash={}，raw_size={}，stored_size={}", row["hash"][:12], row["raw_size"], row["stored_size"])
            access = BlockAccess(
                digest=row["hash"], tier=0, stored_size=row["stored_size"]
            )
            self._cache_put(row["hash"], data)
            return data[:expected_size].ljust(expected_size, b"\x00"), access

        path, tier, repair = self._read_path(row, repair_metadata=False)
        cached = self._cache_get(row["hash"])
        if cached is not None:
            logger.debug("读取块缓存命中：hash={}，raw_size={}", row["hash"][:12], len(cached))
            access = BlockAccess(
                digest=row["hash"],
                tier=tier,
                stored_size=row["stored_size"],
                hot_present=repair.get("hot_present")
                if isinstance(repair.get("hot_present"), bool)
                else None,
                cold_present=repair.get("cold_present")
                if isinstance(repair.get("cold_present"), bool)
                else None,
                preferred_tier=repair.get("preferred_tier"),
            )
            if self.should_copy_up_from_cold(row, tier):
                self.schedule_cold_copy_up(row["hash"], row["stored_size"])
            return cached[:expected_size].ljust(expected_size, b"\x00"), access

        try:
            with timed("block_io.read", bytes_key="block_io.read_bytes", size=row["stored_size"]):
                payload = read_path_bytes(path)
        except PathMissing as exc:
            logger.error("块文件读取时消失：hash={}，tier={}，path={}", row["hash"][:12], tier, path)
            raise FuseOSError(errno.EIO) from exc
        except PathUnavailable as exc:
            logger.warning("块文件临时不可用：hash={}，tier={}，path={}，error={}", row["hash"][:12], tier, path, exc)
            raise FuseOSError(errno.EIO) from exc
        data = self.decode_payload(row, payload)
        self._cache_put(row["hash"], data)
        logger.debug("读取块文件：hash={}，tier={}，stored_size={}，path={}", row["hash"][:12], tier, row["stored_size"], path)

        hot_present = repair.get("hot_present")
        cold_present = repair.get("cold_present")
        access = BlockAccess(
            digest=row["hash"],
            tier=tier,
            stored_size=row["stored_size"],
            hot_present=hot_present if isinstance(hot_present, bool) else None,
            cold_present=cold_present if isinstance(cold_present, bool) else None,
            preferred_tier=repair.get("preferred_tier"),
        )
        if self.should_copy_up_from_cold(row, tier):
            self.schedule_cold_copy_up(row["hash"], row["stored_size"])
        return data[:expected_size].ljust(expected_size, b"\x00"), access

    def read_block_snapshots(
        self, requests: list[tuple[object, int]]
    ) -> list[tuple[bytes, BlockAccess]]:
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
        return (
            tier == 2
            and self.policy.hot_max_bytes > 0
            and row["stored_size"] <= self.policy.hot_max_bytes
        )

    def schedule_cold_copy_up(self, digest: str, stored_size: int) -> None:
        with self._copy_up_lock:
            if digest in self._copy_up_inflight:
                return
            self._copy_up_inflight.add(digest)
        logger.debug("调度冷层块后台提升：hash={}，stored_size={}", digest[:12], stored_size)
        future = self._executor().submit(self._copy_up_cold_block, digest, stored_size)
        future.add_done_callback(lambda done: self._finish_cold_copy_up(digest, done))

    def _copy_up_cold_block(self, digest: str, stored_size: int) -> None:
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
        logger.info("冷层块后台提升到热层：hash={}，stored_size={}", digest[:12], stored_size)

    def _finish_cold_copy_up(self, digest: str, future: Future[None]) -> None:
        with self._copy_up_lock:
            self._copy_up_inflight.discard(digest)
        try:
            future.result()
        except Exception:
            logger.exception("冷层块后台提升失败：hash={}", digest[:12])

    def record_block_access(self, access: BlockAccess, now: int) -> None:
        self.metadata.defer_block_accesses(iter((access.digest,)), now)
        self.record_block_presence(access, now)
        self.metadata.flush_deferred_accesses()

    def record_block_accesses(self, accesses: Iterable[BlockAccess], now: int) -> bool:
        pending = list(accesses)
        should_flush = self.metadata.defer_block_accesses(
            (access.digest for access in pending), now
        )
        return should_flush or any(
            self.access_requires_metadata_update(access) for access in pending
        )

    def access_requires_metadata_update(self, access: BlockAccess) -> bool:
        return (
            access.hot_present is not None
            or access.cold_present is not None
            or access.preferred_tier is not None
            or access.last_promoted_ns is not None
            or access.request_demotion
        )

    def record_block_presence(self, access: BlockAccess, now: int) -> None:
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
                last_promoted_ns=access.last_promoted_ns or (
                    now if access.request_demotion else None
                ),
            )
        if access.request_demotion:
            self._demotion_requested = True

    def ensure_block(self, digest: str, data: bytes, compress: bool) -> None:
        block = self.prepare_block(data, compress)
        if block.digest != digest:
            raise ValueError("digest does not match block data")
        self.ensure_prepared_block(block)

    def ensure_prepared_block(self, block: PreparedBlock) -> None:
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
        pending = list(chunks)
        if len(pending) <= 1 or not compress:
            logger.debug("同步准备块：count={}，compress={}", len(pending), compress)
            return [
                (chunk_index, self._prepare_block_sync(data, compress))
                for chunk_index, data in pending
            ]

        executor = self._executor()
        logger.debug("并行准备块：count={}，compress={}", len(pending), compress)
        digest_futures: list[Future[str]] = []
        payload_futures: list[Future[tuple[bytes, bool]]] = []
        for _chunk_index, data in pending:
            digest_futures.append(executor.submit(self._timed_digest_block, data))
            payload_futures.append(
                executor.submit(self._timed_encode_block, data, compress)
            )
        prepared: list[tuple[int, PreparedBlock]] = []
        for (chunk_index, data), digest_future, payload_future in zip(
            pending, digest_futures, payload_futures, strict=True
        ):
            payload, compressed = payload_future.result()
            digest = digest_future.result()
            self._cache_put(digest, data)
            inline_payload = payload if self.should_inline(payload) else None
            prepared.append(
                (
                    chunk_index,
                    PreparedBlock(
                        digest=digest,
                        raw_size=len(data),
                        payload=payload,
                        compressed=compressed,
                        inline_payload=inline_payload,
                    ),
                )
            )
        return prepared

    def prepare_block(self, data: bytes, compress: bool) -> PreparedBlock:
        return self.prepare_blocks([(0, data)], compress)[0][1]

    def _prepare_block_sync(self, data: bytes, compress: bool) -> PreparedBlock:
        payload, compressed = self._timed_encode_block(data, compress)
        digest = self._timed_digest_block(data)
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
        return sha256(data).hexdigest()

    def _timed_digest_block(self, data: bytes) -> str:
        with timed("block_prepare.hash", bytes_key="block_prepare.hash_bytes", size=len(data)):
            return self.digest_block(data)

    def _timed_encode_block(self, data: bytes, compress: bool) -> tuple[bytes, bool]:
        with timed("block_prepare.encode", bytes_key="block_prepare.raw_bytes", size=len(data)):
            return self.encode_block(data, compress)

    def decrement_block(self, digest: str) -> None:
        row = self.metadata.block_refcount_and_presence(digest)
        if row is None:
            logger.warning("递减块引用时未找到块记录：hash={}", digest[:12])
            return
        if row["refcount"] > 1:
            self.metadata.decrement_block_refcount(digest)
            logger.debug("递减块引用计数：hash={}，refcount={}->{}", digest[:12], row["refcount"], row["refcount"] - 1)
            return
        self.metadata.delete_block(digest)
        if row["storage"] == "tiered":
            self.delete_block_file(digest)
        logger.debug("删除最后一个块引用：hash={}，storage={}", digest[:12], row["storage"])

    def encode_block(self, data: bytes, compress: bool) -> tuple[bytes, bool]:
        if not compress:
            logger.debug("跳过压缩块：raw_size={}，原因=路径策略", len(data))
            return data, False
        if len(data) < self.compression_min_bytes:
            logger.debug("跳过压缩块：raw_size={}，原因=小于压缩阈值", len(data))
            return data, False
        packed = zstd.compress(data, level=self.compression_level)
        if len(packed) >= len(data):
            logger.debug("跳过压缩块：raw_size={}，packed_size={}，原因=压缩无收益", len(data), len(packed))
            return data, False
        logger.debug("压缩块成功：raw_size={}，packed_size={}", len(data), len(packed))
        return packed, True

    def decode_payload(self, row, payload: bytes) -> bytes:
        try:
            data = zstd.decompress(payload) if row["compressed"] else payload
        except zstd.ZstdError as exc:
            logger.error("块解压失败：hash={}，stored_size={}", row["hash"][:12], len(payload))
            raise FuseOSError(errno.EIO) from exc
        if len(data) != row["raw_size"]:
            logger.error("块解码大小不匹配：hash={}，expected={}，actual={}", row["hash"][:12], row["raw_size"], len(data))
            raise FuseOSError(errno.EIO)
        return data

    def should_inline(self, payload: bytes) -> bool:
        return self.inline_max_bytes > 0 and len(payload) <= self.inline_max_bytes

    def _cache_get(self, digest: str) -> bytes | None:
        if self.read_cache_bytes <= 0:
            return None
        with self._read_cache_lock:
            data = self._read_cache.get(digest)
            if data is None:
                return None
            self._read_cache.move_to_end(digest)
            return data

    def _cache_put(self, digest: str, data: bytes) -> None:
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
        return block_file_path(self.tier1_blocks, self.tier2_blocks, digest, tier)

    def take_demotion_request(self) -> bool:
        requested = self._demotion_requested
        self._demotion_requested = False
        return requested

    def note_hot_write(self, stored_size: int) -> None:
        if self.policy.hot_max_bytes <= 0:
            return
        self._hot_bytes_added_since_check += stored_size
        if self._hot_bytes_added_since_check >= self.policy.hot_max_bytes:
            self._demotion_requested = True
            self._hot_bytes_added_since_check = 0

    def write_block_file(self, digest: str, tier: int, payload: bytes) -> None:
        path = self.block_path(digest, tier)
        if probe_path(path).present:
            logger.debug("块文件已存在，跳过写入：hash={}，tier={}", digest[:12], tier)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with timed("block_io.write", bytes_key="block_io.write_bytes", size=len(payload)):
            with open(tmp, "wb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
        os.replace(tmp, path)
        self.fsync_dir(path.parent)
        logger.debug("写入块文件完成：hash={}，tier={}，bytes={}", digest[:12], tier, len(payload))

    def delete_block_file(self, digest: str) -> None:
        for candidate_tier in (1, 2):
            try:
                if unlink_path(self.block_path(digest, candidate_tier)):
                    logger.debug("删除块文件：hash={}，tier={}", digest[:12], candidate_tier)
            except PathUnavailable:
                logger.warning("删除块文件跳过：块路径临时不可用，hash={}，tier={}", digest[:12], candidate_tier)

    def copy_block(self, digest: str, source_tier: int, target_tier: int) -> None:
        source = self.block_path(digest, source_tier)
        target = self.block_path(digest, target_tier)
        source_probe = probe_path(source)
        if source_probe.unavailable:
            logger.warning("复制块跳过：源块临时不可用，hash={}，source_tier={}，error={}", digest[:12], source_tier, source_probe.error)
            return
        if source_probe.missing:
            logger.warning("复制块失败：源块不存在，hash={}，source_tier={}", digest[:12], source_tier)
            return
        target_probe = probe_path(target)
        if target_probe.unavailable:
            logger.warning("复制块跳过：目标层临时不可用，hash={}，target_tier={}，error={}", digest[:12], target_tier, target_probe.error)
            return
        if target_probe.present:
            logger.debug("复制块跳过：目标已存在，hash={}，target_tier={}", digest[:12], target_tier)
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
        logger.debug("复制块完成：hash={}，{} -> {}", digest[:12], source_tier, target_tier)

    def demote_cold_blocks(self) -> None:
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
                logger.warning("热层降级暂停：冷层临时不可用，hash={}，error={}", row["hash"][:12], cold_probe.error)
                return
            if not row["cold_present"] or cold_probe.missing:
                self.copy_block(row["hash"], 1, 2)
            try:
                unlink_path(self.block_path(row["hash"], 1))
            except PathUnavailable:
                logger.warning("热层降级停止：热层块临时不可用，hash={}", row["hash"][:12])
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
            logger.debug("降级块完成：hash={}，stored_size={}，remaining_hot_bytes={}", row["hash"][:12], row["stored_size"], total)

    def cleanup_promoted_cold_copies(self) -> int:
        if self.policy.cold_copy_cleanup_age_ns <= 0:
            return 0
        cutoff = time_ns() - self.policy.cold_copy_cleanup_age_ns
        removed = 0
        for row in self.metadata.promoted_cold_copy_candidates(cutoff):
            path = self.block_path(row["hash"], 2)
            probe = probe_path(path)
            if probe.unavailable:
                logger.warning("跳过清理冷层副本：冷层临时不可用，hash={}，error={}", row["hash"][:12], probe.error)
                continue
            try:
                unlink_path(path)
            except PathUnavailable:
                logger.warning("跳过清理冷层副本：冷层临时不可用，hash={}", row["hash"][:12])
                continue
            self.metadata.set_block_presence(row["hash"], cold_present=False)
            removed += 1
            logger.debug("清理提升后遗留冷层副本：hash={}", row["hash"][:12])
        return removed

    def _read_path(
        self, row, *, repair_metadata: bool = True
    ) -> tuple[Path, int, dict[str, bool | int]]:
        digest = row["hash"]
        hot_path = self.block_path(digest, 1)
        cold_path = self.block_path(digest, 2)
        hot_probe = probe_path(hot_path) if row["hot_present"] else None
        cold_probe = probe_path(cold_path) if row["cold_present"] else None
        hot_exists = bool(row["hot_present"]) and hot_probe is not None and hot_probe.present
        cold_unavailable = cold_probe is not None and cold_probe.unavailable
        if cold_unavailable:
            assert cold_probe is not None
            if not hot_exists:
                logger.warning("冷层块临时不可用：hash={}，path={}，error={}", digest[:12], cold_path, cold_probe.error)
                raise FuseOSError(errno.EIO)
            cold_exists = False
        else:
            cold_exists = bool(row["cold_present"]) and cold_probe is not None and cold_probe.present
        repair: dict[str, bool | int] = {}

        if not hot_exists and not cold_exists:
            hot_probe = probe_path(hot_path)
            cold_probe = probe_path(cold_path)
            hot_exists = hot_probe.present
            if cold_probe.unavailable:
                logger.warning("冷层块临时不可用：hash={}，path={}，error={}", digest[:12], cold_path, cold_probe.error)
                raise FuseOSError(errno.EIO)
            cold_exists = cold_probe.present
            if hot_exists or cold_exists:
                preferred = 1 if hot_exists else 2
                repair = {
                    "hot_present": hot_exists,
                    "cold_present": cold_exists,
                    "preferred_tier": preferred,
                }
                logger.warning("块元数据位置缺失但磁盘存在副本，准备修复：hash={}，hot={}，cold={}", digest[:12], hot_exists, cold_exists)
                if repair_metadata:
                    self.metadata.set_block_presence(digest, **repair)
            else:
                logger.error("块元数据引用的 payload 缺失：hash={}", digest[:12])
                raise FuseOSError(errno.EIO)

        if cold_unavailable and hot_exists:
            logger.warning("块首选冷层临时不可用，临时改读热层且不修复元数据：hash={}", digest[:12])
            return hot_path, 1, repair
        if row["preferred_tier"] == 1 and hot_exists:
            return hot_path, 1, repair
        if row["preferred_tier"] == 2 and cold_exists:
            return cold_path, 2, repair
        if hot_exists:
            repair["preferred_tier"] = 1
            if repair_metadata:
                self.metadata.set_block_presence(digest, preferred_tier=1)
            logger.warning("块首选层缺失，改用热层：hash={}", digest[:12])
            return hot_path, 1, repair
        repair["preferred_tier"] = 2
        if repair_metadata:
            self.metadata.set_block_presence(digest, preferred_tier=2)
        logger.warning("块首选层缺失，改用冷层：hash={}", digest[:12])
        return cold_path, 2, repair

    def fsync_dir(self, path: Path) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
