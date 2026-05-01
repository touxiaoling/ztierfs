"""SQLite 连接池、`TimedConnection` 与 pragma/busy 超时等打开参数。

`ConnectionPool` 在 `max_size` 内复用连接；取连接超时表示池耗尽。`ReadWriteLock` 与池配合：
MetadataStore 用写锁串行化 schema 初始化，用读/写锁区分只读与 IMMEDIATE 事务的并发模型。
"""

import sqlite3
import threading

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, LifoQueue

from loguru import logger

from ztierfs.perf import timed

DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_POOL_SIZE = 8
DEFAULT_SQLITE_CACHE_SIZE = -32 * 1024
DEFAULT_SQLITE_MMAP_SIZE = 256 * 1024 * 1024
DEFAULT_SQLITE_WAL_AUTOCHECKPOINT = 8192


@dataclass(frozen=True)
class SQLitePragmas:
    """打开数据库时应用的 SQLite PRAGMA 参数集合（不可变）。

    字段含义与 `open_database` 中执行的 `PRAGMA` 一一对应：`busy_timeout_ms` 为锁等待毫秒数；
    `cache_size` 为页缓存（负数表示 KB）；`mmap_size` 为内存映射上限；`wal_autocheckpoint` 为 WAL
    自动 checkpoint 页数；`synchronous` 为同步模式；`journal_size_limit` 限制 WAL 日志体量；
    `temp_store` 控制临时表存放位置。
    """
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
    cache_size: int = DEFAULT_SQLITE_CACHE_SIZE
    mmap_size: int = DEFAULT_SQLITE_MMAP_SIZE
    wal_autocheckpoint: int = DEFAULT_SQLITE_WAL_AUTOCHECKPOINT
    synchronous: str = "NORMAL"
    journal_size_limit: int = 128 * 1024 * 1024
    temp_store: str = "MEMORY"


class TimedConnection(sqlite3.Connection):
    """带计时的 `sqlite3.Connection`：在 `execute` / `executemany` 外包一层性能埋点。"""

    def execute(self, sql, parameters=(), /):  # noqa: ANN001
        """执行单条 SQL，并通过 `timed("sqlite.execute")` 记录耗时。"""
        with timed("sqlite.execute"):
            return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):  # noqa: ANN001
        """对多组参数批量执行同一条 SQL，并通过 `timed("sqlite.executemany")` 记录耗时。"""
        with timed("sqlite.executemany"):
            return super().executemany(sql, parameters)


def open_database(
    path: Path,
    *,
    busy_timeout_ms: int | None = None,
    pragmas: SQLitePragmas | None = None,
) -> sqlite3.Connection:
    """在指定路径打开 SQLite，返回已配置 PRAGMA 与 `Row` 工厂的连接。

    使用 `TimedConnection` 作为连接类、`isolation_level=None`（自动提交模式由上层事务控制）、
    `check_same_thread=False` 以配合连接池跨线程复用。若未传入 `pragmas`，则用
    `busy_timeout_ms` 参数或默认忙等待构造 `SQLitePragmas`。

    将返回值用作 ``with open_database(...) as db`` 时，退出上下文只处理事务提交/回滚，
    **不会** ``close()`` 连接；一次性使用请包一层 ``contextlib.closing``（维护 CLI 已如此）。
    """
    logger.debug("打开 SQLite 连接：path={}", path)
    pragmas = pragmas or SQLitePragmas(
        busy_timeout_ms=DEFAULT_BUSY_TIMEOUT_MS
        if busy_timeout_ms is None
        else busy_timeout_ms
    )
    db = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,
        factory=TimedConnection,
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute(f"PRAGMA synchronous={pragmas.synchronous}")
    db.execute(f"PRAGMA busy_timeout={pragmas.busy_timeout_ms}")
    db.execute(f"PRAGMA cache_size={pragmas.cache_size}")
    db.execute(f"PRAGMA mmap_size={pragmas.mmap_size}")
    db.execute(f"PRAGMA wal_autocheckpoint={pragmas.wal_autocheckpoint}")
    db.execute(f"PRAGMA journal_size_limit={pragmas.journal_size_limit}")
    db.execute(f"PRAGMA temp_store={pragmas.temp_store}")
    return db


class ConnectionPool:
    """SQLite 连接池：在 `max_size` 以内复用连接，超出则阻塞或超时。

    空闲连接放在 LIFO 队列中；`acquire` 在无可用连接且已达上限时，在队列上阻塞，直至其他线程
    `release` 归还连接，或阻塞时间超过 `acquire` 的 `timeout` 参数。`close` 后归还的连接会被关闭，
    后续 `acquire` 抛错。
    """
    def __init__(
        self,
        path: Path,
        *,
        max_size: int = DEFAULT_POOL_SIZE,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        pragmas: SQLitePragmas | None = None,
    ):
        """初始化实例。"""
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self.path = path
        self.max_size = max_size
        self.pragmas = pragmas or SQLitePragmas(busy_timeout_ms=busy_timeout_ms)
        self._available: LifoQueue[sqlite3.Connection] = LifoQueue(max_size)
        self._condition = threading.Condition()
        self._created = 0
        self._closed = False
        self._connections: set[sqlite3.Connection] = set()
        logger.debug("创建 SQLite 连接池：path={}，max_size={}", path, max_size)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """`acquire` 取连接、`yield` 给调用方、`finally` 里 `release` 归还；默认 `acquire` 不限制等待时长。"""
        db = self.acquire()
        try:
            yield db
        finally:
            self.release(db)

    def acquire(self, timeout: float | None = None) -> sqlite3.Connection:
        """从池中取一条连接；必要时阻塞。

        若池中有空闲连接则立即返回；否则在未达 `max_size` 时新建一条；已达上限则在队列上
        等待，直到其他线程 `release` 归还连接。`timeout` 为阻塞的最长秒数（`None` 表示一直等）；
        超时仍无连接则抛出 `TimeoutError`。池已关闭时抛 `RuntimeError`。
        """
        with self._condition:
            if self._closed:
                raise RuntimeError("connection pool is closed")
            try:
                return self._available.get_nowait()
            except Empty:
                pass
            if self._created < self.max_size:
                self._created += 1
                create = True
            else:
                create = False

        if create:
            try:
                logger.debug(
                    "连接池创建新连接：path={}，created={}", self.path, self._created
                )
                db = open_database(self.path, pragmas=self.pragmas)
                self._connections.add(db)
                return db
            except Exception:
                with self._condition:
                    self._created -= 1
                    self._condition.notify()
                raise

        try:
            db = self._available.get(timeout=timeout)
        except Empty as exc:
            raise TimeoutError("timed out waiting for a database connection") from exc
        with self._condition:
            if self._closed:
                db.close()
                self._connections.discard(db)
                raise RuntimeError("connection pool is closed")
        return db

    def release(self, db: sqlite3.Connection) -> None:
        """将连接归还池中供复用；若池已关闭则关闭该连接而不入队。"""
        with self._condition:
            if self._closed:
                db.close()
                self._connections.discard(db)
                return
        self._available.put(db)

    def close(self) -> None:
        """处理 close。"""
        logger.debug("关闭 SQLite 连接池：path={}", self.path)
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        while True:
            try:
                db = self._available.get_nowait()
            except Empty:
                break
            db.close()
            self._connections.discard(db)
        for db in list(self._connections):
            db.close()
        self._connections.clear()


class ReadWriteLock:
    """读写锁，采用写者优先（writer-preference）策略。

    当存在正在等待的写者（`acquire_write` 已排队）时，新的读者在 `acquire_read` 中阻塞，
    直到写者获得并释放锁，从而避免读多写少场景下写者长期得不到锁（写饥饿）。写者之间、
    读者与当前写者仍互斥；同一时刻最多一个写者，多个读者可并发持有读锁（在无等待写者时）。
    """

    def __init__(self):
        """初始化实例。"""
        self._condition = threading.Condition(threading.RLock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def read_lock(self) -> Iterator[None]:
        """读锁上下文：进入时 `acquire_read`，退出时 `release_read`（写者优先下可能阻塞）。"""
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """写锁上下文：进入时 `acquire_write`，退出时 `release_write`。"""
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()

    def acquire_read(self) -> None:
        """获取读锁；若已有写者持有锁，或已有写者在等待，则阻塞（写者优先）。"""
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1

    def release_read(self) -> None:
        """释放读锁；当读者计数归零时唤醒等待的写者/读者。"""
        with self._condition:
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    def acquire_write(self) -> None:
        """获取写锁：先增加等待写者计数（使新读者阻塞），再在无其他写者且无读者时独占。"""
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1

    def release_write(self) -> None:
        """释放写锁并唤醒全部等待者（后续由条件判断决定读者或写者进展）。"""
        with self._condition:
            self._writer = False
            self._condition.notify_all()
