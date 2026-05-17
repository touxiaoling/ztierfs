"""元数据 mixin 的共享状态声明（由 `MetadataStore` 组合各 mixin 后整体提供）。

本模块不实现 SQL；各 mixin 在**已开启的**读/写事务内通过 `_db` 执行语句。`MetadataStore` 用线程局部绑定连接，并禁止在读事务中嵌套写事务；详见 `store` 模块。
"""

from __future__ import annotations

import sqlite3
import threading

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .access_stats import BlockAccessStats


class MetadataMixinBase:
    """各元数据 mixin 的公共基类：只声明共享字段和当前事务连接入口。"""

    _deferred_access_lock: threading.Lock
    _deferred_node_atimes: dict[int, int]
    _deferred_block_accesses: dict[str, BlockAccessStats]
    _deferred_access_started_ns: int | None
    _deferred_access_flush_blocks: int
    _deferred_access_flush_ns: int

    @property
    def _db(self) -> sqlite3.Connection:
        """当前线程、当前元数据读/写事务中绑定的 SQLite 连接；无活动事务时由 `MetadataStore` 抛错。

        仅应在 `read_transaction` / `write_transaction`（或存储初始化等内部已设置 `_local.db` 的路径）内使用，保证语句参与同一事务与锁语义。
        """
        raise NotImplementedError
