---
description: Review the current diff against DESIGN_v3.md invariants.
---

# /project:review

Review the staged and unstaged changes for compliance with `designs/DESIGN_v3.md`.

## Step 1: Capture the Diff

Run `git diff HEAD` and `git status` to enumerate every file changed in this branch. Also run `git diff --stat HEAD` for a summary.

## Step 2: Tenant Isolation

For every query against `orders`, `payments`, `webhook_events`, or `order_events` introduced or modified by the diff, confirm:

> Every query against orders, payments, webhook_events, and order_events must include a tenant_id filter. This is enforced by OrderService and WebhookProcessor, never by the caller.

Confirm `tenant_id` originates from the URL path parameter or the injected `RazorpayConfig`, never from a request body, query string, or header.

## Step 3: Idempotency Hierarchy

For any change that affects webhook processing, confirm:

> razorpay_payment_id check inside the DB transaction is the primary guard. event_id ON CONFLICT DO NOTHING is secondary deduplication only and fires first as a fast path.

Reject any change that elevates `event_id` uniqueness to authoritative status, or that performs a state transition before the primary `razorpay_payment_id` check.

## Step 4: State Machine Correctness

For any modification of `OrderService.transition` or its callers, confirm the allowed transitions in Section 4 of DESIGN_v3.md are preserved exactly:

- `initializing -> created`
- `created -> attempted`
- `created -> failed`
- `attempted -> paid`
- `attempted -> failed`
- `failed -> attempted` (retry path; Fix B-1)
- `paid -> fulfilled`
- `paid -> failed`
- `fulfilled -> refunded`

Confirm the no-op case (Fix B-2): `payment.failed` on a `paid` or `fulfilled` order marks the event `done`, not `failed`. Confirm the retry path: `failed -> attempted -> paid` is two transitions in a single processor invocation, both inside the same DB transaction.

## Step 5: Test Coverage on Critical Paths

Any change to the following surfaces must include or update tests in `razorpay_checkout/tests/`:

- Signature verification (`signatures.py`)
- State machine (`orders.py`)
- Webhook pipeline (`webhooks.py`)
- Tenant registry lookup (`router.py`)
- SQS publish path (Webhook Lambda boundary)

Reject the change if test coverage is missing on any of these surfaces.

## Step 6: V3-1 Two-Lambda Boundary

The V3-1 conflict in DESIGN_v3.md is resolved in `CLAUDE.md`: the two-Lambda architecture (Webhook Lambda + SQS + Fulfillment Lambda) is binding. Section 2.1 step 8 (synchronous fulfillment in the handler) is overridden.

If the diff touches `webhooks.py` or fulfillment plumbing, reject any of the following as a regression:

- Direct invocation of `FulfillmentHook` inside the webhook handler.
- Any `asyncio.create_task`, `BackgroundTasks`, or `lifespan` poller scheduled inside the webhook handler.
- Any external call between the DB commit and the SQS publish.
- Any removal, conditional bypass, or silent failure of the SQS publish on a successful `paid` transition.

## Step 7: Unknown Tenant Status Code

If the diff touches the webhook route or its tests, confirm the response for an unknown `tenant_id` is `400 Bad Request` (not 404). Rationale recorded in `CLAUDE.md` Gotchas: 404 leaks tenant existence on a publicly enumerable URL space.

## Output

Produce a single review report with one section per check above. Mark each PASS, FAIL, or N/A. List the exact file and line for any FAIL.
