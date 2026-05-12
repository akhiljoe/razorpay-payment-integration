"""asyncpg pool factory and slow-acquire instrumentation.

`create_pool` builds an `asyncpg.Pool` with explicit `min_size` and `max_size`
sourced from `RazorpayConfig`. Bounded pools are mandatory on Lambda where
concurrency is regional and a runaway pool blocks queue draining.

`acquire_with_timing` wraps `pool.acquire()` and logs a WARN structured record
when the acquire wait exceeds `slow_acquire_ms` (default 200 ms). On Lambda
this fires during Neon cold starts or pool exhaustion under burst load.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import asyncpg

from razorpay_checkout.logging_config import get_logger

if TYPE_CHECKING:
    from razorpay_checkout.config import RazorpayConfig

_log = get_logger("razorpay_checkout.db")


def _normalise_dsn(dsn: str) -> str:
    """asyncpg does not accept the `+asyncpg` SQLAlchemy driver suffix."""
    if dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + dsn[len("postgresql+asyncpg://"):]
    return dsn


async def create_pool(config: RazorpayConfig) -> asyncpg.Pool:
    """Create an asyncpg pool using sizing and timeout from `config`.

    `command_timeout` is set to `config.razorpay_api_timeout` as a coarse upper
    bound on any single statement. Long-running migrations should bypass this
    pool and use Alembic's own engine.
    """
    return await asyncpg.create_pool(
        dsn=_normalise_dsn(str(config.db_dsn)),
        min_size=config.pool_min_size,
        max_size=config.pool_max_size,
        command_timeout=config.razorpay_api_timeout,
    )


@asynccontextmanager
async def acquire_with_timing(
    pool: asyncpg.Pool,
    *,
    slow_acquire_ms: int = 200,
) -> AsyncIterator[asyncpg.Connection]:
    """Yield a connection from `pool`, logging WARN if acquire is slow.

    Use this in request-path code where pool wait time matters. Background
    jobs that already accept latency can use `pool.acquire()` directly.
    """
    start = time.perf_counter()
    async with pool.acquire() as conn:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if elapsed_ms >= slow_acquire_ms:
            _log.warning(
                "slow_pool_acquire",
                elapsed_ms=round(elapsed_ms, 2),
                threshold_ms=slow_acquire_ms,
                pool_size=pool.get_size(),
                pool_free=pool.get_idle_size(),
            )
        yield conn
