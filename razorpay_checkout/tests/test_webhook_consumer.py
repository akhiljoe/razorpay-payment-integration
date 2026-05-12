"""Tests for WebhookConsumer against a live Postgres.

Marked `requires_db`. Razorpay HTTP calls during order creation are mocked
via respx; SQS publishes from the consumer are stubbed via AsyncMock so we
can assert publish-after-commit semantics without LocalStack here.

Live LocalStack-backed publish coverage lives in `tests/test_queue.py`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from razorpay_checkout.amounts import FixedAmountResolver
from razorpay_checkout.client import RazorpayClient
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.models import CreateOrderRequest
from razorpay_checkout.orders import OrderService
from razorpay_checkout.state_machine import OrderStatus
from razorpay_checkout.webhook_consumer import (
    ConsumerEnvelopeError,
    WebhookConsumer,
)
from razorpay_checkout.webhook_idempotency import SkipDecision

pytestmark = pytest.mark.requires_db


WEBHOOK_SECRET = "phase8_consumer_secret"


# ---------- Helpers ----------


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _envelope(
    *,
    tenant_id: str,
    event: str,
    event_id: str,
    payload: dict[str, Any],
    received_at: str | None = None,
    signature: str = "test-signature",
) -> str:
    return json.dumps(
        {
            "envelope_version": 1,
            "tenant_id": tenant_id,
            "event_id": event_id,
            "event": event,
            "received_at": received_at or _now_iso(),
            "signature": signature,
            "payload": payload,
        }
    )


def _captured_payload(*, order_id: str, payment_id: str, amount: int) -> dict[str, Any]:
    return {
        "id": f"evt_{payment_id}",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "method": "card",
                    "status": "captured",
                }
            }
        },
    }


def _authorized_payload(*, order_id: str, payment_id: str, amount: int) -> dict[str, Any]:
    return {
        "id": f"evt_{payment_id}",
        "event": "payment.authorized",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "method": "card",
                    "status": "authorized",
                }
            }
        },
    }


def _failed_payload(*, order_id: str, payment_id: str, amount: int) -> dict[str, Any]:
    return {
        "id": f"evt_{payment_id}",
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "method": "card",
                    "status": "failed",
                }
            }
        },
    }


def _refund_payload(
    *, order_id: str, payment_id: str, refund_id: str, amount: int
) -> dict[str, Any]:
    return {
        "id": f"evt_{refund_id}",
        "event": "refund.processed",
        "payload": {
            "refund": {
                "entity": {
                    "id": refund_id,
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "processed",
                }
            }
        },
    }


def _order_paid_payload(*, order_id: str, payment_id: str, amount: int) -> dict[str, Any]:
    return {
        "id": f"evt_op_{order_id}",
        "event": "order.paid",
        "payload": {
            "order": {"entity": {"id": order_id}},
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                }
            },
        },
    }


# ---------- Fixtures ----------


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
def stub_sqs() -> AsyncMock:
    mock = AsyncMock()
    mock.send_message = AsyncMock(return_value={"MessageId": "msg_FULFILLMENT_001"})
    return mock


@pytest.fixture
async def consumer(
    live_pool, service: OrderService, stub_sqs: AsyncMock
) -> WebhookConsumer:
    return WebhookConsumer(
        pool=live_pool,
        order_service=service,
        sqs_client=stub_sqs,
        fulfillment_queue_url="http://localhost:4566/0/fulfillment-queue.fifo",
    )


@pytest.fixture
async def created_order(service: OrderService) -> Any:
    """Insert an order in `created` state via the real OrderService."""
    with respx.mock() as mock_router:
        mock_router.post("https://api.razorpay.com/v1/orders").mock(
            return_value=httpx.Response(
                200, json={"id": "order_CONS_1", "amount": 50000, "currency": "INR"}
            )
        )
        return await service.create_or_get(CreateOrderRequest(receipt="r-cons-1"))


async def _advance_to(
    live_pool, service: OrderService, order_id: uuid.UUID,
    tenant_id: str, target: OrderStatus,
) -> None:
    """Walk an order forward through legal transitions to `target`."""
    chain = {
        OrderStatus.CREATED: [],
        OrderStatus.ATTEMPTED: [OrderStatus.ATTEMPTED],
        OrderStatus.AUTHORIZED: [OrderStatus.ATTEMPTED, OrderStatus.AUTHORIZED],
        OrderStatus.PAID: [OrderStatus.ATTEMPTED, OrderStatus.PAID],
        OrderStatus.FAILED: [OrderStatus.ATTEMPTED, OrderStatus.FAILED],
    }
    steps = chain.get(target)
    if steps is None:
        raise ValueError(f"unsupported advance target {target!r}")
    async with live_pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT * FROM orders WHERE id = $1 FOR UPDATE", order_id
            )
            for step in steps:
                await service.transition(
                    conn,
                    order_id=order_id,
                    tenant_id=tenant_id,
                    new_status=step,
                    trigger="test_setup",
                )


# ---------- Envelope handling ----------


async def test_malformed_envelope_raises(consumer: WebhookConsumer) -> None:
    with pytest.raises(ConsumerEnvelopeError):
        await consumer.process_record(message_body="not json")


async def test_missing_envelope_field_raises(consumer: WebhookConsumer) -> None:
    with pytest.raises(ConsumerEnvelopeError):
        await consumer.process_record(
            message_body=json.dumps({"tenant_id": "t", "event_id": "e"})
        )


# ---------- Fast-path dedup ----------


async def test_duplicate_event_id_is_short_circuited(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
) -> None:
    payload = _captured_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_DUP", amount=50000
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="payment.captured", event_id="evt_DUP",
        payload=payload,
    )
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.ATTEMPTED,
    )
    decision1 = await consumer.process_record(message_body=body)
    decision2 = await consumer.process_record(message_body=body)
    assert decision1 is SkipDecision.PROCEED
    assert decision2 is SkipDecision.SKIP_ALREADY_APPLIED


# ---------- payment.authorized ----------


async def test_payment_authorized_transitions_and_no_payments_row(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
    stub_sqs: AsyncMock,
) -> None:
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.ATTEMPTED,
    )
    payload = _authorized_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_AUTH", amount=50000
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="payment.authorized",
        event_id="evt_AUTH", payload=payload,
    )
    decision = await consumer.process_record(message_body=body)
    assert decision is SkipDecision.PROCEED

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
        payment_count = await conn.fetchval(
            "SELECT count(*) FROM payments WHERE order_id = $1", created_order.id
        )
    assert row["status"] == "authorized"
    assert payment_count == 0
    stub_sqs.send_message.assert_not_called()


# ---------- payment.captured ----------


async def test_payment_captured_transitions_and_publishes_fulfillment(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
    stub_sqs: AsyncMock,
) -> None:
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.ATTEMPTED,
    )
    payload = _captured_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_CAP", amount=50000
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="payment.captured",
        event_id="evt_CAP", payload=payload,
    )
    decision = await consumer.process_record(
        message_body=body,
        message_attributes={"correlation_id": {"stringValue": "corr_CAP"}},
    )
    assert decision is SkipDecision.PROCEED

    async with live_pool.acquire() as conn:
        order_row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
        payment_row = await conn.fetchrow(
            "SELECT status, is_authoritative, attempt_number "
            "FROM payments WHERE razorpay_payment_id = $1",
            "pay_CAP",
        )
    assert order_row["status"] == "paid"
    assert payment_row["status"] == "captured"
    assert payment_row["is_authoritative"] is True
    assert payment_row["attempt_number"] == 1

    stub_sqs.send_message.assert_awaited_once()
    sent = stub_sqs.send_message.await_args.kwargs
    assert sent["MessageGroupId"] == str(created_order.id)
    attrs = sent["MessageAttributes"]
    assert attrs["correlation_id"]["StringValue"] == "corr_CAP"


async def test_payment_captured_via_failed_recovery(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
) -> None:
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.FAILED,
    )
    payload = _captured_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_REC", amount=50000
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="payment.captured",
        event_id="evt_REC", payload=payload,
    )
    await consumer.process_record(message_body=body)

    async with live_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT from_status, to_status FROM order_events "
            "WHERE order_id = $1 AND trigger = 'webhook' ORDER BY created_at",
            created_order.id,
        )
        order_row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
    assert order_row["status"] == "paid"
    # Two webhook-triggered transitions: failed -> attempted, attempted -> paid
    transitions = [(e["from_status"], e["to_status"]) for e in events]
    assert ("failed", "attempted") in transitions
    assert ("attempted", "paid") in transitions


async def test_payment_captured_publish_failure_keeps_order_paid(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
    stub_sqs: AsyncMock,
) -> None:
    stub_sqs.send_message = AsyncMock(side_effect=RuntimeError("sqs down"))
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.ATTEMPTED,
    )
    payload = _captured_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_PUBFAIL", amount=50000
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="payment.captured",
        event_id="evt_PUBFAIL", payload=payload,
    )
    decision = await consumer.process_record(message_body=body)
    assert decision is SkipDecision.PROCEED  # commit happened; publish failure is logged

    async with live_pool.acquire() as conn:
        order_row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
    assert order_row["status"] == "paid"


# ---------- payment.failed ----------


async def test_payment_failed_transitions(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
) -> None:
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.ATTEMPTED,
    )
    payload = _failed_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_F", amount=50000
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="payment.failed",
        event_id="evt_F", payload=payload,
    )
    await consumer.process_record(message_body=body)

    async with live_pool.acquire() as conn:
        order_row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
        payment_row = await conn.fetchrow(
            "SELECT status, is_authoritative FROM payments WHERE razorpay_payment_id = $1",
            "pay_F",
        )
    assert order_row["status"] == "failed"
    assert payment_row["is_authoritative"] is False


async def test_payment_failed_stale_on_paid_is_noop(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
) -> None:
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.PAID,
    )
    payload = _failed_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_STALE", amount=50000
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="payment.failed",
        event_id="evt_STALE", payload=payload,
    )
    decision = await consumer.process_record(message_body=body)
    assert decision is SkipDecision.STALE_NO_OP

    async with live_pool.acquire() as conn:
        order_row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
    assert order_row["status"] == "paid"


# ---------- refund.processed ----------


async def test_refund_processed_updates_payment_and_inserts_refund(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
) -> None:
    # First capture the order via the consumer so payments row exists.
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.ATTEMPTED,
    )
    captured = _captured_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_REFA", amount=50000
    )
    await consumer.process_record(
        message_body=_envelope(
            tenant_id=db_config.tenant_id, event="payment.captured",
            event_id="evt_CAP_REFA", payload=captured,
        )
    )

    refund = _refund_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_REFA",
        refund_id="rfnd_REFA", amount=50000,
    )
    await consumer.process_record(
        message_body=_envelope(
            tenant_id=db_config.tenant_id, event="refund.processed",
            event_id="evt_REF_REFA", payload=refund,
        )
    )

    async with live_pool.acquire() as conn:
        order_row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
        payment_row = await conn.fetchrow(
            "SELECT status, refunded_at FROM payments WHERE razorpay_payment_id = $1",
            "pay_REFA",
        )
        refund_count = await conn.fetchval(
            "SELECT count(*) FROM refunds WHERE razorpay_refund_id = $1",
            "rfnd_REFA",
        )
    assert order_row["status"] == "refunded"
    assert payment_row["status"] == "refunded"
    assert payment_row["refunded_at"] is not None
    assert refund_count == 1


async def test_refund_for_unknown_payment_flags_event_failed(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
) -> None:
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.PAID,
    )
    refund = _refund_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_UNKNOWN",
        refund_id="rfnd_X", amount=50000,
    )
    await consumer.process_record(
        message_body=_envelope(
            tenant_id=db_config.tenant_id, event="refund.processed",
            event_id="evt_REF_X", payload=refund,
        )
    )
    async with live_pool.acquire() as conn:
        order_row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
        event_row = await conn.fetchrow(
            "SELECT processing_status FROM webhook_events WHERE event_id = $1",
            "evt_REF_X",
        )
    assert order_row["status"] == "paid"  # unchanged
    assert event_row["processing_status"] == "failed"


# ---------- order.paid ----------


async def test_order_paid_is_log_only(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
) -> None:
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.PAID,
    )
    payload = _order_paid_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_OP", amount=50000
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="order.paid",
        event_id="evt_OP", payload=payload,
    )
    decision = await consumer.process_record(message_body=body)
    assert decision is SkipDecision.PROCEED

    async with live_pool.acquire() as conn:
        order_row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
        events = await conn.fetch(
            "SELECT to_status FROM order_events WHERE order_id = $1 AND trigger = 'webhook'",
            created_order.id,
        )
    assert order_row["status"] == "paid"
    # No new order_events from the order.paid event.
    assert "order_paid" not in {e["to_status"] for e in events}


# ---------- Amount mismatch ----------


async def test_amount_mismatch_flags_event_failed_no_transition(
    consumer: WebhookConsumer, live_pool, created_order: Any, db_config: RazorpayConfig,
) -> None:
    await _advance_to(
        live_pool, consumer._service, created_order.id, db_config.tenant_id,
        OrderStatus.ATTEMPTED,
    )
    # Wrong amount: order is 50000, event says 99999.
    payload = _captured_payload(
        order_id=created_order.razorpay_order_id, payment_id="pay_MIS", amount=99999
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="payment.captured",
        event_id="evt_MIS", payload=payload,
    )
    await consumer.process_record(message_body=body)

    async with live_pool.acquire() as conn:
        order_row = await conn.fetchrow(
            "SELECT status FROM orders WHERE id = $1", created_order.id
        )
        event_row = await conn.fetchrow(
            "SELECT processing_status, error FROM webhook_events WHERE event_id = $1",
            "evt_MIS",
        )
    assert order_row["status"] == "attempted"  # unchanged
    assert event_row["processing_status"] == "failed"
    assert "amount_mismatch" in (event_row["error"] or "")


# ---------- Unknown order ----------


async def test_unknown_order_flags_event_failed(
    consumer: WebhookConsumer, db_config: RazorpayConfig,
) -> None:
    payload = _captured_payload(
        order_id="order_DOES_NOT_EXIST", payment_id="pay_NOEX", amount=50000
    )
    body = _envelope(
        tenant_id=db_config.tenant_id, event="payment.captured",
        event_id="evt_NOEX", payload=payload,
    )
    decision = await consumer.process_record(message_body=body)
    assert decision is SkipDecision.PROCEED  # the handler marked failed and returned
