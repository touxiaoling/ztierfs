"""基于 FUSE 调用方 uid/gid 的简易 POSIX mode 权限检查（含 root 特例）。"""

import errno
import os

from loguru import logger
from macfusepy import FuseOSError

from .fs_mixins import FileSystemMixinBase


class AccessControlMixin(FileSystemMixinBase):
    """在 FUSE 请求上下文中，按调用方 **uid/gid** 对 inode 的 **mode** 做类 POSIX 的 R/W/X 检查。

    与标准「三位 user/group/other 权限位」一致：先匹配拥有者、再组、再其他；非 root
    时仅使用与当前调用方对应的那一段位。``os.F_OK`` 只要求 inode 存在。

    **root（有效 uid 为 0）**：读/写不依据 mode 位限制；若需 **执行**（``os.X_OK``），
    则仍要求 mode 中 **至少含一位可执行**（``0o111`` 中任一为真），否则 ``EACCES``——
    避免 root 对完全没有执行位的 inode 执行。

    典型用于 ``open``、路径遍历 ``access``、以及依赖 inode mode 的其他校验路径。
    """

    def _require_open_access(self, node, flags: int) -> None:
        """根据 ``open(2)`` 的访问模式（``O_RDONLY`` / ``O_WRONLY`` / ``O_RDWR``）映射为
        ``os.R_OK`` / ``os.W_OK`` 组合并调用 `_require_access`。若带 ``O_TRUNC``，额外要求写权限。
        """
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
        """校验调用方对 inode 的有效权限 ``mask``（``R_OK`` / ``W_OK`` / ``X_OK`` 可组合）。

        ``node`` 为 ``None`` 时视为不存在，抛出 ``ENOENT``。``mask == F_OK`` 仅检查存在性。

        否则从 `_caller_ids` 取有效 uid/gid；非 root 时若有效 uid 等于 inode 的 ``uid`` 则用
        owner 位段，否则若有效 gid 等于 inode 的 ``gid``、或（有效 uid 等于进程真实 uid 且
        inode 的 ``gid`` 属于 ``os.getgroups()``）则用 group 位段，否则用 other 位段。
        root 行为见类文档说明。
        """
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
        """要求操作者为 inode 的拥有者（``caller uid == node uid``），否则 ``EPERM``。

        **root（uid 0）** 始终通过，与常见 ``chown``/部分元数据操作语义一致。
        """
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
        """返回当前 FUSE 调用上下文中的 ``(uid, gid, pid)``。

        通过 `_caller_provider`（通常来自请求上下文）；不可用时回退为 ``os.getuid`` /
        ``os.getgid`` / ``os.getpid``，用于无请求上下文时的降级。
        """
        try:
            return self._caller_provider()
        except RuntimeError:
            return os.getuid(), os.getgid(), os.getpid()

    def _creation_owner(self) -> tuple[int, int]:
        """新建 inode（文件/目录/链接等）时采用的 **拥有者 uid** 与 **主 gid**，取自 `_caller_ids`。"""
        uid, gid, _pid = self._caller_ids()
        return uid, gid

    def _lock_owner(self) -> int:
        """本进程内 POSIX advisory 锁的「锁主体」标识：将 **uid** 与 **pid** 组合为整型，用于区分不同进程/用户。"""
        uid, _gid, pid = self._caller_ids()
        return (uid << 32) ^ pid
