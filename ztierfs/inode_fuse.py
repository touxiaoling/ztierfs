import errno
import fcntl
import os

from collections.abc import Mapping
from stat import S_IFDIR, S_IFLNK, S_IFREG, S_ISREG
from time import perf_counter_ns, time_ns

from loguru import logger
from macfusepy import FuseOSError, InodeOperations, LowLevelEntry
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
    """面向 macfusepy inode-first 同步 fast path 的 FUSE 入口。"""

    def _decode_name(self, name: bytes) -> str:
        return name.decode("utf-8", "surrogateescape")

    def _node_by_ino(self, ino: int):
        row = self.metadata.node_by_id(ino)
        if row is None:
            raise FuseOSError(errno.ENOENT)
        return row

    def _file_node_from_ino_or_fh(self, ino: int, fh):
        file_id = self.handles.file_id(fh)
        if file_id is not None:
            return self._node_by_ino(file_id)
        return self._node_by_ino(ino)

    def _entry_from_node(self, name: bytes, node, next_id: int) -> LowLevelEntry:
        self._remember_inode_name(node["id"], self._decode_name(name))
        return LowLevelEntry(name, node["id"], self._attrs_from_node(node), next_id)

    def _remember_inode_name(self, inode_id: int, name: str) -> None:
        names = getattr(self, "_inode_names", None)
        if names is None:
            names = {}
            self._inode_names = names
        names[inode_id] = name

    def _name_for_inode(self, node) -> str:
        names = getattr(self, "_inode_names", {})
        name = names.get(node["id"], node["name"] or "")
        return name if name.startswith("/") else f"/{name}"

    def init(self, conn=None, cfg=None) -> None:
        if conn is not None:
            logger.info(
                "FUSE 连接能力：max_read={}，max_write={}，max_readahead={}，max_background={}",
                conn.max_read,
                conn.max_write,
                conn.max_readahead,
                conn.max_background,
            )

    def destroy(self) -> None:
        self.close()

    def close(self) -> None:
        executor = getattr(self, "_readahead_executor", None)
        if executor is not None:
            executor.shutdown(wait=True)
            self._readahead_executor = None
        self.block_store.close()
        self.metadata.close()
        profiler = getattr(self, "_operation_profiler", None)
        if profiler is not None:
            profiler.log_final()

    def _log_value(self, value):
        if isinstance(value, bytes):
            return f"<bytes {len(value)}>"
        if isinstance(value, tuple):
            return tuple(self._log_value(item) for item in value)
        if isinstance(value, list):
            return [self._log_value(item) for item in value]
        return value

    def _run_fuse_op(self, func, /, *args):
        op = func.__name__.removesuffix("_inode").removeprefix("_")
        logged_args = lambda: tuple(self._log_value(arg) for arg in args)
        logger.opt(lazy=True).debug("FUSE inode {}{}", lambda: op, logged_args)
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
            logger.opt(lazy=True).debug(
                "FUSE inode {}{} -> OSError({})",
                lambda: op,
                logged_args,
                lambda: exc.errno,
            )
            raise
        logger.opt(lazy=True).debug(
            "FUSE inode {}{} -> {}",
            lambda: op,
            logged_args,
            lambda: self._log_value(result),
        )
        return result

    def lookup(self, parent: int, name: bytes) -> LowLevelEntry:
        return self._run_fuse_op(self._lookup_inode, parent, name)

    def _lookup_inode(self, parent: int, name: bytes) -> LowLevelEntry:
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            parent_node = self._node_by_ino(parent)
            if parent_node["kind"] != "dir":
                raise FuseOSError(errno.ENOTDIR)
            node = self.metadata.child(parent, self._decode_name(name))
            if node is None:
                raise FuseOSError(errno.ENOENT)
            return self._entry_from_node(name, node, node["id"])

    def getattr(self, ino: int, fh=None) -> dict[str, int]:
        return self._run_fuse_op(self._getattr_inode, ino, fh)

    def _getattr_inode(self, ino: int, fh=None) -> dict[str, int]:
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            return self._attrs_from_node(self._file_node_from_ino_or_fh(ino, fh))

    def setattr(self, ino: int, attrs: Mapping[str, int], to_set: int, fh=None):
        return self._run_fuse_op(self._setattr_inode, ino, attrs, to_set, fh)

    def _setattr_inode(self, ino: int, attrs: Mapping[str, int], to_set: int, fh=None):
        self._ensure_trash_directory_for_caller()
        with self._content_lock(self.handles.file_id(fh) or ino), self.metadata.transaction():
            node = self._file_node_from_ino_or_fh(ino, fh)
            now = time_ns()
            if to_set & FUSE_SET_ATTR_MODE:
                self._require_owner(node)
                kind = {"dir": S_IFDIR, "file": S_IFREG, "symlink": S_IFLNK}[node["kind"]]
                self.metadata.set_node_mode(node["id"], kind | (attrs["st_mode"] & 0o7777), now)
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
                atime = attrs["st_atime"] if to_set & FUSE_SET_ATTR_ATIME else node["atime_ns"]
                mtime = attrs["st_mtime"] if to_set & FUSE_SET_ATTR_MTIME else node["mtime_ns"]
                self.metadata.set_node_times(node["id"], atime, mtime, now)
            return self._attrs_from_node(self._node_by_ino(node["id"]))

    def open(self, ino: int, flags: int, fi=None) -> int:
        return self._run_fuse_op(self._open_inode, ino, flags)

    def _open_inode(self, ino: int, flags: int) -> int:
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
        return self._run_fuse_op(self._read_inode, ino, size, offset, fh)

    def _read_inode(self, ino: int, size: int, offset: int, fh) -> bytes:
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
            should_flush = self.block_store.record_block_accesses(accesses, now) or should_flush
            if should_flush:
                with self.metadata.transaction():
                    for access in accesses:
                        self.block_store.record_block_presence(access, now)
        if self.block_store.take_demotion_request():
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()
        return data

    def write(self, ino: int, data: bytes, offset: int, fh) -> int:
        return self._run_fuse_op(self._write_inode, ino, data, offset, fh)

    def _write_inode(self, ino: int, data: bytes, offset: int, fh) -> int:
        self._ensure_trash_directory_for_caller()
        inode_id = self.handles.file_id(fh) or ino
        with self._content_lock(inode_id):
            with self.metadata.read_transaction():
                node = self._file_node_from_ino_or_fh(ino, fh)
                self._require_access(node, os.W_OK)
                path = self._name_for_inode(node)
                prepared = self.file_content.prepare_write_file(
                    node, path, data, offset
                )
            with self.metadata.transaction():
                written = self.file_content.commit_prepared_write(prepared)
        if self.block_store.take_demotion_request():
            with self.metadata.transaction():
                self.block_store.demote_cold_blocks()
        return written

    def flush(self, ino: int, fh) -> None:
        self._run_fuse_op(self._flush)

    def fsync(self, ino: int, datasync: int, fh) -> None:
        self._run_fuse_op(self._flush)

    def release(self, ino: int, fh) -> int:
        return self._run_fuse_op(self._release, fh)

    def create(self, parent: int, name: bytes, mode: int, flags: int, fi):
        return self._run_fuse_op(self._create_inode, parent, name, mode, flags)

    def _create_inode(self, parent: int, name: bytes, mode: int, flags: int):
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
                self.metadata.reset_file_node(existing["id"], S_IFREG | (mode & 0o7777), now)
                node = self._node_by_ino(existing["id"])
            else:
                inode_id = self.metadata.insert_node(
                    parent, decoded, "file", S_IFREG | (mode & 0o7777), *self._creation_owner(), now
                )
                node = self._node_by_ino(inode_id)
            fh = self.handles.new(node["id"], self._lock_owner())
            return self._entry_from_node(name, node, node["id"]), fh

    def mkdir(self, parent: int, name: bytes, mode: int) -> LowLevelEntry:
        return self._run_fuse_op(self._mkdir_inode, parent, name, mode)

    def _mkdir_inode(self, parent: int, name: bytes, mode: int) -> LowLevelEntry:
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.transaction():
            parent_node = self._node_by_ino(parent)
            self._require_access(parent_node, os.W_OK | os.X_OK)
            if self.metadata.child(parent, decoded) is not None:
                raise FuseOSError(errno.EEXIST)
            inode_id = self.metadata.insert_node(
                parent, decoded, "dir", S_IFDIR | (mode & 0o7777), *self._creation_owner(), time_ns()
            )
            return self._entry_from_node(name, self._node_by_ino(inode_id), inode_id)

    def mknod(self, parent: int, name: bytes, mode: int, dev: int) -> LowLevelEntry:
        if not S_ISREG(mode):
            raise FuseOSError(errno.ENOTSUP)
        return self._run_fuse_op(self._mknod_inode, parent, name, mode)

    def _mknod_inode(self, parent: int, name: bytes, mode: int) -> LowLevelEntry:
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
        return self._run_fuse_op(self._opendir_inode, ino, flags)

    def _opendir_inode(self, ino: int, flags: int = 0) -> int:
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
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            if node["kind"] != "dir":
                raise FuseOSError(errno.ENOTDIR)
            self._require_access(node, os.R_OK | os.X_OK)

    def readdir(self, ino: int, offset: int, size: int, fh, flags: int = 0):
        return self._run_fuse_op(self._readdir_inode, ino, offset, size, fh)

    def _dir_cursor(self, ino: int, offset: int, fh):
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
        handles = getattr(self, "_dir_handles", {})
        state = handles.get(fh)
        if state is not None and state["ino"] == ino:
            state["cursors"][offset] = name

    def _readdir_inode(self, ino: int, offset: int, size: int, fh):
        self._ensure_trash_directory_for_caller()
        with self.metadata.transaction():
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
                entries.append(
                    self._entry_from_node(encoded, child, index)
                )
            self.metadata.touch_node_atime(ino, time_ns())
            return tuple(entries)

    def releasedir(self, ino: int, fh) -> int:
        with self._lock:
            getattr(self, "_dir_handles", {}).pop(fh, None)
        return 0

    def fsyncdir(self, ino: int, datasync: int, fh) -> None:
        self._run_fuse_op(self._flush)

    def unlink(self, parent: int, name: bytes) -> None:
        self._run_fuse_op(self._unlink_inode, parent, name)

    def rmdir(self, parent: int, name: bytes) -> None:
        self._run_fuse_op(self._rmdir_inode, parent, name)

    def _unlink_inode(self, parent: int, name: bytes) -> None:
        self._remove_child_inode(parent, name, want_dir=False)

    def _rmdir_inode(self, parent: int, name: bytes) -> None:
        self._remove_child_inode(parent, name, want_dir=True)

    def _remove_child_inode(self, parent: int, name: bytes, *, want_dir: bool) -> None:
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

    def rename(self, parent: int, name: bytes, newparent: int, newname: bytes, flags: int):
        self._run_fuse_op(
            self._rename_inode, parent, name, newparent, newname, flags
        )

    def _rename_inode(self, parent: int, name: bytes, newparent: int, newname: bytes, flags: int) -> None:
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
            if source["kind"] == "dir" and self.metadata.is_descendant(newparent, source["id"]):
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
        return self._run_fuse_op(self._statfs)

    def access(self, ino: int, amode: int):
        return self._run_fuse_op(self._access_inode, ino, amode)

    def _access_inode(self, ino: int, amode: int) -> int:
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            self._require_amode_access(node, amode)
        return 0

    def readlink(self, ino: int) -> str:
        return self._run_fuse_op(self._readlink_inode, ino)

    def _readlink_inode(self, ino: int) -> str:
        with self.metadata.transaction():
            node = self._node_by_ino(ino)
            if node["kind"] != "symlink":
                raise FuseOSError(errno.EINVAL)
            self.metadata.touch_node_atime(ino, time_ns())
            return node["symlink_target"]

    def symlink(self, link: bytes, parent: int, name: bytes) -> LowLevelEntry:
        return self._run_fuse_op(self._symlink_inode, link, parent, name)

    def _symlink_inode(self, link: bytes, parent: int, name: bytes) -> LowLevelEntry:
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
        return self._run_fuse_op(self._link_inode, ino, newparent, newname)

    def _link_inode(self, ino: int, newparent: int, newname: bytes) -> LowLevelEntry:
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
        return self._run_fuse_op(self._listxattr_inode, ino)

    def _listxattr_inode(self, ino: int):
        self._ensure_trash_directory_for_caller()
        with self.metadata.read_transaction():
            node = self._node_by_ino(ino)
            self._require_access(node, os.R_OK)
            return self.metadata.xattr_names(ino)

    def getxattr(self, ino: int, name: bytes, position: int):
        return self._run_fuse_op(self._getxattr_inode, ino, name)

    def _getxattr_inode(self, ino: int, name: bytes):
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
        self._run_fuse_op(self._setxattr_inode, ino, name, value, options)

    def _setxattr_inode(self, ino: int, name: bytes, value: bytes, options: int) -> None:
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
        self._run_fuse_op(self._removexattr_inode, ino, name)

    def _removexattr_inode(self, ino: int, name: bytes) -> None:
        self._ensure_trash_directory_for_caller()
        decoded = self._decode_name(name)
        with self.metadata.transaction():
            node = self._node_by_ino(ino)
            self._require_access(node, os.W_OK)
            if not self.metadata.remove_xattr(ino, decoded, time_ns()):
                raise FuseOSError(ENOATTR)

    def getlk(self, ino: int, fh, lock: dict[str, int]):
        return self._run_fuse_op(self._lock_inode, ino, fh, fcntl.F_GETLK, lock)

    def setlk(self, ino: int, fh, cmd: int, lock: dict[str, int]):
        return self._run_fuse_op(self._lock_inode, ino, fh, cmd, lock)

    def _lock_inode(self, ino: int, fh, cmd: int, lock: dict[str, int]):
        with self.metadata.read_transaction():
            node = self._file_node_from_ino_or_fh(ino, fh)
            if node["kind"] != "file":
                raise FuseOSError(errno.EISDIR)
            owner = self.handles.lock_owner(fh) or self._lock_owner()
            uid, _gid, pid = self._caller_ids()
            lock.setdefault("l_pid", pid)
        with self._lock:
            return self.locks.apply(node["id"], owner, int(lock.get("l_pid", pid)), cmd, lock)

    def flock(self, ino: int, fh, op: int):
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
        lock = {"l_type": lock_type, "l_whence": 0, "l_start": 0, "l_len": 0, "l_pid": 0}
        return self.setlk(ino, fh, cmd, lock)
