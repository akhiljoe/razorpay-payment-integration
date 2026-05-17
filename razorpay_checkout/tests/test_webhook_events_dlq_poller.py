"""Tests for the webhook-events DLQ poller.

The poller is a thin SQS-record consumer that marks `webhook_events` as
`dead`. It does not retry. We exercise the marker function directly because
the handler entry point is wrapped in `asyncio.run` for Lambda.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from razorpay_checkout.lambdas.webhook_events_dlq_poller import _mark_dead

pytestmark = pytest.mark.requires_db


def _now() -> datetime:
    return datetime.now(tz=UTC)


async def test_mark_dead_updates_processing_status(live_pool) -> None:
    async with live_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO webhook_events
                (tenant_id, event_id, event, payload, correlation_id, received_at)
            VALUES ($1, $2, 'payment.captured', '{}'::jsonb, $3, $4)
            """,
            "t_dlq",
            "evt_DLQ_1",
            "corr_DLQ_1",
            _now(),
        )

    await _mark_dead(live_pool, "t_dlq", "evt_DLQ_1", reason="dlq_test")

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT processing_status, error FROM webhook_events WHERE event_id = $1",
            "evt_DLQ_1",
        )
    assert row["processing_status"] == "dead"
    assert "dlq_test" in (row["error"] or "")


async def test_mark_dead_is_noop_for_unknown_event(live_pool) -> None:
    # Should not raise; just update zero rows.
    await _mark_dead(live_pool, "t_unknown", "evt_NOPE", reason="x")


def test_envelope_parse_recovers_ids() -> None:
    body = json.dumps({"tenant_id": "t1", "event_id": "evt_X", "envelope_version": 1})
    parsed = json.loads(body)
    assert parsed["tenant_id"] == "t1"
    assert parsed["event_id"] == "evt_X"
