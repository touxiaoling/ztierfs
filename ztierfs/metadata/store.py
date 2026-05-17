"""SQLite 元数据门面：组合 schema、命名空间、分块、块表与访问统计等 mixin。

**读/写事务与嵌套**

- 同一线程通过 `threading.local` 持有当前 `sqlite3.Connection`；**嵌套**进入 `read_transaction` / `write_transaction` 时复用同一条连接与同一层 `BEGIN`，不再重复加锁、也不再向连接池再借一条连接。
- **禁止**在已有**读**事务的嵌套层上开启**写**事务：若内层需要写、外层处于只读，会抛 `RuntimeError`（`cannot open a write transaction inside a read transaction`）。在写事务内可再进读或写（仍为同一写事务的语义，仅嵌套深度增加）。

**读锁/写锁与连接池**

- 最外层非嵌套的事务会先获取 `ReadWriteLock`：只读为读锁、可写为写锁；**多个读事务可并发**（与写互斥）；写事务**独占**与所有读/写互斥。随后从 `ConnectionPool` 取一条连接，执行 `BEGIN` 或 `BEGIN IMMEDIATE`。
- `setup()` 在初始化 schema 时单独持写锁并从池中取连接，不经过 `read_transaction`/`write_transaction` 的嵌套逻辑。

**COMMIT 与 `flush_deferred_accesses`**

- 可写（非只读）事务在 `yield` 段正常返回后、`COMMIT` 之前，会调用 `flush_deferred_accesses()`，把延迟的 inode atime/块读次数等**写入同一待提交事务**，与元数据一起 `COMMIT`。
- 发生异常时走 `ROLLBACK`，**不会**在该次 `flush_deferred_accesses` 上提交；是否部分写入与 SQLite 及 mixin 实现有关。

**`close()` / `commit()` 行为**

- 若 `has_deferred_accesses()` 为真，`close()` 会先用一次写事务（空 `transaction()`）**刷掉延迟访问并提交**，再关闭连接池，减少进程退出时统计丢失。
- 对外 `commit()` 同理：有延迟访问时开一次写事务以刷盘；没有则立即返回。二者都不替代应用层在**已有写事务**内对业务与统计的一致提交，而是为「无活动事务时仍有待刷延迟」的收口路径。
"""

import sqlite3
import threading

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from macfusepy import FuseOSError

from .access_stats import (
    BlockAccessStats,
    DEFAULT_DEFERRED_ACCESS_FLUSH_BLOCKS,
    DEFAULT_DEFERRED_ACCESS_FLUSH_NS,
    AccessStatsMixin,
)
from .blocks import BlockMetadataMixin
from .chunks import ChunkMetadataMixin
from .connection import ConnectionPool, ReadWriteLock, SQLitePragmas
from .gc import GarbageCollectionMixin
from .namespace import NamespaceMixin
from .schema import SCHEMA_VERSION, SchemaMixin
from ztierfs.payload_store import NullPayloadStore, PayloadStore


class MetadataStore(
    SchemaMixin,
    NamespaceMixin,
    ChunkMetadataMixin,
    BlockMetadataMixin,
    GarbageCollectionMixin,
    AccessStatsMixin,
):
    """组合各 mixin 后的对外 API；通过 `_db` 的访问须在已打开的 `read_transaction` 或 `write_transaction` 内（含嵌套时复用同一连接）。

    同线程嵌套时勿在读外层再进写内层，见模块文档与 `_transaction`；连接与 `ReadWriteLock` 的配合亦见模块级说明。
    """

    def __init__(
        self,
        path: Path,
        lock: threading.RLock,
        *,
        pragmas: SQLitePragmas | None = None,
        payload_store: PayloadStore | None = None,
        deferred_access_flush_blocks: int = DEFAULT_DEFERRED_ACCESS_FLUSH_BLOCKS,
        deferred_access_flush_ns: int = DEFAULT_DEFERRED_ACCESS_FLUSH_NS,
    ):
        """用给定路径、进程内 `RLock`（FUSE/上层与元数据层协调，非本类替代读写锁）以及可选 PRAGMA、内联块存储与延迟访问刷盘阈值，创建连接池与 `ReadWriteLock` 并执行 `setup()`。"""
        self.path = path
        self.lock = lock
        self.payload_store = payload_store or NullPayloadStore()
        self._pool = ConnectionPool(path, pragmas=pragmas)
        self._rwlock = ReadWriteLock()
        self._local = threading.local()
        self._after_commit_hooks: list[Callable[[], None]] = []
        self._running_after_commit_hooks = False
        self._deferred_access_lock = threading.Lock()
        self._deferred_node_atimes: dict[int, int] = {}
        self._deferred_block_accesses: dict[str, BlockAccessStats] = {}
        self._deferred_access_started_ns: int | None = None
        self._deferred_access_flush_blocks = deferred_access_flush_blocks
        self._deferred_access_flush_ns = deferred_access_flush_ns
        logger.info("初始化 SQLite 元数据存储：path={}", path)
        try:
            self.setup()
        except Exception:
            self._pool.close()
            raise

    def close(self) -> None:
        """先在有延迟访问时通过一次空写事务刷盘并提交（见 `transaction`），再关闭连接池。"""
        logger.info("关闭 SQLite 元数据存储：path={}", self.path)
        if self.has_deferred_accesses():
            with self.transaction():
                pass
        self._pool.close()

    def commit(self) -> None:
        """无活动写事务、但仍有未刷的延迟访问时，用一次空写事务把 `flush_deferred_accesses` 纳入一次 `COMMIT`；无延迟则直接返回。不替代在业务写事务内的正常结束。"""
        if not self.has_deferred_accesses():
            return
        with self.transaction():
            pass

    def add_after_commit_hook(self, hook: Callable[[], None]) -> None:
        """Register a best-effort callback to run after successful outer write commits."""
        self._after_commit_hooks.append(hook)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """`write_transaction` 的别名：可写事务；成功时在 `COMMIT` 前会执行 `flush_deferred_accesses`。"""
        with self.write_transaction():
            yield

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        """只读事务：`BEGIN` + 读锁；可嵌套复用同连接。不可在其嵌套内再要可写 `write_transaction`。"""
        with self._transaction(readonly=True):
            yield

    @contextmanager
    def write_transaction(self) -> Iterator[None]:
        """可写事务：`BEGIN IMMEDIATE` + 写锁；`yield` 之后、`COMMIT` 前调用 `flush_deferred_accesses`；可嵌套复用同连接（仍为同一写事务）。"""
        with self._transaction(readonly=False):
            yield

    @contextmanager
    def _transaction(self, *, readonly: bool) -> Iterator[None]:
        # 嵌套：同线程复用已打开的 sqlite 连接；读事务内不允许降级为写。
        """在 `_local` 上建立/复用事务：最外层加 `ReadWriteLock` 与 `ConnectionPool` 取连接；嵌套只调整深度不重复加锁借连接。若已处于读事务且本次请求非只读则抛 `RuntimeError`。可写时于 `COMMIT` 前执行 `flush_deferred_accesses`；异常时 `ROLLBACK`。"""
        current = getattr(self._local, "db", None)
        if current is not None:
            if not readonly and getattr(self._local, "readonly", False):
                raise RuntimeError(
                    "cannot open a write transaction inside a read transaction"
                )
            yield
            return

        lock_context = (
            self._rwlock.read_lock() if readonly else self._rwlock.write_lock()
        )
        run_after_commit_hooks = False
        with lock_context, self._pool.connection() as db:
            self._local.db = db
            self._local.readonly = readonly
            try:
                db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
                yield
                if not readonly:
                    self.flush_deferred_accesses()
            except Exception as exc:
                if isinstance(exc, FuseOSError):
                    logger.debug(
                        "SQLite 事务因 FUSE 返回码回滚：readonly={}，errno={}",
                        readonly,
                        exc.errno,
                    )
                else:
                    logger.exception("SQLite 事务回滚：readonly={}", readonly)
                db.execute("ROLLBACK")
                raise
            else:
                db.execute("COMMIT")
                if not readonly:
                    self._local.db = None
                    self._local.readonly = False
                    run_after_commit_hooks = True
            finally:
                self._local.db = None
                self._local.readonly = False
        if run_after_commit_hooks:
            self._run_after_commit_hooks()

    @property
    def _db(self) -> sqlite3.Connection:
        """当前线程、当前事务内绑定的连接；无活动事务时访问会抛 `RuntimeError`。"""
        db = getattr(self._local, "db", None)
        if db is None:
            raise RuntimeError("metadata access requires an active transaction")
        return db

    def _run_after_commit_hooks(self) -> None:
        if self._running_after_commit_hooks or not self._after_commit_hooks:
            return
        self._running_after_commit_hooks = True
        try:
            for hook in self._after_commit_hooks:
                try:
                    hook()
                except Exception:
                    logger.exception("after-commit hook failed")
        finally:
            self._running_after_commit_hooks = False

    def setup(self) -> None:
        """持写锁从池中取连接，WAL 与 schema/根节点初始化在单次 `BEGIN IMMEDIATE`…`COMMIT` 中完成；不经过本类可嵌套事务的 `_transaction` 包装。"""
        logger.debug(
            "准备 SQLite schema：path={}，schema_version={}", self.path, SCHEMA_VERSION
        )
        with self._rwlock.write_lock(), self._pool.connection() as db:
            self._local.db = db
            self._local.readonly = False
            db.execute("PRAGMA journal_mode=WAL")
            self._validate_schema_version()
            db.execute("BEGIN IMMEDIATE")
            try:
                self._create_schema()
                self._ensure_root()
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            except Exception:
                logger.exception("SQLite schema 初始化失败，回滚：path={}", self.path)
                db.execute("ROLLBACK")
                raise
            else:
                db.execute("COMMIT")
            finally:
                self._local.db = None
                self._local.readonly = False
