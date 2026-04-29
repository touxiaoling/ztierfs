"""目录树与命名空间：mkdir、link、symlink、rename、readdir 等路径级操作。

在已规范化路径上操作 inode 与 `dir_entries`；返回的 `errno` 对齐常见 POSIX 期望（如 `ENOTDIR`、
`EEXIST`）。普通文件字节读写不在此模块。
"""

import errno
import os

from stat import S_IFDIR, S_IFLNK, S_IFREG, S_ISREG
from time import time_ns

from loguru import logger
from macfusepy import FuseOSError

from .fs_mixins import FileSystemMixinBase

RENAME_NOREPLACE = 0x1


class NamespaceOpsMixin(FileSystemMixinBase):
    """在已规范化路径上维护命名空间：目录项的增删改查、硬链接与符号链接、重命名与 macOS clonefile。

    目录相关：``mkdir`` / ``rmdir`` / ``readdir`` 校验父目录写权限与目标类型（如 ``ENOTDIR``、
    ``EISDIR``、``ENOTEMPTY``）；根目录不可 ``rmdir``（``EBUSY``）。

    ``rename``：支持同 inode 改名无操作、目录不能迁入自身子树（``EINVAL``）、目标存在时可替换
    （非目录覆盖目录项等类型组合遵循 POSIX）；``flags`` 仅识别 ``RENAME_NOREPLACE``，目标已存在
    且带该标志时返回 ``EEXIST``；未知 ``flags`` 位返回 ``EINVAL``。

    ``clonefile``：源须为普通文件；在目标父目录下新建**独立** inode，复制分块引用（递增块引用
    计数）、内联 payload 与扩展属性，**不**重新压缩或重写内容寻址块数据（与 APFS 语义相近的
    逻辑克隆）。目录或非常规类型会拒绝（``EISDIR`` / ``EINVAL``）；目标路径已有目录项则
    ``EEXIST``。

    普通文件的字节级读写不在此 mixin。
    """

    def _readdir(self, path: str):
        """列出目录：返回 ``.``、``..`` 及子项名与属性；要求节点为目录且对调用方可读可进入。

        会更新目录的访问时间（atime）。非目录路径返回 ``ENOTDIR``。
        """
        logger.debug("读取目录：path={}", path)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            node = self.metadata.get_node(path)
            if node["kind"] != "dir":
                raise FuseOSError(errno.ENOTDIR)
            self._require_access(node, os.R_OK | os.X_OK)
            now = time_ns()
            children = self.metadata.children(node["id"])
            parent_node = (
                self.metadata.node_by_id(node["parent_id"])
                if node["parent_id"] is not None
                else node
            )
            self.metadata.touch_node_atime(node["id"], now)
            logger.debug(
                "读取目录完成：path={}，inode={}，entries={}",
                path,
                node["id"],
                len(children),
            )
            return [
                (".", self._attrs_from_node(node)),
                ("..", self._attrs_from_node(parent_node)),
                *((child["name"], self._attrs_from_node(child)) for child in children),
            ]

    def _mkdir(self, path: str, mode: int):
        """在父目录下创建空子目录；``mode`` 的低 12 位与目录类型位写入 inode。

        父目录需写权限与执行权限；同名已存在则 ``EEXIST``。
        """
        logger.debug("创建目录：path={}，mode={:o}", path, mode)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            parent, name = self.metadata.parent_and_name(path)
            self._require_access(parent, os.W_OK | os.X_OK)
            if self.metadata.child(parent["id"], name) is not None:
                raise FuseOSError(errno.EEXIST)
            now = time_ns()
            inode_id = self.metadata.insert_node(
                parent["id"],
                name,
                "dir",
                S_IFDIR | (mode & 0o7777),
                *self._creation_owner(),
                now,
            )
            node = self.metadata.node_by_id(inode_id)
            logger.debug("创建目录完成：path={}，parent_inode={}", path, parent["id"])
            return self._attrs_from_node(node)

    def _mknod(self, path: str, mode: int, dev: int):
        """内部：处理 mknod。"""
        logger.debug(
            "创建设备/普通节点请求：path={}，mode={:o}，dev={}", path, mode, dev
        )
        if not S_ISREG(mode):
            logger.warning("拒绝创建非普通文件节点：path={}，mode={:o}", path, mode)
            raise FuseOSError(errno.ENOTSUP)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            parent, name = self.metadata.parent_and_name(path)
            self._require_access(parent, os.W_OK | os.X_OK)
            if self.metadata.child(parent["id"], name) is not None:
                raise FuseOSError(errno.EEXIST)
            now = time_ns()
            inode_id = self.metadata.insert_node(
                parent["id"],
                name,
                "file",
                S_IFREG | (mode & 0o7777),
                *self._creation_owner(),
                now,
            )
            node = self.metadata.node_by_id(inode_id)
            logger.debug(
                "创建普通文件节点完成：path={}，parent_inode={}", path, parent["id"]
            )
            return self._attrs_from_node(node)

    def _symlink(self, target: str, source: str):
        """内部：处理 symlink。"""
        logger.debug("创建符号链接：target={}，source={}", target, source)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            parent, name = self.metadata.parent_and_name(target)
            self._require_access(parent, os.W_OK | os.X_OK)
            if self.metadata.child(parent["id"], name) is not None:
                raise FuseOSError(errno.EEXIST)
            now = time_ns()
            inode_id = self.metadata.insert_node(
                parent["id"],
                name,
                "symlink",
                S_IFLNK | 0o777,
                *self._creation_owner(),
                now,
                symlink_target=source,
            )
            node = self.metadata.node_by_id(inode_id)
            logger.debug(
                "创建符号链接完成：target={}，parent_inode={}", target, parent["id"]
            )
            return self._attrs_from_node(node)

    def _readlink(self, path: str) -> str:
        """内部：处理 readlink。"""
        logger.debug("读取符号链接：path={}", path)
        with self.metadata.transaction():
            node = self.metadata.get_node(path)
            if node["kind"] != "symlink":
                raise FuseOSError(errno.EINVAL)
            now = time_ns()
            self.metadata.touch_node_atime(node["id"], now)
            return node["symlink_target"]

    def _link(self, target: str, source: str) -> None:
        """内部：处理 link。"""
        logger.debug("创建硬链接：source={}，target={}", source, target)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            source_node = self.metadata.get_node(source)
            if source_node["kind"] == "dir":
                raise FuseOSError(errno.EPERM)
            parent, name = self.metadata.parent_and_name(target)
            self._require_access(parent, os.W_OK | os.X_OK)
            if self.metadata.child(parent["id"], name) is not None:
                raise FuseOSError(errno.EEXIST)
            now = time_ns()
            self.metadata.link_node(parent["id"], name, source_node["id"], now)
            logger.debug(
                "创建硬链接完成：source_inode={}，target={}", source_node["id"], target
            )

    def _clonefile(self, source: str, target: str) -> None:
        """macOS ``clonefile`` 语义：为 ``target`` 新建普通文件 inode，共享源文件的数据分块
        （增加块引用计数）并复制 xattr / 内联 payload 元数据；不重新哈希、压缩或重写块文件。

        源必须为普通文件（目录 ``EISDIR``，其它类型 ``EINVAL``）；需对源有读权限，对目标父
        目录有写+执行权限；``target`` 最后一级名在父目录下不得已存在（``EEXIST``）。
        """
        logger.debug("clonefile：source={}，target={}", source, target)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            source_node = self.metadata.get_node(source)
            if source_node["kind"] == "dir":
                raise FuseOSError(errno.EISDIR)
            if source_node["kind"] != "file":
                raise FuseOSError(errno.EINVAL)
            self._require_access(source_node, os.R_OK)

            parent, name = self.metadata.parent_and_name(target)
            self._require_access(parent, os.W_OK | os.X_OK)
            if self.metadata.child(parent["id"], name) is not None:
                raise FuseOSError(errno.EEXIST)

            now = time_ns()
            uid, gid = self._creation_owner()
            self.metadata.clone_file_node(
                source_node["id"],
                parent["id"],
                name,
                mode=source_node["mode"],
                uid=uid,
                gid=gid,
                size=source_node["size"],
                now=now,
            )
            logger.debug(
                "clonefile 完成：source_inode={}，target={}", source_node["id"], target
            )

    def _unlink(self, path: str) -> None:
        """内部：处理 unlink。"""
        logger.debug("删除文件目录项：path={}", path)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            node = self.metadata.get_node(path)
            if node["kind"] == "dir":
                raise FuseOSError(errno.EISDIR)
            parent = self.metadata.node_by_id(node["parent_id"])
            self._require_access(parent, os.W_OK | os.X_OK)
            self._remove_entry_node(node)
            logger.debug("删除文件目录项完成：path={}，inode={}", path, node["id"])

    def _rmdir(self, path: str) -> None:
        """删除**空**目录：移除目录项并在无硬链接时回收 inode。

        非目录返回 ``ENOTDIR``；根 inode（通常为 1）返回 ``EBUSY``；仍有子项返回
        ``ENOTEMPTY``。父目录需写权限与执行权限。
        """
        logger.debug("删除目录：path={}", path)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            node = self.metadata.get_node(path)
            if node["kind"] != "dir":
                raise FuseOSError(errno.ENOTDIR)
            if node["id"] == 1:
                raise FuseOSError(errno.EBUSY)
            if self.metadata.has_children(node["id"]):
                raise FuseOSError(errno.ENOTEMPTY)
            parent = self.metadata.node_by_id(node["parent_id"])
            self._require_access(parent, os.W_OK | os.X_OK)
            self._remove_entry_node(node)
            logger.debug("删除目录完成：path={}，inode={}", path, node["id"])

    def _rename(self, old: str, new: str, flags: int) -> None:
        """将 ``old`` 所指的目录项移动到 ``new``（可跨父目录）；源与目标父目录均需写+执行权限。

        ``flags``：除 ``RENAME_NOREPLACE``（0x1）外若有其它位则 ``EINVAL``。带
        ``RENAME_NOREPLACE`` 且目标名已存在则 ``EEXIST``。若目标与源为同一 inode 与路径
        语义上的同一项则直接返回。

        若目标已存在且允许替换：文件/目录/符号链接类型组合需合法（例如不能把目录项改名为已存在
        非目录名等）；替换目录时该目录必须为空（``ENOTEMPTY``）。若源为目录，新父目录不能是
        源目录的后代（``EINVAL``）。替换目标会先 ``unlink`` 式移除目标目录项（含 inode 回收
        规则由下层实现）。
        """
        logger.debug("重命名：old={}，new={}，flags={:#x}", old, new, flags)
        if flags & ~RENAME_NOREPLACE:
            logger.warning(
                "拒绝未知 rename flag：old={}，new={}，flags={:#x}", old, new, flags
            )
            raise FuseOSError(errno.EINVAL)
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
            source = self.metadata.get_node(old)
            old_parent = self.metadata.node_by_id(source["parent_id"])
            parent, name = self.metadata.parent_and_name(new)
            self._require_access(old_parent, os.W_OK | os.X_OK)
            self._require_access(parent, os.W_OK | os.X_OK)
            if source["kind"] == "dir" and self.metadata.is_descendant(
                parent["id"], source["id"]
            ):
                raise FuseOSError(errno.EINVAL)
            target = self.metadata.child(parent["id"], name)
            if target is not None:
                if flags & RENAME_NOREPLACE:
                    raise FuseOSError(errno.EEXIST)
                if target["id"] == source["id"]:
                    logger.debug(
                        "重命名目标与源相同，跳过：old={}，new={}，inode={}",
                        old,
                        new,
                        source["id"],
                    )
                    return
                if target["kind"] == "dir" and source["kind"] != "dir":
                    raise FuseOSError(errno.EISDIR)
                if target["kind"] != "dir" and source["kind"] == "dir":
                    raise FuseOSError(errno.ENOTDIR)
                if target["kind"] == "dir" and self.metadata.has_children(target["id"]):
                    raise FuseOSError(errno.ENOTEMPTY)
                self._remove_entry_node(target)
                logger.debug(
                    "重命名覆盖目标：target_inode={}，target_kind={}",
                    target["id"],
                    target["kind"],
                )

            now = time_ns()
            self.metadata.move_entry(
                source["parent_id"],
                source["name"],
                parent["id"],
                name,
                source["id"],
                now,
            )
            logger.debug("重命名完成：old={}，new={}，inode={}", old, new, source["id"])
