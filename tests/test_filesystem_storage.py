import os
import threading
import time

from concurrent.futures import ThreadPoolExecutor

from ztierfs.file_content import FileWriteChunkKind

from .helpers import adapted, connect_sqlite, make_fs, rows


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_ztierfs_uses_conservative_tiering_age_defaults(tmp_path):
    fs_impl = make_fs(tmp_path)
    try:
        assert fs_impl.tiering_policy.min_hot_age_ns == 24 * 60 * 60 * 1_000_000_000
        assert fs_impl.tiering_policy.cold_copy_cleanup_age_ns == 0
    finally:
        fs_impl.close()


def test_ztierfs_splits_compresses_and_reads_files(tmp_path):
    fs_impl = make_fs(tmp_path, compression_min_bytes=0)
    data = b"a" * 3000

    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        assert fs("write", "/note.txt", data, 0, fh) == len(data)
        assert fs("read", "/note.txt", len(data), 0, fh) == data
        assert fs("getattr", "/note.txt")["st_size"] == len(data)

    assert len(rows(fs_impl, "SELECT * FROM file_chunks")) == 3
    assert any(row["compressed"] for row in rows(fs_impl, "SELECT * FROM blocks"))


def test_ztierfs_skips_zstd_for_known_compressed_suffixes(tmp_path):
    fs_impl = make_fs(tmp_path)
    data = b"\xff\xd8" * 1024

    with adapted(fs_impl) as fs:
        fh = fs("create", "/photo.jpg", 0o644)
        fs("write", "/photo.jpg", data, 0, fh)

    block_rows = rows(fs_impl, "SELECT compressed, raw_size, stored_size FROM blocks")
    assert block_rows
    assert {row["compressed"] for row in block_rows} == {0}
    assert {row["raw_size"] for row in block_rows} == {1024}
    assert {row["stored_size"] for row in block_rows} == {1024}


def test_ztierfs_deduplicates_equal_chunks(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"same-block" * 100

    with adapted(fs_impl) as fs:
        first = fs("create", "/first.txt", 0o644)
        second = fs("create", "/second.txt", 0o644)
        fs("write", "/first.txt", data, 0, first)
        fs("write", "/second.txt", data, 0, second)

    block_rows = rows(fs_impl, "SELECT refcount FROM blocks")
    assert len(block_rows) == 1
    assert block_rows[0]["refcount"] == 2


def test_ztierfs_batches_duplicate_prepared_chunks_within_one_write(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"x" * fs_impl.chunk_size
    calls = 0
    original_existing_block_hashes = fs_impl.metadata.existing_block_hashes
    original_insert_blocks = fs_impl.metadata.insert_blocks
    inserted_batch_sizes: list[int] = []

    def observed_existing_block_hashes(digests):
        nonlocal calls
        calls += 1
        return original_existing_block_hashes(digests)

    def observed_insert_blocks(blocks, *, now):
        inserted_batch_sizes.append(len(blocks))
        return original_insert_blocks(blocks, now=now)

    monkeypatch.setattr(
        fs_impl.metadata, "existing_block_hashes", observed_existing_block_hashes
    )
    monkeypatch.setattr(fs_impl.metadata, "insert_blocks", observed_insert_blocks)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/repeated.txt", 0o644)
        fs("write", "/repeated.txt", data * 3, 0, fh)

    block_rows = rows(fs_impl, "SELECT refcount FROM blocks")
    assert len(block_rows) == 1
    assert block_rows[0]["refcount"] == 3
    assert calls == 1
    assert inserted_batch_sizes == [1]


def test_ztierfs_prepares_partial_overwrite_after_read_transaction(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"a" * fs_impl.chunk_size
    original_read_block_snapshot = fs_impl.block_store.read_block_snapshot

    def observed_read_block_snapshot(row, expected_size):
        assert not getattr(fs_impl.metadata._local, "readonly", False)
        return original_read_block_snapshot(row, expected_size)

    monkeypatch.setattr(
        fs_impl.block_store, "read_block_snapshot", observed_read_block_snapshot
    )

    with adapted(fs_impl) as fs:
        fh = fs("create", "/partial.txt", 0o644)
        fs("write", "/partial.txt", data, 0, fh)
        fs("write", "/partial.txt", b"Z", 10, fh)
        assert (
            fs("read", "/partial.txt", len(data), 0, fh) == data[:10] + b"Z" + data[11:]
        )


def test_ztierfs_write_plan_classifies_full_partial_and_sparse_chunks(tmp_path):
    fs_impl = make_fs(tmp_path, chunk_size=8, inline_max_bytes=0)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/planned.bin", 0o644)
        fs("write", "/planned.bin", b"abcdefghij", 0, fh)
        with fs_impl.metadata.read_transaction():
            node = fs_impl.metadata.inode_by_id(fs_impl.handles.file_id(fh))
            plan = fs_impl.file_content.plan_write_file(
                node, "/planned.bin", b"XYZ" + b"12345678", 5
            )
            sparse_plan = fs_impl.file_content.plan_write_file(
                node, "/planned.bin", b"tail", 18
            )

    assert [chunk.kind for chunk in plan.chunks] == [
        FileWriteChunkKind.PARTIAL_EXISTING_REPLACE,
        FileWriteChunkKind.FULL_CHUNK_REPLACE,
    ]
    assert [chunk.kind for chunk in sparse_plan.chunks] == [
        FileWriteChunkKind.SPARSE_EXTEND_PARTIAL
    ]


def test_ztierfs_clonefile_copies_metadata_without_reprocessing_blocks(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path)
    data = b"a" * 2048

    with adapted(fs_impl) as fs:
        fh = fs("create", "/source.txt", 0o640)
        fs("write", "/source.txt", data, 0, fh)
        fs("setxattr", "/source.txt", "user.note", b"copied", 0, 0)

        original_prepare_blocks = fs_impl.block_store.prepare_blocks

        def fail_prepare_blocks(*args, **kwargs):
            raise AssertionError("clonefile must not reprocess file blocks")

        monkeypatch.setattr(fs_impl.block_store, "prepare_blocks", fail_prepare_blocks)
        fs("clonefile", "/source.txt", "/copy.txt")
        monkeypatch.setattr(
            fs_impl.block_store, "prepare_blocks", original_prepare_blocks
        )

        copy_fh = fs("open", "/copy.txt", os.O_RDWR)
        assert fs("read", "/copy.txt", len(data), 0, copy_fh) == data
        assert fs("getxattr", "/copy.txt", "user.note", 0) == b"copied"
        assert fs("getattr", "/copy.txt")["st_nlink"] == 1
        assert fs("getattr", "/source.txt")["st_nlink"] == 1

        fs("write", "/copy.txt", b"b", 0, copy_fh)
        assert fs("read", "/source.txt", len(data), 0, fh) == data
        assert fs("read", "/copy.txt", 1, 0, copy_fh) == b"b"

    block_rows = rows(
        fs_impl, "SELECT raw_size, refcount FROM blocks ORDER BY refcount"
    )
    assert [row["refcount"] for row in block_rows] == [1, 3]
    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM file_chunks")[0]["total"] == 4


def test_ztierfs_inlines_small_processed_blocks_in_database(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=64, compression_min_bytes=0)
    data = b"a" * 1024

    with adapted(fs_impl) as fs:
        fh = fs("create", "/small.txt", 0o644)
        fs("write", "/small.txt", data, 0, fh)
        assert fs("read", "/small.txt", len(data), 0, fh) == data

    block = rows(
        fs_impl,
        """
        SELECT
            storage,
            inline_payload,
            compressed,
            raw_size,
            stored_size,
            hot_present,
            cold_present
        FROM block_records
        """,
    )[0]
    assert block["storage"] == "inline"
    assert block["inline_payload"] is not None
    assert len(rows(fs_impl, "SELECT * FROM block_payloads")) == 1
    assert block["compressed"] == 1
    assert block["raw_size"] == len(data)
    assert block["stored_size"] <= 64
    assert (block["hot_present"], block["cold_present"]) == (0, 0)
    assert not any((tmp_path / "hot" / "blocks").glob("*/*/*"))
    assert not any((tmp_path / "cold" / "blocks").glob("*/*/*"))


def test_ztierfs_uses_processed_payload_size_for_inline_threshold(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=64, compression_min_bytes=0)
    data = bytes(range(256)) * 4

    with adapted(fs_impl) as fs:
        compressible = fs("create", "/compressible.txt", 0o644)
        incompressible = fs("create", "/photo.jpg", 0o644)
        fs("write", "/compressible.txt", b"a" * 1024, 0, compressible)
        fs("write", "/photo.jpg", data, 0, incompressible)

    rows_by_name = {
        row["name"]: row
        for row in rows(
            fs_impl,
            """
            SELECT dir_entries.name, block_records.storage
            FROM dir_entries
            JOIN file_chunks ON file_chunks.file_id = dir_entries.inode_id
            JOIN block_records ON block_records.hash = file_chunks.hash
            """,
        )
    }
    assert rows_by_name["compressible.txt"]["storage"] == "inline"
    assert rows_by_name["photo.jpg"]["storage"] == "tiered"


def test_ztierfs_stores_small_files_as_inline_block_and_reopens(tmp_path):
    fs_impl = make_fs(tmp_path)
    data = b"tiny payload"

    with adapted(fs_impl) as fs:
        fh = fs("create", "/small.txt", 0o644)
        fs("write", "/small.txt", data, 0, fh)
        fs("release", "/small.txt", fh)

    block = rows(
        fs_impl,
        """
        SELECT inodes.size, file_chunks.chunk_index, file_chunks.size AS chunk_size,
               block_records.storage, block_records.inline_payload,
               block_records.stored_size, block_records.hot_present,
               block_records.cold_present
        FROM inodes
        JOIN file_chunks ON file_chunks.file_id = inodes.id
        JOIN block_records ON block_records.hash = file_chunks.hash
        WHERE inodes.kind = 'file'
        """,
    )[0]
    assert block["size"] == len(data)
    assert block["chunk_index"] == 0
    assert block["chunk_size"] == len(data)
    assert block["storage"] == "inline"
    assert bytes(block["inline_payload"]) == data
    assert block["stored_size"] == len(data)
    assert (block["hot_present"], block["cold_present"]) == (0, 0)

    reopened = make_fs(tmp_path)
    with adapted(reopened) as fs:
        fh = fs("open", "/small.txt", os.O_RDONLY)
        assert fs("read", "/small.txt", len(data), 0, fh) == data


def test_open_trunc_uses_inode_selected_before_path_replacement(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/target.txt", 0o644)
        fs("write", "/target.txt", b"old", 0, fh)
        fs("release", "/target.txt", fh)

        replacement = fs("create", "/replacement.txt", 0o644)
        fs("write", "/replacement.txt", b"new", 0, replacement)
        fs("release", "/replacement.txt", replacement)

        original_content_lock = fs_impl._content_lock
        replaced = False

        def content_lock_after_path_replacement(inode_id):
            nonlocal replaced
            if not replaced:
                replaced = True
                fs("rename", "/target.txt", "/old-name.txt", 0)
                fs("rename", "/replacement.txt", "/target.txt", 0)
            return original_content_lock(inode_id)

        monkeypatch.setattr(
            fs_impl, "_content_lock", content_lock_after_path_replacement
        )
        opened = fs("open", "/target.txt", os.O_RDWR | os.O_TRUNC)
        fs("release", "/old-name.txt", opened)

        old_fh = fs("open", "/old-name.txt", os.O_RDONLY)
        new_fh = fs("open", "/target.txt", os.O_RDONLY)
        assert fs("read", "/old-name.txt", 3, 0, old_fh) == b""
        assert fs("read", "/target.txt", 3, 0, new_fh) == b"new"


def test_ztierfs_inline_block_hardlinks_share_inode_content_and_xattrs(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/file.txt", 0o644)
        fs("write", "/file.txt", b"one", 0, fh)
        fs("setxattr", "/file.txt", "user.note", b"shared", 0, 0)
        fs("link", "/alias.txt", "/file.txt")

        alias_fh = fs("open", "/alias.txt", os.O_RDWR)
        fs("write", "/alias.txt", b"two", 0, alias_fh)

        assert fs("read", "/file.txt", 3, 0, fh) == b"two"
        assert fs("getxattr", "/alias.txt", "user.note", 0) == b"shared"
        assert fs("getattr", "/file.txt")["st_nlink"] == 2

    linked_inodes = rows(
        fs_impl,
        "SELECT DISTINCT inode_id FROM dir_entries WHERE name IN ('file.txt', 'alias.txt')",
    )
    assert len(linked_inodes) == 1
    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM blocks")[0]["total"] == 1
    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM file_chunks")[0]["total"] == 1


def test_ztierfs_grows_inline_block_to_tiered_block_when_append_crosses_threshold(
    tmp_path,
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=16)
    expected = b"small" + b"x" * 20

    with adapted(fs_impl) as fs:
        fh = fs("create", "/grows.txt", 0o644)
        fs("write", "/grows.txt", b"small", 0, fh)
        fs("write", "/grows.txt", b"x" * 20, 5, fh)
        assert fs("read", "/grows.txt", len(expected), 0, fh) == expected

    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM file_chunks")[0]["total"] == 1


def test_ztierfs_grows_inline_block_to_tiered_block_when_truncate_grows_past_threshold(
    tmp_path,
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=8)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/sparse.txt", 0o644)
        fs("write", "/sparse.txt", b"abc", 0, fh)
        fs("truncate", "/sparse.txt", 12, fh)
        assert fs("read", "/sparse.txt", 12, 0, fh) == b"abc" + b"\x00" * 9

    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM file_chunks")[0]["total"] == 1


def test_ztierfs_clonefile_copies_inline_block_without_reprocessing_blocks(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/source.txt", 0o644)
        fs("write", "/source.txt", b"small", 0, fh)
        fs("setxattr", "/source.txt", "user.note", b"copied", 0, 0)

        def fail_prepare_blocks(*args, **kwargs):
            raise AssertionError("inline block clonefile must not reprocess blocks")

        monkeypatch.setattr(fs_impl.block_store, "prepare_blocks", fail_prepare_blocks)
        fs("clonefile", "/source.txt", "/copy.txt")

        copy_fh = fs("open", "/copy.txt", os.O_RDONLY)
        assert fs("read", "/copy.txt", 5, 0, copy_fh) == b"small"
        assert fs("getxattr", "/copy.txt", "user.note", 0) == b"copied"

    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM file_chunks")[0]["total"] == 2
    assert rows(fs_impl, "SELECT COUNT(*) AS total FROM blocks")[0]["total"] == 1
    assert rows(fs_impl, "SELECT refcount FROM blocks")[0]["refcount"] == 2


def test_ztierfs_internal_write_merge_does_not_flush_read_stats(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"abcdef" * 200

    with adapted(fs_impl) as fs:
        fh = fs("create", "/chunked.jpg", 0o644)
        fs("write", "/chunked.jpg", data, 0, fh)
        fs("write", "/chunked.jpg", b"PATCH", 10, fh)

    reads = rows(fs_impl, "SELECT COALESCE(SUM(read_count), 0) AS reads FROM blocks")[0]
    assert reads["reads"] == 0


def test_ztierfs_reuses_decoded_block_cache_for_repeated_reads(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"cached block" * 100

    with adapted(fs_impl) as fs:
        fh = fs("create", "/cached.jpg", 0o644)
        fs("write", "/cached.jpg", data, 0, fh)
        assert fs("read", "/cached.jpg", len(data), 0, fh) == data

        def fail_disk_read(_path):
            raise AssertionError(
                "second read should be served from decoded block cache"
            )

        monkeypatch.setattr("ztierfs.block_store.read_path_bytes", fail_disk_read)
        assert fs("read", "/cached.jpg", len(data), 0, fh) == data


def test_ztierfs_updates_refcounts_when_overwriting_and_unlinking(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    shared = b"same-block" * 100
    replacement = b"different!" * 100

    with adapted(fs_impl) as fs:
        first = fs("create", "/first.txt", 0o644)
        second = fs("create", "/second.txt", 0o644)
        fs("write", "/first.txt", shared, 0, first)
        fs("write", "/second.txt", shared, 0, second)
        fs("write", "/first.txt", replacement, 0, first)

        block_rows = rows(fs_impl, "SELECT refcount FROM blocks ORDER BY refcount")
        assert [row["refcount"] for row in block_rows] == [1, 1]

        fs("unlink", "/second.txt")
        fs("release", "/second.txt", second)

    block_rows = rows(fs_impl, "SELECT raw_size, refcount FROM blocks")
    assert len(block_rows) == 1
    assert block_rows[0]["raw_size"] == len(replacement)
    assert block_rows[0]["refcount"] == 1


def test_ztierfs_partial_overwrite_of_deduped_chunk_keeps_other_file_intact(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    shared = bytes(range(256)) * 4
    patch = b"changed"
    expected = shared[:100] + patch + shared[100 + len(patch) :]

    with adapted(fs_impl) as fs:
        first = fs("create", "/first.jpg", 0o644)
        second = fs("create", "/second.jpg", 0o644)
        fs("write", "/first.jpg", shared, 0, first)
        fs("write", "/second.jpg", shared, 0, second)

        fs("write", "/first.jpg", patch, 100, first)

        assert fs("read", "/first.jpg", len(shared), 0, first) == expected
        assert fs("read", "/second.jpg", len(shared), 0, second) == shared

    block_rows = rows(fs_impl, "SELECT refcount FROM blocks ORDER BY refcount")
    assert [row["refcount"] for row in block_rows] == [1, 1]


def test_ztierfs_defers_read_access_stats_until_flush(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"read stats" * 100

    with adapted(fs_impl) as fs:
        fh = fs("create", "/stats.jpg", 0o644)
        fs("write", "/stats.jpg", data, 0, fh)

        assert fs("read", "/stats.jpg", len(data), 0, fh) == data
        assert (
            rows(fs_impl, "SELECT SUM(read_count) AS reads FROM blocks")[0]["reads"]
            == 0
        )

        fs("flush", "/stats.jpg", fh)
        assert (
            rows(fs_impl, "SELECT SUM(read_count) AS reads FROM blocks")[0]["reads"]
            == 1
        )


def test_file_content_read_file_batches_access_stats_until_commit(tmp_path):
    fs_impl = make_fs(tmp_path, chunk_size=1024, inline_max_bytes=0)
    data = bytes(range(256)) * 8

    with adapted(fs_impl) as fs:
        fh = fs("create", "/stats.jpg", 0o644)
        fs("write", "/stats.jpg", data, 0, fh)
        with fs_impl.metadata.read_transaction():
            node = fs_impl.metadata.get_node("/stats.jpg")

        assert fs_impl.file_content.read_file(node, len(data), 0) == data
        assert (
            rows(fs_impl, "SELECT COALESCE(SUM(read_count), 0) AS reads FROM blocks")[
                0
            ]["reads"]
            == 0
        )

        fs("flush", "/stats.jpg", fh)

    assert (
        rows(fs_impl, "SELECT COALESCE(SUM(read_count), 0) AS reads FROM blocks")[0][
            "reads"
        ]
        == 2
    )


def test_ztierfs_reads_small_multi_chunk_plan_synchronously(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = bytes(range(256)) * 8
    lock = threading.Lock()
    active = 0
    max_active = 0

    with adapted(fs_impl) as fs:
        fh = fs("create", "/movie.jpg", 0o644)
        fs("write", "/movie.jpg", data, 0, fh)
        original = fs_impl.block_store.read_block_snapshot

        def observed_read_block(row, expected_size):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                return original(row, expected_size)
            finally:
                with lock:
                    active -= 1

        monkeypatch.setattr(
            fs_impl.block_store, "read_block_snapshot", observed_read_block
        )
        assert fs("read", "/movie.jpg", len(data), 0, fh) == data

    assert max_active == 1


def test_ztierfs_reads_large_multi_chunk_plan_in_parallel(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, chunk_size=128 * 1024, inline_max_bytes=0)
    data = b"".join(bytes([index]) * fs_impl.chunk_size for index in range(4))
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    active = 0
    max_active = 0

    with adapted(fs_impl) as fs:
        fh = fs("create", "/movie.jpg", 0o644)
        fs("write", "/movie.jpg", data, 0, fh)
        original = fs_impl.block_store.read_block_snapshot

        def observed_read_block(row, expected_size):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                barrier.wait(timeout=2)
                return original(row, expected_size)
            finally:
                with lock:
                    active -= 1

        monkeypatch.setattr(
            fs_impl.block_store, "read_block_snapshot", observed_read_block
        )
        assert fs("read", "/movie.jpg", len(data), 0, fh) == data

    assert max_active > 1


def test_ztierfs_prepares_uncompressed_multi_chunk_writes_in_parallel(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = bytes(range(256)) * 8
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    max_active = 0

    original_digest = fs_impl.block_store._timed_digest_block

    def observed_digest(chunk):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait(timeout=2)
            return original_digest(chunk)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(fs_impl.block_store, "_timed_digest_block", observed_digest)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/movie.jpg", 0o644)
        assert fs("write", "/movie.jpg", data, 0, fh) == len(data)

    assert max_active == 2


def test_ztierfs_cold_copy_up_does_not_block_read(tmp_path, monkeypatch):
    fs_impl = make_fs(
        tmp_path,
        hot_cache_max_bytes=1500,
        hot_cache_min_bytes=1024,
        protected_prefix_chunks=0,
        min_hot_age_seconds=0,
        inline_max_bytes=0,
    )
    cold_data = bytes(range(256)) * 4
    hot_data = bytes(reversed(range(256))) * 4
    copy_started = threading.Event()
    allow_copy = threading.Event()

    with adapted(fs_impl) as fs:
        cold_fh = fs("create", "/cold.jpg", 0o644)
        hot_fh = fs("create", "/hot.jpg", 0o644)
        fs("write", "/cold.jpg", cold_data, 0, cold_fh)
        fs("write", "/hot.jpg", hot_data, 0, hot_fh)
        assert rows(
            fs_impl,
            "SELECT COUNT(*) AS total FROM block_records WHERE cold_present = 1",
        )[0]["total"]

        original_copy_block = fs_impl.block_store.copy_block

        def gated_copy_block(digest, source_tier, target_tier):
            copy_started.set()
            assert allow_copy.wait(timeout=2)
            return original_copy_block(digest, source_tier, target_tier)

        monkeypatch.setattr(fs_impl.block_store, "copy_block", gated_copy_block)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                fs, "read", "/cold.jpg", len(cold_data), 0, cold_fh
            )
            try:
                assert future.result(timeout=1) == cold_data
                assert copy_started.wait(timeout=1)
                assert (
                    rows(
                        fs_impl,
                        "SELECT COUNT(*) AS total FROM block_records WHERE hot_present = 1 AND cold_present = 1",
                    )[0]["total"]
                    == 0
                )
            finally:
                allow_copy.set()

        fs("release", "/cold.jpg", cold_fh)
        fs("release", "/hot.jpg", hot_fh)


def test_ztierfs_moves_least_recently_used_blocks_to_cold_tier(tmp_path):
    fs_impl = make_fs(
        tmp_path,
        hot_cache_max_bytes=1500,
        hot_cache_min_bytes=1024,
        protected_prefix_chunks=0,
        min_hot_age_seconds=0,
        inline_max_bytes=0,
    )
    first = bytes(range(256)) * 4
    second = bytes(reversed(range(256))) * 4

    with adapted(fs_impl) as fs:
        first_fh = fs("create", "/first.jpg", 0o644)
        second_fh = fs("create", "/second.jpg", 0o644)
        fs("write", "/first.jpg", first, 0, first_fh)
        fs("write", "/second.jpg", second, 0, second_fh)

        presence = rows(fs_impl, "SELECT hot_present, cold_present FROM block_records")
        assert sorted(
            (row["hot_present"], row["cold_present"]) for row in presence
        ) == [
            (0, 1),
            (1, 0),
        ]
        assert any((tmp_path / "cold" / "blocks").glob("*/*/*"))

        assert fs("read", "/first.jpg", len(first), 0, first_fh) == first

        def tier_counts_ready():
            presence = rows(
                fs_impl, "SELECT hot_present, cold_present FROM block_records"
            )
            return (
                sum(row["hot_present"] for row in presence) == 1
                and sum(row["cold_present"] for row in presence) == 2
            )

        _wait_until(tier_counts_ready)
        presence = rows(fs_impl, "SELECT hot_present, cold_present FROM block_records")
        assert sum(row["hot_present"] for row in presence) == 1
        assert sum(row["cold_present"] for row in presence) == 2


def test_ztierfs_write_only_runs_budgeted_after_commit_demote(tmp_path):
    fs_impl = make_fs(
        tmp_path,
        hot_cache_max_bytes=1,
        hot_cache_min_bytes=0,
        protected_prefix_chunks=0,
        min_hot_age_seconds=0,
        inline_max_bytes=0,
    )
    data = b"".join(bytes([index]) * fs_impl.chunk_size for index in range(3))

    with adapted(fs_impl) as fs:
        fh = fs("create", "/large.jpg", 0o644)
        fs("write", "/large.jpg", data, 0, fh)

        presence = rows(fs_impl, "SELECT hot_present, cold_present FROM block_records")
        assert sum(row["hot_present"] for row in presence) == 2
        assert sum(row["cold_present"] for row in presence) == 1

        fs_impl.metadata._after_commit_hooks.clear()
        assert fs_impl.block_store.drain_requested_demotions(max_blocks=1) == 1
        presence = rows(fs_impl, "SELECT hot_present, cold_present FROM block_records")
        assert sum(row["hot_present"] for row in presence) == 1
        assert sum(row["cold_present"] for row in presence) == 2
        fs_impl.block_store.drain_requested_demotions()
        presence = rows(fs_impl, "SELECT hot_present, cold_present FROM block_records")
        assert sum(row["hot_present"] for row in presence) == 0
        assert sum(row["cold_present"] for row in presence) == 3


def test_ztierfs_keeps_file_prefix_chunks_hot_during_demote(tmp_path):
    fs_impl = make_fs(
        tmp_path,
        hot_cache_max_bytes=1500,
        hot_cache_min_bytes=1024,
        protected_prefix_chunks=1,
        min_hot_age_seconds=0,
        inline_max_bytes=0,
    )
    data = bytes(range(256)) * 4 + bytes(reversed(range(256))) * 4

    with adapted(fs_impl) as fs:
        fh = fs("create", "/movie.jpg", 0o644)
        fs("write", "/movie.jpg", data, 0, fh)

        chunk_rows = rows(
            fs_impl,
            """
            SELECT file_chunks.chunk_index, block_records.hot_present, block_records.cold_present
            FROM file_chunks
            JOIN block_records ON block_records.hash = file_chunks.hash
            ORDER BY file_chunks.chunk_index
            """,
        )
        assert [(row["hot_present"], row["cold_present"]) for row in chunk_rows] == [
            (1, 0),
            (0, 1),
        ]


def test_ztierfs_recovers_when_block_metadata_points_to_missing_tier(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"fallback block" * 80

    with adapted(fs_impl) as fs:
        fh = fs("create", "/file.bin", 0o644)
        fs("write", "/file.bin", data, 0, fh)
        fs("flush", "/file.bin", fh)

        digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
        with connect_sqlite(fs_impl.database) as db:
            db.execute(
                """
                UPDATE blocks
                SET preferred_tier = 2, hot_present = 0, cold_present = 1
                WHERE hash = ?
                """,
                (digest,),
            )
        fs_impl.block_store._read_cache.clear()
        fs_impl.block_store._read_cache_size = 0

        assert fs("read", "/file.bin", len(data), 0, fh) == data
        with connect_sqlite(fs_impl.database) as db:
            row = db.execute(
                """
                SELECT hot_present, cold_present, preferred_tier
                FROM block_records
                WHERE hash = ?
                """,
                (digest,),
            ).fetchone()
            assert row == (1, 0, 1)


def test_ztierfs_normal_hot_read_does_not_probe_cold_tier(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"hot block" * 128

    import ztierfs.block_store as block_store_module

    original_probe_path = block_store_module.probe_path
    original_read_path_bytes = block_store_module.read_path_bytes

    with adapted(fs_impl) as fs:
        fh = fs("create", "/hot.jpg", 0o644)
        fs("write", "/hot.jpg", data, 0, fh)
        digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
        hot_path = fs_impl.block_store.block_path(digest, 1)
        cold_path = fs_impl.block_store.block_path(digest, 2)
        fs_impl.block_store._read_cache.clear()
        fs_impl.block_store._read_cache_size = 0

        def fail_cold_probe(path):
            if path == cold_path:
                raise AssertionError("normal hot read must not probe cold tier")
            return original_probe_path(path)

        monkeypatch.setattr(block_store_module, "probe_path", fail_cold_probe)

        def read_or_fail(path):
            if path == cold_path:
                raise AssertionError("normal hot read must not read cold tier")
            return original_read_path_bytes(path)

        monkeypatch.setattr(block_store_module, "read_path_bytes", read_or_fail)

        assert fs("read", "/hot.jpg", len(data), 0, fh) == data

        assert hot_path.exists()


def test_ztierfs_reads_hot_fallback_when_cold_preferred_unavailable(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"dual tier" * 128

    with adapted(fs_impl) as fs:
        fh = fs("create", "/dual.jpg", 0o644)
        fs("write", "/dual.jpg", data, 0, fh)
        digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
        hot_path = fs_impl.block_store.block_path(digest, 1)
        cold_path = fs_impl.block_store.block_path(digest, 2)
        cold_path.parent.mkdir(parents=True, exist_ok=True)
        cold_path.write_bytes(hot_path.read_bytes())
        with connect_sqlite(fs_impl.database) as db:
            db.execute(
                """
                UPDATE blocks
                SET preferred_tier = 2, cold_present = 1
                WHERE hash = ?
                """,
                (digest,),
            )
        fs_impl.block_store._read_cache.clear()
        fs_impl.block_store._read_cache_size = 0

        from ztierfs.tier_access import PathUnavailable

        import ztierfs.block_store as block_store_module

        original_read_path_bytes = block_store_module.read_path_bytes

        def cold_unavailable_read(path):
            if path == cold_path:
                raise PathUnavailable(path, OSError(5, "cold unavailable"))
            return original_read_path_bytes(path)

        monkeypatch.setattr(
            block_store_module, "read_path_bytes", cold_unavailable_read
        )

        assert fs("read", "/dual.jpg", len(data), 0, fh) == data

    row = rows(
        fs_impl,
        "SELECT hot_present, cold_present, preferred_tier FROM block_records",
    )[0]
    assert (row["hot_present"], row["cold_present"], row["preferred_tier"]) == (1, 1, 2)
