"""Event handlers for the Webhook Consumer.

Each handler receives a `HandlerContext` carrying a locked order row, the
parsed Razorpay payload, and the `OrderService` for state transitions.
Handlers MUST NOT publish to SQS directly; they return a `_HandlerOutcome`
whose `fulfillment_payload` is None unless an after-commit publish is needed.

DESIGN_v5 Section 2.1 webhook event routing table:

| Event              | Action                                                                   |
|--------------------|--------------------------------------------------------------------------|
| payment.authorized | attempted -> authorized; no payments row; no SQS publish                 |
| payment.captured   | -> paid; INSERT payments is_authoritative=TRUE; publish fulfillment      |
| payment.failed     | -> failed; INSERT payments is_authoritative=FALSE; no SQS publish        |
| refund.processed   | -> refunded; UPDATE captured payments row; INSERT refunds; no SQS publish|
| order.paid         | log only; no state change                                                |

DESIGN_v5 Critical Invariant 8: DB commit before any side effect. The consumer
orchestrator enforces this by deferring the publish until after the
transaction's `__aexit__`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from razorpay_checkout.logging_config import get_logger
from razorpay_checkout.state_machine import OrderStatus
from razorpay_checkout.webhook_idempotency import SkipDecision

if TYPE_CHECKING:
    import asyncpg

    from razorpay_checkout.webhook_consumer import HandlerContext, _HandlerOutcome

_log = get_logger("razorpay_checkout.webhook_handlers")


# ---------- payment.authorized ----------


async def handle_payment_authorized(ctx: HandlerContext) -> _HandlerOutcome:
    """attempted -> authorized, with auto-hop from `created` when the widget
    skipped the verify-callback round trip.

    DESIGN_v5 Section 2.1 + 5.3. No payments row. No SQS publish. Amount
    already asserted by the orchestrator. The auto-hop preserves the strict
    external state machine: every order still visits `attempted`, but the
    consumer inserts it on the host's behalf when the widget's success
    callback never fires (server-to-server flows, refund-only integrations,
    or any host that doesn't call POST /payments/verify-callback). The
    `auto_attempted` metadata flag distinguishes implicit hops from real
    user-driven ones at forensic time.
    """
    from razorpay_checkout.webhook_consumer import _HandlerOutcome  # noqa: PLC0415

    if ctx.order_status is OrderStatus.CREATED:
        await ctx.service.transition(
            ctx.conn,
            order_id=ctx.order_id,
            tenant_id=ctx.tenant_id,
            new_status=OrderStatus.ATTEMPTED,
            trigger="webhook",
            webhook_event_id=ctx.webhook_event_id,
            metadata={"event": "payment.authorized", "auto_attempted": True},
        )

    await ctx.service.transition(
        ctx.conn,
        order_id=ctx.order_id,
        tenant_id=ctx.tenant_id,
        new_status=OrderStatus.AUTHORIZED,
        trigger="webhook",
        webhook_event_id=ctx.webhook_event_id,
        metadata={
            "event": "payment.authorized",
            "razorpay_payment_id": _payment_entity(ctx.payload).get("id"),
        },
    )
    return _HandlerOutcome(SkipDecision.PROCEED, None)


# ---------- payment.captured ----------


async def handle_payment_captured(ctx: HandlerContext) -> _HandlerOutcome:
    """Transition to `paid`. INSERT payments row. Request fulfillment publish.

    Handles all three predecessor states:
      attempted -> paid
      authorized -> paid
      failed -> attempted -> paid (two-step recovery)
    """
    from razorpay_checkout.webhook_consumer import _HandlerOutcome  # noqa: PLC0415

    payment = _payment_entity(ctx.payload)
    razorpay_payment_id = payment["id"]
    amount = payment.get("amount", ctx.order_amount)
    method = payment.get("method")
    razorpay_signature = payment.get("razorpay_signature")

    # Two auto-hop edges into `attempted`:
    #   FAILED -> ATTEMPTED  (recovery; Razorpay re-tried a previously failed
    #                        payment and succeeded)
    #   CREATED -> ATTEMPTED (server-to-server or verify-callback-less flow;
    #                        the widget's success callback never ran, so the
    #                        consumer inserts the missing hop on behalf of
    #                        the host)
    # Both keep the external state machine strict while letting production
    # webhooks land cleanly.
    if ctx.order_status in (OrderStatus.FAILED, OrderStatus.CREATED):
        await ctx.service.transition(
            ctx.conn,
            order_id=ctx.order_id,
            tenant_id=ctx.tenant_id,
            new_status=OrderStatus.ATTEMPTED,
            trigger="webhook",
            webhook_event_id=ctx.webhook_event_id,
            metadata={
                "event": "payment.captured",
                "recovery": ctx.order_status is OrderStatus.FAILED,
                "auto_attempted": ctx.order_status is OrderStatus.CREATED,
            },
        )
        from_for_paid = OrderStatus.ATTEMPTED
    else:
        from_for_paid = ctx.order_status

    await ctx.service.transition(
        ctx.conn,
        order_id=ctx.order_id,
        tenant_id=ctx.tenant_id,
        new_status=OrderStatus.PAID,
        trigger="webhook",
        webhook_event_id=ctx.webhook_event_id,
        metadata={"event": "payment.captured", "razorpay_payment_id": razorpay_payment_id},
    )

    attempt_number = await _next_attempt_number(ctx.conn, ctx.order_id)
    await ctx.conn.execute(
        """
        INSERT INTO payments
            (tenant_id, order_id, razorpay_payment_id, razorpay_order_id,
             razorpay_signature, amount, currency, method, status,
             attempt_number, is_authoritative, captured_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'captured', $9, TRUE, NOW())
        """,
        ctx.tenant_id,
        ctx.order_id,
        razorpay_payment_id,
        _razorpay_order_id(ctx.payload, "payment.captured"),
        razorpay_signature,
        amount,
        payment.get("currency", "INR"),
        method,
        attempt_number,
    )

    _log.info(
        "payment_captured_persisted",
        tenant_id=ctx.tenant_id,
        order_id=str(ctx.order_id),
        razorpay_payment_id=razorpay_payment_id,
        amount=amount,
        from_status=from_for_paid.value,
    )

    # Request after-commit fulfillment publish.
    fulfillment_payload = {
        "order_id": str(ctx.order_id),
        "tenant_id": ctx.tenant_id,
        "razorpay_payment_id": razorpay_payment_id,
    }
    return _HandlerOutcome(SkipDecision.PROCEED, fulfillment_payload)


# ---------- payment.failed ----------


async def handle_payment_failed(ctx: HandlerContext) -> _HandlerOutcome:
    """Transition to `failed`. INSERT payments row with `is_authoritative=FALSE`.

    Stale-event no-op cases (`payment.failed` arriving when order is at
    `paid`/`fulfilled`/`fulfillment_failed`) are caught by the orchestrator's
    primary guard before this handler is called.
    """
    from razorpay_checkout.webhook_consumer import _HandlerOutcome  # noqa: PLC0415

    payment = _payment_entity(ctx.payload)
    razorpay_payment_id = payment["id"]
    amount = payment.get("amount", ctx.order_amount)
    method = payment.get("method")

    await ctx.service.transition(
        ctx.conn,
        order_id=ctx.order_id,
        tenant_id=ctx.tenant_id,
        new_status=OrderStatus.FAILED,
        trigger="webhook",
        webhook_event_id=ctx.webhook_event_id,
        metadata={"event": "payment.failed", "razorpay_payment_id": razorpay_payment_id},
    )

    attempt_number = await _next_attempt_number(ctx.conn, ctx.order_id)
    await ctx.conn.execute(
        """
        INSERT INTO payments
            (tenant_id, order_id, razorpay_payment_id, razorpay_order_id,
             amount, currency, method, status, attempt_number, is_authoritative)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'failed', $8, FALSE)
        ON CONFLICT (razorpay_payment_id) DO NOTHING
        """,
        ctx.tenant_id,
        ctx.order_id,
        razorpay_payment_id,
        _razorpay_order_id(ctx.payload, "payment.failed"),
        amount,
        payment.get("currency", "INR"),
        method,
        attempt_number,
    )

    _log.info(
        "payment_failed_persisted",
        tenant_id=ctx.tenant_id,
        order_id=str(ctx.order_id),
        razorpay_payment_id=razorpay_payment_id,
    )
    return _HandlerOutcome(SkipDecision.PROCEED, None)


# ---------- refund.processed ----------


async def handle_refund_processed(ctx: HandlerContext) -> _HandlerOutcome:
    """UPDATE captured payments row to refunded. INSERT refunds. Transition order to refunded.

    Unknown payment id (no authoritative payments row) sets the webhook event
    to `failed` and skips the transition. Operator runs `/reconcile` then
    `/admin/events/{id}/replay`.
    """
    from razorpay_checkout.webhook_consumer import _HandlerOutcome  # noqa: PLC0415

    refund = _refund_entity(ctx.payload)
    razorpay_refund_id = refund["id"]
    razorpay_payment_id = refund.get("payment_id") or refund.get("razorpay_payment_id", "")
    amount = refund.get("amount", ctx.order_amount)
    currency = refund.get("currency", "INR")

    payment_row = await ctx.conn.fetchrow(
        """
        SELECT id FROM payments
         WHERE razorpay_payment_id = $1
           AND tenant_id = $2
           AND is_authoritative = TRUE
        """,
        razorpay_payment_id,
        ctx.tenant_id,
    )
    if payment_row is None:
        await _mark_failed_in_handler(
            ctx, reason=f"refund_for_unknown_payment:{razorpay_payment_id}"
        )
        _log.error(
            "refund_for_unknown_payment",
            tenant_id=ctx.tenant_id,
            order_id=str(ctx.order_id),
            razorpay_refund_id=razorpay_refund_id,
            razorpay_payment_id=razorpay_payment_id,
        )
        return _HandlerOutcome(SkipDecision.PROCEED, None, event_already_marked=True)

    payment_id = payment_row["id"]

    # UPDATE the captured payments row to refunded (NOT a new INSERT).
    await ctx.conn.execute(
        """
        UPDATE payments
           SET status = 'refunded',
               refunded_at = NOW()
         WHERE id = $1
        """,
        payment_id,
    )

    # INSERT the refunds row.
    await ctx.conn.execute(
        """
        INSERT INTO refunds
            (tenant_id, order_id, payment_id, razorpay_refund_id,
             razorpay_payment_id, amount, currency, status, initiated_by,
             notes, processed_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'processed', 'razorpay_dashboard',
                $8::jsonb, NOW())
        ON CONFLICT (razorpay_refund_id) DO NOTHING
        """,
        ctx.tenant_id,
        ctx.order_id,
        payment_id,
        razorpay_refund_id,
        razorpay_payment_id,
        amount,
        currency,
        {"event_id": ctx.webhook_event_id and str(ctx.webhook_event_id)},
    )

    await ctx.service.transition(
        ctx.conn,
        order_id=ctx.order_id,
        tenant_id=ctx.tenant_id,
        new_status=OrderStatus.REFUNDED,
        trigger="webhook",
        webhook_event_id=ctx.webhook_event_id,
        metadata={
            "event": "refund.processed",
            "razorpay_refund_id": razorpay_refund_id,
            "razorpay_payment_id": razorpay_payment_id,
        },
    )

    _log.info(
        "refund_processed",
        tenant_id=ctx.tenant_id,
        order_id=str(ctx.order_id),
        razorpay_refund_id=razorpay_refund_id,
    )
    return _HandlerOutcome(SkipDecision.PROCEED, None)


# ---------- order.paid (log only) ----------


async def handle_order_paid(ctx: HandlerContext) -> _HandlerOutcome:
    """Diagnostic event only. No state change."""
    from razorpay_checkout.webhook_consumer import _HandlerOutcome  # noqa: PLC0415

    _log.info(
        "order_paid_event_logged",
        tenant_id=ctx.tenant_id,
        order_id=str(ctx.order_id),
        order_status=ctx.order_status.value,
    )
    return _HandlerOutcome(SkipDecision.PROCEED, None)


# ---------- helpers ----------


def _payment_entity(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("payload", {})
    entity = inner.get("payment", {}).get("entity", {}) if isinstance(inner, dict) else {}
    return entity if isinstance(entity, dict) else {}


def _refund_entity(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("payload", {})
    entity = inner.get("refund", {}).get("entity", {}) if isinstance(inner, dict) else {}
    return entity if isinstance(entity, dict) else {}


def _razorpay_order_id(payload: dict[str, Any], event: str) -> str:
    inner = payload.get("payload", {})
    if event.startswith("payment."):
        return str(inner.get("payment", {}).get("entity", {}).get("order_id", ""))
    if event.startswith("refund."):
        return str(inner.get("refund", {}).get("entity", {}).get("order_id", ""))
    return str(inner.get("order", {}).get("entity", {}).get("id", ""))


async def _next_attempt_number(conn: asyncpg.Connection, order_id: Any) -> int:
    row = await conn.fetchrow(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n FROM payments WHERE order_id = $1",
        order_id,
    )
    return int(row["n"]) if row is not None else 1


async def _mark_failed_in_handler(ctx: HandlerContext, *, reason: str) -> None:
    await ctx.conn.execute(
        """
        UPDATE webhook_events
           SET processing_status = 'failed',
               processed_at = NOW(),
               attempts = attempts + 1,
               error = $2
         WHERE id = $1
        """,
        ctx.webhook_event_id,
        reason,
    )
