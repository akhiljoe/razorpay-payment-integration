"""Structured JSON logging bootstrap.

Mandatory across the codebase. Every log record carries `timestamp`, `level`,
`module`, and any context-bound fields (`correlation_id`, `tenant_id`,
`order_id`, `payment_id`). Secret values registered via `register_secret` are
redacted from any record before serialization.

No `print` calls are permitted anywhere in the application package.
"""

from __future__ import annotations

import logging
import os
import sys
from threading import Lock
from typing import Any

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

_SECRET_VALUES: set[str] = set()
_SECRETS_LOCK = Lock()
_REDACTION_MARKER = "***"


def register_secret(value: str | None) -> None:
    """Register a secret string so it is redacted from log records.

    Empty or `None` values are ignored. Idempotent across calls. Thread-safe.
    """
    if not value:
        return
    with _SECRETS_LOCK:
        _SECRET_VALUES.add(value)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for secret in _SECRET_VALUES:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, _REDACTION_MARKER)
        return redacted
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        cls = type(value)
        return cls(_redact(v) for v in value)
    return value


def _redact_secrets_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    if not _SECRET_VALUES:
        return event_dict
    return {k: _redact(v) for k, v in event_dict.items()}


def _add_module_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    event_dict.setdefault("module", event_dict.pop("logger", "razorpay_checkout"))
    return event_dict


def configure_logging(level: str | int | None = None) -> None:
    """Initialize structlog with the JSON renderer and required processors.

    Idempotent: safe to call repeatedly. Reads `LOG_LEVEL` from env if `level`
    is None. Routes through `logging` so third-party libraries inherit the
    same handler.
    """
    resolved_level = level if level is not None else os.environ.get("LOG_LEVEL", "INFO")
    if isinstance(resolved_level, str):
        log_level = logging.getLevelName(resolved_level.upper())
    else:
        log_level = resolved_level
    if not isinstance(log_level, int):
        log_level = logging.INFO

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_module_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_secrets_processor,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to `name` (defaults to caller's module)."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(
        name if name else "razorpay_checkout"
    )
    return logger
