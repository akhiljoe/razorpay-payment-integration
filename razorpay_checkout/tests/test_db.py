"""Tests for the asyncpg pool factory.

Unit tests cover DSN normalisation and the slow-acquire instrumentation.
A live integration test is marked `requires_db` and only runs when
DATABASE_URL points at a reachable Postgres (provided by Docker Compose).
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from razorpay_checkout import logging_config
from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.db import acquire_with_timing, create_pool

# ---------- Unit: DSN normalisation ----------


def test_dsn_normalisation_strips_asyncpg_suffix() -> None:
    from razorpay_checkout.db import _normalise_dsn

    raw = "postgresql+asyncpg://u:p@host:5432/d"
    assert _normalise_dsn(raw) == "postgresql://u:p@host:5432/d"


def test_dsn_normalisation_passes_through_plain() -> None:
    from razorpay_checkout.db import _normalise_dsn

    raw = "postgresql://u:p@host:5432/d"
    assert _normalise_dsn(raw) == raw


# ---------- Unit: slow acquire instrumentation ----------


class _FakeAcquireCM:
    """Async context manager that simulates pool.acquire() with a delay."""

    def __init__(self, delay_s: float, conn: object) -> None:
        self._delay_s = delay_s
        self._conn = conn

    async def __aenter__(self) -> object:
        import asyncio

        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        return self._conn

    async def __aexit__(self, *args: object) -> None:
        return None


def _make_fake_pool(delay_s: float = 0.0) -> MagicMock:
    pool = MagicMock()
    pool.acquire = lambda: _FakeAcquireCM(delay_s, MagicMock(name="conn"))
    pool.get_size = MagicMock(return_value=10)
    pool.get_idle_size = MagicMock(return_value=5)
    return pool


async def test_fast_acquire_does_not_log_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging_config.configure_logging("INFO")
    pool = _make_fake_pool(delay_s=0.0)

    async with acquire_with_timing(pool, slow_acquire_ms=200) as conn:
        assert conn is not None

    captured = capsys.readouterr().out
    for line in captured.splitlines():
        if line.strip():
            record = json.loads(line)
            assert record.get("event") != "slow_pool_acquire"


async def test_slow_acquire_logs_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging_config.configure_logging("INFO")
    pool = _make_fake_pool(delay_s=0.05)  # 50ms

    async with acquire_with_timing(pool, slow_acquire_ms=10) as conn:
        assert conn is not None

    captured = capsys.readouterr().out
    matching = [
        json.loads(line)
        for line in captured.splitlines()
        if line.strip() and '"slow_pool_acquire"' in line
    ]
    assert matching, "expected slow_pool_acquire warning record"
    record = matching[0]
    assert record["level"] == "warning"
    assert record["threshold_ms"] == 10
    assert record["elapsed_ms"] >= 10
    assert record["pool_size"] == 10
    assert record["pool_free"] == 5


# ---------- Integration: live Postgres ----------


_DATABASE_URL = os.environ.get("DATABASE_URL")


@pytest.fixture
def live_config() -> RazorpayConfig:
    if not _DATABASE_URL:
        pytest.skip("DATABASE_URL not set; skipping live DB test")
    return RazorpayConfig(
        tenant_id="t_dbtest",
        key_id="rzp_test_x",
        key_secret="x",
        webhook_secret="x",
        db_dsn=_DATABASE_URL,
        pool_min_size=1,
        pool_max_size=3,
    )


@pytest.mark.requires_db
async def test_create_pool_against_live_postgres(live_config: RazorpayConfig) -> None:
    pool = await create_pool(live_config)
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
            assert result == 1
        assert pool.get_min_size() == 1
        assert pool.get_max_size() == 3
    finally:
        await pool.close()


@pytest.mark.requires_db
async def test_acquire_with_timing_against_live_postgres(
    live_config: RazorpayConfig,
) -> None:
    pool = await create_pool(live_config)
    try:
        async with acquire_with_timing(pool, slow_acquire_ms=5000) as conn:
            result = await conn.fetchval("SELECT 1")
            assert result == 1
    finally:
        await pool.close()
