"""Webhook Consumer.

Triggered by `webhook-events.fifo` messages. Only component with Postgres
credentials. DESIGN_v5 Section 2.1 (steps 7 to 15) is the source of truth.

Pipeline per message:

1. Parse envelope; bind correlation_id from MessageAttributes.
2. Fast-path dedup: `INSERT INTO webhook_events ... ON CONFLICT DO NOTHING`
   using producer-supplied `received_at`.
3. Open DB transaction. `SELECT ... FOR UPDATE` on the order row.
4. Primary idempotency guard. SKIP -> mark `done`, commit, return.
5. Amount assertion for capture/authorize events.
6. Dispatch to event handler.
7. Commit transaction.
8. (Captured events only, after commit) publish to `fulfillment-queue.fifo`.

On any DB exception, the transaction rolls back and the SQS message is left
unacked. SQS retries via `maxReceiveCount=5`; after exhaustion the message
moves to `webhook-events-dlq.fifo` and the DLQ poller flags the event `dead`.

Razorpay's 500 ms SLA is bounded by the producer only; the consumer's runtime
is invisible to Razorpay.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg

from razorpay_checkout.correlation import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from razorpay_checkout.logging_config import get_logger
from razorpay_checkout.queue import SQSClient, publish_fulfillment_message
from razorpay_checkout.state_machine import OrderStatus
from razorpay_checkout.telemetry import (
    fulfillment_sqs_publish_failures,
    webhook_consumer_duration_ms,
    webhook_duplicate_count,
)
from razorpay_checkout.webhook_idempotency import SkipDecision, should_skip

if TYPE_CHECKING:
    from razorpay_checkout.orders import OrderService

_log = get_logger("razorpay_checkout.webhook_consumer")


def _parse_received_at(value: str) -> datetime:
    """Parse the envelope's ISO-8601 `received_at` to a `datetime`.

    asyncpg validates parameter types client-side. Passing a string to a
    TIMESTAMPTZ column raises `DataError` even with a `::timestamptz` cast in
    the SQL. See DESIGN_v5 Section 17.6.
    """
    # Python 3.11+ `fromisoformat` accepts the trailing `Z`.
    return datetime.fromisoformat(value)


def _record_duration(start_ns: int, tenant_id: str) -> None:
    """Emit webhook_consumer_duration_ms for the elapsed window."""
    import time as _time  # noqa: PLC0415
    elapsed_ms = (_time.perf_counter_ns() - start_ns) / 1_000_000.0
    webhook_consumer_duration_ms.record(elapsed_ms, {"tenant_id": tenant_id})


SUPPORTED_EVENTS = (
    "payment.authorized",
    "payment.captured",
    "payment.failed",
    "refund.processed",
    "order.paid",
)


class ConsumerEnvelopeError(Exception):
    """Raised when an SQS message body is missing required envelope fields."""


class AmountMismatchError(Exception):
    """Raised when event amount does not match the locked order's amount.

    Per DESIGN_v5 Section 2.1 step 11, the consumer DOES NOT raise this to
    Lambda. It is caught inside `process_record` and surfaces by setting
    `webhook_events.processing_status = 'failed'` and committing. Razorpay
    must not retry amount mismatches indefinitely.
    """


class WebhookConsumer:
    """Stateless consumer. One instance per Lambda container (or per worker)."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        order_service: OrderService,
        sqs_client: SQSClient,
        fulfillment_queue_url: str,
    ) -> None:
        self._pool = pool
        self._service = order_service
        self._sqs = sqs_client
        self._fulfillment_queue_url = fulfillment_queue_url

    async def process_record(
        self,
        *,
        message_body: str,
        message_attributes: dict[str, Any] | None = None,
    ) -> SkipDecision:
        """Process one SQS record. Returns the SkipDecision the guard reached.

        Raises `ConsumerEnvelopeError` on malformed envelope so the message is
        unacked and routes to DLQ after retries (these indicate a producer
        bug, not a recoverable failure).
        """
        import time as _time  # noqa: PLC0415
        _start_ns = _time.perf_counter_ns()
        envelope = self._parse_envelope(message_body)
        cid = self._bind_correlation_id(message_attributes, envelope)

        tenant_id: str = envelope["tenant_id"]
        event_id: str = envelope["event_id"]
        event: str = envelope["event"]
        received_at: str = envelope["received_at"]
        payload: dict[str, Any] = envelope["payload"]

        if event not in SUPPORTED_EVENTS:
            _log.warning(
                "consumer_unknown_event",
                tenant_id=tenant_id,
                event_type=event,
                event_id=event_id,
            )
            return SkipDecision.SKIP_ALREADY_APPLIED

        async with self._pool.acquire() as conn:
            # Fast-path dedup. ON CONFLICT returns no row.
            inserted = await conn.fetchrow(
                """
                INSERT INTO webhook_events
                    (tenant_id, event_id, event, payload, correlation_id, received_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                ON CONFLICT (tenant_id, event_id) DO NOTHING
                RETURNING id
                """,
                tenant_id,
                event_id,
                event,
                payload,
                cid,
                _parse_received_at(received_at),
            )
            if inserted is None:
                webhook_duplicate_count.add(1, {"tenant_id": tenant_id, "kind": "event_id"})
                _log.info(
                    "webhook_duplicate_event_id",
                    tenant_id=tenant_id,
                    event_id=event_id,
                )
                _record_duration(_start_ns, tenant_id)
                return SkipDecision.SKIP_ALREADY_APPLIED
            webhook_event_id: UUID = inserted["id"]

            # Open the application-level transaction. Side effects after commit
            # are emitted by the orchestrator below.
            handler_outcome = await self._run_in_transaction(
                conn=conn,
                webhook_event_id=webhook_event_id,
                tenant_id=tenant_id,
                event=event,
                payload=payload,
            )

        # After-commit side effects (fulfillment publish).
        if handler_outcome.fulfillment_payload is not None:
            try:
                await publish_fulfillment_message(
                    sqs_client=self._sqs,
                    queue_url=self._fulfillment_queue_url,
                    correlation_id=cid,
                    **handler_outcome.fulfillment_payload,
                )
            except Exception as exc:  # noqa: BLE001 - log and continue
                fulfillment_sqs_publish_failures.add(1, {"tenant_id": tenant_id})
                _log.error(
                    "fulfillment_sqs_publish_failed",
                    tenant_id=tenant_id,
                    event_id=event_id,
                    error=str(exc),
                )
                # Order stays in `paid`; stale-paid monitor catches it (Section 7).

        _record_duration(_start_ns, tenant_id)
        return handler_outcome.decision

    # ---------- envelope parsing ----------

    def _parse_envelope(self, message_body: str) -> dict[str, Any]:
        try:
            envelope: Any = json.loads(message_body)
        except json.JSONDecodeError as exc:
            raise ConsumerEnvelopeError(f"invalid JSON: {exc.msg}") from exc

        if not isinstance(envelope, dict):
            raise ConsumerEnvelopeError("envelope is not a JSON object")

        required = ("tenant_id", "event_id", "event", "received_at", "payload")
        for key in required:
            if key not in envelope:
                raise ConsumerEnvelopeError(f"envelope missing field {key!r}")
        if not isinstance(envelope["payload"], dict):
            raise ConsumerEnvelopeError("envelope.payload is not an object")
        return envelope

    def _bind_correlation_id(
        self,
        message_attributes: dict[str, Any] | None,
        envelope: dict[str, Any],  # noqa: ARG002 - reserved for future use
    ) -> str:
        """Re-bind correlation_id from SQS MessageAttributes (Critical Invariant 9).

        Accepts both shapes:
          - Raw SQS dict shape (Lambda event-source mapping):
            ``{"correlation_id": {"StringValue": "uuid", "DataType": "String"}}``
          - Flattened shape (custom pumps / boto3 helpers):
            ``{"correlation_id": "uuid"}``

        Falling through both yields the contextvar's value (set by an outer
        scope) or a fresh UUID. The fresh-UUID fall-through must never silently
        replace an inbound correlation_id; that breaks end-to-end tracing.
        """
        attrs = message_attributes or {}
        attr = attrs.get("correlation_id")
        cid_from_attr: str | None = None
        if isinstance(attr, dict):
            cid_from_attr = (
                attr.get("stringValue")
                or attr.get("StringValue")
                or attr.get("string_value")
            )
        elif isinstance(attr, str) and attr:
            cid_from_attr = attr
        cid = cid_from_attr or get_correlation_id() or new_correlation_id()
        set_correlation_id(cid)
        return cid

    # ---------- transaction orchestration ----------

    async def _run_in_transaction(
        self,
        *,
        conn: asyncpg.Connection,
        webhook_event_id: UUID,
        tenant_id: str,
        event: str,
        payload: dict[str, Any],
    ) -> _HandlerOutcome:
        async with conn.transaction():
            razorpay_order_id = _extract_razorpay_order_id(payload, event)
            order_row = await conn.fetchrow(
                """
                SELECT id, status, amount, tenant_id, razorpay_order_id
                  FROM orders
                 WHERE razorpay_order_id = $1 AND tenant_id = $2
                 FOR UPDATE
                """,
                razorpay_order_id,
                tenant_id,
            )
            if order_row is None:
                # Unknown order. Mark event failed; manual reconcile/replay required.
                await self._mark_event_failed(
                    conn, webhook_event_id, reason="unknown_order"
                )
                _log.warning(
                    "consumer_unknown_order",
                    tenant_id=tenant_id,
                    razorpay_order_id=razorpay_order_id,
                    event_type=event,
                )
                return _HandlerOutcome(SkipDecision.PROCEED, None)

            order_id: UUID = order_row["id"]
            order_status = OrderStatus(order_row["status"])

            # Primary guard.
            captured_exists = False
            if event == "payment.captured":
                razorpay_payment_id = (
                    payload.get("payload", {}).get("payment", {}).get("entity", {}).get("id")
                )
                if razorpay_payment_id:
                    captured_exists = await self._captured_payment_exists(
                        conn, tenant_id, razorpay_payment_id
                    )
            decision = should_skip(
                event, order_status, captured_payment_exists=captured_exists
            )
            if decision is not SkipDecision.PROCEED:
                await self._mark_event_done(
                    conn, webhook_event_id, note=decision.value
                )
                _log.info(
                    "consumer_skipped",
                    tenant_id=tenant_id,
                    order_id=str(order_id),
                    event_type=event,
                    decision=decision.value,
                )
                return _HandlerOutcome(decision, None)

            # Amount assertion (capture and authorize events only).
            if event in {"payment.authorized", "payment.captured"}:
                event_amount = (
                    payload.get("payload", {})
                    .get("payment", {})
                    .get("entity", {})
                    .get("amount")
                )
                if event_amount != order_row["amount"]:
                    await self._mark_event_failed(
                        conn,
                        webhook_event_id,
                        reason=f"amount_mismatch: event={event_amount} order={order_row['amount']}",
                    )
                    _log.error(
                        "consumer_amount_mismatch",
                        tenant_id=tenant_id,
                        order_id=str(order_id),
                        event_type=event,
                        event_amount=event_amount,
                        order_amount=order_row["amount"],
                    )
                    return _HandlerOutcome(SkipDecision.PROCEED, None)

            # Dispatch to the event handler.
            outcome = await self._dispatch_handler(
                conn=conn,
                webhook_event_id=webhook_event_id,
                tenant_id=tenant_id,
                order_id=order_id,
                order_status=order_status,
                order_amount=order_row["amount"],
                event=event,
                payload=payload,
            )
            if not outcome.event_already_marked:
                await self._mark_event_done(conn, webhook_event_id)
            return outcome

    async def _dispatch_handler(
        self,
        *,
        conn: asyncpg.Connection,
        webhook_event_id: UUID,
        tenant_id: str,
        order_id: UUID,
        order_status: OrderStatus,
        order_amount: int,
        event: str,
        payload: dict[str, Any],
    ) -> _HandlerOutcome:
        # Late import to avoid circular module load.
        from razorpay_checkout.webhook_handlers import (  # noqa: PLC0415
            handle_order_paid,
            handle_payment_authorized,
            handle_payment_captured,
            handle_payment_failed,
            handle_refund_processed,
        )

        ctx = HandlerContext(
            conn=conn,
            service=self._service,
            webhook_event_id=webhook_event_id,
            tenant_id=tenant_id,
            order_id=order_id,
            order_status=order_status,
            order_amount=order_amount,
            payload=payload,
        )
        if event == "payment.authorized":
            return await handle_payment_authorized(ctx)
        if event == "payment.captured":
            return await handle_payment_captured(ctx)
        if event == "payment.failed":
            return await handle_payment_failed(ctx)
        if event == "refund.processed":
            return await handle_refund_processed(ctx)
        if event == "order.paid":
            return await handle_order_paid(ctx)
        # SUPPORTED_EVENTS guards against this.
        raise AssertionError(f"unreachable event {event!r}")

    # ---------- helpers ----------

    async def _captured_payment_exists(
        self, conn: asyncpg.Connection, tenant_id: str, razorpay_payment_id: str
    ) -> bool:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM payments
             WHERE tenant_id = $1
               AND razorpay_payment_id = $2
               AND is_authoritative = TRUE
             LIMIT 1
            """,
            tenant_id,
            razorpay_payment_id,
        )
        return row is not None

    async def _mark_event_done(
        self,
        conn: asyncpg.Connection,
        webhook_event_id: UUID,
        *,
        note: str | None = None,
    ) -> None:
        await conn.execute(
            """
            UPDATE webhook_events
               SET processing_status = 'done',
                   processed_at = NOW(),
                   attempts = attempts + 1,
                   error = $2
             WHERE id = $1
            """,
            webhook_event_id,
            note,
        )

    async def _mark_event_failed(
        self,
        conn: asyncpg.Connection,
        webhook_event_id: UUID,
        *,
        reason: str,
    ) -> None:
        await conn.execute(
            """
            UPDATE webhook_events
               SET processing_status = 'failed',
                   processed_at = NOW(),
                   attempts = attempts + 1,
                   error = $2
             WHERE id = $1
            """,
            webhook_event_id,
            reason,
        )


# ---------- Handler context and outcome ----------


class HandlerContext:
    """Bag of arguments passed to each event handler.

    Plain class (not Pydantic) to keep the hot path lean. Handlers receive a
    locked transaction and the order row; they may use the service to
    transition, INSERT rows, and produce a fulfillment payload for the
    after-commit publish step.
    """

    __slots__ = (
        "conn",
        "service",
        "webhook_event_id",
        "tenant_id",
        "order_id",
        "order_status",
        "order_amount",
        "payload",
    )

    def __init__(
        self,
        *,
        conn: asyncpg.Connection,
        service: OrderService,
        webhook_event_id: UUID,
        tenant_id: str,
        order_id: UUID,
        order_status: OrderStatus,
        order_amount: int,
        payload: dict[str, Any],
    ) -> None:
        self.conn = conn
        self.service = service
        self.webhook_event_id = webhook_event_id
        self.tenant_id = tenant_id
        self.order_id = order_id
        self.order_status = order_status
        self.order_amount = order_amount
        self.payload = payload


class _HandlerOutcome:
    """Return value from a handler.

    `fulfillment_payload` is a kwarg dict passed to `publish_fulfillment_message`
    if non-None, after the consumer commits the transaction. Only
    `handle_payment_captured` sets this (and only on a `paid` transition).

    `event_already_marked` tells the orchestrator to SKIP its own
    `_mark_event_done`. Set this when the handler has already written a
    `failed` row (e.g., refund for unknown payment).
    """

    __slots__ = ("decision", "fulfillment_payload", "event_already_marked")

    def __init__(
        self,
        decision: SkipDecision,
        fulfillment_payload: dict[str, Any] | None,
        *,
        event_already_marked: bool = False,
    ) -> None:
        self.decision = decision
        self.fulfillment_payload = fulfillment_payload
        self.event_already_marked = event_already_marked


def _extract_razorpay_order_id(payload: dict[str, Any], event: str) -> str:
    """Pull the razorpay_order_id from the event-specific entity.

    Mirrors `webhook_producer._extract_razorpay_order_id`. Kept duplicated
    rather than imported to keep the consumer importable even if producer
    refactors change its private API.
    """
    body = payload.get("payload")
    if not isinstance(body, dict):
        raise ConsumerEnvelopeError("envelope payload is malformed")

    if event.startswith("payment."):
        entity = body.get("payment", {}).get("entity", {})
        order_id = entity.get("order_id") if isinstance(entity, dict) else None
    elif event.startswith("refund."):
        entity = body.get("refund", {}).get("entity", {})
        order_id = entity.get("order_id") if isinstance(entity, dict) else None
    elif event == "order.paid":
        entity = body.get("order", {}).get("entity", {})
        order_id = entity.get("id") if isinstance(entity, dict) else None
    else:
        order_id = None

    if not isinstance(order_id, str) or not order_id:
        raise ConsumerEnvelopeError(
            f"could not extract razorpay_order_id for event {event!r}"
        )
    return order_id
