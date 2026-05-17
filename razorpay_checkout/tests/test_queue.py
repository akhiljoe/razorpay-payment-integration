"""Tests for queue.py against LocalStack.

Marked `requires_localstack`. Run with the docker-compose LocalStack container
up and `AWS_ENDPOINT_URL` exported.
"""

from __future__ import annotations

import json

import pytest

from razorpay_checkout.queue import (
    ENVELOPE_VERSION,
    SQS_MAX_BODY_BYTES,
    MessageTooLargeError,
    build_webhook_envelope,
    publish_webhook_event,
)

pytestmark = pytest.mark.requires_localstack


# ---------- Unit: envelope builder (no LocalStack needed for these) ----------


def test_envelope_contains_all_required_fields() -> None:
    body = build_webhook_envelope(
        tenant_id="t1",
        event_id="evt_X",
        event="payment.captured",
        received_at="2026-05-12T10:30:00.000Z",
        signature="abc123",
        payload={"event": "payment.captured", "payload": {"payment": {"entity": {}}}},
    )
    parsed = json.loads(body)
    assert parsed["envelope_version"] == ENVELOPE_VERSION
    assert parsed["tenant_id"] == "t1"
    assert parsed["event_id"] == "evt_X"
    assert parsed["event"] == "payment.captured"
    assert parsed["received_at"] == "2026-05-12T10:30:00.000Z"
    assert parsed["signature"] == "abc123"
    assert parsed["payload"]["event"] == "payment.captured"


def test_oversized_envelope_raises() -> None:
    huge_payload = {"blob": "x" * (SQS_MAX_BODY_BYTES + 100)}
    with pytest.raises(MessageTooLargeError):
        build_webhook_envelope(
            tenant_id="t1",
            event_id="e",
            event="payment.captured",
            received_at="2026-05-12T10:30:00.000Z",
            signature="sig",
            payload=huge_payload,
        )


# ---------- Integration: publish to LocalStack and read back ----------


async def test_publish_webhook_event_round_trip(sqs_client, webhook_events_queue_url) -> None:
    body = build_webhook_envelope(
        tenant_id="t1",
        event_id="evt_RT_1",
        event="payment.captured",
        received_at="2026-05-12T10:30:00.000Z",
        signature="abc",
        payload={"event": "payment.captured", "payload": {}},
    )
    message_id = await publish_webhook_event(
        sqs_client=sqs_client,
        queue_url=webhook_events_queue_url,
        body=body,
        razorpay_order_id="order_RT_1",
        tenant_id="t1",
        event_id="evt_RT_1",
        correlation_id="corr_RT_1",
    )
    assert message_id

    received = await sqs_client.receive_message(
        QueueUrl=webhook_events_queue_url,
        MaxNumberOfMessages=1,
        MessageAttributeNames=["All"],
        AttributeNames=["All"],
        WaitTimeSeconds=2,
    )
    messages = received.get("Messages", [])
    assert len(messages) == 1
    msg = messages[0]
    parsed = json.loads(msg["Body"])
    assert parsed["tenant_id"] == "t1"
    assert parsed["event_id"] == "evt_RT_1"
    attrs = msg.get("MessageAttributes", {})
    assert attrs["correlation_id"]["StringValue"] == "corr_RT_1"
    sys_attrs = msg.get("Attributes", {})
    assert sys_attrs.get("MessageGroupId") == "order_RT_1"

    # Cleanup so subsequent tests don't see this message.
    await sqs_client.delete_message(
        QueueUrl=webhook_events_queue_url, ReceiptHandle=msg["ReceiptHandle"]
    )


async def test_sqs_dedup_suppresses_same_event_id(sqs_client, webhook_events_queue_url) -> None:
    """Two sends with the same MessageDeduplicationId must yield only one delivered message."""
    body = build_webhook_envelope(
        tenant_id="t1",
        event_id="evt_DEDUP",
        event="payment.captured",
        received_at="2026-05-12T10:30:00.000Z",
        signature="abc",
        payload={"event": "payment.captured", "payload": {}},
    )
    for _ in range(2):
        await publish_webhook_event(
            sqs_client=sqs_client,
            queue_url=webhook_events_queue_url,
            body=body,
            razorpay_order_id="order_DEDUP",
            tenant_id="t1",
            event_id="evt_DEDUP",
            correlation_id="corr_DEDUP",
        )

    received = await sqs_client.receive_message(
        QueueUrl=webhook_events_queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=2,
    )
    messages = received.get("Messages", [])
    assert len(messages) == 1, f"expected 1 message after dedup, got {len(messages)}"

    await sqs_client.delete_message(
        QueueUrl=webhook_events_queue_url,
        ReceiptHandle=messages[0]["ReceiptHandle"],
    )
