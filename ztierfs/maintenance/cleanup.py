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

    ``removed``：已成功删除冷层块文件，并清除 ``blocks.cold_present`` 的数量。
    ``skipped``：因冷层路径暂时不可用或删除失败而跳过、未改对应元数据的数量。
    """

    removed: int = 0
    skipped: int = 0
    pending_removed: int = 0
    pending_skipped: int = 0


def cleanup_promoted_cold_copies(
    database: str | Path,
    *,
    min_age_seconds: int,
) -> CleanupReport:
    """在块已 copy-up 到热层且元数据首选热层后，按「提升」龄删除冷层上冗余副本并收紧位置记录。

    仅针对 ``storage_kind`` 为分层块、热冷两层均存在、``preferred_tier`` 为热层、
    且 ``last_promoted_ns`` 不晚于当前时刻减去 ``min_age_seconds`` 的条目：尝试删除冷层上的块文件，
    成功则清除该块 ``cold_present``。若冷层路径探测或删除因暂时不可用失败，
    则跳过该项（不计入删除），对应元数据保持不变。

    ``database`` 由 ``resolve_maintenance_paths`` 解析为数据库与冷热层根路径。
    """
    cutoff_ns = time_ns() - min_age_seconds * 1_000_000_000
    paths = resolve_maintenance_paths(database)
    db_path = paths.database
    tier1_path = paths.tier1
    tier2_path = paths.tier2
    removed = 0
    skipped = 0
    pending_removed = 0
    pending_skipped = 0
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
            pending_removed, pending_skipped = _drain_pending_deletions(
                db, tier1_path, tier2_path
            )
            rows = db.execute(
                """
                SELECT blocks.hash
                FROM blocks
                WHERE blocks.storage_kind = 'tiered'
                  AND blocks.hot_present = 1
                  AND blocks.cold_present = 1
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
                    "UPDATE blocks SET cold_present = 0 WHERE hash = ?",
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
    return CleanupReport(
        removed=removed,
        skipped=skipped,
        pending_removed=pending_removed,
        pending_skipped=pending_skipped,
    )


def _drain_pending_deletions(
    db,
    tier1_path: Path,
    tier2_path: Path,
) -> tuple[int, int]:
    """Best-effort drain of physical deletes that were committed before a crash."""
    try:
        rows = db.execute(
            """
            SELECT id, kind, digest, tier
            FROM pending_deletions
            ORDER BY id
            """
        ).fetchall()
    except Exception as exc:
        if "no such table: pending_deletions" in str(exc):
            return 0, 0
        raise

    removed = 0
    skipped = 0
    for row in rows:
        try:
            unlink_path(block_path(tier1_path, tier2_path, row["digest"], row["tier"]))
        except PathUnavailable:
            skipped += 1
            continue
        except OSError:
            logger.exception("维护清理待 GC payload 失败：id={}", row["id"])
            skipped += 1
            continue
        db.execute("DELETE FROM pending_deletions WHERE id = ?", (row["id"],))
        removed += 1
    return removed, skipped
