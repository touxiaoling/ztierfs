from pathlib import Path

from ztierfs.block_layout import block_file_path


def default_database(tier1: str | Path, database: str | Path | None = None) -> Path:
    return Path(database).resolve() if database else Path(tier1).resolve() / "ztierfs.sqlite3"


def block_path(tier1: str | Path, tier2: str | Path, digest: str, tier: int) -> Path:
    return block_file_path(Path(tier1) / "blocks", Path(tier2) / "blocks", digest, tier)
