import fcntl
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from typing import Mapping, cast

import pytest
from ztierfs import ZTierFS
from ztierfs.inode_fuse import (
    FUSE_SET_ATTR_ATIME,
    FUSE_SET_ATTR_GID,
    FUSE_SET_ATTR_MODE,
    FUSE_SET_ATTR_MTIME,
    FUSE_SET_ATTR_SIZE,
    FUSE_SET_ATTR_UID,
)
from ztierfs.pathing import split_path


class TestOperationsAdapter:
    def __init__(self, operations: ZTierFS):
        self.operations = operations

    def __call__(self, op: str, *args: object) -> object:
        fs = self.operations
        if op == "clonefile":
            return fs._clonefile(cast(str, args[0]), cast(str, args[1]))
        if op == "access":
            return fs.access(self._ino(args[0]), cast(int, args[1]))
        if op == "chmod":
            ino = self._ino(args[0])
            return fs.setattr(
                ino,
                cast(Mapping[str, int], {"st_mode": cast(int, args[1])}),
                FUSE_SET_ATTR_MODE,
            )
        if op == "chown":
            ino = self._ino(args[0])
            attrs: dict[str, int] = {}
            to_set = 0
            if args[1] != -1:
                attrs["st_uid"] = cast(int, args[1])
                to_set |= FUSE_SET_ATTR_UID
            if args[2] != -1:
                attrs["st_gid"] = cast(int, args[2])
                to_set |= FUSE_SET_ATTR_GID
            return fs.setattr(ino, attrs, to_set)
        if op == "create":
            parent, name = self._parent_and_name(args[0])
            flags = cast(int, args[2]) if len(args) > 2 else os.O_RDWR
            return fs.create(parent, name, cast(int, args[1]), flags, None)[1]
        if op == "flush":
            return fs.flush(self._ino_from_path_or_fh(args[0], args[1]), args[1])
        if op == "fsync":
            return fs.fsync(
                self._ino_from_path_or_fh(args[0], args[2]),
                cast(int, args[1]),
                args[2],
            )
        if op == "getattr":
            fh = args[1] if len(args) > 1 else None
            return fs.getattr(self._ino_from_path_or_fh(args[0], fh), fh)
        if op == "getxattr":
            pos = cast(int, args[2]) if len(args) > 2 else 0
            return fs.getxattr(self._ino(args[0]), self._encode(args[1]), pos)
        if op == "link":
            source_ino = self._ino(args[1])
            parent, name = self._parent_and_name(args[0])
            return fs.link(source_ino, parent, name)
        if op == "listxattr":
            return fs.listxattr(self._ino(args[0]))
        if op == "lock":
            ino = self._ino_from_path_or_fh(args[0], args[1])
            lock = cast(dict[str, int], args[3])
            return (
                fs.getlk(ino, args[1], lock)
                if args[2] == fcntl.F_GETLK
                else fs.setlk(ino, args[1], cast(int, args[2]), lock)
            )
        if op == "mkdir":
            parent, name = self._parent_and_name(args[0])
            return fs.mkdir(parent, name, cast(int, args[1])).attrs
        if op == "mknod":
            parent, name = self._parent_and_name(args[0])
            return fs.mknod(parent, name, cast(int, args[1]), cast(int, args[2])).attrs
        if op == "open":
            return fs.open(self._ino(args[0]), cast(int, args[1]))
        if op == "read":
            return fs.read(
                self._ino_from_path_or_fh(args[0], args[3]),
                cast(int, args[1]),
                cast(int, args[2]),
                args[3],
            )
        if op == "readdir":
            ino = self._ino(args[0])
            fh = args[1]
            close_fh = False
            if fh is None:
                fh = fs.opendir(ino)
                close_fh = True
            try:
                flags = cast(int, args[2]) if len(args) > 2 else 0
                entries = fs.readdir(ino, 0, 128 * 1024, fh, flags)
            finally:
                if close_fh:
                    fs.releasedir(ino, fh)
            return [self._decode(entry.name) for entry in entries]
        if op == "readlink":
            return fs.readlink(self._ino(args[0]))
        if op == "release":
            return fs.release(self._ino_from_path_or_fh(args[0], args[1]), args[1])
        if op == "removexattr":
            return fs.removexattr(self._ino(args[0]), self._encode(args[1]))
        if op == "rename":
            parent, name = self._parent_and_name(args[0])
            newparent, newname = self._parent_and_name(args[1])
            return fs.rename(parent, name, newparent, newname, cast(int, args[2]))
        if op == "rmdir":
            parent, name = self._parent_and_name(args[0])
            return fs.rmdir(parent, name)
        if op == "setxattr":
            pos = cast(int, args[4]) if len(args) > 4 else 0
            return fs.setxattr(
                self._ino(args[0]),
                self._encode(args[1]),
                cast(bytes, args[2]),
                cast(int, args[3]),
                pos,
            )
        if op == "statfs":
            return fs.statfs(self._ino(args[0] if args else "/"))
        if op == "symlink":
            parent, name = self._parent_and_name(args[0])
            return fs.symlink(self._encode(args[1]), parent, name).attrs
        if op == "truncate":
            fh = args[2] if len(args) > 2 else None
            ino = self._ino_from_path_or_fh(args[0], fh)
            return fs.setattr(
                ino,
                cast(Mapping[str, int], {"st_size": cast(int, args[1])}),
                FUSE_SET_ATTR_SIZE,
                fh,
            )
        if op == "unlink":
            parent, name = self._parent_and_name(args[0])
            return fs.unlink(parent, name)
        if op == "utimens":
            ino = self._ino(args[0])
            times = cast(tuple[int, int], args[1])
            return fs.setattr(
                ino,
                {"st_atime": times[0], "st_mtime": times[1]},
                FUSE_SET_ATTR_ATIME | FUSE_SET_ATTR_MTIME,
                args[2] if len(args) > 2 else None,
            )
        if op == "write":
            return fs.write(
                self._ino_from_path_or_fh(args[0], args[3]),
                cast(bytes, args[1]),
                cast(int, args[2]),
                args[3],
            )
        raise KeyError(op)

    def _ino(self, path: object) -> int:
        ino = 1
        for part in split_path(str(path)):
            ino = self.operations.lookup(ino, self._encode(part)).ino
        return ino

    def _ino_from_path_or_fh(self, path: object, fh: object) -> int:
        file_id = self.operations.handles.file_id(fh)
        return file_id if file_id is not None else self._ino(path)

    def _parent_and_name(self, path: object) -> tuple[int, bytes]:
        parts = split_path(str(path))
        if not parts:
            raise ValueError("root has no parent entry")
        parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        return self._ino(parent_path), self._encode(parts[-1])

    def _encode(self, value: object) -> bytes:
        return str(value).encode("utf-8", "surrogateescape")

    def _decode(self, value: bytes) -> str:
        return value.decode("utf-8", "surrogateescape")

    def close(self) -> None:
        self.operations.close()


@contextmanager
def adapted(operations):
    adapter = TestOperationsAdapter(operations)
    try:
        yield adapter
    finally:
        adapter.close()


def rows(fs: ZTierFS, sql: str):
    with sqlite3.connect(fs.database) as db:
        db.row_factory = sqlite3.Row
        return db.execute(sql).fetchall()


def user_inode_rows(fs: ZTierFS):
    return rows(
        fs,
        """
        SELECT *
        FROM inodes
        WHERE id NOT IN (
            SELECT 1
            UNION
            SELECT trash.inode_id
            FROM dir_entries AS trash
            WHERE trash.parent_id = 1 AND trash.name = '.Trashes'
            UNION
            SELECT child.inode_id
            FROM dir_entries AS child
            JOIN dir_entries AS trash ON trash.inode_id = child.parent_id
            WHERE trash.parent_id = 1 AND trash.name = '.Trashes'
        )
        """,
    )


def user_dir_entry_rows(fs: ZTierFS):
    return rows(
        fs,
        """
        SELECT *
        FROM dir_entries
        WHERE name != '.Trashes'
          AND parent_id NOT IN (
              SELECT trash.inode_id
              FROM dir_entries AS trash
              WHERE trash.parent_id = 1 AND trash.name = '.Trashes'
          )
        """,
    )


def make_fs(tmp_path, **kwargs):
    return ZTierFS(
        tmp_path / "hot",
        tmp_path / "cold",
        tmp_path / "metadata.sqlite3",
        chunk_size=kwargs.pop("chunk_size", 1024),
        hot_cache_max_bytes=kwargs.pop("hot_cache_max_bytes", 0),
        **kwargs,
    )


def unmount(path: Path):
    commands = []
    if sys.platform == "darwin" and shutil.which("diskutil"):
        commands.append(["diskutil", "unmount", "force", str(path)])

    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return


@contextmanager
def mounted_ztierfs(tmp_path):
    tier1 = tmp_path / "hot"
    tier2 = tmp_path / "cold"
    mount = tmp_path / "mount"
    database = tmp_path / "metadata.sqlite3"
    for directory in (tier1, tier2, mount):
        directory.mkdir()

    command = [
        sys.executable,
        "-m",
        "ztierfs",
        "mount",
        str(tier1),
        str(tier2),
        str(mount),
        "--database",
        str(database),
        "--chunk-size",
        "1k",
        "--hot-cache",
        "1500",
    ]
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr else ""
                pytest.fail(f"ztierfs mount process exited early: {stderr}")
            if os.path.ismount(mount):
                break
            time.sleep(0.1)
        else:
            pytest.fail("timed out waiting for ztierfs mount point")

        yield mount, tier1, tier2, database
    finally:
        with suppress(Exception):
            unmount(mount)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
