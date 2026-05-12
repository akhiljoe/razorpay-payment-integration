"""Tests for the event-type-aware primary idempotency guard.

A full matrix over (event, order_status, captured_payment_exists) per
DESIGN_v5 Section 2.1 step 10. Pure function, no DB needed.
"""

from __future__ import annotations

import pytest

from razorpay_checkout.state_machine import OrderStatus
from razorpay_checkout.webhook_idempotency import SkipDecision, should_skip

# ---------- payment.captured ----------


@pytest.mark.parametrize(
    "order_status",
    [
        OrderStatus.PAID,
        OrderStatus.FULFILLED,
        OrderStatus.FULFILLMENT_FAILED,
        OrderStatus.REFUNDED,
    ],
)
def test_payment_captured_skips_when_terminal_and_payment_exists(
    order_status: OrderStatus,
) -> None:
    assert (
        should_skip("payment.captured", order_status, captured_payment_exists=True)
        is SkipDecision.SKIP_ALREADY_APPLIED
    )


@pytest.mark.parametrize(
    "order_status",
    [
        OrderStatus.PAID,
        OrderStatus.FULFILLED,
        OrderStatus.FULFILLMENT_FAILED,
        OrderStatus.REFUNDED,
    ],
)
def test_payment_captured_proceeds_when_terminal_but_no_payment_row(
    order_status: OrderStatus,
) -> None:
    assert (
        should_skip("payment.captured", order_status, captured_payment_exists=False)
        is SkipDecision.PROCEED
    )


@pytest.mark.parametrize(
    "order_status",
    [
        OrderStatus.INITIALIZING,
        OrderStatus.CREATED,
        OrderStatus.ATTEMPTED,
        OrderStatus.AUTHORIZED,
        OrderStatus.FAILED,
    ],
)
def test_payment_captured_proceeds_for_non_terminal(order_status: OrderStatus) -> None:
    assert (
        should_skip("payment.captured", order_status, captured_payment_exists=False)
        is SkipDecision.PROCEED
    )


# ---------- payment.authorized ----------


@pytest.mark.parametrize(
    "order_status",
    [
        OrderStatus.AUTHORIZED,
        OrderStatus.PAID,
        OrderStatus.FULFILLED,
        OrderStatus.FULFILLMENT_FAILED,
        OrderStatus.REFUNDED,
    ],
)
def test_payment_authorized_skips_when_at_or_past_authorized(
    order_status: OrderStatus,
) -> None:
    assert (
        should_skip("payment.authorized", order_status, captured_payment_exists=False)
        is SkipDecision.SKIP_ALREADY_APPLIED
    )


@pytest.mark.parametrize(
    "order_status",
    [
        OrderStatus.INITIALIZING,
        OrderStatus.CREATED,
        OrderStatus.ATTEMPTED,
        OrderStatus.FAILED,
    ],
)
def test_payment_authorized_proceeds_when_before_authorized(
    order_status: OrderStatus,
) -> None:
    assert (
        should_skip("payment.authorized", order_status, captured_payment_exists=False)
        is SkipDecision.PROCEED
    )


# ---------- payment.failed ----------


@pytest.mark.parametrize(
    "order_status", [OrderStatus.FAILED, OrderStatus.REFUNDED]
)
def test_payment_failed_skips_when_terminal(order_status: OrderStatus) -> None:
    assert (
        should_skip("payment.failed", order_status, captured_payment_exists=False)
        is SkipDecision.SKIP_ALREADY_APPLIED
    )


@pytest.mark.parametrize(
    "order_status",
    [OrderStatus.PAID, OrderStatus.FULFILLED, OrderStatus.FULFILLMENT_FAILED],
)
def test_payment_failed_is_stale_when_at_or_past_paid(
    order_status: OrderStatus,
) -> None:
    assert (
        should_skip("payment.failed", order_status, captured_payment_exists=False)
        is SkipDecision.STALE_NO_OP
    )


@pytest.mark.parametrize(
    "order_status",
    [
        OrderStatus.INITIALIZING,
        OrderStatus.CREATED,
        OrderStatus.ATTEMPTED,
        OrderStatus.AUTHORIZED,
    ],
)
def test_payment_failed_proceeds_when_before_paid(order_status: OrderStatus) -> None:
    assert (
        should_skip("payment.failed", order_status, captured_payment_exists=False)
        is SkipDecision.PROCEED
    )


# ---------- refund.processed ----------


def test_refund_processed_skips_when_refunded() -> None:
    assert (
        should_skip(
            "refund.processed", OrderStatus.REFUNDED, captured_payment_exists=False
        )
        is SkipDecision.SKIP_ALREADY_APPLIED
    )


@pytest.mark.parametrize(
    "order_status",
    [
        OrderStatus.PAID,
        OrderStatus.FULFILLED,
        OrderStatus.FULFILLMENT_FAILED,
        OrderStatus.FAILED,
    ],
)
def test_refund_processed_proceeds_otherwise(order_status: OrderStatus) -> None:
    assert (
        should_skip("refund.processed", order_status, captured_payment_exists=False)
        is SkipDecision.PROCEED
    )


# ---------- order.paid and unknown events always proceed ----------


def test_order_paid_always_proceeds() -> None:
    for status in OrderStatus:
        assert (
            should_skip("order.paid", status, captured_payment_exists=False)
            is SkipDecision.PROCEED
        )


def test_unknown_event_proceeds() -> None:
    assert (
        should_skip("some.unknown.event", OrderStatus.PAID, captured_payment_exists=True)
        is SkipDecision.PROCEED
    )
