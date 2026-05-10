---
name: payment-state-machine
description: Invoke automatically when working on orders.py or any state transition logic.
allowed-tools: Read, Grep, Glob
---

# Payment State Machine Skill

Reference for `OrderService.transition` in `razorpay_checkout/razorpay_checkout/orders.py`. Source of truth: Section 4 of `designs/DESIGN_v3.md`.

## Allowed Transitions

| From | To | Trigger |
|---|---|---|
| `initializing` | `created` | Razorpay API returns `rzp_order_id` |
| `created` | `attempted` | First payment event (any outcome) |
| `created` | `failed` | Order expiry |
| `attempted` | `paid` | `payment.captured` webhook |
| `attempted` | `failed` | `payment.failed` webhook |
| `failed` | `attempted` | New `payment.captured` or `payment.failed` event for the same order (Fix B-1) |
| `paid` | `fulfilled` | Fulfillment complete |
| `paid` | `failed` | Fulfillment exhausted retries |
| `fulfilled` | `refunded` | `refund.processed` webhook |

Any edge not in this table raises `IllegalTransitionError`.

## No-Op Cases

- `payment.failed` event on a `paid` or `fulfilled` order: mark `webhook_events.processing_status = 'done'` with a note. Do not raise. (Fix B-2)
- Duplicate webhook with the same `(tenant_id, event_id)`: handled at INSERT via `ON CONFLICT DO NOTHING`. Do not enter the state machine.
- `razorpay_payment_id` already present in `payments` (regardless of `event_id`): mark event `done`. Do not enter the state machine. (Fix V3-3)

## Failed-to-Attempted-to-Paid Retry Path (Fix B-1)

The retry edge `failed -> attempted` exists so that a `payment.captured` event arriving after a prior `payment.failed` event for the same order can be processed. The full path is:

`failed -> attempted -> paid`

This is two transitions in a single processor invocation. Both must be applied inside the same DB transaction, with `SELECT ... FOR UPDATE` held on the `orders` row throughout. The new `payments` row inserted in this path has `attempt_number` greater than 1 and `is_authoritative = TRUE` only on the captured event.

Without this path, a captured payment after a failed attempt would hit `IllegalTransitionError` and leave a captured payment unprocessed.
