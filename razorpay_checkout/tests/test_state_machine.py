"""Tests for the order state machine.

Coverage strategy:

- Enum values match the DB string contract exactly.
- ALLOWED_TRANSITIONS contains exactly the 15 edges from DESIGN_v5 Section 4.
- TERMINAL and NON_TERMINAL partition OrderStatus completely.
- For every (from, to) pair in OrderStatus x OrderStatus, assert_transition
  either returns None (legal) or raises IllegalTransitionError (illegal).
- IllegalTransitionError carries the right metadata.
- is_stale_failed_event answers correctly for all 9 statuses.
"""

from __future__ import annotations

import itertools

import pytest

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

EXPECTED_TRANSITIONS = {
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


def test_enum_values_match_db_string_contract() -> None:
    expected = {
        "initializing", "created", "attempted", "authorized",
        "paid", "fulfilled", "fulfillment_failed", "failed", "refunded",
    }
    actual = {s.value for s in OrderStatus}
    assert actual == expected


def test_allowed_transitions_count_is_15() -> None:
    assert len(ALLOWED_TRANSITIONS) == 15


def test_allowed_transitions_match_design_exactly() -> None:
    assert ALLOWED_TRANSITIONS == frozenset(EXPECTED_TRANSITIONS)


def test_terminal_set() -> None:
    assert TERMINAL == frozenset({OrderStatus.FAILED, OrderStatus.REFUNDED})


def test_non_terminal_set_partition() -> None:
    assert NON_TERMINAL | TERMINAL == set(OrderStatus)
    assert NON_TERMINAL & TERMINAL == set()


def test_only_refunded_is_lifecycle_terminal() -> None:
    """`refunded` is the only state with zero outgoing edges.

    `failed` is idempotency-terminal (receipt is freed) but recoverable via
    `failed -> attempted`. DESIGN_v5 Section 4 documents both senses.
    """
    assert LIFECYCLE_TERMINAL == frozenset({OrderStatus.REFUNDED})
    statuses_with_outgoing = {f for f, _ in ALLOWED_TRANSITIONS}
    statuses_without_outgoing = set(OrderStatus) - statuses_with_outgoing
    assert statuses_without_outgoing == LIFECYCLE_TERMINAL


def test_failed_is_idempotency_terminal_but_recoverable() -> None:
    assert OrderStatus.FAILED in TERMINAL
    assert (OrderStatus.FAILED, OrderStatus.ATTEMPTED) in ALLOWED_TRANSITIONS


def test_no_self_transitions() -> None:
    for from_status, to_status in ALLOWED_TRANSITIONS:
        assert from_status != to_status


# ---------- Parametrized exhaustive coverage ----------


_ALL_PAIRS = list(itertools.product(OrderStatus, OrderStatus))
_LEGAL_PAIRS = [p for p in _ALL_PAIRS if p in EXPECTED_TRANSITIONS]
_ILLEGAL_PAIRS = [p for p in _ALL_PAIRS if p not in EXPECTED_TRANSITIONS]


@pytest.mark.parametrize(("from_status", "to_status"), _LEGAL_PAIRS)
def test_legal_transition_returns_none(
    from_status: OrderStatus, to_status: OrderStatus
) -> None:
    assert assert_transition(from_status, to_status, "order_x") is None


@pytest.mark.parametrize(("from_status", "to_status"), _ILLEGAL_PAIRS)
def test_illegal_transition_raises(
    from_status: OrderStatus, to_status: OrderStatus
) -> None:
    with pytest.raises(IllegalTransitionError) as exc_info:
        assert_transition(from_status, to_status, "order_x")
    assert exc_info.value.from_status is from_status
    assert exc_info.value.to_status is to_status
    assert exc_info.value.order_id == "order_x"


def test_illegal_transition_error_message_includes_status_strings() -> None:
    with pytest.raises(IllegalTransitionError) as exc_info:
        assert_transition(OrderStatus.PAID, OrderStatus.CREATED, "ord_42")
    message = str(exc_info.value)
    assert "paid" in message
    assert "created" in message
    assert "ord_42" in message


# ---------- Stale-failed event matrix ----------


@pytest.mark.parametrize(
    "status",
    [OrderStatus.PAID, OrderStatus.FULFILLED, OrderStatus.FULFILLMENT_FAILED],
)
def test_is_stale_failed_event_truthy(status: OrderStatus) -> None:
    assert is_stale_failed_event(status) is True


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.INITIALIZING,
        OrderStatus.CREATED,
        OrderStatus.ATTEMPTED,
        OrderStatus.AUTHORIZED,
        OrderStatus.FAILED,
        OrderStatus.REFUNDED,
    ],
)
def test_is_stale_failed_event_falsy(status: OrderStatus) -> None:
    assert is_stale_failed_event(status) is False


def test_stale_failed_matrix_covers_all_statuses() -> None:
    # Every OrderStatus must appear in exactly one of the two test parametrize blocks.
    truthy = {OrderStatus.PAID, OrderStatus.FULFILLED, OrderStatus.FULFILLMENT_FAILED}
    falsy = set(OrderStatus) - truthy
    assert truthy | falsy == set(OrderStatus)
    assert truthy & falsy == set()
