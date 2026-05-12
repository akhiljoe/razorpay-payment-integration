"""DLQ poller for `fulfillment-dlq.fifo`.

DESIGN_v5 Section 7 and Section 11. Triggered when the Fulfillment Lambda's
`maxReceiveCount=5` retries are exhausted. The poller transitions the order
from `paid` to `fulfillment_failed` and records the failure reason in
`order_events.metadata`.

Recovery procedure (Runbook 2): fix root cause in `FulfillmentHook`, then
`POST /payments/admin/orders/{order_id}/retry-fulfillment` to re-enqueue.

No-op if the order is already at `fulfillment_failed` (idempotent across
multiple DLQ messages for the same order).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import UUID

import asyncpg

from razorpay_checkout.amounts import FixedAmountResolver
from razorpay_checkout.client import RazorpayClient
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.correlation import new_correlation_id, set_correlation_id
from razorpay_checkout.db import create_pool
from razorpay_checkout.logging_config import configure_logging, get_logger
from razorpay_checkout.orders import OrderService
from razorpay_checkout.state_machine import OrderStatus

_log = get_logger("razorpay_checkout.lambdas.fulfillment_dlq_poller")
_state: dict[str, Any] = {}


async def _init() -> tuple[asyncpg.Pool, OrderService]:
    if "service" not in _state:
        configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
        config = RazorpayConfig()  # type: ignore[call-arg]
        pool: asyncpg.Pool = await create_pool(config)
        service = OrderService(
            pool=pool,
            config=config,
            client=RazorpayClient(config),
            amount_resolver=FixedAmountResolver(amount=1),
        )
        _state["pool"] = pool
        _state["service"] = service
    return _state["pool"], _state["service"]


async def mark_fulfillment_failed(
    pool: asyncpg.Pool,
    service: OrderService,
    *,
    order_id: UUID,
    tenant_id: str,
    reason: str,
    message_id: str = "",
) -> bool:
    """Transition `paid -> fulfillment_failed`. Returns True on transition, False on no-op.

    No-op when the order is already at `fulfillment_failed`. Logs WARN for
    any other unexpected state and does not raise.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status FROM orders WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                order_id,
                tenant_id,
            )
            if row is None:
                _log.error(
                    "dlq_unknown_order",
                    tenant_id=tenant_id,
                    order_id=str(order_id),
                )
                return False
            current = OrderStatus(row["status"])
            if current is OrderStatus.FULFILLMENT_FAILED:
                _log.info(
                    "dlq_already_marked",
                    tenant_id=tenant_id,
                    order_id=str(order_id),
                )
                return False
            if current is not OrderStatus.PAID:
                _log.warning(
                    "dlq_unexpected_state",
                    tenant_id=tenant_id,
                    order_id=str(order_id),
                    order_status=current.value,
                )
                return False
            await service.transition(
                conn,
                order_id=order_id,
                tenant_id=tenant_id,
                new_status=OrderStatus.FULFILLMENT_FAILED,
                trigger="fulfillment",
                metadata={
                    "reason": reason,
                    "dlq_message_id": message_id,
                },
            )
    _log.warning(
        "order_fulfillment_failed",
        tenant_id=tenant_id,
        order_id=str(order_id),
        reason=reason,
    )
    return True


async def _process_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    pool, service = await _init()
    failures: list[dict[str, str]] = []
    for record in records:
        message_id = record.get("messageId", "")
        try:
            envelope = json.loads(record.get("body", ""))
            tenant_id = envelope["tenant_id"]
            order_id = UUID(envelope["order_id"])
            await mark_fulfillment_failed(
                pool,
                service,
                order_id=order_id,
                tenant_id=tenant_id,
                reason=f"sqs_retries_exhausted:{message_id}",
                message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "dlq_poller_record_failed",
                message_id=message_id,
                error=str(exc),
            )
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    set_correlation_id(new_correlation_id())
    return asyncio.run(_process_records(event.get("Records", [])))
