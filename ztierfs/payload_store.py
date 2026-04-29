"""可选大块载荷外置存储：默认仍落在 SQLite；filekv 为原子文件 KV 后端。"""

from __future__ import annotations

import os
import threading

from pathlib import Path
from typing import Protocol

from .perf import timed


class PayloadStore(Protocol):
    """大块载荷外置存储协议：实现应与 SQLite 元数据中的指针字段配合。

    具体后端须保证崩溃或断电后不会出现「半截可见」的正式对象（常见做法是临时文件
    完整写入并经 fsync 落盘后，再原子替换到最终路径，并对目录项 fsync）。
    """
    name: str

    def put(self, key: str, payload: bytes) -> None:
        """持久化写入二进制载荷（实现宜满足原子可见性与落盘可靠性）。"""
        ...

    def get(self, key: str) -> bytes:
        """按 key 读取已持久化的载荷。"""
        ...

    def delete(self, key: str) -> None:
        """删除此外置层中的载荷（不影响 SQLite 内其它元数据；语义由调用方协调）。"""
        ...


class NullPayloadStore:
    """占位实现：内联载荷驻留在 SQLite，不经此外置存储层。

    ``put`` / ``get`` 若被调用说明配置不一致，应报错；``delete`` 为空操作（删除由
    元数据路径处理）。
    """
    name = "sqlite"

    def put(self, key: str, payload: bytes) -> None:
        """不应调用：SQLite 内联载荷由元数据库写入。"""
        raise RuntimeError("sqlite payloads are stored in the metadata database")

    def get(self, key: str) -> bytes:
        """不应调用：SQLite 内联载荷由元数据库读取。"""
        raise RuntimeError("sqlite payloads are stored in the metadata database")

    def delete(self, key: str) -> None:
        """无操作：无外置文件可删。"""
        return


class FileKVPayloadStore:
    """基于文件系统的键值存储：每键对应一个文件，用于试验非 SQLite 载荷后端。

    写入路径：在目标旁写入进程唯一的临时文件，写完后对该文件 ``fsync``，再以
    ``os.replace`` 原子替换最终路径，并对父目录 ``fsync``，避免崩溃后出现半截正式文件。
    键映射到磁盘路径时对 ``/`` 做转义，并按安全文件名（字符串 ``safe``）的前两个字符、
    再两个字符做两级子目录分片，减轻单目录下文件数量。
    """

    name = "filekv"

    def __init__(self, root: Path):
        """``root`` 为载荷文件根目录（不存在则创建）；实例带进程内锁以序列化并发写。"""
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def put(self, key: str, payload: bytes) -> None:
        """临时文件完整写入并 fsync，再原子替换到最终路径，并对所在目录 fsync。"""
        path = self._path(key)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            with timed(
                "payload_store.write",
                bytes_key="payload_store.write_bytes",
                size=len(payload),
            ):
                with open(tmp, "wb") as file:
                    file.write(payload)
                    file.flush()
                    os.fsync(file.fileno())
            os.replace(tmp, path)
            self._fsync_dir(path.parent)

    def get(self, key: str) -> bytes:
        """读取该 key 对应路径上的文件内容。"""
        with timed("payload_store.read"):
            return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        """删除目标文件（若不存在则忽略），并对父目录 fsync 以持久化目录项变更。"""
        path = self._path(key)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                return
            self._fsync_dir(path.parent)

    def _path(self, key: str) -> Path:
        """``root / safe[:2] / safe[2:4] / safe``：分片目录 + 安全文件名。"""
        safe = key.replace("/", "_")
        return self.root / safe[:2] / safe[2:4] / safe

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        """在支持 ``O_DIRECTORY`` 的系统上对目录 fd 执行 fsync（无则跳过）。"""
        if not hasattr(os, "O_DIRECTORY"):
            return
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
