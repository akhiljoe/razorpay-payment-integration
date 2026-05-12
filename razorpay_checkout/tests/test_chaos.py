"""Chaos tests for the webhook consumer and queue plumbing.

Three scenarios the browser dashboard can't reliably reproduce:

1. **Concurrent same event**: 5 `process_record` calls in flight on the
   same `(tenant_id, event_id)` race the fast-path dedup and the FOR UPDATE
   primary guard. Exactly one paid transition must commit.

2. **Poison message → DLQ**: a malformed envelope sits in
   `webhook-events.fifo`. Every receive raises `ConsumerEnvelopeError`. After
   `maxReceiveCount=5` redeliveries the message lands in
   `webhook-events-dlq.fifo`. Proves SQS native retry + DLQ wiring.

3. **DB transient failure → eventual success**: the first two consumer
   attempts hit a flaky asyncpg pool (`acquire` raises). The third succeeds.
   Order reaches `paid` exactly once; no double-fulfillment despite the
   retries. Proves at-least-once retry semantics.

These tests are marked `requires_db` and/or `requires_localstack` so CI can
skip them when infra is missing.
"""

from __future__ import annotations

import asyncio
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
from razorpay_checkout.queue import (
    ensure_webhook_events_queues,
)
from razorpay_checkout.webhook_consumer import (
    WebhookConsumer,
)

# ---------- helpers ----------


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _captured_envelope(
    *,
    tenant_id: str,
    event_id: str,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    amount: int = 50000,
) -> str:
    """Build a canonical consumer-side envelope for `payment.captured`."""
    return json.dumps(
        {
            "envelope_version": 1,
            "tenant_id": tenant_id,
            "event_id": event_id,
            "event": "payment.captured",
            "received_at": _now_iso(),
            "signature": "test-signature",
            "payload": {
                "id": event_id,
                "event": "payment.captured",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": razorpay_payment_id,
                            "order_id": razorpay_order_id,
                            "amount": amount,
                            "currency": "INR",
                            "method": "card",
                            "status": "captured",
                        }
                    }
                },
            },
        }
    )


async def _seed_order(
    service: OrderService, receipt: str, razorpay_order_id: str
) -> Any:
    """Create an order in `created` state. Razorpay HTTP call is mocked."""
    with respx.mock() as mock_router:
        mock_router.post("https://api.razorpay.com/v1/orders").mock(
            return_value=httpx.Response(
                200, json={"id": razorpay_order_id, "amount": 50000, "currency": "INR"}
            )
        )
        return await service.create_or_get(CreateOrderRequest(receipt=receipt))


# ---------- shared fixtures (mirror test_webhook_consumer style) ----------


@pytest.fixture
def stub_sqs_for_consumer() -> AsyncMock:
    mock = AsyncMock()
    mock.send_message = AsyncMock(return_value={"MessageId": "msg_CHAOS_001"})
    return mock


@pytest.fixture
async def service(
    live_pool, db_config: RazorpayConfig, razorpay_client: RazorpayClient,
    fixed_resolver: FixedAmountResolver,
) -> OrderService:
    return OrderService(
        pool=live_pool, config=db_config,
        client=razorpay_client, amount_resolver=fixed_resolver,
    )


@pytest.fixture
async def consumer(
    live_pool, service: OrderService, stub_sqs_for_consumer: AsyncMock
) -> WebhookConsumer:
    return WebhookConsumer(
        pool=live_pool,
        order_service=service,
        sqs_client=stub_sqs_for_consumer,
        fulfillment_queue_url="http://localhost:4566/0/fulfillment-queue.fifo",
    )


# ============================================================
# 1. Concurrent same-event idempotency
# ============================================================


@pytest.mark.requires_db
async def test_concurrent_same_event_deduped(
    live_pool, service: OrderService, consumer: WebhookConsumer
) -> None:
    """5 concurrent process_record calls on the same (tenant_id, event_id)
    must produce exactly 1 paid transition, 1 webhook_events row, and 1
    authoritative payments row.

    This races the fast-path INSERT ... ON CONFLICT (tenant_id, event_id)
    DO NOTHING guard against itself. Postgres's per-row write serialization
    must let exactly one INSERT win; the other four hit the conflict and
    return SKIP_ALREADY_APPLIED.
    """
    order = await _seed_order(service, "chaos-conc-001", "order_chaos_conc_1")
    envelope = _captured_envelope(
        tenant_id=order.tenant_id,
        event_id=f"evt_chaos_conc_{uuid.uuid4().hex[:12]}",
        razorpay_order_id=order.razorpay_order_id,
        razorpay_payment_id="pay_chaos_conc_1",
    )

    # Fire 5 process_record coroutines concurrently. asyncio.gather kicks them
    # off in the same event loop tick, so they reach the INSERT in tight
    # succession. Postgres MVCC + UNIQUE (tenant_id, event_id) decides who wins.
    results = await asyncio.gather(
        *[consumer.process_record(message_body=envelope) for _ in range(5)],
        return_exceptions=True,
    )

    # No exceptions — duplicates are handled, not raised.
    for r in results:
        assert not isinstance(r, BaseException), f"unexpected exception: {r!r}"

    async with live_pool.acquire() as conn:
        wh_rows = await conn.fetch(
            "SELECT * FROM webhook_events WHERE tenant_id = $1 AND event_id = $2",
            order.tenant_id, json.loads(envelope)["event_id"],
        )
        assert len(wh_rows) == 1, f"expected 1 webhook_events row, got {len(wh_rows)}"

        paid_events = await conn.fetch(
            "SELECT * FROM order_events WHERE order_id = $1 AND to_status = 'paid'",
            order.id,
        )
        assert len(paid_events) == 1, (
            f"expected exactly 1 paid transition, got {len(paid_events)}"
        )

        payments = await conn.fetch(
            "SELECT * FROM payments WHERE order_id = $1",
            order.id,
        )
        assert len(payments) == 1, f"expected 1 payments row, got {len(payments)}"
        assert payments[0]["is_authoritative"] is True


# ============================================================
# 2. Poison message → DLQ after maxReceiveCount
# ============================================================


@pytest.mark.requires_localstack
async def test_poison_message_lands_in_dlq(sqs_client) -> None:
    """A malformed envelope cannot be processed. After 5 receives without
    a delete, SQS routes it to webhook-events-dlq.fifo.

    We simulate the consumer's retry-on-failure by calling receive_message
    in a tight loop with `VisibilityTimeout=1` so the message comes back
    immediately. After `maxReceiveCount=5` receives, LocalStack moves it
    to the DLQ.
    """
    main_url, dlq_url = await ensure_webhook_events_queues(sqs_client)
    # Start from a clean DLQ so the assertion at the end isn't polluted by
    # other tests.
    try:
        await sqs_client.purge_queue(QueueUrl=dlq_url)
    except Exception:  # noqa: BLE001 - 60s purge cooldown
        pass
    try:
        await sqs_client.purge_queue(QueueUrl=main_url)
    except Exception:  # noqa: BLE001
        pass

    # Publish raw garbage. FIFO requires MessageGroupId and dedup id.
    poison_id = f"poison-{uuid.uuid4().hex[:8]}"
    await sqs_client.send_message(
        QueueUrl=main_url,
        MessageBody="{not even valid json",
        MessageGroupId="chaos-poison",
        MessageDeduplicationId=poison_id,
    )

    # Simulate the consumer failing to process: receive, do not delete, repeat.
    # maxReceiveCount=5 means the 6th receive routes the message to DLQ.
    for _attempt in range(6):
        resp = await sqs_client.receive_message(
            QueueUrl=main_url,
            MaxNumberOfMessages=1,
            VisibilityTimeout=1,  # come back fast
            WaitTimeSeconds=2,
        )
        msgs = resp.get("Messages", [])
        if not msgs:
            # All 5 receives have been counted; further receives will pull
            # nothing from main because the message is now in DLQ.
            break
        # Pretend the consumer raised ConsumerEnvelopeError: drop without delete.
        # Make the message visible again immediately so we burn through retries.
        try:
            await sqs_client.change_message_visibility(
                QueueUrl=main_url,
                ReceiptHandle=msgs[0]["ReceiptHandle"],
                VisibilityTimeout=0,
            )
        except Exception:  # noqa: BLE001 - some receipts may be stale
            pass

    # Allow LocalStack a moment to move the message to DLQ.
    deadline = asyncio.get_event_loop().time() + 10.0
    dlq_depth = 0
    while asyncio.get_event_loop().time() < deadline:
        attrs = await sqs_client.get_queue_attributes(
            QueueUrl=dlq_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        dlq_depth = int(attrs["Attributes"].get("ApproximateNumberOfMessages", 0))
        if dlq_depth >= 1:
            break
        await asyncio.sleep(0.5)

    assert dlq_depth >= 1, f"poison message never reached DLQ (depth={dlq_depth})"


# ============================================================
# 3. DB transient failure → eventual success, no double-fulfillment
# ============================================================


class _FlakyPool:
    """Thin pass-through wrapper around `asyncpg.Pool` that raises a
    `ConnectionResetError` on the first call to `acquire`, then delegates.

    Used to simulate a transient DB drop without mutating the real pool
    (whose attributes are slot-bound and not monkeypatchable).
    """

    def __init__(self, real_pool: Any) -> None:
        self._real = real_pool
        self._calls = 0

    def acquire(self, *args: Any, **kwargs: Any) -> Any:
        self._calls += 1
        if self._calls == 1:
            raise ConnectionResetError("simulated transient DB drop")
        return self._real.acquire(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.mark.requires_db
async def test_db_transient_failure_eventual_success(
    live_pool, service: OrderService, stub_sqs_for_consumer: AsyncMock,
) -> None:
    """When the first consumer invocation raises a transient DB error, the
    second invocation (SQS re-delivery) must succeed and the order must
    transition to paid exactly once.

    The flaky pool wrapper raises on the first `acquire` and passes through
    on subsequent ones. The first `process_record` thus raises before any DB
    write; the second runs the standard happy path.
    """
    order = await _seed_order(service, "chaos-flaky-001", "order_chaos_flaky_1")
    envelope = _captured_envelope(
        tenant_id=order.tenant_id,
        event_id=f"evt_chaos_flaky_{uuid.uuid4().hex[:12]}",
        razorpay_order_id=order.razorpay_order_id,
        razorpay_payment_id="pay_chaos_flaky_1",
    )

    flaky_pool = _FlakyPool(live_pool)
    flaky_consumer = WebhookConsumer(
        pool=flaky_pool,  # type: ignore[arg-type]
        order_service=service,
        sqs_client=stub_sqs_for_consumer,
        fulfillment_queue_url="http://localhost:4566/0/fulfillment-queue.fifo",
    )

    with pytest.raises(ConnectionResetError):
        await flaky_consumer.process_record(message_body=envelope)

    # Second attempt: pool is healthy again. Must succeed and complete.
    await flaky_consumer.process_record(message_body=envelope)

    async with live_pool.acquire() as conn:
        paid_events = await conn.fetch(
            "SELECT * FROM order_events WHERE order_id = $1 AND to_status = 'paid'",
            order.id,
        )
        assert len(paid_events) == 1, (
            f"expected exactly 1 paid transition after retry, got {len(paid_events)}"
        )

        payments = await conn.fetch(
            "SELECT * FROM payments WHERE order_id = $1",
            order.id,
        )
        assert len(payments) == 1, (
            f"expected 1 payments row after retry, got {len(payments)}"
        )

        wh_rows = await conn.fetch(
            "SELECT processing_status FROM webhook_events WHERE tenant_id = $1",
            order.tenant_id,
        )
        # The successful (second) attempt writes one row with processing_status='done'.
        done_count = sum(1 for r in wh_rows if r["processing_status"] == "done")
        assert done_count == 1, f"expected 1 done webhook_event, got {done_count}"
