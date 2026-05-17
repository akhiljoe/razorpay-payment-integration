"""refunds table with indexes

Revision ID: 0005_refunds
Revises: 0004_payments
Create Date: 2026-05-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_refunds"
down_revision: str | None = "0004_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE refunds (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           TEXT NOT NULL,
            order_id            UUID NOT NULL REFERENCES orders(id),
            payment_id          UUID NOT NULL REFERENCES payments(id),
            razorpay_refund_id  TEXT NOT NULL UNIQUE,
            razorpay_payment_id TEXT NOT NULL,
            amount              INTEGER NOT NULL,
            currency            TEXT NOT NULL,
            status              TEXT NOT NULL,
            initiated_by        TEXT NOT NULL,
            notes               JSONB NOT NULL DEFAULT '{}'::jsonb,
            processed_at        TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute("CREATE INDEX refunds_order_id_idx ON refunds (order_id);")
    op.execute("CREATE INDEX refunds_tenant_idx ON refunds (tenant_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS refunds_tenant_idx;")
    op.execute("DROP INDEX IF EXISTS refunds_order_id_idx;")
    op.execute("DROP TABLE IF EXISTS refunds;")
