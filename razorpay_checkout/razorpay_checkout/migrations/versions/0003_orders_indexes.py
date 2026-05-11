"""orders secondary indexes

Revision ID: 0003_orders_indexes
Revises: 0002_orders
Create Date: 2026-05-11
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_orders_indexes"
down_revision: str | None = "0002_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX orders_tenant_receipt_unique
        ON orders (tenant_id, receipt);
        """
    )
    op.execute("CREATE INDEX orders_tenant_status_idx ON orders (tenant_id, status);")
    op.execute("CREATE INDEX orders_created_at_idx ON orders (created_at);")
    op.execute("CREATE INDEX orders_correlation_id_idx ON orders (correlation_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS orders_correlation_id_idx;")
    op.execute("DROP INDEX IF EXISTS orders_created_at_idx;")
    op.execute("DROP INDEX IF EXISTS orders_tenant_status_idx;")
    op.execute("DROP INDEX IF EXISTS orders_tenant_receipt_unique;")
