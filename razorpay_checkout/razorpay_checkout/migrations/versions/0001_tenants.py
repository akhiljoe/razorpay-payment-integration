"""tenants table and set_updated_at trigger function

Revision ID: 0001_tenants
Revises:
Create Date: 2026-05-11
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_tenants"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
          NEW.updated_at = NOW();
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TABLE tenants (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenants;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
