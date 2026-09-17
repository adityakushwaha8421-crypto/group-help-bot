"""Structured logging with automatic secret redaction."""

from __future__ import annotations

import logging
import re
import sys

import structlog

_SECRET_KEYS = re.compile(r"(token|password|secret|api_key|apikey|session|authorization|cookie)", re.I)
# bot tokens look like 123456789:AAxxxxxxxx ; openai keys sk-...
_SECRET_VALUES = re.compile(r"(\d{6,}:[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{16,})")


def _redact(value):
    if isinstance(value, str):
        return _SECRET_VALUES.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if _SECRET_KEYS.search(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(v) for v in value)
    return value


def redact_processor(_logger, _method, event_dict):
    return _redact(event_dict)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer() if sys.stdout.isatty() else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    for noisy in ("httpx", "httpcore", "aiogram.event", "telethon"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None):
    return structlog.get_logger(name) if name else structlog.get_logger()
