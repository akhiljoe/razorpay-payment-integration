# Troubleshooting

Error or symptom → fix index. For deep incident response, see [`docs/runbooks/`](docs/runbooks/).

Each entry tagged:
- **Hit** — we encountered this during development; the fix is documented in DESIGN_v6 Section 17.
- **Predicted** — invariant in CLAUDE.md; haven't hit it but it's in the design.

---

## Process won't start

### `uv: command not found` (Hit)

uv isn't on PATH. Install: https://docs.astral.sh/uv/. After install, restart your terminal so PATH refreshes.

### `cdk: program not found` even though `npm install -g aws-cdk` worked (Hit)

npm installed `cdk.cmd` to its prefix directory which isn't on the shell PATH. Workaround:

```bash
export PATH="/c/Users/you/AppData/Roaming/npm:$PATH"
which cdk
```

Make permanent:

```bash
echo 'export PATH="/c/Users/you/AppData/Roaming/npm:$PATH"' >> ~/.bashrc
```

`uv run cdk synth` may still fail because uv spawns subprocesses without that PATH addition; activate the venv directly:

```bash
cd infra
source .venv/Scripts/activate
cdk synth
```

(DESIGN_v6 not formally captured — Windows-only quirk.)

### uvicorn boots but `Application startup failed.` immediately (Hit)

Check the traceback **above** the "startup failed" line. Top three causes:

1. **Missing `RAZORPAY_*` fields** — `RazorpayConfig()` raises `ValidationError`. Required: `tenant_id`, `key_id`, `key_secret`, `webhook_secret`, `db_dsn`. Solution: populate `example/backend/.env`.
2. **`aioboto3` `InvalidClientTokenId`** — AWS env vars not exported. `main.py` lifts `.env` into `os.environ`, but if `.env` doesn't exist or `AWS_ENDPOINT_URL` isn't set, aioboto3 goes to real AWS and rejects `test`/`test`. See Section 17.11.
3. **Postgres not reachable** — Docker container not running. `docker ps | grep postgres`.

### uvicorn says `aioboto3` `InvalidClientTokenId` (Hit, Section 17.11)

The `.env` file has AWS values but they're not in `os.environ` for aioboto3. Either:

- Make sure `example/backend/main.py` has the `.env` lift block at the top (it should, since Phase 13).
- Or export AWS vars in the shell before `uvicorn`:
  ```bash
  export AWS_ENDPOINT_URL=http://localhost:4566
  export AWS_DEFAULT_REGION=ap-south-1
  export AWS_ACCESS_KEY_ID=test
  export AWS_SECRET_ACCESS_KEY=test
  ```

---

## HTTP errors

### `POST /payments/orders` returns 500 with `Razorpay 401: Authentication failed` (Hit)

Razorpay rejected the API key. Two scenarios:

1. **Placeholder Razorpay keys in `.env`** (`rzp_test_REPLACE_ME`). Replace with real test keys from https://dashboard.razorpay.com → Settings → API Keys.
2. **Stale `RAZORPAY_KEY_ID` exported in your shell** overriding the `.env` value (Section 17.10). Run:
   ```bash
   for v in $(env | grep -E '^RAZORPAY_' | cut -d= -f1); do unset "$v"; done
   ```
   Restart uvicorn from the same shell.

Diagnose:

```bash
cd example/backend
uv run --project ../../razorpay_checkout python -c "
from razorpay_checkout import RazorpayConfig
print(RazorpayConfig().key_id)
"
```

If output ≠ what's in `.env`, you have leaked process env.

### `POST /payments/orders` returns 500 with `tenant_id ... not found in registry` (Predicted)

The order route uses `config.tenant_id` from env, not the body. Symptom = your `RAZORPAY_TENANT_ID` env / .env value doesn't match a tenant in the `tenants` table. Fix:

```bash
uv run --project razorpay_checkout python scripts/local-fixtures.py seed --tenant <your_tenant_id>
```

### `POST /payments/webhook/{tenant_id}` returns 400 `signature_mismatch` (Predicted)

HMAC verification failed. Three causes:

1. **Wrong `webhook_secret`** in `.env` vs. what signed the request. The simulator uses `dev_webhook_secret` by default; ensure your `.env` matches.
2. **Rotation in progress** and `webhook_secret_previous` is wrong/unset. See [`webhook-secret-rotation.md`](docs/runbooks/webhook-secret-rotation.md).
3. **Wrong tenant in URL path** — different tenants use different secrets; one tenant's signature won't validate at another tenant's URL.

Check logs in uvicorn console for `signature_mismatch` line with the offered signature and the expected fingerprint.

### `POST /payments/webhook/{tenant_id}` returns 400 with `unknown tenant` (Predicted)

URL path tenant id doesn't exist in `TenantConfigRegistry`. Path is publicly enumerable so 400 (not 404) prevents tenant existence leak. Fix: register the tenant via SSM or, for dev, add to `RAZORPAY_TENANT_ID` env.

### Order stays at `created` forever after a payment.captured webhook (Hit, multiple causes)

1. **Local pump not running.** Start `scripts/local-pump.py` in Terminal B. Check uvicorn logs for `Application startup complete.` and pump logs for `pump_ready`.
2. **Consumer rejected with `IllegalTransitionError: created -> paid`.** The auto-hop fix landed in Phase 13. Make sure your `webhook_handlers.py` has the `auto_attempted: true` branch in `handle_payment_captured`.
3. **SQS queue depth > 0 but pump not consuming.** Look at `webhook-events.fifo` `ApproximateNumberOfMessagesNotVisible` — if non-zero, the pump is receiving but failing; check pump logs.

Diagnose:

```bash
uv run --project razorpay_checkout python scripts/local-fixtures.py status
```

Webhook queue depth tells you which stage stalled.

### Order stuck at `paid`, never reaches `fulfilled` (Predicted)

The fulfillment lambda didn't run. See [`stuck-paid-order.md`](docs/runbooks/stuck-paid-order.md).

Locally: check that `local-pump.py` is running and processing both queues. The `fulfillment-queue.fifo` should drain to 0 within seconds of the consumer publishing.

---

## Database errors

### `column "updated_at" of relation "tenants" does not exist` (Hit)

`tenants` table has only `id, name, created_at`. The `updated_at` trigger doesn't fire on `tenants`. Don't include `updated_at` in INSERT/UPDATE. Use:

```sql
INSERT INTO tenants (id, name) VALUES ('local-dev', 'Local Dev Tenant')
ON CONFLICT (id) DO NOTHING;
```

### `Illegal transition X -> Y for order_id=...` (Hit)

State machine rejected the transition. See [ARCHITECTURE.md → State machine](ARCHITECTURE.md#state-machine) for the allowed graph. Common cases:

- `created -> paid` direct: handled by auto-hop in `handle_payment_captured` (Phase 13). If you see this raised, the auto-hop branch is missing.
- `attempted -> fulfilled` direct: you're publishing to the fulfillment queue too early. Fulfillment requires `paid` as the source state.
- `refunded -> anything`: terminal state; the only valid edge is `refunded` itself (no-op).

### `slow_pool_acquire` log lines (Predicted)

DB pool exhausted. See [`incident-checklist.md` → DB pool exhaustion](docs/runbooks/incident-checklist.md). Raise `RAZORPAY_POOL_MAX_SIZE` in `.env` and restart the Lambda/uvicorn.

### asyncpg returns `str` for a JSONB column (Hit, Section 17.8)

JSON codec wasn't registered. `create_pool` in `db.py` registers it at pool init. If you're using a non-standard pool factory, ensure `_register_codecs` runs in `init`.

---

## Idempotency / dedup oddities

### "Same event arrived 5 times concurrently and 5 paid transitions happened" (Predicted)

This shouldn't happen — UNIQUE on `(tenant_id, event_id)` plus FOR UPDATE on the order serialize it. If it does, you have either:

- A bug in the `INSERT ... ON CONFLICT` SQL (check `webhook_consumer.py`).
- Wrong isolation level (must be at least read-committed; the default).
- A race between the fast-path INSERT and the primary guard. Confirm both run inside one connection.

Reproduce: chaos test `test_concurrent_same_event_deduped` proves this works as designed.

### Razorpay retried a payment with a new `event_id` for the same payment, got double-fulfilled (Predicted)

The primary guard (event-type aware, runs after `SELECT ... FOR UPDATE`) catches this. If you see double-fulfillment, the guard isn't running. Inspect `webhook_consumer.py` `_run_in_transaction` and `webhook_idempotency.should_skip`.

Test asserting the invariant: dashboard scenario "Different event_id, same payment" plus pytest `test_webhook_consumer.py::test_duplicate_event_id_is_short_circuited`.

### Webhook arrives with wrong amount (Hit, scenario)

Amount mismatch is a terminal failure mode: webhook_events row marked `processing_status='failed'`, no state transition, no retry. Producer returns 200 to Razorpay (so they don't retry indefinitely). Alarm via `webhook_amount_mismatch_total` metric.

Confirm via dashboard scenario "Amount mismatch" (PASS).

---

## Correlation / tracing

### Three different correlation_ids across one event's lifecycle (Hit, Section 17.12)

`_bind_correlation_id` in `webhook_consumer.py` and `fulfillment.py` only handled the raw SQS dict shape (`{"StringValue": ...}`), not the flattened string. Custom pumps and most boto3 helpers flatten. Fix: handle both shapes (already shipped in Phase 13).

To verify the fix on a fresh run: dashboard → copy snapshot. The `created→attempted`, `attempted→paid`, `paid→fulfilled` rows must share a correlation_id; only `initializing→created` has a different one (order-creation trace, not webhook trace).

---

## Config / env

### Pydantic loads wrong values from `.env` (Hit, Section 17.10)

Process env beats `.env` in pydantic-settings. Check:

```bash
env | grep ^RAZORPAY_
```

If anything appears, it's overriding `.env`. Unset:

```bash
for v in $(env | grep -E '^RAZORPAY_' | cut -d= -f1); do unset "$v"; done
```

Look at `.bashrc` / `.bash_profile` / direnv for the source.

### AWS CLI fails with `NoRegion` (Hit)

`AWS_DEFAULT_REGION` not exported. Run the shell hygiene block from [COMMANDS.md](COMMANDS.md).

---

## Dashboard / UI

### Dashboard 11-scenario "Run all" — one scenario fails with `expected 401, got 400` (Hit)

Producer returns 400 (not 401) for bad signature. Updated scenario to accept any 4xx. If you have an old `dashboard.html`, pull the Phase 14 commit.

### Dashboard scenario stuck on `running` button (Predicted)

Backend or pump not responding. Open browser DevTools → Network tab → look at `/dev/flow/...` polling responses. Common causes:

- 500 from `/dev/flow/{order_id}` — order doesn't exist. Scenario assertion will time out.
- Slow pump. Check Terminal B for `pump_handler_error` lines.

### `/dev/shop` modal says "International cards not supported" (Hit)

Razorpay test account is India-only and refusing the test card 4111... Use:
- **UPI** tab → `success@razorpay`
- **Netbanking** → any bank → Success simulator button
- Domestic card: `5267 3181 8797 5449` Mastercard

### `/dev/shop` stuck on "Confirming your payment" (Hit)

Razorpay can't deliver webhooks to localhost. The frontend self-simulates after success (Phase 13 fix). If still stuck:

- Pump not running (Terminal B).
- Failure path: the storefront also self-simulates `payment.failed` since Phase 14 — confirm `shop.html` has that branch in `onRazorpayFailure`.

### `/dev/dashboard` returns 404 (Hit, Phase 17)

`RAZORPAY_DEV_ROUTES_ENABLED` is unset or `false`. Set it in
`example/backend/.env` (`RAZORPAY_DEV_ROUTES_ENABLED=true`) and restart
the backend. This flag must remain `false` in any deployed environment;
the admin dashboard is the prod-safe alternative.

### `/admin/dashboard` returns 403, modal won't dismiss (Hit, Phase 17)

The key you typed doesn't match `RAZORPAY_ADMIN_API_KEY`. The key
lives in `sessionStorage` and is cleared on tab close (DESIGN 17.15)
— re-open the env badge → paste the right key. If you don't have it,
read it out of `example/backend/.env`
(`RAZORPAY_ADMIN_API_KEY=local-dev-admin-key` by default in dev).

### Dashboard shows a red ribbon "Last refresh failed — data shown below may be stale" (Hit, Phase 17 fix)

The auto-refresh hit a network/backend error. The data on screen is
the last successful fetch, greyed out to make this obvious. Click
Refresh to retry. If it keeps failing: check the backend logs and the
local pump (Terminal B). The banner clears on the next successful
refresh.

### Dashboard state-graph shows a `refunded` order with grey "Paid" / "Fulfilled" lanes (Hit, Phase 17 fix)

Old `dashboard.html` walked the main-path array up to the current
`status` — `refunded` is off-path so prior states rendered grey. Pull
the Phase 17 commit; visited-state detection now reads `*_at`
timestamps. See DESIGN 17.14.

---

## DLQ-related

### Messages stuck in DLQ (Predicted)

See [`dlq-drain.md`](docs/runbooks/dlq-drain.md) for full triage.

Quick: classify whether it's a poison message (malformed envelope) or a transient failure that never recovered. Inspect with:

```bash
uv run --project razorpay_checkout python scripts/dlq-replay.py inspect webhook --index 0
```

Then **delete** (poison) or **replay** (transient).

### `dlq-replay.py inspect` shows messages but `delete --all --dry-run` says "no matching messages" (Hit, Section 17.13)

SQS `receive_message` locks the message for 30s by default. The CLI now releases visibility after read-only operations. If you see this on an older version of the script, pull the Phase 14 commit.

---

## CDK / synth

### `cdk synth` prints `Node 20 has reached end-of-life` warning (Hit)

Harmless. Silence with:

```bash
export JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=true
```

Long-term: upgrade Node to 22 LTS.

### CDK synth fails with `account is not set` (Predicted)

`cdk.json` has `REPLACE_WITH_ACCOUNT_ID` placeholder. Override via env var or edit `cdk.json`:

```bash
CDK_DEFAULT_ACCOUNT=123456789012 cdk synth
```

### `cdk deploy` for PaymentsStack fails with `ECR image :latest not found` (Predicted, Phase 17)

CI hasn't pushed the Lambda container images yet. Build and push from your machine:

```bash
# Phase 17 will ship docker build scripts. Until then:
docker buildx build --platform linux/amd64 -t razorpay-webhook-producer:latest .
docker tag razorpay-webhook-producer:latest <account>.dkr.ecr.ap-south-1.amazonaws.com/razorpay-webhook-producer:latest
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.ap-south-1.amazonaws.com
docker push <account>.dkr.ecr.ap-south-1.amazonaws.com/razorpay-webhook-producer:latest
# Repeat for consumer + fulfillment.
```

---

## When in doubt

1. **Snapshot the system state.** Dashboard → click "Copy snapshot" → paste somewhere.
2. **Check the runbook index.** [`docs/runbooks/`](docs/runbooks/) has 5 incident-focused playbooks.
3. **Read the relevant Section 17 entry** in DESIGN_v6 — we documented every non-obvious bug. 13 entries so far.
4. **`git log --oneline -10`** — the most recent commits often explain why the current code does what it does.
5. **Reproduce in the dashboard's scenario runner.** If a scenario regresses, an invariant broke.

## Related

- [COMMANDS.md](COMMANDS.md) — command reference.
- [docs/runbooks/](docs/runbooks/) — incident-response deep dives.
- [designs/DESIGN_v6.md](../designs/DESIGN_v6.md) — Section 17 captures every documented learning.
