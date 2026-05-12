# Commands

Every command you'll repeatedly run. Organized by task. Ctrl-F your way around.

All commands assume:
- POSIX shell (git bash on Windows; PowerShell users adapt).
- Working directory is the repo root unless noted.
- Stale `RAZORPAY_*` env vars are unset (see "Shell hygiene" below).

## Shell hygiene (run at the start of any new terminal)

Stale `RAZORPAY_*` exports silently override `.env` (DESIGN_v5 Section 17.10). Always reset.

```bash
for v in $(env | grep -E '^RAZORPAY_' | cut -d= -f1); do unset "$v"; done
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=ap-south-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export DATABASE_URL="postgresql://razorpay:razorpay@localhost:5432/razorpay_checkout"
```

## Infrastructure

### Start / stop Postgres + LocalStack

```bash
docker compose up -d postgres localstack
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "postgres|localstack"

docker compose stop postgres localstack   # leaves volumes
docker compose down                       # destroys containers (keeps volumes)
docker compose down -v                    # nuke everything including data
```

### Apply migrations

```bash
cd razorpay_checkout
uv run alembic upgrade head
uv run alembic current
uv run alembic history
```

### Seed / purge / reset local state

```bash
uv run --project razorpay_checkout python scripts/local-fixtures.py status
uv run --project razorpay_checkout python scripts/local-fixtures.py seed
uv run --project razorpay_checkout python scripts/local-fixtures.py seed --tenant local-dev --tenant acme
uv run --project razorpay_checkout python scripts/local-fixtures.py purge
uv run --project razorpay_checkout python scripts/local-fixtures.py purge --hard   # also delete tenant rows
uv run --project razorpay_checkout python scripts/local-fixtures.py reset           # purge + seed
```

## Tests

### Python

```bash
cd razorpay_checkout

uv run pytest -q                                  # everything
uv run pytest -q tests/test_chaos.py              # chaos only
uv run pytest -q -m "not requires_db" -q          # skip DB-backed
uv run pytest -q -m "not requires_localstack"     # skip LocalStack
uv run pytest -q -k "idempotency" -v              # by keyword
uv run pytest --cov=razorpay_checkout --cov-report=term-missing
```

### TypeScript

```bash
cd packages/checkout-widget

npm install
npm test                              # 15 vitest tests
npm run lint
npm run build                         # dist/ with .mjs + .cjs + .d.ts
node -e "console.log(typeof require('./dist/index.cjs').mountCheckout)"
node --input-type=module -e "import('./dist/index.mjs').then(m => console.log(typeof m.mountCheckout))"
npm audit --omit=dev --production     # runtime-only audit
```

### Lint + type-check (Python)

```bash
cd razorpay_checkout
uv run ruff check .
uv run ruff check . --fix             # auto-fix
uv run mypy razorpay_checkout
```

## Dev backend

### Start uvicorn

```bash
cd example/backend
uv run --project ../../razorpay_checkout uvicorn main:app --reload --port 8000
```

Reachable URLs:
- `http://localhost:8000/health`
- `http://localhost:8000/dev/dashboard` — Payment Journey + 11 scenarios
- `http://localhost:8000/dev/shop` — buyer storefront
- `http://localhost:8000/payments/orders` (POST)
- `http://localhost:8000/payments/webhook/{tenant_id}` (POST from Razorpay or simulator)

### Start local pump (drain SQS queues)

```bash
cd /c/Users/you/Documents/razorpay-payment-integration
uv run --project razorpay_checkout python scripts/local-pump.py

uv run --project razorpay_checkout python scripts/local-pump.py --once             # drain and exit
uv run --project razorpay_checkout python scripts/local-pump.py --only webhook     # one queue
uv run --project razorpay_checkout python scripts/local-pump.py --only fulfillment
```

## Driving webhooks

### Synthetic webhook via simulator script

```bash
uv run --project razorpay_checkout python scripts/razorpay-simulator.py payment.captured \
  --tenant local-dev \
  --order-id order_XXX \
  --payment-id pay_XXX \
  --amount 50000

uv run --project razorpay_checkout python scripts/razorpay-simulator.py payment.authorized \
  --tenant local-dev --order-id order_XXX --payment-id pay_XXX --amount 50000

uv run --project razorpay_checkout python scripts/razorpay-simulator.py refund.processed \
  --tenant local-dev --order-id order_XXX --payment-id pay_XXX --refund-id rfnd_XXX --amount 50000

uv run --project razorpay_checkout python scripts/razorpay-simulator.py payment.captured \
  --tenant local-dev --order-id order_XXX --payment-id pay_XXX --amount 50000 \
  --print-only                     # build + sign but don't POST
```

### Curl POST (manual)

```bash
RESP=$(curl -s -X POST http://localhost:8000/payments/orders \
  -H 'content-type: application/json' \
  -d '{"receipt":"my-receipt-001"}')
echo "$RESP" | python -m json.tool

ORDER_ID=$(echo "$RESP" | python -c "import json,sys; print(json.load(sys.stdin)['order_id'])")
curl -s http://localhost:8000/payments/orders/$ORDER_ID | python -m json.tool
```

## SQS inspection

```bash
# Queue depths
aws --endpoint-url=http://localhost:4566 sqs get-queue-attributes \
  --queue-url http://localhost:4566/000000000000/webhook-events.fifo \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible

aws --endpoint-url=http://localhost:4566 sqs get-queue-attributes \
  --queue-url http://localhost:4566/000000000000/webhook-events-dlq.fifo \
  --attribute-names ApproximateNumberOfMessages

# List queues
aws --endpoint-url=http://localhost:4566 sqs list-queues

# Purge a queue (has 60s cooldown)
aws --endpoint-url=http://localhost:4566 sqs purge-queue \
  --queue-url http://localhost:4566/000000000000/webhook-events.fifo
```

## DLQ operations

```bash
uv run --project razorpay_checkout python scripts/dlq-replay.py list webhook
uv run --project razorpay_checkout python scripts/dlq-replay.py list fulfillment

uv run --project razorpay_checkout python scripts/dlq-replay.py inspect webhook --index 0
uv run --project razorpay_checkout python scripts/dlq-replay.py inspect webhook --message-id <id>

uv run --project razorpay_checkout python scripts/dlq-replay.py replay webhook --all --dry-run
uv run --project razorpay_checkout python scripts/dlq-replay.py replay webhook --message-id <id>

uv run --project razorpay_checkout python scripts/dlq-replay.py delete webhook --all --dry-run
uv run --project razorpay_checkout python scripts/dlq-replay.py delete webhook --message-id <id>
```

Production usage requires `--allow-production` flag (refuses non-LocalStack by default).

## Database

### Connect to local Postgres

```bash
docker exec -it razorpay-postgres psql -U razorpay -d razorpay_checkout
```

### Inspect tables

```bash
docker exec -i razorpay-postgres psql -U razorpay -d razorpay_checkout <<'SQL'
SELECT id, status, attempted_at, paid_at, fulfilled_at, correlation_id
  FROM orders ORDER BY created_at DESC LIMIT 5;

SELECT from_status, to_status, trigger, created_at
  FROM order_events
 WHERE order_id = (SELECT id FROM orders ORDER BY created_at DESC LIMIT 1)
 ORDER BY created_at;

SELECT event, processing_status, received_at
  FROM webhook_events ORDER BY received_at DESC LIMIT 5;

SELECT razorpay_payment_id, status, is_authoritative, amount
  FROM payments ORDER BY created_at DESC LIMIT 5;
SQL
```

### Reset DB schema (destructive)

```bash
docker exec -i razorpay-postgres psql -U razorpay -d razorpay_checkout -c \
  "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

cd razorpay_checkout && uv run alembic upgrade head
```

## CDK / AWS

### Prereqs

```bash
# CDK CLI (one-time)
npm install -g aws-cdk
cdk --version    # expect 2.x.x

# Python deps
cd infra
uv sync --extra dev
source .venv/Scripts/activate    # git bash on Windows
```

### Synth (no AWS contact)

```bash
cd infra
cdk synth                                   # both stacks
cdk synth razorpay-free-track-foundation    # one stack
cdk synth razorpay-free-track-payments

ls -la cdk.out/
```

### Diff against a real account (Phase 17+)

```bash
AWS_PROFILE=razorpay-prod cdk diff
AWS_PROFILE=razorpay-prod cdk diff razorpay-free-track-payments
```

### Deploy (Phase 17+, costs real money)

```bash
AWS_PROFILE=razorpay-prod cdk bootstrap aws://<account-id>/ap-south-1   # one-time per account+region

AWS_PROFILE=razorpay-prod cdk deploy razorpay-free-track-foundation
AWS_PROFILE=razorpay-prod cdk deploy razorpay-free-track-payments
```

### Destroy

```bash
AWS_PROFILE=razorpay-prod cdk destroy razorpay-free-track-payments
# Foundation has retained resources — manual cleanup required.
```

## Git

```bash
git status
git log --oneline -10
git diff
git diff --stat
git diff HEAD~1 HEAD                 # last commit's changes

# Pre-commit checks
cd razorpay_checkout
uv run ruff check . && uv run mypy razorpay_checkout && uv run pytest -q
```

**Reminder:** never run `git commit`, `git push`, `git tag`, `git merge`, or any history-writing command on the AI agent's behalf. Stage with `git add`, review the diff, then run commit yourself.

## Common debugging

### See producer ↔ consumer ↔ fulfillment activity

Tail uvicorn console (Terminal A). Filter for a correlation_id:

```bash
# In another terminal:
docker exec -i razorpay-postgres psql -U razorpay -d razorpay_checkout -c \
  "SELECT correlation_id FROM orders ORDER BY created_at DESC LIMIT 1;"
# Then grep uvicorn output for that uuid.
```

### Check what the dashboard sees

Open `http://localhost:8000/dev/dashboard`, click an order, click **Copy snapshot** at the bottom. Get a one-block dump of order + events + payments + queues for hand-off.

### Confirm the right `.env` is loading

```bash
cd example/backend
uv run --project ../../razorpay_checkout python -c "
from razorpay_checkout import RazorpayConfig
c = RazorpayConfig()
print('tenant_id:', c.tenant_id)
print('key_id:', c.key_id)
print('mode:', c.mode)
"
```

If `tenant_id` is something other than `local-dev`, you have a stale shell env var (Section 17.10).

### See OTel metrics in the console

`RAZORPAY_OTEL_EXPORTER=stdout` in `.env`. Metrics flush every 5s in uvicorn output.

## Related

- [QUICKSTART.md](QUICKSTART.md) — first-time setup
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — symptom-indexed fixes
- [docs/runbooks/](docs/runbooks/) — deep dives for incident response
