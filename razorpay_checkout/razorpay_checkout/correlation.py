"""Correlation ID context.

A single `correlation_id` (UUIDv4) is generated at every HTTP entry point and
propagated through DB rows, SQS message attributes, and the FulfillmentHook
metadata. The contextvar binding flows through `asyncio` automatically.

structlog binds the contextvar via `merge_contextvars`, so any logger call
emitted after `set_correlation_id` will carry the `correlation_id` field.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from fastapi import Request

_correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def new_correlation_id() -> str:
    """Return a fresh UUIDv4 string suitable for use as a correlation id."""
    return str(uuid.uuid4())


def set_correlation_id(correlation_id: str) -> None:
    """Bind `correlation_id` to the current context and structlog contextvars."""
    _correlation_id_var.set(correlation_id)
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)


def get_correlation_id() -> str:
    """Return the current context's correlation id, or empty string if unset."""
    return _correlation_id_var.get()


def clear_correlation_id() -> None:
    """Clear the correlation id from context and structlog contextvars."""
    _correlation_id_var.set("")
    structlog.contextvars.unbind_contextvars("correlation_id")


async def correlation_id_dependency(request: Request) -> str:
    """FastAPI dependency.

    Reads an incoming `X-Correlation-Id` header if present (for tracing across
    upstream proxies), otherwise generates a fresh UUIDv4. Binds the value for
    the request lifetime and returns it.
    """
    incoming = request.headers.get("x-correlation-id", "").strip()
    correlation_id = incoming or new_correlation_id()
    set_correlation_id(correlation_id)
    return correlation_id
