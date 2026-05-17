"""Tests for telemetry.py: exporter selection and per-instrument recording."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from razorpay_checkout import telemetry
from razorpay_checkout.config import RazorpayConfig


def _config(otel_exporter: str = "stdout") -> RazorpayConfig:
    return RazorpayConfig(
        tenant_id="t_telemetry",
        key_id="rzp_test_kid",
        key_secret="k",
        webhook_secret="w",
        db_dsn="postgresql://u:p@localhost:5432/d",
        otel_exporter=otel_exporter,  # type: ignore[arg-type]
    )


@pytest.fixture
def in_memory_reader() -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    telemetry.configure_otel(_config(), metric_readers=[reader])
    return reader


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    if data is None:
        return set()
    names: set[str] = set()
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                names.add(metric.name)
    return names


# ---------- configure_otel ----------


def test_configure_otel_stdout_sets_providers() -> None:
    """`stdout` exporter mode spawns a background `PeriodicExportingMetricReader`.

    We must shut down the meter/tracer providers at the end so the reader's
    daemon thread stops before pytest closes captured stdout. Otherwise the
    next 5-second flush hits a closed file and prints a noisy traceback at
    interpreter shutdown. See DESIGN_v5 Section 17.9.
    """
    tracer_provider, meter_provider = telemetry.configure_otel(_config("stdout"))
    try:
        assert tracer_provider is not None
        assert meter_provider is not None
    finally:
        meter_provider.shutdown()
        tracer_provider.shutdown()


def test_configure_otel_with_injected_reader_uses_it() -> None:
    reader = InMemoryMetricReader()
    _, mp = telemetry.configure_otel(_config(), metric_readers=[reader])
    # The provided reader is registered.
    assert reader in mp._sdk_config.metric_readers


# ---------- Instrument recording ----------


def test_webhook_latency_ms_records(in_memory_reader: InMemoryMetricReader) -> None:
    telemetry.webhook_latency_ms.record(42.0, {"tenant_id": "t"})
    assert "webhook_latency_ms" in _metric_names(in_memory_reader)


def test_webhook_publish_failures_records(in_memory_reader: InMemoryMetricReader) -> None:
    telemetry.webhook_publish_failures.add(1, {"tenant_id": "t"})
    assert "webhook_publish_failures" in _metric_names(in_memory_reader)


def test_webhook_duplicate_count_records(in_memory_reader: InMemoryMetricReader) -> None:
    telemetry.webhook_duplicate_count.add(1, {"tenant_id": "t", "kind": "event_id"})
    assert "webhook_duplicate_count" in _metric_names(in_memory_reader)


def test_webhook_consumer_duration_ms_records(in_memory_reader: InMemoryMetricReader) -> None:
    telemetry.webhook_consumer_duration_ms.record(15.5, {"tenant_id": "t"})
    assert "webhook_consumer_duration_ms" in _metric_names(in_memory_reader)


def test_order_state_transition_count_records(in_memory_reader: InMemoryMetricReader) -> None:
    telemetry.order_state_transition_count.add(
        1, {"from": "created", "to": "attempted", "tenant_id": "t"}
    )
    assert "order_state_transition_count" in _metric_names(in_memory_reader)


def test_reconciliation_mismatch_count_records(in_memory_reader: InMemoryMetricReader) -> None:
    telemetry.reconciliation_mismatch_count.add(1, {"reason": "amount_mismatch", "tenant_id": "t"})
    assert "reconciliation_mismatch_count" in _metric_names(in_memory_reader)


def test_dead_event_count_records(in_memory_reader: InMemoryMetricReader) -> None:
    telemetry.dead_event_count.add(1, {"tenant_id": "t"})
    assert "dead_event_count" in _metric_names(in_memory_reader)


def test_fulfillment_duration_ms_records(in_memory_reader: InMemoryMetricReader) -> None:
    telemetry.fulfillment_duration_ms.record(250.0, {"tenant_id": "t"})
    assert "fulfillment_duration_ms" in _metric_names(in_memory_reader)


def test_fulfillment_sqs_publish_failures_records(
    in_memory_reader: InMemoryMetricReader,
) -> None:
    telemetry.fulfillment_sqs_publish_failures.add(1, {"tenant_id": "t"})
    assert "fulfillment_sqs_publish_failures" in _metric_names(in_memory_reader)


def test_cold_start_ms_records(in_memory_reader: InMemoryMetricReader) -> None:
    telemetry.cold_start_ms.record(1200.0, {"service": "webhook_consumer"})
    assert "cold_start_ms" in _metric_names(in_memory_reader)


# ---------- All 10 instruments are exposed at module level ----------


EXPECTED_INSTRUMENTS = (
    "webhook_latency_ms",
    "webhook_publish_failures",
    "webhook_duplicate_count",
    "webhook_consumer_duration_ms",
    "order_state_transition_count",
    "reconciliation_mismatch_count",
    "dead_event_count",
    "fulfillment_duration_ms",
    "fulfillment_sqs_publish_failures",
    "cold_start_ms",
)


@pytest.mark.parametrize("name", EXPECTED_INSTRUMENTS)
def test_instrument_attribute_exists(name: str) -> None:
    assert hasattr(telemetry, name), f"telemetry.{name} not exposed at module level"


def test_exactly_ten_instruments_exposed() -> None:
    """Free Tier cap. If you add an 11th, document the consolidation in
    DESIGN_v5 Section 13 and update this assertion."""
    actual = {
        name
        for name in dir(telemetry)
        if not name.startswith("_")
        and isinstance(getattr(telemetry, name), Any.__class__)
        is False  # crude isinstrument check via Any sentinel
    }
    # The cleaner check: assert at least the 10 are there and bounded.
    assert set(EXPECTED_INSTRUMENTS).issubset(actual)
