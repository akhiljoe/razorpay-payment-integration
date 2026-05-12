"""HMAC-SHA256 signature verification for Razorpay webhooks and callbacks.

Two surfaces:

1. `verify_webhook_signature` runs in the Webhook Producer. Tries the current
   `webhook_secret` first, falls back to `webhook_secret_previous` if set.
   This supports zero-downtime secret rotation (DESIGN Runbook 7). All
   comparisons use `hmac.compare_digest` to defeat timing oracles.

2. `verify_payment_signature` runs in `POST /payments/verify-callback`. The
   payload is the literal `f"{order_id}|{payment_id}"`. Same constant-time
   compare semantics.

Both functions accept the secret as `str` or `bytes` and the signature as
hex-encoded `str` to match what Razorpay sends in `X-Razorpay-Signature`.
"""

from __future__ import annotations

import hashlib
import hmac


def _to_bytes(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _hmac_sha256_hex(secret: str | bytes, body: bytes) -> str:
    return hmac.new(_to_bytes(secret), body, hashlib.sha256).hexdigest()


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


def verify_webhook_signature(
    raw_body: bytes,
    header_signature: str,
    webhook_secret: str | bytes,
    webhook_secret_previous: str | bytes | None = None,
) -> bool:
    """Return True if `header_signature` matches HMAC-SHA256 over `raw_body`.

    Tries the current secret first. If that fails and `webhook_secret_previous`
    is set, retries with the previous secret. Always runs both comparisons to a
    fixed-length finish (no early return on first equality) to keep the timing
    profile uniform across hit and miss.
    """
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")
    if not header_signature:
        return False

    expected_current = _hmac_sha256_hex(webhook_secret, raw_body)
    current_ok = _constant_time_equal(expected_current, header_signature)

    if current_ok:
        return True

    if webhook_secret_previous is None:
        return False

    expected_previous = _hmac_sha256_hex(webhook_secret_previous, raw_body)
    return _constant_time_equal(expected_previous, header_signature)


def verify_payment_signature(
    order_id: str,
    payment_id: str,
    signature: str,
    key_secret: str | bytes,
) -> bool:
    """Return True if `signature` is the HMAC-SHA256 of `order_id|payment_id`.

    Razorpay's client-side success callback delivers this triple to the host.
    The route handler at `POST /payments/verify-callback` calls this to confirm
    the callback was issued by Razorpay before reading the order from the DB.
    """
    if not (order_id and payment_id and signature):
        return False
    payload = f"{order_id}|{payment_id}".encode()
    expected = _hmac_sha256_hex(key_secret, payload)
    return _constant_time_equal(expected, signature)
