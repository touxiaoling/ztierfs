from pathlib import Path

from loguru import logger
from ztierfs.metadata import open_database

from .config import resolve_maintenance_paths
from .reports import StatsReport


def scalar(db, sql: str) -> int:
    return int(db.execute(sql).fetchone()[0])


def collect_stats(
    path: str | Path,
    tier2: str | Path | None = None,
    database: str | Path | None = None,
    *,
    allow_config_mismatch: bool = False,
    update_config: bool = False,
) -> StatsReport:
    paths = resolve_maintenance_paths(
        path,
        tier2,
        database,
        allow_config_mismatch=allow_config_mismatch,
        update_config=update_config,
    )
    db_path = paths.database
    logger.info("收集统计信息：database={}，tier1={}，tier2={}", db_path, paths.tier1, paths.tier2)
    with open_database(db_path) as db:
        inode_counts = {
            row["kind"]: row["count"]
            for row in db.execute("SELECT kind, COUNT(*) AS count FROM inodes GROUP BY kind").fetchall()
        }
        block_counts = {
            "total": scalar(db, "SELECT COUNT(*) FROM blocks"),
            "inline": scalar(db, "SELECT COUNT(*) FROM blocks WHERE storage_kind = 'inline'"),
            "inode_inline": scalar(
                db,
                "SELECT COUNT(*) FROM inode_payloads",
            ),
            "hot": scalar(
                db,
                """
                SELECT COUNT(*)
                FROM blocks
                JOIN block_locations ON block_locations.hash = blocks.hash
                WHERE blocks.storage_kind = 'tiered' AND block_locations.tier = 1
                """,
            ),
            "cold": scalar(
                db,
                """
                SELECT COUNT(*)
                FROM blocks
                JOIN block_locations ON block_locations.hash = blocks.hash
                WHERE blocks.storage_kind = 'tiered' AND block_locations.tier = 2
                """,
            ),
            "both": scalar(
                db,
                """
                SELECT COUNT(*)
                FROM blocks
                JOIN block_locations AS hot_locations
                  ON hot_locations.hash = blocks.hash AND hot_locations.tier = 1
                JOIN block_locations AS cold_locations
                  ON cold_locations.hash = blocks.hash AND cold_locations.tier = 2
                WHERE blocks.storage_kind = 'tiered'
                """,
            ),
            "compressed": scalar(db, "SELECT COUNT(*) FROM blocks WHERE compressed = 1"),
        }
        storage = {
            "logical_file_bytes": scalar(db, "SELECT COALESCE(SUM(size), 0) FROM inodes WHERE kind = 'file'"),
            "referenced_chunk_bytes": scalar(db, "SELECT COALESCE(SUM(size), 0) FROM file_chunks"),
            "unique_raw_bytes": scalar(db, "SELECT COALESCE(SUM(raw_size), 0) FROM blocks")
            + scalar(
                db,
                "SELECT COALESCE(SUM(raw_size), 0) FROM inode_payloads",
            ),
            "stored_bytes": scalar(db, "SELECT COALESCE(SUM(stored_size), 0) FROM blocks")
            + scalar(
                db,
                "SELECT COALESCE(SUM(stored_size), 0) FROM inode_payloads",
            ),
            "inode_inline_stored_bytes": scalar(
                db,
                "SELECT COALESCE(SUM(stored_size), 0) FROM inode_payloads",
            ),
            "inline_stored_bytes": scalar(db, "SELECT COALESCE(SUM(stored_size), 0) FROM blocks WHERE storage_kind = 'inline'"),
            "hot_stored_bytes": scalar(
                db,
                """
                SELECT COALESCE(SUM(stored_size), 0)
                FROM blocks
                JOIN block_locations ON block_locations.hash = blocks.hash
                WHERE blocks.storage_kind = 'tiered' AND block_locations.tier = 1
                """,
            ),
            "cold_stored_bytes": scalar(
                db,
                """
                SELECT COALESCE(SUM(stored_size), 0)
                FROM blocks
                JOIN block_locations ON block_locations.hash = blocks.hash
                WHERE blocks.storage_kind = 'tiered' AND block_locations.tier = 2
                """,
            ),
        }
        report = StatsReport(
            inodes={
                "total": sum(inode_counts.values()),
                "files": inode_counts.get("file", 0),
                "directories": inode_counts.get("dir", 0),
                "symlinks": inode_counts.get("symlink", 0),
            },
            entries={"dir_entries": scalar(db, "SELECT COUNT(*) FROM dir_entries")},
            chunks={"file_chunks": scalar(db, "SELECT COUNT(*) FROM file_chunks")},
            blocks=block_counts,
            storage=storage,
        )
    logger.info(
        "统计信息收集完成：inodes={}，blocks={}，stored_bytes={}",
        report.inodes["total"],
        report.blocks["total"],
        report.storage["stored_bytes"],
    )
    return report
