# CLAUDE.md

Razorpay payment integration module. Embeddable across multiple websites via a FastAPI router (Python) and an npm widget (TypeScript). Local-first development. Source of truth: `designs/DESIGN_v6.md`.

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

Three AWS Lambda functions connected by two SQS FIFO queue pairs. The split exists for two reasons: (1) Razorpay's 500 ms response SLA, decoupled from DB latency, and (2) the public-facing function holds zero database credentials.

| Lambda | Trigger | Responsibilities |
|---|---|---|
| **Webhook Producer** | `POST /payments/webhook/{tenant_id}` | Tenant lookup, signature verification, fast-parse payload, publish to `webhook-events.fifo`. **No DB access.** |
| **Webhook Consumer** | SQS `webhook-events.fifo` | Fast-path dedup, primary idempotency guard, DB transitions, INSERT payments/refunds/order_events, publish to `fulfillment-queue.fifo` on `paid` transitions |
| **Fulfillment Lambda** | SQS `fulfillment-queue.fifo` | Invoke `FulfillmentHook`, transition order to `fulfilled` or `fulfillment_failed`, write `order_events` |

Webhook Producer must return `200` to Razorpay within 500 ms. Webhook Consumer and Fulfillment Lambda both have SQS-native retry (`maxReceiveCount=5`) and DLQs (`webhook-events-dlq.fifo`, `fulfillment-dlq.fifo`).

**Network posture is tier-dependent.** Free Track: all Lambdas public, no VPC, no Interface Endpoints, $0 network cost. Paid Track / RDS-in-VPC: Consumer and Fulfillment move into private subnets with SQS / Secrets Manager / Logs / Monitoring VPC Interface Endpoints. Switching is a CDK diff; application code is identical.

DB: PostgreSQL. Tables: `tenants`, `orders`, `payments`, `refunds`, `webhook_events`, `order_events`. All schema in Alembic migrations. No `metadata.create_all()`. `updated_at` is maintained by a DB-level `BEFORE UPDATE` trigger, never by application code.

## Critical Invariants

1. **Tenant isolation**: Every query against `orders`, `payments`, `refunds`, `webhook_events`, and `order_events` must include a `tenant_id` filter. Enforced by `OrderService` and the Webhook Consumer, never by the caller.
2. **Tenant ID source**: `tenant_id` is extracted from the URL path parameter on the webhook route. It must never come from the request body, query string, or header.
3. **Webhook secret lookup**: Verify `X-Razorpay-Signature` against the tenant-specific `webhook_secret` first, fall back to `webhook_secret_previous` if set. Both checks use `hmac.compare_digest`. Verification precedes payload deserialization. Runs in the **Webhook Producer**.
4. **Producer has no DB credentials**: The Webhook Producer holds IAM permission only for `webhook-events.fifo:SendMessage` and the per-tenant secret read. Any DB access from the producer module is a bug.
5. **Primary idempotency guard (event-type aware)**: Inside the Consumer's DB transaction, after `SELECT ... FOR UPDATE` on the order, check whether the event's target state has already been reached. Rules per event type are in DESIGN_v6 Section 2.1 step 10 and the webhook-idempotency rule. `event_id ON CONFLICT DO NOTHING` is the secondary fast-path.
6. **State transition ordering**: Acquire `SELECT ... FOR UPDATE` on `orders` before any state change. Assert `event_amount == order.amount` on capture and authorize events.
7. **Order creation sequence**: INSERT local `orders` row with `status=initializing` before calling Razorpay. Razorpay is never ahead of the DB.
8. **DB commit before side effects**: The Consumer's DB transaction commits before any `fulfillment-queue.fifo` publish, log, or external call.
9. **Correlation ID propagation**: Generate a UUIDv4 at the Producer entry. Carry through `webhook-events.fifo` `MessageAttributes`, persist on `webhook_events`, `orders`, `order_events`. Re-bind from `MessageAttributes` at the Consumer and Fulfillment Lambda entries. Pass to `FulfillmentHook.metadata`. Every log line and OTel span carries it.
10. **`received_at` is producer-set**: The Consumer takes `received_at` from the SQS message envelope, not `NOW()`. This preserves Razorpay-perceived receipt time despite the producer-to-consumer hop.

## State Machine

`initializing -> created`; `created -> attempted | failed`; `attempted -> authorized | paid | failed`; `authorized -> paid | failed`; `failed -> attempted` (retry); `paid -> fulfilled | fulfillment_failed | refunded`; `fulfilled -> refunded`; `fulfillment_failed -> fulfilled | refunded`.

Any other edge raises `IllegalTransitionError`. Special case: `payment.failed` on `paid`, `fulfilled`, or `fulfillment_failed` is a no-op (mark event `done`).

`fulfillment_failed` is distinct from `failed` so payment-side failures and delivery-side failures are not conflated. Full transition table: DESIGN_v6 Section 4 and the `payment-state-machine` skill.

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
- **Dashboards** (see DESIGN 17.14 / 17.15 + docs/PHASES.md Phase 17):
  - `/dev/dashboard` (full Mission Control, write-capable scenario harness) is gated on `RAZORPAY_DEV_ROUTES_ENABLED=true`. Must be unset in any deployed environment.
  - `/admin/dashboard` (read-only) is gated on `X-Admin-Api-Key`. The frontend stores the key in `sessionStorage`, never `localStorage`.
  - State-graph visited detection in both dashboards reads `*_at` timestamps from the order row, never the live `status` string — otherwise terminal-on-branch orders (`failed`, `refunded`) render their prior lanes grey.
  - Aggregated `/dev/dashboard-data` and `/admin/dashboard-data` endpoints scope per-order webhooks via JSONB path on the payment/refund/order entity, not by tenant alone.

## Gotchas

- Razorpay may retry a webhook with a **new `event_id` for the same payment**. The event-type-aware primary guard inside the DB transaction catches this; do not rely on `event_id` uniqueness alone.
- Lambda freezes process CPU after the HTTP response is flushed. Do not schedule asyncio background tasks in the webhook handler. Use SQS.
- `FulfillmentHook` must be idempotent: SQS provides at-least-once delivery. Fulfillment Lambda also short-circuits if `order.status='fulfilled'` when it acquires the row lock.
- HTTP status for unknown `tenant_id` is **400 Bad Request**, not 404. The multi-tenant URL space is publicly enumerable; 400 prevents tenant existence leak.
- `payment.authorized` does NOT insert a `payments` row. Only terminal payment outcomes (`captured`, `failed`) do.
- `refund.processed` UPDATEs the existing captured `payments` row; do NOT insert a new `payments` row. Insert into `refunds` instead.

## Token Management & Context Protocol

- Do not ingest or process `designs/DESIGN_v6.md` for routine tasks (debugging, linting, or documentation).
- Only reference `designs/DESIGN_v6.md` when the task explicitly requires modifying the state machine, altering the database schema, or changing the core webhook architecture.
- For all other tasks, the **Critical Invariants**, **Architecture**, **State Machine**, and **Webhook Event Routing** sections of this `CLAUDE.md` (plus the `.claude/rules/` files) are the sufficient source of truth.
- When providing code, prioritize functional logic over boilerplate unless a full rewrite is requested, to minimize output token costs.

## Editing Rule

Provide targeted fixes and incremental updates only. Do not rewrite entire files. Maintain existing structure and context. Identify exact change locations and minimize token usage.

## Git Operations Rule

Never run `git commit`, `git push`, `git tag`, `git merge`, or any other git command that writes to history or to a remote. Never pass `-c user.email=...` or `-c user.name=...`, and never run `git config user.email` or `git config user.name`. The user owns their git identity configuration.

When the user asks for a commit, stage with `git add` if needed, then output the proposed commit message and the exact command for the user to run themselves. Read-only git operations (`status`, `log`, `diff`, `show`, `branch`) are fine without asking.
