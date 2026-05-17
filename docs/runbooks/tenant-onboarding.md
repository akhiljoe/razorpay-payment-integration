# Runbook: Tenant onboarding

Add a new tenant to the platform. Roughly 30 minutes end-to-end. Pre-requisites: the tenant has their own Razorpay merchant account.

## Pre-flight

Collect from the tenant:

- **`tenant_id`** — a stable identifier you choose, ASCII, lowercase, hyphen-separated. Example: `acme-india`. Will appear in webhook URLs and logs forever; pick carefully.
- **Razorpay key_id** — `rzp_live_XXX` or `rzp_test_XXX`.
- **Razorpay key_secret** — the 24-char companion to key_id.
- **Tenant display name** — for the `tenants.name` column.
- **Mode** — `test` or `live`.

Optional but recommended:

- **Admin contact email** — for incident notifications.
- **Expected daily transaction volume** — to right-size pool min/max and Lambda concurrency.

## Steps

### 1. SSM parameters

Store each secret in SSM Parameter Store. **Encrypted at rest, restricted IAM**. Path convention: `/razorpay/<tenant_id>/<key>`.

```bash
TENANT=acme-india

aws ssm put-parameter --name /razorpay/$TENANT/key_id \
  --type SecureString --value "rzp_live_XXX"

aws ssm put-parameter --name /razorpay/$TENANT/key_secret \
  --type SecureString --value "YYY"

# Generate a fresh webhook secret (you'll paste this into Razorpay dashboard later)
NEW_SECRET=$(openssl rand -base64 32 | tr -d '+/' | head -c 32)
aws ssm put-parameter --name /razorpay/$TENANT/webhook_secret \
  --type SecureString --value "$NEW_SECRET"

aws ssm put-parameter --name /razorpay/$TENANT/mode \
  --type String --value "live"
```

`webhook_secret_previous` stays unset until the first rotation. The verification code handles `None` gracefully.

### 2. Database row

```sql
INSERT INTO tenants (id, name)
VALUES ('acme-india', 'Acme India Pvt Ltd');
```

Single source of truth for tenant existence. Every tenant-scoped query joins or filters against this.

### 3. Razorpay dashboard configuration

In the tenant's Razorpay dashboard:

1. **Settings → Webhooks → Add new webhook**
2. URL: `https://api.example.com/payments/webhook/acme-india` (path must match `tenant_id`).
3. Secret: paste `$NEW_SECRET` from step 1.
4. **Events to subscribe** (these are the only ones the consumer handles):
   - `payment.authorized`
   - `payment.captured`
   - `payment.failed`
   - `refund.processed`
   - `order.paid` (log-only, optional but recommended for forensics)
5. Activate.

### 4. Tenant config registry reload

The `TenantConfigRegistry` is loaded at Lambda cold start from SSM. Force a refresh:

```bash
aws lambda update-function-configuration \
  --function-name razorpay-webhook-producer \
  --environment 'Variables={TENANT_REFRESH='$(date +%s)'}'

aws lambda update-function-configuration \
  --function-name razorpay-webhook-consumer \
  --environment 'Variables={TENANT_REFRESH='$(date +%s)'}'
```

(Tenant lookup at request time is in the producer; the consumer reads tenant config on demand via the same registry.)

### 5. Smoke test

In test mode first. Skip if onboarding direct to live.

```bash
# A) Create an order through your application or directly:
curl -fsS -X POST https://api.example.com/payments/orders \
  -H "content-type: application/json" \
  -H "X-Tenant-Id: acme-india" \
  -d '{"receipt": "onboard-smoke-001"}'

# Capture the razorpay_order_id from the response.

# B) Fire a synthetic webhook (test mode only — must match the dashboard secret):
RAZORPAY_WEBHOOK_SECRET=$NEW_SECRET \
  python scripts/razorpay-simulator.py payment.captured \
    --tenant acme-india \
    --order-id order_XXX \
    --payment-id pay_smoke_001 \
    --amount 50000 \
    --url https://api.example.com/payments/webhook
```

Expect 200 from the producer. Within ~3 seconds the order should reach `status=fulfilled`. Verify in DB:

```sql
SELECT status, paid_at, fulfilled_at FROM orders
 WHERE razorpay_order_id = 'order_XXX';
```

### 6. Verify isolation

Run the smoke against a different tenant's URL with this tenant's signature — must fail signature verification (or unknown tenant). Documents the multi-tenant boundary works.

```bash
curl -i -X POST https://api.example.com/payments/webhook/some-other-tenant \
  -H "x-razorpay-signature: <one_signed_for_acme_india>" \
  -d '{...}'
# Expect 4xx
```

## Rollback (remove a tenant)

Live tenants should never be deleted while orders exist. Soft-remove instead:

1. **Razorpay dashboard:** deactivate the webhook.
2. **SSM:** delete the four parameters (`/razorpay/$TENANT/*`). Lambda will reject new webhooks for the tenant with 4xx `unknown tenant`.
3. **Leave the DB row.** Historical orders, payments, refunds need the tenant for FK referential integrity (logical FK, not enforced at DB level — but still load-bearing for joins).

Hard-delete only after legal/data retention requirements are met, typically 7 years for payment data.

## What can go wrong

- **`tenant_id` mismatch between URL and SSM key.** Producer returns 4xx `unknown tenant`. Fix: align casing and path-segment encoding.
- **Razorpay dashboard webhook secret typo.** All webhooks return 4xx `signature_mismatch`. Detect via the `signature_mismatch_total` metric. Re-paste the secret.
- **Forgot to insert tenants row.** First captured payment will fail FK-style lookups (logical FK in app code). Insert the row.
- **`mode=live` set with test keys.** Razorpay returns 401 on order creation. Either flip to `mode=test` or supply live keys.

## Prevent

- **Use a tenant onboarding form** that produces the SSM commands and the SQL INSERT. Reduces typos.
- **Automate the dashboard webhook config** via Razorpay's API if you onboard frequently.

## Related

- DESIGN_v6 Section 14 — multi-tenant architecture.
- `.claude/rules/tenant-isolation.md` — the invariants this runbook supports.
- `webhook-secret-rotation.md` for the next rotation in 90 days.
