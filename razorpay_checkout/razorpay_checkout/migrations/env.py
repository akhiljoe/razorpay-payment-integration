"""Alembic environment for async SQLAlchemy + asyncpg.

Reads the database URL from `DATABASE_URL` (preferred) or
`RAZORPAY_DB_DSN`. Schema is hand-authored in `versions/`, so
`target_metadata = None`.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_db_url() -> str:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("RAZORPAY_DB_DSN")
    if raw is None:
        raw = config.get_main_option("sqlalchemy.url") or ""
    if raw.startswith("postgresql://"):
        raw = "postgresql+asyncpg://" + raw[len("postgresql://"):]
    if not raw:
        raise RuntimeError("DATABASE_URL not set and no fallback sqlalchemy.url configured.")
    return raw


config.set_main_option("sqlalchemy.url", _resolve_db_url())

target_metadata = None


def run_migrations_offline() -> None:
    """Render migrations as SQL without a live DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
