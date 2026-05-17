"""ztierfs 命令行入口：解析参数并分派子命令。

子命令：``mount``（FUSE 挂载，含冷热层、块大小、压缩、SQLite/FUSE 调优等）、
``fsck`` / ``scrub``（元数据与块一致性；scrub 另校验 payload 可读）、
``stats``（空间与引用等统计）、``cleanup``（清理 copy-up 后遗留的冷层副本）。
"""

import argparse
import json
import sys
from pathlib import Path

from loguru import logger

from .console import ConsoleOutputConfig, console_output
from .constants import (
    CHUNK_SIZE,
    DEFAULT_COMPRESSION_MIN_BYTES,
    DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS,
    DEFAULT_FUSE_METADATA_CACHE_SECONDS,
    DEFAULT_FUSE_IOSIZE,
    DEFAULT_HOT_CACHE_MAX_BYTES,
    DEFAULT_HOT_CACHE_MIN_BYTES,
    DEFAULT_INLINE_MAX_BYTES,
    DEFAULT_MIN_HOT_AGE_SECONDS,
    DEFAULT_PROTECTED_PREFIX_CHUNKS,
    DEFAULT_READ_CACHE_BYTES,
    DEFAULT_READAHEAD_BLOCKS,
    DEFAULT_READAHEAD_WORKERS,
)
from .filesystem import ZTierFS
from .maintenance import (
    collect_stats,
    cleanup_promoted_cold_copies,
    report_to_text,
    run_fsck,
    run_scrub,
    stats_to_text,
)


def _parse_size(value: str) -> int:
    """将带单位的大小字符串（如 ``10g``）解析为字节数。"""
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    normalized = value.strip().lower()
    if normalized[-1:] in units:
        return int(float(normalized[:-1]) * units[normalized[-1]])
    return int(normalized)


def _parse_nonnegative_float(value: str) -> float:
    """解析非负浮点数；若为负则抛出 ``ArgumentTypeError``。"""
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _parse_nonnegative_int(value: str) -> int:
    """解析非负整数；若为负则抛出 ``ArgumentTypeError``。"""
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def main(argv: list[str] | None = None) -> None:
    """解析 ``argv``（默认同 ``sys.argv``），配置日志与会话输出，执行对应子命令处理函数。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    with console_output(_console_config_from_args(args)):
        args.handler(args)


def _build_parser() -> argparse.ArgumentParser:
    """构建根解析器及 ``mount`` / ``fsck`` / ``scrub`` / ``stats`` / ``cleanup`` 子解析器。"""
    parser = argparse.ArgumentParser(description="SQLite + zstd 分片去重 FUSE 文件系统")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mount = subparsers.add_parser("mount", help="挂载文件系统")
    _add_tier_args(mount)
    mount.add_argument("mountpoint", help="挂载点")
    _add_database_arg(mount)
    _add_logging_args(mount, include_debug=True)
    mount.add_argument(
        "--hot-cache",
        type=_parse_size,
        default=DEFAULT_HOT_CACHE_MAX_BYTES,
        help="热层高水位容量，例如 10g",
    )
    mount.add_argument(
        "--hot-cache-min",
        type=_parse_size,
        default=None,
        help=f"触发降级后迁移到的热层低水位，默认不超过 {DEFAULT_HOT_CACHE_MIN_BYTES}",
    )
    mount.add_argument(
        "--protected-prefix-chunks",
        type=int,
        default=DEFAULT_PROTECTED_PREFIX_CHUNKS,
        help="每个文件开头保留在热层的块数量",
    )
    mount.add_argument(
        "--min-hot-age",
        type=int,
        default=DEFAULT_MIN_HOT_AGE_SECONDS,
        help="块至少多久未读后才允许降级，单位秒，默认 86400",
    )
    mount.add_argument(
        "--cold-copy-cleanup-age",
        type=int,
        default=DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS,
        help="提升到热层后冷层副本至少保留多久，单位秒，默认 0",
    )
    mount.add_argument(
        "--chunk-size", type=_parse_size, default=CHUNK_SIZE, help="块大小，默认 1m"
    )
    mount.add_argument(
        "--inline-max",
        type=_parse_size,
        default=DEFAULT_INLINE_MAX_BYTES,
        help="处理后 payload 不超过该大小的块内联到 SQLite；设为 0 禁用，默认 32k",
    )
    mount.add_argument(
        "--read-cache",
        type=_parse_size,
        default=DEFAULT_READ_CACHE_BYTES,
        help="解码后内容寻址块的内存 LRU 缓存容量；设为 0 禁用，默认 128m",
    )
    mount.add_argument(
        "--readahead-blocks",
        type=int,
        default=DEFAULT_READAHEAD_BLOCKS,
        help="检测到顺序读取时后台预读的后续块数，默认 1；设为 0 禁用",
    )
    mount.add_argument(
        "--readahead-workers",
        type=int,
        default=DEFAULT_READAHEAD_WORKERS,
        help="后台预读线程数，默认 1；设为 0 禁用预读",
    )
    mount.add_argument(
        "--zstd-level",
        type=int,
        default=None,
        help="zstd 压缩等级，默认使用标准库默认值",
    )
    mount.add_argument(
        "--compression-min",
        type=_parse_size,
        default=DEFAULT_COMPRESSION_MIN_BYTES,
        help="小于该大小的 payload 跳过 zstd 压缩尝试，默认 4k",
    )
    mount.add_argument(
        "--sqlite-synchronous",
        choices=["FULL", "NORMAL", "OFF"],
        default="NORMAL",
        help="SQLite synchronous 模式，默认 NORMAL；FULL 更保守，OFF 仅用于明确性能取舍",
    )
    mount.add_argument(
        "--update-config",
        action="store_true",
        help="允许用本次挂载参数重写数据库中的本机存储路径配置",
    )
    mount.add_argument(
        "--iosize",
        type=_parse_size,
        default=DEFAULT_FUSE_IOSIZE,
        help="macFUSE 单次 I/O 请求大小，例如 4m；默认与内部块大小一致，调大可能触发 Finder 大文件复制错误",
    )
    mount.add_argument(
        "--metadata-cache",
        type=_parse_nonnegative_float,
        default=DEFAULT_FUSE_METADATA_CACHE_SECONDS,
        help="内核缓存 inode 属性和目录项的秒数；默认 5，设为 0 关闭缓存",
    )
    mount.add_argument(
        "--defer-permissions",
        action="store_true",
        help="把 access(2) 权限判断交回 ztierfs；默认由内核按返回的 POSIX 属性判断以减少 access 回调",
    )
    mount.add_argument(
        "--fuse-loop-clone-fd",
        action="store_true",
        help="启用 libfuse 多线程 loop 的 clone_fd 模式；默认关闭，适合压测高并发请求时显式尝试",
    )
    mount.add_argument(
        "--fuse-loop-max-idle-threads",
        type=_parse_nonnegative_int,
        default=10,
        help="libfuse 多线程 loop 保留的最大空闲线程数，默认 10",
    )
    mount.add_argument(
        "--profile-interval",
        type=float,
        default=0,
        help="每隔指定秒数输出累计性能统计；设为 0 禁用，默认禁用",
    )
    mount.add_argument("--background", action="store_true", help="后台运行")
    mount.add_argument(
        "--volname",
        default=None,
        help="Finder 中显示的卷名，默认使用挂载点目录名",
    )
    mount.set_defaults(handler=_run_mount)

    fsck = subparsers.add_parser("fsck", help="检查元数据与块文件一致性")
    _add_maintenance_args(fsck)
    fsck.set_defaults(handler=_run_fsck_command)

    scrub = subparsers.add_parser("scrub", help="检查一致性并读取校验 inline/热层 payload")
    _add_maintenance_args(scrub)
    scrub.add_argument(
        "--include-cold",
        action="store_true",
        help="同时读取冷层块 payload；远程冷层可能因此下载完整冷层数据",
    )
    scrub.set_defaults(handler=_run_scrub_command)

    stats = subparsers.add_parser("stats", help="输出文件系统统计信息")
    _add_maintenance_storage_args(stats)
    _add_database_arg(stats)
    _add_logging_args(stats)
    _add_config_override_args(stats)
    stats.add_argument("--json", action="store_true", help="以 JSON 输出")
    stats.set_defaults(handler=_run_stats_command)

    cleanup = subparsers.add_parser("cleanup", help="整理已提升块遗留的冷层副本")
    _add_maintenance_storage_args(cleanup)
    _add_database_arg(cleanup)
    _add_logging_args(cleanup)
    _add_config_override_args(cleanup)
    cleanup.add_argument(
        "--age",
        type=int,
        default=DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS,
        help="只删除已保留至少这么多秒的冷层副本",
    )
    cleanup.add_argument("--json", action="store_true", help="以 JSON 输出")
    cleanup.set_defaults(handler=_run_cleanup_command)
    return parser


def _add_tier_args(parser: argparse.ArgumentParser) -> None:
    """为 ``mount`` 注册热层、冷层两个位置参数。"""
    parser.add_argument("tier1", help="第一层热块目录")
    parser.add_argument("tier2", help="第二层冷块目录")


def _add_maintenance_storage_args(parser: argparse.ArgumentParser) -> None:
    """注册维护子命令的位置参数：元数据路径，及可选的冷层路径（救援/显式层路径）。"""
    parser.add_argument(
        "path",
        help="SQLite 元数据文件；也可作为救援形式传热层目录并同时传 cold-tier",
    )
    parser.add_argument(
        "tier2",
        nargs="?",
        help="救援形式下的第二层冷块目录；省略时 path 解释为 SQLite 元数据文件",
    )


def _add_database_arg(parser: argparse.ArgumentParser) -> None:
    """注册 ``--database``，覆盖默认放在热层目录下的 SQLite 元数据路径。"""
    parser.add_argument("--database", help="SQLite 元数据文件，默认放在第一层目录")


def _add_maintenance_args(parser: argparse.ArgumentParser) -> None:
    """组合存储路径、数据库、日志、配置覆盖，以及 ``fsck``/``scrub`` 的 ``--repair``/``--json``。"""
    _add_maintenance_storage_args(parser)
    _add_database_arg(parser)
    _add_logging_args(parser)
    _add_config_override_args(parser)
    parser.add_argument("--repair", action="store_true", help="执行确定安全的自动修复")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")


def _add_config_override_args(parser: argparse.ArgumentParser) -> None:
    """注册与数据库记录不一致时的 ``--allow-config-mismatch`` 与路径重写 ``--update-config``。"""
    parser.add_argument(
        "--allow-config-mismatch",
        action="store_true",
        help="救援形式下允许显式热/冷层与数据库记录不一致，且不改写数据库配置",
    )
    parser.add_argument(
        "--update-config",
        action="store_true",
        help="救援或迁移时用显式热/冷层重写数据库中的本机存储路径配置",
    )


def _add_logging_args(
    parser: argparse.ArgumentParser, *, include_debug: bool = False
) -> None:
    """注册 ``--log-level``、``--log-file``；``include_debug`` 为真时另加 ``mount`` 专用 ``--debug``。"""
    parser.add_argument(
        "--log-level",
        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="控制台日志级别，默认 INFO",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="可选日志文件路径；默认不写文件日志",
    )
    if include_debug:
        parser.add_argument(
            "--debug",
            action="store_true",
            help="启用 DEBUG 日志并输出 FUSE 回调日志",
        )


def _console_config_from_args(args: argparse.Namespace) -> ConsoleOutputConfig:
    """根据解析结果构造 ``ConsoleOutputConfig``（``--debug`` 时提升日志级别）。"""
    logger_level = "DEBUG" if getattr(args, "debug", False) else args.log_level
    return ConsoleOutputConfig(logger_level=logger_level, log_file=args.log_file)


def _run_mount(args: argparse.Namespace) -> None:
    """按参数构造 ``ZTierFS`` 并以给定挂载选项启动 macFUSE。"""
    from macfusepy import FUSE

    logger.info(
        "准备挂载文件系统：热层={}，冷层={}，挂载点={}",
        args.tier1,
        args.tier2,
        args.mountpoint,
    )
    fs = ZTierFS(
        args.tier1,
        args.tier2,
        args.database,
        chunk_size=args.chunk_size,
        hot_cache_max_bytes=args.hot_cache,
        hot_cache_min_bytes=args.hot_cache_min,
        protected_prefix_chunks=args.protected_prefix_chunks,
        min_hot_age_seconds=args.min_hot_age,
        cold_copy_cleanup_age_seconds=args.cold_copy_cleanup_age,
        compression_level=args.zstd_level,
        compression_min_bytes=args.compression_min,
        inline_max_bytes=args.inline_max,
        read_cache_bytes=args.read_cache,
        readahead_blocks=args.readahead_blocks,
        readahead_workers=args.readahead_workers,
        sqlite_synchronous=args.sqlite_synchronous,
        update_config=args.update_config,
        profile_interval_seconds=args.profile_interval,
    )
    volname = args.volname or Path(args.mountpoint).name or "ztierfs"
    FUSE(
        fs,
        args.mountpoint,
        foreground=not args.background,
        allow_other=True,
        local=True,
        iosize=args.iosize,
        kernel_permissions=not args.defer_permissions,
        attr_timeout=args.metadata_cache,
        entry_timeout=args.metadata_cache,
        loop_clone_fd=args.fuse_loop_clone_fd,
        loop_max_idle_threads=args.fuse_loop_max_idle_threads,
        volname=volname,
    )


def _run_fsck_command(args: argparse.Namespace) -> None:
    """运行 ``run_fsck``，按文本或 JSON 输出报告；存在未自动修复项时以退出码 1 结束。"""
    logger.info("开始执行 fsck：repair={}", args.repair)
    report = run_fsck(
        args.path,
        args.tier2,
        args.database,
        repair=args.repair,
        allow_config_mismatch=args.allow_config_mismatch,
        update_config=args.update_config,
    )
    _emit_check_report(report, json_output=args.json)


def _run_scrub_command(args: argparse.Namespace) -> None:
    """运行 ``run_scrub``（读校验块内容），输出与退出约定同 ``_run_fsck_command``。"""
    logger.info("开始执行 scrub：repair={}", args.repair)
    report = run_scrub(
        args.path,
        args.tier2,
        args.database,
        repair=args.repair,
        allow_config_mismatch=args.allow_config_mismatch,
        update_config=args.update_config,
        include_cold=args.include_cold,
    )
    _emit_check_report(report, json_output=args.json)


def _run_stats_command(args: argparse.Namespace) -> None:
    """调用 ``collect_stats``，以可读摘要或 JSON 打印统计（块、 inode、层占用等由实现决定）。"""
    logger.info("开始收集文件系统统计信息")
    report = collect_stats(
        args.path,
        args.tier2,
        args.database,
        allow_config_mismatch=args.allow_config_mismatch,
        update_config=args.update_config,
    )
    if args.json:
        _emit_stdout(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        _emit_stdout(stats_to_text(report))


def _run_cleanup_command(args: argparse.Namespace) -> None:
    """调用 ``cleanup_promoted_cold_copies``，按 ``--age`` 等条件删除过期的冷层残留副本并汇报数量。"""
    logger.info("开始清理已提升块遗留的冷层副本：min_age_seconds={}", args.age)
    report = cleanup_promoted_cold_copies(
        args.path,
        args.tier2,
        args.database,
        min_age_seconds=args.age,
        allow_config_mismatch=args.allow_config_mismatch,
        update_config=args.update_config,
    )
    if args.json:
        _emit_stdout(
            json.dumps(
                {
                    "removed_cold_copies": report.removed,
                    "skipped_cold_copies": report.skipped,
                    "removed_pending_deletions": report.pending_removed,
                    "skipped_pending_deletions": report.pending_skipped,
                },
                sort_keys=True,
            )
        )
    else:
        _emit_stdout(
            f"cleanup: removed {report.removed} promoted cold copy/copies, "
            f"skipped {report.skipped}, removed {report.pending_removed} pending deletion(s), "
            f"skipped {report.pending_skipped}"
        )


def _emit_check_report(report, *, json_output: bool) -> None:
    """输出 fsck/scrub 报告（文本或 JSON）；若报告中仍有未修复问题则 ``SystemExit(1)``。"""
    if json_output:
        _emit_stdout(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        _emit_stdout(report_to_text(report))
    if report.has_unrepaired:
        raise SystemExit(1)


def _emit_stdout(text: str) -> None:
    """将字符串写入标准输出，若末尾无换行则追加 ``\\n``。"""
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
