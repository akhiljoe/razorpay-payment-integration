"""AWS Lambda handler for fulfillment.

Event source: `fulfillment-queue.fifo`. Returns the SQS partial-batch-failure
shape so SQS retries only the messages that raised.

`FulfillmentHook` is host-supplied. The default no-op hook is used unless the
host sets `app.state` (or an env-driven import path) before the first
invocation. For v1, the host wires a real hook by importing this module and
overwriting `_state["processor"]` at startup, or via a module-level
configuration hook in a later phase.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import aioboto3  # noqa: F401 - kept for ABI parity with other handlers
import asyncpg

from razorpay_checkout.amounts import FixedAmountResolver
from razorpay_checkout.client import RazorpayClient
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.correlation import new_correlation_id, set_correlation_id
from razorpay_checkout.db import create_pool
from razorpay_checkout.fulfillment import (
    DefaultFulfillmentHook,
    FulfillmentHook,
    FulfillmentProcessor,
)
from razorpay_checkout.logging_config import configure_logging, get_logger
from razorpay_checkout.orders import OrderService

_log = get_logger("razorpay_checkout.lambdas.fulfillment_handler")
_state: dict[str, Any] = {}


def register_hook(hook: FulfillmentHook) -> None:
    """Host-app injection point. Call from a Lambda layer or init script."""
    _state["hook"] = hook


async def _init() -> None:
    if _state.get("ready"):
        return
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    config = RazorpayConfig()  # type: ignore[call-arg]
    pool: asyncpg.Pool = await create_pool(config)
    service = OrderService(
        pool=pool,
        config=config,
        client=RazorpayClient(config),
        amount_resolver=FixedAmountResolver(amount=1),  # unused on this path
    )
    hook: FulfillmentHook = _state.get("hook") or DefaultFulfillmentHook()
    processor = FulfillmentProcessor(pool=pool, order_service=service, hook=hook)
    _state.update(
        {
            "ready": True,
            "config": config,
            "pool": pool,
            "service": service,
            "hook": hook,
            "processor": processor,
        }
    )


async def _process_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    await _init()
    processor: FulfillmentProcessor = _state["processor"]
    failures: list[dict[str, str]] = []
    for record in records:
        message_id = record.get("messageId", "")
        try:
            await processor.process_record(
                message_body=record["body"],
                message_attributes=record.get("messageAttributes"),
            )
        except Exception as exc:  # noqa: BLE001 - SQS partial failure semantics
            _log.error(
                "fulfillment_record_failed",
                message_id=message_id,
                error=str(exc),
            )
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    set_correlation_id(new_correlation_id())
    return asyncio.run(_process_records(event.get("Records", [])))
