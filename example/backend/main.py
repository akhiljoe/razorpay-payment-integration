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

import aioboto3
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse

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


@app.get("/dev/test-page", response_class=HTMLResponse)
async def test_page(_request: Request) -> FileResponse:
    """Serve the dev test page HTML. See example/frontend/test_page.html."""
    return FileResponse(FRONTEND_DIR / "test_page.html", media_type="text/html")


@app.get("/dev/widget.js")
async def widget_js(_request: Request) -> FileResponse:
    """Serve the built widget bundle.

    Requires `npm run build` in `packages/checkout-widget/` first.
    """
    return FileResponse(WIDGET_DIST / "vanilla.mjs", media_type="text/javascript")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
