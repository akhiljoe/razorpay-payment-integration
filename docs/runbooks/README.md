# Operational runbooks

Operator-facing playbooks. Each follows the same structure: Symptom → Detect → Diagnose → Resolve → Prevent.

| Runbook | When to use |
|---|---|
| [incident-checklist.md](incident-checklist.md) | Generic on-call response. Start here when something is on fire and you don't yet know what. |
| [dlq-drain.md](dlq-drain.md) | Either DLQ has messages. Classify, replay, or delete. |
| [stuck-paid-order.md](stuck-paid-order.md) | Order at `paid` longer than 15 minutes without reaching `fulfilled`. |
| [webhook-secret-rotation.md](webhook-secret-rotation.md) | Scheduled or reactive rotation using `webhook_secret_previous` for zero downtime. |
| [tenant-onboarding.md](tenant-onboarding.md) | Add a new tenant end-to-end: SSM, tenants table, dashboard config, smoke test. |

## Related tooling

- `scripts/dlq-replay.py` — list/inspect/replay/delete messages in either DLQ. Refuses non-LocalStack endpoints unless `--allow-production` is passed.
- `scripts/local-fixtures.py` — status/seed/purge/reset for dev DB + SQS.
- `scripts/local-pump.py` — drains both queues into the real Consumer + Fulfillment processors for local end-to-end testing.

## Conventions

- **Localhost-safety:** all dev scripts refuse non-`localhost`/`127.0.0.1` endpoints by default. Use `--allow-production` for real AWS only after confirming intent.
- **Dry-run first:** any destructive operation (`delete`, `replay --all`, secret rotation) should be rehearsed with `--dry-run` before commit.
- **Correlation IDs:** every log line and DB row carries the correlation_id from the producer entry. Use it as the join key when reading logs across the consumer / fulfillment hop.
