"""SQLite 连接池、TimedConnection 与 busy/pragma 等打开参数。"""

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
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
    cache_size: int = DEFAULT_SQLITE_CACHE_SIZE
    mmap_size: int = DEFAULT_SQLITE_MMAP_SIZE
    wal_autocheckpoint: int = DEFAULT_SQLITE_WAL_AUTOCHECKPOINT
    synchronous: str = "NORMAL"
    journal_size_limit: int = 128 * 1024 * 1024
    temp_store: str = "MEMORY"


class TimedConnection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):  # noqa: ANN001
        with timed("sqlite.execute"):
            return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):  # noqa: ANN001
        with timed("sqlite.executemany"):
            return super().executemany(sql, parameters)


def open_database(
    path: Path,
    *,
    busy_timeout_ms: int | None = None,
    pragmas: SQLitePragmas | None = None,
) -> sqlite3.Connection:
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
    def __init__(
        self,
        path: Path,
        *,
        max_size: int = DEFAULT_POOL_SIZE,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        pragmas: SQLitePragmas | None = None,
    ):
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self.path = path
        self.max_size = max_size
        self.pragmas = pragmas or SQLitePragmas(busy_timeout_ms=busy_timeout_ms)
        self._available: LifoQueue[sqlite3.Connection] = LifoQueue(max_size)
        self._condition = threading.Condition()
        self._created = 0
        self._closed = False
        logger.debug("创建 SQLite 连接池：path={}，max_size={}", path, max_size)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = self.acquire()
        try:
            yield db
        finally:
            self.release(db)

    def acquire(self, timeout: float | None = None) -> sqlite3.Connection:
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
                return open_database(self.path, pragmas=self.pragmas)
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
                raise RuntimeError("connection pool is closed")
        return db

    def release(self, db: sqlite3.Connection) -> None:
        with self._condition:
            if self._closed:
                db.close()
                return
        self._available.put(db)

    def close(self) -> None:
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


class ReadWriteLock:
    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def read_lock(self) -> Iterator[None]:
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()

    def acquire_read(self) -> None:
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._condition:
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    def acquire_write(self) -> None:
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1

    def release_write(self) -> None:
        with self._condition:
            self._writer = False
            self._condition.notify_all()
