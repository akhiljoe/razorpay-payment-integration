"""Tests for the fulfillment-dlq.fifo poller."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import respx

from razorpay_checkout.amounts import FixedAmountResolver
from razorpay_checkout.client import RazorpayClient
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.lambdas.fulfillment_dlq_poller import mark_fulfillment_failed
from razorpay_checkout.models import CreateOrderRequest
from razorpay_checkout.orders import OrderService
from razorpay_checkout.state_machine import OrderStatus

pytestmark = pytest.mark.requires_db


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


@pytest.fixture
async def paid_order(live_pool, service: OrderService) -> Any:
    with respx.mock() as mock_router:
        mock_router.post("https://api.razorpay.com/v1/orders").mock(
            return_value=httpx.Response(
                200, json={"id": "order_DLQ_1", "amount": 50000, "currency": "INR"}
            )
        )
        record = await service.create_or_get(CreateOrderRequest(receipt="r-dlq-1"))
    async with live_pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT * FROM orders WHERE id = $1 FOR UPDATE", record.id
            )
            for step in (OrderStatus.ATTEMPTED, OrderStatus.PAID):
                await service.transition(
                    conn,
                    order_id=record.id,
                    tenant_id=record.tenant_id,
                    new_status=step,
                    trigger="test_setup",
                )
    return record


async def test_paid_order_transitions_to_fulfillment_failed(
    live_pool, service: OrderService, paid_order: Any
) -> None:
    transitioned = await mark_fulfillment_failed(
        live_pool,
        service,
        order_id=paid_order.id,
        tenant_id=paid_order.tenant_id,
        reason="sqs_retries_exhausted",
        message_id="msg_DLQ_001",
    )
    assert transitioned is True

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, fulfillment_failed_at FROM orders WHERE id = $1",
            paid_order.id,
        )
        evt = await conn.fetchrow(
            "SELECT metadata FROM order_events "
            "WHERE order_id = $1 AND to_status = 'fulfillment_failed'",
            paid_order.id,
        )
    assert row["status"] == "fulfillment_failed"
    assert row["fulfillment_failed_at"] is not None
    assert evt["metadata"]["reason"] == "sqs_retries_exhausted"
    assert evt["metadata"]["dlq_message_id"] == "msg_DLQ_001"


async def test_already_fulfillment_failed_is_noop(
    live_pool, service: OrderService, paid_order: Any
) -> None:
    # First call transitions.
    await mark_fulfillment_failed(
        live_pool, service,
        order_id=paid_order.id, tenant_id=paid_order.tenant_id,
        reason="first", message_id="msg_1",
    )
    # Second call no-ops.
    transitioned = await mark_fulfillment_failed(
        live_pool, service,
        order_id=paid_order.id, tenant_id=paid_order.tenant_id,
        reason="second", message_id="msg_2",
    )
    assert transitioned is False

    async with live_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM order_events "
            "WHERE order_id = $1 AND to_status = 'fulfillment_failed'",
            paid_order.id,
        )
    # Only one transition recorded.
    assert count == 1


async def test_unknown_order_is_noop(
    live_pool, service: OrderService
) -> None:
    transitioned = await mark_fulfillment_failed(
        live_pool, service,
        order_id=uuid.uuid4(), tenant_id="t_phase5",
        reason="missing", message_id="msg_X",
    )
    assert transitioned is False


async def test_order_in_unexpected_state_is_noop(
    live_pool, service: OrderService, paid_order: Any
) -> None:
    # Advance to fulfilled, which is unexpected for the DLQ entry path.
    async with live_pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT * FROM orders WHERE id = $1 FOR UPDATE", paid_order.id
            )
            await service.transition(
                conn,
                order_id=paid_order.id,
                tenant_id=paid_order.tenant_id,
                new_status=OrderStatus.FULFILLED,
                trigger="test_setup",
            )

    transitioned = await mark_fulfillment_failed(
        live_pool, service,
        order_id=paid_order.id, tenant_id=paid_order.tenant_id,
        reason="unexpected", message_id="msg_U",
    )
    assert transitioned is False
