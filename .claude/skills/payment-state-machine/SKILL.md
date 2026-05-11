---
name: payment-state-machine
description: Invoke automatically when working on orders.py or any state transition logic.
allowed-tools: Read, Grep, Glob
---

# Payment State Machine Skill

Reference for `OrderService.transition` in `razorpay_checkout/razorpay_checkout/orders.py`. Source of truth: `designs/DESIGN_v5.md` Section 4.

## Allowed Transitions

| From | To | Trigger |
|---|---|---|
| `initializing` | `created` | Razorpay API returns `rzp_order_id` |
| `created` | `attempted` | First payment event (any outcome) |
| `created` | `failed` | Order expiry |
| `attempted` | `authorized` | `payment.authorized` (auto-capture off) |
| `attempted` | `paid` | `payment.captured` |
| `attempted` | `failed` | `payment.failed` |
| `authorized` | `paid` | `payment.captured` |
| `authorized` | `failed` | `payment.failed` or 5-day capture window expired |
| `failed` | `attempted` | New `payment.*` event for the same order (retry) |
| `paid` | `fulfilled` | Fulfillment hook succeeded |
| `paid` | `fulfillment_failed` | Fulfillment hook exhausted SQS retries |
| `paid` | `refunded` | `refund.processed` before fulfillment |
| `fulfilled` | `refunded` | `refund.processed` after fulfillment |
| `fulfillment_failed` | `fulfilled` | Admin retry succeeded |
| `fulfillment_failed` | `refunded` | Refund issued instead of fulfillment |

Any edge not in this table raises `IllegalTransitionError`. Terminal states: `failed`, `refunded`.

## No-Op Cases

- `payment.failed` event on a `paid`, `fulfilled`, or `fulfillment_failed` order: mark `webhook_events.processing_status = 'done'` with a note. Do not raise.
- Duplicate webhook with same `(tenant_id, event_id)`: handled at INSERT via `ON CONFLICT DO NOTHING`. Do not enter the state machine.
- Event whose target state has already been reached (per the event-type-aware primary guard in `webhook-idempotency` rule): mark `done`. Do not enter the state machine.

## Multi-Step Retry Path

`failed -> attempted -> paid` is two transitions in one processor invocation. Both apply inside the same DB transaction with `SELECT ... FOR UPDATE` held throughout. The new `payments` row has `attempt_number > 1` and `is_authoritative=TRUE` on the capture event.

## Authorize-Then-Capture Path

`attempted -> authorized` on `payment.authorized` does NOT insert a `payments` row. The transition is recorded in `order_events` only.

When `payment.captured` arrives, the order transitions `authorized -> paid` and the `payments` row is INSERTed at that point with `is_authoritative=TRUE`.

## Refund Path

`refund.processed` UPDATEs the existing captured `payments` row to `status='refunded'`, `refunded_at=NOW()`. It INSERTs a row into `refunds`. It transitions the order to `refunded` from `paid`, `fulfilled`, or `fulfillment_failed`.

Never insert a new `payments` row on refund.

## Fulfillment Failure Path

When the Fulfillment Lambda exhausts SQS retries (`maxReceiveCount=5`), a DLQ-poller Lambda transitions the order `paid -> fulfillment_failed` and records the failure reason in `order_events.metadata`. The order does not transition to `failed`; that state is reserved for payment-side failures.

Recovery: `POST /admin/orders/{order_id}/retry-fulfillment` re-enqueues and transitions `fulfillment_failed -> paid -> fulfilled` on success.
