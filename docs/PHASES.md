# Phase history

What shipped in each logical phase of development and why. Companion to `git log` and [IMPLEMENTATION_PLAN_V2.md](IMPLEMENTATION_PLAN_V2.md).

Phases grouped by the deliverable they produced, not strictly by commit. Several commits often belong to one phase. Cross-references to DESIGN_v6 Section 17 (learnings) included where relevant.

---

## Phases 1-2 — Foundations + database schema

**Goal:** stand up the repo skeleton, logging, correlation IDs, and the full database schema as Alembic migrations.

**Shipped:**
- `razorpay_checkout/` Python package skeleton with `pyproject.toml`, `uv` lockfile.
- `docker-compose.yml` (Postgres + LocalStack).
- `logging_config.py` with structured logs and secret redaction via `register_secret`.
- `correlation.py` with `ContextVar`-based correlation ID propagation.
- 7 Alembic migrations creating `tenants`, `orders`, `payments`, `refunds`, `webhook_events`, `order_events` plus the `set_updated_at()` trigger function and `BEFORE UPDATE` triggers on `orders`.

**Why:** state lives in Postgres for ACID guarantees; correlation IDs and structured logs are non-negotiable for an at-least-once async system. Alembic for migrations because hand-rolled `CREATE TABLE` drifts.

**Key files:** `razorpay_checkout/logging_config.py`, `razorpay_checkout/correlation.py`, `razorpay_checkout/migrations/versions/000{1..7}_*.py`.

**Learning captured:** None yet at this stage; bugs surfaced later.

---

## Phases 3-4 — Config, state machine, signature verification

**Goal:** pydantic-settings-based `RazorpayConfig`, the order state machine with `IllegalTransitionError`, HMAC signature utilities, and `TenantConfigRegistry`.

**Shipped:**
- `RazorpayConfig` (`BaseSettings`, env prefix `RAZORPAY_`, `SecretStr` for secrets).
- `state_machine.py`: `OrderStatus` enum, `ALLOWED_TRANSITIONS` dict, `assert_transition`, `IllegalTransitionError`.
- `signatures.py`: `verify_webhook_signature` and `verify_callback_signature` using `hmac.compare_digest`.
- `TenantConfigRegistry` keyed by `tenant_id`.
- `db.py`: asyncpg pool factory with `slow_acquire_ms` instrumentation.

**Why:**
- pydantic v2 over hand-rolled config so secret values auto-redact on `repr()` and we get validation for free.
- A strict state machine catches off-by-one bugs at the API boundary, not in production.
- Tenant config registry lets one set of Lambdas serve N tenants without redeploying.

**Key files:** `razorpay_checkout/config.py`, `razorpay_checkout/state_machine.py`, `razorpay_checkout/signatures.py`, `razorpay_checkout/tenants.py`, `razorpay_checkout/db.py`.

**Learning captured:** Section 17.1 (logging factory must defer `sys.stdout` resolution).

---

## Phase 5 — Order service, Razorpay client, API routes

**Goal:** the synchronous order-creation path. Customer hits POST /payments/orders, server creates a Razorpay order, persists the local row, returns the data the widget needs.

**Shipped:**
- `RazorpayClient` (`httpx.AsyncClient` wrapper).
- `AmountResolver` protocol + `FixedAmountResolver` default.
- `OrderService.create_or_get` (idempotent per `(tenant_id, receipt)`, two-transaction pattern with `pg_advisory_xact_lock` to serialize concurrent calls).
- `OrderService.transition` (FOR UPDATE, allowed-edge assert, order_events INSERT).
- FastAPI router with POST `/payments/orders`, GET `/payments/orders/{id}`, POST `/payments/verify-callback`.

**Why:**
- Idempotent order creation matters because customers double-click "Pay". The advisory lock prevents two Razorpay orders for one receipt.
- Verify-callback exists for hosts whose widget code wants to confirm signature server-side before sending the webhook (Razorpay-paranoid integrations).

**Key files:** `razorpay_checkout/orders.py`, `razorpay_checkout/client.py`, `razorpay_checkout/amounts.py`, `razorpay_checkout/router.py`.

**Learnings captured:**
- Section 17.2 (FastAPI introspection breaks under `TYPE_CHECKING` + future annotations).
- Section 17.3 (UNIQUE constraint isn't sufficient to deduplicate side effects — hence `pg_advisory_xact_lock`).
- Section 17.4 ("terminal" has two senses in the order state machine).

---

## Phases 6-7 — Producer, SQS publish, simulator

**Goal:** the public-facing webhook endpoint. Verify signature, parse, publish to SQS within 500ms. Plus a CLI to simulate Razorpay webhooks locally without touching the real API.

**Shipped:**
- `WebhookProducer` class — no DB access, holds `TenantConfigRegistry` + SQS client.
- POST `/payments/webhook/{tenant_id}` route.
- SQS queue module: `publish_webhook_event`, `publish_fulfillment_message`, `ensure_*_queues` bootstrap.
- `scripts/razorpay-simulator.py` — builds canonical Razorpay payloads, signs with `dev_webhook_secret`, POSTs to the dev backend.

**Why:**
- Splitting producer from consumer is a security boundary (Critical Invariant 4) and an SLA boundary (Razorpay's 500ms ack).
- Local simulator means we can drive the full flow without internet access or rate limits.

**Key files:** `razorpay_checkout/webhook_producer.py`, `razorpay_checkout/queue.py`, `razorpay_checkout/router.py` (webhook route), `scripts/razorpay-simulator.py`.

**Learning captured:** Section 17.5 (`received_at` belongs to the producer, not the consumer).

---

## Phase 8 — Webhook consumer + idempotency

**Goal:** drain `webhook-events.fifo`, apply event-type-aware idempotency, walk state transitions, INSERT payments / refunds rows, publish to fulfillment queue after commit.

**Shipped:**
- `webhook_idempotency.py`: `SkipDecision` enum, `should_skip` (per event type).
- `WebhookConsumer.process_record` orchestration with `_run_in_transaction`, fast-path dedup, primary guard, amount assertion, per-event handlers.
- `webhook_handlers.py`: `handle_payment_authorized`, `handle_payment_captured`, `handle_payment_failed`, `handle_refund_processed`, `handle_order_paid`.
- Lambda entry shim + DLQ poller.

**Why:**
- Idempotency is the most subtle correctness property in this system. Two layers: `event_id ON CONFLICT` for the literal-duplicate case, primary guard for Razorpay's same-payment-different-event-id case.
- INSERT-after-commit ordering for SQS (Critical Invariant 8) prevents the "ghost message" failure mode where SQS sees the publish but the DB rolled back.

**Key files:** `razorpay_checkout/webhook_idempotency.py`, `razorpay_checkout/webhook_consumer.py`, `razorpay_checkout/webhook_handlers.py`.

**Learnings captured:**
- Section 17.6 (asyncpg requires `datetime` for TIMESTAMPTZ params).
- Section 17.7 (orchestrator post-handler writes can overwrite handler-set state).

---

## Phase 9 — Fulfillment + auxiliary workers

**Goal:** consume `fulfillment-queue.fifo`, invoke the host's `FulfillmentHook`, transition to `fulfilled`. Plus stale-paid monitor and DLQ pollers.

**Shipped:**
- `FulfillmentHook` Protocol + `DefaultFulfillmentHook` (no-op).
- `FulfillmentProcessor.process_record` with FOR UPDATE short-circuit on `fulfilled`.
- Stale-paid monitor handler.
- Fulfillment DLQ poller.

**Why:**
- The host's fulfillment logic lives in their code, not ours. The hook protocol is the seam. We just guarantee at-least-once delivery and idempotent short-circuit.
- Stale-paid monitor catches the "consumer committed paid but SQS publish errored" failure mode.

**Key files:** `razorpay_checkout/fulfillment.py`, `razorpay_checkout/stale_paid_monitor.py`, `razorpay_checkout/fulfillment_dlq_poller.py`, `razorpay_checkout/webhook_events_dlq_poller.py`.

**Learning captured:** Section 17.8 (asyncpg returns JSONB as `str` by default; register a codec).

---

## Phase 10 — Admin routes

**Goal:** the breakglass operator surface. Reconciliation, event replay, retry fulfillment.

**Shipped:**
- `reconcile` module: Razorpay `list_payments` + `payments`-table-vs-API cross-check.
- GET `/payments/reconcile` — returns mismatches.
- POST `/payments/admin/events/{event_id}/replay` — re-process a webhook event.
- POST `/payments/admin/orders/{order_id}/retry-fulfillment` — re-publish to fulfillment queue.
- `require_admin` dependency (`X-Admin-Api-Key` match).

**Why:**
- Reconciliation catches webhook-loss scenarios (Razorpay sent it, we never got it). Daily cron in production.
- Replay and retry are the operator's escape hatches when something gets stuck — see `stuck-paid-order.md` runbook.

**Key files:** `razorpay_checkout/reconcile.py`, `razorpay_checkout/router.py` (admin section).

---

## Phase 11 — Observability

**Goal:** OpenTelemetry exporter selector (stdout/xray/otlp), 10 free-tier custom metrics, log sanitization regression test.

**Shipped:**
- `telemetry.py`: `configure_otel`, 10 metric instruments (`webhook_received_count`, `webhook_duplicate_count`, `paid_orders_total`, `fulfillment_*`, `signature_mismatch_total`, etc.).
- `_CorrelationIdSpanProcessor` adding correlation_id to every span.
- Log sanitization test asserting no secret values appear in logs.
- CloudWatch Logs Insights queries documented.

**Why:**
- Observability is the on-call's flashlight. Without metrics, every incident is archaeology.
- Free-tier-friendly metric count (10) means we never bust CloudWatch's custom metric pricing.

**Key files:** `razorpay_checkout/telemetry.py`, `tests/test_telemetry.py`, `tests/test_log_sanitization.py`.

**Learning captured:** Section 17.9 (ConsoleMetricExporter pins `sys.stdout` and runs on a daemon thread). Also the unmarked "set_meter_provider is set-once" trap.

---

## Phase 12 — Frontend (TypeScript widget + dev test page)

**Goal:** the React + vanilla checkout-widget npm package, plus a dev FastAPI backend that wires everything.

**Shipped:**
- `@akhiljoe/checkout-widget` TypeScript package with tsup dual ESM/CJS build.
- React adapter `<RazorpayCheckout>`, vanilla `mountCheckout`.
- 15 vitest tests with jsdom env.
- `example/backend/main.py` wiring lifespan, OrderService, SQS, producer.
- Original `example/frontend/test_page.html`.

**Why:**
- Server-authoritative amount: the widget never sends `amount`, the server computes it via the injected `AmountResolver`. Prevents tampering at the network boundary.

**Key files:** `packages/checkout-widget/src/*`, `packages/checkout-widget/tsup.config.ts`, `example/backend/main.py`.

**Tsup dual-format quirk:** had to add `outExtension` callback to force `.cjs` for CJS output, otherwise the `exports` map referenced files that didn't exist. (Not formally numbered in Section 17 but documented in `package.json`/commit comment.)

---

## Phase 13 — Local SQS pump + dashboard + chaos tests

**Goal:** close the dev loop. A local pump drains both SQS queues into the real consumer + fulfillment processors. A 17-step Payment Journey dashboard renders the flow live. A buyer-perspective storefront simulates the customer flow. Three chaos pytest tests cover what the browser can't.

**Shipped:**
- `scripts/local-pump.py` — standalone process draining both queues.
- `scripts/local-fixtures.py` — status/seed/purge/reset CLI for dev DB + SQS.
- `example/frontend/dashboard.html` — Payment Journey + 11-scenario regression harness + snapshot copy-to-clipboard.
- `example/frontend/shop.html` — buyer-perspective storefront with real Razorpay modal.
- `tests/test_chaos.py` — 3 tests: concurrent dedup, poison → DLQ, DB transient retry.
- Auto-hop `created → attempted` in `handle_payment_authorized` and `handle_payment_captured` for server-to-server / verify-callback-less integrations.
- Fix to `_bind_correlation_id` accepting both raw SQS dict and flattened string MessageAttribute shapes.

**Why:**
- Without the local pump, the queue would fill up but never drain — making the dev loop incomplete.
- The dashboard is the regression harness: 11 buttons assert every Critical Invariant in seconds.
- Auto-hop covers a real production scenario where Razorpay fires payment.captured directly without a verify-callback. The strict state machine is preserved (no new direct edge); the consumer inserts the missing `attempted` step.

**Key files:** `scripts/local-pump.py`, `scripts/local-fixtures.py`, `example/frontend/dashboard.html`, `example/frontend/shop.html`, `razorpay_checkout/webhook_handlers.py` (auto-hop), `razorpay_checkout/webhook_consumer.py` + `fulfillment.py` (`_bind_correlation_id` fix), `tests/test_chaos.py`.

**Learnings captured:**
- Section 17.10 (stray shell exports silently override `.env` in pydantic-settings).
- Section 17.11 (only `RAZORPAY_`-prefixed keys auto-load from `.env`; AWS values need manual lift).
- Section 17.12 (`_bind_correlation_id` must accept both raw and flattened MessageAttribute shapes).

---

## Phase 14 — DLQ replay CLI + operational runbooks

**Goal:** ship the operator's toolkit and the on-call playbooks. Make incidents survivable.

**Shipped:**
- `scripts/dlq-replay.py` — list / inspect / replay / delete for either DLQ. Localhost-safe by default.
- 5 runbooks: `dlq-drain`, `stuck-paid-order`, `webhook-secret-rotation`, `tenant-onboarding`, `incident-checklist`.
- `docs/runbooks/README.md` index.

**Why:**
- Runbooks are written before the first production incident, not after. Cheaper that way.
- DLQ CLI bypasses the SQS console's quirks (visibility-timeout traps, batch limits).

**Key files:** `scripts/dlq-replay.py`, `docs/runbooks/*.md`.

**Learning captured:** Section 17.13 (SQS `receive_message` locks messages even for read-only inspection — release visibility after read-only ops).

---

## Phase 15 — AWS foundation (CDK Python)

**Goal:** the account-level scaffolding that everything else attaches to. No actual deploy yet.

**Shipped:**
- `infra/app.py` + `cdk.json` + `pyproject.toml` — CDK Python entry, env-aware via context.
- `infra/stacks/foundation.py` — `FoundationStack` with 3 ECR repos, 4 log groups, ops SNS topic, 3 AWS Budgets ($5/$25/$100 monthly).
- `docs/cloud/{account-setup,iam-bootstrap,cost-controls}.md` + README index.

**Why:**
- ECR repos must exist before the PaymentsStack Lambdas can reference container images.
- Budgets are the cost safety net. 3 thresholds catch different failure modes.

**Key files:** `infra/app.py`, `infra/stacks/foundation.py`, `infra/cdk.json`, `docs/cloud/*.md`.

---

## Phase 16 — PaymentsStack (the runtime)

**Goal:** Lambdas, SQS, KMS, IAM, Function URL, EventBridge schedule. The whole runtime as CDK code, ready to deploy.

**Shipped:**
- `infra/stacks/payments.py` — `PaymentsStack` (~400 lines).
- KMS customer-managed key with rotation.
- Secrets Manager DB DSN secret (or imported via context).
- 2 SQS FIFO queue pairs with `maxReceiveCount=5`, 180s visibility timeout.
- 3 Lambda `DockerImageFunctions` sourcing `:latest` from Phase 15 ECR repos.
- 3 IAM roles. Producer has explicit `Deny` on `rds:*` and the DB secret (IAM enforcement of Critical Invariant 4).
- Producer Function URL with `auth=NONE` and CORS.
- SQS event source mappings (batch_size=1 for FIFO).
- EventBridge `rate(5 minutes)` for stale-paid-monitor.

**Why:**
- Function URL over API Gateway because we have no need for the latter's features (auth happens in the Lambda; throttling is enough at the URL level). Saves $3.50/M requests.
- `batch_size=1` because FIFO + at-least-once + per-message idempotency means batching gains nothing. Avoids partial-batch retry confusion.

**Key files:** `infra/stacks/payments.py`.

---

## Phase 17 — Production-grade dashboards (Mission Control)

**Goal:** turn the Phase 13 dev dashboard into a two-surface ops UI: a
local-only `/dev/dashboard` for development + scenario regression, and
a read-only `/admin/dashboard` that is safe to expose on a deployed
environment. Both share a Mission Control aesthetic (dark theme,
JetBrains Mono + IBM Plex Sans, cyan accent) and the same 17-step
Payment Journey, but read from disjoint route gates.

**Shipped:**
- **Backend gating:**
  - `RAZORPAY_DEV_ROUTES_ENABLED` env flag gates `/dev/*` (dashboard,
    `/dev/reset`, `/dev/simulate`, `/dev/flow/{id}`). Default `false`;
    must be unset in any prod deployment.
  - `X-Admin-Api-Key` header gates `/admin/*` (read-only). `None` →
    `403`. Aggregated endpoints: `/admin/dashboard-data`,
    `/admin/kpis`, `/admin/orders/{order_id}`.
- **Aggregated endpoint** `/dev/dashboard-data` +
  `/admin/dashboard-data` with filter params (`status_in`,
  `today_only`, `stuck_paid`, `tenant_id`) so the KPI strip and grouped
  Recent list share a single round-trip.
- **Per-order JSONB scoping** in the selected-order block so the
  detail panel only shows webhooks that name the open order.
- **Frontend:**
  - `example/frontend/dashboard.html` (Mission Control): KPI strip
    with six tile-filters, alarm banner (DLQ > 0, stuck-paid > 0),
    tenant filter, grouped Recent (`Errors` / `In-flight` /
    `Completed` / `Refunded`), Payment Journey with lane headers +
    per-stage `Δms` badges (adjacent lanes only), test-scenario
    harness, snapshot copy.
  - `example/frontend/admin-dashboard.html`: read-only mirror gated by
    admin API key; same Journey + grouped Recent + Snapshot panel.
- **Audit-driven polish (single commit):**
  - Timestamp-driven state-graph visited detection (DESIGN 17.14).
  - Admin key migrated to `sessionStorage` (DESIGN 17.15).
  - Stale-data banner + `body.stale` opacity cue on refresh failure.
  - Selected order auto-expands its bucket.
  - Status-adaptive Outcome step (different copy + check/✕ per terminal).
- **Operational scripts:**
  - `scripts/prod-smoke.py` — read-only `/health` + `/admin/kpis` +
    `/admin/dashboard-data` health probe; exit non-zero on regression.
  - `scripts/check-stuck-paid.py` — direct DB query for orders stuck
    at `paid` past a threshold; CSV output for pipelines.
  - `scripts/reconcile-diff.py` — daily reconcile cron against
    `/payments/reconcile`; CSV mode for analysis.

**Why:**
- The Phase 13 dashboard was a dev-only tool that committed to no
  network policy — fine for local, unsafe for any environment with a
  routable URL. Splitting into `/dev/*` (env-flag-gated, write-capable)
  and `/admin/*` (header-gated, read-only) lets the same UI ship in
  both contexts without leaking dev affordances.
- A single aggregated endpoint avoids the N+1 round-trips the Phase 13
  dashboard had between KPIs, Recent, and detail panels — auto-refresh
  is now one request per cycle.
- The audit-driven polish surfaced from a user-led design review; each
  fix has a one-line learning embedded in DESIGN 17.14.

**Key files:** `example/backend/main.py` (route gates + aggregated
endpoint + JSONB scoping), `example/backend/.env`
(`RAZORPAY_DEV_ROUTES_ENABLED`, `RAZORPAY_ADMIN_API_KEY`),
`example/frontend/dashboard.html`,
`example/frontend/admin-dashboard.html`, `scripts/prod-smoke.py`,
`scripts/check-stuck-paid.py`, `scripts/reconcile-diff.py`.

**Learnings captured:**
- Section 17.14 (dashboard visited-state must be timestamp-driven,
  plus per-step duration badges, JSONB webhook scoping, stale-data
  banner, selected-order auto-expand).
- Section 17.15 (admin API key in `sessionStorage`, not
  `localStorage`).

---

## (Phases 18-19, future)

- **Phase 18:** Dockerfiles, build-and-push script, first real `cdk deploy`, smoke against deployed URL.
- **Phase 19:** TEST_PLAN.md, SCALE_PLAN.md, project-closeout docs.

Both are optional; phases 1-17 form a self-contained, locally-shippable artifact.

---

## How to use this file

1. Reading `git log`? Cross-reference here to understand the why behind each commit.
2. Wondering "when did we add X?" — Ctrl-F for the feature; the phase it landed in tells you which commits to read.
3. Looking for a hard-earned learning? Each phase's "Learnings captured" line points to DESIGN_v6 Section 17 entries.

## Related

- [`IMPLEMENTATION_PLAN_V2.md`](IMPLEMENTATION_PLAN_V2.md) — forward-looking phase plan.
- [`designs/DESIGN_v6.md`](../designs/DESIGN_v6.md) — formal design + Section 17 learnings.
- `git log --oneline -20` — actual commit history.
