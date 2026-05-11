"""Tests for the correlation context module."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from razorpay_checkout import correlation, logging_config


@pytest.fixture(autouse=True)
def _reset_correlation() -> None:
    correlation.clear_correlation_id()
    yield
    correlation.clear_correlation_id()


def test_default_correlation_id_is_empty() -> None:
    assert correlation.get_correlation_id() == ""


def test_set_then_get_round_trip() -> None:
    correlation.set_correlation_id("abc-123")
    assert correlation.get_correlation_id() == "abc-123"


def test_new_correlation_id_is_uuid4_format() -> None:
    value = correlation.new_correlation_id()
    pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    assert pattern.match(value), value


async def test_correlation_propagates_through_asyncio_gather() -> None:
    correlation.set_correlation_id("parent-cid")

    async def read() -> str:
        return correlation.get_correlation_id()

    results = await asyncio.gather(read(), read(), read())
    assert results == ["parent-cid"] * 3


async def test_async_child_can_override_independently() -> None:
    correlation.set_correlation_id("outer")

    async def child(label: str) -> str:
        correlation.set_correlation_id(label)
        await asyncio.sleep(0)
        return correlation.get_correlation_id()

    a, b = await asyncio.gather(child("inner-a"), child("inner-b"))
    # Each task has its own copy of the contextvar.
    assert {a, b} == {"inner-a", "inner-b"}


def test_log_record_carries_correlation_id(capsys: pytest.CaptureFixture[str]) -> None:
    logging_config.configure_logging("INFO")
    correlation.set_correlation_id("log-trace-1")
    log = logging_config.get_logger("test")
    log.info("hello", tenant_id="t1", order_id="o1")

    captured = capsys.readouterr().out.strip().splitlines()
    assert captured, "expected at least one log line"
    record = json.loads(captured[-1])
    assert record["event"] == "hello"
    assert record["correlation_id"] == "log-trace-1"
    assert record["tenant_id"] == "t1"
    assert record["order_id"] == "o1"
    assert record["level"] == "info"
    assert "timestamp" in record


def test_secret_redaction_strips_registered_values(capsys: pytest.CaptureFixture[str]) -> None:
    logging_config.configure_logging("INFO")
    logging_config.register_secret("__never_log_this_secret__")
    log = logging_config.get_logger("test")
    log.info(
        "leaky",
        payload={"key_secret": "__never_log_this_secret__"},
        note="contains __never_log_this_secret__ inline",
    )

    out = capsys.readouterr().out
    assert "__never_log_this_secret__" not in out
    assert "***" in out
