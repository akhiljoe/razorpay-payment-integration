"""Tests for RazorpayClient using respx to intercept httpx."""

from __future__ import annotations

import httpx
import pytest
import respx

from razorpay_checkout.client import (
    RazorpayAPIError,
    RazorpayClient,
    RazorpayUnavailableError,
)
from razorpay_checkout.config import RazorpayConfig


@pytest.fixture
def config() -> RazorpayConfig:
    return RazorpayConfig(
        tenant_id="t_test",
        key_id="rzp_test_kid",
        key_secret="rzp_test_ksecret",
        webhook_secret="wsec",
        db_dsn="postgresql://u:p@localhost:5432/d",
        razorpay_api_timeout=1.0,
    )


@pytest.fixture
async def client(config: RazorpayConfig) -> RazorpayClient:
    return RazorpayClient(config)


@respx.mock
async def test_create_order_success_returns_payload(
    client: RazorpayClient,
) -> None:
    route = respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={"id": "order_TEST123", "amount": 50000, "currency": "INR"},
        )
    )
    result = await client.create_order(
        amount=50000, currency="INR", receipt="r1", notes={"k": "v"}
    )
    assert result["id"] == "order_TEST123"
    assert route.called
    sent_request = route.calls[0].request
    assert b'"amount":50000' in sent_request.content
    assert b'"receipt":"r1"' in sent_request.content
    # Basic auth header check
    assert sent_request.headers.get("authorization", "").startswith("Basic ")
    await client.aclose()


@respx.mock
async def test_create_order_timeout_raises_unavailable(
    client: RazorpayClient,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        side_effect=httpx.TimeoutException("timeout")
    )
    with pytest.raises(RazorpayUnavailableError):
        await client.create_order(amount=100, currency="INR", receipt="r1")
    await client.aclose()


@respx.mock
async def test_create_order_5xx_raises_unavailable(
    client: RazorpayClient,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(503, text="upstream down")
    )
    with pytest.raises(RazorpayUnavailableError):
        await client.create_order(amount=100, currency="INR", receipt="r1")
    await client.aclose()


@respx.mock
async def test_create_order_4xx_raises_api_error_with_body(
    client: RazorpayClient,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"code": "BAD_REQUEST_ERROR", "description": "Invalid receipt"}},
        )
    )
    with pytest.raises(RazorpayAPIError) as exc_info:
        await client.create_order(amount=100, currency="INR", receipt="r1")
    assert exc_info.value.status_code == 400
    assert exc_info.value.body["error"]["code"] == "BAD_REQUEST_ERROR"
    await client.aclose()


@respx.mock
async def test_create_order_4xx_non_json_body(client: RazorpayClient) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(400, text="not json")
    )
    with pytest.raises(RazorpayAPIError) as exc_info:
        await client.create_order(amount=100, currency="INR", receipt="r1")
    assert exc_info.value.body == "not json"
    await client.aclose()


@respx.mock
async def test_create_order_transport_error_raises_unavailable(
    client: RazorpayClient,
) -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        side_effect=httpx.ConnectError("dns")
    )
    with pytest.raises(RazorpayUnavailableError):
        await client.create_order(amount=100, currency="INR", receipt="r1")
    await client.aclose()


@respx.mock
async def test_client_uses_configured_timeout(config: RazorpayConfig) -> None:
    config_ = RazorpayConfig(**{**config.model_dump(), "razorpay_api_timeout": 2.5,
                                  "key_secret": config.key_secret.get_secret_value(),
                                  "webhook_secret": config.webhook_secret.get_secret_value(),
                                  "db_dsn": str(config.db_dsn)})
    c = RazorpayClient(config_)
    assert c._client.timeout.read == 2.5
    await c.aclose()
