"""Shared pytest fixtures for the test suite.

`live_pool` and friends are gated by `DATABASE_URL` being set; tests that
require them mark themselves with `@pytest.mark.requires_db`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from razorpay_checkout.amounts import FixedAmountResolver
from razorpay_checkout.client import RazorpayClient
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.db import create_pool

_DATABASE_URL = os.environ.get("DATABASE_URL")


def _has_db() -> bool:
    return bool(_DATABASE_URL)


@pytest.fixture
def db_config() -> RazorpayConfig:
    if not _has_db():
        pytest.skip("DATABASE_URL not set")
    return RazorpayConfig(
        tenant_id="t_phase5",
        key_id="rzp_test_kid",
        key_secret="rzp_test_ksecret",
        webhook_secret="wsec",
        db_dsn=_DATABASE_URL,  # type: ignore[arg-type]
        pool_min_size=1,
        pool_max_size=4,
        razorpay_api_timeout=2.0,
    )


@pytest_asyncio.fixture
async def live_pool(db_config: RazorpayConfig) -> AsyncIterator[asyncpg.Pool]:
    pool = await create_pool(db_config)
    try:
        # Wipe tenant-scoped tables (in FK-safe order) so each test sees a fresh slate.
        async with pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE order_events, refunds, payments, webhook_events, orders "
                "RESTART IDENTITY CASCADE"
            )
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def fixed_resolver() -> FixedAmountResolver:
    return FixedAmountResolver(amount=50000)


@pytest_asyncio.fixture
async def razorpay_client(db_config: RazorpayConfig) -> AsyncIterator[RazorpayClient]:
    client = RazorpayClient(db_config)
    try:
        yield client
    finally:
        await client.aclose()
