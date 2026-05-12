#!/usr/bin/env python3
"""Local SQS pump that closes the dev loop end to end.

Mimics what the AWS Lambda SQS event-source mapping does in production:

  - Long-polls `webhook-events.fifo`, invokes `WebhookConsumer.process_record`
    on each message body. On success, deletes the message. On
    `ConsumerEnvelopeError` (poison), leaves the message unacked so SQS
    increments receive count and routes to DLQ after `maxReceiveCount=5`.
  - Long-polls `fulfillment-queue.fifo`, invokes
    `FulfillmentProcessor.process_record`. Same delete-on-success policy.

Run from a separate terminal alongside `uvicorn main:app`. The uvicorn
process owns the producer side (HTTP, signature verify, SQS publish); this
pump owns the consumer side. Splitting them mirrors production where the
producer is one Lambda and the consumer/fulfillment are separate ones.

Usage:

    python scripts/local-pump.py
    python scripts/local-pump.py --once             # drain whatever is queued, exit
    python scripts/local-pump.py --only webhook     # webhook-events only
    python scripts/local-pump.py --only fulfillment # fulfillment-queue only

Ctrl-C triggers graceful shutdown: stops polling, drains in-flight tasks,
closes the asyncpg pool and aiohttp session.

DESIGN_v5 Section 6 (failure modes), Section 2.1 (consumer flow),
Section 9 (fulfillment flow). Producer-side stays in `WebhookProducer`;
this script must never sign or verify.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

import aioboto3

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "example" / "backend" / ".env"


def _load_env(path: Path) -> None:
    """Lift `.env` into `os.environ` so aioboto3 sees AWS_* values.

    Same shape as `example/backend/main.py`. setdefault means shell exports
    win when explicitly set.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env(ENV_FILE)

# Import after env lift so RazorpayConfig() finds RAZORPAY_DB_DSN etc.
from razorpay_checkout import (  # noqa: E402
    DefaultFulfillmentHook,
    FixedAmountResolver,
    OrderService,
    RazorpayClient,
    RazorpayConfig,
    configure_logging,
    ensure_fulfillment_queues,
    ensure_webhook_events_queues,
    get_logger,
)
from razorpay_checkout.db import create_pool  # noqa: E402
from razorpay_checkout.fulfillment import FulfillmentProcessor  # noqa: E402
from razorpay_checkout.webhook_consumer import (  # noqa: E402
    ConsumerEnvelopeError,
    WebhookConsumer,
)

_log = get_logger("razorpay_checkout.local_pump")


# ---------- polling loop ----------


async def _drain_loop(
    *,
    name: str,
    sqs: Any,
    queue_url: str,
    handler: Any,  # WebhookConsumer | FulfillmentProcessor (both have process_record)
    stop_event: asyncio.Event,
    one_shot: bool,
) -> None:
    """Long-poll a queue and dispatch each message to `handler.process_record`.

    On success: delete message. On `ConsumerEnvelopeError`: log and leave the
    message — SQS will retry, then route to DLQ after maxReceiveCount=5. On
    any other exception: log and leave the message so SQS retries.
    """
    while not stop_event.is_set():
        try:
            resp = await sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=5 if one_shot else 20,
                MessageAttributeNames=["All"],
                AttributeNames=["All"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.error("pump_receive_failed", pump=name, error=str(exc))
            await asyncio.sleep(2)
            continue

        msgs = resp.get("Messages", [])
        if not msgs:
            if one_shot:
                _log.info("pump_drained", pump=name)
                return
            continue

        for msg in msgs:
            await _process_one(
                name=name,
                sqs=sqs,
                queue_url=queue_url,
                handler=handler,
                msg=msg,
            )

        if one_shot and not msgs:
            return


async def _process_one(
    *,
    name: str,
    sqs: Any,
    queue_url: str,
    handler: Any,
    msg: dict[str, Any],
) -> None:
    message_id = msg.get("MessageId", "?")
    body = msg.get("Body", "")
    attrs = _flatten_message_attributes(msg.get("MessageAttributes"))
    try:
        await handler.process_record(
            message_body=body,
            message_attributes=attrs,
        )
    except ConsumerEnvelopeError as exc:
        # Poison message. Do not delete; let SQS retry until DLQ.
        _log.warning(
            "pump_poison_message",
            pump=name,
            message_id=message_id,
            error=str(exc),
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Transient or unhandled. Do not delete; SQS will retry.
        _log.error(
            "pump_handler_error",
            pump=name,
            message_id=message_id,
            error=str(exc),
        )
        return

    try:
        await sqs.delete_message(
            QueueUrl=queue_url,
            ReceiptHandle=msg["ReceiptHandle"],
        )
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "pump_delete_failed",
            pump=name,
            message_id=message_id,
            error=str(exc),
        )


def _flatten_message_attributes(
    raw: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Convert SQS MessageAttributes shape to the `{name: value}` shape that
    `process_record(message_attributes=...)` expects."""
    if not raw:
        return None
    flat: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            flat[k] = v.get("StringValue") or v.get("BinaryValue") or v.get("Value")
        else:
            flat[k] = v
    return flat


# ---------- bootstrap ----------


async def _main(args: argparse.Namespace) -> int:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    config = RazorpayConfig()  # type: ignore[call-arg]
    _log.info("pump_starting", tenant_id=config.tenant_id, only=args.only or "all")

    pool = await create_pool(config)
    rzp_client = RazorpayClient(config)
    service = OrderService(
        pool=pool,
        config=config,
        client=rzp_client,
        amount_resolver=FixedAmountResolver(amount=50000),  # unused by pump
    )

    session = aioboto3.Session()
    sqs_cm = session.client(
        "sqs",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-south-1"),
    )
    sqs = await sqs_cm.__aenter__()

    try:
        webhook_url, _ = await ensure_webhook_events_queues(sqs)
        fulfillment_url, _ = await ensure_fulfillment_queues(sqs)

        consumer = WebhookConsumer(
            pool=pool,
            order_service=service,
            sqs_client=sqs,
            fulfillment_queue_url=fulfillment_url,
        )
        fulfiller = FulfillmentProcessor(
            pool=pool,
            order_service=service,
            hook=DefaultFulfillmentHook(),
        )

        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)

        tasks: list[asyncio.Task[None]] = []
        if args.only in (None, "webhook"):
            tasks.append(
                asyncio.create_task(
                    _drain_loop(
                        name="webhook",
                        sqs=sqs,
                        queue_url=webhook_url,
                        handler=consumer,
                        stop_event=stop_event,
                        one_shot=args.once,
                    ),
                    name="webhook-pump",
                )
            )
        if args.only in (None, "fulfillment"):
            tasks.append(
                asyncio.create_task(
                    _drain_loop(
                        name="fulfillment",
                        sqs=sqs,
                        queue_url=fulfillment_url,
                        handler=fulfiller,
                        stop_event=stop_event,
                        one_shot=args.once,
                    ),
                    name="fulfillment-pump",
                )
            )

        _log.info("pump_ready", queues=[t.get_name() for t in tasks])
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        _log.info("pump_stopped")
    finally:
        await pool.close()
        await rzp_client.aclose()
        await sqs_cm.__aexit__(None, None, None)
    return 0


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGINT/SIGTERM to flip the stop event. Windows: only SIGINT works."""

    def _set_stop() -> None:
        if not stop_event.is_set():
            _log.info("pump_stop_signal_received")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            # Windows ProactorEventLoop does not support add_signal_handler for
            # SIGTERM. SIGINT is handled by the default KeyboardInterrupt path.
            signal.signal(sig, lambda *_: _set_stop())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--once",
        action="store_true",
        help="Drain whatever is currently queued (5s long-poll) and exit",
    )
    p.add_argument(
        "--only",
        choices=("webhook", "fulfillment"),
        default=None,
        help="Run a single pump rather than both",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
