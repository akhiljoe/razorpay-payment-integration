"""Tests for OrderService against a live Postgres.

Most tests below are marked `requires_db`. Run via:

    export DATABASE_URL="postgresql://razorpay:razorpay@localhost:5432/razorpay_checkout"
    uv run pytest tests/test_order_service.py -v -m requires_db
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import respx

from razorpay_checkout.amounts import FixedAmountResolver
from razorpay_checkout.client import RazorpayClient, RazorpayUnavailableError
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.models import CreateOrderRequest
from razorpay_checkout.orders import OrderNotFoundError, OrderService
from razorpay_checkout.state_machine import IllegalTransitionError, OrderStatus

pytestmark = pytest.mark.requires_db


def _rzp_response(razorpay_order_id: str = "order_RZP_abc") -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": razorpay_order_id, "amount": 50000, "currency": "INR"},
    )


@pytest.fixture
async def service(
    live_pool, db_config: RazorpayConfig, razorpay_client: RazorpayClient,
    fixed_resolver: FixedAmountResolver,
) -> OrderService:
    return OrderService(
        pool=live_pool,
        config=db_config,
        client=razorpay_client,
        amount_resolver=fixed_resolver,
    )


# ---------- create_or_get ----------


@respx.mock
async def test_create_order_inserts_initializing_then_created(
    service: OrderService, live_pool,
) -> None:
    route = respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=_rzp_response("order_NEW")
    )
    record = await service.create_or_get(CreateOrderRequest(receipt="r-1"))
    assert record.status == OrderStatus.CREATED
    assert record.razorpay_order_id == "order_NEW"
    assert record.amount == 50000

    async with live_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT from_status, to_status, trigger FROM order_events "
            "WHERE order_id = $1 ORDER BY created_at",
            record.id,
        )
    assert [(e["from_status"], e["to_status"], e["trigger"]) for e in events] == [
        ("initializing", "created", "initializing")
    ]
    assert route.call_count == 1


@respx.mock
async def test_duplicate_receipt_returns_existing(service: OrderService) -> None:
    route = respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=_rzp_response("order_DUP")
    )
    first = await service.create_or_get(CreateOrderRequest(receipt="r-dup"))
    second = await service.create_or_get(CreateOrderRequest(receipt="r-dup"))
    assert first.id == second.id
    assert second.razorpay_order_id == "order_DUP"
    assert route.call_count == 1, "second call must not hit Razorpay"


@respx.mock
async def test_concurrent_inserts_same_receipt_collapse(service: OrderService) -> None:
    route = respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=_rzp_response("order_RACE")
    )
    request = CreateOrderRequest(receipt="r-race")
    results = await asyncio.gather(
        *[service.create_or_get(request) for _ in range(10)]
    )
    ids = {r.id for r in results}
    assert len(ids) == 1, f"expected one order id, got {ids}"
    assert route.call_count == 1


@respx.mock
async def test_razorpay_timeout_leaves_initializing_row(
    service: OrderService, live_pool,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        side_effect=httpx.TimeoutException("timeout")
    )
    with pytest.raises(RazorpayUnavailableError):
        await service.create_or_get(CreateOrderRequest(receipt="r-timeout"))

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, razorpay_order_id FROM orders WHERE receipt = $1",
            "r-timeout",
        )
    assert row is not None
    assert row["status"] == "initializing"
    assert row["razorpay_order_id"] is None


@respx.mock
async def test_retry_after_razorpay_timeout_succeeds(
    service: OrderService, live_pool,
) -> None:
    route = respx.post("https://api.razorpay.com/v1/orders")
    route.side_effect = [
        httpx.TimeoutException("first timeout"),
        _rzp_response("order_RECOVERED"),
    ]
    with pytest.raises(RazorpayUnavailableError):
        await service.create_or_get(CreateOrderRequest(receipt="r-retry"))

    record = await service.create_or_get(CreateOrderRequest(receipt="r-retry"))
    assert record.status == OrderStatus.CREATED
    assert record.razorpay_order_id == "order_RECOVERED"

    async with live_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM orders WHERE receipt = $1",
            "r-retry",
        )
    assert count == 1, "no duplicate rows from retry path"


# ---------- transition ----------


@respx.mock
async def test_transition_legal_edge_writes_order_events(
    service: OrderService, live_pool, db_config: RazorpayConfig,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=_rzp_response("order_T1")
    )
    record = await service.create_or_get(CreateOrderRequest(receipt="r-t1"))

    async with live_pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT * FROM orders WHERE id = $1 FOR UPDATE",
                record.id,
            )
            await service.transition(
                conn,
                order_id=record.id,
                tenant_id=db_config.tenant_id,
                new_status=OrderStatus.ATTEMPTED,
                trigger="webhook",
                metadata={"foo": "bar"},
            )

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, attempted_at FROM orders WHERE id = $1",
            record.id,
        )
        events = await conn.fetch(
            "SELECT from_status, to_status, trigger, metadata FROM order_events "
            "WHERE order_id = $1 ORDER BY created_at",
            record.id,
        )
    assert row["status"] == "attempted"
    assert row["attempted_at"] is not None
    # initializing -> created (from creation) + created -> attempted (this test)
    assert len(events) == 2
    last = events[-1]
    assert last["from_status"] == "created"
    assert last["to_status"] == "attempted"
    assert last["trigger"] == "webhook"


@respx.mock
async def test_transition_illegal_edge_raises(
    service: OrderService, live_pool, db_config: RazorpayConfig,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=_rzp_response("order_T2")
    )
    record = await service.create_or_get(CreateOrderRequest(receipt="r-t2"))

    async with live_pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow("SELECT * FROM orders WHERE id = $1 FOR UPDATE", record.id)
            with pytest.raises(IllegalTransitionError):
                await service.transition(
                    conn,
                    order_id=record.id,
                    tenant_id=db_config.tenant_id,
                    new_status=OrderStatus.REFUNDED,
                    trigger="webhook",
                )

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM orders WHERE id = $1", record.id)
    assert row["status"] == "created", "illegal transition must not mutate orders"


@respx.mock
async def test_transition_rejects_cross_tenant_order(
    service: OrderService, live_pool,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=_rzp_response("order_T3")
    )
    record = await service.create_or_get(CreateOrderRequest(receipt="r-t3"))

    async with live_pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow("SELECT * FROM orders WHERE id = $1 FOR UPDATE", record.id)
            with pytest.raises(OrderNotFoundError):
                await service.transition(
                    conn,
                    order_id=record.id,
                    tenant_id="t_other_tenant",
                    new_status=OrderStatus.ATTEMPTED,
                    trigger="webhook",
                )


@respx.mock
async def test_transition_missing_order_raises(
    service: OrderService, live_pool, db_config: RazorpayConfig,
) -> None:
    async with live_pool.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(OrderNotFoundError):
                await service.transition(
                    conn,
                    order_id=uuid.uuid4(),
                    tenant_id=db_config.tenant_id,
                    new_status=OrderStatus.ATTEMPTED,
                    trigger="webhook",
                )


# ---------- get_by_id / get_by_razorpay_order_id ----------


@respx.mock
async def test_get_by_id_tenant_scoped(
    service: OrderService, db_config: RazorpayConfig,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=_rzp_response("order_G1")
    )
    record = await service.create_or_get(CreateOrderRequest(receipt="r-g1"))

    fetched = await service.get_by_id(record.id, db_config.tenant_id)
    assert fetched is not None
    assert fetched.id == record.id

    assert await service.get_by_id(record.id, "t_other_tenant") is None
    assert await service.get_by_id(uuid.uuid4(), db_config.tenant_id) is None


@respx.mock
async def test_get_by_razorpay_order_id(
    service: OrderService, db_config: RazorpayConfig,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=_rzp_response("order_G2")
    )
    await service.create_or_get(CreateOrderRequest(receipt="r-g2"))

    fetched = await service.get_by_razorpay_order_id("order_G2", db_config.tenant_id)
    assert fetched is not None
    assert fetched.razorpay_order_id == "order_G2"

    assert await service.get_by_razorpay_order_id("order_G2", "t_other") is None
    assert await service.get_by_razorpay_order_id("order_UNKNOWN", db_config.tenant_id) is None
