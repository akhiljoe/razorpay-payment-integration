"""Fulfillment Hook contract and FulfillmentProcessor.

DESIGN_v5 Section 7 and Section 11. Triggered by `fulfillment-queue.fifo`
messages produced by the Webhook Consumer after a successful `paid`
transition.

Responsibilities of `FulfillmentProcessor`:

1. Parse the SQS envelope, bind `correlation_id`.
2. Open DB transaction. `SELECT ... FOR UPDATE` on the order.
3. **Short-circuit** if `order.status == 'fulfilled'`. SQS at-least-once
   delivery can re-deliver after a prior success; the lock + status check
   makes this safe without requiring the hook to be reentrant in the
   strictest sense.
4. If `order.status` is `paid` (normal) or `fulfillment_failed` (admin retry),
   call `FulfillmentHook.fulfill`.
5. On hook success: transition to `fulfilled`. INSERT `order_events`.
6. On hook exception: roll back. SQS retries via `maxReceiveCount=5`. After
   exhaustion the message moves to `fulfillment-dlq.fifo` and the DLQ poller
   transitions the order to `fulfillment_failed`.

The hook is called **inside** the DB transaction. The tradeoff (holding a DB
connection during the hook's runtime) is acceptable at v1 volume. Hooks that
need to do long-running external I/O should accept this and use idempotent
upstream calls.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from razorpay_checkout.correlation import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from razorpay_checkout.logging_config import get_logger
from razorpay_checkout.state_machine import OrderStatus
from razorpay_checkout.telemetry import fulfillment_duration_ms

if TYPE_CHECKING:
    from razorpay_checkout.orders import OrderService

_log = get_logger("razorpay_checkout.fulfillment")


@runtime_checkable
class FulfillmentHook(Protocol):
    """Host-app-supplied fulfillment logic.

    Contract (DESIGN_v5 Section 11):

    - MUST be idempotent: SQS provides at-least-once delivery.
    - MUST be atomic: either all side-effects complete or raise.
    - On exception, SQS retries up to `maxReceiveCount=5`. After exhaustion the
      message moves to DLQ and the order is transitioned to
      `fulfillment_failed`.

    `metadata` contract: `{"correlation_id": str, "razorpay_payment_id": str}`.
    """

    async def fulfill(
        self,
        order_id: str,
        tenant_id: str,
        amount: int,
        metadata: dict[str, Any],
    ) -> None: ...


class DefaultFulfillmentHook:
    """No-op default. Logs and returns. Consuming apps inject their own."""

    async def fulfill(
        self,
        order_id: str,
        tenant_id: str,
        amount: int,
        metadata: dict[str, Any],
    ) -> None:
        _log.info(
            "default_fulfillment_no_op",
            order_id=order_id,
            tenant_id=tenant_id,
            amount=amount,
            correlation_id=metadata.get("correlation_id", ""),
            razorpay_payment_id=metadata.get("razorpay_payment_id", ""),
        )


class FulfillmentEnvelopeError(Exception):
    """Raised on a malformed fulfillment SQS envelope."""


class FulfillmentResult:
    """Result of one record. Lets callers tell short-circuit from real work."""

    SHORT_CIRCUITED = "short_circuited"
    FULFILLED = "fulfilled"
    UNEXPECTED_STATE = "unexpected_state"

    __slots__ = ("outcome", "order_id", "previous_status")

    def __init__(
        self,
        outcome: str,
        order_id: UUID | None = None,
        previous_status: OrderStatus | None = None,
    ) -> None:
        self.outcome = outcome
        self.order_id = order_id
        self.previous_status = previous_status


class FulfillmentProcessor:
    """Stateless processor. Wire one per Lambda container."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        order_service: OrderService,
        hook: FulfillmentHook,
    ) -> None:
        self._pool = pool
        self._service = order_service
        self._hook = hook

    async def process_record(
        self,
        *,
        message_body: str,
        message_attributes: dict[str, Any] | None = None,
    ) -> FulfillmentResult:
        import time as _time  # noqa: PLC0415

        _start_ns = _time.perf_counter_ns()
        envelope = self._parse_envelope(message_body)
        cid = self._bind_correlation_id(message_attributes)

        tenant_id: str = envelope["tenant_id"]
        order_id = UUID(envelope["order_id"])
        razorpay_payment_id: str = envelope.get("razorpay_payment_id", "")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, status, amount, tenant_id
                      FROM orders
                     WHERE id = $1 AND tenant_id = $2
                     FOR UPDATE
                    """,
                    order_id,
                    tenant_id,
                )
                if row is None:
                    _log.error(
                        "fulfillment_unknown_order",
                        tenant_id=tenant_id,
                        order_id=str(order_id),
                    )
                    return FulfillmentResult(FulfillmentResult.UNEXPECTED_STATE, order_id, None)

                current = OrderStatus(row["status"])

                # Short-circuit: order already fulfilled by a prior delivery.
                if current is OrderStatus.FULFILLED:
                    _log.info(
                        "fulfillment_short_circuit_already_fulfilled",
                        tenant_id=tenant_id,
                        order_id=str(order_id),
                    )
                    return FulfillmentResult(FulfillmentResult.SHORT_CIRCUITED, order_id, current)

                # Only `paid` and `fulfillment_failed` are valid entry states.
                if current not in (OrderStatus.PAID, OrderStatus.FULFILLMENT_FAILED):
                    _log.warning(
                        "fulfillment_unexpected_state",
                        tenant_id=tenant_id,
                        order_id=str(order_id),
                        order_status=current.value,
                    )
                    return FulfillmentResult(FulfillmentResult.UNEXPECTED_STATE, order_id, current)

                metadata: dict[str, Any] = {
                    "correlation_id": cid,
                    "razorpay_payment_id": razorpay_payment_id,
                }

                # Hook runs inside the transaction. Exceptions propagate up so
                # SQS retries via maxReceiveCount.
                await self._hook.fulfill(
                    order_id=str(order_id),
                    tenant_id=tenant_id,
                    amount=row["amount"],
                    metadata=metadata,
                )

                await self._service.transition(
                    conn,
                    order_id=order_id,
                    tenant_id=tenant_id,
                    new_status=OrderStatus.FULFILLED,
                    trigger="fulfillment",
                    metadata={
                        "razorpay_payment_id": razorpay_payment_id,
                        "from_status": current.value,
                    },
                )

        _elapsed_ms = (_time.perf_counter_ns() - _start_ns) / 1_000_000.0
        fulfillment_duration_ms.record(_elapsed_ms, {"tenant_id": tenant_id})
        _log.info(
            "fulfillment_completed",
            tenant_id=tenant_id,
            order_id=str(order_id),
            from_status=current.value,
        )
        return FulfillmentResult(FulfillmentResult.FULFILLED, order_id, current)

    # ---------- helpers ----------

    def _parse_envelope(self, message_body: str) -> dict[str, Any]:
        try:
            envelope: Any = json.loads(message_body)
        except json.JSONDecodeError as exc:
            raise FulfillmentEnvelopeError(f"invalid JSON: {exc.msg}") from exc
        if not isinstance(envelope, dict):
            raise FulfillmentEnvelopeError("envelope is not a JSON object")
        for key in ("order_id", "tenant_id"):
            if key not in envelope:
                raise FulfillmentEnvelopeError(f"envelope missing field {key!r}")
        return envelope

    def _bind_correlation_id(self, message_attributes: dict[str, Any] | None) -> str:
        """Re-bind correlation_id from SQS MessageAttributes (Critical Invariant 9).

        Mirrors `WebhookConsumer._bind_correlation_id`. Accepts both the raw
        SQS dict shape (``{"StringValue": "uuid"}``) used by Lambda's
        event-source mapping AND the flattened string shape used by custom
        pumps and most boto3 helpers.
        """
        attrs = message_attributes or {}
        attr = attrs.get("correlation_id")
        cid_from_attr: str | None = None
        if isinstance(attr, dict):
            cid_from_attr = (
                attr.get("stringValue") or attr.get("StringValue") or attr.get("string_value")
            )
        elif isinstance(attr, str) and attr:
            cid_from_attr = attr
        cid = cid_from_attr or get_correlation_id() or new_correlation_id()
        set_correlation_id(cid)
        return cid
