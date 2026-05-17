"""order_events table with indexes

Revision ID: 0007_order_events
Revises: 0006_webhook_events
Create Date: 2026-05-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_order_events"
down_revision: str | None = "0006_webhook_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE order_events (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            order_id         UUID NOT NULL REFERENCES orders(id),
            tenant_id        TEXT NOT NULL,
            from_status      TEXT NOT NULL,
            to_status        TEXT NOT NULL,
            trigger          TEXT NOT NULL,
            webhook_event_id UUID REFERENCES webhook_events(id),
            correlation_id   TEXT NOT NULL,
            metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute("CREATE INDEX order_events_order_id_idx ON order_events (order_id);")
    op.execute(
        "CREATE INDEX order_events_tenant_created_at_idx "
        "ON order_events (tenant_id, created_at);"
    )
    op.execute("CREATE INDEX order_events_correlation_id_idx ON order_events (correlation_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS order_events_correlation_id_idx;")
    op.execute("DROP INDEX IF EXISTS order_events_tenant_created_at_idx;")
    op.execute("DROP INDEX IF EXISTS order_events_order_id_idx;")
    op.execute("DROP TABLE IF EXISTS order_events;")
