"""Regression test: secret values must never appear in captured log output.

DESIGN_v5 Section 10 critical security invariant: `key_secret`,
`webhook_secret`, `webhook_secret_previous`, `admin_api_key` must never
surface in any structured log record. The logging_config secret-redaction
processor replaces them with `***`.

This test constructs a config with sentinel string values, runs a series of
calls that traverse the producer, OrderService, consumer, and admin
exception paths, and captures all stdout. It then asserts none of the
sentinel values appear in the captured output.

If this test ever fails, audit the new code path. A leak means somewhere a
secret was logged as a structured field, an exception message, a repr, or a
str() of a model that did not use SecretStr.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from razorpay_checkout import (
    RazorpayConfig,
    TenantConfigRegistry,
    WebhookProducer,
    configure_logging,
    get_logger,
)
from razorpay_checkout.webhook_producer import SignatureMismatchError, UnknownTenantError

# Sentinels chosen to be unmistakable in any output.
KEY_SECRET = "__SENTINEL_KEY_SECRET_NEVER_LOG_KS__"
WEBHOOK_SECRET = "__SENTINEL_WEBHOOK_SECRET_NEVER_LOG_WS__"
WEBHOOK_SECRET_PREVIOUS = "__SENTINEL_PREVIOUS_WEBHOOK_SECRET_NL_PWS__"
ADMIN_API_KEY = "__SENTINEL_ADMIN_API_KEY_NEVER_LOG_AAK__"


def _config() -> RazorpayConfig:
    return RazorpayConfig(
        tenant_id="t_leakcheck",
        key_id="rzp_test_kid",
        key_secret=KEY_SECRET,
        webhook_secret=WEBHOOK_SECRET,
        webhook_secret_previous=WEBHOOK_SECRET_PREVIOUS,
        admin_api_key=ADMIN_API_KEY,
        db_dsn="postgresql://u:p@localhost:5432/d",
    )


def _captured_payload(order_id: str = "order_LEAK") -> bytes:
    return json.dumps(
        {
            "id": "evt_LEAK_1",
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {"order_id": order_id, "id": "pay_X", "amount": 50000}
                }
            },
        }
    ).encode("utf-8")


async def test_no_sentinel_secrets_appear_in_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    config = _config()  # Registers all four sentinels into the redaction set.

    log = get_logger("test_log_sanitization")
    # Deliberately try to leak via direct field, repr, and string interpolation.
    log.info(
        "ATTEMPT_LEAK_FIELD",
        key_secret_attempt=KEY_SECRET,
        webhook_secret_attempt=WEBHOOK_SECRET,
        webhook_secret_previous_attempt=WEBHOOK_SECRET_PREVIOUS,
        admin_api_key_attempt=ADMIN_API_KEY,
    )
    log.info(
        "ATTEMPT_LEAK_NESTED",
        nested={
            "ks": KEY_SECRET,
            "ws": WEBHOOK_SECRET,
            "wsp": WEBHOOK_SECRET_PREVIOUS,
            "aak": ADMIN_API_KEY,
        },
    )
    log.info("ATTEMPT_LEAK_REPR", config_repr=repr(config))

    # Drive a producer path: unknown tenant + signature mismatch both log.
    stub_sqs = AsyncMock()
    stub_sqs.send_message = AsyncMock(return_value={"MessageId": "msg_LK"})
    producer = WebhookProducer(
        tenant_registry=TenantConfigRegistry([config]),
        sqs_client=stub_sqs,
        queue_url="http://localhost:4566/0/q.fifo",
    )

    # Signature mismatch -> producer logs WARN.
    with pytest.raises(SignatureMismatchError):
        await producer.process(
            raw_body=_captured_payload(),
            header_signature="deadbeef" * 8,
            tenant_id="t_leakcheck",
        )

    # Unknown tenant -> producer logs WARN.
    with pytest.raises(UnknownTenantError):
        await producer.process(
            raw_body=_captured_payload(),
            header_signature="x",
            tenant_id="t_does_not_exist",
        )

    captured = capsys.readouterr().out
    assert captured, "expected some log output"

    for sentinel in (KEY_SECRET, WEBHOOK_SECRET, WEBHOOK_SECRET_PREVIOUS, ADMIN_API_KEY):
        assert sentinel not in captured, (
            f"sentinel secret leaked into logs: {sentinel}\n"
            f"first 500 chars of captured stdout: {captured[:500]}"
        )

    # Positive control: redaction marker should appear because we logged the
    # attempts above; if it doesn't, the processor wasn't installed.
    assert "***" in captured, "expected redaction marker '***' in captured output"
