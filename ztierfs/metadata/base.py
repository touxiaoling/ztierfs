"""元数据 mixin 的共享状态声明与跨域协作接口（由 `MetadataStore` 组合各 mixin 后整体提供）。

本模块不实现 SQL；各 mixin 在**已开启的**读/写事务内通过 `_db` 执行语句。`MetadataStore` 用线程局部绑定连接，并禁止在读事务中嵌套写事务；详见 `store` 模块。
"""

from __future__ import annotations

import sqlite3
import threading

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .access_stats import BlockAccessStats
    from ztierfs.payload_store import PayloadStore


class MetadataMixinBase:
    """各元数据 mixin 的公共基类：只声明跨 mixin 共享的实例字段，以及需由组合类在事务内提供的操作入口。

    具体方法由 `BlockMetadataMixin`、`ChunkMetadataMixin`、`NamespaceMixin` 等在同一个 `MetadataStore` 实例上实现；本类中的 `NotImplementedError` 仅作类型与契约占位，**不是**要求单一子类覆写全部方法。各 mixin 可互相调用这些成员作为跨表协作（例如块 refcount 与 `file_chunks` 配对约定见各 mixin 文档）。
    """

    _deferred_access_lock: threading.Lock
    _deferred_node_atimes: dict[int, int]
    _deferred_block_accesses: dict[str, BlockAccessStats]
    _deferred_access_started_ns: int | None
    _deferred_access_flush_blocks: int
    _deferred_access_flush_ns: int
    payload_store: "PayloadStore"

    @property
    def _db(self) -> sqlite3.Connection:
        """当前线程、当前元数据读/写事务中绑定的 SQLite 连接；无活动事务时由 `MetadataStore` 抛错。

        仅应在 `read_transaction` / `write_transaction`（或存储初始化等内部已设置 `_local.db` 的路径）内使用，保证语句参与同一事务与锁语义。
        """
        raise NotImplementedError

    def increment_block_refcount(self, digest: str) -> None:
        """将 `blocks` 中该内容哈希的引用计数加一（新引用指向已存在块时由上层在写事务中调用）。"""
        raise NotImplementedError

    def block_exists(self, digest: str) -> bool:
        """若 `blocks` 表已有该哈希则返回真，用于存在性检查。"""
        raise NotImplementedError

    def upsert_file_chunk(
        self, file_id: int, chunk_index: int, digest: str, size: int
    ) -> None:
        """插入或更新 `file_chunks` 中 (file_id, chunk_index) 的 hash 与 size；不修改块 refcount。"""
        raise NotImplementedError

    def clone_file_node(
        self,
        source_id: int,
        parent_id: int,
        name: str,
        *,
        mode: int,
        uid: int,
        gid: int,
        size: int,
        now: int,
    ) -> int:
        """从源文件 inode 克隆新普通文件：新 inode、目录项、内联 payload/`file_chunks`/xattr，并增加所引用块的 refcount；返回新 inode id。"""
        raise NotImplementedError

    def set_inline_file(
        self,
        node_id: int,
        data: bytes,
        *,
        compressed: bool,
        raw_size: int,
        now: int,
    ) -> None:
        """将文件数据以内联形式写入 `inode_payloads`（可经外置 `payload_store`），并更新 inode 的 size 与时间戳。"""
        raise NotImplementedError

    def clear_inline_file(self, node_id: int) -> None:
        """删除该 inode 的 `inode_payloads` 行，并视情况移除外置 payload 中的对象。"""
        raise NotImplementedError
