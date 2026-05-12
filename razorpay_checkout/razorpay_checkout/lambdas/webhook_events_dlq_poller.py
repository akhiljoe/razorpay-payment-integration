"""DLQ poller for `webhook-events-dlq.fifo`.

Triggered by messages on the DLQ. For each message:
1. Parse the envelope (best-effort) to recover `tenant_id` and `event_id`.
2. UPDATE `webhook_events` to `processing_status='dead'` with the recovered id.
3. Emit `dead_event_count` log; CloudWatch alarm fires on count > 0.

A message in the DLQ means the consumer failed all `maxReceiveCount` attempts.
The DLQ poller does not retry. Operator's recovery: fix root cause, then
`POST /payments/admin/events/{id}/replay`.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import asyncpg

from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.db import create_pool
from razorpay_checkout.logging_config import configure_logging, get_logger
from razorpay_checkout.telemetry import dead_event_count

_log = get_logger("razorpay_checkout.lambdas.webhook_events_dlq_poller")
_state: dict[str, Any] = {}


async def _init() -> asyncpg.Pool:
    if _state.get("pool") is None:
        configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
        _state["pool"] = await create_pool(RazorpayConfig())  # type: ignore[call-arg]
    return _state["pool"]


async def _mark_dead(pool: asyncpg.Pool, tenant_id: str, event_id: str, reason: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE webhook_events
               SET processing_status = 'dead',
                   error = COALESCE(error, '') || '\n[dlq] ' || $3
             WHERE tenant_id = $1 AND event_id = $2
            """,
            tenant_id,
            event_id,
            reason,
        )


async def _process_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    pool = await _init()
    failures: list[dict[str, str]] = []
    for record in records:
        message_id = record.get("messageId", "")
        try:
            envelope = json.loads(record.get("body", ""))
            tenant_id = envelope.get("tenant_id", "")
            event_id = envelope.get("event_id", "")
            if not tenant_id or not event_id:
                raise ValueError("envelope missing tenant_id or event_id")
            await _mark_dead(
                pool, tenant_id, event_id, reason=f"dlq_message_id={message_id}"
            )
            dead_event_count.add(1, {"tenant_id": tenant_id})
            _log.warning(
                "dead_event_marked",
                tenant_id=tenant_id,
                event_id=event_id,
                message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error("dlq_poller_record_failed", message_id=message_id, error=str(exc))
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    return asyncio.run(_process_records(event.get("Records", [])))
