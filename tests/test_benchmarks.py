import os
import random
import sqlite3
import sys

import pytest

from .helpers import adapted, connect_sqlite, make_fs, mounted_ztierfs, rows, user_dir_entry_rows


pytestmark = pytest.mark.benchmark


def _uncompressed_bytes(size: int) -> bytes:
    pattern = bytes(range(256))
    return (pattern * ((size // len(pattern)) + 1))[:size]


def _assert_no_refcount_drift(fs_impl) -> None:
    refcounts = rows(
        fs_impl,
        """
        SELECT blocks.hash, blocks.refcount, COUNT(file_chunks.hash) AS actual_refs
        FROM blocks
        LEFT JOIN file_chunks ON file_chunks.hash = blocks.hash
        GROUP BY blocks.hash
        """,
    )
    assert all(row["refcount"] == row["actual_refs"] for row in refcounts)


def test_benchmark_small_file_create_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path)
    counter = 0

    with adapted(fs_impl) as fs:

        def create_batch():
            nonlocal counter
            for _ in range(24):
                path = f"/small-{counter}.txt"
                counter += 1
                fh = fs("create", path, 0o644)
                fs("write", path, b"small payload", 0, fh)
                fs("release", path, fh)

        benchmark.pedantic(create_batch, rounds=5, iterations=1)

    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM inodes")[0]["total"] > 1
    _assert_no_refcount_drift(fs_impl)


@pytest.mark.parametrize(
    ("inline_max_bytes", "payload_store"),
    [(0, "sqlite"), (128 * 1024, "sqlite"), (128 * 1024, "filekv")],
)
def test_benchmark_small_file_create_storage_matrix(
    tmp_path, benchmark, inline_max_bytes, payload_store
):
    fs_impl = make_fs(
        tmp_path,
        inline_max_bytes=inline_max_bytes,
        payload_store=payload_store,
    )
    counter = 0

    with adapted(fs_impl) as fs:

        def create_batch():
            nonlocal counter
            for _ in range(16):
                path = f"/matrix-{counter}.txt"
                counter += 1
                fh = fs("create", path, 0o644)
                fs("write", path, b"small payload", 0, fh)
                fs("release", path, fh)

        benchmark.pedantic(create_batch, rounds=3, iterations=1)

    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM inodes")[0]["total"] > 1


def test_benchmark_small_file_read_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path)
    payload = b"small payload"

    with adapted(fs_impl) as fs:
        handles = []
        for index in range(128):
            path = f"/small-{index}.txt"
            fh = fs("create", path, 0o644)
            fs("write", path, payload, 0, fh)
            handles.append((path, fh))

        def read_batch():
            for path, fh in handles:
                assert fs("read", path, len(payload), 0, fh) == payload

        benchmark.pedantic(read_batch, rounds=5, iterations=1)


@pytest.mark.parametrize("payload_store", ["sqlite", "filekv"])
def test_benchmark_small_file_read_payload_store_matrix(
    tmp_path, benchmark, payload_store
):
    fs_impl = make_fs(tmp_path, payload_store=payload_store)
    payload = b"small payload"

    with adapted(fs_impl) as fs:
        handles = []
        for index in range(96):
            path = f"/payload-{index}.txt"
            fh = fs("create", path, 0o644)
            fs("write", path, payload, 0, fh)
            handles.append((path, fh))

        def read_batch():
            for path, fh in handles:
                assert fs("read", path, len(payload), 0, fh) == payload

        benchmark.pedantic(read_batch, rounds=3, iterations=1)


def test_benchmark_readdir_with_attrs_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        for index in range(128):
            path = f"/entry-{index}.txt"
            fh = fs("create", path, 0o644)
            fs("write", path, b"x", 0, fh)

        def read_directory():
            assert len(fs("readdir", "/", None)) >= 128

        benchmark.pedantic(read_directory, rounds=5, iterations=1)


def test_benchmark_stat_many_files_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        paths = []
        for index in range(128):
            path = f"/stat-{index}.txt"
            fh = fs("create", path, 0o644)
            fs("write", path, b"x", 0, fh)
            fs("release", path, fh)
            paths.append(path)

        def stat_all():
            for path in paths:
                assert fs("getattr", path, None)["st_size"] == 1

        benchmark.pedantic(stat_all, rounds=5, iterations=1)


def test_benchmark_inode_first_metadata_walk(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path)

    def seed():
        for index in range(256):
            entry, fh = fs_impl.create(
                1, f"inode-{index}.txt".encode(), 0o644, os.O_RDWR, None
            )
            fs_impl.write(entry.ino, b"x", 0, fh)
            fs_impl.release(entry.ino, fh)

    seed()

    def walk():
        entries = fs_impl.readdir(1, 0, 128 * 1024, 0, 0)
        for entry in entries:
            if entry.name.startswith(b"inode-"):
                assert fs_impl.getattr(entry.ino)["st_size"] == 1

    try:
        benchmark.pedantic(walk, rounds=5, iterations=1)
    finally:
        fs_impl.close()


def test_benchmark_large_sequential_write_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path, chunk_size=256 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(4 * 1024 * 1024)
    counter = 0

    with adapted(fs_impl) as fs:

        def write_file():
            nonlocal counter
            path = f"/large-{counter}.jpg"
            counter += 1
            fh = fs("create", path, 0o644)
            for offset in range(0, len(data), 256 * 1024):
                chunk = data[offset : offset + 256 * 1024]
                assert fs("write", path, chunk, offset, fh) == len(chunk)
            fs("flush", path, fh)
            fs("release", path, fh)

        benchmark.pedantic(write_file, rounds=3, iterations=1)

    chunk_count = rows(fs_impl, "SELECT COUNT(*) AS total FROM file_chunks")[0]["total"]
    assert chunk_count >= 16
    _assert_no_refcount_drift(fs_impl)


def test_benchmark_sequential_block_read_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path, chunk_size=64 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(2 * 1024 * 1024)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/sequential.jpg", 0o644)
        fs("write", "/sequential.jpg", data, 0, fh)

        def read_sequential():
            for offset in range(0, len(data), 64 * 1024):
                assert (
                    fs("read", "/sequential.jpg", 64 * 1024, offset, fh)
                    == data[offset : offset + 64 * 1024]
                )

        benchmark.pedantic(read_sequential, rounds=5, iterations=1)
        fs("release", "/sequential.jpg", fh)


def test_benchmark_random_read_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path, chunk_size=64 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(2 * 1024 * 1024)
    rng = random.Random(0)
    offsets = [rng.randrange(0, len(data) - 4096) for _ in range(128)]

    with adapted(fs_impl) as fs:
        fh = fs("create", "/random.jpg", 0o644)
        fs("write", "/random.jpg", data, 0, fh)

        def read_offsets():
            for offset in offsets:
                assert (
                    fs("read", "/random.jpg", 4096, offset, fh)
                    == data[offset : offset + 4096]
                )

        benchmark.pedantic(read_offsets, rounds=5, iterations=1)
        fs("release", "/random.jpg", fh)

    block = rows(fs_impl, "SELECT SUM(read_count) AS reads FROM blocks")[0]
    assert block["reads"] >= len(offsets)


def test_benchmark_repeated_random_read_cache_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path, chunk_size=64 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(2 * 1024 * 1024)
    offsets = [index * 4096 for index in range(64)]

    with adapted(fs_impl) as fs:
        fh = fs("create", "/cached.jpg", 0o644)
        fs("write", "/cached.jpg", data, 0, fh)

        def read_offsets_twice():
            for _ in range(2):
                for offset in offsets:
                    assert (
                        fs("read", "/cached.jpg", 4096, offset, fh)
                        == data[offset : offset + 4096]
                    )

        benchmark.pedantic(read_offsets_twice, rounds=5, iterations=1)
        fs("release", "/cached.jpg", fh)


def test_benchmark_full_block_overwrite_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path, chunk_size=64 * 1024, inline_max_bytes=0)
    original = _uncompressed_bytes(512 * 1024)
    replacement = bytes(reversed(range(256))) * 256

    with adapted(fs_impl) as fs:
        fh = fs("create", "/overwrite.jpg", 0o644)
        fs("write", "/overwrite.jpg", original, 0, fh)
        offset = 2 * 64 * 1024

        def overwrite_block():
            assert fs("write", "/overwrite.jpg", replacement, offset, fh) == len(
                replacement
            )

        benchmark.pedantic(overwrite_block, rounds=5, iterations=1)
        fs("release", "/overwrite.jpg", fh)


def test_benchmark_cross_block_partial_write_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path, chunk_size=64 * 1024, inline_max_bytes=0)
    data = _uncompressed_bytes(512 * 1024)
    patch = b"P" * 8192

    with adapted(fs_impl) as fs:
        fh = fs("create", "/partial.jpg", 0o644)
        fs("write", "/partial.jpg", data, 0, fh)
        offset = 64 * 1024 - 4096

        def patch_boundary():
            assert fs("write", "/partial.jpg", patch, offset, fh) == len(patch)

        benchmark.pedantic(patch_boundary, rounds=5, iterations=1)
        fs("release", "/partial.jpg", fh)


def test_benchmark_rename_unlink_adapter(tmp_path, benchmark):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    counter = 0

    with adapted(fs_impl) as fs:

        def rename_unlink_batch():
            nonlocal counter
            for _ in range(32):
                source = f"/rename-source-{counter}.txt"
                target = f"/rename-target-{counter}.txt"
                counter += 1
                fh = fs("create", source, 0o644)
                fs("write", source, b"contents", 0, fh)
                fs("rename", source, target, 0)
                fs("unlink", target)
                fs("release", target, fh)

        benchmark.pedantic(rename_unlink_batch, rounds=5, iterations=1)

    assert user_dir_entry_rows(fs_impl) == []
    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM blocks")[0]["total"] == 0


def test_benchmark_cold_copy_up_adapter(tmp_path, benchmark):
    fs_impl = make_fs(
        tmp_path,
        chunk_size=1024,
        hot_cache_max_bytes=1500,
        hot_cache_min_bytes=1024,
        protected_prefix_chunks=0,
        min_hot_age_seconds=0,
        inline_max_bytes=0,
    )
    cold_data = _uncompressed_bytes(1024)
    hot_data = bytes(reversed(range(256))) * 4

    with adapted(fs_impl) as fs:
        cold_fh = fs("create", "/cold.jpg", 0o644)
        hot_fh = fs("create", "/hot.jpg", 0o644)
        fs("write", "/cold.jpg", cold_data, 0, cold_fh)
        fs("write", "/hot.jpg", hot_data, 0, hot_fh)
        assert rows(
            fs_impl,
            "SELECT COUNT(*) AS total FROM block_records WHERE cold_present = 1",
        )[0]["total"]

        def read_cold():
            assert fs("read", "/cold.jpg", len(cold_data), 0, cold_fh) == cold_data

        benchmark.pedantic(read_cold, rounds=5, iterations=1)
        fs("release", "/cold.jpg", cold_fh)
        fs("release", "/hot.jpg", hot_fh)

    presence = rows(
        fs_impl,
        "SELECT SUM(hot_present) AS hot, SUM(cold_present) AS cold FROM block_records",
    )[0]
    assert presence["hot"] >= 1
    assert presence["cold"] >= 1


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin", reason="ztierfs only supports macOS")
def test_benchmark_real_mount_mixed_workload(tmp_path, benchmark):
    payload = _uncompressed_bytes(512 * 1024)

    with mounted_ztierfs(tmp_path) as (mount, _tier1, _tier2, database):
        counter = 0

        def mixed_workload():
            nonlocal counter
            path = mount / f"mounted-{counter}.jpg"
            renamed = mount / f"mounted-renamed-{counter}.jpg"
            counter += 1
            with path.open("wb") as file:
                for offset in range(0, len(payload), 64 * 1024):
                    file.write(payload[offset : offset + 64 * 1024])
                file.flush()
                os.fsync(file.fileno())
            assert path.read_bytes()[4096:8192] == payload[4096:8192]
            path.rename(renamed)
            renamed.unlink()

        benchmark.pedantic(mixed_workload, rounds=3, iterations=1)

        with connect_sqlite(database) as db:
            assert (
                db.execute(
                    """
                    SELECT COUNT(*)
                    FROM dir_entries
                    WHERE name LIKE 'mounted%'
                    """
                ).fetchone()[0]
                == 0
            )
