"""绝对路径规范化与按后缀判断是否允许 zstd 压缩（与 path 工具共用）。"""

import errno
import posixpath

from collections.abc import Iterable
from pathlib import Path

from macfusepy import FuseOSError


def normalize_path(path: str) -> str:
    """规范为以 / 开头的 normpath；拒绝空字节与非绝对路径。"""
    if "\x00" in path or not path.startswith("/"):
        raise FuseOSError(errno.EINVAL)
    normalized = posixpath.normpath(path)
    return "/" if normalized == "." else normalized


def split_path(path: str) -> list[str]:
    """在 normalize 之后按 / 拆成路径分量（根目录返回空列表）。"""
    normalized = normalize_path(path)
    if normalized == "/":
        return []
    return [part for part in normalized.split("/") if part]


def compression_allowed(path: str, compressed_suffixes: Iterable[str]) -> bool:
    """若扩展名在「通常已压缩」集合中，则跳过后端 zstd 以节省 CPU。"""
    suffix = Path(normalize_path(path)).suffix.lower()
    return suffix not in compressed_suffixes
