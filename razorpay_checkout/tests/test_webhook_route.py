"""Tests for POST /payments/webhook/{tenant_id}.

Uses a stubbed SQS client wired through app.state. No LocalStack needed for
HTTP-level coverage; the LocalStack-backed end-to-end test lives in test_queue.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest_asyncio
from fastapi import FastAPI

from razorpay_checkout import (
    RazorpayConfig,
    TenantConfigRegistry,
    WebhookProducer,
    payments_router,
)

WEBHOOK_SECRET = "phase6_route_secret"


def _config(tenant_id: str = "t_route") -> RazorpayConfig:
    return RazorpayConfig(
        tenant_id=tenant_id,
        key_id="rzp_test_kid",
        key_secret="ksec",
        webhook_secret=WEBHOOK_SECRET,
        db_dsn="postgresql://u:p@localhost:5432/d",
    )


def _sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _captured_payload() -> bytes:
    return json.dumps(
        {
            "id": "evt_ROUTE_1",
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {"order_id": "order_ROUTE_1", "id": "pay_X", "amount": 50000}
                }
            },
        }
    ).encode("utf-8")


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    config = _config()
    registry = TenantConfigRegistry([config])
    stub_sqs = AsyncMock()
    stub_sqs.send_message = AsyncMock(return_value={"MessageId": "msg_ROUTE_001"})
    producer = WebhookProducer(
        tenant_registry=registry,
        sqs_client=stub_sqs,
        queue_url="http://localhost:4566/0/webhook-events.fifo",
    )
    app = FastAPI()
    app.state.razorpay_config = config
    app.state.webhook_producer = producer
    app.include_router(payments_router, prefix="/payments")
    yield app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------- 200 queued ----------


async def test_valid_webhook_returns_200_queued(client: httpx.AsyncClient) -> None:
    body = _captured_payload()
    response = await client.post(
        "/payments/webhook/t_route",
        content=body,
        headers={
            "x-razorpay-signature": _sign(body),
            "content-type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["correlation_id"]
    assert response.headers["x-correlation-id"] == payload["correlation_id"]


# ---------- 400 variants ----------


async def test_unknown_tenant_returns_400(client: httpx.AsyncClient) -> None:
    body = _captured_payload()
    response = await client.post(
        "/payments/webhook/t_does_not_exist",
        content=body,
        headers={"x-razorpay-signature": _sign(body)},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "unknown_tenant"


async def test_signature_mismatch_returns_400(client: httpx.AsyncClient) -> None:
    body = _captured_payload()
    response = await client.post(
        "/payments/webhook/t_route",
        content=body,
        headers={"x-razorpay-signature": "deadbeef" * 8},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "signature_mismatch"


async def test_malformed_payload_returns_400(client: httpx.AsyncClient) -> None:
    body = b"not json"
    response = await client.post(
        "/payments/webhook/t_route",
        content=body,
        headers={"x-razorpay-signature": _sign(body)},
    )
    assert response.status_code == 400
    body_json = response.json()
    assert body_json["detail"] == "malformed_payload"
    assert "reason" in body_json


async def test_missing_signature_header_returns_400(client: httpx.AsyncClient) -> None:
    body = _captured_payload()
    response = await client.post(
        "/payments/webhook/t_route",
        content=body,
        # No signature header
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "signature_mismatch"


# ---------- 503 when publish fails ----------


async def test_publish_failure_returns_503(app: FastAPI, client: httpx.AsyncClient) -> None:
    app.state.webhook_producer._sqs.send_message = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("sqs down")
    )
    body = _captured_payload()
    response = await client.post(
        "/payments/webhook/t_route",
        content=body,
        headers={"x-razorpay-signature": _sign(body)},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "queue_unavailable"
