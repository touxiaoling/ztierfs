"""维护侧一致性检查：用 SQLite 元数据对照磁盘上的块文件与内联载荷。

**fsck（`run_fsck`）**：在元数据与路径探测层面核对引用计数、块记录与 `file_chunks`、
冷热层上块文件是否存在、`blocks` 与 `block_locations` 是否一致、目录项与 chunk
关系等；不读取整块 payload 做解压与逐字节校验（内联块仅检查是否有对应行/可读路径）。

**scrub（`run_scrub`）**：在 fsck 相同流程之后，对 inode 内联数据、内联块和热层块文件执行读盘、
按需 zstd 解压，并核对 `stored_size`/`raw_size` 等。冷层块文件默认不读取；需要完整读取冷层时
显式传 `include_cold=True`。

**修复（`repair=True`）**：以 `BEGIN IMMEDIATE` 开启写事务，仅对标记为可修复的问题
执行回调 SQL/删文件；冷层路径**暂时不可用**（`PathUnavailable`、探测为
`unavailable`）时，不把「无法核实」当成「缺失」去删无引用块等破坏性操作；真正路径
不存在（`PathMissing`）才按缺失处理。
"""

import sqlite3

from contextlib import closing
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
    """执行 fsck：元数据与存在性/引用一致性检查，不启用逐块内容校验（`scrub=False`）。"""
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
    include_cold: bool = False,
) -> CheckReport:
    """执行 scrub：在 fsck 基础上对内联与热层 payload 做读盘与解压后的尺寸校验；`include_cold` 为真时也读取冷层。"""
    return Checker(
        path,
        tier2,
        database,
        repair=repair,
        scrub=True,
        allow_config_mismatch=allow_config_mismatch,
        update_config=update_config,
        include_cold=include_cold,
    ).run()


class Checker:
    """根据热/冷层与数据库路径解析配置，打开 SQLite，顺序执行各项检查并汇总 `Issue`。

    `scrub` 为真时，`run` 在常规 fsck 步骤之后额外调用 `_scrub_block_payloads`。
    """

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
        include_cold: bool = False,
    ):
        """保存 tier1/tier2、数据库，以及是否修复、是否 scrub / cold scrub 的标志。"""
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
        self.repair = repair
        self.scrub = scrub
        self.include_cold = include_cold
        self.issues: list[Issue] = []

    def run(self) -> CheckReport:
        """开启事务（修复模式下 `BEGIN IMMEDIATE`），加载块表与 chunk 引用、扫描磁盘块目录，依次执行块记录、内联载荷、磁盘孤儿、chunk/目录项、`nlink` 检查；若启用 scrub 则校验 payload 内容；提交或出错回滚后返回 `CheckReport`。"""
        command = "scrub" if self.scrub else "fsck"
        logger.info(
            "开始维护检查：command={}，database={}，tier1={}，tier2={}，repair={}",
            command,
            self.database,
            self.tier1,
            self.tier2,
            self.repair,
        )
        with closing(open_database(self.database)) as db:
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
        """核对 `blocks` 各行：与 `file_chunks` 聚合引用计数是否一致；探测冷热层块文件是否存在。

        区分冷层**暂时不可用**（探测 `unavailable` 且元数据认为应有冷副本或热层缺失）与
        双 tier 均**确实缺失**文件：前者报 `block_payload_unavailable` 且不对无引用块执行
        可修复删除；后者报 `missing_block_file`。另覆盖内联块缺行、`hot_present`/`cold_present`
        与磁盘不符、`preferred_tier` 指向不存在副本、以及 tiered 却未声明任一层等情形。
        """
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
        """检查 `inode_payloads` 与 inode 类型一致性、`block_payloads` 与 `blocks.storage` 一致性。

        当库中尚无任一块元数据时，额外查找「size>0 却无 chunk 且无内联」的文件 inode，
        报告 `missing_inode_payload`。
        """
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
        """扫描磁盘上存在、但元数据中不存在（或非 tiered 块）的块文件，报告为孤儿块文件。"""
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
        """校验 `file_chunks` 指向的块与 inode 是否存在、chunk 尺寸与 `blocks.raw_size` 是否一致、chunk 是否挂在非文件 inode 上，并检查 `dir_entries` 父子 inode 与目录类型。"""
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
        """将每个 inode 的 `nlink` 与目录项中实际硬链接计数对比（根 inode 特例为 1）。"""
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
        """scrub 阶段：读取 inode 内联与块级内联/分层文件中的 payload，校验压缩与 `stored_size`/`raw_size`/`size`。

        读盘时区分路径缺失（`PathMissing`）与暂时不可读（`PathUnavailable`）；后者记入
        `block_payload_unavailable` 而非当作内容损坏。冷层块默认跳过，除非 `include_cold` 为真。
        """
        for row in db.execute(
            """
            SELECT inodes.id, inodes.size, inode_payloads.payload,
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
                if tier == 1 or self.include_cold
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
        """探测某层块路径是否可访问：暂时不可用则记录 issue 并返回 False；存在则返回 True。"""
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
        """从内联行读取 SQLite payload。"""
        payload = row["payload"] if "payload" in row.keys() else row["inline_payload"]
        return bytes(payload) if payload is not None else None

    def _scrub_payload(
        self,
        digest: str,
        row: sqlite3.Row,
        payload: bytes,
        location: dict[str, Any],
    ) -> None:
        """对已读入的块字节校验 `stored_size`，解压后校验与 `raw_size` 是否一致。"""
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
        """遍历热/冷层 `blocks` 目录，收集 64 位十六进制文件名的摘要及所在 tier；根目录不可扫描时区分暂时不可用与不存在。"""
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
        """构造 `Issue`：若 `repair` 且 `repairable` 且提供了 `repair` 可调用对象则执行并标记已修复。"""
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
        """删除 `blocks` 中指定摘要的行，并尝试删除冷热层上对应块文件。"""
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
        """用磁盘上 hot/cold 实际存在情况重写 `block_locations`，并更新 `preferred_tier`。"""
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
        """在冷层暂时不可核实场景下，仅同步热层（tier=1）在 `block_locations` 中的存在性。"""
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
        """对给定 tier 列表逐个调用 `_unlink_block_file` 删除块文件。"""
        for tier in tiers:
            self._unlink_block_file(digest, tier)

    def _unlink_block_file(self, digest: str, tier: int) -> None:
        """探测块路径后删除文件：路径暂时不可用报 `block_payload_unavailable`；已缺失则静默跳过。"""
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
