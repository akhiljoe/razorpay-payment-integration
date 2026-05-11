"""payments table with indexes

Revision ID: 0004_payments
Revises: 0003_orders_indexes
Create Date: 2026-05-11
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_payments"
down_revision: str | None = "0003_orders_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE payments (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           TEXT NOT NULL,
            order_id            UUID NOT NULL REFERENCES orders(id),
            razorpay_payment_id TEXT NOT NULL UNIQUE,
            razorpay_order_id   TEXT NOT NULL,
            razorpay_signature  TEXT,
            amount              INTEGER NOT NULL,
            currency            TEXT NOT NULL,
            method              TEXT,
            status              TEXT NOT NULL,
            attempt_number      INTEGER NOT NULL DEFAULT 1,
            is_authoritative    BOOLEAN NOT NULL DEFAULT FALSE,
            captured_at         TIMESTAMPTZ,
            refunded_at         TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute("CREATE INDEX payments_order_id_idx ON payments (order_id);")
    op.execute("CREATE INDEX payments_razorpay_order_id_idx ON payments (razorpay_order_id);")
    op.execute("CREATE INDEX payments_tenant_idx ON payments (tenant_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS payments_tenant_idx;")
    op.execute("DROP INDEX IF EXISTS payments_razorpay_order_id_idx;")
    op.execute("DROP INDEX IF EXISTS payments_order_id_idx;")
    op.execute("DROP TABLE IF EXISTS payments;")
