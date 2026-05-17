"""ztierfs 元数据子包的对外入口（少量常用符号再导出）。

再导出：

- `MetadataStore`：SQLite 元数据门面（事务、命名空间、块表等的组合入口）。
- `ConnectionPool`、`SQLitePragmas`、`open_database`：连接池、PRAGMA 参数与打开数据库。
- `SCHEMA_VERSION` / `CONFIG_VERSION`：schema 与本机路径配置行版本。
- `FILESYSTEM_CONFIG_SELECT` / `FILESYSTEM_CONFIG_UPSERT`：路径配置读写 SQL。
- `BLOCK_RECORD_SELECT`：`blocks`、`block_locations`、`block_payloads` 联表查询的公共 SQL 片段。

其余 API 见 `ztierfs.metadata` 包内各子模块。"""

from .connection import ConnectionPool, SQLitePragmas, open_database
from .schema import (
    BLOCK_RECORD_SELECT,
    CONFIG_VERSION,
    FILESYSTEM_CONFIG_SELECT,
    FILESYSTEM_CONFIG_UPSERT,
    SCHEMA_VERSION,
)
from .store import MetadataStore

__all__ = [
    "BLOCK_RECORD_SELECT",
    "CONFIG_VERSION",
    "FILESYSTEM_CONFIG_SELECT",
    "FILESYSTEM_CONFIG_UPSERT",
    "SCHEMA_VERSION",
    "ConnectionPool",
    "MetadataStore",
    "SQLitePragmas",
    "open_database",
]
