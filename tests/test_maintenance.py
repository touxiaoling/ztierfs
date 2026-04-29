import errno
import sqlite3

from pathlib import Path
from time import time_ns

import pytest
from macfusepy import FuseOSError

from ztierfs.maintenance import (
    block_path,
    cleanup_promoted_cold_copies,
    collect_stats,
    run_fsck,
    run_scrub,
)
from ztierfs.tier_access import PathUnavailable

from .helpers import adapted, make_fs, rows


def _make_cold_only_block(fs_impl, data: bytes = b"hello") -> tuple[str, bytes, Path]:
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", data, 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    hot_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 1)
    cold_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 2)
    cold_path.parent.mkdir(parents=True, exist_ok=True)
    hot_path.replace(cold_path)
    with sqlite3.connect(fs_impl.database) as db:
        db.execute("DELETE FROM block_locations WHERE hash = ? AND tier = 1", (digest,))
        db.execute(
            "INSERT OR IGNORE INTO block_locations (hash, tier) VALUES (?, 2)",
            (digest,),
        )
        db.execute("UPDATE blocks SET preferred_tier = 2 WHERE hash = ?", (digest,))
    return digest, data, cold_path


def _make_path_stat_unavailable(monkeypatch, unavailable_path: Path) -> None:
    original_stat = Path.stat

    def stat_with_unavailable(path, *args, **kwargs):
        if path == unavailable_path:
            raise OSError(errno.EIO, "rclone cold tier unavailable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_unavailable)


def test_fsck_reports_clean_filesystem(tmp_path):
    fs_impl = make_fs(tmp_path)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database)
    assert report.ok
    assert report.issues == []


def test_fsck_can_resolve_storage_paths_from_database(tmp_path):
    fs_impl = make_fs(tmp_path)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    report = run_fsck(fs_impl.database)
    assert report.ok


def test_explicit_maintenance_paths_must_match_database_config(tmp_path):
    fs_impl = make_fs(tmp_path)
    fs_impl.close()

    with pytest.raises(ValueError, match="do not match database config"):
        run_fsck(tmp_path / "other-hot", tmp_path / "other-cold", fs_impl.database)


def test_update_config_rehomes_database_storage_paths(tmp_path):
    fs_impl = make_fs(tmp_path)
    fs_impl.close()
    new_hot = tmp_path / "new-hot"
    new_cold = tmp_path / "new-cold"
    new_hot.mkdir()
    new_cold.mkdir()

    report = run_fsck(new_hot, new_cold, fs_impl.database, update_config=True)

    assert report.ok
    config = rows(
        fs_impl, "SELECT hot_tier_path, cold_tier_path FROM filesystem_config"
    )[0]
    assert config["hot_tier_path"] == str(new_hot.resolve())
    assert config["cold_tier_path"] == str(new_cold.resolve())


def test_scrub_reads_filekv_inode_payloads(tmp_path):
    fs_impl = make_fs(tmp_path, payload_store="filekv")
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    report = run_scrub(fs_impl.tier1, fs_impl.tier2, fs_impl.database)
    assert report.ok


def test_scrub_reports_missing_filekv_inode_payload(tmp_path):
    fs_impl = make_fs(tmp_path, payload_store="filekv")
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    key = rows(fs_impl, "SELECT payload_key FROM inode_payloads")[0]["payload_key"]
    safe = key.replace("/", "_")
    (fs_impl.tier1 / "payload-kv" / safe[:2] / safe[2:4] / safe).unlink()

    report = run_scrub(fs_impl.tier1, fs_impl.tier2, fs_impl.database)
    assert any(issue.code == "payload_store_unavailable" for issue in report.issues)


def test_scrub_reads_filekv_inline_block_payloads(tmp_path):
    fs_impl = make_fs(
        tmp_path,
        inline_max_bytes=64,
        compression_min_bytes=0,
        payload_store="filekv",
    )
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"a" * 1024, 0, fh)

    report = run_scrub(fs_impl.tier1, fs_impl.tier2, fs_impl.database)
    assert report.ok


def test_fsck_repairs_block_refcount_mismatch(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    with sqlite3.connect(fs_impl.database) as db:
        db.execute("UPDATE blocks SET refcount = 99 WHERE hash = ?", (digest,))

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)
    assert [issue.code for issue in report.issues] == ["refcount_mismatch"]
    assert report.issues[0].repaired
    assert rows(fs_impl, "SELECT refcount FROM blocks")[0]["refcount"] == 1
    assert run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database).ok


def test_fsck_repairs_block_presence_mismatch(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    hot_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 1)
    cold_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 2)
    cold_path.parent.mkdir(parents=True, exist_ok=True)
    hot_path.replace(cold_path)

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)
    assert [issue.code for issue in report.issues] == [
        "block_presence_mismatch",
        "preferred_tier_missing",
    ]
    assert report.issues[0].repaired
    row = rows(
        fs_impl,
        "SELECT hot_present, cold_present, preferred_tier FROM block_records",
    )[0]
    assert (row["hot_present"], row["cold_present"], row["preferred_tier"]) == (0, 1, 2)


def test_fsck_repairs_unreferenced_block_record_and_file(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 1)
    with sqlite3.connect(fs_impl.database) as db:
        db.execute("DELETE FROM file_chunks")

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)
    assert {issue.code for issue in report.issues} == {
        "refcount_mismatch",
        "unreferenced_block_record",
    }
    assert all(issue.repaired for issue in report.issues)
    assert rows(fs_impl, "SELECT * FROM blocks") == []
    assert not path.exists()


def test_fsck_reports_missing_inline_payload_record(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=64, compression_min_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"a" * 1024, 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    with sqlite3.connect(fs_impl.database) as db:
        db.execute("DELETE FROM block_payloads WHERE hash = ?", (digest,))

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)
    assert [issue.code for issue in report.issues] == ["missing_inline_payload"]
    assert not report.issues[0].repaired


def test_fsck_reports_missing_inode_payload_record(tmp_path):
    fs_impl = make_fs(tmp_path)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    with sqlite3.connect(fs_impl.database) as db:
        db.execute("DELETE FROM inode_payloads")

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)
    assert [issue.code for issue in report.issues] == ["missing_inode_payload"]
    assert not report.issues[0].repaired


def test_fsck_repairs_orphan_disk_block(tmp_path):
    fs_impl = make_fs(tmp_path)
    digest = "a" * 64
    orphan = block_path(fs_impl.tier1, fs_impl.tier2, digest, 1)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)
    assert [issue.code for issue in report.issues] == ["orphan_block_file"]
    assert report.issues[0].repaired
    assert not orphan.exists()


def test_fsck_does_not_repair_missing_referenced_block_file(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    block_path(fs_impl.tier1, fs_impl.tier2, digest, 1).unlink()

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)
    assert [issue.code for issue in report.issues] == ["missing_block_file"]
    assert not report.issues[0].repaired
    assert report.has_unrepaired


def test_read_cold_block_unavailable_preserves_location_metadata(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    _digest, data, cold_path = _make_cold_only_block(fs_impl)
    _make_path_stat_unavailable(monkeypatch, cold_path)

    reopened = make_fs(tmp_path, inline_max_bytes=0)
    with pytest.raises(FuseOSError) as excinfo:
        with adapted(reopened) as fs:
            fh = fs("open", "/note.txt", 0)
            fs("read", "/note.txt", len(data), 0, fh)

    assert excinfo.value.errno == errno.EIO
    row = rows(
        fs_impl,
        "SELECT hot_present, cold_present, preferred_tier FROM block_records",
    )[0]
    assert (row["hot_present"], row["cold_present"], row["preferred_tier"]) == (0, 1, 2)


def test_fsck_repair_skips_cold_unavailable_metadata_changes(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    _digest, _data, cold_path = _make_cold_only_block(fs_impl)
    _make_path_stat_unavailable(monkeypatch, cold_path)

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)

    codes = {issue.code for issue in report.issues}
    assert "block_payload_unavailable" in codes
    assert "missing_block_file" not in codes
    row = rows(
        fs_impl,
        "SELECT hot_present, cold_present, preferred_tier FROM block_records",
    )[0]
    assert (row["hot_present"], row["cold_present"], row["preferred_tier"]) == (0, 1, 2)


def test_scrub_reports_cold_download_failure_as_unavailable(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    _digest, _data, cold_path = _make_cold_only_block(fs_impl, data=b"a" * 1024)

    import ztierfs.maintenance.checker as checker_module

    original_read = checker_module.read_path_bytes

    def read_or_unavailable(path):
        if path == cold_path:
            raise PathUnavailable(path, OSError(errno.EIO, "rclone download failed"))
        return original_read(path)

    monkeypatch.setattr(checker_module, "read_path_bytes", read_or_unavailable)

    report = run_scrub(fs_impl.tier1, fs_impl.tier2, fs_impl.database)
    codes = {issue.code for issue in report.issues}

    assert "block_payload_unavailable" in codes
    assert "corrupt_block_payload" not in codes


def test_scrub_reports_corrupt_compressed_block(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0, compression_min_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"a" * 1024, 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks WHERE compressed = 1")[0]["hash"]
    block_path(fs_impl.tier1, fs_impl.tier2, digest, 1).write_bytes(b"not-zstd")

    report = run_scrub(fs_impl.tier1, fs_impl.tier2, fs_impl.database)
    assert "corrupt_block_payload" in {issue.code for issue in report.issues}


def test_stats_reports_storage_summary(tmp_path):
    fs_impl = make_fs(tmp_path)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    stats = collect_stats(fs_impl.tier1, fs_impl.tier2, fs_impl.database)
    data = stats.to_dict()
    assert data["inodes"]["files"] == 1
    assert data["chunks"]["file_chunks"] == 0
    assert data["blocks"]["total"] == 0
    assert data["blocks"]["inline"] == 0
    assert data["blocks"]["inode_inline"] == 1
    assert data["blocks"]["hot"] == 0
    assert data["blocks"]["cold"] == 0
    assert data["blocks"]["both"] == 0
    assert data["storage"]["logical_file_bytes"] == 5
    assert data["storage"]["inode_inline_stored_bytes"] == 5


def test_cleanup_removes_old_promoted_cold_copy(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    hot_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 1)
    cold_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 2)
    cold_path.parent.mkdir(parents=True, exist_ok=True)
    cold_path.write_bytes(hot_path.read_bytes())
    with sqlite3.connect(fs_impl.database) as db:
        db.execute(
            """
            UPDATE blocks SET preferred_tier = 1, last_promoted_ns = ? WHERE hash = ?
            """,
            (time_ns() - 10_000_000_000, digest),
        )
        db.execute(
            "INSERT OR IGNORE INTO block_locations (hash, tier) VALUES (?, 2)",
            (digest,),
        )

    report = cleanup_promoted_cold_copies(
        fs_impl.tier1,
        fs_impl.tier2,
        fs_impl.database,
        min_age_seconds=1,
    )
    assert report.removed == 1
    assert report.skipped == 0
    assert not cold_path.exists()
    assert (
        rows(fs_impl, "SELECT cold_present FROM block_records")[0]["cold_present"] == 0
    )


def test_cleanup_skips_cold_copy_when_cold_tier_unavailable(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    hot_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 1)
    cold_path = block_path(fs_impl.tier1, fs_impl.tier2, digest, 2)
    cold_path.parent.mkdir(parents=True, exist_ok=True)
    cold_path.write_bytes(hot_path.read_bytes())
    with sqlite3.connect(fs_impl.database) as db:
        db.execute(
            "UPDATE blocks SET preferred_tier = 1, last_promoted_ns = ? WHERE hash = ?",
            (time_ns() - 10_000_000_000, digest),
        )
        db.execute(
            "INSERT OR IGNORE INTO block_locations (hash, tier) VALUES (?, 2)",
            (digest,),
        )

    _make_path_stat_unavailable(monkeypatch, cold_path)

    report = cleanup_promoted_cold_copies(
        fs_impl.tier1,
        fs_impl.tier2,
        fs_impl.database,
        min_age_seconds=1,
    )

    assert report.removed == 0
    assert report.skipped == 1
    assert (
        rows(fs_impl, "SELECT cold_present FROM block_records")[0]["cold_present"] == 1
    )


def test_schema_rejects_invalid_metadata_rows(tmp_path):
    fs_impl = make_fs(tmp_path)
    try:
        with sqlite3.connect(fs_impl.database) as db:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    """
                    INSERT INTO inodes
                        (kind, mode, uid, gid, size, symlink_target, nlink, atime_ns, mtime_ns, ctime_ns)
                    VALUES ('file', 0, 0, 0, -1, NULL, 1, 0, 0, 0)
                    """
                )
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO dir_entries (parent_id, name, inode_id) VALUES (1, '', 1)"
                )
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    """
                    INSERT INTO blocks
                        (hash, storage_kind, preferred_tier, compressed, raw_size, stored_size, refcount, atime_ns)
                    VALUES (?, 'tiered', 0, 0, 1, 1, 0, 0)
                    """,
                    ("b" * 64,),
                )
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    """
                    INSERT INTO file_chunks (file_id, chunk_index, hash, size)
                    VALUES (1, -1, ?, 1)
                    """,
                    ("b" * 64,),
                )
    finally:
        fs_impl.close()


def test_schema_version_mismatch_is_rejected(tmp_path):
    database = tmp_path / "metadata.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version=1")

    with pytest.raises(RuntimeError, match="unsupported metadata schema version"):
        make_fs(tmp_path)


@pytest.mark.parametrize(
    ("corrupt", "expected_code"),
    [
        ("missing_block", "chunk_missing_block_metadata"),
        ("missing_inode", "chunk_missing_inode"),
        ("size_mismatch", "chunk_size_mismatch"),
        ("non_file_inode", "chunk_for_non_file_inode"),
        ("invalid_dir_entry", "invalid_dir_entry"),
    ],
)
def test_fsck_reports_chunk_and_directory_metadata_corruption(
    tmp_path, corrupt, expected_code
):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    with sqlite3.connect(fs_impl.database) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        if corrupt == "missing_block":
            db.execute("DELETE FROM blocks WHERE hash = ?", (digest,))
        elif corrupt == "missing_inode":
            db.execute("UPDATE file_chunks SET file_id = 999")
        elif corrupt == "size_mismatch":
            db.execute("UPDATE file_chunks SET size = 1")
        elif corrupt == "non_file_inode":
            db.execute("UPDATE file_chunks SET file_id = 1")
        elif corrupt == "invalid_dir_entry":
            db.execute(
                """
                INSERT INTO inodes
                    (kind, mode, uid, gid, size, symlink_target, nlink, atime_ns, mtime_ns, ctime_ns)
                VALUES ('file', 0, 0, 0, 0, NULL, 1, 0, 0, 0)
                """
            )
            parent_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                "INSERT INTO dir_entries (parent_id, name, inode_id) VALUES (?, 'bad', 1)",
                (parent_id,),
            )

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database)

    assert expected_code in {issue.code for issue in report.issues}


def test_fsck_repairs_nlink_mismatch(tmp_path):
    fs_impl = make_fs(tmp_path)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    with sqlite3.connect(fs_impl.database) as db:
        db.execute("UPDATE inodes SET nlink = 99 WHERE kind = 'file'")

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)

    assert [issue.code for issue in report.issues] == ["nlink_mismatch"]
    assert report.issues[0].repaired
    assert (
        rows(fs_impl, "SELECT nlink FROM inodes WHERE kind = 'file'")[0]["nlink"] == 1
    )


def test_fsck_repairs_inline_payload_table_corruption(tmp_path):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    digest = rows(fs_impl, "SELECT hash FROM blocks")[0]["hash"]
    orphan_digest = "c" * 64
    with sqlite3.connect(fs_impl.database) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO block_payloads (hash, payload) VALUES (?, ?)",
            (orphan_digest, b"orphan"),
        )
        db.execute(
            "INSERT INTO block_payloads (hash, payload) VALUES (?, ?)",
            (digest, b"unexpected"),
        )

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)

    assert {issue.code for issue in report.issues} == {
        "orphan_inline_payload",
        "unexpected_inline_payload",
    }
    assert all(issue.repaired for issue in report.issues)
    assert rows(fs_impl, "SELECT * FROM block_payloads") == []


def test_fsck_repairs_inode_payload_table_corruption(tmp_path):
    fs_impl = make_fs(tmp_path)
    with adapted(fs_impl) as fs:
        fs("mkdir", "/docs", 0o755)

    orphan_inode = 999
    directory_inode = rows(
        fs_impl, "SELECT inode_id FROM dir_entries WHERE name = 'docs'"
    )[0]["inode_id"]
    with sqlite3.connect(fs_impl.database) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            """
            INSERT INTO inode_payloads (inode_id, payload, compressed, raw_size, stored_size)
            VALUES (?, ?, 0, 6, 6)
            """,
            (orphan_inode, b"orphan"),
        )
        db.execute(
            """
            INSERT INTO inode_payloads (inode_id, payload, compressed, raw_size, stored_size)
            VALUES (?, ?, 0, 10, 10)
            """,
            (directory_inode, b"unexpected"),
        )

    report = run_fsck(fs_impl.tier1, fs_impl.tier2, fs_impl.database, repair=True)

    assert {issue.code for issue in report.issues} == {
        "orphan_inode_payload",
        "unexpected_inode_payload",
    }
    assert all(issue.repaired for issue in report.issues)
    assert rows(fs_impl, "SELECT * FROM inode_payloads") == []


@pytest.mark.parametrize(
    ("sql", "expected_code"),
    [
        ("UPDATE blocks SET stored_size = stored_size + 1", "stored_size_mismatch"),
        ("UPDATE blocks SET raw_size = raw_size + 1", "raw_size_mismatch"),
    ],
)
def test_scrub_reports_payload_size_mismatches(tmp_path, sql, expected_code):
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    with sqlite3.connect(fs_impl.database) as db:
        db.execute(sql)

    report = run_scrub(fs_impl.tier1, fs_impl.tier2, fs_impl.database)

    assert expected_code in {issue.code for issue in report.issues}
