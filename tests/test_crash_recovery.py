import errno
import os

import pytest
from macfusepy import FuseOSError

from ztierfs.block_store import TieringPolicy
from ztierfs.maintenance import block_path, cleanup_promoted_cold_copies, run_fsck

from .helpers import (
    TestOperationsAdapter as OperationsAdapter,
    adapted,
    connect_sqlite,
    make_fs,
    rows,
)


class SimulatedCrash(RuntimeError):
    pass


ENOATTR = getattr(errno, "ENOATTR", getattr(errno, "ENODATA", errno.ENOENT))


def _single_block_digest(fs_impl):
    return rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]


def _write_committed_file(fs_impl, path="/file.jpg", data=None):
    data = data if data is not None else bytes(range(256)) * 4
    fs = OperationsAdapter(fs_impl)
    fh = fs("create", path, 0o644)
    fs("write", path, data, 0, fh)
    fs("release", path, fh)
    return data


def _assert_fsck_ok(fs_impl):
    report = run_fsck(fs_impl.database, repair=True)
    assert report.issues == []


def test_write_crash_after_block_file_before_metadata_commit_is_repairable_orphan(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    original_insert_blocks = fs_impl.metadata.insert_blocks

    def insert_blocks_then_crash(*args, **kwargs):
        original_insert_blocks(*args, **kwargs)
        raise SimulatedCrash(
            "crash after block metadata insert before transaction commit"
        )

    monkeypatch.setattr(fs_impl.metadata, "insert_blocks", insert_blocks_then_crash)

    with pytest.raises(SimulatedCrash):
        with adapted(fs_impl) as fs:
            fh = fs("create", "/new.jpg", 0o644)
            fs("write", "/new.jpg", bytes(range(256)) * 4, 0, fh)

    assert rows(fs_impl, "SELECT * FROM blocks") == []
    assert any((tmp_path / "hot" / "blocks").glob("*/*/*"))

    report = run_fsck(fs_impl.database, repair=True)

    assert [issue.code for issue in report.issues] == ["orphan_block_file"]
    assert report.issues[0].repaired
    assert not any((tmp_path / "hot" / "blocks").glob("*/*/*"))


def test_demote_crash_after_hot_unlink_before_metadata_commit_is_repaired(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(
        tmp_path,
        hot_cache_max_bytes=0,
        hot_cache_min_bytes=0,
        protected_prefix_chunks=0,
        inline_max_bytes=0,
    )
    data = _write_committed_file(fs_impl)
    digest = _single_block_digest(fs_impl)

    fs_impl.block_store.policy = TieringPolicy(
        hot_max_bytes=1,
        hot_min_bytes=0,
        protected_prefix_chunks=0,
        min_hot_age_ns=0,
    )
    original_set_block_presence = fs_impl.metadata.set_block_presence

    def crash_before_recording_demote(digest, **kwargs):
        if kwargs.get("hot_present") is False and kwargs.get("cold_present") is True:
            raise SimulatedCrash("crash after hot block unlink before metadata commit")
        original_set_block_presence(digest, **kwargs)

    monkeypatch.setattr(
        fs_impl.metadata, "set_block_presence", crash_before_recording_demote
    )

    with pytest.raises(SimulatedCrash):
        with fs_impl.metadata.transaction():
            fs_impl.block_store.demote_cold_blocks()
    fs_impl.close()

    assert not block_path(fs_impl.tier1, fs_impl.tier2, digest, 1).exists()
    assert block_path(fs_impl.tier1, fs_impl.tier2, digest, 2).exists()

    report = run_fsck(fs_impl.database, repair=True)

    assert {issue.code for issue in report.issues} == {
        "block_presence_mismatch",
        "preferred_tier_missing",
    }
    assert all(issue.repaired for issue in report.issues)

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(reopened) as fs:
        fh = fs("open", "/file.jpg", os.O_RDONLY)
        assert fs("read", "/file.jpg", len(data), 0, fh) == data


@pytest.mark.parametrize(
    "operation", ["unlink", "truncate", "overwrite", "rename_overwrite"]
)
def test_crash_after_block_delete_before_metadata_commit_reports_missing_referenced_block(
    tmp_path,
    monkeypatch,
    operation,
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    original_data = _write_committed_file(fs_impl, "/victim.jpg")
    digest = _single_block_digest(fs_impl)
    original_delete_block = fs_impl.metadata.delete_block

    def delete_block_then_crash(*args, **kwargs):
        original_delete_block(*args, **kwargs)
        raise SimulatedCrash("crash after block tombstone before transaction commit")

    monkeypatch.setattr(fs_impl.metadata, "delete_block", delete_block_then_crash)

    with pytest.raises(SimulatedCrash):
        with adapted(fs_impl) as fs:
            if operation == "unlink":
                fs("unlink", "/victim.jpg")
            elif operation == "truncate":
                fs("truncate", "/victim.jpg", 0, None)
            elif operation == "overwrite":
                fh = fs("open", "/victim.jpg", os.O_RDWR)
                fs("write", "/victim.jpg", b"replacement" * 100, 0, fh)
            elif operation == "rename_overwrite":
                source = fs("create", "/source.jpg", 0o644)
                fs("write", "/source.jpg", b"source" * 200, 0, source)
                fs("release", "/source.jpg", source)
                fs("rename", "/source.jpg", "/victim.jpg", 0)

    assert block_path(fs_impl.tier1, fs_impl.tier2, digest, 1).exists()

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(reopened) as fs:
        fh = fs("open", "/victim.jpg", os.O_RDONLY)
        assert fs("read", "/victim.jpg", len(original_data), 0, fh) == original_data

    report = run_fsck(fs_impl.database, repair=True)

    assert not report.has_unrepaired


def test_hardlink_unlink_crash_after_entry_remove_rolls_back_metadata(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = bytes(range(256)) * 4
    original_remove_entry = fs_impl.metadata.remove_entry

    def remove_entry_then_crash(*args, **kwargs):
        original_remove_entry(*args, **kwargs)
        raise SimulatedCrash("crash after hardlink dir entry removal before commit")

    with adapted(fs_impl) as fs:
        fh = fs("create", "/original.jpg", 0o644)
        fs("write", "/original.jpg", data, 0, fh)
        fs("link", "/linked.jpg", "/original.jpg")
        fs("setxattr", "/original.jpg", "user.note", b"shared", 0, 0)

        monkeypatch.setattr(fs_impl.metadata, "remove_entry", remove_entry_then_crash)
        with pytest.raises(SimulatedCrash):
            fs("unlink", "/linked.jpg")

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(reopened) as fs:
        assert fs("getattr", "/original.jpg")["st_nlink"] == 2
        assert fs("getattr", "/linked.jpg")["st_nlink"] == 2
        fh = fs("open", "/linked.jpg", os.O_RDONLY)
        assert fs("read", "/linked.jpg", len(data), 0, fh) == data
        assert fs("getxattr", "/linked.jpg", "user.note", 0) == b"shared"

    _assert_fsck_ok(fs_impl)


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("set", None),
        ("replace", b"old"),
        ("remove", b"old"),
    ],
)
def test_xattr_crash_before_commit_preserves_inode_xattrs(
    tmp_path,
    monkeypatch,
    operation,
    expected,
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/file.jpg", 0o644)
        fs("write", "/file.jpg", b"xattr-data" * 100, 0, fh)
        fs("link", "/alias.jpg", "/file.jpg")
        if operation in {"replace", "remove"}:
            fs("setxattr", "/file.jpg", "user.note", b"old", 0, 0)

        if operation in {"set", "replace"}:
            original_set_xattr = fs_impl.metadata.set_xattr

            def set_xattr_then_crash(*args, **kwargs):
                original_set_xattr(*args, **kwargs)
                raise SimulatedCrash("crash after xattr update before commit")

            monkeypatch.setattr(fs_impl.metadata, "set_xattr", set_xattr_then_crash)
            with pytest.raises(SimulatedCrash):
                fs("setxattr", "/alias.jpg", "user.note", b"new", 0, 0)
        else:
            original_remove_xattr = fs_impl.metadata.remove_xattr

            def remove_xattr_then_crash(*args, **kwargs):
                original_remove_xattr(*args, **kwargs)
                raise SimulatedCrash("crash after xattr removal before commit")

            monkeypatch.setattr(
                fs_impl.metadata, "remove_xattr", remove_xattr_then_crash
            )
            with pytest.raises(SimulatedCrash):
                fs("removexattr", "/alias.jpg", "user.note")

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(reopened) as fs:
        assert fs("listxattr", "/file.jpg") == (
            [] if expected is None else ["user.note"]
        )
        if expected is None:
            with pytest.raises(FuseOSError) as exc:
                fs("getxattr", "/alias.jpg", "user.note", 0)
            assert exc.value.errno == ENOATTR
        else:
            assert fs("getxattr", "/alias.jpg", "user.note", 0) == expected

    _assert_fsck_ok(fs_impl)


def test_complex_directory_rename_crash_after_target_remove_rolls_back_tree(
    tmp_path,
    monkeypatch,
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = b"nested-data" * 100
    original_move_entry = fs_impl.metadata.move_entry

    def move_entry_then_crash(*args, **kwargs):
        original_move_entry(*args, **kwargs)
        raise SimulatedCrash("crash after directory move before commit")

    with adapted(fs_impl) as fs:
        fs("mkdir", "/src", 0o755)
        fs("mkdir", "/src/a", 0o755)
        fs("mkdir", "/dst", 0o755)
        fs("mkdir", "/dst/empty", 0o755)
        fh = fs("create", "/src/a/file.jpg", 0o644)
        fs("write", "/src/a/file.jpg", data, 0, fh)
        fs("symlink", "/src/a/link", "../a/file.jpg")

        monkeypatch.setattr(fs_impl.metadata, "move_entry", move_entry_then_crash)
        with pytest.raises(SimulatedCrash):
            fs("rename", "/src/a", "/dst/empty", 0)

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(reopened) as fs:
        assert sorted(fs("readdir", "/src", None)) == [".", "..", "a"]
        assert sorted(fs("readdir", "/dst", None)) == [".", "..", "empty"]
        fh = fs("open", "/src/a/file.jpg", os.O_RDONLY)
        assert fs("read", "/src/a/file.jpg", len(data), 0, fh) == data
        assert fs("readlink", "/src/a/link") == "../a/file.jpg"

    _assert_fsck_ok(fs_impl)


def test_cross_block_overwrite_crash_leaves_old_file_and_repairable_orphans(
    tmp_path,
    monkeypatch,
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    original = b"A" * 3072
    replacement = bytes(range(256)) * 8
    original_replace = fs_impl.metadata.replace_file_chunks

    def replace_chunks_then_crash(*args, **kwargs):
        original_replace(*args, **kwargs)
        raise SimulatedCrash("crash after multi-chunk overwrite metadata update")

    with adapted(fs_impl) as fs:
        fh = fs("create", "/movie.jpg", 0o644)
        fs("write", "/movie.jpg", original, 0, fh)

        monkeypatch.setattr(
            fs_impl.metadata,
            "replace_file_chunks",
            replace_chunks_then_crash,
        )
        with pytest.raises(SimulatedCrash):
            fs("write", "/movie.jpg", replacement, 512, fh)

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(reopened) as fs:
        fh = fs("open", "/movie.jpg", os.O_RDONLY)
        assert fs("read", "/movie.jpg", len(original), 0, fh) == original

    report = run_fsck(fs_impl.database, repair=True)
    assert {issue.code for issue in report.issues} == {"orphan_block_file"}
    assert all(issue.repaired for issue in report.issues)


def test_copy_up_crash_after_hot_copy_before_metadata_commit_is_repaired(
    tmp_path,
    monkeypatch,
):
    fs_impl = make_fs(
        tmp_path,
        hot_cache_max_bytes=0,
        hot_cache_min_bytes=0,
        protected_prefix_chunks=0,
        inline_max_bytes=0,
    )
    data = _write_committed_file(fs_impl)
    digest = _single_block_digest(fs_impl)
    fs_impl.block_store.policy = TieringPolicy(
        hot_max_bytes=1,
        hot_min_bytes=0,
        protected_prefix_chunks=0,
        min_hot_age_ns=0,
    )
    with fs_impl.metadata.transaction():
        fs_impl.block_store.demote_cold_blocks()
    assert not block_path(fs_impl.tier1, fs_impl.tier2, digest, 1).exists()
    assert block_path(fs_impl.tier1, fs_impl.tier2, digest, 2).exists()
    fs_impl.block_store.policy = TieringPolicy(
        hot_max_bytes=10_000,
        hot_min_bytes=0,
        protected_prefix_chunks=0,
        min_hot_age_ns=0,
    )

    original_set_block_presence = fs_impl.metadata.set_block_presence

    def crash_after_copy_up_before_presence_commit(digest, **kwargs):
        if kwargs.get("hot_present") is True and kwargs.get("preferred_tier") == 1:
            raise SimulatedCrash("crash after copy-up hot file before metadata commit")
        original_set_block_presence(digest, **kwargs)

    monkeypatch.setattr(
        fs_impl.metadata,
        "set_block_presence",
        crash_after_copy_up_before_presence_commit,
    )

    with adapted(fs_impl) as fs:
        fh = fs("open", "/file.jpg", os.O_RDONLY)
        assert fs("read", "/file.jpg", len(data), 0, fh) == data

    assert block_path(fs_impl.tier1, fs_impl.tier2, digest, 1).exists()
    assert block_path(fs_impl.tier1, fs_impl.tier2, digest, 2).exists()

    report = run_fsck(fs_impl.database, repair=True)
    assert [issue.code for issue in report.issues] == ["block_presence_mismatch"]
    assert report.issues[0].repaired

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(reopened) as fs:
        fh = fs("open", "/file.jpg", os.O_RDONLY)
        assert fs("read", "/file.jpg", len(data), 0, fh) == data


def test_cleanup_crash_after_cold_unlink_before_metadata_commit_is_repaired(
    tmp_path,
    monkeypatch,
):
    fs_impl = make_fs(
        tmp_path,
        hot_cache_max_bytes=0,
        hot_cache_min_bytes=0,
        protected_prefix_chunks=0,
        inline_max_bytes=0,
    )
    data = _write_committed_file(fs_impl)
    digest = _single_block_digest(fs_impl)
    fs_impl.block_store.policy = TieringPolicy(
        hot_max_bytes=1,
        hot_min_bytes=0,
        protected_prefix_chunks=0,
        min_hot_age_ns=0,
        cold_copy_cleanup_age_ns=1,
    )
    with fs_impl.metadata.transaction():
        fs_impl.block_store.demote_cold_blocks()
    fs_impl.block_store.policy = TieringPolicy(
        hot_max_bytes=10_000,
        hot_min_bytes=0,
        protected_prefix_chunks=0,
        min_hot_age_ns=0,
        cold_copy_cleanup_age_ns=1,
    )
    with adapted(fs_impl) as fs:
        fh = fs("open", "/file.jpg", os.O_RDONLY)
        assert fs("read", "/file.jpg", len(data), 0, fh) == data

    with connect_sqlite(fs_impl.database) as db:
        db.execute("UPDATE blocks SET last_promoted_ns = 0 WHERE hash = ?", (digest,))

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    original_set_block_presence = reopened.metadata.set_block_presence

    def crash_after_cleanup_unlinks_cold_copy(digest, **kwargs):
        if kwargs.get("cold_present") is False:
            raise SimulatedCrash(
                "crash after cleanup cold unlink before metadata commit"
            )
        original_set_block_presence(digest, **kwargs)

    monkeypatch.setattr(
        reopened.metadata,
        "set_block_presence",
        crash_after_cleanup_unlinks_cold_copy,
    )

    with pytest.raises(SimulatedCrash):
        with reopened.metadata.transaction():
            reopened.block_store.policy = TieringPolicy(
                hot_max_bytes=1,
                hot_min_bytes=0,
                protected_prefix_chunks=0,
                cold_copy_cleanup_age_ns=1,
            )
            reopened.block_store.cleanup_promoted_cold_copies()
    reopened.close()

    assert block_path(fs_impl.tier1, fs_impl.tier2, digest, 1).exists()
    assert not block_path(fs_impl.tier1, fs_impl.tier2, digest, 2).exists()

    report = run_fsck(fs_impl.database, repair=True)
    assert [issue.code for issue in report.issues] == ["block_presence_mismatch"]
    assert report.issues[0].repaired

    final = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(final) as fs:
        fh = fs("open", "/file.jpg", os.O_RDONLY)
        assert fs("read", "/file.jpg", len(data), 0, fh) == data


def test_refcount_decrement_crash_before_commit_rolls_back_shared_block(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    shared = bytes(range(256)) * 4
    original_apply_deltas = fs_impl.metadata.apply_block_refcount_deltas

    def apply_deltas_then_crash(deltas, *args, **kwargs):
        if any(delta < 0 for delta in deltas.values()):
            original_apply_deltas(deltas, *args, **kwargs)
            raise SimulatedCrash("crash after refcount decrement before commit")
        return original_apply_deltas(deltas, *args, **kwargs)

    with adapted(fs_impl) as fs:
        first = fs("create", "/first.jpg", 0o644)
        second = fs("create", "/second.jpg", 0o644)
        fs("write", "/first.jpg", shared, 0, first)
        fs("write", "/second.jpg", shared, 0, second)
        fs("release", "/second.jpg", second)

        monkeypatch.setattr(
            fs_impl.metadata, "apply_block_refcount_deltas", apply_deltas_then_crash
        )
        with pytest.raises(SimulatedCrash):
            fs("unlink", "/second.jpg")

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(reopened) as fs:
        assert sorted(fs("readdir", "/", None)) == [
            ".",
            "..",
            ".Trashes",
            "first.jpg",
            "second.jpg",
        ]
        block_rows = rows(reopened, "SELECT refcount FROM blocks")
        assert [row["refcount"] for row in block_rows] == [2]

    _assert_fsck_ok(fs_impl)


def test_hardlink_last_unlink_crash_after_block_delete_reports_missing_block(
    tmp_path,
    monkeypatch,
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    data = bytes(range(256)) * 4
    original_delete_block = fs_impl.metadata.delete_block

    def delete_block_then_crash(*args, **kwargs):
        original_delete_block(*args, **kwargs)
        raise SimulatedCrash("crash after last hardlink block tombstone before commit")

    with adapted(fs_impl) as fs:
        fh = fs("create", "/original.jpg", 0o644)
        fs("write", "/original.jpg", data, 0, fh)
        fs("link", "/linked.jpg", "/original.jpg")
        fs("release", "/original.jpg", fh)
        fs("unlink", "/original.jpg")

        digest = _single_block_digest(fs_impl)
        monkeypatch.setattr(fs_impl.metadata, "delete_block", delete_block_then_crash)
        with pytest.raises(SimulatedCrash):
            fs("unlink", "/linked.jpg")

    assert block_path(fs_impl.tier1, fs_impl.tier2, digest, 1).exists()

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(reopened) as fs:
        fh = fs("open", "/linked.jpg", os.O_RDONLY)
        assert fs("read", "/linked.jpg", len(data), 0, fh) == data

    report = run_fsck(fs_impl.database, repair=True)
    assert report.ok


def test_last_unlink_commit_queues_block_delete_for_cleanup(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    _write_committed_file(fs_impl, "/victim.jpg")
    digest = _single_block_digest(fs_impl)
    fs_impl.metadata._after_commit_hooks.clear()
    monkeypatch.setattr(
        fs_impl.block_store, "drain_pending_deletions", lambda **_kwargs: 0
    )

    with adapted(fs_impl) as fs:
        fs("unlink", "/victim.jpg")

    path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 1)
    assert path.exists()
    pending = rows(fs_impl, "SELECT kind, digest, tier FROM pending_deletions")
    assert [(row["kind"], row["digest"], row["tier"]) for row in pending] == [
        ("block_file", digest, 1)
    ]

    report = cleanup_promoted_cold_copies(
        fs_impl.database,
        min_age_seconds=0,
    )

    assert report.pending_removed == 1
    assert not path.exists()
    assert rows(fs_impl, "SELECT * FROM pending_deletions") == []


def test_after_commit_gc_leaves_large_pending_delete_queue_for_cleanup(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0, chunk_size=1024)
    data = b"".join(bytes([index]) * 1024 for index in range(70))
    fs = OperationsAdapter(fs_impl)

    fh = fs("create", "/large.bin", 0o644)
    fs("write", "/large.bin", data, 0, fh)
    fs("release", "/large.bin", fh)
    fs("unlink", "/large.bin")

    pending = rows(fs_impl, "SELECT * FROM pending_deletions")
    assert len(pending) == 6

    report = cleanup_promoted_cold_copies(
        fs_impl.database,
        min_age_seconds=0,
    )

    assert report.pending_removed == 6
    assert rows(fs_impl, "SELECT * FROM pending_deletions") == []


def test_after_commit_gc_skips_cold_pending_delete_queue(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    _write_committed_file(fs_impl, "/victim.jpg")
    digest = _single_block_digest(fs_impl)
    hot_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 1)
    cold_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 2)
    cold_path.parent.mkdir(parents=True, exist_ok=True)
    cold_path.write_bytes(hot_path.read_bytes())

    fs = OperationsAdapter(fs_impl)
    with connect_sqlite(fs_impl.database) as db:
        db.execute(
            "UPDATE blocks SET cold_present = 1 WHERE hash = ?",
            (digest,),
        )
    fs("unlink", "/victim.jpg")

    assert not hot_path.exists()
    assert cold_path.exists()
    garbage = rows(
        fs_impl,
        f"""
        SELECT refcount, hot_present, cold_present, preferred_tier, cold_gc_enqueued_ns
        FROM blocks
        WHERE hash = '{digest}'
        """,
    )
    assert len(garbage) == 1
    assert (
        garbage[0]["refcount"],
        garbage[0]["hot_present"],
        garbage[0]["cold_present"],
        garbage[0]["preferred_tier"],
    ) == (0, 0, 1, 2)
    assert garbage[0]["cold_gc_enqueued_ns"] is not None
    assert rows(fs_impl, "SELECT * FROM pending_deletions") == []


def test_block_store_close_skips_cold_pending_deletions(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    _write_committed_file(fs_impl, "/victim.jpg")
    digest = _single_block_digest(fs_impl)
    hot_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 1)
    cold_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 2)
    cold_path.parent.mkdir(parents=True, exist_ok=True)
    cold_path.write_bytes(hot_path.read_bytes())
    fs_impl.metadata._after_commit_hooks.clear()
    with connect_sqlite(fs_impl.database) as db:
        db.execute(
            "UPDATE blocks SET cold_present = 1 WHERE hash = ?",
            (digest,),
        )
    OperationsAdapter(fs_impl)("unlink", "/victim.jpg")

    fs_impl.close()

    assert not hot_path.exists()
    assert cold_path.exists()
    garbage = rows(
        fs_impl,
        f"""
        SELECT refcount, hot_present, cold_present, preferred_tier, cold_gc_enqueued_ns
        FROM blocks
        WHERE hash = '{digest}'
        """,
    )
    assert len(garbage) == 1
    assert (
        garbage[0]["refcount"],
        garbage[0]["hot_present"],
        garbage[0]["cold_present"],
        garbage[0]["preferred_tier"],
    ) == (0, 0, 1, 2)
    assert garbage[0]["cold_gc_enqueued_ns"] is not None
    assert rows(fs_impl, "SELECT * FROM pending_deletions") == []


def test_pending_delete_drain_batches_queue_updates(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0, chunk_size=1024)
    data = b"".join(bytes([index]) * 1024 for index in range(3))
    fs_impl.metadata._after_commit_hooks.clear()

    fs = OperationsAdapter(fs_impl)
    fh = fs("create", "/large.bin", 0o644)
    fs("write", "/large.bin", data, 0, fh)
    fs("release", "/large.bin", fh)
    fs("unlink", "/large.bin")

    pending = rows(fs_impl, "SELECT * FROM pending_deletions")
    assert len(pending) == 3
    block_path(fs_impl.tier1, fs_impl.tier2, pending[0]["digest"], 1).unlink()

    write_transactions = 0
    original_transaction = fs_impl.metadata.transaction

    def observed_transaction():
        nonlocal write_transactions
        write_transactions += 1
        return original_transaction()

    monkeypatch.setattr(fs_impl.metadata, "transaction", observed_transaction)

    assert fs_impl.block_store.drain_pending_deletions(batch_size=3) == 3

    assert write_transactions == 1
    assert rows(fs_impl, "SELECT * FROM pending_deletions") == []
