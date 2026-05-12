"""Tests for HMAC-SHA256 signature utilities.

Test vectors here are canonical and shared with `scripts/razorpay-simulator.py`
(to be added in a later phase). The dev secret value is used for both halves
so the simulator and the unit tests stay in lock-step.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from razorpay_checkout.signatures import (
    verify_payment_signature,
    verify_webhook_signature,
)

WEBHOOK_SECRET = "dev_webhook_secret"
PREVIOUS_SECRET = "dev_webhook_secret_previous"
KEY_SECRET = "dev_key_secret"


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# ---------- verify_webhook_signature ----------


def test_valid_signature_returns_true() -> None:
    body = b'{"event":"payment.captured"}'
    signature = _sign(body, WEBHOOK_SECRET)
    assert verify_webhook_signature(body, signature, WEBHOOK_SECRET) is True


def test_tampered_body_fails() -> None:
    body = b'{"event":"payment.captured"}'
    signature = _sign(body, WEBHOOK_SECRET)
    tampered = b'{"event":"payment.failed"}'
    assert verify_webhook_signature(tampered, signature, WEBHOOK_SECRET) is False


def test_wrong_secret_fails() -> None:
    body = b'{"event":"payment.captured"}'
    signature = _sign(body, WEBHOOK_SECRET)
    assert verify_webhook_signature(body, signature, "the_wrong_secret") is False


def test_empty_body_with_valid_signature_for_empty_body() -> None:
    body = b""
    signature = _sign(body, WEBHOOK_SECRET)
    assert verify_webhook_signature(body, signature, WEBHOOK_SECRET) is True


def test_empty_signature_fails() -> None:
    body = b'{"event":"payment.captured"}'
    assert verify_webhook_signature(body, "", WEBHOOK_SECRET) is False


def test_previous_secret_fallback_accepts_old_signature() -> None:
    body = b'{"event":"payment.captured"}'
    signature_with_previous = _sign(body, PREVIOUS_SECRET)
    assert (
        verify_webhook_signature(
            body, signature_with_previous, WEBHOOK_SECRET, PREVIOUS_SECRET
        )
        is True
    )


def test_previous_secret_fallback_rejects_invalid_signature() -> None:
    body = b'{"event":"payment.captured"}'
    assert (
        verify_webhook_signature(
            body, "deadbeef" * 8, WEBHOOK_SECRET, PREVIOUS_SECRET
        )
        is False
    )


def test_no_previous_secret_no_fallback() -> None:
    body = b'{"event":"payment.captured"}'
    sig = _sign(body, PREVIOUS_SECRET)
    assert verify_webhook_signature(body, sig, WEBHOOK_SECRET, None) is False


def test_current_secret_wins_when_both_match_current() -> None:
    body = b'{"event":"payment.captured"}'
    signature = _sign(body, WEBHOOK_SECRET)
    assert (
        verify_webhook_signature(body, signature, WEBHOOK_SECRET, PREVIOUS_SECRET)
        is True
    )


def test_bytes_secret_accepted() -> None:
    body = b'{"event":"payment.captured"}'
    signature = _sign(body, WEBHOOK_SECRET)
    assert verify_webhook_signature(body, signature, WEBHOOK_SECRET.encode("utf-8")) is True


def test_raw_body_must_be_bytes() -> None:
    with pytest.raises(TypeError):
        verify_webhook_signature("not bytes", "sig", WEBHOOK_SECRET)  # type: ignore[arg-type]


# ---------- verify_payment_signature ----------


def test_valid_payment_signature_returns_true() -> None:
    order_id = "order_xxx"
    payment_id = "pay_yyy"
    payload = f"{order_id}|{payment_id}".encode()
    signature = hmac.new(KEY_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    assert verify_payment_signature(order_id, payment_id, signature, KEY_SECRET) is True


def test_mismatched_payment_ids_fail() -> None:
    order_id = "order_xxx"
    payment_id = "pay_yyy"
    payload = f"{order_id}|{payment_id}".encode()
    signature = hmac.new(KEY_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    assert verify_payment_signature("order_zzz", payment_id, signature, KEY_SECRET) is False
    assert verify_payment_signature(order_id, "pay_zzz", signature, KEY_SECRET) is False


def test_wrong_key_secret_fails() -> None:
    order_id = "order_xxx"
    payment_id = "pay_yyy"
    payload = f"{order_id}|{payment_id}".encode()
    signature = hmac.new(KEY_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    assert verify_payment_signature(order_id, payment_id, signature, "wrong") is False


def test_empty_field_fails() -> None:
    assert verify_payment_signature("", "pay", "sig", KEY_SECRET) is False
    assert verify_payment_signature("order", "", "sig", KEY_SECRET) is False
    assert verify_payment_signature("order", "pay", "", KEY_SECRET) is False


# ---------- Canonical test vector ----------
# Shared with scripts/razorpay-simulator.py.
# If you change any constant here, update the simulator too.


def test_canonical_webhook_vector() -> None:
    body = (
        b'{"event":"payment.captured","payload":'
        b'{"payment":{"entity":{"id":"pay_VECTOR","amount":50000}}}}'
    )
    secret = "canonical_dev_secret"
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, expected, secret) is True


def test_canonical_payment_vector() -> None:
    order_id = "order_CANONICAL"
    payment_id = "pay_CANONICAL"
    key_secret = "canonical_key_secret"
    payload = f"{order_id}|{payment_id}".encode()
    expected = hmac.new(key_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    assert verify_payment_signature(order_id, payment_id, expected, key_secret) is True
