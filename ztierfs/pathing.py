import errno
import posixpath

from collections.abc import Iterable
from pathlib import Path

from macfusepy import FuseOSError


def normalize_path(path: str) -> str:
    if "\x00" in path or not path.startswith("/"):
        raise FuseOSError(errno.EINVAL)
    normalized = posixpath.normpath(path)
    return "/" if normalized == "." else normalized


def split_path(path: str) -> list[str]:
    normalized = normalize_path(path)
    if normalized == "/":
        return []
    return [part for part in normalized.split("/") if part]


def compression_allowed(path: str, compressed_suffixes: Iterable[str]) -> bool:
    suffix = Path(normalize_path(path)).suffix.lower()
    return suffix not in compressed_suffixes
