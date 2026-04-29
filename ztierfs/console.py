"""配置终端 Rich 输出，并把标准库 logging 经拦截器转发到 loguru。"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from loguru import logger
from rich import get_console, reconfigure
from rich.logging import RichHandler


@dataclass(frozen=True)
class ConsoleOutputConfig:
    """控制台与可选文件日志的统一配置。

    ``logger_level``：loguru 与 Rich 控制台 sink 的最低级别。
    ``log_file``：若设置则额外写入该路径（父目录不存在时会创建）。
    ``log_file_level``：文件 sink 级别；为 ``None`` 时与 ``logger_level`` 相同。
    ``log_file_rotation`` / ``log_file_retention``：传给 loguru 文件 sink 的轮转与保留策略。
    """
    logger_level: str | int = "INFO"
    log_file: str | Path | None = None
    log_file_level: str | int | None = None
    log_file_rotation: str | int | None = "100 MB"
    log_file_retention: str | int | None = "14 days"


class _LoguruInterceptHandler(logging.Handler):
    """标准 ``logging`` 的 ``Handler``：把 ``LogRecord`` 转成对 loguru 的同级调用。"""
    def emit(self, record: logging.LogRecord) -> None:
        """按记录级别与消息调用 loguru；若有 ``exc_info`` 则一并带上。"""
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def setup_console_output(cfg: ConsoleOutputConfig) -> None:
    """将 Rich 控制台接到 stderr，用 loguru+RichHandler 打控制台日志，并按配置追加文件 sink；再用本模块的拦截 ``Handler`` 调用 ``logging.basicConfig`` 接管根 logger。"""
    reconfigure_kwargs: dict = {"stderr": True}
    reconfigure(**reconfigure_kwargs)
    console = get_console()

    logger.remove()
    rich_handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        show_time=True,
        show_level=True,
        show_path=True,
        markup=False,
    )
    logger.add(rich_handler, level=cfg.logger_level, format="{message}")
    if cfg.log_file is not None:
        Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            cfg.log_file,
            level=cfg.log_file_level or cfg.logger_level,
            rotation=cfg.log_file_rotation,
            retention=cfg.log_file_retention,
            encoding="utf-8",
            backtrace=True,
            diagnose=False,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {thread.name} | {name}:{function}:{line} | {message}",
        )
    logging.basicConfig(
        handlers=[_LoguruInterceptHandler()],
        level=cfg.logger_level,
        force=True,
    )


@contextmanager
def console_output(cfg: ConsoleOutputConfig) -> Iterator[None]:
    """在上下文中临时应用 ``setup_console_output``；退出时移除 loguru sink，并恢复根 logger 原先的 handlers 与级别。"""
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level

    setup_console_output(cfg)
    try:
        yield
    finally:
        logger.remove()
        root_logger.handlers = original_handlers
        root_logger.setLevel(original_level)
