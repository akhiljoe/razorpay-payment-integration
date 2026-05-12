"""Shared pytest fixtures for the test suite.

`live_pool` and friends are gated by `DATABASE_URL`. `sqs_client` is gated by
`AWS_ENDPOINT_URL` (LocalStack). Tests that require either mark themselves
with the matching `@pytest.mark.requires_db` / `@pytest.mark.requires_localstack`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import aioboto3
import asyncpg
import pytest
import pytest_asyncio

from razorpay_checkout.amounts import FixedAmountResolver
from razorpay_checkout.client import RazorpayClient
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.db import create_pool
from razorpay_checkout.queue import (
    WEBHOOK_EVENTS_QUEUE_NAME,
    ensure_webhook_events_queues,
)

_DATABASE_URL = os.environ.get("DATABASE_URL")
_AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL")
_AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")


def _has_db() -> bool:
    return bool(_DATABASE_URL)


def _has_localstack() -> bool:
    return bool(_AWS_ENDPOINT_URL)


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


@pytest_asyncio.fixture
async def sqs_client() -> AsyncIterator[object]:
    """aioboto3 SQS client pointed at LocalStack.

    Skips when `AWS_ENDPOINT_URL` is unset. Yields the client; teardown closes
    the underlying session.
    """
    if not _has_localstack():
        pytest.skip("AWS_ENDPOINT_URL not set")
    session = aioboto3.Session()
    async with session.client(
        "sqs",
        endpoint_url=_AWS_ENDPOINT_URL,
        region_name=_AWS_REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    ) as client:
        yield client


@pytest_asyncio.fixture
async def webhook_events_queue_url(sqs_client: object) -> AsyncIterator[str]:
    """Ensure `webhook-events.fifo` exists; purge it before yielding.

    Returns the queue URL. Purge runs at fixture entry so each test sees an
    empty queue. After the test, the queue stays (LocalStack reset is between
    sessions, not between tests).
    """
    main_url, _dlq_url = await ensure_webhook_events_queues(sqs_client)  # type: ignore[arg-type]
    try:
        await sqs_client.purge_queue(QueueUrl=main_url)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - purge has a 60s cooldown; ignore if recent
        pass
    yield main_url


__all__ = [
    "WEBHOOK_EVENTS_QUEUE_NAME",
]
