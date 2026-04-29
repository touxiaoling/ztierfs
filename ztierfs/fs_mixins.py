"""各 FUSE mixin 共享的字段类型与抽象方法（单独继承不可用）。

权限检查、路径规范化、`LowLevelAttr` 映射等由 `AccessControlMixin`、`NamespaceOpsMixin` 等实现；
读前瞻相关字段仅在单进程、单挂载实例内有效，不与其它进程协调。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from macfusepy import LowLevelAttr

from .block_store import BlockStore
from .file_content import FileContentService
from .handles import HandleTable
from .locks import AdvisoryLockTable
from .metadata import MetadataStore


class FileSystemMixinBase:
    """组成 ZTierFS 的 mixin 所共享的字段与抽象方法约定（非独立基类）。"""

    metadata: MetadataStore
    handles: HandleTable
    locks: AdvisoryLockTable
    block_store: BlockStore
    file_content: FileContentService
    tier1: Path
    tier2: Path
    chunk_size: int
    _lock: threading.RLock
    _content_locks_guard: threading.Lock
    _content_locks: dict[int, threading.RLock]
    _caller_provider: Callable[[], tuple[int, int, int]]
    readahead_blocks: int
    readahead_workers: int
    _read_positions: dict[int, int]
    _readahead_inflight: set[tuple[int, int]]
    _readahead_executor: ThreadPoolExecutor | None
    _readahead_lock: threading.Lock

    def _require_open_access(self, node: Any, flags: int) -> None:
        """子类实现：按 ``open(2)`` 的 ``O_RDONLY`` / ``O_WRONLY`` / ``O_RDWR``（及 ``O_TRUNC``）
        映射为读/写权限掩码，再校验调用方对该 inode 是否具备相应 POSIX mode 权限；不满足时抛
        ``FuseOSError(EACCES)``。"""
        raise NotImplementedError

    def _require_access(self, node: Any, mask: int) -> None:
        """子类实现：在 FUSE 请求上下文中，按调用方有效 uid/gid 对照 inode 的 mode 做
        ``R_OK`` / ``W_OK`` / ``X_OK``（及 ``F_OK`` 存在性）检查；``node`` 缺失时抛 ``ENOENT``，
        权限不足时抛 ``EACCES``。"""
        raise NotImplementedError

    def _require_amode_access(self, node: Any, amode: int) -> None:
        """子类实现：将 ``access(2)`` 风格的 ``amode``（含 macOS 删除访问位等）归并为对 inode、
        必要时对父目录的 ``_require_access`` 调用链，供 ``_access`` 等路径复用。"""
        raise NotImplementedError

    def _require_owner(self, node: Any) -> None:
        """子类实现：要求调用方有效 uid 为该 inode 的拥有者（常见 ``root`` 豁免规则由实现约定）；
        否则抛 ``EPERM``。"""
        raise NotImplementedError

    def _caller_ids(self) -> tuple[int, int, int]:
        """子类实现：返回当前 FUSE 调用对应的 ``(uid, gid, pid)``；无请求上下文时回退为进程
        ``getuid`` / ``getgid`` / ``getpid``，供权限与锁主体等逻辑使用。"""
        raise NotImplementedError

    def _creation_owner(self) -> tuple[int, int]:
        """子类实现：返回新建 inode（文件、目录、符号链接等）时应写入元数据的 ``(uid, gid)``，
        通常取自 ``_caller_ids``。"""
        raise NotImplementedError

    def _lock_owner(self) -> int:
        """子类实现：返回本挂载进程内 POSIX 建议性文件锁所用的「锁主体」整型标识（用于区分用户
        与进程），与 ``AdvisoryLockTable``、句柄释放路径一致。"""
        raise NotImplementedError

    def _ensure_trash_directory_for_caller(self) -> None:
        """子类实现：按当前调用方 uid/gid 在元数据中确保回收站侧目录结构存在（可带 uid 级缓存），
        供 Finder 等通过 ``rename`` 丢入废纸篓前的路径依赖。"""
        raise NotImplementedError

    def _attrs_from_node(self, node: Any) -> Any:
        """子类实现：将 SQLite 中的 inode 行映射为 ``macfusepy.LowLevelAttr``（含目录 ``st_nlink``
        计数约定、``st_blocks`` 由实际占用换算等）。"""
        raise NotImplementedError

    def _schedule_readahead(
        self, plan: Any, offset: int, data_len: int, fh: Any
    ) -> None:
        """子类实现：在顺序读等条件下，为给定读计划异步提交后续若干文件 chunk 的预读任务（受
        ``readahead_blocks`` / ``readahead_workers`` 与去重集合约束），失败应静默忽略。"""
        raise NotImplementedError

    @contextmanager
    def _content_lock(self, inode_id: int) -> Iterator[None]:
        """子类实现：上下文管理器，在 ``with`` 体内持有该 inode 的**内容锁**（可重入），用于在单
        挂载进程内串行化同一普通文件上的读写、截断与块/chunk 更新路径。"""
        raise NotImplementedError
        yield

    def _node_from_handle_or_path(self, path: str, fh: Any) -> Any:
        """子类实现：若 ``fh`` 绑定打开中的文件 inode，则优先返回该 inode 行；否则按规范化路径
        解析；不存在时抛 ``ENOENT``（支持 rename/unlink 后仍按打开时 inode 操作的语义）。"""
        raise NotImplementedError

    def _remove_entry_node(self, node: Any) -> None:
        """子类实现：在元数据事务中删除一条目录项、更新 ``nlink``；若链接计数归零且无打开句柄，
        则删除 inode 及其文件块引用等 payload。"""
        raise NotImplementedError

    def _access(self, path: str, amode: int) -> int:
        """子类实现：对应 FUSE ``access``：在只读事务中解析路径并对目标 inode 调用
        ``_require_amode_access``；成功时返回 ``0``。"""
        raise NotImplementedError

    def _chmod(self, path: str, mode: int, fh: Any = None) -> None:
        """子类实现：按路径或句柄定位 inode，校验拥有者后更新 mode 位（保留类型位），并刷新
        ``ctime`` 等时间戳。"""
        raise NotImplementedError

    def _chown(self, path: str, uid: int, gid: int, fh: Any = None) -> None:
        """子类实现：按路径或句柄更新 inode 的 ``uid`` / ``gid``；``-1`` 表示保持原值。典型实现
        仅允许 root 调用方修改属主，否则抛 ``EPERM``。"""
        raise NotImplementedError

    def _create(self, path: str, mode: int, flags: int = 0) -> Any:
        """子类实现：在父目录下创建或截断为空的普通文件：分配 inode、目录项与初始 mode/属主，
        按需清空已有 chunk，并返回新 FUSE 文件句柄（整型 ``fh``）。"""
        raise NotImplementedError

    def _flush(self) -> None:
        """子类实现：将挂起的元数据写事务提交（例如 ``MetadataStore.commit``），使已完成的操作
        对连接可见。"""
        raise NotImplementedError

    def _getattr(self, path: str, fh: Any = None) -> LowLevelAttr:
        """子类实现：解析 ``path`` / ``fh`` 得到 inode，并返回 ``_attrs_from_node`` 的
        ``LowLevelAttr``。"""
        raise NotImplementedError

    def _getxattr(self, path: str, name: str) -> bytes:
        """子类实现：读取 inode 上指定名的扩展属性值；不存在时抛 ``ENOATTR``（或平台约定 errno）。"""
        raise NotImplementedError

    def _link(self, target: str, source: str) -> None:
        """子类实现：为已存在路径 ``source`` 对应的 inode 在路径 ``target`` 处新增硬链接目录项
        （含父目录权限、禁止目录硬链、``EEXIST`` 等语义）。"""
        raise NotImplementedError

    def _clonefile(self, source: str, target: str) -> None:
        """子类实现：在 macOS ``clonefile`` 语义下复制 inode：共享 chunk 与 xattr、递增块引用计数，
        不重写 payload。"""
        raise NotImplementedError

    def _listxattr(self, path: str) -> list[str]:
        """子类实现：在具备读权限前提下，返回该 inode 在元数据中存储的全部扩展属性名列表。"""
        raise NotImplementedError

    def _lock_file(
        self, path: str, fh: Any, cmd: int, lock: dict[str, int]
    ) -> dict[str, int] | None:
        """子类实现：对普通文件 inode 应用 ``fcntl`` 风格的建议性锁请求（``F_GETLK`` /
        ``F_SETLK`` 等），委托 ``AdvisoryLockTable``；目录应抛 ``EISDIR``。"""
        raise NotImplementedError

    def _mkdir(self, path: str, mode: int) -> Any:
        """子类实现：在已授权父目录下创建子目录 inode 与目录项，设置 mode 与属主，并返回
        ``_attrs_from_node`` 得到的新目录 ``LowLevelAttr``。"""
        raise NotImplementedError

    def _mknod(self, path: str, mode: int, dev: int) -> Any:
        """子类实现：处理 ``mknod``；仅当类型为普通文件时创建节点，否则可抛 ``ENOTSUP`` /
        ``EINVAL`` 等以拒绝设备节点等非常规类型。"""
        raise NotImplementedError

    def _open(self, path: str, flags: int) -> int:
        """子类实现：打开已有普通文件：校验类型与 ``_require_open_access``，按需 ``O_TRUNC`` 截断，
        注册句柄并返回 ``fh``。"""
        raise NotImplementedError

    def _read(self, path: str, size: int, offset: int, fh: Any) -> bytes:
        """子类实现：从句柄/路径对应的文件按 ``offset`` 读取至多 ``size`` 字节（稀疏洞读零），
        更新访问统计并可触发读前瞻与热层降级检查。"""
        raise NotImplementedError

    def _readdir(self, path: str) -> list[Any]:
        """子类实现：读取目录项列表，返回 ``(name, LowLevelAttr)`` 元组序列，须包含 ``.`` 与
        ``..``，并更新目录 ``atime``（在实现约定的事务内）。"""
        raise NotImplementedError

    def _readlink(self, path: str) -> str:
        """子类实现：返回符号链接 inode 存储的目标路径字符串；非链接类型抛 ``EINVAL``。"""
        raise NotImplementedError

    def _release(self, fh: Any) -> int:
        """子类实现：关闭 ``fh``：从句柄表移除、释放该 inode 上的建议锁主体；若 ``nlink==0`` 且无
        其它打开引用则清理孤儿 inode。"""
        raise NotImplementedError

    def _removexattr(self, path: str, name: str) -> None:
        """子类实现：删除指定扩展属性；无该名时抛 ``ENOATTR``。"""
        raise NotImplementedError

    def _rename(self, old: str, new: str, flags: int) -> None:
        """子类实现：在命名空间内重命名或移动目录项，处理覆盖、目录非空、``RENAME_NOREPLACE`` 等
        flag 与跨父目录权限。"""
        raise NotImplementedError

    def _rmdir(self, path: str) -> None:
        """子类实现：删除空目录 inode（仅剩 ``.``/``..`` 语义上的子项计数为 0）；非空抛
        ``ENOTEMPTY``。"""
        raise NotImplementedError

    def _setxattr(self, path: str, name: str, value: bytes, options: int) -> None:
        """子类实现：设置或替换扩展属性，尊重 ``XATTR_CREATE`` / ``XATTR_REPLACE`` 与值大小限制。"""
        raise NotImplementedError

    def _statfs(self) -> dict[str, int]:
        """子类实现：汇总热层、冷层挂载点的 ``statvfs`` 信息，返回 FUSE 期望的块数、可用块、
        ``f_bsize`` 等字段字典。"""
        raise NotImplementedError

    def _symlink(self, target: str, source: str) -> Any:
        """子类实现：在路径 ``target`` 处创建符号链接 inode 与目录项，其链接文本（目标路径）为
        ``source``。"""
        raise NotImplementedError

    def _truncate(self, path: str, length: int, fh: Any = None) -> None:
        """子类实现：在内容锁与写事务中将文件截断为 ``length``，更新 chunk 映射、大小与时间戳，
        并可触发块回收与降级检查。"""
        raise NotImplementedError

    def _unlink(self, path: str) -> None:
        """子类实现：删除非目录 inode（普通文件、符号链接等）的目录项并调用 ``_remove_entry_node``；
        若为目录则抛 ``EISDIR``（应使用 ``rmdir``）。"""
        raise NotImplementedError

    def _utimens(self, path: str, times: Any, fh: Any = None) -> None:
        """子类实现：按 ``times``（或 ``UTIME_NOW`` / ``UTIME_OMIT`` 语义）更新 ``atime`` /
        ``mtime``，必要时触碰 ``ctime``。"""
        raise NotImplementedError

    def _write(self, path: str, data: bytes, offset: int, fh: Any) -> int:
        """子类实现：在内容锁保护下准备并提交写事务，更新分块与引用计数，返回实际写入字节数；
        可触发热层降级检查。"""
        raise NotImplementedError
