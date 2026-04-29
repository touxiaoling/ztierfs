"""轻量性能计数：上下文变量中的计时/字节量，供块层与 SQLite 包装使用。"""

from __future__ import annotations

import contextvars
import time
import threading

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from loguru import logger


@dataclass
class PerfCounters:
    timings_ns: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    bytes: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add_time(self, name: str, elapsed_ns: int) -> None:
        self.timings_ns[name] += elapsed_ns
        self.counts[name] += 1

    def add_bytes(self, name: str, size: int) -> None:
        self.bytes[name] += size

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {
            "timings_ns": dict(self.timings_ns),
            "counts": dict(self.counts),
            "bytes": dict(self.bytes),
        }


_CURRENT_COUNTERS: contextvars.ContextVar[PerfCounters | None] = contextvars.ContextVar(
    "ztierfs_perf_counters", default=None
)


def current_counters() -> PerfCounters | None:
    return _CURRENT_COUNTERS.get()


@contextmanager
def collect_perf() -> Iterator[PerfCounters]:
    counters = PerfCounters()
    token = _CURRENT_COUNTERS.set(counters)
    try:
        yield counters
    finally:
        _CURRENT_COUNTERS.reset(token)


@contextmanager
def timed(name: str, *, bytes_key: str | None = None, size: int = 0) -> Iterator[None]:
    counters = current_counters()
    if counters is None:
        yield
        return
    start = time.perf_counter_ns()
    try:
        yield
    finally:
        counters.add_time(name, time.perf_counter_ns() - start)
        if bytes_key is not None and size:
            counters.add_bytes(bytes_key, size)


class OperationProfiler:
    def __init__(self, *, interval_seconds: float, top_n: int = 20):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = interval_seconds
        self.top_n = top_n
        self._lock = threading.Lock()
        self._timings_ns: dict[str, int] = defaultdict(int)
        self._counts: dict[str, int] = defaultdict(int)
        self._bytes: dict[str, int] = defaultdict(int)
        self._last_log = time.monotonic()

    def record(self, counters: PerfCounters) -> None:
        snapshot = None
        with self._lock:
            for name, elapsed_ns in counters.timings_ns.items():
                self._timings_ns[name] += elapsed_ns
            for name, count in counters.counts.items():
                self._counts[name] += count
            for name, size in counters.bytes.items():
                self._bytes[name] += size
            now = time.monotonic()
            if now - self._last_log >= self.interval_seconds:
                self._last_log = now
                snapshot = self._snapshot_locked()
        if snapshot is not None:
            self._log_snapshot("累计性能统计", snapshot)

    def log_final(self) -> None:
        with self._lock:
            snapshot = self._snapshot_locked()
        self._log_snapshot("最终性能统计", snapshot)

    def _snapshot_locked(self) -> dict[str, dict[str, int]]:
        return {
            "timings_ns": dict(self._timings_ns),
            "counts": dict(self._counts),
            "bytes": dict(self._bytes),
        }

    def _log_snapshot(self, title: str, snapshot: dict[str, dict[str, int]]) -> None:
        timings = snapshot["timings_ns"]
        counts = snapshot["counts"]
        sizes = snapshot["bytes"]
        if not timings:
            return
        rows = []
        for name, elapsed_ns in sorted(
            timings.items(), key=lambda item: item[1], reverse=True
        )[: self.top_n]:
            count = counts.get(name, 0)
            total_s = elapsed_ns / 1_000_000_000
            avg_ms = elapsed_ns / count / 1_000_000 if count else 0
            size = sizes.get(name, 0)
            suffix = f"，bytes={size}" if size else ""
            rows.append(
                f"{name}: count={count}, total={total_s:.3f}s, avg={avg_ms:.3f}ms{suffix}"
            )
        logger.info("{}：\n{}", title, "\n".join(rows))
