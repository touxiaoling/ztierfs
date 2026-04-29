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
    logger_level: str | int = "INFO"
    log_file: str | Path | None = None
    log_file_level: str | int | None = None
    log_file_rotation: str | int | None = "100 MB"
    log_file_retention: str | int | None = "14 days"


class _LoguruInterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def setup_console_output(cfg: ConsoleOutputConfig) -> None:
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
