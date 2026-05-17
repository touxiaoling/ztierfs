"""冷热层路径探测与按路径读字节：按 errno 将结果分为「存在」「ENOENT 类缺失」与「可重试的暂时不可用」。

`stat`/`read_bytes`/`unlink` 遇 `FileNotFoundError`（通常为 ENOENT）归为缺失；遇
`TEMPORARY_UNAVAILABLE_ERRNOS` 中的 errno 视为挂载或网络类瞬时故障，映射为
`PathProbe("unavailable", …)` 或 `PathUnavailable`，与盘上对象真正不存在（`PathMissing`）
区分，供 copy-up、读路径与删除策略决定是否重试或降级。
"""

import errno

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


PathStatus = Literal["present", "missing", "unavailable"]

# 视为「挂载点暂时不可用」而非「对象不存在」的 errno 集合（用于 copy-up / 读路径策略）。
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
    """`probe_path` 的探测结果：按 errno 语义区分三种状态。

    - ``present``：`stat` 成功，路径在盘上可见。
    - ``missing``：捕获 `FileNotFoundError`（多为 ENOENT），视为对象不存在。
    - ``unavailable``：`OSError.errno` 属于 `TEMPORARY_UNAVAILABLE_ERRNOS`（如 EIO、
      ETIMEDOUT、ENETDOWN 等），视为冷层/挂载暂时不可用而非缺失；此时 ``errno``、
      ``error`` 供日志与重试策略使用。
    """

    status: PathStatus
    errno: int | None = None
    error: str | None = None

    @property
    def present(self) -> bool:
        """是否为 ``present``（路径存在且 `stat` 未因暂时性 errno 失败）。"""
        return self.status == "present"

    @property
    def missing(self) -> bool:
        """是否为 ``missing``（ENOENT 类：路径不存在）。"""
        return self.status == "missing"

    @property
    def unavailable(self) -> bool:
        """是否为 ``unavailable``（暂时不可用 errno，非 ENOENT）。"""
        return self.status == "unavailable"


class PathMissing(FileNotFoundError):
    """读路径时盘上无该文件：由 `FileNotFoundError`（多为 ENOENT）转换，表示缺失而非暂时故障。

    与 ``PathUnavailable``（`TEMPORARY_UNAVAILABLE_ERRNOS` 中的 errno）区分。
    """


class PathUnavailable(OSError):
    """路径操作遇暂时不可用 errno：底层为 `OSError`，errno 在 `TEMPORARY_UNAVAILABLE_ERRNOS` 内。

    典型于冷层挂载、网络或远端 transient 错误；非 ENOENT，不应与 ``PathMissing`` 混用。
    ``original`` 保留完整异常链供诊断。
    """

    def __init__(self, path: Path, exc: OSError):
        """用 ``path`` 与 ``exc``（含 errno）构造，供上层按 errno 分类处理。"""
        super().__init__(exc.errno, str(exc), str(path))
        self.path = path
        self.original = exc


def is_temporary_unavailable_error(exc: OSError) -> bool:
    """若 ``exc.errno`` 属于暂时不可用集合则返回真；否则应由调用方原样传播或另行处理。"""
    return exc.errno in TEMPORARY_UNAVAILABLE_ERRNOS


def probe_path(path: Path) -> PathProbe:
    """对 ``path`` 执行 `stat` 并返回 `PathProbe`：按异常类型与 errno 分类。

    - `FileNotFoundError` → ``missing``（ENOENT 类）。
    - `OSError` 且 errno ∈ `TEMPORARY_UNAVAILABLE_ERRNOS` → ``unavailable``，并填充
      ``errno``/``error``。
    - 其余 `OSError` 不捕获，由调用方处理。
    """
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
    """读取 ``path`` 的全部字节；按 errno 语义抛出或传播。

    - `FileNotFoundError`（多为 ENOENT）→ 抛出 ``PathMissing``。
    - `OSError` 且 errno ∈ `TEMPORARY_UNAVAILABLE_ERRNOS` → 抛出 ``PathUnavailable``。
    - 其他 `OSError` 原样抛出。
    """
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise PathMissing(str(path)) from exc
    except OSError as exc:
        if is_temporary_unavailable_error(exc):
            raise PathUnavailable(path, exc) from exc
        raise


def unlink_path(path: Path) -> bool:
    """删除 ``path``（文件或符号链接）；按 errno 语义返回或抛出。

    - `FileNotFoundError`（ENOENT）→ 返回 ``False``（视为已不存在）。
    - `OSError` 且 errno ∈ `TEMPORARY_UNAVAILABLE_ERRNOS` → 抛出 ``PathUnavailable``。
    - 成功删除 → 返回 ``True``；其余 `OSError` 原样抛出。
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        if is_temporary_unavailable_error(exc):
            raise PathUnavailable(path, exc) from exc
        raise
    return True
