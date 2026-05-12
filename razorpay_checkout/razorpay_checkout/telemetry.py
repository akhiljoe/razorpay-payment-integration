"""OpenTelemetry bootstrap and metric instruments.

DESIGN_v5 Section 10 (observability) and Section 13 (metrics). One exporter
selected at startup from `RazorpayConfig.otel_exporter` (`stdout`, `xray`,
`otlp`). No downstream branching on the exporter; modules import the
instruments by name and call them unconditionally.

Free Tier custom metric cap is 10. We register exactly 10 here. The two
metrics dropped (`payment_authorized_count`, `tenant_lookup_failures`) are
recoverable via CloudWatch Logs metric filters on the respective WARN/INFO
log lines if needed.

Per-emit ownership:

| Metric                             | Owner                       |
|------------------------------------|-----------------------------|
| webhook_latency_ms                 | Webhook Producer            |
| webhook_publish_failures           | Webhook Producer            |
| webhook_duplicate_count            | Webhook Consumer            |
| webhook_consumer_duration_ms       | Webhook Consumer            |
| order_state_transition_count       | OrderService.transition     |
| reconciliation_mismatch_count      | reconcile.reconcile         |
| dead_event_count                   | Webhook DLQ poller          |
| fulfillment_duration_ms            | FulfillmentProcessor        |
| fulfillment_sqs_publish_failures   | Webhook Consumer (publish)  |
| cold_start_ms                      | Lambda entry shims          |
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

from razorpay_checkout.correlation import get_correlation_id
from razorpay_checkout.logging_config import get_logger

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics.export import MetricReader

    from razorpay_checkout.config import RazorpayConfig

_log = get_logger("razorpay_checkout.telemetry")

SERVICE_NAME = "razorpay-checkout"

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def configure_otel(
    config: RazorpayConfig,
    *,
    metric_readers: list[MetricReader] | None = None,
) -> tuple[TracerProvider, MeterProvider]:
    """Initialize OTel tracer and meter providers.

    `metric_readers` allows tests to inject `InMemoryMetricReader`. Production
    uses the exporter selected by `config.otel_exporter`:

    - `stdout` (default): `ConsoleSpanExporter` for spans, `ConsoleMetricExporter`
      via a periodic reader (5 s interval). Adequate for Free Track.
    - `xray`: AWS X-Ray via the ADOT Lambda layer. The layer is responsible
      for the actual exporter at runtime; we just emit through the standard
      OTel API.
    - `otlp`: `OTLPSpanExporter` to `config.otel_endpoint`. Future / custom.

    Returns `(tracer_provider, meter_provider)`. Both are also registered as
    the global providers so `trace.get_tracer(...)` and `metrics.get_meter(...)`
    pick them up.
    """
    global _tracer_provider, _meter_provider

    resource = Resource.create(
        {"service.name": SERVICE_NAME, "tenant.id": config.tenant_id}
    )

    # ---------- Tracer ----------
    tracer_provider = TracerProvider(resource=resource)
    if config.otel_exporter == "stdout":
        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif config.otel_exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        endpoint = config.otel_endpoint or "http://localhost:4318/v1/traces"
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
    # xray: emit via ADOT layer at runtime; nothing to add here.

    # Decorate every span with the contextvar correlation_id at start.
    tracer_provider.add_span_processor(_CorrelationIdSpanProcessor())
    trace.set_tracer_provider(tracer_provider)
    _tracer_provider = tracer_provider

    # ---------- Meter ----------
    readers: list[MetricReader] = list(metric_readers) if metric_readers else []
    if not readers:
        if config.otel_exporter == "stdout":
            readers.append(
                PeriodicExportingMetricReader(
                    ConsoleMetricExporter(), export_interval_millis=5_000
                )
            )
        # xray and otlp: hosts provide their own reader. No-op here.

    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(meter_provider)
    _meter_provider = meter_provider

    # Re-bind instruments to the new meter so subsequent emits land in the
    # new reader. Pass the explicit provider — OTel's `set_meter_provider`
    # is set-once and ignores re-configuration after the first call.
    _rebind_instruments(meter_provider)

    _log.info(
        "otel_configured",
        exporter=config.otel_exporter,
        service_name=SERVICE_NAME,
        tenant_id=config.tenant_id,
    )
    return tracer_provider, meter_provider


# ---------- Span processor that injects correlation_id ----------


class _CorrelationIdSpanProcessor(SpanProcessor):
    """Attach the current contextvar `correlation_id` to every started span."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:  # noqa: ARG002
        cid = get_correlation_id()
        if cid:
            span.set_attribute("correlation_id", cid)

    def on_end(self, span: Any) -> None:  # noqa: ARG002
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        return True


# ---------- Metric instruments (exactly 10) ----------

_meter = metrics.get_meter("razorpay_checkout")


def _rebind_instruments(meter_provider: MeterProvider | None = None) -> None:
    """Recreate instruments against `meter_provider` (or the global if None).

    Binding to an explicit provider sidesteps OTel's set-once global, which
    matters for tests that re-`configure_otel` per fixture with different
    `InMemoryMetricReader`s.
    """
    global _meter, webhook_latency_ms, webhook_publish_failures
    global webhook_duplicate_count, webhook_consumer_duration_ms
    global order_state_transition_count, reconciliation_mismatch_count
    global dead_event_count, fulfillment_duration_ms
    global fulfillment_sqs_publish_failures, cold_start_ms

    if meter_provider is None:
        _meter = metrics.get_meter("razorpay_checkout")
    else:
        _meter = meter_provider.get_meter("razorpay_checkout")

    webhook_latency_ms = _meter.create_histogram(
        "webhook_latency_ms",
        unit="ms",
        description="Webhook Producer HTTP entry to 200 response (ms).",
    )
    webhook_publish_failures = _meter.create_counter(
        "webhook_publish_failures",
        description="Webhook Producer SQS SendMessage errors.",
    )
    webhook_duplicate_count = _meter.create_counter(
        "webhook_duplicate_count",
        description="Consumer fast-path or primary-guard duplicate hits.",
    )
    webhook_consumer_duration_ms = _meter.create_histogram(
        "webhook_consumer_duration_ms",
        unit="ms",
        description="Consumer record receive-to-ack duration (ms).",
    )
    order_state_transition_count = _meter.create_counter(
        "order_state_transition_count",
        description="Counts every OrderService.transition by (from, to) labels.",
    )
    reconciliation_mismatch_count = _meter.create_counter(
        "reconciliation_mismatch_count",
        description="Counts each ReconcileMismatch by reason label.",
    )
    dead_event_count = _meter.create_counter(
        "dead_event_count",
        description="Webhook DLQ poller marked a webhook_events row dead.",
    )
    fulfillment_duration_ms = _meter.create_histogram(
        "fulfillment_duration_ms",
        unit="ms",
        description="FulfillmentProcessor record receive-to-ack duration (ms).",
    )
    fulfillment_sqs_publish_failures = _meter.create_counter(
        "fulfillment_sqs_publish_failures",
        description="Webhook Consumer publish to fulfillment-queue.fifo errors.",
    )
    cold_start_ms = _meter.create_histogram(
        "cold_start_ms",
        unit="ms",
        description="Lambda container cold-start init duration (ms).",
    )


# Initialize at import time so emitting modules can import directly.
webhook_latency_ms: Any
webhook_publish_failures: Any
webhook_duplicate_count: Any
webhook_consumer_duration_ms: Any
order_state_transition_count: Any
reconciliation_mismatch_count: Any
dead_event_count: Any
fulfillment_duration_ms: Any
fulfillment_sqs_publish_failures: Any
cold_start_ms: Any

_rebind_instruments()
