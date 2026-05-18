"""版本化 schema：建表/迁移、配置行与块记录查询片段。

**SCHEMA_VERSION 与 user_version**

- `SCHEMA_VERSION`：代码期望的元数据布局版本，建库/迁移成功后应与库内 `PRAGMA user_version` 相等。
- `PRAGMA user_version`：SQLite 持久化的无符号 32 位整数（库文件头），供启动时判断是否与当前代码兼容；
  破坏性演进时同时改 DDL 与 `SCHEMA_VERSION`，并在迁移流程里 `PRAGMA user_version = ...` 与之对齐。

**CONFIG_VERSION**

`filesystem_config.config_version` 使用的行语义版本，与块/inode 布局的 `SCHEMA_VERSION` 独立。

**BLOCK_RECORD_SELECT**

将 `blocks` 与可选 `block_payloads` 联表，供一次查询块元数据、inline 载荷与冷热层 presence。
`hot_present` / `cold_present` 直接保存在 `blocks` 中；1=热层存在，2=冷层存在。
"""

import os

from stat import S_IFDIR
from time import time_ns

from .base import MetadataMixinBase

SCHEMA_VERSION = 10
CONFIG_VERSION = 1

FILESYSTEM_CONFIG_SELECT = """
    SELECT hot_tier_path, cold_tier_path
    FROM filesystem_config
    WHERE id = 1
"""

FILESYSTEM_CONFIG_UPSERT = """
    INSERT INTO filesystem_config (
        id, config_version, hot_tier_path, cold_tier_path
    )
    VALUES (1, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        config_version = excluded.config_version,
        hot_tier_path = excluded.hot_tier_path,
        cold_tier_path = excluded.cold_tier_path
"""

BLOCK_RECORD_SELECT = """
    SELECT
        blocks.hash,
        blocks.storage_kind AS storage,
        blocks.hot_present,
        blocks.cold_present,
        blocks.preferred_tier,
        blocks.compressed,
        blocks.raw_size,
        blocks.stored_size,
        blocks.refcount,
        blocks.atime_ns,
        blocks.read_count,
        blocks.last_promoted_ns,
        blocks.last_demoted_ns,
        blocks.cold_verified_ns,
        blocks.cold_gc_enqueued_ns,
        block_payloads.payload AS inline_payload
    FROM blocks
    LEFT JOIN block_payloads ON block_payloads.hash = blocks.hash
"""


class SchemaMixin(MetadataMixinBase):
    """提供建表 DDL、启动时 schema 版本校验及根 inode 初始化（与 `SCHEMA_VERSION` 一致）。"""

    def _validate_schema_version(self) -> None:
        """校验 `PRAGMA user_version` 是否与 `SCHEMA_VERSION` 匹配。

        `user_version == SCHEMA_VERSION` 则通过；若为 0 且库中尚无任何用户表，视为空库待建表，亦通过；
        否则抛出 `RuntimeError`，表示库文件与当前代码不兼容。
        """
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version == SCHEMA_VERSION:
            return
        if version == 0 and not self._has_existing_schema():
            return
        raise RuntimeError(
            f"unsupported metadata schema version {version}; expected {SCHEMA_VERSION}"
        )

    def _has_existing_schema(self) -> bool:
        """若 `sqlite_master` 中已有非系统对象（表/索引/触发器/视图），返回 True；否则 False。"""
        row = self._db.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger', 'view')
              AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone()
        return row is not None

    def _create_schema(self) -> None:
        """执行完整 DDL：inode/目录项/块/chunk/xattr/配置表及 `block_records` 视图等。"""
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS inodes (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('dir', 'file', 'symlink')),
                mode INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                gid INTEGER NOT NULL,
                size INTEGER NOT NULL DEFAULT 0 CHECK (size >= 0),
                symlink_target TEXT,
                nlink INTEGER NOT NULL DEFAULT 0 CHECK (nlink >= 0),
                atime_ns INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                CHECK (
                    (kind = 'symlink' AND symlink_target IS NOT NULL)
                    OR (kind != 'symlink' AND symlink_target IS NULL)
                )
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS dir_entries (
                parent_id INTEGER NOT NULL REFERENCES inodes(id) ON DELETE CASCADE,
                name TEXT NOT NULL CHECK (length(name) > 0 AND instr(name, '/') = 0),
                inode_id INTEGER NOT NULL REFERENCES inodes(id) ON DELETE RESTRICT,
                PRIMARY KEY(parent_id, name)
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dir_entries_inode ON dir_entries(inode_id)"
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS blocks (
                hash TEXT PRIMARY KEY,
                storage_kind TEXT NOT NULL CHECK (storage_kind IN ('inline', 'tiered')),
                preferred_tier INTEGER NOT NULL CHECK (preferred_tier IN (0, 1, 2)),
                hot_present INTEGER NOT NULL DEFAULT 0 CHECK (hot_present IN (0, 1)),
                cold_present INTEGER NOT NULL DEFAULT 0 CHECK (cold_present IN (0, 1)),
                compressed INTEGER NOT NULL CHECK (compressed IN (0, 1)),
                raw_size INTEGER NOT NULL CHECK (raw_size > 0),
                stored_size INTEGER NOT NULL CHECK (stored_size > 0),
                refcount INTEGER NOT NULL CHECK (refcount >= 0),
                atime_ns INTEGER NOT NULL,
                read_count INTEGER NOT NULL DEFAULT 0,
                last_promoted_ns INTEGER,
                last_demoted_ns INTEGER,
                cold_verified_ns INTEGER,
                cold_gc_enqueued_ns INTEGER,
                CHECK (
                    (storage_kind = 'inline' AND preferred_tier = 0 AND hot_present = 0 AND cold_present = 0)
                    OR (storage_kind = 'tiered' AND preferred_tier IN (1, 2) AND (hot_present = 1 OR cold_present = 1))
                )
            )
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_blocks_tiered_access
            ON blocks(storage_kind, atime_ns, read_count)
            WHERE storage_kind = 'tiered'
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_blocks_promoted_cleanup
            ON blocks(preferred_tier, hot_present, cold_present, last_promoted_ns)
            WHERE storage_kind = 'tiered' AND preferred_tier = 1 AND last_promoted_ns IS NOT NULL
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_blocks_cold_garbage
            ON blocks(cold_gc_enqueued_ns)
            WHERE storage_kind = 'tiered'
              AND refcount = 0
              AND cold_present = 1
              AND cold_gc_enqueued_ns IS NOT NULL
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS block_payloads (
                hash TEXT PRIMARY KEY REFERENCES blocks(hash) ON DELETE CASCADE,
                payload BLOB NOT NULL
            )
            """
        )
        self._db.execute(
            f"CREATE VIEW IF NOT EXISTS block_records AS {BLOCK_RECORD_SELECT}"
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS file_chunks (
                file_id INTEGER NOT NULL REFERENCES inodes(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
                hash TEXT NOT NULL REFERENCES blocks(hash) ON DELETE RESTRICT,
                size INTEGER NOT NULL CHECK (size > 0),
                PRIMARY KEY(file_id, chunk_index)
            ) WITHOUT ROWID
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_file_chunks_hash_chunk_index
            ON file_chunks(hash, chunk_index)
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS inode_xattrs (
                inode_id INTEGER NOT NULL REFERENCES inodes(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                value BLOB NOT NULL,
                PRIMARY KEY(inode_id, name)
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS filesystem_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                config_version INTEGER NOT NULL,
                hot_tier_path TEXT NOT NULL,
                cold_tier_path TEXT NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_deletions (
                id INTEGER PRIMARY KEY,
                digest TEXT NOT NULL,
                tier INTEGER NOT NULL CHECK (tier IN (1, 2)),
                enqueued_ns INTEGER NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_deletions_digest_tier
            ON pending_deletions(digest, tier)
            """
        )

    def _ensure_root(self) -> None:
        """确保 inode `id=1` 的根目录存在（`INSERT OR IGNORE`）。"""
        now = time_ns()
        self._db.execute(
            """
            INSERT OR IGNORE INTO inodes
                (id, kind, mode, uid, gid, size, symlink_target, nlink, atime_ns, mtime_ns, ctime_ns)
            VALUES
                (1, 'dir', ?, ?, ?, 0, NULL, 1, ?, ?, ?)
            """,
            (S_IFDIR | 0o755, os.getuid(), os.getgid(), now, now, now),
        )

    def filesystem_config(self):
        """读取 `filesystem_config` 单行（id=1）：热/冷路径配置。"""
        return self._db.execute(FILESYSTEM_CONFIG_SELECT).fetchone()

    def set_filesystem_config(
        self,
        *,
        hot_tier_path: str,
        cold_tier_path: str,
    ) -> None:
        """写入或更新挂载配置（upsert id=1），`config_version` 使用模块常量 `CONFIG_VERSION`。"""
        self._db.execute(
            FILESYSTEM_CONFIG_UPSERT,
            (
                CONFIG_VERSION,
                hot_tier_path,
                cold_tier_path,
            ),
        )
