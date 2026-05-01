import os
import subprocess
import sys

from stat import S_ISLNK

import pytest

from .helpers import connect_sqlite, mounted_ztierfs


def _xattr_names(path):
    output = subprocess.check_output(["xattr", str(path)], text=True)
    return output.splitlines()


def _get_xattr(path, name):
    return subprocess.check_output(["xattr", "-p", name, str(path)], text=True).rstrip(
        "\n"
    )


def _set_xattr(path, name, value):
    subprocess.run(["xattr", "-w", name, value, str(path)], check=True)


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin", reason="ztierfs only supports macOS")
def test_ztierfs_real_mount_round_trips_files_and_directories(tmp_path):
    with mounted_ztierfs(tmp_path) as (mount, tier1, tier2, database):
        docs = mount / "docs"
        docs.mkdir()

        note = docs / "note.txt"
        data = b"a" * 3000
        note.write_bytes(data)
        assert note.read_bytes() == data

        renamed = docs / "renamed.txt"
        note.rename(renamed)
        with renamed.open("r+b") as file:
            file.seek(512)
            file.write(b"middle")
        assert renamed.read_bytes()[512:518] == b"middle"

        os.truncate(renamed, 1024)
        assert renamed.stat().st_size == 1024
        assert renamed.read_bytes()[512:518] == b"middle"

        renamed.unlink()
        docs.rmdir()
        assert not docs.exists()

        with connect_sqlite(database) as db:
            assert (
                db.execute(
                    """
                    SELECT COUNT(*)
                    FROM dir_entries
                    WHERE name IN ('docs', 'note.txt', 'renamed.txt')
                    """
                ).fetchone()[0]
                == 0
            )
        assert (tier1 / "blocks").exists()
        assert (tier2 / "blocks").exists()


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin", reason="ztierfs only supports macOS")
def test_ztierfs_real_mount_statvfs_and_large_sequential_write(tmp_path):
    """statvfs 与大于集成挂载默认 chunk 的连续写读（FUSE 栈路径）。"""
    with mounted_ztierfs(tmp_path) as (mount, _tier1, _tier2, _database):
        vfs = os.statvfs(mount)
        assert vfs.f_bsize > 0
        assert vfs.f_blocks > 0
        path = mount / "big.bin"
        payload = b"Z" * 3500
        path.write_bytes(payload)
        assert path.stat().st_size == len(payload)
        assert path.read_bytes() == payload
        path.unlink()


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin", reason="ztierfs only supports macOS")
def test_ztierfs_real_mount_supports_symlinks_through_directory_rename(tmp_path):
    with mounted_ztierfs(tmp_path) as (mount, _tier1, _tier2, _database):
        docs = mount / "docs"
        docs.mkdir()
        target = docs / "target.txt"
        target.write_text("target contents", encoding="utf-8")
        link = docs / "link.txt"
        os.symlink("target.txt", link)

        assert S_ISLNK(link.lstat().st_mode)
        assert os.readlink(link) == "target.txt"
        assert link.read_text(encoding="utf-8") == "target contents"

        moved = mount / "moved"
        docs.rename(moved)
        moved_link = moved / "link.txt"
        assert os.readlink(moved_link) == "target.txt"
        assert moved_link.read_text(encoding="utf-8") == "target contents"


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin", reason="ztierfs only supports macOS")
def test_ztierfs_real_mount_shares_hardlink_content_and_xattrs(tmp_path):
    with mounted_ztierfs(tmp_path) as (mount, _tier1, _tier2, _database):
        original = mount / "original.txt"
        original.write_bytes(b"shared contents")
        _set_xattr(original, "user.note", "value")

        alias = mount / "alias.txt"
        os.link(original, alias)

        original_stat = original.stat()
        alias_stat = alias.stat()
        assert original_stat.st_ino == alias_stat.st_ino
        assert original_stat.st_nlink == 2
        assert alias_stat.st_nlink == 2
        assert "user.note" in _xattr_names(alias)
        assert _get_xattr(alias, "user.note") == "value"

        with alias.open("r+b") as file:
            file.write(b"S")
        assert original.read_bytes() == b"Shared contents"

        original.unlink()
        assert alias.stat().st_nlink == 1
        assert alias.read_bytes() == b"Shared contents"
        assert _get_xattr(alias, "user.note") == "value"
