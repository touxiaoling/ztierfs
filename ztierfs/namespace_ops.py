"""命名空间侧仍需路径语义的辅助操作。"""

import errno
import os

from time import time_ns

from loguru import logger
from macfusepy import FuseOSError

from .fs_mixins import FileSystemMixinBase

RENAME_NOREPLACE = 0x1


class NamespaceOpsMixin(FileSystemMixinBase):
    """保留 macOS clonefile 的路径语义实现；其它命名空间回调走 `InodeFuseMixin`。"""

    def _clonefile(self, source: str, target: str) -> None:
        """为 `target` 新建普通文件 inode，共享源文件数据分块和 xattr。"""
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
