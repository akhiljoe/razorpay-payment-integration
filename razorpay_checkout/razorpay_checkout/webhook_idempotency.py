"""Event-type-aware primary idempotency guard for the Webhook Consumer.

Source of truth: `.claude/rules/webhook-idempotency.md` and DESIGN_v5
Section 2.1 step 10. The guard runs **inside** the consumer's DB transaction,
**after** `SELECT ... FOR UPDATE` on the order, so the decision reflects the
current locked row state.

Why the guard exists. Razorpay may retry a webhook with a **new** `event_id`
for the **same** payment. The fast-path `INSERT INTO webhook_events ... ON
CONFLICT (tenant_id, event_id) DO NOTHING` only catches duplicates with the
literal same `event_id`. The primary guard catches the second class.

Three outcomes:

- `PROCEED`: the consumer's handler should run.
- `SKIP_ALREADY_APPLIED`: the order is already at or past the event's target
  state. Mark the webhook `done` and commit without state change.
- `STALE_NO_OP`: a special case for `payment.failed` arriving after the order
  reached `paid`/`fulfilled`/`fulfillment_failed`. The event is stale data,
  not a duplicate. Same handling as `SKIP_ALREADY_APPLIED` but logged with a
  distinct reason.
"""

from __future__ import annotations

from enum import StrEnum

from razorpay_checkout.state_machine import OrderStatus


class SkipDecision(StrEnum):
    """Three-way outcome of the primary idempotency guard."""

    PROCEED = "proceed"
    SKIP_ALREADY_APPLIED = "skip_already_applied"
    STALE_NO_OP = "stale_no_op"


_PAYMENT_CAPTURED_TERMINAL: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.PAID,
        OrderStatus.FULFILLED,
        OrderStatus.FULFILLMENT_FAILED,
        OrderStatus.REFUNDED,
    }
)

_PAYMENT_AUTHORIZED_TERMINAL: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.AUTHORIZED,
        OrderStatus.PAID,
        OrderStatus.FULFILLED,
        OrderStatus.FULFILLMENT_FAILED,
        OrderStatus.REFUNDED,
    }
)

_PAYMENT_FAILED_SKIP: frozenset[OrderStatus] = frozenset(
    {OrderStatus.FAILED, OrderStatus.REFUNDED}
)

_PAYMENT_FAILED_STALE: frozenset[OrderStatus] = frozenset(
    {OrderStatus.PAID, OrderStatus.FULFILLED, OrderStatus.FULFILLMENT_FAILED}
)


def should_skip(
    event: str,
    order_status: OrderStatus,
    *,
    captured_payment_exists: bool,
) -> SkipDecision:
    """Decide whether to apply or skip an event given the locked order row.

    `captured_payment_exists` is True iff a `payments` row with the event's
    `razorpay_payment_id` already exists AND `is_authoritative=TRUE`. It is
    only relevant for `payment.captured`; pass `False` for other events.

    See DESIGN_v5 Section 2.1 step 10 for the source rules. Behavior summary:

    - payment.captured: SKIP if order at paid/fulfilled/fulfillment_failed/refunded
      AND a captured payments row already exists for the razorpay_payment_id.
    - payment.authorized: SKIP if order at authorized/paid/fulfilled/
      fulfillment_failed/refunded.
    - payment.failed: SKIP if order at failed/refunded. STALE_NO_OP if order at
      paid/fulfilled/fulfillment_failed.
    - refund.processed: SKIP if order at refunded.
    - order.paid: never skipped (log-only event).
    """
    if event == "payment.captured":
        if order_status in _PAYMENT_CAPTURED_TERMINAL and captured_payment_exists:
            return SkipDecision.SKIP_ALREADY_APPLIED
        return SkipDecision.PROCEED

    if event == "payment.authorized":
        if order_status in _PAYMENT_AUTHORIZED_TERMINAL:
            return SkipDecision.SKIP_ALREADY_APPLIED
        return SkipDecision.PROCEED

    if event == "payment.failed":
        if order_status in _PAYMENT_FAILED_SKIP:
            return SkipDecision.SKIP_ALREADY_APPLIED
        if order_status in _PAYMENT_FAILED_STALE:
            return SkipDecision.STALE_NO_OP
        return SkipDecision.PROCEED

    if event == "refund.processed":
        if order_status == OrderStatus.REFUNDED:
            return SkipDecision.SKIP_ALREADY_APPLIED
        return SkipDecision.PROCEED

    # order.paid and any unrecognized event always proceed.
    return SkipDecision.PROCEED
