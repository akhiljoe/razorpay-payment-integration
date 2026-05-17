# Runbook: Generic incident checklist

First 15 minutes when something is on fire and you don't yet know what.

## North star

In rough order of importance:

1. **Don't make it worse.** No production writes until you understand the scope.
2. **Stabilize.** Stop new damage. Pause traffic, scale, or roll back if needed.
3. **Communicate.** Customer trust is harder to rebuild than the system.
4. **Investigate.** Root cause comes after stabilization, never before.

## First 60 seconds — orient

Open three tabs:

1. **CloudWatch Dashboard `razorpay-overview`** — queue depths, Lambda concurrency, p99 latencies, error rates.
2. **Grafana `razorpay-business`** — payment volume, success rate, fulfillment rate.
3. **Razorpay status page** — `https://status.razorpay.com`. Razorpay outages cascade.

Check `signature_mismatch_total`. A spike here while everything else looks normal is a **separate** incident class — see "Signature failure spike" below.

## Top failure modes and 15-minute actions

### A. `webhook-events.fifo` depth growing

**Likely:** consumer Lambda concurrency limit hit, DB pool exhausted, or Razorpay sending a retry storm.

```
Check:  ApproximateNumberOfMessages on webhook-events.fifo
        Consumer Lambda ConcurrentExecutions vs reserved limit
        slow_pool_acquire log lines (DB)
```

**Actions:**

1. **Raise Lambda reserved concurrency** if you're hitting the limit. CloudWatch shows `Throttles > 0`.
2. **Scale DB read replica or pool max_size** if `slow_pool_acquire` is firing. Restart Lambdas after raising `RAZORPAY_POOL_MAX_SIZE`.
3. If neither helps, **pause Razorpay retries** by setting the webhook URL to a holding endpoint in dashboard. Drain. Re-enable.

### B. `fulfillment-queue.fifo` depth growing

**Likely:** downstream fulfillment hook is slow/broken, or the Lambda is throttled.

See [`stuck-paid-order.md`](stuck-paid-order.md) — that runbook is the specific case of this incident class.

### C. DLQ has messages

See [`dlq-drain.md`](dlq-drain.md). Treat any DLQ landing as a P2 minimum.

### D. Razorpay 5xx spike during order creation

**Symptom:** `create_order_503` log lines, customers seeing "Cannot start payment".

**Action:**

1. Check Razorpay status page. If degraded, **set a status banner** in the storefront ("Payment provider experiencing issues — please retry").
2. The producer already returns 503 to the caller; customers will retry. **Do not retry server-side** — that masks the outage.
3. If Razorpay is healthy on their side, suspect **expired credentials**. Verify SSM `key_id` and `key_secret` for the affected tenant.

### E. Signature failure spike (`signature_mismatch_total`)

**Likely:** secret rotation mid-flight, attacker probing, or Razorpay re-signed retries with old secret.

```
Check:  rate of signature_mismatch_total
        recent rotation activity in audit log
        source IPs of failing requests
```

**Actions:**

1. **During rotation:** expected; see [`webhook-secret-rotation.md`](webhook-secret-rotation.md). Confirm `webhook_secret_previous` is set.
2. **No recent rotation, mismatches from Razorpay source IPs:** treat as P1. Rotate the secret immediately (compromise scenario).
3. **Mismatches from random IPs:** likely scanning. Confirm rate-limiting at the edge. Add WAF rule if sustained.

### F. DB pool exhaustion

**Symptom:** `slow_pool_acquire` log lines piling up; consumer/fulfillment Lambdas timing out.

**Action:**

1. Check `pg_stat_activity` for stuck queries:
   ```sql
   SELECT pid, state, wait_event, NOW() - query_start AS dur, query
     FROM pg_stat_activity
    WHERE state != 'idle'
    ORDER BY dur DESC LIMIT 20;
   ```
2. Long `SELECT ... FOR UPDATE` waits → row contention. Identify the order, decide if you can kill the holder.
3. Long `idle in transaction` → connection leak. Restart the offending Lambda.
4. If RDS instance is CPU-saturated, scale up. Pool size > connection limit will not help; it just queues earlier.

### G. Order status stuck (any status)

Pick the matching runbook:

- Stuck at `paid` → [`stuck-paid-order.md`](stuck-paid-order.md).
- Stuck at `attempted` → webhook never arrived. Check producer logs for that order's tenant.
- Stuck at `created` → customer never paid (modal dismissed) or webhook lost. Use admin replay if you have proof of payment.

## Stabilize toolbox

Tools you can reach for without code changes:

| Action | Effect | Risk |
|---|---|---|
| `aws lambda update-function-configuration --reserved-concurrent-executions N` | Cap blast radius from a runaway Lambda | None — cleanly drops new invocations |
| Razorpay dashboard → deactivate webhook | Stop the firehose | Backlog accumulates at Razorpay (retried later) |
| Force Lambda cold start via env var bump | Refresh cached SSM, config, or pool | Cold-start latency spike for ~1 min |
| `aws sqs change-message-visibility-batch ... --visibility-timeout 600` | Park a problem message for 10 minutes | Re-delivery delayed |
| Database read-replica failover | Switch reads to a replica during writer issues | Replication lag exposure |

## After stabilization

1. **Snapshot the state.** Queue depths, Lambda metrics, recent failed orders, correlation_ids. Future-you needs these.
2. **Write an incident summary** within 24 hours. Timeline, what worked, what didn't.
3. **File follow-ups for hidden assumptions.** Anything you said "I thought X but actually Y" goes into the design doc Section 17.
4. **Update this runbook** if the playbook missed something.

## What never to do

- **Never bypass signature verification** to clear a backlog. Even with `force_bad_signature` in the dev simulator, production must never accept unsigned webhooks.
- **Never delete from `orders`** to clean up. Use status transitions and admin routes. Deletes break the correlation chain and reconciliation queries.
- **Never push a hotfix straight to live** without running the dashboard scenario suite first. The 11 scenarios catch invariant breaks in seconds.
- **Never share secret values in chat or ticket descriptions.** Use SSM paths instead.

## Related

- `dlq-drain.md`
- `stuck-paid-order.md`
- `webhook-secret-rotation.md`
- DESIGN_v6 Section 6 — failure modes by component.
