# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Cursor Task 与子代理

执行用户任务时，**优先用 Task 子代理**承担可并行的重活，避免主对话上下文被海量检索输出占满。

**适合交给子代理：**跨目录探索与关键词定位、多步只读调查、可写清验收标准的独立子任务、长时间或需密切跟进的 shell/构建（按需后台运行）。

**不必强行子代理：**目标与改动点已明确的一两处编辑、单文件小补丁、读一两个已知路径即可回答的问题。

**模型 `gpt-5.5-high`：**当任务明显依赖复杂推理、多方案权衡、或大范围理解后再下结论时，可在发起 Task 时指定 **`gpt-5.5-high`**；不要为省事先滥用。若用户点名其它模型 slug，仅使用平台当前允许列表中的值，不可用则如实说明。

**约束：**子代理看不到本会话的隐含约定，须在 Task 的说明里写清工作目录、目标、交付格式与仓库级约束（例如本文件中的支持策略与测试要求）。

## 2. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 3. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 4. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 5. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

# 项目上下文

## 项目定位

`ztierfs` 正在被构建为一个真实可写的 FUSE 文件系统，而不是演示项目。处理这个仓库时，要把正确性、持久性、恢复行为和 POSIX 文件系统语义当作产品要求。不要为了让测试通过而简化行为；如果改动可能破坏用户数据、丢失元数据，或违背常见文件系统预期，就应当重新设计。

当前项目尚未真实投入使用，也没有需要兼容的生产数据。因此允许破坏性变更，包括重做 SQLite schema、块存储布局、CLI 参数和内部 API。优先选择长期正确、简单、可维护的设计，不要为了兼容未发布状态而堆叠迁移层或兼容 shim。

这个文件系统只支持 macOS/macFUSE。它把命名空间元数据存储在 SQLite 中，把文件内容存储为内容寻址块，并将块分布在本机热层和 rclone 挂载到本地的网盘冷层。块可以经过 zstd 压缩、按 SHA-256 去重、在读取冷层时 copy-up 到热层，并在热层容量压力下按高/低水位和热度策略降级到冷层。

## 常用命令

本地开发使用 `uv`。

```bash
uv sync
uv run pytest -m "not integration"
uv run pytest -m integration
```

本机开发环境有真实可用的 macFUSE，并且当前用户可运行挂载集成测试。修改挂载路径、FUSE 回调、CLI 挂载参数或真实 POSIX 行为时，优先在单元测试通过后补跑 `uv run pytest -m integration`。单元测试仍使用 macfusepy 的操作适配器，不应依赖真实挂载。

手动挂载示例：

```bash
mkdir -p /tmp/ztierfs-hot /tmp/ztierfs-cold /tmp/ztierfs-mount
uv run python -m ztierfs mount /tmp/ztierfs-hot /tmp/ztierfs-cold /tmp/ztierfs-mount 
```

使用 macOS 的 `diskutil unmount force /tmp/ztierfs-mount` 卸载。

## 架构

- `ztierfs/cli.py` 解析挂载选项，并用 `ZTierFS` 启动 `macfusepy.FUSE`。
- `ztierfs/filesystem.py` 组合 FUSE mixin，初始化 `MetadataStore`、`BlockStore`、`FileContentService`、句柄表和进程内锁表。
- `ztierfs/inode_fuse.py` 实现 macfusepy `InodeOperations` low-level 回调，直接用 inode、parent inode 和目录项名处理真实 FUSE 请求。
- `ztierfs/file_ops.py`、`ztierfs/namespace_ops.py`、`ztierfs/metadata_ops.py`、`ztierfs/access_control.py`、`ztierfs/inode_locks.py` 按职责实现文件内容、命名空间、POSIX 元数据、权限检查和 inode 级锁。
- `ztierfs/metadata/` 负责 SQLite schema、连接池、读写事务，以及 `inodes`、`dir_entries`、`inode_xattrs`、`blocks`、`block_locations`、`block_payloads`、`file_chunks` 的元数据操作。
- `ztierfs/file_content.py` 实现稀疏写、局部覆盖、截断、分块读写、块引用计数更新，以及文件大小和时间戳更新。
- `ztierfs/block_store.py` 负责内容寻址块文件、压缩、原子写入、冷热层迁移、块删除，以及当元数据指向错误层级时的恢复。
- `ztierfs/handles.py` 跟踪 FUSE 文件句柄，确保已 rename 或 unlink 的打开文件在 release 前仍可读取。
- `ztierfs/pathing.py` 规范化绝对 POSIX 路径，并控制哪些后缀跳过压缩。

## 当前文件系统语义

除非用户明确要求设计变更，否则保持这些行为：

- 支持普通文件、目录、符号链接和普通文件硬链接；非普通文件的 `mknod` 会被拒绝。
- 元数据和块记录通过 SQLite 在重新打开后保持持久化。
- 文件数据会被切分为固定大小的块。缺失块读回时表现为以零填充的稀疏区域。
- 相同块去重为一个带引用计数的块。
- 支持 macOS `clonefile`：复制文件节点、xattr 和 chunk 引用，增加块引用计数，不重新压缩或重写块 payload。
- 处理后 payload 不超过阈值的小块可以内联到 SQLite `block_payloads`；inline 块不参与冷热层降级。
- 多个 hardlink 目录项共享同一个 inode、文件内容、xattr、权限和时间戳。
- 扩展属性跟随 inode，因此 hardlink 之间可见同一组 xattr。
- 基于 FUSE 请求上下文中的 uid/gid 执行基础 POSIX mode 权限检查。
- 支持当前进程内的 POSIX advisory 文件锁；不要假设多个独立挂载进程之间会共享锁状态。
- 块先写入热层；当热层存储超过高水位时，按读取时间、读取频率和文件块位置选择候选降级，直到低水位或没有合适候选。
- 每个文件开头的若干块默认保护在热层，避免大文件从头读取时被冷层延迟卡住。
- 读取冷层块时，如果块大小适合，会 copy-up 回热层，并暂时保留冷层副本；后续通过维护清理删除过期冷层副本。
- 对已知通常已经压缩的后缀跳过压缩；如果 zstd 输出不比原始数据更小，也跳过压缩。
- 打开的文件句柄在 rename 或 unlink 后继续指向原 inode；已 unlink 但仍打开的文件会在最后一个句柄 release 后清理。

## 开发规则

- 保持改动小而且由行为驱动。为文件系统语义增加或更新测试，而不仅是测试实现细节。
- 谨慎处理事务边界。元数据更新、引用计数变化和块文件变更在错误情况下也必须保持一致。
- `MetadataStore` 使用显式读/写事务和连接池；不要在 read transaction 内开启 write transaction。
- 修改同时影响 SQLite 元数据和块文件的路径时，优先补充故障注入测试，模拟块文件已写/已删但 SQLite 事务回滚、冷热层迁移中途失败、引用计数未提交等情况。
- 不要引入临时路径解析逻辑；使用 `normalize_path`、`split_path` 和现有元数据查找辅助函数。
- 不要削弱 `BlockStore` 中的 fsync 或原子写入行为。块文件是用户数据。
- 不要假设所有测试都能在每台机器上运行。单元测试应独立于真实 FUSE 挂载，挂载测试应标记为 `integration`。
- 在真实使用前，SQLite schema 和磁盘布局可以破坏性调整；调整时同步更新测试和文档即可。等项目开始承载真实数据后，再把 schema 变更升级为迁移工作。
- 在测试或常见工具依赖的地方，保留类 POSIX 错误行为，例如 `ENOENT`、`EISDIR`、`ENOTDIR`、`ENOTEMPTY`、`EINVAL`、`EIO`。

## 已知限制

这些是当前工程限制，不是可以长期接受的捷径：

- 尚未支持设备节点、高级 rename flag，或多个独立挂载进程之间的全局锁协调。
- 已有基础 `fsck`/`scrub`/`stats`/`cleanup` 维护工具，但修复策略仍有限；被引用但缺失或损坏的块只报告为不可自动修复，尚无修复前备份、可回滚修复日志或复杂目录树修复策略。
- 已有多路径“SQLite 元数据 + 块文件”故障注入测试，覆盖写入孤儿块、冷热层降级中途失败，unlink/truncate/覆盖写/rename 覆盖在块文件删除后事务回滚的报告行为，以及 hardlink、xattr、复杂目录树 rename、跨块覆盖写、copy-up、cleanup 和引用计数更新路径上的事务回滚/修复行为；仍需继续扩展更多组合故障点和长期恢复策略。
- 并发目前只在单个 `ZTierFS` 进程内受保护；不要假设多个挂载可以安全共享同一个数据库和块目录。
- 尚无加密、认证或配额层。

