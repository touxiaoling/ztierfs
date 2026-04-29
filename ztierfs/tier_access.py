"""冷热层路径探测与读取：区分缺失、暂时不可用（网盘 I/O）与可读。"""

import errno

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


PathStatus = Literal["present", "missing", "unavailable"]

TEMPORARY_UNAVAILABLE_ERRNOS = {
    errno.EIO,
    errno.ETIMEDOUT,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ECONNRESET,
    errno.ENOTCONN,
}
for _name in ("EHOSTDOWN", "EHOSTUNREACH"):
    _value = getattr(errno, _name, None)
    if _value is not None:
        TEMPORARY_UNAVAILABLE_ERRNOS.add(_value)


@dataclass(frozen=True)
class PathProbe:
    status: PathStatus
    errno: int | None = None
    error: str | None = None

    @property
    def present(self) -> bool:
        return self.status == "present"

    @property
    def missing(self) -> bool:
        return self.status == "missing"

    @property
    def unavailable(self) -> bool:
        return self.status == "unavailable"


class PathMissing(FileNotFoundError):
    """预期存在的块文件路径在盘上不存在（与暂时性 I/O 错误区分）。"""


class PathUnavailable(OSError):
    """冷层路径存在但当前无法读取（例如 rclone 暂时错误），携带底层 OSError。"""

    def __init__(self, path: Path, exc: OSError):
        super().__init__(exc.errno, str(exc), str(path))
        self.path = path
        self.original = exc


def is_temporary_unavailable_error(exc: OSError) -> bool:
    return exc.errno in TEMPORARY_UNAVAILABLE_ERRNOS


def probe_path(path: Path) -> PathProbe:
    try:
        path.stat()
    except FileNotFoundError:
        return PathProbe("missing")
    except OSError as exc:
        if is_temporary_unavailable_error(exc):
            return PathProbe("unavailable", errno=exc.errno, error=str(exc))
        raise
    return PathProbe("present")


def read_path_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise PathMissing(str(path)) from exc
    except OSError as exc:
        if is_temporary_unavailable_error(exc):
            raise PathUnavailable(path, exc) from exc
        raise


def unlink_path(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        if is_temporary_unavailable_error(exc):
            raise PathUnavailable(path, exc) from exc
        raise
    return True
