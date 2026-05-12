"""Stale-`paid` monitor handler.

DESIGN_v5 Section 7 dual-write safety net. The Webhook Consumer commits the
DB transaction (`paid` row) before publishing to `fulfillment-queue.fifo`. If
the publish fails after commit, the order is stuck in `paid` with no
in-flight fulfillment message.

This scheduled Lambda runs every minute via EventBridge:

```sql
SELECT id, tenant_id FROM orders
 WHERE status = 'paid' AND paid_at < NOW() - INTERVAL '5 minutes'
 LIMIT 100;
```

For each row found, the handler emits a WARN log and a `stale_paid_orders`
metric. CloudWatch alarm fires on any result. Operator's recovery:
`POST /payments/admin/orders/{order_id}/retry-fulfillment`.

The handler does NOT auto-retry. Manual operator action prevents tight loops
where a hook is failing and we keep re-enqueueing.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import asyncpg

from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.correlation import new_correlation_id, set_correlation_id
from razorpay_checkout.db import create_pool
from razorpay_checkout.logging_config import configure_logging, get_logger

_log = get_logger("razorpay_checkout.lambdas.stale_paid_monitor")
_state: dict[str, Any] = {}

STALE_THRESHOLD_MINUTES = 5
MAX_ROWS_PER_RUN = 100


async def _init() -> asyncpg.Pool:
    if "pool" not in _state:
        configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
        _state["pool"] = await create_pool(RazorpayConfig())  # type: ignore[call-arg]
    return _state["pool"]


async def find_stale_paid_orders(
    pool: asyncpg.Pool,
    *,
    threshold_minutes: int = STALE_THRESHOLD_MINUTES,
    limit: int = MAX_ROWS_PER_RUN,
) -> list[dict[str, Any]]:
    """Return list of `{order_id, tenant_id, paid_at, correlation_id}` dicts."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, tenant_id, paid_at, correlation_id
              FROM orders
             WHERE status = 'paid'
               AND paid_at < NOW() - INTERVAL '{threshold_minutes} minutes'
             ORDER BY paid_at ASC
             LIMIT {limit}
            """,  # noqa: S608 - thresholds are int literals from module constants
        )
    return [
        {
            "order_id": str(row["id"]),
            "tenant_id": row["tenant_id"],
            "paid_at": row["paid_at"].isoformat() if row["paid_at"] else None,
            "correlation_id": row["correlation_id"],
        }
        for row in rows
    ]


async def _run() -> dict[str, Any]:
    pool = await _init()
    stale = await find_stale_paid_orders(pool)
    for entry in stale:
        _log.warning(
            "stale_paid_order_detected",
            order_id=entry["order_id"],
            tenant_id=entry["tenant_id"],
            paid_at=entry["paid_at"],
            order_correlation_id=entry["correlation_id"],
        )
    _log.info("stale_paid_monitor_run", count=len(stale))
    return {"stale_paid_count": len(stale)}


def handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    set_correlation_id(new_correlation_id())
    return asyncio.run(_run())
