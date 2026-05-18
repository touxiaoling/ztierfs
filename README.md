# ztierfs

`ztierfs` 是一个可写 FUSE 文件系统，使用 zstd 压缩的内容寻址块，支持去重、压缩和冷热分层存储数据。它的目标是成为一个真实可用的文件系统。

> **仅支持 macOS。** ztierfs 通过 macFUSE 挂载，依赖 macOS 专属的系统调用和文件语义，不维护 Linux/FUSE3 路径。运行前必须先安装 [macFUSE](https://macfuse.github.io/) 并在「系统设置 → 隐私与安全性」中允许其系统扩展，重启后生效。没有安装 macFUSE 时，`uv sync` 仍会成功，但 `mount` 子命令会在加载内核扩展时失败。

这个项目不应被当作一次性 demo。当前实现仍处于早期阶段，也尚未真实投入使用，因此可以接受破坏性的存储格式、schema、CLI 和内部 API 变更。设计和评审应优先考虑长期的数据正确性、可运维性和可恢复性，而不是兼容当前未发布状态。

## 功能

- 通过 FUSE 暴露一个可挂载的文件系统。
- 使用 SQLite 持久化目录和文件元数据。
- 将文件内容切分为固定大小的块。
- 使用 SHA-256 存储块，让相同块可以去重。
- 对未按后缀跳过且达到大小阈值的块使用 zstd 压缩。
- 将处理后 payload 不超过阈值的小块直接内联到 SQLite，避免为小文件创建大量块文件。
- 对常见已压缩后缀跳过压缩，例如 `.jpg`、`.png`、`.zip`、`.zst`、`.mp4` 和 `.pdf`。
- 面向“本机热层 + rclone 挂载网盘冷层”的分层策略：热层使用高/低水位，文件开头块优先留热，冷读提升使用 copy-up，并把冷层视为昂贵但持久的层，尽量减少冷层探测、读取、写入和删除。
- 保留重要的打开文件语义，例如 rename 或 unlink 后仍可通过既有句柄读取原文件。
- 支持符号链接、普通文件硬链接、macOS `clonefile`、扩展属性、基于 mode/uid/gid 的权限检查，以及进程内 POSIX advisory 文件锁。

## 项目状态

`ztierfs` 目前还不适合存放不可替代的生产数据。它已经覆盖了核心文件操作、目录操作、分块、去重、持久化、真实挂载行为、基础维护工具，以及一组“SQLite 元数据 + 块文件”中途失败场景的崩溃恢复测试，但仍缺少一些生产级能力：

- 更完整的崩溃恢复测试矩阵，继续扩展 hardlink、扩展属性、复杂目录树、跨块多阶段写入、copy-up、cleanup、引用计数更新和块删除等路径上的更多故障点组合；
- 更完整的 fsck/scrub 修复策略，包括复杂目录树修复、单挂载进程异常退出后的不一致状态识别、修复前备份和可回滚的修复日志；当前被引用但缺失或损坏的块只会报告为不可自动修复；
- 更丰富的诊断、统计、清理和手动维护类管理命令，例如长期趋势、容量规划和分层策略调优；
- 未来如果开始承载真实数据，再补充面向已有元数据库的 schema 迁移策略。

在这些缺口补齐前，请只把它用于可以重新生成的数据。

当前磁盘格式没有兼容性承诺。如果后续重做 SQLite schema 或块目录布局，可以直接破坏旧的测试数据；等项目开始承载真实数据后，再引入正式迁移、修复和兼容策略。

## 环境要求

- macOS（Apple Silicon 或 Intel 均可），且已安装 [macFUSE](https://macfuse.github.io/)。Linux/FUSE3 不在支持范围内。
- Python `>=3.14`
- `uv`
- 冷层可以是本地目录，也可以是使用 rclone 挂载到本地路径的网盘（推荐 `--vfs-cache-mode full` 等配置）；ztierfs 不直接调用 rclone 接口，但设计上把冷层视为高延迟、高抖动、带本地缓存窗口的昂贵层，而不是普通低成本目录。

### 安装 macFUSE

macFUSE 必须在挂载之前安装好，否则 `mount` 子命令会因为内核扩展不可用而失败。推荐用 Homebrew：

```bash
brew install --cask macfuse
```

或者从 [macfuse.github.io](https://macfuse.github.io/) 下载 `.pkg` 安装包。安装后需要：

1. 打开「系统设置 → 隐私与安全性」，允许 `Benjamin Fleischer`（macFUSE 作者）的系统扩展；
2. 重启 macOS 让内核扩展加载生效；
3. 重启后可以用 `kextstat | grep -i fuse` 或 `mount -t macfuse` 验证扩展已加载。

每次升级 macOS 大版本后，可能需要重新允许 macFUSE 系统扩展并重启。

## 安装

```bash
uv sync
```

`uv sync` 只装 Python 依赖，不会替你安装或启用 macFUSE；如果跳过了上面的 macFUSE 步骤，单元测试可以跑，但真实挂载会失败。

## 手动挂载

分别为热层、冷层和挂载点创建目录：

```bash
mkdir -p /tmp/ztierfs-hot /tmp/ztierfs-cold /tmp/ztierfs-mount
```

启动文件系统：

```bash
uv run python -m ztierfs mount /tmp/ztierfs-hot /tmp/ztierfs-cold /tmp/ztierfs-mount \
  --database /tmp/ztierfs-metadata.sqlite3 \
  --hot-cache 10g \
  --hot-cache-min 8g \
  --chunk-size 4m
```

常用选项按主题分组：

存储与分层：

- `--database`：SQLite 元数据路径，默认是热层目录内的 `ztierfs.sqlite3`。
- `--hot-cache`：热层高水位，热层超过该容量后才触发降级。支持 `k`、`m`、`g`、`t` 后缀。
- `--hot-cache-min`：触发降级后迁移到的低水位，默认不超过 `8g`，也不会超过高水位。
- `--protected-prefix-chunks`：每个文件开头保留在热层的块数量，默认 `2`。这让大文件从头读取时能快速返回头部，同时给 rclone 冷层预读留出时间。
- `--min-hot-age`：块至少多久未读后才允许降级，单位秒，默认 `86400`。
- `--cold-copy-cleanup-age`：冷层块 copy-up 回热层后，冷层副本至少保留多久再允许 `cleanup` 删除，单位秒，默认 `0`。`cleanup` 需要手动调用，因此默认不会自动删除冷层副本。
- `--update-config`：允许用本次挂载命令的热/冷层路径重写数据库中记录的本机存储配置。

分块与压缩：

- `--chunk-size`：文件分块大小，默认 `4m`。
- `--inline-max`：处理后 payload 不超过该大小的块直接内联到 SQLite，默认 `32k`；设为 `0` 禁用。"处理后"指走 zstd 压缩的块使用压缩后体积，按后缀或大小阈值跳过压缩的块使用原始体积。
- `--zstd-level`：zstd 压缩等级，默认使用标准库默认值。
- `--compression-min`：小于该大小的 payload 跳过 zstd 压缩尝试，默认 `4k`。

读路径与缓存：

- `--read-cache`：解码后块的内存 LRU 缓存容量，默认 `128m`；设为 `0` 禁用。
- `--readahead-blocks`：检测到顺序读取时后台预读的后续块数，默认 `1`；设为 `0` 禁用。
- `--readahead-workers`：后台预读线程数，默认 `1`；设为 `0` 同样禁用预读。

底层块文件的字节缓存由上级文件系统和系统缓存负责，例如热层 APFS 的文件缓存，以及冷层 rclone VFS cache、本地缓存目录和 macOS 统一缓存。ztierfs 自己的 `--read-cache` 只缓存解码后的逻辑块，用来减少重复解压和短时间重复读的开销；它不是数据正确性机制，也不是冷层长期缓存。不要把它当成 rclone 或 APFS 缓存的替代品。

读目录、读符号链接和读文件只会延迟合并 inode atime 与块访问统计；达到阈值、后续写事务、`flush`/`fsync`/关闭文件系统或显式维护提交时再落库。这样目录扫描和小读不会为每次访问都打开 SQLite 写事务。

FUSE/内核交互：

- `--iosize`：传给 macFUSE 的单次 I/O 请求大小，默认与 `--chunk-size` 一致。调大可能触发 Finder 大文件复制错误。
- `--metadata-cache`：内核缓存 inode 属性和目录项的秒数，默认 `5`，设为 `0` 关闭缓存。
- `--defer-permissions`：把 `access(2)` 权限判断交回 ztierfs；默认让内核按返回的 POSIX 属性判断，以减少 access 回调。
- `--fuse-loop-clone-fd`：启用 libfuse 多线程 loop 的 `clone_fd` 模式，默认关闭；适合压测高并发请求时显式尝试。
- `--fuse-loop-max-idle-threads`：libfuse 多线程 loop 保留的最大空闲线程数，默认 `10`。
- `--volname`：Finder 中显示的卷名，默认使用挂载点目录名。
- `--background`：让挂载进程在后台运行。默认以前台运行，适合开发和问题诊断。

可靠性与诊断：

- `--sqlite-synchronous`：SQLite synchronous 模式，可选 `FULL`、`NORMAL`（默认）或 `OFF`。`FULL` 更保守，`OFF` 仅用于明确性能取舍。
- `--profile-interval`：每隔指定秒数输出累计性能统计，默认 `0` 表示禁用。
- `--log-level`：控制台日志级别，支持 `TRACE`、`DEBUG`、`INFO`、`WARNING`、`ERROR`，默认 `INFO`。
- `--log-file`：可选日志文件路径。默认不写文件日志；指定后用 loguru 写入 UTF-8 文件日志，并保留 rich 控制台日志输出。
- `--debug`：挂载命令的便捷调试开关，等同于把日志级别提升到 `DEBUG`，用于查看更详细的 FUSE 和内部操作日志。

## 维护命令

`fsck` 检查 SQLite 元数据与块文件的一致性。维护命令的首选入口是 SQLite 元数据文件；数据库会记录热层、冷层的绝对路径，因此在线和离线维护都只需要传一个路径。默认只报告问题；加上 `--repair` 后，会执行确定安全的修复，例如重算块引用计数、修正错误层级、删除无引用块记录和孤儿块文件。`refcount=0` 且仍有 `cold_present=1` 的 cold garbage 是合法维护态，不会被当作普通无引用块错误；如果这类冷层垃圾对应文件已经缺失，`fsck --repair` 会报告并删除这条垃圾元数据。被引用但缺失或损坏的块不会被伪造修复，只会报告错误。冷层使用 rclone 挂载时，如果检查期间冷层或某个块临时不可访问，维护命令会报告 `cold_tier_unavailable` 或 `block_payload_unavailable`，并跳过依赖这次冷层观察的修复，不会把它当作缺失或损坏处理。

ztierfs 的默认可靠性假设是：只要块文件已经通过原子写入、`fsync` 和元数据提交成功落盘，后续字节保持正确应由下层文件系统、磁盘、rclone/VFS cache 或网盘服务负责。ztierfs 主要负责自己的写入顺序、SQLite 元数据、引用计数、冷热层 presence 和崩溃后可诊断状态。块文件可能丢失或暂时不可访问，这是 ztierfs 要报告的状态；持续 bit-rot 或底层静默损坏不进入普通读写热路径的默认假设。

```bash
uv run python -m ztierfs fsck /tmp/ztierfs-metadata.sqlite3
```

`scrub` 在 `fsck` 的基础上读取并校验 SQLite 内联 payload 和热层块 payload，能发现压缩数据损坏、存储大小不匹配和解码后大小异常。默认不会读取冷层块 payload，因此适合作为远程冷层场景的常规维护命令；需要明确诊断冷层内容时加 `--include-cold`，这可能读取或下载完整冷层数据。只有已经读到 payload 后解码或大小不匹配才会报告损坏；rclone 下载失败这类读取错误会报告为暂时不可用。默认维护策略不把冷层内容校验作为常规任务，避免为了低概率损坏检查而持续消耗网盘缓存和远端带宽：

```bash
uv run python -m ztierfs scrub /tmp/ztierfs-metadata.sqlite3
uv run python -m ztierfs scrub /tmp/ztierfs-metadata.sqlite3 --include-cold
```

`stats` 输出 inode、目录项、chunk、块分布、存储占用和维护队列摘要。块统计会区分热层、冷层以及冷热两层都有副本的块；冷层可以是 rclone 或其它网盘本地挂载，`blocks.cold_present` 是 ztierfs 正常路径使用的权威 presence 索引，不表示每次统计或迁移都实时探测远端 payload。`maintenance.pending_deletions` 显示仍等待物理清理的 payload 队列数量，`cold_garbage`、`cold_garbage_bytes` 和 `oldest_cold_garbage_ns` 用于观察可复用但可清理的冷层无引用块。维护命令都支持 `--json`，方便脚本消费：

```bash
uv run python -m ztierfs stats /tmp/ztierfs-metadata.sqlite3 --json
```

普通写事务提交后只会按小预算清理一部分热层 `pending_deletions`，大量 unlink、truncate 或 rename overwrite 不会在返回前同步清空所有物理 GC 队列。剩余队列是可恢复状态，由后续提交或 `cleanup` 继续处理。冷层无引用块按维护对象处理：前台路径不实时删除冷层垃圾，冷层是否已有同 hash 块优先以 SQLite 记录为依据，而不是读取冷层 payload 重新判断。

`cleanup` 删除已经提升到热层、并且超过保留时间的冷层副本，并 drain 热层 `pending_deletions`。它还会把超过冷层 GC 保留期的 `refcount=0` cold garbage 作为显式维护对象批量删除；默认保留期是 30 天，可用 `--cold-gc-age` 调整，用 `--max-cold-deletes` 控制单次删除数量，用 `--dry-run` 先查看候选而不删除。冷层临时不可访问时会跳过对应副本或 cold garbage，并保留 SQLite 元数据，输出中的 `skipped_cold_copies`、`skipped_cold_unavailable`、`removed_cold_garbage`、`reclaimed_cold_bytes` 和 `remaining_cold_garbage` 可用于脚本诊断；待删除热层 payload 路径暂不可用时会保留队列项，并在 JSON 输出中通过 `pending_deletion_unavailable` 计数：

```bash
uv run python -m ztierfs cleanup /tmp/ztierfs-metadata.sqlite3 --age 86400
uv run python -m ztierfs cleanup /tmp/ztierfs-metadata.sqlite3 --cold-gc-age 2592000 --max-cold-deletes 100 --dry-run --json
```

如果整套存储目录被手工移动，先用新的热/冷层路径挂载一次并显式传 `--update-config`，让数据库中的本机路径配置更新到新位置；之后维护命令仍只需要传 SQLite 元数据库路径。挂载命令会校验同一个数据库不能用不同热/冷层打开，除非显式传入 `--update-config`。

所有命令的日志默认写到 stderr，命令结果继续写 stdout，因此 `--json` 输出可以直接交给脚本解析。需要保留诊断日志时，可以为维护命令同样添加 `--log-file /path/to/ztierfs.log`，必要时再配合 `--log-level DEBUG`。

在 macOS 上使用 `diskutil` 卸载：

```bash
diskutil unmount force /tmp/ztierfs-mount
```

## 存储布局

文件系统将元数据和内容分开存储：

```text
hot-tier/
  ztierfs.sqlite3        # 未提供 --database 时的默认元数据库
  blocks/
    aa/bb/<sha256>      # 热层内容寻址块文件

cold-tier/
  blocks/
    aa/bb/<sha256>      # 冷层内容寻址块文件
```

SQLite 表：

- `inodes`：文件、目录和符号链接的 POSIX 元数据。
- `dir_entries`：目录项到 inode 的映射，用于支持 hardlink。
- `inode_xattrs`：挂在 inode 上的扩展属性。
- `blocks`：块元数据、首选层级、冷热层 presence、压缩状态、大小、引用计数、访问时间、读取频率和迁移时间。
- `block_payloads`：inline 块的 payload，直接存入 SQLite。
- `file_chunks`：从 `file_id`（文件 inode）+ `chunk_index` 到块 `hash` 和 `size` 的映射。
- `pending_deletions`：等待提交后或维护命令继续物理删除的 tiered 块文件队列，按 `digest` 和 `tier` 去重。
- `filesystem_config`：记录热层和冷层的绝对路径，便于维护命令只凭数据库定位整套存储。

所有非空普通文件内容都通过 `file_chunks -> blocks` 表达；小文件也会建立 chunk 映射，只是目标块可为 `inline`。块记录有两种存储形态：`inline` 块的 payload 直接关联到 `block_payloads`，不参与冷层降级，且 `preferred_tier = 0`、`hot_present = 0`、`cold_present = 0`；`tiered` 块的文件路径由 SHA-256 digest 派生，按 `aa/bb/<sha256>` 分桶保存在热层或冷层，冷热副本 presence 直接记录在 `blocks.hot_present` 和 `blocks.cold_present`。`block_records` view 会把块元数据和 inline payload 聚合成维护工具使用的统一视图。

## 开发说明

核心模块：

- `ztierfs/filesystem.py` + `ztierfs/fs_mixins.py`：组合 FUSE mixin，初始化元数据、块存储、文件内容服务、句柄表和锁表。
- `ztierfs/inode_fuse.py`：实现 macfusepy `InodeOperations` low-level 回调，直接用 inode、parent inode 和目录项名处理真实 FUSE 请求。
- `ztierfs/file_ops.py`、`ztierfs/namespace_ops.py`、`ztierfs/metadata_ops.py`、`ztierfs/access_control.py`、`ztierfs/inode_locks.py`、`ztierfs/locks.py`：按职责拆分的 `ZTierFS` mixin，实现文件内容、命名空间、POSIX 元数据、权限、inode 级锁和 POSIX advisory 锁。
- `ztierfs/metadata/`：SQLite schema、连接池、读写事务，以及命名空间、块、chunk 和访问统计的元数据访问层。
- `ztierfs/file_content.py`：块级文件读写、稀疏区域、截断、读缓存与预读、以及引用计数更新。
- `ztierfs/block_store.py` + `ztierfs/block_layout.py` + `ztierfs/tier_access.py`：块压缩、原子写入、删除、copy-up 提升、策略性降级和层级回退。
- `ztierfs/handles.py`、`ztierfs/pathing.py`、`ztierfs/console.py`、`ztierfs/perf.py`：句柄跟踪、路径规范化与压缩后缀判断、控制台日志输出和性能统计采样。
- `ztierfs/maintenance/`：`fsck`、`scrub`、`stats`、`cleanup` 共享的检查器、修复器、报告与配置加载。

修改文件系统行为时，优先增加描述可观察文件系统语义的测试。当前已经覆盖的重要行为包括稀疏写、跨块覆盖、去重、`clonefile` 块引用克隆、引用计数清理、已打开后 unlink 的文件、rename 覆盖已打开目标、重新打开后的持久化，以及真实挂载下的读写闭环。涉及 SQLite 元数据和块文件同时变化的路径，还应补充故障注入测试；现有崩溃恢复测试覆盖了写入后事务回滚留下孤儿块、冷热降级中途失败后的层级修复，unlink、truncate、覆盖写和 rename 覆盖在块文件删除后事务回滚时的不可修复缺块报告，以及 hardlink、xattr、复杂目录树 rename、跨块覆盖写、copy-up、cleanup 和引用计数更新路径上的事务回滚/修复行为。

性能改动应结合 `tests/test_benchmarks.py` 里的 workload 观察 small file read/write、readdir with attrs、stat many files、large sequential write、sequential block read、random read、full block overwrite、cross-block partial write、rename/unlink batch 和 cold copy-up。除总耗时外，优先关注 SQLite execute/executemany 次数、块 IO 字节数、zstd encode/decode 时间、after-commit GC 清理数量和 pending deletion 残留数量。

## 运行测试

运行不需要真实挂载的单元测试和 inode-first 适配器测试：

```bash
uv run pytest -m "not integration"
```

当宿主机具备 FUSE 支持和挂载权限时，运行真实 FUSE 挂载测试：

```bash
uv run pytest -m integration
```

## 数据安全说明

`ztierfs` 已在相关路径使用原子块写入，并对块文件和目录执行 fsync，也已有多路径崩溃注入测试约束 SQLite 事务回滚与块文件副作用的恢复行为。但项目尚未声明完整崩溃一致性，同时更新 SQLite 行和块文件的代码需要尤其谨慎。在崩溃注入矩阵继续扩展、`fsck`/`scrub` 修复策略覆盖到带备份与回滚日志的复杂场景之前，请只把它用于可以重新生成的数据。
