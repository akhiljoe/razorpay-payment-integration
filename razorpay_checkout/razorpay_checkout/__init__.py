"""Razorpay payment integration module.

Public re-exports for host apps:

- `payments_router` for FastAPI mounting.
- `RazorpayConfig` and `TenantConfigRegistry` for per-tenant setup.
- `OrderService` and `RazorpayClient` for direct use or test fixtures.
- `AmountResolver`, `FixedAmountResolver` for the pluggable pricing path.
- `WebhookProducer` and webhook exception classes for the producer Lambda.
- SQS publishers and bootstrap helpers from `queue`.
- State machine types so host apps can reason about transitions if needed.
"""

from razorpay_checkout.amounts import (
    AmountResolutionError,
    AmountResolver,
    FixedAmountResolver,
)
from razorpay_checkout.client import (
    RazorpayAPIError,
    RazorpayClient,
    RazorpayUnavailableError,
)
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.correlation import (
    correlation_id_dependency,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from razorpay_checkout.logging_config import configure_logging, get_logger, register_secret
from razorpay_checkout.orders import OrderNotFoundError, OrderService
from razorpay_checkout.queue import (
    FULFILLMENT_DLQ_NAME,
    FULFILLMENT_QUEUE_NAME,
    WEBHOOK_EVENTS_DLQ_NAME,
    WEBHOOK_EVENTS_QUEUE_NAME,
    aws_endpoint_url,
    build_webhook_envelope,
    ensure_fulfillment_queues,
    ensure_webhook_events_queues,
    publish_fulfillment_message,
    publish_webhook_event,
)
from razorpay_checkout.router import payments_router
from razorpay_checkout.state_machine import (
    ALLOWED_TRANSITIONS,
    LIFECYCLE_TERMINAL,
    NON_TERMINAL,
    TERMINAL,
    IllegalTransitionError,
    OrderStatus,
    assert_transition,
    is_stale_failed_event,
)
from razorpay_checkout.tenants import TenantConfigRegistry
from razorpay_checkout.webhook_producer import (
    MalformedPayloadError,
    PublishFailureError,
    SignatureMismatchError,
    UnknownTenantError,
    WebhookAckResponse,
    WebhookProducer,
)

__version__ = "0.0.0"

__all__ = [
    "ALLOWED_TRANSITIONS",
    "FULFILLMENT_DLQ_NAME",
    "FULFILLMENT_QUEUE_NAME",
    "LIFECYCLE_TERMINAL",
    "NON_TERMINAL",
    "TERMINAL",
    "WEBHOOK_EVENTS_DLQ_NAME",
    "WEBHOOK_EVENTS_QUEUE_NAME",
    "AmountResolutionError",
    "AmountResolver",
    "FixedAmountResolver",
    "IllegalTransitionError",
    "MalformedPayloadError",
    "OrderNotFoundError",
    "OrderService",
    "OrderStatus",
    "PublishFailureError",
    "RazorpayAPIError",
    "RazorpayClient",
    "RazorpayConfig",
    "RazorpayUnavailableError",
    "SignatureMismatchError",
    "TenantConfigRegistry",
    "UnknownTenantError",
    "WebhookAckResponse",
    "WebhookProducer",
    "__version__",
    "assert_transition",
    "aws_endpoint_url",
    "build_webhook_envelope",
    "configure_logging",
    "correlation_id_dependency",
    "ensure_fulfillment_queues",
    "ensure_webhook_events_queues",
    "get_correlation_id",
    "get_logger",
    "is_stale_failed_event",
    "new_correlation_id",
    "payments_router",
    "publish_fulfillment_message",
    "publish_webhook_event",
    "register_secret",
    "set_correlation_id",
]
