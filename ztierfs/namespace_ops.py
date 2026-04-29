import errno
import os

from stat import S_IFDIR, S_IFLNK, S_IFREG, S_ISREG
from time import time_ns

from loguru import logger
from macfusepy import FuseOSError

from .fs_mixins import FileSystemMixinBase

RENAME_NOREPLACE = 0x1


class NamespaceOpsMixin(FileSystemMixinBase):
    def _readdir(self, path: str):
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
            logger.debug("读取目录完成：path={}，inode={}，entries={}", path, node["id"], len(children))
            return [
                (".", self._attrs_from_node(node)),
                ("..", self._attrs_from_node(parent_node)),
                *((child["name"], self._attrs_from_node(child)) for child in children),
            ]

    def _mkdir(self, path: str, mode: int):
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
        logger.debug("创建设备/普通节点请求：path={}，mode={:o}，dev={}", path, mode, dev)
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
            logger.debug("创建普通文件节点完成：path={}，parent_inode={}", path, parent["id"])
            return self._attrs_from_node(node)

    def _symlink(self, target: str, source: str):
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
            logger.debug("创建符号链接完成：target={}，parent_inode={}", target, parent["id"])
            return self._attrs_from_node(node)

    def _readlink(self, path: str) -> str:
        logger.debug("读取符号链接：path={}", path)
        with self.metadata.transaction():
            node = self.metadata.get_node(path)
            if node["kind"] != "symlink":
                raise FuseOSError(errno.EINVAL)
            now = time_ns()
            self.metadata.touch_node_atime(node["id"], now)
            return node["symlink_target"]

    def _link(self, target: str, source: str) -> None:
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
            logger.debug("创建硬链接完成：source_inode={}，target={}", source_node["id"], target)

    def _clonefile(self, source: str, target: str) -> None:
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
            logger.debug("clonefile 完成：source_inode={}，target={}", source_node["id"], target)

    def _unlink(self, path: str) -> None:
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
        logger.debug("重命名：old={}，new={}，flags={:#x}", old, new, flags)
        if flags & ~RENAME_NOREPLACE:
            logger.warning("拒绝未知 rename flag：old={}，new={}，flags={:#x}", old, new, flags)
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
                    logger.debug("重命名目标与源相同，跳过：old={}，new={}，inode={}", old, new, source["id"])
                    return
                if target["kind"] == "dir" and source["kind"] != "dir":
                    raise FuseOSError(errno.EISDIR)
                if target["kind"] != "dir" and source["kind"] == "dir":
                    raise FuseOSError(errno.ENOTDIR)
                if target["kind"] == "dir" and self.metadata.has_children(target["id"]):
                    raise FuseOSError(errno.ENOTEMPTY)
                self._remove_entry_node(target)
                logger.debug("重命名覆盖目标：target_inode={}，target_kind={}", target["id"], target["kind"])

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
