# Webhook flow

Sequence diagram for one happy-path `payment.captured` event end to end.

```mermaid
sequenceDiagram
  participant RZP as Razorpay
  participant Prod as Producer Lambda
  participant WQ as webhook-events.fifo
  participant Cons as Consumer Lambda
  participant DB as Postgres
  participant FQ as fulfillment-queue.fifo
  participant Ful as Fulfillment Lambda
  participant Hook as FulfillmentHook

  RZP->>+Prod: POST /payments/webhook/{tenant_id}<br/>x-razorpay-signature: hmac(...)
  Note over Prod: 1. Lookup tenant in registry<br/>2. Verify signature (current, then previous)<br/>3. Generate correlation_id (UUIDv4)<br/>4. Build canonical envelope
  Prod->>+WQ: SendMessage<br/>MessageGroupId = razorpay_order_id<br/>MessageAttributes.correlation_id
  WQ-->>-Prod: MessageId
  Prod-->>-RZP: 200 queued<br/>(within 500ms)

  WQ->>+Cons: receive_message<br/>(event-source mapping)
  Note over Cons: 1. Re-bind correlation_id from MessageAttributes
  Cons->>+DB: INSERT INTO webhook_events ...<br/>ON CONFLICT (tenant_id, event_id) DO NOTHING
  Note over Cons: Fast-path dedup. If conflict: mark done, return.
  DB-->>-Cons: row_id
  Cons->>+DB: BEGIN
  Cons->>DB: SELECT * FROM orders WHERE razorpay_order_id = ? FOR UPDATE
  DB-->>Cons: order row (status=created OR attempted OR authorized)
  Note over Cons: Primary guard:<br/>skip if order at paid/fulfilled/etc<br/>AND captured payments row exists
  Note over Cons: Amount assertion:<br/>event_amount == order.amount
  alt order.status == created (auto-hop)
    Cons->>DB: UPDATE orders SET status='attempted'<br/>INSERT order_events (auto_attempted=true)
  end
  Cons->>DB: UPDATE orders SET status='paid', paid_at=NOW()<br/>INSERT order_events
  Cons->>DB: INSERT payments (..., is_authoritative=TRUE)
  Cons->>-DB: COMMIT
  Note over Cons: DB-before-side-effects (Critical Invariant 8)
  Cons->>+FQ: SendMessage<br/>MessageGroupId = razorpay_order_id<br/>MessageAttributes.correlation_id (preserved)
  FQ-->>-Cons: MessageId
  Cons-->>WQ: DeleteMessage (success)

  FQ->>+Ful: receive_message
  Note over Ful: Re-bind correlation_id from MessageAttributes
  Ful->>+DB: BEGIN
  Ful->>DB: SELECT * FROM orders WHERE id = ? AND tenant_id = ? FOR UPDATE
  DB-->>Ful: order row (status=paid)
  Note over Ful: Short-circuit if status=fulfilled (re-delivery)
  Ful->>+Hook: fulfill(order_id, tenant_id, amount, metadata)
  Note over Hook: Host code: send receipt, grant access,<br/>charge upstream, whatever fulfilment means.<br/>MUST be idempotent.
  Hook-->>-Ful: success
  Ful->>DB: UPDATE orders SET status='fulfilled', fulfilled_at=NOW()<br/>INSERT order_events
  Ful->>-DB: COMMIT
  Ful-->>FQ: DeleteMessage (success)
```

## Critical points

- **Producer ack is unconditional 200** as long as signature is valid. Even if SQS publish fails, we publish to OPS topic for ops to investigate.
- **Consumer commits before publishing to fulfillment-queue.** If publish errors after commit, the order is stranded in `paid`. The stale-paid monitor sweeps every 5 minutes for these.
- **Fulfillment's transaction wraps the hook call.** If the hook raises, the transaction rolls back and SQS will retry (up to `maxReceiveCount=5`). After 5 failures, DLQ.
- **Correlation_id survives both SQS hops** via MessageAttributes. Critical Invariant 9, was broken in early Phase 13 and fixed (Section 17.12).
- **Auto-hop** for `created → attempted` lives in the consumer, not the state machine. Lets server-to-server flows land cleanly without weakening the external state machine.

## Read next

- [system-architecture.md](system-architecture.md) — components view.
- [state-machine.md](state-machine.md) — the full status graph.
- [`.claude/rules/webhook-idempotency.md`](../../.claude/rules/webhook-idempotency.md) — formal idempotency contract.
