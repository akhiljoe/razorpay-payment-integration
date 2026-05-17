"""Tests for the order routes using FastAPI's TestClient against the live pool.

Marked `requires_db` because the routes call OrderService, which writes to
Postgres. Razorpay HTTP calls are intercepted by respx.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI

from razorpay_checkout import (
    FixedAmountResolver,
    OrderService,
    RazorpayClient,
    payments_router,
)
from razorpay_checkout.config import RazorpayConfig

pytestmark = pytest.mark.requires_db


def _payment_signature(order_id: str, payment_id: str, key_secret: str) -> str:
    payload = f"{order_id}|{payment_id}".encode()
    return hmac.new(key_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


@pytest_asyncio.fixture
async def app(
    live_pool,
    db_config: RazorpayConfig,
    razorpay_client: RazorpayClient,
    fixed_resolver: FixedAmountResolver,
) -> AsyncIterator[FastAPI]:
    service = OrderService(
        pool=live_pool,
        config=db_config,
        client=razorpay_client,
        amount_resolver=fixed_resolver,
    )
    app = FastAPI()
    app.state.razorpay_config = db_config
    app.state.razorpay_pool = live_pool
    app.state.order_service = service
    app.include_router(payments_router, prefix="/payments")
    yield app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------- POST /payments/orders ----------


@respx.mock
async def test_post_orders_returns_201_with_expected_shape(
    client: httpx.AsyncClient,
    db_config: RazorpayConfig,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            200, json={"id": "order_API_1", "amount": 50000, "currency": "INR"}
        )
    )
    response = await client.post(
        "/payments/orders",
        json={"receipt": "r-api-1"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["razorpay_order_id"] == "order_API_1"
    assert body["amount"] == 50000
    assert body["currency"] == "INR"
    assert body["key_id"] == db_config.key_id


@respx.mock
async def test_post_orders_idempotent_on_receipt(
    client: httpx.AsyncClient,
) -> None:
    route = respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            200, json={"id": "order_API_2", "amount": 50000, "currency": "INR"}
        )
    )
    r1 = await client.post("/payments/orders", json={"receipt": "r-api-2"})
    r2 = await client.post("/payments/orders", json={"receipt": "r-api-2"})
    assert r1.json()["order_id"] == r2.json()["order_id"]
    assert route.call_count == 1


@respx.mock
async def test_post_orders_503_on_razorpay_timeout(client: httpx.AsyncClient) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        side_effect=httpx.TimeoutException("timeout")
    )
    response = await client.post("/payments/orders", json={"receipt": "r-api-3"})
    assert response.status_code == 503
    assert response.json()["detail"] == "razorpay_api_unavailable"


# ---------- GET /payments/orders/{order_id} ----------


@respx.mock
async def test_get_order_status(
    client: httpx.AsyncClient,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            200, json={"id": "order_GET_1", "amount": 50000, "currency": "INR"}
        )
    )
    create = await client.post("/payments/orders", json={"receipt": "r-get-1"})
    order_id = create.json()["order_id"]

    response = await client.get(f"/payments/orders/{order_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == order_id
    assert body["status"] == "created"
    assert body["razorpay_order_id"] == "order_GET_1"


async def test_get_order_404_on_unknown_id(client: httpx.AsyncClient) -> None:
    import uuid

    response = await client.get(f"/payments/orders/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == "order_not_found"


# ---------- POST /payments/verify-callback ----------


@respx.mock
async def test_verify_callback_valid_signature(
    client: httpx.AsyncClient,
    db_config: RazorpayConfig,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            200, json={"id": "order_VC_1", "amount": 50000, "currency": "INR"}
        )
    )
    create = await client.post("/payments/orders", json={"receipt": "r-vc-1"})
    order_id = create.json()["order_id"]
    razorpay_order_id = create.json()["razorpay_order_id"]
    payment_id = "pay_TEST_1"
    signature = _payment_signature(
        razorpay_order_id, payment_id, db_config.key_secret.get_secret_value()
    )

    response = await client.post(
        "/payments/verify-callback",
        json={
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verified"] is True
    assert body["order_id"] == order_id
    assert body["reason"] is None


@respx.mock
async def test_verify_callback_signature_mismatch_returns_400(
    client: httpx.AsyncClient,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            200, json={"id": "order_VC_2", "amount": 50000, "currency": "INR"}
        )
    )
    await client.post("/payments/orders", json={"receipt": "r-vc-2"})

    response = await client.post(
        "/payments/verify-callback",
        json={
            "razorpay_order_id": "order_VC_2",
            "razorpay_payment_id": "pay_TEST_2",
            "razorpay_signature": "deadbeef" * 8,
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["verified"] is False
    assert body["reason"] == "signature_mismatch"


@respx.mock
async def test_verify_callback_unknown_order(
    client: httpx.AsyncClient,
    db_config: RazorpayConfig,
) -> None:
    signature = _payment_signature(
        "order_UNKNOWN", "pay_X", db_config.key_secret.get_secret_value()
    )
    response = await client.post(
        "/payments/verify-callback",
        json={
            "razorpay_order_id": "order_UNKNOWN",
            "razorpay_payment_id": "pay_X",
            "razorpay_signature": signature,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verified"] is False
    assert body["reason"] == "order_not_found"
