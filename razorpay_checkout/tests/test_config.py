"""Tests for RazorpayConfig."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from razorpay_checkout import logging_config
from razorpay_checkout.config import RazorpayConfig


@pytest.fixture
def base_kwargs() -> dict[str, object]:
    return {
        "tenant_id": "t_test",
        "key_id": "rzp_test_xxx",
        "key_secret": "__test_key_secret_value__",
        "webhook_secret": "__test_webhook_secret_value__",
        "db_dsn": "postgresql://u:p@localhost:5432/d",
    }


def test_config_constructs_with_required_fields(base_kwargs: dict[str, object]) -> None:
    config = RazorpayConfig(**base_kwargs)
    assert config.tenant_id == "t_test"
    assert config.key_id == "rzp_test_xxx"
    assert isinstance(config.key_secret, SecretStr)
    assert isinstance(config.webhook_secret, SecretStr)
    assert config.mode == "test"
    assert config.pool_min_size == 2
    assert config.pool_max_size == 10
    assert config.otel_exporter == "stdout"


def test_secret_values_never_appear_in_repr(base_kwargs: dict[str, object]) -> None:
    config = RazorpayConfig(**base_kwargs)
    rendered = repr(config)
    assert "__test_key_secret_value__" not in rendered
    assert "__test_webhook_secret_value__" not in rendered
    assert "**********" in rendered


def test_secret_values_never_appear_in_model_dump(base_kwargs: dict[str, object]) -> None:
    config = RazorpayConfig(**base_kwargs)
    dump = config.model_dump()
    assert "__test_key_secret_value__" not in str(dump)
    assert "__test_webhook_secret_value__" not in str(dump)


def test_secrets_get_registered_for_log_redaction(
    base_kwargs: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging_config.configure_logging("INFO")
    RazorpayConfig(**base_kwargs)
    log = logging_config.get_logger("test_config")
    log.info(
        "echo",
        leak_a="__test_key_secret_value__",
        leak_b="__test_webhook_secret_value__",
    )
    captured = capsys.readouterr().out
    assert "__test_key_secret_value__" not in captured
    assert "__test_webhook_secret_value__" not in captured
    assert "***" in captured


def test_admin_api_key_optional(base_kwargs: dict[str, object]) -> None:
    config = RazorpayConfig(**base_kwargs)
    assert config.admin_api_key is None


def test_webhook_secret_previous_optional(base_kwargs: dict[str, object]) -> None:
    base_kwargs["webhook_secret_previous"] = "__previous_secret__"
    config = RazorpayConfig(**base_kwargs)
    assert config.webhook_secret_previous is not None
    assert config.webhook_secret_previous.get_secret_value() == "__previous_secret__"


def test_pool_sizes_must_be_at_least_one(base_kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RazorpayConfig(**base_kwargs, pool_min_size=0)


def test_invalid_otel_exporter_rejected(base_kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RazorpayConfig(**base_kwargs, otel_exporter="invalid")
