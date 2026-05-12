"""Webhook Producer.

Public-facing Lambda. Verifies the Razorpay signature, fast-parses the payload,
and publishes to `webhook-events.fifo`. Holds no DB credentials and performs
zero DB I/O.

DESIGN_v5 Section 2.1 (steps 1 to 6) is the source of truth for the pipeline.
The Razorpay 500 ms SLA is bounded by this function only; consumer latency is
invisible to Razorpay.

Exception model:
- `UnknownTenantError` -> caller maps to 400.
- `SignatureMismatchError` -> caller maps to 400. (DESIGN says 400, not 401, to
  match the "unknown tenant" code path and avoid leaking tenant enumeration.)
- `MalformedPayloadError` -> caller maps to 400.
- `PublishFailureError` -> caller maps to 503; Razorpay will retry.

The producer does not catch `aioboto3` transport errors except to wrap them
in `PublishFailureError`. The route handler decides the HTTP status.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from razorpay_checkout.correlation import get_correlation_id, new_correlation_id
from razorpay_checkout.logging_config import get_logger
from razorpay_checkout.queue import SQSClient, build_webhook_envelope, publish_webhook_event
from razorpay_checkout.signatures import verify_webhook_signature

if TYPE_CHECKING:
    from razorpay_checkout.tenants import TenantConfigRegistry

_log = get_logger("razorpay_checkout.webhook_producer")


class UnknownTenantError(Exception):
    """Tenant_id from the URL path is not registered."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(f"Unknown tenant: {tenant_id!r}")


class SignatureMismatchError(Exception):
    """HMAC verification failed against both current and previous webhook secrets."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(f"Signature mismatch for tenant {tenant_id!r}")


class MalformedPayloadError(Exception):
    """Body could not be parsed as JSON or required fields are missing."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Malformed payload: {reason}")


class PublishFailureError(Exception):
    """SQS SendMessage failed after signature verification succeeded."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"SQS publish failed: {reason}")


class WebhookAckResponse:
    """Producer's return value. Plain class, not Pydantic, to keep the hot path lean."""

    __slots__ = ("status", "correlation_id", "message_id")

    def __init__(
        self,
        *,
        status: str,
        correlation_id: str,
        message_id: str,
    ) -> None:
        self.status = status
        self.correlation_id = correlation_id
        self.message_id = message_id


def _fast_parse(raw_body: bytes) -> dict[str, Any]:
    """Parse JSON and assert presence of the four fields the producer needs.

    The producer extracts `event_id`, `event`, and `razorpay_order_id` from the
    payload. Razorpay's payload shape places `razorpay_order_id` at
    `payload.payment.entity.order_id` for payment events and at
    `payload.refund.entity.order_id` for refund events.
    """
    try:
        parsed: Any = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise MalformedPayloadError(f"invalid JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise MalformedPayloadError("top-level JSON is not an object")

    return parsed


def _extract_event_id(payload: dict[str, Any]) -> str:
    """Razorpay puts the event id at the top level as `id`."""
    event_id = payload.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise MalformedPayloadError("missing or non-string top-level `id`")
    return event_id


def _extract_event_type(payload: dict[str, Any]) -> str:
    event = payload.get("event")
    if not isinstance(event, str) or not event:
        raise MalformedPayloadError("missing or non-string top-level `event`")
    return event


def _extract_razorpay_order_id(payload: dict[str, Any], event: str) -> str:
    """Pull `order_id` out of the event-specific entity for FIFO grouping.

    Payment events: `payload.payment.entity.order_id`.
    Refund events: `payload.refund.entity.order_id`.
    `order.paid`: `payload.order.entity.id`.
    """
    body = payload.get("payload")
    if not isinstance(body, dict):
        raise MalformedPayloadError("missing or non-object `payload`")

    if event.startswith("payment."):
        entity = body.get("payment", {}).get("entity", {})
        order_id = entity.get("order_id") if isinstance(entity, dict) else None
    elif event.startswith("refund."):
        entity = body.get("refund", {}).get("entity", {})
        order_id = entity.get("order_id") if isinstance(entity, dict) else None
    elif event == "order.paid":
        entity = body.get("order", {}).get("entity", {})
        order_id = entity.get("id") if isinstance(entity, dict) else None
    else:
        order_id = None

    if not isinstance(order_id, str) or not order_id:
        raise MalformedPayloadError(
            f"could not extract razorpay_order_id for event {event!r}"
        )
    return order_id


class WebhookProducer:
    """Stateless producer. Wire one instance at app startup; share across requests."""

    def __init__(
        self,
        *,
        tenant_registry: TenantConfigRegistry,
        sqs_client: SQSClient,
        queue_url: str,
    ) -> None:
        self._tenants = tenant_registry
        self._sqs = sqs_client
        self._queue_url = queue_url

    async def process(
        self,
        *,
        raw_body: bytes,
        header_signature: str,
        tenant_id: str,
        correlation_id: str | None = None,
    ) -> WebhookAckResponse:
        """Verify, parse, and enqueue. See DESIGN_v5 Section 2.1.

        Caller is responsible for converting exceptions to HTTP status codes.
        Producer never writes to Postgres and never calls `OrderService`.
        """
        cid = correlation_id or get_correlation_id() or new_correlation_id()
        received_at = datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

        # Step 2: tenant resolution from URL path parameter (only valid source).
        config = self._tenants.get(tenant_id)
        if config is None:
            _log.warning("webhook_unknown_tenant", tenant_id=tenant_id)
            raise UnknownTenantError(tenant_id)

        # Step 3: signature verification BEFORE any JSON parse to defeat DoS.
        webhook_secret = config.webhook_secret.get_secret_value()
        previous = (
            config.webhook_secret_previous.get_secret_value()
            if config.webhook_secret_previous is not None
            else None
        )
        if not verify_webhook_signature(
            raw_body, header_signature, webhook_secret, previous
        ):
            _log.warning("webhook_signature_mismatch", tenant_id=tenant_id)
            raise SignatureMismatchError(tenant_id)

        # Step 4: fast-parse to extract dedup and grouping keys.
        payload = _fast_parse(raw_body)
        event_id = _extract_event_id(payload)
        event_type = _extract_event_type(payload)
        razorpay_order_id = _extract_razorpay_order_id(payload, event_type)

        # Step 5: build envelope and publish.
        envelope_body = build_webhook_envelope(
            tenant_id=tenant_id,
            event_id=event_id,
            event=event_type,
            received_at=received_at,
            signature=header_signature,
            payload=payload,
        )
        try:
            message_id = await publish_webhook_event(
                sqs_client=self._sqs,
                queue_url=self._queue_url,
                body=envelope_body,
                razorpay_order_id=razorpay_order_id,
                tenant_id=tenant_id,
                event_id=event_id,
                correlation_id=cid,
            )
        except Exception as exc:  # noqa: BLE001 - wrap any aioboto3/transport error
            _log.error(
                "webhook_publish_failed",
                tenant_id=tenant_id,
                event_id=event_id,
                error=str(exc),
            )
            raise PublishFailureError(str(exc)) from exc

        # Step 6: return 200-queued shape. The route handler maps this to HTTP 200.
        return WebhookAckResponse(
            status="queued",
            correlation_id=cid,
            message_id=message_id,
        )
