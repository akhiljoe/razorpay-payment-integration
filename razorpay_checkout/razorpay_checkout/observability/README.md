# Observability assets

CloudWatch Logs Insights queries used in production runbooks.

## `logs_insights/webhook_trace.cwlogs`

End-to-end trace of a single webhook event. Replace `<CORRELATION_ID>` with
the UUID from the producer's 200 response header `X-Correlation-Id`, the
runbook ticket, or the operator-supplied value. Spans producer entry,
`webhook_events` insert, primary guard decision, transition, fulfillment
publish, and fulfillment handler completion.

Used by **Runbook 1: Stuck or Dead Webhook Event**.

## `logs_insights/dead_events_24h.cwlogs`

All `webhook_events` rows that the DLQ poller has marked `processing_status='dead'`
in the last 24 hours. Used as a backstop after a CloudWatch alarm fires on
`dead_event_count > 0`.

Used by **Runbook 1: Stuck or Dead Webhook Event** and the daily ops review.

## Updating these queries

Each query is a literal CloudWatch Logs Insights expression. To test locally
without CloudWatch: replicate the same filter against the JSON log lines
captured by `pytest` (see `tests/test_log_sanitization.py` for the JSON shape).

If the structured log schema changes, update both query files and the
runbook references in `runbooks/`.
