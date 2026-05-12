# Runbook: Webhook secret rotation

Zero-downtime rotation of a tenant's `webhook_secret`. Use this when a secret leaks, on a scheduled cadence (every 90 days recommended), or when offboarding an integration.

## Symptom / Trigger

- **Reactive:** secret leaked in a log, support ticket, or git history. Rotate immediately.
- **Proactive:** scheduled rotation.
- **Defensive:** Razorpay support requested re-issue.

## Rotation sequence

The system supports two valid signing secrets at once: `webhook_secret` (active) and `webhook_secret_previous` (grace). HMAC verification tries the active first, falls back to previous. This is the load-bearing mechanism — do not break it.

### Step 1: Generate new secret

```bash
NEW_SECRET=$(openssl rand -base64 32 | tr -d '+/' | head -c 32)
echo "$NEW_SECRET"
# Capture in your password manager. Do NOT paste in chat.
```

### Step 2: Configure Razorpay dashboard

1. Sign in to Razorpay dashboard for the **target tenant's** account.
2. Settings → Webhooks → select the production webhook URL.
3. Click **Edit secret** → paste `$NEW_SECRET` → Save.
4. Razorpay starts signing future webhooks with the new secret **immediately**. In-flight retries of old webhooks will still use the old secret for the next ~24 hours.

### Step 3: Update SSM with both secrets

In SSM Parameter Store for the target tenant:

- `/razorpay/<tenant_id>/webhook_secret_previous` ← set to the current value of `/razorpay/<tenant_id>/webhook_secret`
- `/razorpay/<tenant_id>/webhook_secret` ← set to `$NEW_SECRET`

Both updates must commit before any Lambda restart. Otherwise a Lambda cold start between the two updates will reject old-secret-signed retries.

```bash
OLD_SECRET=$(aws ssm get-parameter --name /razorpay/$TENANT/webhook_secret --with-decryption --query Parameter.Value --output text)

aws ssm put-parameter \
  --name /razorpay/$TENANT/webhook_secret_previous \
  --type SecureString --value "$OLD_SECRET" --overwrite

aws ssm put-parameter \
  --name /razorpay/$TENANT/webhook_secret \
  --type SecureString --value "$NEW_SECRET" --overwrite
```

### Step 4: Force Lambda config refresh

The Webhook Producer caches SSM values. Either wait for the cache TTL (default 5 min) or force a reload:

```bash
aws lambda update-function-configuration \
  --function-name razorpay-webhook-producer \
  --environment 'Variables={ROTATION_BUMP='$(date +%s)'}'
```

The dummy env var changes the function version hash, forcing a cold start across all warm containers.

### Step 5: Validate

Fire a synthetic webhook with the new secret and watch logs:

```bash
RAZORPAY_WEBHOOK_SECRET=$NEW_SECRET \
  python scripts/razorpay-simulator.py payment.captured \
    --tenant $TENANT \
    --order-id <a-known-test-order> \
    --payment-id pay_rotation_test \
    --url https://api.example.com/payments/webhook
```

Producer logs should show `signature_verified` and `webhook_event_published`. Status 200.

Now repeat with the OLD secret — it must also pass during the grace window:

```bash
RAZORPAY_WEBHOOK_SECRET=$OLD_SECRET \
  python scripts/razorpay-simulator.py payment.captured ...
```

Logs should show `signature_verified_via_previous_secret`. Status 200.

### Step 6: After 48 hours, retire the previous secret

48 hours is conservative — Razorpay's retry budget is typically < 24 hours. Set the previous to empty:

```bash
aws ssm put-parameter \
  --name /razorpay/$TENANT/webhook_secret_previous \
  --type SecureString --value "" --overwrite
```

Force Lambda refresh again as in step 4. Confirm OLD-secret webhooks now return 4xx with `signature_mismatch`.

## Rollback

If step 5 validation fails, revert SSM:

```bash
aws ssm put-parameter \
  --name /razorpay/$TENANT/webhook_secret \
  --type SecureString --value "$OLD_SECRET" --overwrite

aws ssm put-parameter \
  --name /razorpay/$TENANT/webhook_secret_previous \
  --type SecureString --value "" --overwrite
```

Force Lambda refresh. Then re-paste the OLD secret in Razorpay dashboard.

Razorpay will retry any in-flight webhooks signed during the bad window; they'll succeed once the rollback completes.

## What can go wrong

- **Cache not flushed.** Lambda still holds the old SSM values. Symptom: signature failures for OLD-secret webhooks. Fix: re-run step 4 or wait the cache TTL.
- **Razorpay dashboard updated but SSM lagged.** New-secret webhooks rejected with `signature_mismatch`. Fix: complete step 3 ASAP. Razorpay will retry.
- **Two engineers rotating simultaneously.** SSM has no compare-and-swap on plain put-parameter. Use a Jira / Slack lock convention for the duration.

## Prevent

- **Calendar the rotation cadence** — 90 days is industry-standard.
- **Alert on `signature_mismatch_total` deviation**. A spike during rotation is expected; outside rotation windows, it indicates an attack or a misconfigured caller.
- **Never log the raw secret.** All secrets are `SecretStr`, but a careless `repr(config)` can still leak. The `register_secret` logging filter catches it; don't bypass.

## Related

- DESIGN_v5 Section 8 — signature verification path.
- `tenant-onboarding.md` for the initial secret setup.
