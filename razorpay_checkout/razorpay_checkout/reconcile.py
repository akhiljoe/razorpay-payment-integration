"""Reconciliation between Razorpay's payments API and local DB.

DESIGN_v5 Section 2.1 (reconcile route) and Section 5.8 sequence. Pulls all
Razorpay payments in the date range, joins with local authoritative payments
+ refunds, and emits `ReconcileMismatch` rows for each discrepancy.

Reasons emitted:

- `missing_in_local_db`: Razorpay has a captured payment we don't.
- `amount_mismatch`: Razorpay amount != local amount for the same payment id.
- `status_mismatch`: Razorpay says captured but local says failed (or v.v.).
- `refund_status_mismatch`: Razorpay shows refunded but local doesn't.

Razorpay-only entries (where local has rows but Razorpay doesn't) are
intentionally NOT emitted: this would surface every payment that has aged out
of Razorpay's range but is still in our DB. Adjust the date range to cover
the local rows you want validated.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any

import asyncpg

from razorpay_checkout.logging_config import get_logger
from razorpay_checkout.models import ReconcileMismatch, ReconcileResponse
from razorpay_checkout.telemetry import reconciliation_mismatch_count

if TYPE_CHECKING:
    from razorpay_checkout.client import RazorpayClient

_log = get_logger("razorpay_checkout.reconcile")


def _unix(d: date, *, end: bool = False) -> int:
    """Convert a date to a Unix-second boundary. `end=True` returns 23:59:59 UTC."""
    t = time(23, 59, 59) if end else time(0, 0, 0)
    return int(datetime.combine(d, t, tzinfo=UTC).timestamp())


def _is_refund_status(rzp_status: str) -> bool:
    return rzp_status in {"refunded", "fully_refunded", "partially_refunded"}


async def reconcile(
    *,
    pool: asyncpg.Pool,
    client: RazorpayClient,
    tenant_id: str,
    from_date: date,
    to_date: date,
) -> ReconcileResponse:
    """Run a reconciliation pass for one tenant over a date range."""
    from_unix = _unix(from_date)
    to_unix = _unix(to_date, end=True)

    rzp_items = await client.list_payments(from_unix=from_unix, to_unix=to_unix)

    razorpay_payment_ids = [item["id"] for item in rzp_items if "id" in item]

    async with pool.acquire() as conn:
        local_rows = await conn.fetch(
            """
            SELECT razorpay_payment_id, amount, status
              FROM payments
             WHERE tenant_id = $1
               AND is_authoritative = TRUE
               AND razorpay_payment_id = ANY($2::text[])
            """,
            tenant_id,
            razorpay_payment_ids,
        )
        refund_rows = await conn.fetch(
            """
            SELECT razorpay_payment_id
              FROM refunds
             WHERE tenant_id = $1
               AND razorpay_payment_id = ANY($2::text[])
            """,
            tenant_id,
            razorpay_payment_ids,
        )

    local_index: dict[str, dict[str, Any]] = {
        row["razorpay_payment_id"]: {"amount": row["amount"], "status": row["status"]}
        for row in local_rows
    }
    refunded_locally: set[str] = {row["razorpay_payment_id"] for row in refund_rows}

    mismatches: list[ReconcileMismatch] = []
    for item in rzp_items:
        rzp_id = item.get("id")
        if not isinstance(rzp_id, str):
            continue
        rzp_amount = item.get("amount")
        rzp_status = item.get("status", "")

        local = local_index.get(rzp_id)
        if local is None:
            mismatches.append(
                ReconcileMismatch(
                    razorpay_payment_id=rzp_id,
                    reason="missing_in_local_db",
                    razorpay_amount=rzp_amount,
                    razorpay_status=rzp_status,
                )
            )
            continue

        if isinstance(rzp_amount, int) and local["amount"] != rzp_amount:
            mismatches.append(
                ReconcileMismatch(
                    razorpay_payment_id=rzp_id,
                    reason="amount_mismatch",
                    razorpay_amount=rzp_amount,
                    local_amount=local["amount"],
                    razorpay_status=rzp_status,
                    local_status=local["status"],
                )
            )
            continue

        # status_mismatch: Razorpay says captured but local says failed.
        if (
            rzp_status == "captured"
            and local["status"] != "captured"
            and local["status"] != "refunded"
        ):
            mismatches.append(
                ReconcileMismatch(
                    razorpay_payment_id=rzp_id,
                    reason="status_mismatch",
                    razorpay_status=rzp_status,
                    local_status=local["status"],
                )
            )
            continue

        # refund_status_mismatch: Razorpay says refunded but local has no refund.
        if _is_refund_status(rzp_status) and rzp_id not in refunded_locally:
            mismatches.append(
                ReconcileMismatch(
                    razorpay_payment_id=rzp_id,
                    reason="refund_status_mismatch",
                    razorpay_status=rzp_status,
                    local_status=local["status"],
                )
            )

    for m in mismatches:
        reconciliation_mismatch_count.add(1, {"reason": m.reason, "tenant_id": tenant_id})
    _log.info(
        "reconcile_completed",
        tenant_id=tenant_id,
        from_date=str(from_date),
        to_date=str(to_date),
        razorpay_count=len(rzp_items),
        local_count=len(local_rows),
        mismatch_count=len(mismatches),
    )
    return ReconcileResponse(
        from_date=str(from_date),
        to_date=str(to_date),
        total_razorpay_payments_fetched=len(rzp_items),
        total_local_payments=len(local_rows),
        mismatches=mismatches,
    )
