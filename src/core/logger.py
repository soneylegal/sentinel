"""
Sentinel - Structured Logging Configuration (Loguru)

Configures Loguru to emit structured JSON logs suitable for ingestion
by Datadog, ELK, Loki, or any structured log aggregator.
"""

from __future__ import annotations

import sys
from typing import Any

from loguru import logger


def setup_logger(level: str = "INFO", fmt: str = "json") -> None:
    """Configure Loguru with structured JSON output.

    Args:
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        fmt: Output format — 'json' for structured, 'pretty' for human-readable.
    """
    # Remove default handler
    logger.remove()

    if fmt == "json":
        logger.add(
            sys.stderr,
            level=level.upper(),
            format=_json_format,
            colorize=False,
            serialize=True,
        )
    else:
        logger.add(
            sys.stderr,
            level=level.upper(),
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            ),
            colorize=True,
        )

    logger.info(
        "Logger initialized",
        level=level,
        format=fmt,
        component="core.logger",
    )


def _json_format(record: Any) -> str:
    """Custom JSON format string for Loguru serialization."""
    return "{time:YYYY-MM-DDTHH:mm:ss.SSSZ} | {level} | {name}:{function}:{line} | {message}\n"


def get_logger() -> Any:
    """Return the configured Loguru logger instance.

    Returns ``loguru.logger`` — typed as ``Any`` because loguru does not
    ship PEP 561 type stubs and mypy cannot resolve its attributes
    under ``--strict``.  This is the officially recommended workaround.
    """
    return logger
