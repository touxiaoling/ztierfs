"""维护工具共用的默认库路径与块路径拼接。"""

from pathlib import Path

from ztierfs.block_layout import block_file_path


def default_database(tier1: str | Path, database: str | Path | None = None) -> Path:
    """未显式指定时，SQLite 库默认位于热层根目录下的 ztierfs.sqlite3。"""
    return (
        Path(database).resolve()
        if database
        else Path(tier1).resolve() / "ztierfs.sqlite3"
    )


def block_path(tier1: str | Path, tier2: str | Path, digest: str, tier: int) -> Path:
    """维护侧使用的块文件路径（在 tier 的 blocks 子目录下）。"""
    return block_file_path(Path(tier1) / "blocks", Path(tier2) / "blocks", digest, tier)
