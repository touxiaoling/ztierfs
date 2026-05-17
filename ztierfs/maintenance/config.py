"""维护工具解析 SQLite 元数据库中记录的冷热层根路径。

维护命令只接受元数据库文件作为入口，并从 ``filesystem_config`` 读取
``hot_tier_path`` / ``cold_tier_path``。显式 hot/cold tier 覆盖曾用于早期救援路径；
现在本机存储路径由挂载初始化写入，维护侧只消费这份配置，避免同一数据库被不同命令
用不同层路径解释。
"""

import sqlite3

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ztierfs.metadata import FILESYSTEM_CONFIG_SELECT, open_database


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
    database: str | Path,
) -> MaintenancePaths:
    """读取 ``database`` 的本机存储配置并返回维护命令需要的绝对路径。"""
    db_path = Path(database).resolve()
    config = _read_required_config(db_path)
    return MaintenancePaths(
        database=db_path,
        tier1=Path(config["hot_tier_path"]).resolve(),
        tier2=Path(config["cold_tier_path"]).resolve(),
    )


def _read_required_config(db_path: Path) -> sqlite3.Row:
    """从 ``db_path`` 读取 ``filesystem_config``；缺表或无行时抛 ``ValueError``。

    供 **DB-only** 分支使用：必须能从库得到冷热层路径配置。
    """
    config = _read_optional_config(db_path)
    if config is None:
        raise ValueError(
            "metadata database does not contain storage path config; "
            "mount once with the intended hot/cold tiers to write filesystem_config"
        )
    return config


def _read_optional_config(db_path: Path) -> sqlite3.Row | None:
    """读取 ``id=1`` 的 ``filesystem_config`` 行；无表或无行时返回 ``None``。

    显式 tier 模式下用于判断库内是否已有存储路径配置，以便走 mismatch / 写回 / 新建
    分支；其它 ``sqlite3.OperationalError`` 仍会向上抛出。
    """
    with closing(open_database(db_path)) as db:
        try:
            return db.execute(FILESYSTEM_CONFIG_SELECT).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: filesystem_config" in str(exc):
                return None
            raise
