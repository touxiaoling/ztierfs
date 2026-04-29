"""POSIX 绝对路径辅助：规范化、拆分为路径分量，以及按扩展名决定是否走后端 zstd。

`normalize_path` 与 `split_path` 供 FUSE 命名空间与块写入路径共用，保证同一逻辑
下路径表示一致。`compression_allowed` 在已知「通常已压缩」的后缀上建议跳过压缩，
由调用方（如块写入）结合「压缩后体积」等策略最终决定是否压缩。"""

import errno
import posixpath

from collections.abc import Iterable
from pathlib import Path

from macfusepy import FuseOSError


def normalize_path(path: str) -> str:
    """将输入规范为绝对 POSIX 路径字符串。

    使用 `posixpath.normpath` 折叠路径中的 ``.``、``..`` 与段间多余斜杠；具体规则与
    标准库一致（例如保留某些实现下的双前导斜杠等边界行为）。

    若规范化结果为 ``"."``，则返回 ``"/"``；否则返回规范化字符串。合法输入须为以
    ``/`` 开头的绝对路径；若路径中含 NUL（``"\\x00"``）或非绝对路径，抛出
    `FuseOSError(EINVAL)`。
    """
    if "\x00" in path or not path.startswith("/"):
        raise FuseOSError(errno.EINVAL)
    normalized = posixpath.normpath(path)
    return "/" if normalized == "." else normalized


def split_path(path: str) -> list[str]:
    """先经 `normalize_path` 再按 ``/`` 拆成非空路径分量列表。

    根 ``"/"`` 对应空列表 ``[]``；例如 ``"/a/b"`` 得到 ``["a", "b"]``。
    连续或尾部的斜杠已在规范化阶段去掉，分量中不会出现空串。
    """
    normalized = normalize_path(path)
    if normalized == "/":
        return []
    return [part for part in normalized.split("/") if part]


def compression_allowed(path: str, compressed_suffixes: Iterable[str]) -> bool:
    """根据路径最后一个 ``Path.suffix``（小写）判断是否允许尝试 zstd。

    先对 ``path`` 调用 `normalize_path`，再取 `pathlib.Path` 的 ``suffix`` 并转小写，
    与 ``compressed_suffixes`` 中的项做成员比较（调用方应使用小写后缀集合）。

    若后缀落在集合内，返回 ``False``（建议跳过压缩，因格式往往已压缩、再压收益小
    且费 CPU）；否则返回 ``True``（允许压缩管线处理）。仅表达「按扩展名的启发式」，
    不保证文件内容类型；最终是否写入压缩块仍由上层逻辑决定。
    """
    suffix = Path(normalize_path(path)).suffix.lower()
    return suffix not in compressed_suffixes
