"""copy-up 后冷层上多余块副本的按龄清理。"""

from contextlib import closing
from pathlib import Path
from dataclasses import dataclass
from time import time_ns

from loguru import logger

from ztierfs.metadata import open_database
from ztierfs.tier_access import PathUnavailable, probe_path, unlink_path

from .config import resolve_maintenance_paths
from .paths import block_path


@dataclass(frozen=True)
class CleanupReport:
    """copy-up 后将冷层多余副本清理完毕后的计数汇总。

    ``removed``：已成功删除冷层块文件，并从 ``block_locations`` 中移除 tier 2 记录的数量。
    ``skipped``：因冷层路径暂时不可用或删除失败而跳过、未改对应元数据的数量。
    """
    removed: int = 0
    skipped: int = 0


def cleanup_promoted_cold_copies(
    path: str | Path,
    tier2: str | Path | None = None,
    database: str | Path | None = None,
    *,
    min_age_seconds: int,
    allow_config_mismatch: bool = False,
    update_config: bool = False,
) -> CleanupReport:
    """在块已 copy-up 到热层且元数据首选热层后，按「提升」龄删除冷层上冗余副本并收紧位置记录。

    仅针对 ``storage_kind`` 为分层块、热冷两层均有 ``block_locations``、``preferred_tier`` 为热层、
    且 ``last_promoted_ns`` 不晚于当前时刻减去 ``min_age_seconds`` 的条目：尝试删除冷层上的块文件，
    成功则删除该行在 ``block_locations`` 中 tier 2 的记录。若冷层路径探测或删除因暂时不可用失败，
    则跳过该项（不计入删除），对应元数据保持不变。

    ``path`` / ``tier2`` / ``database`` 由 ``resolve_maintenance_paths`` 解析为数据库与冷热层根路径；
    ``allow_config_mismatch`` 与 ``update_config`` 的含义与同函数的 CLI 维护路径解析一致。
    """
    cutoff_ns = time_ns() - min_age_seconds * 1_000_000_000
    paths = resolve_maintenance_paths(
        path,
        tier2,
        database,
        allow_config_mismatch=allow_config_mismatch,
        update_config=update_config,
    )
    db_path = paths.database
    tier1_path = paths.tier1
    tier2_path = paths.tier2
    removed = 0
    skipped = 0
    logger.info(
        "开始维护清理冷层副本：database={}，tier1={}，tier2={}，min_age_seconds={}",
        db_path,
        tier1_path,
        tier2_path,
        min_age_seconds,
    )
    with closing(open_database(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            rows = db.execute(
                """
                SELECT blocks.hash
                FROM blocks
                JOIN block_locations AS hot_locations
                  ON hot_locations.hash = blocks.hash AND hot_locations.tier = 1
                JOIN block_locations AS cold_locations
                  ON cold_locations.hash = blocks.hash AND cold_locations.tier = 2
                WHERE blocks.storage_kind = 'tiered'
                  AND blocks.preferred_tier = 1
                  AND blocks.last_promoted_ns IS NOT NULL
                  AND blocks.last_promoted_ns <= ?
                ORDER BY last_promoted_ns ASC
                """,
                (cutoff_ns,),
            ).fetchall()
            for row in rows:
                path = block_path(tier1_path, tier2_path, row["hash"], 2)
                probe = probe_path(path)
                if probe.unavailable:
                    skipped += 1
                    logger.warning(
                        "维护清理跳过冷层副本：冷层临时不可用，hash={}，error={}",
                        row["hash"][:12],
                        probe.error,
                    )
                    continue
                try:
                    if probe.present:
                        unlink_path(path)
                except PathUnavailable as exc:
                    skipped += 1
                    logger.warning(
                        "维护清理跳过冷层副本：删除时冷层临时不可用，hash={}，error={}",
                        row["hash"][:12],
                        exc,
                    )
                    continue
                db.execute(
                    "DELETE FROM block_locations WHERE hash = ? AND tier = 2",
                    (row["hash"],),
                )
                removed += 1
                logger.debug("维护清理冷层副本：hash={}", row["hash"][:12])
        except Exception:
            logger.exception("维护清理失败，回滚事务")
            db.execute("ROLLBACK")
            raise
        else:
            db.execute("COMMIT")
    logger.info("维护清理完成：removed={}，skipped={}", removed, skipped)
    return CleanupReport(removed=removed, skipped=skipped)
