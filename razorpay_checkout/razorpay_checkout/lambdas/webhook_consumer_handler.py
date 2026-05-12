"""AWS Lambda handler for the Webhook Consumer.

Event source: `webhook-events.fifo`. Returns the SQS partial-batch-failure
shape so SQS retries only the messages that raised.

Construction pattern: the handler module reads a small set of environment
variables, builds the DB pool and SQS client lazily on first invocation, and
caches them on the module so warm invocations skip the cold-start cost.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import aioboto3
import asyncpg

from razorpay_checkout.amounts import FixedAmountResolver
from razorpay_checkout.client import RazorpayClient
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.correlation import new_correlation_id, set_correlation_id
from razorpay_checkout.db import create_pool
from razorpay_checkout.logging_config import configure_logging, get_logger
from razorpay_checkout.orders import OrderService
from razorpay_checkout.webhook_consumer import WebhookConsumer

_log = get_logger("razorpay_checkout.lambdas.webhook_consumer_handler")
_state: dict[str, Any] = {}


async def _init() -> None:
    """One-time async init. Idempotent across warm invocations."""
    if _state.get("ready"):
        return
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    config = RazorpayConfig()  # type: ignore[call-arg]
    pool: asyncpg.Pool = await create_pool(config)
    rzp_client = RazorpayClient(config)
    service = OrderService(
        pool=pool,
        config=config,
        client=rzp_client,
        amount_resolver=FixedAmountResolver(amount=1),  # resolver unused on this path
    )
    session = aioboto3.Session()
    sqs_cm = session.client(
        "sqs",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-south-1"),
    )
    sqs = await sqs_cm.__aenter__()
    consumer = WebhookConsumer(
        pool=pool,
        order_service=service,
        sqs_client=sqs,
        fulfillment_queue_url=os.environ["FULFILLMENT_QUEUE_URL"],
    )
    _state.update(
        {
            "ready": True,
            "config": config,
            "pool": pool,
            "service": service,
            "sqs": sqs,
            "consumer": consumer,
        }
    )


async def _process_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    await _init()
    consumer: WebhookConsumer = _state["consumer"]
    failures: list[dict[str, str]] = []
    for record in records:
        message_id = record.get("messageId", "")
        try:
            await consumer.process_record(
                message_body=record["body"],
                message_attributes=record.get("messageAttributes"),
            )
        except Exception as exc:  # noqa: BLE001 - we surface partial failure
            _log.error(
                "consumer_record_failed",
                message_id=message_id,
                error=str(exc),
            )
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point. Wraps the async pipeline in `asyncio.run`."""
    set_correlation_id(new_correlation_id())
    records = event.get("Records", [])
    return asyncio.run(_process_records(records))
