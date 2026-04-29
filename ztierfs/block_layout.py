"""块文件在 tier1/tier2 目录下的两级散列路径（防单目录文件过多）。"""

from pathlib import Path


def block_file_path(
    tier1_blocks: str | Path,
    tier2_blocks: str | Path,
    digest: str,
    tier: int,
) -> Path:
    """返回给定摘要与 tier（1=热，2=冷）下的块文件绝对路径。"""
    root = Path(tier1_blocks).resolve() if tier == 1 else Path(tier2_blocks).resolve()
    return root / digest[:2] / digest[2:4] / digest
