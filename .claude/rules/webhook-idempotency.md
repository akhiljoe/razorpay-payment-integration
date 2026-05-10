---
description: Enforce three-layer idempotency on webhook processing.
globs:
  - "razorpay_checkout/razorpay_checkout/webhooks.py"
  - "razorpay_checkout/razorpay_checkout/orders.py"
---

# Webhook Idempotency Rule

razorpay_payment_id check inside the DB transaction is the primary guard. event_id ON CONFLICT DO NOTHING is secondary deduplication only and fires first as a fast path.

## Required Sequence Before Any State Transition

1. **Fast-path dedup**: `INSERT INTO webhook_events (...) ON CONFLICT (tenant_id, event_id) DO NOTHING`. On conflict, increment `webhook_duplicate_count`, mark event `done`, return `200 queued`.
2. **Begin DB transaction**.
3. **Primary guard (Fix V3-3)**: `SELECT ... FROM payments WHERE razorpay_payment_id = ? AND tenant_id = ? FOR KEY SHARE`. If found, mark `webhook_events.processing_status = 'done'`, commit, return `200 queued`. This catches the case where Razorpay retries with a new `event_id` for the same payment (B-4).
4. **Row lock**: `SELECT ... FROM orders WHERE razorpay_order_id = ? AND tenant_id = ? FOR UPDATE`.
5. **Amount assertion**: `event_amount == order.amount`. On mismatch, set event `failed`, alert, return `200 queued`. Do not return 500; Razorpay must not retry an amount mismatch indefinitely.
6. **State transition** via `OrderService.transition`. Special case: `payment.failed` on a `paid` or `fulfilled` order is a no-op, not a failure (Fix B-2).
7. **Commit transaction** before any side effect (SQS publish, fulfillment, log).

## Forbidden Patterns

- Treating `event_id` uniqueness as the authoritative idempotency mechanism.
- State transitions outside the explicit DB transaction.
- Side effects between DB transitions but before commit.
- Skipping the `razorpay_payment_id` check because `event_id` was new.
- Performing the primary guard outside `FOR KEY SHARE` or weaker isolation than read-committed with a row lock.
