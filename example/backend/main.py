"""Developer test backend.

Mounts `payments_router` plus a `/dev/test-page` and `/dev/widget.js` route
for manual end-to-end smoke testing with Razorpay test cards.

Not for production. Run with:

    cd example/backend && uvicorn main:app --reload --port 8000

Required env (typically read from a sibling .env file):
- RAZORPAY_TENANT_ID, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
  RAZORPAY_WEBHOOK_SECRET, RAZORPAY_DB_DSN
- AWS_ENDPOINT_URL (LocalStack), AWS_DEFAULT_REGION, AWS creds
- FULFILLMENT_QUEUE_URL after `ensure_fulfillment_queues` on LocalStack
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

# Load .env into os.environ so AWS_* (read directly by aioboto3) and any other
# non-RAZORPAY_-prefixed values are visible. pydantic-settings only auto-loads
# RAZORPAY_-prefixed keys via RazorpayConfig.
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

import hashlib
import hmac
import json
import time
import uuid

import aioboto3
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


# ============================================================
# Feature flags — environment-gated route exposure.
#
# RAZORPAY_DEV_ROUTES_ENABLED:
#   When "true" (case-insensitive), the /dev/* routes (dashboard, shop,
#   test-page, simulate, reset, flow, queues) are reachable. Default is
#   FALSE so production deployments do not expose destructive endpoints
#   on a public Function URL.
#
#   Set to "true" in `example/backend/.env` for local development.
#   Leave unset (or set to anything else) in production.
#
# Use `_require_dev_routes()` as a FastAPI dependency on every /dev/*
# handler. Disabled routes return 404 (not 403) so attackers can't
# enumerate which endpoints exist.
# ============================================================
def _dev_routes_enabled() -> bool:
    return os.environ.get("RAZORPAY_DEV_ROUTES_ENABLED", "").lower() == "true"


def _require_dev_routes() -> None:
    if not _dev_routes_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _require_admin_key(
    x_admin_api_key: str | None = Header(default=None, alias="X-Admin-Api-Key"),
    request: Request = None,  # noqa: B008 - FastAPI injects
) -> None:
    """Header-based admin auth for /admin/* routes.

    Reads `admin_api_key` from `RazorpayConfig`. If unset, the route is
    treated as closed (403 always). Compare with `hmac.compare_digest` to
    avoid timing leaks.
    """
    config = request.app.state.razorpay_config
    expected = config.admin_api_key.get_secret_value() if config.admin_api_key else None
    if not expected:
        raise HTTPException(status_code=403, detail="admin disabled")
    if not x_admin_api_key or not hmac.compare_digest(expected, x_admin_api_key):
        raise HTTPException(status_code=403, detail="invalid admin key")

from razorpay_checkout import (
    DefaultFulfillmentHook,
    FixedAmountResolver,
    OrderService,
    RazorpayClient,
    RazorpayConfig,
    TenantConfigRegistry,
    WebhookProducer,
    configure_logging,
    configure_otel,
    ensure_fulfillment_queues,
    ensure_webhook_events_queues,
    payments_router,
)
from razorpay_checkout.db import create_pool

HERE = Path(__file__).resolve().parent
FRONTEND_DIR = HERE.parent / "frontend"
WIDGET_DIST = HERE.parent.parent / "packages" / "checkout-widget" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire all the bits at startup, tear down on shutdown."""
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    config = RazorpayConfig()  # type: ignore[call-arg]
    configure_otel(config)

    pool = await create_pool(config)
    rzp_client = RazorpayClient(config)
    service = OrderService(
        pool=pool,
        config=config,
        client=rzp_client,
        amount_resolver=FixedAmountResolver(
            amount=int(os.environ.get("DEV_TEST_AMOUNT_PAISE", "50000"))
        ),
    )

    session = aioboto3.Session()
    sqs_cm = session.client(
        "sqs",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-south-1"),
    )
    sqs = await sqs_cm.__aenter__()
    webhook_queue_url, _ = await ensure_webhook_events_queues(sqs)
    fulfillment_queue_url, _ = await ensure_fulfillment_queues(sqs)

    registry = TenantConfigRegistry([config])
    producer = WebhookProducer(
        tenant_registry=registry,
        sqs_client=sqs,
        queue_url=webhook_queue_url,
    )

    app.state.razorpay_config = config
    app.state.razorpay_pool = pool
    app.state.order_service = service
    app.state.razorpay_client = rzp_client
    app.state.sqs_client = sqs
    app.state.fulfillment_queue_url = fulfillment_queue_url
    app.state.webhook_producer = producer
    app.state.hook = DefaultFulfillmentHook()

    yield

    await pool.close()
    await rzp_client.aclose()
    await sqs_cm.__aexit__(None, None, None)


app = FastAPI(lifespan=lifespan, title="Razorpay Checkout dev backend")
app.include_router(payments_router, prefix="/payments")


@app.get("/dev/test-page", response_class=HTMLResponse, dependencies=[Depends(_require_dev_routes)])
async def test_page(_request: Request) -> FileResponse:
    """Serve the dev test page HTML. See example/frontend/test_page.html."""
    return FileResponse(FRONTEND_DIR / "test_page.html", media_type="text/html")


@app.get("/dev/shop", response_class=HTMLResponse, dependencies=[Depends(_require_dev_routes)])
async def dev_shop(_request: Request) -> FileResponse:
    """Buyer-perspective storefront simulation. Shows a product, opens the
    real Razorpay checkout modal, and walks through the confirmation
    timeline (paid -> webhook -> consumer -> fulfilled)."""
    return FileResponse(FRONTEND_DIR / "shop.html", media_type="text/html")


@app.get("/dev/widget.js", dependencies=[Depends(_require_dev_routes)])
async def widget_js(_request: Request) -> FileResponse:
    """Serve the built widget bundle.

    Requires `npm run build` in `packages/checkout-widget/` first.
    """
    return FileResponse(WIDGET_DIST / "vanilla.mjs", media_type="text/javascript")


@app.get("/health")
async def health() -> dict[str, str]:
    """Public, unauthenticated. Reach for this from load balancers + smoke tests."""
    return {"status": "ok"}


# ---------- /dev/dashboard live state UI (DEV ONLY) ----------


@app.get("/dev/dashboard", response_class=HTMLResponse, dependencies=[Depends(_require_dev_routes)])
async def dev_dashboard(_request: Request) -> FileResponse:
    """Single-page dev UI. Local-dev only — gated by RAZORPAY_DEV_ROUTES_ENABLED."""
    return FileResponse(FRONTEND_DIR / "dashboard.html", media_type="text/html")


# ---------- /admin/dashboard (PROD-SAFE read-only) ----------


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(_request: Request) -> FileResponse:
    """Production-safe read-only dashboard. Serves the HTML unauthenticated
    (it's static); the API endpoints it calls require X-Admin-Api-Key."""
    return FileResponse(FRONTEND_DIR / "admin-dashboard.html", media_type="text/html")


def _row_to_dict(row: object) -> dict:
    return dict(row) if row is not None else {}


def _isoformat_dict(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


@app.get("/dev/flow", dependencies=[Depends(_require_dev_routes)])
async def dev_flow_index(request: Request) -> JSONResponse:
    """List recent orders for the dashboard's dropdown."""
    pool = request.app.state.razorpay_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, razorpay_order_id, receipt, status, amount, created_at
              FROM orders
             ORDER BY created_at DESC
             LIMIT 20
            """
        )
    return JSONResponse(
        {"orders": [_isoformat_dict(_row_to_dict(r)) for r in rows]}
    )


@app.get("/dev/flow/{order_id}", dependencies=[Depends(_require_dev_routes)])
async def dev_flow_detail(order_id: str, request: Request) -> JSONResponse:
    # Legacy endpoint, now delegates to the aggregated payload's selected
    # block. Ensures the same JSONB filter (webhook_events scoped to this
    # order's razorpay_order_id) applies whether the dashboard pulls via
    # /dev/flow/:id or /dev/dashboard-data. Returns the selected detail
    # only (no kpis/queues/orders list) for compatibility.
    full = await _aggregated_dashboard_payload(
        request, order_id=order_id, recent_limit=0
    )
    if full["selected"] is None:
        raise HTTPException(404, "order not found")
    return JSONResponse(full["selected"])


@app.get("/dev/flow/{order_id}/__legacy_disabled", dependencies=[Depends(_require_dev_routes)])
async def _dev_flow_detail_legacy(order_id: str, request: Request) -> JSONResponse:
    """Aggregated view of one order: row, payments, refunds, webhook_events,
    order_events. The dashboard polls this every second."""
    pool = request.app.state.razorpay_pool
    try:
        oid = uuid.UUID(order_id)
    except ValueError as exc:
        raise HTTPException(400, f"invalid order_id: {exc}") from exc

    async with pool.acquire() as conn:
        order = await conn.fetchrow(
            "SELECT * FROM orders WHERE id = $1", oid
        )
        if order is None:
            raise HTTPException(404, "order not found")
        payments = await conn.fetch(
            "SELECT * FROM payments WHERE order_id = $1 ORDER BY created_at",
            oid,
        )
        refunds = await conn.fetch(
            "SELECT * FROM refunds WHERE order_id = $1 ORDER BY created_at",
            oid,
        )
        events = await conn.fetch(
            "SELECT * FROM order_events WHERE order_id = $1 ORDER BY created_at",
            oid,
        )
        wh = await conn.fetch(
            """
            SELECT id, event_id, event, processing_status, received_at,
                   correlation_id
              FROM webhook_events
             WHERE tenant_id = $1
             ORDER BY received_at DESC
             LIMIT 20
            """,
            order["tenant_id"],
        )

    return JSONResponse(
        {
            "order": _isoformat_dict(_row_to_dict(order)),
            "payments": [_isoformat_dict(_row_to_dict(r)) for r in payments],
            "refunds": [_isoformat_dict(_row_to_dict(r)) for r in refunds],
            "order_events": [_isoformat_dict(_row_to_dict(r)) for r in events],
            "webhook_events": [_isoformat_dict(_row_to_dict(r)) for r in wh],
        }
    )


@app.get("/dev/queues", dependencies=[Depends(_require_dev_routes)])
async def dev_queues(request: Request) -> JSONResponse:
    """SQS queue depths so the dashboard can show inflight work."""
    sqs = request.app.state.sqs_client
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    queues = [
        "webhook-events.fifo",
        "webhook-events-dlq.fifo",
        "fulfillment-queue.fifo",
        "fulfillment-dlq.fifo",
    ]
    out: dict[str, dict] = {}
    for name in queues:
        try:
            url_resp = await sqs.get_queue_url(QueueName=name)
            attrs = await sqs.get_queue_attributes(
                QueueUrl=url_resp["QueueUrl"],
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )
            a = attrs.get("Attributes", {})
            out[name] = {
                "visible": int(a.get("ApproximateNumberOfMessages", 0)),
                "inflight": int(a.get("ApproximateNumberOfMessagesNotVisible", 0)),
            }
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)}
    return JSONResponse({"endpoint": endpoint, "queues": out})


# ---------- /dev/simulate one-click webhook fire ----------


def _build_simulator_payload(
    event_type: str,
    *,
    order_id: str,
    payment_id: str,
    amount: int,
    refund_id: str | None,
    event_id: str,
) -> dict:
    """Mirrors scripts/razorpay-simulator.build_payload. Kept inline so the
    dashboard does not have to shell out."""
    now = int(time.time())

    def _payment(status: str, captured: bool) -> dict:
        return {
            "id": payment_id,
            "entity": "payment",
            "amount": amount,
            "currency": "INR",
            "status": status,
            "order_id": order_id,
            "method": "card",
            "captured": captured,
            "captured_at": now if captured else None,
            "created_at": now,
        }

    base: dict = {
        "id": event_id,
        "entity": "event",
        "event": event_type,
        "created_at": now,
    }

    if event_type == "payment.authorized":
        base["contains"] = ["payment"]
        base["payload"] = {"payment": {"entity": _payment("authorized", False)}}
        return base
    if event_type == "payment.captured":
        base["contains"] = ["payment"]
        base["payload"] = {"payment": {"entity": _payment("captured", True)}}
        return base
    if event_type == "payment.failed":
        base["contains"] = ["payment"]
        base["payload"] = {"payment": {"entity": _payment("failed", False)}}
        return base
    if event_type == "refund.processed":
        if refund_id is None:
            refund_id = f"rfnd_{uuid.uuid4().hex[:14]}"
        base["contains"] = ["refund", "payment"]
        base["payload"] = {
            "refund": {
                "entity": {
                    "id": refund_id,
                    "entity": "refund",
                    "amount": amount,
                    "currency": "INR",
                    "payment_id": payment_id,
                    "order_id": order_id,
                    "status": "processed",
                    "created_at": now,
                }
            },
            "payment": {"entity": _payment("refunded", True)},
        }
        return base
    if event_type == "order.paid":
        base["contains"] = ["order", "payment"]
        base["payload"] = {
            "order": {
                "entity": {
                    "id": order_id,
                    "entity": "order",
                    "amount": amount,
                    "amount_paid": amount,
                    "amount_due": 0,
                    "currency": "INR",
                    "status": "paid",
                    "created_at": now,
                }
            },
            "payment": {"entity": _payment("captured", True)},
        }
        return base
    raise HTTPException(400, f"unsupported event_type {event_type!r}")


@app.post("/dev/simulate", dependencies=[Depends(_require_dev_routes)])
async def dev_simulate(payload: dict, request: Request) -> JSONResponse:
    """Build, sign, and POST a canonical Razorpay webhook to the local
    producer route. The dashboard wires every "Fire X" button to this.

    Body: {event_type, razorpay_order_id, razorpay_payment_id, amount?, refund_id?}
    """
    event_type = payload.get("event_type")
    rzp_order_id = payload.get("razorpay_order_id")
    rzp_payment_id = payload.get("razorpay_payment_id") or f"pay_{uuid.uuid4().hex[:14]}"
    amount = int(payload.get("amount", 50000))
    refund_id = payload.get("refund_id")

    if not event_type or not rzp_order_id:
        raise HTTPException(400, "event_type and razorpay_order_id are required")

    config: RazorpayConfig = request.app.state.razorpay_config
    secret = config.webhook_secret.get_secret_value()
    # Caller can supply an event_id to exercise fast-path duplicate detection.
    event_id = payload.get("event_id") or f"evt_{uuid.uuid4().hex[:24]}"
    # Caller can request a bad signature to exercise producer-side rejection.
    force_bad_signature = bool(payload.get("force_bad_signature"))

    event_payload = _build_simulator_payload(
        event_type,
        order_id=rzp_order_id,
        payment_id=rzp_payment_id,
        amount=amount,
        refund_id=refund_id,
        event_id=event_id,
    )
    body = json.dumps(event_payload, separators=(",", ":")).encode("utf-8")
    if force_bad_signature:
        signature = "deadbeef" * 8
    else:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    target = f"http://localhost:8000/payments/webhook/{config.tenant_id}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            target,
            content=body,
            headers={
                "x-razorpay-signature": signature,
                "content-type": "application/json",
            },
        )

    try:
        producer_response = resp.json()
    except ValueError:
        producer_response = {"text": resp.text}

    return JSONResponse(
        {
            "event_id": event_id,
            "event_type": event_type,
            "razorpay_payment_id": rzp_payment_id,
            "amount": amount,
            "status_code": resp.status_code,
            "producer_response": producer_response,
        }
    )


# ---------- /dev/reset wipe DB + queues ----------


@app.post("/dev/reset", dependencies=[Depends(_require_dev_routes)])
async def dev_reset(request: Request) -> JSONResponse:
    """Truncate tenant-scoped tables and purge SQS queues. Re-seed tenant."""
    pool = request.app.state.razorpay_pool
    sqs = request.app.state.sqs_client
    config: RazorpayConfig = request.app.state.razorpay_config

    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE order_events, webhook_events, refunds, payments, orders "
            "RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            config.tenant_id,
            f"Dev tenant {config.tenant_id}",
        )

    purged = []
    for name in (
        "webhook-events.fifo",
        "webhook-events-dlq.fifo",
        "fulfillment-queue.fifo",
        "fulfillment-dlq.fifo",
    ):
        try:
            url_resp = await sqs.get_queue_url(QueueName=name)
            await sqs.purge_queue(QueueUrl=url_resp["QueueUrl"])
            purged.append(name)
        except Exception as exc:  # noqa: BLE001
            purged.append(f"{name}: {exc}")

    return JSONResponse({"truncated": True, "queues_purged": purged})


# ============================================================
# Aggregated read endpoints — KPIs + dashboard data.
#
# These are used by BOTH:
#   - /dev/dashboard-data (gated by RAZORPAY_DEV_ROUTES_ENABLED)
#   - /admin/dashboard-data (gated by X-Admin-Api-Key)
#
# Centralizing the SQL here means the dev and prod dashboards stay in
# lockstep. The shared helper does one Postgres round-trip with WITH
# clauses + one SQS GetQueueAttributes per queue, replacing what was
# previously 3 separate API calls per dashboard refresh.
# ============================================================


async def _aggregated_dashboard_payload(
    request: Request,
    *,
    order_id: str | None = None,
    tenant_id: str | None = None,
    recent_limit: int = 10,
    status_in: list[str] | None = None,
    today_only: bool = False,
    stuck_paid: bool = False,
) -> dict:
    pool = request.app.state.razorpay_pool
    sqs = request.app.state.sqs_client
    config: RazorpayConfig = request.app.state.razorpay_config

    payload: dict = {
        "kpis": {},
        "queues": {},
        "orders": [],
        "selected": None,
        "tenant_id_default": config.tenant_id,
    }

    async with pool.acquire() as conn:
        # Today's KPIs in one query. UTC day; the dashboard renders local.
        kpi_row = await conn.fetchrow(
            """
            WITH today AS (
              SELECT status FROM orders
               WHERE created_at >= date_trunc('day', NOW())
                 AND ($1::text IS NULL OR tenant_id = $1)
            )
            SELECT
              COUNT(*) FILTER (WHERE status = 'paid')                AS today_paid,
              COUNT(*) FILTER (WHERE status = 'fulfilled')           AS today_fulfilled,
              COUNT(*) FILTER (WHERE status = 'failed')              AS today_failed,
              COUNT(*) FILTER (WHERE status = 'fulfillment_failed')  AS today_fulfillment_failed,
              COUNT(*) FILTER (WHERE status = 'refunded')            AS today_refunded,
              COUNT(*) FILTER (WHERE status IN ('created','attempted','authorized')) AS in_flight,
              COUNT(*) AS today_total
              FROM today
            """,
            tenant_id,
        )
        payload["kpis"] = dict(kpi_row) if kpi_row else {}

        # Stuck-paid: paid > 15 min, not yet fulfilled.
        stuck = await conn.fetchval(
            """
            SELECT COUNT(*) FROM orders
             WHERE status = 'paid'
               AND paid_at < NOW() - INTERVAL '15 minutes'
               AND ($1::text IS NULL OR tenant_id = $1)
            """,
            tenant_id,
        )
        payload["kpis"]["stuck_paid"] = int(stuck or 0)

        # Recent orders. Tenant + optional status / today / stuck filters.
        # Filters are AND-combined; status_in is OR within itself.
        filter_clauses = ["($1::text IS NULL OR tenant_id = $1)"]
        filter_args: list = [tenant_id]
        if status_in:
            placeholders = ", ".join(
                f"${i + 2}" for i in range(len(status_in))
            )
            filter_clauses.append(f"status IN ({placeholders})")
            filter_args.extend(status_in)
        if today_only:
            filter_clauses.append("created_at >= date_trunc('day', NOW())")
        if stuck_paid:
            filter_clauses.append(
                "status = 'paid' AND paid_at < NOW() - INTERVAL '15 minutes'"
            )
        where_sql = " AND ".join(filter_clauses)
        rows = await conn.fetch(
            f"""
            SELECT id, tenant_id, razorpay_order_id, receipt, status, amount, created_at
              FROM orders
             WHERE {where_sql}
             ORDER BY created_at DESC
             LIMIT {int(recent_limit)}
            """,
            *filter_args,
        )
        payload["orders"] = [_isoformat_dict(_row_to_dict(r)) for r in rows]

        # Selected-order detail (if order_id provided).
        if order_id:
            try:
                oid = uuid.UUID(order_id)
            except ValueError:
                oid = None
            if oid is not None:
                order = await conn.fetchrow(
                    "SELECT * FROM orders WHERE id = $1", oid
                )
                if order is not None:
                    payments = await conn.fetch(
                        "SELECT * FROM payments WHERE order_id = $1 ORDER BY created_at",
                        oid,
                    )
                    refunds = await conn.fetch(
                        "SELECT * FROM refunds WHERE order_id = $1 ORDER BY created_at",
                        oid,
                    )
                    events = await conn.fetch(
                        "SELECT * FROM order_events WHERE order_id = $1 ORDER BY created_at",
                        oid,
                    )
                    # Webhook events scoped to THIS order. The payload column
                    # is JSONB; Razorpay puts the order id under different
                    # paths per event type:
                    #   payment.captured / .authorized / .failed:
                    #     payload->'payload'->'payment'->'entity'->>'order_id'
                    #   refund.processed:
                    #     payload->'payload'->'refund'->'entity'->>'order_id'
                    #   order.paid:
                    #     payload->'payload'->'order'->'entity'->>'id'
                    # Filtering server-side avoids showing other orders'
                    # webhooks in the per-order journey view.
                    wh = await conn.fetch(
                        """
                        SELECT id, event_id, event, processing_status, received_at,
                               correlation_id
                          FROM webhook_events
                         WHERE tenant_id = $1
                           AND (
                             payload->'payload'->'payment'->'entity'->>'order_id' = $2
                             OR payload->'payload'->'refund'->'entity'->>'order_id' = $2
                             OR payload->'payload'->'order'->'entity'->>'id' = $2
                           )
                         ORDER BY received_at DESC
                         LIMIT 20
                        """,
                        order["tenant_id"],
                        order["razorpay_order_id"],
                    )
                    payload["selected"] = {
                        "order": _isoformat_dict(_row_to_dict(order)),
                        "payments": [_isoformat_dict(_row_to_dict(r)) for r in payments],
                        "refunds": [_isoformat_dict(_row_to_dict(r)) for r in refunds],
                        "order_events": [_isoformat_dict(_row_to_dict(r)) for r in events],
                        "webhook_events": [_isoformat_dict(_row_to_dict(r)) for r in wh],
                    }

    # Queue depths (4 SQS calls, ~100ms total in prod).
    for name in (
        "webhook-events.fifo",
        "webhook-events-dlq.fifo",
        "fulfillment-queue.fifo",
        "fulfillment-dlq.fifo",
    ):
        try:
            url_resp = await sqs.get_queue_url(QueueName=name)
            attrs = await sqs.get_queue_attributes(
                QueueUrl=url_resp["QueueUrl"],
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )
            a = attrs.get("Attributes", {})
            payload["queues"][name] = {
                "visible": int(a.get("ApproximateNumberOfMessages", 0)),
                "inflight": int(a.get("ApproximateNumberOfMessagesNotVisible", 0)),
            }
        except Exception as exc:  # noqa: BLE001
            payload["queues"][name] = {"error": str(exc)}

    return payload


@app.get("/dev/dashboard-data", dependencies=[Depends(_require_dev_routes)])
async def dev_dashboard_data(
    request: Request,
    order_id: str | None = None,
    tenant_id: str | None = None,
    status_in: str | None = None,
    today_only: bool = False,
    stuck_paid: bool = False,
) -> JSONResponse:
    """Single aggregated endpoint replacing /dev/flow + /dev/flow/:id +
    /dev/queues. Reduces dashboard cold paths from 3 invocations to 1.

    Filters: status_in (CSV of OrderStatus values), today_only, stuck_paid.
    """
    status_list = [s.strip() for s in status_in.split(",")] if status_in else None
    return JSONResponse(
        await _aggregated_dashboard_payload(
            request,
            order_id=order_id,
            tenant_id=tenant_id,
            status_in=status_list,
            today_only=today_only,
            stuck_paid=stuck_paid,
        )
    )


# ============================================================
# /admin/* — production-safe read-only routes.
# All require X-Admin-Api-Key header. No simulate/reset.
# ============================================================


@app.get("/admin/dashboard-data", dependencies=[Depends(_require_admin_key)])
async def admin_dashboard_data(
    request: Request,
    order_id: str | None = None,
    tenant_id: str | None = None,
    status_in: str | None = None,
    today_only: bool = False,
    stuck_paid: bool = False,
) -> JSONResponse:
    """Aggregated read-only dashboard data. Same shape as
    /dev/dashboard-data but gated by admin key. Safe to expose on a
    production Function URL."""
    status_list = [s.strip() for s in status_in.split(",")] if status_in else None
    return JSONResponse(
        await _aggregated_dashboard_payload(
            request,
            order_id=order_id,
            tenant_id=tenant_id,
            status_in=status_list,
            today_only=today_only,
            stuck_paid=stuck_paid,
        )
    )


@app.get("/admin/kpis", dependencies=[Depends(_require_admin_key)])
async def admin_kpis(request: Request, tenant_id: str | None = None) -> JSONResponse:
    """Lightweight KPI-only endpoint for tight loops or external monitors.

    Returns today's counts + stuck-paid + queue depths. Skips the orders
    list and selected-order detail to keep payload tiny.
    """
    full = await _aggregated_dashboard_payload(
        request, order_id=None, tenant_id=tenant_id, recent_limit=0
    )
    return JSONResponse({"kpis": full["kpis"], "queues": full["queues"]})


@app.get("/admin/orders/{order_id}", dependencies=[Depends(_require_admin_key)])
async def admin_order_detail(order_id: str, request: Request) -> JSONResponse:
    """Single-order flow data. Admin-gated read-only view of one order
    + its events, webhooks, payments, refunds."""
    full = await _aggregated_dashboard_payload(
        request, order_id=order_id, recent_limit=0
    )
    if full["selected"] is None:
        raise HTTPException(404, "order not found")
    return JSONResponse(full["selected"])
