---
description: Review the current diff against DESIGN_v5.md invariants.
---

# /project:review

Review staged and unstaged changes for compliance with `designs/DESIGN_v5.md`.

## Step 1: Capture the Diff

Run `git diff HEAD`, `git diff --stat HEAD`, and `git status` to enumerate every file changed.

## Step 2: Tenant Isolation

For every query against `orders`, `payments`, `refunds`, `webhook_events`, or `order_events` introduced or modified by the diff, confirm:

> Every query against orders, payments, refunds, webhook_events, and order_events must include a tenant_id filter. This is enforced by OrderService and WebhookProcessor, never by the caller.

Confirm `tenant_id` originates from the URL path parameter (webhook route) or the injected `RazorpayConfig` (everything else), never from a request body, query string, or header.

## Step 3: Idempotency Hierarchy

For any change affecting webhook processing, confirm the event-type-aware primary guard is intact (inside DB transaction, after `SELECT ... FOR UPDATE`):

- `payment.captured`: skip if `order.status IN ('paid', 'fulfilled', 'fulfillment_failed', 'refunded')` AND a captured `payments` row exists for `razorpay_payment_id`.
- `payment.authorized`: skip if `order.status IN ('authorized', 'paid', 'fulfilled', 'fulfillment_failed', 'refunded')`.
- `payment.failed`: skip if `order.status IN ('failed', 'refunded')`. No-op (mark `done`) on `paid`, `fulfilled`, `fulfillment_failed`.
- `refund.processed`: skip if `order.status = 'refunded'`.

Reject any change that elevates `event_id` uniqueness to the authoritative guard, performs a state transition before the primary guard fires, or runs the guard outside the DB transaction or without `FOR UPDATE`.

## Step 4: State Machine Correctness

For modifications to `OrderService.transition` or its callers, confirm the allowed transitions in DESIGN_v5 Section 4 are preserved exactly. Special case (no-op): `payment.failed` on `paid`, `fulfilled`, or `fulfillment_failed` marks the event `done`, not `failed`.

Confirm:

- `payment.authorized` does NOT insert a `payments` row.
- `refund.processed` UPDATEs the existing captured `payments` row to `status='refunded'` and INSERTs a `refunds` row; does NOT insert a new `payments` row.
- `paid -> fulfillment_failed` is the edge used on fulfillment hook exhaustion (NOT `paid -> failed`).
- `fulfillment_failed -> fulfilled` is the admin retry recovery edge.

## Step 5: Correlation ID Propagation

Confirm `correlation_id`:

1. Generated at every HTTP entry (order creation and webhook receipt).
2. Persisted on `orders.correlation_id`, `webhook_events.correlation_id`, and `order_events.correlation_id`.
3. Included in SQS `MessageAttributes` on every publish.
4. Read by the Fulfillment Lambda from SQS attributes and propagated to logs and `FulfillmentHook.metadata`.
5. Present in every log line and OTel span.

Flag any handler entry that does not generate or read `correlation_id`.

## Step 6: Test Coverage on Critical Paths

Any change to the following surfaces must include or update tests in `razorpay_checkout/tests/`:

- Signature verification (`signatures.py`).
- State machine (`orders.py`), including `authorized`, `fulfillment_failed`, and refund edges.
- Webhook pipeline (`webhooks.py`), including refund and authorize handlers.
- Tenant registry lookup (`router.py`).
- SQS publish path on `paid` transition only.
- Correlation ID propagation end-to-end.

## Step 7: Two-Lambda Boundary

If the diff touches `webhooks.py` or fulfillment plumbing, reject:

- Direct invocation of `FulfillmentHook` inside the webhook handler.
- Any `asyncio.create_task`, `BackgroundTasks`, or `lifespan` poller in the webhook handler.
- Any external call between the DB commit and the SQS publish.
- Any removal, conditional bypass, or silent failure of the SQS publish on a `paid` transition.

## Step 8: Unknown Tenant Status Code

If the diff touches the webhook route or its tests, confirm the response for an unknown `tenant_id` is `400 Bad Request` (not 404).

## Output

One section per step. Mark each PASS, FAIL, or N/A. List the exact file and line for any FAIL.
