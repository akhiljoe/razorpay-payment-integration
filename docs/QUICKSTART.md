# Quickstart

Clone-to-running locally in ~10 minutes. No AWS account required. Tested commands only.

## Prerequisites

- **Windows + git bash** (instructions use POSIX syntax; PowerShell users adapt)
- **Docker Desktop** running
- **Node.js 20+** (`node --version`)
- **`uv`** installed (`uv --version`; install via <https://docs.astral.sh/uv/>)
- **Razorpay test keys** if you want to run the buyer-perspective storefront against real Razorpay test mode. Get them at <https://dashboard.razorpay.com> → Settings → API Keys → Generate Test Key. Skippable if you only run unit tests + dashboard scenarios.

## Step 1 — Clone and start infrastructure (2 min)

```bash
cd /c/Users/you/Documents/razorpay-payment-integration   # adjust path

# Postgres + LocalStack containers
docker compose up -d postgres localstack
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "postgres|localstack"
# Both should show 'Up ... (healthy)'
```

## Step 2 — Install Python deps + migrations (2 min)

```bash
cd razorpay_checkout
uv sync --extra dev
uv run alembic upgrade head
# Expect: "INFO  [alembic.runtime.migration] Will assume transactional DDL"
#         followed by "Running upgrade ... 0007_order_events"

cd /c/Users/you/Documents/razorpay-payment-integration
uv run --project razorpay_checkout python scripts/local-fixtures.py reset
# Inserts the local-dev tenant; purges all queues.
```

## Step 3 — Install TypeScript widget (2 min)

```bash
cd packages/checkout-widget
npm install
npm test       # 15 vitest tests pass
npm run build  # dual ESM/CJS output in dist/
```

## Step 4 — Create the dev backend `.env` (1 min)

```bash
cat > /c/Users/you/Documents/razorpay-payment-integration/example/backend/.env <<'EOF'
RAZORPAY_DB_DSN=postgresql://razorpay:razorpay@localhost:5432/razorpay_checkout
AWS_ENDPOINT_URL=http://localhost:4566
AWS_DEFAULT_REGION=ap-south-1
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
RAZORPAY_TENANT_ID=local-dev
RAZORPAY_KEY_ID=rzp_test_REPLACE_ME
RAZORPAY_KEY_SECRET=REPLACE_ME
RAZORPAY_WEBHOOK_SECRET=dev_webhook_secret
RAZORPAY_MODE=test
RAZORPAY_OTEL_EXPORTER=stdout
DEV_TEST_AMOUNT_PAISE=50000
LOG_LEVEL=INFO
EOF
```

Replace `rzp_test_REPLACE_ME` with your real Razorpay test key id, and `REPLACE_ME` (key_secret) with the secret. **Skip this only if you don't need the storefront.**

## Step 5 — Start the three runtime processes (in three terminals)

### Terminal A — uvicorn (dev backend)

```bash
for v in $(env | grep -E '^RAZORPAY_' | cut -d= -f1); do unset "$v"; done
cd /c/Users/you/Documents/razorpay-payment-integration/example/backend
uv run --project ../../razorpay_checkout uvicorn main:app --reload --port 8000
```

Pass criterion: `Application startup complete.` and `queue_pair_ensured` for both queues.

### Terminal B — local pump (consumer drain)

```bash
for v in $(env | grep -E '^RAZORPAY_' | cut -d= -f1); do unset "$v"; done
cd /c/Users/you/Documents/razorpay-payment-integration
uv run --project razorpay_checkout python scripts/local-pump.py
```

Pass criterion: `pump_ready queues=['webhook-pump','fulfillment-pump']`.

### Terminal C — open browser

Open **`http://localhost:8000/dev/dashboard`** in Chrome.

- Click **Reset (purge DB + queues)**.
- Click **Run all scenarios in sequence**.
- Watch all 11 scenarios turn green within ~40 seconds.

Then **`http://localhost:8000/dev/shop`** if you have Razorpay test keys.

- Click **Pay Now**, complete payment via UPI / Netbanking / card.
- Watch the timeline auto-fill to "Order fulfilled".

## What you just proved

In ~10 minutes you have a fully functioning, locally-runnable payment integration that:

- Persists multi-tenant order state in Postgres
- Verifies HMAC webhook signatures with previous-secret fallback
- Publishes to SQS FIFO with `MessageGroupId` per order
- Consumes with event-type-aware idempotency
- Fulfills with at-least-once retry semantics
- Emits structured logs + OpenTelemetry metrics
- Catches poison messages in a DLQ after 5 retries

11 of the 13 documented Section 17 learnings became part of the system because something broke during the build — the dashboard scenarios are how you'd catch a regression of any of those.

## Next steps

| Want to | Read |
|---|---|
| See every command | [COMMANDS.md](COMMANDS.md) |
| Read system overview | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Hit an error | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Run against AWS | [docs/cloud/account-setup.md](docs/cloud/account-setup.md) |

## Common first-time issues

- **`uv: command not found`** — install from <https://docs.astral.sh/uv/>, restart terminal.
- **`docker compose` says `network already exists`** — run `docker compose down` then `docker compose up -d`.
- **`alembic upgrade head` fails with auth error** — Postgres container password is `razorpay`. The DSN in `.env` should already match. If you changed Postgres creds, update `RAZORPAY_DB_DSN` in `.env`.
- **uvicorn 500s on `/payments/orders`** — your `.env` has placeholder Razorpay keys. Replace them, restart uvicorn. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) section "Order creation returns 500".
- **Razorpay modal says "International cards not supported"** — your test account is India-only. Use UPI `success@razorpay` or Netbanking → Success simulator button.

If something else breaks, [TROUBLESHOOTING.md](TROUBLESHOOTING.md) is the lookup index.
