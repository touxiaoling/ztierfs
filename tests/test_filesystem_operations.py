import errno
import fcntl
import os

import pytest
from macfusepy import FuseOSError
from stat import S_IFDIR, S_IFLNK, S_IFMT, S_IMODE

from ztierfs.handles import FileHandleSnapshot
from ztierfs.pathing import normalize_path

from .helpers import adapted, make_fs, rows, user_dir_entry_rows, user_inode_rows


def test_ztierfs_rejects_invalid_paths_and_supports_name_max_components(tmp_path):
    fs_impl = make_fs(tmp_path)

    for path in ("", "relative/path", "/bad\x00name"):
        with pytest.raises(FuseOSError) as exc:
            normalize_path(path)
        assert exc.value.errno == errno.EINVAL

    long_name = "n" * 255
    with adapted(fs_impl) as fs:
        fh = fs("create", f"/{long_name}", 0o644)
        fs("write", f"/{long_name}", b"ok", 0, fh)
        assert fs("read", f"/{long_name}", 2, 0, fh) == b"ok"
        assert long_name in fs("readdir", "/", None)


def test_ztierfs_preserves_zero_byte_files_without_blocks(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/empty.txt", 0o644)
        assert fs("getattr", "/empty.txt")["st_size"] == 0
        assert fs("read", "/empty.txt", 64, 0, fh) == b""
        fs("release", "/empty.txt", fh)

    assert rows(fs_impl, "SELECT * FROM file_chunks") == []
    assert rows(fs_impl, "SELECT * FROM blocks") == []

    reopened = make_fs(tmp_path)
    with adapted(reopened) as fs:
        fh = fs("open", "/empty.txt", os.O_RDONLY)
        assert fs("read", "/empty.txt", 64, 0, fh) == b""


def test_ztierfs_create_existing_file_respects_open_flags(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"contents", 0, fh)
        fs("release", "/note.txt", fh)

        reopened = fs("create", "/note.txt", 0o600, os.O_RDWR | os.O_CREAT)
        assert fs("read", "/note.txt", 8, 0, reopened) == b"contents"
        assert fs("getattr", "/note.txt")["st_size"] == 8
        fs("release", "/note.txt", reopened)

        truncated = fs(
            "create", "/note.txt", 0o600, os.O_RDWR | os.O_CREAT | os.O_TRUNC
        )
        assert fs("getattr", "/note.txt")["st_size"] == 0
        assert S_IMODE(fs("getattr", "/note.txt")["st_mode"]) == 0o644
        fs("release", "/note.txt", truncated)

        with pytest.raises(FuseOSError) as exc:
            fs("create", "/note.txt", 0o600, os.O_RDWR | os.O_CREAT | os.O_EXCL)
        assert exc.value.errno == errno.EEXIST


def test_ztierfs_handles_reads_and_writes_on_exact_chunk_boundaries(tmp_path):
    fs_impl = make_fs(tmp_path, chunk_size=8, inline_max_bytes=0)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/boundary.bin", 0o644)
        assert fs("write", "/boundary.bin", b"abcdefgh", 0, fh) == 8
        assert fs("write", "/boundary.bin", b"XYZ", 8, fh) == 3

        assert fs("read", "/boundary.bin", 11, 0, fh) == b"abcdefghXYZ"
        assert fs("read", "/boundary.bin", 4, 7, fh) == b"hXYZ"
        assert fs("getattr", "/boundary.bin")["st_size"] == 11

    chunk_rows = rows(
        fs_impl,
        "SELECT chunk_index FROM file_chunks ORDER BY chunk_index",
    )
    assert [row["chunk_index"] for row in chunk_rows] == [0, 1]


def test_ztierfs_handles_sparse_and_partial_writes(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/data.bin", 0o644)
        assert fs("write", "/data.bin", b"tail", 6, fh) == 4
        assert fs("read", "/data.bin", 10, 0, fh) == b"\x00" * 6 + b"tail"

        assert fs("write", "/data.bin", b"abc", 2, fh) == 3
        assert fs("read", "/data.bin", 10, 0, fh) == b"\x00\x00abc\x00tail"

        fs("truncate", "/data.bin", 1500, fh)
        assert fs("getattr", "/data.bin")["st_size"] == 1500
        assert fs("read", "/data.bin", 4, 1496, fh) == b"\x00" * 4

        fs("truncate", "/data.bin", 4, fh)
        assert fs("read", "/data.bin", 16, 0, fh) == b"\x00\x00ab"


def test_ztierfs_reports_lowlevel_stat_fields(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/sparse.bin", 0o644)
        fs("write", "/sparse.bin", b"tail", 4096, fh)
        attrs = fs("getattr", "/sparse.bin")

        assert attrs["st_blksize"] == fs_impl.chunk_size
        assert attrs["st_blocks"] < (attrs["st_size"] + 511) // 512
        assert attrs["st_birthtime"] == attrs["st_ctime"]


def test_ztierfs_handles_overwrites_across_chunk_boundaries(tmp_path):
    fs_impl = make_fs(tmp_path)
    original = b"a" * 2048

    with adapted(fs_impl) as fs:
        fh = fs("create", "/data.bin", 0o644)
        fs("write", "/data.bin", original, 0, fh)
        fs("write", "/data.bin", b"XYZ", 1023, fh)

        expected = b"a" * 1023 + b"XYZ" + b"a" * (2048 - 1026)
        assert fs("read", "/data.bin", len(original), 0, fh) == expected


def test_ztierfs_reads_renamed_file_by_existing_handle(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/before.txt", 0o644)
        fs("write", "/before.txt", b"contents", 0, fh)
        fs("rename", "/before.txt", "/after.txt", 0)

        assert fs("read", "/before.txt", 8, 0, fh) == b"contents"


def test_ztierfs_uses_lightweight_inode_lookup_for_open_handle_io(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path)
    original_node_by_id = fs_impl.metadata.node_by_id
    forbidden = False

    def observed_node_by_id(node_id):
        if forbidden:
            raise AssertionError("open handle IO should use lightweight inode lookup")
        return original_node_by_id(node_id)

    monkeypatch.setattr(fs_impl.metadata, "node_by_id", observed_node_by_id)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/before.txt", 0o644)
        fs("write", "/before.txt", b"contents", 0, fh)
        fs("rename", "/before.txt", "/after.txt", 0)

        forbidden = True
        assert fs("getattr", "/before.txt", fh)["st_size"] == 8
        fs("write", "/before.txt", b"!", 8, fh)
        assert fs("read", "/before.txt", 9, 0, fh) == b"contents!"


def test_ztierfs_uses_handle_snapshot_for_open_handle_permissions(
    tmp_path, monkeypatch
):
    fs_impl = make_fs(tmp_path)
    original_require_access = fs_impl._require_access
    used_snapshots: list[bool] = []

    def observed_require_access(node, mask):
        used_snapshots.append(isinstance(node, FileHandleSnapshot))
        return original_require_access(node, mask)

    monkeypatch.setattr(fs_impl, "_require_access", observed_require_access)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/cached.txt", 0o644)
        fs("write", "/cached.txt", b"abc", 0, fh)

        used_snapshots.clear()
        fs("write", "/cached.txt", b"d", 3, fh)
        assert fs("read", "/cached.txt", 4, 0, fh) == b"abcd"
        assert used_snapshots == [True, True]


def test_ztierfs_keeps_unlinked_open_file_until_release(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fh = fs("create", "/open.txt", 0o644)
        fs("write", "/open.txt", b"contents", 0, fh)
        fs("unlink", "/open.txt")

        assert fs("readdir", "/", None) == [".", "..", ".Trashes"]
        with pytest.raises(FuseOSError):
            fs("getattr", "/open.txt")
        assert fs("read", "/open.txt", 8, 0, fh) == b"contents"

        fs("release", "/open.txt", fh)

    assert user_inode_rows(fs_impl) == []
    assert user_dir_entry_rows(fs_impl) == []
    assert rows(fs_impl, "SELECT * FROM blocks") == []


def test_ztierfs_rename_over_open_target_preserves_target_handle(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        old_fh = fs("create", "/target.txt", 0o644)
        fs("write", "/target.txt", b"old", 0, old_fh)
        new_fh = fs("create", "/source.txt", 0o644)
        fs("write", "/source.txt", b"new", 0, new_fh)

        fs("rename", "/source.txt", "/target.txt", 0)

        assert fs("read", "/target.txt", 3, 0, old_fh) == b"old"
        assert fs("read", "/target.txt", 3, 0, new_fh) == b"new"
        fs("release", "/target.txt", old_fh)
        fs("release", "/target.txt", new_fh)


def test_ztierfs_supports_rename_noreplace_flag(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        source = fs("create", "/source.txt", 0o644)
        fs("write", "/source.txt", b"source", 0, source)
        target = fs("create", "/target.txt", 0o644)
        fs("write", "/target.txt", b"target", 0, target)

        with pytest.raises(FuseOSError) as exc:
            fs("rename", "/source.txt", "/target.txt", 0x1)
        assert exc.value.errno == errno.EEXIST
        assert fs("read", "/source.txt", 6, 0, source) == b"source"
        assert fs("read", "/target.txt", 6, 0, target) == b"target"

        fs("rename", "/source.txt", "/renamed.txt", 0x1)
        assert fs("read", "/renamed.txt", 6, 0, source) == b"source"


def test_ztierfs_supports_macos_per_volume_trash(tmp_path):
    uid = os.getuid()
    gid = os.getgid()
    fs_impl = make_fs(tmp_path, caller_provider=lambda: (uid, gid, 1234))
    trash_path = f"/.Trashes/{uid}"

    with adapted(fs_impl) as fs:
        assert fs("readdir", "/", None) == [".", "..", ".Trashes"]

        trash_root = fs("getattr", "/.Trashes")
        assert S_IFMT(trash_root["st_mode"]) == S_IFDIR
        assert S_IMODE(trash_root["st_mode"]) == 0o1777
        assert trash_root["st_uid"] == 0
        assert trash_root["st_gid"] == 0

        user_trash = fs("getattr", trash_path)
        assert S_IFMT(user_trash["st_mode"]) == S_IFDIR
        assert S_IMODE(user_trash["st_mode"]) == 0o700
        assert user_trash["st_uid"] == uid
        assert user_trash["st_gid"] == gid

        fh = fs("create", "/delete-me.txt", 0o644)
        fs("write", "/delete-me.txt", b"trash contents", 0, fh)
        assert fs("access", "/delete-me.txt", 0x801) == 0
        fs("rename", "/delete-me.txt", f"{trash_path}/delete-me.txt", 0)

        with pytest.raises(FuseOSError) as exc:
            fs("getattr", "/delete-me.txt")
        assert exc.value.errno == errno.ENOENT
        assert fs("read", f"{trash_path}/delete-me.txt", 14, 0, fh) == b"trash contents"
        assert fs("readdir", trash_path, None) == [".", "..", "delete-me.txt"]


def test_ztierfs_denies_macos_delete_access_without_parent_write(tmp_path):
    uid = os.getuid() or 501
    gid = os.getgid() or 20
    fs_impl = make_fs(tmp_path, caller_provider=lambda: (uid, gid, 1234))

    with adapted(fs_impl) as fs:
        fs("mkdir", "/readonly", 0o755)
        fh = fs("create", "/readonly/delete-me.txt", 0o644)
        fs("release", "/readonly/delete-me.txt", fh)
        fs("chmod", "/readonly", 0o555)

        with pytest.raises(FuseOSError) as exc:
            fs("access", "/readonly/delete-me.txt", 0x801)
        assert exc.value.errno == errno.EACCES


def test_ztierfs_allows_macos_delete_access_probe_on_root(tmp_path):
    uid = os.getuid() or 501
    gid = os.getgid() or 20
    fs_impl = make_fs(tmp_path, caller_provider=lambda: (uid, gid, 1234))

    with adapted(fs_impl) as fs:
        assert fs("access", "/", 0x801) == 0
        assert fs("readdir", "/", None) == [".", "..", ".Trashes"]


def test_ztierfs_creates_trash_directory_for_calling_user(tmp_path, monkeypatch):
    owner_uid = os.getuid()
    owner_gid = os.getgid()
    other_uid = owner_uid + 10000
    other_gid = owner_gid + 10000
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        monkeypatch.setattr(
            fs_impl, "_caller_provider", lambda: (other_uid, other_gid, 4321)
        )
        other_trash = fs("getattr", f"/.Trashes/{other_uid}")
        assert S_IMODE(other_trash["st_mode"]) == 0o700
        assert other_trash["st_uid"] == other_uid
        assert other_trash["st_gid"] == other_gid

        monkeypatch.setattr(
            fs_impl, "_caller_provider", lambda: (owner_uid, owner_gid, 4322)
        )
        with pytest.raises(FuseOSError) as exc:
            fs("readdir", f"/.Trashes/{other_uid}", None)
        assert exc.value.errno == errno.EACCES


def test_ztierfs_persists_metadata_and_blocks_across_reopen(tmp_path):
    fs_impl = make_fs(tmp_path)
    data = b"persistent data" * 100

    with adapted(fs_impl) as fs:
        fs("mkdir", "/docs", 0o755)
        fh = fs("create", "/docs/file.txt", 0o644)
        fs("write", "/docs/file.txt", data, 0, fh)

    reopened = make_fs(tmp_path)
    with adapted(reopened) as fs:
        fh = fs("open", "/docs/file.txt", os.O_RDONLY)
        assert fs("read", "/docs/file.txt", len(data), 0, fh) == data
        assert fs("readdir", "/docs", None) == [".", "..", "file.txt"]


def test_ztierfs_updates_directories_renames_and_truncates(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fs("mkdir", "/docs", 0o755)
        fh = fs("create", "/docs/file.txt", 0o644)
        fs("write", "/docs/file.txt", b"abcdef", 0, fh)
        fs("rename", "/docs/file.txt", "/docs/renamed.txt", 0)
        fs("truncate", "/docs/renamed.txt", 3, fh)

        assert sorted(fs("readdir", "/docs", None)) == [".", "..", "renamed.txt"]
        assert fs("read", "/docs/renamed.txt", 16, 0, fh) == b"abc"
        assert fs("getattr", "/docs/renamed.txt")["st_size"] == 3

        fs("unlink", "/docs/renamed.txt")
        fs("rmdir", "/docs")
        assert fs("readdir", "/", None) == [".", "..", ".Trashes"]


def test_ztierfs_defers_readdir_atime_until_metadata_commit(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fs("mkdir", "/docs", 0o755)
        ino = fs_impl.lookup(1, b"docs").ino
        before = rows(fs_impl, f"SELECT atime_ns FROM inodes WHERE id = {ino}")[0][
            "atime_ns"
        ]

        assert fs("readdir", "/docs", None) == [".", ".."]

        assert (
            rows(fs_impl, f"SELECT atime_ns FROM inodes WHERE id = {ino}")[0][
                "atime_ns"
            ]
            == before
        )
        fs_impl.metadata.commit()
        assert (
            rows(fs_impl, f"SELECT atime_ns FROM inodes WHERE id = {ino}")[0][
                "atime_ns"
            ]
            > before
        )


def test_ztierfs_defers_readlink_atime_until_metadata_commit(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fs("symlink", "/link.txt", "target.txt")
        ino = fs_impl.lookup(1, b"link.txt").ino
        before = rows(fs_impl, f"SELECT atime_ns FROM inodes WHERE id = {ino}")[0][
            "atime_ns"
        ]

        assert fs("readlink", "/link.txt") == "target.txt"

        assert (
            rows(fs_impl, f"SELECT atime_ns FROM inodes WHERE id = {ino}")[0][
                "atime_ns"
            ]
            == before
        )
        fs_impl.metadata.commit()
        assert (
            rows(fs_impl, f"SELECT atime_ns FROM inodes WHERE id = {ino}")[0][
                "atime_ns"
            ]
            > before
        )


def test_ztierfs_reports_expected_errors(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fs("mkdir", "/docs", 0o755)
        fh = fs("create", "/docs/file.txt", 0o644)

        with pytest.raises(FuseOSError):
            fs("read", "/missing.txt", 1, 0, None)
        with pytest.raises(FuseOSError):
            fs("rmdir", "/docs")
        with pytest.raises(FuseOSError):
            fs("write", "/docs/file.txt", b"x", -1, fh)
        with pytest.raises(FuseOSError):
            fs("rename", "/docs", "/docs/child", 0)


def test_ztierfs_reports_precise_namespace_error_numbers(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fs("mkdir", "/parent", 0o755)
        fs("mkdir", "/parent/child", 0o755)
        file_fh = fs("create", "/file.txt", 0o644)

        with pytest.raises(FuseOSError) as exc:
            fs("rmdir", "/parent")
        assert exc.value.errno == errno.ENOTEMPTY

        with pytest.raises(FuseOSError) as exc:
            fs("rename", "/file.txt", "/renamed.txt", 0x2)
        assert exc.value.errno == errno.EINVAL

        with pytest.raises(FuseOSError) as exc:
            fs("rename", "/file.txt", "/parent", 0)
        assert exc.value.errno == errno.EISDIR

        with pytest.raises(FuseOSError) as exc:
            fs("rename", "/parent", "/file.txt", 0)
        assert exc.value.errno == errno.ENOTDIR

        fs("release", "/file.txt", file_fh)


def test_ztierfs_supports_symlinks(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fs("symlink", "/link.txt", "target.txt")

        attrs = fs("getattr", "/link.txt")
        assert S_IFMT(attrs["st_mode"]) == S_IFLNK
        assert attrs["st_size"] == len("target.txt")
        assert fs("readlink", "/link.txt") == "target.txt"

        fs("rename", "/link.txt", "/renamed.txt", 0)
        assert fs("readlink", "/renamed.txt") == "target.txt"
        fs("unlink", "/renamed.txt")
        assert fs("readdir", "/", None) == [".", "..", ".Trashes"]


def test_ztierfs_supports_hardlinks_for_file_inodes(tmp_path):
    fs_impl = make_fs(tmp_path)
    data = b"shared contents"

    with adapted(fs_impl) as fs:
        first = fs("create", "/first.txt", 0o644)
        fs("write", "/first.txt", data, 0, first)
        fs("link", "/second.txt", "/first.txt")

        assert fs("getattr", "/first.txt")["st_nlink"] == 2
        assert fs("getattr", "/second.txt")["st_nlink"] == 2
        second = fs("open", "/second.txt", os.O_RDWR)
        assert fs("read", "/second.txt", len(data), 0, second) == data

        fs("write", "/second.txt", b"S", 0, second)
        assert fs("read", "/first.txt", len(data), 0, first) == b"Shared contents"

        fs("unlink", "/first.txt")
        assert fs("getattr", "/second.txt")["st_nlink"] == 1
        assert fs("read", "/second.txt", len(data), 0, second) == b"Shared contents"
        fs("release", "/first.txt", first)
        fs("unlink", "/second.txt")
        fs("release", "/second.txt", second)

    assert user_inode_rows(fs_impl) == []
    assert rows(fs_impl, "SELECT * FROM blocks") == []


def test_ztierfs_xattrs_are_shared_by_hardlinks(tmp_path):
    fs_impl = make_fs(tmp_path)

    with adapted(fs_impl) as fs:
        fs("create", "/file.txt", 0o644)
        fs("setxattr", "/file.txt", "user.note", b"value", 0, 0)
        fs("link", "/alias.txt", "/file.txt")

        assert list(fs("listxattr", "/alias.txt")) == ["user.note"]
        assert fs("getxattr", "/alias.txt", "user.note", 0) == b"value"

        fs("removexattr", "/alias.txt", "user.note")
        with pytest.raises(FuseOSError) as exc:
            fs("getxattr", "/file.txt", "user.note", 0)
        assert exc.value.errno in (
            errno.ENOENT,
            getattr(errno, "ENOATTR", errno.ENOENT),
            getattr(errno, "ENODATA", errno.ENOENT),
        )


def test_ztierfs_enforces_basic_mode_permissions(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path)
    owner = os.getuid()
    group = os.getgid()

    with adapted(fs_impl) as fs:
        monkeypatch.setattr(fs_impl, "_caller_provider", lambda: (owner, group, 1233))
        fh = fs("create", "/private.txt", 0o600)
        fs("write", "/private.txt", b"secret", 0, fh)
        fs("release", "/private.txt", fh)

        monkeypatch.setattr(
            fs_impl, "_caller_provider", lambda: (owner + 10000, group + 10000, 1234)
        )
        with pytest.raises(FuseOSError) as exc:
            fs("open", "/private.txt", os.O_RDONLY)
        assert exc.value.errno == errno.EACCES

        monkeypatch.setattr(fs_impl, "_caller_provider", lambda: (owner, group, 1235))
        readable = fs("open", "/private.txt", os.O_RDONLY)
        assert fs("read", "/private.txt", 6, 0, readable) == b"secret"


def test_ztierfs_denies_writes_and_metadata_changes_without_permission(
    tmp_path, monkeypatch
):
    owner_uid = os.getuid()
    owner_gid = os.getgid()
    if owner_uid == 0:
        pytest.skip("root bypasses normal write permission checks")
    other_uid = owner_uid + 10000
    other_gid = owner_gid + 10000
    fs_impl = make_fs(tmp_path, caller_provider=lambda: (owner_uid, owner_gid, 1234))

    with adapted(fs_impl) as fs:
        fh = fs("create", "/readonly.txt", 0o644)
        fs("write", "/readonly.txt", b"data", 0, fh)
        fs("chmod", "/readonly.txt", 0o444)

        with pytest.raises(FuseOSError) as exc:
            fs("write", "/readonly.txt", b"x", 0, fh)
        assert exc.value.errno == errno.EACCES

        with pytest.raises(FuseOSError) as exc:
            fs("setxattr", "/readonly.txt", "user.note", b"value", 0, 0)
        assert exc.value.errno == errno.EACCES

        monkeypatch.setattr(
            fs_impl, "_caller_provider", lambda: (other_uid, other_gid, 1235)
        )
        with pytest.raises(FuseOSError) as exc:
            fs("chmod", "/readonly.txt", 0o644)
        assert exc.value.errno == errno.EPERM

        assert fs("read", "/readonly.txt", 4, 0, fh) == b"data"


def test_ztierfs_advisory_locks_conflict_by_owner(tmp_path, monkeypatch):
    fs_impl = make_fs(tmp_path)
    uid = os.getuid()
    gid = os.getgid()

    with adapted(fs_impl) as fs:
        monkeypatch.setattr(fs_impl, "_caller_provider", lambda: (uid, gid, 1001))
        first = fs("create", "/locked.txt", 0o644)
        fs(
            "lock",
            "/locked.txt",
            first,
            fcntl.F_SETLK,
            {"l_type": fcntl.F_WRLCK, "l_start": 0, "l_len": 10, "l_pid": 1001},
        )

        monkeypatch.setattr(fs_impl, "_caller_provider", lambda: (uid, gid, 1002))
        second = fs("open", "/locked.txt", os.O_RDWR)
        probe = fs(
            "lock",
            "/locked.txt",
            second,
            fcntl.F_GETLK,
            {"l_type": fcntl.F_WRLCK, "l_start": 0, "l_len": 10, "l_pid": 1002},
        )
        assert probe["l_type"] == fcntl.F_WRLCK
        assert probe["l_pid"] == 1001
        with pytest.raises(FuseOSError) as exc:
            fs(
                "lock",
                "/locked.txt",
                second,
                fcntl.F_SETLK,
                {"l_type": fcntl.F_RDLCK, "l_start": 0, "l_len": 10, "l_pid": 1002},
            )
        assert exc.value.errno == errno.EAGAIN

        monkeypatch.setattr(fs_impl, "_caller_provider", lambda: (uid, gid, 1001))
        fs(
            "lock",
            "/locked.txt",
            first,
            fcntl.F_SETLK,
            {"l_type": fcntl.F_UNLCK, "l_start": 0, "l_len": 10, "l_pid": 1001},
        )
        monkeypatch.setattr(fs_impl, "_caller_provider", lambda: (uid, gid, 1002))
        fs(
            "lock",
            "/locked.txt",
            second,
            fcntl.F_SETLK,
            {"l_type": fcntl.F_RDLCK, "l_start": 0, "l_len": 10, "l_pid": 1002},
        )


def test_ztierfs_read_beyond_eof_returns_empty(tmp_path):
    fs_impl = make_fs(tmp_path)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/small.txt", 0o644)
        fs("write", "/small.txt", b"abc", 0, fh)
        assert fs("read", "/small.txt", 10, 0, fh) == b"abc"
        assert fs("read", "/small.txt", 10, 3, fh) == b""
        assert fs("read", "/small.txt", 5, 10, fh) == b""
        fs("release", "/small.txt", fh)


def test_ztierfs_write_spanning_chunk_boundary(tmp_path):
    """默认 chunk_size=1024：在 1023 处写 2 字节应落到相邻两块。"""
    fs_impl = make_fs(tmp_path, inline_max_bytes=0)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/span.txt", 0o644)
        fs("write", "/span.txt", b"x" * 1023, 0, fh)
        fs("write", "/span.txt", b"yz", 1023, fh)
        assert fs("getattr", "/span.txt")["st_size"] == 1025
        assert fs("read", "/span.txt", 1025, 0, fh) == b"x" * 1023 + b"yz"
        fs("release", "/span.txt", fh)
