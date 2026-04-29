"""维护工具（fsck、清理、统计等）与挂载实例共用的路径约定。

- **元数据库**：未单独指定 SQLite 路径时，默认使用热层（tier1）根目录下的
  ``ztierfs.sqlite3``，与在线挂载时的默认库位置一致。
- **块文件**：与 ``BlockStore`` 相同，块落在各层根目录的 ``blocks`` 子目录下，
  并按内容摘要做两级十六进制前缀分片（详见 ``ztierfs.block_layout``）。
"""

from pathlib import Path

from ztierfs.block_layout import block_file_path


def default_database(tier1: str | Path, database: str | Path | None = None) -> Path:
    """解析维护命令要打开的 SQLite 元数据库路径。

    ``tier1`` 为热层根目录（与 CLI ``mount`` 的第一个参数含义一致）。若 ``database``
    为 ``None``，则返回 ``<tier1>/ztierfs.sqlite3``；否则将 ``database`` 视为用户
    显式给出的库文件路径（可为相对或绝对路径），经 ``Path.resolve()`` 规范化后返回。
    """
    return (
        Path(database).resolve()
        if database
        else Path(tier1).resolve() / "ztierfs.sqlite3"
    )


def block_path(tier1: str | Path, tier2: str | Path, digest: str, tier: int) -> Path:
    """计算给定摘要与层级下，块 payload 在磁盘上的路径（维护侧与在线逻辑对齐）。

    在 ``tier1``、``tier2`` 分别为热层、冷层根目录的前提下，实际块根为
    ``<tier1>/blocks`` 与 ``<tier2>/blocks``。``tier`` 为 ``1`` 时使用热层块根，
    为 ``2`` 时使用冷层块根。相对路径由 ``block_file_path`` 解析为绝对路径，
    最终形态为 ``<resolved_blocks_root>/<digest 前 2 字符>/<再 2 字符>/<完整 digest>``。
    """
    return block_file_path(Path(tier1) / "blocks", Path(tier2) / "blocks", digest, tier)
