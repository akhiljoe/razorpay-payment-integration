# Razorpay Payment Integration: Phased Implementation Plan

Source of truth: `designs/DESIGN_v5.md`. This plan covers every section without exception. Phases are ordered by hard dependencies, not by team taxonomy.

## Non-Negotiables Anchored in This Plan

1. **Local-first, cloud-later.** Postgres via Docker Compose. AWS via LocalStack. Zero real AWS calls until local verification is complete and committed. **LocalStack caveat:** Lambda cold starts, IAM policy enforcement, CloudWatch metric filter latency, X-Ray sampling decisions, SQS FIFO throughput tail behavior, and Secrets Manager rotation are not faithfully reproduced. These must be re-validated post-deploy.
2. **Structured JSON logging from line one.** `structlog` with `JSONRenderer`. No `print` anywhere. Required fields on every record: `timestamp`, `level`, `module`, `trace_id` (aliased to `correlation_id` for this domain), `tenant_id`, `order_id`, `payment_id` (where applicable). On Lambda, logger writes to `stdout` so CloudWatch ingests it.
3. **CDK in Python.** The design's `infra/bin/app.ts` layout is overridden by the explicit user constraint. CDK lives in `infra/` as Python (`infra/app.py`, `infra/stacks/free_stack.py`, `infra/stacks/paid_stack.py`).
4. **Phase gates.** Each phase ends with a verification block and a literal `git commit` command. The developer does not advance without green verification.

---

## Phase: Repository Scaffolding and Logging Foundation

- [ ] **Initialize the repository skeleton matching DESIGN_v5 Section 1.**
  - **Why:** All later steps assume this layout. Misaligned paths cause import errors and break `pyproject.toml` package discovery.
  - **How:** `git init`. Create directories: `razorpay_checkout/razorpay_checkout/`, `razorpay_checkout/razorpay_checkout/migrations/`, `razorpay_checkout/tests/`, `packages/checkout-widget/src/`, `example/backend/`, `example/frontend/`, `infra/stacks/`, `scripts/`, `designs/`. Add `.gitignore` covering `__pycache__/`, `.venv/`, `node_modules/`, `dist/`, `.env`, `cdk.out/`, `.pytest_cache/`, `htmlcov/`, `*.egg-info/`, `localstack-volume/`.
  - **Files:** Directory tree above. `.gitignore`. `README.md` (one paragraph linking to `designs/DESIGN_v5.md`).
  - **I verify by:** `find . -type d -not -path './.git*' | sort` matches the design layout. `git status` shows the tree staged.

- [ ] **Create `razorpay_checkout/pyproject.toml` with locked Python dependencies.**
  - **Why:** Single dependency manifest keeps Lambda packaging deterministic. Pinning `httpx`, `asyncpg`, `pydantic`, `structlog`, and `opentelemetry` versions prevents drift between local and Lambda runtimes.
  - **How:** Use `uv init --package razorpay_checkout`. Add dependencies: `fastapi>=0.115`, `pydantic>=2.7`, `pydantic-settings>=2.4`, `asyncpg>=0.29`, `httpx>=0.27`, `structlog>=24.1`, `opentelemetry-api>=1.27`, `opentelemetry-sdk>=1.27`, `opentelemetry-exporter-otlp-proto-http>=1.27`, `mangum>=0.17`, `alembic>=1.13`, `boto3>=1.34`. Dev dependencies: `pytest>=8`, `pytest-asyncio>=0.23`, `pytest-cov>=5`, `httpx`, `respx`, `ruff>=0.6`, `mypy>=1.11`. Set `[tool.ruff] line-length = 100`. Set `[tool.pytest.ini_options] asyncio_mode = "auto"`.
  - **Files:** `razorpay_checkout/pyproject.toml`, `razorpay_checkout/uv.lock`.
  - **I verify by:** `cd razorpay_checkout && uv sync && uv run python -c "import fastapi, asyncpg, structlog, opentelemetry"` exits 0.

- [ ] **Add `docker-compose.yml` at the repo root with Postgres 16 and LocalStack.**
  - **Why:** Local-first mandate. Postgres for the live DB. LocalStack for SQS, Lambda, CloudWatch Logs, Secrets Manager, EventBridge.
  - **How:** Services: `postgres` (image `postgres:16-alpine`, env `POSTGRES_USER=razorpay`, `POSTGRES_PASSWORD=razorpay`, `POSTGRES_DB=razorpay_checkout`, port `5432:5432`, volume `pgdata:/var/lib/postgresql/data`, healthcheck `pg_isready -U razorpay`). `localstack` (image `localstack/localstack:3.7`, env `SERVICES=sqs,lambda,logs,secretsmanager,events,iam,cloudwatch`, `DEFAULT_REGION=ap-south-1`, port `4566:4566`, volume `localstack-volume:/var/lib/localstack`). Add `depends_on: postgres: condition: service_healthy` on any app service.
  - **Files:** `docker-compose.yml`, `.env.example` with `DATABASE_URL=postgresql://razorpay:razorpay@localhost:5432/razorpay_checkout`, `AWS_ENDPOINT_URL=http://localhost:4566`, `AWS_DEFAULT_REGION=ap-south-1`, `AWS_ACCESS_KEY_ID=test`, `AWS_SECRET_ACCESS_KEY=test`.
  - **I verify by:** `docker compose up -d` then `docker compose ps` shows both services healthy. `psql postgresql://razorpay:razorpay@localhost:5432/razorpay_checkout -c "SELECT 1"` returns `1`. `curl http://localhost:4566/_localstack/health` returns JSON with `sqs: "available"`.

- [ ] **Create the structured logging bootstrap module.**
  - **Why:** Section 10 mandates JSON output with `correlation_id` on every record. `key_secret` and `webhook_secret` must never appear in logs. This module is imported by every other module from the first commit.
  - **How:** Module exports `configure_logging(level: str = "INFO") -> None` and `get_logger(name: str) -> structlog.BoundLogger`. Use `structlog.configure(processors=[structlog.contextvars.merge_contextvars, structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso", utc=True), structlog.processors.StackInfoRenderer(), structlog.processors.format_exc_info, _redact_secrets_processor, structlog.processors.JSONRenderer()], wrapper_class=structlog.make_filtering_bound_logger(logging.INFO), logger_factory=structlog.PrintLoggerFactory())`. `_redact_secrets_processor` replaces any value matching `RazorpayConfig.key_secret.get_secret_value()` or `webhook_secret.get_secret_value()` with `"***"`. Register `key_secret` and `webhook_secret` candidates via a process-wide `SECRET_VALUES: set[str]` updated by `RazorpayConfig.__init__`.
  - **Files:** `razorpay_checkout/razorpay_checkout/logging_config.py`.
  - **I verify by:** `uv run python -c "from razorpay_checkout.logging_config import configure_logging, get_logger; configure_logging(); log = get_logger('test'); log.info('hello', tenant_id='t1', order_id='o1')"` emits one line of valid JSON with required keys, parseable by `python -c "import json,sys; json.loads(sys.stdin.read())"`.

- [ ] **Create the correlation ID module.**
  - **Why:** Section 10 requires one `correlation_id` per logical request, propagated through HTTP, DB, SQS, Fulfillment Lambda, and `FulfillmentHook.metadata`. A `contextvars.ContextVar` is the only safe way to thread it through async code.
  - **How:** Define `correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")`. Export `new_correlation_id() -> str` returning `str(uuid.uuid4())`, `set_correlation_id(cid: str) -> None`, `get_correlation_id() -> str`. Wire `structlog.contextvars.bind_contextvars(correlation_id=cid)` inside `set_correlation_id`. Add a FastAPI dependency `correlation_id_dependency(request: Request) -> str` that calls `new_correlation_id()`, sets it, and binds it.
  - **Files:** `razorpay_checkout/razorpay_checkout/correlation.py`.
  - **I verify by:** A unit test in `tests/test_correlation.py` asserts `get_correlation_id() == ""` by default, that `set_correlation_id("abc")` makes `get_correlation_id() == "abc"`, and that a log emitted after `set_correlation_id` includes `correlation_id=abc` in JSON output (captured via `capsys`).

- [ ] **Phase gate verification and commit.**
  - **Verify:** `uv run pytest -q tests/test_correlation.py`. `docker compose up -d` healthy. `tree -L 3 -I '__pycache__|node_modules|.venv'` matches Section 1.
  - **Commit:** `git add -A && git commit -m "scaffold: repo layout, docker compose, structured logging, correlation context"`

---

## Phase: Database Schema and Migrations

> Depends on: previous phase.

- [ ] **Initialize Alembic inside `razorpay_checkout/`.**
  - **Why:** DESIGN Section 3 forbids `metadata.create_all()`. Alembic is the only schema source of truth.
  - **How:** `uv run alembic init razorpay_checkout/migrations`. In `alembic.ini` set `sqlalchemy.url = ${DATABASE_URL}`. In `migrations/env.py` read `DATABASE_URL` from environment via `os.environ`. Use the async branch (`AsyncEngine`) since the app is asyncpg. Replace the autogenerate stub: target metadata is `None` initially since schema is hand-authored per the design.
  - **Files:** `razorpay_checkout/alembic.ini`, `razorpay_checkout/razorpay_checkout/migrations/env.py`, `razorpay_checkout/razorpay_checkout/migrations/script.py.mako`.
  - **I verify by:** `uv run alembic current` prints empty (no revisions) without error.

- [ ] **Migration 0001: `tenants` table and `set_updated_at` trigger function.**
  - **Why:** Multi-tenant FK target. The trigger function is shared across tables that have `updated_at`.
  - **How:** `uv run alembic revision -m "tenants and updated_at trigger function"`. Body: `CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());` and `CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$ LANGUAGE plpgsql;`. Downgrade drops both.
  - **Files:** `razorpay_checkout/razorpay_checkout/migrations/versions/0001_tenants.py`.
  - **I verify by:** `uv run alembic upgrade head` succeeds. `psql ... -c "\d tenants"` shows columns. `psql ... -c "SELECT proname FROM pg_proc WHERE proname='set_updated_at'"` returns one row.

- [ ] **Migration 0002: `orders` table with all columns, CHECK constraints, and `BEFORE UPDATE` trigger.**
  - **Why:** Core entity. The `BEFORE UPDATE` trigger guarantees `updated_at` integrity (DESIGN Section 3 forbids application-managed `updated_at`).
  - **How:** New revision. SQL exactly: `CREATE TABLE orders (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id TEXT NOT NULL, receipt TEXT NOT NULL, razorpay_order_id TEXT UNIQUE, amount INTEGER NOT NULL CHECK (amount > 0), currency TEXT NOT NULL DEFAULT 'INR' CHECK (char_length(currency) <= 3), status TEXT NOT NULL DEFAULT 'initializing', notes JSONB NOT NULL DEFAULT '{}', correlation_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), attempted_at TIMESTAMPTZ, authorized_at TIMESTAMPTZ, paid_at TIMESTAMPTZ, fulfilled_at TIMESTAMPTZ, fulfillment_failed_at TIMESTAMPTZ, failed_at TIMESTAMPTZ, refunded_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());`. Then `CREATE EXTENSION IF NOT EXISTS pgcrypto;` (above the CREATE TABLE since `gen_random_uuid` needs it). Then `CREATE TRIGGER orders_updated_at BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION set_updated_at();`.
  - **Files:** `razorpay_checkout/razorpay_checkout/migrations/versions/0002_orders.py`.
  - **I verify by:** `uv run alembic upgrade head`. `psql ... -c "INSERT INTO orders (tenant_id, receipt, amount, correlation_id) VALUES ('t1', 'r1', 100, 'c1') RETURNING id, status, updated_at"` returns `status=initializing`. Then `UPDATE orders SET amount=200 WHERE receipt='r1'` and `SELECT updated_at > created_at FROM orders WHERE receipt='r1'` returns `t`.

- [ ] **Migration 0003: `orders` indexes.**
  - **Why:** DESIGN Section 3 lists six required indexes. Without them, reconciliation scans, webhook lookups, and tenant-scoped admin queries do full table scans at any nontrivial volume.
  - **How:** New revision. Statements: `CREATE UNIQUE INDEX orders_tenant_receipt_unique ON orders(tenant_id, receipt);` (the `UNIQUE(razorpay_order_id)` and PK are implicit from the table definition), `CREATE INDEX orders_tenant_status_idx ON orders(tenant_id, status);`, `CREATE INDEX orders_created_at_idx ON orders(created_at);`, `CREATE INDEX orders_correlation_id_idx ON orders(correlation_id);`.
  - **Files:** `razorpay_checkout/razorpay_checkout/migrations/versions/0003_orders_indexes.py`.
  - **I verify by:** `psql ... -c "\d orders"` lists all four named indexes plus `orders_pkey` and `orders_razorpay_order_id_key`.

- [ ] **Migration 0004: `payments` table with indexes.**
  - **Why:** Payment rows are written only on terminal payment outcomes (`captured`, `failed`). `is_authoritative` distinguishes capture-sourced rows from failed rows for reconciliation.
  - **How:** SQL exactly: `CREATE TABLE payments (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id TEXT NOT NULL, order_id UUID NOT NULL REFERENCES orders(id), razorpay_payment_id TEXT NOT NULL UNIQUE, razorpay_order_id TEXT NOT NULL, razorpay_signature TEXT, amount INTEGER NOT NULL, currency TEXT NOT NULL, method TEXT, status TEXT NOT NULL, attempt_number INTEGER NOT NULL DEFAULT 1, is_authoritative BOOLEAN NOT NULL DEFAULT FALSE, captured_at TIMESTAMPTZ, refunded_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());`. Indexes: `CREATE INDEX payments_order_id_idx ON payments(order_id);`, `CREATE INDEX payments_razorpay_order_id_idx ON payments(razorpay_order_id);`, `CREATE INDEX payments_tenant_idx ON payments(tenant_id);`.
  - **Files:** `razorpay_checkout/razorpay_checkout/migrations/versions/0004_payments.py`.
  - **I verify by:** `psql ... -c "\d payments"` lists all required indexes and the UNIQUE on `razorpay_payment_id`.

- [ ] **Migration 0005: `refunds` table with indexes.**
  - **Why:** One row per Razorpay refund event. The `amount` column supports v2 partial refunds without a migration. `initiated_by` records trigger source per the design.
  - **How:** `CREATE TABLE refunds (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id TEXT NOT NULL, order_id UUID NOT NULL REFERENCES orders(id), payment_id UUID NOT NULL REFERENCES payments(id), razorpay_refund_id TEXT NOT NULL UNIQUE, razorpay_payment_id TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, initiated_by TEXT NOT NULL, notes JSONB NOT NULL DEFAULT '{}', processed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());`. Indexes: `CREATE INDEX refunds_order_id_idx ON refunds(order_id);`, `CREATE INDEX refunds_tenant_idx ON refunds(tenant_id);`.
  - **Files:** `razorpay_checkout/razorpay_checkout/migrations/versions/0005_refunds.py`.
  - **I verify by:** `psql ... -c "\d refunds"` lists the unique on `razorpay_refund_id` plus both secondary indexes.

- [ ] **Migration 0006: `webhook_events` table with composite unique and indexes.**
  - **Why:** `UNIQUE(tenant_id, event_id)` is the fast-path idempotency guard (DESIGN Section 2.1 step 4). `(tenant_id, processing_status)` index serves the processor poll.
  - **How:** `CREATE TABLE webhook_events (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, event TEXT NOT NULL, payload JSONB NOT NULL, correlation_id TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), processed_at TIMESTAMPTZ, processing_status TEXT NOT NULL DEFAULT 'pending', error TEXT, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5, UNIQUE(tenant_id, event_id));`. Indexes: `CREATE INDEX webhook_events_tenant_status_idx ON webhook_events(tenant_id, processing_status);`, `CREATE INDEX webhook_events_received_at_idx ON webhook_events(received_at);`, `CREATE INDEX webhook_events_correlation_id_idx ON webhook_events(correlation_id);`.
  - **Files:** `razorpay_checkout/razorpay_checkout/migrations/versions/0006_webhook_events.py`.
  - **I verify by:** `INSERT INTO webhook_events (tenant_id, event_id, event, payload, correlation_id) VALUES ('t1','e1','payment.captured','{}','c1')` succeeds; a second identical insert fails with `unique_violation`.

- [ ] **Migration 0007: `order_events` table with indexes.**
  - **Why:** Append-only history for every state change. Required by Section 5 sequence diagrams (every transition writes one row).
  - **How:** `CREATE TABLE order_events (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), order_id UUID NOT NULL REFERENCES orders(id), tenant_id TEXT NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL, trigger TEXT NOT NULL, webhook_event_id UUID REFERENCES webhook_events(id), correlation_id TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());`. Indexes: `CREATE INDEX order_events_order_id_idx ON order_events(order_id);`, `CREATE INDEX order_events_tenant_created_at_idx ON order_events(tenant_id, created_at);`, `CREATE INDEX order_events_correlation_id_idx ON order_events(correlation_id);`.
  - **Files:** `razorpay_checkout/razorpay_checkout/migrations/versions/0007_order_events.py`.
  - **I verify by:** All migrations re-applied from scratch on a fresh DB: `psql -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'` then `uv run alembic upgrade head` completes; `\dt` lists `tenants, orders, payments, refunds, webhook_events, order_events`.

- [ ] **Phase gate verification and commit.**
  - **Verify:** Fresh DB upgrade and downgrade round-trip: `alembic upgrade head && alembic downgrade base && alembic upgrade head` succeeds. `pg_dump --schema-only` matches the design exactly (eyeball indexes, columns, defaults).
  - **Commit:** `git add -A && git commit -m "db: alembic migrations for tenants, orders, payments, refunds, webhook_events, order_events"`

---

## Phase: Config, Tenant Registry, and DB Pool

> Depends on: previous phase.

- [ ] **Author `RazorpayConfig` with Pydantic v2 and `SecretStr`.**
  - **Why:** DESIGN Section 2.1. `SecretStr` prevents accidental leakage in logs and reprs. `BaseSettings` makes env-driven config testable.
  - **How:** Subclass `BaseSettings` with model_config `SettingsConfigDict(env_prefix="RAZORPAY_", env_file=".env", extra="ignore")`. Fields exactly as in the design: `tenant_id: str`, `key_id: str`, `key_secret: SecretStr`, `webhook_secret: SecretStr`, `webhook_secret_previous: SecretStr | None = None`, `mode: Literal["test","live"] = "test"`, `db_dsn: PostgresDsn`, `pool_min_size: int = 2`, `pool_max_size: int = 10`, `razorpay_api_timeout: float = 10.0`, `otel_exporter: Literal["stdout","xray","otlp"] = "stdout"`, `otel_endpoint: str | None = None`, `admin_api_key: SecretStr | None = None`. In `__init__`, push `key_secret.get_secret_value()` and `webhook_secret.get_secret_value()` into the logging module's `SECRET_VALUES` set.
  - **Files:** `razorpay_checkout/razorpay_checkout/config.py`.
  - **I verify by:** `RAZORPAY_TENANT_ID=t1 RAZORPAY_KEY_ID=k RAZORPAY_KEY_SECRET=s RAZORPAY_WEBHOOK_SECRET=w RAZORPAY_DB_DSN=postgresql://x uv run python -c "from razorpay_checkout.config import RazorpayConfig; c = RazorpayConfig(); print(repr(c))"` does not print `s` or `w` in the output (they appear as `SecretStr('**********')`).

- [ ] **Implement `TenantConfigRegistry`.**
  - **Why:** DESIGN Section 2.1. The webhook router resolves `RazorpayConfig` from this registry using the URL path parameter, before signature verification. Lookup must be O(1) and thread-safe.
  - **How:** Class with `_configs: dict[str, RazorpayConfig] = {}` and `_lock = threading.Lock()`. Methods `register(self, config: RazorpayConfig) -> None` (locks, stores by `config.tenant_id`), `get(self, tenant_id: str) -> RazorpayConfig | None` (read; no lock needed for dict.get in CPython). Constructor accepts optional list of configs to pre-register.
  - **Files:** `razorpay_checkout/razorpay_checkout/tenants.py`.
  - **I verify by:** `tests/test_tenants.py` asserts: register+get round-trips; `get("unknown")` returns `None`; concurrent register from 50 threads results in 50 stored entries (use `concurrent.futures.ThreadPoolExecutor`).

- [ ] **Implement `signatures.verify_webhook_signature` and `verify_payment_signature`.**
  - **Why:** DESIGN Section 2.1 step 3 mandates `hmac.compare_digest`, current-then-previous fallback, and HMAC-SHA256 over raw body. Section 14 lists signature tests as a unit test gate.
  - **How:** `verify_webhook_signature(raw_body: bytes, header_signature: str, webhook_secret: str, webhook_secret_previous: str | None = None) -> bool`: compute `hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()`, compare to `header_signature` via `hmac.compare_digest`. If False and previous is set, compute with previous and compare. Return `False` on both failures. `verify_payment_signature(order_id: str, payment_id: str, signature: str, key_secret: str) -> bool`: payload string is `f"{order_id}|{payment_id}"`, same HMAC-SHA256 hex, `compare_digest`.
  - **Files:** `razorpay_checkout/razorpay_checkout/signatures.py`, `razorpay_checkout/tests/test_signatures.py`.
  - **I verify by:** Tests pass for: valid signature (true), tampered body (false), wrong secret (false), empty body (false), previous secret fallback (true), valid payment signature (true), mismatched payment IDs (false), wrong key secret (false). Test vectors are the canonical fixtures shared with `scripts/razorpay-simulator.py` (created in a later phase).

- [ ] **Add asyncpg pool factory with explicit sizing.**
  - **Why:** DESIGN convention: `asyncpg` with explicit `pool_min_size` and `pool_max_size`. Lambda concurrency depends on bounded pool acquire.
  - **How:** Module exports `async def create_pool(config: RazorpayConfig) -> asyncpg.Pool`: `await asyncpg.create_pool(dsn=str(config.db_dsn), min_size=config.pool_min_size, max_size=config.pool_max_size, command_timeout=config.razorpay_api_timeout)`. Wrap acquire in a function that logs pool acquire duration above 200 ms at WARN with `correlation_id` in the log record.
  - **Files:** `razorpay_checkout/razorpay_checkout/db.py`.
  - **I verify by:** `tests/test_db.py` against the local Postgres: pool creates, `async with pool.acquire() as conn: await conn.fetchval("SELECT 1")` returns 1. Assert `pool._minsize == 2` and `pool._maxsize == 10` (asyncpg internals; acceptable in a single test).

- [ ] **Phase gate verification and commit.**
  - **Verify:** `uv run pytest tests/test_tenants.py tests/test_signatures.py tests/test_db.py -q` all pass. `uv run ruff check .` clean.
  - **Commit:** `git add -A && git commit -m "core: RazorpayConfig, TenantConfigRegistry, signature utilities, asyncpg pool"`

---

## Phase: Order State Machine

> Depends on: previous phase.

- [ ] **Define `OrderStatus` enum and the transition table.**
  - **Why:** DESIGN Section 4 enumerates 14 allowed transitions plus the `payment.failed` stale-event special case. A declarative table is the only safe representation.
  - **How:** `OrderStatus(str, Enum)` with values exactly: `initializing, created, attempted, authorized, paid, fulfilled, fulfillment_failed, failed, refunded`. Module-level `ALLOWED_TRANSITIONS: set[tuple[OrderStatus, OrderStatus]]` containing the 14 edges in DESIGN Section 4. `TERMINAL = {OrderStatus.failed, OrderStatus.refunded}`. `NON_TERMINAL = set(OrderStatus) - TERMINAL`.
  - **Files:** `razorpay_checkout/razorpay_checkout/state_machine.py`.
  - **I verify by:** `tests/test_state_machine.py` asserts `(initializing, created) in ALLOWED_TRANSITIONS`, `(initializing, paid) not in ALLOWED_TRANSITIONS`, and that `len(ALLOWED_TRANSITIONS) == 14`.

- [ ] **Implement `IllegalTransitionError` and `assert_transition`.**
  - **Why:** DESIGN Section 4: any edge not in the table raises. The check must run before any DB write to avoid corrupt history.
  - **How:** `IllegalTransitionError(Exception)` with `__init__(self, from_status, to_status, order_id)` storing fields for structured logging. `assert_transition(from_status: OrderStatus, to_status: OrderStatus, order_id: str) -> None` raises if `(from_status, to_status) not in ALLOWED_TRANSITIONS`.
  - **Files:** Same file.
  - **I verify by:** `tests/test_state_machine.py` parametrized: every illegal pair raises `IllegalTransitionError`; every legal pair returns `None`.

- [ ] **Implement the `payment.failed` stale-event special case as `is_stale_failed_event`.**
  - **Why:** DESIGN Section 4 + Section 6: a `payment.failed` arriving when the order is in `paid`, `fulfilled`, or `fulfillment_failed` is a stale event, treated as a no-op and the event is marked `done`. This is not a transition; it is a valid no-op.
  - **How:** `is_stale_failed_event(current_status: OrderStatus) -> bool` returns `current_status in {OrderStatus.paid, OrderStatus.fulfilled, OrderStatus.fulfillment_failed}`.
  - **Files:** Same file.
  - **I verify by:** `tests/test_state_machine.py::test_stale_failed_no_op` returns True for the three statuses and False otherwise.

- [ ] **Phase gate verification and commit.**
  - **Verify:** `uv run pytest tests/test_state_machine.py -q` reports the full parametrized matrix.
  - **Commit:** `git add -A && git commit -m "state-machine: OrderStatus enum, transition table, IllegalTransitionError, stale-failed helper"`

---

## Phase: OrderService and Orders Routes

> Depends on: previous phase.

- [ ] **Create `OrderService.create_or_get` with the `initializing -> created` sequence.**
  - **Why:** DESIGN Section 2.1: INSERT local row with `status=initializing` before calling Razorpay. Razorpay never ahead of DB. `UNIQUE(tenant_id, receipt)` collapses concurrent inserts.
  - **How:** `class OrderService` constructed with `pool: asyncpg.Pool`, `config: RazorpayConfig`, `razorpay_client: RazorpayClient`. Method `async def create_or_get(self, receipt: str, currency: str, amount: int, notes: dict, correlation_id: str) -> OrderRecord`. Algorithm: `BEGIN; SELECT * FROM orders WHERE tenant_id=$1 AND receipt=$2 AND status IN (non_terminal_list) FOR UPDATE`. If found and `razorpay_order_id IS NOT NULL`, return it. If found and `razorpay_order_id IS NULL` (prior crashed init), proceed to Razorpay call. If not found, `INSERT INTO orders (tenant_id, receipt, amount, currency, notes, correlation_id) VALUES (..., 'initializing') RETURNING id`. Commit the insert in its own transaction so a parallel request blocks on the UNIQUE. Then call `razorpay_client.create_order(...)`. On success `UPDATE orders SET razorpay_order_id=$1, status='created' WHERE id=$2`. On Razorpay timeout, leave row in `initializing` and raise `RazorpayUnavailableError`.
  - **Files:** `razorpay_checkout/razorpay_checkout/orders.py`, `razorpay_checkout/razorpay_checkout/client.py` (`RazorpayClient.create_order` using `httpx.AsyncClient` with `BasicAuth(key_id, key_secret.get_secret_value())` and `razorpay_api_timeout`).
  - **I verify by:** `tests/test_order_service.py`: (1) first call inserts `initializing` then upgrades to `created` with `razorpay_order_id`; (2) `respx` simulates Razorpay timeout, asserts row stays at `initializing`, raises `RazorpayUnavailableError`; (3) ten concurrent calls with the same `receipt` result in exactly one Razorpay POST (`respx` call count == 1) and one DB row.

- [ ] **Implement `OrderService.transition`.**
  - **Why:** Central choke point so tenant isolation and `IllegalTransitionError` cannot be bypassed by callers. DESIGN Critical Invariant 1 (Tenant isolation) and Section 4.
  - **How:** `async def transition(self, conn: asyncpg.Connection, order_id: UUID, new_status: OrderStatus, trigger: str, webhook_event_id: UUID | None, metadata: dict) -> None`. Caller must already hold `SELECT ... FOR UPDATE`. Reads `from_status` via `SELECT status, tenant_id FROM orders WHERE id=$1`. Tenant scoping check: passed `tenant_id` (via metadata or arg) must equal row's tenant. Calls `assert_transition`. Builds `UPDATE orders SET status=$new_status, {status}_at=NOW() WHERE id=$order_id AND tenant_id=$tenant_id`. Inserts into `order_events` with `(order_id, tenant_id, from_status, to_status, trigger, webhook_event_id, correlation_id, metadata)`.
  - **Files:** `razorpay_checkout/razorpay_checkout/orders.py`.
  - **I verify by:** Integration test against local Postgres: legal transition writes both the orders UPDATE and one `order_events` row with matching `from_status`, `to_status`, `correlation_id`. Illegal transition raises `IllegalTransitionError` and leaves the DB unchanged (assert row count in `order_events` did not increase, status did not change).

- [ ] **Wire up the FastAPI router and `payments_router` symbol.**
  - **Why:** Public API surface per DESIGN Section 2.1.
  - **How:** `razorpay_checkout/router.py` exports `payments_router = APIRouter()`. `razorpay_checkout/__init__.py` re-exports `payments_router`, `RazorpayConfig`, `TenantConfigRegistry`, `FulfillmentHook`. Add a startup dependency that constructs `OrderService` and stores it in `app.state` or via FastAPI's `Depends`.
  - **Files:** `razorpay_checkout/razorpay_checkout/router.py`, `razorpay_checkout/razorpay_checkout/__init__.py`.
  - **I verify by:** `uv run python -c "from razorpay_checkout import payments_router; print([r.path for r in payments_router.routes])"` runs without error.

- [ ] **Implement `POST /payments/orders`.**
  - **Why:** DESIGN Section 2.1 Order creation sequence. Server is authoritative for `amount`; the widget never sends amount.
  - **How:** Add route handler accepting `CreateOrderRequest(currency: str = "INR", receipt: str, notes: dict[str, str] = {})`. Dependency on `correlation_id_dependency` and a tenant-resolved `RazorpayConfig`. Server computes `amount` from a pluggable `AmountResolver` Protocol (default for v1: a fixed price from `RazorpayConfig` or from `notes["product_id"]` lookup; the implementation can be a stub raising `NotImplementedError` for the host app to override at integration time). Calls `OrderService.create_or_get`. Returns `CreateOrderResponse(order_id, razorpay_order_id, amount, currency, key_id)`. On `RazorpayUnavailableError`, return 503.
  - **Files:** `razorpay_checkout/razorpay_checkout/router.py`.
  - **I verify by:** `uv run pytest tests/test_orders_route.py -q` using `httpx.AsyncClient` against the FastAPI app: a POST with a fresh receipt returns 201 with non-null `razorpay_order_id`. A second POST with the same receipt returns the same `order_id`. Razorpay timeout simulation returns 503.

- [ ] **Implement `GET /payments/orders/{order_id}`.**
  - **Why:** DESIGN Section 2.1. Frontend polls this to detect `fulfilled`.
  - **How:** Handler reads `order_id` path param, resolves tenant from `RazorpayConfig` dependency, runs `SELECT ... FROM orders WHERE id=$1 AND tenant_id=$2`. Maps row to `OrderStatusResponse`. Returns 404 if not found.
  - **Files:** Same router.
  - **I verify by:** `tests/test_orders_route.py::test_get_order_status` asserts the response shape and 404 on unknown id. Also asserts cross-tenant query returns 404 (insert order under tenant A, GET with tenant B context, expect 404).

- [ ] **Implement `POST /payments/verify-callback`.**
  - **Why:** DESIGN Section 2.1 + Section 5.7. Confirms order exists and is not in a terminal failed state. Does not unlock access.
  - **How:** Handler accepts `VerifyCallbackRequest(razorpay_order_id, razorpay_payment_id, razorpay_signature)`. Calls `signatures.verify_payment_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature, key_secret)`. On signature failure return `VerifyCallbackResponse(verified=False, order_id="", reason="signature_mismatch")` with HTTP 400. On signature success, `SELECT id, status FROM orders WHERE razorpay_order_id=$1 AND tenant_id=$2`. If `status='failed'`, return `verified=False, reason="terminal_failed"`. Otherwise return `verified=True, order_id, reason=None` with HTTP 200.
  - **Files:** Same router.
  - **I verify by:** Three test cases pass: valid signature on `paid` order returns `verified=True`; tampered signature returns 400 and `signature_mismatch`; signature valid but order in `failed` returns `verified=False, terminal_failed`.

- [ ] **Phase gate verification and commit.**
  - **Verify:** `uv run pytest tests/test_orders_route.py tests/test_order_service.py -q`. `curl -X POST http://localhost:8000/payments/orders -H 'content-type: application/json' -d '{"receipt":"manual-test-1"}'` returns a `razorpay_order_id`. Logs include `correlation_id`.
  - **Commit:** `git add -A && git commit -m "orders: OrderService, /payments/orders, /payments/orders/{id}, /payments/verify-callback"`

---

## Phase: Webhook Pipeline

> Depends on: previous phase.

- [ ] **Add LocalStack SQS publisher with FIFO semantics.**
  - **Why:** DESIGN Section 7. `fulfillment-queue.fifo` with `MessageGroupId=order_id`, `correlation_id` in `MessageAttributes`. DB commit must precede SQS publish.
  - **How:** Module exports `async def publish_fulfillment_message(client, queue_url, order_id, tenant_id, correlation_id, razorpay_payment_id) -> str`. Use `aioboto3` against `endpoint_url=os.environ["AWS_ENDPOINT_URL"]`. Body JSON `{"order_id": ..., "tenant_id": ..., "razorpay_payment_id": ...}`. `MessageGroupId=order_id`. `MessageDeduplicationId=f"{order_id}:{razorpay_payment_id}"`. `MessageAttributes={"correlation_id": {"StringValue": correlation_id, "DataType": "String"}}`. Returns SQS message id. Add a startup helper `ensure_fulfillment_queue()` that creates `fulfillment-queue.fifo` and `fulfillment-dlq.fifo` on LocalStack if missing.
  - **Files:** `razorpay_checkout/razorpay_checkout/queue.py`.
  - **I verify by:** `aws --endpoint-url http://localhost:4566 sqs list-queues` shows both FIFO queues. Test publishes a message and `aws ... sqs receive-message` returns it with the correlation_id attribute.

- [ ] **Implement `WebhookProcessor` skeleton with the fast-path dedup and DB transaction shell.**
  - **Why:** DESIGN Section 2.1 webhook pipeline + `.claude/rules/webhook-idempotency.md`. The order of operations is non-negotiable.
  - **How:** Class with method `async def process(self, raw_body: bytes, header_signature: str, tenant_id: str) -> WebhookAckResponse`. Steps: (1) Resolve config from `TenantConfigRegistry`; raise `UnknownTenantError` if `None`. (2) Verify signature via `verify_webhook_signature` (current + previous); raise `SignatureMismatchError` on fail. (3) Parse payload to `RazorpayWebhookPayload`. (4) Fast-path `INSERT INTO webhook_events (tenant_id, event_id, event, payload, correlation_id) VALUES (...) ON CONFLICT (tenant_id, event_id) DO NOTHING RETURNING id`; if no row returned, increment `webhook_duplicate_count` metric and return `WebhookAckResponse(status="queued")`. (5) Begin transaction. (6) Acquire `SELECT * FROM orders WHERE razorpay_order_id=$1 AND tenant_id=$2 FOR UPDATE`. (7) Dispatch to event-type handler. (8) Commit. (9) Optional SQS publish for `paid` transition. (10) Return `queued`.
  - **Files:** `razorpay_checkout/razorpay_checkout/webhooks.py`.
  - **I verify by:** Tests pass for unknown tenant raising `UnknownTenantError`, signature mismatch raising `SignatureMismatchError`, duplicate event_id resulting in `webhook_duplicate_count += 1` and no transition.

- [ ] **Implement the event-type-aware primary idempotency guard.**
  - **Why:** DESIGN Section 2.1 step 6 + `.claude/rules/webhook-idempotency.md`. Razorpay may retry with a new `event_id` for the same payment; relying on `event_id` uniqueness alone is forbidden.
  - **How:** `def should_skip(event: str, order: OrderRecord, razorpay_payment_id: str | None, captured_payment_exists: bool) -> SkipDecision`. Rules: `payment.captured` -> skip if `order.status in {paid, fulfilled, fulfillment_failed, refunded}` AND `captured_payment_exists`. `payment.authorized` -> skip if `order.status in {authorized, paid, fulfilled, fulfillment_failed, refunded}`. `payment.failed` -> skip if `order.status in {failed, refunded}`. Stale-no-op if `order.status in {paid, fulfilled, fulfillment_failed}`. `refund.processed` -> skip if `order.status == refunded`. Return enum-like result: `PROCEED | SKIP_ALREADY_APPLIED | STALE_NO_OP`. Caller marks `webhook_events.processing_status='done'` on skip variants, commits, returns 200.
  - **Files:** Same module.
  - **I verify by:** `tests/test_webhook_idempotency.py` enumerates a parametrized matrix of `(event_type, order_status, has_captured_payment) -> expected_decision` covering every cell from the rules above. All pass.

- [ ] **Implement `handle_payment_authorized`.**
  - **Why:** DESIGN Section 5.3: `attempted -> authorized`, no `payments` row, no SQS publish, amount assertion.
  - **How:** After `SELECT FOR UPDATE` and primary guard: assert `payload.payment.entity.amount == order.amount`; on mismatch set `webhook_events.processing_status='failed'`, increment `reconciliation_mismatch_count{reason="amount_mismatch"}`, commit, return 200. Else `OrderService.transition(conn, order.id, OrderStatus.authorized, trigger="webhook", webhook_event_id=event.id, metadata={"correlation_id": cid, "razorpay_payment_id": pid})`. Mark `webhook_events.processing_status='done'`. Emit `payment_authorized_count` metric. Commit. Return 200.
  - **Files:** Same module.
  - **I verify by:** Simulator POSTs a signed `payment.authorized` for an `attempted` order; the order moves to `authorized`, `order_events` has one row, no row in `payments`, no SQS message in `fulfillment-queue.fifo`.

- [ ] **Implement `handle_payment_captured`.**
  - **Why:** DESIGN Section 2.1 + Section 5.2. INSERT payments row with `is_authoritative=TRUE`. Two-step transition through `attempted` if order was in `failed`. SQS publish after commit only.
  - **How:** After guard and amount assertion: determine path. If `order.status == 'failed'`, first `OrderService.transition(... attempted ...)` then `... paid ...` in the same transaction (two `order_events` rows). Else single transition to `paid`. INSERT into `payments` with `is_authoritative=TRUE`, `status='captured'`, `captured_at=NOW()`, `attempt_number = (SELECT COALESCE(MAX(attempt_number),0)+1 FROM payments WHERE order_id=$1)`. Mark `webhook_events.processing_status='done'`. **Commit transaction.** Then `publish_fulfillment_message(...)`. On SQS publish exception, do not raise to caller (DB is already authoritative); log ERROR, increment `fulfillment_sqs_publish_failures`. Order stays `paid`, dual-write monitor catches it. Return 200.
  - **Files:** Same module.
  - **I verify by:** Tests cover (a) `attempted -> paid` single edge writes one `order_events` and one `payments` row, SQS receives one message; (b) `failed -> attempted -> paid` writes two `order_events` rows and one `payments` row with `attempt_number=2`; (c) commit before SQS verified by patching SQS to raise after commit and asserting order is still `paid` in DB.

- [ ] **Implement `handle_payment_failed`.**
  - **Why:** DESIGN Section 2.1 + state machine: insert payments row with `is_authoritative=FALSE`. Stale event on `paid`/`fulfilled`/`fulfillment_failed` is a no-op.
  - **How:** If `is_stale_failed_event(order.status)`, set `webhook_events.processing_status='done'` with note `stale_failed_event`, commit, return 200. Else `OrderService.transition(... failed ...)`. INSERT payments row with `is_authoritative=FALSE`, `status='failed'`. Mark `done`. Commit. No SQS publish.
  - **Files:** Same module.
  - **I verify by:** Tests pass for: `attempted -> failed` inserts payments row with `is_authoritative=FALSE`; `paid -> failed` is a no-op, `order_events` count unchanged, `webhook_events.processing_status='done'`.

- [ ] **Implement `handle_refund_processed`.**
  - **Why:** DESIGN Section 5.6 + `.claude/rules/webhook-idempotency.md`. UPDATE the existing captured `payments` row (do not INSERT a new one). INSERT into `refunds`. Transition order to `refunded`.
  - **How:** After guard: `SELECT id FROM payments WHERE razorpay_payment_id=$1 AND tenant_id=$2 AND is_authoritative=TRUE`. If null, set `webhook_events.processing_status='failed'`, alert (log ERROR), return 200. Else `UPDATE payments SET status='refunded', refunded_at=NOW() WHERE id=$1`. `INSERT INTO refunds (tenant_id, order_id, payment_id, razorpay_refund_id, razorpay_payment_id, amount, currency, status, initiated_by, notes, processed_at)` with `initiated_by='razorpay_dashboard'`. `OrderService.transition(... refunded ...)`. Mark `done`. Commit. No SQS publish.
  - **Files:** Same module.
  - **I verify by:** Tests cover (a) `paid -> refunded`, `fulfilled -> refunded`, `fulfillment_failed -> refunded` each work; (b) `refund.processed` for unknown payment results in `processing_status='failed'` and a single ERROR log line; (c) duplicate `razorpay_refund_id` raises `UniqueViolationError` caught and treated as already-processed (guard returns 200 queued).

- [ ] **Implement `handle_order_paid` as log-only.**
  - **Why:** DESIGN Section 2.1: `order.paid` is diagnostic only.
  - **How:** Log INFO with `event='order.paid'`, `order_id`, `correlation_id`. Mark `webhook_events.processing_status='done'`. Commit. Return 200.
  - **Files:** Same module.
  - **I verify by:** Test asserts no state change on the order and a single INFO log record with `event=order.paid`.

- [ ] **Add `POST /payments/webhook/{tenant_id}` route.**
  - **Why:** DESIGN Section 2.1 + `.claude/rules/tenant-isolation.md`: tenant_id is from the URL path; never from body or header. Unknown tenant returns 400, not 404.
  - **How:** Handler reads `tenant_id` path param, raw body via `request.body()`, signature header via `request.headers["X-Razorpay-Signature"]`. Creates `correlation_id` and binds to log context. Resolves `RazorpayConfig` from `TenantConfigRegistry`. On unknown tenant, return `JSONResponse(status_code=400, content={"detail": "unknown tenant"})`. Else delegate to `WebhookProcessor.process(...)`. On `SignatureMismatchError` return 400. On amount mismatch the processor returns 200 internally (handler does not see exception). On `AmountMismatchError` raised early (shouldn't be), still return 200. On unexpected exception, log with `correlation_id` and re-raise so FastAPI returns 500 and Razorpay retries.
  - **Files:** `razorpay_checkout/razorpay_checkout/router.py`.
  - **I verify by:** Run `python scripts/razorpay-simulator.py payment.captured --tenant t1 --order-id rzp_test_xxx --payment-id pay_test_yyy` (created next phase) against the running app; verify 200 response, order moves to `paid`, SQS receives one message, logs include matching `correlation_id` across HTTP entry, DB writes, and SQS publish.

- [ ] **Phase gate verification and commit.**
  - **Verify:** `uv run pytest tests/test_webhook* tests/test_webhook_idempotency.py -q`. Manually POST a duplicate event_id and observe `webhook_duplicate_count` increments. POST a refund event for a non-existent payment and observe `processing_status='failed'`. SQS publish failure (kill LocalStack temporarily and POST a `payment.captured`) leaves order `paid` with no SQS message; restore LocalStack.
  - **Commit:** `git add -A && git commit -m "webhooks: tenant lookup, idempotency guards, event handlers, SQS publish after commit"`

---

## Phase: Razorpay Webhook Simulator

> Depends on: webhook pipeline complete. Listed here because integration tests in the next phase require it.

- [ ] **Author `scripts/razorpay-simulator.py` with HMAC signing.**
  - **Why:** DESIGN Section 15. Deterministic, no network beyond localhost. Source of truth shared with `tests/test_signatures.py` and `.claude/commands/local-sig-validate.md`.
  - **How:** CLI: `python scripts/razorpay-simulator.py <event_type> --tenant <id> --order-id <rzp_order_id> --payment-id <rzp_payment_id> [--amount <paise>] [--refund-id <rzp_refund_id>] [--secret <webhook_secret>] [--url <target_url>]`. Build canonical Razorpay payload structure for each event type (event-type templates). Compute `signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()`. POST via `httpx` with `X-Razorpay-Signature: <sig>` header. Default URL `http://localhost:8000/payments/webhook/<tenant>`. Print response status and body to stdout in JSON.
  - **Files:** `scripts/razorpay-simulator.py`, `.claude/commands/local-sig-validate.md` (records the canonical HMAC vectors used by simulator and test).
  - **I verify by:** `python scripts/razorpay-simulator.py payment.captured --tenant t1 --order-id order_X --payment-id pay_Y --amount 50000 --secret <local_dev_secret>` returns 200 from a running app, and the corresponding test vector in `tests/test_signatures.py::test_canonical_vector` validates with the same secret.

- [ ] **Phase gate verification and commit.**
  - **Verify:** All four event types (`payment.authorized`, `payment.captured`, `payment.failed`, `refund.processed`) succeed end-to-end against the local app. Re-running with the same `--payment-id` produces a duplicate (event_id collides) and the `webhook_duplicate_count` metric increments.
  - **Commit:** `git add -A && git commit -m "tools: razorpay-simulator with canonical HMAC vectors"`

---

## Phase: Fulfillment Lambda and Safety Nets

> Depends on: webhook pipeline.

- [ ] **Define `FulfillmentHook` protocol and default no-op implementation.**
  - **Why:** DESIGN Section 11. Hook must be idempotent. Default is no-op for v1.
  - **How:** `class FulfillmentHook(Protocol)` with `async def fulfill(self, order_id: str, tenant_id: str, amount: int, metadata: dict) -> None`. `class DefaultFulfillmentHook` logs INFO and returns. `metadata` contract: `{"correlation_id": str, "razorpay_payment_id": str}`.
  - **Files:** `razorpay_checkout/razorpay_checkout/fulfillment.py`.
  - **I verify by:** `tests/test_fulfillment.py::test_default_hook_no_op` confirms it logs and returns None.

- [ ] **Implement Fulfillment Lambda handler with short-circuit and transition.**
  - **Why:** DESIGN Section 7: short-circuit if `order.status='fulfilled'` to handle interleaved SQS deliveries. DB write must precede no further side effects.
  - **How:** `async def fulfillment_handler(event: dict, context) -> dict`. Iterates SQS `Records`. For each record: parse body, extract `correlation_id` from `messageAttributes.correlation_id.stringValue`, bind to log context. Open pool, begin transaction, `SELECT ... FOR UPDATE` on order. If `order.status == 'fulfilled'`, mark associated `webhook_events.processing_status='done'`, commit, continue. Else if `order.status != 'paid'` and `order.status != 'fulfillment_failed'`, log WARN and continue (do not transition). Else call `hook.fulfill(order_id, tenant_id, amount, metadata)`. On success `OrderService.transition(... fulfilled ...)`, mark webhook event done, commit. On hook exception, re-raise (SQS retries). For Lambda packaging, file exposes `handler` as the entry point and uses asyncio.run inside if needed.
  - **Files:** Same module; `razorpay_checkout/razorpay_checkout/lambdas/fulfillment.py` for the Lambda entry point shim.
  - **I verify by:** `tests/test_fulfillment.py::test_short_circuit` pre-marks order as `fulfilled` and asserts hook is not called. `tests/test_fulfillment.py::test_paid_to_fulfilled` calls handler with a `paid` order, default hook runs, order transitions to `fulfilled`, `order_events` row written. `tests/test_fulfillment.py::test_hook_exception_reraises` configures a hook that raises and asserts the handler re-raises.

- [ ] **Implement DLQ poller that transitions `paid -> fulfillment_failed`.**
  - **Why:** DESIGN Section 7 + Section 11: after SQS exhaustion (`maxReceiveCount=5`), the order must move to `fulfillment_failed` and a CloudWatch alarm fire on `fulfillment-dlq.fifo` depth > 0.
  - **How:** `async def dlq_poller_handler(event: dict, context) -> dict`. Triggered by `fulfillment-dlq.fifo` event source. For each record, extract `order_id`, `tenant_id`, `correlation_id`. Run `OrderService.transition(... fulfillment_failed ..., trigger='fulfillment', metadata={"correlation_id": cid, "error": <last error from message>})`. The `metadata` JSON records the failure reason for Runbook 2.
  - **Files:** `razorpay_checkout/razorpay_checkout/lambdas/dlq_poller.py`.
  - **I verify by:** Test simulates an SQS record on the DLQ queue, asserts order transitions from `paid` to `fulfillment_failed`, `order_events.metadata.error` is populated.

- [ ] **Implement the stale-`paid` monitor Lambda.**
  - **Why:** DESIGN Section 7 dual-write safety net: orders in `paid` for > 5 minutes with no in-flight SQS message indicate SQS publish failure.
  - **How:** `async def stale_paid_monitor_handler(event, context)`. Query `SELECT id, tenant_id FROM orders WHERE status='paid' AND paid_at < NOW() - INTERVAL '5 minutes' LIMIT 100`. For each, emit `stale_paid_orders` metric, log WARN with order_id and correlation_id. Do not auto-retry; the operator uses `/admin/orders/{id}/retry-fulfillment`.
  - **Files:** `razorpay_checkout/razorpay_checkout/lambdas/stale_paid_monitor.py`.
  - **I verify by:** Insert an order with `status='paid', paid_at=NOW() - INTERVAL '10 minutes'`, run handler locally, observe one WARN log line and one metric emission.

- [ ] **Phase gate verification and commit.**
  - **Verify:** End-to-end local flow: simulator triggers `payment.captured`, webhook commits, SQS receives, LocalStack triggers Fulfillment Lambda (via `awslocal lambda create-function` and event-source-mapping), order transitions to `fulfilled`. Stale-paid monitor unit test green. DLQ poller test green.
  - **Commit:** `git add -A && git commit -m "fulfillment: hook protocol, Fulfillment Lambda, DLQ poller, stale-paid monitor"`

---

## Phase: Admin Routes

> Depends on: webhook and fulfillment phases.

- [ ] **Add admin authentication dependency.**
  - **Why:** DESIGN Section 2.1: admin routes require `X-Admin-Api-Key` matching `RazorpayConfig.admin_api_key`. If `admin_api_key is None`, return 403.
  - **How:** Dependency `async def require_admin(request: Request, config: RazorpayConfig = Depends(get_config)) -> None`. If `config.admin_api_key is None` raise `HTTPException(403)`. Else compare `request.headers.get("X-Admin-Api-Key")` to `config.admin_api_key.get_secret_value()` via `hmac.compare_digest`. On mismatch raise 403.
  - **Files:** `razorpay_checkout/razorpay_checkout/router.py` (or a new `admin.py` for the routes).
  - **I verify by:** Test cases: `admin_api_key=None` always returns 403; correct key returns 200 on a stub admin route; incorrect key returns 403 with no information leak.

- [ ] **Implement `GET /payments/reconcile`.**
  - **Why:** DESIGN Section 2.1 + Section 5.8. Pulls Razorpay payments for a date range, compares to local DB, returns mismatches.
  - **How:** Handler reads `from_date`, `to_date` query params. Iterates Razorpay `GET /v1/payments` with `skip`/`count` pagination until empty. Builds `set(razorpay_payment_id)`. Queries local `payments WHERE is_authoritative=TRUE AND created_at BETWEEN $1 AND $2`. Joins `refunds` for the same set. Builds `ReconcileMismatch` rows for each reason. Returns `ReconcileResponse`. Emits `reconciliation_mismatch_count{reason}` metric per category.
  - **Files:** `razorpay_checkout/razorpay_checkout/router.py`, `razorpay_checkout/razorpay_checkout/reconcile.py`.
  - **I verify by:** Integration test: insert two local payments, mock Razorpay returning three (one extra, one with different amount), assert response contains exactly two mismatches with the right reasons.

- [ ] **Implement `POST /payments/admin/events/{event_id}/replay`.**
  - **Why:** DESIGN Section 2.1: resets `failed` or `dead` webhook event to `pending`.
  - **How:** Handler with admin dependency. `UPDATE webhook_events SET processing_status='pending', attempts=0, error=NULL WHERE id=$1 AND tenant_id=$2 AND processing_status IN ('failed','dead') RETURNING processing_status`. Returns `ReplayEventResponse(event_id, previous_status, new_status='pending')`. 404 if not found, 409 if not in eligible state.
  - **Files:** Same router.
  - **I verify by:** Test pre-inserts a `failed` event, calls the route, asserts status flips to `pending`. Calling again on the now-pending event returns 409.

- [ ] **Implement `POST /payments/admin/orders/{order_id}/retry-fulfillment`.**
  - **Why:** DESIGN Section 2.1: recovers from SQS publish failures or fulfillment hook exhaustion. Re-enqueues an SQS message.
  - **How:** Admin handler. `SELECT status, tenant_id, razorpay_payment_id FROM orders LEFT JOIN payments ON orders.id=payments.order_id AND payments.is_authoritative=TRUE WHERE orders.id=$1 AND orders.tenant_id=$2 AND orders.status IN ('paid','fulfillment_failed')`. If found, call `publish_fulfillment_message(...)`. INSERT `order_events` with `trigger='admin_retry'`. Return `RetryFulfillmentResponse(order_id, sqs_message_id, previous_status)`.
  - **Files:** Same router.
  - **I verify by:** Test creates a `paid` order with no SQS message, calls the route, asserts the SQS queue receives one message and the response contains the SQS message id. Calling on a `fulfilled` order returns 409.

- [ ] **Phase gate verification and commit.**
  - **Verify:** All three admin route tests pass. Manually call each route with `curl -H 'X-Admin-Api-Key: <local_dev_key>'` and observe correct behavior including 403 on missing key.
  - **Commit:** `git add -A && git commit -m "admin: reconcile, replay event, retry fulfillment with X-Admin-Api-Key"`

---

## Phase: Observability (OpenTelemetry and Custom Metrics)

> Depends on: webhook and fulfillment phases.

- [ ] **Add `telemetry.py` with exporter selection.**
  - **Why:** DESIGN Section 10. Exporter chosen at startup from `RazorpayConfig.otel_exporter`. No application branching on the exporter elsewhere.
  - **How:** `def configure_otel(config: RazorpayConfig) -> trace.TracerProvider`. Switch on `config.otel_exporter`: `"stdout"` -> `ConsoleSpanExporter`; `"otlp"` -> `OTLPSpanExporter(endpoint=config.otel_endpoint)`; `"xray"` -> documented as Paid-Track-only via ADOT Lambda layer (no code path in v1; the function raises if selected without ADOT layer being detected). Set `Resource(attributes={"service.name": "razorpay-checkout", "tenant.id": config.tenant_id})`. Install `FastAPIInstrumentor` and `AsyncPGInstrumentor`. Every created span gets `correlation_id` from contextvar as an attribute via a `SpanProcessor` that reads `correlation_id_var` on span start.
  - **Files:** `razorpay_checkout/razorpay_checkout/telemetry.py`.
  - **I verify by:** With `OTEL_EXPORTER=stdout`, hitting `/payments/orders` prints OTel spans to stdout (JSON), each carrying `correlation_id` and `tenant.id`.

- [ ] **Emit all DESIGN Section 13 metrics.**
  - **Why:** DESIGN Section 13 enumerates ten metrics. Free tier custom metric quota is exactly ten; no consolidation needed at v1.
  - **How:** In `telemetry.py` create a `meter = metrics.get_meter("razorpay-checkout")` and ten instruments: `webhook_duplicate_count` (Counter), `order_state_transition_count` (Counter with `from`,`to` labels), `fulfillment_duration_ms` (Histogram), `fulfillment_sqs_publish_failures` (Counter), `reconciliation_mismatch_count` (Counter with `reason` label), `cold_start_ms` (Histogram, recorded on first Lambda invocation per container via global flag), `dead_event_count` (Counter), `webhook_latency_ms` (Histogram), `payment_authorized_count` (Counter), `tenant_lookup_failures` (Counter). Wire emissions from the existing handlers.
  - **Files:** `razorpay_checkout/razorpay_checkout/telemetry.py` (instruments), updates throughout webhook, fulfillment, reconcile, router files (emissions).
  - **I verify by:** Trigger each codepath in tests; assert the corresponding instrument was recorded via an in-memory `MetricReader` (`InMemoryMetricReader`) in `tests/test_telemetry.py`.

- [ ] **Assert secret values never appear in logs.**
  - **Why:** DESIGN Section 10. Critical security invariant.
  - **How:** `tests/test_log_sanitization.py` configures `RazorpayConfig` with sentinel secrets (`KEY_SECRET=__should_never_appear_kSec__`, `WEBHOOK_SECRET=__should_never_appear_whSec__`). Captures stdout during a happy-path flow (order creation + simulator-driven webhook + fulfillment). Greps captured output for either sentinel. Assert zero matches.
  - **Files:** `razorpay_checkout/tests/test_log_sanitization.py`.
  - **I verify by:** Test runs in CI; failure is a build break.

- [ ] **Add a CloudWatch Logs Insights query file for debugging a failed webhook event end-to-end.**
  - **Why:** Operational mandate: at least one example Logs Insights query checked in.
  - **How:** File contents:
    ```
    fields @timestamp, level, module, event, correlation_id, tenant_id, order_id, razorpay_payment_id, error
    | filter correlation_id = "<CORRELATION_ID>"
    | sort @timestamp asc
    | limit 200
    ```
    Plus a second query for "all `dead` events in last 24h":
    ```
    fields @timestamp, correlation_id, tenant_id, event_id, error
    | filter processing_status = "dead"
    | filter @timestamp > ago(24h)
    | sort @timestamp desc
    ```
  - **Files:** `razorpay_checkout/observability/logs_insights/webhook_trace.cwlogs`, `razorpay_checkout/observability/logs_insights/dead_events_24h.cwlogs`. Referenced from Runbook 1 in `runbooks/` (later phase).
  - **I verify by:** Files exist in the repo and the README under `observability/` links them for `ops` onboarding.

- [ ] **Phase gate verification and commit.**
  - **Verify:** `uv run pytest tests/test_telemetry.py tests/test_log_sanitization.py -q`. Run a happy-path flow with `OTEL_EXPORTER=stdout`; observe spans with `correlation_id` and one of each metric instrument firing across the flow.
  - **Commit:** `git add -A && git commit -m "observability: OTel exporter selector, ten custom metrics, log sanitization tests, Logs Insights queries"`

---

## Phase: Frontend Widget Package

> Depends on: orders and verify-callback routes available locally.

- [ ] **Initialize TypeScript package `@akhiljoe/checkout-widget`.**
  - **Why:** DESIGN Section 2.2. The widget is the only client surface; amount safety hinges on the server-returned amount being used unchanged.
  - **How:** In `packages/checkout-widget/`: `npm init -y`. `package.json` name `@akhiljoe/checkout-widget`, version `0.1.0`, main `dist/index.cjs.js`, module `dist/index.esm.js`, types `dist/index.d.ts`, exports map, files `["dist"]`. Dev deps: `typescript`, `tsup`, `vitest`, `@types/react`, `eslint`, `@typescript-eslint/parser`, `@typescript-eslint/eslint-plugin`. Peer deps: `react@>=18`. `tsconfig.json` strict mode, target ES2020, jsx `react-jsx`.
  - **Files:** `packages/checkout-widget/package.json`, `packages/checkout-widget/tsconfig.json`, `packages/checkout-widget/tsup.config.ts`.
  - **I verify by:** `cd packages/checkout-widget && npm install && npm run build` produces `dist/` artifacts. `npm run lint` passes.

- [ ] **Implement `src/core.ts` with the internal flow.**
  - **Why:** DESIGN Section 2.2 internal core flow. CSP-blocked check, server-amount-only contract.
  - **How:** Export `async function runCheckout(opts: CheckoutOptions): Promise<void>`. Step 1: `POST orderEndpoint` with `{receipt, currency}`. Step 2: ensure `https://checkout.razorpay.com/v1/checkout.js` is loaded; if not, dynamically inject and `await` script load with 3s timeout. If `window.Razorpay` undefined after 3s, call `onFailure({code:"csp_blocked", description:"Razorpay script blocked by CSP", reason:"csp", source:"client", step:"load_script", metadata:{orderId:"",paymentId:""}})`. Step 3: construct `new window.Razorpay({key, amount, currency, order_id, name, theme, handler, modal:{ondismiss}})`. Step 4: `rzp.open()`. Map Razorpay success callback to `PaymentResult`; map error callback to `PaymentError`.
  - **Files:** `packages/checkout-widget/src/core.ts`.
  - **I verify by:** `vitest` unit test mocks `fetch` to return a valid `CreateOrderResponse`, mocks `window.Razorpay`, asserts `runCheckout` invokes `Razorpay.open()`. A second test removes `window.Razorpay` after timeout and asserts `onFailure` is called with `code="csp_blocked"`.

- [ ] **Implement `src/react.tsx` `<RazorpayCheckout>` component.**
  - **Why:** DESIGN Section 2.2 React API.
  - **How:** Functional component returning `<button>` with `buttonLabel` (default "Pay"). `onClick` calls `runCheckout(props)`. Forwards `onSuccess`, `onFailure`, `onDismiss`. No `amount` prop.
  - **Files:** `packages/checkout-widget/src/react.tsx`.
  - **I verify by:** Render via React Testing Library, click the button, assert `runCheckout` (mocked) called with expected props excluding `amount`.

- [ ] **Implement `src/vanilla.ts` `mountCheckout`.**
  - **Why:** DESIGN Section 2.2: vanilla adapter for non-React hosts. Returns an unmount function.
  - **How:** `export function mountCheckout(element: HTMLElement, options: CheckoutOptions): () => void`. Creates a `<button>` inside `element`, attaches click handler invoking `runCheckout`, returns a function that removes the button.
  - **Files:** `packages/checkout-widget/src/vanilla.ts`.
  - **I verify by:** vitest renders a JSDOM `div`, calls `mountCheckout(div, opts)`, asserts button is appended, click invokes `runCheckout`, returned function removes the button.

- [ ] **Build the developer test page (HTML served by FastAPI).**
  - **Why:** Constraint: minimal local page that exercises full payment flow end-to-end with displayed order id, payment status, and log output. Developer tool, not production UI.
  - **How:** FastAPI route `GET /dev/test-page` (only mounted when `RAZORPAY_MODE=test`) returns an inline HTML page. The page contains: (a) a "Create order" button calling `POST /payments/orders` with a generated receipt, displaying the returned `order_id`, `razorpay_order_id`, and `amount`; (b) imports the built `checkout-widget` from `/packages/checkout-widget/dist/` served via FastAPI `StaticFiles`; (c) mounts `mountCheckout` with `onSuccess`/`onFailure`/`onDismiss` writing to a `<pre id="log">` element; (d) after `onSuccess` posts to `/payments/verify-callback` and then polls `GET /payments/orders/{order_id}` every 2s for 30s, updating a status display; (e) shows a final status label (Created, Authorized, Paid, Fulfilled, Refunded, Failed). Use Razorpay test card `4111 1111 1111 1111`, any future expiry, any CVV, OTP `1234`.
  - **Files:** `example/frontend/test_page.html` (template), `example/backend/main.py` (FastAPI app that mounts `payments_router` plus `/dev/test-page`).
  - **I verify by:** `uvicorn example.backend.main:app --reload` then `http://localhost:8000/dev/test-page` loads. Click "Create order", see `razorpay_order_id`. Click "Pay", complete with the test card. Observe the page transitions through `Paid` then `Fulfilled` within 30s (default no-op fulfillment hook). Log panel shows correlation_id-tagged lines mirroring server logs.

- [ ] **Phase gate verification and commit.**
  - **Verify:** Browser-based end-to-end test page completes a full happy path with Razorpay test mode (requires tunnel or local Razorpay test webhook delivery to the simulator). Vitest suite green. ESLint clean.
  - **Commit:** `git add -A && git commit -m "frontend: checkout-widget package, React + vanilla adapters, developer test page"`

---

## Phase: Local Integration and Chaos Tests

> Depends on: all functional phases.

- [ ] **Author integration test harness against Docker Compose Postgres and LocalStack.**
  - **Why:** DESIGN Section 14 integration tests must run against real Postgres and real (LocalStack-emulated) SQS to catch SQL and JSON-coding bugs that mocks miss.
  - **How:** `tests/integration/conftest.py` provides fixtures: `pg_pool` (asyncpg pool against the compose Postgres, runs `alembic upgrade head` before yield, truncates non-tenants tables after each test); `sqs_client` (aioboto3 against LocalStack, recreates the queues before each test); `app_client` (httpx.AsyncClient backed by the FastAPI app with a registered test tenant); `simulator(event_type, **kwargs)` helper that imports and invokes the simulator in-process.
  - **Files:** `razorpay_checkout/tests/integration/conftest.py`.
  - **I verify by:** `uv run pytest tests/integration/ -q` collects tests and the fixtures resolve.

- [ ] **Write the full happy-path integration test.**
  - **Why:** DESIGN Section 14: full flow create order, capture, webhook, SQS, fulfillment.
  - **How:** Test: POST `/payments/orders` -> assert `created`. Invoke simulator `payment.captured` -> assert order `paid`, payments row `is_authoritative=TRUE`, one SQS message. Manually invoke `fulfillment_handler` against the queued message -> assert order `fulfilled`, `order_events` count == 3 (`initializing -> created`, `created -> attempted -> paid`, `paid -> fulfilled` is 2 distinct rows from the capture step plus 1 from fulfill, so explicitly assert exact entries by `from_status, to_status` tuples).
  - **Files:** `razorpay_checkout/tests/integration/test_happy_path.py`.
  - **I verify by:** Test passes; in DB inspect: order final status `fulfilled`, one row in `payments`, three or four rows in `order_events` matching the expected transition sequence.

- [ ] **Write the `failed -> attempted -> paid` retry integration test.**
  - **Why:** DESIGN Section 5.4. The two-step transition in a single webhook event is non-trivial and historically a regression risk.
  - **How:** Simulator `payment.failed` -> order `failed`, payments row `is_authoritative=FALSE`, `attempt_number=1`. Simulator `payment.captured` (different payment_id) -> order `paid`, second payments row with `attempt_number=2`, `is_authoritative=TRUE`. `order_events` records both edges (`attempted -> failed` and `failed -> attempted` and `attempted -> paid`).
  - **Files:** Same directory.
  - **I verify by:** Test passes with the expected `order_events` sequence.

- [ ] **Write the duplicate webhook tests.**
  - **Why:** DESIGN Section 5.5 + Section 14: duplicate `event_id` short-circuits; duplicate payment with new `event_id` is caught by the primary guard.
  - **How:** Test A: simulator twice with the same generated `event_id` -> second call increments `webhook_duplicate_count`, no second `order_events` row. Test B: simulator with a forced new `event_id` but same `payment_id` -> primary guard fires, `webhook_events.processing_status='done'` with note, no second `order_events` row, `webhook_duplicate_count` unchanged but a `duplicate_payment_id` log line emitted.
  - **Files:** Same directory.
  - **I verify by:** Both assertions pass.

- [ ] **Write the multi-tenant isolation test.**
  - **Why:** DESIGN Critical Invariant 1 + `.claude/rules/tenant-isolation.md`. The strongest correctness property in the system.
  - **How:** Register two tenants `t1`, `t2`. Create orders under each. Send simulator events for `t1` to the `t2` webhook URL with `t1`'s secret -> expect 400 (signature won't match `t2`'s secret). Send simulator events for `t1` with `t1`'s secret to `t1`'s URL -> only `t1`'s orders transition. Run `SELECT count(*) FROM orders WHERE tenant_id='t2' AND status != 'created'` and assert zero.
  - **Files:** Same directory.
  - **I verify by:** Test passes; manual SQL also confirms zero cross-tenant pollution.

- [ ] **Write the concurrent-order-creation test.**
  - **Why:** DESIGN Section 14: 10 simultaneous POSTs with the same receipt result in exactly one Razorpay order.
  - **How:** `respx` patches Razorpay `POST /v1/orders`. Use `asyncio.gather` to fire 10 simultaneous calls. Assert `respx.routes[create_order].call_count == 1` and a single row in `orders`.
  - **Files:** Same directory.
  - **I verify by:** Test passes.

- [ ] **Write the SQS publish failure test.**
  - **Why:** DESIGN failure mode + Runbook 3. DB stays correct; admin retry recovers.
  - **How:** Patch `publish_fulfillment_message` to raise on first call. Simulator `payment.captured` -> assert order `paid`, no SQS message in queue, `fulfillment_sqs_publish_failures` incremented. Call `POST /payments/admin/orders/{id}/retry-fulfillment` with admin key -> assert SQS message produced, order can complete to `fulfilled` after fulfillment handler runs.
  - **Files:** Same directory.
  - **I verify by:** Assertions hold.

- [ ] **Write the fulfillment exhaustion test.**
  - **Why:** DESIGN Section 7 + Section 11. After 5 retries, DLQ poller transitions to `fulfillment_failed`.
  - **How:** Use a `FailingHook` that raises `RuntimeError("forced")`. Call `fulfillment_handler` directly 5 times with the same SQS record. Then call `dlq_poller_handler` with the corresponding DLQ record. Assert order is `fulfillment_failed`, `order_events.metadata.error` contains `"forced"`. Then `POST /payments/admin/orders/{id}/retry-fulfillment` with a fixed (non-failing) hook -> order eventually `fulfilled`.
  - **Files:** Same directory.
  - **I verify by:** Assertions hold.

- [ ] **Write the refund tests (`paid`, `fulfilled`, `fulfillment_failed` to `refunded`).**
  - **Why:** DESIGN Section 5.6 + Section 14: refund event must succeed from all three predecessor states.
  - **How:** Three subtests setting up each predecessor state, simulator `refund.processed` for each, assert `orders.status='refunded'`, `payments.status='refunded'`, one row in `refunds` with `initiated_by='razorpay_dashboard'`.
  - **Files:** Same directory.
  - **I verify by:** All three assertions hold.

- [ ] **Write the unknown-tenant test.**
  - **Why:** DESIGN failure mode: unknown tenant returns 400 and does not persist the event.
  - **How:** POST to `/payments/webhook/<unregistered>` -> 400. `SELECT count(*) FROM webhook_events WHERE tenant_id='<unregistered>'` returns 0.
  - **Files:** Same directory.
  - **I verify by:** Assertions hold.

- [ ] **Write the stuck-in-`authorized` test.**
  - **Why:** DESIGN Section 6: orders stuck in `authorized` > 24h should be detected. The Lambda is added in a later phase; this test exercises the detection query directly.
  - **How:** Insert order with `status='authorized', authorized_at=NOW() - INTERVAL '25 hours'`. Run the scheduled query `SELECT id FROM orders WHERE status='authorized' AND authorized_at < NOW() - INTERVAL '24 hours'`. Assert the row is returned. (CloudWatch alarm wiring is validated post-deploy.)
  - **Files:** Same directory.
  - **I verify by:** Query returns the test row.

- [ ] **Phase gate verification and commit.**
  - **Verify:** `uv run pytest -q` (unit + integration) green. Coverage report shows the webhook pipeline, state machine, and order service above 90%.
  - **Commit:** `git add -A && git commit -m "tests: integration coverage for happy path, retry, duplicates, multi-tenant, refund, sqs failure, fulfillment exhaustion"`

---

## Phase: Auxiliary Lambdas and Operational Tooling

> Depends on: previous phases. Logical follow-up before cloud deployment.

- [ ] **Add the Neon keep-alive Lambda handler.**
  - **Why:** DESIGN Section 6 + Runbook 6: EventBridge runs `SELECT 1` every 4 days to keep Neon awake.
  - **How:** `async def keep_alive_handler(event, context)` opens pool, runs `SELECT 1`, returns. Logs INFO. EventBridge schedule wired in CDK phase.
  - **Files:** `razorpay_checkout/razorpay_checkout/lambdas/keep_alive.py`.
  - **I verify by:** Unit test invokes handler against local Postgres, asserts `SELECT 1` query was executed.

- [ ] **Add the stuck-in-`authorized` monitor.**
  - **Why:** DESIGN Section 6 + Runbook 4: alarm on `authorized_at < NOW() - INTERVAL '24 hours'`.
  - **How:** Handler runs the query, emits metric `orders_stuck_authorized_count`, logs WARN per row.
  - **Files:** `razorpay_checkout/razorpay_checkout/lambdas/authorized_monitor.py`.
  - **I verify by:** Unit test sets up a stale `authorized` order, runs handler, observes one metric record.

- [ ] **Author the eight operational runbooks as markdown.**
  - **Why:** DESIGN Section 12. Runbooks are deliverables; they reference admin endpoints and metrics implemented above.
  - **How:** One file per runbook with sections "Trigger", "Steps", "Verification". Each step references concrete commands or routes: e.g., Runbook 1 includes `curl -X POST -H 'X-Admin-Api-Key: $KEY' $API/payments/admin/events/$ID/replay`. Runbook 2 references the Logs Insights query checked in earlier.
  - **Files:** `runbooks/01_stuck_or_dead_webhook_event.md`, `02_fulfillment_dlq_message.md`, `03_order_stuck_in_paid.md`, `04_order_stuck_in_authorized.md`, `05_reconciliation_mismatch.md`, `06_neon_paused.md`, `07_webhook_secret_rotation.md`, `08_captured_no_fulfillment.md`, `09_refund_out_of_sync.md`.
  - **I verify by:** Files exist, each contains the trigger condition exactly as in DESIGN Section 12, and the recovery `curl` commands compile (manually inspect).

- [ ] **Phase gate verification and commit.**
  - **Verify:** Unit tests for keep_alive and authorized_monitor handlers pass. Runbook files render cleanly.
  - **Commit:** `git add -A && git commit -m "ops: keep-alive lambda, authorized monitor, nine operational runbooks"`

---

## Phase: AWS Foundation (Pre-Deployment Groundwork)

> Depends on: full local stack committed and verified.

- [ ] **Create the AWS administrator IAM user (out-of-band, not in CDK).**
  - **Why:** Constraint: "AWS Foundation Before Cloud Deployment". The bootstrapping admin must exist before any CDK deploy can run. Following AWS best practice, the root account is locked down and an administrative IAM user is created with MFA.
  - **How:** In the AWS console (root account, one-time): create user `razorpay-bootstrap-admin`. Attach managed policy `AdministratorAccess`. Enable MFA. Generate an access key. Record account id.
  - **Files:** N/A (manual). Record locations in `infra/README.md` under "Bootstrap".
  - **I verify by:** `aws --profile razorpay-bootstrap sts get-caller-identity` returns the user ARN and account id.

- [ ] **Define IAM groups with least-privilege policies scoped to this project.**
  - **Why:** Long-running access should not flow through the bootstrap admin. Per-role IAM separation prevents accidental damage.
  - **How:** Create groups `razorpay-deployer` (CDK deploy + read CloudFormation, read/write SSM under `/razorpay/*`, read/write Secrets Manager under `razorpay/*`, read Logs, read/write Lambda, read/write SQS for queues prefixed `fulfillment-`), `razorpay-operator` (read Logs, read/write SSM, read SQS, invoke Lambdas under `razorpay-*`), `razorpay-readonly` (read Logs, read SSM, read SQS depth). Inline policy documents committed to `infra/iam_policies/`.
  - **Files:** `infra/iam_policies/deployer.json`, `infra/iam_policies/operator.json`, `infra/iam_policies/readonly.json`, `infra/README.md` (instructions for applying them).
  - **I verify by:** `aws --profile razorpay-bootstrap iam list-groups` shows all three. `aws iam get-group-policy --group-name razorpay-deployer --policy-name deployer-inline` returns the JSON matching the file.

- [ ] **Configure AWS CLI named profiles.**
  - **Why:** Local CLI must distinguish bootstrap admin from deployer from operator.
  - **How:** `~/.aws/credentials` has profiles `razorpay-bootstrap`, `razorpay-deployer-dev`, `razorpay-operator-dev`. `~/.aws/config` sets `region=ap-south-1` per profile. Document in `infra/README.md`.
  - **Files:** N/A on disk in the repo. Documented in `infra/README.md`.
  - **I verify by:** `aws --profile razorpay-deployer-dev sts get-caller-identity` returns the deployer's ARN.

- [ ] **Provision Secrets Manager entries (one per tenant per environment).**
  - **Why:** DESIGN Section 6 + Runbook 7: `RAZORPAY_WEBHOOK_SECRET` and `RAZORPAY_WEBHOOK_SECRET_PREVIOUS` must be retrievable from Secrets Manager; Lambda fetches them at startup.
  - **How:** Out-of-band (or via a one-off script `scripts/seed_secrets.py`): create `razorpay/<env>/<tenant_id>/key_secret`, `razorpay/<env>/<tenant_id>/webhook_secret`, `razorpay/<env>/<tenant_id>/webhook_secret_previous` (optional), `razorpay/<env>/<tenant_id>/admin_api_key`. JSON value `{"value": "<secret>"}`.
  - **Files:** `scripts/seed_secrets.py`, `infra/README.md` documenting the naming convention.
  - **I verify by:** `aws --profile razorpay-deployer-dev secretsmanager get-secret-value --secret-id razorpay/dev/t1/webhook_secret --query SecretString --output text` returns the expected JSON.

- [ ] **Phase gate verification and commit.**
  - **Verify:** All four `aws` CLI checks above succeed. `infra/README.md` documents the entire bootstrap procedure.
  - **Commit:** `git add -A && git commit -m "aws-foundation: IAM groups, least-privilege policies, named CLI profiles, Secrets Manager seeding"`

---

## Phase: CDK Infrastructure in Python

> Depends on: AWS foundation. Local cdk synth only in this phase.

- [ ] **Initialize the CDK Python project under `infra/`.**
  - **Why:** Constraint: CDK in Python under `infra/`. Overrides the design's TypeScript file names; same directory.
  - **How:** `cd infra && cdk init app --language python`. Dependencies: `aws-cdk-lib>=2.150`, `constructs>=10`, `aws-cdk.aws-lambda-python-alpha`. Create `app.py` instantiating `FreeStack(app, "razorpay-free-dev", env=Environment(region="ap-south-1"))` and `PaidStack(app, "razorpay-paid-prod", env=Environment(region="ap-south-1"))`. `cdk.json` `context: {tier: "free"}` default.
  - **Files:** `infra/app.py`, `infra/cdk.json`, `infra/requirements.txt`, `infra/stacks/__init__.py`.
  - **I verify by:** `cd infra && cdk list` outputs both stack names without error.

- [ ] **Implement `FreeStack` with all v1 resources.**
  - **Why:** DESIGN Section 7 + Section 8. Webhook Lambda, Fulfillment Lambda, SQS FIFO main + DLQ, scheduled monitors, log groups with 7-day retention.
  - **How:** Resources: `Queue` `fulfillment-queue.fifo` (`fifo=True`, `content_based_deduplication=False`, `dead_letter_queue={max_receive_count: 5, queue: dlq}`), DLQ `fulfillment-dlq.fifo` (`fifo=True`). `aws_lambda_python_alpha.PythonFunction` for `webhook` (memory 512MB, timeout 30s, env `OTEL_EXPORTER=stdout`, secrets from Secrets Manager via `aws_lambda.Function.add_environment` referencing arn). API Gateway `HttpApi` with route `POST /payments/webhook/{tenant_id}` integrated to webhook Lambda, plus routes for `/payments/orders`, `/payments/orders/{order_id}`, `/payments/verify-callback`, `/payments/reconcile`, `/payments/admin/*`. Fulfillment `PythonFunction` with SQS event source mapping (batch size 1, `reportBatchItemFailures=True`). DLQ poller `PythonFunction` with event source mapping on the DLQ. Stale-paid monitor and stuck-authorized monitor as `PythonFunction` triggered by EventBridge `Schedule(every minute)` and `Schedule(every hour)` respectively. Keep-alive on `Schedule(every 4 days)`. CloudWatch alarms: `Alarm` on DLQ depth > 0, on dead-event count metric > 0, on stale-paid metric > 0, on stuck-authorized metric > 0.
  - **Files:** `infra/stacks/free_stack.py`.
  - **I verify by:** `cd infra && cdk synth razorpay-free-dev > /tmp/free-synth.yaml` succeeds. `grep -c 'AWS::Lambda::Function' /tmp/free-synth.yaml` returns 6 (webhook, fulfillment, dlq_poller, stale_paid_monitor, authorized_monitor, keep_alive). `grep -c 'AWS::SQS::Queue' /tmp/free-synth.yaml` returns 2.

- [ ] **Implement `PaidStack` (extends `FreeStack`).**
  - **Why:** DESIGN Section 9: Paid Track is Lambda with provisioned concurrency = 1 on the webhook function. WAF, secrets rotation, full log retention.
  - **How:** Subclass `FreeStack` or compose. Override webhook function `current_version` and add `Alias` with `provisioned_concurrent_executions=1`. Add WAFv2 web ACL bound to the HttpApi (rate-limit rule 2000 req/5min per IP). Increase log retention to 30 days. Switch `OTEL_EXPORTER=xray` and attach ADOT Lambda layer. Optionally add RDS construct (off by default; toggled via context flag `with_rds=true`).
  - **Files:** `infra/stacks/paid_stack.py`.
  - **I verify by:** `cd infra && cdk synth razorpay-paid-prod > /tmp/paid-synth.yaml` succeeds. `grep 'ProvisionedConcurrentExecutions' /tmp/paid-synth.yaml` returns at least one match.

- [ ] **Add CDK construct tests.**
  - **Why:** Catch resource drift before deploy. Tests run in CI without AWS credentials.
  - **How:** `infra/tests/test_free_stack.py` uses `aws_cdk.assertions.Template`. Assert `Template.from_stack(stack).resource_count_is("AWS::Lambda::Function", 6)`, `has_resource_properties("AWS::SQS::Queue", {"FifoQueue": True})`, etc.
  - **Files:** `infra/tests/test_free_stack.py`, `infra/tests/test_paid_stack.py`.
  - **I verify by:** `cd infra && pytest -q tests/` green.

- [ ] **Phase gate verification and commit.**
  - **Verify:** `cdk synth` for both stacks succeeds. `cdk diff razorpay-free-dev --profile razorpay-deployer-dev` runs (will show "all new" if no prior deploy, which is expected). Construct tests green. No real deployment yet.
  - **Commit:** `git add -A && git commit -m "infra: cdk python stacks for free and paid tracks, construct tests"`

---

## Phase: First Cloud Deployment and Validation

> Depends on: all previous phases. This is where LocalStack diverges from real AWS.

- [ ] **Bootstrap the CDK environment in the dev account.**
  - **Why:** First-ever CDK deploy in an account requires `cdk bootstrap` to create the staging bucket and roles.
  - **How:** `cd infra && cdk bootstrap aws://<account>/ap-south-1 --profile razorpay-deployer-dev`.
  - **Files:** N/A.
  - **I verify by:** `aws --profile razorpay-deployer-dev cloudformation describe-stacks --stack-name CDKToolkit` returns CREATE_COMPLETE.

- [ ] **Deploy `razorpay-free-dev`.**
  - **Why:** Smoke deployment to validate IAM, secrets, alarms, and webhook latency on real Lambda.
  - **How:** `cd infra && cdk deploy razorpay-free-dev --profile razorpay-deployer-dev`. Inspect outputs for the HttpApi URL. Register the URL `<api>/payments/webhook/<tenant_id>` in the Razorpay test dashboard.
  - **Files:** N/A.
  - **I verify by:** `curl <api>/health` returns 200 (assuming a `/health` route was added; if not, observe the CloudFormation `CREATE_COMPLETE` plus an empty 404 from the API Gateway).

- [ ] **Run the simulator against the deployed API.**
  - **Why:** Confirms the webhook Lambda processes a signed event end-to-end in real AWS.
  - **How:** `python scripts/razorpay-simulator.py payment.captured --tenant t1 --order-id rzp_order_dev_001 --payment-id pay_dev_001 --secret <secrets_manager_value> --url <api>/payments/webhook/t1`.
  - **Files:** N/A.
  - **I verify by:** Response is 200. CloudWatch Logs Insights query (`fields ... | filter correlation_id = "<from response or logs>"`) returns the full trace: webhook entry, `webhook_events` insert, transition to `paid`, SQS publish, fulfillment Lambda entry, transition to `fulfilled`. The deployed `webhook_trace.cwlogs` file from the observability phase is the exact query.

- [ ] **Verify CloudWatch alarms fire on synthetic failure.**
  - **Why:** Alarm wiring cannot be verified on LocalStack. This step closes that gap.
  - **How:** Manually push a message to `fulfillment-dlq.fifo`. Wait for the alarm to fire (check `aws cloudwatch describe-alarms --state-value ALARM`). Verify PagerDuty (or whatever sink) received the page.
  - **Files:** N/A.
  - **I verify by:** Alarm transitions to ALARM state. Manually `aws sqs purge-queue` after verification.

- [ ] **Execute the live Razorpay-test-mode end-to-end flow via the developer test page.**
  - **Why:** Constraint: the test page exercises the full flow with Razorpay test card credentials and triggers the real Razorpay webhook.
  - **How:** Open the developer test page (deployed alongside the API or run locally with the deployed API URL configured). Create an order. Pay with test card `4111 1111 1111 1111`. Observe the page transition to `Paid` then `Fulfilled` within 30s. The webhook arrives at the deployed API, not the simulator.
  - **Files:** N/A.
  - **I verify by:** Page shows `Fulfilled`. CloudWatch logs include matching `correlation_id` across HTTP entry, `webhook_events` insert, `paid` transition, SQS publish, fulfillment Lambda, `fulfilled` transition.

- [ ] **Phase gate verification and commit.**
  - **Verify:** Live happy-path completed. CloudWatch alarms validated. Logs Insights query returns the end-to-end trace.
  - **Commit:** `git add -A && git commit -m "deploy: razorpay-free-dev deployed and validated end-to-end on AWS"`

---

## Phase: Out-of-Scope Deferrals and Scaling Triggers

> Depends on: deployment. Documentation closeout.

- [ ] **Author `SCALE_PLAN.md` capturing every item from DESIGN Section 16.**
  - **Why:** DESIGN Section 16 is a contract: each deferred item has a stated promotion trigger. Codifying them prevents accidental scope creep and gives ops the green light to add an item when the trigger fires.
  - **How:** One markdown table with columns: Item, v1 Behavior, Promotion Trigger, Owner, Rough Effort, Rollback. Rows include: transactional outbox, scheduled reconciliation, constant-time webhook responses, manual capture admin endpoint, automated capture retry, refund UI + initiation API, partial refund support, subscriptions, multi-currency, partial captures, 3DS edge cases, payment link generation, UPI QR, EMI, international methods, API versioning, SQS Standard + SNS fan-out, `Idempotency-Key` header, vendor-agnostic schema column naming. Plus the Section 7 SQS upgrade triggers (fulfillment p95 > 30s for 1h, DLQ depth > 100 for 1h, fan-out requirement) and the Section 9 Fargate trigger (concurrent execution > 900 of 1000 for > 1h or p99 cold start breach).
  - **Files:** `SCALE_PLAN.md`.
  - **I verify by:** Table contains all 19 design rows plus the architectural scaling triggers. Each promotion trigger is a measurable condition.

- [ ] **Author `TEST_PLAN.md` mapping DESIGN Section 14 to test file paths.**
  - **Why:** DESIGN repository layout names this file. It is the auditable cross-reference for "every test scenario in the design is covered".
  - **How:** Sections: Unit, Integration, Chaos. Each list item is a one-line description from DESIGN Section 14, followed by the file path and test function name implementing it.
  - **Files:** `TEST_PLAN.md`.
  - **I verify by:** Every bullet in Section 14 has a corresponding row. No "TBD" entries remain after final commit.

- [ ] **Phase gate verification and final commit.**
  - **Verify:** Both planning documents complete. `git log --oneline` shows one commit per phase. CI green on the deployed branch.
  - **Commit:** `git add -A && git commit -m "docs: SCALE_PLAN and TEST_PLAN cross-references for deferrals and tests"`

---

## Appendix: Cross-Cutting Verifications That Must Hold at Every Phase

These are repeated checks rather than steps. The developer runs them before each phase commit.

| Check | Command | Pass Criterion |
|---|---|---|
| **No `print` calls** | `rg -n "(?<!\\w)print\\(" razorpay_checkout/razorpay_checkout/` | Zero matches outside test fixtures |
| **No secrets in logs** | `uv run pytest tests/test_log_sanitization.py` | Green |
| **Tenant filter on every query** | `rg -n "FROM (orders\|payments\|refunds\|webhook_events\|order_events)" razorpay_checkout/razorpay_checkout/ \| rg -v "tenant_id"` | Zero matches (every result line includes `tenant_id`) |
| **Alembic round-trip** | `uv run alembic downgrade base && uv run alembic upgrade head` | Both succeed |
| **Lint** | `uv run ruff check . && uv run mypy razorpay_checkout/` | Both clean |
| **Test coverage** | `uv run pytest --cov=razorpay_checkout --cov-fail-under=85` | Pass |

## Appendix: LocalStack Gaps Re-Validated Post-Deploy

| Behavior | Why It Differs | Validated By |
|---|---|---|
| Lambda cold start latency | LocalStack reuses containers indefinitely | First-deploy timing observed in CloudWatch metrics |
| IAM policy enforcement | LocalStack permissive in community edition | `aws sts decode-authorization-message` on a denied call |
| CloudWatch metric filter latency | LocalStack lag is undefined | Synthetic alarm trip in the deployment phase |
| SQS FIFO throughput per `MessageGroupId` | LocalStack does not throttle to 1/s per group | Load test post-deploy |
| Secrets Manager rotation hooks | LocalStack does not run rotation lambdas | Manual webhook secret rotation drill (Runbook 7) post-deploy |
| X-Ray sampling | LocalStack does not implement sampling decisions | Inspect actual sampled spans in X-Ray console |
