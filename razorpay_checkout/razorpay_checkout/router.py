"""FastAPI router and DI for the order routes.

Host apps mount the router under any prefix:

    from razorpay_checkout import payments_router
    app.include_router(payments_router, prefix="/payments")

Required app state (set during startup):

    app.state.razorpay_config: RazorpayConfig
    app.state.razorpay_pool: asyncpg.Pool
    app.state.order_service: OrderService

Subsequent phases register the webhook routes on the same router.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.correlation import correlation_id_dependency
from razorpay_checkout.logging_config import get_logger
from razorpay_checkout.models import (
    CreateOrderRequest,
    CreateOrderResponse,
    OrderStatusResponse,
    VerifyCallbackRequest,
    VerifyCallbackResponse,
)
from razorpay_checkout.orders import OrderService
from razorpay_checkout.signatures import verify_payment_signature
from razorpay_checkout.state_machine import OrderStatus

_log = get_logger("razorpay_checkout.router")

payments_router = APIRouter()


# ---------- Dependencies ----------


def get_config(request: Request) -> RazorpayConfig:
    config = getattr(request.app.state, "razorpay_config", None)
    if config is None:
        raise RuntimeError(
            "RazorpayConfig is not registered on app.state.razorpay_config"
        )
    return config  # type: ignore[no-any-return]


def get_order_service(request: Request) -> OrderService:
    service = getattr(request.app.state, "order_service", None)
    if service is None:
        raise RuntimeError(
            "OrderService is not registered on app.state.order_service"
        )
    return service  # type: ignore[no-any-return]


# ---------- POST /orders ----------


@payments_router.post(
    "/orders",
    response_model=CreateOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_order(
    payload: CreateOrderRequest,
    correlation_id: Annotated[str, Depends(correlation_id_dependency)],
    config: Annotated[RazorpayConfig, Depends(get_config)],
    service: Annotated[OrderService, Depends(get_order_service)],
) -> CreateOrderResponse:
    """Idempotent order creation. See DESIGN_v5 Section 2.1."""
    from razorpay_checkout.client import RazorpayUnavailableError

    try:
        record = await service.create_or_get(payload, correlation_id=correlation_id)
    except RazorpayUnavailableError as exc:
        _log.warning("create_order_503", reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="razorpay_api_unavailable",
        ) from exc

    if record.razorpay_order_id is None:
        # Should not happen on success; surface as 503 to trigger client retry.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="razorpay_order_pending",
        )

    return CreateOrderResponse(
        order_id=record.id,
        razorpay_order_id=record.razorpay_order_id,
        amount=record.amount,
        currency=record.currency,
        key_id=config.key_id,
    )


# ---------- GET /orders/{order_id} ----------


@payments_router.get(
    "/orders/{order_id}",
    response_model=OrderStatusResponse,
)
async def get_order(
    order_id: UUID,
    config: Annotated[RazorpayConfig, Depends(get_config)],
    service: Annotated[OrderService, Depends(get_order_service)],
) -> OrderStatusResponse:
    """Tenant-scoped order lookup. Returns 404 on unknown id or cross-tenant."""
    record = await service.get_by_id(order_id, config.tenant_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="order_not_found",
        )
    return OrderStatusResponse(
        order_id=record.id,
        razorpay_order_id=record.razorpay_order_id,
        status=record.status,
        amount=record.amount,
        currency=record.currency,
        created_at=record.created_at,
        attempted_at=record.attempted_at,
        authorized_at=record.authorized_at,
        paid_at=record.paid_at,
        fulfilled_at=record.fulfilled_at,
        fulfillment_failed_at=record.fulfillment_failed_at,
        failed_at=record.failed_at,
        refunded_at=record.refunded_at,
    )


# ---------- POST /verify-callback ----------


@payments_router.post(
    "/verify-callback",
    response_model=VerifyCallbackResponse,
)
async def verify_callback(
    payload: VerifyCallbackRequest,
    config: Annotated[RazorpayConfig, Depends(get_config)],
    service: Annotated[OrderService, Depends(get_order_service)],
) -> VerifyCallbackResponse | JSONResponse:
    """Verify the client-side payment signature and confirm order is not terminal-failed.

    Returns 200 with `verified=true` on success. Returns 400 with the failure
    reason on signature mismatch. Returns 200 with `verified=false` on missing
    or terminally-failed order (these are response-level, not transport-level
    errors).
    """
    if not verify_payment_signature(
        payload.razorpay_order_id,
        payload.razorpay_payment_id,
        payload.razorpay_signature,
        config.key_secret.get_secret_value(),
    ):
        body = VerifyCallbackResponse(
            verified=False, order_id="", reason="signature_mismatch"
        )
        return JSONResponse(status_code=400, content=body.model_dump())

    record = await service.get_by_razorpay_order_id(
        payload.razorpay_order_id, config.tenant_id
    )
    if record is None:
        return VerifyCallbackResponse(
            verified=False, order_id="", reason="order_not_found"
        )

    if record.status == OrderStatus.FAILED:
        return VerifyCallbackResponse(
            verified=False, order_id=str(record.id), reason="terminal_failed"
        )

    return VerifyCallbackResponse(
        verified=True, order_id=str(record.id), reason=None
    )


# ---------- POST /webhook/{tenant_id} (Webhook Producer) ----------

from razorpay_checkout.webhook_producer import (  # noqa: E402
    MalformedPayloadError,
    PublishFailureError,
    SignatureMismatchError,
    UnknownTenantError,
    WebhookProducer,
)


def get_webhook_producer(request: Request) -> WebhookProducer:
    producer = getattr(request.app.state, "webhook_producer", None)
    if producer is None:
        raise RuntimeError(
            "WebhookProducer is not registered on app.state.webhook_producer"
        )
    return producer  # type: ignore[no-any-return]


@payments_router.post(
    "/webhook/{tenant_id}",
    status_code=status.HTTP_200_OK,
    response_model=None,
)
async def webhook(
    tenant_id: str,
    request: Request,
    producer: Annotated[WebhookProducer, Depends(get_webhook_producer)],
) -> JSONResponse:
    """Razorpay webhook entry point.

    DESIGN_v5 Section 2.1: returns 200 queued in under 500 ms. The producer
    holds no DB credentials; downstream processing happens in the consumer
    Lambda. Unknown tenant returns 400 (not 404) per the design's
    publicly-enumerable-URL rationale.
    """
    raw_body = await request.body()
    header_signature = request.headers.get("x-razorpay-signature", "")

    try:
        ack = await producer.process(
            raw_body=raw_body,
            header_signature=header_signature,
            tenant_id=tenant_id,
        )
    except UnknownTenantError:
        return JSONResponse(status_code=400, content={"detail": "unknown_tenant"})
    except SignatureMismatchError:
        return JSONResponse(status_code=400, content={"detail": "signature_mismatch"})
    except MalformedPayloadError as exc:
        return JSONResponse(
            status_code=400, content={"detail": "malformed_payload", "reason": exc.reason}
        )
    except PublishFailureError as exc:
        # Producer could not publish to SQS. Tell Razorpay to retry.
        _log.error("webhook_route_503", reason=exc.reason)
        return JSONResponse(
            status_code=503, content={"detail": "queue_unavailable"}
        )

    return JSONResponse(
        status_code=200,
        content={"status": ack.status, "correlation_id": ack.correlation_id},
        headers={"X-Correlation-Id": ack.correlation_id},
    )
