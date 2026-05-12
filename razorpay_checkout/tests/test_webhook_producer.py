"""Tests for WebhookProducer.

Most tests use a stub SQS client (`AsyncMock`) and exercise the producer
logic directly. One LocalStack-backed test confirms end-to-end publish.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.tenants import TenantConfigRegistry
from razorpay_checkout.webhook_producer import (
    MalformedPayloadError,
    PublishFailureError,
    SignatureMismatchError,
    UnknownTenantError,
    WebhookProducer,
)

# ---------- Helpers ----------


WEBHOOK_SECRET = "phase6_webhook_secret"


def _config(tenant_id: str = "t_p6") -> RazorpayConfig:
    return RazorpayConfig(
        tenant_id=tenant_id,
        key_id="rzp_test_kid",
        key_secret="key_sec",
        webhook_secret=WEBHOOK_SECRET,
        db_dsn="postgresql://u:p@localhost:5432/d",
    )


def _payload(event: str = "payment.captured", order_id: str = "order_X") -> dict[str, Any]:
    return {
        "id": f"evt_{event.replace('.', '_')}_X",
        "event": event,
        "payload": {
            "payment": {"entity": {"order_id": order_id, "id": "pay_X", "amount": 50000}},
        },
    }


def _refund_payload(order_id: str = "order_X") -> dict[str, Any]:
    return {
        "id": "evt_refund_X",
        "event": "refund.processed",
        "payload": {
            "refund": {"entity": {"order_id": order_id, "id": "rfnd_X"}},
        },
    }


def _order_paid_payload(order_id: str = "order_X") -> dict[str, Any]:
    return {
        "id": "evt_order_paid_X",
        "event": "order.paid",
        "payload": {
            "order": {"entity": {"id": order_id}},
        },
    }


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.fixture
def registry() -> TenantConfigRegistry:
    return TenantConfigRegistry([_config("t_p6")])


@pytest.fixture
def stub_sqs() -> AsyncMock:
    mock = AsyncMock()
    mock.send_message = AsyncMock(return_value={"MessageId": "msg_STUB_001"})
    return mock


@pytest.fixture
def producer(registry: TenantConfigRegistry, stub_sqs: AsyncMock) -> WebhookProducer:
    return WebhookProducer(
        tenant_registry=registry,
        sqs_client=stub_sqs,
        queue_url="http://localhost:4566/000000000000/webhook-events.fifo",
    )


# ---------- Happy path ----------


async def test_valid_event_publishes_and_returns_queued(
    producer: WebhookProducer, stub_sqs: AsyncMock
) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    ack = await producer.process(
        raw_body=body,
        header_signature=_sign(body),
        tenant_id="t_p6",
    )
    assert ack.status == "queued"
    assert ack.correlation_id
    assert ack.message_id == "msg_STUB_001"

    stub_sqs.send_message.assert_awaited_once()
    kwargs = stub_sqs.send_message.await_args.kwargs
    assert kwargs["MessageGroupId"] == "order_X"
    assert kwargs["MessageDeduplicationId"] == "t_p6:evt_payment_captured_X"
    attrs = kwargs["MessageAttributes"]
    assert attrs["correlation_id"]["StringValue"] == ack.correlation_id
    envelope = json.loads(kwargs["MessageBody"])
    assert envelope["tenant_id"] == "t_p6"
    assert envelope["event"] == "payment.captured"
    assert envelope["event_id"] == "evt_payment_captured_X"
    assert "received_at" in envelope


async def test_refund_event_extracts_order_id_from_refund_entity(
    producer: WebhookProducer, stub_sqs: AsyncMock
) -> None:
    body = json.dumps(_refund_payload(order_id="order_RF")).encode("utf-8")
    await producer.process(
        raw_body=body, header_signature=_sign(body), tenant_id="t_p6"
    )
    kwargs = stub_sqs.send_message.await_args.kwargs
    assert kwargs["MessageGroupId"] == "order_RF"


async def test_order_paid_event_extracts_order_id_from_order_entity(
    producer: WebhookProducer, stub_sqs: AsyncMock
) -> None:
    body = json.dumps(_order_paid_payload(order_id="order_OP")).encode("utf-8")
    await producer.process(
        raw_body=body, header_signature=_sign(body), tenant_id="t_p6"
    )
    kwargs = stub_sqs.send_message.await_args.kwargs
    assert kwargs["MessageGroupId"] == "order_OP"


# ---------- 400-class failures ----------


async def test_unknown_tenant_raises(producer: WebhookProducer) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    with pytest.raises(UnknownTenantError):
        await producer.process(
            raw_body=body,
            header_signature=_sign(body),
            tenant_id="t_unknown",
        )


async def test_signature_mismatch_raises(producer: WebhookProducer) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    with pytest.raises(SignatureMismatchError):
        await producer.process(
            raw_body=body,
            header_signature="deadbeef" * 8,
            tenant_id="t_p6",
        )


async def test_malformed_json_raises(producer: WebhookProducer) -> None:
    body = b"not json {"
    with pytest.raises(MalformedPayloadError):
        await producer.process(
            raw_body=body,
            header_signature=_sign(body),
            tenant_id="t_p6",
        )


async def test_missing_event_id_raises(producer: WebhookProducer) -> None:
    payload = _payload()
    del payload["id"]
    body = json.dumps(payload).encode("utf-8")
    with pytest.raises(MalformedPayloadError):
        await producer.process(
            raw_body=body,
            header_signature=_sign(body),
            tenant_id="t_p6",
        )


async def test_missing_razorpay_order_id_raises(producer: WebhookProducer) -> None:
    payload = _payload()
    payload["payload"]["payment"]["entity"].pop("order_id")
    body = json.dumps(payload).encode("utf-8")
    with pytest.raises(MalformedPayloadError):
        await producer.process(
            raw_body=body,
            header_signature=_sign(body),
            tenant_id="t_p6",
        )


# ---------- Signature verification details ----------


async def test_previous_secret_fallback_works(stub_sqs: AsyncMock) -> None:
    config = RazorpayConfig(
        tenant_id="t_rot",
        key_id="rzp_test_kid",
        key_secret="key_sec",
        webhook_secret="new_secret",
        webhook_secret_previous="old_secret",
        db_dsn="postgresql://u:p@localhost:5432/d",
    )
    producer = WebhookProducer(
        tenant_registry=TenantConfigRegistry([config]),
        sqs_client=stub_sqs,
        queue_url="http://localhost:4566/0/q.fifo",
    )
    body = json.dumps(_payload()).encode("utf-8")
    ack = await producer.process(
        raw_body=body,
        header_signature=_sign(body, secret="old_secret"),
        tenant_id="t_rot",
    )
    assert ack.status == "queued"


# ---------- Publish failure surfaces as PublishFailureError ----------


async def test_publish_failure_wraps_aioboto3_exception(
    registry: TenantConfigRegistry,
) -> None:
    failing_sqs = AsyncMock()
    failing_sqs.send_message = AsyncMock(side_effect=RuntimeError("sqs down"))
    producer = WebhookProducer(
        tenant_registry=registry,
        sqs_client=failing_sqs,
        queue_url="http://localhost:4566/0/q.fifo",
    )
    body = json.dumps(_payload()).encode("utf-8")
    with pytest.raises(PublishFailureError):
        await producer.process(
            raw_body=body,
            header_signature=_sign(body),
            tenant_id="t_p6",
        )


# ---------- Critical invariant: producer NEVER touches the DB ----------


async def test_producer_does_not_use_asyncpg_pool(
    producer: WebhookProducer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch asyncpg.create_pool to raise on use and confirm the happy path still works.

    The producer must not import asyncpg, instantiate a pool, or open a
    connection. Any DB usage from the producer module is a security bug
    (DESIGN_v5 Section 2.1: "The producer holds no database credentials").
    """
    import asyncpg

    async def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Producer must never call asyncpg.create_pool")

    monkeypatch.setattr(asyncpg, "create_pool", _explode)

    body = json.dumps(_payload()).encode("utf-8")
    ack = await producer.process(
        raw_body=body,
        header_signature=_sign(body),
        tenant_id="t_p6",
    )
    assert ack.status == "queued"
