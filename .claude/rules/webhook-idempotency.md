---
description: Enforce event-type-aware idempotency on webhook processing.
globs:
  - "razorpay_checkout/razorpay_checkout/webhooks.py"
  - "razorpay_checkout/razorpay_checkout/orders.py"
---

# Webhook Idempotency Rule

The primary guard is event-type aware and runs inside the DB transaction after `SELECT ... FOR UPDATE` on the order. `event_id ON CONFLICT DO NOTHING` is the secondary fast-path at INSERT time.

## Required Sequence Before Any State Transition

1. **Fast-path dedup**: `INSERT INTO webhook_events (...) ON CONFLICT (tenant_id, event_id) DO NOTHING`. On conflict, increment `webhook_duplicate_count`, mark event `done`, return `200 queued`.
2. **Begin DB transaction**.
3. **Acquire row lock**: `SELECT ... FROM orders WHERE razorpay_order_id = ? AND tenant_id = ? FOR UPDATE`.
4. **Primary idempotency guard (event-type aware)**:
   - `payment.captured`: skip if `order.status IN ('paid', 'fulfilled', 'fulfillment_failed', 'refunded')` AND a captured `payments` row exists for `razorpay_payment_id`.
   - `payment.authorized`: skip if `order.status IN ('authorized', 'paid', 'fulfilled', 'fulfillment_failed', 'refunded')`.
   - `payment.failed`: skip if `order.status IN ('failed', 'refunded')`. No-op (mark `done`) if `order.status IN ('paid', 'fulfilled', 'fulfillment_failed')`.
   - `refund.processed`: skip if `order.status = 'refunded'`.

   On skip, mark `webhook_events.processing_status = 'done'`, commit, return `200 queued`.
5. **Amount assertion** for capture and authorize events: `event_amount == order.amount`. On mismatch, set event `failed`, alert, return `200 queued`. Do not return 500; Razorpay must not retry an amount mismatch indefinitely.
6. **State transition** via `OrderService.transition`. INSERT `payments` row only on `payment.captured` or `payment.failed`. INSERT `refunds` row on `refund.processed`. Never insert a `payments` row on `payment.authorized`.
7. **Commit transaction** before any side effect (SQS publish, log, external call).

## Forbidden Patterns

- Treating `event_id` uniqueness as the authoritative idempotency mechanism.
- A primary guard that is not event-type aware.
- State transitions outside the explicit DB transaction.
- Side effects between DB transitions but before commit.
- Performing the primary guard outside `FOR UPDATE` or weaker isolation than read-committed with a row lock.
- Inserting a `payments` row on `payment.authorized` (no row until capture or failed).
- Inserting a new `payments` row on `refund.processed` (UPDATE the existing captured row to `status='refunded'`).
