"""Tests for the stale-`paid` monitor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from razorpay_checkout.lambdas.stale_paid_monitor import find_stale_paid_orders

pytestmark = pytest.mark.requires_db


async def _insert_paid_order(pool, *, tenant_id: str, paid_at: datetime) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO orders
                (tenant_id, receipt, amount, currency, notes, correlation_id,
                 status, razorpay_order_id, paid_at)
            VALUES ($1, $2, 50000, 'INR', '{}'::jsonb, $3, 'paid', $4, $5)
            RETURNING id
            """,
            tenant_id,
            f"receipt-{uuid.uuid4()}",
            f"corr-{uuid.uuid4()}",
            f"order_rzp_{uuid.uuid4().hex[:12]}",
            paid_at,
        )


async def test_fresh_paid_order_is_not_stale(live_pool) -> None:
    now = datetime.now(tz=UTC)
    await _insert_paid_order(
        live_pool, tenant_id="t_phase5", paid_at=now - timedelta(minutes=1)
    )
    stale = await find_stale_paid_orders(live_pool, threshold_minutes=5)
    assert stale == []


async def test_stale_paid_order_is_flagged(live_pool) -> None:
    now = datetime.now(tz=UTC)
    order_id = await _insert_paid_order(
        live_pool, tenant_id="t_phase5", paid_at=now - timedelta(minutes=10)
    )
    stale = await find_stale_paid_orders(live_pool, threshold_minutes=5)
    assert len(stale) == 1
    assert stale[0]["order_id"] == str(order_id)
    assert stale[0]["tenant_id"] == "t_phase5"


async def test_limit_bounds_results(live_pool) -> None:
    now = datetime.now(tz=UTC)
    for _ in range(3):
        await _insert_paid_order(
            live_pool, tenant_id="t_phase5", paid_at=now - timedelta(minutes=10)
        )
    stale = await find_stale_paid_orders(live_pool, threshold_minutes=5, limit=2)
    assert len(stale) == 2
