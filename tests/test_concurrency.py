import errno
import os
import sqlite3
import threading

from concurrent.futures import ThreadPoolExecutor

import pytest
from macfusepy import FuseOSError

import ztierfs.metadata.store as store_module
from ztierfs.metadata import ConnectionPool

from .helpers import adapted, connect_sqlite, make_fs, rows, user_dir_entry_rows, user_inode_rows


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
    assert refcounts
    assert all(row["refcount"] == row["actual_refs"] for row in refcounts)
    assert not rows(fs_impl, "SELECT * FROM blocks WHERE refcount < 0")


def test_connection_pool_reuses_connections_and_times_out_when_full(tmp_path):
    pool = ConnectionPool(tmp_path / "metadata.sqlite3", max_size=1)
    first = pool.acquire()
    try:
        with pytest.raises(TimeoutError):
            pool.acquire(timeout=0.01)
    finally:
        pool.release(first)

    with pool.connection() as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -(32 * 1024)
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 256 * 1024 * 1024
        assert db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 8192

    pool.close()
    with pytest.raises(RuntimeError):
        pool.acquire()


def test_metadata_store_rejects_write_nested_in_read_transaction(tmp_path):
    fs_impl = make_fs(tmp_path)
    try:
        with fs_impl.metadata.read_transaction():
            with pytest.raises(RuntimeError):
                with fs_impl.metadata.transaction():
                    pass
    finally:
        fs_impl.close()


def test_metadata_store_logs_expected_fuse_errors_at_debug(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path)
    calls = {"debug": [], "exception": []}

    class FakeLogger:
        def info(self, message, *args):
            pass

        def debug(self, message, *args):
            calls["debug"].append((message, args))

        def exception(self, message, *args):
            calls["exception"].append((message, args))

    monkeypatch.setattr(store_module, "logger", FakeLogger())

    try:
        with pytest.raises(FuseOSError):
            with fs_impl.metadata.read_transaction():
                raise FuseOSError(errno.ENOENT)
    finally:
        fs_impl.close()

    assert calls["debug"] == [
        ("SQLite 事务因 FUSE 返回码回滚：readonly={}，errno={}", (True, errno.ENOENT))
    ]
    assert calls["exception"] == []


def test_metadata_store_keeps_exception_logs_for_unexpected_errors(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path)
    calls = {"exception": []}

    class FakeLogger:
        def info(self, message, *args):
            pass

        def debug(self, message, *args):
            pass

        def exception(self, message, *args):
            calls["exception"].append((message, args))

    monkeypatch.setattr(store_module, "logger", FakeLogger())

    try:
        with pytest.raises(ValueError):
            with fs_impl.metadata.read_transaction():
                raise ValueError("boom")
    finally:
        fs_impl.close()

    assert calls["exception"] == [("SQLite 事务回滚：readonly={}", (True,))]


def test_ztierfs_handles_parallel_reads_and_writes_without_refcount_drift(tmp_path):
    fs_impl = make_fs(tmp_path, chunk_size=256, inline_max_bytes=0)
    paths = [f"/file-{index}.bin" for index in range(8)]

    with adapted(fs_impl) as fs:
        handles = {path: fs("create", path, 0o644) for path in paths}

        def exercise_file(path: str) -> bytes:
            fh = handles[path]
            final = b""
            for iteration in range(12):
                payload = f"{path}:{iteration}".encode().ljust(700, b"x")
                fs("write", path, payload, 0, fh)
                assert fs("getattr", path)["st_size"] == len(payload)
                assert fs("read", path, len(payload), 0, fh) == payload
                fs("readdir", "/", None)
                final = payload
            return final

        with ThreadPoolExecutor(max_workers=len(paths)) as executor:
            expected = dict(zip(paths, executor.map(exercise_file, paths), strict=True))

        for path, payload in expected.items():
            assert fs("read", path, len(payload), 0, handles[path]) == payload
            fs("release", path, handles[path])

    _assert_no_refcount_drift(fs_impl)

    with connect_sqlite(fs_impl.database) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_ztierfs_serializes_parallel_writes_to_same_inode(tmp_path):
    fs_impl = make_fs(tmp_path, chunk_size=128, inline_max_bytes=0)
    payloads = [bytes([65 + index]) * 128 for index in range(12)]

    with adapted(fs_impl) as fs:
        handles = [fs("create", "/shared.bin", 0o644)]
        handles.extend(fs("open", "/shared.bin", os.O_RDWR) for _ in payloads[1:])
        start = threading.Barrier(len(payloads))

        def write_chunk(index: int) -> None:
            payload = payloads[index]
            offset = index * len(payload)
            start.wait(timeout=5)
            assert fs("write", "/shared.bin", payload, offset, handles[index]) == len(
                payload
            )
            assert (
                fs("read", "/shared.bin", len(payload), offset, handles[index])
                == payload
            )

        with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
            list(executor.map(write_chunk, range(len(payloads))))

        expected = b"".join(payloads)
        assert fs("getattr", "/shared.bin")["st_size"] == len(expected)
        assert fs("read", "/shared.bin", len(expected), 0, handles[0]) == expected
        for handle in handles:
            fs("release", "/shared.bin", handle)

    _assert_no_refcount_drift(fs_impl)


def test_ztierfs_handles_parallel_namespace_churn_in_same_directory(tmp_path):
    fs_impl = make_fs(tmp_path, chunk_size=256, inline_max_bytes=0)
    worker_count = 8
    iterations = 8

    with adapted(fs_impl) as fs:
        fs("mkdir", "/work", 0o755)
        start = threading.Barrier(worker_count)

        def churn(worker: int) -> None:
            start.wait(timeout=5)
            for iteration in range(iterations):
                source = f"/work/{worker}-{iteration}.tmp"
                target = f"/work/{worker}-{iteration}.dat"
                payload = f"{worker}:{iteration}".encode().ljust(512, b"n")
                handle = fs("create", source, 0o644)
                fs("write", source, payload, 0, handle)
                fs("rename", source, target, 0)
                assert fs("read", target, len(payload), 0, handle) == payload
                fs("readdir", "/work", None)
                fs("unlink", target)
                fs("release", target, handle)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(churn, range(worker_count)))

        assert fs("readdir", "/work", None) == [".", ".."]
        fs("rmdir", "/work")

    assert user_inode_rows(fs_impl) == []
    assert user_dir_entry_rows(fs_impl) == []
    assert rows(fs_impl, "SELECT * FROM blocks") == []


def test_ztierfs_handles_parallel_cold_copy_up_of_deduplicated_block(tmp_path):
    fs_impl = make_fs(
        tmp_path,
        chunk_size=1024,
        hot_cache_max_bytes=1500,
        hot_cache_min_bytes=1024,
        protected_prefix_chunks=0,
        min_hot_age_seconds=0,
        inline_max_bytes=0,
    )
    paths = [f"/cold-{index}.jpg" for index in range(6)]
    cold_data = bytes(range(256)) * 4
    hot_data = bytes(reversed(range(256))) * 4

    with adapted(fs_impl) as fs:
        handles = {}
        for path in paths:
            handles[path] = fs("create", path, 0o644)
            fs("write", path, cold_data, 0, handles[path])
        hot_handle = fs("create", "/hot.jpg", 0o644)
        fs("write", "/hot.jpg", hot_data, 0, hot_handle)

        assert rows(
            fs_impl,
            "SELECT COUNT(*) AS total FROM block_records WHERE cold_present = 1",
        )[0]["total"]

        start = threading.Barrier(len(paths))

        def read_cold(path: str) -> bytes:
            start.wait(timeout=5)
            return fs("read", path, len(cold_data), 0, handles[path])

        with ThreadPoolExecutor(max_workers=len(paths)) as executor:
            assert list(executor.map(read_cold, paths)) == [cold_data] * len(paths)

        for path, handle in handles.items():
            fs("release", path, handle)
        fs("release", "/hot.jpg", hot_handle)

    _assert_no_refcount_drift(fs_impl)
    presence = rows(
        fs_impl,
        "SELECT SUM(hot_present) AS hot, SUM(cold_present) AS cold FROM block_records",
    )[0]
    assert presence["hot"] >= 1
    assert presence["cold"] >= 1
