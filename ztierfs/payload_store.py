from __future__ import annotations

import os
import threading

from pathlib import Path
from typing import Protocol

from .perf import timed


class PayloadStore(Protocol):
    name: str

    def put(self, key: str, payload: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...


class NullPayloadStore:
    name = "sqlite"

    def put(self, key: str, payload: bytes) -> None:
        raise RuntimeError("sqlite payloads are stored in the metadata database")

    def get(self, key: str) -> bytes:
        raise RuntimeError("sqlite payloads are stored in the metadata database")

    def delete(self, key: str) -> None:
        return


class FileKVPayloadStore:
    """Small embedded key-value store backed by atomic files.

    This is deliberately simple: it gives the prototype a non-SQLite payload
    backend while preserving the same fsync discipline used for block files.
    """

    name = "filekv"

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def put(self, key: str, payload: bytes) -> None:
        path = self._path(key)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            with timed("payload_store.write", bytes_key="payload_store.write_bytes", size=len(payload)):
                with open(tmp, "wb") as file:
                    file.write(payload)
                    file.flush()
                    os.fsync(file.fileno())
            os.replace(tmp, path)
            self._fsync_dir(path.parent)

    def get(self, key: str) -> bytes:
        with timed("payload_store.read"):
            return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                return
            self._fsync_dir(path.parent)

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_")
        return self.root / safe[:2] / safe[2:4] / safe

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
