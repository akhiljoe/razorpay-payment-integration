"""Tests for the admin routes: reconcile, replay event, retry fulfillment."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from unittest.mock import AsyncMock

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


ADMIN_KEY = "phase10_admin_key"


@pytest_asyncio.fixture
async def admin_app(
    live_pool,
    razorpay_client: RazorpayClient,
    fixed_resolver: FixedAmountResolver,
) -> AsyncIterator[tuple[FastAPI, AsyncMock, RazorpayConfig]]:
    config = RazorpayConfig(
        tenant_id="t_admin",
        key_id="rzp_test_kid",
        key_secret="ksec",
        webhook_secret="wsec",
        admin_api_key=ADMIN_KEY,
        db_dsn="postgresql://razorpay:razorpay@localhost:5432/razorpay_checkout",
        pool_min_size=1,
        pool_max_size=4,
        razorpay_api_timeout=2.0,
    )
    # Use the live pool for DB but build a fresh service against the admin tenant config.
    service = OrderService(
        pool=live_pool,
        config=config,
        client=razorpay_client,
        amount_resolver=fixed_resolver,
    )
    stub_sqs = AsyncMock()
    stub_sqs.send_message = AsyncMock(return_value={"MessageId": "msg_ADMIN_001"})
    app = FastAPI()
    app.state.razorpay_config = config
    app.state.razorpay_pool = live_pool
    app.state.order_service = service
    app.state.razorpay_client = razorpay_client
    app.state.sqs_client = stub_sqs
    app.state.fulfillment_queue_url = "http://localhost:4566/0/fulfillment-queue.fifo"
    app.include_router(payments_router, prefix="/payments")
    yield app, stub_sqs, config


@pytest_asyncio.fixture
async def client(
    admin_app: tuple[FastAPI, AsyncMock, RazorpayConfig],
) -> AsyncIterator[httpx.AsyncClient]:
    app, _, _ = admin_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------- require_admin ----------


async def test_admin_route_403_when_no_header(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/payments/reconcile",
        params={"from_date": str(date.today()), "to_date": str(date.today())},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "admin_api_key_mismatch"


async def test_admin_route_403_when_wrong_key(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/payments/reconcile",
        params={"from_date": str(date.today()), "to_date": str(date.today())},
        headers={"x-admin-api-key": "wrong"},
    )
    assert response.status_code == 403


@pytest_asyncio.fixture
async def client_no_admin_key(
    live_pool,
    razorpay_client: RazorpayClient,
    fixed_resolver: FixedAmountResolver,
) -> AsyncIterator[httpx.AsyncClient]:
    """App with admin_api_key=None: every admin route returns 403 admin_api_disabled."""
    config = RazorpayConfig(
        tenant_id="t_noadmin",
        key_id="rzp_test_kid",
        key_secret="ksec",
        webhook_secret="wsec",
        db_dsn="postgresql://razorpay:razorpay@localhost:5432/razorpay_checkout",
        pool_min_size=1,
        pool_max_size=4,
        razorpay_api_timeout=2.0,
    )
    service = OrderService(
        pool=live_pool,
        config=config,
        client=razorpay_client,
        amount_resolver=fixed_resolver,
    )
    app = FastAPI()
    app.state.razorpay_config = config
    app.state.razorpay_pool = live_pool
    app.state.order_service = service
    app.state.razorpay_client = razorpay_client
    app.state.sqs_client = AsyncMock()
    app.state.fulfillment_queue_url = "x"
    app.include_router(payments_router, prefix="/payments")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_admin_disabled_returns_403(client_no_admin_key: httpx.AsyncClient) -> None:
    response = await client_no_admin_key.get(
        "/payments/reconcile",
        params={"from_date": str(date.today()), "to_date": str(date.today())},
        headers={"x-admin-api-key": "anything"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "admin_api_disabled"


# ---------- GET /payments/reconcile ----------


@respx.mock
async def test_reconcile_detects_missing_in_local_db(
    client: httpx.AsyncClient,
) -> None:
    respx.get("https://api.razorpay.com/v1/payments").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "items": [{"id": "pay_RZP_ONLY", "amount": 50000, "status": "captured"}],
            },
        )
    )
    response = await client.get(
        "/payments/reconcile",
        params={"from_date": "2026-05-01", "to_date": "2026-05-12"},
        headers={"x-admin-api-key": ADMIN_KEY},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_razorpay_payments_fetched"] == 1
    assert body["total_local_payments"] == 0
    assert len(body["mismatches"]) == 1
    assert body["mismatches"][0]["reason"] == "missing_in_local_db"
    assert body["mismatches"][0]["razorpay_payment_id"] == "pay_RZP_ONLY"


@respx.mock
async def test_reconcile_detects_amount_mismatch(
    client: httpx.AsyncClient,
    admin_app,
    live_pool,
) -> None:
    _, _, config = admin_app
    # Seed a local payment so the join finds something.
    async with live_pool.acquire() as conn:
        # Need an order row first because payments has FK to orders.
        order_id = await conn.fetchval(
            """
            INSERT INTO orders (tenant_id, receipt, amount, currency, correlation_id,
                                status, razorpay_order_id, paid_at)
            VALUES ($1, 'r-rec-amt', 50000, 'INR', 'corr-rec', 'paid', 'order_REC_AMT', NOW())
            RETURNING id
            """,
            config.tenant_id,
        )
        await conn.execute(
            """
            INSERT INTO payments
                (tenant_id, order_id, razorpay_payment_id, razorpay_order_id,
                 amount, currency, status, is_authoritative, captured_at)
            VALUES ($1, $2, 'pay_AMT_X', 'order_REC_AMT',
                    50000, 'INR', 'captured', TRUE, NOW())
            """,
            config.tenant_id,
            order_id,
        )

    respx.get("https://api.razorpay.com/v1/payments").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "items": [{"id": "pay_AMT_X", "amount": 99999, "status": "captured"}],
            },
        )
    )
    response = await client.get(
        "/payments/reconcile",
        params={"from_date": "2026-05-01", "to_date": "2026-05-12"},
        headers={"x-admin-api-key": ADMIN_KEY},
    )
    body = response.json()
    reasons = [m["reason"] for m in body["mismatches"]]
    assert "amount_mismatch" in reasons


# ---------- POST /payments/admin/events/{event_id}/replay ----------


async def _insert_event(
    live_pool,
    *,
    tenant_id: str,
    processing_status: str,
    event_id_text: str = "evt_X",
) -> uuid.UUID:
    from datetime import UTC, datetime

    async with live_pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO webhook_events
                (tenant_id, event_id, event, payload, correlation_id,
                 received_at, processing_status, attempts, error)
            VALUES ($1, $2, 'payment.captured', '{}'::jsonb, 'c1', $3, $4, 3, 'boom')
            RETURNING id
            """,
            tenant_id,
            event_id_text,
            datetime.now(tz=UTC),
            processing_status,
        )


async def test_replay_failed_event_returns_pending(
    client: httpx.AsyncClient,
    admin_app,
    live_pool,
) -> None:
    _, _, config = admin_app
    event_id = await _insert_event(
        live_pool,
        tenant_id=config.tenant_id,
        processing_status="failed",
        event_id_text="evt_REPLAY_F",
    )
    response = await client.post(
        f"/payments/admin/events/{event_id}/replay",
        headers={"x-admin-api-key": ADMIN_KEY},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["previous_status"] == "failed"
    assert body["new_status"] == "pending"

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT processing_status, attempts, error FROM webhook_events WHERE id = $1",
            event_id,
        )
    assert row["processing_status"] == "pending"
    assert row["attempts"] == 0
    assert row["error"] is None


async def test_replay_dead_event_returns_pending(
    client: httpx.AsyncClient,
    admin_app,
    live_pool,
) -> None:
    _, _, config = admin_app
    event_id = await _insert_event(
        live_pool,
        tenant_id=config.tenant_id,
        processing_status="dead",
        event_id_text="evt_REPLAY_D",
    )
    response = await client.post(
        f"/payments/admin/events/{event_id}/replay",
        headers={"x-admin-api-key": ADMIN_KEY},
    )
    assert response.status_code == 200
    assert response.json()["previous_status"] == "dead"


async def test_replay_unknown_event_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        f"/payments/admin/events/{uuid.uuid4()}/replay",
        headers={"x-admin-api-key": ADMIN_KEY},
    )
    assert response.status_code == 404


async def test_replay_pending_event_returns_409(
    client: httpx.AsyncClient,
    admin_app,
    live_pool,
) -> None:
    _, _, config = admin_app
    event_id = await _insert_event(
        live_pool,
        tenant_id=config.tenant_id,
        processing_status="pending",
        event_id_text="evt_REPLAY_PEND",
    )
    response = await client.post(
        f"/payments/admin/events/{event_id}/replay",
        headers={"x-admin-api-key": ADMIN_KEY},
    )
    assert response.status_code == 409


# ---------- POST /payments/admin/orders/{order_id}/retry-fulfillment ----------


async def _seed_paid_order(live_pool, tenant_id: str, *, with_payment: bool = True) -> uuid.UUID:
    async with live_pool.acquire() as conn:
        order_id = await conn.fetchval(
            """
            INSERT INTO orders
                (tenant_id, receipt, amount, currency, correlation_id,
                 status, razorpay_order_id, paid_at)
            VALUES ($1, $2, 50000, 'INR', 'c1', 'paid', $3, NOW())
            RETURNING id
            """,
            tenant_id,
            f"r-{uuid.uuid4()}",
            f"order_rzp_{uuid.uuid4().hex[:12]}",
        )
        if with_payment:
            await conn.execute(
                """
                INSERT INTO payments
                    (tenant_id, order_id, razorpay_payment_id, razorpay_order_id,
                     amount, currency, status, is_authoritative, captured_at)
                VALUES ($1, $2, $3, $4, 50000, 'INR', 'captured', TRUE, NOW())
                """,
                tenant_id,
                order_id,
                f"pay_rzp_{uuid.uuid4().hex[:12]}",
                f"order_rzp_{uuid.uuid4().hex[:12]}",
            )
    return order_id


async def test_retry_fulfillment_paid_order_publishes_sqs(
    client: httpx.AsyncClient,
    admin_app,
    live_pool,
) -> None:
    _, stub_sqs, config = admin_app
    order_id = await _seed_paid_order(live_pool, config.tenant_id)
    response = await client.post(
        f"/payments/admin/orders/{order_id}/retry-fulfillment",
        headers={"x-admin-api-key": ADMIN_KEY},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sqs_message_id"]
    assert body["previous_status"] == "paid"

    stub_sqs.send_message.assert_awaited_once()
    sent = stub_sqs.send_message.await_args.kwargs
    assert sent["MessageGroupId"] == str(order_id)

    async with live_pool.acquire() as conn:
        evt = await conn.fetchrow(
            "SELECT trigger, metadata FROM order_events "
            "WHERE order_id = $1 AND trigger = 'admin_retry'",
            order_id,
        )
    assert evt is not None
    assert evt["metadata"]["sqs_message_id"] == body["sqs_message_id"]


async def test_retry_fulfillment_unknown_order_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        f"/payments/admin/orders/{uuid.uuid4()}/retry-fulfillment",
        headers={"x-admin-api-key": ADMIN_KEY},
    )
    assert response.status_code == 404


async def test_retry_fulfillment_wrong_state_returns_409(
    client: httpx.AsyncClient,
    admin_app,
    live_pool,
) -> None:
    _, _, config = admin_app
    # Seed an order in `created` state which is not eligible.
    async with live_pool.acquire() as conn:
        order_id = await conn.fetchval(
            """
            INSERT INTO orders
                (tenant_id, receipt, amount, currency, correlation_id,
                 status, razorpay_order_id)
            VALUES ($1, $2, 50000, 'INR', 'c1', 'created', $3)
            RETURNING id
            """,
            config.tenant_id,
            f"r-{uuid.uuid4()}",
            f"order_rzp_{uuid.uuid4().hex[:12]}",
        )
    response = await client.post(
        f"/payments/admin/orders/{order_id}/retry-fulfillment",
        headers={"x-admin-api-key": ADMIN_KEY},
    )
    assert response.status_code == 409
