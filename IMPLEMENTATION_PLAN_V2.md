# Razorpay Payment Integration: Phased Implementation Plan (V2)

Source of truth: `designs/DESIGN_v5.md`. Phases are ordered by hard dependency. Every step is atomic. Every phase gates on local verification and ends in a committed checkpoint.

**Standing constraints** (apply to every step below):

- **Local-first.** Postgres via Docker Compose, AWS via LocalStack. No real AWS calls until the dedicated cloud phase.
- **Structured JSON logs from line one.** `structlog` with `JSONRenderer`. No `print`. Every record carries `timestamp`, `level`, `module`, `correlation_id`, and `tenant_id`/`order_id`/`payment_id` where applicable.
- **CDK in Python**, under `infra/`.
- **Every commit is verified.** No placeholder stubs survive a phase gate.

---

## Phase: Repository Scaffolding and Logging Foundation

- [ ] **What:** Initialize the git repository and create the directory tree from DESIGN Section 1.
  **Output:** `razorpay_checkout/`, `packages/checkout-widget/`, `example/{backend,frontend}/`, `infra/stacks/`, `scripts/`, `designs/` directories present; `.gitignore` covering `__pycache__/`, `.venv/`, `node_modules/`, `dist/`, `.env`, `cdk.out/`, `.pytest_cache/`, `localstack-volume/`; initial `README.md` linking to `designs/DESIGN_v5.md`.

- [ ] **What:** Create the Python package manifest with pinned runtime and dev dependencies.
  **Output:** `razorpay_checkout/pyproject.toml` declaring `fastapi`, `pydantic`, `pydantic-settings`, `asyncpg`, `httpx`, `structlog`, `opentelemetry-*`, `mangum`, `alembic`, `boto3`, plus dev deps `pytest`, `pytest-asyncio`, `pytest-cov`, `respx`, `ruff`, `mypy`; `uv sync` resolves cleanly producing `uv.lock`.

- [ ] **What:** Add the local infrastructure compose file with Postgres 16 and LocalStack 3.
  **Output:** `docker-compose.yml` with healthy `postgres:16-alpine` (port 5432) and `localstack/localstack:3.7` (port 4566, services `sqs,lambda,logs,secretsmanager,events,iam,cloudwatch`); `.env.example` with `DATABASE_URL`, `AWS_ENDPOINT_URL`, `AWS_DEFAULT_REGION`, dummy AWS keys; `docker compose up -d` reports both services healthy.

- [ ] **What:** Implement the structured logging bootstrap with secret redaction.
  **Output:** `razorpay_checkout/razorpay_checkout/logging_config.py` exporting `configure_logging` and `get_logger`; the redaction processor scrubs any string in a process-wide `SECRET_VALUES` set; a smoke run emits one valid JSON log line carrying `timestamp`, `level`, `module`, `tenant_id`, `order_id`.

- [ ] **What:** Implement the correlation ID context module backed by `contextvars`.
  **Output:** `razorpay_checkout/razorpay_checkout/correlation.py` exporting `new_correlation_id`, `set_correlation_id`, `get_correlation_id`, and a FastAPI dependency `correlation_id_dependency`; `tests/test_correlation.py` passes, asserting context propagation through `asyncio.gather` and binding into structlog's JSON output.

- [ ] **What:** Add a CI-compatible lint and type-check configuration.
  **Output:** `ruff` and `mypy` configured in `pyproject.toml`; `uv run ruff check .` and `uv run mypy razorpay_checkout/` both exit 0 on the scaffolded code.

**Phase gate.** `docker compose ps` healthy; `uv run pytest -q tests/test_correlation.py` green; `uv run ruff check .` clean; tree matches DESIGN Section 1.
**Commit:** `git commit -m "scaffold: repo layout, docker compose, structured logging, correlation context"`

---

## Phase: Database Schema and Migrations

> Depends on: scaffolding.

- [ ] **What:** Initialize Alembic inside the Python package with async support.
  **Output:** `razorpay_checkout/alembic.ini` and `razorpay_checkout/razorpay_checkout/migrations/env.py` reading `DATABASE_URL` from the environment; `uv run alembic current` prints empty without error.

- [ ] **What:** Migration 0001 creates `tenants` and the shared `set_updated_at` trigger function.
  **Output:** Revision `0001_tenants.py` applied; `\d tenants` shows the design columns; `SELECT proname FROM pg_proc WHERE proname='set_updated_at'` returns one row; `pgcrypto` extension enabled.

- [ ] **What:** Migration 0002 creates the `orders` table with all timestamps and CHECK constraints, plus the `BEFORE UPDATE` trigger.
  **Output:** Revision `0002_orders.py` applied; insert with `status='initializing'` succeeds; an `UPDATE` reflects a fresh `updated_at` via the trigger.

- [ ] **What:** Migration 0003 adds the four secondary `orders` indexes from DESIGN Section 3.
  **Output:** Revision `0003_orders_indexes.py` applied; `\d orders` lists `orders_tenant_receipt_unique`, `orders_tenant_status_idx`, `orders_created_at_idx`, `orders_correlation_id_idx`.

- [ ] **What:** Migration 0004 creates `payments` with `is_authoritative` and three indexes.
  **Output:** Revision `0004_payments.py` applied; `\d payments` shows the unique on `razorpay_payment_id` plus `order_id`, `razorpay_order_id`, and `tenant_id` indexes.

- [ ] **What:** Migration 0005 creates `refunds` with `initiated_by` and two indexes.
  **Output:** Revision `0005_refunds.py` applied; `\d refunds` shows the unique on `razorpay_refund_id` and the `order_id` and `tenant_id` indexes.

- [ ] **What:** Migration 0006 creates `webhook_events` with `UNIQUE(tenant_id, event_id)` and three indexes.
  **Output:** Revision `0006_webhook_events.py` applied; an INSERT plus duplicate INSERT confirms the unique constraint fires.

- [ ] **What:** Migration 0007 creates `order_events` with three indexes.
  **Output:** Revision `0007_order_events.py` applied; full downgrade-then-upgrade round-trip succeeds on a fresh DB.

**Phase gate.** `alembic downgrade base && alembic upgrade head` succeeds; `pg_dump --schema-only` matches DESIGN Section 3 exactly.
**Commit:** `git commit -m "db: alembic migrations for tenants, orders, payments, refunds, webhook_events, order_events"`

---

## Phase: Core Config, Tenant Registry, and DB Pool

> Depends on: schema applied locally.

- [ ] **What:** Implement `RazorpayConfig` as a Pydantic v2 `BaseSettings` model with `SecretStr` for all secrets.
  **Output:** `razorpay_checkout/razorpay_checkout/config.py`; `repr(RazorpayConfig(...))` never prints the underlying secret values; secret strings are registered into the logging redaction set on construction.

- [ ] **What:** Implement `TenantConfigRegistry` for O(1), thread-safe lookup by `tenant_id`.
  **Output:** `razorpay_checkout/razorpay_checkout/tenants.py`; `tests/test_tenants.py` passes for register, get, unknown returns `None`, and 50-thread concurrent register correctness.

- [ ] **What:** Implement HMAC-SHA256 webhook and payment signature utilities with constant-time compare and previous-secret fallback.
  **Output:** `razorpay_checkout/razorpay_checkout/signatures.py` and `tests/test_signatures.py` covering valid signature, tampered body, wrong secret, empty body, previous-secret fallback, payment signature mismatch.

- [ ] **What:** Implement the asyncpg pool factory with explicit `pool_min_size` and `pool_max_size`.
  **Output:** `razorpay_checkout/razorpay_checkout/db.py`; `tests/test_db.py` against local Postgres confirms `SELECT 1` succeeds and pool min/max sizes match config.

**Phase gate.** All four unit suites green; `ruff` and `mypy` clean.
**Commit:** `git commit -m "core: RazorpayConfig, TenantConfigRegistry, signatures, asyncpg pool"`

---

## Phase: Order State Machine

> Depends on: core config.

- [ ] **What:** Define the `OrderStatus` enum and the 14-edge transition table from DESIGN Section 4.
  **Output:** `razorpay_checkout/razorpay_checkout/state_machine.py` with `ALLOWED_TRANSITIONS` of length 14; `tests/test_state_machine.py` asserts inclusion and exclusion samples.

- [ ] **What:** Implement `IllegalTransitionError` and `assert_transition`.
  **Output:** Same module; parametrized test enumerates every legal and illegal edge.

- [ ] **What:** Implement the `payment.failed` stale-event helper `is_stale_failed_event`.
  **Output:** Same module; test confirms it returns `True` for `paid`, `fulfilled`, `fulfillment_failed` and `False` otherwise.

**Phase gate.** State machine suite green; coverage of `state_machine.py` at 100%.
**Commit:** `git commit -m "state-machine: OrderStatus, transitions, IllegalTransitionError, stale-failed helper"`

---

## Phase: OrderService and Order Routes

> Depends on: state machine.

- [ ] **What:** Implement `RazorpayClient.create_order` using `httpx.AsyncClient` with Basic auth and configurable timeout.
  **Output:** `razorpay_checkout/razorpay_checkout/client.py`; `respx`-driven test asserts request shape and timeout handling.

- [ ] **What:** Implement `OrderService.create_or_get` enforcing the `initializing -> created` sequence and `UNIQUE(tenant_id, receipt)` idempotency.
  **Output:** `razorpay_checkout/razorpay_checkout/orders.py`; integration test confirms first call inserts `initializing` then upgrades to `created`; ten concurrent same-receipt POSTs yield exactly one Razorpay call.

- [ ] **What:** Implement `OrderService.transition` with mandatory tenant filter and `order_events` write.
  **Output:** Same module; test confirms legal transition writes the orders UPDATE and an `order_events` row; illegal edge raises `IllegalTransitionError` with zero side effects.

- [ ] **What:** Wire the FastAPI `payments_router` and re-export public symbols.
  **Output:** `razorpay_checkout/razorpay_checkout/router.py` and `__init__.py`; `from razorpay_checkout import payments_router` succeeds with no routes registered yet.

- [ ] **What:** Implement `POST /payments/orders` returning `CreateOrderResponse`.
  **Output:** Route registered; integration test creates an order, replays the same receipt, and verifies 503 path on simulated Razorpay timeout.

- [ ] **What:** Implement `GET /payments/orders/{order_id}` with tenant scoping.
  **Output:** Route registered; test confirms response shape, 404 on unknown id, and 404 on cross-tenant access.

- [ ] **What:** Implement `POST /payments/verify-callback` enforcing payment-signature HMAC and non-terminal-failed check.
  **Output:** Route registered; three tests pass for valid, tampered, and terminal-failed cases.

**Phase gate.** Order suite green; manual `curl POST /payments/orders` returns a populated response with structured logs carrying `correlation_id`.
**Commit:** `git commit -m "orders: OrderService and /payments/orders, /payments/orders/{id}, /payments/verify-callback"`

---

## Phase: Webhook Pipeline

> Depends on: order service.

- [ ] **What:** Implement the SQS FIFO publisher and queue bootstrap helpers for LocalStack.
  **Output:** `razorpay_checkout/razorpay_checkout/queue.py`; `aws --endpoint-url http://localhost:4566 sqs list-queues` shows `fulfillment-queue.fifo` and `fulfillment-dlq.fifo` after `ensure_fulfillment_queue()`.

- [ ] **What:** Implement the `WebhookProcessor` skeleton with tenant lookup, signature verification, fast-path dedup, and transaction shell.
  **Output:** `razorpay_checkout/razorpay_checkout/webhooks.py`; tests confirm unknown-tenant raises, signature mismatch raises, duplicate `event_id` short-circuits with metric increment.

- [ ] **What:** Implement the event-type-aware primary idempotency guard per `.claude/rules/webhook-idempotency.md`.
  **Output:** `should_skip` function and `SkipDecision` enum in the same module; parametrized test matrix over `(event_type, order_status, captured_payment_exists)` covers every row in DESIGN Section 2.1 step 6.

- [ ] **What:** Implement `handle_payment_authorized` with amount assertion and no payments-row insert.
  **Output:** Handler integrated into the dispatcher; simulator-driven test transitions `attempted -> authorized`, writes one `order_events` row, leaves `payments` empty, publishes nothing to SQS.

- [ ] **What:** Implement `handle_payment_captured` covering `failed -> attempted -> paid`, payments row insert with `is_authoritative=TRUE`, and SQS publish after commit.
  **Output:** Handler integrated; three tests pass for single-edge `attempted -> paid`, two-edge `failed -> attempted -> paid`, and SQS-failure-after-commit (order stays `paid`).

- [ ] **What:** Implement `handle_payment_failed` with stale-event no-op and `is_authoritative=FALSE` payments row.
  **Output:** Handler integrated; tests pass for `attempted -> failed` and stale-failed no-op on `paid`.

- [ ] **What:** Implement `handle_refund_processed` that UPDATEs the captured payments row and INSERTs into `refunds`.
  **Output:** Handler integrated; tests pass for `paid -> refunded`, `fulfilled -> refunded`, `fulfillment_failed -> refunded`, and unknown-payment refund flagged `processing_status='failed'`.

- [ ] **What:** Implement `handle_order_paid` as a log-only event.
  **Output:** Handler integrated; test confirms zero state change and one INFO log line.

- [ ] **What:** Register `POST /payments/webhook/{tenant_id}` resolving tenant strictly from the path parameter.
  **Output:** Route registered; manual simulator POST against the running app returns 200 in under 500 ms; unknown-tenant POST returns 400 without persisting a `webhook_events` row.

**Phase gate.** Full webhook suite green; LocalStack SQS receives messages with `correlation_id` in `MessageAttributes`; manual SQS-down test shows order stays `paid` with no message published.
**Commit:** `git commit -m "webhooks: tenant lookup, idempotency guards, event handlers, SQS publish after commit"`

---

## Phase: Razorpay Webhook Simulator

> Depends on: webhook pipeline.

- [ ] **What:** Build `scripts/razorpay-simulator.py` with HMAC signing of canonical payloads for all four event types.
  **Output:** Script supports `payment.authorized`, `payment.captured`, `payment.failed`, `refund.processed`; default URL `http://localhost:8000/payments/webhook/<tenant>`; signed POST yields 200 against the running app.

- [ ] **What:** Document canonical HMAC vectors shared between the simulator and `tests/test_signatures.py`.
  **Output:** `.claude/commands/local-sig-validate.md` containing vectors that match both the simulator output and the signature unit tests.

**Phase gate.** All four event types succeed end-to-end locally; duplicate replay increments `webhook_duplicate_count`.
**Commit:** `git commit -m "tools: razorpay-simulator and canonical HMAC vectors"`

---

## Phase: Fulfillment Lambda and Safety Nets

> Depends on: webhooks.

- [ ] **What:** Define the `FulfillmentHook` Protocol and ship the default no-op implementation.
  **Output:** `razorpay_checkout/razorpay_checkout/fulfillment.py`; test confirms default hook logs and returns `None`.

- [ ] **What:** Implement the Fulfillment Lambda handler with `SELECT FOR UPDATE` short-circuit on already-`fulfilled` orders.
  **Output:** `razorpay_checkout/razorpay_checkout/lambdas/fulfillment.py`; tests pass for short-circuit skip, `paid -> fulfilled` happy path, and exception re-raise on hook failure.

- [ ] **What:** Implement the DLQ poller transitioning exhausted orders to `fulfillment_failed`.
  **Output:** `razorpay_checkout/razorpay_checkout/lambdas/dlq_poller.py`; test confirms `paid -> fulfillment_failed` with the error captured in `order_events.metadata`.

- [ ] **What:** Implement the stale-`paid` monitor handler that flags orders in `paid` for more than 5 minutes.
  **Output:** `razorpay_checkout/razorpay_checkout/lambdas/stale_paid_monitor.py`; test confirms one WARN log line and one metric emission per stale order.

**Phase gate.** End-to-end local flow: simulator triggers `payment.captured`, LocalStack invokes fulfillment Lambda via event-source mapping, order ends in `fulfilled`; DLQ test reaches `fulfillment_failed`.
**Commit:** `git commit -m "fulfillment: hook protocol, lambda handler, dlq poller, stale-paid monitor"`

---

## Phase: Admin Routes

> Depends on: webhook and fulfillment phases.

- [ ] **What:** Implement the `require_admin` dependency comparing `X-Admin-Api-Key` with `hmac.compare_digest` and returning 403 when `admin_api_key is None`.
  **Output:** Dependency in `router.py`; three tests pass for unset, mismatched, and correct keys.

- [ ] **What:** Implement `GET /payments/reconcile` pulling Razorpay payments via cursor pagination and emitting `ReconcileMismatch` rows.
  **Output:** Route registered; integration test seeds three Razorpay payments against two local payments and asserts the right mismatch reasons.

- [ ] **What:** Implement `POST /payments/admin/events/{event_id}/replay` resetting `failed` or `dead` events to `pending`.
  **Output:** Route registered; test transitions a `failed` event back to `pending` and rejects already-pending events with 409.

- [ ] **What:** Implement `POST /payments/admin/orders/{order_id}/retry-fulfillment` re-enqueueing SQS messages with `trigger='admin_retry'` recorded.
  **Output:** Route registered; test on a `paid` order yields one SQS message and a new `order_events` row.

**Phase gate.** Admin suite green; manual `curl` against each route with and without `X-Admin-Api-Key` returns expected codes.
**Commit:** `git commit -m "admin: reconcile, replay event, retry fulfillment endpoints"`

---

## Phase: Observability (OpenTelemetry and Metrics)

> Depends on: webhook and fulfillment.

- [ ] **What:** Implement `configure_otel` selecting the exporter from `RazorpayConfig.otel_exporter` (`stdout`, `xray`, `otlp`) with no downstream branching.
  **Output:** `razorpay_checkout/razorpay_checkout/telemetry.py`; with `OTEL_EXPORTER=stdout`, hitting `/payments/orders` prints JSON spans tagged with `correlation_id` and `tenant.id`.

- [ ] **What:** Register the ten DESIGN Section 13 metric instruments and emit each from its owning code path.
  **Output:** `webhook_duplicate_count`, `order_state_transition_count`, `fulfillment_duration_ms`, `fulfillment_sqs_publish_failures`, `reconciliation_mismatch_count`, `cold_start_ms`, `dead_event_count`, `webhook_latency_ms`, `payment_authorized_count`, `tenant_lookup_failures` all wired; `tests/test_telemetry.py` uses `InMemoryMetricReader` to confirm each instrument records.

- [ ] **What:** Add a regression test asserting no secret value appears anywhere in captured log output during a full happy path.
  **Output:** `tests/test_log_sanitization.py` runs the simulator-driven flow with sentinel secrets and `grep`s captured stdout for zero matches.

- [ ] **What:** Check in CloudWatch Logs Insights queries for end-to-end webhook tracing and a 24-hour dead-event scan.
  **Output:** `razorpay_checkout/observability/logs_insights/webhook_trace.cwlogs` and `dead_events_24h.cwlogs` committed; both referenced from `runbooks/01_stuck_or_dead_webhook_event.md`.

**Phase gate.** Telemetry and sanitization suites green; spans tagged with `correlation_id` visible end-to-end.
**Commit:** `git commit -m "observability: otel exporter selector, ten metrics, log sanitization, logs insights queries"`

---

## Phase: Frontend Checkout Widget

> Depends on: orders and verify-callback routes.

- [ ] **What:** Initialize the TypeScript package with strict tsconfig, `tsup` bundler, and `vitest`.
  **Output:** `packages/checkout-widget/{package.json,tsconfig.json,tsup.config.ts}`; `npm run build` produces `dist/` artifacts; `npm run lint` clean.

- [ ] **What:** Implement `src/core.ts` with the order POST, Razorpay script injection (3 second timeout), and result mapping.
  **Output:** `runCheckout` exported; vitest passes for happy path and `csp_blocked` failure.

- [ ] **What:** Implement the React component `<RazorpayCheckout>` with no `amount` prop.
  **Output:** `src/react.tsx`; React Testing Library asserts click invokes `runCheckout` with expected props.

- [ ] **What:** Implement the vanilla `mountCheckout` adapter returning an unmount function.
  **Output:** `src/vanilla.ts`; JSDOM test confirms mount, click, and clean unmount.

- [ ] **What:** Build the developer test page served by the example FastAPI app for end-to-end manual verification.
  **Output:** `example/backend/main.py` mounts `payments_router` plus `/dev/test-page`; the page creates an order, opens the widget, polls `/payments/orders/{id}` for 30 seconds, and displays `order_id`, status transitions, and tagged log lines in an in-page console.

**Phase gate.** Widget unit tests green; manual end-to-end run against Razorpay test card `4111 1111 1111 1111` reaches `Fulfilled`.
**Commit:** `git commit -m "frontend: checkout-widget package, react+vanilla adapters, developer test page"`

---

## Phase: Local Integration and Chaos Tests

> Depends on: all functional phases.

- [ ] **What:** Author the integration test harness wiring Docker Compose Postgres, LocalStack SQS, and a test tenant registration.
  **Output:** `tests/integration/conftest.py` provides `pg_pool`, `sqs_client`, `app_client`, and `simulator` fixtures.

- [ ] **What:** Implement the full happy-path integration test.
  **Output:** Test `test_happy_path.py` ends with order in `fulfilled`, one `payments` row, and the exact expected `order_events` sequence.

- [ ] **What:** Implement the `failed -> attempted -> paid` retry test.
  **Output:** Test passes with two payments rows (one `is_authoritative=FALSE`, one `TRUE` with `attempt_number=2`) and the expected `order_events` trail.

- [ ] **What:** Implement the duplicate-webhook tests covering both fast-path and primary-guard paths.
  **Output:** Tests confirm same-`event_id` increments `webhook_duplicate_count`; different-`event_id`-same-`payment_id` triggers the primary guard with no extra `order_events`.

- [ ] **What:** Implement the multi-tenant isolation test.
  **Output:** Cross-tenant signature mismatch returns 400; `SELECT count(*) FROM orders WHERE tenant_id='t2' AND status != 'created'` is 0 after a flood of t1 events.

- [ ] **What:** Implement the concurrent-order-creation test with `respx`.
  **Output:** Ten parallel POSTs against the same receipt produce exactly one Razorpay call and one `orders` row.

- [ ] **What:** Implement the SQS-publish-failure recovery test.
  **Output:** With SQS patched to raise, order stays `paid`, `fulfillment_sqs_publish_failures` increments, and admin retry restores end-to-end completion.

- [ ] **What:** Implement the fulfillment-exhaustion-then-recovery test.
  **Output:** Failing hook runs five times, DLQ poller transitions order to `fulfillment_failed`, then admin retry with a healthy hook reaches `fulfilled`.

- [ ] **What:** Implement the three refund-state tests (`paid`, `fulfilled`, `fulfillment_failed` to `refunded`).
  **Output:** All three reach `refunded` with `payments.status='refunded'` and a `refunds` row inserted with `initiated_by='razorpay_dashboard'`.

- [ ] **What:** Implement the unknown-tenant and stuck-`authorized` tests.
  **Output:** Unknown-tenant POST returns 400 and no `webhook_events` row; stuck-`authorized` query returns the 25-hour-old order.

**Phase gate.** `uv run pytest -q --cov=razorpay_checkout --cov-fail-under=85` green.
**Commit:** `git commit -m "tests: integration coverage for happy path, retries, duplicates, multi-tenant, refund, sqs failure"`

---

## Phase: Auxiliary Lambdas and Operational Runbooks

> Depends on: integration tests.

- [ ] **What:** Implement the Neon keep-alive Lambda handler running `SELECT 1`.
  **Output:** `razorpay_checkout/razorpay_checkout/lambdas/keep_alive.py`; unit test passes against local Postgres.

- [ ] **What:** Implement the stuck-`authorized` monitor handler.
  **Output:** `razorpay_checkout/razorpay_checkout/lambdas/authorized_monitor.py`; test emits one metric per stale order.

- [ ] **What:** Author the nine operational runbooks from DESIGN Section 12.
  **Output:** `runbooks/01_stuck_or_dead_webhook_event.md` through `09_refund_out_of_sync.md`, each with Trigger, Steps, Verification sections and concrete `curl` commands plus the relevant Logs Insights query.

**Phase gate.** Auxiliary Lambda tests green; runbook files render and reference live admin endpoints.
**Commit:** `git commit -m "ops: keep-alive, authorized monitor, nine runbooks"`

---

## Phase: AWS Foundation (Pre-Deployment)

> Depends on: fully verified local stack. No CDK deploy yet.

- [ ] **What:** Create the bootstrap IAM administrator user with MFA in the dev account.
  **Output:** `razorpay-bootstrap-admin` user with `AdministratorAccess`; `aws --profile razorpay-bootstrap sts get-caller-identity` returns the ARN.

- [ ] **What:** Define IAM groups `razorpay-deployer`, `razorpay-operator`, `razorpay-readonly` with least-privilege inline policies committed to the repo.
  **Output:** `infra/iam_policies/{deployer,operator,readonly}.json`; `aws iam get-group-policy` returns each JSON matching the file.

- [ ] **What:** Configure AWS CLI named profiles for bootstrap, deployer, and operator roles in dev.
  **Output:** `~/.aws/{credentials,config}` updated; each profile's `sts get-caller-identity` returns the expected ARN; the procedure is documented in `infra/README.md`.

- [ ] **What:** Seed Secrets Manager entries per tenant per environment using a one-off script.
  **Output:** `scripts/seed_secrets.py`; secrets `razorpay/<env>/<tenant>/{key_secret,webhook_secret,webhook_secret_previous,admin_api_key}` exist and retrieve via `aws secretsmanager get-secret-value`.

**Phase gate.** All four AWS CLI checks succeed; `infra/README.md` documents the bootstrap end-to-end.
**Commit:** `git commit -m "aws-foundation: iam groups, named profiles, secrets manager seeding"`

---

## Phase: CDK Infrastructure (Python, Local Synth Only)

> Depends on: AWS foundation.

- [ ] **What:** Initialize the Python CDK project under `infra/` with both stacks listed.
  **Output:** `infra/app.py`, `infra/cdk.json`, `infra/requirements.txt`; `cdk list` outputs `razorpay-free-dev` and `razorpay-paid-prod`.

- [ ] **What:** Implement `FreeStack` with SQS FIFO + DLQ, six Lambda functions, HttpApi routes, schedules, log groups, and alarms.
  **Output:** `infra/stacks/free_stack.py`; `cdk synth razorpay-free-dev` succeeds; synthesized template contains six Lambda functions and two FIFO queues.

- [ ] **What:** Implement `PaidStack` adding provisioned concurrency on the webhook Lambda, WAFv2, 30-day log retention, and the ADOT layer with `OTEL_EXPORTER=xray`.
  **Output:** `infra/stacks/paid_stack.py`; `cdk synth razorpay-paid-prod` succeeds; template includes `ProvisionedConcurrentExecutions`.

- [ ] **What:** Add CDK construct tests asserting resource counts and properties.
  **Output:** `infra/tests/test_free_stack.py` and `test_paid_stack.py`; `pytest infra/tests` green without AWS credentials.

**Phase gate.** Both stacks synth; construct tests green; no deployment performed.
**Commit:** `git commit -m "infra: cdk python free and paid stacks with construct tests"`

---

## Phase: First Cloud Deployment and Validation

> Depends on: CDK ready. Real AWS calls begin here.

- [ ] **What:** Bootstrap the CDK environment in the dev account and region.
  **Output:** `cdk bootstrap` succeeds with `CDKToolkit` stack `CREATE_COMPLETE`.

- [ ] **What:** Deploy `razorpay-free-dev` and capture the HttpApi URL output.
  **Output:** Stack `CREATE_COMPLETE`; deployment outputs the live `https://<api>.execute-api.<region>.amazonaws.com` URL.

- [ ] **What:** Run the local simulator against the deployed webhook URL with secrets fetched from Secrets Manager.
  **Output:** Simulator POST returns 200; CloudWatch Logs Insights `webhook_trace.cwlogs` query with the response's `correlation_id` returns the full path: API entry, `webhook_events` insert, transition to `paid`, SQS publish, fulfillment Lambda, transition to `fulfilled`.

- [ ] **What:** Trigger the DLQ alarm synthetically by injecting a message into `fulfillment-dlq.fifo`.
  **Output:** Alarm transitions to `ALARM`; PagerDuty (or configured sink) receives the page; queue purged after verification.

- [ ] **What:** Execute the live Razorpay test-mode end-to-end flow via the developer test page pointed at the deployed API.
  **Output:** Test page reaches `Fulfilled` within 30 seconds; CloudWatch logs show matching `correlation_id` end-to-end.

**Phase gate.** Live happy path complete; alarm wiring confirmed; Logs Insights returns the full trace.
**Commit:** `git commit -m "deploy: razorpay-free-dev validated end-to-end on aws"`

---

## Phase: Documentation, Deferrals, and Scaling Triggers

> Depends on: deployment. Closeout phase.

- [ ] **What:** Author `TEST_PLAN.md` cross-referencing every bullet in DESIGN Section 14 with its implementing test file and function name.
  **Output:** `TEST_PLAN.md` lists Unit, Integration, and Chaos sections; no row is marked TBD.

- [ ] **What:** Author `SCALE_PLAN.md` capturing every deferred item from DESIGN Section 16 plus the Section 7 SQS upgrade triggers and the Section 9 Fargate trigger.
  **Output:** `SCALE_PLAN.md` table with rows for transactional outbox, scheduled reconciliation, constant-time webhook responses, manual capture admin endpoint, automated capture retry, refund UI and initiation API, partial refunds, subscriptions, multi-currency, partial captures, 3DS edge cases, payment link generation, UPI QR, EMI, international methods, API versioning, SQS Standard + SNS fan-out, `Idempotency-Key` header, vendor-agnostic schema naming, plus the architectural scaling triggers; each row carries the exact promotion trigger from DESIGN_v5.md.

**Phase gate.** Both planning documents complete; CI green on the deployed branch.
**Commit:** `git commit -m "docs: test_plan and scale_plan with deferral promotion triggers"`

---

## Cross-Cutting Verifications (Run Before Every Phase Commit)

| Check | Command | Pass Criterion |
|---|---|---|
| **No `print` calls** | `rg -n "(?<!\\w)print\\(" razorpay_checkout/razorpay_checkout/` | Zero matches outside test fixtures |
| **No secrets in logs** | `uv run pytest tests/test_log_sanitization.py` | Green |
| **Tenant filter on every query** | `rg -n "FROM (orders\|payments\|refunds\|webhook_events\|order_events)" razorpay_checkout/razorpay_checkout/ \| rg -v "tenant_id"` | Zero unfiltered hits |
| **Alembic round-trip** | `uv run alembic downgrade base && uv run alembic upgrade head` | Both succeed |
| **Lint and types** | `uv run ruff check . && uv run mypy razorpay_checkout/` | Both clean |
| **Coverage** | `uv run pytest --cov=razorpay_checkout --cov-fail-under=85` | Pass |

## LocalStack Gaps Re-Validated Post-Deploy

| Behavior | Why It Differs | Validated By |
|---|---|---|
| Lambda cold start | LocalStack reuses containers | First-deploy CloudWatch timing |
| IAM enforcement | Community LocalStack is permissive | Denied call decoded via `sts decode-authorization-message` |
| Metric filter latency | LocalStack lag undefined | Synthetic alarm trip in deployment phase |
| SQS FIFO per-group throttling | LocalStack does not enforce 1/s per group | Post-deploy load test |
| Secrets Manager rotation | LocalStack does not run rotation Lambdas | Runbook 7 dry-run post-deploy |
| X-Ray sampling | LocalStack omits sampling | X-Ray console inspection post-deploy |
