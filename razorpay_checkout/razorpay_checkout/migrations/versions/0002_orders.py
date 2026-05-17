"""orders table with BEFORE UPDATE trigger

Revision ID: 0002_orders
Revises: 0001_tenants
Create Date: 2026-05-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_orders"
down_revision: str | None = "0001_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto";')
    op.execute(
        """
        CREATE TABLE orders (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               TEXT NOT NULL,
            receipt                 TEXT NOT NULL,
            razorpay_order_id       TEXT UNIQUE,
            amount                  INTEGER NOT NULL CHECK (amount > 0),
            currency                TEXT NOT NULL DEFAULT 'INR'
                                      CHECK (char_length(currency) <= 3),
            status                  TEXT NOT NULL DEFAULT 'initializing',
            notes                   JSONB NOT NULL DEFAULT '{}'::jsonb,
            correlation_id          TEXT NOT NULL,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            attempted_at            TIMESTAMPTZ,
            authorized_at           TIMESTAMPTZ,
            paid_at                 TIMESTAMPTZ,
            fulfilled_at            TIMESTAMPTZ,
            fulfillment_failed_at   TIMESTAMPTZ,
            failed_at               TIMESTAMPTZ,
            refunded_at             TIMESTAMPTZ,
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE TRIGGER orders_updated_at
        BEFORE UPDATE ON orders
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS orders_updated_at ON orders;")
    op.execute("DROP TABLE IF EXISTS orders;")
