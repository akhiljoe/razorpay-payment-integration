"""OrderService: the only entry point for orders, payments-row, refunds, order_events writes.

Two responsibilities:

1. `create_or_get` — DESIGN_v5 Section 2.1 order creation sequence:
   (a) check existing non-terminal row for `(tenant_id, receipt)`,
   (b) INSERT `initializing` row,
   (c) call Razorpay,
   (d) UPDATE to `created`.
   `UNIQUE(tenant_id, receipt)` collapses concurrent INSERTs. A Postgres
   advisory transaction lock additionally serialises the Razorpay call so
   ten concurrent same-receipt POSTs produce exactly one Razorpay order.

2. `transition` — central choke point for any order state change. Caller must
   already hold `SELECT ... FOR UPDATE` on the order row. Writes the matching
   `{status}_at` column and inserts an `order_events` row.

Tenant filter is enforced inside this module. Callers cannot omit it.

Concurrency model for create_or_get (two transactions on the same connection):

  Txn A (commits the initializing row):
    - pg_advisory_xact_lock((tenant_id, receipt))
    - If existing row has razorpay_order_id, return it (lock auto-released).
    - Else INSERT initializing row.
    - COMMIT releases the lock and persists the row.

  Txn B (commits the Razorpay handshake):
    - pg_advisory_xact_lock((tenant_id, receipt))  -- serialises competing callers
    - Re-check the row inside the new lock.
    - If razorpay_order_id is now set by a competitor, return that row.
    - Else call Razorpay, UPDATE the row to created, INSERT order_events.
    - COMMIT releases the lock.

  On Razorpay failure inside Txn B, the txn rolls back so the UPDATE does not
  happen. The initializing row committed by Txn A stays, matching the DESIGN
  contract that the row persists for the next retry.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg

from razorpay_checkout.client import (
    RazorpayAPIError,
    RazorpayClient,
    RazorpayUnavailableError,
)
from razorpay_checkout.correlation import get_correlation_id, new_correlation_id
from razorpay_checkout.logging_config import get_logger
from razorpay_checkout.models import CreateOrderRequest, OrderRecord
from razorpay_checkout.state_machine import (
    OrderStatus,
    assert_transition,
)
from razorpay_checkout.telemetry import order_state_transition_count

if TYPE_CHECKING:
    from razorpay_checkout.amounts import AmountResolver
    from razorpay_checkout.config import RazorpayConfig

_log = get_logger("razorpay_checkout.orders")


_STATUS_TIMESTAMP_COLUMN: dict[OrderStatus, str | None] = {
    OrderStatus.INITIALIZING: None,
    OrderStatus.CREATED: None,
    OrderStatus.ATTEMPTED: "attempted_at",
    OrderStatus.AUTHORIZED: "authorized_at",
    OrderStatus.PAID: "paid_at",
    OrderStatus.FULFILLED: "fulfilled_at",
    OrderStatus.FULFILLMENT_FAILED: "fulfillment_failed_at",
    OrderStatus.FAILED: "failed_at",
    OrderStatus.REFUNDED: "refunded_at",
}


_INT64_MAX = (1 << 63) - 1


def _advisory_lock_key(tenant_id: str, receipt: str) -> int:
    """Stable signed-int64 hash for `pg_advisory_xact_lock`.

    Blake2b 8-byte digest mapped to the signed int64 range so Postgres's
    `bigint` accepts it without overflow.
    """
    digest = hashlib.blake2b(
        f"{tenant_id}\x00{receipt}".encode(), digest_size=8
    ).digest()
    n = int.from_bytes(digest, "big", signed=False)
    if n > _INT64_MAX:
        n -= 1 << 64
    return n


class OrderNotFoundError(Exception):
    """Raised when a tenant-scoped order lookup returns no row."""


class OrderService:
    """Orchestrates order creation and state transitions."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        config: RazorpayConfig,
        client: RazorpayClient,
        amount_resolver: AmountResolver,
    ) -> None:
        self._pool = pool
        self._config = config
        self._client = client
        self._amount_resolver = amount_resolver

    # ---------- create_or_get ----------

    async def create_or_get(
        self,
        request: CreateOrderRequest,
        *,
        correlation_id: str | None = None,
    ) -> OrderRecord:
        """Idempotent order creation per (tenant_id, receipt). See module docstring."""
        cid = correlation_id or get_correlation_id() or new_correlation_id()
        tenant_id = self._config.tenant_id
        lock_key = _advisory_lock_key(tenant_id, request.receipt)

        async with self._pool.acquire() as conn:
            # === Txn A: ensure an `initializing` row exists for (tenant_id, receipt) ===
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

                existing = await conn.fetchrow(
                    "SELECT * FROM orders WHERE tenant_id = $1 AND receipt = $2",
                    tenant_id,
                    request.receipt,
                )

                if existing is not None and existing["razorpay_order_id"] is not None:
                    _log.info(
                        "order_create_idempotent_hit",
                        tenant_id=tenant_id,
                        receipt=request.receipt,
                        order_id=str(existing["id"]),
                        razorpay_order_id=existing["razorpay_order_id"],
                    )
                    return _row_to_record(existing)

                if existing is not None:
                    order_id: UUID = existing["id"]
                    amount = existing["amount"]
                    currency = existing["currency"]
                else:
                    amount = await self._amount_resolver.resolve(request, tenant_id)
                    currency = request.currency
                    inserted = await conn.fetchrow(
                        """
                        INSERT INTO orders
                            (tenant_id, receipt, amount, currency, notes,
                             correlation_id, status)
                        VALUES ($1, $2, $3, $4, $5, $6, 'initializing')
                        RETURNING id
                        """,
                        tenant_id,
                        request.receipt,
                        amount,
                        currency,
                        request.notes,
                        cid,
                    )
                    assert inserted is not None
                    order_id = inserted["id"]
            # Txn A committed. Initializing row is durable.

            # === Txn B: call Razorpay and UPDATE, serialised by advisory lock ===
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

                # Re-check: a concurrent caller may have finished while we
                # were waiting at the advisory lock.
                current = await conn.fetchrow(
                    "SELECT * FROM orders WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                    order_id,
                    tenant_id,
                )
                if current is None:
                    raise OrderNotFoundError(str(order_id))
                if current["razorpay_order_id"] is not None:
                    _log.info(
                        "order_create_followed_leader",
                        tenant_id=tenant_id,
                        receipt=request.receipt,
                        order_id=str(order_id),
                        razorpay_order_id=current["razorpay_order_id"],
                    )
                    return _row_to_record(current)

                try:
                    rzp = await self._client.create_order(
                        amount=amount,
                        currency=currency,
                        receipt=request.receipt,
                        notes=request.notes,
                    )
                except RazorpayUnavailableError:
                    _log.warning(
                        "order_create_razorpay_unavailable",
                        tenant_id=tenant_id,
                        receipt=request.receipt,
                        order_id=str(order_id),
                    )
                    raise
                except RazorpayAPIError as exc:
                    _log.error(
                        "order_create_razorpay_api_error",
                        tenant_id=tenant_id,
                        receipt=request.receipt,
                        status_code=exc.status_code,
                    )
                    raise

                razorpay_order_id = rzp["id"]

                updated = await conn.fetchrow(
                    """
                    UPDATE orders
                       SET razorpay_order_id = $1,
                           status = 'created'
                     WHERE id = $2 AND tenant_id = $3
                    RETURNING *
                    """,
                    razorpay_order_id,
                    order_id,
                    tenant_id,
                )
                if updated is None:
                    raise OrderNotFoundError(str(order_id))

                await conn.execute(
                    """
                    INSERT INTO order_events
                        (order_id, tenant_id, from_status, to_status,
                         trigger, correlation_id, metadata)
                    VALUES ($1, $2, 'initializing', 'created',
                            'initializing', $3, $4::jsonb)
                    """,
                    order_id,
                    tenant_id,
                    cid,
                    {"razorpay_order_id": razorpay_order_id},
                )

        _log.info(
            "order_created",
            tenant_id=tenant_id,
            receipt=request.receipt,
            order_id=str(order_id),
            razorpay_order_id=razorpay_order_id,
            amount=amount,
        )
        return _row_to_record(updated)

    # ---------- transition ----------

    async def transition(
        self,
        conn: asyncpg.Connection,
        *,
        order_id: UUID,
        tenant_id: str,
        new_status: OrderStatus,
        trigger: str,
        webhook_event_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Apply a state transition. Caller MUST already hold `SELECT FOR UPDATE`."""
        row = await conn.fetchrow(
            "SELECT status, tenant_id FROM orders WHERE id = $1 AND tenant_id = $2",
            order_id,
            tenant_id,
        )
        if row is None:
            raise OrderNotFoundError(str(order_id))

        from_status = OrderStatus(row["status"])
        assert_transition(from_status, new_status, str(order_id))

        timestamp_col = _STATUS_TIMESTAMP_COLUMN[new_status]
        if timestamp_col is not None:
            await conn.execute(
                f"""
                UPDATE orders
                   SET status = $1,
                       {timestamp_col} = NOW()
                 WHERE id = $2 AND tenant_id = $3
                """,  # noqa: S608 - column name is whitelisted via enum
                new_status.value,
                order_id,
                tenant_id,
            )
        else:
            await conn.execute(
                "UPDATE orders SET status = $1 WHERE id = $2 AND tenant_id = $3",
                new_status.value,
                order_id,
                tenant_id,
            )

        cid = get_correlation_id() or new_correlation_id()
        await conn.execute(
            """
            INSERT INTO order_events
                (order_id, tenant_id, from_status, to_status,
                 trigger, webhook_event_id, correlation_id, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            order_id,
            tenant_id,
            from_status.value,
            new_status.value,
            trigger,
            webhook_event_id,
            cid,
            metadata or {},
        )

        order_state_transition_count.add(
            1,
            {
                "from": from_status.value,
                "to": new_status.value,
                "tenant_id": tenant_id,
            },
        )
        _log.info(
            "order_transition",
            tenant_id=tenant_id,
            order_id=str(order_id),
            from_status=from_status.value,
            to_status=new_status.value,
            trigger=trigger,
        )

    # ---------- Lookups ----------

    async def get_by_id(self, order_id: UUID, tenant_id: str) -> OrderRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM orders WHERE id = $1 AND tenant_id = $2",
                order_id,
                tenant_id,
            )
        return _row_to_record(row) if row is not None else None

    async def get_by_razorpay_order_id(
        self, razorpay_order_id: str, tenant_id: str
    ) -> OrderRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM orders WHERE razorpay_order_id = $1 AND tenant_id = $2",
                razorpay_order_id,
                tenant_id,
            )
        return _row_to_record(row) if row is not None else None


def _row_to_record(row: asyncpg.Record) -> OrderRecord:
    """Map an asyncpg Record from `orders` to `OrderRecord`."""

    def _ts(name: str) -> datetime | None:
        val = row[name]
        return val if isinstance(val, datetime) else None

    return OrderRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        receipt=row["receipt"],
        razorpay_order_id=row["razorpay_order_id"],
        amount=row["amount"],
        currency=row["currency"],
        status=OrderStatus(row["status"]),
        correlation_id=row["correlation_id"],
        created_at=row["created_at"],
        attempted_at=_ts("attempted_at"),
        authorized_at=_ts("authorized_at"),
        paid_at=_ts("paid_at"),
        fulfilled_at=_ts("fulfilled_at"),
        fulfillment_failed_at=_ts("fulfillment_failed_at"),
        failed_at=_ts("failed_at"),
        refunded_at=_ts("refunded_at"),
        updated_at=row["updated_at"],
    )
