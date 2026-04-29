"""块与 inode 的访问时间（atime）及块读次数统计，支持延迟合并以降低写放大。

**立即路径与延迟路径**

- ``defer_node_atime`` / ``defer_block_accesses``：在进程内、受 ``_deferred_access_lock``
  保护的队列中合并更新（inode 只保留每个 id 的最新 ``atime_ns``；块按 digest 合并
  ``atime_ns`` 与累加 ``read_count``）。二者返回 ``bool``，表示按当前配置是否**建议**
  尽快开写事务刷盘（例如读路径在阈值到达时主动 ``transaction()``，以便顺带提交
  ``record_block_presence`` 等元数据），并不保证队列已空。
- ``touch_block_atime``：对当前 ``_db`` 连接**立即**执行一条 ``UPDATE blocks``（刷新
  ``atime_ns`` 且 ``read_count + 1``），**不**经过延迟队列；调用方须已在合适的
  ``write_transaction``（或等价的写连接上下文）内，否则统计可能未随事务提交。

**与 ``MetadataStore`` 写事务的关系**

- ``flush_deferred_accesses`` 在 ``MetadataStore._transaction`` 中，于写事务
  ``yield`` 正常返回之后、``COMMIT`` **之前**被调用，将本批延迟的 inode atime 与块访问统计
  写入**同一** SQLite 提交。若 ``yield`` 体内抛错走 ``ROLLBACK``，则**不会**调用本方法，
  延迟队列仍保留在内存中，待后续成功写事务再刷。
- ``close`` / ``commit`` 在仍有延迟项时也会通过写事务走到同一刷新路径，避免进程
  退出丢失未提交的访问统计。

**刷新阈值（``MetadataStore`` 构造参数，默认见模块常量）**

- ``_deferred_access_flush_blocks``：延迟队列中**不同块 digest** 的数量达到该值
  时，``defer_*`` 的返回值为真，建议刷盘（仅统计块字典行数，与 inode 条数无关）。
- ``_deferred_access_flush_ns``：自本线程**首次**登记延迟访问起的单调时钟间隔
  （纳秒）达到该值时同样建议刷盘；默认 ``1_000_000_000`` 即约 1 秒。

其它代码路径（如 ``BlockStore``）也可能在写连接上直接调用 ``flush_deferred_accesses``，
仍须与 ``MetadataStore`` 的写事务/连接绑定一致。
"""

from collections.abc import Iterator
from dataclasses import dataclass

from .base import MetadataMixinBase

DEFAULT_DEFERRED_ACCESS_FLUSH_BLOCKS = 64
DEFAULT_DEFERRED_ACCESS_FLUSH_NS = 1_000_000_000


@dataclass(frozen=True)
class BlockAccessStats:
    """延迟队列中单个块的合并统计：最新 ``atime_ns`` 与自上次刷盘以来累加的读次数增量。"""

    digest: str
    atime_ns: int
    read_count: int


class AccessStatsMixin(MetadataMixinBase):
    """访问统计的延迟队列与刷盘：与 ``MetadataStore`` 写事务协作，按阈值建议提前提交。"""

    def defer_node_atime(self, node_id: int, now: int) -> bool:
        """将 inode 的 ``atime_ns`` 记入延迟表（同 id 覆盖为最新 ``now``）。

        若距本批起始时间或块队列规模已达阈值，返回 ``True``，提示调用方可开写事务
        以触发 ``flush_deferred_accesses``（通常与块访问延迟一并提交）。
        """

        with self._deferred_access_lock:
            self._start_deferred_accesses(now)
            self._deferred_node_atimes[node_id] = now
            return self._deferred_accesses_should_flush(now)

    def touch_block_atime(self, digest: str, now: int) -> None:
        """立即更新块的 ``atime_ns`` 并将 ``read_count`` 加一；不经延迟队列。

        须在已绑定的写库连接上使用（一般由 ``write_transaction`` 包裹），与 ``defer_*``
        的批量、事务尾刷新路径相对。
        """

        self._db.execute(
            """
            UPDATE blocks
            SET atime_ns = ?, read_count = read_count + 1
            WHERE hash = ?
            """,
            (now, digest),
        )

    def defer_block_accesses(self, digests: Iterator[str], now: int) -> bool:
        """将一次或多次块读合并记入延迟表：同 ``digest`` 累加 ``read_count``，``atime_ns`` 取本次 ``now``。

        遍历完成后若达到块条数或时间阈值，返回 ``True``，含义同 ``defer_node_atime``。
        """

        with self._deferred_access_lock:
            self._start_deferred_accesses(now)
            for digest in digests:
                current = self._deferred_block_accesses.get(digest)
                read_count = 1 if current is None else current.read_count + 1
                self._deferred_block_accesses[digest] = BlockAccessStats(
                    digest=digest,
                    atime_ns=now,
                    read_count=read_count,
                )
            return self._deferred_accesses_should_flush(now)

    def has_deferred_accesses(self) -> bool:
        """若 inode 或块延迟队列非空则返回真（持 ``_deferred_access_lock`` 读取）。"""

        with self._deferred_access_lock:
            return bool(self._deferred_node_atimes or self._deferred_block_accesses)

    def flush_deferred_accesses(self) -> None:
        """将延迟的 inode atime 与块 ``atime_ns``/``read_count`` 增量写入 ``_db``。

        调用方须保证当前连接处于可写且最终会被提交（典型为 ``MetadataStore`` 写事务
        收尾）；本方法先在锁内取出并清空队列，再执行 ``executemany``，与 ``COMMIT``
        同属一次提交的有效范围。
        """

        with self._deferred_access_lock:
            node_atimes = self._deferred_node_atimes
            block_accesses = list(self._deferred_block_accesses.values())
            self._deferred_node_atimes = {}
            self._deferred_block_accesses = {}
            self._deferred_access_started_ns = None

        if node_atimes:
            self._db.executemany(
                "UPDATE inodes SET atime_ns = ? WHERE id = ?",
                [(atime_ns, node_id) for node_id, atime_ns in node_atimes.items()],
            )
        if block_accesses:
            self._db.executemany(
                """
                UPDATE blocks
                SET atime_ns = ?, read_count = read_count + ?
                WHERE hash = ?
                """,
                [
                    (access.atime_ns, access.read_count, access.digest)
                    for access in block_accesses
                ],
            )

    def _start_deferred_accesses(self, now: int) -> None:
        """本线程延迟批的起始时间：首次登记时记录 ``now``，供时间阈值判断。"""

        if self._deferred_access_started_ns is None:
            self._deferred_access_started_ns = now

    def _deferred_accesses_should_flush(self, now: int) -> bool:
        """块延迟表行数是否不少于 ``_deferred_access_flush_blocks``，或距批起始是否已满 ``_deferred_access_flush_ns``。"""

        if len(self._deferred_block_accesses) >= self._deferred_access_flush_blocks:
            return True
        if self._deferred_access_started_ns is None:
            return False
        return now - self._deferred_access_started_ns >= self._deferred_access_flush_ns
