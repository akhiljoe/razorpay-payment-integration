# Canonical HMAC test vectors

Single source of truth for HMAC-SHA256 signature contracts in this project.
The Webhook Producer, `scripts/razorpay-simulator.py`, and
`tests/test_signatures.py` all reference these vectors. **A change to any one
must update all three** or the simulator and the production code drift apart
silently.

The signature contract: `HMAC-SHA256(secret, raw_body)` rendered as a
lowercase hex digest. `hmac.compare_digest` is used for the equality check
(constant-time).

## Webhook signature

Computed over the raw HTTP request body bytes and the tenant's `webhook_secret`.

### Vector W1: minimal payment.captured

| Field | Value |
|---|---|
| `secret` | `canonical_dev_secret` |
| `body` (bytes) | `{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_VECTOR","amount":50000}}}}` |
| Expected hex digest | Computed at runtime in `tests/test_signatures.py::test_canonical_webhook_vector` |

Producer accepts when `X-Razorpay-Signature == HMAC-SHA256(secret, body).hex()`.

### Rotation: previous-secret fallback

When `webhook_secret_previous` is set, the producer also accepts signatures
computed under the previous secret. Tested in
`test_previous_secret_fallback_accepts_old_signature`.

| Current secret | Previous secret | Body signed with | Acceptance |
|---|---|---|---|
| `new_secret` | `old_secret` | `old_secret` | accepted |
| `new_secret` | None | `old_secret` | rejected |
| `new_secret` | `old_secret` | unrelated | rejected |

## Payment-callback signature

Computed by Razorpay's client over `f"{razorpay_order_id}|{razorpay_payment_id}"`
with the tenant's `key_secret`.

### Vector P1: client callback verification

| Field | Value |
|---|---|
| `key_secret` | `canonical_key_secret` |
| `razorpay_order_id` | `order_CANONICAL` |
| `razorpay_payment_id` | `pay_CANONICAL` |
| Signed string | `order_CANONICAL|pay_CANONICAL` |
| Expected hex digest | Computed at runtime in `tests/test_signatures.py::test_canonical_payment_vector` |

`verify_payment_signature(order_id, payment_id, signature, key_secret)`
returns `True` iff the signature matches.

## Simulator wire format

`scripts/razorpay-simulator.py` builds Razorpay-shaped event payloads with the
following top-level structure:

```json
{
  "id": "evt_<uuid>",
  "entity": "event",
  "event": "<event_type>",
  "created_at": <unix_seconds>,
  "contains": [...],
  "payload": { ...event-specific entities... }
}
```

The signature is computed over the serialised JSON body (compact, no
whitespace) and sent as `X-Razorpay-Signature`. The producer fast-parses the
same body to extract `id`, `event`, and the entity's `order_id`.

### Sample invocation

```bash
python scripts/razorpay-simulator.py payment.captured \
    --tenant t_dev \
    --order-id order_TEST_001 \
    --payment-id pay_TEST_001 \
    --amount 50000 \
    --secret dev_webhook_secret \
    --url http://localhost:8000/payments/webhook
```

Expected response: HTTP 200 with body `{"status":"queued","correlation_id":"<uuid>"}`.

### Replay behavior

Re-running the same command with the same `--event-id` (use the value the
simulator prints back) produces the same `MessageDeduplicationId`, which SQS
FIFO suppresses inside its 5-minute dedup window. Inside that window, the
second POST returns 200 but the consumer never sees the duplicate message.

## When to update this file

Update this file in the **same commit** as any change to:

- `razorpay_checkout/signatures.py` (signature algorithm or comparison)
- `razorpay_checkout/webhook_producer.py` (signature header name or body
  pre-parse shape)
- `scripts/razorpay-simulator.py` (payload structure or signing input)
- `tests/test_signatures.py` (canonical vector constants)

A drift between any two of these breaks local development and CI silently.
