# CLAUDE.md

Razorpay payment integration module. Embeddable across multiple websites via a FastAPI router (Python) and an npm widget (TypeScript). Local-first development. All conventions derived from `designs/DESIGN_v3.md`.

## Build, Test, Lint Commands

**Python (`razorpay_checkout/`)**:

- Install: `uv sync` (run from `razorpay_checkout/`)
- Test: `uv run pytest`
- Migrations apply: `uv run alembic upgrade head`
- Migrations generate: `uv run alembic revision --autogenerate -m "<msg>"`

**TypeScript (`packages/checkout-widget/`)**:

- Install: `npm install`
- Test: `npm test`
- Lint: `npm run lint`

**Infra (`infra/`)**:

- Install: `npm install`
- Diff: `npx cdk diff`
- Local synth only. Cloud deployment is out of scope for routine sessions.

## Architecture Summary

Two distinct deliverables:

1. **`razorpay_checkout`**: Python FastAPI router mountable at `/payments`. Handles order creation, webhook receipt, callback verification, reconciliation, and admin replay.
2. **`@akhiljoe/checkout-widget`**: Frontend npm package (React component plus vanilla adapter) that loads `checkout.js`, opens the modal, and surfaces typed events.

Backend deploys as **two AWS Lambda functions** connected by an **SQS FIFO queue** (`fulfillment-queue.fifo`). See "Critical Conflict" below.

DB: PostgreSQL. Tables: `tenants`, `orders`, `payments`, `webhook_events`, `order_events`. All schema lives in Alembic migrations. No `metadata.create_all()`. `updated_at` is maintained by a DB-level `BEFORE UPDATE` trigger, never by application code.

## Critical Invariants

1. **Tenant isolation**: Every query against orders, payments, webhook_events, and order_events must include a tenant_id filter. This is enforced by OrderService and WebhookProcessor, never by the caller.
2. **Tenant ID source**: `tenant_id` must be extracted from the URL path parameter per Fix V3-2. It must never be taken from the request body.
3. **Webhook secret lookup**: Verify `X-Razorpay-Signature` against the tenant-specific `webhook_secret` first, falling back to `webhook_secret_previous` if set. Both checks use `hmac.compare_digest`.
4. **Idempotency hierarchy**: razorpay_payment_id check inside the DB transaction is the primary guard. event_id ON CONFLICT DO NOTHING is secondary deduplication only and fires first as a fast path.
5. **State transition ordering**: Acquire `SELECT ... FOR UPDATE` on the `orders` row before any state change. Assert `event_amount == order.amount` before transitioning to `paid`.
6. **Order creation sequence**: INSERT local `orders` row with `status=initializing` before calling Razorpay. Razorpay is never ahead of the DB.
7. **DB commit before side effects**: The webhook DB transaction commits before any fulfillment side effect or SQS publish.

## Webhook Route Format

`POST /payments/webhook/{tenant_id}` (Fix V3-2). The path parameter resolves the tenant config from `TenantConfigRegistry` before signature verification. Single-tenant deployments register one config and still use the `/{tenant_id}` URL.

## V3-1 Webhook Fulfillment Decision (resolved)

DESIGN_v3.md was internally inconsistent on fulfillment placement. Section 2.1 step 8 said synchronous-in-handler; Sections 5.2, 6, and 7 said two-Lambda via SQS. The two-Lambda model is binding. Section 2.1 step 8 is overridden as a stale dissent.

Rationale:

- **Latency**: Razorpay enforces a 500 ms response SLA on the webhook endpoint and deactivates endpoints that exceed it. SQS publish p50 is ~10 to 30 ms, well within budget. Synchronous fulfillment with any external `FulfillmentHook` call (the design notes 1 to 10 s) blows the SLA.
- **Cost**: SQS free tier is 1M requests per month; project volume is ~3k. Both Lambdas share the 1M Lambda invocation free tier. Total: $0.
- **Modularity**: Fulfillment Lambda scales independently, has SQS-native retry + DLQ (`maxReceiveCount=5`, then `fulfillment-dlq.fifo`), and the `FulfillmentHook` can be swapped without touching the webhook code path.

PRs that touch `webhooks.py` or fulfillment plumbing must preserve the two-Lambda boundary. Reintroducing synchronous fulfillment inside the webhook handler is a regression and must be rejected on review.

## Two-Lambda Separation of Concerns

| Lambda | Triggered by | Responsibilities |
|---|---|---|
| **Webhook Lambda** | `POST /payments/webhook/{tenant_id}` | Tenant lookup, signature verification, idempotency checks, DB state transitions, SQS publish |
| **Fulfillment Lambda** | SQS `fulfillment-queue.fifo` | Invoke `FulfillmentHook`, update order to `fulfilled` or `failed`, write `order_events` |

The Webhook Lambda must return `200` to Razorpay within 500 ms. The Fulfillment Lambda has its own retry and DLQ semantics (`maxReceiveCount=5`, then `fulfillment-dlq.fifo`).

## State Machine

`OrderService.transition` allowed edges: `initializing -> created`; `created -> attempted | failed`; `attempted -> paid | failed`; `failed -> attempted` (Fix B-1 retry path); `paid -> fulfilled | failed`; `fulfilled -> refunded`. Any other edge raises `IllegalTransitionError`. Special case (Fix B-2): `payment.failed` on a `paid` or `fulfilled` order is a no-op, not a failure.

## Conventions

- **Pydantic v2** for all request/response models. `SecretStr` for `key_secret` and `webhook_secret`. No secret value may appear in any log record (verified by test).
- **asyncpg** with explicit pool sizing (`pool_min_size`, `pool_max_size`).
- **OpenTelemetry**: exporter selected at startup via `OTEL_EXPORTER` env var (`stdout` | `xray` | `otlp`). No application branching on the exporter.
- `payments.is_authoritative = TRUE` only on `payment.captured`. Reconciliation queries authoritative payments only.
- Admin routes require `X-Admin-Api-Key` matching `RazorpayConfig.admin_api_key`. If `admin_api_key is None`, the route returns `403`.

## Gotchas

- Razorpay may retry a webhook with a **new `event_id` for the same payment** (B-4 / V3-3). The `razorpay_payment_id` primary guard is the only thing that catches this; do not rely on `event_id` uniqueness alone.
- Lambda freezes the process CPU after the HTTP response is flushed. Do not schedule asyncio background tasks in the webhook handler. Use SQS.
- Fulfillment hook **must be idempotent**: SQS provides at-least-once delivery.
- HTTP status for unknown `tenant_id` on the webhook route is **400 Bad Request**. The multi-tenant URL space `/payments/