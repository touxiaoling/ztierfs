"""Physical block-file IO for tiered content-addressed payloads."""

import os
import shutil
import threading

from pathlib import Path

from loguru import logger

from .block_layout import block_file_path
from .perf import timed
from .tier_access import (
    PathProbe,
    probe_path,
    read_path_bytes,
    unlink_path,
)


class BlockFileIO:
    """Atomic physical IO boundary for tiered block files."""

    def __init__(self, tier1_blocks: Path, tier2_blocks: Path):
        self.tier1_blocks = tier1_blocks
        self.tier2_blocks = tier2_blocks

    def block_path(self, digest: str, tier: int) -> Path:
        """Return the content-addressed block-file path for ``digest`` on ``tier``."""
        return block_file_path(self.tier1_blocks, self.tier2_blocks, digest, tier)

    def read_payload(self, digest: str, tier: int) -> bytes:
        """Read the payload bytes for ``digest`` on ``tier``."""
        return self.read_path(self.block_path(digest, tier))

    def read_path(self, path: Path) -> bytes:
        """Read payload bytes from an already selected path."""
        return read_path_bytes(path)

    def write_atomic(self, digest: str, tier: int, payload: bytes) -> None:
        """Atomically write a block file with file and directory fsync."""
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

    def copy_atomic(self, digest: str, source_tier: int, target_tier: int) -> None:
        """Atomically copy a block between tiers; missing/unavailable paths are safe no-ops."""
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

    def unlink(self, digest: str, tier: int) -> bool:
        """Unlink a block file; return false if it was already missing."""
        return unlink_path(self.block_path(digest, tier))

    def probe(self, digest: str, tier: int) -> PathProbe:
        """Probe a block file path without reading its payload."""
        return probe_path(self.block_path(digest, tier))

    def probe_path(self, path: Path) -> PathProbe:
        """Probe an already selected block path without reading its payload."""
        return probe_path(path)

    @staticmethod
    def fsync_dir(path: Path) -> None:
        """Fsync a directory file descriptor when the platform supports it."""
        if not hasattr(os, "O_DIRECTORY"):
            return
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
