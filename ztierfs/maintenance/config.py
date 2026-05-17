"""维护工具解析「元数据库 + 冷热层根」路径（CLI 与 checker 共用）。

**仅数据库（DB-only）**：位置参数 ``path`` 指向 **SQLite 元数据库文件** 且 **不传**
``tier2``。从库表 ``filesystem_config`` 读取 ``hot_tier_path`` / ``cold_tier_path``；
此时 **禁止** 再传 ``database``，也 **禁止** 使用
``allow_config_mismatch`` / ``update_config``（二者只在显式冷热路径模式下有意义）。

**显式冷热层（tier1 + tier2）**：``path`` 为热层（tier1）根目录，``tier2`` 为冷层根；
``database`` 可选，缺省为 ``<tier1>/ztierfs.sqlite3``（见 ``default_database``）。若库
内已有配置行，会将 CLI 给出的 tier 与库内路径比对；不一致时默认报错，除非传入
``allow_config_mismatch=True``（仍用 CLI 路径继续）或 ``update_config=True``（用 CLI
路径写回库内配置）。
"""

import sqlite3

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ztierfs.metadata import open_database

from .paths import default_database


@dataclass(frozen=True)
class MaintenancePaths:
    """解析后的维护侧路径集合（均为已 ``resolve()`` 的绝对路径）。

    ``database``：打开的 SQLite 元数据库。``tier1`` / ``tier2``：热层、冷层 **根目录**
    （块实际在各自 ``blocks`` 子目录下，由 ``BlockStore`` 约定）。
    """
    database: Path
    tier1: Path
    tier2: Path


def resolve_maintenance_paths(
    path: str | Path,
    tier2: str | Path | None = None,
    database: str | Path | None = None,
    *,
    allow_config_mismatch: bool = False,
    update_config: bool = False,
) -> MaintenancePaths:
    """将 ``path`` / ``tier2`` / ``database`` 解析为 ``MaintenancePaths``。

    **DB-only**（``tier2 is None``）：``path`` 必须为数据库文件；从库读取 tier 与
    若传 ``database`` 或与 mismatch 相关参数则抛 ``ValueError``。

    **显式 tier1+tier2**（``tier2`` 非空）：``path`` 为热层根；``database`` 决定库文件
    位置（默认 ``<tier1>/ztierfs.sqlite3``）。若库中 **无** ``filesystem_config``：
    仅在 ``update_config=True`` 时写入新配置行，否则返回 CLI 给出的 tier，且
    若库中 **有** 配置：``update_config=True`` 时用 CLI tier 覆盖库内路径并提交；
    否则用 ``_path_mismatches`` 比较规范化后的热/冷路径，不一致且未设 ``allow_config_mismatch`` 时抛 ``ValueError``，提示使用
    ``--allow-config-mismatch`` 或 ``--update-config``。
    """
    if tier2 is None:
        if database is not None:
            raise ValueError(
                "--database is redundant when the positional path is already the database"
            )
        if allow_config_mismatch or update_config:
            raise ValueError(
                "--allow-config-mismatch and --update-config require explicit hot/cold tiers"
            )
        db_path = Path(path).resolve()
        config = _read_required_config(db_path)
        return MaintenancePaths(
            database=db_path,
            tier1=Path(config["hot_tier_path"]).resolve(),
            tier2=Path(config["cold_tier_path"]).resolve(),
        )

    tier1_path = Path(path).resolve()
    tier2_path = Path(tier2).resolve()
    db_path = default_database(tier1_path, database)
    config = _read_optional_config(db_path)
    if config is None:
        if update_config:
            _write_config(db_path, tier1_path, tier2_path)
        return MaintenancePaths(
            database=db_path,
            tier1=tier1_path,
            tier2=tier2_path,
        )

    if update_config:
        _write_config(db_path, tier1_path, tier2_path)
        return MaintenancePaths(
            database=db_path,
            tier1=tier1_path,
            tier2=tier2_path,
        )

    mismatches = _path_mismatches(config, tier1_path, tier2_path)
    if mismatches and not allow_config_mismatch:
        details = ", ".join(
            f"{key}: stored={stored!r}, requested={requested!r}"
            for key, stored, requested in mismatches
        )
        raise ValueError(
            "explicit storage paths do not match database config; "
            f"use --allow-config-mismatch or --update-config ({details})"
        )

    return MaintenancePaths(
        database=db_path,
        tier1=tier1_path,
        tier2=tier2_path,
    )


def _read_required_config(db_path: Path) -> sqlite3.Row:
    """从 ``db_path`` 读取 ``filesystem_config``；缺表或无行时抛 ``ValueError``。

    供 **DB-only** 分支使用：必须能从库得到冷热层路径配置。
    """
    config = _read_optional_config(db_path)
    if config is None:
        raise ValueError(
            "metadata database does not contain storage path config; "
            "rerun with explicit <hot-tier> <cold-tier> --database, optionally --update-config"
        )
    return config


def _read_optional_config(db_path: Path) -> sqlite3.Row | None:
    """读取 ``id=1`` 的 ``filesystem_config`` 行；无表或无行时返回 ``None``。

    显式 tier 模式下用于判断库内是否已有存储路径配置，以便走 mismatch / 写回 / 新建
    分支；其它 ``sqlite3.OperationalError`` 仍会向上抛出。
    """
    with closing(open_database(db_path)) as db:
        try:
            return db.execute(
                """
                SELECT hot_tier_path, cold_tier_path
                FROM filesystem_config
                WHERE id = 1
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: filesystem_config" in str(exc):
                return None
            raise


def _write_config(
    db_path: Path,
    tier1: Path,
    tier2: Path,
) -> None:
    """在 ``IMMEDIATE`` 事务中创建（若不存在）``filesystem_config`` 并 UPSERT 第 1 行。

    失败时回滚。
    """
    with closing(open_database(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS filesystem_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    config_version INTEGER NOT NULL,
                    hot_tier_path TEXT NOT NULL,
                    cold_tier_path TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO filesystem_config (
                    id, config_version, hot_tier_path, cold_tier_path
                )
                VALUES (1, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    config_version = excluded.config_version,
                    hot_tier_path = excluded.hot_tier_path,
                    cold_tier_path = excluded.cold_tier_path
                """,
                (
                    str(tier1),
                    str(tier2),
                ),
            )
        except Exception:
            db.execute("ROLLBACK")
            raise
        else:
            db.execute("COMMIT")



def _path_mismatches(
    config: sqlite3.Row, tier1: Path, tier2: Path
) -> list[tuple[str, str, str]]:
    """比较库内 ``hot_tier_path`` / ``cold_tier_path`` 与 CLI 给出的 ``tier1`` / ``tier2``。

    逐项 ``Path(...).resolve()`` 后比较；不一致则收集为 ``(字段名, 库内原始字符串,
    CLI 期望值字符串)`` 元组列表，供 ``resolve_maintenance_paths`` 在未允许 mismatch 时
    组装错误信息。
    """
    expected = {
        "hot_tier_path": str(tier1),
        "cold_tier_path": str(tier2),
    }
    return [
        (key, config[key], value)
        for key, value in expected.items()
        if Path(config[key]).resolve() != Path(value)
    ]
