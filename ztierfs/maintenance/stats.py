"""从 SQLite 元数据库读取维护用统计，组装为 `StatsReport`。

`StatsReport` 五段（`inodes` / `entries` / `chunks` / `blocks` / `storage`）均为 SQL 聚合结果，彼此维度不同，不可直接相加等同「磁盘占用」或「用户可见文件总大小」。例如 `blocks.total` 是 `blocks` 表行数；`storage.logical_file_bytes` 是普通文件 inode 上记录的 `size` 之和；去重与压缩会使 `unique_raw_bytes`、`stored_bytes` 与逻辑大小不一致。
"""

from contextlib import closing
from pathlib import Path

from loguru import logger
from ztierfs.metadata import open_database

from .config import resolve_maintenance_paths
from .reports import StatsReport


def scalar(db, sql: str) -> int:
    """执行只返回单行单列的 SQL（如 ``COUNT(*)``、``COALESCE(SUM(...), 0)``），取第一列并转为 ``int``。"""
    return int(db.execute(sql).fetchone()[0])


def collect_stats(
    database: str | Path,
) -> StatsReport:
    """解析维护路径并打开元数据库，用聚合查询填充 `StatsReport`。

    **inodes**：由 ``SELECT kind, COUNT(*) ... GROUP BY kind`` 得到各类 inode 个数；返回中的 ``total`` 为各类之和，``files``/``directories``/``symlinks`` 分别对应 ``kind`` 为 ``file``/``dir``/``symlink`` 的 ``COUNT(*)``。

    **entries**：``dir_entries`` 为 ``COUNT(*)``，目录项（名字 → inode）总行数。

    **chunks**：``file_chunks`` 为 ``COUNT(*)``，文件块映射表总行数（描述文件逻辑区间与块 ``hash`` 的对应关系）。

    **blocks**（均为 ``COUNT(*)``，计数对象略有不同）：

    - ``total``：``blocks`` 表行数（内容寻址块记录总数）。
    - ``inline``：``storage_kind = 'inline'`` 的块（小块元数据记在 ``blocks``，载荷可在 SQLite）。
    - ``hot``：``storage_kind = 'tiered'`` 且 ``hot_present = 1`` 的块。
    - ``cold``：同上但 ``tier = 2``（冷层）。
    - ``both``：同时关联到 ``tier = 1`` 与 ``tier = 2`` 的 ``tiered`` 块（例如迁移中间态或双副本策略下的块数）。
    - ``compressed``：``compressed = 1`` 的块行数。

    **storage**（字节和；多处为 ``SUM``，无行时 ``COALESCE(..., 0)``）：

    - ``logical_file_bytes``：``inodes`` 中 ``kind = 'file'`` 的 ``SUM(size)``，即各普通文件当前逻辑长度之和（稀疏/洞不一定反映在未引用块上）。
    - ``referenced_chunk_bytes``：``file_chunks.size`` 的 ``SUM``，chunk 行声明覆盖的字节总量（与 inode size、块去重关系需分开理解）。
    - ``unique_raw_bytes``：``blocks.raw_size`` 的 ``SUM``，未压缩载荷字节总量（去重后按块存储计）。
    - ``stored_bytes``：``blocks.stored_size`` 的 ``SUM``，实际持久化字节（含压缩等）。
    - ``inline_stored_bytes``：仅 ``blocks`` 且 ``storage_kind = 'inline'`` 的 ``SUM(stored_size)``。
    - ``hot_stored_bytes`` / ``cold_stored_bytes``：``tiered`` 块按 ``hot_present`` / ``cold_present`` 对 ``blocks.stored_size`` 求和（同一 ``tiered`` 块若在两 tier 均有位置，两段 ``SUM`` 可能各计一份，与 ``both`` 语义一致）。
    """
    paths = resolve_maintenance_paths(database)
    db_path = paths.database
    logger.info(
        "收集统计信息：database={}，tier1={}，tier2={}",
        db_path,
        paths.tier1,
        paths.tier2,
    )
    with closing(open_database(db_path)) as db:
        inode_counts = {
            row["kind"]: row["count"]
            for row in db.execute(
                "SELECT kind, COUNT(*) AS count FROM inodes GROUP BY kind"
            ).fetchall()
        }
        block_counts = {
            "total": scalar(db, "SELECT COUNT(*) FROM blocks"),
            "inline": scalar(
                db, "SELECT COUNT(*) FROM blocks WHERE storage_kind = 'inline'"
            ),
            "hot": scalar(
                db,
                """
                SELECT COUNT(*)
                FROM blocks
                WHERE storage_kind = 'tiered' AND hot_present = 1
                """,
            ),
            "cold": scalar(
                db,
                """
                SELECT COUNT(*)
                FROM blocks
                WHERE storage_kind = 'tiered' AND cold_present = 1
                """,
            ),
            "both": scalar(
                db,
                """
                SELECT COUNT(*)
                FROM blocks
                WHERE storage_kind = 'tiered'
                  AND hot_present = 1
                  AND cold_present = 1
                """,
            ),
            "cold_garbage": scalar(
                db,
                """
                SELECT COUNT(*)
                FROM blocks
                WHERE storage_kind = 'tiered'
                  AND refcount = 0
                  AND cold_present = 1
                """,
            ),
            "compressed": scalar(
                db, "SELECT COUNT(*) FROM blocks WHERE compressed = 1"
            ),
        }
        storage = {
            "logical_file_bytes": scalar(
                db, "SELECT COALESCE(SUM(size), 0) FROM inodes WHERE kind = 'file'"
            ),
            "referenced_chunk_bytes": scalar(
                db, "SELECT COALESCE(SUM(size), 0) FROM file_chunks"
            ),
            "unique_raw_bytes": scalar(
                db, "SELECT COALESCE(SUM(raw_size), 0) FROM blocks"
            ),
            "stored_bytes": scalar(
                db, "SELECT COALESCE(SUM(stored_size), 0) FROM blocks"
            ),
            "inline_stored_bytes": scalar(
                db,
                "SELECT COALESCE(SUM(stored_size), 0) FROM blocks WHERE storage_kind = 'inline'",
            ),
            "hot_stored_bytes": scalar(
                db,
                """
                SELECT COALESCE(SUM(stored_size), 0)
                FROM blocks
                WHERE storage_kind = 'tiered' AND hot_present = 1
                """,
            ),
            "cold_stored_bytes": scalar(
                db,
                """
                SELECT COALESCE(SUM(stored_size), 0)
                FROM blocks
                WHERE storage_kind = 'tiered' AND cold_present = 1
                """,
            ),
            "cold_garbage_bytes": scalar(
                db,
                """
                SELECT COALESCE(SUM(stored_size), 0)
                FROM blocks
                WHERE storage_kind = 'tiered'
                  AND refcount = 0
                  AND cold_present = 1
                """,
            ),
        }
        oldest_cold_garbage_ns = scalar(
            db,
            """
            SELECT COALESCE(MIN(cold_gc_enqueued_ns), 0)
            FROM blocks
            WHERE storage_kind = 'tiered'
              AND refcount = 0
              AND cold_present = 1
              AND cold_gc_enqueued_ns IS NOT NULL
            """,
        )
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
            maintenance={
                "pending_deletions": scalar(
                    db, "SELECT COUNT(*) FROM pending_deletions"
                ),
                "cold_garbage": block_counts["cold_garbage"],
                "cold_garbage_bytes": storage["cold_garbage_bytes"],
                "oldest_cold_garbage_ns": oldest_cold_garbage_ns,
            },
        )
    logger.info(
        "统计信息收集完成：inodes={}，blocks={}，stored_bytes={}",
        report.inodes["total"],
        report.blocks["total"],
        report.storage["stored_bytes"],
    )
    return report
