#!/usr/bin/env python
"""Profile representative ztierfs workloads without requiring a real FUSE mount."""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import random
import sys
import tempfile
import time
from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.helpers import TestOperationsAdapter, make_fs, rows  # noqa: E402
from ztierfs.perf import collect_perf  # noqa: E402


def _uncompressed_bytes(size: int) -> bytes:
    pattern = bytes(range(256))
    return (pattern * ((size // len(pattern)) + 1))[:size]


@contextmanager
def profiled_adapter(operations, profile: cProfile.Profile):
    adapter = TestOperationsAdapter(operations)
    try:
        yield adapter
    finally:
        profile.disable()
        adapter.close()
        profile.enable()


def small_file_create(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root)
    with profiled_adapter(fs_impl, profile) as fs:
        for index in range(200):
            path = f"/small-{index}.txt"
            fh = fs("create", path, 0o644)
            fs("write", path, b"small payload", 0, fh)
            fs("release", path, fh)


def small_file_create_no_inline(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root, inline_max_bytes=0)
    with profiled_adapter(fs_impl, profile) as fs:
        for index in range(200):
            path = f"/small-no-inline-{index}.txt"
            fh = fs("create", path, 0o644)
            fs("write", path, b"small payload", 0, fh)
            fs("release", path, fh)


def small_file_read(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root)
    payload = b"small payload"
    with profiled_adapter(fs_impl, profile) as fs:
        handles = []
        for index in range(500):
            path = f"/small-{index}.txt"
            fh = fs("create", path, 0o644)
            fs("write", path, payload, 0, fh)
            handles.append((path, fh))
        for path, fh in handles:
            fs("read", path, len(payload), 0, fh)


def metadata_walk(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root)
    with profiled_adapter(fs_impl, profile) as fs:
        for index in range(500):
            path = f"/entry-{index}.txt"
            fh = fs("create", path, 0o644)
            fs("write", path, b"x", 0, fh)
            fs("release", path, fh)
        for _ in range(50):
            fs("readdir", "/", None)
        for index in range(500):
            fs("getattr", f"/entry-{index}.txt", None)


def large_sequential_write(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root, chunk_size=256 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(16 * 1024 * 1024)
    with profiled_adapter(fs_impl, profile) as fs:
        fh = fs("create", "/large.jpg", 0o644)
        for offset in range(0, len(data), 256 * 1024):
            fs("write", "/large.jpg", data[offset : offset + 256 * 1024], offset, fh)
        fs("flush", "/large.jpg", fh)
        fs("release", "/large.jpg", fh)


def sequential_block_read(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root, chunk_size=256 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(16 * 1024 * 1024)
    with profiled_adapter(fs_impl, profile) as fs:
        fh = fs("create", "/sequential.jpg", 0o644)
        fs("write", "/sequential.jpg", data, 0, fh)
        for offset in range(0, len(data), 256 * 1024):
            fs("read", "/sequential.jpg", 256 * 1024, offset, fh)
        fs("release", "/sequential.jpg", fh)


def random_read(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root, chunk_size=64 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(8 * 1024 * 1024)
    rng = random.Random(0)
    offsets = [rng.randrange(0, len(data) - 4096) for _ in range(2000)]
    with profiled_adapter(fs_impl, profile) as fs:
        fh = fs("create", "/random.jpg", 0o644)
        fs("write", "/random.jpg", data, 0, fh)
        for offset in offsets:
            fs("read", "/random.jpg", 4096, offset, fh)
        fs("release", "/random.jpg", fh)


def repeated_random_read(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root, chunk_size=64 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(8 * 1024 * 1024)
    rng = random.Random(0)
    offsets = [rng.randrange(0, len(data) - 4096) for _ in range(1000)]
    with profiled_adapter(fs_impl, profile) as fs:
        fh = fs("create", "/cached.jpg", 0o644)
        fs("write", "/cached.jpg", data, 0, fh)
        for _ in range(2):
            for offset in offsets:
                fs("read", "/cached.jpg", 4096, offset, fh)
        fs("release", "/cached.jpg", fh)


def overwrite(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root, chunk_size=256 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(16 * 1024 * 1024)
    replacement = bytes(reversed(range(256))) * 1024
    with profiled_adapter(fs_impl, profile) as fs:
        fh = fs("create", "/overwrite.jpg", 0o644)
        fs("write", "/overwrite.jpg", data, 0, fh)
        for offset in range(0, len(data), 256 * 1024):
            fs("write", "/overwrite.jpg", replacement, offset, fh)
        fs("release", "/overwrite.jpg", fh)


def rename_unlink(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(root, inline_max_bytes=0)
    with profiled_adapter(fs_impl, profile) as fs:
        for index in range(500):
            source = f"/source-{index}.txt"
            target = f"/target-{index}.txt"
            fh = fs("create", source, 0o644)
            fs("write", source, b"contents", 0, fh)
            fs("rename", source, target, 0)
            fs("unlink", target)
            fs("release", target, fh)


def cold_copy_up(root: Path, profile: cProfile.Profile) -> None:
    fs_impl = make_fs(
        root,
        chunk_size=1024,
        hot_cache_max_bytes=1500,
        hot_cache_min_bytes=1024,
        protected_prefix_chunks=0,
        min_hot_age_seconds=0,
        inline_max_bytes=0,
    )
    cold_data = _uncompressed_bytes(1024)
    hot_data = bytes(reversed(range(256))) * 4
    with profiled_adapter(fs_impl, profile) as fs:
        cold_fh = fs("create", "/cold.jpg", 0o644)
        hot_fh = fs("create", "/hot.jpg", 0o644)
        fs("write", "/cold.jpg", cold_data, 0, cold_fh)
        fs("write", "/hot.jpg", hot_data, 0, hot_fh)
        assert rows(
            fs_impl,
            "SELECT COUNT(*) AS total FROM block_records WHERE cold_present = 1",
        )[0]["total"]
        for _ in range(200):
            fs("read", "/cold.jpg", len(cold_data), 0, cold_fh)
        fs("release", "/cold.jpg", cold_fh)
        fs("release", "/hot.jpg", hot_fh)


WORKLOADS: dict[str, Callable[[Path, cProfile.Profile], None]] = {
    "small-file-create": small_file_create,
    "small-file-create-no-inline": small_file_create_no_inline,
    "small-file-read": small_file_read,
    "large-sequential-write": large_sequential_write,
    "metadata-walk": metadata_walk,
    "sequential-block-read": sequential_block_read,
    "random-read": random_read,
    "repeated-random-read": repeated_random_read,
    "overwrite": overwrite,
    "rename-unlink": rename_unlink,
    "cold-copy-up": cold_copy_up,
}


def profile_workload(
    name: str, workload: Callable[[Path, cProfile.Profile], None], limit: int
) -> str:
    profile = cProfile.Profile()
    logger.disable("ztierfs")
    started = time.perf_counter()
    with collect_perf() as counters:
        try:
            with tempfile.TemporaryDirectory(prefix=f"ztierfs-profile-{name}-") as temp:
                root = Path(temp)
                profile.enable()
                workload(root, profile)
                profile.disable()
        finally:
            logger.enable("ztierfs")
    elapsed = time.perf_counter() - started

    output = io.StringIO()
    output.write(f"\n## {name}\n")
    output.write(f"wall_seconds: {elapsed:.6f}\n")
    perf = counters.snapshot()
    if perf["timings_ns"]:
        output.write("perf_counters:\n")
        for key, total_ns in sorted(perf["timings_ns"].items()):
            count = perf["counts"].get(key, 0)
            output.write(f"  {key}: {total_ns / 1_000_000:.3f} ms ({count} calls)\n")
        for key, total_bytes in sorted(perf["bytes"].items()):
            output.write(f"  {key}: {total_bytes} bytes\n")
    stats = pstats.Stats(profile, stream=output).strip_dirs().sort_stats("cumulative")
    stats.print_stats(limit)
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "workloads",
        nargs="*",
        choices=sorted(WORKLOADS),
        default=sorted(WORKLOADS),
        help="workloads to profile; defaults to all",
    )
    parser.add_argument(
        "--limit", type=int, default=30, help="number of profile rows per workload"
    )
    parser.add_argument("--output", type=Path, help="optional path for the text report")
    args = parser.parse_args()

    report = "".join(
        profile_workload(name, WORKLOADS[name], args.limit) for name in args.workloads
    )
    if args.output:
        args.output.write_text(report)
    else:
        sys.stdout.write(report)


if __name__ == "__main__":
    main()
