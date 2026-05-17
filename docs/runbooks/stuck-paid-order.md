# Runbook: Stuck paid order

When an order has been in `status=paid` longer than the SLO (default 15 minutes) without reaching `fulfilled` or `fulfillment_failed`.

## Symptom

CloudWatch alarm `stale-paid-orders` fires. Customer support reports "I paid but my order shows as pending." The `paid_orders_stale` metric is non-zero.

## Detect

The `stale-paid-monitor` Lambda emits the count on schedule. To check by hand:

```sql
SELECT id, razorpay_order_id, paid_at, NOW() - paid_at AS stuck_for, correlation_id
  FROM orders
 WHERE status = 'paid'
   AND paid_at < NOW() - INTERVAL '15 minutes'
 ORDER BY paid_at;
```

## Diagnose

A paid order means the **consumer Lambda** finished its work cleanly: DB committed, fulfillment message published to `fulfillment-queue.fifo`. The hand-off to the **fulfillment Lambda** is where it broke. Three causes, in order of likelihood:

1. **Fulfillment-queue depth elevated.** Lambda is rate-limited, downstream hook is slow, or the function is being throttled by reserved concurrency. Check `ApproximateNumberOfMessagesNotVisible` on the fulfillment queue.

2. **Fulfillment DLQ landings.** Message exhausted `maxReceiveCount=5` and routed to `fulfillment-dlq.fifo`. See [`dlq-drain.md`](dlq-drain.md).

3. **Consumer published fulfillment message failed silently.** The SQS publish happens after DB commit (Critical Invariant 8). If the publish errored after commit, the consumer logs a `fulfillment_sqs_publish_failed` and the order is stranded. The `stale-paid-monitor` is the safety net for exactly this case.

Pull the correlation_id and search logs:

```sql
SELECT correlation_id FROM orders WHERE id = '<order_id>';
```

```bash
aws logs start-query \
  --log-group-name /aws/lambda/razorpay-fulfillment \
  --query-string "fields @timestamp, @message | filter correlation_id = '<cid>'"
```

If you see **no fulfillment Lambda logs** for that correlation_id, the publish never landed → cause (3). If you see logs but no `fulfillment_completed` → cause (2). If logs show `fulfillment_short_circuit_already_fulfilled` but DB says paid, you have a DB replication lag issue worth a separate ticket.

## Resolve

**Cause 1 (queue backed up):** wait or scale Lambda reserved concurrency. No data action needed. Orders will drain.

**Cause 2 (DLQ landing):** follow [`dlq-drain.md`](dlq-drain.md). If the hook bug is fixed and deployed, replay the DLQ messages and they'll fulfill.

**Cause 3 (publish never happened):** use the admin retry route to manually re-trigger fulfillment:

```bash
curl -fsS -X POST https://api.example.com/payments/admin/orders/<order_id>/retry-fulfillment \
  -H "X-Admin-Api-Key: $RAZORPAY_ADMIN_API_KEY"
```

This re-acquires the row lock, re-publishes to fulfillment-queue, and returns the new message id. The order should reach `fulfilled` within seconds. The fulfillment processor short-circuits on `status=fulfilled` so if it somehow already happened concurrently, no double-fulfillment.

## Verify

```sql
SELECT status, paid_at, fulfilled_at, fulfilled_at - paid_at AS dwell
  FROM orders WHERE id = '<order_id>';
```

`fulfilled_at` should be populated, dwell time < 5 seconds in typical conditions.

## Prevent

- **`stale-paid-monitor` runs every 5 minutes** in production. Don't extend the interval without raising the SLO.
- **Watch the `fulfillment_sqs_publish_failures` metric.** Spikes there foreshadow stuck-paid alarms.
- **Keep `FulfillmentHook` idempotent.** Retries are expected; non-idempotent hooks cause duplicate downstream side effects.
- **Pre-warm the fulfillment Lambda** if your traffic has spiky patterns.

## Related

- `.claude/rules/webhook-idempotency.md` for short-circuit semantics.
- DESIGN_v6 Section 7 — fulfillment Lambda flow.
- `dlq-drain.md`
