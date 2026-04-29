from pathlib import Path


def block_file_path(
    tier1_blocks: str | Path,
    tier2_blocks: str | Path,
    digest: str,
    tier: int,
) -> Path:
    root = Path(tier1_blocks).resolve() if tier == 1 else Path(tier2_blocks).resolve()
    return root / digest[:2] / digest[2:4] / digest
