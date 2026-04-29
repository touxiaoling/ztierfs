"""内容寻址块在磁盘上的路径约定。

块按 SHA-256（或其它）十六进制摘要寻址，落在「热层」或「冷层」根目录下。
为减轻单目录 inode/目录项压力，采用两级前缀子目录：取摘要前 2 个与再 2 个
十六进制字符作为子目录名，完整摘要作为文件名，即::

    <tier_root>/<digest[0:2]>/<digest[2:4]>/<digest 全文>

``tier`` 约定：1 表示热层（``tier1_blocks``），2 表示冷层（``tier2_blocks``）。
根路径经 ``resolve()`` 规范化，便于跨符号链接或相对路径时得到稳定绝对路径。
"""

from pathlib import Path


def block_file_path(
    tier1_blocks: str | Path,
    tier2_blocks: str | Path,
    digest: str,
    tier: int,
) -> Path:
    """根据摘要与层级，解析块文件在磁盘上的绝对路径。

    参数 ``digest`` 应为小写或大写一致的十六进制字符串（常见为 64 位 SHA-256），
    长度至少 4，以便切出两级前缀目录；更短时行为取决于调用方约定，本函数不校验。

    Args:
        tier1_blocks: 热层块根目录（``tier == 1`` 时使用）。
        tier2_blocks: 冷层块根目录（``tier == 2`` 时使用）。
        digest: 块的十六进制内容摘要，用作文件名及前缀分片依据。
        tier: ``1`` 热层，``2`` 冷层；决定选用哪个根目录。

    Returns:
        形如 ``<resolved_root>/<aa>/<bb>/<digest>`` 的 ``Path``，其中 ``aa``、``bb``
        分别为 ``digest`` 的前两个与再两个十六进制字符。
    """
    root = Path(tier1_blocks).resolve() if tier == 1 else Path(tier2_blocks).resolve()
    return root / digest[:2] / digest[2:4] / digest
