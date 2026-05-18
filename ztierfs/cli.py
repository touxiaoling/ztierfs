"""ztierfs 命令行入口：Typer 子命令与执行分派。"""

import json
import sys
from pathlib import Path
from typing import Annotated, Literal

import click
import typer
from loguru import logger
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from .console import ConsoleOutputConfig, console_output
from .constants import (
    CHUNK_SIZE,
    DEFAULT_COMPRESSION_MIN_BYTES,
    DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS,
    DEFAULT_COLD_GC_AGE_SECONDS,
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
    CheckReport,
    CleanupReport,
    StatsReport,
    collect_stats,
    cleanup_promoted_cold_copies,
    run_fsck,
    run_scrub,
)

LogLevel = Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"]
SqliteSynchronous = Literal["FULL", "NORMAL", "OFF"]

app = typer.Typer(
    help="SQLite + zstd 分片去重 FUSE 文件系统",
    add_completion=False,
    no_args_is_help=False,
)


def _parse_size(value: str | int) -> int:
    """将带单位的大小字符串（如 ``10g``）解析为字节数。"""
    if isinstance(value, int):
        return value
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    normalized = value.strip().lower()
    if normalized[-1:] in units:
        return int(float(normalized[:-1]) * units[normalized[-1]])
    return int(normalized)


def _size_option(value: str | int) -> int:
    try:
        return _parse_size(value)
    except ValueError as exc:
        raise click.BadParameter(
            "must be a size such as 4096, 32k, 128m, or 10g"
        ) from exc


def _optional_size_option(value: str | int | None) -> int | None:
    if value is None:
        return None
    return _size_option(value)


def _nonnegative_float(value: float) -> float:
    if value < 0:
        raise click.BadParameter("must be non-negative")
    return value


def _nonnegative_int(value: int | None) -> int | None:
    if value is not None and value < 0:
        raise click.BadParameter("must be non-negative")
    return value


def main(argv: list[str] | None = None) -> None:
    """解析 ``argv``（默认同 ``sys.argv``），执行对应 Typer 子命令。"""
    try:
        app(args=argv, prog_name="ztierfs", standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc
    except click.Abort as exc:
        raise SystemExit(1) from exc


@app.command("mount")
def mount_command(
    tier1: Annotated[str, typer.Argument(help="第一层热块目录")],
    tier2: Annotated[str, typer.Argument(help="第二层冷块目录")],
    mountpoint: Annotated[str, typer.Argument(help="挂载点")],
    database: Annotated[
        str | None,
        typer.Option("--database", help="SQLite 元数据文件，默认放在第一层目录"),
    ] = None,
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="控制台日志级别，默认 INFO")
    ] = "INFO",
    log_file: Annotated[
        str | None,
        typer.Option("--log-file", help="可选日志文件路径；默认不写文件日志"),
    ] = None,
    debug: Annotated[
        bool, typer.Option("--debug", help="启用 DEBUG 日志并输出 FUSE 回调日志")
    ] = False,
    hot_cache: Annotated[
        int,
        typer.Option(
            "--hot-cache", parser=_size_option, help="热层高水位容量，例如 10g"
        ),
    ] = DEFAULT_HOT_CACHE_MAX_BYTES,
    hot_cache_min: Annotated[
        int | None,
        typer.Option(
            "--hot-cache-min",
            parser=_optional_size_option,
            help=f"触发降级后迁移到的热层低水位，默认不超过 {DEFAULT_HOT_CACHE_MIN_BYTES}",
        ),
    ] = None,
    protected_prefix_chunks: Annotated[
        int,
        typer.Option(
            "--protected-prefix-chunks", help="每个文件开头保留在热层的块数量"
        ),
    ] = DEFAULT_PROTECTED_PREFIX_CHUNKS,
    min_hot_age: Annotated[
        int,
        typer.Option(
            "--min-hot-age", help="块至少多久未读后才允许降级，单位秒，默认 86400"
        ),
    ] = DEFAULT_MIN_HOT_AGE_SECONDS,
    cold_copy_cleanup_age: Annotated[
        int,
        typer.Option(
            "--cold-copy-cleanup-age",
            help="提升到热层后冷层副本至少保留多久，单位秒，默认 0",
        ),
    ] = DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS,
    chunk_size: Annotated[
        int, typer.Option("--chunk-size", parser=_size_option, help="块大小，默认 1m")
    ] = CHUNK_SIZE,
    inline_max: Annotated[
        int,
        typer.Option(
            "--inline-max",
            parser=_size_option,
            help="处理后 payload 不超过该大小的块内联到 SQLite；设为 0 禁用，默认 32k",
        ),
    ] = DEFAULT_INLINE_MAX_BYTES,
    read_cache: Annotated[
        int,
        typer.Option(
            "--read-cache",
            parser=_size_option,
            help="解码后内容寻址块的内存 LRU 缓存容量；设为 0 禁用，默认 128m",
        ),
    ] = DEFAULT_READ_CACHE_BYTES,
    readahead_blocks: Annotated[
        int,
        typer.Option(
            "--readahead-blocks",
            help="检测到顺序读取时后台预读的后续块数，默认 1；设为 0 禁用",
        ),
    ] = DEFAULT_READAHEAD_BLOCKS,
    readahead_workers: Annotated[
        int,
        typer.Option(
            "--readahead-workers", help="后台预读线程数，默认 1；设为 0 禁用预读"
        ),
    ] = DEFAULT_READAHEAD_WORKERS,
    zstd_level: Annotated[
        int | None,
        typer.Option("--zstd-level", help="zstd 压缩等级，默认使用标准库默认值"),
    ] = None,
    compression_min: Annotated[
        int,
        typer.Option(
            "--compression-min",
            parser=_size_option,
            help="小于该大小的 payload 跳过 zstd 压缩尝试，默认 4k",
        ),
    ] = DEFAULT_COMPRESSION_MIN_BYTES,
    sqlite_synchronous: Annotated[
        SqliteSynchronous,
        typer.Option(
            "--sqlite-synchronous",
            help="SQLite synchronous 模式，默认 NORMAL；FULL 更保守，OFF 仅用于明确性能取舍",
        ),
    ] = "NORMAL",
    update_config: Annotated[
        bool,
        typer.Option(
            "--update-config", help="允许用本次挂载参数重写数据库中的本机存储路径配置"
        ),
    ] = False,
    iosize: Annotated[
        int,
        typer.Option(
            "--iosize",
            parser=_size_option,
            help="macFUSE 单次 I/O 请求大小，例如 4m；默认与内部块大小一致，调大可能触发 Finder 大文件复制错误",
        ),
    ] = DEFAULT_FUSE_IOSIZE,
    metadata_cache: Annotated[
        float,
        typer.Option(
            "--metadata-cache",
            callback=_nonnegative_float,
            help="内核缓存 inode 属性和目录项的秒数；默认 5，设为 0 关闭缓存",
        ),
    ] = DEFAULT_FUSE_METADATA_CACHE_SECONDS,
    defer_permissions: Annotated[
        bool,
        typer.Option(
            "--defer-permissions",
            help="把 access(2) 权限判断交回 ztierfs；默认由内核按返回的 POSIX 属性判断以减少 access 回调",
        ),
    ] = False,
    fuse_loop_clone_fd: Annotated[
        bool,
        typer.Option(
            "--fuse-loop-clone-fd",
            help="启用 libfuse 多线程 loop 的 clone_fd 模式；默认关闭，适合压测高并发请求时显式尝试",
        ),
    ] = False,
    fuse_loop_max_idle_threads: Annotated[
        int | None,
        typer.Option(
            "--fuse-loop-max-idle-threads",
            callback=_nonnegative_int,
            help="libfuse 多线程 loop 保留的最大空闲线程数，默认 10",
        ),
    ] = 10,
    profile_interval: Annotated[
        float,
        typer.Option(
            "--profile-interval",
            help="每隔指定秒数输出累计性能统计；设为 0 禁用，默认禁用",
        ),
    ] = 0,
    background: Annotated[bool, typer.Option("--background", help="后台运行")] = False,
    volname: Annotated[
        str | None,
        typer.Option("--volname", help="Finder 中显示的卷名，默认使用挂载点目录名"),
    ] = None,
) -> None:
    """挂载 ztierfs 文件系统。"""
    from macfusepy import FUSE

    with console_output(_console_config(log_level, log_file, debug=debug)):
        logger.info(
            "准备挂载文件系统：热层={}，冷层={}，挂载点={}", tier1, tier2, mountpoint
        )
        fs = ZTierFS(
            tier1,
            tier2,
            database,
            chunk_size=chunk_size,
            hot_cache_max_bytes=hot_cache,
            hot_cache_min_bytes=hot_cache_min,
            protected_prefix_chunks=protected_prefix_chunks,
            min_hot_age_seconds=min_hot_age,
            cold_copy_cleanup_age_seconds=cold_copy_cleanup_age,
            compression_level=zstd_level,
            compression_min_bytes=compression_min,
            inline_max_bytes=inline_max,
            read_cache_bytes=read_cache,
            readahead_blocks=readahead_blocks,
            readahead_workers=readahead_workers,
            sqlite_synchronous=sqlite_synchronous,
            update_config=update_config,
            profile_interval_seconds=profile_interval,
        )
        volume_name = volname or Path(mountpoint).name or "ztierfs"
        FUSE(
            fs,
            mountpoint,
            foreground=not background,
            allow_other=True,
            local=True,
            iosize=iosize,
            kernel_permissions=not defer_permissions,
            attr_timeout=metadata_cache,
            entry_timeout=metadata_cache,
            loop_clone_fd=fuse_loop_clone_fd,
            loop_max_idle_threads=fuse_loop_max_idle_threads,
            volname=volume_name,
        )


@app.command("fsck")
def fsck_command(
    database: Annotated[str, typer.Argument(help="SQLite 元数据文件")],
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="控制台日志级别，默认 INFO")
    ] = "INFO",
    log_file: Annotated[
        str | None,
        typer.Option("--log-file", help="可选日志文件路径；默认不写文件日志"),
    ] = None,
    repair: Annotated[
        bool, typer.Option("--repair", help="执行确定安全的自动修复")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="以 JSON 输出")] = False,
) -> None:
    """检查元数据与块文件一致性。"""
    with console_output(_console_config(log_level, log_file)):
        logger.info("开始执行 fsck：repair={}", repair)
        report = run_fsck(database, repair=repair)
        _emit_check_report(report, json_output=json_output)


@app.command("scrub")
def scrub_command(
    database: Annotated[str, typer.Argument(help="SQLite 元数据文件")],
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="控制台日志级别，默认 INFO")
    ] = "INFO",
    log_file: Annotated[
        str | None,
        typer.Option("--log-file", help="可选日志文件路径；默认不写文件日志"),
    ] = None,
    repair: Annotated[
        bool, typer.Option("--repair", help="执行确定安全的自动修复")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="以 JSON 输出")] = False,
    include_cold: Annotated[
        bool,
        typer.Option(
            "--include-cold",
            help="同时读取冷层块 payload；远程冷层可能因此下载完整冷层数据",
        ),
    ] = False,
) -> None:
    """检查一致性并读取校验 inline/热层 payload。"""
    with console_output(_console_config(log_level, log_file)):
        logger.info("开始执行 scrub：repair={}", repair)
        report = run_scrub(database, repair=repair, include_cold=include_cold)
        _emit_check_report(report, json_output=json_output)


@app.command("stats")
def stats_command(
    database: Annotated[str, typer.Argument(help="SQLite 元数据文件")],
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="控制台日志级别，默认 INFO")
    ] = "INFO",
    log_file: Annotated[
        str | None,
        typer.Option("--log-file", help="可选日志文件路径；默认不写文件日志"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="以 JSON 输出")] = False,
) -> None:
    """输出文件系统统计信息。"""
    with console_output(_console_config(log_level, log_file)):
        logger.info("开始收集文件系统统计信息")
        report = collect_stats(database)
        _emit_report(
            report,
            json_output=json_output,
            rich_formatter=_stats_report_to_rich,
        )


@app.command("cleanup")
def cleanup_command(
    database: Annotated[str, typer.Argument(help="SQLite 元数据文件")],
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="控制台日志级别，默认 INFO")
    ] = "INFO",
    log_file: Annotated[
        str | None,
        typer.Option("--log-file", help="可选日志文件路径；默认不写文件日志"),
    ] = None,
    age: Annotated[
        int, typer.Option("--age", help="只删除已保留至少这么多秒的冷层副本")
    ] = DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS,
    cold_gc_age: Annotated[
        int,
        typer.Option(
            "--cold-gc-age",
            help="只删除进入 cold garbage 至少这么多秒的无引用冷层块，默认 30 天",
        ),
    ] = DEFAULT_COLD_GC_AGE_SECONDS,
    max_cold_deletes: Annotated[
        int | None,
        typer.Option(
            "--max-cold-deletes",
            callback=_nonnegative_int,
            help="本次最多删除多少个 cold garbage 块；默认不限",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="只报告会清理的 cold garbage，不删除块文件或元数据"
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="以 JSON 输出")] = False,
) -> None:
    """整理已提升块遗留的冷层副本。"""
    with console_output(_console_config(log_level, log_file)):
        logger.info(
            "开始清理已提升块遗留的冷层副本：min_age_seconds={}，cold_gc_age_seconds={}，dry_run={}",
            age,
            cold_gc_age,
            dry_run,
        )
        report = cleanup_promoted_cold_copies(
            database,
            min_age_seconds=age,
            cold_gc_age_seconds=cold_gc_age,
            max_cold_deletes=max_cold_deletes,
            dry_run=dry_run,
        )
        _emit_report(
            report,
            json_output=json_output,
            json_formatter=_cleanup_report_to_dict,
            rich_formatter=_cleanup_report_to_rich,
        )


def _console_config(
    log_level: LogLevel,
    log_file: str | None,
    *,
    debug: bool = False,
) -> ConsoleOutputConfig:
    """根据解析结果构造 ``ConsoleOutputConfig``（``--debug`` 时提升日志级别）。"""
    logger_level = "DEBUG" if debug else log_level
    return ConsoleOutputConfig(logger_level=logger_level, log_file=log_file)


def _emit_check_report(report, *, json_output: bool) -> None:
    """输出 fsck/scrub 报告（文本或 JSON）；若报告中仍有未修复问题则 ``SystemExit(1)``。"""
    _emit_report(report, json_output=json_output, rich_formatter=_check_report_to_rich)
    if report.has_unrepaired:
        raise SystemExit(1)


def _emit_report(
    report,
    *,
    json_output: bool,
    rich_formatter,
    json_formatter=None,
) -> None:
    """按 CLI 约定输出报告：JSON 走稳定 dict，文本走对应 formatter。"""
    if json_output:
        formatter = json_formatter or (lambda value: value.to_dict())
        _emit_stdout(json.dumps(formatter(report), ensure_ascii=False, sort_keys=True))
    else:
        Console(file=sys.stdout).print(rich_formatter(report))


def _check_report_to_rich(report: CheckReport):
    """将 fsck/scrub 报告渲染为 Rich 输出。"""
    if not report.issues:
        return Text(f"{report.command}: ok", style="green")

    table = Table(
        title=f"{report.command}: {len(report.issues)} issue(s)",
        title_style="bold",
        show_lines=False,
    )
    table.add_column("State", no_wrap=True)
    table.add_column("Code", style="cyan", no_wrap=True)
    table.add_column("Message")
    table.add_column("Details", overflow="fold")
    for issue in report.issues:
        state = "repaired" if issue.repaired else "unrepaired"
        state_style = "green" if issue.repaired else "red"
        details = (
            json.dumps(issue.details, ensure_ascii=False, sort_keys=True)
            if issue.details
            else ""
        )
        table.add_row(
            Text(state, style=state_style),
            issue.code,
            issue.message,
            details,
        )
    return table


def _stats_report_to_rich(report: StatsReport):
    """将 stats 报告渲染为分组 Rich 表格。"""
    tables = []
    for section, values in report.to_dict().items():
        table = Table(title=section, title_style="bold", show_header=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")
        for key, value in values.items():
            table.add_row(key, str(value))
        tables.append(table)
    return Group(*tables)


def _cleanup_report_to_dict(report) -> dict[str, int]:
    """将 cleanup 报告转为 CLI JSON 字段，保持既有输出契约。"""
    return {
        "removed_cold_copies": report.removed,
        "skipped_cold_copies": report.skipped,
        "removed_pending_deletions": report.pending_removed,
        "skipped_pending_deletions": report.pending_skipped,
        "pending_deletion_unavailable": report.pending_unavailable,
        "cold_garbage_candidates": report.cold_garbage_candidates,
        "removed_cold_garbage": report.removed_cold_garbage,
        "skipped_cold_unavailable": report.skipped_cold_unavailable,
        "reclaimed_cold_bytes": report.reclaimed_cold_bytes,
        "remaining_cold_garbage": report.remaining_cold_garbage,
    }


def _cleanup_report_to_rich(report: CleanupReport):
    """将 cleanup 报告渲染为 Rich 汇总表。"""
    rows = [
        ("Promoted cold copies removed", report.removed, "green"),
        ("Promoted cold copies skipped", report.skipped, "yellow"),
        ("Pending deletions removed", report.pending_removed, "green"),
        ("Pending deletions skipped", report.pending_skipped, "yellow"),
        ("Pending deletions unavailable", report.pending_unavailable, "red"),
        ("Cold garbage candidates", report.cold_garbage_candidates, "cyan"),
        ("Cold garbage removed", report.removed_cold_garbage, "green"),
        ("Cold unavailable skipped", report.skipped_cold_unavailable, "yellow"),
        ("Reclaimed cold bytes", report.reclaimed_cold_bytes, "green"),
        ("Remaining cold garbage", report.remaining_cold_garbage, "cyan"),
    ]
    table = Table(title="cleanup", title_style="bold", show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    for label, value, style in rows:
        table.add_row(label, Text(str(value), style=style if value else "dim"))
    return table


def _emit_stdout(text: str) -> None:
    """将字符串写入标准输出，若末尾无换行则追加 ``\\n``。"""
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
