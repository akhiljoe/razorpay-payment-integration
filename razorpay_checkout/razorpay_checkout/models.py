"""Pydantic models for the order API surface and the internal `OrderRecord`.

Wire schemas mirror DESIGN_v5 Section 2.1 exactly. `OrderRecord` is the
internal representation used by `OrderService`; do not return it from a route
handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from razorpay_checkout.state_machine import OrderStatus

# ---------- POST /payments/orders ----------


class CreateOrderRequest(BaseModel):
    """Request body for order creation.

    The server is the sole authority for `amount`. There is no `amount` field
    in the request body. Host apps inject an `AmountResolver` to compute it
    from `notes` (e.g., `notes["product_id"]` lookup).
    """

    currency: str = Field(default="INR", max_length=3)
    receipt: str = Field(..., min_length=1, max_length=255)
    notes: dict[str, str] = Field(default_factory=dict)


class CreateOrderResponse(BaseModel):
    order_id: UUID
    razorpay_order_id: str
    amount: int
    currency: str
    key_id: str


# ---------- GET /payments/orders/{order_id} ----------


class OrderStatusResponse(BaseModel):
    order_id: UUID
    razorpay_order_id: str | None
    status: OrderStatus
    amount: int
    currency: str
    created_at: datetime
    attempted_at: datetime | None = None
    authorized_at: datetime | None = None
    paid_at: datetime | None = None
    fulfilled_at: datetime | None = None
    fulfillment_failed_at: datetime | None = None
    failed_at: datetime | None = None
    refunded_at: datetime | None = None


# ---------- POST /payments/verify-callback ----------


class VerifyCallbackRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


VerifyReason = Literal[
    "signature_mismatch",
    "order_not_found",
    "terminal_failed",
]


class VerifyCallbackResponse(BaseModel):
    verified: bool
    order_id: str  # empty string when verification fails before lookup
    reason: VerifyReason | None = None


# ---------- Internal: OrderRecord ----------


class OrderRecord(BaseModel):
    """Internal projection of an `orders` row.

    Not returned from a route handler. Use the wire models above for HTTP
    responses so the public schema stays stable when internal columns change.
    """

    id: UUID
    tenant_id: str
    receipt: str
    razorpay_order_id: str | None
    amount: int
    currency: str
    status: OrderStatus
    correlation_id: str
    created_at: datetime
    attempted_at: datetime | None = None
    authorized_at: datetime | None = None
    paid_at: datetime | None = None
    fulfilled_at: datetime | None = None
    fulfillment_failed_at: datetime | None = None
    failed_at: datetime | None = None
    refunded_at: datetime | None = None
    updated_at: datetime


# ---------- Admin: replay event / retry fulfillment / reconcile ----------


class ReplayEventResponse(BaseModel):
    event_id: UUID
    previous_status: str
    new_status: Literal["pending"]


class RetryFulfillmentResponse(BaseModel):
    order_id: UUID
    sqs_message_id: str
    previous_status: Literal["paid", "fulfillment_failed"]


ReconcileReason = Literal[
    "missing_in_local_db",
    "amount_mismatch",
    "status_mismatch",
    "refund_status_mismatch",
]


class ReconcileMismatch(BaseModel):
    razorpay_payment_id: str
    reason: ReconcileReason
    razorpay_amount: int | None = None
    local_amount: int | None = None
    razorpay_status: str | None = None
    local_status: str | None = None


class ReconcileResponse(BaseModel):
    from_date: str
    to_date: str
    total_razorpay_payments_fetched: int
    total_local_payments: int
    mismatches: list[ReconcileMismatch]

