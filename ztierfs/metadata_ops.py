"""inode 属性映射、access 辅助与 statfs。"""

import errno
import os

from macfusepy import LowLevelAttr

from .fs_mixins import FileSystemMixinBase

# 扩展属性：macOS 常用 ENOATTR；部分平台回落为 ENODATA。
ENOATTR = getattr(errno, "ENOATTR", getattr(errno, "ENODATA", errno.ENOENT))
XATTR_CREATE = 0x1
XATTR_REPLACE = 0x2
MACOS_DELETE_ACCESS = 0x800


class MetadataOpsMixin(FileSystemMixinBase):
    """供 low-level FUSE 回调复用的 POSIX 元数据辅助逻辑。"""

    def _allocated_bytes_from_node(self, node) -> int:
        """估算用于 `st_blocks` 的已分配字节数。"""
        if node["kind"] == "file":
            return self.metadata.file_allocated_size(node["id"])
        return int(node["size"])

    def _attrs_from_node(self, node) -> LowLevelAttr:
        """将 inode 记录转为 FUSE `LowLevelAttr`。"""
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

    def _require_amode_access(self, node, amode: int) -> None:
        """处理 POSIX `access(2)` 与 macOS 删除访问探测。"""
        posix_amode = amode & (os.R_OK | os.W_OK | os.X_OK)
        if amode & MACOS_DELETE_ACCESS and node["parent_id"] is not None:
            parent = self.metadata.node_by_id(node["parent_id"])
            self._require_access(parent, os.W_OK | os.X_OK)
            posix_amode &= ~os.X_OK
        self._require_access(node, posix_amode)

    def _statfs(self) -> dict[str, int]:
        """合并热层与冷层挂载点的 `statvfs` 结果。"""
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
