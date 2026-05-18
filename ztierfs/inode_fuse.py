"""macfusepy 以 inode 为主的低层 FUSE 回调（lookup、readdir、read/write、xattr、锁等）。

与路径级 API 的分工：本模块用 ``parent`` + ``name`` 或 ``ino`` 直接定位元数据；**文件句柄**
``fh`` 由 ``handles`` 分配时绑定「打开时的 inode」。对 ``getattr`` / ``setattr`` / ``read`` /
``write`` / ``getlk`` / ``setlk`` 等，若 ``fh`` 能解析出 ``file_id``，则**以句柄上的 inode 为准**，
``ino`` 仅作回退——从而在 rename/unlink 之后仍对已打开文件按**打开时**的 inode 读写，符合 POSIX。

**目录句柄**：``opendir`` / ``readdir`` / ``releasedir`` 使用进程内自增的目录 ``fh``（``_dir_handles``），
与文件 ``fh`` 无关。

**错误约定**：对内核可见的错误以 ``FuseOSError(errno)`` 或 ``OSError``（带 ``errno``）抛出；常见值包括
``ENOENT``（inode/子项不存在）、``ENOTDIR`` / ``EISDIR``（类型不符）、``EEXIST`` / ``ENOTEMPTY``、
``EINVAL``（非法参数或 rename 环）、``EPERM`` / ``EACCES``（权限）、``ENOTSUP``（不支持的 mknod）、
``ENOATTR``（扩展属性不存在，见 ``metadata_ops.ENOATTR``）。``setattr`` 实际更新哪些字段由 ``to_set`` 中的
``FUSE_SET_ATTR_*`` 位决定。
"""

import errno
import fcntl
import os

from collections.abc import Mapping
from stat import S_IFDIR, S_IFLNK, S_IFREG, S_ISREG
from time import perf_counter_ns, time_ns

from loguru import logger
from macfusepy import FuseOSError, InodeOperations, LowLevelAttr, LowLevelEntry
from .fs_mixins import FileSystemMixinBase
from .metadata_ops import ENOATTR, XATTR_CREATE, XATTR_REPLACE
from .namespace_ops import RENAME_NOREPLACE
from .perf import collect_perf

FUSE_SET_ATTR_MODE = 1 << 0
FUSE_SET_ATTR_UID = 1 << 1
FUSE_SET_ATTR_GID = 1 << 2
FUSE_SET_ATTR_SIZE = 1 << 3
FUSE_SET_ATTR_ATIME = 1 << 4
FUSE_SET_ATTR_MTIME = 1 << 5


class InodeFuseMixin(InodeOperations, FileSystemMixinBase):
    """实现 ``InodeOperations``：在 inode 号与目录项名上完成 FUSE 低层语义。

    与 ``NamespaceOpsMixin`` / ``FileOpsMixin`` 等组合后构成完整文件系统；本类方法多为 macfusepy
    入口，内部再调用 ``_*_inode`` 或共享辅助函数。日志与性能采样经 ``_run_fuse_op`` 包装。
    """

    def _decode_name(self, name: bytes) -> str:
        """将 FUSE 传入的目录项名 ``bytes`` 解码为 ``str``（UTF-8 + surrogateescape，与编码对称）。"""
        return name.decode("utf-8", "surrogateescape")

    def _node_by_ino(self, ino: int):
        """按 inode 号加载元数据行；不存在则 ``FuseOSError(ENOENT)``。"""
        row = self.metadata.node_by_id(ino)
        if row is None:
            raise FuseOSError(errno.ENOENT)
        return row

    def _file_node_from_ino_or_fh(self, ino: int, fh):
        """解析「当前操作针对的」文件 inode：若 ``fh`` 对应打开文件句柄则取其绑定的 ``file_id``，否则用 ``ino``。

        用于在 **``ino`` 与回调参数可能已过时**（例如 rename 后）时仍以打开时 inode 为准；``ENOENT`` 来自
        ``_node_by_ino``。
        """
        file_id = self.handles.file_id(fh)
        if file_id is not None:
            return self._node_by_ino(file_id)
        return self._node_by_ino(ino)

    def _entry_from_node(self, name: bytes, node, next_id: int) -> LowLevelEntry:
        """由子节点行构造 ``LowLevelEntry``，并缓存 inode→显示名供块路径等使用。"""
        self._remember_inode_name(node["id"], self._decode_name(name))
        return LowLevelEntry(name, node["id"], self._attrs_from_node(node), next_id)

    def _remember_inode_name(self, inode_id: int, name: str) -> None:
        """记录 inode 最近对应的单路径名（进程内字典，非持久化）。"""
        names = getattr(self, "_inode_names", None)
        if names is None:
            names = {}
            self._inode_names = names
        names[inode_id] = name

    def _name_for_inode(self, node) -> str:
        """返回用于日志/块操作的规范化绝对路径样式字符串（优先缓存名，否则节点字段）。"""
        names = getattr(self, "_inode_names", {})
        name = names.get(node["id"], node["name"] or "")
        return name if name.startswith("/") else f"/{name}"

    def init(self, conn=None, cfg=None) -> None:
        """FUSE 会话初始化：可选记录连接能力（``max_read`` / ``max_write`` 等）；无 inode/fh。"""
        if conn is not None:
            logger.info(
                "FUSE 连接能力：max_read={}，max_write={}，max_readahead={}，max_background={}",
                conn.max_read,
                conn.max_write,
                conn.max_readahead,
                conn.max_background,
            )

    def destroy(self) -> None:
        """FUSE 销毁钩子：释放资源（委托 ``close()``）。"""
        self.close()

    def close(self) -> None:
        """关闭预读线程池、块存储与元数据连接，并输出性能分析收尾日志。"""
        if getattr(self, "_ztierfs_closed", False):
            return
        executor = getattr(self, "_readahead_executor", None)
        if executor is not None:
            executor.shutdown(wait=True)
            self._readahead_executor = None
        self.block_store.close()
        self.metadata.close()
        profiler = getattr(self, "_operation_profiler", None)
        if profiler is not None:
            profiler.log_final()
        self._ztierfs_closed = True

    def _log_value(self, value):
        """将调试日志中的参数值转为可打印形式（缩短 ``bytes``、递归容器）。"""
        if isinstance(value, bytes):
            return f"<bytes {len(value)}>"
        if isinstance(value, tuple):
            return tuple(self._log_value(item) for item in value)
        if isinstance(value, list):
            return [self._log_value(item) for item in value]
        return value

    def _run_fuse_op(self, func, /, *args):
        """包装 FUSE 内部实现：打 debug 日志、可选性能计数；``OSError`` 原样上抛（保留 ``errno``）。"""
        op = func.__name__.removesuffix("_inode").removeprefix("_")

        def _logged_args():
            """惰性求值：与外层相同的参数元组，经 ``_log_value`` 脱敏。"""
            return tuple(self._log_value(arg) for arg in args)

        logger.opt(lazy=True).debug("FUSE inode {}{}", lambda: op, _logged_args)
        profiler = getattr(self, "_operation_profiler", None)
        try:
            if profiler is None:
                result = func(*args)
            else:
                with collect_perf() as counters:
                    started = perf_counter_ns()
                    try:
                        result = func(*args)
                    finally:
                        counters.add_time(f"fuse.{op}", perf_counter_ns() - started)
                        profiler.record(counters)
        except OSError as exc:
            errno_ = exc.errno
            logger.opt(lazy=True).debug(
                "FUSE inode {}{} -> OSError({})",
                lambda: op,
                _logged_args,
                lambda: errno_,
            )
            raise
        logger.opt(lazy=True).debug(
            "FUSE inode {}{} -> {}",
            lambda: op,
            _logged_args,
            lambda: self._log_value(result),
        )
        return result

    def lookup(self, parent: int, name: bytes) -> LowLevelEntry:
        """在父目录 ``parent`` 下解析 ``name`` → ``LowLevelEntry``。

        **inode**：``parent`` 为目录 inode；返回子项的 ``ino``。无 ``fh``。

        **errno**：父非目录 ``ENOTDIR``；子项不存在 ``ENOENT``；权限等由下层 ``_require_*`` 抛出
        ``EACCES`` / ``EPERM`` 等。
        """
        return self._run_fuse_op(self._lookup_inode, parent, name)

    def _lookup_inode(self, parent: int, name: bytes) -> LowLevelEntry:
        """``lookup`` 的实现体（只读事务）。"""
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            parent_node = self._node_by_ino(parent)
            if parent_node["kind"] != "dir":
                raise FuseOSError(errno.ENOTDIR)
            node = self.metadata.child(parent, self._decode_name(name))
            if node is None:
                raise FuseOSError(errno.ENOENT)
            return self._entry_from_node(name, node, node["id"])

    def getattr(self, ino: int, fh=None) -> LowLevelAttr:
        """返回 inode ``ino`` 的 ``LowLevelAttr``。

        **inode vs fh**：若 ``fh`` 为打开文件句柄且能解析 ``file_id``，则属性取自该 inode（与 ``ino`` 可能
        不一致，见 rename 后已打开文件）。否则用 ``ino``。

        **errno**：本路径仅做元数据加载；节点不存在时 ``ENOENT``（``_node_by_ino``）。不在此处做
        ``access`` 式权限过滤。
        """
        return self._run_fuse_op(self._getattr_inode, ino, fh)

    def _getattr_inode(self, ino: int, fh=None) -> LowLevelAttr:
        """``getattr`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            return self._attrs_from_node(self._file_node_from_ino_or_fh(ino, fh))

    def setattr(self, ino: int, attrs: Mapping[str, int], to_set: int, fh=None):
        """按 ``to_set`` 位掩码更新元数据（mode/uid/gid/size/atime/mtime）。

        **inode vs fh**：内容锁与目标节点均用 ``handles.file_id(fh) or ino``；节点加载用
        ``_file_node_from_ino_or_fh``，与 ``getattr`` 一致优先句柄 inode。

        **errno**：非 root 改属主 ``EPERM``；对非文件截断 ``EISDIR``；写/属主相关权限 ``EACCES`` 等；
        ``ENOENT`` 来自缺失 inode。
        """
        return self._run_fuse_op(self._setattr_inode, ino, attrs, to_set, fh)

    def _setattr_inode(self, ino: int, attrs: Mapping[str, int], to_set: int, fh=None):
        """``setattr`` 的实现体（写事务 + 内容锁）。"""
        self._ensure_trash_directory_for_caller()
        with (
            self._content_lock(self.handles.file_id(fh) or ino),
            self.metadata.transaction(),
        ):
            node = self._file_node_from_ino_or_fh(ino, fh)
            now = time_ns()
            if to_set & FUSE_SET_ATTR_MODE:
                self._require_owner(node)
                kind = {"dir": S_IFDIR, "file": S_IFREG, "symlink": S_IFLNK}[
                    node["kind"]
                ]
                self.metadata.set_node_mode(
                    node["id"], kind | (attrs["st_mode"] & 0o7777), now
                )
            if to_set & (FUSE_SET_ATTR_UID | FUSE_SET_ATTR_GID):
                if self._caller_ids()[0] != 0:
                    raise FuseOSError(errno.EPERM)
                uid = attrs["st_uid"] if to_set & FUSE_SET_ATTR_UID else node["uid"]
                gid = attrs["st_gid"] if to_set & FUSE_SET_ATTR_GID else node["gid"]
                self.metadata.set_node_owner(node["id"], uid, gid, now)
            if to_set & FUSE_SET_ATTR_SIZE:
                if node["kind"] != "file":
                    raise FuseOSError(errno.EISDIR)
                self._require_access(node, os.W_OK)
                self.file_content.truncate_file(
                    node["id"], self._name_for_inode(node), attrs["st_size"]
                )
                node = self._node_by_ino(node["id"])
            if to_set & (FUSE_SET_ATTR_ATIME | FUSE_SET_ATTR_MTIME):
                if self._caller_ids()[0] != 0 and self._caller_ids()[0] != node["uid"]:
                    self._require_access(node, os.W_OK)
                atime = (
                    attrs["st_atime"]
                    if to_set & FUSE_SET_ATTR_ATIME
                    else node["atime_ns"]
                )
                mtime = (
                    attrs["st_mtime"]
                    if to_set & FUSE_SET_ATTR_MTIME
                    else node["mtime_ns"]
                )
                self.metadata.set_node_times(node["id"], atime, mtime, now)
            return self._attrs_from_node(self._node_by_ino(node["id"]))

    def open(self, ino: int, flags: int, fi=None) -> int:
        """打开普通文件：校验类型与打开权限，按需 ``O_TRUNC``，并分配新的**文件** ``fh``。

        **inode**：仅使用参数 ``ino``（新句柄绑定该 inode）。``fi`` 由框架传入，本实现未用。

        **errno**：非文件 ``EISDIR``；``_require_open_access`` 失败；``O_TRUNC`` 时写权限；``ENOENT``。
        """
        return self._run_fuse_op(self._open_inode, ino, flags)

    def _open_inode(self, ino: int, flags: int) -> int:
        """``open`` 的实现体；返回 ``handles.new(ino, ...)``。"""
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            if node["kind"] != "file":
                raise FuseOSError(errno.EISDIR)
            self._require_open_access(node, flags)
        if flags & os.O_TRUNC:
            with self._content_lock(ino), self.metadata.transaction():
                node = self._node_by_ino(ino)
                self.file_content.truncate_file(ino, self._name_for_inode(node), 0)
        return self.handles.new(ino, self._lock_owner())

    def read(self, ino: int, size: int, offset: int, fh) -> bytes:
        """从文件读取 ``size`` 字节（自 ``offset``）。

        **inode vs fh**：内容锁与读计划使用 ``handles.file_id(fh) or ino``；节点与 R_OK 检查用
        ``_file_node_from_ino_or_fh(ino, fh)``，保证 rename 后仍读打开时文件。

        **errno**：``ENOENT``、读权限 ``EACCES`` 等。
        """
        return self._run_fuse_op(self._read_inode, ino, size, offset, fh)

    def _read_inode(self, ino: int, size: int, offset: int, fh) -> bytes:
        """``read`` 的实现体（内容锁 + 读计划 + 可选预读/降级）。"""
        self._ensure_trash_directory_for_caller()
        inode_id = self.handles.file_id(fh) or ino
        with self._content_lock(inode_id):
            with self.metadata.read_transaction():
                node = self._file_node_from_ino_or_fh(ino, fh)
                self._require_access(node, os.R_OK)
                plan = self.file_content.plan_read(node, size, offset)
            data, accesses = self.file_content.execute_read_plan(plan)
            self._schedule_readahead(plan, offset, len(data), fh)
        if plan.chunks:
            now = time_ns()
            should_flush = self.metadata.defer_node_atime(plan.file_id, now)
            should_flush = (
                self.block_store.record_block_accesses(accesses, now) or should_flush
            )
            if should_flush:
                with self.metadata.transaction():
                    for access in accesses:
                        self.block_store.record_block_presence(access, now)
        if self.block_store.take_demotion_request():
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()
        return data

    def write(self, ino: int, data: bytes, offset: int, fh) -> int:
        """向文件写入数据，返回写入字节数。

        **inode vs fh**：与 ``read`` 相同——锁与提交路径用 ``file_id(fh) or ino``，元数据节点用
        ``_file_node_from_ino_or_fh``。

        **errno**：写权限 ``EACCES`` 等；``ENOENT``。
        """
        return self._run_fuse_op(self._write_inode, ino, data, offset, fh)

    def _write_inode(self, ino: int, data: bytes, offset: int, fh) -> int:
        """``write`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        inode_id = self.handles.file_id(fh) or ino
        with self._content_lock(inode_id):
            with self.metadata.read_transaction():
                node = self._file_node_from_ino_or_fh(ino, fh)
                self._require_access(node, os.W_OK)
                path = self._name_for_inode(node)
                plan = self.file_content.plan_write_file(node, path, data, offset)
            prepared = self.file_content.prepare_file_write(plan)
            with self.metadata.transaction():
                written = self.file_content.commit_prepared_write(prepared)
        if self.block_store.take_demotion_request():
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()
        return written

    def flush(self, ino: int, fh) -> None:
        """刷新挂起元数据（委托基类 ``_flush``）；框架传入 ``ino``/``fh`` 但本包装不区分 inode。

        **errno**：由 ``_flush`` / 事务实现决定（如 ``EIO``）。
        """
        self._run_fuse_op(self._flush)

    def fsync(self, ino: int, datasync: int, fh) -> None:
        """与 ``flush`` 相同：通过 ``_run_fuse_op`` 调用 ``_flush``；``datasync`` 未单独区分。"""
        self._run_fuse_op(self._flush)

    def release(self, ino: int, fh) -> int:
        """释放 ``open``/``create`` 返回的文件 ``fh``（``handles``）；``ino`` 由框架传入。"""
        return self._run_fuse_op(self._release, fh)

    def create(self, parent: int, name: bytes, mode: int, flags: int, fi):
        """在 ``parent`` 下创建或替换普通文件：返回 ``(LowLevelEntry, fh)``。

        **inode**：新或已有文件 inode；**fh** 为新文件句柄。

        **errno**：已存在且为目录 ``EISDIR``；父目录权限；``EEXIST`` 等由下层决定。
        """
        return self._run_fuse_op(self._create_inode, parent, name, mode, flags)

    def _create_inode(self, parent: int, name: bytes, mode: int, flags: int):
        """``create`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.transaction():
            parent_node = self._node_by_ino(parent)
            self._require_access(parent_node, os.W_OK | os.X_OK)
            existing = self.metadata.child(parent, decoded)
            now = time_ns()
            if existing is not None:
                if existing["kind"] != "file":
                    raise FuseOSError(errno.EISDIR)
                self._require_open_access(existing, flags | os.O_TRUNC)
                self.file_content.remove_file_chunks(existing["id"])
                self.metadata.reset_file_node(
                    existing["id"], S_IFREG | (mode & 0o7777), now
                )
                node = self._node_by_ino(existing["id"])
            else:
                inode_id = self.metadata.insert_node(
                    parent,
                    decoded,
                    "file",
                    S_IFREG | (mode & 0o7777),
                    *self._creation_owner(),
                    now,
                )
                node = self._node_by_ino(inode_id)
            fh = self.handles.new(node["id"], self._lock_owner())
            return self._entry_from_node(name, node, node["id"]), fh

    def mkdir(self, parent: int, name: bytes, mode: int) -> LowLevelEntry:
        """在父目录下创建子目录。

        **errno**：同名已存在 ``EEXIST``；父非目录或权限失败 ``ENOENT``/``EACCES`` 等。
        """
        return self._run_fuse_op(self._mkdir_inode, parent, name, mode)

    def _mkdir_inode(self, parent: int, name: bytes, mode: int) -> LowLevelEntry:
        """``mkdir`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.transaction():
            parent_node = self._node_by_ino(parent)
            self._require_access(parent_node, os.W_OK | os.X_OK)
            if self.metadata.child(parent, decoded) is not None:
                raise FuseOSError(errno.EEXIST)
            inode_id = self.metadata.insert_node(
                parent,
                decoded,
                "dir",
                S_IFDIR | (mode & 0o7777),
                *self._creation_owner(),
                time_ns(),
            )
            return self._entry_from_node(name, self._node_by_ino(inode_id), inode_id)

    def mknod(self, parent: int, name: bytes, mode: int, dev: int) -> LowLevelEntry:
        """仅支持普通文件位（``S_ISREG``）；否则 ``FuseOSError(ENOTSUP)``。``dev`` 未使用。

        **errno**：非普通文件 ``ENOTSUP``；创建路径上 ``EEXIST``、权限等。
        """
        if not S_ISREG(mode):
            raise FuseOSError(errno.ENOTSUP)
        return self._run_fuse_op(self._mknod_inode, parent, name, mode)

    def _mknod_inode(self, parent: int, name: bytes, mode: int) -> LowLevelEntry:
        """``mknod``（普通文件）的实现体。"""
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.transaction():
            parent_node = self._node_by_ino(parent)
            self._require_access(parent_node, os.W_OK | os.X_OK)
            if self.metadata.child(parent, decoded) is not None:
                raise FuseOSError(errno.EEXIST)
            inode_id = self.metadata.insert_node(
                parent,
                decoded,
                "file",
                S_IFREG | (mode & 0o7777),
                *self._creation_owner(),
                time_ns(),
            )
            return self._entry_from_node(name, self._node_by_ino(inode_id), inode_id)

    def opendir(self, ino: int, flags: int = 0, fi=None) -> int:
        """打开目录：分配**目录专用** ``fh``（存于 ``_dir_handles``），与文件 ``fh`` 空间独立。

        **errno**：非目录 ``ENOTDIR``；目录遍历权限 ``EACCES`` 等（见 ``_require_dir_inode``）。
        """
        return self._run_fuse_op(self._opendir_inode, ino, flags)

    def _opendir_inode(self, ino: int, flags: int = 0) -> int:
        """``opendir`` 的实现体；``flags``/``fi`` 当前未用于权限以外的逻辑。"""
        self._require_dir_inode(ino)
        with self._lock:
            handles = getattr(self, "_dir_handles", None)
            if handles is None:
                handles = {}
                self._dir_handles = handles
                self._next_dir_fh = 1
            fh = self._next_dir_fh
            self._next_dir_fh += 1
            handles[fh] = {"ino": ino, "cursors": {}}
            return fh

    def _require_dir_inode(self, ino: int):
        """断言 ``ino`` 为目录且调用方具备 ``R_OK|X_OK``；否则 ``ENOTDIR`` 或 ``EACCES``。"""
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            if node["kind"] != "dir":
                raise FuseOSError(errno.ENOTDIR)
            self._require_access(node, os.R_OK | os.X_OK)

    def readdir(self, ino: int, offset: int, size: int, fh, flags: int = 0):
        """枚举目录项；``offset``/``size`` 为 FUSE 分页游标，**目录 fh** 用于在 ``_dir_handles`` 中保存续读位置。

        **inode vs fh**：校验 ``ino`` 为目录；``fh`` 必须与 ``opendir`` 时一致且绑定同一 ``ino``，否则
        游标逻辑可能无法续传（``_dir_cursor`` 返回 ``None``）。

        **errno**：``ENOTDIR``、``ENOENT``、权限错误等。
        """
        return self._run_fuse_op(self._readdir_inode, ino, offset, size, fh)

    def _dir_cursor(self, ino: int, offset: int, fh):
        """解析续读游标：``fh`` 状态与 ``ino`` 不匹配则返回 ``None``。"""
        handles = getattr(self, "_dir_handles", {})
        state = handles.get(fh)
        if state is None or state["ino"] != ino:
            return None
        cursors = state["cursors"]
        if offset <= 0:
            cursors.clear()
            return None
        return cursors.get(offset)

    def _remember_dir_cursor(self, ino: int, fh, offset: int, name: str) -> None:
        """在目录 ``fh`` 下记录 ``offset``→子项名，供后续 ``children_after`` 分页。"""
        handles = getattr(self, "_dir_handles", {})
        state = handles.get(fh)
        if state is not None and state["ino"] == ino:
            state["cursors"][offset] = name

    def _readdir_inode(self, ino: int, offset: int, size: int, fh):
        """``readdir`` 的实现体：含 ``.``/``..``、分页子项、延迟更新 atime。"""
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            if node["kind"] != "dir":
                raise FuseOSError(errno.ENOTDIR)
            self._require_access(node, os.R_OK | os.X_OK)
            entries: list[LowLevelEntry] = []
            if offset < 1:
                entries.append(self._entry_from_node(b".", node, 1))
            if offset < 2:
                parent = self._node_by_ino(node["parent_id"] or node["id"])
                entries.append(self._entry_from_node(b"..", parent, 2))
            limit = max(64, min(4096, size // 48 if size else 256))
            after_name = self._dir_cursor(ino, offset, fh)
            if offset <= 2 or after_name is not None:
                children = self.metadata.children_after(ino, after_name, limit)
                first_next_id = max(2, offset) + 1
            else:
                start = max(0, offset - 2)
                children = self.metadata.children_page(ino, start, limit)
                first_next_id = start + 3
            for index, child in enumerate(children, start=first_next_id):
                encoded = child["name"].encode("utf-8", "surrogateescape")
                self._remember_dir_cursor(ino, fh, index, child["name"])
                entries.append(self._entry_from_node(encoded, child, index))
        should_flush = self.metadata.defer_node_atime(ino, time_ns())
        if should_flush:
            self.metadata.commit()
        return tuple(entries)

    def releasedir(self, ino: int, fh) -> int:
        """关闭 ``opendir`` 分配的目录 ``fh``；忽略未知 ``fh`` 时静默成功。"""
        with self._lock:
            getattr(self, "_dir_handles", {}).pop(fh, None)
        return 0

    def fsyncdir(self, ino: int, datasync: int, fh) -> None:
        """与 ``flush`` 相同，委托 ``_flush``；目录 ``ino``/``fh`` 不参与区分。"""
        self._run_fuse_op(self._flush)

    def unlink(self, parent: int, name: bytes) -> None:
        """删除普通文件或符号链接目录项（非目录）。

        **errno**：目标为目录 ``EISDIR``；``unlink`` 目录请用 ``rmdir``；不存在 ``ENOENT``；非空目录
        不在此路径。父目录 ``W_OK|X_OK``。
        """
        self._run_fuse_op(self._unlink_inode, parent, name)

    def rmdir(self, parent: int, name: bytes) -> None:
        """删除空子目录。

        **errno**：目标非目录 ``ENOTDIR``；非空 ``ENOTEMPTY``；``ENOENT`` 等。
        """
        self._run_fuse_op(self._rmdir_inode, parent, name)

    def _unlink_inode(self, parent: int, name: bytes) -> None:
        """``unlink``：``want_dir=False``。"""
        self._remove_child_inode(parent, name, want_dir=False)

    def _rmdir_inode(self, parent: int, name: bytes) -> None:
        """``rmdir``：``want_dir=True``。"""
        self._remove_child_inode(parent, name, want_dir=True)

    def _remove_child_inode(self, parent: int, name: bytes, *, want_dir: bool) -> None:
        """``unlink``/``rmdir`` 共用：按 ``want_dir`` 校验类型与子项是否为空。"""
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.transaction():
            parent_node = self._node_by_ino(parent)
            self._require_access(parent_node, os.W_OK | os.X_OK)
            node = self.metadata.child(parent, decoded)
            if node is None:
                raise FuseOSError(errno.ENOENT)
            if want_dir:
                if node["kind"] != "dir":
                    raise FuseOSError(errno.ENOTDIR)
                if self.metadata.has_children(node["id"]):
                    raise FuseOSError(errno.ENOTEMPTY)
            elif node["kind"] == "dir":
                raise FuseOSError(errno.EISDIR)
            self._remove_entry_node(node)

    def rename(
        self, parent: int, name: bytes, newparent: int, newname: bytes, flags: int
    ):
        """重命名/移动目录项；支持 ``RENAME_NOREPLACE``（仅该 flag，其余位 ``EINVAL``）。

        **errno**：源不存在 ``ENOENT``；目标存在且 ``NOREPLACE`` → ``EEXIST``；目录套娃 ``EINVAL``；
        类型冲突 ``EISDIR``/``ENOTDIR``；非空目录被替换 ``ENOTEMPTY``；跨父需双方写权限等。
        """
        self._run_fuse_op(self._rename_inode, parent, name, newparent, newname, flags)

    def _rename_inode(
        self, parent: int, name: bytes, newparent: int, newname: bytes, flags: int
    ) -> None:
        """``rename`` 的实现体。"""
        if flags & ~RENAME_NOREPLACE:
            raise FuseOSError(errno.EINVAL)
        self._ensure_trash_directory_for_caller()
        source_name = self._decode_name(name)
        target_name = self._decode_name(newname)
        with self.metadata.transaction():
            source = self.metadata.child(parent, source_name)
            if source is None:
                raise FuseOSError(errno.ENOENT)
            old_parent = self._node_by_ino(parent)
            target_parent = self._node_by_ino(newparent)
            self._require_access(old_parent, os.W_OK | os.X_OK)
            self._require_access(target_parent, os.W_OK | os.X_OK)
            if source["kind"] == "dir" and self.metadata.is_descendant(
                newparent, source["id"]
            ):
                raise FuseOSError(errno.EINVAL)
            target = self.metadata.child(newparent, target_name)
            if target is not None:
                if flags & RENAME_NOREPLACE:
                    raise FuseOSError(errno.EEXIST)
                if target["id"] == source["id"]:
                    return
                if target["kind"] == "dir" and source["kind"] != "dir":
                    raise FuseOSError(errno.EISDIR)
                if target["kind"] != "dir" and source["kind"] == "dir":
                    raise FuseOSError(errno.ENOTDIR)
                if target["kind"] == "dir" and self.metadata.has_children(target["id"]):
                    raise FuseOSError(errno.ENOTEMPTY)
                self._remove_entry_node(target)
            self.metadata.move_entry(
                parent, source_name, newparent, target_name, source["id"], time_ns()
            )
            self._remember_inode_name(source["id"], target_name)

    def statfs(self, ino: int):
        """返回文件系统统计；``ino`` 常为挂载点，具体语义由 ``_statfs`` 决定。"""
        return self._run_fuse_op(self._statfs)

    def access(self, ino: int, amode: int):
        """POSIX ``access``：按 ``amode`` 检查调用方是否可访问该 inode；成功返回 ``0``。

        **errno**：``ENOENT``；不满足 ``amode`` 时多为 ``EACCES``（由 ``_require_amode_access``）。
        """
        return self._run_fuse_op(self._access_inode, ino, amode)

    def _access_inode(self, ino: int, amode: int) -> int:
        """``access`` 的实现体。"""
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            self._require_amode_access(node, amode)
        return 0

    def readlink(self, ino: int) -> str:
        """读取符号链接目标字符串（非 ``bytes``）；无 ``fh``。

        **errno**：非 symlink ``EINVAL``（本实现选择）；``ENOENT``。
        """
        return self._run_fuse_op(self._readlink_inode, ino)

    def _readlink_inode(self, ino: int) -> str:
        """``readlink`` 的实现体；延迟更新 symlink atime。"""
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            if node["kind"] != "symlink":
                raise FuseOSError(errno.EINVAL)
            target = node["symlink_target"]
        should_flush = self.metadata.defer_node_atime(ino, time_ns())
        if should_flush:
            self.metadata.commit()
        return target

    def symlink(self, link: bytes, parent: int, name: bytes) -> LowLevelEntry:
        """在 ``parent`` 下创建指向 ``link`` 的符号链接。

        **errno**：同名 ``EEXIST``；父目录权限等。
        """
        return self._run_fuse_op(self._symlink_inode, link, parent, name)

    def _symlink_inode(self, link: bytes, parent: int, name: bytes) -> LowLevelEntry:
        """``symlink`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.transaction():
            parent_node = self._node_by_ino(parent)
            self._require_access(parent_node, os.W_OK | os.X_OK)
            if self.metadata.child(parent, decoded) is not None:
                raise FuseOSError(errno.EEXIST)
            inode_id = self.metadata.insert_node(
                parent,
                decoded,
                "symlink",
                S_IFLNK | 0o777,
                *self._creation_owner(),
                time_ns(),
                symlink_target=self._decode_name(link),
            )
            return self._entry_from_node(name, self._node_by_ino(inode_id), inode_id)

    def link(self, ino: int, newparent: int, newname: bytes) -> LowLevelEntry:
        """为已有 inode ``ino`` 在 ``newparent``/``newname`` 处创建硬链接。

        **errno**：源为目录 ``EPERM``（禁止目录硬链）；目标 ``EEXIST``；``ENOENT``；权限错误。
        """
        return self._run_fuse_op(self._link_inode, ino, newparent, newname)

    def _link_inode(self, ino: int, newparent: int, newname: bytes) -> LowLevelEntry:
        """``link`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(newname)
        with self.metadata.transaction():
            source = self._node_by_ino(ino)
            if source["kind"] == "dir":
                raise FuseOSError(errno.EPERM)
            parent_node = self._node_by_ino(newparent)
            self._require_access(parent_node, os.W_OK | os.X_OK)
            if self.metadata.child(newparent, decoded) is not None:
                raise FuseOSError(errno.EEXIST)
            self.metadata.link_node(newparent, decoded, ino, time_ns())
            node = self.metadata.child(newparent, decoded)
            return self._entry_from_node(newname, node, ino)

    def listxattr(self, ino: int):
        """列出 inode 上所有扩展属性名；需 ``R_OK``。

        **errno**：``ENOENT``；``EACCES`` 等。
        """
        return self._run_fuse_op(self._listxattr_inode, ino)

    def _listxattr_inode(self, ino: int):
        """``listxattr`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            self._require_access(node, os.R_OK)
            return self.metadata.xattr_names(ino)

    def getxattr(self, ino: int, name: bytes, position: int):
        """读取扩展属性值；``position`` 未使用（非资源分支语义）。

        **errno**：不存在 ``ENOATTR``（项目常量，通常为 ``ENODATA`` 类值）；``ENOENT``；``EACCES``。
        """
        return self._run_fuse_op(self._getxattr_inode, ino, name)

    def _getxattr_inode(self, ino: int, name: bytes):
        """``getxattr`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            self._require_access(node, os.R_OK)
            row = self.metadata.xattr(ino, decoded)
            if row is None:
                raise FuseOSError(ENOATTR)
            return bytes(row["value"])

    def setxattr(
        self, ino: int, name: bytes, value: bytes, options: int, position: int
    ) -> None:
        """设置扩展属性；``XATTR_CREATE`` / ``XATTR_REPLACE`` 与 ``options`` 组合。

        **errno**：``CREATE`` 且已存在 ``EEXIST``；``REPLACE`` 且不存在 ``ENOATTR``；需 ``W_OK``；
        ``ENOENT`` 等。
        """
        self._run_fuse_op(self._setxattr_inode, ino, name, value, options)

    def _setxattr_inode(
        self, ino: int, name: bytes, value: bytes, options: int
    ) -> None:
        """``setxattr`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.transaction():
            node = self._node_by_ino(ino)
            self._require_access(node, os.W_OK)
            exists = self.metadata.xattr(ino, decoded) is not None
            if options & XATTR_CREATE and exists:
                raise FuseOSError(errno.EEXIST)
            if options & XATTR_REPLACE and not exists:
                raise FuseOSError(ENOATTR)
            self.metadata.set_xattr(ino, decoded, value, time_ns())

    def removexattr(self, ino: int, name: bytes) -> None:
        """删除扩展属性；无则 ``ENOATTR``。"""
        self._run_fuse_op(self._removexattr_inode, ino, name)

    def _removexattr_inode(self, ino: int, name: bytes) -> None:
        """``removexattr`` 的实现体。"""
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.transaction():
            node = self._node_by_ino(ino)
            self._require_access(node, os.W_OK)
            if not self.metadata.remove_xattr(ino, decoded, time_ns()):
                raise FuseOSError(ENOATTR)

    def getlk(self, ino: int, fh, lock: dict[str, int]):
        """查询建议性区间锁（``fcntl.F_GETLK``）；目标文件由 ``_file_node_from_ino_or_fh`` 决定。

        **errno**：非文件 ``EISDIR``；锁表相关 ``EAGAIN``/``EACCES`` 等见 ``locks.apply``。
        """
        return self._run_fuse_op(self._lock_inode, ino, fh, fcntl.F_GETLK, lock)

    def setlk(self, ino: int, fh, cmd: int, lock: dict[str, int]):
        """设置/清除建议性区间锁（``F_SETLK`` / ``F_SETLKW`` 等）；``fh`` 用于绑定锁属主信息。"""
        return self._run_fuse_op(self._lock_inode, ino, fh, cmd, lock)

    def _lock_inode(self, ino: int, fh, cmd: int, lock: dict[str, int]):
        """``getlk``/``setlk`` 共用：只读事务解析节点，进程内 ``locks.apply`` 执行锁语义。"""
        with self.metadata.read_transaction():
            node = self._file_node_from_ino_or_fh(ino, fh)
            if node["kind"] != "file":
                raise FuseOSError(errno.EISDIR)
            owner = self.handles.lock_owner(fh) or self._lock_owner()
            uid, _gid, pid = self._caller_ids()
            lock.setdefault("l_pid", pid)
        with self._lock:
            return self.locks.apply(
                node["id"], owner, int(lock.get("l_pid", pid)), cmd, lock
            )

    def flock(self, ino: int, fh, op: int):
        """BSD ``flock`` 语义适配：映射为整文件 ``setlk``（``l_start``/``l_len`` 为 0）。

        **inode vs fh**：与 ``setlk`` 相同。``LOCK_NB`` 决定 ``F_SETLK`` vs 阻塞 ``F_SETLKW``（若平台无
        ``F_SETLKW`` 则回退 ``F_SETLK``）。
        """
        if op & fcntl.LOCK_UN:
            lock_type = fcntl.F_UNLCK
        elif op & fcntl.LOCK_EX:
            lock_type = fcntl.F_WRLCK
        else:
            lock_type = fcntl.F_RDLCK
        cmd = (
            fcntl.F_SETLK
            if op & fcntl.LOCK_NB
            else getattr(fcntl, "F_SETLKW", fcntl.F_SETLK)
        )
        lock = {
            "l_type": lock_type,
            "l_whence": 0,
            "l_start": 0,
            "l_len": 0,
            "l_pid": 0,
        }
        return self.setlk(ino, fh, cmd, lock)
