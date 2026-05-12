"""Order state machine.

Single source of truth for legal order status transitions, per DESIGN_v5
Section 4. The set is closed: any (from_status, to_status) pair not present
in `ALLOWED_TRANSITIONS` raises `IllegalTransitionError` when fed to
`assert_transition`.

Two terminal statuses exist: `failed` and `refunded`. No outgoing edges.

One stale-event exception lives outside the table: a `payment.failed` webhook
for an order in `paid`, `fulfilled`, or `fulfillment_failed` is treated as a
valid no-op (mark webhook `done`, do not transition). Use
`is_stale_failed_event` to detect this case before calling `assert_transition`.
"""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    """Order lifecycle status. The string value is what lands in the DB."""

    INITIALIZING = "initializing"
    CREATED = "created"
    ATTEMPTED = "attempted"
    AUTHORIZED = "authorized"
    PAID = "paid"
    FULFILLED = "fulfilled"
    FULFILLMENT_FAILED = "fulfillment_failed"
    FAILED = "failed"
    REFUNDED = "refunded"


# 15 allowed transitions, mirrored from DESIGN_v5 Section 4.
# Order is deliberate: lifecycle progression then recovery paths.
ALLOWED_TRANSITIONS: frozenset[tuple[OrderStatus, OrderStatus]] = frozenset(
    {
        (OrderStatus.INITIALIZING, OrderStatus.CREATED),
        (OrderStatus.CREATED, OrderStatus.ATTEMPTED),
        (OrderStatus.CREATED, OrderStatus.FAILED),
        (OrderStatus.ATTEMPTED, OrderStatus.AUTHORIZED),
        (OrderStatus.ATTEMPTED, OrderStatus.PAID),
        (OrderStatus.ATTEMPTED, OrderStatus.FAILED),
        (OrderStatus.AUTHORIZED, OrderStatus.PAID),
        (OrderStatus.AUTHORIZED, OrderStatus.FAILED),
        (OrderStatus.FAILED, OrderStatus.ATTEMPTED),
        (OrderStatus.PAID, OrderStatus.FULFILLED),
        (OrderStatus.PAID, OrderStatus.FULFILLMENT_FAILED),
        (OrderStatus.PAID, OrderStatus.REFUNDED),
        (OrderStatus.FULFILLED, OrderStatus.REFUNDED),
        (OrderStatus.FULFILLMENT_FAILED, OrderStatus.FULFILLED),
        (OrderStatus.FULFILLMENT_FAILED, OrderStatus.REFUNDED),
    }
)


# Idempotency-terminal: receipt is freed for re-use. Matches the prose in
# DESIGN_v5 Section 4 ("`failed` and `refunded` are terminal") and the
# non-terminal list used by `OrderService.create_or_get`. Note that `failed`
# is idempotency-terminal but **not** lifecycle-terminal: `failed -> attempted`
# is a legal transition (a new payment.* event can resurrect the order).
TERMINAL: frozenset[OrderStatus] = frozenset(
    {OrderStatus.FAILED, OrderStatus.REFUNDED}
)
NON_TERMINAL: frozenset[OrderStatus] = frozenset(OrderStatus) - TERMINAL

# Lifecycle-terminal: zero outgoing edges in the transition table. Only
# `refunded` qualifies. `failed` has the recovery edge above.
LIFECYCLE_TERMINAL: frozenset[OrderStatus] = frozenset({OrderStatus.REFUNDED})


_STALE_FAILED_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.PAID, OrderStatus.FULFILLED, OrderStatus.FULFILLMENT_FAILED}
)


class IllegalTransitionError(Exception):
    """Raised when an attempted state transition is not in `ALLOWED_TRANSITIONS`.

    Carries `from_status`, `to_status`, and `order_id` for structured logging
    and for the operator's view in admin/replay flows.
    """

    def __init__(
        self,
        from_status: OrderStatus,
        to_status: OrderStatus,
        order_id: str,
    ) -> None:
        self.from_status = from_status
        self.to_status = to_status
        self.order_id = order_id
        super().__init__(
            f"Illegal transition {from_status.value} -> {to_status.value} "
            f"for order_id={order_id}"
        )


def assert_transition(
    from_status: OrderStatus,
    to_status: OrderStatus,
    order_id: str,
) -> None:
    """Raise `IllegalTransitionError` if (`from_status`, `to_status`) is not allowed.

    Self-transitions are illegal. Terminal-source edges are illegal. The full
    set of legal edges is `ALLOWED_TRANSITIONS`.
    """
    if (from_status, to_status) not in ALLOWED_TRANSITIONS:
        raise IllegalTransitionError(from_status, to_status, order_id)


def is_stale_failed_event(current_status: OrderStatus) -> bool:
    """Return True if a `payment.failed` event arriving now should be a no-op.

    DESIGN_v5 Section 4 special case: once an order has reached `paid`,
    `fulfilled`, or `fulfillment_failed`, a late `payment.failed` from Razorpay
    is stale data. The Webhook Consumer marks the event `done` and does not
    transition the order.
    """
    return current_status in _STALE_FAILED_STATUSES
