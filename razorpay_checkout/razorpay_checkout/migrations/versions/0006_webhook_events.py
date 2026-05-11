"""webhook_events table with composite unique and indexes

Revision ID: 0006_webhook_events
Revises: 0005_refunds
Create Date: 2026-05-11
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_webhook_events"
down_revision: str | None = "0005_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE webhook_events (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           TEXT NOT NULL,
            event_id            TEXT NOT NULL,
            event               TEXT NOT NULL,
            payload             JSONB NOT NULL,
            correlation_id      TEXT NOT NULL,
            received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processed_at        TIMESTAMPTZ,
            processing_status   TEXT NOT NULL DEFAULT 'pending',
            error               TEXT,
            attempts            INTEGER NOT NULL DEFAULT 0,
            max_attempts        INTEGER NOT NULL DEFAULT 5,
            CONSTRAINT webhook_events_tenant_event_id_key
              UNIQUE (tenant_id, event_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX webhook_events_tenant_status_idx "
        "ON webhook_events (tenant_id, processing_status);"
    )
    op.execute("CREATE INDEX webhook_events_received_at_idx ON webhook_events (received_at);")
    op.execute(
        "CREATE INDEX webhook_events_correlation_id_idx ON webhook_events (correlation_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS webhook_events_correlation_id_idx;")
    op.execute("DROP INDEX IF EXISTS webhook_events_received_at_idx;")
    op.execute("DROP INDEX IF EXISTS webhook_events_tenant_status_idx;")
    op.execute("DROP TABLE IF EXISTS webhook_events;")
