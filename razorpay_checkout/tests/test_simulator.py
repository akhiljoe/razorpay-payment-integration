"""Tests for scripts/razorpay-simulator.py.

The simulator is the canonical wire-format producer for our own tests. It must
emit payloads that the production `WebhookProducer` accepts and signatures
that `verify_webhook_signature` validates. Anything else is a drift bug.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest

from razorpay_checkout import (
    RazorpayConfig,
    TenantConfigRegistry,
    WebhookProducer,
)
from razorpay_checkout.signatures import verify_webhook_signature

# ---------- Load the simulator script as a module ----------


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "razorpay-simulator.py"


def _load_simulator() -> Any:
    spec = importlib.util.spec_from_file_location("rzp_simulator", str(_SCRIPT))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load simulator at {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rzp_simulator"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sim() -> Any:
    return _load_simulator()


# ---------- Payload shape ----------


@pytest.mark.parametrize(
    "event_type",
    ["payment.authorized", "payment.captured", "payment.failed"],
)
def test_payment_payload_extracts_order_id_at_expected_path(sim: Any, event_type: str) -> None:
    payload = sim.build_payload(
        event_type=event_type,
        order_id="order_X",
        payment_id="pay_X",
        amount=50000,
        refund_id=None,
        event_id="evt_X",
    )
    assert payload["id"] == "evt_X"
    assert payload["event"] == event_type
    assert payload["payload"]["payment"]["entity"]["order_id"] == "order_X"
    assert payload["payload"]["payment"]["entity"]["amount"] == 50000


def test_refund_payload_extracts_order_id_at_refund_entity(sim: Any) -> None:
    payload = sim.build_payload(
        event_type="refund.processed",
        order_id="order_R",
        payment_id="pay_R",
        amount=50000,
        refund_id="rfnd_R",
        event_id="evt_R",
    )
    assert payload["payload"]["refund"]["entity"]["order_id"] == "order_R"
    assert payload["payload"]["refund"]["entity"]["id"] == "rfnd_R"


def test_order_paid_payload_extracts_order_id_at_order_entity(sim: Any) -> None:
    payload = sim.build_payload(
        event_type="order.paid",
        order_id="order_OP",
        payment_id="pay_OP",
        amount=50000,
        refund_id=None,
        event_id="evt_OP",
    )
    assert payload["payload"]["order"]["entity"]["id"] == "order_OP"


def test_refund_without_refund_id_raises(sim: Any) -> None:
    with pytest.raises(ValueError, match="refund.processed requires --refund-id"):
        sim.build_payload(
            event_type="refund.processed",
            order_id="o",
            payment_id="p",
            amount=1,
            refund_id=None,
            event_id="e",
        )


def test_unsupported_event_type_raises(sim: Any) -> None:
    with pytest.raises(ValueError, match="unsupported event_type"):
        sim.build_payload(
            event_type="not.an.event",
            order_id="o",
            payment_id="p",
            amount=1,
            refund_id=None,
            event_id="e",
        )


# ---------- Signature verification round-trip ----------


@pytest.mark.parametrize(
    "event_type",
    [
        "payment.authorized",
        "payment.captured",
        "payment.failed",
    ],
)
def test_simulator_signature_validates_against_production_verifier(
    sim: Any, event_type: str
) -> None:
    payload = sim.build_payload(
        event_type=event_type,
        order_id="order_S",
        payment_id="pay_S",
        amount=50000,
        refund_id=None,
        event_id="evt_S",
    )
    body = sim.serialise_payload(payload)
    secret = "round_trip_secret"
    signature = sim.sign_body(body, secret)
    assert verify_webhook_signature(body, signature, secret) is True


def test_simulator_signature_round_trip_for_refund(sim: Any) -> None:
    payload = sim.build_payload(
        event_type="refund.processed",
        order_id="order_RT",
        payment_id="pay_RT",
        amount=50000,
        refund_id="rfnd_RT",
        event_id="evt_RT",
    )
    body = sim.serialise_payload(payload)
    signature = sim.sign_body(body, "round_trip_secret")
    assert verify_webhook_signature(body, signature, "round_trip_secret") is True


# ---------- Drift guard: simulator output is accepted by WebhookProducer ----------


WEBHOOK_SECRET = "phase7_simulator_secret"


def _config(tenant_id: str = "t_sim") -> RazorpayConfig:
    return RazorpayConfig(
        tenant_id=tenant_id,
        key_id="rzp_test_kid",
        key_secret="k",
        webhook_secret=WEBHOOK_SECRET,
        db_dsn="postgresql://u:p@localhost:5432/d",
    )


@pytest.mark.parametrize(
    "event_type",
    [
        "payment.authorized",
        "payment.captured",
        "payment.failed",
        "order.paid",
    ],
)
async def test_producer_accepts_simulator_output(sim: Any, event_type: str) -> None:
    """The drift guard. If this fails, simulator and producer have diverged."""
    config = _config()
    stub_sqs = AsyncMock()
    stub_sqs.send_message = AsyncMock(return_value={"MessageId": "msg_DRIFT_GUARD"})
    producer = WebhookProducer(
        tenant_registry=TenantConfigRegistry([config]),
        sqs_client=stub_sqs,
        queue_url="http://localhost:4566/0/q.fifo",
    )

    payload = sim.build_payload(
        event_type=event_type,
        order_id="order_DRIFT",
        payment_id="pay_DRIFT",
        amount=50000,
        refund_id=None,
        event_id=f"evt_{event_type.replace('.', '_')}_drift",
    )
    body = sim.serialise_payload(payload)
    signature = sim.sign_body(body, WEBHOOK_SECRET)

    ack = await producer.process(raw_body=body, header_signature=signature, tenant_id="t_sim")
    assert ack.status == "queued"
    sent = stub_sqs.send_message.await_args.kwargs
    assert sent["MessageGroupId"] == "order_DRIFT"


async def test_producer_accepts_simulator_refund_output(sim: Any) -> None:
    config = _config()
    stub_sqs = AsyncMock()
    stub_sqs.send_message = AsyncMock(return_value={"MessageId": "msg_RF"})
    producer = WebhookProducer(
        tenant_registry=TenantConfigRegistry([config]),
        sqs_client=stub_sqs,
        queue_url="http://localhost:4566/0/q.fifo",
    )

    payload = sim.build_payload(
        event_type="refund.processed",
        order_id="order_RFG",
        payment_id="pay_RFG",
        amount=50000,
        refund_id="rfnd_RFG",
        event_id="evt_refund_RFG",
    )
    body = sim.serialise_payload(payload)
    signature = sim.sign_body(body, WEBHOOK_SECRET)

    ack = await producer.process(raw_body=body, header_signature=signature, tenant_id="t_sim")
    assert ack.status == "queued"


# ---------- CLI argparse smoke ----------


def test_parse_args_minimal(sim: Any) -> None:
    args = sim.parse_args(
        [
            "payment.captured",
            "--tenant",
            "t",
            "--order-id",
            "o",
            "--payment-id",
            "p",
        ]
    )
    assert args.event_type == "payment.captured"
    assert args.tenant == "t"
    assert args.amount == sim.DEFAULT_AMOUNT_PAISE
    assert args.url == sim.DEFAULT_URL


def test_parse_args_rejects_unknown_event_type(sim: Any) -> None:
    with pytest.raises(SystemExit):
        sim.parse_args(
            [
                "not.an.event",
                "--tenant",
                "t",
                "--order-id",
                "o",
                "--payment-id",
                "p",
            ]
        )


def test_print_only_mode_returns_zero_without_http(
    sim: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = sim.main(
        [
            "payment.captured",
            "--tenant",
            "t_print",
            "--order-id",
            "order_PRINT",
            "--payment-id",
            "pay_PRINT",
            "--print-only",
            "--event-id",
            "evt_PRINT",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["event_id"] == "evt_PRINT"
    assert parsed["target"].endswith("/t_print")
    assert parsed["signature"]
    assert parsed["body"]["payload"]["payment"]["entity"]["order_id"] == "order_PRINT"
