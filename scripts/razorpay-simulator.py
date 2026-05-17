#!/usr/bin/env python3
"""Razorpay webhook simulator.

DESIGN_v5 Section 15. Builds a canonical Razorpay payload, signs it with the
dev `webhook_secret`, and POSTs to the webhook URL. Deterministic and
offline-only; the simulator never talks to api.razorpay.com.

Usage:

    python scripts/razorpay-simulator.py <event_type> \
        --tenant <tenant_id> \
        --order-id <razorpay_order_id> \
        --payment-id <razorpay_payment_id> \
        [--amount <paise>] \
        [--refund-id <razorpay_refund_id>] \
        [--secret <webhook_secret>] \
        [--url <target_base_url>] \
        [--event-id <event_id>]

Supported event types: payment.authorized, payment.captured, payment.failed,
refund.processed, order.paid.

The signature contract MUST stay in lockstep with
`razorpay_checkout.signatures.verify_webhook_signature` and the unit tests in
`tests/test_signatures.py`. The canonical vectors live in
`.claude/commands/local-sig-validate.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import uuid
from typing import Any

import httpx

SUPPORTED_EVENTS = (
    "payment.authorized",
    "payment.captured",
    "payment.failed",
    "refund.processed",
    "order.paid",
)

DEFAULT_URL = "http://localhost:8000/payments/webhook"
# Developer-tool default for local-only signing, not a real credential.
# Production never reads from this constant. Pass `--secret` explicitly in any
# integration test that needs a different value.
DEFAULT_SECRET = "dev_webhook_secret"  # noqa: S105
DEFAULT_AMOUNT_PAISE = 50000


def _now_unix() -> int:
    return int(time.time())


def _payment_entity(
    payment_id: str,
    order_id: str,
    amount: int,
    status: str,
    *,
    captured: bool = False,
) -> dict[str, Any]:
    """Subset of Razorpay's `payment.entity` shape needed by the producer.

    The producer reads `payload.payment.entity.order_id` and `.amount`. Other
    fields are present for forensic realism but not consumed by v1.
    """
    return {
        "id": payment_id,
        "entity": "payment",
        "amount": amount,
        "currency": "INR",
        "status": status,
        "order_id": order_id,
        "method": "card",
        "captured": captured,
        "captured_at": _now_unix() if captured else None,
        "created_at": _now_unix(),
    }


def _refund_entity(
    refund_id: str, payment_id: str, order_id: str, amount: int
) -> dict[str, Any]:
    return {
        "id": refund_id,
        "entity": "refund",
        "amount": amount,
        "currency": "INR",
        "payment_id": payment_id,
        "order_id": order_id,
        "status": "processed",
        "created_at": _now_unix(),
    }


def _order_entity(order_id: str, amount: int) -> dict[str, Any]:
    return {
        "id": order_id,
        "entity": "order",
        "amount": amount,
        "amount_paid": amount,
        "amount_due": 0,
        "currency": "INR",
        "status": "paid",
        "created_at": _now_unix(),
    }


def build_payload(
    *,
    event_type: str,
    order_id: str,
    payment_id: str,
    amount: int,
    refund_id: str | None,
    event_id: str,
) -> dict[str, Any]:
    """Build the top-level Razorpay event payload for the given event type.

    Top-level `id` is the event id (consumed by the producer for fast-path dedup).
    Top-level `event` is the event type. `payload.<entity>.entity.*` carries the
    business object.
    """
    if event_type == "payment.authorized":
        status = "authorized"
        captured = False
    elif event_type == "payment.captured":
        status = "captured"
        captured = True
    elif event_type == "payment.failed":
        status = "failed"
        captured = False
    elif event_type in {"refund.processed", "order.paid"}:
        status = "captured"
        captured = True
    else:
        raise ValueError(f"unsupported event_type {event_type!r}")

    base: dict[str, Any] = {
        "id": event_id,
        "entity": "event",
        "event": event_type,
        "created_at": _now_unix(),
    }

    if event_type.startswith("payment."):
        base["contains"] = ["payment"]
        base["payload"] = {
            "payment": {
                "entity": _payment_entity(
                    payment_id=payment_id,
                    order_id=order_id,
                    amount=amount,
                    status=status,
                    captured=captured,
                )
            }
        }
        return base

    if event_type == "refund.processed":
        if refund_id is None:
            raise ValueError("refund.processed requires --refund-id")
        base["contains"] = ["refund", "payment"]
        base["payload"] = {
            "refund": {
                "entity": _refund_entity(
                    refund_id=refund_id,
                    payment_id=payment_id,
                    order_id=order_id,
                    amount=amount,
                )
            },
            "payment": {
                "entity": _payment_entity(
                    payment_id=payment_id,
                    order_id=order_id,
                    amount=amount,
                    status="refunded",
                    captured=True,
                )
            },
        }
        return base

    if event_type == "order.paid":
        base["contains"] = ["order", "payment"]
        base["payload"] = {
            "order": {"entity": _order_entity(order_id=order_id, amount=amount)},
            "payment": {
                "entity": _payment_entity(
                    payment_id=payment_id,
                    order_id=order_id,
                    amount=amount,
                    status="captured",
                    captured=True,
                )
            },
        }
        return base

    raise AssertionError(f"unreachable: {event_type!r}")


def sign_body(body: bytes, secret: str) -> str:
    """HMAC-SHA256(secret, body) hex digest. Mirrors verify_webhook_signature."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def serialise_payload(payload: dict[str, Any]) -> bytes:
    """Compact JSON serialisation. Producer's signature verification uses raw bytes."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sign and POST a canonical Razorpay webhook to a local app."
    )
    parser.add_argument("event_type", choices=SUPPORTED_EVENTS)
    parser.add_argument("--tenant", required=True, help="Tenant id (URL path segment)")
    parser.add_argument("--order-id", required=True, help="Razorpay order id")
    parser.add_argument("--payment-id", required=True, help="Razorpay payment id")
    parser.add_argument(
        "--amount", type=int, default=DEFAULT_AMOUNT_PAISE, help="Amount in paise"
    )
    parser.add_argument(
        "--refund-id",
        default=None,
        help="Razorpay refund id (required for refund.processed)",
    )
    parser.add_argument(
        "--secret",
        default=DEFAULT_SECRET,
        help="Webhook signing secret (must match tenant's webhook_secret)",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="Base URL (tenant is appended). Default localhost:8000",
    )
    parser.add_argument(
        "--event-id",
        default=None,
        help="Event id (random UUIDv4 if omitted)",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Build and sign but do not POST. Prints body and signature to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    event_id = args.event_id or f"evt_{uuid.uuid4().hex[:24]}"

    payload = build_payload(
        event_type=args.event_type,
        order_id=args.order_id,
        payment_id=args.payment_id,
        amount=args.amount,
        refund_id=args.refund_id,
        event_id=event_id,
    )
    body = serialise_payload(payload)
    signature = sign_body(body, args.secret)

    target = f"{args.url.rstrip('/')}/{args.tenant}"

    if args.print_only:
        sys.stdout.write(
            json.dumps(
                {
                    "event_id": event_id,
                    "event_type": args.event_type,
                    "target": target,
                    "signature": signature,
                    "body": payload,
                },
                indent=2,
            )
            + "\n"
        )
        return 0

    response = httpx.post(
        target,
        content=body,
        headers={
            "x-razorpay-signature": signature,
            "content-type": "application/json",
        },
        timeout=10.0,
    )

    sys.stdout.write(
        json.dumps(
            {
                "event_id": event_id,
                "event_type": args.event_type,
                "target": target,
                "status_code": response.status_code,
                "response": _safe_json(response),
            },
            indent=2,
        )
        + "\n"
    )
    return 0 if 200 <= response.status_code < 300 else 1


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


if __name__ == "__main__":
    sys.exit(main())
