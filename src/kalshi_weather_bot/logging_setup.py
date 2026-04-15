from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import structlog


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handler_file = logging.handlers.RotatingFileHandler(
        Path(log_dir) / "bot.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
    )
    handler_stream = logging.StreamHandler()

    logging.basicConfig(
        level=numeric_level,
        format="%(message)s",
        handlers=[handler_file, handler_stream],
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
