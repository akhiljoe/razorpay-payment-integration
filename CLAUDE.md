# CLAUDE.md

Razorpay payment integration module. Embeddable across multiple websites via a FastAPI router (Python) and an npm widget (TypeScript). Local-first development. Source of truth: `designs/DESIGN_v5.md`.

## Build, Test, Lint Commands

**Python (`razorpay_checkout/`)**:

- Install: `uv sync`
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

**Local Webhook Simulation**: `python scripts/razorpay-simulator.py <event_type> --tenant <id> --order-id <rzp_order_id> --payment-id <rzp_payment_id>`. Supported events: `payment.authorized`, `payment.captured`, `payment.failed`, `refund.processed`.

## Architecture

Two AWS Lambda functions connected by an SQS FIFO queue (`fulfillment-queue.fifo`).

| Lambda | Trigger | Responsibilities |
|---|---|---|
| **Webhook Lambda** | `POST /payments/webhook/{tenant_id}` | Tenant lookup, signature verification, idempotency, DB transitions, SQS publish |
| **Fulfillment Lambda** | SQS `fulfillment-queue.fifo` | Invoke `FulfillmentHook`, transition order to `fulfilled` or `fulfillment_failed`, write `order_events` |

Webhook Lambda must return `200` to Razorpay within 500 ms. Fulfillment Lambda has SQS-native retry (`maxReceiveCount=5`) and DLQ (`fulfillment-dlq.fifo`).

DB: PostgreSQL. Tables: `tenants`, `orders`, `payments`, `refunds`, `webhook_events`, `order_events`. All schema in Alembic migrations. No `metadata.create_all()`. `updated_at` is maintained by a DB-level `BEFORE UPDATE` trigger, never by application code.

## Critical Invariants

1. **Tenant isolation**: Every query against `orders`, `payments`, `refunds`, `webhook_events`, and `order_events` must include a `tenant_id` filter. Enforced by `OrderService` and `WebhookProcessor`, never by the caller.
2. **Tenant ID source**: `tenant_id` is extracted from the URL path parameter on the webhook route. It must never come from the request body, query string, or header.
3. **Webhook secret lookup**: Verify `X-Razorpay-Signature` against the tenant-specific `webhook_secret` first, fall back to `webhook_secret_previous` if set. Both checks use `hmac.compare_digest`. Verification precedes payload deserialization.
4. **Primary idempotency guard (event-type aware)**: Inside the DB transaction, after `SELECT ... FOR UPDATE` on the order, check whether the event's target state has already been reached. Rules per event type are in DESIGN_v5 Section 2.1 step 6 and the webhook-idempotency rule. `event_id ON CONFLICT DO NOTHING` is the secondary fast-path.
5. **State transition ordering**: Acquire `SELECT ... FOR UPDATE` on `orders` before any state change. Assert `event_amount == order.amount` on capture and authorize events.
6. **Order creation sequence**: INSERT local `orders` row with `status=initializing` before calling Razorpay. Razorpay is never ahead of the DB.
7. **DB commit before side effects**: The webhook DB transaction commits before any SQS publish, log, or external call.
8. **Correlation ID propagation**: Generate a UUIDv4 at every HTTP entry. Persist on `orders`, `webhook_events`, `order_events`. Include in SQS `MessageAttributes`. Pass to `FulfillmentHook.metadata`. Every log line and OTel span carries it.

## State Machine

`initializing -> created`; `created -> attempted | failed`; `attempted -> authorized | paid | failed`; `authorized -> paid | failed`; `failed -> attempted` (retry); `paid -> fulfilled | fulfillment_failed | refunded`; `fulfilled -> refunded`; `fulfillment_failed -> fulfilled | refunded`.

Any other edge raises `IllegalTransitionError`. Special case: `payment.failed` on `paid`, `fulfilled`, or `fulfillment_failed` is a no-op (mark event `done`).

`fulfillment_failed` is distinct from `failed` so payment-side failures and delivery-side failures are not conflated. Full transition table: DESIGN_v5 Section 4 and the `payment-state-machine` skill.

## Webhook Event Routing

| Event | Action |
|---|---|
| `payment.authorized` | `attempted -> authorized`. No `payments` row. No SQS publish. |
| `payment.captured` | Transition to `paid` (from `attempted`, `authorized`, or via `failed -> attempted`). INSERT `payments` row, `is_authoritative=TRUE`. Publish SQS message. |
| `payment.failed` | Transition to `failed`. INSERT `payments` row, `is_authoritative=FALSE`. No SQS publish. |
| `refund.processed` | Transition to `refunded`. UPDATE `payments.status='refunded'`. INSERT `refunds` row. No SQS publish. |
| `order.paid` | Log only. No state change. |

## Conventions

- **Pydantic v2** with `SecretStr` for `key_secret` and `webhook_secret`. No secret value may appear in any log record (asserted in tests).
- **asyncpg** with explicit pool sizing (`pool_min_size`, `pool_max_size`).
- **OpenTelemetry**: exporter selected at startup via `OTEL_EXPORTER` env var (`stdout` | `xray` | `otlp`). No application branching on the exporter.
- `payments.is_authoritative=TRUE` only on `payment.captured`. Reconciliation queries authoritative payments only.
- Admin routes require `X-Admin-Api-Key` matching `RazorpayConfig.admin_api_key`. If `None`, the route returns `403`.

## Gotchas

- Razorpay may retry a webhook with a **new `event_id` for the same payment**. The event-type-aware primary guard inside the DB transaction catches this; do not rely on `event_id` uniqueness alone.
- Lambda freezes process CPU after the HTTP response is flushed. Do not schedule asyncio background tasks in the webhook handler. Use SQS.
- `FulfillmentHook` must be idempotent: SQS provides at-least-once delivery. Fulfillment Lambda also short-circuits if `order.status='fulfilled'` when it acquires the row lock.
- HTTP status for unknown `tenant_id` is **400 Bad Request**, not 404. The multi-tenant URL space is publicly enumerable; 400 prevents tenant existence leak.
- `payment.authorized` does NOT insert a `payments` row. Only terminal payment outcomes (`captured`, `failed`) do.
- `refund.processed` UPDATEs the existing captured `payments` row; do NOT insert a new `payments` row. Insert into `refunds` instead.

## Editing Rule

Provide targeted fixes and incremental updates only. Do not rewrite entire files. Maintain existing structure and context. Identify exact change locations and minimize token usage.

## Git Operations Rule

Never run `git commit`, `git push`, `git tag`, `git merge`, or any other git command that writes to history or to a remote. Never pass `-c user.email=...` or `-c user.name=...`, and never run `git config user.email` or `git config user.name`. The user owns their git identity configuration.

When the user asks for a commit, stage with `git add` if needed, then output the proposed commit message and the exact command for the user to run themselves. Read-only git operations (`status`, `log`, `diff`, `show`, `branch`) are fine without asking.
