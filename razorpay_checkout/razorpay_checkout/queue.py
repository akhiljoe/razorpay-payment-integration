"""SQS FIFO publishers and bootstrap helpers.

Two queue pairs:
- `webhook-events.fifo` + `webhook-events-dlq.fifo`: producer -> consumer.
- `fulfillment-queue.fifo` + `fulfillment-dlq.fifo`: consumer -> fulfillment.

DESIGN_v5 Section 7 specifies the network posture: producer publishes to
`webhook-events.fifo` only. The consumer publishes to `fulfillment-queue.fifo`
after committing the DB transition. Both Lambdas reach SQS via the public
internet on Free Track or via a VPC Interface Endpoint on Paid Track.

This module is the only place that constructs SQS message envelopes. The
envelope schema is versioned to keep producer and consumer in lockstep.

Message envelope schema (envelope_version = 1):

    {
      "envelope_version": 1,
      "tenant_id": "...",
      "event_id": "...",
      "event": "payment.captured",
      "received_at": "2026-05-12T10:30:00.000Z",
      "signature": "<hex>",
      "payload": { ...the parsed Razorpay payload... }
    }

SQS attributes set by the producer:
- `MessageGroupId = razorpay_order_id` (per-order ordering).
- `MessageDeduplicationId = f"{tenant_id}:{event_id}"` (5-min FIFO dedup).
- `MessageAttributes.correlation_id` (string).
"""

from __future__ import annotations

import json
import os
from typing import Any

from razorpay_checkout.logging_config import get_logger

# aioboto3's SQS client lacks stable type stubs. Use `Any` and rely on tests.
SQSClient = Any  # noqa: N816 - re-exported alias

_log = get_logger("razorpay_checkout.queue")

ENVELOPE_VERSION = 1

WEBHOOK_EVENTS_QUEUE_NAME = "webhook-events.fifo"
WEBHOOK_EVENTS_DLQ_NAME = "webhook-events-dlq.fifo"
FULFILLMENT_QUEUE_NAME = "fulfillment-queue.fifo"
FULFILLMENT_DLQ_NAME = "fulfillment-dlq.fifo"

# SQS message body has a 256 KB cap. Razorpay payloads are well under 10 KB.
SQS_MAX_BODY_BYTES = 256 * 1024


class MessageTooLargeError(Exception):
    """Raised when the serialized SQS body exceeds SQS's 256 KB cap."""


def aws_endpoint_url() -> str | None:
    """Return AWS_ENDPOINT_URL if set (LocalStack), else None (real AWS)."""
    url = os.environ.get("AWS_ENDPOINT_URL", "").strip()
    return url or None


def build_webhook_envelope(
    *,
    tenant_id: str,
    event_id: str,
    event: str,
    received_at: str,
    signature: str,
    payload: dict[str, Any],
) -> str:
    """Serialize the producer's envelope to a JSON string for SQS body.

    `received_at` is the producer's wall clock at HTTP entry, ISO-8601 UTC.
    The consumer reads this verbatim and writes it to `webhook_events.received_at`.
    """
    envelope = {
        "envelope_version": ENVELOPE_VERSION,
        "tenant_id": tenant_id,
        "event_id": event_id,
        "event": event,
        "received_at": received_at,
        "signature": signature,
        "payload": payload,
    }
    body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    if len(body.encode("utf-8")) > SQS_MAX_BODY_BYTES:
        raise MessageTooLargeError(
            f"envelope is {len(body)} bytes, exceeds SQS 256 KB cap"
        )
    return body


async def publish_webhook_event(
    *,
    sqs_client: SQSClient,
    queue_url: str,
    body: str,
    razorpay_order_id: str,
    tenant_id: str,
    event_id: str,
    correlation_id: str,
) -> str:
    """Send one message to `webhook-events.fifo`. Returns the SQS MessageId."""
    response = await sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=body,
        MessageGroupId=razorpay_order_id,
        MessageDeduplicationId=f"{tenant_id}:{event_id}",
        MessageAttributes={
            "correlation_id": {
                "DataType": "String",
                "StringValue": correlation_id,
            },
        },
    )
    message_id: str = response["MessageId"]
    _log.info(
        "webhook_event_published",
        queue=queue_url,
        message_id=message_id,
        razorpay_order_id=razorpay_order_id,
        event_id=event_id,
        correlation_id=correlation_id,
    )
    return message_id


async def publish_fulfillment_message(
    *,
    sqs_client: SQSClient,
    queue_url: str,
    order_id: str,
    tenant_id: str,
    razorpay_payment_id: str,
    correlation_id: str,
) -> str:
    """Send one message to `fulfillment-queue.fifo`. Returns the SQS MessageId."""
    body = json.dumps(
        {
            "envelope_version": ENVELOPE_VERSION,
            "order_id": order_id,
            "tenant_id": tenant_id,
            "razorpay_payment_id": razorpay_payment_id,
        },
        separators=(",", ":"),
    )
    response = await sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=body,
        MessageGroupId=order_id,
        MessageDeduplicationId=f"{order_id}:{razorpay_payment_id}",
        MessageAttributes={
            "correlation_id": {
                "DataType": "String",
                "StringValue": correlation_id,
            },
        },
    )
    message_id: str = response["MessageId"]
    _log.info(
        "fulfillment_message_published",
        queue=queue_url,
        message_id=message_id,
        order_id=order_id,
        razorpay_payment_id=razorpay_payment_id,
        correlation_id=correlation_id,
    )
    return message_id


async def ensure_queue_pair(
    sqs_client: SQSClient,
    *,
    main_name: str,
    dlq_name: str,
    max_receive_count: int = 5,
) -> tuple[str, str]:
    """Create the FIFO main + DLQ pair if absent. Returns `(main_url, dlq_url)`.

    Idempotent: re-running against existing queues returns the same URLs and
    does not fail. Intended for LocalStack bootstrap and CDK-managed envs use
    CDK instead.
    """
    dlq_resp = await sqs_client.create_queue(
        QueueName=dlq_name,
        Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
    )
    dlq_url: str = dlq_resp["QueueUrl"]
    dlq_arn_resp = await sqs_client.get_queue_attributes(
        QueueUrl=dlq_url, AttributeNames=["QueueArn"]
    )
    dlq_arn = dlq_arn_resp["Attributes"]["QueueArn"]

    main_resp = await sqs_client.create_queue(
        QueueName=main_name,
        Attributes={
            "FifoQueue": "true",
            "ContentBasedDeduplication": "false",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": max_receive_count}
            ),
        },
    )
    main_url: str = main_resp["QueueUrl"]

    _log.info(
        "queue_pair_ensured",
        main_name=main_name,
        main_url=main_url,
        dlq_name=dlq_name,
        dlq_url=dlq_url,
    )
    return main_url, dlq_url


async def ensure_webhook_events_queues(sqs_client: SQSClient) -> tuple[str, str]:
    """Convenience: ensure `webhook-events.fifo` + DLQ exist."""
    return await ensure_queue_pair(
        sqs_client,
        main_name=WEBHOOK_EVENTS_QUEUE_NAME,
        dlq_name=WEBHOOK_EVENTS_DLQ_NAME,
    )


async def ensure_fulfillment_queues(sqs_client: SQSClient) -> tuple[str, str]:
    """Convenience: ensure `fulfillment-queue.fifo` + DLQ exist."""
    return await ensure_queue_pair(
        sqs_client,
        main_name=FULFILLMENT_QUEUE_NAME,
        dlq_name=FULFILLMENT_DLQ_NAME,
    )
