"""copy-up 后冷层副本与无引用冷层垃圾的显式维护清理。"""

from contextlib import closing
from pathlib import Path
from dataclasses import dataclass
from time import time_ns

from loguru import logger

from ztierfs.constants import DEFAULT_COLD_GC_AGE_SECONDS
from ztierfs.metadata import open_database
from ztierfs.pending_deletions import drain_pending_block_files
from ztierfs.tier_access import PathUnavailable, probe_path, unlink_path

from .config import resolve_maintenance_paths
from .paths import block_path


@dataclass(frozen=True)
class CleanupReport:
    """cleanup 命令的计数汇总。

    ``removed``：已成功删除冷层块文件，并清除 ``blocks.cold_present`` 的数量。
    ``skipped``：因冷层路径暂时不可用或删除失败而跳过、未改对应元数据的数量。
    """

    removed: int = 0
    skipped: int = 0
    pending_removed: int = 0
    pending_skipped: int = 0
    pending_unavailable: int = 0
    cold_garbage_candidates: int = 0
    removed_cold_garbage: int = 0
    skipped_cold_unavailable: int = 0
    reclaimed_cold_bytes: int = 0
    remaining_cold_garbage: int = 0


def cleanup_promoted_cold_copies(
    database: str | Path,
    *,
    min_age_seconds: int,
    cold_gc_age_seconds: int | None = DEFAULT_COLD_GC_AGE_SECONDS,
    max_cold_deletes: int | None = None,
    dry_run: bool = False,
) -> CleanupReport:
    """在块已 copy-up 到热层且元数据首选热层后，按「提升」龄删除冷层上冗余副本并收紧位置记录。

    仅针对 ``storage_kind`` 为分层块、热冷两层均存在、``preferred_tier`` 为热层、
    且 ``last_promoted_ns`` 不晚于当前时刻减去 ``min_age_seconds`` 的条目：尝试删除冷层上的块文件，
    成功则清除该块 ``cold_present``。若冷层路径探测或删除因暂时不可用失败，
    则跳过该项（不计入删除），对应元数据保持不变。

    ``database`` 由 ``resolve_maintenance_paths`` 解析为数据库与冷热层根路径。
    """
    promoted_cutoff_ns = time_ns() - min_age_seconds * 1_000_000_000
    cold_gc_cutoff_ns = (
        time_ns() - cold_gc_age_seconds * 1_000_000_000
        if cold_gc_age_seconds is not None
        else None
    )
    paths = resolve_maintenance_paths(database)
    db_path = paths.database
    tier1_path = paths.tier1
    tier2_path = paths.tier2
    removed = 0
    skipped = 0
    pending_removed = 0
    pending_skipped = 0
    pending_unavailable = 0
    cold_garbage_candidates = 0
    removed_cold_garbage = 0
    skipped_cold_unavailable = 0
    reclaimed_cold_bytes = 0
    logger.info(
        "开始维护清理冷层副本：database={}，tier1={}，tier2={}，min_age_seconds={}，cold_gc_age_seconds={}，max_cold_deletes={}，dry_run={}",
        db_path,
        tier1_path,
        tier2_path,
        min_age_seconds,
        cold_gc_age_seconds,
        max_cold_deletes,
        dry_run,
    )
    with closing(open_database(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not dry_run:
                pending_removed, pending_skipped, pending_unavailable = (
                    _drain_pending_deletions(db, tier1_path, tier2_path, tier=1)
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
                (promoted_cutoff_ns,),
            ).fetchall()
            for row in rows:
                if dry_run:
                    continue
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
            if cold_gc_cutoff_ns is not None:
                (
                    cold_garbage_candidates,
                    removed_cold_garbage,
                    skipped_cold_unavailable,
                    reclaimed_cold_bytes,
                ) = _cleanup_cold_garbage(
                    db,
                    tier1_path,
                    tier2_path,
                    cutoff_ns=cold_gc_cutoff_ns,
                    max_deletes=max_cold_deletes,
                    dry_run=dry_run,
                )
        except Exception:
            logger.exception("维护清理失败，回滚事务")
            db.execute("ROLLBACK")
            raise
        else:
            db.execute("COMMIT")
        remaining_cold_garbage = _cold_garbage_count(db)
    logger.info(
        "维护清理完成：removed={}，skipped={}，removed_cold_garbage={}，skipped_cold_unavailable={}",
        removed,
        skipped,
        removed_cold_garbage,
        skipped_cold_unavailable,
    )
    return CleanupReport(
        removed=removed,
        skipped=skipped,
        pending_removed=pending_removed,
        pending_skipped=pending_skipped,
        pending_unavailable=pending_unavailable,
        cold_garbage_candidates=cold_garbage_candidates,
        removed_cold_garbage=removed_cold_garbage,
        skipped_cold_unavailable=skipped_cold_unavailable,
        reclaimed_cold_bytes=reclaimed_cold_bytes,
        remaining_cold_garbage=remaining_cold_garbage,
    )


def _drain_pending_deletions(
    db,
    tier1_path: Path,
    tier2_path: Path,
    *,
    tier: int | None = None,
) -> tuple[int, int, int]:
    """Best-effort drain of physical deletes that were committed before a crash."""
    tier_filter = "" if tier is None else "WHERE tier = ?"
    params = () if tier is None else (tier,)
    try:
        rows = db.execute(
            f"""
            SELECT id, digest, tier
            FROM pending_deletions
            {tier_filter}
            ORDER BY id
            """,
            params,
        ).fetchall()
    except Exception as exc:
        if "no such table: pending_deletions" in str(exc):
            return 0, 0, 0
        raise

    outcome = drain_pending_block_files(
        rows,
        lambda digest, row_tier: block_path(tier1_path, tier2_path, digest, row_tier),
    )
    db.executemany(
        "DELETE FROM pending_deletions WHERE id = ?",
        [(deletion_id,) for deletion_id in outcome.removed_ids],
    )
    return (
        len(outcome.removed_ids),
        len(outcome.deferred_ids),
        len(outcome.unavailable_ids),
    )


def _cleanup_cold_garbage(
    db,
    tier1_path: Path,
    tier2_path: Path,
    *,
    cutoff_ns: int,
    max_deletes: int | None,
    dry_run: bool,
) -> tuple[int, int, int, int]:
    rows = db.execute(
        """
        SELECT hash, stored_size
        FROM blocks
        WHERE storage_kind = 'tiered'
          AND refcount = 0
          AND cold_present = 1
          AND cold_gc_enqueued_ns IS NOT NULL
          AND cold_gc_enqueued_ns <= ?
        ORDER BY cold_gc_enqueued_ns ASC, hash ASC
        """,
        (cutoff_ns,),
    ).fetchall()
    candidates = len(rows)
    if max_deletes is not None:
        rows = rows[:max_deletes]
    if dry_run:
        return candidates, 0, 0, 0

    removed = 0
    skipped_unavailable = 0
    reclaimed_bytes = 0
    for row in rows:
        path = block_path(tier1_path, tier2_path, row["hash"], 2)
        probe = probe_path(path)
        if probe.unavailable:
            skipped_unavailable += 1
            logger.warning(
                "维护清理跳过冷层垃圾：冷层临时不可用，hash={}，error={}",
                row["hash"][:12],
                probe.error,
            )
            continue
        try:
            if probe.present:
                unlink_path(path)
        except PathUnavailable as exc:
            skipped_unavailable += 1
            logger.warning(
                "维护清理跳过冷层垃圾：删除时冷层临时不可用，hash={}，error={}",
                row["hash"][:12],
                exc,
            )
            continue
        db.execute("DELETE FROM blocks WHERE hash = ?", (row["hash"],))
        removed += 1
        reclaimed_bytes += int(row["stored_size"])
        logger.debug("维护清理冷层垃圾：hash={}", row["hash"][:12])
    return candidates, removed, skipped_unavailable, reclaimed_bytes


def _cold_garbage_count(db) -> int:
    return int(
        db.execute(
            """
            SELECT COUNT(*)
            FROM blocks
            WHERE storage_kind = 'tiered'
              AND refcount = 0
              AND cold_present = 1
            """
        ).fetchone()[0]
    )
