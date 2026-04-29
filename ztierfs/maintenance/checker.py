"""一致性检查与校验读取：对照元数据验证块文件与内联载荷。"""

import sqlite3

from pathlib import Path
from typing import Any

import compression.zstd as zstd
from loguru import logger

from ztierfs.metadata import BLOCK_RECORD_SELECT
from ztierfs.metadata import open_database
from ztierfs.tier_access import (
    PathMissing,
    PathUnavailable,
    is_temporary_unavailable_error,
    probe_path,
    read_path_bytes,
    unlink_path,
)

from .config import resolve_maintenance_paths
from .paths import block_path
from .reports import CheckReport, Issue


def run_fsck(
    path: str | Path,
    tier2: str | Path | None = None,
    database: str | Path | None = None,
    *,
    repair: bool = False,
    allow_config_mismatch: bool = False,
    update_config: bool = False,
) -> CheckReport:
    return Checker(
        path,
        tier2,
        database,
        repair=repair,
        scrub=False,
        allow_config_mismatch=allow_config_mismatch,
        update_config=update_config,
    ).run()


def run_scrub(
    path: str | Path,
    tier2: str | Path | None = None,
    database: str | Path | None = None,
    *,
    repair: bool = False,
    allow_config_mismatch: bool = False,
    update_config: bool = False,
) -> CheckReport:
    return Checker(
        path,
        tier2,
        database,
        repair=repair,
        scrub=True,
        allow_config_mismatch=allow_config_mismatch,
        update_config=update_config,
    ).run()


class Checker:
    """打开配置与数据库，执行结构检查、可选修复与按块的 scrub。"""

    def __init__(
        self,
        path: str | Path,
        tier2: str | Path | None,
        database: str | Path | None,
        *,
        repair: bool,
        scrub: bool,
        allow_config_mismatch: bool,
        update_config: bool,
    ):
        paths = resolve_maintenance_paths(
            path,
            tier2,
            database,
            allow_config_mismatch=allow_config_mismatch,
            update_config=update_config,
        )
        self.tier1 = paths.tier1
        self.tier2 = paths.tier2
        self.database = paths.database
        self.payload_store_path = paths.payload_store_path
        self.repair = repair
        self.scrub = scrub
        self.issues: list[Issue] = []

    def run(self) -> CheckReport:
        command = "scrub" if self.scrub else "fsck"
        logger.info(
            "开始维护检查：command={}，database={}，tier1={}，tier2={}，repair={}",
            command,
            self.database,
            self.tier1,
            self.tier2,
            self.repair,
        )
        with open_database(self.database) as db:
            db.execute("BEGIN IMMEDIATE" if self.repair else "BEGIN")
            try:
                blocks = {
                    row["hash"]: row
                    for row in db.execute(BLOCK_RECORD_SELECT).fetchall()
                }
                actual_refs = {
                    row["hash"]: row["count"]
                    for row in db.execute(
                        "SELECT hash, COUNT(*) AS count FROM file_chunks GROUP BY hash"
                    ).fetchall()
                }
                disk_blocks = self._scan_disk_blocks()
                logger.debug(
                    "维护检查扫描完成：blocks={}，chunk_refs={}，disk_blocks={}",
                    len(blocks),
                    len(actual_refs),
                    len(disk_blocks),
                )

                self._check_block_records(db, blocks, actual_refs)
                self._check_inline_payload_records(db, has_block_records=bool(blocks))
                self._check_disk_orphans(db, blocks, disk_blocks)
                self._check_chunk_metadata(db)
                self._check_nlinks(db)
                if self.scrub:
                    self._scrub_block_payloads(db, blocks)
            except Exception:
                logger.exception("维护检查失败，回滚事务：command={}", command)
                db.execute("ROLLBACK")
                raise
            else:
                db.execute("COMMIT")
        logger.info(
            "维护检查完成：command={}，issues={}，repaired={}，unrepaired={}",
            command,
            len(self.issues),
            sum(1 for issue in self.issues if issue.repaired),
            sum(1 for issue in self.issues if not issue.repaired),
        )
        return CheckReport(command, self.issues)

    def _check_block_records(
        self,
        db: sqlite3.Connection,
        blocks: dict[str, sqlite3.Row],
        actual_refs: dict[str, int],
    ) -> None:
        for digest, row in blocks.items():
            actual = actual_refs.get(digest, 0)
            hot_probe = probe_path(block_path(self.tier1, self.tier2, digest, 1))
            cold_probe = probe_path(block_path(self.tier1, self.tier2, digest, 2))
            hot_exists = hot_probe.present
            cold_exists = cold_probe.present
            cold_unavailable = cold_probe.unavailable and (
                bool(row["cold_present"]) or not hot_exists
            )
            if row["refcount"] != actual:
                self._issue(
                    "refcount_mismatch",
                    "block refcount does not match file_chunks references",
                    {
                        "hash": digest,
                        "stored_refcount": row["refcount"],
                        "actual_refcount": actual,
                    },
                    repairable=True,
                    repair=lambda digest=digest, actual=actual: db.execute(
                        "UPDATE blocks SET refcount = ? WHERE hash = ?",
                        (actual, digest),
                    ),
                )

            if actual == 0:
                self._issue(
                    "unreferenced_block_record",
                    "block metadata is not referenced by any file chunk",
                    {"hash": digest, "cold_unavailable": cold_unavailable},
                    repairable=not cold_unavailable,
                    repair=(
                        lambda digest=digest: self._delete_block_record_and_files(
                            db, digest
                        )
                    )
                    if not cold_unavailable
                    else None,
                )
            elif row["storage"] == "inline":
                if (
                    self._inline_payload_bytes(
                        row, issue_context={"hash": digest, "refcount": actual}
                    )
                    is None
                ):
                    self._issue(
                        "missing_inline_payload",
                        "referenced inline block metadata has no inline payload",
                        {"hash": digest, "refcount": actual},
                    )
            elif cold_unavailable:
                self._issue(
                    "block_payload_unavailable",
                    "referenced cold block payload cannot be verified right now",
                    {
                        "hash": digest,
                        "tier": 2,
                        "refcount": actual,
                        "error": str(cold_probe.error),
                    },
                )
                if bool(row["hot_present"]) != hot_exists:
                    self._issue(
                        "block_presence_mismatch",
                        "hot block metadata presence does not match block files on disk",
                        {
                            "hash": digest,
                            "stored_hot": bool(row["hot_present"]),
                            "actual_hot": hot_exists,
                            "cold_unavailable": True,
                        },
                        repairable=True,
                        repair=lambda digest=digest, hot=hot_exists: (
                            self._repair_hot_presence(db, digest, hot)
                        ),
                    )
            elif not hot_exists and not cold_exists:
                self._issue(
                    "missing_block_file",
                    "referenced block metadata has no block file in either tier",
                    {"hash": digest, "refcount": actual},
                )
            else:
                stored_hot = bool(row["hot_present"])
                stored_cold = bool(row["cold_present"])
                if stored_hot != hot_exists or stored_cold != cold_exists:
                    preferred = 1 if hot_exists else 2
                    if row["preferred_tier"] == 2 and cold_exists:
                        preferred = 2
                    if preferred == 2 and not cold_exists:
                        preferred = 1
                    self._issue(
                        "block_presence_mismatch",
                        "block metadata presence does not match block files on disk",
                        {
                            "hash": digest,
                            "stored_hot": stored_hot,
                            "stored_cold": stored_cold,
                            "actual_hot": hot_exists,
                            "actual_cold": cold_exists,
                        },
                        repairable=True,
                        repair=lambda digest=digest, hot=hot_exists, cold=cold_exists, preferred=preferred: (
                            self._repair_block_presence(
                                db, digest, hot, cold, preferred
                            )
                        ),
                    )

                preferred_exists = (
                    hot_exists if row["preferred_tier"] == 1 else cold_exists
                )
                if not preferred_exists:
                    actual_tier = 1 if hot_exists else 2
                    self._issue(
                        "preferred_tier_missing",
                        "block preferred tier points to a missing copy",
                        {
                            "hash": digest,
                            "stored_preferred_tier": row["preferred_tier"],
                            "actual_tier": actual_tier,
                        },
                        repairable=True,
                        repair=lambda digest=digest, actual_tier=actual_tier: (
                            db.execute(
                                "UPDATE blocks SET preferred_tier = ? WHERE hash = ?",
                                (actual_tier, digest),
                            )
                        ),
                    )

            if (
                actual != 0
                and row["storage"] == "tiered"
                and not row["hot_present"]
                and not row["cold_present"]
            ):
                self._issue(
                    "missing_block_presence",
                    "referenced block metadata does not declare any storage tier",
                    {"hash": digest},
                    repairable=True,
                    repair=lambda digest=digest, hot=hot_exists, cold=cold_exists: (
                        self._repair_block_presence(
                            db, digest, hot, cold, 1 if hot else 2
                        )
                    ),
                )

    def _check_inline_payload_records(
        self, db: sqlite3.Connection, *, has_block_records: bool
    ) -> None:
        for row in db.execute(
            """
            SELECT inode_payloads.inode_id, inodes.kind
            FROM inode_payloads
            LEFT JOIN inodes ON inodes.id = inode_payloads.inode_id
            """
        ).fetchall():
            if row["kind"] is None:
                self._issue(
                    "orphan_inode_payload",
                    "inode inline payload exists without inode metadata",
                    {"inode": row["inode_id"]},
                    repairable=True,
                    repair=lambda inode=row["inode_id"]: db.execute(
                        "DELETE FROM inode_payloads WHERE inode_id = ?",
                        (inode,),
                    ),
                )
            elif row["kind"] != "file":
                self._issue(
                    "unexpected_inode_payload",
                    "non-file inode has an inline payload",
                    {"inode": row["inode_id"], "kind": row["kind"]},
                    repairable=True,
                    repair=lambda inode=row["inode_id"]: db.execute(
                        "DELETE FROM inode_payloads WHERE inode_id = ?",
                        (inode,),
                    ),
                )

        if not has_block_records:
            for row in db.execute(
                """
                SELECT inodes.id, inodes.size
                FROM inodes
                LEFT JOIN inode_payloads ON inode_payloads.inode_id = inodes.id
                WHERE inodes.kind = 'file'
                  AND inodes.size > 0
                  AND inode_payloads.inode_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM file_chunks WHERE file_chunks.file_id = inodes.id
                  )
                """
            ).fetchall():
                self._issue(
                    "missing_inode_payload",
                    "non-empty file has neither chunk metadata nor inline payload",
                    {"inode": row["id"], "size": row["size"]},
                )

        for row in db.execute(
            """
            SELECT block_payloads.hash, blocks.storage_kind AS storage
            FROM block_payloads
            LEFT JOIN blocks ON blocks.hash = block_payloads.hash
            """
        ).fetchall():
            if row["storage"] is None:
                self._issue(
                    "orphan_inline_payload",
                    "inline payload exists without block metadata",
                    {"hash": row["hash"]},
                    repairable=True,
                    repair=lambda digest=row["hash"]: db.execute(
                        "DELETE FROM block_payloads WHERE hash = ?",
                        (digest,),
                    ),
                )
            elif row["storage"] != "inline":
                self._issue(
                    "unexpected_inline_payload",
                    "tiered block metadata has an inline payload",
                    {"hash": row["hash"], "storage": row["storage"]},
                    repairable=True,
                    repair=lambda digest=row["hash"]: db.execute(
                        "DELETE FROM block_payloads WHERE hash = ?",
                        (digest,),
                    ),
                )

    def _check_disk_orphans(
        self,
        db: sqlite3.Connection,
        blocks: dict[str, sqlite3.Row],
        disk_blocks: dict[str, set[int]],
    ) -> None:
        for digest, tiers in sorted(disk_blocks.items()):
            if digest in blocks and blocks[digest]["storage"] == "tiered":
                continue
            self._issue(
                "orphan_block_file",
                "block file exists on disk without block metadata",
                {"hash": digest, "tiers": sorted(tiers)},
                repairable=True,
                repair=lambda digest=digest, tiers=tuple(tiers): (
                    self._delete_disk_block_files(digest, tiers)
                ),
            )

    def _check_chunk_metadata(self, db: sqlite3.Connection) -> None:
        for row in db.execute(
            """
            SELECT file_chunks.file_id, file_chunks.chunk_index, file_chunks.hash
            FROM file_chunks
            LEFT JOIN blocks ON blocks.hash = file_chunks.hash
            WHERE blocks.hash IS NULL
            """
        ).fetchall():
            self._issue(
                "chunk_missing_block_metadata",
                "file chunk points to missing block metadata",
                dict(row),
            )

        for row in db.execute(
            """
            SELECT file_chunks.file_id, file_chunks.chunk_index, file_chunks.hash
            FROM file_chunks
            LEFT JOIN inodes ON inodes.id = file_chunks.file_id
            WHERE inodes.id IS NULL
            """
        ).fetchall():
            self._issue(
                "chunk_missing_inode", "file chunk points to missing inode", dict(row)
            )

        for row in db.execute(
            """
            SELECT file_chunks.file_id, file_chunks.chunk_index, file_chunks.hash, file_chunks.size, blocks.raw_size
            FROM file_chunks
            JOIN blocks ON blocks.hash = file_chunks.hash
            WHERE file_chunks.size != blocks.raw_size
            """
        ).fetchall():
            self._issue(
                "chunk_size_mismatch",
                "file chunk size does not match block raw size",
                dict(row),
            )

        for row in db.execute(
            """
            SELECT file_chunks.file_id, file_chunks.chunk_index, inodes.kind
            FROM file_chunks
            JOIN inodes ON inodes.id = file_chunks.file_id
            WHERE inodes.kind != 'file'
            """
        ).fetchall():
            self._issue(
                "chunk_for_non_file_inode",
                "file chunk points to a non-file inode",
                dict(row),
            )

        for row in db.execute(
            """
            SELECT dir_entries.parent_id, dir_entries.name, dir_entries.inode_id
            FROM dir_entries
            LEFT JOIN inodes AS parent ON parent.id = dir_entries.parent_id
            LEFT JOIN inodes AS child ON child.id = dir_entries.inode_id
            WHERE parent.id IS NULL OR child.id IS NULL OR parent.kind != 'dir'
            """
        ).fetchall():
            self._issue(
                "invalid_dir_entry",
                "directory entry points to missing or invalid inode metadata",
                dict(row),
            )

    def _check_nlinks(self, db: sqlite3.Connection) -> None:
        rows = db.execute(
            """
            SELECT inodes.id, inodes.kind, inodes.nlink, COUNT(dir_entries.inode_id) AS actual
            FROM inodes
            LEFT JOIN dir_entries ON dir_entries.inode_id = inodes.id
            GROUP BY inodes.id
            """
        ).fetchall()
        for row in rows:
            expected = 1 if row["id"] == 1 else row["actual"]
            if row["nlink"] != expected:
                self._issue(
                    "nlink_mismatch",
                    "inode nlink does not match directory entries",
                    {
                        "inode": row["id"],
                        "kind": row["kind"],
                        "stored_nlink": row["nlink"],
                        "actual_nlink": expected,
                    },
                    repairable=True,
                    repair=lambda inode=row["id"], expected=expected: db.execute(
                        "UPDATE inodes SET nlink = ? WHERE id = ?",
                        (expected, inode),
                    ),
                )

    def _scrub_block_payloads(
        self, db: sqlite3.Connection, blocks: dict[str, sqlite3.Row]
    ) -> None:
        for row in db.execute(
            """
            SELECT inodes.id, inodes.size, inode_payloads.payload,
                   inode_payloads.payload_store, inode_payloads.payload_key,
                   inode_payloads.compressed, inode_payloads.raw_size,
                   inode_payloads.stored_size
            FROM inode_payloads
            JOIN inodes ON inodes.id = inode_payloads.inode_id
            """
        ).fetchall():
            payload = self._inline_payload_bytes(
                row, issue_context={"inode": row["id"]}
            )
            if payload is None:
                self._issue(
                    "missing_inode_payload",
                    "inode inline payload cannot be read",
                    {"inode": row["id"]},
                )
                continue
            if len(payload) != row["stored_size"]:
                self._issue(
                    "inode_payload_stored_size_mismatch",
                    "inode inline payload size does not match metadata stored_size",
                    {
                        "inode": row["id"],
                        "expected": row["stored_size"],
                        "actual": len(payload),
                    },
                )
                continue
            try:
                data = zstd.decompress(payload) if row["compressed"] else payload
            except zstd.ZstdError as exc:
                self._issue(
                    "corrupt_inode_payload",
                    "inode inline payload cannot be decoded",
                    {"inode": row["id"], "error": str(exc)},
                )
                continue
            if len(data) != row["raw_size"] or len(data) != row["size"]:
                self._issue(
                    "inode_payload_raw_size_mismatch",
                    "inode inline payload raw size does not match metadata",
                    {
                        "inode": row["id"],
                        "expected": row["size"],
                        "actual": len(data),
                    },
                )

        for digest, row in blocks.items():
            if row["storage"] == "inline":
                payload = self._inline_payload_bytes(
                    row, issue_context={"hash": digest}
                )
                if payload is None:
                    self._issue(
                        "missing_inline_payload",
                        "inline block metadata has no inline payload",
                        {"hash": digest},
                    )
                    continue
                self._scrub_payload(digest, row, payload, {"storage": "inline"})
                continue

            paths = [
                (tier, block_path(self.tier1, self.tier2, digest, tier))
                for tier in (1, 2)
                if self._payload_path_available(digest, tier)
            ]
            for tier, path in paths:
                try:
                    payload = read_path_bytes(path)
                    self._scrub_payload(digest, row, payload, {"tier": tier})
                except PathMissing:
                    self._issue(
                        "missing_block_file",
                        "referenced block payload disappeared before scrub",
                        {"hash": digest, "tier": tier},
                    )
                except PathUnavailable as exc:
                    self._issue(
                        "block_payload_unavailable",
                        "block payload cannot be read right now",
                        {"hash": digest, "tier": tier, "error": str(exc)},
                    )
                except (OSError, zstd.ZstdError) as exc:
                    self._issue(
                        "corrupt_block_payload",
                        "block payload cannot be read or decoded",
                        {"hash": digest, "tier": tier, "error": str(exc)},
                    )

    def _payload_path_available(self, digest: str, tier: int) -> bool:
        path = block_path(self.tier1, self.tier2, digest, tier)
        probe = probe_path(path)
        if probe.unavailable:
            self._issue(
                "block_payload_unavailable",
                "block payload cannot be verified right now",
                {"hash": digest, "tier": tier, "error": str(probe.error)},
            )
            return False
        return probe.present

    def _inline_payload_bytes(
        self, row: sqlite3.Row, *, issue_context: dict[str, Any]
    ) -> bytes | None:
        store = (
            row["payload_store"]
            if "payload_store" in row.keys()
            else row["inline_payload_store"]
        )
        payload = row["payload"] if "payload" in row.keys() else row["inline_payload"]
        key = (
            row["payload_key"]
            if "payload_key" in row.keys()
            else row["inline_payload_key"]
        )
        if store == "sqlite":
            return bytes(payload) if payload is not None else None
        if key is None:
            return None
        path = self._external_payload_path(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            self._issue(
                "payload_store_unavailable",
                "external inline payload cannot be read",
                {
                    **issue_context,
                    "payload_store": store,
                    "payload_key": key,
                    "error": str(exc),
                },
            )
            return None

    def _external_payload_path(self, key: str) -> Path:
        safe = key.replace("/", "_")
        root = self.payload_store_path or self.tier1 / "payload-kv"
        return root / safe[:2] / safe[2:4] / safe

    def _scrub_payload(
        self,
        digest: str,
        row: sqlite3.Row,
        payload: bytes,
        location: dict[str, Any],
    ) -> None:
        if len(payload) != row["stored_size"]:
            self._issue(
                "stored_size_mismatch",
                "block payload size does not match metadata stored_size",
                {
                    "hash": digest,
                    **location,
                    "metadata_size": row["stored_size"],
                    "payload_size": len(payload),
                },
            )
        try:
            data = zstd.decompress(payload) if row["compressed"] else payload
        except zstd.ZstdError as exc:
            self._issue(
                "corrupt_block_payload",
                "block payload cannot be decoded",
                {"hash": digest, **location, "error": str(exc)},
            )
            return
        if len(data) != row["raw_size"]:
            self._issue(
                "raw_size_mismatch",
                "decoded block size does not match metadata raw_size",
                {
                    "hash": digest,
                    **location,
                    "metadata_size": row["raw_size"],
                    "decoded_size": len(data),
                },
            )

    def _scan_disk_blocks(self) -> dict[str, set[int]]:
        found: dict[str, set[int]] = {}
        for tier, root in ((1, self.tier1 / "blocks"), (2, self.tier2 / "blocks")):
            root_probe = probe_path(root)
            if root_probe.missing:
                continue
            if root_probe.unavailable:
                self._issue(
                    "cold_tier_unavailable" if tier == 2 else "hot_tier_unavailable",
                    "block tier cannot be scanned right now",
                    {"tier": tier, "path": str(root), "error": str(root_probe.error)},
                )
                continue
            try:
                for path in root.rglob("*"):
                    try:
                        if not path.is_file() or path.name.startswith("."):
                            continue
                    except OSError as exc:
                        if is_temporary_unavailable_error(exc):
                            self._issue(
                                "cold_tier_unavailable"
                                if tier == 2
                                else "hot_tier_unavailable",
                                "block tier cannot be scanned completely right now",
                                {"tier": tier, "path": str(root), "error": str(exc)},
                            )
                            break
                        raise
                    digest = path.name
                    if len(digest) != 64:
                        continue
                    found.setdefault(digest, set()).add(tier)
            except OSError as exc:
                if is_temporary_unavailable_error(exc):
                    self._issue(
                        "cold_tier_unavailable"
                        if tier == 2
                        else "hot_tier_unavailable",
                        "block tier cannot be scanned right now",
                        {"tier": tier, "path": str(root), "error": str(exc)},
                    )
                    continue
                raise
        logger.debug("扫描块文件完成：count={}", len(found))
        return found

    def _issue(
        self,
        code: str,
        message: str,
        details: dict[str, Any],
        *,
        repairable: bool = False,
        repair: Any = None,
    ) -> None:
        issue = Issue(code, message, details, repairable)
        if self.repair and repairable and repair is not None:
            repair()
            issue.repaired = True
            logger.info("维护检查问题已修复：code={}，details={}", code, details)
        else:
            logger.warning(
                "维护检查发现问题：code={}，repairable={}，details={}",
                code,
                repairable,
                details,
            )
        self.issues.append(issue)

    def _delete_block_record_and_files(
        self, db: sqlite3.Connection, digest: str
    ) -> None:
        logger.info("删除无引用块记录和块文件：hash={}", digest[:12])
        db.execute("DELETE FROM blocks WHERE hash = ?", (digest,))
        self._delete_disk_block_files(digest, (1, 2))

    def _repair_block_presence(
        self,
        db: sqlite3.Connection,
        digest: str,
        hot: bool,
        cold: bool,
        preferred_tier: int,
    ) -> None:
        logger.info(
            "修复块位置元数据：hash={}，hot={}，cold={}，preferred_tier={}",
            digest[:12],
            hot,
            cold,
            preferred_tier,
        )
        for tier, present in ((1, hot), (2, cold)):
            if present:
                db.execute(
                    "INSERT OR IGNORE INTO block_locations (hash, tier) VALUES (?, ?)",
                    (digest, tier),
                )
            else:
                db.execute(
                    "DELETE FROM block_locations WHERE hash = ? AND tier = ?",
                    (digest, tier),
                )
        db.execute(
            "UPDATE blocks SET preferred_tier = ? WHERE hash = ?",
            (preferred_tier, digest),
        )

    def _repair_hot_presence(
        self,
        db: sqlite3.Connection,
        digest: str,
        hot: bool,
    ) -> None:
        logger.info("修复热层块位置元数据：hash={}，hot={}", digest[:12], hot)
        if hot:
            db.execute(
                "INSERT OR IGNORE INTO block_locations (hash, tier) VALUES (?, 1)",
                (digest,),
            )
        else:
            db.execute(
                "DELETE FROM block_locations WHERE hash = ? AND tier = 1",
                (digest,),
            )

    def _delete_disk_block_files(self, digest: str, tiers: tuple[int, ...]) -> None:
        for tier in tiers:
            self._unlink_block_file(digest, tier)

    def _unlink_block_file(self, digest: str, tier: int) -> None:
        try:
            path = block_path(self.tier1, self.tier2, digest, tier)
            probe = probe_path(path)
            if probe.unavailable:
                self._issue(
                    "block_payload_unavailable",
                    "block file cannot be deleted right now",
                    {"hash": digest, "tier": tier, "error": str(probe.error)},
                )
                return
            if probe.missing:
                return
            if unlink_path(path):
                logger.info("删除孤儿块文件：hash={}，tier={}", digest[:12], tier)
        except PathUnavailable as exc:
            self._issue(
                "block_payload_unavailable",
                "block file cannot be deleted right now",
                {"hash": digest, "tier": tier, "error": str(exc)},
            )
