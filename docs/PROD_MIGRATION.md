# Prod migration — local → live for demo-tenant

Same code. Different config. Different runtime substrate.

## What changes

| Concern | Local | Prod |
|---|---|---|
| **Backend** | uvicorn on localhost:8000 | 3 Lambdas + 2 DLQ pollers behind API Gateway / Function URLs |
| **Postgres** | docker-compose | RDS Postgres (db.t4g.micro starts fine) |
| **SQS** | LocalStack | Real AWS SQS FIFO |
| **Razorpay mode** | `test` | `live` |
| **Webhook delivery** | Browser self-fires `/dev/simulate` | Razorpay's servers POST your real webhook URL |
| **`/dev/*` routes** | Enabled (`RAZORPAY_DEV_ROUTES_ENABLED=true`) | **DISABLED** — leave the env var unset |
| **Admin API key** | `local-dev-admin-key` | 32-byte random, in Secrets Manager |
| **CORS** | `localhost:5500` | `https://example.com,https://www.example.com` |
| **Tenant id** | `local-dev` | `demo-tenant` |
| **TLS** | none | required (Razorpay rejects non-HTTPS webhook URLs) |

## Step-by-step

### 1. AWS bootstrap (one-time)

Follow `docs/cloud/account-setup.md` then `docs/cloud/iam-bootstrap.md`. Provisions the deploy IAM role and CDK trust.

### 2. Provision RDS Postgres

`db.t4g.micro` in the same VPC as the Lambdas (or public-accessible if Free Track). Note the DSN:
```
postgresql://daj_app:<password>@<rds-endpoint>:5432/razorpay_checkout
```

### 3. Deploy the AWS stacks

```bash
cd infra
uv sync
npx cdk diff      # review
npx cdk deploy    # creates SQS, 3 Lambdas + 2 DLQ pollers + stale-paid monitor, IAM
```

Outputs include the webhook function URL — that's what you give Razorpay.

### 4. Set per-Lambda env vars (via Secrets Manager / SSM)

The CDK wires these to each Lambda. Set them once for the `demo-tenant` tenant:

```
RAZORPAY_TENANT_ID=demo-tenant
RAZORPAY_MODE=live
RAZORPAY_KEY_ID=rzp_live_xxxxxxxxxx
RAZORPAY_KEY_SECRET=<from Razorpay dashboard, generate "Live Key">
RAZORPAY_WEBHOOK_SECRET=<generate fresh; you'll paste this into Razorpay dashboard>
RAZORPAY_DB_DSN=postgresql://daj_app:...@rds.../razorpay_checkout
RAZORPAY_ADMIN_API_KEY=<32-byte random; openssl rand -hex 32>
RAZORPAY_OTEL_EXPORTER=xray
CORS_ORIGINS=https://example.com,https://www.example.com
```

**Do NOT set** `RAZORPAY_DEV_ROUTES_ENABLED`. Unset means `/dev/*` routes return 404.

### 5. Run migrations against RDS

```bash
cd razorpay_checkout
RAZORPAY_DB_DSN=postgresql://daj_app:...@rds.../razorpay_checkout \
  uv run alembic upgrade head
```

### 6. Seed the tenant row

```sql
INSERT INTO tenants (id, name) VALUES ('demo-tenant', 'Demo Store');
```

### 7. Register the webhook in Razorpay dashboard

https://dashboard.razorpay.com → Webhooks → Add New:

- **URL:** `https://<api-gateway-domain>/payments/webhook/demo-tenant`
- **Secret:** same value as `RAZORPAY_WEBHOOK_SECRET` from step 4
- **Active events:** `payment.authorized`, `payment.captured`, `payment.failed`, `refund.processed`, `order.paid`

### 8. Update demo-tenant/app.js (three constants)

```js
// demo-tenant/app.js, lines 207, 213, 216
API_BASE: "https://<api-gateway-domain>",        // was: http://localhost:8000
RAZORPAY_KEY_ID: "rzp_live_xxxxxxxxxx",          // was: rzp_test_XXXXXXXXXXXXXX
TENANT_ID: "demo-tenant",                       // was: local-dev
```

Deploy `demo-tenant/` to your static host (S3+CloudFront, Vercel, Netlify, whatever).

### 9. Smoke tests (run all of these before announcing)

| Check | Expected |
|---|---|
| `curl https://<api>/health` | `200 {"status":"ok"}` |
| `curl https://<api>/payments/webhook/unknown` | `400` with `unknown_tenant` (NOT 404 — prevents tenant enumeration) |
| `curl https://<api>/dev/dashboard` | `404` (dev routes correctly disabled) |
| `curl https://<api>/admin/dashboard-data` | `403` (admin key required) |
| `curl -H "X-Admin-Api-Key: $KEY" https://<api>/admin/kpis` | `200` with empty KPI counts |
| In Razorpay dashboard → Webhooks → click "Test webhook" | Appears within seconds in `/admin/dashboard` (with admin key) |
| Real ₹1 booking through example.com | Order walks `created → attempted → paid → fulfilled`. Refund it from Razorpay dashboard, verify status becomes `refunded`. |

### 10. Operational follow-ups

| Concern | Where |
|---|---|
| What to do if a webhook lands in DLQ | `docs/runbooks/dlq-drain.md` |
| Order stuck in `paid` for >15 min | `docs/runbooks/stuck-paid-order.md` (the stale-paid monitor alerts on this) |
| Rotating the webhook secret | `docs/runbooks/webhook-secret-rotation.md` |
| Adding a second tenant later | `docs/runbooks/tenant-onboarding.md` |
| Incident playbook | `docs/runbooks/incident-checklist.md` |

## What you do NOT need

- The `plugin/` folder from the earlier packaging exercise. Use the original project tree.
- `/dev/simulate`, `/dev/reset`, `/dev/dashboard` — Razorpay's real servers fire webhooks in prod.
- LocalStack, docker-compose.
- `scripts/local-pump.py`, `scripts/local-fixtures.py`, `scripts/razorpay-simulator.py` — dev/ops only.

## What you DO want to keep handy

- `scripts/dlq-replay.py` — operational, for replaying messages out of a real DLQ.
- `scripts/reconcile-diff.py` — operational, for reconciling Razorpay payments vs your DB.
- `scripts/check-stuck-paid.py` — operational, for spotting orders stuck pre-fulfillment.
- `scripts/prod-smoke.py` — operational, for periodic prod health checks.

Run these against the prod environment via a bastion or admin role; they all refuse non-localhost endpoints unless you pass `--allow-production`.
