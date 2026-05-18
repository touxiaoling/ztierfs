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
    """在当前上下文内聚合的性能计数：各标签下的累计纳秒耗时、调用次数与可选的字节量。"""

    timings_ns: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    bytes: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add_time(self, name: str, elapsed_ns: int) -> None:
        """将 `elapsed_ns` 累加到 `name`，并增加该名称的调用次数。"""
        self.timings_ns[name] += elapsed_ns
        self.counts[name] += 1

    def add_bytes(self, name: str, size: int) -> None:
        """将 `size` 字节累加到 `name` 对应的字节统计上。"""
        self.bytes[name] += size

    def snapshot(self) -> dict[str, dict[str, int]]:
        """返回 timing、count、bytes 三份字典的浅拷贝，供合并或导出。"""
        return {
            "timings_ns": dict(self.timings_ns),
            "counts": dict(self.counts),
            "bytes": dict(self.bytes),
        }


_CURRENT_COUNTERS: contextvars.ContextVar[PerfCounters | None] = contextvars.ContextVar(
    "ztierfs_perf_counters", default=None
)


def current_counters() -> PerfCounters | None:
    """返回当前上下文中活动的 `PerfCounters`；若未处于 `collect_perf` 块内则为 `None`。"""
    return _CURRENT_COUNTERS.get()


def count(name: str, amount: int = 1) -> None:
    """若存在活动计数器，则给 ``name`` 增加 ``amount`` 次数。"""
    counters = current_counters()
    if counters is not None:
        counters.counts[name] += amount


@contextmanager
def collect_perf() -> Iterator[PerfCounters]:
    """在 `with` 块内将新的 `PerfCounters` 挂到当前 context，退出时恢复，供嵌套 `timed` 等写入。"""
    counters = PerfCounters()
    token = _CURRENT_COUNTERS.set(counters)
    try:
        yield counters
    finally:
        _CURRENT_COUNTERS.reset(token)


@contextmanager
def timed(name: str, *, bytes_key: str | None = None, size: int = 0) -> Iterator[None]:
    """若存在活动计数器，则记录本段代码在 `name` 下的纳秒耗时；当提供 `bytes_key` 且 `size>0` 时同时累加字节量。"""
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
    """跨多次 `record` 合并 `PerfCounters`，按固定间隔将累计结果中耗时最高的若干项输出到日志。"""

    def __init__(self, *, interval_seconds: float, top_n: int = 20):
        """`interval_seconds` 控制两次定期日志之间的最小间隔；`top_n` 为每次输出中按总耗时排序取前多少条。"""
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
        """将一次 `PerfCounters` 的快照合并进内部累计；若距上次输出已超过 `interval_seconds`，则打一条汇总日志。"""
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
        """立即输出当前全部累计统计（常用于进程或会话结束前）。"""
        with self._lock:
            snapshot = self._snapshot_locked()
        self._log_snapshot("最终性能统计", snapshot)

    def _snapshot_locked(self) -> dict[str, dict[str, int]]:
        """在已持锁的前提下，将内部 timing、count、bytes 复制为与 `PerfCounters.snapshot` 同构的字典。"""
        return {
            "timings_ns": dict(self._timings_ns),
            "counts": dict(self._counts),
            "bytes": dict(self._bytes),
        }

    def _log_snapshot(self, title: str, snapshot: dict[str, dict[str, int]]) -> None:
        """按总耗时降序取前 `top_n` 条，格式化 count、总秒数、平均毫秒与可选字节后写入日志。"""
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
