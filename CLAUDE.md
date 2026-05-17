@/Users/tomin/.codex/RTK.md

# ztierfs Agent 指南

## 项目定位

`ztierfs` 是一个面向真实使用的 macOS 可写 FUSE 文件系统，不是一次性 demo。它通过 macFUSE 暴露文件系统语义，用 SQLite 持久化命名空间和文件元数据，用 zstd 压缩的内容寻址块存储文件内容，并支持去重、inline 小块、本机热层与 rclone 本地挂载冷层之间的冷热分层。

核心价值：在保持常见 POSIX 文件系统语义的前提下，为可重新生成的数据提供去重、压缩、冷热分层和可诊断的恢复能力。

代码取舍从数据正确性和可恢复性倒推：

- 保持读写、rename、unlink、hardlink、xattr、clonefile、权限和打开句柄等文件系统语义稳定。
- SQLite 元数据、块文件、引用计数和冷热层位置必须在成功、失败和中途崩溃后保持可检查、可恢复或明确报告。
- 块文件是用户数据，不要削弱原子写入、fsync、事务边界或错误报告。
- 维护命令应优先做到保守、可诊断；不能安全修复的损坏应报告为不可自动修复，不要伪造数据。

当前项目仍处于早期阶段，尚未承诺兼容已有生产数据。可以接受破坏性的 SQLite schema、块目录布局、CLI 和内部 API 变更；优先选择长期正确、简单、可维护的设计，不要为未发布状态堆叠迁移层或兼容 shim。

本项目只支持 macOS/macFUSE，不维护 Linux/FUSE3 路径。不要为了跨平台抽象牺牲当前 macOS 行为的正确性。

## 编程原则

- **KISS:** 直接表达文件系统行为，优先清晰、可审计的实现。
- **YAGNI:** 不为尚不存在的迁移、远程存储接口、多进程协调、加密、配额或跨平台需求增加抽象层。
- **DRY:** 重复且影响维护的事务、块访问、路径处理或报告逻辑应整合，但不要为了形式统一引入更大的间接层。
- **行为优先:** 修改文件系统行为时，优先补充能描述可观察语义的测试，而不是只测试内部函数形状。
- **小步改动:** 只修改完成任务所需的路径；不要顺手重构无关模块或改变磁盘格式，除非该变更直接服务当前目标。

## 关键语义

除非用户明确要求设计变更，否则保持这些行为：

- 支持普通文件、目录、符号链接和普通文件 hardlink；非普通文件的 `mknod` 应拒绝。
- 元数据和块记录必须能在重新打开后持久化。
- 文件内容按固定大小切块；缺失块读取表现为零填充稀疏区域。
- 相同块按 SHA-256 去重，并通过引用计数管理生命周期。
- macOS `clonefile` 克隆 inode、xattr 和 chunk 引用，增加块引用计数，不重写 payload。
- 小块可 inline 到 SQLite；inline 块不参与冷热层降级。
- hardlink 共享同一个 inode、文件内容、xattr、权限和时间戳。
- 扩展属性跟随 inode。
- 权限检查基于 FUSE 请求上下文中的 uid/gid 和 POSIX mode。
- POSIX advisory 文件锁只保证当前挂载进程内一致；不要假设多个独立挂载进程共享锁状态。
- 打开的文件句柄在 rename 或 unlink 后继续指向原 inode；已 unlink 但仍打开的文件在最后一个句柄 release 后清理。
- 块先写入热层；热层超过高水位后按读取时间、读取频率和文件块位置选择候选降级到冷层。
- 每个文件开头的若干块默认保护在热层，避免大文件顺序读取开头时被冷层延迟卡住。
- 读取冷层块时可 copy-up 回热层，并暂时保留冷层副本；后续由 `cleanup` 删除过期冷层副本。
- 对常见已压缩后缀跳过 zstd；zstd 输出不比原始数据小时也跳过压缩。

## 开发规则

- 使用现有路径工具和元数据访问层，不要临时拼接路径或绕过 `normalize_path`、`split_path`、`MetadataStore` 和现有查询辅助函数。
- `MetadataStore` 使用显式读写事务和连接池；不要在 read transaction 内开启 write transaction。
- 同时影响 SQLite 元数据和块文件的改动，要特别检查事务提交、回滚、引用计数、孤儿块、缺失块和冷热层位置。
- 修改写入、删除、truncate、rename 覆盖、copy-up、downgrade、cleanup 或引用计数路径时，优先补充故障注入测试。
- 不要削弱 `BlockStore` 的原子写入、目录 fsync、文件 fsync 和块 hash 校验。
- 保留类 POSIX 错误行为，例如 `ENOENT`、`EISDIR`、`ENOTDIR`、`ENOTEMPTY`、`EINVAL`、`EIO`。
- 单元测试不应依赖真实 FUSE 挂载；真实挂载测试必须标记为 `integration`。
- 修改 schema、磁盘布局、CLI 或文档时，同步更新测试和 README 中对应说明。
- 修改代码后运行`uv run ruff format . && uv run ruff check --fix . && uv run ty check --fix .`然后修复所有问题。

## 常用命令

本地开发使用 `uv`，并按仓库指令给 shell 命令加 `rtk` 前缀。

```bash
rtk uv sync
rtk uv run pytest -m "not integration"
rtk uv run pytest -m integration
```

修改 CLI、维护命令或纯逻辑路径时，至少运行相关单元测试。修改 FUSE 回调、挂载参数、真实 POSIX 行为或 macOS 特有语义时，单元测试通过后优先补跑 integration 测试。

常用定向测试：

```bash
rtk uv run pytest tests/test_filesystem_operations.py -q
rtk uv run pytest tests/test_filesystem_storage.py -q
rtk uv run pytest tests/test_crash_recovery.py -q
rtk uv run pytest tests/test_maintenance.py -q
rtk uv run pytest tests/test_cli.py -q
```

手动挂载示例：

```bash
rtk mkdir -p /tmp/ztierfs-hot /tmp/ztierfs-cold /tmp/ztierfs-mount
rtk uv run python -m ztierfs mount /tmp/ztierfs-hot /tmp/ztierfs-cold /tmp/ztierfs-mount \
  --database /tmp/ztierfs-metadata.sqlite3
```

卸载使用 macOS 工具：

```bash
rtk diskutil unmount force /tmp/ztierfs-mount
```

## 主要模块

- `ztierfs/cli.py`：解析 CLI 和挂载参数，启动 `macfusepy.FUSE`。
- `ztierfs/filesystem.py`、`ztierfs/fs_mixins.py`：组合 FUSE mixin，初始化元数据、块存储、文件内容服务、句柄表和锁表。
- `ztierfs/inode_fuse.py`：实现 macfusepy `InodeOperations` low-level 回调。
- `ztierfs/file_ops.py`、`ztierfs/namespace_ops.py`、`ztierfs/metadata_ops.py`、`ztierfs/access_control.py`、`ztierfs/inode_locks.py`、`ztierfs/locks.py`：文件内容、命名空间、POSIX 元数据、权限和锁语义。
- `ztierfs/metadata/`：SQLite schema、连接池、事务和元数据访问层。
- `ztierfs/file_content.py`：分块读写、稀疏区域、覆盖写、截断、读缓存、预读和引用计数更新。
- `ztierfs/block_store.py`、`ztierfs/block_layout.py`、`ztierfs/tier_access.py`：块压缩、内容寻址、原子写入、删除、copy-up 和冷热分层。
- `ztierfs/handles.py`：FUSE 文件句柄跟踪，保证 rename/unlink 后的打开文件语义。
- `ztierfs/maintenance/`：`fsck`、`scrub`、`stats`、`cleanup` 的检查、修复、报告和配置加载。

## 已知限制

- 尚未适合存放不可替代的生产数据。
- `fsck`/`scrub` 修复策略仍有限；被引用但缺失或损坏的块只报告，不自动伪造修复。
- 崩溃恢复测试已有基础矩阵，但仍需继续扩展 hardlink、xattr、复杂目录树、跨块多阶段写入、copy-up、cleanup 和引用计数更新等组合故障点。
- 并发一致性只面向单个 `ZTierFS` 进程；不要假设多个挂载进程可以安全共享同一个数据库和块目录。
- 尚无设备节点、高级 rename flag、加密、认证、配额或正式 schema 迁移层。
