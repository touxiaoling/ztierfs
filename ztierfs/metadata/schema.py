import os

from stat import S_IFDIR
from time import time_ns

from .base import MetadataMixinBase

SCHEMA_VERSION = 6
CONFIG_VERSION = 1


BLOCK_RECORD_SELECT = """
    SELECT
        blocks.hash,
        blocks.storage_kind AS storage,
        CASE
            WHEN hot_locations.hash IS NULL THEN 0
            ELSE 1
        END AS hot_present,
        CASE
            WHEN cold_locations.hash IS NULL THEN 0
            ELSE 1
        END AS cold_present,
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
        block_payloads.payload AS inline_payload,
        block_payloads.payload_store AS inline_payload_store,
        block_payloads.payload_key AS inline_payload_key
    FROM blocks
    LEFT JOIN block_locations AS hot_locations
        ON hot_locations.hash = blocks.hash AND hot_locations.tier = 1
    LEFT JOIN block_locations AS cold_locations
        ON cold_locations.hash = blocks.hash AND cold_locations.tier = 2
    LEFT JOIN block_payloads ON block_payloads.hash = blocks.hash
"""


class SchemaMixin(MetadataMixinBase):
    def _validate_schema_version(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version == SCHEMA_VERSION:
            return
        if version == 0 and not self._has_existing_schema():
            return
        raise RuntimeError(
            f"unsupported metadata schema version {version}; expected {SCHEMA_VERSION}"
        )

    def _has_existing_schema(self) -> bool:
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
            CREATE TABLE IF NOT EXISTS inode_payloads (
                inode_id INTEGER PRIMARY KEY REFERENCES inodes(id) ON DELETE CASCADE,
                payload BLOB,
                payload_store TEXT NOT NULL DEFAULT 'sqlite',
                payload_key TEXT,
                compressed INTEGER NOT NULL CHECK (compressed IN (0, 1)),
                raw_size INTEGER NOT NULL CHECK (raw_size > 0),
                stored_size INTEGER NOT NULL CHECK (stored_size > 0),
                CHECK (
                    (payload_store = 'sqlite' AND payload IS NOT NULL AND payload_key IS NULL)
                    OR (payload_store != 'sqlite' AND payload IS NULL AND payload_key IS NOT NULL)
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
                compressed INTEGER NOT NULL CHECK (compressed IN (0, 1)),
                raw_size INTEGER NOT NULL CHECK (raw_size > 0),
                stored_size INTEGER NOT NULL CHECK (stored_size > 0),
                refcount INTEGER NOT NULL CHECK (refcount >= 0),
                atime_ns INTEGER NOT NULL,
                read_count INTEGER NOT NULL DEFAULT 0,
                last_promoted_ns INTEGER,
                last_demoted_ns INTEGER,
                cold_verified_ns INTEGER,
                CHECK (
                    (storage_kind = 'inline' AND preferred_tier = 0)
                    OR (storage_kind = 'tiered' AND preferred_tier IN (1, 2))
                )
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS block_locations (
                hash TEXT NOT NULL REFERENCES blocks(hash) ON DELETE CASCADE,
                tier INTEGER NOT NULL CHECK (tier IN (1, 2)),
                PRIMARY KEY(hash, tier)
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_block_locations_tier_hash ON block_locations(tier, hash)"
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
            ON blocks(preferred_tier, last_promoted_ns)
            WHERE storage_kind = 'tiered' AND preferred_tier = 1 AND last_promoted_ns IS NOT NULL
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS block_payloads (
                hash TEXT PRIMARY KEY REFERENCES blocks(hash) ON DELETE CASCADE,
                payload BLOB,
                payload_store TEXT NOT NULL DEFAULT 'sqlite',
                payload_key TEXT,
                CHECK (
                    (payload_store = 'sqlite' AND payload IS NOT NULL AND payload_key IS NULL)
                    OR (payload_store != 'sqlite' AND payload IS NULL AND payload_key IS NOT NULL)
                )
            )
            """
        )
        self._db.execute(f"CREATE VIEW IF NOT EXISTS block_records AS {BLOCK_RECORD_SELECT}")
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
                cold_tier_path TEXT NOT NULL,
                payload_store TEXT NOT NULL CHECK (payload_store IN ('sqlite', 'filekv')),
                payload_store_path TEXT,
                CHECK (
                    (payload_store = 'sqlite' AND payload_store_path IS NULL)
                    OR (payload_store = 'filekv' AND payload_store_path IS NOT NULL)
                )
            )
            """
        )

    def _ensure_root(self) -> None:
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
        return self._db.execute(
            """
            SELECT hot_tier_path, cold_tier_path, payload_store, payload_store_path
            FROM filesystem_config
            WHERE id = 1
            """
        ).fetchone()

    def set_filesystem_config(
        self,
        *,
        hot_tier_path: str,
        cold_tier_path: str,
        payload_store: str,
        payload_store_path: str | None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO filesystem_config (
                id, config_version, hot_tier_path, cold_tier_path, payload_store, payload_store_path
            )
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                config_version = excluded.config_version,
                hot_tier_path = excluded.hot_tier_path,
                cold_tier_path = excluded.cold_tier_path,
                payload_store = excluded.payload_store,
                payload_store_path = excluded.payload_store_path
            """,
            (
                CONFIG_VERSION,
                hot_tier_path,
                cold_tier_path,
                payload_store,
                payload_store_path,
            ),
        )
