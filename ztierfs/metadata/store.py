"""SQLite 元数据门面：组合 schema、命名空间、分块、块表与访问统计等 mixin。"""

import sqlite3
import threading

from collections.abc import Iterator
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
from .namespace import NamespaceMixin
from .schema import SCHEMA_VERSION, SchemaMixin
from ztierfs.payload_store import NullPayloadStore, PayloadStore


class MetadataStore(
    SchemaMixin,
    NamespaceMixin,
    ChunkMetadataMixin,
    BlockMetadataMixin,
    AccessStatsMixin,
):
    """显式读/写事务与连接池上的 inode、目录项、块与 file_chunks 操作入口。"""

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
        self.path = path
        self.lock = lock
        self.payload_store = payload_store or NullPayloadStore()
        self._pool = ConnectionPool(path, pragmas=pragmas)
        self._rwlock = ReadWriteLock()
        self._local = threading.local()
        self._deferred_access_lock = threading.Lock()
        self._deferred_node_atimes: dict[int, int] = {}
        self._deferred_block_accesses: dict[str, BlockAccessStats] = {}
        self._deferred_access_started_ns: int | None = None
        self._deferred_access_flush_blocks = deferred_access_flush_blocks
        self._deferred_access_flush_ns = deferred_access_flush_ns
        logger.info("初始化 SQLite 元数据存储：path={}", path)
        self.setup()

    def close(self) -> None:
        logger.info("关闭 SQLite 元数据存储：path={}", self.path)
        if self.has_deferred_accesses():
            with self.transaction():
                pass
        self._pool.close()

    def commit(self) -> None:
        if not self.has_deferred_accesses():
            return
        with self.transaction():
            pass

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.write_transaction():
            yield

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        with self._transaction(readonly=True):
            yield

    @contextmanager
    def write_transaction(self) -> Iterator[None]:
        with self._transaction(readonly=False):
            yield

    @contextmanager
    def _transaction(self, *, readonly: bool) -> Iterator[None]:
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
            finally:
                self._local.db = None
                self._local.readonly = False

    @property
    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            raise RuntimeError("metadata access requires an active transaction")
        return db

    def setup(self) -> None:
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
