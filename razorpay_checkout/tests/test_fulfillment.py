"""Tests for FulfillmentHook protocol, DefaultFulfillmentHook, and FulfillmentProcessor.

Marked `requires_db` because FulfillmentProcessor uses a real Postgres
transaction. Razorpay HTTP calls are mocked via respx; the hook is replaced
by AsyncMock so we can assert it was called or made to raise.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from razorpay_checkout.amounts import FixedAmountResolver
from razorpay_checkout.client import RazorpayClient
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.fulfillment import (
    DefaultFulfillmentHook,
    FulfillmentEnvelopeError,
    FulfillmentProcessor,
    FulfillmentResult,
)
from razorpay_checkout.models import CreateOrderRequest
from razorpay_checkout.orders import OrderService
from razorpay_checkout.state_machine import OrderStatus

pytestmark = pytest.mark.requires_db


# ---------- Default no-op hook ----------


async def test_default_fulfillment_hook_logs_and_returns() -> None:
    hook = DefaultFulfillmentHook()
    # Just confirms it runs without raising and returns None.
    result = await hook.fulfill(
        order_id="o1",
        tenant_id="t1",
        amount=100,
        metadata={"correlation_id": "c1", "razorpay_payment_id": "p1"},
    )
    assert result is None


# ---------- Helpers / fixtures ----------


def _envelope(order_id: str, tenant_id: str, payment_id: str = "pay_X") -> str:
    return json.dumps(
        {
            "envelope_version": 1,
            "order_id": order_id,
            "tenant_id": tenant_id,
            "razorpay_payment_id": payment_id,
        }
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


@pytest.fixture
async def created_order(service: OrderService) -> Any:
    with respx.mock() as mock_router:
        mock_router.post("https://api.razorpay.com/v1/orders").mock(
            return_value=httpx.Response(
                200, json={"id": "order_FUL_1", "amount": 50000, "currency": "INR"}
            )
        )
        return await service.create_or_get(CreateOrderRequest(receipt="r-ful-1"))


async def _advance_to(
    live_pool, service: OrderService, order_id: uuid.UUID,
    tenant_id: str, target: OrderStatus,
) -> None:
    chain = {
        OrderStatus.PAID: [OrderStatus.ATTEMPTED, OrderStatus.PAID],
        OrderStatus.FULFILLED: [OrderStatus.ATTEMPTED, OrderStatus.PAID, OrderStatus.FULFILLED],
        OrderStatus.FULFILLMENT_FAILED: [
            OrderStatus.ATTEMPTED, OrderStatus.PAID, OrderStatus.FULFILLMENT_FAILED
        ],
    }
    async with live_pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT * FROM orders WHERE id = $1 FOR UPDATE", order_id
            )
            for step in chain[target]:
                await service.transition(
                    conn,
                    order_id=order_id,
                    tenant_id=tenant_id,
                    new_status=step,
                    trigger="test_setup",
                )


@pytest.fixture
def stub_hook() -> AsyncMock:
    mock = AsyncMock()
    mock.fulfill = AsyncMock(return_value=None)
    return mock


@pytest.fixture
async def processor(
    live_pool, service: OrderService, stub_hook: AsyncMock
) -> FulfillmentProcessor:
    return FulfillmentProcessor(
        pool=live_pool, order_service=service, hook=stub_hook
    )


# ---------- Envelope errors ----------


async def test_malformed_envelope_raises(processor: FulfillmentProcessor) -> None:
    with pytest.raises(FulfillmentEnvelopeError):
        await processor.process_record(message_body="not json")


async def test_missing_order_id_raises(processor: FulfillmentProcessor) -> None:
    with pytest.raises(FulfillmentEnvelopeError):
        await processor.process_record(
            message_body=json.dumps({"tenant_id": "t1"})
        )


# ---------- Happy path: paid -> fulfilled ----------


async def test_paid_order_calls_hook_and_transitions_to_fulfilled(
    processor: FulfillmentProcessor, live_pool, created_order: Any,
    db_config: RazorpayConfig, stub_hook: AsyncMock,
) -> None:
    await _advance_to(
        live_pool, processor._service, created_order.id, db_config.tenant_id,
        OrderStatus.PAID,
    )
    result = await processor.process_record(
        message_body=_envelope(str(created_order.id), db_config.tenant_id),
        message_attributes={"correlation_id": {"stringValue": "corr_FUL"}},
    )
    assert result.outcome == FulfillmentResult.FULFILLED
    assert result.previous_status is OrderStatus.PAID

    stub_hook.fulfill.assert_awaited_once()
    kwargs = stub_hook.fulfill.await_args.kwargs
    assert kwargs["order_id"] == str(created_order.id)
    assert kwargs["tenant_id"] == db_config.tenant_id
    assert kwargs["metadata"]["correlation_id"] == "corr_FUL"
    assert kwargs["metadata"]["razorpay_payment_id"] == "pay_X"

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, fulfilled_at FROM orders WHERE id = $1",
            created_order.id,
        )
    assert row["status"] == "fulfilled"
    assert row["fulfilled_at"] is not None


# ---------- Short-circuit: already fulfilled ----------


async def test_already_fulfilled_short_circuits_without_hook_call(
    processor: FulfillmentProcessor, live_pool, created_order: Any,
    db_config: RazorpayConfig, stub_hook: AsyncMock,
) -> None:
    await _advance_to(
        live_pool, processor._service, created_order.id, db_config.tenant_id,
        OrderStatus.FULFILLED,
    )
    result = await processor.process_record(
        message_body=_envelope(str(created_order.id), db_config.tenant_id),
    )
    assert result.outcome == FulfillmentResult.SHORT_CIRCUITED
    stub_hook.fulfill.assert_not_called()


# ---------- Admin retry: fulfillment_failed -> fulfilled ----------


async def test_fulfillment_failed_order_recovers_via_processor(
    processor: FulfillmentProcessor, live_pool, created_order: Any,
    db_config: RazorpayConfig, stub_hook: AsyncMock,
) -> None:
    await _advance_to(
        live_pool, processor._service, created_order.id, db_config.tenant_id,
        OrderStatus.FULFILLMENT_FAILED,
    )
    result = await processor.process_record(
        message_body=_envelope(str(created_order.id), db_config.tenant_id),
    )
    assert result.outcome == FulfillmentResult.FULFILLED
    assert result.previous_status is OrderStatus.FULFILLMENT_FAILED
    stub_hook.fulfill.assert_awaited_once()

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
    assert row["status"] == "fulfilled"


# ---------- Hook exception: re-raises and rolls back ----------


async def test_hook_exception_rolls_back_and_reraises(
    processor: FulfillmentProcessor, live_pool, created_order: Any,
    db_config: RazorpayConfig, stub_hook: AsyncMock,
) -> None:
    await _advance_to(
        live_pool, processor._service, created_order.id, db_config.tenant_id,
        OrderStatus.PAID,
    )
    stub_hook.fulfill = AsyncMock(side_effect=RuntimeError("hook boom"))

    with pytest.raises(RuntimeError, match="hook boom"):
        await processor.process_record(
            message_body=_envelope(str(created_order.id), db_config.tenant_id),
        )

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
    # Rollback: order stays in paid.
    assert row["status"] == "paid"


# ---------- Unknown order ----------


async def test_unknown_order_returns_unexpected_state(
    processor: FulfillmentProcessor, db_config: RazorpayConfig,
    stub_hook: AsyncMock,
) -> None:
    result = await processor.process_record(
        message_body=_envelope(str(uuid.uuid4()), db_config.tenant_id),
    )
    assert result.outcome == FulfillmentResult.UNEXPECTED_STATE
    stub_hook.fulfill.assert_not_called()


# ---------- Unexpected state: order in attempted ----------


async def test_attempted_order_skips_with_unexpected_state(
    processor: FulfillmentProcessor, live_pool, created_order: Any,
    db_config: RazorpayConfig, stub_hook: AsyncMock,
) -> None:
    # Order is in `created` from fixture; bump only one step to `attempted`.
    async with live_pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT * FROM orders WHERE id = $1 FOR UPDATE", created_order.id
            )
            await processor._service.transition(
                conn,
                order_id=created_order.id,
                tenant_id=db_config.tenant_id,
                new_status=OrderStatus.ATTEMPTED,
                trigger="test_setup",
            )

    result = await processor.process_record(
        message_body=_envelope(str(created_order.id), db_config.tenant_id),
    )
    assert result.outcome == FulfillmentResult.UNEXPECTED_STATE
    stub_hook.fulfill.assert_not_called()
