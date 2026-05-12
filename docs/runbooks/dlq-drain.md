# Runbook: DLQ drain

When the on-call alarm fires for `webhook-events-dlq.fifo` or `fulfillment-dlq.fifo` at `ApproximateNumberOfMessages > 0`.

## Symptom

Either DLQ has messages. CloudWatch alarm `webhook-events-dlq-not-empty` or `fulfillment-dlq-not-empty` is in `ALARM`. PagerDuty pages on-call.

## Detect

```bash
aws sqs get-queue-attributes \
  --queue-url <dlq-url> \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

Or in dev:

```bash
python scripts/dlq-replay.py list webhook
python scripts/dlq-replay.py list fulfillment
```

## Diagnose

A message landed in a DLQ for one of three reasons. Inspect the body to classify:

```bash
python scripts/dlq-replay.py inspect webhook --index 0
```

1. **Poison message — malformed envelope.** Body fails JSON parse or is missing required fields. Almost always indicates a producer bug or a Razorpay API version mismatch. CloudWatch logs in the consumer will show repeated `ConsumerEnvelopeError` lines.
2. **Transient downstream failure that never recovered.** Body is well-formed and matches a valid event. Consumer logs show repeated exceptions from `_run_in_transaction`. Common causes: DB pool exhaustion sustained > 5 retries, Razorpay API outage during a refund cross-check, asyncpg connection drops.
3. **Bug in handler code.** Body is well-formed, no obvious downstream signal. Consumer logs show a Python traceback that recurred on each retry.

Always cross-reference with `webhook_events` table when the body has an `event_id`:

```sql
SELECT id, processing_status, event, received_at
  FROM webhook_events
 WHERE event_id = '<from body>';
```

`processing_status='pending'` confirms the consumer never committed. `processing_status='failed'` means the consumer reached the amount-mismatch or similar terminal branch and the DLQ landing is a separate fluke (rare).

## Resolve

**Poison message** → delete. There's no recovery; replaying just routes it back to DLQ.

```bash
python scripts/dlq-replay.py delete webhook --message-id <id> --dry-run
# review output, then drop --dry-run
python scripts/dlq-replay.py delete webhook --message-id <id>
```

Also file an incident ticket for the producer bug. Attach the body.

**Transient failure that has cleared** → replay.

```bash
# 1. Confirm the downstream is healthy first
curl -fsS $RAZORPAY_HEALTH_URL || echo "Razorpay still degraded"
# Check DB connection counts vs pool max_size

# 2. Replay
python scripts/dlq-replay.py replay webhook --message-id <id> --dry-run
python scripts/dlq-replay.py replay webhook --message-id <id>
```

The consumer's primary idempotency guard catches re-processing. If the original failure left a partial transition, the guard will skip it cleanly.

**Bug in handler** → do **not** replay until the bug is fixed and deployed. Either:

- Park the message (leave it in DLQ; alarm stays red).
- Snapshot the body, delete from DLQ, file a bug. The order in the DB will sit until you manually transition it (use the admin route `POST /payments/admin/events/{event_id}/replay` after the fix lands).

## Bulk drain after an incident

After a confirmed downstream outage that DLQ'd many messages:

```bash
# Dry-run first to scope the blast radius.
python scripts/dlq-replay.py replay webhook --all --dry-run | tee /tmp/dlq-replay.log
wc -l /tmp/dlq-replay.log

# Replay
python scripts/dlq-replay.py replay webhook --all
```

Don't replay > 1000 messages in one shot. SQS FIFO has 300 tx/s per MessageGroupId. Batch by tenant or by hour if needed.

## Prevent

- **Maintain producer/consumer envelope versioning.** Bumping `envelope_version` should be a coordinated rollout, not a unilateral producer change.
- **Add a synthetic poison-message regression test** for any envelope schema change.
- **Set the alarm threshold to depth > 0**, not depth > N. A single DLQ landing is an incident.
- **Keep `maxReceiveCount` at 5.** Higher and you'll mask real outages with retry storms; lower and transient blips DLQ unnecessarily.

## Related

- `.claude/rules/webhook-idempotency.md` — why replay is safe.
- DESIGN_v5 Section 6 — DLQ wiring.
- Runbook `incident-checklist.md` for queue-depth incident triage.
