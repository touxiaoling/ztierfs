"""inode 元数据侧 FUSE：getattr、chmod、时间戳、xattr 等与 stat 属性映射。"""

import errno
import os

from stat import S_IFDIR, S_IFLNK, S_IFREG
from time import time_ns

from loguru import logger
from macfusepy import FuseOSError, LowLevelAttr

from .fs_mixins import FileSystemMixinBase

# 扩展属性：macOS 常用 ENOATTR；部分平台回落为 ENODATA
ENOATTR = getattr(errno, "ENOATTR", getattr(errno, "ENODATA", errno.ENOENT))
XATTR_CREATE = 0x1
XATTR_REPLACE = 0x2
MACOS_DELETE_ACCESS = 0x800


class MetadataOpsMixin(FileSystemMixinBase):
    """POSIX 元数据与 xattr；将 SQLite 中的 inode 行映射为 FUSE LowLevelAttr。"""

    def _allocated_bytes_from_node(self, node) -> int:
        if node["kind"] == "file":
            if node["inline_stored_size"]:
                return int(node["inline_stored_size"])
            return self.metadata.file_allocated_size(node["id"])
        return int(node["size"])

    def _attrs_from_node(self, node) -> LowLevelAttr:
        nlink = (
            2 + self.metadata.child_dir_count(node["id"])
            if node["kind"] == "dir"
            else node["nlink"]
        )
        allocated_bytes = self._allocated_bytes_from_node(node)
        return LowLevelAttr(
            st_ino=node["id"],
            st_mode=node["mode"],
            st_nlink=nlink,
            st_uid=node["uid"],
            st_gid=node["gid"],
            st_size=node["size"],
            st_blocks=(allocated_bytes + 511) // 512,
            st_blksize=self.chunk_size,
            st_atime=node["atime_ns"],
            st_mtime=node["mtime_ns"],
            st_ctime=node["ctime_ns"],
            st_birthtime=node["ctime_ns"],
        )

    def _getattr(self, path: str, fh=None) -> LowLevelAttr:
        logger.debug("获取属性：path={}，fh={}", path, fh)
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            node = self._node_from_handle_or_path(path, fh)
            return self._attrs_from_node(node)

    def _chmod(self, path: str, mode: int, fh=None) -> None:
        logger.debug("修改权限：path={}，mode={:o}，fh={}", path, mode, fh)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            node = self._node_from_handle_or_path(path, fh)
            self._require_owner(node)
            kind = {"dir": S_IFDIR, "file": S_IFREG, "symlink": S_IFLNK}[node["kind"]]
            now = time_ns()
            self.metadata.set_node_mode(node["id"], kind | (mode & 0o7777), now)
            logger.debug(
                "修改权限完成：path={}，inode={}，mode={:o}", path, node["id"], mode
            )

    def _chown(self, path: str, uid: int, gid: int, fh=None) -> None:
        logger.debug("修改所有者：path={}，uid={}，gid={}，fh={}", path, uid, gid, fh)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            node = self._node_from_handle_or_path(path, fh)
            if self._caller_ids()[0] != 0:
                raise FuseOSError(errno.EPERM)
            next_uid = node["uid"] if uid == -1 else uid
            next_gid = node["gid"] if gid == -1 else gid
            now = time_ns()
            self.metadata.set_node_owner(node["id"], next_uid, next_gid, now)
            logger.debug(
                "修改所有者完成：path={}，inode={}，uid={}，gid={}",
                path,
                node["id"],
                next_uid,
                next_gid,
            )

    def _utimens(self, path: str, times, fh=None) -> None:
        logger.debug("更新时间戳：path={}，times={}，fh={}", path, times, fh)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            node = self._node_from_handle_or_path(path, fh)
            if self._caller_ids()[0] != 0 and self._caller_ids()[0] != node["uid"]:
                self._require_access(node, os.W_OK)
            now = time_ns()
            atime, mtime = times if times else (now, now)
            self.metadata.set_node_times(node["id"], atime, mtime, now)
            logger.debug("更新时间戳完成：path={}，inode={}", path, node["id"])

    def _getxattr(self, path: str, name: str) -> bytes:
        logger.debug("读取扩展属性：path={}，name={}", path, name)
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            node = self.metadata.get_node(path)
            self._require_access(node, os.R_OK)
            row = self.metadata.xattr(node["id"], name)
            if row is None:
                raise FuseOSError(ENOATTR)
            return bytes(row["value"])

    def _listxattr(self, path: str) -> list[str]:
        logger.debug("列出扩展属性：path={}", path)
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            node = self.metadata.get_node(path)
            self._require_access(node, os.R_OK)
            return self.metadata.xattr_names(node["id"])

    def _setxattr(self, path: str, name: str, value: bytes, options: int) -> None:
        logger.debug(
            "设置扩展属性：path={}，name={}，bytes={}，options={:#x}",
            path,
            name,
            len(value),
            options,
        )
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            node = self.metadata.get_node(path)
            self._require_access(node, os.W_OK)
            exists = self.metadata.xattr(node["id"], name) is not None
            if options & XATTR_CREATE and exists:
                raise FuseOSError(errno.EEXIST)
            if options & XATTR_REPLACE and not exists:
                raise FuseOSError(ENOATTR)
            self.metadata.set_xattr(node["id"], name, value, time_ns())
            logger.debug(
                "设置扩展属性完成：path={}，inode={}，name={}", path, node["id"], name
            )

    def _removexattr(self, path: str, name: str) -> None:
        logger.debug("删除扩展属性：path={}，name={}", path, name)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            node = self.metadata.get_node(path)
            self._require_access(node, os.W_OK)
            if not self.metadata.remove_xattr(node["id"], name, time_ns()):
                raise FuseOSError(ENOATTR)
            logger.debug(
                "删除扩展属性完成：path={}，inode={}，name={}", path, node["id"], name
            )

    def _require_amode_access(self, node, amode: int) -> None:
        posix_amode = amode & (os.R_OK | os.W_OK | os.X_OK)
        if amode & MACOS_DELETE_ACCESS and node["parent_id"] is not None:
            parent = self.metadata.node_by_id(node["parent_id"])
            self._require_access(parent, os.W_OK | os.X_OK)
            posix_amode &= ~os.X_OK
        self._require_access(node, posix_amode)

    def _access(self, path: str, amode: int) -> int:
        posix_amode = amode & (os.R_OK | os.W_OK | os.X_OK)
        logger.debug(
            "检查访问权限：path={}，amode={:#x}，posix_amode={:#x}",
            path,
            amode,
            posix_amode,
        )
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            node = self.metadata.get_node(path)
            self._require_amode_access(node, amode)
            return 0

    def _lock_file(
        self, path: str, fh, cmd: int, lock: dict[str, int]
    ) -> dict[str, int] | None:
        logger.debug("处理文件锁：path={}，fh={}，cmd={}，lock={}", path, fh, cmd, lock)
        with self.metadata.read_transaction():
            node = self._node_from_handle_or_path(path, fh)
            if node["kind"] != "file":
                raise FuseOSError(errno.EISDIR)
            owner = self.handles.lock_owner(fh) or self._lock_owner()
            uid, _gid, pid = self._caller_ids()
            lock.setdefault("l_pid", pid)
        with self._lock:
            result = self.locks.apply(
                node["id"], owner, int(lock.get("l_pid", pid)), cmd, lock
            )
            logger.debug(
                "文件锁处理完成：path={}，inode={}，result={}", path, node["id"], result
            )
            return result

    def _statfs(self) -> dict[str, int]:
        logger.debug("读取文件系统容量统计")
        hot = os.statvfs(self.tier1)
        cold = os.statvfs(self.tier2)
        block_size = hot.f_frsize or hot.f_bsize

        def blocks(st, attr: str) -> int:
            return getattr(st, attr) * (st.f_frsize or st.f_bsize) // block_size

        return {
            "f_bavail": blocks(hot, "f_bavail") + blocks(cold, "f_bavail"),
            "f_bfree": blocks(hot, "f_bfree") + blocks(cold, "f_bfree"),
            "f_blocks": blocks(hot, "f_blocks") + blocks(cold, "f_blocks"),
            "f_bsize": block_size,
            "f_ffree": hot.f_ffree + cold.f_ffree,
            "f_files": hot.f_files + cold.f_files,
            "f_flag": hot.f_flag,
        }
