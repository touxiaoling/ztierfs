"""基于 FUSE 调用方 uid/gid 的简易 POSIX mode 权限检查（含 root 特例）。"""

import errno
import os

from loguru import logger
from macfusepy import FuseOSError

from .fs_mixins import FileSystemMixinBase


class AccessControlMixin(FileSystemMixinBase):
    """open/access/chmod 等路径上的 RWX 与打开模式校验。"""

    def _require_open_access(self, node, flags: int) -> None:
        access_mode = flags & os.O_ACCMODE
        if access_mode == os.O_RDONLY:
            self._require_access(node, os.R_OK)
        elif access_mode == os.O_WRONLY:
            self._require_access(node, os.W_OK)
        else:
            self._require_access(node, os.R_OK | os.W_OK)
        if flags & os.O_TRUNC:
            self._require_access(node, os.W_OK)

    def _require_access(self, node, mask: int) -> None:
        if node is None:
            raise FuseOSError(errno.ENOENT)
        if mask == os.F_OK:
            return
        uid, gid, _pid = self._caller_ids()
        if uid == 0:
            if mask & os.X_OK and not node["mode"] & 0o111:
                logger.debug(
                    "root 执行权限检查失败：inode={}，mask={:#x}", node["id"], mask
                )
                raise FuseOSError(errno.EACCES)
            return
        if uid == node["uid"]:
            bits = (node["mode"] >> 6) & 0o7
        elif gid == node["gid"] or (
            uid == os.getuid() and node["gid"] in os.getgroups()
        ):
            bits = (node["mode"] >> 3) & 0o7
        else:
            bits = node["mode"] & 0o7
        if mask & os.R_OK and not bits & 0o4:
            logger.debug(
                "读权限检查失败：inode={}，uid={}，gid={}，mask={:#x}",
                node["id"],
                uid,
                gid,
                mask,
            )
            raise FuseOSError(errno.EACCES)
        if mask & os.W_OK and not bits & 0o2:
            logger.debug(
                "写权限检查失败：inode={}，uid={}，gid={}，mask={:#x}",
                node["id"],
                uid,
                gid,
                mask,
            )
            raise FuseOSError(errno.EACCES)
        if mask & os.X_OK and not bits & 0o1:
            logger.debug(
                "执行权限检查失败：inode={}，uid={}，gid={}，mask={:#x}",
                node["id"],
                uid,
                gid,
                mask,
            )
            raise FuseOSError(errno.EACCES)

    def _require_owner(self, node) -> None:
        uid, _gid, _pid = self._caller_ids()
        if uid != 0 and uid != node["uid"]:
            logger.debug(
                "所有者权限检查失败：inode={}，caller_uid={}，owner_uid={}",
                node["id"],
                uid,
                node["uid"],
            )
            raise FuseOSError(errno.EPERM)

    def _caller_ids(self) -> tuple[int, int, int]:
        try:
            return self._caller_provider()
        except RuntimeError:
            return os.getuid(), os.getgid(), os.getpid()

    def _creation_owner(self) -> tuple[int, int]:
        uid, gid, _pid = self._caller_ids()
        return uid, gid

    def _lock_owner(self) -> int:
        uid, _gid, pid = self._caller_ids()
        return (uid << 32) ^ pid
